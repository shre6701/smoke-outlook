"""Map API:  uv run uvicorn api.main:app --port 8000   (then open http://localhost:8000)

Grids and particle tracks go out as small binary arrays (gzip on the wire), never as big JSON.
Everything simulated carries its label; the what-if is always a separate field.
"""

import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from ingest import config, db
from ingest.config import STALE_HOURS
from sim.run import AGREE, WHATIF_LABEL

app = FastAPI(title="Smoke & GRAP Outlook", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(GZipMiddleware, minimum_size=1000)

WIND_LEVELS = {"10m": 0, "925": 1, "850": 2, "700": 3}
WIND_SCALE = 4.0  # int8 = m/s x 4  (range about -32..32 m/s)
PBL_STEP_M = 20.0  # uint8 = metres / 20  (0..5100 m)
TRACK_PAD = 4.0
UNOFFICIAL = "Unofficial. Not a government forecast or advisory."


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def binary(arr: np.ndarray) -> Response:
    return Response(
        np.ascontiguousarray(arr).tobytes(),
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=300"},
    )


@lru_cache(maxsize=4)
def _open(path: str):
    import xarray as xr

    with xr.open_dataset(path) as ds:
        return ds.load()


def run_row(conn, run_id: int | None = None) -> dict:
    row = conn.execute(
        "SELECT run_id, started_at, gfs_init_time, members, n_detections, n_events, n_clusters, "
        "emission_kg, emission_frp_kg, ef_low, ef_central, ef_high, runtime_s, path, params, label "
        "FROM sim_run WHERE (%(id)s::bigint IS NULL OR run_id = %(id)s) "
        "ORDER BY run_id DESC LIMIT 1",
        {"id": run_id},
    ).fetchone()
    if not row:
        raise HTTPException(404, "No simulation run yet. Run: uv run python -m sim.run")
    keys = [
        "run_id", "started_at", "gfs_init_time", "members", "n_detections", "n_events",
        "n_clusters", "emission_kg", "emission_frp_kg", "ef_low", "ef_central", "ef_high",
        "runtime_s", "path", "params", "label",
    ]  # fmt: skip
    run = dict(zip(keys, row, strict=True))
    run["label"] = label_for(conn, run)
    return run


def run_data(run: dict):
    path = Path(run["path"])
    path = path if path.is_absolute() else config.ROOT / path
    if not path.exists():
        raise HTTPException(404, "The output file of this run is missing")
    return _open(str(path))


def latest_cycle(conn) -> tuple[datetime, str]:
    row = conn.execute(
        "SELECT init_time, path FROM gfs_cycle WHERE n_times >= 73 ORDER BY init_time DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise HTTPException(404, "No weather stored. Run: uv run python -m ingest.run")
    path = Path(row[1])
    return row[0], str(path if path.is_absolute() else config.ROOT / path)


def field_max(ds) -> float:
    """One colour scale for the observed-fires field and the what-if field."""
    top = float(max(ds.pm25_fire.max(), ds.pm25_fire_whatif.max())) if "pm25_fire_whatif" in ds \
        else float(ds.pm25_fire.max())  # fmt: skip
    return max(top, 1.0)


def freshness(conn) -> list[dict]:
    now = datetime.now(UTC)
    checks = [
        ("Weather forecast (GFS)", "gfs", "SELECT max(init_time) FROM gfs_cycle"),
        ("Fires (VIIRS)", "firms", "SELECT max(observed_at) FROM fire_detection"),
        ("India reference stations", "stations_in",
         "SELECT max(r.observed_at) FROM station_reading r JOIN station s USING (source, "
         "station_id) WHERE s.country = 'IN' AND s.is_reference"),
    ]  # fmt: skip
    out = []
    for label, key, sql in checks:
        newest = conn.execute(sql).fetchone()[0]
        age = (now - newest).total_seconds() / 3600 if newest else None
        out.append({
            "source": label, "newest": iso(newest) if newest else None,
            "age_h": round(age, 1) if age is not None else None,
            "stale": age is None or age > STALE_HOURS[key],
        })  # fmt: skip
    return out


VALIDATED_LABEL = "simulated – skill per city shown (hindcasts of 2024 and 2025)"


def skill(conn) -> dict:
    """Validation results per city from the hindcasts (empty until sim.validate has run).
    A city is "useful" only if it passed in every season that had enough measurements."""
    if not conn.execute("SELECT to_regclass('validation_skill')").fetchone()[0]:
        return {}
    rows = conn.execute(
        "SELECT scope, name, season, metrics FROM validation_skill "
        "WHERE (scope = 'city' AND lead_day = 1) OR scope = 'ledger' ORDER BY name, season"
    ).fetchall()
    out: dict = {}
    for scope, name, season, m in rows:
        city = out.setdefault(name, {"seasons": [], "ledger_points": None})
        if scope == "ledger":
            prev = city["ledger_points"] or {"median": 0.0, "p90": 0.0}
            city["ledger_points"] = {"median": max(prev["median"], m["median_points"]),
                                     "p90": max(prev["p90"], m["p90_points"])}  # fmt: skip
            continue
        city["seasons"].append({
            "season": season, "days": m.get("n_days"), "verdict": m.get("verdict"),
            "change_corr": m.get("r_change"), "change_corr_range": m.get("r_change_ci"),
            "spike_auc": m.get("auc_spike"), "cams_spike_auc": m.get("auc_spike_cams"),
            "reference_share": m.get("stations_reference_share"),
        })  # fmt: skip
    for city in out.values():
        scored = [s for s in city["seasons"] if s["spike_auc"] is not None]
        city["verdict"] = (
            "useful" if scored and all(s["verdict"] == "useful" for s in scored)
            else "not reliable here"
        )  # fmt: skip
        city["low_cost_only"] = any((s["reference_share"] or 0) < 0.5 for s in city["seasons"])
    return out


def label_for(conn, run: dict) -> str:
    """Runs made before validation carry "not yet validated"; once skill exists, say so."""
    return VALIDATED_LABEL if skill(conn) else run["label"]


@app.get("/api/meta")
def meta(run_id: int | None = None):
    with db.connect() as conn:
        run = run_row(conn, run_id)
        ds = run_data(run)
        init, wind_path = latest_cycle(conn)
        wind = _open(wind_path)
        receptors = []
        pops = dict(conn.execute("SELECT name, pop_max FROM place").fetchall())
        check = run["params"].get("forward_over_backward", {})
        skills = skill(conn)
        for name, (lat, lon) in config.RECEPTORS.items():
            ratio = check.get(name)
            receptors.append({
                "name": name, "lat": lat, "lon": lon, "population": pops.get(name),
                "self_check": ratio, "skill": skills.get(name),
                "low_confidence": ratio is None or not AGREE[0] <= ratio <= AGREE[1],
            })  # fmt: skip
        times = [iso(t.astype("datetime64[s]").astype(datetime).replace(tzinfo=UTC))
                 for t in ds.time.values]  # fmt: skip
        lats, lons = np.sort(wind.latitude.values), np.sort(wind.longitude.values)
        return {
            "run_id": run["run_id"], "label": label_for(conn, run),
            "whatif_label": WHATIF_LABEL, "validated": bool(skills),
            "unofficial": UNOFFICIAL, "started_at": iso(run["started_at"]),
            "gfs_init": iso(run["gfs_init_time"]), "members": run["members"],
            "n_detections": run["n_detections"], "n_events": run["n_events"],
            "n_clusters": run["n_clusters"],
            "emission_t": {
                "central": run["emission_kg"] / 1000,
                "low": (run["emission_frp_kg"] or 0) * run["ef_low"] / run["ef_central"] / 1000,
                "high": run["emission_kg"] * run["ef_high"] / run["ef_central"] / 1000,
            },
            "times": times,
            "grid": {"west": config.WEST, "south": config.SOUTH, "east": config.EAST,
                     "north": config.NORTH, "ny": ds.sizes["lat"], "nx": ds.sizes["lon"]},
            "field": {"encoding": "ug_m3 = expm1(byte / 255 * log1p(max))", "max": field_max(ds)},
            "tracks": {"n": int(ds.sizes.get("particle", 0)),
                       "west": config.WEST - TRACK_PAD, "south": config.SOUTH - TRACK_PAD,
                       "east": config.EAST + TRACK_PAD, "north": config.NORTH + TRACK_PAD},
            "wind": {"init": iso(init), "hours": int(wind.sizes["valid_time"]),
                     "west": float(lons[0]), "east": float(lons[-1]), "south": float(lats[0]),
                     "north": float(lats[-1]), "ny": len(lats), "nx": len(lons),
                     "levels": list(WIND_LEVELS), "scale": WIND_SCALE, "pbl_step_m": PBL_STEP_M},
            "receptors": receptors, "freshness": freshness(conn),
        }  # fmt: skip


@app.get("/api/field/{run_id}/{kind}.bin")
def field(run_id: int, kind: str):
    """uint8 [time, lat (south to north), lon]; see meta.field for the scale."""
    if kind not in ("pm25", "whatif"):
        raise HTTPException(404, "kind is pm25 or whatif")
    with db.connect() as conn:
        ds = run_data(run_row(conn, run_id))
    name = "pm25_fire" if kind == "pm25" or "pm25_fire_whatif" not in ds else "pm25_fire_whatif"
    scaled = np.log1p(np.clip(ds[name].values, 0, None)) / np.log1p(field_max(ds))
    return binary(np.round(255 * np.clip(scaled, 0, 1)).astype(np.uint8))


@app.get("/api/tracks/{run_id}.bin")
def tracks(run_id: int):
    """uint16 lon[time, n], uint16 lat[time, n], uint8 state[time, n], uint8 assumed[n].
    lon/lat are scaled 0..65535 over meta.tracks; state 0 = absent, 1 = near ground, 2 = aloft."""
    with db.connect() as conn:
        ds = run_data(run_row(conn, run_id))
    if "track_lon" not in ds:
        return binary(np.zeros(0, dtype=np.uint8))
    w, s = config.WEST - TRACK_PAD, config.SOUTH - TRACK_PAD
    e, n = config.EAST + TRACK_PAD, config.NORTH + TRACK_PAD
    state = ds.track_state.values.copy()
    lon, lat = ds.track_lon.values, ds.track_lat.values
    inside = (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)
    state[~inside] = 0
    qlon = np.where(inside, (lon - w) / (e - w) * 65535, 0).astype(np.uint16)
    qlat = np.where(inside, (lat - s) / (n - s) * 65535, 0).astype(np.uint16)
    blob = qlon.tobytes() + qlat.tobytes() + state.tobytes() + ds.track_assumed.values.tobytes()
    return Response(blob, media_type="application/octet-stream",
                    headers={"Cache-Control": "public, max-age=300"})  # fmt: skip


def _wind_arrays(wind):
    wind = wind.sortby("latitude").sortby("longitude").sortby("isobaricInhPa", ascending=False)
    return wind


@app.get("/api/wind/{level}.bin")
def wind_field(level: str):
    """int8 u[time, lat, lon] then int8 v[time, lat, lon], in m/s x meta.wind.scale."""
    if level not in WIND_LEVELS:
        raise HTTPException(404, f"level is one of {list(WIND_LEVELS)}")
    with db.connect() as conn:
        _, path = latest_cycle(conn)
    w = _wind_arrays(_open(path))
    if level == "10m":
        u, v = w.u10.values, w.v10.values
    else:
        k = WIND_LEVELS[level] - 1
        u, v = w.u.values[:, k], w.v.values[:, k]
    q = np.clip(np.round(np.stack([u, v]) * WIND_SCALE), -127, 127).astype(np.int8)
    return binary(q)


@app.get("/api/pbl.bin")
def pbl():
    """uint8 [time, lat, lon]: boundary-layer height in steps of meta.wind.pbl_step_m."""
    with db.connect() as conn:
        _, path = latest_cycle(conn)
    w = _wind_arrays(_open(path))
    return binary(np.clip(np.round(w.hpbl.values / PBL_STEP_M), 0, 255).astype(np.uint8))


@lru_cache(maxsize=1)
def _boundaries() -> str:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT kind, name, code, note, ST_AsGeoJSON(ST_SimplifyPreserveTopology(
                ST_Intersection(geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326)), 0.02), 4)
            FROM boundary WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            """,
            (config.WEST - 8, config.SOUTH - 8, config.EAST + 8, config.NORTH + 8) * 2,
        ).fetchall()
    feats = [
        {"type": "Feature", "geometry": json.loads(g),
         "properties": {"kind": kind, "name": name, "code": code, "note": note}}
        for kind, name, code, note, g in rows if g
    ]  # fmt: skip
    return json.dumps({"type": "FeatureCollection", "features": feats})


@app.get("/api/boundaries")
def boundaries():
    return Response(_boundaries(), media_type="application/geo+json",
                    headers={"Cache-Control": "public, max-age=86400"})  # fmt: skip


@app.get("/api/fires/{run_id}")
def fires(run_id: int):
    """Fire clusters of the run, column by column (observed fires only)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT cluster_id, round(ST_X(geom)::numeric, 3), round(ST_Y(geom)::numeric, 3), "
            "round(frp_mw::numeric, 1), n_detections, round((emission_kg / 1000)::numeric, 2), "
            "country, region, first_observed, last_observed "
            "FROM sim_cluster WHERE run_id = %s ORDER BY cluster_id",
            (run_id,),
        ).fetchall()
    cols = list(zip(*rows, strict=True)) if rows else [[]] * 10
    keys = ["id", "lon", "lat", "frp_mw", "events", "emission_t", "country", "region"]
    out = {k: [float(x) if hasattr(x, "as_tuple") else x for x in c]
           for k, c in zip(keys, cols[:8], strict=True)}  # fmt: skip
    out["first_seen"] = [iso(t) for t in cols[8]]
    out["last_seen"] = [iso(t) for t in cols[9]]
    return out


