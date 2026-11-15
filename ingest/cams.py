"""CAMS PM2.5 forecast at the receptors, via the Open-Meteo air-quality API (baseline)."""

from datetime import UTC, datetime

from ingest import config
from ingest.http import get

URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


def parse(payload: dict) -> list[tuple[datetime, float]]:
    hourly = payload["hourly"]
    out = []
    for t, v in zip(hourly["time"], hourly["pm2_5"], strict=True):
        if v is not None:
            out.append((datetime.fromisoformat(t).replace(tzinfo=UTC), float(v)))
    return out


def ingest(conn) -> tuple[str, int, str]:
    # One snapshot per hour at most; each snapshot is kept for point-in-time use.
    fetched_at = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    new = 0
    for name, (lat, lon) in config.RECEPTORS.items():
        payload = get(
            URL,
            params={
                "latitude": lat, "longitude": lon, "hourly": "pm2_5", "domains": "cams_global",
                "timezone": "UTC", "past_days": 1, "forecast_days": 4,
            },
        ).json()  # fmt: skip
        with conn.cursor() as cur:
            for valid, pm25 in parse(payload):
                cur.execute(
                    """
                    INSERT INTO cams_forecast (receptor, observed_at, published_at,
                        published_basis, pm25_ugm3, geom)
                    VALUES (%s, %s, %s, 'first_seen', %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                    ON CONFLICT DO NOTHING
                    """,
                    (name, valid, fetched_at, pm25, lon, lat),
                )
                new += cur.rowcount
        conn.commit()
    return "ok", new, f"{len(config.RECEPTORS)} receptors, {new} new hourly values"


ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_cams_hourly (
    receptor  text NOT NULL,
    hour_utc  timestamptz NOT NULL,
    pm25_ugm3 real NOT NULL,
    PRIMARY KEY (receptor, hour_utc)
);
"""


def archive(conn, start, end) -> int:
    """CAMS PM2.5 for past dates (benchmark in validation only, never a model input).
    Open-Meteo's history joins the first hours of successive CAMS runs, so every hour comes
    from a run started on about the same day: a same-day CAMS forecast, not a 2-3 day one."""
    conn.execute(ARCHIVE_SCHEMA)
    new = 0
    for name, (lat, lon) in config.RECEPTORS.items():
        payload = get(
            URL,
            params={
                "latitude": lat, "longitude": lon, "hourly": "pm2_5", "domains": "cams_global",
                "timezone": "UTC", "start_date": f"{start}", "end_date": f"{end}",
            },
        ).json()  # fmt: skip
        with conn.cursor() as cur:
            for valid, pm25 in parse(payload):
                cur.execute(
                    "INSERT INTO archive_cams_hourly VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (name, valid, pm25),
                )
                new += cur.rowcount
    conn.commit()
    return new
