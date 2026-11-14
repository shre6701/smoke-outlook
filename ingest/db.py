"""Postgres connection and schema."""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def connect() -> psycopg.Connection:
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise SystemExit("POSTGRES_PASSWORD not set (copy .env.example to .env)")
    try:
        return psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            user=os.getenv("POSTGRES_USER", "smoke"),
            password=password,
            dbname=os.getenv("POSTGRES_DB", "smoke"),
            connect_timeout=5,
        )
    except psycopg.OperationalError:
        raise SystemExit("Postgres not reachable. Start it: docker compose up -d") from None


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute((Path(__file__).parent / "schema.sql").read_text())
    conn.commit()
