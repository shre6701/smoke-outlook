"""Database tests on a throwaway database: schema, ingest behaviour, status flags, sim run."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from ingest import db, firms, stations, status
from ingest import run as ingest_run

pytestmark = pytest.mark.db

HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
    "confidence,version,bright_ti5,frp,daynight\n"
)


def firms_csv(when: datetime, points) -> str:
    rows = [
        f"{lat},{lon},330.0,0.4,0.4,{when:%Y-%m-%d},{when:%H%M},N20,VIIRS,n,2.0NRT,300.0,{frp},D"
        for lat, lon, frp in points
    ]
    return HEADER + "\n".join(rows) + "\n"


class Reply:
    def __init__(self, text="", payload=None):
        self.text, self._payload = text, payload

    def json(self):
        return self._payload


def test_schema_can_be_applied_twice(testdb):
    conn = testdb()
    db.ensure_schema(conn)
    db.ensure_schema(conn)
    n = conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
        "('fire_detection', 'station', 'station_reading', 'cams_forecast', 'gfs_cycle')"
    ).fetchone()[0]
    assert n == 5


def test_schema_upgrades_a_table_made_before_sensor_ids(testdb):
    conn = testdb()
    conn.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    conn.execute(
        """
        CREATE TABLE station (source text, station_id text, name text, country text NOT NULL,
            provider text, is_reference boolean NOT NULL, geom geometry(Point, 4326) NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (source, station_id));
        CREATE TABLE station_reading (source text NOT NULL, station_id text NOT NULL,
            parameter text NOT NULL, unit text NOT NULL, value real NOT NULL,
            observed_at timestamptz NOT NULL, published_at timestamptz NOT NULL,
            published_basis text NOT NULL, ingested_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source, station_id, parameter, observed_at));
        INSERT INTO station VALUES ('openaq', '1', 'x', 'IN', 'p', true,
            ST_SetSRID(ST_MakePoint(77, 28), 4326));
        INSERT INTO station_reading
            VALUES ('openaq', '1', 'pm25', 'u', 5, now(), now(), 'first_seen');
        """
    )
    db.ensure_schema(conn)
    db.ensure_schema(conn)
    assert conn.execute("SELECT sensor_id, value FROM station_reading").fetchall() == [("", 5.0)]


def test_firms_ingest_is_idempotent_and_keeps_first_publish_time(testdb, monkeypatch):
    conn = testdb()
    db.ensure_schema(conn)
    when = datetime.now(UTC) - timedelta(hours=3)
    csv = firms_csv(when, [(30.5, 75.5, 4.0), (31.0, 74.0, 9.0), (10.0, 75.0, 5.0)])
    monkeypatch.setenv("FIRMS_MAP_KEY", "k")
    monkeypatch.setattr(firms, "get", lambda url, **kw: Reply(csv))
    monkeypatch.setattr(firms.config, "FIRMS_SOURCES", ["VIIRS_NOAA20_NRT"])
    assert firms.ingest(conn)[:2] == ("ok", 2)  # the third point is outside the domain
    first = conn.execute("SELECT min(published_at) FROM fire_detection").fetchone()[0]
    assert firms.ingest(conn)[:2] == ("ok", 0)
    n, pub = conn.execute("SELECT count(*), max(published_at) FROM fire_detection").fetchone()
    assert n == 2 and pub == first
    x, y = conn.execute("SELECT ST_X(geom), ST_Y(geom) FROM fire_detection ORDER BY 1").fetchone()
    assert (x, y) == (74.0, 31.0)  # lon/lat not swapped


def test_firms_error_page_is_a_failure_not_empty_data(testdb, monkeypatch):
    conn = testdb()
    db.ensure_schema(conn)
    monkeypatch.setenv("FIRMS_MAP_KEY", "k")
    monkeypatch.setattr(firms, "get", lambda url, **kw: Reply("Invalid MAP_KEY."))
    with pytest.raises(RuntimeError):
        firms.ingest(conn)


def test_openaq_ingest_labels_stations_and_dedupes(testdb, monkeypatch):
    conn = testdb()
    db.ensure_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0)
    locs = [
        {"id": 1, "name": "ref", "isMonitor": True, "country": {"code": "IN"},
         "provider": {"name": "CPCB"}, "coordinates": {"latitude": 28.6, "longitude": 77.2}},
        {"id": 2, "name": "lowcost", "isMonitor": False, "country": {"code": "IN"},
         "provider": {"name": "AirGradient"}, "coordinates": {"latitude": 31.6, "longitude": 74.9}},
        {"id": 3, "name": "abroad", "isMonitor": True, "country": {"code": "PK"},
         "provider": {"name": "x"}, "coordinates": {"latitude": 31.5, "longitude": 74.3}},
    ]  # fmt: skip

    def item(loc, sensor, age_h, value):
        utc = (now - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"datetime": {"utc": utc}, "value": value, "locationsId": loc, "sensorsId": sensor}

    latest = [
        item(1, 11, 1, 150.0), item(1, 11, 1, 150.0),  # the API repeats entries across pages
        item(1, 12, 1, 140.0),  # second sensor at the same station
        item(2, 21, 1, 90.0), item(2, 21, 24 * 30, 80.0),  # one fresh, one dead
        item(3, 31, 1, 70.0),  # station abroad: not ingested
        item(99, 9, 1, 10.0),  # station outside the domain list
    ]  # fmt: skip

    def fake_get(url, **kw):
        return Reply(payload={"results": locs if url.endswith("/locations") else latest})

    monkeypatch.setenv("OPENAQ_API_KEY", "k")
    monkeypatch.setattr(stations, "get", fake_get)
    assert stations.ingest_openaq(conn)[:2] == ("ok", 3)
    assert stations.ingest_openaq(conn)[:2] == ("ok", 0)
    rows = conn.execute(
        "SELECT s.country, s.is_reference, count(*) FROM station_reading r JOIN station s "
        "USING (source, station_id) GROUP BY 1, 2 ORDER BY 1"
    ).fetchall()
    assert rows == [("IN", False, 1), ("IN", True, 2)]


def test_one_failing_source_does_not_stop_the_others_and_hides_keys(testdb, monkeypatch, capsys):
    secret = "SECRETKEY123"

    def boom(conn):
        from ingest.http import get

        get(f"http://127.0.0.1:9/api/{secret}/x", retries=1, timeout=2)

    def fine(conn):
        return "ok", 7, "seven rows"

    failed = ingest_run.run_once(["bad", "good"], {"bad": boom, "good": fine})
    assert failed == 1
    rows = testdb().execute("SELECT source, status, rows_new, note FROM ingest_run ORDER BY id")
    rows = rows.fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("bad", "failed", 0), ("good", "ok", 7)]
    assert secret not in rows[0][3] and secret not in capsys.readouterr().out


def test_status_flags_stale_and_empty_and_sets_exit_code(testdb, monkeypatch, capsys):
    conn = testdb()
    db.ensure_schema(conn)
    monkeypatch.delenv("DATAGOV_API_KEY", raising=False)
    assert status.main() == 1  # everything empty
    out = capsys.readouterr().out
    assert out.count("EMPTY") == 4 and "SKIPPED (no DATAGOV_API_KEY)" in out

    now = datetime.now(UTC)
    pt = "ST_SetSRID(ST_MakePoint(77, 28), 4326)"
    conn.execute(
        f"INSERT INTO fire_detection (source, observed_at, published_at, published_basis, "
        f"latitude, longitude, geom) VALUES ('s', %s, %s, 'first_seen', 28, 77, {pt})",
        (now - timedelta(hours=30), now),
    )
    for sid, country, age in (("1", "IN", 1),):
        conn.execute(f"INSERT INTO station VALUES ('openaq', %s, 'n', %s, 'p', true, {pt})",
                     (sid, country))  # fmt: skip
        conn.execute(
            "INSERT INTO station_reading (source, station_id, sensor_id, parameter, unit, value, "
            "observed_at, published_at, published_basis) "
            "VALUES ('openaq', %s, 's', 'pm25', 'u', 50, %s, %s, 'first_seen')",
            (sid, now - timedelta(hours=age), now),
        )
    conn.execute(
        f"INSERT INTO cams_forecast VALUES ('Delhi', %s, %s, 'first_seen', now(), 80, {pt})",
        (now, now - timedelta(hours=1)),
    )
    conn.execute(
        "INSERT INTO gfs_cycle VALUES (%s, %s, %s, %s, 'x', now(), 1, 73, '{u}', 'p', "
        "ST_MakeEnvelope(69, 25, 81, 35, 4326))",
        (now - timedelta(hours=6), now, now, now),
    )
    conn.commit()
    assert status.main() == 1
    lines = capsys.readouterr().out.splitlines()

    def line(label):
        return next(ln for ln in lines if ln.startswith(label))

    assert "STALE" in line("FIRMS fires")  # newest fire is 30 h old, limit 24 h
    for label in ("GFS forecast", "India reference stations", "CAMS"):
        assert " ok " in line(label)
    conn.execute("UPDATE fire_detection SET observed_at = now() - interval '2 hours'")
    conn.commit()
    assert status.main() == 0


# ---------- simulation end to end ----------


def write_cycle(path, init: datetime, u: float, hours: int = 72):
    import xarray as xr

    t = np.datetime64(init.replace(tzinfo=None), "s") + np.arange(hours + 1) * np.timedelta64(
        1, "h"
    )
    lat, lon = np.arange(35.0, 24.99, -0.25), np.arange(69.0, 81.01, 0.25)
    lev = np.array([925.0, 850.0, 700.0])
    s3, s2 = (len(t), 3, len(lat), len(lon)), (len(t), len(lat), len(lon))
    d3 = ("valid_time", "isobaricInhPa", "latitude", "longitude")
    d2 = ("valid_time", "latitude", "longitude")
    xr.Dataset(
        {"u": (d3, np.full(s3, u)), "v": (d3, np.zeros(s3)), "u10": (d2, np.full(s2, u)),
         "v10": (d2, np.zeros(s2)), "hpbl": (d2, np.full(s2, 800.0)),
         "t2m": (d2, np.full(s2, 290.0))},
        coords={"valid_time": t, "latitude": lat, "longitude": lon, "isobaricInhPa": lev},
    ).to_netcdf(path)  # fmt: skip


def add_cycle(conn, path, init, published):
    conn.execute(
        "INSERT INTO gfs_cycle VALUES (%s, %s, %s, %s, 'test', now(), 1, 73, '{u}', %s, "
        "ST_MakeEnvelope(69, 25, 81, 35, 4326))",
        (init, init, init + timedelta(hours=72), published, str(path)),
    )


def add_fire(conn, lat, lon, frp, observed, published):
    conn.execute(
        "INSERT INTO fire_detection (source, observed_at, published_at, published_basis, "
        "latitude, longitude, geom, frp_mw) VALUES ('t', %s, %s, 'first_seen', %s, %s, "
        "ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)",
        (observed, published, lat, lon, lon, lat, frp),
    )


@pytest.mark.parametrize("session_tz", ["UTC", "Asia/Kolkata"])
def test_sim_run_end_to_end(testdb, tmp_path, session_tz):
    from sim import run as sim_run

    conn = testdb(options=f"-c timezone={session_tz}")
    sim_run.ensure_schema(conn)
    # The latest 20:00 UTC (01:30 IST): fires seen 2 h earlier are night fires, which burn
    # for one hour from the sighting. Day fires are covered in tests/test_sim.py.
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    now -= timedelta(hours=(now.hour - 20) % 24)
    init = (now - timedelta(hours=5)).replace(minute=0, second=0)
    prev = init - timedelta(hours=6)
    # Westerly wind 5 m/s in the newest cycle; the older cycle is calm.
    write_cycle(tmp_path / "new.nc", init, u=5.0)
    write_cycle(tmp_path / "old.nc", prev, u=0.0)
    add_cycle(conn, tmp_path / "new.nc", init, init + timedelta(hours=4))
    add_cycle(conn, tmp_path / "old.nc", prev, prev + timedelta(hours=4))
    add_cycle(conn, tmp_path / "future.nc", init + timedelta(hours=6), now + timedelta(hours=3))
    # Fire A: 100 km west of Delhi (upwind). Fire B: east of Delhi (downwind, never arrives).
    add_fire(conn, 28.61, 76.2, 50.0, now - timedelta(hours=2), now - timedelta(hours=1))
    add_fire(conn, 28.61, 76.21, 30.0, now - timedelta(hours=2), now - timedelta(hours=1))
    add_fire(conn, 28.61, 78.5, 60.0, now - timedelta(hours=2), now - timedelta(hours=1))
    # Not allowed in this run: seen by us only after `now`, or older than 120 h.
    add_fire(conn, 28.61, 76.5, 999.0, now - timedelta(hours=1), now + timedelta(hours=2))
    add_fire(conn, 28.61, 76.5, 999.0, now - timedelta(hours=130), now - timedelta(hours=129))
    conn.commit()

    r = sim_run.run(conn, now, n_particles=4000, out_dir=tmp_path / "sim", n_per_arrival=200)

    assert r["init"] == init and r["members"] == [f"gfs_{init:%Y%m%d_%H}", f"gfs_{prev:%Y%m%d_%H}"]
    assert r["n_detections"] == 3 and r["n_clusters"] == 2  # 999 MW fires were excluded
    row = conn.execute("SELECT n_detections, runtime_s, label FROM sim_run").fetchone()
    assert row[0] == 3 and row[1] > 0 and "simulated" in row[2]
    assert 0.6 < r["check"]["Delhi"] < 1.6  # forward and backward runs agree

    # Only the upwind cluster (50 + 30 MW) reaches Delhi; the calm member brings almost nothing
    # (only the random-walk tail), the windy member a lot.
    by = conn.execute(
        "SELECT c.frp_mw, k.member, max(k.pm25_ugm3) FROM sim_contribution k "
        "JOIN sim_cluster c USING (run_id, cluster_id) WHERE k.receptor = 'Delhi' GROUP BY 1, 2"
    ).fetchall()
    peak = {member: value for frp, member, value in by}
    assert {frp for frp, _, _ in by} == {80.0}
    assert peak[f"gfs_{init:%Y%m%d_%H}"] > 100 * peak.get(f"gfs_{prev:%Y%m%d_%H}", 0.0)
    peak_at = conn.execute(
        "SELECT valid_at FROM sim_receptor_outlook WHERE receptor = 'Delhi' "
        "ORDER BY pm25_ugm3 DESC LIMIT 1"
    ).fetchone()[0]
    hours_after_fire = (peak_at - (now - timedelta(hours=2))).total_seconds() / 3600
    assert 3 <= hours_after_fire <= 8  # about 100 km at 5 m/s, plus up to 1 h of burning

    # Range: low <= central <= high; the calm member pulls the low end to about zero.
    bad = conn.execute(
        "SELECT count(*) FROM sim_receptor_outlook WHERE pm25_low > pm25_ugm3 + 1e-9 "
        "OR pm25_ugm3 > pm25_high + 1e-9 OR pm25_ugm3 IS NULL"
    ).fetchone()[0]
    assert bad == 0
    lo, mid, n = conn.execute(
        "SELECT pm25_low, pm25_ugm3, n_members FROM sim_receptor_outlook "
        "WHERE receptor = 'Delhi' ORDER BY pm25_ugm3 DESC LIMIT 1"
    ).fetchone()
    assert lo < 0.01 * mid and mid > 0 and n == 2
    # Zero hours are stored too; the older member's forecast ends 6 hours earlier (4 receptors).
    counts = dict(conn.execute("SELECT member, count(*) FROM sim_receptor_hourly GROUP BY 1"))
    assert counts[f"gfs_{init:%Y%m%d_%H}"] - counts[f"gfs_{prev:%Y%m%d_%H}"] == 4 * 6

    # What-if (fires assumed to continue): never below the observed-fires value, equal to it
    # before the run time, and above it somewhere in the forecast.
    w = conn.execute(
        "SELECT count(*) FILTER (WHERE o.pm25_whatif_ugm3 < o.pm25_ugm3 - 1e-6), "
        "max(abs(o.pm25_whatif_ugm3 - o.pm25_ugm3)) FILTER (WHERE o.valid_at <= r.started_at), "
        "max(o.pm25_whatif_ugm3 - o.pm25_ugm3) FILTER (WHERE o.valid_at > r.started_at) "
        "FROM sim_receptor_outlook o JOIN sim_run r USING (run_id)"
    ).fetchone()
    assert w[0] == 0 and w[1] < 1e-6 and w[2] > 0 and r["n_assumed"] > 0

    import xarray as xr

    ds = xr.open_dataset(r["path"])
    assert ds.pm25_fire.shape[1:] == (80, 120) and ds.footprint.shape == (4, 80, 120)
    assert float((ds.pm25_fire_whatif - ds.pm25_fire).min()) >= -1e-6
    assert "what-if" in ds.pm25_fire_whatif.attrs["label"]
    assert float(ds.pm25_fire.min()) >= 0 and not bool(ds.pm25_fire.isnull().any())
    assert ds.attrs["label"] == sim_run.LABEL


def test_sim_run_refuses_without_data(testdb, tmp_path):
    from sim import run as sim_run

    conn = testdb()
    sim_run.ensure_schema(conn)
    with pytest.raises(sim_run.NothingToRun):
        sim_run.run(conn, datetime.now(UTC), out_dir=tmp_path)


def test_zero_fire_day_gives_a_zero_run_not_an_error(testdb, tmp_path):
    from sim import run as sim_run

    conn = testdb()
    sim_run.ensure_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0)
    init = (now - timedelta(hours=5)).replace(minute=0, second=0)
    write_cycle(tmp_path / "new.nc", init, u=5.0)
    add_cycle(conn, tmp_path / "new.nc", init, init + timedelta(hours=4))
    conn.commit()
    r = sim_run.run(conn, now, out_dir=tmp_path / "sim", n_per_arrival=20)
    assert r["n_events"] == 0 and r["conc_max"] == 0.0 and r["n_rows"] == 0
    n, total = conn.execute("SELECT count(*), sum(pm25_ugm3) FROM sim_receptor_hourly").fetchone()
    assert n == 4 * 73 and total == 0.0


def test_same_fire_seen_by_two_satellites_is_one_event(testdb):
    from sim import fires as fires_mod
    from sim import run as sim_run

    conn = testdb()
    sim_run.ensure_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0)
    # Keep both passes inside one 6-hour window whatever the clock says.
    base = datetime.fromtimestamp((now.timestamp() // 21600) * 21600, UTC) - timedelta(hours=5)

    def add(source, lat, lon, frp, minutes):
        conn.execute(
            "INSERT INTO fire_detection (source, observed_at, published_at, published_basis, "
            "latitude, longitude, geom, frp_mw) VALUES (%s, %s, %s, 'first_seen', %s, %s, "
            "ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)",
            (source, base + timedelta(minutes=minutes), base + timedelta(hours=1), lat, lon,
             lon, lat, frp),
        )  # fmt: skip

    # Fire 1: NOAA-20 sees 2 touching pixels, NOAA-21 sees 1 of them 50 minutes later.
    add("VIIRS_NOAA20_NRT", 30.000, 75.000, 5.0, 0)
    add("VIIRS_NOAA20_NRT", 30.003, 75.000, 7.0, 0)
    add("VIIRS_NOAA21_NRT", 30.001, 75.001, 9.0, 50)
    # Fire 2: 30 km away, one satellite only.
    add("VIIRS_NOAA21_NRT", 30.300, 75.000, 4.0, 50)
    conn.commit()
    f = fires_mod.load(conn, base - timedelta(hours=1), base + timedelta(hours=5))
    assert f.n_detections == 4 and len(f.frp) == 2 and f.n_clusters == 2
    big = int(f.pixels.argmax())
    assert f.pixels[big] == 2 and f.frp[big] == 12.0  # the 2-pixel view wins; no double count
    assert f.pixels.sum() == 3


def test_disputed_areas_are_their_own_class(testdb):
    conn = testdb()
    db.ensure_schema(conn)

    def box(kind, name, code, w, s, e, n):
        conn.execute(
            "INSERT INTO boundary (kind, name, code, geom) VALUES (%s, %s, %s, "
            "ST_Multi(ST_MakeEnvelope(%s, %s, %s, %s, 4326)))",
            (kind, name, code, w, s, e, n),
        )

    box("country", "A-land", "AA", 70, 26, 75, 34)
    box("country", "B-land", "BB", 75, 26, 80, 34)
    box("region", "West", "AA", 70, 26, 75, 34)
    box("region", "East", "BB", 75, 26, 80, 34)
    box("disputed", "Middle", None, 74, 32, 76, 34)  # overlaps both countries

    def place(lon, lat):
        return conn.execute(
            "SELECT (place_of(ST_SetSRID(ST_MakePoint(%s, %s), 4326))).*", (lon, lat)
        ).fetchone()

    assert place(72, 30) == ("AA", "West") and place(78, 30) == ("BB", "East")
    assert place(74.5, 33) == ("disputed", "Middle")  # administered by A, still "disputed"
    assert place(75.5, 33) == ("disputed", "Middle")  # administered by B, same wording
    assert place(60, 10) == ("other", None)


def test_daily_source_share_adds_up_and_uses_forecast_hours_only(testdb, tmp_path):
    from sim import run as sim_run

    conn = testdb()
    sim_run.ensure_schema(conn)
    for kind, name, code, w, e in (("country", "A", "AA", 70, 77), ("country", "B", "BB", 77, 80)):
        conn.execute(
            "INSERT INTO boundary (kind, name, code, geom) VALUES (%s, %s, %s, "
            "ST_Multi(ST_MakeEnvelope(%s, 26, %s, 34, 4326)))", (kind, name, code, w, e),
        )  # fmt: skip
    now = datetime.now(UTC).replace(microsecond=0)
    init = (now - timedelta(hours=5)).replace(minute=0, second=0)
    write_cycle(tmp_path / "new.nc", init, u=5.0)
    add_cycle(conn, tmp_path / "new.nc", init, init + timedelta(hours=4))
    add_fire(conn, 28.61, 76.2, 50.0, now - timedelta(hours=2), now - timedelta(hours=1))
    conn.commit()
    sim_run.run(conn, now, n_particles=3000, out_dir=tmp_path / "sim", n_per_arrival=150)
    rows = conn.execute(
        "SELECT receptor, day_utc, country, share FROM sim_source_share ORDER BY 1, 2"
    ).fetchall()
    assert rows and {r[2] for r in rows} == {"AA"}  # the fire is in country A
    assert all(r[3] == pytest.approx(1.0) for r in rows)
    assert min(r[1] for r in rows) >= now.date()
    assert conn.execute("SELECT DISTINCT country FROM sim_cluster").fetchall() == [("AA",)]


def test_sim_uses_past_wind_cycles_but_never_as_ensemble_members(testdb, tmp_path):
    """Short cycles (first hours only) carry real winds for the days just past. A fire seen
    30 h ago must be moved by them, and they must not count as forecast members."""
    from sim import run as sim_run

    conn = testdb()
    sim_run.ensure_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0)
    init = (now - timedelta(hours=5)).replace(minute=0, second=0)
    write_cycle(tmp_path / "new.nc", init, u=0.0)  # calm forecast
    add_cycle(conn, tmp_path / "new.nc", init, init + timedelta(hours=4))
    for i in range(1, 7):  # six short past cycles with a 5 m/s westerly
        t = init - timedelta(hours=6 * i)
        write_cycle(tmp_path / f"p{i}.nc", t, u=5.0, hours=5)
        conn.execute(
            "INSERT INTO gfs_cycle VALUES (%s, %s, %s, %s, 'test', now(), 1, 6, '{u}', %s, "
            "ST_MakeEnvelope(69, 25, 81, 35, 4326))",
            (t, t, t + timedelta(hours=5), t + timedelta(hours=4), str(tmp_path / f"p{i}.nc")),
        )
    # 30 h ago, 300 km west of Delhi: reaches Delhi only if the past westerlies are used.
    add_fire(conn, 28.61, 74.1, 50.0, now - timedelta(hours=30), now - timedelta(hours=29))
    conn.commit()
    r = sim_run.run(conn, now, n_particles=3000, out_dir=tmp_path / "sim", n_per_arrival=150)
    assert r["members"] == [f"gfs_{init:%Y%m%d_%H}"]  # short cycles are not members
    assert r["n_events"] == 1
    first, peak = conn.execute(
        "SELECT min(valid_at), max(pm25_ugm3) FROM sim_receptor_hourly WHERE receptor = 'Delhi'"
    ).fetchone()
    assert peak > 0
    assert abs((first - (now - timedelta(hours=24))).total_seconds()) <= 3600  # nowcast window


def test_gfs_ingest_failure_leaves_no_half_written_cycle(testdb, tmp_path, monkeypatch):
    from weather import gfs

    conn = testdb()
    db.ensure_schema(conn)
    init = datetime(2026, 11, 14, 12, tzinfo=UTC)
    monkeypatch.setattr(gfs, "OUT_DIR", tmp_path / "gfs")
    monkeypatch.setattr(gfs, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(gfs, "published_at", lambda init, fxx=72: (init, "test"))

    class Fake:
        def download(self, search):
            return None

    monkeypatch.setattr(gfs, "_herbie", lambda init, fxx: Fake())

    def broken(init, fxx):
        raise OSError("connection dropped at hour 3")

    monkeypatch.setattr(gfs, "load_step", broken)
    with pytest.raises(OSError):
        gfs.ingest_cycle(conn, init, 1, max_fxx=5)
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM gfs_cycle").fetchone()[0] == 0
    assert not list((tmp_path / "gfs").glob("*")) if (tmp_path / "gfs").exists() else True

    # The whole source then reports "failed" through the runner, and other sources still run.
    monkeypatch.setattr(gfs, "latest_complete_cycle", lambda now=None: init)
    failed = ingest_run.run_once(
        ["gfs", "ok"], {"gfs": gfs.ingest, "ok": lambda conn: ("ok", 1, "fine")}
    )
    assert failed == 1
    rows = testdb().execute("SELECT source, status FROM ingest_run ORDER BY id").fetchall()
    assert rows == [("gfs", "failed"), ("ok", "ok")]


def test_datagov_fetch_pages_through_and_stores_as_published(testdb, monkeypatch):
    conn = testdb()
    db.ensure_schema(conn)
    ist_now = datetime.now(UTC) + timedelta(hours=5, minutes=30) - timedelta(hours=1)
    stamp = ist_now.strftime("%d-%m-%Y %H:00:00")

    def rec(i, pollutant="PM2.5", value="120"):
        return {"station": f"Station {i}", "latitude": "28.6", "longitude": "77.2",
                "last_update": stamp, "pollutant_id": pollutant, "avg_value": value}  # fmt: skip

    pages = {
        0: [rec(i) for i in range(1000)],
        1000: [rec(1000), rec(1001, "NO2"), rec(1002, value="NA")],
    }
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(params["offset"])
        assert params["api-key"] == "k"
        return Reply(payload={"records": pages[params["offset"]]})

    monkeypatch.setenv("DATAGOV_API_KEY", "k")
    monkeypatch.setattr(stations, "get", fake_get)
    status_, new, _ = stations.ingest_datagov(conn)
    assert (status_, new) == ("ok", 1001) and calls == [0, 1000]
    assert stations.ingest_datagov(conn)[1] == 0
    unit, param, n = conn.execute(
        "SELECT unit, parameter, count(*) FROM station_reading GROUP BY 1, 2"
    ).fetchone()
    assert (unit, param, n) == ("as published", "pm25_avg_as_published", 1001)


def test_permanent_hot_spots_are_not_treated_as_field_fires(testdb):
    from sim import fires as fires_mod
    from sim import run as sim_run

    conn = testdb()
    sim_run.ensure_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0)
    for day in range(12):  # a kiln: the same spot, 12 days in a row
        t = now - timedelta(days=day, hours=3)
        add_fire(conn, 29.0004, 76.0003, 2.0, t, t + timedelta(hours=1))
    one_off = now - timedelta(hours=5)
    add_fire(conn, 29.2, 76.2, 6.0, one_off, one_off + timedelta(hours=1))  # a field fire
    # A field that burned on only 3 days is still a fire.
    for day in range(3):
        t = now - timedelta(days=day, hours=4)
        add_fire(conn, 29.5, 76.5, 3.0, t, t + timedelta(hours=1))
    conn.commit()
    f = fires_mod.load(conn, now - timedelta(hours=72), now)
    assert f.n_static == 3  # the kiln's three sightings inside the 72 h window
    assert f.n_detections == 4 and sorted(f.frp.tolist()) == [3.0, 3.0, 3.0, 6.0]
