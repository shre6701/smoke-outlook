"""NASA FIRMS active fires (VIIRS NOAA-20 / NOAA-21, near real time) for the v1 domain."""

import csv
import io
import os
from datetime import UTC, datetime

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


def ingest(conn, days: int = 4) -> tuple[str, int, str]:
    key = os.getenv("FIRMS_MAP_KEY")
    if not key:
        return "skipped", 0, "FIRMS_MAP_KEY not set"
    fetched_at = datetime.now(UTC)
    new, seen = 0, 0
    for source in config.FIRMS_SOURCES:
        url = URL.format(
            key=key, source=source, days=days,
            w=config.WEST, s=config.SOUTH, e=config.EAST, n=config.NORTH,
        )  # fmt: skip
        text = get(url).text
        if not text.startswith("latitude,"):
            raise RuntimeError(f"FIRMS {source}: unexpected response (check key / quota)")
        rows = parse_csv(text, source)
        seen += len(rows)
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO fire_detection (source, observed_at, published_at, published_basis,
                        latitude, longitude, geom, frp_mw, confidence, daynight, bright_ti4,
                        scan, track, version)
                    VALUES (%(source)s, %(observed_at)s, %(pub)s, 'first_seen',
                        %(latitude)s, %(longitude)s,
                        ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326),
                        %(frp_mw)s, %(confidence)s, %(daynight)s, %(bright_ti4)s,
                        %(scan)s, %(track)s, %(version)s)
                    ON CONFLICT (source, observed_at, latitude, longitude) DO NOTHING
                    """,
                    {**r, "pub": fetched_at},
                )
                new += cur.rowcount
        conn.commit()
    return "ok", new, f"{seen} detections in the last {days} d, {new} new"
