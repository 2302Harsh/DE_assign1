"""Augment DC fast chargers with pricing, plug and operator data from two free,
public charger-location sources.

Run with: python 03_data_augmentation.py [--refresh]

Two sources are tried in order, each downloaded once in bulk (all of Australia)
and cached in data/raw/, so a rerun needs no network access unless --refresh is
passed:
  1. Open Charge Map (OCM) - the primary source, requires a free API key.
  2. OpenStreetMap (OSM), via its Overpass query API - a secondary source used
     only for chargers OCM could not match. No API key needed.
Google Places/Maps was considered but not used: it has no free bulk "every EV
charger in a region" query, needs a billing-enabled key (this project must run
with no paid key and no hardcoded secrets), and doesn't tag a structured
"charging network operator" the way OCM and OSM both do - which the matching
below depends on. OSM covers the same ground for free, with no key, in the
same reproducible bulk-download-and-cache shape already used for OCM.

Matching (identical rules for both sources - see match_candidates()):
  * 'operator_and_distance': closest DC-capable candidate within 500 m whose
    canonical operator equals the charger's.
  * 'distance_only': otherwise, closest DC-capable candidate of any operator
    within 50 m.
  * A candidate that would otherwise qualify is REJECTED - never accepted -
    when its postcode or leading street number conflicts with the charger's,
    unless the two are within 50 m of each other (at that range, GPS
    proximity is stronger evidence than address text, which varies a lot in
    quality between sources).
  * 'none': no qualifying, non-conflicting candidate from either source.
Every attempt (one row per charger per source) is logged to
data/processed/augmentation_audit.csv, including rejections and why.
See README.md for the final match counts and the reasoning behind the
thresholds above.
"""

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

from common import (
    AUDIT_PATH, AUGMENTED_PATH, CLEANED_PATH, OCM_CACHE_PATH, OSM_CACHE_PATH,
    PROCESSED_DTYPES, PROJECT_DIR, PROJECTED_CRS, canonical_operator,
)


OCM_API_URL = "https://api.openchargemap.io/v3/poi/"
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"
# Australia's bounding box (south, west, north, east) - matches 02's
# LAT_RANGE/LON_RANGE. Both bulk downloads cover the whole country in one
# request rather than one request per charger.
AU_BBOX = (-45.0, 105.0, -9.0, 160.0)

# --- Matching thresholds ---------------------------------------------------
# Same canonical operator within this distance: the strongest signal, and the
# same 500 m used in the project's original single-source version.
OPERATOR_MATCH_RADIUS_M = 500
# Any operator within this distance, no operator match required. Tightened
# from an earlier 100 m to 50 m after testing turned up an 83 m gap between
# two *different* operators' real, distinct chargers on the same street - a
# reminder that "closest site" without an operator match is not the same as
# "same site", and 100 m wasn't a safe enough cutoff for that weaker signal.
DISTANCE_ONLY_RADIUS_M = 50
# Below this distance, GPS proximity overrides a postcode/address mismatch:
# the two points are for all practical purposes the same spot, and address
# text is the less reliable signal of the two (e.g. one source records a
# business name or a highway-frontage address, the other a reception/laneway
# address, for what is physically the same charging site).
TRUST_DISTANCE_M = 50
# A charger and a candidate whose leading street numbers differ by more than
# this are treated as a conflict (see address_conflict()) unless they are
# within TRUST_DISTANCE_M of each other. 10 tolerates minor numbering
# differences across one large complex ("1063" vs "1067") without accepting a
# clearly different street ("7 Bungan St" vs "1 Park Street").
ADDRESS_NUMBER_TOLERANCE = 10

# OCM connector titles starting with any of these are DC connectors, used as a
# fallback when OCM's own CurrentType field (checked first) is missing.
DC_CONNECTOR_PREFIXES = ("CCS", "CHAdeMO", "Tesla")
# OSM tags a DC-capable connector by socket key. Bare "type1"/"type2"/"tesla"/
# "as3112"/"wall" are AC-only connectors and are deliberately excluded.
OSM_DC_SOCKET_KEYS = {
    "chademo", "tesla_supercharger", "tesla_supercharger_ccs", "type1_combo", "type2_combo",
}
# OSM socket key -> the plug type name used elsewhere in this project, so OSM
# and OCM connectors of the same physical standard share one plug_types
# lookup table in the database instead of two near-duplicate spellings.
OSM_PLUG_TYPE_NAMES = {
    "type2_combo": "CCS (Type 2)",
    "type1_combo": "CCS (Type 1)",
    "chademo": "CHAdeMO",
    "tesla_supercharger": "Tesla (Model S/X)",
    "tesla_supercharger_ccs": "CCS (Type 2)",
    "type2": "Type 2 (Socket Only)",
    "type1": "Type 1 (J1772)",
    "tesla": "Tesla (Model S/X)",
    "as3112": "Type I (AS 3112)",
}

