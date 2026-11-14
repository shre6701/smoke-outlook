-- Stage 2 tables. Everything here is SIMULATED (label stored with each run).
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
    emission_kg       double precision NOT NULL,  -- PM2.5, central emission factor
    ef_low            real NOT NULL,
    ef_central        real NOT NULL,
    ef_high           real NOT NULL,
    runtime_s         real NOT NULL,
    path              text NOT NULL,          -- NetCDF with the gridded field and footprints
    params            jsonb NOT NULL,
    label             text NOT NULL DEFAULT 'simulated – not yet validated'
);

CREATE TABLE IF NOT EXISTS sim_cluster (
    run_id         bigint NOT NULL REFERENCES sim_run ON DELETE CASCADE,
    cluster_id     integer NOT NULL,
    geom           geometry(Point, 4326) NOT NULL,  -- FRP-weighted centre
    n_detections   integer NOT NULL,
    frp_mw         real NOT NULL,
    emission_kg    double precision NOT NULL,       -- central emission factor
    first_observed timestamptz NOT NULL,
    last_observed  timestamptz NOT NULL,
    PRIMARY KEY (run_id, cluster_id)
);

-- Simulated fire-smoke PM2.5 at a receptor from one fire cluster (central emission factor).
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

-- Hourly fire-smoke PM2.5 per receptor with its range:
-- low = lowest member x low emission factor, high = highest member x high emission factor.
CREATE OR REPLACE VIEW sim_receptor_outlook AS
WITH per_member AS (
    SELECT run_id, member, receptor, valid_at, sum(pm25_ugm3) AS pm25
    FROM sim_contribution GROUP BY 1, 2, 3, 4
)
SELECT m.run_id, m.receptor, m.valid_at,
       max(m.pm25) FILTER (WHERE m.member = r.members[1]) AS pm25_ugm3,
       min(m.pm25) * r.ef_low / r.ef_central AS pm25_low,
       max(m.pm25) * r.ef_high / r.ef_central AS pm25_high,
       count(*) AS n_members, r.label
FROM per_member m JOIN sim_run r USING (run_id)
GROUP BY m.run_id, m.receptor, m.valid_at, r.members, r.ef_low, r.ef_high, r.ef_central, r.label;
