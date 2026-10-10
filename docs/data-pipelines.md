# FieldWatch: Data & ML Pipelines

Oct 6, 2026 · @Caleb

## Overview and principles

FieldWatch runs five kinds of pipelines, and the rule that ties them together is: **do the heavy, deterministic data work before an investigation starts, so agents only read finished results.** That is what makes the 60-second trigger-to-text budget possible.

| Pipeline | Runs when | Purpose | Datasets |
| --- | --- | --- | --- |
| A. Onboarding context build | Once per farm, refreshed yearly | Precompute everything static about each field | CSB, CDL, SSURGO, DEM, hydrography, wetlands, floodplains, wells, groundwater, past imagery, economics, crop calendar |
| B. Imagery events | A new satellite scene covers a registered field | Detect and characterize anomalies | Sentinel-2 (MVP), Sentinel-1, Landsat, PlanetScope (adapters) |
| C. Hazard events | Storm data, alerts, weekly drought updates | Open watches and pre-attach evidence | MRMS hail, NWS alerts, Drought Monitor, SPC storm reports |
| D. Photo intake | Farmer uploads a photo | Ground-truth evidence for an investigation | Farmer photos + knowledge base |
| E. Knowledge ingestion | At the event from the pre-built source list (T25), refreshed occasionally | Agronomy retrieval for agents | Extension and disease publications |

Design principles:

- **Deterministic compute stays outside the agents.** Raster math, zonal statistics, anomaly scores and hypothesis scoring are plain code. Agents decide what evidence to gather, interpret unstructured input (photos, farmer texts) and explain.
- **Everything is keyed by `field_id`.** Every dataset is reduced to per-field facts at onboarding.
- **Sensor-agnostic imagery.** Each satellite source is an adapter behind one interface; analyzers never know which satellite a scene came from.
- **Replayable.** Demo replay injects a historical scene into the same entry point a live scene uses. Nothing downstream is special-cased.
- **Compute near the data.** Sentinel-2 files live in AWS us-west-2, so the imagery analyzers run there.

&#91;embedded content: data flow · 5 pipelines into shared stores, read by agents\]

Each source feeds one pipeline; every pipeline writes per-field results to the shared stores, and agents read those results through Gateway tools before texting the farmer.

## Shared foundations

One S3 bucket, twelve DynamoDB tables, container-image Lambdas for geospatial work, and Step Functions for fan-out. The implementation specs ("Data model") are the contract for every key, index and TTL; this section summarizes them.

**Storage layout (S3)**

| Prefix | Contents | Format |
| --- | --- | --- |
| `static/` | Nebraska slices of statewide datasets (CSB, CDL, gNATSGO, hydrography, wells) | GeoParquet, COG |
| `fields/{field_id}/geometry.geojson` | Field boundary | GeoJSON |
| `context/{field_id}/` | Per-field rasters and baseline: `soil_zones.tif`, `lowspot.tif`, `baseline.parquet` | COG, Parquet |
| `analysis/{field_id}/{date}/{source}/` | Index rasters, anomaly mask, overlay PNGs (`true_color`, `health`, `mask`), `result.json` | COG, PNG, JSON |
| `hazards/hail/{valid_time}/` | Clipped hail swath raster and PNG | COG, PNG |
| `uploads/{upload_id}/` | Farmer photos and documents (location and time kept, other metadata stripped) | JPEG, PNG, PDF |
| `kb/` | Knowledge-base source documents with metadata sidecars | Text, PDF |

No scene copies are stored: the scene filter passes the STAC item's band links along, and the analyzer reads them in place.

**DynamoDB tables** (all `fw-` prefixed, on-demand)

