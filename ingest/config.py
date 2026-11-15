"""Stage 1 settings: v1 domain, receptors, freshness limits."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

# v1 domain (SPEC §0): west, south, east, north
WEST, SOUTH, EAST, NORTH = 70.0, 26.0, 80.0, 34.0

# Receptors are Indian cities only (SPEC §0). Fires anywhere in the domain are sources.
RECEPTORS = {
    "Delhi": (28.61, 77.21),
    "Chandigarh": (30.73, 76.78),
    "Amritsar": (31.63, 74.87),
}
# Reporting region of each receptor (SPEC §0: North now; East, West, South in v3).
REGION = dict.fromkeys(RECEPTORS, "North")

FIRMS_SOURCES = ["VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]

# A source is flagged STALE when its newest timestamp is older than this (hours).
STALE_HOURS = {
    "gfs": 14,  # cycles every 6 h, complete about 5 h after init
    "firms": 24,  # each satellite passes about twice a day
    "stations_in": 6,
    "cams": 24,  # age of our newest fetch
    "datagov": 6,
}


# Stations are ingested for India only: there are no receptors abroad to compare with.
STATION_COUNTRY = "IN"


def in_domain(lat: float, lon: float) -> bool:
    return SOUTH <= lat <= NORTH and WEST <= lon <= EAST
