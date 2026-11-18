"""PySpark: station archives -> hourly PM2.5 per station -> Postgres (validation only).

    uv run python -m spark_jobs.station_archive

Two archives, cleaned the same way and kept only for the receptor city boxes:
- OpenAQ archive files, data/openaq/records (ingest/openaq_archive.py): all four cities.
  In 2024 it holds only the US diplomatic monitors here; in 2025 also the CPCB stations.
- XKDR Forum India Air Quality Database, data/xkdr (ingest/xkdr_archive.py): CPCB stations,
  used for the 2024 season. Its US Embassy rows are skipped (already in OpenAQ).

Cleaning rules:
- PM2.5, plus CO and PM10 as smoke tracers (table archive_tracer_hourly); negative values
  and error codes are dropped;
- a value at or above the instrument limit (CENSOR_UGM3) is kept and flagged `censored`:
  the true value is at least that high (SPEC §3: treat as censored, not missing);
- hours are UTC hour starts. OpenAQ stamps the end of the hour. XKDR times are naive Indian
  Standard Time and are taken as the start of the hour (CPCB "from date").
Needs Java 17+ (JAVA_HOME, or a runtime under ~/.local/share/jdk/).
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from ingest import config, db

OPENAQ_RECORDS = config.DATA_DIR / "openaq" / "records"
OPENAQ_LOCATIONS = config.DATA_DIR / "openaq" / "locations.json"
XKDR_DIR = config.DATA_DIR / "xkdr" / "v1"
HALF_BOX = 0.25  # same city box as sim.engine.Receptor
CENSOR_UGM3 = 985.0  # CPCB analysers stop at about 1,000 µg/m³; embassy monitors at 985
MAX_VALID_UGM3 = 2000.0  # above this a value is an error code, not a reading
IST_OFFSET_S = 19800

SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_station (
    source       text NOT NULL,                -- 'openaq' | 'xkdr'
    station_id   text NOT NULL,
    city         text NOT NULL,                -- receptor whose city box it is in
    name         text,
    country      text,
    provider     text,
    is_reference boolean NOT NULL,             -- false = low-cost sensor
    geom         geometry(Point, 4326) NOT NULL,
    PRIMARY KEY (source, station_id)
);
CREATE TABLE IF NOT EXISTS archive_pm25_hourly (
    source      text NOT NULL,
    station_id  text NOT NULL,
    hour_utc    timestamptz NOT NULL,          -- start of the hour
    pm25_ugm3   real NOT NULL,
    n_readings  integer NOT NULL,
    censored    boolean NOT NULL,              -- at the instrument limit: true value >= this
    PRIMARY KEY (source, station_id, hour_utc),
    FOREIGN KEY (source, station_id) REFERENCES archive_station
);
CREATE INDEX IF NOT EXISTS archive_pm25_hourly_hour_idx ON archive_pm25_hourly (hour_utc);
-- Smoke tracers from the same stations (validation only): CO in mg/m³, PM10 in µg/m³.
CREATE TABLE IF NOT EXISTS archive_tracer_hourly (
    source      text NOT NULL,
    station_id  text NOT NULL,
    hour_utc    timestamptz NOT NULL,
    parameter   text NOT NULL CHECK (parameter IN ('co', 'pm10')),
    value       real NOT NULL,
    n_readings  integer NOT NULL,
    PRIMARY KEY (source, station_id, hour_utc, parameter),
    FOREIGN KEY (source, station_id) REFERENCES archive_station
);
"""

# Tracers: OpenAQ parameter, XKDR parameter_name, highest valid value. OpenAQ labels CPCB CO
# as ppb while the values are mg/m³ (median about 1); see co_to_mgm3.
TRACERS = {"co": ("co", "CO", 1e5), "pm10": ("pm10", "PM10", MAX_VALID_UGM3)}
CO_PPB_MEDIAN_ABOVE = 50.0  # a station whose CO median is above this reports real ppb
CO_MG_PER_PPB = 0.001145  # mg/m³ per ppb at 25 °C


