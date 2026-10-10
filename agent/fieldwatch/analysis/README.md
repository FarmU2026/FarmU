# analysis/ (WS1 field analyzer, Aaron)

Deterministic field analysis: `analyze(FieldAnalysisRequest) -> FieldAnalysisResult` (contracts.py).
Suggested modules: `scenes.py` (STAC search as of a date), `indices.py` (masking, NDVI/NDRE/NDMI),
`baseline.py` (same weeks in prior seasons, change since last pass), `regions.py` (z-score regions),
`patterns.py` (ring/wedge/edge/stripe, overlaps), `gates.py`, `render.py` (overlays; reuse imagery.py).
When it works, point `registry.analyze_field` at it and set `stub=False`.
No web-app or AWS-SDK imports: this code is packaged into a Lambda as-is.