@app.get("/api/stations")
def stations():
    """Latest PM2.5 reading per station in the last 7 days, column by column."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (s.source, s.station_id) s.name, round(ST_X(s.geom)::numeric, 3),
                   round(ST_Y(s.geom)::numeric, 3), s.country, s.is_reference, s.provider,
                   r.value, r.observed_at
            FROM station s JOIN station_reading r USING (source, station_id)
            WHERE r.parameter = 'pm25' AND r.observed_at > now() - interval '7 days'
            ORDER BY s.source, s.station_id, r.observed_at DESC
            """
        ).fetchall()
    now = datetime.now(UTC)
    cols = list(zip(*rows, strict=True)) if rows else [[]] * 8
    age = [round((now - t).total_seconds() / 3600, 1) for t in cols[7]]
    return {
        "name": cols[0], "lon": [float(x) for x in cols[1]], "lat": [float(x) for x in cols[2]],
        "country": cols[3], "reference": cols[4], "provider": cols[5],
        "pm25": [round(v, 1) for v in cols[6]], "age_h": age,
        "stale": [a > STALE_HOURS["stations_in"] for a in age],
        "note": "Measured PM2.5 from all pollution sources. Low-cost sensors are less accurate.",
    }  # fmt: skip


def _receptor(name: str) -> tuple[float, float]:
    if name not in config.RECEPTORS:
        raise HTTPException(404, f"receptor is one of {list(config.RECEPTORS)}")
    return config.RECEPTORS[name]


