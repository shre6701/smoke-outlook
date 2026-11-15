# Progress

Target machine: Windows + WSL2, 16 GB RAM, no GPU, 20-50 GB free disk.

## Stage 0 — Setup (done)

**Works**
- `docker compose up -d` starts Postgres 16 + PostGIS 3.4 (healthy; `airflow` database also created).
- Optional profiles start and respond: `--profile kafka` (single-node KRaft), `--profile airflow` (standalone, port 8080).
- `uv sync`, `uv run pytest` (4 tests pass, incl. a PostGIS connection test), ruff, pre-commit hooks.
- `.env.example` with every key and its sign-up link.

**Thin**
- All checks were run in a Linux cloud container, not on the WSL2 laptop. Run the README quick start there once.
- Airflow is `standalone` mode (dev only). No DAGs yet.
- Folders are empty placeholders.

**Risks to plan around**
- Disk (20-50 GB): Docker images already take about 4 GB with Airflow. Two-season hindcast in Stage 3 must keep only subset GRIB fields for the v1 domain and delete raw files after use.
- RAM (16 GB): don't run Kafka + Airflow + simulation together under WSL2; set a WSL memory cap if Windows gets slow.

## Stage 1 — Live inputs (done, 2026-11-14)

**Works** (run on the WSL2 laptop, real data)
- One command: `uv run python -m ingest.run` (about 2.5 min for a new GFS cycle, seconds otherwise). `--every 60` repeats it hourly so station readings build a history. Each source runs alone; a failure or missing key never stops the others.
- `uv run python -m ingest.status`: rows, newest timestamp, age and ok / STALE / EMPTY per source; exit code 1 if a required source is stale.
- GFS: latest complete cycle from `noaa-gfs-bdp-pds`, byte-range subset (U/V at 925/850/700 hPa and 10 m, HPBL, 2 m TMP), 0–72 h hourly (73 times), cropped to the v1 domain + 1°, saved as `data/gfs/*.nc` with a catalogue row in `gfs_cycle`.
- FIRMS VIIRS NOAA-20 + NOAA-21 NRT (last 2 days) → `fire_detection` (PostGIS points, FRP).
- Stations via OpenAQ v3 → `station`, `station_reading`; reference vs low-cost labelled. First run: India 148 reference + 43 low-cost reporting, Pakistan 190 low-cost.
- CAMS PM2.5 for the 4 receptors via Open-Meteo → `cams_forecast` (every fetch kept).
- Every table has `observed_at`, `published_at`, `published_basis`, `ingested_at`. Re-running adds no duplicates.
- data.gov.in is optional: no `DATAGOV_API_KEY` → "skipped", shown as SKIPPED in status.

**Thin**
- data.gov.in code is written from the published record layout but never run (site down, no key). Its values are stored "as published" until the unit is checked.
- CPCB's own feed: `airquality.cpcb.gov.in` and `cpcb.nic.in` time out from this network, so no CPCB-direct fetcher was built. India live data is CPCB as mirrored by OpenAQ (fresh today, but it was stale in Oct 2026).
- Pakistan: no reference monitor is reporting on OpenAQ right now (low-cost only). Punjab EPA is not used: the site refuses scripted access and its terms could not be read.
- `published_at` is a real source time only for GFS (S3 Last-Modified). For FIRMS, stations and CAMS it is the time we first fetched the row.
- GFS: 2 m TMP only (no temperature on pressure levels).
- OpenAQ's latest-values listing repeats some entries across pages (705 listed, about 430 distinct); duplicates are dropped by key. Readings are now keyed by sensor id.
- No real scheduler: `--every 60` is a plain loop in a terminal (Airflow is v3). No dbt tests yet.
- uv was missing on the laptop; installed with `pip install --user uv` (needs `~/.local/bin` on PATH). Docker Desktop must be started first.

**Next**
- When data.gov.in is back: add `DATAGOV_API_KEY`, run `uv run python -m ingest.run --only datagov`, check the unit.

## Stage 2 — Simulation engine (done, 2026-11-14)

