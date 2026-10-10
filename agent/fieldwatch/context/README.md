# Field context (WS4)

Pure source clients and deterministic reducers returning the existing SoilProfile,
TerrainSummary, DiseaseReport and FieldContext contracts. No Strands or web imports.

- Soil Data Access: clipped map-unit polygons, component-weighted root-zone AWC,
  depth-overlap texture summaries, sandy flag (at least half of component weight
  has at least 70% root-zone sand), and soil-zone GeoJSON.
- 3DEP: selects a 1 m tile containing the complete field, windowed elevation with
  a 30 m halo, mean slope and priority-flood depression mask (at least 0.15 m deep).
  Writes a GeoTIFF with 255 outside the field. Refuses windows above 4M pixels;
  fields crossing tiles need a later mosaic implementation.
- USDA CSB 2018-2025 rev23: GDAL range reads using pyogrio, EPSG:5070 farm envelope,
  persistent per-farm cache in S3 or a local cache. Crop history uses the greatest
  overlapping CSB polygon. Cohorts within 10 km use the latest available source
  year (2025 for current seasons); they are a historical same-crop proxy.
- Economics: NASS_API_KEY enables NASS annual county yield / national price.
  Excludes annual observations from the as-of year and records loaded afterward.
  Without a key or usable rows, corn/soybean/wheat/milo use explicit planning
  assumptions, with no market quote date. Alfalfa ton-based values are never
  returned in the contract's bushel units.
- Disease: county feed parser distinguishes current confirmations from history
  and excludes observations after the requested season. Configure
  FIELDWATCH_EDDMAPS_COUNTY_URL (with {species} and {season} placeholders), or
  FIELDWATCH_EDDMAPS_SNAPSHOT pointing to a JSON object keyed by species ID
  (84963, 17596, 17739). Feed rows contain county_fips/FIPS, status,
  first_observed and last_observed. The live map returned HTTP 403 during this
  implementation; the exact authorized county feed must be supplied by the
  data owner. Missing configuration raises a clear error, preserving a partial
  workflow result rather than fabricating confirmations.

The registry keeps its single DiseaseReport shape; get_disease_reports returns
the first confirmed report (then alphabetically) and raises LookupError when
none exist. disease_reports returns the full list, including an empty list,
for WS15. Contracts have not changed.

Cloud access uses fw-fields/by-field and fw-field-context from config. Completed
workflow cache rows are filtered to contract attributes so hazard extensions
and workflow metadata remain intact. A partial cached context stays partial.
WS15 invokes each component separately and writes only its owned attributes;
raster/vector stages use the shared geo_lambda image.

Local field lookup uses config.FARM_FILE. Artifacts use FIELDWATCH_CONTEXT_CACHE
or agent/data/context-cache; USDA farm cache defaults to the OS temporary directory.
With BUCKET_NAME / FIELDWATCH_BUCKET configured, artifacts and USDA cache use S3.
No credentials are created or deployments performed.

Validation: offline source and workflow tests; no AWS calls. USDA source URL was
verified with HTTP HEAD (200); a small SDA live check timed out. DEM/CSB live
geospatial reads and NASS authenticated queries still need deployment validation.

