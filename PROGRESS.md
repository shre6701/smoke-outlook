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

**Next (Stage 1)**
- Fill `.env`: `FIRMS_MAP_KEY`, `DATAGOV_API_KEY`.
- Live inputs: GFS byte-range subset, FIRMS, stations, CAMS → Postgres with publish + observation timestamps.

**Risks to plan around**
- Disk (20-50 GB): Docker images already take about 4 GB with Airflow. Two-season hindcast in Stage 3 must keep only subset GRIB fields for the v1 domain and delete raw files after use.
- RAM (16 GB): don't run Kafka + Airflow + simulation together under WSL2; set a WSL memory cap if Windows gets slow.

## Later
- (ideas only; not built)
