"""Clean the raw TfNSW EV-charger data and attach an ABS SA4 region to each charger.

Run with: python 02_clean_and_spatial_join.py

Cleaning policies (also summarised in README.md):
  * Text: whitespace collapsed; embedded newlines in addresses become ", ".
  * Operator: spelling variants and 13-character truncations mapped to one
    canonical name (common.canonical_operator).
  * charger_rating: free text parsed into a numeric per-connector maximum in kW.
    Placeholders such as "AC" have no power information and become NULL.
  * charger_type: "Upcoming" rows are KEPT but flagged status = 'upcoming' with
    charger_type NULL (AC/DC is unknown for planned sites). Analysis of existing
    infrastructure should filter on status = 'existing'.
  * Near-duplicates: rows sharing coordinates, canonical operator and charger_type
    are the same charger reported by two source files. The most complete row is
    kept and its NULLs are filled from the dropped rows. Rows at the same
    coordinates with two different station names are distinct sites and are kept.
  * SA4 assignment: point-in-polygon first; points that miss every polygon (e.g.
    coastal points) fall back to the nearest polygon within 2 km, measured in a
    projected CRS. Assignment method and distance are stored per row.
"""

import re

import geopandas as gpd
import pandas as pd

from common import (
    CLEANED_PATH, EV_CSV_PATH, PROCESSED_DATA_DIR, PROJECTED_CRS, SA4_SHAPEFILE_PATH,
    canonical_operator,
)


# Australia's bounding box (including offshore territories); rejects swapped or
# zero coordinates without needing any state-specific knowledge. A row with
# latitude/longitude outside this box is clearly bad data, not just unusual.
LAT_RANGE = (-45.0, -9.0)
LON_RANGE = (105.0, 160.0)
# How far (in metres) a point is allowed to be outside every SA4 polygon and
# still be assigned to the nearest one (see assign_sa4 below).
MAX_SA4_NEAREST_M = 2000

# Raw column name -> cleaner name used from here on.
COLUMN_RENAMES = {
    "objectid": "source_objectid",
    "lganame": "lga_name",
    "pcode": "postcode",
}
# Rows that agree on all of these are treated as the same physical charger
# reported twice (see drop_near_duplicates).
NEAR_DUPLICATE_KEY = ["latitude", "longitude", "operator", "charger_type"]
# Columns written to the cleaned CSV, and their order. Anything not listed here
# (e.g. helper columns used only while cleaning) is dropped from the output.
OUTPUT_COLUMNS = [
    "charger_id", "source_objectid", "station_name", "station_address", "latitude",
    "longitude", "lga_name", "postcode", "source", "operator", "number_of_plugs",
    "charger_type", "status", "charger_rating_raw", "charger_rating_kw", "sa4_code",
    "sa4_name", "sa4_assignment", "sa4_distance_m",
]


def parse_rating_kw(text) -> float | None:
    """Return the highest per-connector power in kW, or None if there is none.

    "22 kW" and "22" -> 22; "2x350kW & 2x175kW" -> 350; "AC" -> None.

    "2x350kW & 2x175kW" describes two connectors at 350 kW and two at 175 kW;
    we keep the single highest figure (350) as a simple, comparable number
    rather than trying to represent every connector's rating in one column.
    """
    if pd.isna(text):
        return None
    # Remove all whitespace and lower-case first so "2 x 350 kW" and "2x350kW"
    # are handled the same way, then drop the "2x" connector-count prefixes so
    # they aren't mistaken for a power figure (e.g. "2x350" should give 350,
    # not treat "2" as a rating too).
    per_connector = re.sub(r"\d+x", "", re.sub(r"\s", "", str(text).lower()))  # drop "2x"
    # Pull out every remaining number (e.g. from "350kw&175kw" -> ["350", "175"])
    # and keep the largest one.
    powers = [float(p) for p in re.findall(r"\d[\d.]*", per_connector)]
    return max(powers) if powers else None


def completeness(df: pd.DataFrame, stage: str) -> pd.DataFrame:
    """Non-null counts per column, in long format so stages can be stacked.

    Called once for the raw data and once for the cleaned data; the two
    results are concatenated in main() so data_completeness.csv shows, per
    column, what fraction of rows had a value before and after cleaning.
    """
    return pd.DataFrame({
        "stage": stage,
        "column": df.columns,
        "non_null": df.notna().sum().values,
        "total": len(df),
        "pct_complete": (df.notna().mean() * 100).round(1).values,
    })


def load_raw() -> pd.DataFrame:
    """Read the raw TfNSW CSV and normalise its column names to snake_case."""
    # utf-8-sig strips the byte-order-mark the source file starts with (Excel
    # convention); without it the first column would be read as "﻿objectid".
    # PCODE is forced to text so postcodes like "0800" keep their leading zero.
    df = pd.read_csv(EV_CSV_PATH, encoding="utf-8-sig", dtype={"PCODE": "string"})
    # "Station_name" -> "station_name", "Charger_Type" -> "charger_type", etc.
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")
    required = {"latitude", "longitude", "operator", "charger_type", "charger_rating"}
    missing = required.difference(df.columns)
    if missing:
        # Fail fast with a clear message rather than letting a later step crash
        # on a missing column with a confusing KeyError.
        raise ValueError(f"EV data is missing required columns: {sorted(missing)}")
    return df


