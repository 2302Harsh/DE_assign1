"""Paths and helpers shared by the pipeline scripts (01-04)."""

import re
from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent
RAW_DATA_DIR = PROJECT_DIR / "data" / "raw"
PROCESSED_DATA_DIR = PROJECT_DIR / "data" / "processed"
DATABASE_DIR = PROJECT_DIR / "database"

EV_CSV_PATH = RAW_DATA_DIR / "ev_chargers.csv"
SA4_SHAPEFILE_PATH = RAW_DATA_DIR / "SA4_shapefile" / "SA4_2026_AUST_GDA2020.shp"
OCM_CACHE_PATH = RAW_DATA_DIR / "ocm_au_pois.json"
CLEANED_PATH = PROCESSED_DATA_DIR / "ev_chargers_cleaned_sa4.csv"
AUGMENTED_PATH = PROCESSED_DATA_DIR / "ev_chargers_augmented.csv"
DATABASE_PATH = DATABASE_DIR / "ev_database.duckdb"
SCHEMA_PATH = DATABASE_DIR / "schema.sql"

# GDA2020 / Australian Albers: an equal-area projected CRS in metres, so
# distances and nearest-neighbour searches are meaningful anywhere in Australia.
PROJECTED_CRS = "EPSG:9473"

# Columns that must not be parsed as numbers when the processed CSVs are re-read
# (otherwise SA4 code 106 becomes 106.0 and postcodes lose leading zeros).
PROCESSED_DTYPES = {"sa4_code": "string", "postcode": "string"}

# Lower-case spelling variant -> canonical operator name. Keys are compared after
# whitespace is collapsed and any trailing "(...)" qualifier is removed, so
# "BP Pulse (AU)" and "Tesla (Tesla-only charging)" resolve via "bp pulse" / "tesla".
# Names not listed here are kept as written. The TfNSW file truncates some names to
# 13 characters ("Energy Austra"); the completions below were checked against the
# station addresses (e.g. "University of" is the University of Wollongong charger).
OPERATOR_ALIASES = {
    "bp australia": "BP",
    "bp pulse": "BP",
    "tesla motors": "Tesla",
    "non-networked": "Non-networked",
    "evie networks": "Evie",
    "charge hub": "ChargeHub",
    "chargehub": "ChargeHub",
    "nrma electric": "NRMA",
    "jolt": "JOLT",
    "wevolt": "Wevolt",
    "noodoe ev": "Noodoe",
    "ampol ampcharge": "Ampol",
    "viva energy a": "Viva Energy Australia",
    "plus es manag": "PLUS ES",
    "energy austra": "Energy Australia",
    "fast cities a": "Fast Cities Australia",
    "university of": "University of Wollongong",
}


def canonical_operator(name) -> str | None:
    """Map an operator name from either source to one canonical spelling.

    Returns None for missing names and for Open Charge Map placeholders such as
    "(Unknown Operator)", which carry no operator information.
    """
    if pd.isna(name):
        return None
    collapsed = re.sub(r"\s+", " ", str(name)).strip()
    base = re.sub(r" ?\([^)]*\)$", "", collapsed)  # whitespace is already collapsed
    if not base:
        return None
    return OPERATOR_ALIASES.get(base.lower(), base)
