"""Validation of the hindcasts against station measurements (SPEC §5).

    uv run python -m sim.validate          # prints the report, writes docs/validation.md,
                                           # stores the numbers in table validation_skill

What is compared. The model gives fire smoke only; stations measure PM2.5 from every source.
So the test is not "does the number match" but "does simulated smoke arrive when measured
PM2.5 goes up". For every daily run (12:00 UTC) and each 24-hour window after it:
- rank correlation (Spearman) between simulated fire smoke and measured PM2.5;
- the same for the day-to-day change (does a simulated rise come with a measured rise);
- how well a simulated rise picks out the biggest measured rises (AUC, 0.5 = coin toss);
- whether adding the simulation to "tomorrow = today" lowers the error in the other season.
Benchmarks: persistence (the last 24 h measured), and CAMS total PM2.5 (same-day runs).

The thresholds for "useful" were fixed before the results were seen (VERDICT_* below).

Second target (added 15 Nov 2026, after the first result was weak, and fixed before any new
result was seen). Total PM2.5 change also holds fireworks and weather-driven build-up, which
a fire-smoke model cannot know. The weather-adjusted target therefore
- leaves out days whose window, or the 24 h before it, includes a Diwali fireworks night;
- removes from both the measured and the simulated change what the change in forecast
  ventilation (boundary-layer height x wind speed, same forecast, point-in-time) explains,
  in rank space (a partial rank correlation), and scores what is left.
The same thresholds apply. Both targets are always reported; the verdict uses the adjusted
one when ventilation is stored for at least 20 days, and says which it used.
"""

import csv
import json
import sys
from datetime import UTC, date, datetime, timedelta

import numpy as np

from ingest import config, db
from sim import constants as c
from sim.hindcast import SEASONS

WINDOWS = {1: (0, 24), 2: (24, 48), 3: (48, 66)}  # lead day -> hours after the run
MIN_COVER = 0.75  # share of hours in a window that must have a measurement
BLOCK_DAYS, N_BOOT = 5, 1000  # moving-block bootstrap (days are not independent)
SPIKE_QUANTILE = 0.75  # the top quarter of measured day-to-day rises are "spikes"
# A city-season is "useful" for smoke arrival when, for the first 24 h:
VERDICT_MIN_AUC = 0.65  # simulated rises pick out measured spikes at least this well
VERDICT_CI_ABOVE = 0.0  # and the 90% interval of the change correlation is above zero
# Diwali fireworks nights (first and last evening), as already listed under "Limits".
FIREWORKS = {2024: (date(2024, 10, 31), date(2024, 11, 1)),
             2025: (date(2025, 10, 20), date(2025, 10, 21))}  # fmt: skip
FIREWORKS_FROM_UTC_H = 12  # 17:30 IST on the first evening, to the same hour after the last
DSS_FILE = config.ROOT / "docs" / "ref" / "dss_delhi_stubble_share.csv"
REPORT = config.ROOT / "docs" / "validation.md"

SCHEMA = """
CREATE TABLE IF NOT EXISTS validation_skill (
    computed_at timestamptz NOT NULL DEFAULT now(),
    scope       text NOT NULL,      -- 'city' | 'region' | 'ledger' | 'scale' | 'dss'
    name        text NOT NULL,      -- city, country code, ...
    season      integer NOT NULL,   -- 0 = both seasons together
    lead_day    integer NOT NULL,   -- 0 = not by lead
    metrics     jsonb NOT NULL,
    PRIMARY KEY (scope, name, season, lead_day)
);
"""


# ---------------------------------------------------------------- small statistics
def rank(x: np.ndarray) -> np.ndarray:
    """Average ranks (ties share a rank)."""
    order = np.argsort(x, kind="stable")
    r = np.empty(len(x))
    r[order] = np.arange(len(x), dtype=float)
    _, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=r)
    return (sums / counts)[inv]