def clean_text(df: pd.DataFrame) -> pd.DataFrame:
    """Trim whitespace in every text column and normalise line breaks in addresses."""
    text_columns = df.select_dtypes(include=["object", "string"]).columns
    for column in text_columns:
        # Strip leading/trailing spaces (fixes "BP Australia " -> "BP Australia")
        # and turn any resulting empty string into a proper missing value.
        df[column] = df[column].str.strip().replace("", pd.NA)
    # Some addresses contain embedded newlines (e.g. "123 Main St\nClontarf NSW"),
    # apparently from a multi-line form field in the source system. Replace the
    # newline(s) with ", " so the address reads as one line, matching the
    # comma-separated style used elsewhere in the same column.
    df["station_address"] = df["station_address"].str.replace(r"\s*[\r\n]+\s*", ", ", regex=True)
    # Collapse any other runs of internal whitespace ("Sydney  City" -> "Sydney City")
    # in every text column except the address, which was already handled above.
    for column in text_columns.drop("station_address"):
        df[column] = df[column].str.replace(r"\s+", " ", regex=True)
    return df


def clean_fields(df: pd.DataFrame) -> pd.DataFrame:
    """Type-convert coordinates, canonicalise operator names, and derive new columns."""
    df = df.rename(columns=COLUMN_RENAMES)

    # errors="coerce" turns anything that isn't a valid number (blank, text,
    # etc.) into NaN instead of raising, so bad rows can be filtered out below
    # rather than crashing the whole script.
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    # Keep only rows whose coordinates are both present and inside Australia's
    # bounding box (catches NaNs from the coercion above and swapped/garbage values).
    valid = (
        df["latitude"].between(*LAT_RANGE) & df["longitude"].between(*LON_RANGE)
    )
    print(f"Dropped {(~valid).sum()} rows with missing or out-of-range coordinates.")
    df = df[valid].copy()

    # Int64 (capital I) is pandas' nullable integer type: it can hold missing
    # values (<NA>) unlike a plain int column, and - unlike a float column -
    # it never renders a whole number like 696 as "696.0" when written to CSV.
    df["source_objectid"] = df["source_objectid"].astype("Int64")  # else written as 696.0
    # Fold spelling variants / truncations into one name per operator (see common.py).
    df["operator"] = df["operator"].map(canonical_operator)
    # charger_type in the raw data is one of "AC", "DC" or "Upcoming". Split
    # that into two columns: status records whether the site exists yet, and
    # charger_type keeps only the electrical type (which "Upcoming" doesn't say).
    df["status"] = df["charger_type"].str.lower().map({"upcoming": "upcoming"}).fillna("existing")
    df["charger_type"] = df["charger_type"].str.upper().where(lambda s: s.isin(["AC", "DC"]))
    df["charger_rating_raw"] = df["charger_rating"]           # keep the original text for reference
    df["charger_rating_kw"] = df["charger_rating"].map(parse_rating_kw)  # numeric version
    return df.drop(columns="charger_rating")


