"""Shared fixture: a throwaway Postgres database, so tests never touch real data."""

import os
import uuid

import psycopg
import pytest

from ingest import db as db_mod


def _dsn(dbname: str) -> str | None:
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        return None
    return (
        f"host={os.getenv('POSTGRES_HOST', 'localhost')} port={os.getenv('POSTGRES_PORT', '5432')} "
        f"user={os.getenv('POSTGRES_USER', 'smoke')} password={password} dbname={dbname} "
        "connect_timeout=3"
    )


@pytest.fixture
def testdb(monkeypatch):
    """Yields connect(); ingest.db.connect is redirected to the temporary database."""
    admin_dsn = _dsn("postgres")
    if not admin_dsn:
        pytest.skip("POSTGRES_PASSWORD not set")
    try:
        admin = psycopg.connect(admin_dsn, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (docker compose up -d)")
    name = f"smoke_test_{uuid.uuid4().hex[:10]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    opened = []

    def connect(**kw):
        conn = psycopg.connect(_dsn(name), **kw)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db_mod, "connect", connect)
    try:
        yield connect
    finally:
        for conn in opened:
            conn.close()
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.close()
