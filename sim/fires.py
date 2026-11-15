"""Fire detections -> fire events -> clusters -> PM2.5 emissions -> particles.

Event: one fire seen during one satellite pass window. NOAA-20 and NOAA-21 pass about
50 minutes apart and often see the same fire, so touching pixels within a 6-hour window are
one event, sized by the satellite that saw the most pixels (no double counting).
Cluster: events within about 5 km of each other over the whole period (PostGIS DBSCAN).
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from sim import constants as c

# Hot spots seen at the same ~1 km spot on 10 or more of the last 45 days are not field fires
# (cement kilns, steel works, flares, brick kilns): crop fires do not burn one spot for weeks.
STATIC_DAYS, STATIC_WINDOW_DAYS, STATIC_RADIUS_DEG = 10, 45, 0.015
STATIC_CTE = f"""
    static AS (
        SELECT round(longitude::numeric, 2)::float8 AS x, round(latitude::numeric, 2)::float8 AS y
        FROM fire_detection
        WHERE observed_at > %(end)s::timestamptz - interval '{STATIC_WINDOW_DAYS} days'
          AND observed_at <= %(end)s AND published_at <= %(end)s
        GROUP BY 1, 2
        HAVING count(DISTINCT (observed_at AT TIME ZONE 'UTC')::date) >= {STATIC_DAYS}
    )"""
NOT_STATIC = f"""NOT EXISTS (
            SELECT 1 FROM static s WHERE abs(f.longitude - s.x) < {STATIC_RADIUS_DEG}
              AND abs(f.latitude - s.y) < {STATIC_RADIUS_DEG})"""

SQL = f"""
    WITH {STATIC_CTE}, raw AS (
        SELECT f.*, {NOT_STATIC} AS field_fire
        FROM fire_detection f
        WHERE observed_at > %(start)s AND observed_at <= %(end)s
          AND published_at <= %(end)s AND frp_mw > 0
    ), d AS (
        SELECT *, floor(extract(epoch FROM observed_at) / 21600) AS win
        FROM raw WHERE field_fire
    ), g AS (
        SELECT *, ST_ClusterDBSCAN(geom, 0.004, 1) OVER (PARTITION BY win) AS grp FROM d
    ), per_sat AS (
        SELECT win, grp, source, count(*) AS n, sum(frp_mw) AS frp, min(observed_at) AS t,
               sum(longitude * frp_mw) / sum(frp_mw) AS lon,
               sum(latitude * frp_mw) / sum(frp_mw) AS lat
        FROM g GROUP BY win, grp, source
    ), ev AS (
        SELECT DISTINCT ON (win, grp) lon, lat, t, frp, n
        FROM per_sat ORDER BY win, grp, n DESC, frp DESC
    )
    SELECT ST_ClusterDBSCAN(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 0.05, 1) OVER (),
           lon, lat, t, frp, n,
           (SELECT count(*) FROM d), (SELECT count(*) FROM raw WHERE NOT field_fire)
    FROM ev
