"""Constants and assumptions for the smoke model. Each has a source or says "assumption"."""

# ---- Emissions, method A (central and high): burned area per detection ----
# FINN v2.5 (Wiedinmyer et al. 2023, Geosci. Model Dev. 16, 3873): each VIIRS 375 m detection
# is given 0.14 km² of burned area; crop fuel loading 902 g/m² (their Table 2).
# Assumption: all of that fuel burns (upper bound; FINN's own burn fraction was not checked).
PIXEL_AREA_M2 = 0.14e6
CROP_FUEL_KG_M2 = 0.902

# ---- Emissions, method B (low): fire radiative power ----
# Fire radiative energy -> dry biomass burned: 0.368 kg per MJ.
# Wooster, Roberts, Perry & Kaufman (2005), J. Geophys. Res. 110, D24311.
BIOMASS_KG_PER_MJ = 0.368
# Assumption: each detected fire burns for one hour at the observed power. A polar orbiter
# sees a fire for an instant, and most short crop fires are never seen: a known underestimate.
BURN_SECONDS = 3600.0

# ---- Daytime fires: when they burn, and how many the satellites miss ----
# The VIIRS satellites pass at about 13:30 local time. Geostationary images (GK2A-AMI) show
# that crop fires in Punjab and Haryana start around 12:00 IST, peak at 16:00-17:00 IST and
# die down through 17:00-18:00 IST in 2024 and 2025, and that VIIRS missed about 56% (2024)
# and 64% (2025) of the fire detections. Jethva (2026), Remote Sens. 18(10), 1506,
# doi:10.3390/rs18101506 (CC BY). A transport model with the emission peak moved to late
# afternoon had a smaller Delhi bias in 2024: Biswal et al. (2025), Environ. Sci.: Atmos. 5,
# 1211, doi:10.1039/d5ea00052a.
# Assumption: a fire seen by day stands for burning spread over that afternoon as a triangle
# (start, peak, end below), and for the unseen fires near it. One factor for all years and
# all places (the evidence is for Indian Punjab and Haryana only; one factor everywhere
# leaves the ledger shares unchanged). Hindsight: this was published in 2026, after the
# seasons used for validation.
IST_OFFSET_S = 19800.0  # UTC+5:30
DAY_FIRE_HOURS_IST = (9.0, 18.0)  # a detection in these local hours is a "day" fire
DAY_BURN_IST = (12.0, 16.5, 18.5)  # start, peak, end of the afternoon burning, hours IST
LATE_FIRE_MISS = (0.56 + 0.64) / 2  # share of daytime fires the 13:30 pass does not see
LATE_FIRE_MISS_RANGE = (0.56, 0.64)
DAY_FIRE_FACTOR = 1.0 / (1.0 - LATE_FIRE_MISS)  # 2.5: emission per detected day fire
EMISSION_SLICE_S = 1800.0  # emissions are handed to the model in half-hour pieces

# ---- PM2.5 emission factor for crop-residue burning, g per kg dry matter ----
# Low: Akagi et al. (2011), Atmos. Chem. Phys. 11, 4039 (crop residue, 6.26).
# Central: mean of Akagi (6.26) and Andreae (2019), Atmos. Chem. Phys. 19, 8523 (about 8).
# High: FINN v2.5 crops (13.4; Wiedinmyer et al. 2023, Table 4).
EF_PM25_LOW = 6.26
EF_PM25_CENTRAL = (6.26 + 8.0) / 2
EF_PM25_HIGH = 13.4

# ---- How long smoke is followed ----
# Assumption: smoke up to 5 days old still counts at a city (was 3-4 days). Fine particles
# stay in the air for several days when there is no rain, and still post-monsoon spells in
# north India last about that long. To be checked against a 7-day run on sample days.
SMOKE_LOOKBACK_H = 120  # fires this far back are sources
SMOKE_MAX_AGE_H = 120  # air older than this is no longer followed back from a city
FIRE_REPEAT_H = 72  # what-if for coming days repeats the mean of the last 3 days (backtest)

# Dry deposition velocity of fine particles, m/s (0.1 cm/s; typical accumulation-mode value,
# Seinfeld & Pandis, Atmospheric Chemistry and Physics, 3rd ed. 2016, ch. 19).
DRY_DEPOSITION_M_S = 0.001

# Assumption: sub-grid horizontal mixing as a random walk, m²/s.
K_HORIZONTAL_M2_S = 1000.0

# Fallback heights of the wind levels (10 m, then 925 / 850 / 700 hPa in the ICAO standard
# atmosphere), used only when a weather file has no geopotential height.
LEVEL_HEIGHTS_M = (10.0, 762.0, 1457.0, 3012.0)
# Where terrain is above a pressure level, the level is placed just above the ground.
MIN_LEVEL_HEIGHTS_M = (20.0, 30.0, 40.0)

HPBL_MIN_M = 50.0  # floor for the boundary-layer height (avoids dividing by ~0 at night)
M_PER_DEG = 111_320.0