# Operators named in the assignment brief as the priority for this pass. Not
# a filter - matching runs the same way for every operator - just what
# main() reports a per-operator coverage breakdown for.
FOCUS_OPERATORS = ["Evie", "JOLT", "Chargefox", "Exploren", "Ampol", "BP", "Tesla", "NRMA"]

# Matches "$0.55/kWh", "AUD 0.42/kWh", "55c/kWh" and similar.
PRICE_PER_KWH = re.compile(
    r"(?:\$|aud)\s*(\d+(?:\.\d+)?)\s*(?:/|per)\s*kwh"  # $0.55/kWh, AUD 0.42/kWh
    r"|(\d+(?:\.\d+)?)\s*c(?:ents?)?\s*(?:/|per)\s*kwh",  # 55c/kWh
    re.IGNORECASE,
)
PRICE_RANGE = re.compile(r"\d\s*c?\s*[-–]\s*\$?\d")  # "59c-68c/kWh": the price varies
FREE = re.compile(r"free( to use| charging)?|no charge|0(\.0+)?", re.IGNORECASE)
POSTCODE = re.compile(r"\d{4}")                       # first 4-digit run in free text
LEADING_NUMBER = re.compile(r"^\s*(\d+)\s*(?:[-/](\d+))?")  # "27-33 Oaks Ave" -> (27, 33)
RATE_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Payload columns copied from a matched external site into the augmented CSV.
# flatten_ocm_poi() and flatten_osm_element() both produce exactly this shape
# (plus "has_dc", "lat", "lon", used only for matching, not carried through),
# so match_candidates() below can treat either source identically.
EXT_COLUMNS = [
    "source_poi_id", "ext_operator", "ext_name", "ext_address", "ext_postcode",
    "usage_cost", "price_per_kwh", "plug_types", "num_points", "num_connectors", "rate_kw",
]
MATCH_COLUMNS = ["augmentation_source", "match_method", "match_distance_m"]
AUDIT_COLUMNS = [
    "charger_id", "operator", "station_address", "postcode", "source", "decision",
    "match_method", "distance_m", "candidate_operator", "candidate_address",
    "candidate_postcode", "reject_reason",
]


def get_api_key() -> str:
    """Read OPENCHARGEMAP_API_KEY from .env. Only OCM needs a key; OSM does not."""
    load_dotenv(PROJECT_DIR / ".env")
    key = os.environ.get("OPENCHARGEMAP_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENCHARGEMAP_API_KEY is not set. Copy .env.example to .env and add your "
            "Open Charge Map key (or run with a cached data/raw/ocm_au_pois.json)."
        )
    return key


