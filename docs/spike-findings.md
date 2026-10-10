# Spike findings (Oct 9–10)

What we learned by trying every data source and the core AgentCore pieces before building. **Where this file disagrees with the other docs, this file wins** (and `docs/decisions.md` wins over both).

The AWS spikes ran in a different, brand-new account in us-west-2. Account-level results (model availability, quotas, verification) may differ on the team account; data-source and API results apply everywhere.

## Data sources: access (test area Thurston County, NE)

Every check was a small remote read; nothing needs to be downloaded ahead of time.

| Source (pipelines doc section) | Access method that worked | Time | Notes |
| --- | --- | --- | --- |
| Field boundaries, USDA CSB 2018–2025 (A1, A3) | GDAL reading USDA's national zip in place over HTTP (range reads, no download) | ~85 s per area | `CDL2018`…`CDL2025` crop codes per field, acres, county FIPS. Whole county: 5,919 fields, ~3 MB geometry. **EPSG:5070** |
| Field boundaries, fiboa GeoParquet | DuckDB over HTTP with a bbox filter | ~1 s | Faster but dissolved and 2023-only (finding 1) |
| Sentinel-2 | Earth Search STAC search + windowed band read | ~3 s + ~2 s | Plenty of clear summer 2026 scenes; tile MGRS-14TPM over the test area |
| MRMS hail | GDAL GRIB driver reading the gzip file in place | ~1.4 s | Daily files every 30 min, ~1 MB; archive back to Oct 2020; 0.01° grid; −1 = no data |
| Weather (Open-Meteo archive) | REST, no key | ~1 s | |
| Drought Monitor | REST, no key, county FIPS + dates | ~0.4 s | |
| NWS alerts | REST, no key, User-Agent header | ~0.3 s | Current alerts only |
| SPC storm reports | Daily CSV by date | ~0.5 s | |
| Soil (NRCS Soil Data Access) | SQL over REST: map units, horizons and **polygons** in an area | ~1.2 s | |
| Elevation (USGS 3DEP) | TNM products API to find the tile, then a windowed read of the 1 m lidar COG | ~1.4 s | Pick the tile that contains the field |
| Streams / wetlands / floodplain | ArcGIS REST (USGS NHD, USFWS NWI, FEMA NFHL) with an envelope, GeoJSON out | 0.5–1 s | |
| Surface-water history (JRC) | Windowed read of the occurrence tile | ~1.1 s | |
| Registered wells (Nebraska DWEE) | ArcGIS REST "Registered Wells" layer | <1 s | Contains owner names (finding 6) |
| Groundwater levels | New USGS Water Data API (OGC) | <1 s | Old NWIS endpoint retired |
| County disease reports | EDDMapS JSON behind the Corn ipmPIPE maps | <1 s | Keyed by county FIPS; finding 13 |
| NASS Quick Stats / Crop Progress, AMS prices | REST | — | **Need API keys** (finding 7) |

## Data sources: format check

| Source | Verdict | Detail |
| --- | --- | --- |
| Sentinel-2 | Usable, with scaling | 16-bit integers, nodata 0. In `sentinel-2-c1-l2a` each band's metadata gives scale 0.0001 and offset −0.1; apply them before indices. The legacy `sentinel-2-l2a` collection (used by `imagery.py` today) already has the offset applied, so ratios like NDVI are fine there. Red/NIR 10 m; red-edge, SWIR and the scene-classification band 20 m |
| MRMS hail | Usable | Values in **mm** ("MESH Hail Swath 1440-min"). Real Nebraska 4.5-inch report → 47.9 mm MESH nearby: radar underestimates giant hail |
| Drought Monitor | Needs a rule | Percent of county per category, not one category |
| NWS alerts | Partly | Many live alerts have no polygon (zone-based). Can't replay past dates; use the Iowa Environmental Mesonet storm-based-warnings archive (July 2026, Omaha office: 55 warnings, all with polygons) |
| Soil Data Access | Usable | Texture, available water, conductivity, organic matter, pH, depths, drainage class, hydrologic group, water-table depth |
| NHD / NWI / FEMA | Usable | Lines and polygons in lon/lat, with type and flood-zone codes |
| USGS groundwater | Usable, sparse | Few, old readings; default parameter is water-level elevation, not depth |
| Open-Meteo | Usable | Precipitation, max/min temperature, reference ET, max wind |
| EDDMapS | Usable | First/last observation dates, "Certain" (this year) vs "History". Tar spot 84963, southern rust 17596, gray leaf spot 17739. Check terms of use before showing it publicly |

## Findings

