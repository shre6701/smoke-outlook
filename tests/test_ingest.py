from datetime import UTC, datetime, timedelta

from ingest import cams, firms, stations
from ingest.status import flag

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)

FIRMS_CSV = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
    "confidence,version,bright_ti5,frp,daynight\n"
    "30.5,75.5,335.3,0.48,0.48,2026-11-14,828,N20,VIIRS,n,2.0NRT,300.6,3.39,D\n"
    "10.0,75.5,335.3,0.48,0.48,2026-11-14,828,N20,VIIRS,n,2.0NRT,300.6,3.39,D\n"
)


def test_firms_parse_time_and_domain():
    rows = firms.parse_csv(FIRMS_CSV, "VIIRS_NOAA20_NRT")
    assert len(rows) == 1  # second row is outside the v1 domain
    assert rows[0]["observed_at"] == datetime(2026, 11, 14, 8, 28, tzinfo=UTC)
    assert rows[0]["frp_mw"] == 3.39


def test_openaq_reading_drops_old_and_bad():
    def item(utc, value=50.0):
        return {"datetime": {"utc": utc}, "value": value, "locationsId": 1, "sensorsId": 9}

    ok = stations.openaq_reading(item("2026-11-14T11:00:00Z"), NOW)
    assert ok["value"] == 50.0 and ok["sensor_id"] == "9"
    assert stations.openaq_reading(item("2018-02-22T04:00:00Z"), NOW) is None
    assert stations.openaq_reading(item("2026-11-14T11:00:00Z", -999), NOW) is None
    assert stations.openaq_reading(item("2026-11-15T11:00:00Z"), NOW) is None  # future


def test_openaq_station_labels_low_cost():
    loc = {
        "id": 7, "name": "x", "isMonitor": False, "country": {"code": "IN"},
        "provider": {"name": "AirGradient"}, "coordinates": {"latitude": 31.6, "longitude": 74.9},
    }  # fmt: skip
    s = stations.openaq_station(loc)
    assert s["is_reference"] is False and s["country"] == "IN"
    assert stations.openaq_station({**loc, "country": {"code": "PK"}}) is None  # India only
    loc["coordinates"] = {"latitude": 10.0, "longitude": 74.3}
    assert stations.openaq_station(loc) is None


def test_datagov_skips_cleanly_without_key(monkeypatch):
    monkeypatch.delenv("DATAGOV_API_KEY", raising=False)
    status, rows, note = stations.ingest_datagov(conn=None)
    assert (status, rows) == ("skipped", 0) and "DATAGOV_API_KEY" in note


def test_datagov_rows_ist_to_utc_and_na():
    base = {
        "station": "ITO, Delhi - CPCB", "latitude": "28.63", "longitude": "77.24",
        "last_update": "14-11-2026 17:30:00",
    }  # fmt: skip
    recs = [
        {**base, "pollutant_id": "PM2.5", "avg_value": "310"},
        {**base, "pollutant_id": "PM2.5", "avg_value": "NA"},
        {**base, "pollutant_id": "NO2", "avg_value": "40"},
    ]
    rows = stations.datagov_rows(recs)
    assert len(rows) == 1
    assert rows[0]["observed_at"] == datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def test_cams_parse_skips_nulls():
    payload = {"hourly": {"time": ["2026-11-14T00:00", "2026-11-14T01:00"], "pm2_5": [80.5, None]}}
    assert cams.parse(payload) == [(datetime(2026, 11, 14, 0, tzinfo=UTC), 80.5)]


def test_stale_flag():
    assert flag(0, None, 6, NOW) == "EMPTY"
    assert flag(5, NOW - timedelta(hours=7), 6, NOW) == "STALE"
    assert flag(5, NOW - timedelta(hours=1), 6, NOW) == "ok"
