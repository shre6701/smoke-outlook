"""Live PM2.5 station readings for the v1 domain.

- OpenAQ v3 (India and Pakistan): reference monitors and low-cost sensors, labelled.
- data.gov.in real-time feed (India): optional, skipped cleanly without DATAGOV_API_KEY.
"""

import os
import time
from datetime import UTC, datetime, timedelta

from ingest import config
from ingest.http import get

OPENAQ = "https://api.openaq.org/v3"
PM25_ID = 2  # OpenAQ parameter id for PM2.5 in µg/m³
MAX_AGE = timedelta(days=7)  # older "latest" values are dead sensors: not stored
DATAGOV_URL = "https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"

INSERT_STATION = """
    INSERT INTO station (source, station_id, name, country, provider, is_reference, geom)
    VALUES (%(source)s, %(station_id)s, %(name)s, %(country)s, %(provider)s, %(is_reference)s,
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326))
    ON CONFLICT (source, station_id) DO UPDATE SET name = EXCLUDED.name,
        provider = EXCLUDED.provider, is_reference = EXCLUDED.is_reference,
        geom = EXCLUDED.geom, updated_at = now()
"""
INSERT_READING = """
    INSERT INTO station_reading (source, station_id, sensor_id, parameter, unit, value,
        observed_at, published_at, published_basis)
    VALUES (%(source)s, %(station_id)s, %(sensor_id)s, %(parameter)s, %(unit)s, %(value)s,
        %(observed_at)s,
        %(pub)s, 'first_seen')
    ON CONFLICT DO NOTHING
"""
DROP_PRE_SENSOR_ROW = """
    DELETE FROM station_reading WHERE source = %(source)s AND station_id = %(station_id)s
        AND sensor_id = '' AND parameter = %(parameter)s AND observed_at = %(observed_at)s
"""


def _pages(url: str, params: dict, headers: dict):
    page = 1
    while True:
        results = get(url, params={**params, "limit": 1000, "page": page}, headers=headers).json()
        results = results.get("results", [])
        yield from results
        if len(results) < 1000:
            return
        page += 1
        time.sleep(0.3)  # stay well under 60 requests/min


def openaq_station(loc: dict) -> dict | None:
    c = loc.get("coordinates") or {}
    lat, lon = c.get("latitude"), c.get("longitude")
    if lat is None or lon is None or not config.in_domain(lat, lon):
        return None
    return {
        "source": "openaq",
        "station_id": str(loc["id"]),
        "name": loc.get("name"),
        "country": (loc.get("country") or {}).get("code") or "??",
        "provider": (loc.get("provider") or {}).get("name"),
        "is_reference": bool(loc.get("isMonitor")),
        "lat": lat,
        "lon": lon,
    }


def openaq_reading(item: dict, now: datetime) -> dict | None:
    """One entry of /parameters/2/latest -> reading, or None if unusable or too old."""
    utc = (item.get("datetime") or {}).get("utc")
    value = item.get("value")
    if not utc or value is None or value < 0:
        return None
    observed = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    if observed > now + timedelta(hours=1) or now - observed > MAX_AGE:
        return None
    return {
        "source": "openaq",
        "station_id": str(item["locationsId"]),
        "sensor_id": str(item.get("sensorsId") or ""),
        "parameter": "pm25",
        "unit": "µg/m³",
        "value": float(value),
        "observed_at": observed,
    }


def ingest_openaq(conn) -> tuple[str, int, str]:
    key = os.getenv("OPENAQ_API_KEY")
    if not key:
        return "skipped", 0, "OPENAQ_API_KEY not set"
    headers = {"X-API-Key": key}
    now = datetime.now(UTC)
    bbox = f"{config.WEST},{config.SOUTH},{config.EAST},{config.NORTH}"
    stations = {}
    for loc in _pages(f"{OPENAQ}/locations", {"bbox": bbox, "parameters_id": PM25_ID}, headers):
        s = openaq_station(loc)
        if s:
            stations[s["station_id"]] = s
    readings = []
    for item in _pages(f"{OPENAQ}/parameters/{PM25_ID}/latest", {}, headers):
        if str(item.get("locationsId")) in stations:
            r = openaq_reading(item, now)
            if r:
                readings.append(r)
    new = 0
    with conn.cursor() as cur:
        cur.executemany(INSERT_STATION, list(stations.values()))
        for r in readings:
            cur.execute(DROP_PRE_SENSOR_ROW, r)  # same reading stored before sensor ids
            cur.execute(INSERT_READING, {**r, "pub": now})
            new += cur.rowcount
    conn.commit()
    return "ok", new, f"{len(stations)} stations, {len(readings)} fresh readings, {new} new"


def datagov_rows(records: list[dict]) -> list[dict]:
    """data.gov.in records -> PM2.5 rows. Values are stored as published (unit not verified)."""
    out = []
    for rec in records:
        if (rec.get("pollutant_id") or "").upper().replace(" ", "") != "PM2.5":
            continue
        try:
            lat, lon = float(rec["latitude"]), float(rec["longitude"])
            value = float(rec.get("avg_value") or rec.get("pollutant_avg"))
            ist = datetime.strptime(rec["last_update"], "%d-%m-%Y %H:%M:%S")
        except (KeyError, TypeError, ValueError):
            continue  # "NA" values and broken rows
        if not config.in_domain(lat, lon):
            continue
        out.append(
            {
                "source": "datagov",
                "station_id": rec.get("station") or f"{lat},{lon}",
                "sensor_id": "",
                "name": rec.get("station"),
                "country": "IN",
                "provider": "CPCB via data.gov.in",
                "is_reference": True,
                "lat": lat,
                "lon": lon,
                "parameter": "pm25_avg_as_published",
                "unit": "as published",
                "value": value,
                "observed_at": (ist - timedelta(hours=5, minutes=30)).replace(tzinfo=UTC),
            }
        )
    return out


def ingest_datagov(conn) -> tuple[str, int, str]:
    key = os.getenv("DATAGOV_API_KEY")
    if not key:
        return "skipped", 0, "DATAGOV_API_KEY not set (optional source)"
    now = datetime.now(UTC)
    records, offset = [], 0
    while True:
        batch = get(
            DATAGOV_URL,
            params={"api-key": key, "format": "json", "limit": 1000, "offset": offset},
        ).json()
        batch = batch.get("records", [])
        records += batch
        if len(batch) < 1000:
            break
        offset += 1000
    rows = datagov_rows(records)
    new = 0
    with conn.cursor() as cur:
        cur.executemany(INSERT_STATION, rows)
        for r in rows:
            cur.execute(INSERT_READING, {**r, "pub": now})
            new += cur.rowcount
    conn.commit()
    return "ok", new, f"{len(rows)} PM2.5 rows in domain, {new} new"
