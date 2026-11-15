"""Stage 3: hindcasts are point-in-time, the statistics are right, station cleaning is right."""

from datetime import UTC, datetime, timedelta

import numpy as np
import psycopg
import pytest

from sim import validate as v
from tests.test_integration import add_cycle, add_fire, write_cycle

# ---------- statistics ----------


def test_rank_shares_ties_and_spearman_is_one_for_any_rising_curve():
    assert list(v.rank(np.array([10.0, 0.0, 0.0, 5.0]))) == [3.0, 0.5, 0.5, 2.0]
    x = np.arange(30.0)
    assert v.spearman(x, np.exp(x / 5)) == pytest.approx(1.0)
    assert v.spearman(x, -x) == pytest.approx(-1.0)
    assert np.isnan(v.spearman(x, np.zeros(30)))  # no smoke at all: no score, not zero


def test_auc_is_one_half_for_no_skill_and_one_for_perfect():
    event = np.array([0, 0, 0, 1, 1], dtype=bool)
    assert v.auc(np.array([1.0, 2, 3, 4, 5]), event) == 1.0
    assert v.auc(np.array([5.0, 4, 3, 2, 1]), event) == 0.0
    assert v.auc(np.zeros(5), event) == 0.5


def test_block_bootstrap_range_contains_the_estimate():
    rng = np.random.default_rng(1)
    x = rng.normal(size=60)
    y = x + rng.normal(size=60)
    lo, hi = v.block_ci(v.spearman, x, y)
    assert lo < v.spearman(x, y) < hi and lo > 0


def test_smoke_scale_is_never_negative():
    x = np.arange(30.0)
    assert v.fit_line(x, 100 - x)[1] == 0.0
    a, b, k = v.fit_two(x, x[::-1].copy(), 3 * x + 1)
    assert k >= 0 and b == pytest.approx(3.0, abs=0.2)


def test_window_needs_enough_measured_hours():
    x = np.arange(48.0)
    x[30:40] = np.nan
    assert v.window_mean(x, 0, 24) == pytest.approx(11.5)
    assert np.isnan(v.window_mean(x, 24, 48))  # 10 of 24 hours missing
    assert np.isnan(v.window_mean(x, -5, 10))  # starts before the data


def test_a_model_that_knows_the_rises_is_useful_and_noise_is_not():
    rng = np.random.default_rng(0)
    smoke = np.clip(rng.normal(1, 1, 61).cumsum() % 7, 0, None)
    obs = 80 + 30 * smoke + rng.normal(0, 5, 61)

    def pairs(sim):
        return {"obs": obs[1:], "obs_prev": obs[:-1], "whatif": sim[1:], "seen": sim[1:],
                "frp": sim[1:], "seen_prev": sim[:-1], "cams": np.full(60, np.nan)}  # fmt: skip

    assert v.city_metrics(pairs(smoke))["verdict"] == "useful"
    assert v.city_metrics(pairs(rng.random(61)))["verdict"] == "not reliable here"


# ---------- hindcast ----------


