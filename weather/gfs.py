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
    r"|:HGT:(?:925|850|700) mb:|:HGT:surface:"  # true height of the wind levels + terrain
)
N_FIELDS = 10
MAX_FXX = 72
SHORT_FXX = 5  # older cycles: only the hours until the next cycle starts (past winds)
PAD = 4.0  # degrees kept around the domain so smoke that leaves can come back
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


def published_at(init: datetime, fxx: int = MAX_FXX) -> tuple[datetime, str]:
    """When the cycle became complete: S3 Last-Modified of the last forecast hour used."""
    try:
        r = requests.head(_herbie(init, fxx).grib, timeout=30)
        return parsedate_to_datetime(r.headers["Last-Modified"]), "s3_last_modified"
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return datetime.now(UTC), "first_seen"


VARIABLES = ["gh", "hpbl", "orog", "t2m", "u", "u10", "v", "v10"]


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


def ingest(conn, step_hours: int = 1, back: int = 0, history_hours: int = 72):
    """Store the latest complete cycle in full, the `back` cycles before it in full
    (ensemble members), and the first hours of every older cycle within `history_hours`
    (real winds for the days just past, each hour from the newest forecast issued by then)."""
    latest = latest_complete_cycle()
    if latest is None:
        raise RuntimeError("no complete GFS cycle found in the last 36 h")
    total, new_full, new_short, failed = 0, [], 0, []
    for i in range(history_hours // 6, -1, -1):
        init = latest - timedelta(hours=6 * i)
        full = i <= back
        try:
            rows = ingest_cycle(conn, init, step_hours, MAX_FXX if full else SHORT_FXX)
        except Exception as e:  # noqa: BLE001 - an old cycle may be missing; keep the others
            conn.rollback()
            if i == 0:
                raise
            failed.append(f"{init:%d %HZ} ({type(e).__name__})")
            continue
        total += rows
        if rows and full:
            new_full.append(f"{init:%d %HZ}")
        new_short += bool(rows and not full)
    note = f"latest {latest:%Y-%m-%d %HZ}; new full cycles: {', '.join(new_full) or 'none'}; "
    note += f"new past-wind cycles: {new_short}"
    if failed:
        note += f"; could not fetch: {', '.join(failed)}"
    return "ok", total, note


def ingest_cycle(conn, init: datetime, step_hours: int, max_fxx: int = MAX_FXX) -> int:
    """Store one cycle up to forecast hour max_fxx. Returns the number of times written
    (0 if an equal or longer copy is already stored)."""
    import xarray as xr

    hours = list(range(0, max_fxx + 1, step_hours))
    have = conn.execute(
        "SELECT 1 FROM gfs_cycle WHERE init_time = %s AND variables = %s AND path LIKE %s "
        "AND step_hours <= %s AND n_times >= %s",
        (init, VARIABLES, f"%pad{PAD:g}%", step_hours, len(hours)),
    ).fetchone()
    if have:
        return 0
    pub, basis = published_at(init, max_fxx)
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
    path = OUT_DIR / f"gfs_{init:%Y%m%d_%H}_pad{PAD:g}_f{max_fxx:03d}.nc"
    tmp = path.with_suffix(".part")
    ds.to_netcdf(tmp)
    tmp.replace(path)  # the catalogue never points at a half-written file
    times = [init + timedelta(hours=h) for h in hours]
    lat0, lat1 = float(ds.latitude.min()), float(ds.latitude.max())
    lon0, lon1 = float(ds.longitude.min()), float(ds.longitude.max())
    conn.execute(
        """
        INSERT INTO gfs_cycle (init_time, observed_from, observed_to, published_at,
            published_basis, step_hours, n_times, variables, path, bbox)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
        ON CONFLICT (init_time) DO UPDATE SET step_hours = EXCLUDED.step_hours,
            n_times = EXCLUDED.n_times, variables = EXCLUDED.variables, bbox = EXCLUDED.bbox,
            path = EXCLUDED.path, observed_to = EXCLUDED.observed_to,
            published_at = EXCLUDED.published_at,
            ingested_at = now()
        """,
        (init, times[0], times[-1], pub, basis, step_hours, len(times), variables,
         str(path.relative_to(config.ROOT)), lon0, lat0, lon1, lat1),
    )  # fmt: skip
    conn.commit()
    # Raw GRIB subsets are not needed after cropping. Only this process's own download folder
    # (RAW_DIR/gfs): hindcast workers keep theirs in sub-folders of the shared RAW_DIR.
    for f in (RAW_DIR / "gfs").rglob("*"):
        if f.is_file():
            f.unlink()
    return len(times)
