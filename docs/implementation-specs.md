# FieldWatch: Implementation Specifications

Oct 6, 2026 · @Caleb

Every component's contract in one place: what it accepts, what it returns or emits, and the rules it follows, so team members can build in parallel on Saturday and plug together without surprises. Version 1; anything marked "confirm" depends on an AWS API detail to check this week.

## Conventions

These apply to every interface below. Interfaces are described as field tables, not code: at the event, ticket T04 writes the TypeScript types and generates the Pydantic models from these tables. In the tables, `?` after a field name means optional, "list of X" is an array, and "one of" lists the allowed string values.

| Topic | Rule |
| --- | --- |
| IDs | Prefixed ULIDs: `farm_`, `per_` (person), `fld_`, `inv_` (investigation), `anm_` (anomaly), `apr_` (approval), `act_` (action), `msg_`, `upl_` (upload), `wch_` (watch). Scene IDs are the STAC item ID unchanged |
| Time | ISO 8601 UTC strings (`2026-07-14T17:21:05Z`); scene dates as `YYYY-MM-DD`; each farm stores its IANA time zone for quiet hours |
| Geometry | GeoJSON in EPSG:4326, coordinates `[lon, lat]`; raster bounds as `[west, south, east, north]` |
| Units | Area in acres, distance in meters, yield in bu/ac, money in USD as numbers with two decimals; every number that came from a dataset carries its source and date nearby |
| Versioning | Every event detail and request body has `schema_version: "1"`; consumers reject unknown major versions |
| Errors | HTTP and tool errors use `{ error: { code, message, retryable } }`; codes listed per component |
| Idempotency | Events and writes carry a deterministic key (for example `field_id#scene_id`); writes use conditional puts on a table's primary key or on fw-idempotency, never on a GSI (which can't enforce uniqueness), so a retry is a no-op |
| Auth: internal | IAM roles per function; AgentCore Gateway accepts calls only from the Runtime's role |
| Auth: farmer links | Signed tokens (`t` query parameter): HMAC over `{farm_id, scope, subject_id, exp}`; scopes `onboard`, `report`, `upload`, `work_order` |
| Auth: dashboard | Operator passcode exchanged for a short-lived bearer token (Cognito later) |
| Timeouts | Lambdas default to 30 s; exceptions are listed per component |
| Auth: external APIs | API keys live in AgentCore Identity credential providers and are injected by the Gateway; no Lambda or agent stores them |

## Shared types

Built at the event (T04) in one shared package (`fieldwatch-schemas`) and imported everywhere.

**Basic formats**

| Name | Format |
| --- | --- |
| ISOTime | ISO 8601 UTC string, e.g. `2026-07-14T17:21:05Z` |
| ISODate | `YYYY-MM-DD` |
| Bounds | Four numbers: west, south, east, north |
| FieldRef | `farm_id` (string), `field_id` (string) |
| Crop | One of `corn`, `soybeans`, `other` |
| ConditionId | One of `pivot_failure`, `hail`, `drought`, `ponding`, `nitrogen`, `herbicide_drift`, `planter_skip`, `disease_pest`, `unknown` |

**PatternFeatures** (all numbers 0–1)

| Field | Type | Meaning |
| --- | --- | --- |
| `ring_score` | number | Stress concentrated on a ring around the pivot center |
| `wedge_score` | number | Concentrated in one angular sector |
| `edge_band_score` | number | Concentrated along one field edge |
| `stripe_score` | number | Straight parallel bands |
| `lowspot_overlap` | number | Share of the region inside the low-spot mask |
| `sandy_soil_overlap` | number | Share inside sandy soil zones |
| `hail_swath_overlap` | number or null | Null when there is no open hail watch |

**AnomalyRegion**

| Field | Type | Notes |
| --- | --- | --- |
| `region_id` | string | |
| `geometry` | GeoJSON Polygon | |
| `area_acres` | number | |
| `centroid` | [lon, lat] | |
| `score` | number | Combined anomaly score; higher is more unusual |
| `features` | PatternFeatures | |

**OverlayRef:** `s3_key` (string), `bounds` (Bounds).

**EvidenceItem**

| Field | Type | Notes |
| --- | --- | --- |
| `evidence_id` | string | |
| `hypothesis` | ConditionId | |
| `direction` | one of `supports`, `refutes` | |
| `weight_key` | string | Key into the condition catalog's weight table |
| `source` | string | Tool name, `photo`, `document` or `farmer_reply` |
| `value` | any JSON | The raw fact, e.g. rain over the last 30 days in mm |
| `reason` | string | One plain-language sentence |
| `created_at` | ISOTime | |

**Hypothesis**

| Field | Type | Notes |
| --- | --- | --- |
| `condition` | ConditionId | |
| `probability` | number | From the scorer, never from an LLM |
| `status` | one of `open`, `likely`, `ruled_out` | |

**ImpactEstimate**

| Field | Type | Notes |
| --- | --- | --- |
| `affected_acres` | number | |
| `low_usd`, `high_usd` | number | The dollar range |
| `basis` | string | Inputs in words, e.g. yield, price and loss share |

**ProposedAction**

| Field | Type | Notes |
| --- | --- | --- |
| `action_type` | one of `work_order`, `scout_pin`, `insurance_notice`, `none` | |
| `summary` | string | |
| `assignee_person_id?` | string | |

**ErrorBody:** an `error` object with `code` (string), `message` (string) and `retryable` (boolean).

## Event catalog

All events go on one custom EventBridge bus, `fieldwatch`. Source is `fieldwatch.<component>`; the detail-type is the event name. Register each schema in the EventBridge schema registry so consumers can generate bindings.

| Event | Producer | Consumers | Idempotency key |
| --- | --- | --- | --- |
| `SceneAnalyzed` | Scene analysis | Projector (dashboard) | `field_id#scene_id` |
| `AnomalyDetected` | Decision | Session starter, projector | `field_id#date` |
| `MrmsFileAvailable` | MRMS forwarder (us-east-1) | Hail filter | S3 key |
| `WatchOpened` | Hail filter, pollers | Projector; read by field analyzer | `watch_id` |
| `InvestigationProgress` | Agent service | Projector | `investigation_id#seq` |
| `InvestigationConcluded` | Agent service | Projector | `investigation_id` |
| `ReplyReceived` | Reply handler | Session starter (resume), projector | provider message ID |
| `ApprovalRecorded` | Reply handler, web API | Session starter (resume), projector | `approval_id` |
| `PhotoReceived`, `DocumentReceived` | Intake | Session starter (resume), projector | `upload_id` |
| `ActionCompleted` | Web API (work order done) | Session starter, projector | `action_id` |
| MessageSent, MessageReceived | Send Lambda, reply handler | Projector (phone mirror) | message\_id |

