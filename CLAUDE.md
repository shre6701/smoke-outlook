# Smoke & GRAP Outlook (India)
Live system: simulate where smoke from real fires goes over 72h using real forecast weather, then turn it into actions (targeting brief, stagnation outlook, GRAP alerts, smoke source ledger, claim checker) for Indian cities. Full spec: `docs/SPEC.md`. Read only the section a stage names.

## Hard rules
- No synthetic data. Inputs are real; simulation outputs and what-ifs are labelled "simulated", with validation skill shown next to them.
- Point-in-time: anything evaluated at time t uses only data published before t (archived forecast runs by init time, never reanalysis, in backtests). A dbt test enforces this.
- India-centric, fair on sources: receptors are Indian cities only. Sources are split by Indian state and by neighbouring country with the same method and wording for both, always shown together. Fires abroad are simulated as sources. No blame language, uncertainty always shown, every claim cited. Say on the ledger that smoke leaving India is not shown.
- Never show a GRAP rule without its source PDF and page. Always show probability, accuracy and "unofficial".
- Minimal user data; no phone numbers in logs. Respect every data source's terms. Ask before anything paid.
- **Phases:** v1 smoke map → v2 decisions → v3 scale (SPEC §0). Don't build anything from a later phase early.
- **Scope lock:** build only what docs/SPEC.md and STAGES.md describe. Suggest ideas in PROGRESS.md under "Later"; don't build them.

## Stack (one job each)
Kafka: live fires, readings, new forecast cycles. Airflow: per-cycle pipeline, daily ingest, weekly retrain. PySpark: OpenAQ archive. Databricks Free: parallel hindcasts. xarray + GRIB byte-range (Herbie): gridded weather from NOAA GFS on AWS. NumPy Lagrangian model: simulation. Postgres+PostGIS, dbt: storage, transforms, tests. FastAPI (+MCP server): APIs. MapLibre (globe) + deck.gl: the live simulation map, the main product. PWA web push: alerts (EN/HI), no third-party chat app. LLM agent (plain OpenAI-compatible client first, then LangGraph via ChatNVIDIA): orders, rules, cited Q&A, briefs, claim checker. LLM settings only from env (LLM_BASE_URL, LLM_MODEL, LLM_API_KEY) so the provider can be swapped without code changes; see SPEC §8. Gymnasium: contextual bandit for alert timing. kind: deploy. Docker Compose, GitHub Actions, uv, ruff, pytest.

## Token rules
- Never print or read raw data, GRIB/Zarr/NetCDF contents, bulk PDFs or big logs. Inspect with small scripts: shapes, counts, schemas, ≤5 sample values.
- Long output: `| tail -n 30` or quiet flags. Run only the tests related to the change.
- Don't re-read files you just wrote. Don't explore the repo broadly. No subagents unless asked.
- Replies: max 5 short lines, simple English, no recaps.
- End of each stage: update `PROGRESS.md` (works / thin / next / Later) and `DECISIONS.md` (one line each: what, why, alternative). Commit and push.
- Blocked → stop and give options in ≤5 lines. Don't guess.
