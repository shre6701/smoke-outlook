"""Lagrangian particle model (NumPy). Forward from fires, backward from receptors.

One step: move with the wind (plus a random walk), re-mix particles that are inside the
boundary layer over its full depth, remove mass by dry deposition, drop particles that
leave the weather grid. Particles above the boundary layer stay at their height until the
layer grows up to them again.
"""

from dataclasses import dataclass

import numpy as np

from sim.constants import DRY_DEPOSITION_M_S, K_HORIZONTAL_M2_S, M_PER_DEG
from sim.met import Met


@dataclass
class Params:
    dt: float = 600.0  # seconds
    k_h: float = K_HORIZONTAL_M2_S
    v_dep: float = DRY_DEPOSITION_M_S
    seed: int = 0


@dataclass
class Particles:
    lon: np.ndarray
    lat: np.ndarray
    z: np.ndarray  # m above ground
    mass: np.ndarray  # kg PM2.5 (forward) or 1.0 (backward)
    release: np.ndarray  # s since met.t0
    group: np.ndarray  # cluster index (forward) or receptor index (backward)
    alive: np.ndarray

    @property
    def n(self) -> int:
        return len(self.lon)


def make_particles(lon, lat, mass, release, group) -> Particles:
    lon = np.asarray(lon, dtype=float)
    return Particles(
        lon=lon.copy(), lat=np.asarray(lat, dtype=float).copy(), z=np.zeros(len(lon)),
        mass=np.asarray(mass, dtype=float).copy(), release=np.asarray(release, dtype=float),
        group=np.asarray(group, dtype=np.intp), alive=np.ones(len(lon), dtype=bool),
    )  # fmt: skip


def step(met: Met, p: Particles, idx: np.ndarray, t: float, dt: float, prm: Params, rng) -> None:
    """Advance particles idx from time t to t + dt (dt < 0 runs backward). In place."""
    if len(idx) == 0:
        return
    lon, lat, z = p.lon[idx], p.lat[idx], p.z[idx]
    u, v = met.wind(t, lon, lat, z)
    dx, dy = u * dt, v * dt
    if prm.k_h > 0:
        sigma = np.sqrt(2 * prm.k_h * abs(dt))
        dx = dx + rng.normal(0, sigma, len(idx))
        dy = dy + rng.normal(0, sigma, len(idx))
    lat_new = lat + dy / M_PER_DEG
    lon_new = lon + dx / (M_PER_DEG * np.cos(np.radians(lat)))
    ok = met.inside(lon_new, lat_new)
    h = met.pbl(t + dt, np.where(ok, lon_new, lon), np.where(ok, lat_new, lat))
    mixed = z <= h
    z = np.where(mixed, rng.random(len(idx)) * h, z)
    if prm.v_dep > 0:
        p.mass[idx] = np.where(mixed, p.mass[idx] * np.exp(-prm.v_dep * abs(dt) / h), p.mass[idx])
    p.lon[idx], p.lat[idx], p.z[idx] = lon_new, lat_new, z
    p.alive[idx] = ok


@dataclass
class Grid:
    west: float
    south: float
    east: float
    north: float
    res: float = 0.1

    @property
    def shape(self) -> tuple[int, int]:
        return (
            round((self.north - self.south) / self.res),
            round((self.east - self.west) / self.res),
        )

    @property
    def lats(self):
        return self.south + self.res * (np.arange(self.shape[0]) + 0.5)

    @property
    def lons(self):
        return self.west + self.res * (np.arange(self.shape[1]) + 0.5)

    def area_m2(self):
        """Cell area per latitude row."""
        return (self.res * M_PER_DEG) ** 2 * np.cos(np.radians(self.lats))

    def bin(self, lon, lat, weight) -> np.ndarray:
        """Sum weight per cell, divided by cell area."""
        ny, nx = self.shape
        iy = np.floor((lat - self.south) / self.res).astype(np.intp)
        ix = np.floor((lon - self.west) / self.res).astype(np.intp)
        ok = (iy >= 0) & (iy < ny) & (ix >= 0) & (ix < nx)
        flat = np.bincount(iy[ok] * nx + ix[ok], weights=weight[ok], minlength=ny * nx)
        return flat.reshape(ny, nx) / self.area_m2()[:, None]


@dataclass
class Receptor:
    name: str
    lat: float
    lon: float
    half_box: float = 0.25  # degrees: city-scale average (about 50 km square)

    def mask(self, lon, lat):
        return (np.abs(lon - self.lon) <= self.half_box) & (np.abs(lat - self.lat) <= self.half_box)

    def area_m2(self):
        side = 2 * self.half_box * M_PER_DEG
        return side * side * np.cos(np.radians(self.lat))


