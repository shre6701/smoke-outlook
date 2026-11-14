# Smoke & GRAP Outlook (South Asia)

Simulates where smoke from real fires goes over 72 hours using real forecast weather.
Outputs are simulated and unofficial. Spec: `docs/SPEC.md`. Status: `PROGRESS.md`.

## Quick start

```bash
cp .env.example .env        # then fill in the keys
uv sync
uv run pre-commit install
docker compose up -d        # Postgres + PostGIS
uv run pytest
```

## Live inputs (Stage 1)

```bash
uv run python -m ingest.run      # GFS, FIRMS, stations, CAMS, boundaries -> Postgres
uv run python -m ingest.status   # rows, newest timestamp, STALE flags per source
```

The first ingest downloads about 3 GB of weather (10 min); later runs take seconds unless a new cycle is out.

Then the simulation (Stage 2, about 3.5 min):

```bash
uv run python -m sim.run         # 72 h smoke forecast: grid + cluster-to-city contribution table
```

`uv run python -m sim.fire_forecast` prints the backtest behind the what-if for future fires.

Output is simulated and not yet validated; absolute values are known to be too low (see `PROGRESS.md`).

For ingest, add `--every 60` to repeat hourly. Sources without a key (for example `DATAGOV_API_KEY`) are skipped, not failed.

Optional services: `docker compose --profile kafka up -d`, `docker compose --profile airflow up -d`.
On Windows, keep the repo inside the WSL2 filesystem (not `/mnt/c`).
