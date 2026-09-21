# NSW EV Charger Pipeline

Downloads NSW electric-vehicle charger locations (Transport for NSW) and ABS SA4
boundaries, cleans the chargers, assigns each one to an SA4 region, augments DC fast
chargers with pricing / plug / operator data from Open Charge Map (OCM), and stores
everything in a normalised DuckDB database for coverage analysis.

```
01_download_data.py            data/raw/ev_chargers.csv, data/raw/SA4_shapefile/
02_clean_and_spatial_join.py   data/processed/ev_chargers_cleaned_sa4.csv, data_completeness.csv
03_data_augmentation.py        data/raw/ocm_au_pois.json (cache), data/processed/ev_chargers_augmented.csv
04_db_storage.py               database/ev_database.duckdb  (schema in database/schema.sql)
common.py                      shared paths and the operator-name canonicaliser
```

## Setup

Tested with Python 3.14.

```
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env
```

Edit `.env` and set `OPENCHARGEMAP_API_KEY` (free key from openchargemap.org). The key
is only needed the first time step 03 runs; after that the downloaded data is cached in
`data/raw/ocm_au_pois.json` and the pipeline runs offline. **Do not put `.env` in a
submission zip.**

## Run

```
python 01_download_data.py          # skips files that already exist; --force to re-download
python 02_clean_and_spatial_join.py
python 03_data_augmentation.py      # --refresh to re-download the Open Charge Map data
python 04_db_storage.py             # rebuilds the database from scratch, then verifies it
```

Every step is rerunnable. Step 04 ends with checks (row counts, orphan foreign keys,
an `ST_Within` query against the SA4 polygons, R-tree indexes) and exits non-zero if
any fail.

## Cleaning decisions (step 02)

Raw file: 1,958 rows. Cleaned: 1,947.

| Issue | Decision |
|---|---|
| Operator spelling (`BP` / `BP Australia `, `Tesla Motors `, `Non-Networked`, `Evie Networks`, `Charge Hub`, `NRMA Electric`) | Mapped to one canonical name in `common.OPERATOR_ALIASES`. |
| Operator names truncated at 13 characters (`Energy Austra`, `Fast Cities A`, `Viva Energy A`, `PLUS ES Manag`, `University of`) | Completed by alias. `University of` is the University of Wollongong charger (checked against its address). |
| `charger_rating` free text (`22 kW`, `22`, `AC`, `2x350kW & 2x175kW`) | Parsed to `charger_rating_kw`, the highest per-connector power (`2x350kW & 2x175kW` -> 350). `AC` states no power and becomes NULL (519 AC rows). The original text is kept as `charger_rating_raw` in the CSV. |
| 98 `Upcoming` rows | **Kept, flagged** `status = 'upcoming'`, with `charger_type` NULL because AC/DC is unknown for planned sites. Filter on `status = 'existing'` for existing infrastructure. They are never sent for augmentation. |
| Newlines in addresses (733 rows) | Replaced by `, ` so they match the comma-style addresses in the other source file. |
| Near-duplicates (same coordinates + operator + `charger_type`) | 11 rows removed. They are the same charger reported by two source files (one with an `OBJECTID`, one with an LGA and program). The fullest row is kept and its gaps are filled from the dropped row. Rows sharing coordinates but with two different station names (e.g. Jac & Jones vs Latitude 32 Cellar Door) are different sites and are kept. |
| Exact duplicates | None in the raw data. |
| `objectid` 94% blank | Not usable as a key. Kept as `source_objectid` in the CSV only; a generated `charger_id` is the key everywhere. |

### Completeness (percent of rows with a value)

| Column | Raw | Cleaned |
|---|---|---|
| station_name | 26.6 | 26.6 |
| objectid / source_objectid | 6.2 | 6.2 |
| lganame, pcode, source | 93.8 | 94.4 |
| charger_rating_kw (numeric) | – | 73.3 |
| sa4_code | – | 100.0 |
| charger_type | 100.0 | 95.0 (the 98 upcoming rows are NULL) |
| all others (address, operator, plugs, rating text, lat/lon) | 100.0 | 100.0 |

The full per-column table is written to `data/processed/data_completeness.csv`.

### SA4 assignment

Point-in-polygon (`within`) first: 1,946 chargers. Points outside every polygon fall
back to the nearest polygon within 2 km, with distances measured in a projected CRS
(EPSG:9473, GDA2020 Australian Albers) because degrees are not distances. That catches
one coastal charger (Clontarf, 1.8 m outside its polygon). `sa4_assignment` and
`sa4_distance_m` record which rule applied. All 1,947 chargers end up with an SA4 code,
stored as text so `106` never becomes `106.0`.

## Augmentation decisions (step 03)

The script downloads all 1,367 Australian Open Charge Map sites in one request, caches
the raw JSON, and matches offline against the 431 DC chargers. That replaces one API
call per charger, is reproducible, and a failed run costs nothing to repeat.

Each DC charger gets its best candidate among OCM sites that report a DC connector:

1. `operator_and_distance`: same canonical operator within 500 m (nearest wins).
2. `distance_only`: otherwise, any operator within 100 m.
3. `none`: no candidate. Missing data is stored as NULL, never as the string `'Unknown'`.

