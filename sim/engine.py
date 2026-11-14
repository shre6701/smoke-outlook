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
    mass2: np.ndarray | None = None  # same smoke under a second emission method

    @property
    def n(self) -> int:
        return len(self.lon)


def make_particles(lon, lat, mass, release, group, mass2=None) -> Particles:
    lon = np.asarray(lon, dtype=float)
    return Particles(
        mass2=None if mass2 is None else np.asarray(mass2, dtype=float).copy(),
        lon=lon.copy(), lat=np.asarray(lat, dtype=float).copy(), z=np.zeros(len(lon)),
        mass=np.asarray(mass, dtype=float).copy(), release=np.asarray(release, dtype=float),
        group=np.asarray(group, dtype=np.intp), alive=np.ones(len(lon), dtype=bool),
    )  # fmt: skip


def step(met: Met, p: Particles, idx: np.ndarray, t: float, dt: float, prm: Params, rng) -> None:
    """Advance particles idx from time t to t + dt (dt < 0 runs backward). In place."""
    if len(idx) == 0:
        return
    lon, lat, z = p.lon[idx], p.lat[idx], p.z[idx]
    # The model has no vertical wind, so sideways spreading or piling-up of air must be
    # balanced through the top of the boundary layer:
    #  - spreading (divergence > 0): clean air is mixed in from above -> smoke is diluted.
    #    Forward this happens by itself (particles move apart); backward it is a weight.
    #  - piling-up (divergence < 0): the surplus air, with its smoke, leaves through the top.
    #    Forward the surplus mass is removed; backward nothing is needed.
    # With both rules the forward and backward runs describe the same physics.
    u, v, div = met.wind(t, lon, lat, z, with_divergence=True)
    change = (
        np.exp(-np.maximum(div, 0.0) * abs(dt)) if dt < 0 else np.exp(np.minimum(div, 0.0) * dt)
    )
    p.mass[idx] *= change
    if p.mass2 is not None:
        p.mass2[idx] *= change
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
        keep = np.where(mixed, np.exp(-prm.v_dep * abs(dt) / h), 1.0)
        p.mass[idx] *= keep
        if p.mass2 is not None:
            p.mass2[idx] *= keep
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
                t_end: float, out_every: float = 3600.0, prm: Params | None = None,
                t_out_start: float | None = None, n_observed: int | None = None):  # fmt: skip
    """Run from the first release to t_end.

    Returns (out_times [s], conc [time, lat, lon] µg/m³ near the ground,
             contrib [time, receptor, group] µg/m³, stats dict).
    Ground-level concentration = mass of particles inside the boundary layer, spread evenly
    over its depth.
    """
    prm = prm or Params()
    rng = np.random.default_rng(prm.seed)
    t = float(np.floor(p.release.min() / prm.dt) * prm.dt)
    first_out = np.ceil(t / out_every) * out_every
    if t_out_start is not None:  # smoke is followed from the first fire, reported from here
        first_out = max(first_out, np.ceil(t_out_start / out_every) * out_every)
    out_times = np.arange(first_out, t_end + 1, out_every)
    conc = np.zeros((len(out_times), *grid.shape), dtype=np.float32)
    contrib = np.zeros((len(out_times), len(receptors), n_groups), dtype=np.float32)
    contrib2 = np.zeros_like(contrib) if p.mass2 is not None else None
    conc_all = np.zeros_like(conc) if n_observed is not None else None
    started = np.zeros(p.n, dtype=bool)
    mass_start = float(p.mass.sum())
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
            if n_observed is None:
                conc[k] = grid.bin(lon, lat, w)
            else:  # groups from n_observed up are what-if fires: kept out of the main field
                seen = p.group[idx] < n_observed
                conc[k] = grid.bin(lon[seen], lat[seen], w[seen])
                conc_all[k] = grid.bin(lon, lat, w)
            w2 = None
            if contrib2 is not None:
                w2 = np.where(p.z[idx] <= h, p.mass2[idx] * 1e9 / h, 0.0)
            for r, rec in enumerate(receptors):
                m = rec.mask(lon, lat)
                g = p.group[idx][m]
                contrib[k, r] = np.bincount(g, weights=w[m], minlength=n_groups) / rec.area_m2()
                if w2 is not None:
                    contrib2[k, r] = (
                        np.bincount(g, weights=w2[m], minlength=n_groups) / rec.area_m2()
                    )
            k += 1
        if t >= t_end - 1e-6:
            break
        step(met, p, np.flatnonzero(started & p.alive), t, prm.dt, prm, rng)
        t += prm.dt
    stats = {
        "particles": p.n,
        "left_grid_frac": float(1 - p.alive.mean()),
        # still in the air inside the weather grid (not deposited, vented or blown out)
        "mass_left_frac": float(p.mass[p.alive].sum() / max(mass_start, 1e-30)),
        "contrib2": contrib2,
        "conc_all": conc_all,
    }
    return out_times, conc, contrib, stats


@dataclass
class Sources:
    """Emission events for the receptor-oriented (backward) calculation.
    rate: [n_methods, n_events] kg/s, emitted from `start` for `duration` seconds."""

    lon: np.ndarray
    lat: np.ndarray
    start: np.ndarray  # s since met.t0
    group: np.ndarray  # cluster index
    rate: np.ndarray
    duration: float


