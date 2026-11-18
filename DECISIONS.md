# Decisions

One line each: what — why — alternative.

## Stage 0
- Python >=3.12 with uv, `package = false` — flat top-level folders, no build step needed — alternative: installable `src/` package.
- `airflow/`, `dbt/`, `web/`, `k8s/` have no `__init__.py` — a Python package named `airflow` would shadow the real Airflow library — alternative: rename folders.
- Postgres image `postgis/postgis:16-3.4` — official PostGIS build, stable — alternative: Postgres 17 or TimescaleDB.
- Kafka and Airflow are Compose profiles, off by default — 16 GB RAM laptop; they are v3 work — alternative: always on.
- Kafka `apache/kafka:3.9.1` single-node KRaft, 1 GB cap — no ZooKeeper, least memory — alternative: Redpanda.
- Airflow `apache/airflow:3.0.6` in `standalone` mode, metadata in a separate `airflow` database on the same Postgres, 3 GB cap — one container, fine for dev — alternative: full multi-container Airflow.
- Test for PostGIS skips (not fails) when the DB is down — tests stay runnable without Docker — alternative: hard fail.
- pre-commit: ruff, ruff-format, large-file (1 MB) and private-key checks — stops data files and keys getting committed — alternative: CI only (CI is Stage 9).
- Data files (`data/`, GRIB, NetCDF, Zarr, Parquet) are git-ignored — size and source terms — alternative: Git LFS.
- LLM settings come only from `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` — provider swap without code changes (SPEC §8) — alternative: provider-specific config.
- Postgres password only from `.env` (compose fails fast if unset, no default anywhere) — no credential in git, GitGuardian clean — alternative: Docker secrets (overkill for local dev).

## Stage 1
- GFS grid stored as one cropped NetCDF per cycle in `data/gfs/`, catalogue row in Postgres — grids are arrays, not rows; about 2 MB per cycle — alternative: PostGIS raster or one row per grid cell.
- GFS 0–72 h hourly, domain + 1° pad, raw GRIB deleted after cropping — smoother particle motion; about 700 MB download and 2.5 min per cycle, 6 MB kept — alternative: 3-hourly (`--gfs-step 3`, a third of the download).
- "Latest cycle" = newest init whose f072 file exists on AWS — never a half-uploaded run — alternative: take the newest folder.
- `published_at` = source publish time when given (GFS: S3 Last-Modified), else time first fetched (`published_basis = 'first_seen'`), never updated — honest point-in-time bound — alternative: assume publish = observation time (leaks future data).
- Schema applied from `ingest/schema.sql` on each run (idempotent) — works on an existing DB volume — alternative: Docker init script (runs only on a fresh volume) or dbt (later).
- Live stations come from OpenAQ v3 (`/locations` by bbox + `/parameters/2/latest`, about 25 calls) — CPCB-direct is unreachable here and OpenAQ's CPCB mirror is fresh again — alternative: CPCB portal API (base64-wrapped, terms unchecked).
- "Latest" values older than 7 days are not stored — dead sensors are not current data — alternative: store all and filter later.
- data.gov.in is optional: without `DATAGOV_API_KEY` it reports "skipped" — site is down, key not available — alternative: fail the whole run.
- data.gov.in values stored as `pm25_avg_as_published`, unit "as published" — the feed's unit could not be checked — alternative: assume µg/m³.
- Punjab EPA (aqi.punjab.gov.pk) not used — HTTP 403 to scripts and no terms readable — alternative: scrape it (not without permission).
- HTTP errors log host and status only — FIRMS and data.gov.in put the key in the URL — alternative: log full URLs.
- Status exits 1 when a required source is STALE or EMPTY; limits: GFS 14 h, FIRMS 24 h, stations 6 h, CAMS fetch 24 h — usable by a scheduler later — alternative: print only.

- Readings keyed by OpenAQ sensor id — a station can carry more than one PM2.5 sensor — alternative: one reading per station and hour.
- `--every MIN` loop in `ingest.run` for hourly runs — builds station history now without v3 tooling — alternative: cron or Airflow (v3).
- ISRO INSAT AOD not built, listed under Later — outside SPEC; column AOD is not ground PM2.5 and MOSDAC terms are unclear — alternative: add as a validation layer after the terms are read.