def _cache_json(path: Path, data) -> None:
    """Write *data* as JSON to *path* without ever leaving a corrupt cache file
    behind (same atomic write-then-rename pattern as 01_download_data.py)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                     dir=path.parent, suffix=".part") as tmp:
        json.dump(data, tmp)
    Path(tmp.name).replace(path)


def load_ocm_pois(refresh: bool) -> list[dict]:
    """Return every Australian OCM point of interest, downloading only if uncached."""
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
    _cache_json(OCM_CACHE_PATH, pois)
    print(f"Cached {len(pois)} sites in {OCM_CACHE_PATH}")
    return pois


def load_osm_pois(refresh: bool) -> list[dict]:
    """Return every Australian OSM charging_station element, downloading only if uncached.

    The public Overpass API is free and needs no key, but is shared and rate
    limited, so a busy instance can reply 429 (too many requests) or 504
    (timed out) even for a valid query. Retried a few times with backoff
    before giving up - this happened routinely while developing this script.
    """
    if OSM_CACHE_PATH.exists() and not refresh:
        print(f"Using cached OpenStreetMap data: {OSM_CACHE_PATH}")
        return json.loads(OSM_CACHE_PATH.read_text(encoding="utf-8"))["elements"]

    print("Downloading all Australian OpenStreetMap charging stations...")
    south, west, north, east = AU_BBOX
    query = f"""
        [out:json][timeout:180];
        (
          node["amenity"="charging_station"]({south},{west},{north},{east});
          way["amenity"="charging_station"]({south},{west},{north},{east});
        );
        out center tags;
    """
    # A descriptive User-Agent is expected Overpass API etiquette; requests
    # without one are more likely to be refused.
    headers = {"User-Agent": "COMP5339-assignment-pipeline/1.0 (student project)"}
    last_error = "unknown error"
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(OVERPASS_API_URL, data={"data": query}, timeout=180, headers=headers)
            if response.status_code == 200:
                data = response.json()
                _cache_json(OSM_CACHE_PATH, data)
                print(f"Cached {len(data['elements'])} sites in {OSM_CACHE_PATH}")
                return data["elements"]
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as error:
            last_error = str(error)
        if attempt < max_attempts:
            wait_s = 20 * attempt
            print(f"Overpass API attempt {attempt} failed ({last_error}); retrying in {wait_s}s...")
            time.sleep(wait_s)
    raise RuntimeError(
        f"Could not download OpenStreetMap data after {max_attempts} attempts ({last_error}). "
        "The public Overpass API is sometimes busy - wait a bit and rerun with --refresh, "
        "or rerun later once a cached data/raw/osm_au_charging.json exists."
    )


def clean_postcode(text) -> str | None:
    """Extract a 4-digit Australian postcode from free text, e.g. "NSW 2630" -> "2630".

    Needed because Open Charge Map's Postcode field sometimes includes a state
    prefix; comparing that text directly against a clean postcode would treat
    every such row as a mismatch.
    """
    if not isinstance(text, str):
        return None
    match = POSTCODE.search(text)
    return match.group(0) if match else None


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


def leading_number_range(address) -> tuple[int, int] | None:
    """Return the leading street-number range of an address, e.g. "27-33 Oaks
    Ave" -> (27, 33), "12 Smith St" -> (12, 12). None if there's no leading number."""
    if not isinstance(address, str):
        return None
    match = LEADING_NUMBER.match(address.strip())
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2) or match.group(1))
    return (low, high) if low <= high else (high, low)


def address_conflict(charger_address, candidate_address) -> bool:
    """True if both addresses start with a street number and those numbers are
    clearly different (see ADDRESS_NUMBER_TOLERANCE). False whenever either
    address has no leading number to compare - that's "no evidence", not "no conflict"."""
    charger_range = leading_number_range(charger_address)
    candidate_range = leading_number_range(candidate_address)
    if charger_range is None or candidate_range is None:
        return False
    return (
        candidate_range[0] - charger_range[1] > ADDRESS_NUMBER_TOLERANCE
        or charger_range[0] - candidate_range[1] > ADDRESS_NUMBER_TOLERANCE
    )


def pick_street_address(line1, line2) -> str | None:
    """Prefer whichever OCM address line looks like a street ("12 Smith St").

    OCM's AddressLine1 is sometimes a business name instead of a street (e.g.
    "Woolworths", with the actual street in AddressLine2), so the line that
    starts with a digit is preferred; if neither does, both are combined for
    display (address_conflict() above simply finds no leading number there,
    which correctly treats it as unable to confirm or deny a conflict).
    """
    for candidate in (line2, line1):
        if isinstance(candidate, str) and re.match(r"^\s*\d", candidate):
            return candidate
    return ", ".join(line for line in (line1, line2) if line) or None


def flatten_ocm_poi(poi: dict) -> dict:
    """Reduce one nested Open Charge Map POI record to the shared candidate shape."""
    connections = poi.get("Connections") or []
    titles = [(c.get("ConnectionType") or {}).get("Title", "").strip() for c in connections]
    plug_types = [t for t in dict.fromkeys(titles) if t and t != "Unknown"]
    has_dc = any((c.get("CurrentType") or {}).get("Title") == "DC" for c in connections) or any(
        t.startswith(DC_CONNECTOR_PREFIXES) for t in titles
    )
    usage_cost = (poi.get("UsageCost") or "").strip() or None
    address = poi["AddressInfo"]
    powers = [c["PowerKW"] for c in connections if isinstance(c.get("PowerKW"), (int, float))]
    return {
        "source_poi_id": str(poi["ID"]),
        "ext_operator": canonical_operator((poi.get("OperatorInfo") or {}).get("Title")),
        "ext_name": address.get("Title"),
        "ext_address": pick_street_address(address.get("AddressLine1"), address.get("AddressLine2")),
        "ext_postcode": clean_postcode(address.get("Postcode")),
        "usage_cost": usage_cost,
        "price_per_kwh": parse_price_per_kwh(usage_cost),
        "plug_types": ", ".join(plug_types) or None,
        # NumberOfPoints is OCM's count of charging bays; Quantity (default 1) counts
        # connectors per connection record, so the two can legitimately differ.
        "num_points": poi.get("NumberOfPoints"),
        "num_connectors": sum(c.get("Quantity") or 1 for c in connections) or None,
        "rate_kw": max(powers) if powers else None,
        "has_dc": has_dc,
        "lat": address.get("Latitude"),
        "lon": address.get("Longitude"),
    }


