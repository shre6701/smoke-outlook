"""Country, region and disputed-area boundaries from Natural Earth (public domain).

Disputed areas are their own class: a point inside one is tagged "disputed" with the area's
Natural Earth name, never as part of either country. Same rule on every side of every border.
"""

import json

from ingest import config
from ingest.http import get

BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
# kind -> (file, name property, code property, note property)
FILES = {
    "country": ("ne_10m_admin_0_countries", "NAME", "ISO_A2", None),
    "region": ("ne_10m_admin_1_states_provinces", "name", "iso_a2", None),
    "disputed": ("ne_10m_admin_0_disputed_areas", "BRK_NAME", None, "NOTE_BRK"),
}
PLACES = "ne_10m_populated_places_simple"
PAD = 4.0
DIR = config.DATA_DIR / "boundaries"


def _bbox(geom: dict) -> tuple[float, float, float, float]:
    def points(c):
        if isinstance(c[0], int | float):
            yield c
        else:
            for part in c:
                yield from points(part)

    xs, ys = zip(*[(pt[0], pt[1]) for pt in points(geom["coordinates"])], strict=True)
    return min(xs), min(ys), max(xs), max(ys)


def near_domain(geom: dict) -> bool:
    w, s, e, n = _bbox(geom)
    return not (
        e < config.WEST - PAD or w > config.EAST + PAD
        or n < config.SOUTH - PAD or s > config.NORTH + PAD
    )  # fmt: skip


def ingest(conn) -> tuple[str, int, str]:
    have = conn.execute("SELECT (SELECT count(*) FROM boundary), (SELECT count(*) FROM place)")
    n_boundary, n_place = have.fetchone()
    if n_boundary and n_place:
        return "ok", 0, "already loaded"
    conn.execute("DELETE FROM boundary")
    conn.execute("DELETE FROM place")
    DIR.mkdir(parents=True, exist_ok=True)
    new = 0
    with conn.cursor() as cur:
        for kind, (name, p_name, p_code, p_note) in FILES.items():
            path = DIR / f"{name}.geojson"
            if not path.exists():
                path.write_bytes(get(BASE + path.name, timeout=300).content)
            for f in json.loads(path.read_text())["features"]:
                geom, props = f.get("geometry"), f["properties"]
                if not geom or not near_domain(geom):
                    continue
                cur.execute(
                    "INSERT INTO boundary (kind, name, code, note, geom) VALUES "
                    "(%s, %s, %s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))",
                    (kind, props.get(p_name), props.get(p_code) if p_code else None,
                     props.get(p_note) if p_note else None, json.dumps(geom)),
                )  # fmt: skip
                new += 1
    path = DIR / f"{PLACES}.geojson"
    if not path.exists():
        path.write_bytes(get(BASE + path.name, timeout=300).content)
    places = 0
    with conn.cursor() as cur:
        for f in json.loads(path.read_text())["features"]:
            lon, lat = f["geometry"]["coordinates"][:2]
            props = f["properties"]
            if not near_domain({"coordinates": [lon, lat]}) or not props.get("pop_max"):
                continue
            cur.execute(
                "INSERT INTO place (name, pop_max, geom) VALUES (%s, %s, "
                "ST_SetSRID(ST_MakePoint(%s, %s), 4326))",
                (props.get("name"), int(props["pop_max"]), lon, lat),
            )
            places += 1
    conn.commit()
    return "ok", new + places, f"{new} Natural Earth shapes and {places} places near the domain"
