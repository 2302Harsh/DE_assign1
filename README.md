# NSW EV Charger Pipeline

Downloads NSW electric-vehicle charger locations (Transport for NSW) and ABS SA4
boundaries, cleans the chargers, assigns each one to an SA4 region, augments DC fast
chargers with pricing / plug / operator data from two free public sources (Open
Charge Map and OpenStreetMap), and stores everything in a normalised DuckDB database
for coverage analysis.

```
01_download_data.py            data/raw/ev_chargers.csv, data/raw/SA4_shapefile/
02_clean_and_spatial_join.py   data/processed/ev_chargers_cleaned_sa4.csv, data_completeness.csv
03_data_augmentation.py        data/raw/ocm_au_pois.json + osm_au_charging.json (caches),
                                data/processed/ev_chargers_augmented.csv, augmentation_audit.csv
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
`data/raw/ocm_au_pois.json` and the pipeline runs offline. Step 03's second source,
OpenStreetMap, needs no key at all. **Do not put `.env` in a submission zip.**

## Run

```
python 01_download_data.py          # skips files that already exist; --force to re-download
python 02_clean_and_spatial_join.py
python 03_data_augmentation.py      # --refresh to re-download both external sources
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

**Result: 260 of 431 DC chargers (60.3%) augmented** - 166 via Open Charge Map (the
primary source), 94 via OpenStreetMap (a secondary source, used only for chargers OCM
couldn't match). 171 remain unmatched; 9 chargers had a nearby candidate that was
rejected as a conflict and never got a fallback match. An earlier, single-source,
lower-validation version of this script reached 42.9% (185/431); see below for what
changed and why the number moved the way it did.

### Two sources, tried in order

Both sources are downloaded **once, in bulk, for the whole of Australia** and cached in
`data/raw/` (`ocm_au_pois.json`, `osm_au_charging.json`), then matched offline. That
replaces one API call per charger with two calls total, is fully reproducible, and a
failed run costs nothing to repeat - rerun the script and it picks up from the cache.

1. **Open Charge Map (OCM)** - the same primary source as before, still requires a free
   API key (`OPENCHARGEMAP_API_KEY` in `.env`).
2. **OpenStreetMap (OSM)**, via its Overpass query API - a secondary source used only for
   chargers OCM did not match. **No API key needed.**

**Why OSM and not Google Places/Maps**, which the brief suggested first: Google Places
has no free bulk "every EV charger in a region" query - covering all of NSW would mean
one paid, billing-enabled-key call per charger (or per small search grid), which this
project can't require of anyone re-running it, and its results don't carry a
structured "charging network operator" field the way OCM and OSM both do, which the
matching below depends on. OSM's Overpass API covers the same ground for free, with no
key, tags `operator` explicitly, and fits the same bulk-download-and-cache shape
already used for OCM.

### Matching rules (identical for both sources)

Each DC charger gets the single best candidate among external sites that report a
DC-capable connector:

1. `operator_and_distance`: closest candidate within 500 m whose canonical operator
   equals the charger's (nearest wins).
2. `distance_only`: otherwise, closest candidate of any operator within 50 m.
3. `none`: no qualifying candidate. Missing data is stored as NULL, never the string
   `'Unknown'`.

A candidate that satisfies one of those two rules is still **rejected**, not matched, if
its postcode or leading street number conflicts with the charger's - unless the two are
within 50 m of each other, where GPS proximity is treated as stronger evidence than
address text (address quality varies a lot between sources: one record might use a
business name or a highway-frontage address where another uses a laneway/reception
address for the very same site). Every attempt - accepted, rejected, or no candidate
found - is logged per charger per source in `data/processed/augmentation_audit.csv`,
with the reason for any rejection.

### Why 100 m became 50 m, and why validation was added at all

The original 03 queried the OCM API per charger with `maxresults: 1` inside 100 m,
taking whatever came back with no cross-check - the only way it could be wrong was if a
different site happened to be the single closest thing within 100 m. Moving to a bulk
download and evaluating *every* nearby candidate made room for real validation, and
testing turned up two problems the old code couldn't have caught:

- An 83 m gap was found between two *different* operators' real, distinct chargers on
  the same street (Evie and JOLT, Dee Why). 100 m without an operator match was not a
  safe cutoff for "closest site = same site", so the no-operator-match radius
  (`DISTANCE_ONLY_RADIUS_M`) was tightened to 50 m for both sources.
- OCM's `Postcode` field sometimes includes a state prefix ("NSW 2630"), which a naive
  string comparison would flag as a conflict against a clean "2630" - fixed by
  extracting the 4-digit code before comparing (`clean_postcode()`).
- A naive address check on its own was too strict: one true match had the same GPS
  point to within 6 cm but different address text on each side (a highway address vs a
  reception address for the same large complex), which the address-number check alone
  would have rejected. That's why address conflicts are only enforced beyond 50 m
  (`TRUST_DISTANCE_M`) - within that range, distance already proves it.

Radii were **not** simply widened to hit the target - `OPERATOR_MATCH_RADIUS_M` stayed
at 500 m throughout. Coverage came from adding the second source and improving
candidate selection, not from accepting more distant guesses; the closer, safer
cross-operator radius (50 m vs the original 100 m) if anything makes the primary
source's matches stricter than before.

### Rejections found (not just theorised)

All 16 rejections in the audit trail are genuine-looking conflicts, e.g. a JOLT charger
1.7 m from an OCM candidate with a different postcode text (`2140` vs `2135` - a data
entry difference, not a location match); an Evie charger 454 m from an OCM candidate on
a different street (`65 Hume St` vs `88 Christie St` - almost certainly a different Evie
site in the same suburb). None of the 260 accepted matches were flagged.

### DC-connector requirement

Both sources are filtered to sites reporting a DC-capable connector before matching -
OCM via its `CurrentType`/connector-title fields, OSM via specific socket keys
(`chademo`, `tesla_supercharger`, `tesla_supercharger_ccs`, `type1_combo`,
`type2_combo`; bare `type1`/`type2`/`tesla`/`as3112`/`wall` are AC-only and excluded).
For OSM this is a real trade-off: only 807 of 1,617 Australian charging_station entries
carry that detail, so requiring it costs matches (94 vs 153 if the requirement were
dropped and operator identity trusted alone) in exchange for not treating an
AC-destination-charger entry as evidence for a DC charger. Coverage is comfortably above
target either way, so the safer, filtered version was kept.

### Coverage by focus operator (Evie, JOLT, Chargefox, Exploren, Ampol, BP, Tesla, NRMA)

| Operator | Matched | Total |
|---|---|---|
| Evie | 62 | 103 |
| JOLT | 13 | 49 |
| Chargefox | 22 | 50 |
| Exploren | 5 | 23 |
| Ampol | 22 | 32 |
| BP | 23 | 32 |
| Tesla | 51 | 56 |
| NRMA | 54 | 67 |

Tesla, NRMA, Ampol and BP - all with strong, distinctive OCM/OSM presence - see the best
coverage. JOLT and Exploren, run as many small kerbside/council installations, are the
weakest: OCM and OSM simply don't catalogue as many of those individually.

### Other fields

- **Price**: `usage_cost` keeps the source's raw text (OCM only; OSM has no priced-tariff
  field); `price_per_kwh` is the parsed AUD/kWh (`FREE` -> 0, `55c/kWh` and `$0.55/kWh`
  -> 0.55). Ranges, several different prices, and text without a per-kWh price are left NULL.