def flatten_osm_element(element: dict) -> dict:
    """Reduce one OSM node/way element to the same shape flatten_ocm_poi() produces."""
    if element["type"] == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:  # a "way" (an area, e.g. a car park) - "out center" gave us its midpoint
        centre = element.get("center") or {}
        lat, lon = centre.get("lat"), centre.get("lon")

    tags = element.get("tags", {})
    # OSM records each connector type as its own "socket:<type>" tag, valued
    # with how many of that connector the site has (e.g. "socket:chademo": "2").
    # Sub-tags like "socket:type2:voltage" describe one connector, not a count
    # of a new type, so only keys with exactly one ":" are connector counts.
    sockets = {k[len("socket:"):]: v for k, v in tags.items() if k.startswith("socket:") and k.count(":") == 1}
    has_dc = bool(set(sockets) & OSM_DC_SOCKET_KEYS)
    plug_types = list(dict.fromkeys(OSM_PLUG_TYPE_NAMES[key] for key in sockets if key in OSM_PLUG_TYPE_NAMES))
    num_connectors = 0
    for value in sockets.values():
        try:
            num_connectors += int(value)
        except (TypeError, ValueError):
            num_connectors += 1  # a non-numeric tag value (e.g. "yes") still means "at least one"
    num_points = None
    if str(tags.get("capacity", "")).isdigit():
        num_points = int(tags["capacity"])
    # Power is tagged per connector, e.g. "socket:type2_combo:output": "120 kW".
    # A handful of entries give watts instead of kW without saying so (e.g.
    # "250000"); treat anything above 1000 as watts and convert.
    rate_values = []
    for key, value in tags.items():
        if key.endswith(":output") and isinstance(value, str):
            for number in RATE_NUMBER.findall(value):
                number = float(number)
                rate_values.append(number / 1000 if number > 1000 else number)
    street = (tags.get("addr:housenumber", "") + " " + tags.get("addr:street", "")).strip() or None

    return {
        "source_poi_id": f"{element['type']}/{element['id']}",
        "ext_operator": canonical_operator(tags.get("operator") or tags.get("brand")),
        "ext_name": tags.get("name"),
        "ext_address": street,
        "ext_postcode": clean_postcode(tags.get("addr:postcode")),
        "usage_cost": None,       # OSM has a yes/no "fee" tag but no priced tariff text
        "price_per_kwh": None,
        "plug_types": ", ".join(plug_types) or None,
        "num_points": num_points,
        "num_connectors": num_connectors or None,
        "rate_kw": max(rate_values) if rate_values else None,
        "has_dc": has_dc,
        "lat": lat,
        "lon": lon,
    }


def to_projected_points(df: pd.DataFrame, lon: str, lat: str) -> gpd.GeoDataFrame:
    """Turn lon/lat columns into point geometry in PROJECTED_CRS, with x/y columns.

    The x/y columns (metres) let match_candidates() compute plain Euclidean
    distances with numpy afterwards, which is simpler and faster than calling
    a geometry .distance() method per pair.
    """
    points = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df[lon], df[lat]), crs="EPSG:4326"
    ).to_crs(PROJECTED_CRS)
    points["x"], points["y"] = points.geometry.x, points.geometry.y
    return points


def _no_candidate_audit(chargers: pd.DataFrame, source: str) -> pd.DataFrame:
    """One audit row per charger, recording that this source had nothing nearby."""
    return pd.DataFrame({
        "charger_id": chargers["charger_id"], "operator": chargers["operator"],
        "station_address": chargers["station_address"], "postcode": chargers["postcode"],
        "source": source, "decision": "no_candidate", "match_method": None, "distance_m": None,
        "candidate_operator": None, "candidate_address": None, "candidate_postcode": None,
        "reject_reason": None,
    })