## Stage 2
- Pure NumPy particle model, 10-minute steps, bilinear/linear interpolation — 100k particles × 2 members in 73 s on the laptop — alternative: numba or a compiled model (HYSPLIT/FLEXPART; heavier, licence terms).
- Boundary layer: particles inside it are re-mixed over its full depth every step; particles above stay aloft until it grows back — simple, testable well-mixed assumption with a night residual layer — alternative: turbulent velocity scheme.
- Ground concentration = in-layer mass / (cell area × local boundary-layer height) — far less noisy than counting particles in a thin surface slab — alternative: fixed 0–100 m layer.
- Emissions: FRP × 1 h × 0.368 kg/MJ (Wooster 2005) × EF 6.26–8.0 g/kg (Akagi 2011, Andreae 2019) — cited, transparent, no tuning — alternative: scale to match observations now (not before Stage 3 validation).
- No hand-tuned scale factor although values are about 100× too low — a fitted factor belongs to validation, with held-out data — alternative: borrow a factor from the literature.
- Clusters by PostGIS `ST_ClusterDBSCAN` (0.05°, min 1 point) — one SQL line, no extra library — alternative: grid cells or scikit-learn.
- Receptor value = mean over a ±0.25° box — city-scale average, stable with 100k particles — alternative: single grid cell.
- Hours before the newest cycle starts use the previous stored cycle (else the first forecast hour) — fires seen before the cycle still get real winds, point-in-time safe — alternative: start all smoke at cycle start.
- Ensemble members = previous cycles (≤ 12 h older) with the same fires; range = min/max member × low/high emission factor — SPEC §4 — alternative: perturbed winds.
- Gridded output in `data/sim/*.nc`, tables for run, clusters and contributions in Postgres — grids are arrays; tables are queried by the map — alternative: all in Postgres.
- `ingest.run` keeps the previous GFS cycle too (`--gfs-back 1`) — needed for the ensemble and pre-cycle winds; +700 MB download once per cycle — alternative: single cycle, no forecast uncertainty.
- Country/region tagging of clusters left to Stage 2b — boundary choice is sensitive and needs the owner's decision — alternative: pick a boundary file now.

- Database tests use a throwaway database per test (`tests/conftest.py`) and skip when Postgres is down — real tables are never touched — alternative: transactions on the real database.
- Test weather and fires are made-up arrays, used only inside tests — exact answers are known, so physics can be checked — alternative: recorded real files (large, answers unknown).
- Particle count stays at 100,000 — noise 3–8%, 78 s; 400,000 gains under 4% for 4× the time — alternative: 400,000 (still inside the budget).

## Improvement round (after Stage 2)
- Boundaries: Natural Earth 1:10m countries, admin-1 and disputed areas; disputed areas are their own class and win over any country — neutral, same rule on every side; public domain — alternative: one country's official map, or OSM (ODbL).
- Ledger days are UTC days — India and Pakistan clocks differ by 30 min; neither is favoured — alternative: local day per city.
- Central emission = burned-area method (FINN v2.5: 0.14 km²/pixel, 902 g/m²), low = fire-power method; both stored per row — two published methods bracket the truth better than one; no fitting to our own observations — alternative: one method with a tuned factor.
- High emission factor 13.4 g/kg (FINN v2.5 crops), low 6.26 (Akagi 2011), central 7.13 — spans the published range — alternative: Andreae (2019) only.
- Fire event = touching pixels in one 6-hour pass window, sized by the satellite with more pixels — removes NOAA-20/21 double sightings without merging separate fires — alternative: count every detection.
- City contributions come from backward runs (500 particles per city-hour), the map field from the forward run — every backward particle counts for its city, so ranking noise drops — alternative: forward only with 4× particles.
- Backward weights are multiplied by exp(−divergence × time) — needed for forward/backward agreement in real winds (no vertical wind in the model) — alternative: ingest vertical velocity and advect in 3-D.
- Forward/backward ratio outside 0.67–1.5 marks a city low confidence — a built-in, per-run quality flag — alternative: no flag.
- Wind levels use GFS geopotential height minus terrain; levels under the terrain sit just above ground — alternative: standard-atmosphere heights.
- Weather area = domain + 4° — byte-range downloads fetch the whole globe anyway, so this costs only 13 MB per cycle — alternative: + 1°.
- WAQI not used — its terms forbid archived redistribution and it gives an index only — alternative: use it for display only (still needs notice).
- Repeating today's fires on days 2–3 not built — it invents future inputs; owner's call under the "no synthetic data" rule — alternative: build it as a labelled what-if.

