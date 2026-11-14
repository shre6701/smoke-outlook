"""Live smoke run:  uv run python -m sim.run

Forward 72 h from the fire clusters of the last 24 h, backward footprints from the receptors,
contribution of every cluster to every receptor. Output is SIMULATED and not yet validated.
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from ingest import config, db
from sim import constants as c
from sim import fires as fires_mod
from sim.engine import Grid, Params, Receptor, make_particles, run_backward, run_forward
from sim.met import from_netcdf

LABEL = "simulated – not yet validated"
OUT_DIR = config.DATA_DIR / "sim"
HORIZON_H = 72
AGREE = (0.67, 1.5)  # forward/backward ratio outside this = low confidence for that city
MIN_ROW_UGM3 = 1e-4  # smaller cluster-hour contributions are not stored row by row


def weak_cities(check: dict) -> list[str]:
    """Cities where the forward and backward runs disagree by more than the AGREE band."""
    return [k for k, v in check.items() if v is None or not AGREE[0] <= v <= AGREE[1]]


class NothingToRun(RuntimeError):
    pass


def ensure_schema(conn) -> None:
    db.ensure_schema(conn)
    conn.execute((Path(__file__).parent / "schema.sql").read_text())
    conn.commit()


def cycles(conn, now: datetime) -> list[tuple[datetime, Path]]:
    """Stored GFS cycles published before now, newest first, whose file exists."""
    rows = conn.execute(
        "SELECT init_time, path FROM gfs_cycle WHERE published_at <= %s "
        "AND init_time > %s ORDER BY init_time DESC",
        (now, now - timedelta(hours=48)),
    ).fetchall()
    rows = [(t.astimezone(UTC), config.ROOT / p) for t, p in rows]
    return [(t, p) for t, p in rows if p.exists()]


def _rel(path: Path) -> str:
    return str(path.relative_to(config.ROOT)) if path.is_relative_to(config.ROOT) else str(path)


def run(conn, now: datetime, n_particles: int = 100_000, max_members: int = 3,
        out_dir: Path = OUT_DIR, n_per_arrival: int = 500) -> dict:  # fmt: skip
    """One run using only data published before `now`. Returns a summary dict.

    The map field comes from a forward run (smoke followed from the fires). The receptor
    numbers come from backward runs (air followed back from each city), which are much less
    noisy for a single place. The two are compared and the ratio is stored as a self-check.
    """
    wall = time.perf_counter()
    avail = cycles(conn, now)
    if not avail:
        raise NothingToRun("No GFS cycle stored. Run: uv run python -m ingest.run")
    init = avail[0][0]
    t0 = np.datetime64(init.replace(tzinfo=None), "s")
    t_end = HORIZON_H * 3600.0
    fires = fires_mod.load(conn, now - timedelta(hours=24), now)
    n_events = len(fires.frp)  # zero fires is a valid day: the run then reports zero smoke

    grid = Grid(config.WEST, config.SOUTH, config.EAST, config.NORTH)
    receptors = [Receptor(n, lat, lon) for n, (lat, lon) in config.RECEPTORS.items()]
    prm = Params()

    # Member 0: newest cycle, with older cycles filling the hours before it starts.
    # Other members: the same fires moved by an older cycle alone (forecast uncertainty).
    member_paths = {f"gfs_{init:%Y%m%d_%H}": [p for _, p in avail][::-1]}
    for t, _ in avail[1:max_members]:
        if init - t <= timedelta(hours=12):
            member_paths[f"gfs_{t:%Y%m%d_%H}"] = [p for tt, p in avail if tt <= t][::-1]

    mets = {name: from_netcdf(paths, t0=t0) for name, paths in member_paths.items()}
    met0 = next(iter(mets.values()))
    n_part, stats0, check = 0, {}, {}
    if n_events:
        p = make_particles(*fires_mod.particles_for(fires, t0, n_particles))
        out_t0, conc0, fwd, stats0 = run_forward(
            met0, p, grid, receptors, fires.n_clusters, t_end, prm=prm
        )
        stats0.pop("contrib2")
        n_part = stats0.pop("particles")
        sources = fires_mod.sources_for(fires, t0)
        t_stop = float(np.floor(sources.start.min() / prm.dt) * prm.dt)
    else:
        out_t0 = np.arange(0.0, t_end + 1, 3600.0)
        conc0 = np.zeros((len(out_t0), *grid.shape), dtype=np.float32)
        sources, t_stop = None, float(met0.times[0])

    results, foot0 = {}, None
    for name, met in mets.items():
        arr = out_t0[out_t0 <= met.times[-1] + 1e-6]
        out = run_backward(met, receptors, grid, arr, t_stop, n_per_arrival, prm,
                           sources=sources, n_groups=fires.n_clusters)  # fmt: skip
        foot, contrib = out if sources is not None else (out, None)
        if contrib is None:
            contrib = np.zeros((2, len(arr), len(receptors), 0), dtype=np.float32)
        results[name] = (arr, contrib)
        if foot0 is None:
            foot0 = foot
    if n_events:  # self-check: receptor dose, forward / backward (1.0 = perfect agreement)
        back = results[next(iter(results))][1][0].sum(axis=(0, 2))
        ahead = fwd.sum(axis=(0, 2))
        check = {
            r.name: round(float(ahead[i] / back[i]), 3) if back[i] > 0 else None
            for i, r in enumerate(receptors)
        }
    runtime = time.perf_counter() - wall

    import xarray as xr

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"sim_{init:%Y%m%d_%H}_run{now:%Y%m%d_%H%M%S}.nc"
    xr.Dataset(
        {
            "pm25_fire": (("time", "lat", "lon"), conc0, {"units": "ug m-3"}),
            "footprint": (("receptor", "lat", "lon"), foot0, {"units": "s m-3"}),
        },
        coords={
            "time": t0 + out_t0.astype("timedelta64[s]"),
            "lat": grid.lats, "lon": grid.lons, "receptor": [r.name for r in receptors],
        },
        attrs={"label": LABEL, "gfs_init": init.isoformat(), "run_started": now.isoformat(),
               "emission_method": "burned area per detection (FINN v2.5), central factor"},
    ).to_netcdf(path)  # fmt: skip

    e, e2 = fires.area_kg, fires.frp_kg
    params = {"dt_s": prm.dt, "k_h": prm.k_h, "v_dep": prm.v_dep, "burn_s": c.BURN_SECONDS,
              "n_per_arrival": n_per_arrival, "forward_over_backward": check,
              "low_confidence": weak_cities(check), **stats0}  # fmt: skip
    run_id = conn.execute(
        """
        INSERT INTO sim_run (started_at, gfs_init_time, members, fires_from, fires_to,
            n_detections, n_events, n_clusters, n_particles, emission_kg, emission_frp_kg,
            ef_low, ef_central, ef_high, runtime_s, path, params, label)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING run_id
        """,
        (now, init, list(member_paths), now - timedelta(hours=24), now, fires.n_detections,
         n_events, fires.n_clusters, n_part, float(e.sum()), float(e2.sum()), c.EF_PM25_LOW,
         c.EF_PM25_CENTRAL, c.EF_PM25_HIGH, runtime, _rel(path), json.dumps(params), LABEL),
    ).fetchone()[0]  # fmt: skip
    n_rows = 0
    with conn.cursor() as cur:
        for k in range(fires.n_clusters):
            m = fires.cluster == k
            w = fires.frp[m] / fires.frp[m].sum()
            cur.execute(
                """
                INSERT INTO sim_cluster (run_id, cluster_id, geom, n_detections, frp_mw,
                    emission_kg, emission_frp_kg, first_observed, last_observed, country, region)
                SELECT %s, %s, pt, %s, %s, %s, %s, %s, %s, (place_of(pt)).country,
                       (place_of(pt)).region
                FROM (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS pt) q
                """,
                (run_id, k, int(m.sum()), float(fires.frp[m].sum()), float(e[m].sum()),
                 float(e2[m].sum()),
                 fires.observed[m].min().astype(datetime).replace(tzinfo=UTC),
                 fires.observed[m].max().astype(datetime).replace(tzinfo=UTC),
                 float(fires.lon[m] @ w), float(fires.lat[m] @ w)),
            )  # fmt: skip
        cols = "(run_id, member, cluster_id, receptor, valid_at, pm25_ugm3, pm25_frp_ugm3)"
        with cur.copy(f"COPY sim_contribution {cols} FROM STDIN") as copy:
            for name, (arr, contrib) in results.items():
                ti, ri, ki = np.nonzero(contrib[0] > MIN_ROW_UGM3)
                for a, b, k in zip(ti, ri, ki, strict=True):
                    valid = init + timedelta(seconds=float(arr[a]))
                    copy.write_row((run_id, name, int(k), receptors[b].name, valid,
                                    float(contrib[0, a, b, k]),
                                    float(contrib[1, a, b, k])))  # fmt: skip
                    n_rows += 1
        cols = "(run_id, member, receptor, valid_at, pm25_ugm3, pm25_frp_ugm3)"
        with cur.copy(f"COPY sim_receptor_hourly {cols} FROM STDIN") as copy:
            for name, (arr, contrib) in results.items():
                total = contrib.sum(axis=3)
                for a in range(len(arr)):
                    valid = init + timedelta(seconds=float(arr[a]))
                    for b, rec in enumerate(receptors):
                        copy.write_row((run_id, name, rec.name, valid, float(total[0, a, b]),
                                        float(total[1, a, b])))  # fmt: skip
    conn.commit()
    return {
        "run_id": run_id, "init": init, "members": list(member_paths),
        "n_detections": fires.n_detections, "n_events": n_events,
        "n_clusters": fires.n_clusters, "emission_kg": float(e.sum()),
        "emission_frp_kg": float(e2.sum()), "left_grid_frac": stats0.get("left_grid_frac", 0.0),
        "n_particles": n_part, "conc_max": float(conc0.max()), "n_rows": n_rows,
        "check": check, "runtime_s": runtime, "path": path,
    }  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--particles", type=int, default=100_000, help="forward particle cap")
    ap.add_argument("--members", type=int, default=3, help="max forecast cycles in the ensemble")
    args = ap.parse_args()
    conn = db.connect()
    ensure_schema(conn)
    if not conn.execute("SELECT count(*) FROM boundary").fetchone()[0]:
        from ingest import boundaries

        print("boundaries:", boundaries.ingest(conn)[2])
    try:
        r = run(conn, datetime.now(UTC), args.particles, args.members)
    except NothingToRun as e:
        raise SystemExit(str(e)) from None
    hi = r["emission_kg"] * c.EF_PM25_HIGH / c.EF_PM25_CENTRAL / 1000
    lo = r["emission_frp_kg"] * c.EF_PM25_LOW / c.EF_PM25_CENTRAL / 1000
    print(f"run {r['run_id']}: {LABEL}")
    print(f"  GFS {r['init']:%Y-%m-%d %HZ}, members {r['members']}")
    print(f"  {r['n_detections']} detections -> {r['n_events']} fire events -> "
          f"{r['n_clusters']} clusters")  # fmt: skip
    print(f"  PM2.5 emitted: {r['emission_kg'] / 1000:.0f} t (range {lo:.0f}-{hi:.0f} t: "
          f"fire-power method to burned-area method)")  # fmt: skip
    print(f"  {r['n_particles']} forward particles, {r['left_grid_frac']:.0%} left the weather "
          f"grid, map max {r['conc_max']:.0f} ug/m3, {r['n_rows']} contribution rows")  # fmt: skip
    rows = conn.execute(
        "SELECT DISTINCT ON (receptor) receptor, o.pm25_ugm3, pm25_low, pm25_high, valid_at "
        "FROM sim_receptor_outlook o JOIN sim_run r USING (run_id) WHERE run_id = %s "
        "AND valid_at >= r.started_at ORDER BY receptor, pm25_ugm3 DESC",
        (r["run_id"],),
    ).fetchall()
    shares = conn.execute(
        "SELECT receptor, country, sum(dose_ugm3_h) / NULLIF(sum(sum(dose_ugm3_h)) "
        "OVER (PARTITION BY receptor), 0) FROM sim_source_share WHERE run_id = %s "
        "GROUP BY 1, 2 ORDER BY 1, 3 DESC",
        (r["run_id"],),
    ).fetchall()
    for name, peak, lo, hi, when in rows:
        split = ", ".join(f"{cc} {sh:.0%}" for rec, cc, sh in shares if rec == name and sh)
        print(f"  {name:11} peak fire smoke {peak:6.1f} ug/m3 (range {lo:.1f}-{hi:.1f}) "
              f"at {when.astimezone(UTC):%d %b %H:%M} UTC | 72 h: {split or 'none'}")  # fmt: skip
    print(f"  self-check, forward/backward dose per city (1.0 = agree): {r['check']}")
    weak = weak_cities(r["check"])
    if weak:
        print(f"  LOW CONFIDENCE (the two methods disagree): {', '.join(weak)}")
    budget = "within" if r["runtime_s"] <= 600 else "OVER"
    print(f"  runtime {r['runtime_s']:.0f} s ({budget} the 10-minute budget) -> {r['path'].name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
