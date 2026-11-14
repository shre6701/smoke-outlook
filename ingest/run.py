"""One command for every live input:  uv run python -m ingest.run

Each source runs on its own: a failure or a missing key never stops the others.
Use --only gfs firms ... to run a subset, --every 60 to repeat every 60 minutes.
"""

import argparse
import sys
import time
from datetime import UTC, datetime
from functools import partial

from ingest import boundaries, cams, db, firms, stations
from weather import gfs

SOURCES = {
    "boundaries": boundaries.ingest,
    "gfs": gfs.ingest,
    "firms": firms.ingest,
    "openaq": stations.ingest_openaq,
    "datagov": stations.ingest_datagov,
    "cams": cams.ingest,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="+", choices=list(SOURCES), default=list(SOURCES))
    ap.add_argument("--every", type=int, metavar="MIN", help="repeat every MIN minutes")
    ap.add_argument("--gfs-step", type=int, default=1, choices=[1, 3], help="GFS step in hours")
    ap.add_argument("--gfs-back", type=int, default=1, help="also keep this many earlier cycles")
    args = ap.parse_args()
    sources = {**SOURCES, "gfs": partial(gfs.ingest, step_hours=args.gfs_step, back=args.gfs_back)}
    while True:
        failed = run_once(args.only, sources)
        if not args.every:
            return 1 if failed else 0
        print(f"sleeping {args.every} min (Ctrl+C to stop)", flush=True)
        time.sleep(args.every * 60)


def run_once(names: list[str], sources: dict) -> int:
    conn = db.connect()
    db.ensure_schema(conn)
    failed = 0
    for name in names:
        started = datetime.now(UTC)
        try:
            status, rows, note = sources[name](conn)
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
        print(f"{name:8} {status:8} {note}", flush=True)
    conn.close()
    print("Now run: uv run python -m ingest.status")
    return failed


if __name__ == "__main__":
    sys.exit(main())
