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

Optional services: `docker compose --profile kafka up -d`, `docker compose --profile airflow up -d`.
On Windows, keep the repo inside the WSL2 filesystem (not `/mnt/c`).
