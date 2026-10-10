# hazards/ (WS3 hazards, Kurt)

Weather hazards as evidence: `get_hail` → `HailReport` (MRMS MESH in mm via GDAL, SPC reports within 10 km),
`get_drought` → `DroughtStatus` (US Drought Monitor county percentages + a written rule),
historical warnings from the Iowa Environmental Mesonet archive (the NWS API can't replay past dates).
Every function takes a date range or as-of date (replay). Replace the stubs in registry.py.