@app.get("/api/receptor/{run_id}/{name}")
def receptor(run_id: int, name: str):
    lat, lon = _receptor(name)
    with db.connect() as conn:
        run = run_row(conn, run_id)
        series = conn.execute(
            "SELECT valid_at, pm25_ugm3, pm25_low, pm25_high, pm25_whatif_ugm3 "
            "FROM sim_receptor_outlook WHERE run_id = %s AND receptor = %s ORDER BY valid_at",
            (run_id, name),
        ).fetchall()
        top = conn.execute(
            """
            SELECT c.cluster_id, c.country, c.region, round(ST_X(c.geom)::numeric, 3),
                   round(ST_Y(c.geom)::numeric, 3), sum(k.pm25_ugm3) AS dose
            FROM sim_contribution k JOIN sim_cluster c USING (run_id, cluster_id)
            WHERE k.run_id = %s AND k.receptor = %s AND k.member = %s AND k.valid_at >= %s
            GROUP BY 1, 2, 3, 4, 5 ORDER BY dose DESC LIMIT 8
            """,
            (run_id, name, run["members"][0], run["started_at"]),
        ).fetchall()
        total = (
            conn.execute(
                "SELECT sum(pm25_ugm3) FROM sim_contribution WHERE run_id = %s AND receptor = %s "
                "AND member = %s AND valid_at >= %s",
                (run_id, name, run["members"][0], run["started_at"]),
            ).fetchone()[0]
            or 0.0
        )
        from sim.engine import Receptor
        from sim.run import observed_now

        obs = observed_now(conn, datetime.now(UTC), Receptor(name, lat, lon))
        cams = conn.execute(
            "SELECT observed_at, pm25_ugm3 FROM cams_forecast WHERE receptor = %s AND "
            "published_at = (SELECT max(published_at) FROM cams_forecast WHERE receptor = %s) "
            "ORDER BY observed_at",
            (name, name),
        ).fetchall()
        city_skill = skill(conn).get(name)
        label = label_for(conn, run)
    ratio = run["params"].get("forward_over_backward", {}).get(name)
    return {
        "name": name, "label": label, "whatif_label": WHATIF_LABEL, "skill": city_skill,
        "started_at": iso(run["started_at"]),
        "self_check": ratio,
        "low_confidence": ratio is None or not AGREE[0] <= ratio <= AGREE[1],
        "times": [iso(r[0]) for r in series],
        "pm25": [round(r[1] or 0, 3) for r in series],
        "low": [round(r[2] or 0, 3) for r in series],
        "high": [round(r[3] or 0, 3) for r in series],
        "whatif": [round(r[4] or 0, 3) for r in series],
        "measured_now": None if not obs else
            {"pm25": round(obs[0]), "stations": obs[1], "reference": obs[2],
             "note": "all pollution sources, last 3 hours"},
        "cams": {"note": "CAMS model forecast of total PM2.5 (Copernicus, via Open-Meteo)",
                 "times": [iso(r[0]) for r in cams], "pm25": [round(r[1], 1) for r in cams]},
        "top_clusters": [
            {"id": r[0], "country": r[1], "region": r[2], "lon": float(r[3]), "lat": float(r[4]),
             "share": round(r[5] / total, 3) if total else 0.0}
            for r in top
        ],
    }  # fmt: skip


