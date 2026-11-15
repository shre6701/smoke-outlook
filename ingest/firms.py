"""NASA FIRMS active fires (VIIRS NOAA-20 / NOAA-21, near real time) for the v1 domain."""

import csv
import io
import os
from datetime import UTC, date, datetime, timedelta

from ingest import config
from ingest.http import get

URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{w},{s},{e},{n}/{days}"


def parse_csv(text: str, source: str) -> list[dict]:
    """FIRMS CSV -> rows. acq_time is HHMM in UTC without leading zeros."""
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        hhmm = r["acq_time"].zfill(4)
        observed = datetime.strptime(f"{r['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=UTC)
        lat, lon = float(r["latitude"]), float(r["longitude"])
        if not config.in_domain(lat, lon):
            continue
        rows.append(
            {
                "source": source,
                "observed_at": observed,
                "latitude": lat,
                "longitude": lon,
                "frp_mw": float(r["frp"]) if r.get("frp") else None,
                "confidence": r.get("confidence"),
                "daynight": r.get("daynight"),
                "bright_ti4": float(r["bright_ti4"]) if r.get("bright_ti4") else None,
                "scan": float(r["scan"]) if r.get("scan") else None,
                "track": float(r["track"]) if r.get("track") else None,
                "version": r.get("version"),
            }
        )
    return rows


def _store(conn, rows: list[dict], published_at: datetime, basis: str) -> int:
    new = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO fire_detection (source, observed_at, published_at, published_basis,
                    latitude, longitude, geom, frp_mw, confidence, daynight, bright_ti4,
                    scan, track, version)
                VALUES (%(source)s, %(observed_at)s, %(pub)s, %(basis)s,
                    %(latitude)s, %(longitude)s,
                    ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326),
                    %(frp_mw)s, %(confidence)s, %(daynight)s, %(bright_ti4)s,
                    %(scan)s, %(track)s, %(version)s)
                ON CONFLICT (source, observed_at, latitude, longitude) DO NOTHING
                """,
                {**r, "pub": published_at, "basis": basis},
            )
            new += cur.rowcount
    conn.commit()
    return new


def _fetch(key: str, source: str, days: int, start: date | None = None) -> list[dict]:
    url = URL.format(
        key=key, source=source, days=days,
        w=config.WEST, s=config.SOUTH, e=config.EAST, n=config.NORTH,
    )  # fmt: skip
    if start is not None:
        url += f"/{start:%Y-%m-%d}"
    text = get(url).text
    if not text.startswith("latitude,"):
        raise RuntimeError(f"FIRMS {source}: unexpected response (check key / quota)")
    return parse_csv(text, source)


def ingest(conn, days: int = 4) -> tuple[str, int, str]:
    key = os.getenv("FIRMS_MAP_KEY")
    if not key:
        return "skipped", 0, "FIRMS_MAP_KEY not set"
    fetched_at = datetime.now(UTC)
    new, seen = 0, 0
    for source in config.FIRMS_SOURCES:
        rows = _fetch(key, source, days)
        seen += len(rows)
        new += _store(conn, rows, fetched_at, "first_seen")
    return "ok", new, f"{seen} detections in the last {days} d, {new} new"


def backfill(conn, start: date, end: date) -> int:
    """Fetch older detections (for testing methods on this season). They are stored with
    published_basis = 'backfill' and today's fetch time, so no live run can mistake them for
    data that was available earlier than it really was fetched here."""
    key = os.environ["FIRMS_MAP_KEY"]
    fetched_at = datetime.now(UTC)
    new, day = 0, start
    while day <= end:
        for source in config.FIRMS_SOURCES:
            new += _store(conn, _fetch(key, source, 5, day), fetched_at, "backfill")
        day += timedelta(days=5)
    return new


if __name__ == "__main__":  # uv run python -m ingest.firms 2026-09-25
    import sys

    from ingest import db

    conn = db.connect()
    db.ensure_schema(conn)
    first = date.fromisoformat(sys.argv[1])
    print(f"{backfill(conn, first, datetime.now(UTC).date())} new detections since {first}")