**Works** (live run on the laptop, real fires and real forecast winds)
- One command: `uv run python -m sim.run`. First run: 659 detections → 292 clusters, 100,000 particles, 2 forecast-cycle members, **73 s** (budget 10 min). Runtime is stored in `sim_run.runtime_s`.
- Forward 72 h from the fire clusters of the last 24 h: hourly gridded fire-smoke PM2.5 (0.1°, 80×100) in `data/sim/*.nc`.
- Contribution table: `sim_contribution` (cluster × receptor × hour × member); `sim_cluster` (centre, FRP, emission); view `sim_receptor_outlook` with low/high range.
- Backward run from the 4 receptors: footprint per receptor (where its air comes from), in the same NetCDF.
- Uncertainty: low/high emission factor × neighbouring forecast cycles (`ingest.run` now also keeps the previous cycle).
- Physics tests first, 12 of them: zero wind, uniform wind, boundary-layer cap, night residual layer, deposition decay, mass/volume, downwind-only contribution, backward footprint upwind, weather stitching.
- Point-in-time: a run uses only fires and GFS cycles with `published_at` before its start.

**Thin**
- **Absolute values are far too low**: peak fire smoke at the receptors is about 0.1 µg/m³, against tens to hundreds observed in this season. Cause is emissions, not transport: 33 t PM2.5/day from satellite FRP with "1 detection = 1 hour of burning". Small, short crop fires are mostly missed between satellite passes. **Shares and rankings are usable; absolute µg/m³ are not** until Stage 3 fits a scale factor against observations.
- Only fires already seen are released (no "same fires again tomorrow"), so days 2–3 are under-supplied.
- Wind level heights use the standard atmosphere as height above ground: fine on the plains, wrong in the hills. No terrain.
- No wet deposition (no rain field ingested), no chemistry (secondary particles ignored).
- Horizontal mixing (1000 m²/s) and the 1-hour burn are assumptions, not cited values. The dry-deposition and Andreae (2019) numbers should be checked against the sources before publishing.
- 65% of particles leave the weather grid within 72 h (domain + 1° is small); returning smoke is lost.
- Backward run ignores deposition. Clusters have no country/region tag yet.
- Ensemble: 2 cycles only (3 once more cycles are stored).

### Test pass after Stage 2 (2026-11-14)

**Checked**
- 35 automated tests pass (`uv run pytest`, about 35 s). The 11 database tests run on a throwaway database that is created and dropped each time; real data is never touched.
- Covered: schema applied twice; upgrade of an older table; FIRMS and OpenAQ ingest idempotent, duplicates and dead sensors dropped, lon/lat order; an error page is a failure, not "no fires"; one failing source does not stop the rest and never prints a key; status STALE/EMPTY flags and exit code; a full simulation run in both UTC and India time zones, incl. point-in-time (fires and forecast cycles published after the run start are ignored).
- Forward and backward runs agree within 15% for a steady source (reciprocity). Same seed gives identical output.
- On the real data: random noise 3–8% per city at 100,000 particles; 400,000 particles change results by under 4%; halving the time step changes them by up to 11%. 25,000 particles is too few (up to 32% off).
- Real inputs are physically sensible: Delhi boundary layer about 100 m at night and 2,100 m mid-afternoon; winds up to 16 m/s; no negative or over-range station values.

**Fixed during the pass**
- Times read from Postgres were assumed to be UTC; a non-UTC session would have shifted fires by hours.
- Low end of the range ignored a member with zero smoke; hours a member does not cover are no longer counted as zero either (new table `sim_receptor_hourly`).
- `sim.run` split into a testable `run()`.

**Still weak**
- The top-10 cluster ranking per city is only partly stable between random seeds (6–8 of 10 shared). Rank small contributors with care, or use more particles (400,000 takes about 2.5 min per member).
- 161 fire pairs are the same fire seen by both satellites within 90 minutes; each counts as its own hour of burning.
- Stations median 141 µg/m³ and CAMS 62–77, against simulated fire smoke of about 0.1: the emission scale problem above, confirmed.
- Not tested: the data.gov.in fetcher against the real site; GFS download failure half-way; behaviour with zero fires in season; anything on a second machine.

### Improvement round (2026-11-14, branch `stage-2c-boundaries-emissions`)