| Table | Holds |
| --- | --- |
| `fw-farms` | Farm (time zone, quiet hours) and its people (phone, role, call or text, opt-in) |
| `fw-fields` | Geometry pointer, acres, crop, irrigation, pivot geometry, build status |
| `fw-tile-index` | Sentinel-2 tile → fields it covers |
| `fw-field-context` | Soil, terrain, water, wells, economics, stage, baseline summary, drought |
| `fw-scene-analyses` | Scores, regions, pattern features, overlay links |
| `fw-watches` | Open hail, drought and alert watches |
| `fw-investigations` | Status, hypotheses, evidence items, diagnosis, impact |
| `fw-approvals` | Pending actions, decisions, expiry |
| `fw-actions` | Work orders, scout pins, insurance drafts |
| `fw-messages` | Outbound and inbound texts |
| `fw-uploads` | Photo and document status and results |
| `fw-idempotency` | Dedupe keys for retries and duplicate deliveries |

**Compute**

- **Container-image Lambdas** for anything touching GDAL, GRIB2 or large geospatial libraries (zip packages are too small).
- **Step Functions** with a Map state to analyze many fields per scene in parallel.
- **Plain Lambdas** for API calls (weather, drought, economics) and tool handlers.
- **EventBridge** as the internal event bus `fieldwatch` (`SceneAnalyzed`, `AnomalyDetected`, `MrmsFileAvailable`, `WatchOpened`, `ReplyReceived`, `ApprovalRecorded`, `PhotoReceived`, `DocumentReceived`, `ActionCompleted`, investigation progress and message events; full list in the specs' event catalog).

**Regions:** everything in us-west-2 (next to the Sentinel-2 files) except a small MRMS forwarder Lambda in us-east-1. The MRMS notification topic lives in us-east-1, so a forwarder there filters the high-volume stream and relays each relevant file as an event to the us-west-2 bus; the files themselves are small, so the hail filter reads them cross-region. Confirm AgentCore and your Bedrock models are available in us-west-2 before committing to it as the primary region.

**Core libraries:** pystac-client, rasterio, rioxarray, numpy, shapely, geopandas, pyarrow, exactextract (zonal stats), scikit-image (morphology), xarray + cfgrib (GRIB2), duckdb with the spatial extension (querying GeoParquet slices).

## A. Onboarding context pipelines

When a farmer claims their fields, a Step Functions workflow runs A1 first, then A2–A10 in parallel per field, and writes the results to FieldContext. For a demo farm of 6–10 fields it should finish in a few minutes; the slowest step is the imagery baseline (A8).

### A1. Field boundaries (USDA Crop Sequence Boundaries)

1. Before onboarding: load the Nebraska slice of CSB from Source Cooperative's GeoParquet into `static/` (DuckDB filter by state or bounding box).
2. The onboarding map shows CSB polygons near the farmer's address; the farmer taps their fields.
3. Per selected field: repair geometry, compute acres and centroid, and make an **interior buffer** (about 10 m inward) so edge pixels don't mix in road or neighbor signal.
4. Store the 8-year crop rotation history that comes with each polygon.

Output: `fw-fields` rows with geometry, acres, rotation history, and the field's Sentinel-2 tiles in `fw-tile-index`.

### A2. Irrigation and pivot geometry

1. Flag likely pivots by polygon circularity (4π · area / perimeter² close to 1) or a circle-sector shape.
2. Confirm on a mid-season vegetation composite: fit a circle to the green edge to get **center point and radius**; detect corners watered by an end gun.
3. Cross-check with Nebraska DNR registered irrigation wells inside or next to the field (A7).
4. Farmer confirms with one tap ("Is this field irrigated by a center pivot?").

Output: `irrigated` and `pivot` (`center`, `radius_m`) on the Fields item. Corner watering is a later addition, not in the MVP schema.

### A3. Crop type (Cropland Data Layer + phenology)

1. Majority CDL class per field for past years (30 m raster, zonal majority).
2. The current year's CDL generally isn't published until after the season, so for the current season: predict from the CSB rotation (corn–soybean alternation), then confirm from the vegetation curve shape (corn and soybean green up and peak at different times), then ask the farmer if the two disagree.

Output: `crop_current`, `crop_history`.

### A4. Soil profile (SSURGO / gNATSGO)

1. Zonal statistics on the gridded map-unit raster: which soil map units cover the field, and what fraction each.
2. Join map units → components → **horizons** (layers to about 2 m): depth, sand/silt/clay, available water capacity, saturated hydraulic conductivity, organic matter, pH.
3. Derive per map unit: root-zone available water (0–100 cm and 0–150 cm), texture by depth band, drainage class, hydrologic soil group, depth to water table and to restrictive layer.
4. Write a **soil-zones raster** clipped to the field (each pixel tagged with its map unit) so later steps can test whether an anomaly follows soil boundaries.

For a small demo farm, NRCS Soil Data Access queries by polygon are an alternative to the gridded product.

### A5. Terrain (elevation)

1. Pull the best available elevation for the field: USGS 3DEP at about 10 m or finer lidar where it exists (Copernicus 30 m is too coarse to see in-field depressions).
2. Compute slope, depressions (filled DEM minus DEM) and flow accumulation.
3. Save a **low-spot mask** to `context/`.

### A6. Surface water, wetlands and floodplains

1. Spatial joins against USGS hydrography (streams, lakes, ponds), National Wetlands Inventory and FEMA flood hazard zones, within the field plus a buffer.
2. Zonal mean of JRC Global Surface Water occurrence (how often each pixel has historically been under water).
3. Record distances to the nearest stream and water body.

Output: `borders_water`, `nearest_stream_m`, `floodplain_pct`, `wetland_pct`, `historically_wet_pct`.

### A7. Wells and groundwater

1. Nebraska DNR registered wells within the field or about 400 m of it: use, depth, static water level, pumping rate.
2. Nearest USGS groundwater monitoring wells and their recent level trend.
3. The field's Natural Resources District, for allocation rules (context for advice; not used in MVP logic).

### A8. Historical vegetation baseline (the key ML input)

1. STAC search for all Sentinel-2 scenes over the field for the past 5 seasons.
2. Per scene: cloud mask from the scene classification band, then NDVI, NDRE and NDMI per pixel.
3. Aggregate to **zones** (a coarse grid of about 30 m cells, or pivot rings × quadrants for pivots) and to the whole field.
4. Fit the expected seasonal curve per zone and crop from **prior seasons only** (see Models, M1) and save mean and spread by day of year.
5. Store current-season points separately, so a replay only sees scenes dated before the replayed scene (the specs' "as of" rules).
6. Pick a **cohort** of same-crop fields within about 10 km from CSB for current-season comparison.

Output: `context/{field_id}/baseline.parquet` and the `baseline` summary (`key`, `seasons`, `cohort_field_ids`).

A corn–soybean rotation means a field has only 2–3 past seasons of the same crop, so the cohort comparison matters as much as the field's own history.

### A9. Economics

1. NASS Quick Stats, called through the `get_county_stats` Gateway tool (the API key stays in AgentCore Identity): county yields for the field's crop over the last 5 years, irrigated and non-irrigated separately.
2. Current price per bushel from a USDA market source (refreshed daily by a scheduled job).

Output: `expected_yield_bu_ac`, `price_usd_bu`, `price_as_of` and `sources`.

### A10. Crop calendar

1. NASS Crop Progress for Nebraska (weekly percent planted, silking, mature, harvested).
2. Combined with the field's own vegetation curve, estimate the field's current growth stage.

This drives false-alarm suppression: a field turning brown in late September is maturing, not failing.

## B. Imagery event pipeline

A new Sentinel-2 scene wakes the system: Earth Search publishes every new scene's record to a public SNS topic, which you subscribe an SQS queue to. Everything after that is the same code path for live scenes and demo replays.

**B1. Trigger and filter**

1. SNS (Earth Search new item) → SQS `fw-scene-queue` in your account → Scene Filter Lambda.
2. Look up which registered fields fall in the scene's tile (`fw-tile-index`, keyed by the item's `grid:code`, built at onboarding).
3. Drop the scene if no fields match or its overall cloud cover is above 80%; dedupe on the scene ID.
4. Start the Scene Analysis state machine with the matching field list. The per-field cloud check happens in the analyzer: it measures the field's clear fraction and returns `too_cloudy` below `min_clear_fraction`.

**B2. Field Analyzer (one container Lambda per field, run in parallel)**

1. **Read** only the field's window (plus a small buffer) of the bands needed: blue, green, red, red-edge, near-infrared, shortwave infrared, and the scene classification band. Cloud-optimized files make this a few small HTTP range reads, not a full download.
2. **Resample** the 20 m bands (red-edge, SWIR) to 10 m.
3. **Mask** clouds, shadows and snow, and apply the interior buffer from A1.
4. **Compute indices:** NDVI (overall vigor), NDRE (chlorophyll and nitrogen; more sensitive late season), NDMI (canopy water).
5. **Score anomalies** three ways per pixel and per zone:
   - against the field's own expected value for this day of year (z-score, from A8);
   - against this week's same-crop cohort (catches region-wide effects like a heat wave, which should not alarm);
   - change since the last clear scene (catches sudden events like hail).
6. **Build the anomaly mask:** pixels well below expectation and dropping; clean it up with morphology; keep connected regions larger than about 0.5 ha.
7. **Extract pattern features** per region (see Models, M3): ring-ness around the pivot center, wedge concentration, edge-band distance profile, stripe linearity, overlap with the low-spot mask (A5), overlap with sandy soil zones (A4), overlap with any open hail swath (C1).
8. **Render overlays** for the map as PNGs with their geographic bounds: `true_color`, `health` (the crop-health view, from NDVI) and `mask` (the problem area). The true-color vs. health pair is the "invisible to visible" reveal in the demo.
9. **Apply false-alarm gates:** expected senescence for the scene date's growth stage (A10), whole-field drops that match harvest timing, and region-wide cohort drops.
10. **Write** the outputs and `result.json` to `analysis/` and return a FieldAnalysisResult to the state machine.

**B3. Decision**

A small rule-based Lambda reads the analysis, writes the `fw-scene-analyses` row and emits `SceneAnalyzed`. If a region passes the score and size thresholds and no gate is set, it also emits `AnomalyDetected` (field, regions with features, overlays, open watch IDs). The session starter turns that event into a context bundle and starts an investigation (the agent layer).

**B4. Other sensors (adapters behind the same interface)**

| Adapter | Adds | Notes | Priority |
| --- | --- | --- | --- |
| Sentinel-2 | Primary 10 m optical, every few days | Live trigger | MVP |
| Sentinel-1 radar | Sees through clouds; standing water appears very dark in backscatter | Change detection vs. a pre-event scene; Nebraska's flat terrain keeps it simple | Should |
| Landsat Collection 2 | Thermal band: hot canopy relative to air temperature signals water stress | Pair with Open-Meteo air temperature | Later |
| PlanetScope | Near-daily 3 m; pins down the day stress began | Education license: 30-day delay, non-commercial; compare changes within each sensor, not raw values across sensors | Optional |

**B5. Replay mode (demo)**

An API call `POST /v1/replay` with a historical scene ID fetches its STAC item and sends a replay message into the same queue the live topic feeds. It also replays the archived hail files for the 14 days before the scene, so watches exist as they would have. `POST /v1/replay/reset` clears the demo's replay state. The pitch can say, honestly, "we're replaying the July 14th satellite pass; everything after this runs live."

## C. Hazard event pipelines

Hazard pipelines don't diagnose anything on their own. They open **watches** that make the imagery pipeline more sensitive for those fields and pre-attach evidence, so the next clear satellite pass can confirm or rule out damage.

### C1. Hail (NOAA MRMS)

1. MRMS publishes new-file notifications on an SNS topic in us-east-1 → the MRMS forwarder Lambda (us-east-1) → `MrmsFileAvailable` on the us-west-2 bus → Hail Filter Lambda.
2. The forwarder keeps only the hourly maximum-hail-size files (`CONUS/MESH_Max_60min_00.50/`) and drops the rest of the high-volume MRMS suite by key prefix; replay backfill uses the daily files (`CONUS/MESH_Max_1440min_00.50/`).
3. Decompress the GRIB2 file, read it with cfgrib, clip to the bounding box of all registered farms.
4. Per field: maximum hail size and the share of the field above the threshold (default 1 inch).
5. Above threshold: open a hail watch (expires after 14 days), save the clipped swath raster and PNG for the map, and emit `WatchOpened`; optionally send a heads-up text ("Radar suggests hail over the east 80 last night. I'll check the next clear satellite image.").
6. While the watch is open, the Field Analyzer lowers its threshold for that field and tests overlap between the anomaly and the swath.

For the demo replay, you need archived MESH for the event date. Check how far back the AWS bucket retains files; if it doesn't reach July, find an archive source this week.

### C2. NWS alerts (should-have)

EventBridge Scheduler polls the NWS active-alerts API for Nebraska every few minutes (send a User-Agent header). Severe thunderstorm and tornado warning polygons that intersect a field open a watch.

### C3. Drought Monitor

A weekly scheduled job pulls the county drought category for each farm's county. A change to D2 or worse opens a drought watch; the category is also stored in FieldContext as evidence for water-stress hypotheses.

### C4. SPC storm reports (should-have)

A daily job pulls the previous day's hail and wind reports. Reports within about 10 km of a field are attached to that field's open watch as ground-truth corroboration of the radar.

## Investigation-time tools

Agents never touch raw datasets. They call these tools through AgentCore Gateway, and almost all of them read precomputed results, which keeps each call fast. Read tools are always allowed. Write tools that act on the farmer's behalf need an approval, checked by the Gateway's Cedar policy and again in the tool's Lambda. Messaging tools need no approval but are limited by quiet hours and rate. Exact inputs and outputs are in the implementation specs ("Gateway tools").

| Tool | Reads | Typical latency | Access |
| --- | --- | --- | --- |
| `get_field_context` | FieldContext (soil, terrain, water, wells, economics, stage) | Under 100 ms | Read |
| `get_scene_analysis` | SceneAnalyses: scores, regions, pattern features, overlay links | Under 100 ms | Read |
| `get_index_history` | Baseline curves + recent scenes for a field or zone | Under 1 s | Read |
| `compare_to_cohort` | Same-crop neighbor fields, same scene | Under 1 s | Read |
| `get_weather` | Open-Meteo history: rain, temperature, wind, reference ET (cached per field and day) | 1–2 s | Read |
| `get_hazards` | Open watches: hail swaths, alerts, storm reports | Under 100 ms | Read |
| `get_drought` | Drought Monitor county statistics for a date range (OpenAPI target, no Lambda) | Under 1 s | Read |
| `get_county_stats` | NASS Quick Stats county records (OpenAPI target; key injected from AgentCore Identity) | Under 1 s | Read |
| `test_overlap` | Overlap of an anomaly region with soil zones, low spots or a hail swath (statistic + p-value) | 1–3 s | Read |
| `search_agronomy` | Knowledge base, filtered by crop, growth stage, condition | 1–3 s | Read |
| `get_disease_reports` | County disease confirmations for the season | Under 1 s | Read |
| `estimate_impact` | Economics model (M5) for a region, condition and days until fixed | Under 1 s | Read |
| `score_hypotheses` | Condition catalog + evidence so far (M4) | Under 1 s | Read |
| `request_photo` | Creates an upload link pinned to coordinates and texts it | 1–2 s | Write, no approval |
| `send_farmer_message` | SMS (or in-app phone fallback) | 1–2 s | Write, policy-limited (quiet hours, rate) |
| `create_work_order` | Work order record | Under 1 s | Write, needs approval |
| `draft_insurance_notice` | Prefilled notice-of-damage draft for the farmer to send | Under 1 s | Write, needs approval |
| `create_scout_pin` | Coordinates + what to look for, sent to the farmer | Under 1 s | Write, needs approval |

AgentCore Code Interpreter covers anything ad hoc an agent decides it needs (for example, plotting this field's curve against last year's). AgentCore Observability traces of these calls are what the evidence trail in the UI is built from.

## D. Photo intake pipeline

Photos turn "I can't tell from space" into ground truth. The agent requests one when imagery can't separate its top hypotheses, and the farmer can also send one unprompted.

1. **Request:** `request_photo` creates a short-lived upload link tied to the investigation and a map pin, then texts it: "Could you snap a photo of a leaf near this spot?"
2. **Capture:** a mobile web page (no app) opens the phone camera, records the browser's location and timestamp, and uploads directly to `uploads/{upload_id}/` with a presigned URL.
3. **Intake Lambda** (S3 event):
   - validate file type and size; strip metadata except location and time; convert HEIC to JPEG;
   - downscale for the model (1,568 px on the long side);
   - check sharpness (variance of the Laplacian); if blurry, mark the upload `retake` so the still-open upload page asks for another photo right away;
   - record the photo's distance to the anomaly region.
4. **Analysis:** a multimodal Bedrock model gets the photo plus retrieved disease and symptom guides for this crop, growth stage and county reports. It must return structured output: plant part, crop, visible symptoms, candidate conditions each with confidence and the visual evidence for it, and a suggested follow-up.
5. **Evidence:** the result becomes an evidence item on the investigation (`PhotoReceived` event), and the supervisor agent resumes and re-scores the hypotheses.

Wording rule: results say "consistent with," never a definitive diagnosis, and suggest the UNL Plant & Pest Diagnostic Clinic for confirmation.

## E. Knowledge ingestion pipeline

A Bedrock Knowledge Base gives agents grounded agronomy answers; the condition catalog (Models, M4) is separate structured data, not RAG.

1. **Collect:** Nebraska Extension CropWatch articles and NebGuides, plus Crop Protection Network disease pages, for corn and soybean: diseases, pests, hail and water-stress damage, nitrogen, irrigation troubleshooting. Check each source's reuse terms, and keep the source URL for every document.
2. **Tag metadata** per document: crop, topic, condition, growth stage, region, year.
3. **Ingest** into the Knowledge Base with an S3 Vectors store so retrieval can pre-filter by metadata (for example, only corn, only reproductive stages).
4. **Retrieve** through `search_agronomy`; answers that reach the farmer cite the publication link.

Keep the corpus small and on-topic: a few dozen well-chosen documents beat a thousand loosely related ones.

## Models

No model needs training or fine-tuning for the MVP. The ML is statistical baselines, geospatial feature engineering and a deterministic evidence scorer, with foundation models doing perception (photos, anomaly maps) and explanation.

| Model | Does | Method | Trained? |
| --- | --- | --- | --- |
| M1 Seasonal baseline | Expected index value per zone and day of year | Curve fit over past seasons of the same crop | Fit per field, no labels |
| M2 Anomaly scorer | How unusual this scene is | Z-scores vs. baseline and cohort, plus change since last scene | No |
| M3 Pattern features + rules | The shape of the stress | Geometry and overlap statistics, then rules | No |
| M4 Hypothesis scorer | Ranked conditions with confidence | Evidence-weighted log-odds from the condition catalog | No (weights set by hand) |
| M5 Impact model | Dollar estimate with a range | Area × yield × loss fraction × price | No |
| M6 Vision | Photo and anomaly-map interpretation | Multimodal Bedrock model + retrieval | No |
| M7 Pattern classifier (optional) | Learned version of M3's rules | Gradient-boosted trees on M3 features | Only if labeled examples turn up |

**M1. Seasonal baseline.** For each zone, fit a smooth seasonal curve (a double-logistic or a few harmonic terms) to past same-crop seasons and keep the residual spread by day of year. With only 2–3 same-crop seasons per field, shrink each zone toward the field mean and the cohort when history is thin.

**M2. Anomaly scorer.** For pixel or zone i at day t:

```latex
z_i(t) = \frac{x_i(t) - \hat{x}_i(t)}{\hat{\sigma}_i(t)}
```

and combine it with the cohort deviation and the change since the last clear scene into one region score. Thresholds are tuned by hand at the event, on the real examples found this week.

**M3. Pattern features.** Per anomaly region: radial profile around the pivot center (ring), angular concentration (wedge), distance-to-edge profile (edge band), elongation and line fit (stripes), and overlap with low spots, sandy soil zones and hail swaths. Rules map feature combinations to condition likelihoods. A vision model looking at the rendered anomaly map gives a second opinion, recorded as separate evidence.

**M4. Hypothesis scorer.** Each condition in the catalog has a prior (by crop, stage, irrigation, season) and a weight for each kind of evidence. Each evidence item updates the condition's log-odds:

```latex
\log O(c \mid e_1, \dots, e_n) = \log O(c) + \sum_{k=1}^{n} w_{c,k}
```

Agents choose which evidence to gather next and convert unstructured input (a photo, a farmer's reply) into evidence items; the numbers come from this scorer, so confidence is reproducible and auditable rather than invented by an LLM.

**M5. Impact model.**

```latex
\text{loss} = A \times Y \times f(\text{condition}, \text{stage}, \text{severity}, \text{days}) \times P
```

A = affected acres (from the anomaly region), Y = expected yield (A9), P = price (A9), and f = loss fraction from published guidance (hail defoliation tables by growth stage, water-stress loss around silking). Report a low–high range, not a single number.

**M6. Vision.** A multimodal Bedrock model with structured output, grounded by retrieval (D, E). No training.

**Why no fine-tuning:** there's no labeled data for these conditions, fine-tuning and serving a custom model takes hours and extra setup, the hard part (reasoning across evidence) is handled better by tools plus retrieval, and judges score the demo rather than the training.

## Hackathon build order and checks this week

Build along the demo's critical path first: one field, one replayed scene, one anomaly, end to end. Breadth comes after the loop works.

1. Field registry from CSB for the demo farm (A1) and pivot geometry (A2).
2. Baseline for the demo fields (A8), started early because it's the slowest step.
3. Field Analyzer on the hero scene (B2), including the true-color and NDVI overlays.
4. Replay trigger (B5) and the decision step (B3).
5. Context pipelines needed by the hero diagnosis: soil (A4), terrain (A5), economics (A9), crop stage (A10).
6. Hail pipeline (C1) and Drought Monitor (C3).
7. Tools (Investigation-time tools) and the knowledge base (E).
8. Photo intake (D).
9. Live Sentinel-2 SNS subscription (B1), water layers (A6), wells (A7), other sensors (B4).

Verify this week, by hand (browser, CLI or docs, no app code):

- [ ] Pull the Nebraska slice of CSB and confirm your family's fields are in it as separate polygons
- [ ] Find cloud-free Sentinel-2 scenes over those fields this summer, and a real stress event worth replaying
- [ ] Ask your family what went wrong on the farm this summer (hail date, pivot trouble, dry spells)
- [ ] Confirm MRMS MESH archive retention covers the event date; find an archive if not
- [ ] Confirm the Earth Search SNS topic accepts a subscription from your account
- [ ] Check that Soil Data Access, Open-Meteo history, Drought Monitor and NASS Quick Stats return data for your county
- [ ] Check 3DEP elevation resolution available for the farm
- [ ] Confirm AgentCore and your Bedrock models are available in us-west-2
- [ ] Check whether the account has an active US origination identity (the SMS sandbox still needs one; toll-free registration can take up to 15 business days); plan the in-app phone mirror regardless
- [ ] Apply for Planet's Education and Research program (optional; ask about non-commercial terms)
- [ ] Ask the organizers whether downloading public datasets before the event is allowed

## Sources

- [USDA Crop Sequence Boundaries on Source Cooperative](https://source.coop/fiboa/us-usda-cropland)
- [USDA NASS Crop Sequence Boundaries metadata](https://data.nass.usda.gov/Research_and_Science/Crop-Sequence-Boundaries/metadata_Crop-Sequence-Boundaries-2025.htm)
- [Element 84 Earth Search](https://github.com/Element84/earth-search)
- [Sentinel-2 Cloud-Optimized GeoTIFFs on the AWS Registry of Open Data](https://registry.opendata.aws/sentinel-2-l2a-cogs/)
- [NOAA MRMS on the AWS Registry of Open Data](https://registry.opendata.aws/noaa-mrms-pds/)
- [MRMS Maximum Estimated Size of Hail product guide](https://vlab.noaa.gov/web/wdtd/-/maximum-estimated-size-of-hail-mes-2)
- [Planet Education and Research program](https://planet.com/markets/education-and-research/)
