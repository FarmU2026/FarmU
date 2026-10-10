"""AnalyzeField (state machine step 2): runs in the geo container image (infra/docker/geo).

The analyzer is workstream 1's. This is only the adapter: validate the request, call the registry's `analyze_field`
(a stub returning the contract sample until WS1 lands), validate the result against the contract, return it as JSON.
The result's overlays are S3 keys (never inline images).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from fieldwatch import contracts as C


def analyze(event: dict) -> dict:
    from fieldwatch.registry import BY_NAME  # imported late: pulls in strands/rasterio only where it is needed

    request = C.FieldAnalysisRequest.model_validate(event)
    out = BY_NAME["analyze_field"].fn(**request.model_dump())
    if isinstance(out, BaseModel):
        out = out.model_dump(mode="json")
    return C.FieldAnalysisResult.model_validate(out).model_dump(mode="json")


def handler(event: dict, context: Any = None) -> dict:
    return analyze(event)
