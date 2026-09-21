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
OPERATOR_MATCH_RADIUS_M = 500
DISTANCE_ONLY_RADIUS_M = 100
DC_CONNECTOR_PREFIXES = ("CCS", "CHAdeMO", "Tesla")

PRICE_PER_KWH = re.compile(
    r"(?:\$|aud)\s*(\d+(?:\.\d+)?)\s*(?:/|per)\s*kwh"  # $0.55/kWh, AUD 0.42/kWh
    r"|(\d+(?:\.\d+)?)\s*c(?:ents?)?\s*(?:/|per)\s*kwh",  # 55c/kWh
    re.IGNORECASE,
)
PRICE_RANGE = re.compile(r"\d\s*c?\s*[-–]\s*\$?\d")  # "59c-68c/kWh": the price varies
FREE = re.compile(r"free( to use| charging)?|no charge|0(\.0+)?", re.IGNORECASE)

POI_COLUMNS = [
    "ocm_poi_id", "ocm_operator", "usage_cost", "price_per_kwh", "plug_types",
    "num_points", "num_connectors",
]
MATCH_COLUMNS = ["match_method", "match_distance_m"]


def get_api_key() -> str:
    load_dotenv(PROJECT_DIR / ".env")
    key = os.environ.get("OPENCHARGEMAP_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENCHARGEMAP_API_KEY is not set. Copy .env.example to .env and add your "
            "Open Charge Map key (or run with a cached data/raw/ocm_au_pois.json)."
        )
    return key


def load_ocm_pois(refresh: bool) -> list[dict]:
    """Return every Australian OCM POI, downloading only if there is no local cache."""
    if OCM_CACHE_PATH.exists() and not refresh:
        print(f"Using cached Open Charge Map data: {OCM_CACHE_PATH}")
        return json.loads(OCM_CACHE_PATH.read_text(encoding="utf-8"))

    print("Downloading all Australian Open Charge Map sites...")
    params = {
        "output": "json", "countrycode": "AU", "maxresults": 100000,
        "compact": "false", "verbose": "false", "key": get_api_key(),
    }
    response = requests.get(OCM_API_URL, params=params, timeout=300)
    response.raise_for_status()
    pois = response.json()

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
        return None
    prices = {
        float(dollars) if dollars else float(cents) / 100
        for dollars, cents in PRICE_PER_KWH.findall(text)
    }
    return prices.pop() if len(prices) == 1 else None


def flatten_poi(poi: dict) -> dict:
    connections = poi.get("Connections") or []
    titles = [(c.get("ConnectionType") or {}).get("Title", "").strip() for c in connections]
    plug_types = [t for t in dict.fromkeys(titles) if t and t != "Unknown"]
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

    pairs = gpd.sjoin(chargers, sites, predicate="dwithin", distance=OPERATOR_MATCH_RADIUS_M)
    pairs["match_distance_m"] = np.hypot(pairs["x"] - pairs["site_x"], pairs["y"] - pairs["site_y"]).round(1)
    pairs["_same_operator"] = pairs["operator"] == pairs["ocm_operator"]
    candidates = pairs[pairs["_same_operator"] | (pairs["match_distance_m"] <= DISTANCE_ONLY_RADIUS_M)].copy()
    candidates["match_method"] = np.where(
        candidates["_same_operator"], "operator_and_distance", "distance_only"
    )
    best = candidates.sort_values(
        ["charger_id", "_same_operator", "match_distance_m"], ascending=[True, False, True]
    ).drop_duplicates("charger_id")
    matched = best[["charger_id", *POI_COLUMNS, *MATCH_COLUMNS]]
    result = dc[["charger_id"]].merge(matched, on="charger_id", how="left", validate="one_to_one")
    result["match_method"] = result["match_method"].fillna("none")
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
    dc = df[df["charger_type"] == "DC"]
    print(f"Matching {len(dc)} DC chargers against {int(pois['has_dc'].sum())} DC-capable OCM sites...")

    matches = match_dc_chargers(dc, pois)
    counts = matches["match_method"].value_counts()
    matched = len(matches) - counts.get("none", 0)
    print(counts.to_dict())
    print(f"Coverage: {matched}/{len(matches)} DC chargers augmented ({matched / len(matches):.1%})")

    merged = df.merge(matches, on="charger_id", how="left", validate="one_to_one")
    merged.to_csv(AUGMENTED_PATH, index=False)
    print(f"Saved augmented dataset to {AUGMENTED_PATH}")


if __name__ == "__main__":
    main()