@app.get("/api/footprint/{run_id}/{name}.bin")
def footprint(run_id: int, name: str):
    """uint8 [lat, lon]: where the city's air comes from; byte = 255 x sqrt(value / max)."""
    _receptor(name)
    with db.connect() as conn:
        ds = run_data(run_row(conn, run_id))
    f = ds.footprint.sel(receptor=name).values
    return binary(np.round(255 * np.sqrt(f / max(float(f.max()), 1e-30))).astype(np.uint8))


@app.get("/api/ledger/{run_id}")
def ledger(run_id: int):
    """Daily share of each Indian city's simulated fire smoke by source: each Indian state and
    each neighbouring country in one table, same method for both, with the spread across
    forecast members. Inflow to Indian cities only. Observed fires only; UTC days."""
    with db.connect() as conn:
        run = run_row(conn, run_id)
        rows = conn.execute(
            """
            WITH d AS (
                SELECT k.member, k.receptor, (k.valid_at AT TIME ZONE 'UTC')::date AS day,
                       c.country,
                       CASE WHEN c.country = 'IN' THEN coalesce(c.region, 'state unknown')
                            ELSE '' END AS state,
                       sum(k.pm25_ugm3) AS dose
                FROM sim_contribution k JOIN sim_cluster c USING (run_id, cluster_id)
                WHERE k.run_id = %(run)s AND k.valid_at >= %(start)s AND k.pm25_ugm3 > 0
                  AND k.receptor = ANY(%(cities)s)
                GROUP BY 1, 2, 3, 4, 5
            ), s AS (
                SELECT *, dose / sum(dose) OVER (PARTITION BY member, receptor, day) AS share
                FROM d
            )
            SELECT receptor, day, country,
                   max(share) FILTER (WHERE member = %(m0)s), min(share), max(share),
                   count(*), max(dose) FILTER (WHERE member = %(m0)s), state
            FROM s GROUP BY receptor, day, country, state ORDER BY 1, 2, 4 DESC NULLS LAST
            """,
            {"run": run_id, "start": run["started_at"], "m0": run["members"][0],
             "cities": list(config.RECEPTORS)},
        ).fetchall()  # fmt: skip
        regions = conn.execute(
            "SELECT receptor, country, region, sum(dose_ugm3_h) FROM sim_source_share "
            "WHERE run_id = %s AND receptor = ANY(%s) GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC",
            (run_id, list(config.RECEPTORS)),
        ).fetchall()
        skills = skill(conn)
        label = label_for(conn, run)
    n_members = len(run["members"])
    check = run["params"].get("forward_over_backward", {})
    return {
        "label": label, "members": n_members,
        # how far a share moved between the forecast a day ahead and the one on the day,
        # in the 2024 and 2025 hindcasts (percentage points; the larger of the two seasons)
        "revision_points": {k: v["ledger_points"] for k, v in skills.items()},
        "revision_note": "± = how much this share typically changed between the forecast made "
                         "a day ahead and the one made on the day, in hindcasts of 2024 and 2025.",
        "note": "Share of simulated FIRE smoke only (not of all pollution), from fires seen in "
                "the last 3 days, split by Indian state and by neighbouring country with the "
                "same method. Smoke reaching Indian cities only; smoke leaving India is not "
                "computed. Shares from abroad cannot be checked against stations there. "
                "Disputed areas are their own class. Days are UTC days. "
                "Range = spread across forecast runs.",
        "rows": [
            {"receptor": r[0], "day": r[1].isoformat(), "source": r[2], "state": r[8] or None,
             "share": round(r[3], 3) if r[3] is not None else 0.0,
             # a source missing from some forecast run had a zero share there
             "share_min": round(r[4], 3) if r[6] == n_members else 0.0,
             "share_max": round(r[5], 3),
             "low_confidence": not AGREE[0] <= (check.get(r[0]) or 0) <= AGREE[1]}
            for r in rows
        ],
        "regions": [{"receptor": r[0], "source": r[1], "region": r[2], "dose": round(r[3], 3)}
                    for r in regions],
    }  # fmt: skip


