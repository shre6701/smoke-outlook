"""One command for every live input:  uv run python -m ingest.run

Each source runs on its own: a failure or a missing key never stops the others.
Use --only gfs firms ... to run a subset.
"""

import argparse
import sys
from datetime import UTC, datetime

from ingest import cams, db, firms, stations
from weather import gfs

SOURCES = {
    "gfs": gfs.ingest,
    "firms": firms.ingest,
    "openaq": stations.ingest_openaq,
    "datagov": stations.ingest_datagov,
    "cams": cams.ingest,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="+", choices=list(SOURCES), default=list(SOURCES))
    args = ap.parse_args()
    conn = db.connect()
    db.ensure_schema(conn)
    failed = 0
    for name in args.only:
        started = datetime.now(UTC)
        try:
            status, rows, note = SOURCES[name](conn)
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the rest
            conn.rollback()
            status, rows, note = "failed", 0, f"{type(e).__name__}: {str(e)[:300]}"
            failed += 1
        conn.execute(
            "INSERT INTO ingest_run (source, started_at, status, rows_new, note) "
            "VALUES (%s, %s, %s, %s, %s)",
            (name, started, status, rows, note),
        )
        conn.commit()
        print(f"{name:8} {status:8} {note}")
    print("Now run: uv run python -m ingest.status")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