def drop_near_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse the same charger reported by two source files (see module docstring).

    Two rows that share coordinates, operator and charger_type but come from
    different source files are usually the exact same charger - one row from
    an older extract with an OBJECTID and LGA, another from a newer extract
    without them (or vice versa). Rather than pick one at random and lose
    whatever detail only the other row has, this keeps the more complete row
    and fills any of its remaining gaps from the row it's replacing.
    """
    # Rank rows within each duplicate group by how many non-null values they
    # have, most complete first, so the "best" row of a group is always first
    # after groupby() below (groupby preserves the order rows arrive in).
    ranked = df.assign(_filled=df.notna().sum(axis=1)).sort_values(
        "_filled", ascending=False, kind="stable"
    )
    groups = []
    for _, group in ranked.groupby(NEAR_DUPLICATE_KEY, dropna=False, sort=False):
        # Only collapse the group if every row shares the same station_name
        # (or all have no name at all) - two different names at the same
        # coordinates means two distinct chargers, not a duplicate report.
        if len(group) > 1 and group["station_name"].nunique() <= 1:
            # bfill() ("backward fill") copies each column's next non-null
            # value upward into any earlier NaN, so the first row (the most
            # complete one, thanks to the sort above) ends up filled in from
            # whatever the other row(s) had. iloc[[0]] then keeps just that
            # single, now-more-complete row and discards the rest.
            group = group.bfill().iloc[[0]]  # keep the fullest row, fill its gaps
        groups.append(group)
    return pd.concat(groups).drop(columns="_filled").sort_index()


def load_sa4() -> gpd.GeoDataFrame:
    """Load the ABS SA4 region boundaries, keeping only regions with a real polygon."""
    # columns=[...] tells geopandas to read only these attribute columns (plus
    # geometry, which is always included) instead of the whole shapefile.
    sa4 = gpd.read_file(SA4_SHAPEFILE_PATH, columns=["SA4_CODE26", "SA4_NAME26"])
    sa4 = sa4.rename(columns={"SA4_CODE26": "sa4_code", "SA4_NAME26": "sa4_name"})
    # Non-geographic codes ("Migratory - Offshore - Shipping", "No usual address", ...)
    # have no polygon and can never contain a charger.
    return sa4[sa4.geometry.notna()]


def assign_sa4(df: pd.DataFrame, sa4: gpd.GeoDataFrame) -> pd.DataFrame:
    """Add sa4_code, sa4_name, sa4_assignment ('within'/'nearest') and sa4_distance_m.

    Two-step spatial join:
      1. Point-in-polygon: exact, and correct for the vast majority of chargers.
      2. Nearest-polygon fallback for the handful of points (e.g. right on the
         coastline) that don't fall inside any polygon due to how precisely
         the coastline is drawn versus GPS measurement error.
    """
    # Turn the plain latitude/longitude columns into point geometries, in the
    # same CRS (EPSG:4326, i.e. GPS coordinates) as the raw data, then
    # reproject to match the SA4 shapefile's CRS so the two can be compared.
    points = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df["longitude"], df["latitude"]), crs="EPSG:4326"
    ).to_crs(sa4.crs)

    # sjoin (spatial join) with predicate="within" keeps only point/polygon
    # pairs where the point actually falls inside the polygon - this is the
    # spatial equivalent of a SQL INNER JOIN, matched by geometry instead of a key.
    within = gpd.sjoin(points[["geometry"]], sa4, how="inner", predicate="within")
    within = within[~within.index.duplicated()]  # a point on a shared border matches twice
    result = pd.DataFrame(index=df.index, columns=["sa4_code", "sa4_name"], dtype="string")
    result.loc[within.index, ["sa4_code", "sa4_name"]] = within[["sa4_code", "sa4_name"]]
    result["sa4_assignment"] = pd.Series("within", index=within.index)
    result["sa4_distance_m"] = pd.Series(0.0, index=within.index)

    # Nearest-polygon distances need a projected CRS; degrees are not distances.
    # Only the points that missed every polygon above need this extra step.
    outside = points.loc[points.index.difference(within.index), ["geometry"]]
    if not outside.empty:
        # sjoin_nearest finds, for each point, the closest polygon and the
        # distance to it; max_distance caps how far it's allowed to search, so
        # a point nowhere near any region (a genuine data error) is left
        # unassigned rather than matched to something far away and wrong.
        nearest = gpd.sjoin_nearest(
            outside.to_crs(PROJECTED_CRS), sa4.to_crs(PROJECTED_CRS), how="inner",
            max_distance=MAX_SA4_NEAREST_M, distance_col="sa4_distance_m",
        )
        nearest = nearest[~nearest.index.duplicated()]
        result.loc[nearest.index, ["sa4_code", "sa4_name"]] = nearest[["sa4_code", "sa4_name"]]
        result.loc[nearest.index, "sa4_distance_m"] = nearest["sa4_distance_m"].round(1)
        result.loc[nearest.index, "sa4_assignment"] = "nearest"
    return df.join(result)


def main() -> None:
    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)

    raw = load_raw()
    print(f"Raw rows: {len(raw)}")
    df = clean_text(raw.copy())
    df = df.drop_duplicates()  # remove rows that are 100% identical across every column
    print(f"After exact-duplicate removal: {len(df)}")
    df = clean_fields(df)
    df = drop_near_duplicates(df)
    print(f"After near-duplicate removal: {len(df)}")

    # Build a fresh 1..N id now that rows have been dropped/merged, because
    # objectid (the source's own id) is blank for 94% of rows and so can't be
    # used as a reliable key.
    df = df.reset_index(drop=True)
    df.insert(0, "charger_id", range(1, len(df) + 1))  # objectid is 94% blank, so not a key
    df = assign_sa4(df, load_sa4())

    unassigned = df["sa4_code"].isna().sum()
    print(f"SA4 assignment: {df['sa4_assignment'].value_counts().to_dict()}; unassigned: {unassigned}")
    print(f"Status: {df['status'].value_counts().to_dict()}")

    # Only the columns meant for downstream use are written out - helper
    # columns created along the way (e.g. from the spatial join) are dropped.
    df[OUTPUT_COLUMNS].to_csv(CLEANED_PATH, index=False)
    quality = pd.concat([completeness(raw, "raw"), completeness(df[OUTPUT_COLUMNS], "cleaned")])
    quality.to_csv(PROCESSED_DATA_DIR / "data_completeness.csv", index=False)
    print(f"Saved {len(df)} cleaned chargers to {CLEANED_PATH}")


if __name__ == "__main__":
    main()
