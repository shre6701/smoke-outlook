"""Fire detections -> clusters -> PM2.5 emissions -> particles."""

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from sim import constants as c

# Detections closer than about 5 km chain into one cluster (PostGIS DBSCAN, 0.05 degrees).
SQL = """
    SELECT ST_ClusterDBSCAN(geom, 0.05, 1) OVER () AS cluster_id,
           longitude, latitude, observed_at, frp_mw
    FROM fire_detection
    WHERE observed_at > %(start)s AND observed_at <= %(end)s
      AND published_at <= %(end)s AND frp_mw > 0
"""


def emission_kg(frp_mw, ef_g_per_kg: float = c.EF_PM25_CENTRAL):
    """PM2.5 (kg) from fire radiative power (MW), assuming BURN_SECONDS of burning."""
    return frp_mw * c.BURN_SECONDS * c.BIOMASS_KG_PER_MJ * ef_g_per_kg / 1000.0


@dataclass
class Fires:
    cluster: np.ndarray  # cluster index per detection, 0..n_clusters-1
    lon: np.ndarray
    lat: np.ndarray
    observed: np.ndarray  # datetime64[s]
    frp: np.ndarray

    @property
    def n_clusters(self) -> int:
        return int(self.cluster.max()) + 1 if len(self.cluster) else 0


def load(conn, start: datetime, end: datetime) -> Fires:
    rows = conn.execute(SQL, {"start": start, "end": end}).fetchall()
    cols = list(zip(*rows, strict=True)) if rows else [[], [], [], [], []]
    naive = [t.replace(tzinfo=None) for t in cols[3]]
    return Fires(
        cluster=np.array(cols[0], dtype=np.intp), lon=np.array(cols[1], dtype=float),
        lat=np.array(cols[2], dtype=float), observed=np.array(naive, dtype="datetime64[s]"),
        frp=np.array(cols[4], dtype=float),
    )  # fmt: skip


def particles_for(fires: Fires, t0: np.datetime64, n_total: int, n_min: int = 10, seed: int = 0):
    """Particle arrays (lon, lat, mass_kg, release_s, cluster): each detection gets particles
    in proportion to its emission (at least n_min), released evenly over the assumed burn."""
    rng = np.random.default_rng(seed)
    e = emission_kg(fires.frp)
    n = np.maximum(n_min, np.round(n_total * e / e.sum())).astype(int)
    rep = np.repeat(np.arange(len(e)), n)
    obs_s = (fires.observed - t0).astype(float)
    release = obs_s[rep] + rng.random(len(rep)) * c.BURN_SECONDS
    return fires.lon[rep], fires.lat[rep], (e / n)[rep], release, fires.cluster[rep]
