"""Past-season station readings from the public OpenAQ archive on AWS (for validation only).

    uv run python -m ingest.openaq_archive 2024 2025

Files: s3://openaq-data-archive/records/csv.gz/locationid=<id>/year=/month=/ (one small file
per location and day, public, no key). The location list comes from the OpenAQ v3 API
(OPENAQ_API_KEY): every PM2.5 location inside a receptor's city box. Raw files are kept under
data/openaq/ in the same folder layout; spark_jobs/station_archive.py turns them into hourly
values. Credit: OpenAQ, with CPCB, the US State Department / AirNow and the sensor networks.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests

from ingest import config
from ingest.http import USER_AGENT, get

API = "https://api.openaq.org/v3/locations"
ARCHIVE = "https://openaq-data-archive.s3.amazonaws.com/records/csv.gz"
OUT_DIR = config.DATA_DIR / "openaq"
LOCATIONS = OUT_DIR / "locations.json"
HALF_BOX = 0.25  # same city box as sim.engine.Receptor
MARGIN_DAYS = 7  # days kept before and after the season


def find_locations() -> list[dict]:
    """PM2.5 locations in each receptor box: id, city, country, provider, reference-grade?"""
    headers = {"X-API-Key": os.environ["OPENAQ_API_KEY"]}
    out = []
    for city, (lat, lon) in config.RECEPTORS.items():
        page = 1
        while True:
            res = get(API, headers=headers, params={
                "bbox": f"{lon - HALF_BOX},{lat - HALF_BOX},{lon + HALF_BOX},{lat + HALF_BOX}",
                "parameters_id": 2, "limit": 1000, "page": page,
            }).json()["results"]  # fmt: skip
            for loc in res:
                out.append({
                    "location_id": loc["id"], "city": city, "name": loc["name"],
                    "country": (loc.get("country") or {}).get("code"),
                    "provider": loc["provider"]["name"], "is_reference": bool(loc["isMonitor"]),
                    "latitude": loc["coordinates"]["latitude"],
                    "longitude": loc["coordinates"]["longitude"],
                    "first": ((loc.get("datetimeFirst") or {}).get("utc") or "")[:10],
                    "last": ((loc.get("datetimeLast") or {}).get("utc") or "")[:10],
                })  # fmt: skip
            if len(res) < 1000:
                break
            page += 1
    return out


def _download(job: tuple[int, date]) -> int:
    loc, day = job
    rel = f"locationid={loc}/year={day.year}/month={day.month:02d}"
    path = OUT_DIR / "records" / rel / f"location-{loc}-{day:%Y%m%d}.csv.gz"
    if path.exists():
        return 0
    r = requests.get(f"{ARCHIVE}/{rel}/{path.name}", headers={"User-Agent": USER_AGENT},
                     timeout=60)  # fmt: skip
    if r.status_code in (403, 404):  # no data from this location on this day
        return 0
    r.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(r.content)
    tmp.replace(path)
    return 1


def fetch(seasons: list[int]) -> tuple[int, int]:
    from sim.hindcast import SEASONS

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    locations = find_locations()
    LOCATIONS.write_text(json.dumps(locations, indent=1))
    jobs = []
    for season in seasons:
        first, last = SEASONS[season]
        first, last = first - timedelta(days=MARGIN_DAYS), last + timedelta(days=MARGIN_DAYS)
        for loc in locations:
            if not loc["first"] or loc["first"] > f"{last}" or loc["last"] < f"{first}":
                continue  # the location did not report in this season
            d = first
            while d <= last:
                jobs.append((loc["location_id"], d))
                d += timedelta(days=1)
    with ThreadPoolExecutor(16) as pool:
        new = sum(pool.map(_download, jobs))
    return len(locations), new


if __name__ == "__main__":
    from ingest import db  # noqa: F401 - loads .env

    n_loc, n_new = fetch([int(a) for a in sys.argv[1:]] or [2024, 2025])
    n_files = sum(1 for _ in (OUT_DIR / "records").rglob("*.csv.gz"))
    print(f"{n_loc} locations in the city boxes, {n_new} new daily files, {n_files} files stored")
