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

**Next (Stage 2)**
- Lagrangian model on `data/gfs/*.nc` + `fire_detection`; physics unit tests first; 10-minute budget.
- When data.gov.in is back: add `DATAGOV_API_KEY`, run `uv run python -m ingest.run --only datagov`, check the unit.

## Later
- ISRO INSAT-3D/3DR/3DS aerosol optical depth (MOSDAC, 30 min, both countries) as an independent check on where the simulated plume is. Column haze, not ground PM2.5, so not a station substitute. Checked 2026-11-14: free with registration; general accounts may get data 3 days late (live needs a privileged account or standing order); redistribution/display terms not stated on the pages read (read `mosdac-data-guidelines` PDF first). NavIC is navigation only: no air-quality data.
- Pakistan reference data: ask Punjab EPA for permission or a feed; US Embassy Lahore/Islamabad monitors show no data on OpenAQ after March 2025.
- Bias-correct Lahore low-cost sensors against the embassy history (Stage 3 topic).
- CPCB-direct fetcher once `airquality.cpcb.gov.in` is reachable and its terms are read.
- (ideas only; not built)
