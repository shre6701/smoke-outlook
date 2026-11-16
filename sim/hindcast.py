"""Point-in-time hindcasts of past seasons (SPEC §5).

    uv run python -m sim.hindcast fetch 2024     # archived GFS forecasts + FIRMS archive fires
    uv run python -m sim.hindcast run 2024       # one run per day, as it would have run then

Each day gets one run at 12:00 UTC (17:30 IST / 17:00 PKT), just after the afternoon satellite
pass. It uses only what had been published by then: the newest GFS forecast cycle (06Z, real
publish time from S3) and fires seen so far. Reanalysis is never used.

Archive fires (FIRMS standard product) carry no publish time. They are given the near-real-time
delay of 3 hours after the satellite pass (NASA FIRMS: "within 3 hours"), marked
published_basis = 'assumed_latency_3h'. The standard product is a reprocessed version of what
was available live, so detections can differ slightly from the live feed of that day.
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

from ingest import config, db
from sim import constants as c
from sim import fires as fires_mod
from sim.engine import Grid, Params, Receptor, run_backward
from sim.met import from_netcdf

SEASONS = {  # post-monsoon burning season, first and last run day
    2024: (date(2024, 10, 1), date(2024, 11, 30)),
    2025: (date(2025, 10, 1), date(2025, 11, 30)),
}
RUN_HOUR_UTC = 12
CYCLE_HOUR_UTC = 6  # the cycle that is complete (about 09:50 UTC) before the run
GFS_STEP_H = 3  # archive weather every 3 h (live uses 1 h); see DECISIONS
FIRMS_SP = ["VIIRS_NOAA20_SP", "VIIRS_NOAA21_SP"]
FIRMS_LATENCY = timedelta(hours=3)
FIRMS_BASIS = "assumed_latency_3h"
STATIC_HISTORY_DAYS = 48  # fires before the season, for the permanent-hot-spot filter
HORIZON_H, NOWCAST_H = 72, 24
LOOKBACK_H, MAX_AGE_H, REPEAT_H = c.SMOKE_LOOKBACK_H, c.SMOKE_MAX_AGE_H, c.FIRE_REPEAT_H
SCHEMA = Path(__file__).parent / "hindcast.sql"


def ensure_schema(conn) -> None:
    db.ensure_schema(conn)
    conn.execute(SCHEMA.read_text())
    conn.commit()


def run_times(season: int) -> list[datetime]:
    first, last = SEASONS[season]
    n = (last - first).days + 1
    return [
        datetime(first.year, first.month, first.day, RUN_HOUR_UTC, tzinfo=UTC) + timedelta(days=i)
        for i in range(n)
    ]


# ---------------------------------------------------------------- inputs
def fetch_fires(conn, season: int) -> int:
    from ingest import firms

    key = os.environ["FIRMS_MAP_KEY"]
    first, last = SEASONS[season]
    day, new = first - timedelta(days=STATIC_HISTORY_DAYS), 0
    while day <= last:
        for source in FIRMS_SP:
            rows = firms._fetch(key, source, 5, day)
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        """
                        INSERT INTO fire_detection (source, observed_at, published_at,
                            published_basis, latitude, longitude, geom, frp_mw, confidence,
                            daynight, bright_ti4, scan, track, version)
                        VALUES (%(source)s, %(observed_at)s, %(pub)s, %(basis)s, %(latitude)s,
                            %(longitude)s,
                            ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326),
                            %(frp_mw)s, %(confidence)s, %(daynight)s, %(bright_ti4)s, %(scan)s,
                            %(track)s, %(version)s)
                        ON CONFLICT (source, observed_at, latitude, longitude) DO NOTHING
                        """,
                        {**r, "pub": r["observed_at"] + FIRMS_LATENCY, "basis": FIRMS_BASIS},
                    )
                    new += cur.rowcount
            conn.commit()
        day += timedelta(days=5)
    return new


def cycle_inits(season: int) -> list[datetime]:
    first, last = SEASONS[season]
    start = datetime(first.year, first.month, first.day, CYCLE_HOUR_UTC, tzinfo=UTC)
    n = (last - first).days + 1
    return [start + timedelta(days=i) for i in range(-(LOOKBACK_H // 24) - 1, n)]


def _fetch_gfs_part(args) -> tuple[int, list[str]]:
    part, inits = args
    from weather import gfs

    gfs.RAW_DIR = gfs.RAW_DIR / f"p{part}"  # each worker cleans only its own raw files
    conn = db.connect()
    done, failed = 0, []
    for init in inits:
        try:
            done += bool(gfs.ingest_cycle(conn, init, GFS_STEP_H, gfs.MAX_FXX))
        except Exception as e:  # noqa: BLE001 - report and go on; the run step lists gaps
            conn.rollback()
            failed.append(f"{init:%Y-%m-%d} ({type(e).__name__})")
    return done, failed


def fetch_gfs(season: int, workers: int = 3) -> tuple[int, list[str]]:
    from multiprocessing import get_context

    inits = cycle_inits(season)
    parts = [(k, inits[k::workers]) for k in range(workers)]
    with get_context("spawn").Pool(workers) as pool:
        out = pool.map(_fetch_gfs_part, parts)
    return sum(d for d, _ in out), [f for _, fs in out for f in fs]


# ---------------------------------------------------------------- one run
def cycles(conn, now: datetime) -> list[tuple[datetime, Path, datetime]]:
    """Stored cycles published before `now`, newest first: (init, file, published).
    The first is a full 72 h forecast."""
    rows = conn.execute(
        "SELECT init_time, path, published_at, n_times, step_hours FROM gfs_cycle "
        "WHERE published_at <= %s AND init_time > %s ORDER BY init_time DESC",
        (now, now - timedelta(hours=LOOKBACK_H + 30)),
    ).fetchall()
    out = [
        (t.astimezone(UTC), config.ROOT / p, pub.astimezone(UTC), (n - 1) * step >= HORIZON_H)
        for t, p, pub, n, step in rows
    ]
    out = [r for r in out if r[1].exists()]
    while out and not out[0][3]:
        out.pop(0)
    return [r[:3] for r in out]


def ventilation(met, receptors, arrivals) -> np.ndarray:
    """Forecast boundary-layer height x 10 m wind speed (m²/s), [arrival, receptor]. Low
    values mean still air that lets every kind of pollution build up. Weather only."""
    lon = np.array([r.lon for r in receptors])
    lat = np.array([r.lat for r in receptors])
    z = np.full(len(lon), 10.0)
    out = np.zeros((len(arrivals), len(lon)), dtype=np.float32)
    for a, t in enumerate(arrivals):
        u, v = met.wind(float(t), lon, lat, z)
        out[a] = met.pbl(float(t), lon, lat) * np.hypot(u, v)
    return out


def simulate(conn, now: datetime, n_per_arrival: int = 500, seed: int = 0) -> dict | None:
    """Receptor numbers for one run time, from data published before `now` only.
    Returns None when no forecast cycle is stored for that time."""
    wall = time.perf_counter()
    avail = cycles(conn, now)
    if not avail:
        return None
    init = avail[0][0]
    t0 = np.datetime64(init.replace(tzinfo=None), "s")
    t_end = HORIZON_H * 3600.0
    now_s = (np.datetime64(now.replace(tzinfo=None), "s") - t0).astype(float)
    met = from_netcdf([p for _, p, _ in avail][::-1], t0=t0)
    fires = fires_mod.load(conn, now - timedelta(hours=LOOKBACK_H), now)
    receptors = [Receptor(n, lat, lon) for n, (lat, lon) in config.RECEPTORS.items()]
    grid = Grid(config.WEST, config.SOUTH, config.EAST, config.NORTH)
    arrivals = np.arange(now_s - NOWCAST_H * 3600.0, t_end + 1, 3600.0)
    valid = [init + timedelta(seconds=float(a)) for a in arrivals]
    G = fires.n_clusters
    summary = {
        "run_at": now, "gfs_init": init, "gfs_published": max(pub for _, _, pub in avail),
        "n_cycles": len(avail), "n_detections": fires.n_detections, "n_events": len(fires.frp),
        "n_clusters": G, "emission_kg": float(fires.area_kg.sum()),
        "emission_frp_kg": float(fires.frp_kg.sum()), "valid": valid,
        "receptors": [r.name for r in receptors],
        "vent": ventilation(met, receptors, arrivals),
    }  # fmt: skip
    if not G:  # a day without fires is a valid day with zero fire smoke
        zero = np.zeros((len(valid), len(receptors)), dtype=np.float32)
        return {**summary, "n_assumed": 0, "pm25": zero, "pm25_frp": zero, "whatif": zero,
                "countries": [], "by_country": np.zeros((2, len(valid), len(receptors), 0)),
                "runtime_s": time.perf_counter() - wall}  # fmt: skip
    assumed = fires_mod.assume_future(fires, t0, now_s, t_end, REPEAT_H)
    everything = fires_mod.combine(fires, assumed)
    sources = fires_mod.sources_for(everything, t0)
    prm = Params(seed=seed)
    t_stop = float(np.floor(sources.start.min() / prm.dt) * prm.dt)
    _, contrib = run_backward(met, receptors, grid, arrivals, max(t_stop, float(met.times[0])),
                              n_per_arrival, prm, sources=sources, n_groups=2 * G,
                              max_age=MAX_AGE_H * 3600.0)  # fmt: skip
    seen, twin = contrib[:, :, :, :G], contrib[0][:, :, G:]
    # Country / region of each cluster (FRP-weighted centre), same rule as the live ledger.
    lon = np.array([fires.lon[fires.cluster == k] @ fires.frp[fires.cluster == k]
                    / fires.frp[fires.cluster == k].sum() for k in range(G)])  # fmt: skip
    lat = np.array([fires.lat[fires.cluster == k] @ fires.frp[fires.cluster == k]
                    / fires.frp[fires.cluster == k].sum() for k in range(G)])  # fmt: skip
    place = conn.execute(
        "SELECT (place_of(ST_SetSRID(ST_MakePoint(x, y), 4326))).country "
        "FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY AS q(x, y, i) ORDER BY i",
        (lon.tolist(), lat.tolist()),
    ).fetchall()
    country = np.array([p[0] or "other" for p in place])
    names = sorted(set(country))
    by_country = np.stack(
        [
            np.stack([seen[0][:, :, country == c].sum(axis=2) for c in names], axis=2),
            np.stack([twin[:, :, country == c].sum(axis=2) for c in names], axis=2),
        ]
    )  # [observed | assumed repeats, arrival, receptor, country]
    return {
        **summary, "n_assumed": len(assumed.frp), "pm25": seen[0].sum(axis=2),
        "pm25_frp": seen[1].sum(axis=2), "whatif": seen[0].sum(axis=2) + twin.sum(axis=2),
        "countries": names, "by_country": by_country,
        "fires_published_max": _fires_published_max(conn, now),
        "runtime_s": time.perf_counter() - wall,
    }  # fmt: skip


def _fires_published_max(conn, now: datetime):
    return conn.execute(
        "SELECT max(published_at) FROM fire_detection WHERE observed_at > %s "
        "AND observed_at <= %s AND published_at <= %s",
        (now - timedelta(hours=LOOKBACK_H), now, now),
    ).fetchone()[0]


def store(conn, season: int, r: dict, params: dict) -> int:
    conn.execute("DELETE FROM hindcast_run WHERE season = %s AND run_at = %s",
                 (season, r["run_at"]))  # fmt: skip
    run_id = conn.execute(
        """
        INSERT INTO hindcast_run (season, run_at, gfs_init_time, gfs_published_at,
            fires_published_max, n_cycles, n_detections, n_events, n_assumed, n_clusters,
            emission_kg, emission_frp_kg, runtime_s, params)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING run_id
        """,
        (season, r["run_at"], r["gfs_init"], r["gfs_published"], r.get("fires_published_max"),
         r["n_cycles"], r["n_detections"], r["n_events"], r["n_assumed"], r["n_clusters"],
         r["emission_kg"], r["emission_frp_kg"], r["runtime_s"], json.dumps(params)),
    ).fetchone()[0]  # fmt: skip
    with conn.cursor() as cur:
        with cur.copy(
            "COPY hindcast_hourly (run_id, receptor, valid_at, lead_h, pm25_ugm3, "
            "pm25_frp_ugm3, pm25_whatif_ugm3, vent_m2s) FROM STDIN"
        ) as copy:
            for a, valid in enumerate(r["valid"]):
                lead = (valid - r["run_at"]).total_seconds() / 3600.0
                for b, name in enumerate(r["receptors"]):
                    copy.write_row(
                        (
                            run_id,
                            name,
                            valid,
                            lead,
                            float(r["pm25"][a, b]),
                            float(r["pm25_frp"][a, b]),
                            float(r["whatif"][a, b]),
                            None if r.get("vent") is None else float(r["vent"][a, b]),
                        )
                    )
        with cur.copy(
            "COPY hindcast_source (run_id, receptor, valid_at, country, pm25_ugm3, "
            "pm25_assumed_ugm3) FROM STDIN"
        ) as copy:
            bc = r["by_country"]
            for a, valid in enumerate(r["valid"]):
                for b, name in enumerate(r["receptors"]):
                    for k, country in enumerate(r["countries"]):
                        if bc[0, a, b, k] > 0 or bc[1, a, b, k] > 0:
                            copy.write_row((run_id, name, valid, country, float(bc[0, a, b, k]),
                                            float(bc[1, a, b, k])))  # fmt: skip
    conn.commit()
    return run_id


def _run_one(args) -> str:
    season, now, n_per_arrival = args
    conn = db.connect()
    try:
        r = simulate(conn, now, n_per_arrival)
        if r is None:
            return f"{now:%Y-%m-%d} SKIPPED (no forecast cycle stored)"
        store(conn, season, r, {"n_per_arrival": n_per_arrival, "gfs_step_h": GFS_STEP_H,
                                "fires": FIRMS_SP, "fires_basis": FIRMS_BASIS,
                                "day_fire_factor": c.DAY_FIRE_FACTOR,
                                "day_burn_ist": c.DAY_BURN_IST, "lookback_h": LOOKBACK_H,
                                "max_age_h": MAX_AGE_H})  # fmt: skip
        peak = ", ".join(f"{n} {r['whatif'][:, i].max():.1f}" for i, n in enumerate(r["receptors"]))
        return (f"{now:%Y-%m-%d} GFS {r['gfs_init']:%d %HZ} {r['n_events']:5d} events "
                f"{r['runtime_s']:4.0f} s | peak incl. what-if: {peak}")  # fmt: skip
    finally:
        conn.close()


def run_season(season: int, workers: int = 4, n_per_arrival: int = 500, redo: bool = False,
               only: list[datetime] | None = None) -> None:  # fmt: skip
    from multiprocessing import get_context

    conn = db.connect()
    ensure_schema(conn)
    have = {t.astimezone(UTC) for (t,) in conn.execute(
        "SELECT run_at FROM hindcast_run WHERE season = %s", (season,)).fetchall()}  # fmt: skip
    conn.close()
    todo = [t for t in (only or run_times(season)) if redo or t not in have]
    print(f"season {season}: {len(todo)} runs to do, {len(have)} already stored")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    with get_context("spawn").Pool(workers) as pool:
        for line in pool.imap(_run_one, [(season, t, n_per_arrival) for t in todo]):
            print(line, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("step", choices=["fetch", "run"])
    ap.add_argument("season", type=int, choices=sorted(SEASONS))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--redo", action="store_true", help="recompute runs already stored")
    ap.add_argument("--skip-gfs", action="store_true")
    ap.add_argument("--days", nargs="*", default=[], help="redo only these days (YYYY-MM-DD)")
    args = ap.parse_args()
    if args.step == "fetch":
        conn = db.connect()
        ensure_schema(conn)
        print(f"fires: {fetch_fires(conn, args.season)} new archive detections")
        conn.close()
        if not args.skip_gfs:
            done, failed = fetch_gfs(args.season, args.workers)
            print(f"GFS: {done} new cycles" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    else:
        only = [datetime.fromisoformat(d).replace(hour=RUN_HOUR_UTC, tzinfo=UTC) for d in args.days]
        run_season(args.season, args.workers, redo=args.redo or bool(only), only=only or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