## Hardening round before merge
- Past winds = first 6 hours of each older GFS cycle (72 h back) — every past hour uses the newest forecast issued by then: real, point-in-time safe, and small — alternative: reanalysis (not allowed in backtests) or no history.
- Fires of the last 72 h are simulated, results reported from 24 h before the run — smoke lives for days; yesterday's fires matter today — alternative: last 24 h only (under-supplies today).
- Backward air older than 96 h is dropped — it has lost its smoke; keeps runtime inside budget — alternative: follow all air to the first fire.
- Venting rule: convergence removes mass (forward), divergence dilutes (backward weight ≤ 1) — the model has no vertical wind; this keeps both directions consistent and removes heavy-tailed weights — alternative: ingest vertical velocity for 3-D transport.
- Under-terrain pressure levels take the 10 m wind — GFS fills those levels with extrapolated values — alternative: mask mountain cells.
- "Measured vs simulated" is shown as context only, never as skill — measured PM2.5 includes all sources; skill needs Stage 3 — alternative: hide it.
- Merged to main by pull request after the clean-clone test — one reviewed history; earlier stage branches are contained in it — alternative: merge each stage branch separately.

## Fires on forecast days
- Future fires = mean of the last 3 days, same place and time of day — best on totals in the season backtest (23–32% error) and tied best at 100 km; matches the 72 h fire window — alternative: repeat the last day (35% error), 7-day mean (biased low while the season grows), day-of-year climatology (needs the archive).
- The what-if is stored and shown separately from observed-fire results, with its own label — it is an assumption, not data ("no synthetic data" rule) — alternative: fold it into the main forecast.
- Targeting and the cross-border ledger use observed fires only — the backtest shows place is not predictable — alternative: include assumed fires.
- Method is chosen on 1° block error, not 25 km cells — smoke travels 100+ km, and no method has skill at 25 km — alternative: cell error (would favour over-smoothed 7-day means).
- Season backfill rows are marked `published_basis = 'backfill'` with today's fetch time — they can never pass as data that was available earlier — alternative: pretend they were live.
- Forward particle cap raised to 150,000 — observed and assumed fires share it — alternative: keep 100,000 (noisier map).

## Stage 2b
- Web app is plain HTML/CSS/JS modules with no build step — no Node on the machine, nothing to compile, easy to read — alternative: React/Vite build.
- MapLibre GL JS 5.24.1 and deck.gl 9.4.1 from unpkg at fixed versions, cached by the service worker — each file is over the 1 MB commit limit — alternative: vendor them in the repo.
- Basemap = our own Natural Earth boundaries served by the API — one neutral depiction of borders everywhere, works offline — alternative: third-party tiles (their border choices, extra terms).
- Grids drawn on canvases as MapLibre canvas sources; points and lines in deck.gl (overlaid, not interleaved) — both work on the globe — alternative: deck.gl BitmapLayer.
- Binary formats: uint8 log-scaled fields, uint16 particle positions, int8 winds — about 1.6 MB for everything on first load — alternative: JSON (10× larger).
- Targeting weight = city population from Natural Earth populated places — public domain, already in our source — alternative: gridded population (new dataset).
- Fire markers, India, Pakistan, disputed and what-if colours come from a colour-blind-checked palette; countries always carry a text label — never colour alone — alternative: red/green.
- Static hot spots (10+ days of the last 45 at one ~1 km spot) are dropped before simulation — crop fires do not burn one spot for weeks — alternative: FIRMS "type" flag (archive product only).
- Headless-browser libraries were unpacked into the scratch folder, not installed system-wide — no sudo, nothing changed on the machine — alternative: `playwright install-deps`.