def test_hindcast_uses_only_inputs_published_before_the_run(testdb, tmp_path):
    from sim import hindcast as h

    conn = testdb()
    h.ensure_schema(conn)
    now = datetime(2024, 11, 5, 12, tzinfo=UTC)
    init = now - timedelta(hours=6)
    write_cycle(tmp_path / "a.nc", init, u=5.0)
    write_cycle(tmp_path / "b.nc", init - timedelta(hours=24), u=5.0)
    add_cycle(conn, tmp_path / "a.nc", init, init + timedelta(hours=4))
    add_cycle(conn, tmp_path / "b.nc", init - timedelta(hours=24), init - timedelta(hours=20))
    # A later cycle exists in the archive but was published after the run time.
    add_cycle(conn, tmp_path / "late.nc", init + timedelta(hours=6), now + timedelta(hours=4))
    add_fire(conn, 28.61, 76.2, 50.0, now - timedelta(hours=5), now - timedelta(hours=2))
    # Seen by the satellite before the run, but published only after it: must not be used.
    add_fire(conn, 28.61, 76.6, 999.0, now - timedelta(hours=1), now + timedelta(hours=2))
    conn.commit()

    r = h.simulate(conn, now, n_per_arrival=100)
    assert r["gfs_init"] == init and r["gfs_published"] <= now
    assert r["n_events"] == 1 and r["fires_published_max"] <= now
    assert r["valid"][0] == now - timedelta(hours=24) and r["valid"][-1] == init + timedelta(
        hours=72
    )
    delhi = r["receptors"].index("Delhi")
    assert r["pm25"][:, delhi].max() > 0  # westerly wind carries the upwind fire to Delhi
    assert np.all(r["whatif"] >= r["pm25"] - 1e-6)
    assert r["by_country"][0].sum(axis=2) == pytest.approx(r["pm25"], rel=1e-4, abs=1e-6)
    h.store(conn, 2024, r, {})
    h.store(conn, 2024, r, {})  # storing a run again replaces it
    assert conn.execute("SELECT count(*) FROM hindcast_run").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM hindcast_hourly").fetchone()[0] == 91 * 4

    # The database itself refuses a run whose inputs are newer than its run time.
    with pytest.raises(psycopg.errors.CheckViolation):
        h.store(conn, 2024, {**r, "gfs_published": now + timedelta(minutes=1)}, {})
    conn.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        h.store(conn, 2024, {**r, "fires_published_max": now + timedelta(minutes=1)}, {})


def test_hindcast_without_a_stored_forecast_is_skipped_not_invented(testdb):
    from sim import hindcast as h

    conn = testdb()
    h.ensure_schema(conn)
    assert h.simulate(conn, datetime(2024, 11, 5, 12, tzinfo=UTC)) is None


def test_archive_fires_get_the_assumed_three_hour_delay(testdb, monkeypatch):
    from ingest import firms
    from sim import hindcast as h

    conn = testdb()
    h.ensure_schema(conn)
    seen = datetime(2024, 10, 20, 8, 0, tzinfo=UTC)
    row = {"observed_at": seen, "latitude": 30.0, "longitude": 75.0, "frp_mw": 5.0,
           "confidence": "n", "daynight": "D", "bright_ti4": 330.0, "scan": 0.4, "track": 0.4,
           "version": "2"}  # fmt: skip
    monkeypatch.setenv("FIRMS_MAP_KEY", "k")
    monkeypatch.setattr(
        firms, "_fetch", lambda key, source, days, start: [{**row, "source": source}]
    )
    monkeypatch.setattr(h, "SEASONS", {2024: (seen.date(), seen.date())})
    monkeypatch.setattr(h, "STATIC_HISTORY_DAYS", 0)
    assert h.fetch_fires(conn, 2024) == 2  # one per satellite; a second fetch adds nothing
    assert h.fetch_fires(conn, 2024) == 0
    rows = conn.execute("SELECT DISTINCT published_at, published_basis FROM fire_detection")
    assert rows.fetchall() == [(seen + timedelta(hours=3), "assumed_latency_3h")]


# ---------- station archive (Spark) ----------