@app.get("/api/targeting/{run_id}")
def targeting(run_id: int, hours: int = 48, limit: int = 20):
    """Fire clusters burning now, ranked by simulated population-weighted exposure over the
    next `hours` hours at the tracked Indian cities. Observed fires only; fires in India and
    abroad are ranked by the same rule."""
    with db.connect() as conn:
        run = run_row(conn, run_id)
        rows = conn.execute(
            """
            WITH pop AS (SELECT name, max(pop_max) AS pop FROM place GROUP BY name),
            k AS (
                SELECT k.member, k.cluster_id, k.receptor, sum(k.pm25_ugm3) AS dose
                FROM sim_contribution k
                WHERE k.run_id = %(run)s AND k.valid_at >= %(start)s AND k.valid_at < %(end)s
                  AND k.receptor = ANY(%(cities)s)
                GROUP BY 1, 2, 3
            ), e AS (
                SELECT member, cluster_id, sum(dose * pop) AS exposure,
                       (array_agg(receptor ORDER BY dose * pop DESC))[1] AS top_receptor
                FROM k JOIN pop ON pop.name = k.receptor GROUP BY 1, 2
            ), sh AS (
                SELECT *, exposure / sum(exposure) OVER (PARTITION BY member) AS share FROM e
            )
            SELECT c.cluster_id, c.country, c.region, round(ST_X(c.geom)::numeric, 3),
                   round(ST_Y(c.geom)::numeric, 3), c.frp_mw, c.n_detections, c.last_observed,
                   max(sh.share) FILTER (WHERE sh.member = %(m0)s) AS share,
                   min(sh.share), max(sh.share), count(*),
                   max(sh.top_receptor) FILTER (WHERE sh.member = %(m0)s),
                   max(sh.exposure) FILTER (WHERE sh.member = %(m0)s)
            FROM sh JOIN sim_cluster c ON c.run_id = %(run)s AND c.cluster_id = sh.cluster_id
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
            ORDER BY share DESC NULLS LAST LIMIT %(limit)s
            """,
            {"run": run_id, "start": run["started_at"], "m0": run["members"][0],
             "end": run["started_at"] + timedelta(hours=hours), "limit": limit,
             "cities": list(config.RECEPTORS)},
        ).fetchall()  # fmt: skip
    n_members = len(run["members"])
    return {
        "label": run["label"], "hours": hours,
        "note": "Ranked by simulated fire smoke x city population at the tracked Indian "
                f"cities ({', '.join(config.RECEPTORS)}). Fires in India and abroad are ranked "
                "by the same rule. Shares are of that total. "
                "Range = spread across forecast runs. Small shares are not ranked reliably.",
        "rows": [
            {"rank": i + 1, "id": r[0], "country": r[1], "region": r[2], "lon": float(r[3]),
             "lat": float(r[4]), "frp_mw": round(r[5], 1), "events": r[6],
             "last_seen": iso(r[7]), "share": round(r[8] or 0, 4),
             "share_min": round(r[9], 4) if r[11] == n_members else 0.0,
             "share_max": round(r[10], 4), "most_affected": r[12],
             "exposure_person_ugm3_h": round(r[13] or 0)}
            for i, r in enumerate(rows)
        ],
    }  # fmt: skip