## Stage 3
- One hindcast run per day at 12:00 UTC with the 06Z forecast cycle — why: it is the first run that has the afternoon satellite pass (most fires) and a complete cycle (published about 09:50 UTC); alternative: every cycle (4× the cost for little extra).
- Hindcasts in their own tables (`hindcast_*`), not `sim_*` — why: a past-season run can never show up on the live map; alternative: a flag on `sim_run`.
- Point-in-time enforced by a database CHECK on `hindcast_run` plus a pytest — why: dbt is not set up yet; alternative: wait for the dbt test (Stage 7/9).
- Archive fires = FIRMS standard product with an assumed 3-hour publish delay — why: the live feed of 2024/2025 is no longer served and the archive has no publish time; alternative: none found.
- Archive weather every 3 hours, not every hour — why: 40 GB download instead of 120 GB; a test on live data gave 5–13% more smoke at two cities and a near-identical day-to-day pattern; alternative: hourly.
- Hindcast uses the newest forecast cycle only and no forward (map) run — why: receptor numbers come from the backward run; keeps a run at about 7 minutes; alternative: full live configuration (3× the time).
- Stations: OpenAQ archive for 2025 and for Lahore, XKDR for India in 2024 — why: XKDR's CPCB data ends 2025-09-01 (the spec expected it to cover 2025), and OpenAQ's archive has no CPCB stations here in 2024; alternative: one source only (would lose a season).
- City value = median of reference monitors each hour; low-cost sensors only when no reference monitor reports — why: same rule in both countries; alternative: mean (sensitive to one bad sensor).
- Values at the instrument limit are kept and flagged, not dropped — why: SPEC §3 (censored, not missing); a median is not moved by them.
- Skill is judged on arrival (rises and spikes), not on amounts — why: the model has fire smoke only and stations measure everything; alternative: error in µg/m³ (would mostly measure the missing sources).
- "Useful" rule fixed before seeing results: spike AUC ≥ 0.65 and change correlation's 90% range above zero, in every season with data — why: avoids tuning the bar to the result; alternative: judge by eye.
- Ranges from a 5-day block bootstrap — why: consecutive days are not independent; alternative: ordinary bootstrap (ranges too narrow).
- No fitted emission scale is applied to the live map — why: a straight-line fit also absorbs non-fire pollution on calm days, so it is an upper guide only; it is reported, not used.
- CAMS benchmark = Open-Meteo history (same-day CAMS runs) — why: older CAMS forecast runs are not served there; this favours CAMS and is stated; alternative: Copernicus ADS download (new account, larger job).
- DSS benchmark = only values quoted with a date in a published source (`docs/ref/dss_delhi_stubble_share.csv`) — why: no download API (SPEC §3); alternative: none without access.
- Ledger range = how far a share moved between the day-ahead and same-day hindcast — why: measures real forecast uncertainty, independent of the emission scale; alternative: member spread only (already shown).
- Fire query: the hot-spot list is computed once per query (`MATERIALIZED`) — why: with a full season loaded the old form took over 10 minutes; results are identical.
- Java 17 runtime in the user folder for Spark — why: no admin rights needed; alternative: system package.