def test_station_cleaning_rules(tmp_path):
    pytest.importorskip("pyspark")
    from spark_jobs import station_archive as sa

    if not sa.java_home():
        pytest.skip("no Java runtime for Spark")
    d = tmp_path / "locationid=1"
    d.mkdir()
    head = (
        '"location_id","sensors_id","location","datetime","lat","lon","parameter","units","value"\n'
    )
    rows = [
        '1,9,"x","2024-11-01T01:00:00+05:30","1","1","pm25","u","100.0"',  # hour ends 19:30 UTC
        '1,9,"x","2024-11-01T01:15:00+05:30","1","1","pm25","u","200.0"',  # same UTC hour
        '1,9,"x","2024-11-01T03:00:00+05:30","1","1","pm25","u","-999"',  # error code
        '1,9,"x","2024-11-01T04:00:00+05:30","1","1","pm25","u","999.9"',  # instrument limit
        '1,9,"x","2024-11-01T05:00:00+05:30","1","1","pm10","u","50"',  # other pollutant
        '1,9,"x","2024-10-27T00:30:00+00:00","1","1","pm25","u","10"',  # clock-change night
        '1,9,"x","2024-10-27T01:30:00+00:00","1","1","pm25","u","20"',
    ]
    (d / "f.csv").write_text(head + "\n".join(rows) + "\n")
    spark = sa.session()
    try:
        out = {r["hour_epoch"]: r for r in sa.openaq_hourly(spark, tmp_path).collect()}
    finally:
        spark.stop()

    def epoch(*a):
        return int(datetime(*a, tzinfo=UTC).timestamp())

    assert len(out) == 4
    first = out[epoch(2024, 10, 31, 19)]
    assert first["pm25_ugm3"] == 150.0 and first["n_readings"] == 2 and not first["censored"]
    assert out[epoch(2024, 10, 31, 22)]["censored"]
    assert {epoch(2024, 10, 27, 0), epoch(2024, 10, 27, 1)} <= set(out)


# ---------- more conditions ----------


def _hindcast_inputs(conn, tmp_path, now, with_fire=True, days_back=(0, 1)):
    init = now - timedelta(hours=6)
    for d in days_back:
        t = init - timedelta(days=d)
        write_cycle(tmp_path / f"c{d}.nc", t, u=5.0)
        add_cycle(conn, tmp_path / f"c{d}.nc", t, t + timedelta(hours=4))
    if with_fire:
        add_fire(conn, 28.61, 76.2, 50.0, now - timedelta(hours=5), now - timedelta(hours=2))
    conn.commit()
    return init


def test_hindcast_day_without_fires_is_zero_smoke_not_an_error(testdb, tmp_path):
    from sim import hindcast as h

    conn = testdb()
    h.ensure_schema(conn)
    now = datetime(2024, 10, 2, 12, tzinfo=UTC)
    _hindcast_inputs(conn, tmp_path, now, with_fire=False)
    r = h.simulate(conn, now, n_per_arrival=50)
    assert r["n_events"] == 0 and float(r["whatif"].max()) == 0.0
    h.store(conn, 2024, r, {})
    row = conn.execute("SELECT fires_published_max, n_events FROM hindcast_run").fetchone()
    assert row == (None, 0)
    assert conn.execute("SELECT max(pm25_ugm3) FROM hindcast_hourly").fetchone()[0] == 0


def test_hindcast_with_todays_forecast_missing_falls_back_to_yesterdays(testdb, tmp_path):
    from sim import hindcast as h

    conn = testdb()
    h.ensure_schema(conn)
    now = datetime(2024, 11, 5, 12, tzinfo=UTC)
    init = _hindcast_inputs(conn, tmp_path, now, days_back=(1, 2))  # today's cycle never arrived
    r = h.simulate(conn, now, n_per_arrival=50)
    assert r["gfs_init"] == init - timedelta(days=1)
    assert r["valid"][-1] == r["gfs_init"] + timedelta(hours=72)  # a shorter outlook, no guess
    h.store(conn, 2024, r, {})  # still passes the point-in-time check


def test_hindcast_is_the_same_whatever_the_database_time_zone(testdb, tmp_path):
    from sim import hindcast as h

    now = datetime(2024, 11, 5, 12, tzinfo=UTC)
    conn = testdb()
    h.ensure_schema(conn)
    _hindcast_inputs(conn, tmp_path, now)
    a = h.simulate(conn, now, n_per_arrival=50)
    other = testdb(options="-c timezone=Asia/Karachi")
    b = h.simulate(other, now, n_per_arrival=50)
    assert a["valid"] == b["valid"] and a["n_events"] == b["n_events"] == 1
    assert np.array_equal(a["pm25"], b["pm25"])