Every event detail carries the two **EventBase** fields, `schema_version` (always `"1"`) and `occurred_at` (ISOTime), plus the fields below.

| Event | Fields |
| --- | --- |
| `SceneAnalyzed` | `field` (FieldRef), `scene_id`, `source`, `date` (ISODate), `status` (one of `ok`, `too_cloudy`, `no_baseline`, `error`), `max_score`, `region_count`, `analysis_key` |
| `AnomalyDetected` | `anomaly_id`, `field`, `scene_id`, `date`, `regions` (list of AnomalyRegion), `overlays` (`true_color`, `health`, `mask`, each an OverlayRef), `open_watch_ids` (list of string), `replay` (boolean) |
| `MrmsFileAvailable` | `bucket`, `key`, `product`, `valid_time` (ISOTime) |
| `WatchOpened` | `watch_id`, `type` (one of `hail`, `drought`, `nws_alert`), `fields` (list of FieldRef), `detail` (free-form object, e.g. max hail size and swath key), `expires_at` |
| `InvestigationProgress` | `investigation_id`, `seq` (number), `agent` (one of `supervisor`, `imagery`, `environment`, `agronomy`), `step` (one of `plan`, `tool_call`, `evidence`, `score`, `message`, `waiting`, `done`, `error`), `text` (plain-language step shown on the dashboard), `tool?`, `latency_ms?`, `evidence?` (EvidenceItem), `hypotheses?` (list of Hypothesis) |
| `InvestigationConcluded` | `investigation_id`, `field`, `diagnosis` (`condition`, `summary`), `hypotheses`, `impact` (ImpactEstimate), `proposed_action` (ProposedAction), `message_id?`, `fast_path` (boolean) |
| `ReplyReceived` | `reply_id`, `farm_id`, `person_id`, `intent` (one of `yes`, `no`, `more`, `photo`, `call`, `stop`, `help`, `free_text`), `text`, `investigation_id?`, `approval_id?` |
| `ApprovalRecorded` | `approval_id`, `investigation_id`, `decision` (one of `approved`, `declined`), `decided_by`, `channel` (one of `sms`, `web`, `voice`) |
| `PhotoReceived`, `DocumentReceived` (shared shape, UploadReceived) | `upload_id`, `kind` (one of `photo`, `document`), `field`, `investigation_id?`, `s3_key`, `location?` (`lon`, `lat`, `accuracy_m`), `result` (PhotoResult or DocumentResult, defined under Upload intake) |
| `ActionCompleted` | `action_id`, `investigation_id`, `completed_by`, `photo_upload_id?` |

`MessageSent` and `MessageReceived` carry `{ message_id, farm_id, person_id, direction, body, investigation_id?, status }`; the projector turns them into `message_out` and `message_in` feed messages, so it stays the only publisher to the live feed.

## Data model

All DynamoDB tables use on-demand capacity and the prefix `fw-`. There are twelve; five of them (tile index, actions, messages, uploads, idempotency) exist because the interfaces below need them.

| Table | Partition key | Sort key | Main attributes | Indexes and TTL |
| --- | --- | --- | --- | --- |
| `fw-farms` | `farm_id` | `item` (`FARM` or `PERSON#per_…`) | Farm: name, time zone, quiet hours. Person: name, `phone_e164`, role, `channel_pref`, `opt_in_at`, `opted_out` | GSI `by-phone`: `phone_e164` → farm and person (inbound lookup) |
| `fw-fields` | `farm_id` | `field_id` | name, `geometry_key` (S3), acres, `crop_current`, irrigated, `pivot {center, radius_m}`, `csb_id`, `status` (`building`, `ready`) | GSI `by-field`: `field_id` |
| `fw-tile-index` | `tile_id` (Sentinel-2 grid tile) | `field_id` | `farm_id` | none |
| `fw-field-context` | `field_id` | none | FieldContext document (see Context builder) | none |
| `fw-scene-analyses` | `field_id` | `date#source` | status, `max_score`, regions, overlays, `analysis_key` | TTL 180 days |
| `fw-watches` | `field_id` | `opened_at#watch_id` | type, status, detail | TTL on `expires_at` |
| `fw-investigations` | `investigation_id` | none | field, `anomaly_id`, status, hypotheses, evidence, diagnosis, impact, `proposed_action`, `runtime_session_id`, `fast_path`, timestamps | GSI `by-field`: `field_id` + `created_at`. No dedupe index: `investigation_id` is derived from `anomaly_id`, so a conditional put on the key dedupes. |
| `fw-approvals` | `approval_id` | none | `investigation_id`, action, `sent_to_person_id`, status (`pending`, `approved`, `declined`, `expired`), `decided_by`, channel | GSI `by-person-status`; TTL on `expires_at` |
| `fw-actions` | `action_id` | none | type, `investigation_id`, assignee, status, pin, `done_at` | GSI `by-assignee` |
| `fw-messages` | `message_id` | none | direction, `person_id`, body, `provider_message_id`, status, `idempotency_key` | GSI `by-provider-id (matches a reply to the message it answers)` |
| fw-uploads | upload\_id | none | kind, field, investigation\_id, status (pending, accepted, retake), result, s3\_key | TTL 365 days |
| fw-idempotency | key (e.g. scene#id, msg#key, sms-in#id) | none | result pointer (so a retry can return the first result) | TTL 7 days |

**S3 bucket `fieldwatch-<account>-usw2`**

| Prefix | Written by | Contents |
| --- | --- | --- |
| `static/{dataset}/` | Prep script | Nebraska slices: CSB GeoParquet, CDL, gNATSGO, hydrography, wells |
| `fields/{field_id}/geometry.geojson` | Web API (onboarding) | Field boundary |
| `context/{field_id}/` | Context builder | `soil_zones.tif`, `lowspot.tif`, `baseline.parquet` |
| `analysis/{field_id}/{date}/{source}/` | Field analyzer | `indices.tif`, `mask.tif`, `true_color.png`, `health.png`, `mask.png`, `result.json` |
| `hazards/hail/{valid_time}/` | Hail filter | `swath.tif`, `swath.png` |
| `uploads/{upload_id}/` | Phone via presigned PUT | Original photo or document |
| `kb/` | Knowledge ingestion | Source documents with metadata sidecar files |

Lifecycle: `analysis/` and `hazards/` expire after 180 days; `uploads/` after 365 days or on farm deletion.

## Imagery pipeline

### Scene filter (Lambda, Python, 512 MB, 30 s)

**Trigger:** SQS `fw-scene-queue` (subscribed to the Earth Search new-item SNS topic; also fed by the replay API). Batch size 10, partial batch failures reported.

**Input:** the SQS body is either the Earth Search message (a full STAC item) or a replay envelope `{ replay: true, stac_item, field_ids? }`.

**Rules:** skip collections other than `sentinel-2-c1-l2a`; look up `fw-tile-index` by the item's grid:code property (for example MGRS-14TNM); skip if no fields match or scene cloud cover is above 80%; record scene#\<scene\_id> in fw-idempotency with a conditional put (a repeat delivery stops here, since Express workflows don't deduplicate by execution name the way Standard ones do), then start one execution per scene.

