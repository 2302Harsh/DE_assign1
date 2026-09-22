"""Augment DC fast chargers with pricing, plug and operator data from Open Charge Map.

Run with: python 03_data_augmentation.py [--refresh]

Instead of one API call per charger, this downloads every Open Charge Map (OCM)
point of interest for Australia once, caches the raw JSON in data/raw/, and
matches offline. A rerun therefore needs no API key or network access; pass
--refresh to download a fresh copy.

Matching strategy (each DC charger gets the best candidate, or none):
  1. 'operator_and_distance': the nearest OCM site with a DC connector within
     500 m whose canonical operator equals the charger's.
  2. 'distance_only': otherwise, the nearest OCM site with a DC connector within
     100 m, whatever its operator.
  3. 'none': no candidate qualified.
Wider radii were tested and rejected: beyond ~500 m same-operator candidates are
frequently a different site (README.md has the radius/coverage trade-off).
Every DC row stores match_method and match_distance_m. Non-DC rows were never
matched, so their augmentation columns (including match_method) are NULL.
"""

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

from common import (
    AUGMENTED_PATH, CLEANED_PATH, OCM_CACHE_PATH, PROCESSED_DTYPES, PROJECT_DIR,
    PROJECTED_CRS, canonical_operator,
)


OCM_API_URL = "https://api.openchargemap.io/v3/poi/"
# See the matching-strategy note in the module docstring for how these two
# radii are used, and README.md for the coverage/precision trade-off behind
# the chosen values.
OPERATOR_MATCH_RADIUS_M = 500
DISTANCE_ONLY_RADIUS_M = 100
# A connector "Title" starting with any of these is a DC connector even when
# Open Charge Map's own CurrentType field (checked first) is missing.
DC_CONNECTOR_PREFIXES = ("CCS", "CHAdeMO", "Tesla")

# Matches "$0.55/kWh", "AUD 0.42/kWh", "55c/kWh" and similar - see
# parse_price_per_kwh() below for how the two alternatives are used.
PRICE_PER_KWH = re.compile(
    r"(?:\$|aud)\s*(\d+(?:\.\d+)?)\s*(?:/|per)\s*kwh"  # $0.55/kWh, AUD 0.42/kWh
    r"|(\d+(?:\.\d+)?)\s*c(?:ents?)?\s*(?:/|per)\s*kwh",  # 55c/kWh
    re.IGNORECASE,
)
PRICE_RANGE = re.compile(r"\d\s*c?\s*[-–]\s*\$?\d")  # "59c-68c/kWh": the price varies
FREE = re.compile(r"free( to use| charging)?|no charge|0(\.0+)?", re.IGNORECASE)

# Columns copied from a matched Open Charge Map site into the output.
POI_COLUMNS = [
    "ocm_poi_id", "ocm_operator", "usage_cost", "price_per_kwh", "plug_types",
    "num_points", "num_connectors",
]
# Columns describing *how* a charger was matched (or that it wasn't).
MATCH_COLUMNS = ["match_method", "match_distance_m"]


def get_api_key() -> str:
    """Read OPENCHARGEMAP_API_KEY from .env, raising a clear error if it's missing.

    Only called when there's no cached data to fall back on (see load_ocm_pois).
    """
    load_dotenv(PROJECT_DIR / ".env")
    key = os.environ.get("OPENCHARGEMAP_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENCHARGEMAP_API_KEY is not set. Copy .env.example to .env and add your "
            "Open Charge Map key (or run with a cached data/raw/ocm_au_pois.json)."
        )
    return key


def load_ocm_pois(refresh: bool) -> list[dict]:
    """Return every Australian OCM POI, downloading only if there is no local cache.

    "POI" (point of interest) is Open Charge Map's term for one charging site,
    which can have several individual connectors/plugs.
    """
    if OCM_CACHE_PATH.exists() and not refresh:
        print(f"Using cached Open Charge Map data: {OCM_CACHE_PATH}")
        return json.loads(OCM_CACHE_PATH.read_text(encoding="utf-8"))

    print("Downloading all Australian Open Charge Map sites...")
    params = {
        "output": "json", "countrycode": "AU", "maxresults": 100000,
        "compact": "false", "verbose": "false", "key": get_api_key(),
    }
    response = requests.get(OCM_API_URL, params=params, timeout=300)
    response.raise_for_status()  # raise if the API returned an HTTP error status
    pois = response.json()

    # Same atomic-write pattern as 01_download_data.py: write to a temp file
    # first, then rename, so an interrupted download never leaves a corrupt
    # (partially written) cache file that a later run would trust and load.
    OCM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                     dir=OCM_CACHE_PATH.parent, suffix=".part") as tmp:
        json.dump(pois, tmp)
    Path(tmp.name).replace(OCM_CACHE_PATH)
    print(f"Cached {len(pois)} sites in {OCM_CACHE_PATH}")
    return pois