def match_candidates(chargers: pd.DataFrame, sites: pd.DataFrame, source: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Match *chargers* against *sites* from one external *source*.

    chargers: charger_id, operator, postcode, station_address, latitude, longitude.
    sites: ext_operator, ext_postcode, ext_address, plus the rest of EXT_COLUMNS.

    Returns (matched, audit):
      matched - charger_id + EXT_COLUMNS + MATCH_COLUMNS, one row per accepted match.
      audit   - one row per input charger recording what this source did with it
                (decision: 'accepted' / 'rejected' / 'no_candidate'; see module docstring).
    """
    empty_matched = pd.DataFrame(columns=["charger_id", *EXT_COLUMNS, *MATCH_COLUMNS])
    if chargers.empty or sites.empty:
        return empty_matched, _no_candidate_audit(chargers, source)

    chargers_g = to_projected_points(chargers, "longitude", "latitude")
    sites_g = to_projected_points(sites, "lon", "lat").rename(columns={"x": "site_x", "y": "site_y"})

    # sjoin with predicate="dwithin" pairs every charger with every candidate
    # site within OPERATOR_MATCH_RADIUS_M of it (the wider of the two radii) -
    # one output row per (charger, nearby site) pair, the spatial equivalent
    # of a SQL join on "distance(a, b) <= radius" instead of an equality condition.
    pairs = gpd.sjoin(chargers_g, sites_g, predicate="dwithin", distance=OPERATOR_MATCH_RADIUS_M)
    if pairs.empty:
        return empty_matched, _no_candidate_audit(chargers, source)

    pairs["distance_m"] = np.hypot(pairs["x"] - pairs["site_x"], pairs["y"] - pairs["site_y"]).round(1)
    pairs["same_operator"] = pairs["operator"] == pairs["ext_operator"]
    pairs["postcode_conflict"] = (
        pairs["postcode"].notna() & pairs["ext_postcode"].notna() & (pairs["postcode"] != pairs["ext_postcode"])
    )
    number_conflict = pd.Series(
        [address_conflict(a, b) for a, b in zip(pairs["station_address"], pairs["ext_address"])],
        index=pairs.index,
    )
    pairs["address_conflict"] = number_conflict & (pairs["distance_m"] > TRUST_DISTANCE_M)
    pairs["conflict"] = pairs["postcode_conflict"] | pairs["address_conflict"]
    pairs["match_method"] = np.where(pairs["same_operator"], "operator_and_distance", "distance_only")
    pairs["qualifies"] = pairs["same_operator"] | (pairs["distance_m"] <= DISTANCE_ONLY_RADIUS_M)

    qualifying = pairs[pairs["qualifies"]]
    # For each charger, prefer a same-operator match over a distance-only one
    # (ascending=False on same_operator puts True first), then the closest
    # non-conflicting candidate.
    accepted = (
        qualifying[~qualifying["conflict"]]
        .sort_values(["charger_id", "same_operator", "distance_m"], ascending=[True, False, True])
        .drop_duplicates("charger_id")
    )
    # "Rejected" means every qualifying candidate for that charger conflicted -
    # not just that *a* candidate conflicted while a different one was accepted.
    rejected_ids = set(qualifying["charger_id"]) - set(accepted["charger_id"])
    rejected = (
        qualifying[qualifying["charger_id"].isin(rejected_ids)]
        .sort_values("distance_m")
        .drop_duplicates("charger_id")
    )
    no_candidate = chargers[~chargers["charger_id"].isin(qualifying["charger_id"])]

    matched = accepted[["charger_id", *EXT_COLUMNS]].copy()
    matched.insert(1, "augmentation_source", source)
    matched["match_method"] = accepted["match_method"].to_numpy()
    matched["match_distance_m"] = accepted["distance_m"].to_numpy()

    def reject_reason(row) -> str:
        reasons = []
        if row["postcode_conflict"]:
            reasons.append("postcode_conflict")
        if row["address_conflict"]:
            reasons.append("address_conflict")
        return "+".join(reasons)

    accepted_audit = pd.DataFrame({
        "charger_id": accepted["charger_id"], "operator": accepted["operator"],
        "station_address": accepted["station_address"], "postcode": accepted["postcode"],
        "source": source, "decision": "accepted", "match_method": accepted["match_method"],
        "distance_m": accepted["distance_m"], "candidate_operator": accepted["ext_operator"],
        "candidate_address": accepted["ext_address"], "candidate_postcode": accepted["ext_postcode"],
        "reject_reason": None,
    })
    rejected_audit = pd.DataFrame({
        "charger_id": rejected["charger_id"], "operator": rejected["operator"],
        "station_address": rejected["station_address"], "postcode": rejected["postcode"],
        "source": source, "decision": "rejected", "match_method": rejected["match_method"],
        "distance_m": rejected["distance_m"], "candidate_operator": rejected["ext_operator"],
        "candidate_address": rejected["ext_address"], "candidate_postcode": rejected["ext_postcode"],
        "reject_reason": rejected.apply(reject_reason, axis=1),
    })
    audit = pd.concat([accepted_audit, rejected_audit, _no_candidate_audit(no_candidate, source)], ignore_index=True)
    return matched, audit[AUDIT_COLUMNS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true",
                        help="re-download both external sources even if cached")
    args = parser.parse_args()

    df = pd.read_csv(CLEANED_PATH, dtype=PROCESSED_DTYPES)
    dc = df[df["charger_type"] == "DC"][
        ["charger_id", "operator", "postcode", "station_address", "latitude", "longitude"]
    ]
    print(f"Matching {len(dc)} DC chargers...")

    # --- Source 1: Open Charge Map (primary) --------------------------------
    ocm_pois = pd.DataFrame([flatten_ocm_poi(p) for p in load_ocm_pois(args.refresh)]).dropna(subset=["lat", "lon"])
    ocm_matched, ocm_audit = match_candidates(dc, ocm_pois[ocm_pois["has_dc"]], "open_charge_map")
    print(f"Open Charge Map: {len(ocm_matched)} matched, "
          f"{(ocm_audit['decision'] == 'rejected').sum()} rejected, "
          f"{(ocm_audit['decision'] == 'no_candidate').sum()} had no candidate")

    # --- Source 2: OpenStreetMap (secondary, remaining chargers only) -------
    remaining = dc[~dc["charger_id"].isin(ocm_matched["charger_id"])]
    osm_pois = pd.DataFrame([flatten_osm_element(e) for e in load_osm_pois(args.refresh)]).dropna(subset=["lat", "lon"])
    osm_matched, osm_audit = match_candidates(remaining, osm_pois[osm_pois["has_dc"]], "openstreetmap")
    print(f"OpenStreetMap:   {len(osm_matched)} matched, "
          f"{(osm_audit['decision'] == 'rejected').sum()} rejected, "
          f"{(osm_audit['decision'] == 'no_candidate').sum()} had no candidate")

    matches = pd.concat([ocm_matched, osm_matched], ignore_index=True)
    audit = pd.concat([ocm_audit, osm_audit], ignore_index=True)
    audit.to_csv(AUDIT_PATH, index=False)
    print(f"Saved match/reject audit trail to {AUDIT_PATH}")

    matched_count = len(matches)
    ever_rejected = (
        set(ocm_audit.loc[ocm_audit["decision"] == "rejected", "charger_id"])
        | set(osm_audit.loc[osm_audit["decision"] == "rejected", "charger_id"])
    )
    rejected_chargers = len(ever_rejected - set(matches["charger_id"]))
    unmatched_count = len(dc) - matched_count
    print(f"\nSummary: {len(ocm_matched)} via Open Charge Map + {len(osm_matched)} via OpenStreetMap "
          f"= {matched_count} matched; {rejected_chargers} charger(s) had only conflicting candidates; "
          f"{unmatched_count} remain unmatched.")
    print(f"Coverage: {matched_count}/{len(dc)} DC chargers augmented ({matched_count / len(dc):.1%})")

    still_unmatched = dc[~dc["charger_id"].isin(matches["charger_id"])]
    print("\nCoverage by focus operator:")
    for operator in FOCUS_OPERATORS:
        total = (dc["operator"] == operator).sum()
        matched = total - (still_unmatched["operator"] == operator).sum()
        print(f"  {operator:12s} {matched:3d}/{total:3d}")

    # Left-merge the match results back onto every charger (AC, DC and
    # upcoming); non-DC rows simply get NULLs in every augmentation column,
    # including match_method, showing they were never attempted.
    merged = df.merge(matches, on="charger_id", how="left", validate="one_to_one")
    count_columns = ["num_points", "num_connectors"]
    merged[count_columns] = merged[count_columns].astype("Int64")  # else written as 12.0
    merged.to_csv(AUGMENTED_PATH, index=False)
    print(f"\nSaved augmented dataset to {AUGMENTED_PATH}")


if __name__ == "__main__":
    main()
