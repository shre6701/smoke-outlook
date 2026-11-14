"""How should fires on forecast days be guessed? Candidate methods and their backtest.

    uv run python -m sim.fire_forecast        # skill table on this season's real fires

Nobody can observe tomorrow's fires. Operational systems either repeat the latest day
(HRRR-Smoke, NOAA's air-quality forecast) or use the long-term average for that calendar day
(India's DSS for days 2-5). The backtest scores simple methods against what was later seen.
Anything built from these guesses is a labelled what-if, never an observation.
"""

from datetime import UTC, date, datetime, timedelta

import numpy as np

from ingest import config

RES = 0.25  # degrees; scoring grid
EVENTS_SQL = """
    WITH d AS (
        SELECT *, floor(extract(epoch FROM observed_at) / 21600) AS win
        FROM fire_detection WHERE observed_at >= %s AND observed_at < %s AND frp_mw > 0
    ), g AS (
        SELECT *, ST_ClusterDBSCAN(geom, 0.004, 1) OVER (PARTITION BY win) AS grp FROM d
    ), per_sat AS (
        SELECT win, grp, source, count(*) AS n, min(observed_at) AS t,
               avg(longitude) AS lon, avg(latitude) AS lat
        FROM g GROUP BY win, grp, source
    )
    SELECT DISTINCT ON (win, grp) (t AT TIME ZONE 'UTC')::date, lon, lat, n
    FROM per_sat ORDER BY win, grp, n DESC
"""


def grid_shape() -> tuple[int, int]:
    return (round((config.NORTH - config.SOUTH) / RES), round((config.EAST - config.WEST) / RES))


def daily_cells(conn, start: date, end: date) -> dict[date, np.ndarray]:
    """Burned pixels per UTC day and grid cell (two-satellite repeats merged)."""
    ny, nx = grid_shape()
    days = {start + timedelta(days=i): np.zeros((ny, nx)) for i in range((end - start).days + 1)}
    t0 = datetime(start.year, start.month, start.day, tzinfo=UTC)
    t1 = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    for day, lon, lat, n in conn.execute(EVENTS_SQL, (t0, t1)).fetchall():
        iy = min(int((lat - config.SOUTH) / RES), ny - 1)
        ix = min(int((lon - config.WEST) / RES), nx - 1)
        days[day][iy, ix] += n
    return days


def cell_countries(conn) -> np.ndarray:
    ny, nx = grid_shape()
    out = np.empty((ny, nx), dtype=object)
    for iy in range(ny):
        for ix in range(nx):
            lon, lat = config.WEST + (ix + 0.5) * RES, config.SOUTH + (iy + 0.5) * RES
            out[iy, ix] = conn.execute(
                "SELECT (place_of(ST_SetSRID(ST_MakePoint(%s, %s), 4326))).country", (lon, lat)
            ).fetchone()[0]
    return out


# Each method: history (list of daily grids, oldest first, last = issue day) -> guess for any
# later day. The same guess is used for every lead time.
def _trend(h):
    ratio = h[-1].sum() / max(h[-2].sum(), 1.0)
    return h[-1] * float(np.clip(ratio, 0.5, 2.0))


METHODS = {
    "no future fires": lambda h: np.zeros_like(h[-1]),
    "repeat last day": lambda h: h[-1],
    "mean of last 2 days": lambda h: np.mean(h[-2:], axis=0),
    "mean of last 3 days": lambda h: np.mean(h[-3:], axis=0),
    "mean of last 5 days": lambda h: np.mean(h[-5:], axis=0),
    "mean of last 7 days": lambda h: np.mean(h[-7:], axis=0),
    "last day x 1-day trend": _trend,
}
MIN_HISTORY = 7


def backtest(days: dict[date, np.ndarray], countries: np.ndarray | None = None) -> list[dict]:
    """Score every method for lead times of 1, 2 and 3 days.

    cell_error  = sum |guess - seen| / sum seen over all cells and days (0 = perfect; the
                  'no future fires' method scores exactly 1)
    area_error  = the same on 1-degree blocks (about 100 km: the scale smoke transport cares
                  about; it is what the method is chosen on)
    total_error = median over days of |guess total - seen total| / seen total
    bias        = sum guess / sum seen
    """
    keys = sorted(days)
    rows = []
    for name, fn in METHODS.items():
        for lead in (1, 2, 3):
            num = den = gsum = num1 = 0.0
            rel, by_country = [], {}
            for i in range(MIN_HISTORY - 1, len(keys) - lead):
                guess = fn([days[k] for k in keys[: i + 1]])
                seen = days[keys[i + lead]]
                num += np.abs(guess - seen).sum()
                num1 += np.abs(_blocks(guess) - _blocks(seen)).sum()
                den += seen.sum()
                gsum += guess.sum()
                if seen.sum() > 0:
                    rel.append(abs(guess.sum() - seen.sum()) / seen.sum())
                if countries is not None:
                    for cc in ("IN", "PK"):
                        m = countries == cc
                        if seen[m].sum() > 0:
                            by_country.setdefault(cc, []).append(
                                abs(guess[m].sum() - seen[m].sum()) / seen[m].sum()
                            )
            rows.append(
                {
                    "method": name, "lead": lead, "cell_error": num / den,
                    "area_error": num1 / den,
                    "total_error": float(np.median(rel)), "bias": gsum / den,
                    **{f"total_error_{cc}": float(np.median(v)) for cc, v in by_country.items()},
                    "n_days": len(rel),
                }
            )  # fmt: skip
    return rows


def _blocks(a: np.ndarray, k: int = 4) -> np.ndarray:
    """Sum k x k cells (0.25 degree cells -> 1 degree blocks)."""
    ny, nx = a.shape
    return a[: ny // k * k, : nx // k * k].reshape(ny // k, k, nx // k, k).sum(axis=(1, 3))


def best_method(rows: list[dict]) -> str:
    """Lowest mean 1-degree error over the three lead times."""
    score = {}
    for r in rows:
        score.setdefault(r["method"], []).append(r["area_error"])
    return min(score, key=lambda k: float(np.mean(score[k])))


def main() -> int:
    from ingest import db

    conn = db.connect()
    first, last = conn.execute(
        "SELECT min(observed_at AT TIME ZONE 'UTC')::date, "
        "max(observed_at AT TIME ZONE 'UTC')::date FROM fire_detection"
    ).fetchone()
    last -= timedelta(days=1)  # today is incomplete
    days = daily_cells(conn, first, last)
    rows = backtest(days, cell_countries(conn))
    print(f"Fire-guess backtest, {first} to {last} ({len(days)} days, {RES} degree cells)")
    print(f"{'method':24} lead  cell_err  area_err  total_err  IN_err  PK_err   bias")
    for r in rows:
        print(f"{r['method']:24} {r['lead']:>3}d  {r['cell_error']:8.2f}  {r['area_error']:8.2f}  "
              f"{r['total_error']:9.2f}  "
              f"{r.get('total_error_IN', float('nan')):6.2f}  "
              f"{r.get('total_error_PK', float('nan')):6.2f}  {r['bias']:5.2f}")  # fmt: skip
    print("best at the 1-degree scale:", best_method(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