**Added**
- **Boundaries:** Natural Earth (public domain) countries, states/provinces and disputed areas in table `boundary`; SQL function `place_of(point)`. A point in a disputed area is tagged `disputed` with the area's name, on every side, never as either country. Each fire cluster now has `country` and `region`; view `sim_source_share` gives the daily share per city by source (UTC days, forecast hours only).
- **Emissions, two cited methods side by side:** A) burned area, 0.14 km² per detected pixel × 902 g/m² crop fuel (FINN v2.5) — central and high; B) fire power × 1 h (Wooster 2005) — low. First run: 562 t PM2.5 (range 28–1,056 t). The range is honest: a factor of about 40.
- **No double counting:** the same fire seen by NOAA-20 and NOAA-21 in one pass window is one event, sized by the satellite that saw more pixels (659 detections → 555 events).
- **City numbers from backward runs:** air is followed back from each city and meets the fires on the way. Top-10 cluster ranking is now 6–10 of 10 stable between seeds (was 3–9), noise 2–6%.
- **Self-check in every run:** forward and backward doses are compared per city; outside 0.67–1.5 the city is printed and stored as low confidence. Now: Delhi 0.98, Lahore 1.10, Amritsar 0.87, **Chandigarh 0.48 (low confidence)**.
- **Divergence correction** for backward runs (found by the self-check: without it the two methods differed by up to 2× in real winds).
- **True level heights:** GFS geopotential height and terrain are ingested; 925 hPa is 570 m above the Punjab plain, not the 762 m assumed before.
- **Wider weather area** (domain + 4°, was 1°): particles lost from the grid fall from 65% to 42%. No extra download.
- **Zero-fire day** gives a zero run, not an error. Outlook peaks are reported for forecast hours only.
- **Status** now watches India *reference* stations separately, so a stale CPCB mirror cannot hide behind fresh low-cost sensors.
- 42 tests (was 35), incl. disputed class, two-satellite merge, terrain heights, spreading wind, daily shares.

**Station alternatives to data.gov.in (checked, none added)**
- data.gov.in and CPCB's own sites: still down / unreachable from this network.
- WAQI (aqicn.org): terms forbid redistributing cached or archived data, ask for notice before public use, and the feed is an index, not concentrations. Not usable for validation or a stored history.
- XKDR database: an archive (last export 2026-09-30), not live. Still right for Stage 3.
- OpenAQ stays the live source (148 India reference stations reporting).

**Still weak**
- Absolute smoke is still low: peaks of 1–2 µg/m³ at the cities against station medians near 140 (all sources). Only the last 24 h of fires are released and no fitted scale is used. Stage 3 decides.
- Chandigarh, next to the mountains: forward and backward disagree by 2×.
- Shares rest on fires seen in the last 24 h only; they are simulated and unvalidated, in both directions.
- Burn fraction for method A is assumed to be 100% (not read from the FINN paper). The 1-hour burn (method B) and horizontal mixing remain assumptions.
- Not built (needs your decision, touches the "no synthetic data" rule): repeating today's fires on forecast days 2–3.

### Hardening round before merge (2026-11-14, branch `stage-2d-history-hardening`)

**Added**
- **Three days of fires with real past winds.** Ingest keeps the first hours of every GFS cycle of the last 72 h (11 small files, about 1 GB download once, then one per new cycle) and 4 days of FIRMS. The run follows fires of the last 72 h and reports from 24 h before the run to 72 h ahead. Today: 2,277 detections → 1,733 events → 679 clusters, about 2,000 t PM2.5 (range 106–3,733 t).
- **Venting rule.** Where wind piles air up, the surplus (with its smoke) leaves through the top of the boundary layer; where wind spreads, clean air dilutes. This removed a false 9.8 µg/m³ spike at Chandigarh and makes forward and backward runs consistent.
- Pressure levels under the terrain use the surface wind (no made-up mountain flow).
- **Measured vs simulated line** in every run (measured = all pollution sources; low-cost sensors labelled).
- GFS files are written atomically; one missing old cycle no longer fails the ingest.
- pre-commit hooks installed on this machine (they had never run here; they caught one style issue).

**Tested**
- 46 automated tests pass, here and in a clean clone with a fresh environment (stand-in for a second machine).
- New tests: failed weather download leaves no half-written cycle and does not stop other sources; data.gov.in fetcher with paging (against a stand-in server, still never against the real site); past-wind cycles are used but never count as forecast members; piling-up wind; zero-fire day.
- Real ingest into a brand-new empty database works; the simulation then says clearly that weather is missing.
- On real data: country shares differ by at most 1 point between random seeds; city totals vary 0–7%; halving the time step changes totals by 0–10%; 7–9 of the top-10 clusters are stable.
- Forward/backward self-check: Delhi 0.92, Lahore 1.13, Amritsar 1.00, Chandigarh 0.62 (still flagged low confidence; its India share is 19% backward vs 29% forward).

**Still weak**
- **Simulated fire smoke is 0–3% of measured PM2.5** at the four cities right now (e.g. Delhi 0.1 of 201 µg/m³). Measured values include all sources, so this is not a skill score, but published stubble shares for mid-November are usually far higher. Treat absolute values as too low until Stage 3; shares and rankings are the usable output.
- Runtime is now about 3.5 min (was 2 min) because of the 3-day history; budget is 10 min.
- 95% of the smoke has left the weather grid, deposited or vented after 72 h; smoke that would return from outside is lost.
- GFS files are never pruned (about 80 MB per day).
- Still assumptions, not cited: 100% burn of fuel in a detected pixel, 1-hour burn (low method), horizontal mixing rate.
- Not built, awaiting your decision: repeating today's fires on forecast days 2–3.