def parse_price_per_kwh(text) -> float | None:
    """Parse a per-kWh price in AUD from free-text OCM UsageCost.

    "FREE" -> 0.0; "55c/kWh" and "$0.55/kWh" -> 0.55. Returns None when there is no
    per-kWh price, when the text quotes a range, or when it quotes several
    different per-kWh prices (the raw text is kept in usage_cost).
    """
    if pd.isna(text) or not str(text).strip():
        return None
    text = str(text).strip()
    if FREE.fullmatch(text):
        return 0.0
    if PRICE_RANGE.search(text):
        # e.g. "$0.45 - $0.60/kWh": there's no single price to report, so
        # leave it NULL rather than arbitrarily picking one end of the range.
        return None
    # PRICE_PER_KWH has two alternative groups: (dollars) for "$0.55/kWh" style
    # and (cents) for "55c/kWh" style. Exactly one of the two is non-empty in
    # each match, so `dollars` decides which conversion to apply. A set (not a
    # list) is used so that if the same price appears twice in the text it
    # still counts once; if the text names two *different* prices (e.g. peak
    # and off-peak tariffs), len() below will be > 1 and we bail out.
    prices = {
        float(dollars) if dollars else float(cents) / 100
        for dollars, cents in PRICE_PER_KWH.findall(text)
    }
    return prices.pop() if len(prices) == 1 else None


def flatten_poi(poi: dict) -> dict:
    """Reduce one nested Open Charge Map POI record to a flat dict of the fields we need."""
    connections = poi.get("Connections") or []
    # Each connection has a ConnectionType with a human-readable Title, e.g.
    # "CCS (Type 2)" or "CHAdeMO". dict.fromkeys() deduplicates while
    # preserving the order the plug types first appear in (a plain set()
    # would work too but wouldn't keep a stable order).
    titles = [(c.get("ConnectionType") or {}).get("Title", "").strip() for c in connections]
    plug_types = [t for t in dict.fromkeys(titles) if t and t != "Unknown"]
    # A site counts as DC-capable if any connection is explicitly tagged DC,
    # or - as a fallback for connections missing that tag - if its plug type
    # name starts with a known DC-only connector family (CCS, CHAdeMO, Tesla).
    has_dc = any((c.get("CurrentType") or {}).get("Title") == "DC" for c in connections) or any(
        t.startswith(DC_CONNECTOR_PREFIXES) for t in titles
    )
    usage_cost = (poi.get("UsageCost") or "").strip() or None
    address = poi["AddressInfo"]
    return {
        "ocm_poi_id": poi["ID"],
        "ocm_operator": canonical_operator((poi.get("OperatorInfo") or {}).get("Title")),
        "usage_cost": usage_cost,
        "price_per_kwh": parse_price_per_kwh(usage_cost),
        "plug_types": ", ".join(plug_types) or None,
        # NumberOfPoints is OCM's count of charging bays; Quantity (default 1) counts
        # connectors per connection record, so the two can legitimately differ.
        "num_points": poi.get("NumberOfPoints"),
        "num_connectors": sum(c.get("Quantity") or 1 for c in connections),
        "has_dc": has_dc,
        "ocm_latitude": address.get("Latitude"),
        "ocm_longitude": address.get("Longitude"),
    }


def to_projected_points(df: pd.DataFrame, lon: str, lat: str) -> gpd.GeoDataFrame:
    """Turn lon/lat columns into point geometry in PROJECTED_CRS, with x/y columns.

    The x/y columns (metres) let match_dc_chargers compute plain Euclidean
    distances with numpy afterwards, which is simpler and faster than calling
    a geometry .distance() method per pair.
    """
    points = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df[lon], df[lat]), crs="EPSG:4326"
    ).to_crs(PROJECTED_CRS)
    points["x"], points["y"] = points.geometry.x, points.geometry.y
    return points


