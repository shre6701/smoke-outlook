-- Stage 1 tables. Idempotent: applied on every ingest run.
-- observed_at  = when the thing happened (or, for forecasts, valid time).
-- published_at = when the source made it available. If the source gives no
--                publish time we store the time we first fetched it
--                (published_basis = 'first_seen'), which is the honest
--                point-in-time bound.
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS ingest_run (
    id          bigserial PRIMARY KEY,
    source      text NOT NULL,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL CHECK (status IN ('ok', 'skipped', 'failed')),
    rows_new    integer NOT NULL DEFAULT 0,
    note        text
);

CREATE TABLE IF NOT EXISTS fire_detection (
    id              bigserial PRIMARY KEY,
    source          text NOT NULL,
    observed_at     timestamptz NOT NULL,
    published_at    timestamptz NOT NULL,
    published_basis text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    latitude        double precision NOT NULL,
    longitude       double precision NOT NULL,
    geom            geometry(Point, 4326) NOT NULL,
    frp_mw          real,
    confidence      text,
    daynight        text,
    bright_ti4      real,
    scan            real,
    track           real,
    version         text,
    UNIQUE (source, observed_at, latitude, longitude)
);
CREATE INDEX IF NOT EXISTS fire_detection_geom_idx ON fire_detection USING gist (geom);
CREATE INDEX IF NOT EXISTS fire_detection_obs_idx ON fire_detection (observed_at);

CREATE TABLE IF NOT EXISTS station (
    source       text NOT NULL,
    station_id   text NOT NULL,
    name         text,
    country      text NOT NULL,
    provider     text,
    is_reference boolean NOT NULL,  -- false = low-cost sensor: label and down-weight
    geom         geometry(Point, 4326) NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, station_id)
);
CREATE INDEX IF NOT EXISTS station_geom_idx ON station USING gist (geom);

CREATE TABLE IF NOT EXISTS station_reading (
    source          text NOT NULL,
    station_id      text NOT NULL,
    sensor_id       text NOT NULL DEFAULT '',  -- a station can have several sensors
    parameter       text NOT NULL,
    unit            text NOT NULL,
    value           real NOT NULL,
    observed_at     timestamptz NOT NULL,
    published_at    timestamptz NOT NULL,
    published_basis text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, station_id, sensor_id, parameter, observed_at),
    FOREIGN KEY (source, station_id) REFERENCES station (source, station_id)
);
-- Upgrade a table created before sensor_id existed (old rows keep sensor_id = '').
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'station_reading' AND column_name = 'sensor_id'
    ) THEN
        ALTER TABLE station_reading ADD COLUMN sensor_id text NOT NULL DEFAULT '';
        ALTER TABLE station_reading DROP CONSTRAINT station_reading_pkey;
        ALTER TABLE station_reading
            ADD PRIMARY KEY (source, station_id, sensor_id, parameter, observed_at);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS station_reading_obs_idx ON station_reading (observed_at);

-- CAMS PM2.5 forecast at the receptors (baseline, via Open-Meteo). Every fetch is kept
-- (one per hour at most) so a backtest can use only what was available at time t.
CREATE TABLE IF NOT EXISTS cams_forecast (
    receptor        text NOT NULL,
    observed_at     timestamptz NOT NULL,  -- forecast valid time
    published_at    timestamptz NOT NULL,
    published_basis text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    pm25_ugm3       real NOT NULL,
    geom            geometry(Point, 4326) NOT NULL,
    PRIMARY KEY (receptor, observed_at, published_at)
);

-- One row per GFS cycle. The gridded subset lives in a NetCDF file (path), not in Postgres.
CREATE TABLE IF NOT EXISTS gfs_cycle (
    init_time       timestamptz PRIMARY KEY,  -- model run time
    observed_from   timestamptz NOT NULL,     -- first valid time
    observed_to     timestamptz NOT NULL,     -- last valid time
    published_at    timestamptz NOT NULL,
    published_basis text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    step_hours      integer NOT NULL,
    n_times         integer NOT NULL,
    variables       text[] NOT NULL,
    path            text NOT NULL,
    bbox            geometry(Polygon, 4326) NOT NULL
);