def spearman(x, y) -> float:
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if len(x) < 5:
        return float("nan")
    rx, ry = rank(x), rank(y)
    if rx.std() == 0 or ry.std() == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def auc(score, event) -> float:
    """Chance that a random event day has a higher score than a random non-event day."""
    score, event = np.asarray(score, dtype=float), np.asarray(event, dtype=bool)
    n1, n0 = int(event.sum()), int((~event).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    r = rank(score)
    return float((r[event].sum() - n1 * (n1 - 1) / 2) / (n1 * n0))


def block_ci(fn, x, y, seed: int = 0) -> tuple[float, float]:
    """90% interval of fn(x, y) from a moving-block bootstrap over days."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    n = len(x)
    if n < 3 * BLOCK_DAYS:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - BLOCK_DAYS + 1, size=(N_BOOT, int(np.ceil(n / BLOCK_DAYS))))
    idx = (starts[:, :, None] + np.arange(BLOCK_DAYS)).reshape(N_BOOT, -1)[:, :n]
    vals = np.array([fn(x[i], y[i]) for i in idx])
    vals = vals[np.isfinite(vals)]
    if len(vals) < N_BOOT / 2:
        return float("nan"), float("nan")
    lo, hi = np.percentile(vals, [5, 95])
    return float(lo), float(hi)


def fit_line(x, y, nonneg: bool = True) -> tuple[float, float]:
    """Least squares y = a + b x; with nonneg, b is not allowed below zero."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if len(x) < 5 or x.std() == 0:
        return float(np.mean(y)) if len(y) else float("nan"), 0.0
    b = float(np.cov(x, y, bias=True)[0, 1] / x.var())
    if nonneg:
        b = max(b, 0.0)
    return float(y.mean() - b * x.mean()), b


def fit_two(p, s, y) -> tuple[float, float, float]:
    """Least squares y = a + b p + k s with k >= 0 (smoke cannot lower PM2.5)."""
    A = np.column_stack([np.ones(len(p)), p, s])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    if coef[2] < 0:
        a, b = fit_line(p, y, nonneg=False)
        return a, b, 0.0
    return float(coef[0]), float(coef[1]), float(coef[2])


def adjusted_scores(d_sim, obs_x) -> tuple[float, float]:
    """(change correlation, spike AUC) after removing, in rank space, what the ventilation
    change x explains from both the simulated and the measured change.
    obs_x: two columns, measured change and ventilation change."""
    d_sim, obs_x = np.asarray(d_sim, dtype=float), np.asarray(obs_x, dtype=float)
    if len(d_sim) < 8:
        return float("nan"), float("nan")
    rx = rank(obs_x[:, 1])
    res = []
    for y in (rank(d_sim), rank(obs_x[:, 0])):
        a, b = fit_line(rx, y, nonneg=False)
        res.append(y - (a + b * rx))
    rs, ro = res
    if rs.std() == 0 or ro.std() == 0:
        return float("nan"), float("nan")
    return float(np.corrcoef(rs, ro)[0, 1]), auc(rs, ro >= np.quantile(ro, SPIKE_QUANTILE))


def fireworks_hours(season: int, t0: datetime, n: int) -> np.ndarray:
    """True for the hours of the Diwali fireworks nights and the mornings after."""
    out = np.zeros(n, dtype=bool)
    if season in FIREWORKS:
        first, last = FIREWORKS[season]
        a = datetime(first.year, first.month, first.day, FIREWORKS_FROM_UTC_H, tzinfo=UTC)
        b = datetime(last.year, last.month, last.day, FIREWORKS_FROM_UTC_H, tzinfo=UTC)
        i0, i1 = hours_between(t0, a), hours_between(t0, b + timedelta(days=1))
        out[max(i0, 0) : max(i1, 0)] = True
    return out


def rmse(e) -> float:
    return float(np.sqrt(np.mean(np.square(e)))) if len(e) else float("nan")


# ---------------------------------------------------------------- data
def hours_between(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 3600)


def load_series(conn, season: int):
    """Hourly arrays per city over the season: measured PM2.5 (median of stations), the kind
    of stations used, and CAMS. Reference monitors are used whenever any reported that hour;
    low-cost sensors only fill hours without one."""
    first, last = SEASONS[season]
    t0 = datetime(first.year, first.month, first.day, tzinfo=UTC) - timedelta(days=3)
    n = hours_between(t0, datetime(last.year, last.month, last.day, tzinfo=UTC)) + 24 * 5
    t1 = t0 + timedelta(hours=n)
    obs = {k: np.full(n, np.nan) for k in config.RECEPTORS}
    ref = {k: np.zeros(n, dtype=bool) for k in config.RECEPTORS}
    nst = {k: np.zeros(n, dtype=int) for k in config.RECEPTORS}
    cens = {k: 0 for k in config.RECEPTORS}
    rows = conn.execute(
        """
        SELECT DISTINCT ON (s.city, h.hour_utc) s.city, h.hour_utc, s.is_reference,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY h.pm25_ugm3),
               count(*), sum(h.censored::int)
        FROM archive_pm25_hourly h JOIN archive_station s USING (source, station_id)
        WHERE h.hour_utc >= %s AND h.hour_utc < %s
        GROUP BY s.city, h.hour_utc, s.is_reference
        ORDER BY s.city, h.hour_utc, s.is_reference DESC
        """,
        (t0, t1),
    ).fetchall()
    for city, hour, is_ref, med, k, n_cens in rows:
        if city not in obs:  # rows of a city that is no longer a receptor
            continue
        i = hours_between(t0, hour.astimezone(UTC))
        obs[city][i], ref[city][i], nst[city][i] = med, is_ref, k
        cens[city] += int(n_cens)
    cams = {k: np.full(n, np.nan) for k in config.RECEPTORS}
    for city, hour, v in conn.execute(
        "SELECT receptor, hour_utc, pm25_ugm3 FROM archive_cams_hourly "
        "WHERE hour_utc >= %s AND hour_utc < %s", (t0, t1),
    ).fetchall():  # fmt: skip
        if city in cams:
            cams[city][hours_between(t0, hour.astimezone(UTC))] = v
    return t0, obs, ref, nst, cens, cams


TRACER_SQL = {
    # CO, mg/m³: median of the reference stations reporting that hour
    "co": """
        SELECT s.city, t.hour_utc, percentile_cont(0.5) WITHIN GROUP (ORDER BY t.value)
        FROM archive_tracer_hourly t JOIN archive_station s USING (source, station_id)
        WHERE t.parameter = 'co' AND s.is_reference AND t.hour_utc >= %s AND t.hour_utc < %s
        GROUP BY 1, 2""",
    # fine fraction PM2.5/PM10: per station-hour ratio, then the median of the stations.
    # PM10 below 5 µg/m³ or a ratio above 1.2 is an instrument mismatch, not air.
    "fine_fraction": """
        SELECT s.city, p.hour_utc, percentile_cont(0.5) WITHIN GROUP (ORDER BY
               p.pm25_ugm3 / t.value)
        FROM archive_pm25_hourly p
        JOIN archive_tracer_hourly t ON t.source = p.source AND t.station_id = p.station_id
             AND t.hour_utc = p.hour_utc AND t.parameter = 'pm10'
        JOIN archive_station s ON s.source = p.source AND s.station_id = p.station_id
        WHERE s.is_reference AND NOT p.censored AND t.value >= 5
          AND p.pm25_ugm3 <= 1.2 * t.value AND p.hour_utc >= %s AND p.hour_utc < %s
        GROUP BY 1, 2""",
}
TRACER_ADJUST = {"co": True, "fine_fraction": False}  # remove the ventilation change?


def load_tracers(conn, t0: datetime, n: int) -> dict[str, dict[str, np.ndarray]]:
    """Hourly smoke-tracer arrays per city over the season (third target)."""
    out = {}
    for name, sql in TRACER_SQL.items():
        arr = {k: np.full(n, np.nan) for k in config.RECEPTORS}
        for city, hour, v in conn.execute(sql, (t0, t0 + timedelta(hours=n))).fetchall():
            if city in arr:
                arr[city][hours_between(t0, hour.astimezone(UTC))] = v
        out[name] = arr
    return out


def load_runs(conn, season: int, t0: datetime, n: int):
    """Per city: list of (run_at index, {col: hourly array aligned to the season hours})."""
    out = {k: {} for k in config.RECEPTORS}
    for run_at, city, valid, a, b, w, vent in conn.execute(
        """
        SELECT r.run_at, h.receptor, h.valid_at, h.pm25_ugm3, h.pm25_frp_ugm3,
               h.pm25_whatif_ugm3, h.vent_m2s
        FROM hindcast_hourly h JOIN hindcast_run r USING (run_id) WHERE r.season = %s
        """,
        (season,),
    ).fetchall():
        if city not in out:
            continue
        key = hours_between(t0, run_at.astimezone(UTC))
        run = out[city].setdefault(
            key, {k: np.full(n, np.nan) for k in ("seen", "frp", "whatif", "vent")}
        )
        i = hours_between(t0, valid.astimezone(UTC))
        if 0 <= i < n:
            run["seen"][i], run["frp"][i], run["whatif"][i] = a, b, w
            run["vent"][i] = np.nan if vent is None else vent
    return out


def window_mean(x: np.ndarray, i0: int, i1: int, min_cover: float = MIN_COVER) -> float:
    seg = x[max(i0, 0) : i1]
    ok = np.isfinite(seg)
    if i0 < 0 or not len(seg) or ok.mean() < min_cover:
        return float("nan")
    return float(seg[ok].mean())


def pairs(obs, cams, runs, lead: int, fireworks=None) -> dict[str, np.ndarray]:
    """One row per run day: window means after the run and for the 24 h before it."""
    h0, h1 = WINDOWS[lead]
    cols = {k: [] for k in ("day", "obs", "obs_prev", "cams", "seen", "whatif", "frp",
                            "seen_prev", "vent", "vent_prev", "fireworks")}  # fmt: skip
    for k in sorted(runs):
        run = runs[k]
        # measured hour i covers [i, i+1); simulated value at index i is the arrival at i:00
        cols["day"].append(k)
        cols["obs"].append(window_mean(obs, k + h0, k + h1))
        cols["obs_prev"].append(window_mean(obs, k - 24, k))
        cols["cams"].append(window_mean(cams, k + h0, k + h1))
        cols["seen_prev"].append(window_mean(run["seen"], k - 24, k))
        vent = run.get("vent", np.full(len(obs), np.nan))
        cols["vent"].append(window_mean(vent, k + h0, k + h1))
        cols["vent_prev"].append(window_mean(vent, k - 24, k))
        fw = fireworks is not None and (
            fireworks[max(k + h0, 0) : k + h1].any() or fireworks[max(k - 24, 0) : k].any()
        )
        cols["fireworks"].append(float(fw))
        for name in ("seen", "whatif", "frp"):
            cols[name].append(window_mean(run[name], k + h0, k + h1))
    return {k: np.array(v, dtype=float) for k, v in cols.items()}


# ---------------------------------------------------------------- metrics
def city_metrics(p: dict) -> dict:
    ok = np.isfinite(p["obs"]) & np.isfinite(p["obs_prev"]) & np.isfinite(p["whatif"])
    q = {k: v[ok] for k, v in p.items()}
    n = int(ok.sum())
    if n < 20:
        return {"n_days": n, "verdict": "too few days with measurements"}
    d_obs = q["obs"] - q["obs_prev"]
    d_sim = q["whatif"] - q["seen_prev"]
    spike = d_obs >= np.quantile(d_obs, SPIKE_QUANTILE)
    has_cams = np.isfinite(q["cams"])
    out = {
        "n_days": n,
        "obs_mean": float(q["obs"].mean()),
        "sim_mean": float(q["seen"].mean()),
        "sim_whatif_mean": float(q["whatif"].mean()),
        "sim_frp_mean": float(q["frp"].mean()),
        # level: does more simulated smoke go with higher measured PM2.5
        "r_level": spearman(q["whatif"], q["obs"]),
        "r_level_ci": block_ci(spearman, q["whatif"], q["obs"]),
        "r_level_observed_fires_only": spearman(q["seen"], q["obs"]),
        "r_level_persistence": spearman(q["obs_prev"], q["obs"]),
        "r_level_cams": spearman(q["cams"][has_cams], q["obs"][has_cams]),
        # change: does a simulated rise come with a measured rise
        "r_change": spearman(d_sim, d_obs),
        "r_change_ci": block_ci(spearman, d_sim, d_obs),
        "r_change_cams": spearman((q["cams"] - q["obs_prev"])[has_cams], d_obs[has_cams]),
        "auc_spike": auc(d_sim, spike),
        "auc_spike_ci": block_ci(lambda a, b: auc(a, b > 0.5), d_sim, spike.astype(float)),
        "auc_spike_cams": auc((q["cams"] - q["obs_prev"])[has_cams], spike[has_cams]),
        "spike_threshold_ugm3": float(np.quantile(d_obs, SPIKE_QUANTILE)),
    }
    lo = out["r_change_ci"][0]
    good = (out["auc_spike"] >= VERDICT_MIN_AUC) and np.isfinite(lo) and lo > VERDICT_CI_ABOVE
    out["verdict_total_change"] = "useful" if good else "not reliable here"
    out["verdict"], out["verdict_basis"] = out["verdict_total_change"], "total PM2.5 change"
    # Second target: fireworks days out, ventilation change removed (see module docstring).
    nan = np.full(n, np.nan)
    fireworks = q.get("fireworks", np.zeros(n)) >= 0.5
    with np.errstate(invalid="ignore", divide="ignore"):
        d_vent = np.log(q.get("vent", nan) / q.get("vent_prev", nan))
    use = np.isfinite(d_vent) & ~fireworks
    out["n_fireworks_days_dropped"] = int(fireworks.sum())
    out["n_days_adjusted"] = int(use.sum())
    if use.sum() >= 20:
        ds, ox = d_sim[use], np.column_stack([d_obs[use], d_vent[use]])
        out["r_change_adj"], out["auc_spike_adj"] = adjusted_scores(ds, ox)
        out["r_change_adj_ci"] = block_ci(lambda a, b: adjusted_scores(a, b)[0], ds, ox)
        out["auc_spike_adj_ci"] = block_ci(lambda a, b: adjusted_scores(a, b)[1], ds, ox)
        # how much of the measured change still air alone goes with (negative = as expected)
        out["r_change_ventilation_only"] = spearman(d_vent[use], d_obs[use])
        lo = out["r_change_adj_ci"][0]
        good = (np.isfinite(out["auc_spike_adj"]) and out["auc_spike_adj"] >= VERDICT_MIN_AUC
                and np.isfinite(lo) and lo > VERDICT_CI_ABOVE)  # fmt: skip
        out["verdict_adjusted"] = "useful" if good else "not reliable here"
        out["verdict"], out["verdict_basis"] = out["verdict_adjusted"], "weather-adjusted change"
    return out


def tracer_metrics(p: dict, adjust: bool) -> dict:
    """Third target: does a simulated rise come with a rise in a smoke tracer? Fireworks days
    out; with `adjust`, the ventilation change is removed as in the second target."""
    fireworks = p["fireworks"] >= 0.5
    ok = (np.isfinite(p["obs"]) & np.isfinite(p["obs_prev"]) & np.isfinite(p["whatif"])
          & np.isfinite(p["seen_prev"]) & ~fireworks)  # fmt: skip
    if adjust:
        with np.errstate(invalid="ignore", divide="ignore"):
            d_vent = np.log(p["vent"] / p["vent_prev"])
        ok &= np.isfinite(d_vent)
    n = int(ok.sum())
    if n < 20:
        return {"n_days": n, "verdict": "too few days with measurements"}
    d_obs = (p["obs"] - p["obs_prev"])[ok]
    d_sim = (p["whatif"] - p["seen_prev"])[ok]
    if adjust:
        ox = np.column_stack([d_obs, d_vent[ok]])
        r, a = adjusted_scores(d_sim, ox)
        r_ci = block_ci(lambda x, y: adjusted_scores(x, y)[0], d_sim, ox)
        a_ci = block_ci(lambda x, y: adjusted_scores(x, y)[1], d_sim, ox)
    else:
        spike = d_obs >= np.quantile(d_obs, SPIKE_QUANTILE)
        r, a = spearman(d_sim, d_obs), auc(d_sim, spike)
        r_ci = block_ci(spearman, d_sim, d_obs)
        a_ci = block_ci(lambda x, y: auc(x, y > 0.5), d_sim, spike.astype(float))
    good = np.isfinite(a) and a >= VERDICT_MIN_AUC and np.isfinite(r_ci[0]) and r_ci[0] > 0
    return {"n_days": n, "weather_adjusted": adjust, "obs_mean": float(p["obs"][ok].mean()),
            "r_change": r, "r_change_ci": r_ci, "auc_spike": a, "auc_spike_ci": a_ci,
            "verdict": "useful" if good else "not reliable here"}  # fmt: skip


def added_value(train: dict, test: dict) -> dict:
    """Fit on one season, score on the other: does adding simulated smoke to persistence
    lower the error? Also returns the fitted scale (µg/m³ measured per µg/m³ simulated)."""

    def clean(p):
        ok = np.isfinite(p["obs"]) & np.isfinite(p["obs_prev"]) & np.isfinite(p["whatif"])
        return p["obs_prev"][ok], p["whatif"][ok], p["obs"][ok]

    (p0, s0, y0), (p1, s1, y1) = clean(train), clean(test)
    if len(y0) < 20 or len(y1) < 20:
        return {}
    a, b = fit_line(p0, y0, nonneg=False)
    a2, b2, k2 = fit_two(p0, s0, y0)
    e_pers = rmse(y1 - (a + b * p1))
    e_both = rmse(y1 - (a2 + b2 * p1 + k2 * s1))
    return {"rmse_persistence": e_pers, "rmse_with_sim": e_both,
            "improvement": 1.0 - e_both / e_pers, "scale_with_persistence": k2,
            "n_train": len(y0), "n_test": len(y1)}  # fmt: skip


def scale_fit(p: dict) -> dict:
    """Simple scale: measured = background + scale x simulated (same-day window).
    This is an UPPER guide: still, calm days raise both smoke and all other pollution, so the
    slope also soaks up non-fire pollution that happens on the same days."""
    ok = np.isfinite(p["obs"]) & np.isfinite(p["seen"])
    if ok.sum() < 20:
        return {}
    a, b = fit_line(p["seen"][ok], p["obs"][ok])
    return {
        "background_ugm3": a,
        "scale": b,
        "n_days": int(ok.sum()),
        "sim_mean": float(p["seen"][ok].mean()),
        "obs_mean": float(p["obs"][ok].mean()),
    }


def ledger_revision(conn, season: int) -> dict:
    """How much the cross-border share for the same 24 h changes between the forecast made
    1 day earlier and the one made on the day (observed fires only, as in the live ledger).
    Returns percentage points per receptor: median and 90th percentile of the change."""
    rows = conn.execute(
        """
        WITH w AS (
            SELECT s.receptor, r.run_at,
                   floor(extract(epoch FROM s.valid_at - r.run_at) / 86400)::int AS lead,
                   s.country, sum(s.pm25_ugm3) AS dose
            FROM hindcast_source s JOIN hindcast_run r USING (run_id)
            WHERE r.season = %s AND s.valid_at >= r.run_at
              AND s.valid_at < r.run_at + interval '48 hours'
            GROUP BY 1, 2, 3, 4
        ), t AS (
            SELECT receptor, run_at + lead * interval '1 day' AS window_start, lead, country,
                   dose, sum(dose) OVER (PARTITION BY receptor, run_at, lead) AS total
            FROM w
        )
        SELECT a.receptor, a.country, a.dose / a.total, b.dose / b.total
        FROM t a JOIN t b USING (receptor, window_start, country)
        WHERE a.lead = 0 AND b.lead = 1 AND a.total > 0 AND b.total > 0
        """,
        (season,),
    ).fetchall()
    out = {}
    for city in config.RECEPTORS:
        d = np.array([abs(x - y) * 100 for r, cc, x, y in rows if r == city and cc == "PK"])
        if len(d) >= 10:
            out[city] = {"n_days": len(d), "median_points": float(np.median(d)),
                         "p90_points": float(np.percentile(d, 90))}  # fmt: skip
    return out


def read_dss() -> list[dict]:
    with DSS_FILE.open() as f:
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    for r in rows:
        r["period_start"] = date.fromisoformat(r["period_start"])
        r["period_end"] = date.fromisoformat(r["period_end"])
        r["share_pct"] = float(r["share_pct"])
    return rows


def dss_compare(t0s: dict, obs: dict, runs: dict, scale_low: float, scale_high: float):
    """Our Delhi fire-smoke share next to each published DSS value. Days in IST, lead day 1
    numbers (hours 0-24 of each run cover 17:30 IST to 17:30 IST, so a calendar day is built
    from the two runs that cover it). Share = scale x simulated / measured."""
    out = []
    for row in read_dss():
        season = row["period_start"].year
        if season not in t0s:
            continue
        t0, o, rr = t0s[season], obs[season]["Delhi"], runs[season]["Delhi"]
        sim = np.full(len(o), np.nan)
        for k in sorted(rr):  # first 24 h of every run, later runs overwrite nothing
            seg = rr[k]["seen"][k : k + 24]
            sim[k:k + len(seg)] = np.where(np.isfinite(sim[k:k + len(seg)]),
                                           sim[k:k + len(seg)], seg)  # fmt: skip
        ist = timedelta(hours=5, minutes=30)
        a = datetime.combine(row["period_start"], datetime.min.time(), UTC) - ist
        b = datetime.combine(row["period_end"] + timedelta(days=1), datetime.min.time(), UTC) - ist
        i0, i1 = int(np.ceil((a - t0).total_seconds() / 3600)), hours_between(t0, b)
        s, m = window_mean(sim, i0, i1, 0.9), window_mean(o, i0, i1, 0.5)
        out.append({
            "period": f"{row['period_start']}" if row["period_start"] == row["period_end"]
            else f"{row['period_start']} to {row['period_end']}",
            "dss_pct": row["share_pct"], "sim_ugm3": s, "obs_ugm3": m,
            "ours_pct_low": 100 * scale_low * s / m if m else float("nan"),
            "ours_pct_high": 100 * scale_high * s / m if m else float("nan"),
            "unscaled_pct": 100 * s / m if m else float("nan"),
            "scale_to_match_dss": row["share_pct"] / 100 * m / s if s and s > 0 else float("nan"),
            "source": row["source"], "source_url": row["source_url"],
        })  # fmt: skip
    return out


# ---------------------------------------------------------------- report
def compute(conn) -> dict:
    from ingest.cams import ARCHIVE_SCHEMA
    from spark_jobs.station_archive import SCHEMA as STATION_SCHEMA

    conn.execute(STATION_SCHEMA)  # empty tables give "too few days", not a crash
    conn.execute(ARCHIVE_SCHEMA)
    res = {"city": {}, "added": {}, "scale": {}, "ledger": {}, "stations": {}, "runs": {},
           "tracer": {}}  # fmt: skip
    P, t0s, all_obs, all_runs = {}, {}, {}, {}
    for season in SEASONS:
        t0, obs, ref, nst, cens, cams = load_series(conn, season)
        runs = load_runs(conn, season, t0, len(obs["Delhi"]))
        fw = fireworks_hours(season, t0, len(obs["Delhi"]))
        tracers = load_tracers(conn, t0, len(obs["Delhi"]))
        t0s[season], all_obs[season], all_runs[season] = t0, obs, runs
        res["runs"][season] = len(runs["Delhi"])
        for city in config.RECEPTORS:
            has = np.isfinite(obs[city])
            res["stations"][(city, season)] = {
                "hours": int(has.sum()), "reference_share": float(ref[city][has].mean())
                if has.any() else 0.0,
                "median_stations": float(np.median(nst[city][has])) if has.any() else 0.0,
                "censored_station_hours": cens[city],
            }  # fmt: skip
            for lead in WINDOWS:
                p = pairs(obs[city], cams[city], runs[city], lead, fw)
                P[(city, season, lead)] = p
                res["city"][(city, season, lead)] = city_metrics(p)
            res["scale"][(city, season)] = scale_fit(P[(city, season, 1)])
            nan = np.full(len(obs[city]), np.nan)
            for name, series in tracers.items():
                pt = pairs(series[city], nan, runs[city], 1, fw)
                res["tracer"][(city, season, name)] = tracer_metrics(pt, TRACER_ADJUST[name])
        res["ledger"][season] = ledger_revision(conn, season)
    seasons = sorted(SEASONS)
    for city in config.RECEPTORS:
        for lead in WINDOWS:
            for train, test in ((seasons[0], seasons[-1]), (seasons[-1], seasons[0])):
                res["added"][(city, test, lead)] = added_value(
                    P[(city, train, lead)], P[(city, test, lead)]
                )
    delhi = [res["scale"][("Delhi", s)].get("scale", np.nan) for s in seasons]
    fin = [x for x in delhi if np.isfinite(x)]
    res["scale_range_delhi"] = (min(fin), max(fin)) if fin else (np.nan, np.nan)
    res["dss"] = dss_compare(t0s, all_obs, all_runs, *res["scale_range_delhi"])
    return res


def _f(x, nd=2) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x:.{nd}f}"