### Fires on forecast days: research, backtest, what-if (2026-11-14)

**Research**
- US systems (HRRR-Smoke, NOAA air-quality forecast) repeat the latest day's fires.
- India's DSS (IITM) uses seen fires on day 1 and the long-term average for that calendar day on days 2–5. That needs a multi-year archive, which we will have in Stage 3.
- NASA Earth Observatory (Dec 2025): most stubble fires are now lit at 4–6 pm local time, after the early-afternoon satellite pass, so VIIRS misses many. This is the most likely reason our absolute smoke is far too low.

**Backtest on this season's real fires** (25 Sep – 13 Nov 2026, 38,000 detections backfilled; `uv run python -m sim.fire_forecast`)
- Exact place is not predictable: at 25 km cells every method is only 3–12% better than "no future fires".
- At 100 km blocks, a 2–3 day mean cuts the error by 27–37%.
- Totals are predictable: the 3-day mean misses the domain total by 23% (1 day ahead) to 32% (3 days), with similar skill for India (28–35%) and Pakistan (29–36%).
- Repeating only the last day is worse (35% on totals); adding a trend is worse still (44%).
- **Chosen: mean of the last 3 days**, repeated at the same place and time of day.

**Built**
- Every run now also gives a labelled what-if: observed fires plus assumed repeats. Stored separately everywhere (`pm25_whatif_ugm3`, `pm25_assumed_ugm3`, NetCDF `pm25_fire_whatif`); observed-only numbers are unchanged. Targeting and the ledger use observed fires only.
- Today: what-if peaks are 1–9× the observed-only peaks (e.g. Lahore 0.7 → 6.4 µg/m³).
- 48 tests pass. Runtime about 5 min with 3 forecast members (budget 10).

**Limits**
- The what-if is good for "how much smoke, roughly", not for "which fire". Backtest is one season, one satellite family, and backfilled data (fetched today, not point-in-time).
- Weather is not used (rain or a harvest ending will break it).


## Stage 2b — Live map (done, 2026-11-15)

