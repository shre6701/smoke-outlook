"""Past-season India station readings from the XKDR Forum India Air Quality Database.

    uv run python -m ingest.xkdr_archive 2024

Monthly Parquet files (hourly CPCB CAAQM and US Embassy monitors), free key as a bearer token
(XKDR_API_KEY), CC BY 4.0. Credit: "India Air Quality Database, XKDR Forum", with CPCB and
AirNow. Checked 2026-11-15: CPCB stations end on 2025-09-01 in this export, so it covers the
2024 season only; the 2025 season comes from the OpenAQ archive (ingest/openaq_archive.py).
"""

import os
import sys

import requests

from ingest import config
from ingest.http import USER_AGENT

BASE = "https://airquality.xkdr.org/v1/files/"
OUT_DIR = config.DATA_DIR / "xkdr"
MONTHS = (9, 10, 11, 12)  # season with a margin on both sides


def fetch(seasons: list[int]) -> int:
    headers = {"Authorization": f"Bearer {os.environ['XKDR_API_KEY']}", "User-Agent": USER_AGENT}
    keys = ["v1/stations.parquet"]
    keys += [
        f"v1/measurements/year={y}/month={m:02d}/data.parquet" for y in seasons for m in MONTHS
    ]
    new = 0
    for key in keys:
        path = OUT_DIR / key
        if path.exists():
            continue
        r = requests.get(BASE + key, headers=headers, timeout=300)
        if r.status_code != 200:
            raise RuntimeError(f"XKDR {key}: HTTP {r.status_code}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(path)
        new += 1
    return new


if __name__ == "__main__":
    from ingest import db  # noqa: F401 - loads .env

    print(f"{fetch([int(a) for a in sys.argv[1:]] or [2024])} new files in {OUT_DIR}")