def _ci(pair) -> str:
    return f"{_f(pair[0])} to {_f(pair[1])}"


def report(res: dict) -> str:
    L = ["# Validation: hindcasts of the 2024 and 2025 seasons", ""]
    L += [
        "Generated by `uv run python -m sim.validate`. One run per day at 12:00 UTC, "
        f"1 Oct – 30 Nov ({', '.join(f'{s}: {n} runs' for s, n in res['runs'].items())}), "
        "each using only weather forecasts and fires published before the run.",
        "",
        "**Read this first.** The model gives fire smoke only. Stations measure PM2.5 from all "
        "sources. A good score here means simulated smoke arrives when measured PM2.5 goes up; "
        "it does not mean the simulated amount is right. All simulated values are labelled "
        "simulated. Days are not independent, so ranges come from a 5-day block bootstrap.",
        "",
        "**Model version.** Day fires are spread over the afternoon (12:00 to 18:30 IST, peak "
        f"16:30) and multiplied by {c.DAY_FIRE_FACTOR:.1f} for the fires the 13:30 satellite "
        f"pass does not see; smoke is followed for {c.SMOKE_MAX_AGE_H // 24} days. The timing "
        "and the factor come from Jethva (2026, doi:10.3390/rs18101506), published after "
        "these seasons: the data in every run is point-in-time, this part of the method is "
        "hindsight. One factor is used for all years and all places.",
        "",
        "## Measurements used",
        "",
        "| City | Season | Hours with data | Reference monitors | Stations per hour (median) |",
        "|---|---|---|---|---|",
    ]
    for (city, season), s in res["stations"].items():
        kind = f"{s['reference_share']:.0%}" + (" (LOW-COST sensors)" if s["reference_share"] < 0.5
                                                else "")  # fmt: skip
        L.append(f"| {city} | {season} | {s['hours']} | {kind} | {s['median_stations']:.0f} |")
    L += [
        "",
        "## Skill per city (first 24 hours after the run)",
        "",
        "Change correlation: rank correlation between the simulated and the measured change "
        "from the previous 24 h (1 = perfect, 0 = none). Spike AUC: how well a simulated rise "
        "picks out the top quarter of measured rises (1 = perfect, 0.5 = coin toss). "
        f"Rule fixed in advance: **useful** needs AUC ≥ {VERDICT_MIN_AUC} and a change "
        "correlation whose 90% range is above zero.",
        "",
        "| City | Season | Days | Change corr. (90% range) | Spike AUC (90% range) | "
        "CAMS change corr. | CAMS spike AUC | Verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for (city, season, lead), m in res["city"].items():
        if lead != 1:
            continue
        if "r_change" not in m:
            L.append(f"| {city} | {season} | {m['n_days']} | | | | | {m['verdict']} |")
            continue
        L.append(
            f"| {city} | {season} | {m['n_days']} | {_f(m['r_change'])} ({_ci(m['r_change_ci'])}) "
            f"| {_f(m['auc_spike'])} ({_ci(m['auc_spike_ci'])}) | {_f(m['r_change_cams'])} "
            f"| {_f(m['auc_spike_cams'])} | **{m['verdict']}** |"
        )
    L += [
        "",
        "CAMS here is the same-day CAMS forecast of total PM2.5 (all sources), so it has an "
        "easier job than a fire-only model and is not a like-for-like rival.",
        "",
        "## Skill per city, weather-adjusted target (first 24 hours)",
        "",
        "Added after the first result, fixed before any new result was seen. Days around the "
        "Diwali fireworks nights are left out. What the change in forecast ventilation "
        "(boundary-layer height × wind speed) explains is removed from both the measured and "
        "the simulated change, so this asks: does simulated smoke rise when measured PM2.5 "
        "rises by more than still air alone would give? Same thresholds. "
        "Ventilation only = rank correlation of the ventilation change with the measured "
        "change (negative = still air goes with a rise). The verdict column of the table "
        "above uses this target where ventilation is stored.",
        "",
        "| City | Season | Days | Fireworks days left out | Ventilation only | "
        "Adjusted change corr. (90% range) | Adjusted spike AUC (90% range) | Verdict on "
        "total change | Verdict, adjusted |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for (city, season, lead), m in res["city"].items():
        if lead == 1 and "r_change_adj" in m:
            L.append(
                f"| {city} | {season} | {m['n_days_adjusted']} | {m['n_fireworks_days_dropped']} "
                f"| {_f(m['r_change_ventilation_only'])} | {_f(m['r_change_adj'])} "
                f"({_ci(m['r_change_adj_ci'])}) | {_f(m['auc_spike_adj'])} "
                f"({_ci(m['auc_spike_adj_ci'])}) | {m['verdict_total_change']} | "
                f"**{m['verdict_adjusted']}** |"
            )
    L += [
        "",
        "## Skill per city, smoke tracers (first 24 hours)",
        "",
        "Added 18 Nov 2026, rules fixed before any tracer result was seen. Total PM2.5 also "
        "holds traffic, dust and still-air build-up, so two measured signals that follow smoke "
        "more closely are scored, from the same reference stations. CO: carbon monoxide, which "
        "fires give off (traffic too); the ventilation change is removed as above. Fine "
        "fraction: PM2.5 ÷ PM10, which rises with smoke and falls with dust; a ratio is not "
        "diluted by still air, so nothing is removed. Fireworks days are left out of both. "
        "Same thresholds. These do not change the map's verdict.",
        "",
        "| City | Season | Tracer | Days | Change corr. (90% range) | Spike AUC (90% range) "
        "| Verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    label = {"co": "CO, weather-adjusted", "fine_fraction": "fine fraction"}
    for (city, season, name), m in res.get("tracer", {}).items():
        if "r_change" in m:
            L.append(
                f"| {city} | {season} | {label[name]} | {m['n_days']} | {_f(m['r_change'])} "
                f"({_ci(m['r_change_ci'])}) | {_f(m['auc_spike'])} ({_ci(m['auc_spike_ci'])}) "
                f"| **{m['verdict']}** |"
            )
        else:
            L.append(f"| {city} | {season} | {label[name]} | {m['n_days']} | – | – "
                     f"| {m['verdict']} |")  # fmt: skip
    L += [
        "",
        "## Skill by lead day",
        "",
        "Level correlation: rank correlation of simulated fire smoke (with the what-if repeats "
        "for future fires) with measured PM2.5. Persistence = the previous 24 h measured.",
        "",
        "| City | Season | Lead day | Level corr. | observed fires only | Persistence | CAMS | "
        "Change corr. | Spike AUC |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for (city, season, lead), m in res["city"].items():
        if "r_level" in m:
            L.append(
                f"| {city} | {season} | {lead} | {_f(m['r_level'])} | "
                f"{_f(m['r_level_observed_fires_only'])} | {_f(m['r_level_persistence'])} | "
                f"{_f(m['r_level_cams'])} | {_f(m['r_change'])} | {_f(m['auc_spike'])} |"
            )
    L += [
        "",
        "## Skill per region",
        "",
        "Mean of the city scores in each region ("
        + "; ".join(
            f"{reg}: {', '.join(c for c in config.RECEPTORS if config.REGION[c] == reg)}"
            for reg in dict.fromkeys(config.REGION.values())
        )
        + "), first 24 hours. Receptors are Indian cities only.",
        "",
        "| Region | Season | Change corr. | Spike AUC | Adjusted change corr. | "
        "Adjusted spike AUC | Cities useful |",
        "|---|---|---|---|---|---|---|",
    ]
    for (code, season), m in region_metrics(res).items():
        L.append(f"| {code} | {season} | {_f(m['r_change'])} | {_f(m['auc_spike'])} | "
                 f"{_f(m['r_change_adj'])} | {_f(m['auc_spike_adj'])} | "
                 f"{m['n_useful']} of {m['n_cities']} |")  # fmt: skip
    L += [
        "",
        '## Does the simulation add to "tomorrow = today"?',
        "",
        "Fitted on one season, scored on the other. Improvement = how much lower the error is "
        "when simulated smoke is added to persistence (negative = worse).",
        "",
        "| City | Scored on | Lead day | Error, persistence (µg/m³) | Error, with simulation | "
        "Improvement |",
        "|---|---|---|---|---|---|",
    ]
    for (city, season, lead), a in res["added"].items():
        if a:
            L.append(f"| {city} | {season} | {lead} | {_f(a['rmse_persistence'], 0)} | "
                     f"{_f(a['rmse_with_sim'], 0)} | {a['improvement']:+.1%} |")  # fmt: skip
    L += [
        "",
        "## Emission scale",
        "",
        "Simulated fire smoke is far below measured PM2.5. The table shows the factor that a "
        "straight-line fit (measured = background + scale × simulated) gives. It is an upper "
        "guide, not a measurement: calm days raise fire smoke and every other kind of "
        "pollution together, so part of the scale is not smoke.",
        "",
        "| City | Season | Simulated mean (µg/m³) | Measured mean | Fitted background | "
        "Fitted scale |",
        "|---|---|---|---|---|---|",
    ]
    for (city, season), s in res["scale"].items():
        if s:
            L.append(f"| {city} | {season} | {_f(s['sim_mean'])} | {_f(s['obs_mean'], 0)} | "
                     f"{_f(s['background_ugm3'], 0)} | {_f(s['scale'], 1)} |")  # fmt: skip
    lo_s, hi_s = res["scale_range_delhi"]
    L += [
        "",
        f"Emission range already carried by every run: ×{c.EF_PM25_LOW / c.EF_PM25_CENTRAL:.2f} "
        f"to ×{c.EF_PM25_HIGH / c.EF_PM25_CENTRAL:.2f} around the central factor.",
        "",
        "## Against DSS (IITM) stubble shares for Delhi",
        "",
        "DSS values are the few dated figures found in published sources (no download API). "
        "DSS uses the previous evening's fire counts and its own emissions; this model uses "
        "live fires and forecast winds. It is a benchmark, not truth, and this model does not "
        'replace it. "Ours" = scale × simulated ÷ measured, with the Delhi scale range above '
        f"({_f(lo_s, 1)} to {_f(hi_s, 1)}).",
        "",
        "| Period (IST) | DSS share | Ours, unscaled | Ours, scaled (range) | Scale that would "
        "match DSS | Source |",
        "|---|---|---|---|---|---|",
    ]
    for d in res["dss"]:
        L.append(
            f"| {d['period']} | {d['dss_pct']:.1f}% | {_f(d['unscaled_pct'], 1)}% | "
            f"{_f(d['ours_pct_low'], 0)}–{_f(d['ours_pct_high'], 0)}% | "
            f"{_f(d['scale_to_match_dss'], 0)} | [{d['source']}]({d['source_url']}) |"
        )
    L += [
        "",
        "## Smoke source ledger: how much a share moves",
        "",
        "For the same 24 hours, the share of an Indian city's simulated fire smoke that comes "
        "from fires in Pakistan (the only neighbouring country inside the v1 domain; the rest "
        "is from Indian states and disputed areas) is computed twice: by the run of that day "
        "and by the run one day earlier. The difference is a measure of forecast uncertainty "
        "in the ledger. Shares do not depend on the emission scale.",
        "",
        "| City | Season | Days | Typical change (median, points) | 9 days in 10 below |",
        "|---|---|---|---|---|",
    ]
    for season, by_city in res["ledger"].items():
        for city, m in by_city.items():
            L.append(f"| {city} | {season} | {m['n_days']} | {m['median_points']:.0f} | "
                     f"{m['p90_points']:.0f} |")  # fmt: skip
    L += [
        "",
        "## Limits",
        "",
        "- Archive weather is stored every 3 hours (the live system uses every hour).",
        "- Archive fires are the reprocessed FIRMS product with an assumed 3-hour delay; the "
        "live feed of the day may have differed slightly.",
        f"- Two seasons, one run per day, {len(config.RECEPTORS)} cities, all in the north. "
        "Shares from fires "
        "abroad cannot be checked against stations in the source area.",
        "- Scores for one city-season rest on about 60 days that are not independent.",
        "- Measured PM2.5 includes fireworks (Diwali: 31 Oct – 1 Nov 2024, 20–21 Oct 2025), "
        "traffic, industry and dust. None of these are in the model. The weather-adjusted "
        "target leaves the fireworks days out; the first table does not.",
        "- The missed-fire factor and the afternoon timing rest on one study of Indian Punjab "
        "and Haryana; they are applied to every fire in the domain.",
        "- Sources: OpenAQ (CPCB, US diplomatic posts via AirNow, low-cost networks); India Air "
        "Quality Database, XKDR Forum (CPCB, CC BY 4.0); NASA FIRMS; NOAA GFS; CAMS via "
        "Open-Meteo.",
        "",
    ]
    return "\n".join(L)


def region_metrics(res: dict) -> dict:
    out = {}
    for season in SEASONS:
        for code in dict.fromkeys(config.REGION.values()):
            ms = [
                res["city"][(city, season, 1)]
                for city in config.RECEPTORS
                if config.REGION[city] == code and "r_change" in res["city"][(city, season, 1)]
            ]
            if ms:
                out[(code, season)] = {
                    "r_change": float(np.nanmean([m["r_change"] for m in ms])),
                    "auc_spike": float(np.nanmean([m["auc_spike"] for m in ms])),
                    "r_change_adj": float(np.mean([m.get("r_change_adj", np.nan) for m in ms])),
                    "auc_spike_adj": float(np.mean([m.get("auc_spike_adj", np.nan) for m in ms])),
                    "n_useful": sum(m["verdict"] == "useful" for m in ms), "n_cities": len(ms),
                }  # fmt: skip
    return out


def _json(m: dict) -> str:
    def fix(v):
        if isinstance(v, tuple | list):
            return [fix(x) for x in v]
        if isinstance(v, float | np.floating):
            return None if not np.isfinite(v) else round(float(v), 4)
        return v

    return json.dumps({k: fix(v) for k, v in m.items()})


def store(conn, res: dict) -> None:
    conn.execute(SCHEMA)
    conn.execute("TRUNCATE validation_skill")
    ins = "INSERT INTO validation_skill (scope, name, season, lead_day, metrics) VALUES "
    ins += "(%s, %s, %s, %s, %s)"
    for (city, season, lead), m in res["city"].items():
        m = {
            **m,
            **{f"added_{k}": v for k, v in res["added"].get((city, season, lead), {}).items()},
        }
        if lead == 1:
            m = {**m, **{f"stations_{k}": v for k, v in res["stations"][(city, season)].items()}}
        conn.execute(ins, ("city", city, season, lead, _json(m)))
    for (code, season), m in region_metrics(res).items():
        conn.execute(ins, ("region", code, season, 1, _json(m)))
    for (city, season), m in res["scale"].items():
        conn.execute(ins, ("scale", city, season, 0, _json(m)))
    for season, by_city in res["ledger"].items():
        for city, m in by_city.items():
            conn.execute(ins, ("ledger", city, season, 0, _json(m)))
    for (city, season, name), m in res.get("tracer", {}).items():
        conn.execute(ins, (f"tracer_{name}", city, season, 1, _json(m)))
    for i, d in enumerate(res["dss"]):
        conn.execute(ins, ("dss", d["period"], 0, i, _json(d)))
    conn.commit()


def main() -> int:
    conn = db.connect()
    if not conn.execute("SELECT to_regclass('hindcast_run')").fetchone()[0]:
        raise SystemExit("No hindcasts. Run: uv run python -m sim.hindcast run 2024")
    res = compute(conn)
    store(conn, res)
    text = report(res)
    REPORT.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
