# WS3 hazards

`get_hail(field_id, start, end)` uses the MRMS 1,440-minute maximum ending
at midnight after each requested UTC calendar day. Rasterio invokes GDAL's GRIB
driver through `/vsigzip//vsicurl/`; values remain millimetres. Negative,
nonfinite and GDAL nodata values are excluded. Missing coverage raises an error
rather than returning zero hail. `pct_area_over_threshold` is a fraction in
[0, 1], using the union of impacted valid pixels over the date range. At the
0.01-degree MRMS resolution, all-touched pixel coverage approximates farm area.
SPC daily files span 12Z to 12Z; overnight reports are assigned the following
UTC date and reports are selected within 10 km of the field boundary. Unknown
sizes remain null. Ground reports corroborate radar; they never replace MESH.

`get_drought(county_fips, as_of)` requests cumulative county statistics. The
worst category whose cumulative area (that category or worse) reaches 50% is
the county label. Output percentages are exclusive categories and sum to 100%.
Use the latest map date on or before `as_of`; never use a future map.

`get_warnings(polygon, start, end)` intersects field geometry with archived IEM
storm-based warning polygons in an inclusive UTC date range. NWS current alerts
cannot replay past warnings. This is a core helper, without a new registry
schema or contract.

The hail filter accepts raw S3 notifications, SNS envelopes and EventBridge
`MrmsFileAvailable` replay details. Stable watch IDs and row keys preserve
redeliveries. Watch rows use epoch `expires_at` for DynamoDB TTL; emitted contract
watches use ISO strings. Existing closed watches are preserved. EventBridge is
at least once: a failure after event publication but before marking the row can
repeat `WatchOpened`; consumers must deduplicate by watch ID.

The drought poller writes a `drought` extension in `fw-field-context` because
the fixed `FieldContext` contract does not include that field. Model consumers
must select known fields, and retrieve drought through `get_drought` or this
row extension. A conditional update prevents older replay dates overwriting
newer drought context. A D2+ category change opens a watch before context writes
so failed event delivery can retry.

All owned resources are us-west-2. The `AWS::SNS::Subscription` uses the existing
NOAA us-east-1 topic, with no topic or forwarder created there. Both Lambda roles
have `fieldwatch-boundary`. No deployment is performed by this workstream.

Sources:
- https://registry.opendata.aws/noaa-mrms-pds/
- https://droughtmonitor.unl.edu/DmData/DataDownload/WebServiceInfo.aspx
- https://droughtmonitor.unl.edu/About/AbouttheData/StatisticsExplanation.aspx
- https://mesonet.agron.iastate.edu/geojson/sbw.py?help
- https://www.spc.noaa.gov/climo/reports/
