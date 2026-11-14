-- Stage 2 tables. Everything here is SIMULATED (label stored with each run).
-- Two emission methods are carried side by side:
--   pm25_ugm3     = method A, burned area per detection (FINN v2.5), central emission factor
--   pm25_frp_ugm3 = method B, fire radiative power x 1 h, central emission factor (low end)
CREATE TABLE IF NOT EXISTS sim_run (
    run_id            bigserial PRIMARY KEY,
    started_at        timestamptz NOT NULL,   -- also the point-in-time cut: only data seen before this
    gfs_init_time     timestamptz NOT NULL REFERENCES gfs_cycle (init_time),
    members           text[] NOT NULL,        -- forecast cycles used as ensemble members
    fires_from        timestamptz NOT NULL,
    fires_to          timestamptz NOT NULL,
    n_detections      integer NOT NULL,
    n_clusters        integer NOT NULL,
    n_particles       integer NOT NULL,
    emission_kg       double precision NOT NULL,  -- PM2.5, method A, central emission factor
    ef_low            real NOT NULL,
    ef_central        real NOT NULL,
    ef_high           real NOT NULL,
    runtime_s         real NOT NULL,
    path              text NOT NULL,          -- NetCDF with the gridded field and footprints
    params            jsonb NOT NULL,
    label             text NOT NULL DEFAULT 'simulated – not yet validated'
);
ALTER TABLE sim_run ADD COLUMN IF NOT EXISTS n_events integer;
ALTER TABLE sim_run ADD COLUMN IF NOT EXISTS emission_frp_kg double precision;

CREATE TABLE IF NOT EXISTS sim_cluster (
    run_id         bigint NOT NULL REFERENCES sim_run ON DELETE CASCADE,
    cluster_id     integer NOT NULL,
    geom           geometry(Point, 4326) NOT NULL,  -- FRP-weighted centre
    n_detections   integer NOT NULL,                -- fire events (two-satellite repeats merged)
    frp_mw         real NOT NULL,
    emission_kg    double precision NOT NULL,       -- method A, central emission factor
    first_observed timestamptz NOT NULL,
    last_observed  timestamptz NOT NULL,
    PRIMARY KEY (run_id, cluster_id)
);
ALTER TABLE sim_cluster ADD COLUMN IF NOT EXISTS emission_frp_kg double precision;
ALTER TABLE sim_cluster ADD COLUMN IF NOT EXISTS country text;  -- ISO code, 'disputed', 'other'
ALTER TABLE sim_cluster ADD COLUMN IF NOT EXISTS region text;

-- Simulated fire-smoke PM2.5 at a receptor from one fire cluster.
CREATE TABLE IF NOT EXISTS sim_contribution (
    run_id     bigint NOT NULL,
    member     text NOT NULL,
    cluster_id integer NOT NULL,
    receptor   text NOT NULL,
    valid_at   timestamptz NOT NULL,
    pm25_ugm3  real NOT NULL,
    PRIMARY KEY (run_id, member, cluster_id, receptor, valid_at),
    FOREIGN KEY (run_id, cluster_id) REFERENCES sim_cluster ON DELETE CASCADE
);
ALTER TABLE sim_contribution ADD COLUMN IF NOT EXISTS pm25_frp_ugm3 real;
-- smoke from the what-if repeats of this cluster on coming days (0 before the run time)
ALTER TABLE sim_contribution ADD COLUMN IF NOT EXISTS pm25_assumed_ugm3 real;

-- Hourly total per receptor and member, zeros included, only for hours the member covers.
CREATE TABLE IF NOT EXISTS sim_receptor_hourly (
    run_id    bigint NOT NULL REFERENCES sim_run ON DELETE CASCADE,
    member    text NOT NULL,
    receptor  text NOT NULL,
    valid_at  timestamptz NOT NULL,
    pm25_ugm3 real NOT NULL,
    PRIMARY KEY (run_id, member, receptor, valid_at)
);
ALTER TABLE sim_receptor_hourly ADD COLUMN IF NOT EXISTS pm25_frp_ugm3 real;
-- observed fires + what-if repeats on coming days (burned-area method)
ALTER TABLE sim_receptor_hourly ADD COLUMN IF NOT EXISTS pm25_whatif_ugm3 real;

-- Hourly fire-smoke PM2.5 per receptor with its range:
--   central = method A, central factor, newest forecast cycle
--   low     = lowest member, method B (FRP), low factor
--   high    = highest member, method A, high factor
--   what-if = central + assumed repeats of the last days' fires on coming days (labelled)
DROP VIEW IF EXISTS sim_receptor_outlook;
CREATE VIEW sim_receptor_outlook AS
SELECT h.run_id, h.receptor, h.valid_at,
       max(h.pm25_ugm3) FILTER (WHERE h.member = r.members[1]) AS pm25_ugm3,
       min(COALESCE(h.pm25_frp_ugm3, h.pm25_ugm3)) * r.ef_low / r.ef_central AS pm25_low,
       max(h.pm25_ugm3) * r.ef_high / r.ef_central AS pm25_high,
       count(*) AS n_members, r.label,
       max(COALESCE(h.pm25_whatif_ugm3, h.pm25_ugm3)) FILTER (WHERE h.member = r.members[1])
           AS pm25_whatif_ugm3
FROM sim_receptor_hourly h JOIN sim_run r USING (run_id)
GROUP BY h.run_id, h.receptor, h.valid_at, r.members, r.ef_low, r.ef_high, r.ef_central, r.label;

-- Daily share of each receptor's simulated fire smoke by source country / region
-- (newest cycle, method A; forecast hours only; days in UTC so no country's clock is favoured).
-- Shares do not depend on the emission scale.
DROP VIEW IF EXISTS sim_source_share;
CREATE VIEW sim_source_share AS
SELECT k.run_id, k.receptor, (k.valid_at AT TIME ZONE 'UTC')::date AS day_utc,
       c.country, c.region,
       sum(k.pm25_ugm3) AS dose_ugm3_h,
       sum(k.pm25_ugm3) / NULLIF(sum(sum(k.pm25_ugm3)) OVER (
           PARTITION BY k.run_id, k.receptor, (k.valid_at AT TIME ZONE 'UTC')::date), 0) AS share,
       r.label
FROM sim_contribution k
JOIN sim_cluster c USING (run_id, cluster_id)
JOIN sim_run r USING (run_id)
WHERE k.member = r.members[1] AND k.valid_at >= r.started_at AND k.pm25_ugm3 > 0
GROUP BY k.run_id, k.receptor, (k.valid_at AT TIME ZONE 'UTC')::date, c.country, c.region, r.label;