def _fake_season(conn, year, days, smoke_of_day, obs_of_day, cities=("Delhi", "Chandigarh")):
    """Hindcast rows and station hours written straight into the tables."""
    from spark_jobs import station_archive as sa

    conn.execute(sa.SCHEMA)
    start = datetime(year, 10, 1, 12, tzinfo=UTC)
    for city in cities:
        conn.execute(
            "INSERT INTO archive_station VALUES ('t', %s, %s, 'x', 'XX', 'p', true, "
            "ST_SetSRID(ST_MakePoint(0, 0), 4326)) ON CONFLICT DO NOTHING", (city, city),
        )  # fmt: skip
    for d in range(days):
        run_at = start + timedelta(days=d)
        run_id = conn.execute(
            "INSERT INTO hindcast_run (season, run_at, gfs_init_time, gfs_published_at, n_cycles, "
            "n_detections, n_events, n_assumed, n_clusters, emission_kg, emission_frp_kg, "
            "runtime_s, params) VALUES (%s, %s, %s, %s, 1, 1, 1, 1, 1, 1, 1, 1, '{}') "
            "RETURNING run_id",
            (year, run_at, run_at - timedelta(hours=6), run_at - timedelta(hours=2)),
        ).fetchone()[0]
        for city in cities:
            for lead in range(-24, 67):
                day = d + lead // 24  # the model "knows" the smoke of each coming day
                s = smoke_of_day(day)
                conn.execute(
                    "INSERT INTO hindcast_hourly VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (run_id, city, run_at + timedelta(hours=lead), lead, s, s / 10, s),
                )
                if lead >= 0:
                    for cc, part in (("IN", 0.7), ("PK", 0.3 if d % 2 else 0.1)):
                        conn.execute(
                            "INSERT INTO hindcast_source VALUES (%s, %s, %s, %s, %s, 0)",
                            (run_id, city, run_at + timedelta(hours=lead), cc, s * part + 0.01),
                        )
            for hour in range(24):
                conn.execute(
                    "INSERT INTO archive_pm25_hourly VALUES ('t', %s, %s, %s, 1, false) "
                    "ON CONFLICT DO NOTHING",
                    (city, run_at + timedelta(hours=hour), obs_of_day(d)),
                )
    conn.commit()


def test_report_end_to_end_scores_a_good_model_and_handles_cities_without_data(
    testdb, monkeypatch, tmp_path
):
    from datetime import date

    from sim import hindcast as h

    conn = testdb()
    h.ensure_schema(conn)
    rng = np.random.default_rng(3)
    smoke = rng.gamma(1.0, 2.0, 80)
    for year in (2024, 2025):
        _fake_season(conn, year, 40, lambda d: float(smoke[max(d, 0)]),
                     lambda d: float(60 + 25 * smoke[d]))  # fmt: skip
    seasons = {y: (date(y, 10, 1), date(y, 11, 9)) for y in (2024, 2025)}
    monkeypatch.setattr(v, "SEASONS", seasons)
    monkeypatch.setattr(v, "REPORT", tmp_path / "validation.md")
    res = v.compute(conn)
    delhi = res["city"][("Delhi", 2024, 1)]
    assert delhi["verdict"] == "useful" and delhi["auc_spike"] > 0.9 and delhi["n_days"] >= 35
    assert res["added"][("Delhi", 2025, 1)]["improvement"] > 0.5
    assert res["scale"][("Delhi", 2024)]["scale"] == pytest.approx(25, rel=0.1)
    # Cities with no stations in the test get no score and no "useful" badge.
    assert res["city"][("Amritsar", 2024, 1)]["verdict"] == "too few days with measurements"
    assert res["ledger"][2024]["Delhi"]["median_points"] > 0
    text = v.report(res)
    assert "| Delhi | 2024 |" in text and "not replace" in text and "simulated" in text
    v.store(conn, res)
    v.store(conn, res)  # can be run again
    n = conn.execute("SELECT count(*) FROM validation_skill WHERE scope = 'city'").fetchone()[0]
    assert n == 4 * 2 * 3


def test_report_with_no_hindcasts_at_all_gives_no_verdicts(testdb):
    from sim import hindcast as h
    from spark_jobs import station_archive as sa

    conn = testdb()
    h.ensure_schema(conn)
    conn.execute(sa.SCHEMA)
    conn.execute("CREATE TABLE archive_cams_hourly (receptor text, hour_utc timestamptz, "
                 "pm25_ugm3 real)")  # fmt: skip
    res = v.compute(conn)
    assert {m["verdict"] for m in res["city"].values()} == {"too few days with measurements"}
    assert "Validation" in v.report(res)


