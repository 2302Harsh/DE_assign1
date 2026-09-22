-- EV charger database (DuckDB).
--
-- Standalone use:  duckdb ev_database.duckdb < schema.sql
-- The GEOMETRY type, ST_* functions and RTREE indexes come from DuckDB's spatial
-- extension. INSTALL downloads it once per machine; LOAD is needed in every session
-- (including any session that only queries the views below).
-- 04_db_storage.py runs this file, then loads the data.
--
-- Design: third normal form. Regions, operators, locations, chargers and plug types
-- each live in one table, so an operator's spelling or a location's SA4 region is
-- stored once. External-source augmentation sits in its own tables because it
-- exists only for the DC chargers that were matched.
--
-- Table overview (see README.md for the full normalisation discussion):
--   sa4_regions          one row per ABS SA4 region (all of them, even with 0 chargers)
--   operators            one row per canonical operator name (shared by all three sources:
--                        TfNSW, Open Charge Map and OpenStreetMap)
--   locations            one row per distinct (latitude, longitude)
--   chargers             one row per physical charger; links a location to an operator
--   plug_types           one row per distinct plug type name reported by an external source
--   charger_matches      one row per DC charger that was matched against an external
--                        source (the augmentation "audit trail": which source, which
--                        method, how far away - see 03_data_augmentation.py)
--   charger_plug_types   join table: which plug types (plug_types) a charger's
--                        matched external site (charger_matches) reports

INSTALL spatial;
LOAD spatial;

-- Drop in dependency order so the script can be re-run on an existing database.
DROP VIEW IF EXISTS v_sa4_coverage;
DROP VIEW IF EXISTS v_chargers;
DROP TABLE IF EXISTS charger_plug_types;
DROP TABLE IF EXISTS charger_matches;
DROP TABLE IF EXISTS chargers;
DROP TABLE IF EXISTS plug_types;
DROP TABLE IF EXISTS operators;
DROP TABLE IF EXISTS locations;
DROP TABLE IF EXISTS sa4_regions;

-- ABS SA4 regions with polygons (lon/lat, GDA2020, simplified to ~50 m). Every
-- geographic region is loaded, including those with no chargers.
CREATE TABLE sa4_regions (
    sa4_code    VARCHAR PRIMARY KEY,
    sa4_name    VARCHAR NOT NULL,
    gcc_name    VARCHAR,
    state_name  VARCHAR NOT NULL,
    area_sqkm   DOUBLE,
    geom        GEOMETRY NOT NULL
);

-- One canonical name per operator; fed by both the TfNSW and OCM operator names.
CREATE TABLE operators (
    operator_id    INTEGER PRIMARY KEY,
    operator_name  VARCHAR NOT NULL UNIQUE
);

-- A physical position. Several chargers (e.g. two operators) can share one location.
-- Coordinates are held only in geom; use ST_X / ST_Y or v_chargers for lon / lat.
CREATE TABLE locations (
    location_id      INTEGER PRIMARY KEY,
    address          VARCHAR,
    postcode         VARCHAR,
    lga_name         VARCHAR,
    geom             GEOMETRY NOT NULL,
    sa4_code         VARCHAR REFERENCES sa4_regions (sa4_code),
    -- 'within' = point-in-polygon; 'nearest' = point just outside every polygon.
    sa4_assignment   VARCHAR CHECK (sa4_assignment IN ('within', 'nearest'))
);

-- Charger characteristics. charger_type is NULL for upcoming sites (AC/DC unknown);
-- charger_rating_kw is the highest per-connector power and NULL when not stated.
CREATE TABLE chargers (
    charger_id         INTEGER PRIMARY KEY,
    location_id        INTEGER NOT NULL REFERENCES locations (location_id),
    operator_id        INTEGER NOT NULL REFERENCES operators (operator_id),
    station_name       VARCHAR,
    charger_type       VARCHAR CHECK (charger_type IN ('AC', 'DC')),
    status             VARCHAR NOT NULL CHECK (status IN ('existing', 'upcoming')),
    charger_rating_kw  DOUBLE CHECK (charger_rating_kw > 0),
    number_of_plugs    INTEGER CHECK (number_of_plugs > 0),
    source             VARCHAR
);

