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

# Columns build_tables() needs from the augmented CSV; checked up front so a
# missing/renamed column fails with a clear message here rather than a
# confusing KeyError deep inside build_tables().
REQUIRED_COLUMNS = {
    "charger_id", "station_name", "station_address", "latitude", "longitude", "lga_name",
    "postcode", "source", "operator", "number_of_plugs", "charger_type", "status",
    "charger_rating_kw", "sa4_code", "sa4_assignment", "augmentation_source", "source_poi_id",
    "ext_operator", "ext_name", "ext_address", "ext_postcode", "usage_cost", "price_per_kwh",
    "plug_types", "num_points", "num_connectors", "rate_kw", "match_method", "match_distance_m",
}
# Every table in the database, used by verify() to print row counts.
TABLES = [
    "sa4_regions", "operators", "locations", "chargers", "plug_types", "charger_matches",
    "charger_plug_types",
]
# (child table, foreign-key column, parent table, parent key) - every
# relationship declared in schema.sql, re-checked explicitly in verify()
# as a second line of defence on top of the database's own FOREIGN KEY constraints.
FOREIGN_KEYS = [
    ("locations", "sa4_code", "sa4_regions", "sa4_code"),
    ("chargers", "location_id", "locations", "location_id"),
    ("chargers", "operator_id", "operators", "operator_id"),
    ("charger_matches", "charger_id", "chargers", "charger_id"),
    ("charger_matches", "ext_operator_id", "operators", "operator_id"),
    ("charger_plug_types", "charger_id", "charger_matches", "charger_id"),
    ("charger_plug_types", "plug_type_id", "plug_types", "plug_type_id"),
]


def lookup_table(names: pd.Series, key: str, value: str) -> pd.DataFrame:
    """Numbered table of the distinct non-null values in *names*.

    Used to build the operators and plug_types tables: given a column of
    repeated text values (e.g. "BP", "Tesla", "BP", "Evie", ...), returns one
    row per distinct value with a new integer id (1, 2, 3, ...) - the classic
    "lookup table" / "dimension table" pattern that lets other tables store a
    small integer foreign key instead of repeating the text on every row.
    """
    distinct = names.dropna().drop_duplicates().sort_values()
    return pd.DataFrame({key: range(1, len(distinct) + 1), value: distinct.values})