- **`rate_kw`**: the highest per-connector charging power the matched source reports, in
  kW - from OCM's `PowerKW` field, or OSM's `socket:*:output` tags (a few OSM entries give
  watts instead of kW without saying so; anything above 1000 is treated as watts and converted).
- **`num_points` / `num_connectors`**: `num_points` is the source's count of charging
  bays; `num_connectors` sums the connector counts across all of a site's connections
  (OCM's `Quantity`, OSM's per-socket-type counts). Both differ from TfNSW's
  `number_of_plugs` because the sources count different things.
- **Operator**: TfNSW, OCM and OSM operator names all go through the same
  canonicaliser (`common.canonical_operator`), so `BP Pulse (AU)`, `bp pulse` and `BP`
  become one operator, and OSM's `AmpCharge` / `Plus ES` resolve to TfNSW's `Ampol` /
  `PLUS ES`. Placeholders such as OCM's `(Unknown Operator)` become NULL.
  `chargers.operator_id` is always the TfNSW operator; the matched site's own operator is
  stored separately on `charger_matches.ext_operator_id` for comparison, never silently
  overwriting the TfNSW value.
- **`augmentation_source` / `source_poi_id`**: which source matched (`open_charge_map` /
  `openstreetmap`) and the site's id in that source (OCM's numeric id, or OSM's
  `node/<id>` / `way/<id>`), so a match can be traced back to the original record.