**Works** (`uv run uvicorn api.main:app --port 8000`, then open http://localhost:8000)
- Installable web app (PWA) on a MapLibre globe with deck.gl: animated smoke particles (near ground vs aloft), smoke field, fires sized by power, animated wind at 4 levels, trapped-air shading, stations with freshness (reference vs low-cost, "no recent reading"), time slider from 24 h before the run to 72 h ahead with play.
- Click a city: outlook chart with low–high band and the what-if as a dashed line, measured PM2.5 now, CAMS value, model self-check, largest source clusters, and a "where its air comes from" overlay. Click a fire: emission range, which cities its smoke reaches, people reached.
- Targeting table (top 20 clusters by smoke × city population, next 48 h, with range across forecast runs) and cross-border ledger (daily shares per city, both directions in one view, ranges, disputed areas as their own class).
- Labels everywhere: "simulated – not yet validated", "Unofficial", the what-if label when that layer is on, a low-confidence warning for Chandigarh.
- The basemap is our own Natural Earth layer, so borders are drawn the same neutral way as in the tables; no third-party map tiles.
- API sends grids and particle tracks as small binary arrays: the whole first load is about 0.4 MB plus 1.2 MB of particle tracks (gzip). No large JSON.
- Permanent industrial hot spots (same spot on 10+ of the last 45 days: kilns, plants) are no longer treated as fires.
- 53 tests pass, incl. 4 API tests. Checked in a headless browser at desktop and phone sizes: loads with no errors, all layers and panels work, offline service worker registers.

**Thin**
- **Smoothness is not measured on real hardware.** The only browser available here renders in software (3 frames/s), which says nothing about a laptop or phone GPU. Please open it on your laptop and phone and tell me if it stutters.
- Libraries (MapLibre, deck.gl) and map fonts load from public CDNs with fixed versions; the first visit needs internet.
- No 7-day replay (SPEC §1): the slider covers 24 h back to 72 h ahead of the latest run only.
- "People reached" counts only the four tracked cities (a lower bound). No population grid.
- The top-ranked cluster today is a large fire area in the Rajasthan desert seen on 6 days; it may not be crop burning (no land-cover data), and crop emission factors are applied to it.
- Wind streaks are exaggerated for visibility and are not to scale.
- No login, no rate limits; meant for local use until Stage 9.

**Next (Stage 3)**
- Validation: hindcast 2024 and 2025 point-in-time, compare with stations, CAMS and DSS; fit and report the emission scale; show skill per city on the map.

## Stage 3 — Validation (done, 2026-11-15) — RESULT: SKILL IS WEAK

**Works** (`docs/validation.md`, README "Validation")
- 122 point-in-time hindcast runs: every day of 1 Oct – 30 Nov in 2024 and 2025 at 12:00 UTC, with the forecast cycle and fires published before then (database check + test). About 6 minutes per run.
- Station archive for the four city boxes by PySpark: OpenAQ archive (2025, Lahore) + XKDR (India 2024); 314,000 station-hours; readings at the instrument limit kept and flagged.
- Benchmarks: persistence, CAMS (same-day runs), the few published DSS values with sources.
- Skill per city and country in README; the map shows a badge per city, a past-seasons table in the city panel, and how far ledger shares moved in hindcasts.
- Labels changed from "not yet validated" to "skill per city shown"; a city is "useful" only if it passes in every season.
- 71 tests pass (18 new). Panels and labels checked in Windows Chrome on live data: no errors (the map canvas itself does not draw in that headless mode, so smoothness is still unchecked).

**Result**
- All 8 city-seasons: "not reliable here". Rise match -0.24 to 0.17, spike score 0.36 to 0.58 (0.5 = coin toss).
- Season-long pattern exists (0.42–0.75) but persistence is better and the simulation adds nothing reliable to it.
- Amounts low by roughly 6–20× against DSS season shares; the 2024→2025 drop in DSS is not reproduced.
- Ledger shares for the same day move 4–26 points (median) between day-ahead and same-day forecasts.

**Thin**
- Archive weather is 3-hourly (live: hourly); on live data that changed city means by 5–13%.
- One forecast member, one run per day, 500 particles per arrival hour.
- DSS comparison rests on 4 daily values and 2 season means from one news report of a CSE analysis.
- Lahore: one reference monitor in 2024, low-cost only in 2025. Amritsar: one station.
- The rise test uses total PM2.5; fireworks (Diwali), traffic and weather-driven build-up are in the measurements and not in the model. CAMS also scores low on it.
- No browser runs inside WSL here (missing system libraries); UI checks used Windows Chrome through a test-only harness.
- XKDR hour convention (start of hour) is assumed, not confirmed.

**Next (agreed 2026-11-15): improve the model, then re-score on the same two seasons**
- Missed late-afternoon fires; emissions per fire; smoke older than 3 days; a better test target than total PM2.5 change.
- Then Stage 3b (fire type) as added to STAGES.md.

## Later
- Land-cover mask so non-crop fires (desert, forest) are labelled and get their own emission factors.
- Gridded population for a real "people reached".
- Geostationary fire data (Meteosat over the Indian Ocean, GEO-KOMPSAT-2A) to catch the 4–6 pm fires that polar satellites miss; check access and terms first.
- Calendar-day fire climatology for days 2–5 (the DSS method) once the 2024–2025 archive is loaded in Stage 3.
- Ingest GFS geopotential height and terrain so wind levels sit at true height above ground.
- Ingest GFS rain rate for wet deposition.
- Persistence emissions (repeat today's fires on forecast days 2–3), clearly labelled.
- ISRO INSAT-3D/3DR/3DS aerosol optical depth (MOSDAC, 30 min, both countries) as an independent check on where the simulated plume is. Column haze, not ground PM2.5, so not a station substitute. Checked 2026-11-14: free with registration; general accounts may get data 3 days late (live needs a privileged account or standing order); redistribution/display terms not stated on the pages read (read `mosdac-data-guidelines` PDF first). NavIC is navigation only: no air-quality data.
- Pakistan reference data: ask Punjab EPA for permission or a feed; US Embassy Lahore/Islamabad monitors show no data on OpenAQ after March 2025.
- Bias-correct Lahore low-cost sensors against the embassy history (Stage 3 topic).
- CPCB-direct fetcher once `airquality.cpcb.gov.in` is reachable and its terms are read.
- Fire type is now Stage 3b. Alerts to authorities (asked 2026-11-15). Possible: label each fire "likely crop-residue burning" from land cover (cropland), harvest calendar, size and time of day, and send the ranked list to district officials (this is the v2 targeting brief, SPEC §2). Not possible from satellites: who lit a fire or why, so no "deliberate" label and no blame wording. Needs first: a land-cover mask, better arrival skill than Stage 3 found, and a named, consenting recipient for any automatic message.
- (ideas only; not built)