def co_to_mgm3(values: list[float]) -> float:
    """Factor that turns one station's CO values into mg/m³, judged from their median."""
    if not values:
        return 1.0
    med = sorted(values)[len(values) // 2]
    return CO_MG_PER_PPB if med > CO_PPB_MEDIAN_ABOVE else 1.0


def java_home() -> str | None:
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    for cand in sorted((Path.home() / ".local/share/jdk").glob("*/bin/java")):
        return str(cand.parents[1])
    return None


def session():
    from pyspark.sql import SparkSession

    home = java_home()
    if home:
        os.environ["JAVA_HOME"] = home
    return (
        SparkSession.builder.master("local[4]")
        .appName("station-archive")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.driver.memory", "2g")
        .config("spark.ui.enabled", "false")
        .config("spark.ui.showConsoleProgress", "false")
        .config("spark.sql.shuffle.partitions", "8")
        .getOrCreate()
    )


def _hourly(readings, name: str = "pm25_ugm3", max_valid: float = MAX_VALID_UGM3):
    """readings: station_id, epoch (s, UTC, inside the hour the reading belongs to), v.
    Epoch seconds, not timestamps: collected timestamps come back in the machine's local time
    zone, which repeats an hour when its clocks change."""
    from pyspark.sql import functions as F

    ok = readings.where(F.col("epoch").isNotNull() & (F.col("v") >= 0) & (F.col("v") <= max_valid))
    hour = (F.floor(F.col("epoch") / 3600) * 3600).alias("hour_epoch")
    return ok.groupBy("station_id", hour).agg(
        F.avg("v").alias(name),
        F.count("*").cast("int").alias("n_readings"),
        (F.max("v") >= CENSOR_UGM3).alias("censored"),
    )


def openaq_hourly(spark, records: Path = OPENAQ_RECORDS, tracer: str | None = None):
    """Hourly PM2.5 per station, or with `tracer` ('co' | 'pm10') that pollutant as `value`."""
    from pyspark.sql import functions as F

    param, _, max_valid = TRACERS[tracer] if tracer else ("pm25", None, MAX_VALID_UGM3)
    raw = (
        spark.read.option("header", True)
        .option("recursiveFileLookup", True)
        .csv(str(records))
        .where(F.col("parameter") == param)
    )
    # A reading stamped 01:00 covers 00:00-01:00: put it in the hour that starts at 00:00.
    return _hourly(
        raw.select(
            F.col("location_id").cast("string").alias("station_id"),
            (F.unix_timestamp(F.to_timestamp("datetime")) - 1).alias("epoch"),
            F.col("value").cast("double").alias("v"),
        ),
        "value" if tracer else "pm25_ugm3",
        max_valid,
    )


def xkdr_stations(spark, xkdr_dir: Path = XKDR_DIR) -> list[dict]:
    """CPCB stations inside a receptor city box."""
    out = []
    for s in spark.read.parquet(str(xkdr_dir / "stations.parquet")).collect():
        if s["source"] != "cpcb_caaqm" or s["latitude"] is None or s["longitude"] is None:
            continue
        for city, (lat, lon) in config.RECEPTORS.items():
            if abs(s["latitude"] - lat) <= HALF_BOX and abs(s["longitude"] - lon) <= HALF_BOX:
                out.append({
                    "station_id": s["station_id"], "city": city, "name": s["station_name"],
                    "country": "IN", "provider": "CPCB", "is_reference": True,
                    "latitude": s["latitude"], "longitude": s["longitude"],
                })  # fmt: skip
    return out


def xkdr_hourly(spark, station_ids: list[str], xkdr_dir: Path = XKDR_DIR,
                tracer: str | None = None):  # fmt: skip
    from pyspark.sql import functions as F

    _, param, max_valid = TRACERS[tracer] if tracer else (None, "PM2.5", MAX_VALID_UGM3)
    raw = spark.read.parquet(str(xkdr_dir / "measurements")).where(
        (F.col("parameter_name") == param) & F.col("station_id").isin(station_ids)
    )
    # The session time zone is UTC, so the naive IST clock time reads as UTC: shift it back.
    return _hourly(
        raw.select(
            "station_id",
            (F.unix_timestamp("collected_at") - IST_OFFSET_S).alias("epoch"),
            F.col("value").cast("double").alias("v"),
        ),
        "value" if tracer else "pm25_ugm3",
        max_valid,
    )


def main() -> int:
    if not OPENAQ_RECORDS.exists():
        raise SystemExit("No archive files. Run: uv run python -m ingest.openaq_archive")
    spark = session()
    spark.sparkContext.setLogLevel("ERROR")
    stations = [
        {**s, "source": "openaq", "station_id": str(s["location_id"])}
        for s in json.loads(OPENAQ_LOCATIONS.read_text())
    ]
    rows = [("openaq", r) for r in openaq_hourly(spark).collect()]
    tracer_rows = [("openaq", t, r) for t in TRACERS
                   for r in openaq_hourly(spark, tracer=t).collect()]  # fmt: skip
    if (XKDR_DIR / "measurements").exists():
        xs = xkdr_stations(spark)
        ids = [s["station_id"] for s in xs]
        stations += [{**s, "source": "xkdr"} for s in xs]
        rows += [("xkdr", r) for r in xkdr_hourly(spark, ids).collect()]
        tracer_rows += [("xkdr", t, r) for t in TRACERS
                        for r in xkdr_hourly(spark, ids, tracer=t).collect()]  # fmt: skip
    spark.stop()

    co_vals: dict[str, list[float]] = {}
    for source, t, r in tracer_rows:
        if t == "co" and source == "openaq":
            co_vals.setdefault(r["station_id"], []).append(r["value"])
    co_factor = {sid: co_to_mgm3(v) for sid, v in co_vals.items()}

    conn = db.connect()
    db.ensure_schema(conn)
    conn.execute(SCHEMA)
    conn.execute("TRUNCATE archive_pm25_hourly, archive_tracer_hourly, archive_station")
    for s in stations:
        conn.execute(
            """
            INSERT INTO archive_station (source, station_id, city, name, country, provider,
                is_reference, geom)
            VALUES (%s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            ON CONFLICT DO NOTHING
            """,
            (s["source"], s["station_id"], s["city"], s["name"], s["country"], s["provider"],
             s["is_reference"], s["longitude"], s["latitude"]),
        )  # fmt: skip
    known = {tuple(r) for r in conn.execute("SELECT source, station_id FROM archive_station")}
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY archive_pm25_hourly (source, station_id, hour_utc, pm25_ugm3, n_readings, "
            "censored) FROM STDIN"
        ) as copy,
    ):
        for source, r in rows:
            if (source, r["station_id"]) in known:
                copy.write_row((source, r["station_id"],
                                datetime.fromtimestamp(r["hour_epoch"], UTC), r["pm25_ugm3"],
                                r["n_readings"], r["censored"]))  # fmt: skip
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY archive_tracer_hourly (source, station_id, hour_utc, parameter, value, "
            "n_readings) FROM STDIN"
        ) as copy,
    ):
        for source, t, r in tracer_rows:
            if (source, r["station_id"]) in known:
                openaq_co = t == "co" and source == "openaq"
                f = co_factor.get(r["station_id"], 1.0) if openaq_co else 1.0
                copy.write_row((source, r["station_id"],
                                datetime.fromtimestamp(r["hour_epoch"], UTC), t, r["value"] * f,
                                r["n_readings"]))  # fmt: skip
    conn.commit()
    summary = conn.execute(
        """
        SELECT s.city, extract(year FROM h.hour_utc)::int, s.is_reference, h.source, count(*),
               count(DISTINCT h.station_id), sum(h.censored::int)
        FROM archive_pm25_hourly h JOIN archive_station s USING (source, station_id)
        GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3 DESC, 4
        """
    ).fetchall()
    print(f"{len(rows)} station-hours read")
    for city, year, ref, source, n, n_loc, n_cens in summary:
        kind = "reference" if ref else "LOW-COST"
        print(f"  {city:11} {year} {kind:9} {source:6} {n_loc:3d} stations {n:7d} hours, "
              f"{n_cens} at the instrument limit")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
