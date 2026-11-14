"""Counts and newest timestamp per source, with stale ones flagged.

    uv run python -m ingest.status

Exit code 1 if any required source is STALE or EMPTY.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

from ingest import db
from ingest.config import STALE_HOURS

# label, key in STALE_HOURS, SQL returning (count, newest timestamp), what the timestamp is
CHECKS = [
    ("GFS forecast cycles", "gfs", "SELECT count(*), max(init_time) FROM gfs_cycle", "init"),
    (
        "FIRMS fires (NOAA-20/21)", "firms",
        "SELECT count(*), max(observed_at) FROM fire_detection", "observed",
    ),
    (
        "India stations (OpenAQ)", "stations_in",
        "SELECT count(*), max(r.observed_at) FROM station_reading r JOIN station s "
        "USING (source, station_id) WHERE r.source = 'openaq' AND s.country = 'IN'", "observed",
    ),
    (
        "Pakistan stations (OpenAQ)", "stations_pk",
        "SELECT count(*), max(r.observed_at) FROM station_reading r JOIN station s "
        "USING (source, station_id) WHERE r.source = 'openaq' AND s.country = 'PK'", "observed",
    ),
    (
        "India stations (data.gov.in)", "datagov",
        "SELECT count(*), max(observed_at) FROM station_reading WHERE source = 'datagov'",
        "observed",
    ),
    ("CAMS PM2.5 at receptors", "cams", "SELECT count(*), max(published_at) FROM cams_forecast",
     "fetched"),
]  # fmt: skip
OPTIONAL = {"datagov": "DATAGOV_API_KEY"}


def flag(count: int, newest: datetime | None, limit_h: float, now: datetime) -> str:
    if not count or newest is None:
        return "EMPTY"
    return "STALE" if now - newest > timedelta(hours=limit_h) else "ok"


def main() -> int:
    conn = db.connect()
    db.ensure_schema(conn)
    now = datetime.now(UTC)
    bad = 0
    print(f"Status at {now:%Y-%m-%d %H:%M} UTC")
    print(f"{'source':30} {'rows':>8}  {'newest (UTC)':17} {'age':>7}  {'limit':>5}  flag")
    for label, key, sql, kind in CHECKS:
        count, newest = conn.execute(sql).fetchone()
        f = flag(count, newest, STALE_HOURS[key], now)
        if key in OPTIONAL and not os.getenv(OPTIONAL[key]) and f != "ok":
            f = f"SKIPPED (no {OPTIONAL[key]})"
        elif f != "ok":
            bad += 1
        age = f"{(now - newest).total_seconds() / 3600:.1f}h" if newest else "-"
        when = f"{newest:%Y-%m-%d %H:%M}" if newest else "-"
        print(f"{label:30} {count:>8}  {when:17} {age:>7}  {STALE_HOURS[key]:>4}h  {f} ({kind})")
    split = conn.execute(
        "SELECT s.country, s.is_reference, count(DISTINCT s.station_id) FROM station s "
        "JOIN station_reading r USING (source, station_id) "
        "WHERE r.observed_at > now() - interval '6 hours' GROUP BY 1, 2 ORDER BY 1, 2 DESC"
    ).fetchall()
    for country, ref, n in split:
        print(
            f"  stations reporting in last 6 h: {country} {'reference' if ref else 'low-cost'} {n}"
        )
    last = conn.execute(
        "SELECT DISTINCT ON (source) source, status, note FROM ingest_run ORDER BY source, id DESC"
    ).fetchall()
    for source, status, note in last:
        if status != "ok":
            print(f"  last run {source}: {status} - {note}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