@app.get("/api/cluster/{run_id}/{cluster_id}")
def cluster(run_id: int, cluster_id: int):
    """Where one fire cluster's smoke goes: per tracked city, and how many people live there."""
    with db.connect() as conn:
        run = run_row(conn, run_id)
        c = conn.execute(
            "SELECT country, region, ST_X(geom), ST_Y(geom), frp_mw, n_detections, emission_kg, "
            "emission_frp_kg, first_observed, last_observed FROM sim_cluster "
            "WHERE run_id = %s AND cluster_id = %s",
            (run_id, cluster_id),
        ).fetchone()
        if not c:
            raise HTTPException(404, "No such fire cluster in this run")
        rows = conn.execute(
            """
            WITH tot AS (
                SELECT receptor, sum(pm25_ugm3) AS total FROM sim_contribution
                WHERE run_id = %(run)s AND member = %(m0)s AND valid_at >= %(start)s GROUP BY 1
            )
            SELECT k.receptor, sum(k.pm25_ugm3), max(k.pm25_ugm3),
                   (array_agg(k.valid_at ORDER BY k.pm25_ugm3 DESC))[1], tot.total,
                   sum(COALESCE(k.pm25_assumed_ugm3, 0)),
                   (SELECT max(pop_max) FROM place WHERE name = k.receptor)
            FROM sim_contribution k JOIN tot USING (receptor)
            WHERE k.run_id = %(run)s AND k.member = %(m0)s AND k.cluster_id = %(c)s
              AND k.valid_at >= %(start)s
            GROUP BY k.receptor, tot.total ORDER BY 2 DESC
            """,
            {"run": run_id, "m0": run["members"][0], "c": cluster_id, "start": run["started_at"]},
        ).fetchall()
    cities = [
        {"receptor": r[0], "share_of_city_fire_smoke": round(r[1] / r[4], 4) if r[4] else 0.0,
         "peak_ugm3": round(r[2], 3), "peak_at": iso(r[3]), "population": r[6],
         "whatif_extra_dose": round(r[5], 3)}
        for r in rows
    ]  # fmt: skip
    reached = [x for x in cities if x["share_of_city_fire_smoke"] >= 0.01]
    return {
        "id": cluster_id, "label": run["label"], "country": c[0], "region": c[1],
        "lon": round(c[2], 3), "lat": round(c[3], 3), "frp_mw": round(c[4], 1), "events": c[5],
        "emission_t": {"central": round(c[6] / 1000, 2),
                       "low": round((c[7] or 0) * run["ef_low"] / run["ef_central"] / 1000, 2),
                       "high": round(c[6] * run["ef_high"] / run["ef_central"] / 1000, 2)},
        "first_seen": iso(c[8]), "last_seen": iso(c[9]),
        "cities": cities,
        "people_reached": sum(x["population"] or 0 for x in reached),
        "people_note": "Population of the tracked cities where this cluster gives at least 1% "
                       "of the city's simulated fire smoke over 72 h. Only four cities are "
                       "tracked, so this is a lower bound.",
    }  # fmt: skip


WEB = config.ROOT / "web"
if (WEB / "index.html").exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
