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
- Disease: the public EDDMapS annual-progress county JSON feed works without a
  token. Default and exact optional FIELDWATCH_EDDMAPS_COUNTY_URL value:
  `https://maps.eddmaps.org/annualprogress/data.cfm?sub={species}&year={season}`.
  Requests identify themselves as FieldWatch because CloudFront blocks the
  default requests user agent. The year query selects the requested season.
  The feed has `us`, `ca`, and `mx` maps; `us` is keyed by county FIPS (leading zeroes may be omitted).
  County values use `ObservationDate`, `LastObservationDate` (MM-DD-YYYY), and
  `Type` (`Certain` for current confirmations, `History` for historical reports).
  Only Certain records whose last observation is in the requested season are
  current; future observation dates are excluded. This seam accepts a season,
  not a within-season cutoff date. Unknown payload schemas and HTTP failures
  raise errors, preserving a partial workflow result.
  FIELDWATCH_EDDMAPS_SNAPSHOT optionally selects an offline JSON object keyed by
  species ID (84963, 17596, 17739), with each value a public feed payload or the
  existing exported county row list. An explicit URL takes precedence.
  A live check on October 10, 2026 for Thurston County, NE (31173), season 2026
  returned tar spot history (2021-10-06 through 2023-09-21), southern rust history
  (2018-07-26 through 2020-07-23), and no gray leaf spot county row. Absence is no
  county evidence, never evidence that a disease is absent from the field.

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

To repeat the public feed check (network required; no AWS):

```powershell
cd agent
python -m pytest tests/test_context_disease.py -q -m live -s
```
