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

### Terms / licence per source (as understood on 2026-11-14; re-check before any public launch)
- NOAA GFS on AWS Open Data — US Government open data, no use restrictions; credit NOAA/NCEP.
- NASA FIRMS — free and open (NASA data policy); MAP_KEY limit 5,000 transactions per 10 min; cite NASA FIRMS / LANCE; NRT data is not for life-safety decisions.
- OpenAQ v3 — free API key, 60 requests/min; licence belongs to each original provider, so credit OpenAQ plus the provider (CPCB, AirGradient: CC BY-SA 4.0, Hawanama, AirNow). Per-location licence field was empty for CPCB: to verify.
- data.gov.in — Government Open Data License – India (attribution required); free key. Not yet used.
- Open-Meteo air-quality API (CAMS) — data CC BY 4.0, free tier is non-commercial and under about 10,000 calls/day; credit Open-Meteo and "Copernicus Atmosphere Monitoring Service".
- Punjab EPA — terms not found; not used.
