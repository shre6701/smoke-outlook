"""Forecast weather on a regular grid, with fast interpolation to particle positions."""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from sim.constants import HPBL_MIN_M, LEVEL_HEIGHTS_M


def _frac(axis: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Lower index and weight of x on a regular or irregular ascending axis (clamped)."""
    if len(axis) == 1:
        return np.zeros(x.shape, dtype=np.intp), np.zeros(x.shape)
    i = np.clip(np.searchsorted(axis, x, side="right") - 1, 0, len(axis) - 2)
    w = np.clip((x - axis[i]) / (axis[i + 1] - axis[i]), 0.0, 1.0)
    return i, w


@dataclass
class Met:
    """times: seconds since t0 (ascending). lats, lons ascending. heights: m above ground.
    u, v: [time, level, lat, lon] m/s. hpbl: [time, lat, lon] m."""

    times: np.ndarray
    lats: np.ndarray
    lons: np.ndarray
    u: np.ndarray
    v: np.ndarray
    hpbl: np.ndarray
    heights: np.ndarray = field(default_factory=lambda: np.array(LEVEL_HEIGHTS_M))
    t0: np.datetime64 | None = None  # absolute time of second 0

    def inside(self, lon, lat):
        return (
            (lon >= self.lons[0]) & (lon <= self.lons[-1])
            & (lat >= self.lats[0]) & (lat <= self.lats[-1])
        )  # fmt: skip

    def _hweights(self, t, lat, lon):
        ti, tw = _frac(self.times, np.full(lon.shape, t, dtype=float))
        yi, yw = _frac(self.lats, lat)
        xi, xw = _frac(self.lons, lon)
        return ti, tw, yi, yw, xi, xw

    def pbl(self, t: float, lon, lat):
        """Boundary-layer height (m) at time t (s) and positions, floored at HPBL_MIN_M."""
        ti, tw, yi, yw, xi, xw = self._hweights(t, lat, lon)
        out = 0.0
        for dt, wt in ((0, 1 - tw), (1, tw)):
            tt = np.minimum(ti + dt, len(self.times) - 1)
            for dy, wy in ((0, 1 - yw), (1, yw)):
                for dx, wx in ((0, 1 - xw), (1, xw)):
                    out = out + wt * wy * wx * self.hpbl[tt, yi + dy, xi + dx]
        return np.maximum(out, HPBL_MIN_M)

    def wind(self, t: float, lon, lat, z):
        """u, v (m/s) at time t (s), positions and heights z (m above ground)."""
        ti, tw, yi, yw, xi, xw = self._hweights(t, lat, lon)
        zi, zw = _frac(self.heights, z)
        u = v = 0.0
        for dt, wt in ((0, 1 - tw), (1, tw)):
            tt = np.minimum(ti + dt, len(self.times) - 1)
            for dz, wz in ((0, 1 - zw), (1, zw)):
                zz = np.minimum(zi + dz, len(self.heights) - 1)
                for dy, wy in ((0, 1 - yw), (1, yw)):
                    for dx, wx in ((0, 1 - xw), (1, xw)):
                        w = wt * wz * wy * wx
                        u = u + w * self.u[tt, zz, yi + dy, xi + dx]
                        v = v + w * self.v[tt, zz, yi + dy, xi + dx]
        return u, v


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
    return Met(
        times=times, lats=ds.latitude.values.astype(float), lons=ds.longitude.values.astype(float),
        u=u.astype(np.float32), v=v.astype(np.float32), hpbl=ds.hpbl.values.astype(np.float32),
        t0=t0,
    )  # fmt: skip