"""


def emission_area_kg(n_pixels, ef_g_per_kg: float = c.EF_PM25_CENTRAL):
    """Method A: PM2.5 (kg) from burned area, 0.14 km² per detected pixel (FINN v2.5)."""
    return n_pixels * c.PIXEL_AREA_M2 * c.CROP_FUEL_KG_M2 * ef_g_per_kg / 1000.0


def emission_frp_kg(frp_mw, ef_g_per_kg: float = c.EF_PM25_CENTRAL):
    """Method B: PM2.5 (kg) from fire radiative power (MW), assuming BURN_SECONDS of burning."""
    return frp_mw * c.BURN_SECONDS * c.BIOMASS_KG_PER_MJ * ef_g_per_kg / 1000.0


@dataclass
class Fires:
    cluster: np.ndarray  # cluster index per event, 0..n_clusters-1
    lon: np.ndarray
    lat: np.ndarray
    observed: np.ndarray  # datetime64[s], first sighting in the pass window
    frp: np.ndarray  # MW, summed over the event's pixels
    pixels: np.ndarray  # number of 375 m pixels
    n_detections: int = 0  # raw detections before merging the two satellites
    n_static: int = 0  # detections dropped as permanent hot spots (industry)

    @property
    def n_clusters(self) -> int:
        return int(self.cluster.max()) + 1 if len(self.cluster) else 0

    @property
    def area_kg(self):
        return emission_area_kg(self.pixels)

    @property
    def frp_kg(self):
        return emission_frp_kg(self.frp)


def load(conn, start: datetime, end: datetime) -> Fires:
    rows = conn.execute(SQL, {"start": start, "end": end}).fetchall()
    cols = list(zip(*rows, strict=True)) if rows else [[], [], [], [], [], [], [0], [0]]
    naive = [t.astimezone(UTC).replace(tzinfo=None) for t in cols[3]]  # session may not be UTC
    return Fires(
        cluster=np.array(cols[0], dtype=np.intp), lon=np.array(cols[1], dtype=float),
        lat=np.array(cols[2], dtype=float), observed=np.array(naive, dtype="datetime64[s]"),
        frp=np.array(cols[4], dtype=float), pixels=np.array(cols[5], dtype=float),
        n_detections=int(cols[6][0]), n_static=int(cols[7][0]),
    )  # fmt: skip


def assume_future(fires: Fires, t0: np.datetime64, now_s: float, t_end_s: float,
                  lookback_h: float) -> Fires:  # fmt: skip
    """What-if fires for the forecast days: every fire of the last `lookback_h` hours is
    assumed to burn again at the same place and time of day on each coming day, at
    24 / lookback_h of its size, so each future day gets the mean of the last days.
    (Chosen by backtest, see sim/fire_forecast.py.) Cluster index is offset by n_clusters,
    so a what-if twin never mixes with its observed cluster."""
    if not len(fires.frp):
        return fires
    start = (fires.observed - t0).astype(float)
    weight = 24.0 / lookback_h
    index, when = [], []
    n_days = int(np.ceil((t_end_s - start.min()) / 86400.0)) if len(start) else 0
    for day in range(1, n_days + 1):
        t = start + day * 86400.0
        keep = np.flatnonzero((t > now_s) & (t <= t_end_s))
        index.append(keep)
        when.append(t[keep])
    index = np.concatenate(index) if index else np.array([], dtype=np.intp)
    when = np.concatenate(when) if when else np.array([])
    return Fires(
        cluster=fires.cluster[index] + fires.n_clusters, lon=fires.lon[index],
        lat=fires.lat[index], observed=t0 + when.astype("timedelta64[s]"),
        frp=fires.frp[index] * weight, pixels=fires.pixels[index] * weight,
    )  # fmt: skip


def combine(a: Fires, b: Fires) -> Fires:
    return Fires(
        cluster=np.concatenate([a.cluster, b.cluster]), lon=np.concatenate([a.lon, b.lon]),
        lat=np.concatenate([a.lat, b.lat]), observed=np.concatenate([a.observed, b.observed]),
        frp=np.concatenate([a.frp, b.frp]), pixels=np.concatenate([a.pixels, b.pixels]),
        n_detections=a.n_detections, n_static=a.n_static,
    )  # fmt: skip


def particles_for(fires: Fires, t0: np.datetime64, n_total: int, n_min: int = 10, seed: int = 0):
    """Particle arrays (lon, lat, mass_kg, release_s, cluster, mass2_kg): each event gets
    particles in proportion to its method-A emission (at least n_min), released evenly over
    the assumed burn. mass = method A, mass2 = method B, both with the central factor."""
    rng = np.random.default_rng(seed)
    e, e2 = fires.area_kg, fires.frp_kg
    n = np.maximum(n_min, np.round(n_total * e / e.sum())).astype(int)
    rep = np.repeat(np.arange(len(e)), n)
    obs_s = (fires.observed - t0).astype(float)
    release = obs_s[rep] + rng.random(len(rep)) * c.BURN_SECONDS
    return fires.lon[rep], fires.lat[rep], (e / n)[rep], release, fires.cluster[rep], (e2 / n)[rep]


def sources_for(fires: Fires, t0: np.datetime64):
    from sim.engine import Sources

    return Sources(
        lon=fires.lon, lat=fires.lat, start=(fires.observed - t0).astype(float),
        group=fires.cluster, rate=np.stack([fires.area_kg, fires.frp_kg]) / c.BURN_SECONDS,
        duration=c.BURN_SECONDS,
    )  # fmt: skip