def run_forward(met: Met, p: Particles, grid: Grid, receptors: list[Receptor], n_groups: int,
                t_end: float, out_every: float = 3600.0, prm: Params | None = None):  # fmt: skip
    """Run from the first release to t_end.

    Returns (out_times [s], conc [time, lat, lon] µg/m³ near the ground,
             contrib [time, receptor, group] µg/m³, stats dict).
    Ground-level concentration = mass of particles inside the boundary layer, spread evenly
    over its depth.
    """
    prm = prm or Params()
    rng = np.random.default_rng(prm.seed)
    t = float(np.floor(p.release.min() / prm.dt) * prm.dt)
    out_times = np.arange(np.ceil(t / out_every) * out_every, t_end + 1, out_every)
    conc = np.zeros((len(out_times), *grid.shape), dtype=np.float32)
    contrib = np.zeros((len(out_times), len(receptors), n_groups), dtype=np.float32)
    started = np.zeros(p.n, dtype=bool)
    k = 0
    while True:
        new = (~started) & (p.release <= t)
        if new.any():  # fresh smoke starts well mixed in the boundary layer
            p.z[new] = rng.random(int(new.sum())) * met.pbl(t, p.lon[new], p.lat[new])
            started |= new
        if k < len(out_times) and t >= out_times[k] - 1e-6:
            idx = np.flatnonzero(started & p.alive)
            lon, lat = p.lon[idx], p.lat[idx]
            h = met.pbl(t, lon, lat)
            w = np.where(p.z[idx] <= h, p.mass[idx] * 1e9 / h, 0.0)  # µg per m of depth
            conc[k] = grid.bin(lon, lat, w)
            for r, rec in enumerate(receptors):
                m = rec.mask(lon, lat)
                contrib[k, r] = (
                    np.bincount(p.group[idx][m], weights=w[m], minlength=n_groups) / rec.area_m2()
                )
            k += 1
        if t >= t_end - 1e-6:
            break
        step(met, p, np.flatnonzero(started & p.alive), t, prm.dt, prm, rng)
        t += prm.dt
    stats = {
        "particles": p.n,
        "left_grid_frac": float(1 - p.alive.mean()),
        "mass_left_frac": float(p.mass[p.alive].sum() / max(p.mass.sum(), 1e-30)),
    }
    return out_times, conc, contrib, stats


def run_backward(met: Met, receptors: list[Receptor], grid: Grid, arrivals: np.ndarray,
                 t_stop: float, n_per_arrival: int = 300, prm: Params | None = None):  # fmt: skip
    """Footprint per receptor: where the air arriving at the receptor spent time near the ground.

    Returns [receptor, lat, lon] in s/m³: multiply a cell's emission rate (kg/s) by its value
    to get that cell's mean contribution (kg/m³) at the receptor, averaged over the arrivals.
    Deposition is ignored on the way back.
    """
    prm = prm or Params()
    prm = Params(dt=prm.dt, k_h=prm.k_h, v_dep=0.0, seed=prm.seed + 1)
    rng = np.random.default_rng(prm.seed)
    n = len(receptors) * len(arrivals) * n_per_arrival
    rec_i = np.repeat(np.arange(len(receptors)), len(arrivals) * n_per_arrival)
    hb = np.array([r.half_box for r in receptors])[rec_i]
    p = make_particles(
        lon=np.array([r.lon for r in receptors])[rec_i] + rng.uniform(-1, 1, n) * hb,
        lat=np.array([r.lat for r in receptors])[rec_i] + rng.uniform(-1, 1, n) * hb,
        mass=np.ones(n),
        release=np.tile(np.repeat(arrivals, n_per_arrival), len(receptors)),
        group=rec_i,
    )
    foot = np.zeros((len(receptors), *grid.shape))
    started = np.zeros(n, dtype=bool)
    t = float(np.ceil(arrivals.max() / prm.dt) * prm.dt)
    while t > t_stop + 1e-6:
        new = (~started) & (p.release >= t)
        if new.any():
            p.z[new] = rng.random(int(new.sum())) * met.pbl(t, p.lon[new], p.lat[new])
            started |= new
        idx = np.flatnonzero(started & p.alive)
        step(met, p, idx, t, -prm.dt, prm, rng)
        t -= prm.dt
        idx = idx[p.alive[idx]]
        h = met.pbl(t, p.lon[idx], p.lat[idx])
        w = np.where(p.z[idx] <= h, prm.dt / h, 0.0)
        for r in range(len(receptors)):
            m = p.group[idx] == r
            foot[r] += grid.bin(p.lon[idx][m], p.lat[idx][m], w[m])
    return (foot / (len(arrivals) * n_per_arrival)).astype(np.float32)
