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
# zero coordinates without needing any state-specific knowledge.
LAT_RANGE = (-45.0, -9.0)
LON_RANGE = (105.0, 160.0)
MAX_SA4_NEAREST_M = 2000

COLUMN_RENAMES = {
    "objectid": "source_objectid",
    "lganame": "lga_name",
    "pcode": "postcode",
}
NEAR_DUPLICATE_KEY = ["latitude", "longitude", "operator", "charger_type"]
OUTPUT_COLUMNS = [
    "charger_id", "source_objectid", "station_name", "station_address", "latitude",
    "longitude", "lga_name", "postcode", "source", "operator", "number_of_plugs",
    "charger_type", "status", "charger_rating_raw", "charger_rating_kw", "sa4_code",
    "sa4_name", "sa4_assignment", "sa4_distance_m",
]


def parse_rating_kw(text) -> float | None:
    """Return the highest per-connector power in kW, or None if there is none.

    "22 kW" and "22" -> 22; "2x350kW & 2x175kW" -> 350; "AC" -> None.
    """
    if pd.isna(text):
        return None
    per_connector = re.sub(r"\d+x", "", re.sub(r"\s", "", str(text).lower()))  # drop "2x"
    powers = [float(p) for p in re.findall(r"\d[\d.]*", per_connector)]
    return max(powers) if powers else None


def completeness(df: pd.DataFrame, stage: str) -> pd.DataFrame:
    """Non-null counts per column, in long format so stages can be stacked."""
    return pd.DataFrame({
        "stage": stage,
        "column": df.columns,
        "non_null": df.notna().sum().values,
        "total": len(df),
        "pct_complete": (df.notna().mean() * 100).round(1).values,
    })


def load_raw() -> pd.DataFrame:
    df = pd.read_csv(EV_CSV_PATH, encoding="utf-8-sig", dtype={"PCODE": "string"})
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")
    required = {"latitude", "longitude", "operator", "charger_type", "charger_rating"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"EV data is missing required columns: {sorted(missing)}")
    return df


def clean_text(df: pd.DataFrame) -> pd.DataFrame:
    text_columns = df.select_dtypes(include=["object", "string"]).columns
    for column in text_columns:
        df[column] = df[column].str.strip().replace("", pd.NA)
    df["station_address"] = df["station_address"].str.replace(r"\s*[\r\n]+\s*", ", ", regex=True)
    for column in text_columns.drop("station_address"):
        df[column] = df[column].str.replace(r"\s+", " ", regex=True)
    return df


def clean_fields(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=COLUMN_RENAMES)
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    valid = (
        df["latitude"].between(*LAT_RANGE) & df["longitude"].between(*LON_RANGE)
    )
    print(f"Dropped {(~valid).sum()} rows with missing or out-of-range coordinates.")
    df = df[valid].copy()

    df["source_objectid"] = df["source_objectid"].astype("Int64")  # else written as 696.0
    df["operator"] = df["operator"].map(canonical_operator)
    df["status"] = df["charger_type"].str.lower().map({"upcoming": "upcoming"}).fillna("existing")
    df["charger_type"] = df["charger_type"].str.upper().where(lambda s: s.isin(["AC", "DC"]))
    df["charger_rating_raw"] = df["charger_rating"]
    df["charger_rating_kw"] = df["charger_rating"].map(parse_rating_kw)
    return df.drop(columns="charger_rating")


def drop_near_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse the same charger reported by two source files (see module docstring)."""
    ranked = df.assign(_filled=df.notna().sum(axis=1)).sort_values(
        "_filled", ascending=False, kind="stable"
    )
    groups = []
    for _, group in ranked.groupby(NEAR_DUPLICATE_KEY, dropna=False, sort=False):
        if len(group) > 1 and group["station_name"].nunique() <= 1:
            group = group.bfill().iloc[[0]]  # keep the fullest row, fill its gaps
        groups.append(group)
    return pd.concat(groups).drop(columns="_filled").sort_index()


def load_sa4() -> gpd.GeoDataFrame:
    sa4 = gpd.read_file(SA4_SHAPEFILE_PATH, columns=["SA4_CODE26", "SA4_NAME26"])
    sa4 = sa4.rename(columns={"SA4_CODE26": "sa4_code", "SA4_NAME26": "sa4_name"})
    # Non-geographic codes ("Migratory - Offshore - Shipping", "No usual address", ...)
    # have no polygon and can never contain a charger.
    return sa4[sa4.geometry.notna()]


def assign_sa4(df: pd.DataFrame, sa4: gpd.GeoDataFrame) -> pd.DataFrame:
    """Add sa4_code, sa4_name, sa4_assignment ('within'/'nearest') and sa4_distance_m."""
    points = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df["longitude"], df["latitude"]), crs="EPSG:4326"
    ).to_crs(sa4.crs)

    within = gpd.sjoin(points[["geometry"]], sa4, how="inner", predicate="within")
    within = within[~within.index.duplicated()]  # a point on a shared border matches twice
    result = pd.DataFrame(index=df.index, columns=["sa4_code", "sa4_name"], dtype="string")
    result.loc[within.index, ["sa4_code", "sa4_name"]] = within[["sa4_code", "sa4_name"]]
    result["sa4_assignment"] = pd.Series("within", index=within.index)
    result["sa4_distance_m"] = pd.Series(0.0, index=within.index)

    # Nearest-polygon distances need a projected CRS; degrees are not distances.
    outside = points.loc[points.index.difference(within.index), ["geometry"]]
    if not outside.empty:
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
    df = df.drop_duplicates()
    print(f"After exact-duplicate removal: {len(df)}")
    df = clean_fields(df)
    df = drop_near_duplicates(df)
    print(f"After near-duplicate removal: {len(df)}")

    df = df.reset_index(drop=True)
    df.insert(0, "charger_id", range(1, len(df) + 1))  # objectid is 94% blank, so not a key
    df = assign_sa4(df, load_sa4())

    unassigned = df["sa4_code"].isna().sum()
    print(f"SA4 assignment: {df['sa4_assignment'].value_counts().to_dict()}; unassigned: {unassigned}")
    print(f"Status: {df['status'].value_counts().to_dict()}")

    df[OUTPUT_COLUMNS].to_csv(CLEANED_PATH, index=False)
    quality = pd.concat([completeness(raw, "raw"), completeness(df[OUTPUT_COLUMNS], "cleaned")])
    quality.to_csv(PROCESSED_DATA_DIR / "data_completeness.csv", index=False)
    print(f"Saved {len(df)} cleaned chargers to {CLEANED_PATH}")


if __name__ == "__main__":
    main()