CREATE TABLE plug_types (
    plug_type_id    INTEGER PRIMARY KEY,
    plug_type_name  VARCHAR NOT NULL UNIQUE
);

-- Audit trail for augmentation against the two external sources (see
-- 03_data_augmentation.py). One row per DC charger that was *attempted*
-- against a source that found at least one nearby candidate; a charger with
-- no row here either isn't a DC charger or had no candidate from either
-- source within range (data/processed/augmentation_audit.csv has the full
-- accepted/rejected/no-candidate trail for every DC charger, including those
-- with no row here).
--   augmentation_source  which source this match came from
--   source_poi_id        the matched site's id in that source (OCM's numeric
--                        ID, or OSM's "node/<id>" / "way/<id>")
--   ext_*                the matched site's own operator/name/address/postcode,
--                        kept alongside chargers.operator_id (the TfNSW value)
--                        so the two can be compared rather than one overwriting
--                        the other
--   num_points           the source's count of charging bays (NULL if unstated)
--   num_connectors       sum of connector counts across all of the site's connections
--   rate_kw              highest per-connector charging power the source reports, in kW
--   price_per_kwh        AUD per kWh parsed from usage_cost; 0 = free; NULL = not
--                        parseable or not offered by this source (OpenStreetMap
--                        has no priced-tariff field, so this is always NULL there)
CREATE TABLE charger_matches (
    charger_id           INTEGER PRIMARY KEY REFERENCES chargers (charger_id),
    augmentation_source  VARCHAR NOT NULL CHECK (augmentation_source IN ('open_charge_map', 'openstreetmap')),
    match_method         VARCHAR NOT NULL CHECK (match_method IN ('operator_and_distance', 'distance_only')),
    match_distance_m     DOUBLE NOT NULL,
    source_poi_id        VARCHAR NOT NULL,
    ext_operator_id       INTEGER REFERENCES operators (operator_id),
    ext_name              VARCHAR,
    ext_address           VARCHAR,
    ext_postcode          VARCHAR,
    usage_cost            VARCHAR,
    price_per_kwh         DOUBLE CHECK (price_per_kwh >= 0),
    num_points            INTEGER,
    num_connectors        INTEGER,
    rate_kw               DOUBLE CHECK (rate_kw > 0)
);

-- Plug types reported by the matched external site (replaces a comma-joined string).
CREATE TABLE charger_plug_types (
    charger_id    INTEGER NOT NULL REFERENCES charger_matches (charger_id),
    plug_type_id  INTEGER NOT NULL REFERENCES plug_types (plug_type_id),
    PRIMARY KEY (charger_id, plug_type_id)
);

-- R-tree indexes speed up spatial queries (ST_Within, ST_Intersects, nearest-
-- polygon searches, ...) on these two geometry columns the way a normal
-- B-tree index speeds up an equality/range lookup on an ordinary column.
CREATE INDEX idx_locations_geom ON locations USING RTREE (geom);
CREATE INDEX idx_sa4_regions_geom ON sa4_regions USING RTREE (geom);

-- Flat, analysis-friendly view of a charger with its operator, location and region.
CREATE VIEW v_chargers AS
SELECT c.charger_id, c.station_name, o.operator_name, c.charger_type, c.status,
       c.charger_rating_kw, c.number_of_plugs, c.source,
       l.address, l.postcode, l.lga_name,
       ST_Y(l.geom) AS latitude, ST_X(l.geom) AS longitude,
       l.sa4_code, r.sa4_name, r.state_name
FROM chargers c
JOIN operators o USING (operator_id)
JOIN locations l USING (location_id)
LEFT JOIN sa4_regions r USING (sa4_code);

-- Existing chargers per SA4 region, including regions with none (the coverage gaps).
CREATE VIEW v_sa4_coverage AS
SELECT r.sa4_code, r.sa4_name, r.state_name, r.area_sqkm,
       count(c.charger_id) AS existing_chargers,
       count(c.charger_id) FILTER (WHERE c.charger_type = 'DC') AS existing_dc_chargers
FROM sa4_regions r
LEFT JOIN locations l USING (sa4_code)
LEFT JOIN chargers c ON c.location_id = l.location_id AND c.status = 'existing'
GROUP BY r.sa4_code, r.sa4_name, r.state_name, r.area_sqkm;
