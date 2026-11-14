"""Forecast weather on a regular grid, with fast interpolation to particle positions."""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from sim.constants import HPBL_MIN_M, LEVEL_HEIGHTS_M, M_PER_DEG, MIN_LEVEL_HEIGHTS_M


def _frac(axis: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Lower index and weight of x on an ascending axis (clamped at both ends)."""
    if len(axis) == 1:
        return np.zeros(x.shape, dtype=np.intp), np.zeros(x.shape)
    i = np.clip(np.searchsorted(axis, x, side="right") - 1, 0, len(axis) - 2)
    w = np.clip((x - axis[i]) / (axis[i + 1] - axis[i]), 0.0, 1.0)
    return i, w


@dataclass
class Met:
    """times: seconds since t0 (ascending). lats, lons ascending.
    u, v: [time, level, lat, lon] m/s for the levels 10 m, 925, 850, 700 hPa.
    hpbl: [time, lat, lon] m.
    z_agl: optional [time, 3, lat, lon], true height above ground of the three pressure
    levels; without it the fixed `heights` are used everywhere."""

    times: np.ndarray
    lats: np.ndarray
    lons: np.ndarray
    u: np.ndarray
    v: np.ndarray
    hpbl: np.ndarray
    heights: np.ndarray = field(default_factory=lambda: np.array(LEVEL_HEIGHTS_M))
    t0: np.datetime64 | None = None  # absolute time of second 0
    z_agl: np.ndarray | None = None

    def __post_init__(self):
        self._nk = self.u.shape[1]
        self._yx = len(self.lats) * len(self.lons)
        self._u, self._v, self._h = self.u.ravel(), self.v.ravel(), self.hpbl.ravel()
        self._z = None if self.z_agl is None else self.z_agl.ravel()
        self._div = None  # horizontal divergence, built on first use (backward runs only)

    def _divergence(self) -> np.ndarray:
        """Horizontal wind divergence (1/s) on the grid: how fast air spreads out sideways."""
        if self._div is None:
            cos = np.cos(np.radians(self.lats))[None, None, :, None]
            if len(self.lons) > 1 and len(self.lats) > 1:
                dudx = np.gradient(self.u.astype(float), self.lons, axis=3)
                dvdy = np.gradient(self.v.astype(float) * cos, self.lats, axis=2)
                self._div = ((dudx + dvdy) / (M_PER_DEG * cos)).astype(np.float32).ravel()
            else:
                self._div = np.zeros(self.u.size, dtype=np.float32)
        return self._div

    def inside(self, lon, lat):
        return (
            (lon >= self.lons[0]) & (lon <= self.lons[-1])
            & (lat >= self.lats[0]) & (lat <= self.lats[-1])
        )  # fmt: skip

    def _corners(self, t: float, lon, lat):
        """The 8 (time, lat, lon) corners around each point: (time index, y*X+x, weight)."""
        nx = len(self.lons)
        ti, tw = _frac(self.times, np.array([float(t)]))
        t0, t1 = int(ti[0]), min(int(ti[0]) + 1, len(self.times) - 1)
        yi, yw = _frac(self.lats, lat)
        xi, xw = _frac(self.lons, lon)
        out = []
        for tt, wt in ((t0, 1.0 - float(tw[0])), (t1, float(tw[0]))):
            if wt == 0.0:
                continue
            for dy, wy in ((0, 1 - yw), (1, yw)):
                for dx, wx in ((0, 1 - xw), (1, xw)):
                    out.append((tt, (yi + dy) * nx + xi + dx, wt * wy * wx))
        return out

    def pbl(self, t: float, lon, lat):
        """Boundary-layer height (m) at time t (s) and positions, floored at HPBL_MIN_M."""
        out = 0.0
        for tt, yx, w in self._corners(t, lon, lat):
            out = out + w * self._h[tt * self._yx + yx]
        return np.maximum(out, HPBL_MIN_M)

    def wind(self, t: float, lon, lat, z, with_divergence: bool = False):
        """u, v (m/s) at time t (s), positions and heights z (m above ground).
        With with_divergence, also the horizontal divergence (1/s) at the same points."""
        dfield = self._divergence() if with_divergence else None
        corners = self._corners(t, lon, lat)
        nk, yx_n = self._nk, self._yx
        if self._z is None:
            zi, zw = _frac(self.heights, z)
        else:  # local height of each pressure level, then the bracket around z
            lev = [0.0, 0.0, 0.0]
            for tt, yx, w in corners:
                base = tt * 3 * yx_n + yx
                for k in range(3):
                    lev[k] = lev[k] + w * self._z[base + k * yx_n]
            hts = np.stack([np.full(np.shape(z), self.heights[0]), *lev])
            zi = (z >= hts[1]).astype(np.intp) + (z >= hts[2])
            rows = np.arange(len(zi))
            lower, upper = hts[zi, rows], hts[zi + 1, rows]
            zw = np.clip((z - lower) / np.maximum(upper - lower, 1.0), 0.0, 1.0)
        u = v = d = 0.0
        for tt, yx, w in corners:
            for dz, wz in ((0, 1 - zw), (1, zw)):
                idx = (tt * nk + np.minimum(zi + dz, nk - 1)) * yx_n + yx
                ww = w * wz
                u = u + ww * self._u[idx]
                v = v + ww * self._v[idx]
                if dfield is not None:
                    d = d + ww * dfield[idx]
        return (u, v, d) if with_divergence else (u, v)


def from_netcdf(paths: list[Path], t0: np.datetime64 | None = None) -> Met:
    """Build a Met from GFS subset files written by weather/gfs.py.

    paths: newest cycle LAST. Older cycles only fill the hours before the next cycle starts
    (each hour uses the newest forecast that had been issued for it by then).
    """
    import xarray as xr

    parts, start_next = [], None
    for path in reversed(paths):
        ds = xr.open_dataset(path).load()
        if start_next is not None:
            ds = ds.sel(valid_time=ds.valid_time < start_next)
        if ds.sizes["valid_time"]:
            parts.append(ds)
            start_next = ds.valid_time.values[0]
    ds = xr.concat(parts[::-1], dim="valid_time") if len(parts) > 1 else parts[0]
    ds = ds.sortby("latitude").sortby("longitude").sortby("isobaricInhPa", ascending=False)
    assert list(ds.isobaricInhPa.values) == [925.0, 850.0, 700.0], ds.isobaricInhPa.values
    if t0 is None:
        t0 = np.datetime64(xr.open_dataset(paths[-1]).valid_time.values[0], "s")
    times = (ds.valid_time.values.astype("datetime64[s]") - t0).astype(float)
    order = ("valid_time", "isobaricInhPa", "latitude", "longitude")
    u = np.concatenate([ds.u10.values[:, None], ds.u.transpose(*order).values], axis=1)
    v = np.concatenate([ds.v10.values[:, None], ds.v.transpose(*order).values], axis=1)
    z_agl = None
    if "gh" in ds and "orog" in ds:
        above = ds.gh.transpose(*order).values - ds.orog.values[:, None]
        # A pressure level under the terrain has no real wind (the model fills in a guess).
        # Use the 10 m wind there, so mountains do not inject made-up flow.
        under = above < 10.0
        u[:, 1:] = np.where(under, u[:, :1], u[:, 1:])
        v[:, 1:] = np.where(under, v[:, :1], v[:, 1:])
        z_agl = np.maximum(above, np.array(MIN_LEVEL_HEIGHTS_M)[None, :, None, None])
        z_agl = np.ascontiguousarray(z_agl, dtype=np.float32)
    return Met(
        times=times, lats=ds.latitude.values.astype(float), lons=ds.longitude.values.astype(float),
        u=np.ascontiguousarray(u, dtype=np.float32), v=np.ascontiguousarray(v, dtype=np.float32),
        hpbl=np.ascontiguousarray(ds.hpbl.values, dtype=np.float32), t0=t0, z_agl=z_agl,
    )  # fmt: skip
