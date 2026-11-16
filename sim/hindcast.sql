-- Stage 3 tables: point-in-time hindcasts of past seasons. Everything here is SIMULATED.
-- Kept apart from the live sim_* tables so a past-season run can never appear on the live map.
CREATE TABLE IF NOT EXISTS hindcast_run (
    run_id              bigserial PRIMARY KEY,
    season              integer NOT NULL,
    run_at              timestamptz NOT NULL,   -- the point-in-time cut
    gfs_init_time       timestamptz NOT NULL,
    gfs_published_at    timestamptz NOT NULL,   -- newest weather input
    fires_published_max timestamptz,            -- newest fire input (NULL: no fires)
    n_cycles            integer NOT NULL,
    n_detections        integer NOT NULL,
    n_events            integer NOT NULL,
    n_assumed           integer NOT NULL,
    n_clusters          integer NOT NULL,
    emission_kg         double precision NOT NULL,
    emission_frp_kg     double precision NOT NULL,
    runtime_s           real NOT NULL,
    params              jsonb NOT NULL,
    UNIQUE (season, run_at),
    -- point-in-time rule: nothing published after the run time may be used
    CONSTRAINT hindcast_point_in_time CHECK (
        gfs_published_at <= run_at AND gfs_init_time <= run_at
        AND (fires_published_max IS NULL OR fires_published_max <= run_at))
);

-- Simulated fire-smoke PM2.5 per receptor and hour. lead_h < 0: hours before the run.
--   pm25_ugm3        observed fires, burned-area method, central factor
--   pm25_frp_ugm3    observed fires, fire-power method (low end)
--   pm25_whatif_ugm3 observed fires + assumed repeats on coming days (labelled what-if)
CREATE TABLE IF NOT EXISTS hindcast_hourly (
    run_id           bigint NOT NULL REFERENCES hindcast_run ON DELETE CASCADE,
    receptor         text NOT NULL,
    valid_at         timestamptz NOT NULL,
    lead_h           real NOT NULL,
    pm25_ugm3        real NOT NULL,
    pm25_frp_ugm3    real NOT NULL,
    pm25_whatif_ugm3 real NOT NULL,
    vent_m2s         real,   -- forecast boundary-layer height x 10 m wind speed (weather only)
    PRIMARY KEY (run_id, receptor, valid_at)
);
ALTER TABLE hindcast_hourly ADD COLUMN IF NOT EXISTS vent_m2s real;
CREATE INDEX IF NOT EXISTS hindcast_hourly_valid_idx ON hindcast_hourly (receptor, valid_at);

-- The same, split by where the fires were (country code, 'disputed', 'other').
CREATE TABLE IF NOT EXISTS hindcast_source (
    run_id            bigint NOT NULL REFERENCES hindcast_run ON DELETE CASCADE,
    receptor          text NOT NULL,
    valid_at          timestamptz NOT NULL,
    country           text NOT NULL,
    pm25_ugm3         real NOT NULL,
    pm25_assumed_ugm3 real NOT NULL,
    PRIMARY KEY (run_id, receptor, valid_at, country)
);