def test_city_value_prefers_reference_monitors_and_survives_values_at_the_limit(testdb):
    from sim import hindcast as h
    from spark_jobs import station_archive as sa

    conn = testdb()
    h.ensure_schema(conn)
    conn.execute(sa.SCHEMA)
    conn.execute("CREATE TABLE archive_cams_hourly (receptor text, hour_utc timestamptz, "
                 "pm25_ugm3 real)")  # fmt: skip
    for sid, ref in (("r1", True), ("r2", True), ("r3", True), ("cheap", False)):
        conn.execute("INSERT INTO archive_station VALUES ('t', %s, 'Delhi', 'x', 'IN', 'p', %s, "
                     "ST_SetSRID(ST_MakePoint(0, 0), 4326))", (sid, ref))  # fmt: skip
    t = datetime(2024, 10, 5, 0, tzinfo=UTC)
    rows = [("r1", t, 100, False), ("r2", t, 120, False), ("r3", t, 999, True),
            ("cheap", t, 5, False), ("cheap", t + timedelta(hours=1), 77, False)]  # fmt: skip
    for sid, when, value, cens in rows:
        conn.execute("INSERT INTO archive_pm25_hourly VALUES ('t', %s, %s, %s, 1, %s)",
                     (sid, when, value, cens))  # fmt: skip
    conn.commit()
    t0, obs, ref, nst, cens, _ = v.load_series(conn, 2024)
    i = v.hours_between(t0, t)
    assert obs["Delhi"][i] == 120 and ref["Delhi"][i] and nst["Delhi"][i] == 3  # median, ref only
    assert obs["Delhi"][i + 1] == 77 and not ref["Delhi"][i + 1]  # low-cost fills the gap
    assert cens["Delhi"] == 1 and np.isnan(obs["Chandigarh"]).all()


def test_xkdr_times_are_read_as_indian_standard_time(tmp_path):
    pytest.importorskip("pyspark")
    import pyarrow as pa
    import pyarrow.parquet as pq

    from spark_jobs import station_archive as sa

    if not sa.java_home():
        pytest.skip("no Java runtime for Spark")
    (tmp_path / "measurements").mkdir()
    pq.write_table(pa.table({
        "station_id": ["in_box", "far", "embassy"], "station_name": ["a", "b", "c"],
        "source": ["cpcb_caaqm", "cpcb_caaqm", "us_embassy"],
        "latitude": [28.7, 19.0, 28.6], "longitude": [77.1, 72.8, 77.2],
    }), tmp_path / "stations.parquet")  # fmt: skip
    pq.write_table(pa.table({
        "station_id": ["in_box", "in_box", "in_box"],
        "parameter_name": ["PM2.5", "PM2.5", "PM10"],
        "collected_at": [datetime(2024, 11, 1, 5, 30), datetime(2024, 11, 1, 6, 30),
                         datetime(2024, 11, 1, 5, 30)],
        "value": [300.0, 1500.5, 9.0],
    }), tmp_path / "measurements" / "data.parquet")  # fmt: skip
    spark = sa.session()
    try:
        stations = sa.xkdr_stations(spark, tmp_path)
        out = sa.xkdr_hourly(spark, [s["station_id"] for s in stations], tmp_path).collect()
    finally:
        spark.stop()
    assert [(s["station_id"], s["city"]) for s in stations] == [("in_box", "Delhi")]
    by_hour = {r["hour_epoch"]: r for r in out}
    midnight = int(datetime(2024, 11, 1, 0, tzinfo=UTC).timestamp())  # 05:30 IST = 00:00 UTC
    assert by_hour[midnight]["pm25_ugm3"] == 300.0
    assert by_hour[midnight + 3600]["censored"]  # 1,500 is kept and flagged, PM10 is ignored
    assert len(out) == 2
