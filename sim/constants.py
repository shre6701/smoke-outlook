"""Constants and assumptions for the smoke model. Each has a source or says "assumption"."""

# Fire radiative energy -> dry biomass burned: 0.368 kg per MJ.
# Wooster, Roberts, Perry & Kaufman (2005), J. Geophys. Res. 110, D24311.
BIOMASS_KG_PER_MJ = 0.368

# PM2.5 emission factor for crop-residue burning, g per kg dry matter.
# Low: Akagi et al. (2011), Atmos. Chem. Phys. 11, 4039 (crop residue, 6.26).
# High: Andreae (2019), Atmos. Chem. Phys. 19, 8523 (agricultural residue, about 8).
EF_PM25_LOW = 6.26
EF_PM25_HIGH = 8.0
EF_PM25_CENTRAL = (EF_PM25_LOW + EF_PM25_HIGH) / 2

# Assumption: each satellite detection stands for one hour of burning at the observed power.
# A polar orbiter sees a fire for an instant; small crop fires are short. Large uncertainty.
BURN_SECONDS = 3600.0

# Dry deposition velocity of fine particles, m/s (0.1 cm/s; typical accumulation-mode value,
# Seinfeld & Pandis, Atmospheric Chemistry and Physics, 3rd ed. 2016, ch. 19).
DRY_DEPOSITION_M_S = 0.001

# Assumption: sub-grid horizontal mixing as a random walk, m²/s.
K_HORIZONTAL_M2_S = 1000.0

# Heights of the wind levels: 10 m, then 925 / 850 / 700 hPa in the ICAO standard atmosphere.
# Assumption: used as height above ground (true over the plains, wrong over mountains).
LEVEL_HEIGHTS_M = (10.0, 762.0, 1457.0, 3012.0)

HPBL_MIN_M = 50.0  # floor for the boundary-layer height (avoids dividing by ~0 at night)
M_PER_DEG = 111_320.0
