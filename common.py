"""Paths and helpers shared by the pipeline scripts (01-04).

Keeping paths and the operator-name cleanup in one module means every script
agrees on where files live and on what counts as "the same operator", instead
of each script repeating (and risking drifting from) its own copy.
"""

import re
from pathlib import Path

import pandas as pd


# --------------------------------------------------------------------------
# Project layout. Path(__file__).resolve().parent is the folder this file
# lives in, so every path below is anchored to the project root regardless of
# which directory the scripts are *run* from.
# --------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
RAW_DATA_DIR = PROJECT_DIR / "data" / "raw"            # step 01 writes here
PROCESSED_DATA_DIR = PROJECT_DIR / "data" / "processed"  # steps 02-03 write here
DATABASE_DIR = PROJECT_DIR / "database"                 # step 04 writes here

# Individual files used by more than one script.
EV_CSV_PATH = RAW_DATA_DIR / "ev_chargers.csv"                          # step 01 output, step 02 input
SA4_SHAPEFILE_PATH = RAW_DATA_DIR / "SA4_shapefile" / "SA4_2026_AUST_GDA2020.shp"  # step 01 output, steps 02 & 04 input
OCM_CACHE_PATH = RAW_DATA_DIR / "ocm_au_pois.json"                       # step 03's cached Open Charge Map download
OSM_CACHE_PATH = RAW_DATA_DIR / "osm_au_charging.json"                   # step 03's cached OpenStreetMap download
CLEANED_PATH = PROCESSED_DATA_DIR / "ev_chargers_cleaned_sa4.csv"       # step 02 output, step 03 input
AUGMENTED_PATH = PROCESSED_DATA_DIR / "ev_chargers_augmented.csv"       # step 03 output, step 04 input
AUDIT_PATH = PROCESSED_DATA_DIR / "augmentation_audit.csv"              # step 03's match/reject audit trail
DATABASE_PATH = DATABASE_DIR / "ev_database.duckdb"                     # step 04 output
SCHEMA_PATH = DATABASE_DIR / "schema.sql"                               # step 04 input (table definitions)

# GDA2020 / Australian Albers: an equal-area projected CRS in metres, so
# distances and nearest-neighbour searches are meaningful anywhere in Australia.
# (Latitude/longitude in EPSG:4326 are angles, not metres, so distances measured
# directly on them are distorted and get worse the further you are from the
# equator - a projected CRS avoids that problem.)
PROJECTED_CRS = "EPSG:9473"

# Columns that must not be parsed as numbers when the processed CSVs are re-read
# (otherwise SA4 code 106 becomes 106.0 and postcodes lose leading zeros).
# pandas.read_csv(..., dtype=PROCESSED_DTYPES) keeps these columns as text.
# ext_postcode (added by 03) is a postcode too - same risk, same fix.
PROCESSED_DTYPES = {"sa4_code": "string", "postcode": "string", "ext_postcode": "string"}

# Lower-case spelling variant -> canonical operator name. Keys are compared after
# whitespace is collapsed and any trailing "(...)" qualifier is removed, so
# "BP Pulse (AU)" and "Tesla (Tesla-only charging)" resolve via "bp pulse" / "tesla".
# Names not listed here are kept as written. The TfNSW file truncates some names to
# 13 characters ("Energy Austra"); the completions below were checked against the
# station addresses (e.g. "University of" is the University of Wollongong charger).
#
# Three sources feed this table: TfNSW (02), Open Charge Map (03) and
# OpenStreetMap (03). The OSM-specific entries below were found by inspecting
# OSM's "operator" tag values against the TfNSW spellings already listed here
# (e.g. OSM tags Ampol's network "AmpCharge"; TfNSW calls the same network "Ampol").
OPERATOR_ALIASES = {
    "bp australia": "BP",
    "bp pulse": "BP",
    "tesla motors": "Tesla",
    "tesla, inc.": "Tesla",       # OSM operator:wikipedia-sourced variant
    "tesla supercharger": "Tesla",
    "non-networked": "Non-networked",
    "evie networks": "Evie",
    "charge hub": "ChargeHub",
    "chargehub": "ChargeHub",
    "nrma electric": "NRMA",
    "jolt": "JOLT",
    "wevolt": "Wevolt",
    "noodoe ev": "Noodoe",
    "ampol ampcharge": "Ampol",
    "ampcharge": "Ampol",         # OSM operator tag for Ampol's charging network
    "viva energy a": "Viva Energy Australia",
    "plus es manag": "PLUS ES",
    "plus es": "PLUS ES",         # OSM uses title case ("Plus ES"); force TfNSW's all-caps spelling
    "energy austra": "Energy Australia",
    "fast cities a": "Fast Cities Australia",
    "university of": "University of Wollongong",
}


def canonical_operator(name) -> str | None:
    """Map an operator name from either source to one canonical spelling.

    Used by both 02 (TfNSW operator column) and 03 (Open Charge Map operator
    names), so a charger and its match always compare the same spelling.

    Returns None for missing names and for Open Charge Map placeholders such as
    "(Unknown Operator)", which carry no operator information.
    """
    if pd.isna(name):
        return None
    # Collapse "Tesla   Motors" -> "Tesla Motors" and trim leading/trailing spaces
    # (the raw data has trailing-space variants like "BP Australia ").
    collapsed = re.sub(r"\s+", " ", str(name)).strip()
    # Strip a trailing "(...)" qualifier, e.g. "BP Pulse (AU)" -> "BP Pulse",
    # "Tesla (Tesla-only charging)" -> "Tesla". Open Charge Map uses these
    # qualifiers to add detail; they are not part of the operator's name.
    base = re.sub(r" ?\([^)]*\)$", "", collapsed)  # whitespace is already collapsed
    if not base:
        # The whole name was a qualifier, e.g. "(Unknown Operator)" -> "".
        return None
    # Look up the lower-cased name in the alias table; if it's not a known
    # variant, use the name as given (title-cased-ish text from the source).
    return OPERATOR_ALIASES.get(base.lower(), base)