**Output (state machine input), SceneAnalysisInput:**

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `"1"` | |
| `scene.scene_id` | string | STAC item ID |
| `scene.source` | `"sentinel-2-c1-l2a"` | |
| `scene.datetime` | ISOTime | |
| `scene.tile_id` | string | |
| `scene.cloud_cover` | number | |
| `scene.assets` | map of band name to COG href | Bands: `blue`, `green`, `red`, `rededge1`, `nir`, `nir08`, `swir16`, `scl` |
| `fields` | list of FieldRef | |
| `replay` | boolean | |

### Scene analysis (Step Functions Express)

Stages, in order:

1. **Map over `fields`** (max concurrency 10), per field:
   1. `LoadFieldInputs` (Lambda): reads Fields, FieldContext summary and open Watches; outputs `FieldAnalysisRequest`.
   2. `AnalyzeField` (field analyzer Lambda): outputs `FieldAnalysisResult`.
   3. `Decide` (decision Lambda): writes the SceneAnalyses row, emits `SceneAnalyzed`, and emits `AnomalyDetected` when the rule passes.
2. **Output:** `{ scene_id, results: { field_id, status, emitted_anomaly_id? }[] }`.

Errors inside one field's branch are caught and recorded as `status: "error"`; one bad field never fails the scene.

### Field analyzer (Lambda, container image with GDAL, 3,008 MB, 90 s)

**FieldAnalysisRequest**

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `"1"` | |
| `scene` | same shape as `scene` in SceneAnalysisInput | |
| `field` | FieldRef | |
| `geometry_key` | string | S3 key of the field boundary |
| `interior_buffer_m` | number | Default 10 |
| `baseline_key` | string | `context/{field_id}/baseline.parquet` |
| `context_rasters` | `soil_zones_key`, `lowspot_key` (strings) | |
| `pivot?` | `center` ([lon, lat]), `radius_m` | |
| `open_watches` | list of `watch_id`, `type`, `swath_key?` | |
| `thresholds` | `min_clear_fraction`, `min_region_acres`, `z_threshold` (numbers) | |

**IndexStats:** `mean`, `p10`, `p50`, `p90` (numbers).

**FieldAnalysisResult**

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `"1"` | |
| `field`, `scene_id`, `date` | FieldRef, string, ISODate | |
| `status` | one of `ok`, `too_cloudy`, `no_baseline`, `error` | |
| `clear_fraction` | number | |
| `indices` | `ndvi`, `ndre`, `ndmi`, each IndexStats | |
| `anomaly` | `field_score`, `z_baseline`, `z_cohort`, `delta_since_last` (numbers), `regions` (list of AnomalyRegion) | |
| `gates` | `senescence`, `harvest`, `regional` (booleans) | |
| `overlays` | `true_color`, `health`, `mask`, each an OverlayRef | |
| `analysis_key` | string | S3 prefix holding all outputs |
| `error?` | the `error` object from ErrorBody | |

Error codes: `COG_READ_FAILED` (retryable), `NO_BASELINE`, `GEOMETRY_INVALID`.

### Decision (Lambda, 256 MB)

**Input:** `{ result: FieldAnalysisResult, thresholds }`. **Rule:** emit when `status == "ok"`, at least one region has `score >= z_threshold` and `area_acres >= min_region_acres`, and no gate is true. **Output:** `{ emit: boolean; reason: string; anomaly_id?: string }`. Writes are conditional on `field_id#date` so a replayed scene doesn't create a second anomaly.

### Replay API

`POST /v1/replay` (dashboard auth). Body `{ scene_id: string; field_ids?: string[] }`. Fetches the STAC item from Earth Search and sends a replay envelope to `fw-scene-queue`. Response `202 { replay_id }`. Rate-limited to one call per 30 seconds. `POST /v1/replay/reset` clears the demo farm's investigations, approvals and actions created by replays.

### Replay correctness: "as of" rules

Replaying a July scene in October must see the world as it was in July, or the baseline, growth stage and hazards will all be wrong:

1. **Baseline:** built from prior seasons only; the current season's scenes are stored separately in `baseline.parquet` and the analyzer reads only those dated before the scene (for "change since last clear scene").
2. **Growth stage:** computed by the analyzer for the scene date (Crop Progress history + the field's curve), not read from the stored current stage.
3. **Drought and weather:** every lookup takes the scene date (`get_drought` takes `a date range`; `get_weather` already takes a range).
4. **Hail:** `POST /v1/replay` also publishes `MrmsFileAvailable` for the archived daily hail-size files covering the 14 days before the scene, so watches exist as they would have.
5. **Two tiles, one field:** fields straddling two Sentinel-2 tiles get one analysis per tile per date; the decision step keeps the one with the higher clear fraction.
6. **IDs:** `anomaly_id` is derived from `field_id` and date; `investigation_id` from `anomaly_id`. Replaying twice without `/replay/reset` is a no-op.

## Hazard pipeline

### MRMS forwarder (Lambda in us-east-1, 128 MB)

**Trigger:** the MRMS new-object SNS topic (Lambda subscription). **Rule:** keep only keys under CONUS/MESH\_Max\_60min\_00.50/ (live); replay backfill reads CONUS/MESH\_Max\_1440min\_00.50/. **Output:** `MrmsFileAvailable` sent with PutEvents to the `fieldwatch` bus in us-west-2. Expect high message volume; the filter must return in milliseconds.

### Hail filter (Lambda, container image with eccodes, 1,024 MB, 60 s)

**Input:** `MrmsFileAvailable`. **Stages:**

1. Download and decompress the GRIB2 file (small); read with cfgrib.
2. Clip to the bounding box of all registered farms (cached list, refreshed every 10 minutes).
3. For each field: max hail size (inches) and share of field area above `hail_threshold_in` (default 1.0).
4. For fields over threshold: write the swath raster and PNG to `hazards/hail/{valid_time}/`, upsert a Watch per field, emit one `WatchOpened` per farm.

**Output detail:** `{ max_hail_in: number; pct_area_over_threshold: number; swath_key: string; valid_time: ISOTime }`. Watches expire after 14 days.

### NWS alert poller (Lambda, every 5 minutes via EventBridge Scheduler)

`GET https://api.weather.gov/alerts/active?area=NE` with a descriptive User-Agent. Severe thunderstorm and tornado warning polygons intersecting fields open a `nws_alert` watch (expires at the alert's end time). State: last-seen alert IDs in a small DynamoDB item to avoid duplicates.

### Drought poller (Lambda, weekly)

Fetches the US Drought Monitor county category for each farm's county; writes `drought: { category, as_of }` into FieldContext; opens a `drought` watch when the category is D2 or worse.

### Storm report poller (Lambda, daily; should-have)

Pulls the previous day's SPC hail and wind reports; attaches reports within 10 km of a field to that field's open hail watch as `detail.ground_reports[]`.

## Context builder (Step Functions Standard)

**Trigger:** the web API after onboarding saves fields. **Input:** `{ schema_version: "1", farm_id, field_ids: string[] }`. **Output:** `{ farm_id, results: { field_id, status: "ready" | "partial" | "failed", failed_stages: string[] }[] }`; each field's `status` in `fw-fields` flips to `ready` at the end.

Stages per field (Map, max concurrency 5). Stages 2–9 run as a Parallel state; each writes only its own sub-document of FieldContext, so a failure in one leaves the others usable (`partial`).

| # | Stage (Lambda) | Reads | Writes |
| --- | --- | --- | --- |
| 1 | `LoadBoundary` | `fields/{id}/geometry.geojson` | Normalized geometry, acres, centroid, Sentinel-2 tiles → `fw-tile-index` |
| 2 | `Pivot` | Geometry, mid-season composite | `pivot` on the Fields item |
| 3 | `Crop` | CDL slices, CSB rotation | `crop_current`, `crop_history` |
| 4 | `Soil` | gNATSGO raster + tables (or Soil Data Access) | `soil`, `context/{id}/soil_zones.tif` |
| 5 | `Terrain` | 3DEP elevation | `terrain`, `context/{id}/lowspot.tif` |
| 6 | `Water` | Hydrography, wetlands, flood zones, surface-water occurrence | `water` |
| 7 | `Wells` | Nebraska DNR wells, USGS groundwater | `wells` |
| 8 | `Economics` | NASS Quick Stats (through the get\_county\_stats Gateway tool), price source | `economics` |
| 9 | `Stage` | NASS Crop Progress | `stage` |
| 10 | `Baseline` (container Lambda, 15 min; or Fargate if it runs long) | Past Sentinel-2 scenes | `context/{id}/baseline.parquet`, `baseline` summary, cohort |

**FieldContext** (each sub-document is written by one stage)

| Field | Contents |
| --- | --- |
| `schema_version`, `field_id`, `built_at` | `"1"`, string, ISOTime |
| `soil` | `map_units` (list of `mukey`, `pct_area`, `texture_0_30`, `texture_30_100`, `awc_0_100_cm`, `drainage_class`, `hydro_group`), `zones_key` |
| `terrain` | `slope_mean_pct`, `lowspot_pct`, `lowspot_key` |
| `water` | `nearest_stream_m` (number or null), `floodplain_pct`, `wetland_pct`, `historically_wet_pct`, `borders_water` (boolean) |
| `wells` | `irrigation_wells` (list of `id`, `depth_m?`, `static_level_m?`, `pump_gpm?`) |
| `economics` | `expected_yield_bu_ac`, `price_usd_bu`, `price_as_of` (ISODate), `sources` (list of string) |
| `stage` | `estimate` (growth stage in words, e.g. "R2 (blister)"), `as_of` |
| `baseline` | `key`, `seasons`, `cohort_field_ids` (list of string) |
| `drought?` | `category` (one of `none`, `D0`–`D4`), `as_of` |

## Agent service (AgentCore Runtime, Strands)

One container image, one Runtime, one entrypoint that accepts three request kinds. The Runtime session ID is fieldwatch-session-\<investigation\_id> (AgentCore requires at least 33 characters), so a resume lands in the same session when it's still alive; when it isn't, the agent rebuilds state from `fw-investigations` and Memory.

### Session starter (Lambda, 256 MB, 15 s)

**Trigger:** EventBridge rules on `AnomalyDetected`, `ReplyReceived`, `ApprovalRecorded`, `PhotoReceived`, `DocumentReceived`, `ActionCompleted`. **Does:** builds the request below (for a new anomaly, creates the Investigations item with a conditional put on an investigation\_id derived from `anomaly_id`), then invokes the Runtime; the agent acknowledges at once and keeps working as a background task, reporting HealthyBusy on /ping so the session stays alive, and the starter returns. **Errors:** `RUNTIME_THROTTLED` (retryable; EventBridge retry policy with a dead-letter queue).

### Runtime request and response

**AgentRequest:** every request has `schema_version` (`"1"`) and a `kind` that decides its other fields.

| `kind` | Other fields | Use |
| --- | --- | --- |
| `start_investigation` | `investigation_id`, `anomaly` (AnomalyDetected), `context` (ContextBundle) | New anomaly |
| `resume` | `investigation_id`, `event` (ReplyReceived, ApprovalRecorded, PhotoReceived/DocumentReceived or ActionCompleted) | Continue a paused investigation |
| `question` | `farm_id`, `person_id`, `text` | Free-text question outside an investigation |

**ContextBundle** (precomputed, so the supervisor starts informed)

| Field | Type | Notes |
| --- | --- | --- |
| `field`, `field_name` | FieldRef, string | |
| `crop`, `irrigated` | Crop, boolean | |
| `pivot?` | `center` ([lon, lat]), `radius_m` | |
| `stage` | string | |
| `analysis` | FieldAnalysisResult | |
| `open_watches` | list of `watch_id`, `type`, `detail` | |
| `people` | list of `person_id`, `name`, `role`, `channel_pref` (one of `sms`, `call`) | |
| `prior_incidents` | list of string | From fw-investigations (GSI by-field), not Memory |

**AgentResult**

| Field | Type |
| --- | --- |
| `investigation_id?` | string |
| `status` | one of `awaiting_reply`, `awaiting_upload`, `concluded`, `answered`, `failed` |
| `fast_path?` | boolean |
| `error?` | the `error` object from ErrorBody |

Progress is not returned in the response; every step is emitted as `InvestigationProgress` on the bus (and traced), so the dashboard doesn't depend on the caller.

### Investigation lifecycle

1. `start_investigation`: write status `running`; run the scorer with include\_analysis, which turns the analysis's pattern features and open watches into evidence items.
2. **Fast path:** if the top hypothesis is at or above `fast_path_threshold` (default 0.9), skip to step 5.
3. Call the three specialists in parallel with `SpecialistRequest`s.
4. Merge evidence; re-score; if the top two hypotheses are within 0.15 and a photo would separate them, call `request_photo` and set `awaiting_upload`.
5. Estimate impact; write diagnosis; call `send_farmer_message` with a proposed action and create a pending approval; set `awaiting_reply`; emit `InvestigationConcluded`.
6. `resume` with `ApprovalRecorded(approved)`: call the write tool for the action (policy checks the approval); confirm by message; status `concluded`.
7. `resume` with a photo, document or free-text reply: add evidence items, re-score, and return to step 4 or 5.

**Memory mapping.** AgentCore Memory events use `actorId = farm_id` and `sessionId = investigation_id`. Three long-term strategies write under `/fieldwatch/farm/{actorId}/`: user preference (quiet hours, call or text, who gets copies), semantic (farmer corrections and facts about the farm), and **episodic** (how each investigation unfolded and ended, including the farmer's verdict). At the start of an investigation the supervisor retrieves the top episodes for the same field and condition, so it can say "last July this ring pattern was a nozzle." Exact records (diagnoses, approvals, actions) stay in DynamoDB; episodes are what the agent recalls, not the system of record.

**Runtime role permissions.** Besides Bedrock models, Gateway and Memory, the Runtime's IAM role needs `events:PutEvents` on the `fieldwatch` bus and read/write on `fw-investigations`, because the agent records its own progress and state.

### Specialist contract (agents as tools)

**SpecialistRequest**

| Field | Type |
| --- | --- |
| `investigation_id`, `field` | string, FieldRef |
| `region_ids` | list of string |
| `hypotheses_to_test` | list of ConditionId |
| `budget` | `max_tool_calls`, `max_output_tokens` (numbers) |

**SpecialistResponse**

| Field | Type | Notes |
| --- | --- | --- |
| `agent` | one of `imagery`, `environment`, `agronomy` | |
| `evidence` | list of EvidenceItem | |
| `notes` | string | Short, for the supervisor only |
| `tool_calls` | number | |

### Supervisor output (structured, validated before any tool call)

**SupervisorDecision**

| Field | Contents | Rule |
| --- | --- | --- |
| `diagnosis` | `condition` (ConditionId), `summary_plain` | No jargon, at most 2 sentences |
| `message` | `person_id`, `body`, `reply_options` (any of `YES`, `NO`, `MORE`, `PHOTO`) | |
| `proposed_action` | ProposedAction | |
| `photo_request?` | `lon`, `lat`, `what_to_photograph` | |

### Limits

| Limit | Default |
| --- | --- |
| Wall-clock per invocation | 90 s |
| Tool calls per investigation | 40 (specialists: 8 each) |
| Output tokens per specialist call | 1,500 |
| Model temperature | 0 for specialists; low for the supervisor |
| Retries on model throttling | 2, with backoff; then fall back to the second configured model |

## Gateway tools

Each tool is an MCP tool on AgentCore Gateway backed by one Lambda target. Every call carries `investigation_id`, which the policy uses as context and the handler uses for audit. Read tools return within 3 seconds or fail with `UPSTREAM_TIMEOUT` (retryable).

| Tool | Input | Output | Access |
| --- | --- | --- | --- |
| `get_field_context` | `{ field_id }` | `FieldContext` | Read |
| `get_scene_analysis` | `{ field_id, date? }` (latest if omitted) | `FieldAnalysisResult` plus presigned overlay URLs | Read |
| `get_index_history` | `{ field_id, index: "ndvi" \| "ndre" \| "ndmi", start, end, region_id? }` | `{ points: { date, value, expected, z }[] }` | Read |
| `compare_to_cohort` | `{ field_id, date }` | `{ field_value, cohort_median, cohort_n, z_cohort }` | Read |
| `get_weather` | `{ field_id, start, end }` | `{ daily: { date, precip_mm, tmax_c, tmin_c, et0_mm, wind_max_kph }[], totals }` | Read |
| `get_hazards` | `{ field_id, start, end }` | `{ watches: WatchOpened[] }` | Read |
| `get_drought` | `{ aoi: county_fips, startdate, enddate }` | `Drought Monitor county statistics for the range (API response)` | Read (OpenAPI target, no Lambda) |
| get\_county\_stats | { commodity\_desc, statisticcat\_desc, state\_alpha, county\_name, year } | NASS Quick Stats records (API response) | Read (OpenAPI target; API key injected from AgentCore Identity) |
| `test_overlap` | `{ field_id, region_id, layer: "soil_sandy" \| "lowspot" \| "hail_swath" }` | `{ overlap: number, expected_by_chance: number, p_value: number }` | Read |
| `search_agronomy` | `{ query, crop?, condition?, stage? }` | `{ results: { title, url, snippet, score }[] }` (max 5) | Read |
| `get_disease_reports` | `{ county_fips, crop, season }` | `{ reports: { disease, confirmed_on, source }[] }` | Read |
| `score_hypotheses` | `{ investigation_id, evidence: EvidenceItem[], include_analysis?: { field_id, date } }` | `{ hypotheses: Hypothesis[] }` | Read (deterministic) |
| `estimate_impact` | `{ field_id, region_ids, condition, days_until_fixed }` | `ImpactEstimate` | Read (deterministic) |
| `request_photo` | `{ investigation_id, person_id, lon, lat, what_to_photograph }` | `{ upload_link_id, message_id }` | Write, no approval |
| `send_farmer_message` | `SendMessageRequest` (see Messaging) | `SendMessageResult` | Write, no approval; quiet hours and rate limits enforced |
| `create_work_order` | `{ investigation_id, approval_id, assignee_person_id, field_id, pin: [lon, lat], instructions }` | `{ action_id, message_id }` | Write, needs approval |
| `create_scout_pin` | `{ investigation_id, approval_id, person_id, pin, what_to_check }` | `{ action_id, message_id }` | Write, needs approval |
| `draft_insurance_notice` | `{ investigation_id, approval_id, person_id }` | `{ action_id, document_key }` (a draft for the farmer to send) | Write, needs approval |

**Target types and credentials.** Every tool is a Lambda target except `get_drought` and `get_county_stats`, which are OpenAPI targets defined from each API's OpenAPI description, trimmed to the one operation needed. The Quick Stats key lives in an AgentCore Identity API-key credential provider attached to that target's outbound auth, so neither the agent nor any Lambda holds it; the context builder's economics stage calls the same tool through the Gateway. OpenAPI targets return raw responses, so keep them to small single-call lookups; anything that needs caching or per-field summarizing stays a Lambda.

**Approval check for write tools.** The handler loads the approval and requires all of: `status == "approved"`, `investigation_id` matches, the action type matches, not expired. AgentCore Policy can only see a tool's input arguments (context.input), not the Approvals table, so the Gateway policy requires a well-formed approval\_id on every write tool and the handler check above is the real enforcement. Policy also can't tell supervisor calls from specialist calls (they share the Runtime's IAM identity), so specialists get read-only tool lists in the Strands app. Write tools are idempotent on approval\_id: one approval, at most one action.

**Error codes:** `NOT_FOUND`, `POLICY_DENIED` (missing or mismatched approval), `RATE_LIMITED`, `QUIET_HOURS` (message deferred, not an error for the agent), `UPSTREAM_TIMEOUT`, `INVALID_INPUT`.

**Scorer details.** `score_hypotheses` loads the condition catalog (versioned JSON in S3: priors by crop, stage and irrigation; weights per `weight_key`), sums log-odds per condition, normalizes, and marks `ruled_out` below 0.05 and `likely` above 0.6. The catalog version is stored on the investigation so results are reproducible.

## Messaging

### Send (Lambda behind `send_farmer_message`, also used by `request_photo` and write tools)

**SendMessageRequest**

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `"1"` | |
| `person_id` | string | |
| `investigation_id?` | string | |
| `body` | string | Under about 300 GSM-7 characters; no emoji |
| `link?` | `scope` (one of `report`, `upload`, `work_order`, `onboard`), `subject_id`, `ttl_hours` | |
| `approval?` | `action_type` (as in ProposedAction), `ttl_hours` | Creates a pending approval |
| `urgency` | one of `low`, `actionable`, `urgent` | |
| `idempotency_key` | string | e.g. `investigation_id#message-kind` |

**SendMessageResult**

| Field | Type |
| --- | --- |
| `message_id` | string |
| `approval_id?` | string |
| `status` | one of `sent`, `deferred_quiet_hours`, `rate_limited`, `mirror_only`, `opted_out` |
| `scheduled_for?` | ISOTime |

**Rules, in order:** look up the person (opted out → `opted_out`); dedupe on `idempotency_key`; append the signed link if requested; if `urgency != "urgent"` and it's quiet hours in the farm's time zone, schedule with EventBridge Scheduler and return `deferred_quiet_hours`; enforce the daily cap; send via End User Messaging (or publish only to the phone-mirror channel when `SMS_ENABLED=false`, returning `mirror_only`); write `fw-messages`; emit MessageSent.

### Inbound replies

End User Messaging delivers two-way replies to an SNS topic; the reply handler Lambda subscribes. The payload carries originationNumber, destinationNumber, messageKeyword, messageBody, inboundMessageId and previousPublishedMessageId (the ID of the message being replied to).

### Reply handler (Lambda, 256 MB)

1. Dedupe on inboundMessageId (fw-idempotency).
2. Find the person by `phone_e164` (GSI `by-phone`); unknown numbers get a short "text JOIN to sign up" reply and nothing else.
3. Classify intent: exact keywords first (`YES`, `Y`, `NO`, `N`, `MORE`, `PHOTO`, `CALL`, `STOP`, `HELP`, case-insensitive, trimmed); anything else is `free_text`.
4. `STOP` and `HELP` are handled here and never reach the agent; `STOP` sets `opted_out`.
5. For `YES`/`NO`: match previousPublishedMessageId to the outbound message and its approval; if it's missing, fall back to this person's most recent pending, unexpired approval. If found, write the decision conditionally (`status == pending`) and emit `ApprovalRecorded`; if none, treat the reply as `free_text`.
6. For anything that wasn't an approval decision, attach the person's most recent open investigation (within 7 days) and emit `ReplyReceived`. Approval replies emit only `ApprovalRecorded`, so one YES never resumes the agent twice.
7. Emit MessageReceived (the projector shows it in the phone mirror).

The agent never decides whether a reply is an approval; this handler does.

## Web API and live feed

API Gateway HTTP API, base path `/v1`, JSON only, one Lambda per route group. Token-scoped routes read `t` from the query string; dashboard routes require `Authorization: Bearer <token>`.

| Method | Path | Auth | Request | Response |
| --- | --- | --- | --- | --- |
| GET | `/onboarding/fields?bbox=w,s,e,n` | `onboard` | none | GeoJSON FeatureCollection of candidate fields: `csb_id`, acres, `claimed` |
| POST | `/farms/{farm_id}/fields` | `onboard` | `{ fields: { csb_id?, geometry?, name, irrigated, pivot?, crop }[] }` | `201 { field_ids, context_build_id }` |
| POST | `/farms/{farm_id}/people` | `onboard` | `{ name, phone_e164, role, channel_pref }` | `201 { person_id, opt_in: "pending" }` |
| GET | `/reports/{investigation_id}` | `report` | none | `ReportView` |
| POST | `/approvals/{approval_id}/decision` | `report` | `{ decision: "approve" \| "decline", reason? }` | `200 { status }`; emits `ApprovalRecorded` |
| POST | `/uploads` | `upload` | `{ kind, content_type, field_id, investigation_id?, location? }` | `201 { upload_id, put_url, expires_at }` |
| GET | `/uploads/{upload_id}` | `upload` | none | `{ status: "pending" \| "accepted" \| "retake", message? }` |
| GET | `/work-orders/{action_id}` | `work_order` | none | `{ field_name, pin, instructions, status }` |
| POST | `/work-orders/{action_id}/done` | `work_order` | `{ photo_upload_id? }` | `200`; emits `ActionCompleted` |
| GET | `/farms/{farm_id}/overview` | Dashboard | none | `{ fields: { field_id, name, geometry, status, latest_score }[], open_investigations }` |
| GET | `/investigations/{investigation_id}` | Dashboard | none | Full Investigations item with evidence |
| POST | `/replay`, `/replay/reset` | Dashboard | See Imagery pipeline | `202` |
| POST | `/auth/dashboard` | Passcode | `{ passcode }` | `{ token, expires_at }` |
| POST | /mirror/reply | Dashboard | { person\_id, text } | 202; runs the reply handler as if the text came by SMS (for demos with SMS disabled) |

**ReportView**

| Field | Contents | Notes |
| --- | --- | --- |
| `field` | `field_id`, `name`, `acres` | |
| `scene_date` | ISODate | |
| `overlays` | `true_color_url`, `health_url`, `mask_url`, `bounds` | |
| `summary` | `headline`, `detail` | |
| `checked` | list of `condition`, `label`, `status` (as in Hypothesis), `reason` | |
| `impact` | ImpactEstimate | |
| `action?` | `approval_id`, `label`, `status` (one of `pending`, `approved`, `declined`, `expired`) | |
| `navigate_url` | string | Opens the phone's maps app at the pin |
| `updated_since_sent` | boolean | |

**Error codes:** `TOKEN_EXPIRED` (the page shows "text LINK for a new one"), `TOKEN_SCOPE`, `NOT_FOUND`, `ALREADY_DECIDED` (shows who decided and when), `INVALID_INPUT`.

### Live feed (AppSync Events)

Namespace `fieldwatch`; channels `/farms/{farm_id}/feed` (dashboard) and `/farms/{farm_id}/phone` (phone mirror). Only the projector Lambda publishes; dashboards subscribe with the dashboard token (publish with IAM; subscribe through a Lambda authorizer that checks the dashboard token).

**FeedMessage**

| Field | Type | Notes |
| --- | --- | --- |
| `type` | one of `field_status`, `scene_analyzed`, `investigation_step`, `evidence_added`, `hypotheses_updated`, `concluded`, `message_out`, `message_in`, `action_created` | |
| `ts` | ISOTime | |
| `investigation_id?` | string | |
| `agent?` | one of `supervisor`, `imagery`, `environment`, `agronomy` | |
| `payload` | free-form object | The source event's fields, trimmed for display |

## Upload intake and knowledge base

### Intake (Lambda, container image, 1,024 MB, 60 s)

**Trigger:** S3 `ObjectCreated` on `uploads/`. **Stages:**

1. Validate type and size (photos: JPEG/HEIC/PNG up to 15 MB; documents: PDF or photo up to 20 MB).
2. Strip metadata except capture time and location; convert HEIC to JPEG; downscale to 1,568 px on the long side.
3. Photos: sharpness check (variance of the Laplacian below threshold → `retake`); distance from the investigation's anomaly region.
4. Photos: multimodal model with retrieved symptom guides for the crop, stage and county → `PhotoResult`.
5. Documents: extraction with a multimodal model or Bedrock Data Automation → `DocumentResult`, always `needs_confirmation`.
6. Update the upload status (read by `GET /uploads/{id}`) and emit `PhotoReceived` or `DocumentReceived`.

**PhotoResult**

| Field | Contents |
| --- | --- |
| `quality` | `sharpness` (number), `ok` (boolean), `retake_reason?` (one of `blurry`, `too_far`, `wrong_subject`) |
| `distance_to_region_m?` | number |
| `observation.plant_part` | one of `leaf`, `stalk`, `ear`, `canopy`, `soil`, `other` |
| `observation.crop` | Crop |
| `observation.symptoms` | list of string |
| `observation.candidates` | list of `condition` (ConditionId), `detail`, `confidence` (one of `low`, `medium`, `high`), `visual_evidence` |
| `observation.suggested_followup` | string |

**DocumentResult**

| Field | Contents |
| --- | --- |
| `doc_type` | one of `soil_test`, `tissue_test`, `water_test`, `other` |
| `lab?`, `sampled_on?` | string, ISODate |
| `samples` | list of `sample_id`, `field_id?`, `tests` (list of `name`, `value` (number), `unit`) |
| `needs_confirmation` | always `true` |

Photo and document candidates become evidence items only after the supervisor maps them to `weight_key`s; document values only after the farmer confirms them by text.

### Knowledge base retrieval

Bedrock Knowledge Base `fw-agronomy` on S3 Vectors. Every source document has a metadata sidecar `{ crop: string[], condition: ConditionId[], stage: string[], region: "NE" | "Midwest", year: number, url: string }`. `search_agronomy` calls the retrieve API with a metadata filter built from its optional arguments and returns at most five chunks with their source URLs; answers sent to farmers cite the URL.

## Evaluations (AgentCore Evaluations)

The scenario suite is the definition of demo-ready, and it runs as AgentCore Evaluations rather than a hand-rolled script, so the same evaluators also score live investigations afterward.

### Scenario file (`eval/scenarios/*.json`)

The file format only; the scenarios themselves and their expected values are written at the event (T32).

| Field | Contents | Notes |
| --- | --- | --- |
| `schema_version` | `"1"` | |
| `scenario_id` | string | |
| `scene_id` | string | Replayed through `POST /v1/replay` |
| `field_ids` | list of string | |
| `expected` | `alert` (boolean), `top_condition?` (ConditionId), `action_type?`, `max_trigger_to_message_s` (number) | |
| `scripted_replies?` | list of `after` (one of `alert`, `photo_request`), `text`, `photo_s3_key?` | |

### Runner (script, run from a laptop or CodeBuild)

1. For each scenario: `POST /v1/replay/reset`, then `POST /v1/replay` with SMS disabled (`mirror_only`).
2. Feed `scripted_replies` through `POST /v1/mirror/reply` when the matching message appears on the feed.
3. Wait for `InvestigationConcluded` (or a timeout), then collect the investigation and its Runtime session ID.
4. Start an on-demand evaluation over those sessions with the evaluators below, passing each scenario's `expected` block to the custom evaluators.
5. Pass/fail per scenario; the suite passes only if every scenario passes.

### Evaluators

| Evaluator | Kind | Pass when |
| --- | --- | --- |
| Answer quality (helpfulness, faithfulness to tool results) | Built-in (choose from the built-in set) | Score at or above the threshold set Saturday morning |
| Tool selection | Built-in | No write tool called without an approval; specialists call only read tools |
| `top_condition_correct` | Custom (code) | Top hypothesis equals `expected.top_condition` |
| `alert_decision_correct` | Custom (code) | An alert was sent if and only if `expected.alert` |
| `message_rules` | Custom (LLM judge) | Under \~300 characters, no jargon (NDVI, anomaly, z-score), names the field, includes reply options |
| `latency` | Custom (code) | Trigger-to-message time under `expected.max_trigger_to_message_s` |

### Online evaluation

An online evaluation config samples 10% of live investigations (100% during demo week) with the built-in quality evaluators, `message_rules` and `alert_decision_correct` (scored later against the farmer's verdict). Results land in CloudWatch next to the cost and latency metrics. Both evaluators and the online config are Terraform resources (`aws_bedrockagentcore_evaluator`, `aws_bedrockagentcore_online_evaluation_config`).

## Consistency review

Tracing every event and call end to end (replay → analysis → anomaly → agent → text → reply → action, plus photos and hazards) found thirteen problems. All are fixed in the sections above.

| # | Problem | Fix (section) |
| --- | --- | --- |
| 1 | Idempotency relied on GSIs, which can't enforce uniqueness | `fw-idempotency` table; IDs derived deterministically (Conventions, Data model, Agent service) |
| 2 | A YES emitted both `ApprovalRecorded` and `ReplyReceived`, resuming the agent twice and risking two work orders | Approval replies emit only `ApprovalRecorded`; write tools idempotent on `approval_id` (Messaging, Gateway tools) |
| 3 | Replaying a July scene in October used today's baseline, growth stage, drought and hail | "As of" rules (Imagery pipeline) |
| 4 | The decision step had no thresholds | Thresholds passed in (Imagery pipeline) |
| 5 | Send and reply Lambdas published to the live feed directly, contradicting "only the projector publishes" | `MessageSent` / `MessageReceived` events (Event catalog, Messaging) |
| 6 | No way to reply when SMS is disabled | `POST /mirror/reply` (Web API) |
| 7 | Upload status had nowhere to live | `fw-uploads` (Data model) |
| 8 | Session IDs were shorter than AgentCore's 33-character minimum | `fieldwatch-session-` prefix (Agent service) |
| 9 | The Cedar policy read approval status, which AgentCore Policy can't see | Policy checks tool inputs; the Lambda enforces status (Gateway tools) |
| 10 | The Runtime role couldn't emit progress events or write state | Added permissions (Agent service) |
| 11 | A Lambda read prior incidents from Memory | Read from DynamoDB; Memory holds preferences and corrections (Agent service) |
| 12 | The scorer couldn't use precomputed pattern features | `include_analysis` (Gateway tools) |
| 13 | Fields spanning two satellite tiles got two analyses per date | Keep the clearer one (Imagery pipeline) |

## Confirmed against AWS and provider docs (Oct 6)

| Item | Finding | Effect on this spec |
| --- | --- | --- |
| AgentCore Runtime | Session IDs must be 33–256 characters; sessions last up to 8 hours and a terminated session gets a fresh environment on the next invoke; containers serve `POST /invocations` and `GET /ping` on port 8080, ARM64; a `HealthyBusy` ping keeps a session active for background work | Session-ID prefix; ARM64 image; background-task pattern for the starter (fixed) |
| AgentCore Gateway (Lambda targets) | Tool schemas (input and output) are inline or in S3; the Lambda receives the tool arguments as the event and the tool name, prefixed with the target name and `___`, in the client context | One Lambda can host several tools; `investigation_id` must be an explicit argument, as specified |
| AgentCore Policy | Cedar policies attached to a Gateway; the schema is generated from the tools; principals are OAuth users or IAM entities; the only context is `context.input` (the tool's arguments) | Policy redesigned (fixed) |
| AgentCore Memory | Events are scoped by `actorId` + `sessionId`; long-term strategies (semantic, summarization, user preference, episodic, custom) write under namespace templates, which can include custom variables | Memory mapping added |
| End User Messaging | Inbound SMS payload includes `previousPublishedMessageId` | Exact reply-to-approval matching (fixed). Your number's registration status can only be checked in the console |
| AppSync Events | Connect, publish and subscribe auth modes are set per API and per channel namespace (API key, IAM, Lambda authorizer among them) | Publish with IAM from the projector; subscribe through a Lambda authorizer that checks the dashboard token |
| Earth Search | `sentinel-2-c1-l2a` assets `blue`, `green`, `red`, `rededge1`, `nir`, `nir08`, `swir16`, `scl` (plus a `cloud` probability layer); tile in `grid:code`; files in us-west-2 | Asset names confirmed; tile index uses `grid:code`; consider `cloud` for better masking |
| MRMS | Hail products live under `CONUS/MESH_Max_{30,60,120,240,360,1440}min_00.50/` and `CONUS/MESH_00.50/` | Forwarder filters on `CONUS/MESH_Max_60min_00.50/`; replay uses the 1,440-minute files |
| Terraform | The AWS provider has resources for AgentCore runtimes, gateways, gateway targets, memory and strategies, policy engines and policies, and evaluators | Terraform can manage the whole stack |

Still open, all quick checks:

- [ ] SMS registration status of your number (AWS console)
- [ ] MRMS archive depth: `aws s3 ls --no-sign-request s3://noaa-mrms-pds/CONUS/MESH_Max_1440min_00.50/` and look for July 2026 dates
- [ ] Gateway OpenAPI target with an Identity API-key credential provider: how the key is attached (header vs. query parameter; Quick Stats expects a key query parameter)
- [ ] AgentCore Evaluations: the built-in evaluator names, and how custom evaluators receive per-scenario expected values

Sources: [InvokeAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html) · [Runtime sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html) · [Runtime HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html) · [Gateway Lambda targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html) · [Policy schema constraints](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-schema-constraints.html) · [Memory organization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-organization.html) · [Two-way SMS payload](https://docs.aws.amazon.com/sms-voice/latest/userguide/two-way-sms-payload.html) · [AppSync Events with IAM publishing](https://docs.aws.amazon.com/appsync/latest/eventapi/lambda-iam-auth.html) · [Earth Search collection](https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a) · [Terraform AgentCore resources](https://awsfundamentals.com/terraform/bedrockagentcore)

Related: FieldWatch: System Design for architecture and rationale; FieldWatch: Data & ML Pipelines for dataset details.
