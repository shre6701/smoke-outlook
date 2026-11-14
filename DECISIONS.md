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