def run_backward(met: Met, receptors: list[Receptor], grid: Grid, arrivals: np.ndarray,
                 t_stop: float, n_per_arrival: int = 300, prm: Params | None = None,
                 sources: Sources | None = None, n_groups: int = 0,
                 max_age: float | None = None):  # fmt: skip
    """Follow the air arriving at each receptor backward in time.

    Returns the footprint [receptor, lat, lon] in s/m³: multiply a cell's emission rate (kg/s)
    by its value to get that cell's mean contribution (kg/m³) at the receptor, averaged over
    the arrivals.

    With `sources`, also returns contrib [method, arrival, receptor, group] in µg/m³: every
    time a particle is inside the boundary layer over a cell and time slot where a source is
    emitting, that source's share is added. All particles start at a receptor, so this is far
    less noisy for receptors than counting forward particles that happen to arrive there.
    """
    prm = prm or Params()
    prm = Params(dt=prm.dt, k_h=prm.k_h, v_dep=prm.v_dep, seed=prm.seed + 1)
    rng = np.random.default_rng(prm.seed)
    n_rec, n_arr, dt = len(receptors), len(arrivals), prm.dt
    n = n_rec * n_arr * n_per_arrival
    rec_i = np.repeat(np.arange(n_rec), n_arr * n_per_arrival)
    arr_i = np.tile(np.repeat(np.arange(n_arr), n_per_arrival), n_rec)
    hb = np.array([r.half_box for r in receptors])[rec_i]
    p = make_particles(
        lon=np.array([r.lon for r in receptors])[rec_i] + rng.uniform(-1, 1, n) * hb,
        lat=np.array([r.lat for r in receptors])[rec_i] + rng.uniform(-1, 1, n) * hb,
        mass=np.ones(n),  # fraction of the smoke that survives deposition on the way
        release=np.asarray(arrivals, dtype=float)[arr_i],
        group=rec_i,
    )
    ny, nx = grid.shape
    ncell, area = ny * nx, grid.area_m2()
    foot = np.zeros((n_rec, ny, nx))
    contrib = None
    if sources is not None:
        n_slot = max(1, round(sources.duration / dt))
        iy = np.floor((sources.lat - grid.south) / grid.res).astype(np.intp)
        ix = np.floor((sources.lon - grid.west) / grid.res).astype(np.intp)
        ok = (iy >= 0) & (iy < ny) & (ix >= 0) & (ix < nx)
        slot0 = np.floor(sources.start / dt).astype(np.int64)
        slot_min = int(slot0.min()) if len(slot0) else 0
        ev = np.repeat(np.flatnonzero(ok), n_slot)
        slots = (slot0[ok, None] - slot_min + np.arange(n_slot)).ravel()
        key = (slots * ncell + iy[ev] * nx + ix[ev]) * n_groups + sources.group[ev]
        ukey, inv = np.unique(key, return_inverse=True)
        rate = np.stack(
            [np.bincount(inv, weights=r[ev], minlength=len(ukey)) for r in sources.rate]
        )
        ucell, ugrp = ukey // n_groups, ukey % n_groups
        size = n_arr * n_rec * n_groups
        contrib = np.zeros((len(sources.rate), size))
    started = np.zeros(n, dtype=bool)
    t = float(np.ceil(np.max(arrivals) / dt) * dt)
    while t > t_stop + 1e-6:
        new = (~started) & (p.release >= t)
        if new.any():
            p.z[new] = rng.random(int(new.sum())) * met.pbl(t, p.lon[new], p.lat[new])
            started |= new
        if max_age is not None:  # air that old has lost its smoke: stop following it
            p.alive &= ~(started & (p.release - t > max_age))
        idx = np.flatnonzero(started & p.alive)
        step(met, p, idx, t, -dt, prm, rng)
        t -= dt
        idx = idx[p.alive[idx]]
        lon, lat = p.lon[idx], p.lat[idx]
        h = met.pbl(t, lon, lat)
        cy = np.floor((lat - grid.south) / grid.res).astype(np.intp)
        cx = np.floor((lon - grid.west) / grid.res).astype(np.intp)
        use = (p.z[idx] <= h) & (cy >= 0) & (cy < ny) & (cx >= 0) & (cx < nx)
        idx, h, cy, cx = idx[use], h[use], cy[use], cx[use]
        w = p.mass[idx] * dt / (h * area[cy])  # s/m³ per particle
        cell = cy * nx + cx
        foot += np.bincount(
            p.group[idx] * ncell + cell, weights=w, minlength=n_rec * ncell
        ).reshape(n_rec, ny, nx)
        if contrib is not None and len(ukey):
            pk = (round(t / dt) - slot_min) * ncell + cell
            lo = np.searchsorted(ucell, pk, side="left")
            cnt = np.searchsorted(ucell, pk, side="right") - lo
            j = 0
            sel = np.flatnonzero(cnt > 0)
            while len(sel):
                e = lo[sel] + j
                flat = (arr_i[idx[sel]] * n_rec + p.group[idx[sel]]) * n_groups + ugrp[e]
                for s in range(len(contrib)):
                    contrib[s] += np.bincount(flat, weights=w[sel] * rate[s, e], minlength=size)
                j += 1
                sel = sel[cnt[sel] > j]
    foot = (foot / (n_arr * n_per_arrival)).astype(np.float32)
    if contrib is None:
        return foot
    contrib = contrib.reshape(len(contrib), n_arr, n_rec, n_groups) * 1e9 / n_per_arrival
    return foot, contrib.astype(np.float32)
