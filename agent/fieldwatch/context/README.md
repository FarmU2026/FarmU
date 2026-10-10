# context/ (WS4 field context, Kurt)

Static facts about a field, as `FieldContext` / `SoilProfile` / `DiseaseReport` (contracts.py).
Suggested modules: `soil.py` (NRCS Soil Data Access), `terrain.py` (3DEP 1 m low spots),
`boundaries.py` (USDA CSB 2018–2025: crop history and same-crop neighbors; EPSG:5070; cache per farm),
`disease.py` (EDDMapS county reports), `economics.py` (NASS if a key is set, else crop defaults).
Replace the stubs `get_soil_profile`, `get_field_context`, `get_disease_reports` in registry.py.
See docs/spike-findings.md for the exact endpoints that worked.