### Terms / licence per source (as understood on 2026-11-14; re-check before any public launch)
- NOAA GFS on AWS Open Data — US Government open data, no use restrictions; credit NOAA/NCEP.
- NASA FIRMS — free and open (NASA data policy); MAP_KEY limit 5,000 transactions per 10 min; cite NASA FIRMS / LANCE; NRT data is not for life-safety decisions.
- OpenAQ v3 — free API key, 60 requests/min; licence belongs to each original provider, so credit OpenAQ plus the provider (CPCB, AirGradient: CC BY-SA 4.0, Hawanama, AirNow). Per-location licence field was empty for CPCB: to verify.
- data.gov.in — Government Open Data License – India (attribution required); free key. Not yet used.
- Open-Meteo air-quality API (CAMS) — data CC BY 4.0, free tier is non-commercial and under about 10,000 calls/day; credit Open-Meteo and "Copernicus Atmosphere Monitoring Service".
- Punjab EPA — terms not found; not used.
- Natural Earth — public domain; no restriction, credit optional ("Made with Natural Earth").
- WAQI / aqicn.org — not used: no redistribution of cached or archived data, attribution and prior notice required.
- MapLibre GL JS (BSD-3-Clause) and deck.gl (MIT), loaded from unpkg; map fonts from demotiles.maplibre.org (Noto Sans, OFL).
- FINN v2.5 — only published method constants are used (Wiedinmyer et al. 2023, CC BY 4.0), not its data.
- OpenAQ archive on AWS (`openaq-data-archive`) — public, no key; data keeps each provider's licence (CPCB, AirNow / US State Department, AirGradient and other sensor networks); credit OpenAQ and the provider.
- XKDR Forum India Air Quality Database — CC BY 4.0; credit "India Air Quality Database, XKDR Forum" plus CPCB and AirNow; used for validation only.
- NASA FIRMS standard (archive) product — same terms as the near-real-time feed; credit NASA FIRMS.
- Scope is India-centric (15 Nov 2026): receptors are Indian cities only; ledger shows inflow split by Indian state and neighbouring country; fires abroad still simulated — owner wants the product to answer "what reaches Indian cities and from where" — alternative: two-way ledger with Lahore/Dhaka/Kathmandu as receptors (dropped).
- No station ingest outside India — no receptors abroad, and Punjab EPA terms were unclear — alternative: Pakistan stations for validation; cost: foreign source shares can't be checked at the source, and the ledger says so.
- Regions phased North (v1) → East, West, South (v3); v1 domain widened to 82°E to include Lucknow — keeps the 10-minute laptop run and disk budget — alternative: all India from v1 (about 6x the area).
- West/South get the stagnation outlook first; ledger shown there only with validated skill — fire smoke is expected to be a small share of their pollution, to be measured in the hindcast, not assumed — alternative: leave those cities out.
- Ledger rows carry `source` (country class) plus `state` (Indian state, else empty); map bars keep one colour per country class with Indian states as separate labelled segments in A–Z order — same method for states and countries, order never by rank — alternative: one colour per state (not readable, not colour-blind safe).
- Old Lahore rows (simulation, hindcast, station archive) stay in existing databases and are filtered out by the receptor list — no destructive migration — alternative: delete them.
- Validation "region" replaces "country" as the grouping (scope `region` in `validation_skill`); all v1 receptors are North — alternative: keep a one-country table.
- Lahore removed from published result tables by hand, other numbers untouched, with a dated note in `docs/validation.md` — no database here to regenerate the report — alternative: leave stale tables until the next re-score.
- Lucknow added as a receptor and the domain edge moved to 82°E in code (15 Nov 2026) — the earlier decision was only on paper — alternative: wait for v3.
- Day fires are spread over the afternoon (12:00–18:30 IST, peak 16:30) instead of one hour from the sighting — geostationary images show the burning peaks at 16:00–17:00 IST in 2024–2025 (Jethva 2026, doi:10.3390/rs18101506); a later peak cut the Delhi bias in another model (Biswal et al. 2025, doi:10.1039/d5ea00052a) — alternative: geostationary fire data itself (still under Later: needs a terms check).
- Day fires are multiplied by 2.5 for the fires the 13:30 pass misses (56% missed in 2024, 64% in 2025, mean 60%; Jethva 2026) — one factor for all years and all places, so ledger shares do not move and no year gets its own hindsight number — alternative: a factor per year (leaks later knowledge into each season) or only for Indian Punjab/Haryana (different method per source area).
- That timing and factor are hindsight for the 2024–2025 hindcasts (published 2026) — said in the report; inputs stay point-in-time — alternative: validate on a 2026 season only (not over yet).
- Emission per detection (FINN area and fuel) left as it was — after the 2.5 factor the season total should be near published inventories (Liu et al. 2020: about 10 Tg dry matter a year across north India); to be checked on the fetched fires before the re-run — alternative: fit a scale to stations (mixes in non-fire pollution).
- Smoke followed for 5 days (fires and air age 120 h, was 72 h and 96 h); the what-if repeat for coming days still uses the last 3 days — still spells last about that long — alternative: 7 days (to be compared on sample days; about 1.5x the run time).
- Second test target, fixed before any new result: Diwali fireworks days left out, and the change in forecast ventilation removed from both measured and simulated change (partial rank correlation); same thresholds; both targets always shown, verdict uses the adjusted one — total PM2.5 change also holds fireworks and still-air build-up — alternative: a smoke tracer (CO, PM2.5/PM10) from stations (not in the archive tables yet).
- Each hindcast run now stores forecast ventilation (boundary-layer height × 10 m wind) per city and hour — needed for the second target, weather only — alternative: re-read weather files at scoring time.
- Station archive download retries 500/503 and connection errors up to 4 times with backoff — the OpenAQ S3 archive returned a 500 mid-run and killed the whole fetch — alternative: skip failed days (would silently drop measurements).
- Re-score kept the thresholds and both targets unchanged; result (0 of 4 useful) published as is — rules were fixed before results — alternative: none considered.
