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


def cycles(conn, now: datetime) -> list[tuple[datetime, Path]]:
    """Stored GFS cycles published before now, newest first, whose file exists."""
    rows = conn.execute(
        "SELECT init_time, path FROM gfs_cycle WHERE published_at <= %s "
        "AND init_time > %s ORDER BY init_time DESC",
        (now, now - timedelta(hours=48)),
    ).fetchall()
    return [(t, config.ROOT / p) for t, p in rows if (config.ROOT / p).exists()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--particles", type=int, default=100_000, help="forward particle cap")
    ap.add_argument("--members", type=int, default=3, help="max forecast cycles in the ensemble")
    args = ap.parse_args()
    wall = time.perf_counter()
    now = datetime.now(UTC)
    conn = db.connect()
    db.ensure_schema(conn)
    conn.execute((Path(__file__).parent / "schema.sql").read_text())
    conn.commit()

    avail = cycles(conn, now)
    if not avail:
        raise SystemExit("No GFS cycle stored. Run: uv run python -m ingest.run")
    init = avail[0][0]
    t0 = np.datetime64(init.replace(tzinfo=None), "s")
    t_end = HORIZON_H * 3600.0
    fires = fires_mod.load(conn, now - timedelta(hours=24), now)
    if not len(fires.frp):
        raise SystemExit("No fire detections in the last 24 h. Run: uv run python -m ingest.run")

    grid = Grid(config.WEST, config.SOUTH, config.EAST, config.NORTH)
    receptors = [Receptor(n, lat, lon) for n, (lat, lon) in config.RECEPTORS.items()]
    prm = Params()

    # Member 0: newest cycle, with older cycles filling the hours before it starts.
    # Other members: the same fires moved by an older cycle alone (forecast uncertainty).
    newest_first = [p for _, p in avail]
    member_paths = {f"gfs_{init:%Y%m%d_%H}": newest_first[::-1]}
    for t, _ in avail[1 : args.members]:
        if init - t <= timedelta(hours=12):
            older = [p for tt, p in avail if tt <= t]
            member_paths[f"gfs_{t:%Y%m%d_%H}"] = older[::-1]

    results, conc0, stats0, met0 = {}, None, None, None
    for name, paths in member_paths.items():
        met = from_netcdf(paths, t0=t0)
        p = make_particles(*fires_mod.particles_for(fires, t0, args.particles))
        out_t, conc, contrib, stats = run_forward(
            met, p, grid, receptors, fires.n_clusters, min(t_end, met.times[-1]), prm=prm
        )
        results[name] = (out_t, contrib)
        if conc0 is None:
            conc0, stats0, met0, out_t0 = conc, stats, met, out_t
    arrivals = np.arange(0.0, t_end + 1, 3 * 3600.0)
    foot = run_backward(met0, receptors, grid, arrivals, float(met0.times[0]), prm=prm)
    runtime = time.perf_counter() - wall

    # ---- store ----
    import xarray as xr

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"sim_{init:%Y%m%d_%H}_run{now:%Y%m%d_%H%M}.nc"
    xr.Dataset(
        {
            "pm25_fire": (("time", "lat", "lon"), conc0, {"units": "ug m-3"}),
            "footprint": (("receptor", "lat", "lon"), foot, {"units": "s m-3"}),
        },
        coords={
            "time": t0 + out_t0.astype("timedelta64[s]"),
            "lat": grid.lats, "lon": grid.lons, "receptor": [r.name for r in receptors],
        },
        attrs={"label": LABEL, "gfs_init": init.isoformat(), "run_started": now.isoformat()},
    ).to_netcdf(path)  # fmt: skip

    e = fires_mod.emission_kg(fires.frp)
    run_id = conn.execute(
        """
        INSERT INTO sim_run (started_at, gfs_init_time, members, fires_from, fires_to,
            n_detections, n_clusters, n_particles, emission_kg, ef_low, ef_central, ef_high,
            runtime_s, path, params, label)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING run_id
        """,
        (now, init, list(member_paths), now - timedelta(hours=24), now, len(e),
         fires.n_clusters, stats0["particles"], float(e.sum()), c.EF_PM25_LOW, c.EF_PM25_CENTRAL,
         c.EF_PM25_HIGH, runtime, str(path.relative_to(config.ROOT)),
         json.dumps({"dt_s": prm.dt, "k_h": prm.k_h, "v_dep": prm.v_dep,
                     "burn_s": c.BURN_SECONDS, **stats0}), LABEL),
    ).fetchone()[0]  # fmt: skip
    with conn.cursor() as cur:
        for k in range(fires.n_clusters):
            m = fires.cluster == k
            w = fires.frp[m] / fires.frp[m].sum()
            cur.execute(
                "INSERT INTO sim_cluster VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326),"
                " %s, %s, %s, %s, %s)",
                (run_id, k, float(fires.lon[m] @ w), float(fires.lat[m] @ w), int(m.sum()),
                 float(fires.frp[m].sum()), float(e[m].sum()),
                 fires.observed[m].min().astype(datetime).replace(tzinfo=UTC),
                 fires.observed[m].max().astype(datetime).replace(tzinfo=UTC)),
            )  # fmt: skip
        n_rows = 0
        with cur.copy("COPY sim_contribution FROM STDIN") as copy:
            for name, (out_t, contrib) in results.items():
                ti, ri, ki = np.nonzero(contrib > 0)
                for a, b, k in zip(ti, ri, ki, strict=True):
                    valid = init + timedelta(seconds=float(out_t[a]))
                    copy.write_row((run_id, name, int(k), receptors[b].name, valid,
                                    float(contrib[a, b, k])))  # fmt: skip
                    n_rows += 1
    conn.commit()

    # ---- report ----
    print(f"run {run_id}: {LABEL}")
    print(f"  GFS {init:%Y-%m-%d %HZ}, members {list(member_paths)}")
    print(f"  {len(e)} detections -> {fires.n_clusters} clusters, {e.sum() / 1000:.1f} t PM2.5 "
          f"(range {e.sum() * c.EF_PM25_LOW / c.EF_PM25_CENTRAL / 1000:.1f}-"
          f"{e.sum() * c.EF_PM25_HIGH / c.EF_PM25_CENTRAL / 1000:.1f} t)")  # fmt: skip
    print(f"  {stats0['particles']} particles, {stats0['left_grid_frac']:.0%} left the grid, "
          f"max {conc0.max():.1f} ug/m3, {n_rows} contribution rows")  # fmt: skip
    rows = conn.execute(
        "SELECT receptor, max(pm25_ugm3), max(pm25_low), max(pm25_high), "
        "(array_agg(valid_at ORDER BY pm25_ugm3 DESC NULLS LAST))[1] "
        "FROM sim_receptor_outlook WHERE run_id = %s GROUP BY 1 ORDER BY 2 DESC NULLS LAST",
        (run_id,),
    ).fetchall()
    for name, peak, _lo, hi, when in rows:
        print(f"  {name:11} peak fire smoke {peak or 0:6.1f} ug/m3 (up to {hi:.1f}) "
              f"at {when:%d %b %H:%M} UTC")  # fmt: skip
    budget = "within" if runtime <= 600 else "OVER"
    print(f"  runtime {runtime:.0f} s ({budget} the 10-minute budget) -> {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
