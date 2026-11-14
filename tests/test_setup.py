import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

FOLDERS = [
    "agent", "ingest", "spark_jobs", "weather", "sim", "dbt", "model",
    "airflow", "streaming", "api", "web", "bandit", "k8s", "tests",
]  # fmt: skip

ENV_KEYS = [
    "FIRMS_MAP_KEY", "DATAGOV_API_KEY", "XKDR_API_KEY", "OPENAQ_API_KEY",
    "NVIDIA_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
]  # fmt: skip


def test_folders_exist():
    missing = [f for f in FOLDERS if not (ROOT / f).is_dir()]
    assert not missing


def test_env_example_has_keys():
    text = (ROOT / ".env.example").read_text()
    missing = [k for k in ENV_KEYS if f"{k}=" not in text]
    assert not missing


def test_env_file_not_tracked():
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()


@pytest.mark.db
def test_postgis_available():
    import psycopg

    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        pytest.skip("POSTGRES_PASSWORD not set (copy .env.example to .env)")
    dsn = (
        f"host={os.getenv('POSTGRES_HOST', 'localhost')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"user={os.getenv('POSTGRES_USER', 'smoke')} "
        f"password={password} "
        f"dbname={os.getenv('POSTGRES_DB', 'smoke')} connect_timeout=3"
    )
    try:
        conn = psycopg.connect(dsn)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (docker compose up -d)")
    with conn:
        version = conn.execute("SELECT postgis_version()").fetchone()[0]
    assert version