### Audit trail

`data/processed/augmentation_audit.csv` has one row per (DC charger, source attempted):
`decision` is `accepted`, `rejected` or `no_candidate`; rejected rows carry
`reject_reason` (`postcode_conflict`, `address_conflict`, or both). A charger unmatched
by OCM but later matched by OSM appears twice - once per source - so the full history of
how (or whether) it was resolved is visible, not just the final outcome.

## Database design (step 04)

```
sa4_regions 1 ---< locations 1 ---< chargers >--- 1 operators
                                       | 1                ^
                                       0..1               |
                                  charger_matches ---------+ (ext_operator_id)
                                       | 1
                                       *
                              charger_plug_types >--- 1 plug_types
```

| Table | Rows | Holds |
|---|---|---|
| `sa4_regions` | 89 | Every geographic SA4 region (including those with no chargers) with its polygon, simplified to ~50 m |
| `operators` | 43 | Canonical operator names from all three sources (TfNSW, OCM, OSM) |
| `locations` | 1,938 | Address, postcode, LGA, point geometry, SA4 code |
| `chargers` | 1,947 | Type, status, rating (kW), plugs, program; FKs to location and operator |
| `plug_types` / `charger_plug_types` | 5 / 407 | Plug types of the matched external site |
| `charger_matches` | 260 | Only accepted matches: source, method, distance, matched site's operator/name/address/postcode, price, plug counts, rate |

`charger_matches` holds **accepted matches only** - a DC charger that was attempted but
rejected or found no candidate has no row here (that full trail lives in
`data/processed/augmentation_audit.csv`, not the database, since it covers *attempts*,
which are not part of the normalised charger/location/operator model).

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
  be mostly NULL columns; a separate `charger_matches` table also holds which source and
  method matched, and how far away.
- **Coordinates once.** Latitude/longitude are held only in `geom` (with an R-tree
  index); the view exposes them as plain columns.
- **Constraints.** `CHECK`s on `charger_type`, `status`, positive ratings and match
  method, plus enforced foreign keys.

The cost is joins. For a 1,947-row dataset that is negligible, and the views give
analysts the flat, denormalised shape when they want it. A single wide table would be
simpler to load but would repeat operator names and SA4 attributes on every row and
hide which fields are missing because nothing matched.

## Limitations

- 171 of 431 DC chargers (39.7%) still have no match: neither OCM nor OSM has a
  DC-capable site within range for them (mostly Evie, Chargefox, Exploren and JOLT -
  see the focus-operator table above).
- `charger_rating_kw` (TfNSW) and `rate_kw` (matched source) are both per connector, not
  per site; site totals are not derived.
- OSM's `addr:postcode`/street tags are sparse (about 1% and 3% of entries respectively),
  so the conflict check has little to work with there; it relies more on the
  DC-connector requirement and the operator/distance rules than on address validation.
- SA4 polygons in the database are simplified (0.0005 degrees). Assignment in step 02
  used the full-resolution polygons. `04_db_storage.py` checks that every location
  still lies in its assigned polygon.
- The near-duplicate rule keys on coordinates, so the same physical charger recorded
  with slightly different coordinates is not caught.