**Data**
1. Use USDA's CSB 2018–2025 release, not the fiboa GeoParquet copy: the fiboa file is 2023-only and merges neighboring same-crop fields. Read the demo county once (≈85 s) and cache it.
2. CSB coordinates are EPSG:5070; transform the area of interest first or a lon/lat filter silently returns nothing.
3. GDAL reads MRMS GRIB2 directly (even from the gzip over HTTP), so the hail code needs no eccodes/cfgrib.
4. Soil zones can be built from Soil Data Access polygons alone; no gNATSGO raster needed.
5. The old USGS groundwater-levels API is retired; use the Water Data API's field-measurements collection.
6. Wells data includes owner names: keep only use, depth, water levels and pump rate.
7. Get two free API keys: NASS Quick Stats (also Crop Progress) and USDA AMS MyMarketNews (prices).
8. Plenty of clear imagery: ~20 scenes under 20% cloud over the test area from July 1 to Aug 15, 2026.
11. Apply Sentinel-2 scale/offset from each asset's metadata when using the C1 collection.
12. Replayed hazards need the Iowa Environmental Mesonet archive; the NWS API only serves current alerts.
13. `get_disease_reports` source: EDDMapS county data behind the Corn ipmPIPE maps.
14. Drought needs a derivation rule from county percentages to one category.
15. Hail thresholds in mm (1 in = 25.4 mm); keep SPC ground reports as corroboration.
16. Groundwater is context only, never scoring evidence.

**Models and accounts**
17. New AWS accounts go through verification (about 1.5 hours here); Bedrock inference is denied until it clears.
27. On the spike account, Claude 5-family models were "not available for this account"; Claude 4.x (Sonnet 4.6, Sonnet 4.5, Opus 4.6, Haiku 4.5) worked. Check the team account.
28. Anthropic models need the one-time use-case form (Bedrock console → model catalog → any Claude model). Nova models need no form and are the fallback for every role.
19. New accounts cap Lambda concurrency at 10. Request an increase to 1,000 early; until then no reserved concurrency and keep Map concurrency low.

**AgentCore and Strands**
29. The Python starter toolkit is deprecated. Use the **AgentCore CLI** (`npm install @aws/agentcore`, locally per project). Its commands are `create`, `add`, `deploy`, `invoke`, `logs`, `traces`, `status`, `run`; there is no `configure`.
30. Use the **CodeZip** build: on Windows, `uv` packs Linux ARM64 wheels into a zip (43 MB in the spike). No Docker, no container, no ECR. `uv` must be installed.
31. The AgentCore CLI deploys with **CDK**: `agentcore.json` (resources, tags) + `aws-targets.json` (account, region) + a generated CDK app. First deploy ~100 s including CDK bootstrap; code-only redeploys ~60 s.
32. The generated agent template (entrypoint, per-session agent cache, model loader, MCP client) is a good starting shape. It wires in a third-party MCP server (`mcp.exa.ai`) by default: remove it. Session IDs must be ≥ 33 characters.
33. Latency through the CLI: cold invoke ~11 s, warm ~6 s for a one-tool turn, ~13.5 s for a turn that lists and calls a Gateway tool.
20. Gateway with IAM inbound auth: a role AgentCore can assume with `lambda:InvokeFunction`; gateway authorizer `AWS_IAM`; Lambda target with inline tool schemas and credential provider `GATEWAY_IAM_ROLE`. READY in under a minute. Exception level DEBUG gives readable errors while building.
21. Clients sign MCP calls with SigV4 for service `bedrock-agentcore`, POSTing JSON-RPC to the gateway's `/mcp` URL. ~0.25 s gateway overhead per tool call.
22. Tool names arrive as `<target-name>___<tool>`. The Lambda event is just the tool arguments; tool name, gateway ID and request ID are in the client context (`bedrockAgentCoreToolName`, …).
23. The Gateway validates arguments against the tool's input schema before calling the Lambda. Making `approval_id` / `investigation_id` required in write-tool schemas is a free first enforcement layer.
24. Tool results come back as MCP text content (the Lambda's JSON as a string); errors as results with `isError: true`.
34. Agent → Gateway from inside Runtime needs a small httpx auth class that SigV4-signs each MCP request (~20 lines) and `bedrock-agentcore:InvokeGateway` on the runtime role.
25. Python SDKs need the `botocore[crt]` extra to use `aws login` credentials.

**AWS side effects and teardown**
36. CDK bootstrap (which the AgentCore CLI runs automatically on first deploy) creates a KMS key (~$1/month) plus a versioned S3 bucket and an ECR repo that are retained when the stack is deleted. Bootstrap once, knowingly.
37. The first agent invoke switches the account's X-Ray traces to CloudWatch Transaction Search (span indexing 100%, `aws/spans` log group). That powers AgentCore Observability; set indexing to a deliberate percentage.
38. Teardown order: inline policies added outside CloudFormation → agent stack → gateway targets, then gateway → Lambdas → IAM roles → empty and delete the CDK bootstrap bucket (all versions) → log groups.

**SMS**
39. AWS End User Messaging needs a registered toll-free number or 10DLC to text real US phones (toll-free up to 15 business days; 10DLC 4–10 weeks). Not possible this weekend: Twilio trial (verified numbers) plus the phone mirror is the path.

**Windows gotchas**
35. The Windows AWS CLI can't read Git Bash `/tmp` paths in `file://` arguments; some CLIs' `--help` hangs unless stdin is closed; set `MSYS_NO_PATHCONV=1` so `/aws/lambda/...` isn't rewritten as a file path.