def match_dc_chargers(dc: pd.DataFrame, pois: pd.DataFrame) -> pd.DataFrame:
    """Return one row per DC charger with its best OCM match (see module docstring)."""
    chargers = to_projected_points(dc[["charger_id", "operator", "longitude", "latitude"]],
                                   "longitude", "latitude")[["charger_id", "operator", "x", "y", "geometry"]]
    sites = pois[pois["has_dc"]].dropna(subset=["ocm_latitude", "ocm_longitude"])
    sites = to_projected_points(sites, "ocm_longitude", "ocm_latitude").rename(
        columns={"x": "site_x", "y": "site_y"})

    # sjoin with predicate="dwithin" pairs every charger with every OCM site
    # within OPERATOR_MATCH_RADIUS_M of it (the wider of the two radii we
    # care about) - one output row per (charger, nearby site) pair. This is a
    # spatial equivalent of a SQL join on "distance(a, b) <= radius" instead
    # of an equality condition.
    pairs = gpd.sjoin(chargers, sites, predicate="dwithin", distance=OPERATOR_MATCH_RADIUS_M)
    # Exact straight-line distance in metres between each charger/site pair,
    # using the x/y (already in the metric PROJECTED_CRS) computed above.
    pairs["match_distance_m"] = np.hypot(pairs["x"] - pairs["site_x"], pairs["y"] - pairs["site_y"]).round(1)
    pairs["_same_operator"] = pairs["operator"] == pairs["ocm_operator"]
    # Keep only pairs that satisfy one of the two matching rules: same
    # operator within the (wider) operator radius, or any operator within the
    # (narrower) distance-only radius.
    candidates = pairs[pairs["_same_operator"] | (pairs["match_distance_m"] <= DISTANCE_ONLY_RADIUS_M)].copy()
    candidates["match_method"] = np.where(
        candidates["_same_operator"], "operator_and_distance", "distance_only"
    )
    # For each charger, prefer a same-operator match over a distance-only one
    # (ascending=False on _same_operator puts True first), and among those of
    # the same method prefer the closest one. drop_duplicates then keeps just
    # that first (= best) row per charger_id.
    best = candidates.sort_values(
        ["charger_id", "_same_operator", "match_distance_m"], ascending=[True, False, True]
    ).drop_duplicates("charger_id")
    matched = best[["charger_id", *POI_COLUMNS, *MATCH_COLUMNS]]
    # Left-merge so every DC charger appears exactly once, whether or not it
    # matched. validate="one_to_one" makes pandas raise if that assumption is
    # ever violated (e.g. a bug lets a charger_id match more than one site).
    result = dc[["charger_id"]].merge(matched, on="charger_id", how="left", validate="one_to_one")
    result["match_method"] = result["match_method"].fillna("none")  # unmatched chargers
    # Int64 (nullable integer) keeps these whole-number columns from being
    # written to CSV as "12.0" the way an ordinary float column would once it
    # contains any missing values.
    count_columns = ["ocm_poi_id", "num_points", "num_connectors"]
    result[count_columns] = result[count_columns].astype("Int64")  # else written as 12.0
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true",
                        help="re-download the Open Charge Map data even if it is cached")
    args = parser.parse_args()

    df = pd.read_csv(CLEANED_PATH, dtype=PROCESSED_DTYPES)
    pois = pd.DataFrame([flatten_poi(p) for p in load_ocm_pois(args.refresh)])
    dc = df[df["charger_type"] == "DC"]  # only DC fast chargers are augmented (see brief)
    print(f"Matching {len(dc)} DC chargers against {int(pois['has_dc'].sum())} DC-capable OCM sites...")

    matches = match_dc_chargers(dc, pois)
    counts = matches["match_method"].value_counts()
    matched = len(matches) - counts.get("none", 0)
    print(counts.to_dict())
    print(f"Coverage: {matched}/{len(matches)} DC chargers augmented ({matched / len(matches):.1%})")

    # Left-merge the match results back onto every charger (AC, DC and
    # upcoming); non-DC rows simply get NULLs in every augmentation column,
    # including match_method, showing they were never attempted.
    merged = df.merge(matches, on="charger_id", how="left", validate="one_to_one")
    merged.to_csv(AUGMENTED_PATH, index=False)
    print(f"Saved augmented dataset to {AUGMENTED_PATH}")


if __name__ == "__main__":
    main()
