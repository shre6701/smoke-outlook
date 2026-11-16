"""Map API on a throwaway database with a small simulated run."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.test_integration import add_cycle, add_fire, write_cycle

pytestmark = pytest.mark.db


@pytest.fixture
def client(testdb, tmp_path):
    from api import main
    from sim import run as sim_run

    conn = testdb()
    sim_run.ensure_schema(conn)
    for name, code, w, e in (("A", "IN", 70, 77), ("B", "PK", 77, 80)):
        conn.execute(
            "INSERT INTO boundary (kind, name, code, geom) VALUES ('country', %s, %s, "
            "ST_Multi(ST_MakeEnvelope(%s, 26, %s, 34, 4326)))", (name, code, w, e),
        )  # fmt: skip
    for name, pop, lon, lat in (
        ("Delhi", 15_000_000, 77.21, 28.61),
        ("Chandigarh", 1_000_000, 76.78, 30.73),
    ):
        conn.execute(
            "INSERT INTO place (name, pop_max, geom) VALUES (%s, %s, "
            "ST_SetSRID(ST_MakePoint(%s, %s), 4326))", (name, pop, lon, lat),
        )  # fmt: skip
    # The latest 20:00 UTC (01:30 IST), so the fires below are night fires at any clock time.
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    now -= timedelta(hours=(now.hour - 20) % 24)
    init = (now - timedelta(hours=5)).replace(minute=0, second=0)
    write_cycle(tmp_path / "new.nc", init, u=5.0)
    add_cycle(conn, tmp_path / "new.nc", init, init + timedelta(hours=4))
    add_fire(conn, 28.61, 76.2, 50.0, now - timedelta(hours=2), now - timedelta(hours=1))
    # In box B (code PK), 15 km upwind of Delhi, so the ledger has a source abroad too.
    add_fire(conn, 28.61, 77.05, 20.0, now - timedelta(hours=2), now - timedelta(hours=1))
    conn.commit()
    sim_run.run(conn, now, n_particles=3000, out_dir=tmp_path / "sim", n_per_arrival=120)
    main._open.cache_clear()
    main._boundaries.cache_clear()
    return TestClient(main.app)


def test_meta_and_binary_layers_have_the_promised_shapes(client):
    m = client.get("/api/meta").json()
    assert "simulated" in m["label"] and "what-if" in m["whatif_label"] and m["unofficial"]
    t, g, w, n = len(m["times"]), m["grid"], m["wind"], m["tracks"]["n"]
    rid = m["run_id"]
    for kind in ("pm25", "whatif"):
        assert len(client.get(f"/api/field/{rid}/{kind}.bin").content) == t * g["ny"] * g["nx"]
    assert len(client.get(f"/api/tracks/{rid}.bin").content) == t * n * 5 + n
    assert len(client.get("/api/wind/850.bin").content) == 2 * w["hours"] * w["ny"] * w["nx"]
    assert len(client.get("/api/pbl.bin").content) == w["hours"] * w["ny"] * w["nx"]
    assert len(client.get(f"/api/footprint/{rid}/Delhi.bin").content) == g["ny"] * g["nx"]
    # The what-if field is never below the observed-fires field (same colour scale).
    a = np.frombuffer(client.get(f"/api/field/{rid}/pm25.bin").content, dtype=np.uint8)
    b = np.frombuffer(client.get(f"/api/field/{rid}/whatif.bin").content, dtype=np.uint8)
    assert (b >= a).all() and b.max() == 255
    wind = np.frombuffer(client.get("/api/wind/10m.bin").content, dtype=np.int8)
    assert wind[: wind.size // 2].max() == 5 * w["scale"] and wind[wind.size // 2 :].max() == 0
    assert {r["name"]: r["population"] for r in m["receptors"]}["Delhi"] == 15_000_000


def test_tables_are_consistent_and_labelled(client):
    rid = client.get("/api/meta").json()["run_id"]
    fires = client.get(f"/api/fires/{rid}").json()
    assert sorted(fires["country"]) == ["IN", "PK"] and len(fires["id"]) == 2
    led = client.get(f"/api/ledger/{rid}").json()
    assert "simulated" in led["label"] and "FIRE smoke only" in led["note"]
    by_day = {}
    for r in led["rows"]:
        by_day.setdefault((r["receptor"], r["day"]), []).append(r["share"])
        assert r["share_min"] <= r["share"] + 1e-9 <= r["share_max"] + 2e-9
    assert by_day and all(abs(sum(v) - 1) < 0.01 for v in by_day.values())
    # India-centric: Indian receptors only; Indian sources carry a state, foreign ones do not
    assert {r["receptor"] for r in led["rows"]} <= {"Delhi", "Chandigarh", "Amritsar"}
    assert {r["source"] for r in led["rows"]} == {"IN", "PK"}
    assert all((r["state"] is not None) == (r["source"] == "IN") for r in led["rows"])
    assert "leaving India is not" in led["note"]
    assert client.get(f"/api/receptor/{rid}/Lahore").status_code == 404
    tgt = client.get(f"/api/targeting/{rid}").json()
    shares = [r["share"] for r in tgt["rows"]]
    assert shares == sorted(shares, reverse=True) and abs(sum(shares) - 1) < 0.01
    top = tgt["rows"][0]
    assert top["country"] == "IN" and top["most_affected"] == "Delhi"  # the upwind fire
    c = client.get(f"/api/cluster/{rid}/{top['id']}").json()
    assert c["people_reached"] == 15_000_000 and c["cities"][0]["receptor"] == "Delhi"
    assert c["emission_t"]["low"] < c["emission_t"]["central"] < c["emission_t"]["high"]
    d = client.get(f"/api/receptor/{rid}/Delhi").json()
    assert len(d["times"]) == len(d["pm25"]) == len(d["whatif"]) and max(d["pm25"]) > 0
    assert all(
        lo <= mid + 1e-6 <= hi + 2e-6
        for lo, mid, hi in zip(d["low"], d["pm25"], d["high"], strict=True)
    )
    assert d["top_clusters"][0]["id"] == top["id"]


def test_bad_requests_and_static_page(client):
    rid = client.get("/api/meta").json()["run_id"]
    assert client.get(f"/api/receptor/{rid}/Nowhere").status_code == 404
    assert client.get("/api/wind/500.bin").status_code == 404
    assert client.get(f"/api/cluster/{rid}/99999").status_code == 404
    assert client.get(f"/api/field/{rid}/other.bin").status_code == 404
    assert client.get("/api/meta?run_id=99999").status_code == 404
    page = client.get("/")
    assert page.status_code == 200 and "Smoke Outlook" in page.text
    assert (
        client.get("/sw.js").status_code == 200
        and client.get("/manifest.webmanifest").status_code == 200
    )


def test_stations_report_stale_readings(client, testdb):
    conn = testdb()
    pt = "ST_SetSRID(ST_MakePoint(77.2, 28.6), 4326)"
    for sid, age in (("fresh", 1), ("old", 30)):
        conn.execute(
            f"INSERT INTO station VALUES ('openaq', %s, %s, 'IN', 'p', true, {pt})", (sid, sid)
        )
        conn.execute(
            "INSERT INTO station_reading (source, station_id, sensor_id, parameter, unit, value, "
            "observed_at, published_at, published_basis) VALUES ('openaq', %s, 's', 'pm25', 'u', "
            "88, now() - %s * interval '1 hour', now(), 'first_seen')", (sid, age),
        )  # fmt: skip
    conn.commit()
    s = client.get("/api/stations").json()
    assert dict(zip(s["name"], s["stale"], strict=True)) == {"fresh": False, "old": True}
    assert "all pollution sources" in s["note"]


def test_labels_say_not_validated_until_skill_exists_then_show_it_per_city(client, testdb):
    import json

    from sim import validate

    m = client.get("/api/meta").json()
    assert "not yet validated" in m["label"] and not m["validated"]
    assert all(r["skill"] is None for r in m["receptors"])

    conn = testdb()
    conn.execute(validate.SCHEMA)
    ins = "INSERT INTO validation_skill (scope, name, season, lead_day, metrics) VALUES "
    ins += "(%s, %s, %s, %s, %s)"
    good = {"n_days": 60, "verdict": "useful", "r_change": 0.5, "auc_spike": 0.8,
            "stations_reference_share": 1.0}  # fmt: skip
    weak = {**good, "verdict": "not reliable here", "auc_spike": 0.55}
    other = "Chandigarh"
    for name, season, metrics in (("Delhi", 2024, good), ("Delhi", 2025, good),
                                  (other, 2024, good), (other, 2025, weak)):  # fmt: skip
        conn.execute(ins, ("city", name, season, 1, json.dumps(metrics)))
    conn.execute(ins, ("ledger", "Delhi", 2024, 0, '{"median_points": 6, "p90_points": 20}'))
    conn.execute(ins, ("ledger", "Delhi", 2025, 0, '{"median_points": 9, "p90_points": 15}'))
    conn.commit()

    m = client.get("/api/meta").json()
    assert m["validated"] and "not yet validated" not in m["label"] and "simulated" in m["label"]
    skill = {r["name"]: r["skill"] for r in m["receptors"]}
    assert skill["Delhi"]["verdict"] == "useful"
    assert skill["Chandigarh"]["verdict"] == "not reliable here"  # one weak season is enough
    rid = m["run_id"]
    assert client.get(f"/api/receptor/{rid}/Chandigarh").json()["skill"]["verdict"] != "useful"
    led = client.get(f"/api/ledger/{rid}").json()
    assert led["revision_points"]["Delhi"] == {"median": 9, "p90": 20}  # the worse season
    assert "simulated" in client.get(f"/api/targeting/{rid}").json()["label"]
