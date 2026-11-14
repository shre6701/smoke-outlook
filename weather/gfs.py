"""Latest complete GFS 0.25° cycle, byte-range subset, cropped to the v1 domain.

Fields (SPEC §3): U/V wind at 925/850/700 hPa and 10 m, boundary-layer height, 2 m temperature.
The cropped grid is written to data/gfs/*.nc; Postgres holds one catalogue row per cycle.
"""

import logging
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import requests

from ingest import config

SEARCH = (
    r":(?:UGRD|VGRD):(?:925|850|700) mb:"
    r"|:(?:UGRD|VGRD):10 m above ground:"
    r"|:HPBL:|:TMP:2 m above ground:"
)
N_FIELDS = 10
MAX_FXX = 72
PAD = 1.0  # degrees kept around the domain so edges can be interpolated
RAW_DIR = config.DATA_DIR / "gfs_raw"
OUT_DIR = config.DATA_DIR / "gfs"

logging.getLogger("herbie").setLevel(logging.ERROR)


def _herbie(init: datetime, fxx: int):
    from herbie import Herbie

    return Herbie(
        init.replace(tzinfo=None), model="gfs", product="pgrb2.0p25", fxx=fxx,
        priority=["aws"], save_dir=RAW_DIR, verbose=False,
    )  # fmt: skip


def latest_complete_cycle(now: datetime | None = None) -> datetime | None:
    """Newest init time whose last needed forecast hour is already on AWS."""
    now = now or datetime.now(UTC)
    t = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)
    for _ in range(6):
        H = _herbie(t, MAX_FXX)
        if H.grib and H.idx:
            return t
        t -= timedelta(hours=6)
    return None


def published_at(init: datetime) -> tuple[datetime, str]:
    """When the cycle became complete: S3 Last-Modified of the last forecast hour."""
    try:
        r = requests.head(_herbie(init, MAX_FXX).grib, timeout=30)
        return parsedate_to_datetime(r.headers["Last-Modified"]), "s3_last_modified"
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return datetime.now(UTC), "first_seen"


VARIABLES = ["hpbl", "t2m", "u", "u10", "v", "v10"]


def _tidy(ds):
    """Crop to the padded domain; keep only dimension coords; name HPBL clearly."""
    ds = ds.sel(
        latitude=slice(config.NORTH + PAD, config.SOUTH - PAD),
        longitude=slice(config.WEST - PAD, config.EAST + PAD),
    )
    ds = ds.drop_vars([c for c in ds.coords if c not in ds.dims and c != "valid_time"])
    # eccodes has no short name for GFS boundary-layer height: it arrives as "unknown".
    return ds.rename({v: "hpbl" for v in ds.data_vars if v in ("unknown", "blh")})


def load_step(init: datetime, fxx: int):
    import xarray as xr

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # noisy xarray merge notices inside Herbie
        parts = _herbie(init, fxx).xarray(SEARCH, remove_grib=True)
    parts = parts if isinstance(parts, list) else [parts]
    ds = xr.merge([_tidy(p) for p in parts], compat="override", combine_attrs="drop")
    return ds.expand_dims(valid_time=[ds["valid_time"].values]).load()


def ingest(conn, step_hours: int = 1) -> tuple[str, int, str]:
    import xarray as xr

    init = latest_complete_cycle()
    if init is None:
        raise RuntimeError("no complete GFS cycle found in the last 36 h")
    have = conn.execute("SELECT step_hours FROM gfs_cycle WHERE init_time = %s", (init,)).fetchone()
    if have and have[0] <= step_hours:  # a coarser stored cycle is replaced by a finer one
        return "ok", 0, f"cycle {init:%Y-%m-%d %HZ} already stored ({have[0]}-hourly)"
    pub, basis = published_at(init)
    hours = list(range(0, MAX_FXX + 1, step_hours))
    # Download the byte ranges in parallel; decode one at a time (eccodes is not thread-safe).
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda f: _herbie(init, f).download(SEARCH), hours))
    ds = xr.concat([load_step(init, f) for f in hours], dim="valid_time")
    ds.attrs = {
        "source": "NOAA GFS 0.25 deg, s3://noaa-gfs-bdp-pds",
        "init_time": init.isoformat(),
        "note": "forecast (not reanalysis); cropped subset",
    }
    variables = sorted(ds.data_vars)
    levels = ds.sizes.get("isobaricInhPa", 0)
    if variables != VARIABLES or levels != 3:
        raise RuntimeError(f"GFS subset incomplete: vars={variables} levels={levels}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"gfs_{init:%Y%m%d_%H}.nc"
    ds.to_netcdf(path)
    times = [init + timedelta(hours=h) for h in hours]
    lat0, lat1 = float(ds.latitude.min()), float(ds.latitude.max())
    lon0, lon1 = float(ds.longitude.min()), float(ds.longitude.max())
    conn.execute(
        """
        INSERT INTO gfs_cycle (init_time, observed_from, observed_to, published_at,
            published_basis, step_hours, n_times, variables, path, bbox)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
        ON CONFLICT (init_time) DO UPDATE SET step_hours = EXCLUDED.step_hours,
            n_times = EXCLUDED.n_times, ingested_at = now()
        """,
        (init, times[0], times[-1], pub, basis, step_hours, len(times), variables,
         str(path.relative_to(config.ROOT)), lon0, lat0, lon1, lat1),
    )  # fmt: skip
    conn.commit()
    for f in RAW_DIR.rglob("*"):  # raw GRIB subsets are not needed after cropping
        if f.is_file():
            f.unlink()
    return "ok", len(times), f"cycle {init:%Y-%m-%d %HZ}, {len(times)} times, {variables}"
