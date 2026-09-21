"""Load the augmented EV-charger data and SA4 regions into a DuckDB database.

Run with: python 04_db_storage.py

The database file is rebuilt from scratch on every run (DuckDB does not shrink a file
when tables are dropped). database/schema.sql defines the tables; this script splits the
flat augmented CSV into them, loads the SA4 polygons straight from the ABS shapefile,
and finishes with verification checks.
"""

import duckdb
import pandas as pd

from common import (
    AUGMENTED_PATH, DATABASE_DIR, DATABASE_PATH, PROCESSED_DTYPES, SA4_SHAPEFILE_PATH,
    SCHEMA_PATH,
)


# ~50 m in degrees. Keeps the database around 11 MB instead of ~45 MB with full-detail
# coastlines, while every charger still falls in its assigned region under ST_Within
# (checked in verify()).
SA4_SIMPLIFY_TOLERANCE_DEG = 0.0005

REQUIRED_COLUMNS = {
    "charger_id", "station_name", "station_address", "latitude", "longitude", "lga_name",
    "postcode", "source", "operator", "number_of_plugs", "charger_type", "status",
    "charger_rating_kw", "sa4_code", "sa4_assignment", "ocm_poi_id", "ocm_operator",
    "usage_cost", "price_per_kwh", "plug_types", "num_points", "num_connectors",
    "match_method", "match_distance_m",
}
TABLES = [
    "sa4_regions", "operators", "locations", "chargers", "plug_types", "ocm_matches",
    "charger_plug_types",
]
# (child table, foreign-key column, parent table, parent key)
FOREIGN_KEYS = [
    ("locations", "sa4_code", "sa4_regions", "sa4_code"),
    ("chargers", "location_id", "locations", "location_id"),
    ("chargers", "operator_id", "operators", "operator_id"),
    ("ocm_matches", "charger_id", "chargers", "charger_id"),
    ("ocm_matches", "ocm_operator_id", "operators", "operator_id"),
    ("charger_plug_types", "charger_id", "ocm_matches", "charger_id"),
    ("charger_plug_types", "plug_type_id", "plug_types", "plug_type_id"),
]


def lookup_table(names: pd.Series, key: str, value: str) -> pd.DataFrame:
    """Numbered table of the distinct non-null values in *names*."""
    distinct = names.dropna().drop_duplicates().sort_values()
    return pd.DataFrame({key: range(1, len(distinct) + 1), value: distinct.values})


