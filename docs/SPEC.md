# SPEC

## 0. Phases (build in this order; don't start a phase before the previous one works)
- **Focus:** India. Receptors (cities we forecast for) are Indian only. Fires in neighbouring countries stay in the simulation as sources, so each city's smoke is split by Indian state and by neighbouring country. Smoke leaving India is out of scope.
- **v1 Smoke map (now), North:** north-west Indo-Gangetic Plain, 26–34°N, 70–82°E (sources: Pakistani Punjab, Indian Punjab, Haryana, Delhi-NCR, UP). Receptors: Delhi, Chandigarh, Amritsar, Lucknow. Live map, targeting table, source ledger, validation.
- **v2 Decisions:** GRAP Ahead model, LLM agent (orders, rules, cited Q&A), claim checker, in-app profile and web push alerts.
- **v3 Scale:** domain widened to all India plus upwind neighbours (6–37°N, 60–100°E). **East:** Kanpur, Patna, Kolkata, Guwahati with the full ledger (sources: upwind Indian states, Nepal, Bangladesh, Myanmar). **West and south:** Mumbai, Ahmedabad, Pune, Hyderabad, Bengaluru, Chennai with the stagnation outlook first; the ledger is shown there only with its validated skill, and marked "fire smoke is a small share here" where the hindcast shows that (don't assume it; measure it). Kafka, Airflow, Databricks hindcasts, kind, bandit, MCP/API for dashboards like World Monitor.

## 1. The map (main product)
Installable web app (PWA), MapLibre GL JS v5 globe + deck.gl v9 (MapboxOverlay or @deck.gl/maplibre). Put text labels in MapLibre layers, not deck.gl TextLayer (it may not render on the globe):
- Animated smoke particles from real fire detections moving with real forecast winds; 72h time slider; 7-day replay.
- Layers: fires (sized by FRP), wind (animated, choose a level), boundary-layer height (stagnation shading), stations (reference vs low-cost marked; stale ones shown as "no recent reading"), simulated PM2.5, borders.
- Click a city: outlook, where its smoke comes from (backward run), ledger split by Indian state and neighbouring country, accuracy. Click a fire cluster: where its smoke goes and how many people it reaches.
- Every simulated layer is labelled "simulated" with its validation skill. Works on mobile.

## 2. Products
1. **Targeting brief (v1):** fire clusters burning now (in India or abroad, labelled by state/country) ranked by simulated contribution to population-weighted exposure in India over the next 48h.
2. **Smoke source ledger (v1):** daily share of each Indian receptor's simulated fire smoke by source: each Indian state and each neighbouring country, in one table, with uncertainty ranges. Note on the ledger: inflow to Indian cities only; outflow from India is not computed.
3. **Stagnation outlook (v1):** per receptor, when calm winds and a low boundary layer will trap local emissions.
4. **GRAP Ahead (v2):** P(each GRAP stage in force) at 24/48/72h and "what it means for you" per user type (construction, fleets, schools/parents, BS-III petrol / BS-IV diesel owners), English and Hindi, in-app and as web push.
5. **Claim checker (v2):** claims from officials and media about where an Indian city's smoke comes from (another state or another country), with sources, rated against the ledger "consistent / not consistent / can't tell", with reasoning; human approval before publishing.
6. **Fire type (v1, Stage 3b):** each fire cluster labelled "likely crop-residue burning", "forest / scrub", "industrial" or "unknown" from land cover and season, with its basis shown. It describes what burned, never who lit it or why; no blame wording, same labels in every country.

**Who uses what:** residents and parents (B2C): the map, city outlook, GRAP Ahead alerts. Businesses (construction, fleets, schools; D2C/B2B): GRAP Ahead per user type and alerts. Officials: targeting brief with fire type, smoke source ledger, stagnation outlook. Alerts go only to people who signed up in the app; nothing is sent to an authority automatically without a named recipient who agreed to it. No advice is shown where validation says "not reliable here".

## 3. Data sources (verified 14 Nov 2026)
- **Forecast weather (simulation):** NOAA GFS 0.25° GRIB2 on AWS `noaa-gfs-bdp-pds` (2021 onward), read with byte-range subsets via the Herbie library (`herbie-data`; search strings like `:UGRD:850 mb`). Fields: UGRD/VGRD at 925/850/700 hPa and 10 m, HPBL (boundary-layer height), TMP. Confirm each field with `H.inventory()` first; if HPBL isn't in `pgrb2.0p25`, check `pgrb2b.0p25`. Archived cycles by init time give point-in-time hindcasts. Don't use `noaa-gfs-pds` (4-week rolling). dynamical.org's GFS Zarr has NO pressure-level winds or boundary-layer height; use it only for quick 10/80/100 m wind display if handy.
- **Fires:** NASA FIRMS area API with a free MAP_KEY (5,000 transactions per 10 min). Use VIIRS_NOAA20_NRT and VIIRS_NOAA21_NRT (S-NPP is being retired); *_SP products for archives. Emissions from FRP with cited factors.
- **India stations, history:** XKDR Forum India Air Quality API/Parquet (hourly CPCB CAAQM + US Embassy monitors since 2009). Free key as a bearer token; CC BY 4.0, so credit "India Air Quality Database, XKDR Forum" plus CPCB and AirNow. Coverage runs to about Mar 2026, so it covers the 2024 and 2025 seasons but NOT live data; check `GET /v1/meta`. Use monthly Parquet files for whole seasons (single queries time out at 90 s).
- **India stations, live:** CPCB's public station feed (official; the portal API is base64-wrapped, so find the stable feed and check its terms), fallback data.gov.in real-time AQI API (free key, hourly, no history). OpenAQ: v3 only and only where fresh; its CPCB feed has been stale since 2026-10-07.
- **Stations abroad:** not ingested (no receptors abroad). Fires abroad come from the same FIRMS feed as Indian fires. Foreign source shares can't be checked against stations in the source area; say so next to them.
- **CPCB gaps:** readings near the 1,000 µg/m³ instrument limit often go missing; treat as censored (≥ limit), not missing.
- **Freshness:** every ingest records the newest timestamp per source; stale data is flagged, never shown as current. Never synthetic. Bias-corrected CAMS may fill the display only if labelled "model-filled", and never in validation.
- **Baseline forecast:** CAMS via the Open-Meteo air-quality API (free, ~10k calls/day non-commercial, points only).
- **Population:** WorldPop grids.
- **Land cover (Stage 3b):** a global cropland / tree-cover map (candidate: ESA WorldCover 10 m; read its licence and confirm before use).
- **GRAP (v2):** CAQM orders and schedules (caqm.nic.in PDFs); thresholds and restrictions per schedule version (revised Dec 2024, Nov 2025, Sep 2026); flag pre-emptive and court-driven orders.
- **Benchmarks:** IITM DSS daily stubble share (ews.tropmet.res.in/dss; no download API found and access has been limited before, so record values from the site or dated news with their source); CAMS. Note: DSS's same-day stubble share uses the previous evening's VIIRS fire counts, and its 3-day stubble forecast uses climatological fire counts and a 2021 emissions inventory. Our live-fire, forecast-wind simulation is different from that; say so clearly, and never claim to replace DSS.
- **Claims (v2):** a curated list of official statements and news URLs about smoke reaching Indian cities; no scraping against terms.

## 4. Simulation
- Lagrangian particles: release from fire clusters, advect with forecast winds, mix within the boundary layer, decay and deposition at cited rates. Forward 72h from fires; backward from receptors.
- Budget: one 72h v1-domain run in ≤10 min on a laptop (cap particles; profile first).
- Uncertainty: ensemble over emission factors and neighbouring forecast cycles; report ranges.
- Physics unit tests: uniform wind, zero wind, boundary-layer cap.

## 5. Validation (gate before any advice)
Hindcast the 2024 and 2025 post-monsoon seasons point-in-time (archived GFS + FIRMS SP + XKDR station data). Compare simulated smoke arrival with observed PM2.5 spikes, CAMS and DSS stubble shares. Report skill per city and per region (North; later East, West, South). Where skill is weak, the app says "not reliable here".

## 6. GRAP Ahead model (v2)
Labels: actual CAQM orders. Features include simulated smoke and stagnation. Baselines: persistence and the CAMS-AQI threshold. Backtest 2024–25 and 2025–26: hits, misses, false alarms, lead time, calibration. If no better than the baselines, ship the best baseline and say so.

## 7. Launch
Share the map with Indian journalists and researchers covering smog, then construction, fleet and parent groups (v2). Track users and weekly accuracy in PROGRESS.md.

## 8. LLM provider (v2)
- Development: NVIDIA API catalog (https://integrate.api.nvidia.com/v1, key NVIDIA_API_KEY). Pick tool-calling models from `ChatNVIDIA.get_available_models()`; start with Nemotron.
- Trial only: no production use, no user data or user questions sent to it; prompts may be used by NVIDIA.
- Save credits: local OCR (Tesseract) first; cache by input hash; batch; back off on 429 errors.
- Before launch: switch live features to a production-allowed option (paid API, paid serverless host of the same open model, or a local model via Ollama) by changing env vars only; re-run the agent eval.