def build_tables(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split the flat augmented CSV into one DataFrame per table (except sa4_regions).

    The augmented CSV (from step 03) is "wide": one row per charger with every
    attribute - address, operator, OCM match, plug types - in the same row.
    This function reshapes that into the normalised tables schema.sql expects:
    one row per operator, one per location, one per charger, etc. sa4_regions
    is handled separately in load_database() because it comes straight from
    the shapefile, not from this CSV.
    """
    missing = REQUIRED_COLUMNS.difference(df.columns)
    if missing:
        raise ValueError(f"Input data is missing required columns: {sorted(missing)}")

    # Operators come from all three sources, already reduced to one canonical
    # spelling. pd.concat stacks the TfNSW operator column and the matched
    # external site's operator column into one long series before
    # deduplicating, so every source shares one operators table instead of
    # ending up with separate id spaces per source.
    operators = lookup_table(pd.concat([df["operator"], df["ext_operator"]]),
                             "operator_id", "operator_name")
    operator_ids = dict(zip(operators["operator_name"], operators["operator_id"]))

    # groupby(...).ngroup() assigns the same small integer to every row that
    # shares the same (latitude, longitude) pair, in effect generating a
    # location_id without ever having to loop over the rows by hand. +1 makes
    # the ids start at 1 instead of 0, matching the convention used elsewhere.
    df = df.assign(location_id=df.groupby(["latitude", "longitude"]).ngroup() + 1)
    locations = df.drop_duplicates("location_id")[
        ["location_id", "station_address", "postcode", "lga_name", "latitude", "longitude",
         "sa4_code", "sa4_assignment"]
    ].rename(columns={"station_address": "address"})

    chargers = df[[
        "charger_id", "location_id", "station_name", "charger_type", "status",
        "charger_rating_kw", "number_of_plugs", "source",
    ]].assign(operator_id=df["operator"].map(operator_ids))

    # Only DC chargers that were actually matched by 03 (against either
    # external source) have an augmentation_source at all; every other
    # augmentation column is NULL on every other row. charger_matches only
    # ever holds rows for chargers that were successfully matched - a
    # charger that was attempted but rejected or found no candidate has no
    # row here (that full trail, including rejections, is in
    # data/processed/augmentation_audit.csv, not in the database).
    matched = df[df["augmentation_source"].notna()]
    charger_matches = matched[[
        "charger_id", "augmentation_source", "match_method", "match_distance_m", "source_poi_id",
        "ext_name", "ext_address", "ext_postcode", "usage_cost", "price_per_kwh",
        "num_points", "num_connectors", "rate_kw",
    ]].assign(ext_operator_id=matched["ext_operator"].map(operator_ids).astype("Int64"))

    # plug_types in the CSV is a single comma-joined string per charger, e.g.
    # "CCS (Type 2), CHAdeMO". str.split(", ") turns that into a list per row,
    # and explode() then turns each list into its own row (one charger_id can
    # now appear multiple times, once per plug type) - this is what lets the
    # database store plug types in their own normalised table below instead
    # of as one un-queryable string column.
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
        "plug_types": plug_types, "charger_matches": charger_matches,
        "charger_plug_types": charger_plug_types,
    }


def load_database(con: duckdb.DuckDBPyConnection, tables: dict[str, pd.DataFrame]) -> None:
    """Create the schema and load every table, in an order that satisfies foreign keys."""
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    # Regions come straight from the shapefile so that regions with no chargers exist.
    # ST_Read (from DuckDB's spatial extension) can query a shapefile directly
    # with SQL, so the region polygons never need to pass through pandas.
    # ST_SimplifyPreserveTopology reduces how many points describe each
    # polygon's outline (see SA4_SIMPLIFY_TOLERANCE_DEG above) while keeping
    # the polygons valid (no self-intersections) and their shared borders
    # aligned with neighbouring regions.
    con.execute(f"""
        INSERT INTO sa4_regions BY NAME
        SELECT SA4_CODE26 AS sa4_code, SA4_NAME26 AS sa4_name, GCC_NAME26 AS gcc_name,
               STE_NAME26 AS state_name, AREASQKM26 AS area_sqkm,
               ST_SimplifyPreserveTopology(geom, {SA4_SIMPLIFY_TOLERANCE_DEG}) AS geom
        FROM ST_Read(?)
        WHERE geom IS NOT NULL
    """, [SA4_SHAPEFILE_PATH.as_posix()])

    # con.register() makes a pandas DataFrame queryable by name from SQL,
    # without writing it to disk first - "stg_" (staging) distinguishes these
    # temporary, in-memory views from the real tables schema.sql created above.
    for name, frame in tables.items():
        con.register(f"stg_{name}", frame)
    con.execute("INSERT INTO operators BY NAME SELECT * FROM stg_operators")
    # "BY NAME" matches columns by name rather than position, so this still
    # works even though the staging table's column order differs from the
    # real table's (it also drops latitude/longitude in favour of ST_Point()).
    con.execute("""
        INSERT INTO locations BY NAME
        SELECT * EXCLUDE (latitude, longitude), ST_Point(longitude, latitude) AS geom
        FROM stg_locations
    """)
    # Parents before children: the foreign keys are enforced on insert.
    # chargers references locations/operators, charger_matches references
    # chargers, charger_plug_types references charger_matches and plug_types -
    # so this order must be followed or DuckDB will reject the insert.
    for name in ["chargers", "plug_types", "charger_matches", "charger_plug_types"]:
        con.execute(f"INSERT INTO {name} BY NAME SELECT * FROM stg_{name}")


def verify(con: duckdb.DuckDBPyConnection, expected_chargers: int) -> None:
    """Fail loudly if the loaded database is inconsistent.

    Runs a handful of sanity checks after loading (row counts, orphaned
    foreign keys, a spatial query, and the presence of the R-tree indexes)
    and raises with a combined message if any of them fail, so a broken
    load is caught immediately instead of surfacing later as wrong query
    results.
    """
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    print("Row counts:", counts)
    problems = []
    if counts["chargers"] != expected_chargers:
        problems.append(f"chargers has {counts['chargers']} rows, expected {expected_chargers}")

    # Even though every foreign key in schema.sql is already enforced by
    # DuckDB itself, this independently re-derives "orphan" rows (a foreign
    # key value with no matching parent) with a LEFT JOIN, as an extra,
    # visible check that the loaded data is actually consistent.
    for child, column, parent, key in FOREIGN_KEYS:
        orphans = con.execute(
            f"SELECT count(*) FROM {child} c LEFT JOIN {parent} p ON c.{column} = p.{key} "
            f"WHERE c.{column} IS NOT NULL AND p.{key} IS NULL"
        ).fetchone()[0]
        if orphans:
            problems.append(f"{orphans} orphan rows: {child}.{column} -> {parent}.{key}")

    # Spatial check: every stored location must lie inside the polygon of its SA4 code.
    # This is the proof that ST_Within (and the spatial extension generally)
    # is working, and that simplifying the polygons in load_database() didn't
    # push any location outside its assigned region.
    agree, total = con.execute("""
        SELECT count(r.sa4_code), count(*)
        FROM locations l
        LEFT JOIN sa4_regions r ON l.sa4_code = r.sa4_code AND ST_Within(l.geom, r.geom)
    """).fetchone()
    print(f"Spatial check: {agree}/{total} locations lie inside their assigned SA4 polygon")
    if agree != total:
        problems.append(f"{total - agree} locations are outside their assigned SA4 polygon")

    # Informational, not a pass/fail check: reports how many NSW SA4 regions
    # have zero chargers, which is the coverage-gap analysis the assignment
    # brief asks for (made possible by loading every region from the
    # shapefile, not just the regions that happen to contain a charger).
    nsw = con.execute("""
        SELECT count(*) FILTER (WHERE existing_chargers = 0),
               count(*) FILTER (WHERE existing_dc_chargers = 0), count(*)
        FROM v_sa4_coverage WHERE state_name = 'New South Wales'
    """).fetchone()
    print(f"Coverage: of {nsw[2]} NSW SA4 regions, {nsw[0]} have no existing charger "
          f"and {nsw[1]} have no existing DC charger")

    # Informational: how the augmentation split between the two external
    # sources, out of every DC charger (not just NSW - matches 03's own summary).
    dc_total = con.execute("SELECT count(*) FROM chargers WHERE charger_type = 'DC'").fetchone()[0]
    by_source = con.execute("""
        SELECT augmentation_source, count(*) FROM charger_matches GROUP BY 1 ORDER BY 1
    """).fetchall()
    augmented = sum(n for _, n in by_source)
    print(f"Augmentation: {augmented}/{dc_total} DC chargers ({augmented / dc_total:.1%}) - "
          + ", ".join(f"{source}: {n}" for source, n in by_source))

    # Confirms both spatial (R-tree) indexes from schema.sql were actually created.
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

    # Delete any existing database (and its write-ahead-log file) before
    # connecting, so every run starts from a genuinely empty file - dropping
    # and recreating tables inside an existing .duckdb file does not shrink
    # it back down, so old, unused space would otherwise accumulate on every rerun.
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