def build_tables(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split the flat augmented CSV into one DataFrame per table (except sa4_regions)."""
    missing = REQUIRED_COLUMNS.difference(df.columns)
    if missing:
        raise ValueError(f"Input data is missing required columns: {sorted(missing)}")

    # Operators come from both sources, already reduced to one canonical spelling.
    operators = lookup_table(pd.concat([df["operator"], df["ocm_operator"]]),
                             "operator_id", "operator_name")
    operator_ids = dict(zip(operators["operator_name"], operators["operator_id"]))

    df = df.assign(location_id=df.groupby(["latitude", "longitude"]).ngroup() + 1)
    locations = df.drop_duplicates("location_id")[
        ["location_id", "station_address", "postcode", "lga_name", "latitude", "longitude",
         "sa4_code", "sa4_assignment"]
    ].rename(columns={"station_address": "address"})

    chargers = df[[
        "charger_id", "location_id", "station_name", "charger_type", "status",
        "charger_rating_kw", "number_of_plugs", "source",
    ]].assign(operator_id=df["operator"].map(operator_ids))

    matched = df[df["match_method"].notna()]
    ocm_matches = matched[[
        "charger_id", "match_method", "match_distance_m", "ocm_poi_id", "usage_cost",
        "price_per_kwh", "num_points", "num_connectors",
    ]].assign(ocm_operator_id=matched["ocm_operator"].map(operator_ids).astype("Int64"))

    charger_plugs = (
        matched[["charger_id", "plug_types"]].dropna()
        .assign(plug_type_name=lambda d: d["plug_types"].str.split(", "))
        .explode("plug_type_name")
    )
    plug_types = lookup_table(charger_plugs["plug_type_name"], "plug_type_id", "plug_type_name")
    plug_type_ids = dict(zip(plug_types["plug_type_name"], plug_types["plug_type_id"]))
    charger_plug_types = pd.DataFrame({
        "charger_id": charger_plugs["charger_id"],
        "plug_type_id": charger_plugs["plug_type_name"].map(plug_type_ids),
    })

    return {
        "operators": operators, "locations": locations, "chargers": chargers,
        "plug_types": plug_types, "ocm_matches": ocm_matches,
        "charger_plug_types": charger_plug_types,
    }


def load_database(con: duckdb.DuckDBPyConnection, tables: dict[str, pd.DataFrame]) -> None:
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    # Regions come straight from the shapefile so that regions with no chargers exist.
    con.execute(f"""
        INSERT INTO sa4_regions BY NAME
        SELECT SA4_CODE26 AS sa4_code, SA4_NAME26 AS sa4_name, GCC_NAME26 AS gcc_name,
               STE_NAME26 AS state_name, AREASQKM26 AS area_sqkm,
               ST_SimplifyPreserveTopology(geom, {SA4_SIMPLIFY_TOLERANCE_DEG}) AS geom
        FROM ST_Read(?)
        WHERE geom IS NOT NULL
    """, [SA4_SHAPEFILE_PATH.as_posix()])

    for name, frame in tables.items():
        con.register(f"stg_{name}", frame)
    con.execute("INSERT INTO operators BY NAME SELECT * FROM stg_operators")
    con.execute("""
        INSERT INTO locations BY NAME
        SELECT * EXCLUDE (latitude, longitude), ST_Point(longitude, latitude) AS geom
        FROM stg_locations
    """)
    # Parents before children: the foreign keys are enforced on insert.
    for name in ["chargers", "plug_types", "ocm_matches", "charger_plug_types"]:
        con.execute(f"INSERT INTO {name} BY NAME SELECT * FROM stg_{name}")


def verify(con: duckdb.DuckDBPyConnection, expected_chargers: int) -> None:
    """Fail loudly if the loaded database is inconsistent."""
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    print("Row counts:", counts)
    problems = []
    if counts["chargers"] != expected_chargers:
        problems.append(f"chargers has {counts['chargers']} rows, expected {expected_chargers}")

    for child, column, parent, key in FOREIGN_KEYS:
        orphans = con.execute(
            f"SELECT count(*) FROM {child} c LEFT JOIN {parent} p ON c.{column} = p.{key} "
            f"WHERE c.{column} IS NOT NULL AND p.{key} IS NULL"
        ).fetchone()[0]
        if orphans:
            problems.append(f"{orphans} orphan rows: {child}.{column} -> {parent}.{key}")

    # Spatial check: every stored location must lie inside the polygon of its SA4 code.
    agree, total = con.execute("""
        SELECT count(r.sa4_code), count(*)
        FROM locations l
        LEFT JOIN sa4_regions r ON l.sa4_code = r.sa4_code AND ST_Within(l.geom, r.geom)
    """).fetchone()
    print(f"Spatial check: {agree}/{total} locations lie inside their assigned SA4 polygon")
    if agree != total:
        problems.append(f"{total - agree} locations are outside their assigned SA4 polygon")

    nsw = con.execute("""
        SELECT count(*) FILTER (WHERE existing_chargers = 0),
               count(*) FILTER (WHERE existing_dc_chargers = 0), count(*)
        FROM v_sa4_coverage WHERE state_name = 'New South Wales'
    """).fetchone()
    print(f"Coverage: of {nsw[2]} NSW SA4 regions, {nsw[0]} have no existing charger "
          f"and {nsw[1]} have no existing DC charger")

    indexes = con.execute("SELECT count(*) FROM duckdb_indexes() WHERE sql LIKE '%RTREE%'").fetchone()[0]
    if indexes != 2:
        problems.append(f"expected 2 R-tree indexes, found {indexes}")
    if problems:
        raise RuntimeError("Verification failed:\n  " + "\n  ".join(problems))
    print("Verification passed.")


def main() -> None:
    for path in (AUGMENTED_PATH, SCHEMA_PATH, SA4_SHAPEFILE_PATH):
        if not path.exists():
            raise FileNotFoundError(f"Required input not found: {path}")
    DATABASE_DIR.mkdir(exist_ok=True)

    df = pd.read_csv(AUGMENTED_PATH, dtype=PROCESSED_DTYPES)
    tables = build_tables(df)

    DATABASE_PATH.unlink(missing_ok=True)
    DATABASE_PATH.with_suffix(".duckdb.wal").unlink(missing_ok=True)
    con = duckdb.connect(str(DATABASE_PATH))
    try:
        load_database(con, tables)
        verify(con, expected_chargers=len(df))
    finally:
        con.close()
    print(f"Database written to {DATABASE_PATH}")


if __name__ == "__main__":
    main()
