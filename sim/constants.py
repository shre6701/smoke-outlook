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

# ---- PM2.5 emission factor for crop-residue burning, g per kg dry matter ----
# Low: Akagi et al. (2011), Atmos. Chem. Phys. 11, 4039 (crop residue, 6.26).
# Central: mean of Akagi (6.26) and Andreae (2019), Atmos. Chem. Phys. 19, 8523 (about 8).
# High: FINN v2.5 crops (13.4; Wiedinmyer et al. 2023, Table 4).
EF_PM25_LOW = 6.26
EF_PM25_CENTRAL = (6.26 + 8.0) / 2
EF_PM25_HIGH = 13.4

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