Result: **185 of 431 DC chargers (42.9%) augmented**; 171 by rule 1, 14 by rule 2. The
old 100 m / first-result approach reached 37.6%. `match_method` and `match_distance_m`
are stored per row (median distance 30 m).

The 50% target is not reached, deliberately. OCM simply has no site near many of these
chargers: even the nearest OCM site of any kind is within 500 m for only about half of
them. Wider radii buy coverage by matching different sites. Same-operator candidates
between 500 m and 1.5 km were often other addresses (e.g. `1 Frederick St` vs
`88 Christie St`). Trade-off, measured with the final code:

| Operator radius | Distance-only radius | DC chargers matched |
|---|---|---|
| 250 m | 100 m | 41.3% |
| 500 m | 50 m | 42.0% |
| **500 m** | **100 m** | **42.9%** |
| 500 m | 250 m | 46.2% |
| 1,000 m | 100 m | 44.8% |
| 1,500 m | 100 m | 47.1% |

Change `OPERATOR_MATCH_RADIUS_M` / `DISTANCE_ONLY_RADIUS_M` in `03_data_augmentation.py`
to reproduce or explore other settings. An address-similarity tier was also tried and
dropped: it added only 4-5 matches at the strictest thresholds and produced obvious false
matches (e.g. `224 Princes Hwy` vs `121 Princes Hwy`) below them.

Other fields:

- **Price**: `usage_cost` keeps OCM's text; `price_per_kwh` is the parsed AUD/kWh
  (`FREE` -> 0, `55c/kWh` and `$0.55/kWh` -> 0.55). Ranges, several different prices
  and text without a per-kWh price are left NULL. 121 of the 150 usage-cost texts parse.
- **`num_points` / `num_connectors`** replace the old `num_bays`, which counted
  connection records. `num_points` is OCM's `NumberOfPoints`; `num_connectors` sums
  each connection's `Quantity` (1 if unstated). They differ from the TfNSW
  `number_of_plugs` because the sources count different things.
- **Operator**: TfNSW and OCM names go through the same canonicaliser, so `BP Pulse (AU)`
  and `BP` become one operator. OCM placeholders such as `(Unknown Operator)` become
  NULL. `chargers.operator_id` is the TfNSW operator; the OCM operator is stored on the
  match row for comparison.

## Database design (step 04)

```
sa4_regions 1 ---< locations 1 ---< chargers >--- 1 operators
                                       | 1                ^
                                       0..1               |
                                    ocm_matches ----------+ (ocm_operator_id)
                                       | 1
                                       *
                              charger_plug_types >--- 1 plug_types
```

| Table | Rows | Holds |
|---|---|---|
| `sa4_regions` | 89 | Every geographic SA4 region (including those with no chargers) with its polygon, simplified to ~50 m |
| `operators` | 42 | Canonical operator names from both sources |
| `locations` | 1,938 | Address, postcode, LGA, point geometry, SA4 code |
| `chargers` | 1,947 | Type, status, rating (kW), plugs, program; FKs to location and operator |
| `plug_types` / `charger_plug_types` | 4 / 306 | Plug types of the matched OCM site |
| `ocm_matches` | 431 | Match method and distance, OCM site, price, points, connectors |

Views: `v_chargers` (flat charger + operator + location + region, with lat/lon from the
geometry) and `v_sa4_coverage` (existing chargers per SA4, including regions with none).
Filter `state_name = 'New South Wales'` for NSW-only analysis; the shapefile is national.

### Normalised vs denormalised

The database is in third normal form, which is the right shape here:

- **Update anomalies.** An operator's name and a location's SA4 region are each stored
  once. Operator spelling was the biggest data-quality problem, and a lookup table makes
  it impossible for `BP` and `BP Australia` to coexist.
- **First normal form.** `plug_types` was a comma-joined string; it is now a junction
  table, so "which chargers have CHAdeMO" is a join, not a `LIKE`.
- **Sparse data.** Augmentation exists only for matched DC chargers. A wide table would
  be mostly NULL columns; a separate `ocm_matches` table also holds the audit trail.
- **Coordinates once.** Latitude/longitude are held only in `geom` (with an R-tree
  index); the view exposes them as plain columns.
- **Constraints.** `CHECK`s on `charger_type`, `status`, positive ratings and match
  method, plus enforced foreign keys.

The cost is joins. For a 1,947-row dataset that is negligible, and the views give
analysts the flat, denormalised shape when they want it. A single wide table would be
simpler to load but would repeat operator names and SA4 attributes on every row and
hide which fields are missing because nothing matched.

## Limitations

- OCM coverage caps augmentation at roughly half of the DC chargers (see above).
- `charger_rating_kw` is per connector; site totals are not derived.
- SA4 polygons in the database are simplified (0.0005 degrees). Assignment in step 02
  used the full-resolution polygons. `04_db_storage.py` checks that every location
  still lies in its assigned polygon.
- The near-duplicate rule keys on coordinates, so the same physical charger recorded
  with slightly different coordinates is not caught.
