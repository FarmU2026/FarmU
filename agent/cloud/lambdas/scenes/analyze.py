"""AnalyzeField (state machine step 2): runs in the geo container image (infra/docker/geo).

The analyzer is workstream 1's. This is only the adapter: validate the request, call the registry's `analyze_field`
(a stub returning the contract sample until WS1 lands), validate the result against the contract, return it as JSON.
The result's overlays are S3 keys (never inline images).
"""
from __future__ import annotations

import os
from contextlib import nullcontext
from typing import Any

from pydantic import BaseModel

from fieldwatch import contracts as C
from fieldwatch.interfaces import Env


def _overlay_scope():
    """WS1 overlay fix (touches WS2 lane minimally): this Lambda alone writes overlay PNGs to the data bucket (it has
    write grants); the read-only Gateway tool Lambda never enters this scope, so its analyze_field stays NullWriter."""
    bucket = os.environ.get(Env.BUCKET)
    if not bucket:
        return nullcontext()
    from fieldwatch.analysis.s3_writer import S3Writer
    from fieldwatch.analysis.writer import overlay_writer

    return overlay_writer(S3Writer(bucket, region=os.environ.get(Env.REGION)))


def analyze(event: dict) -> dict:
    from fieldwatch.registry import BY_NAME  # imported late: pulls in strands/rasterio only where it is needed

    request = C.FieldAnalysisRequest.model_validate(event)
    with _overlay_scope():
        out = BY_NAME["analyze_field"].fn(**request.model_dump())
    if isinstance(out, BaseModel):
        out = out.model_dump(mode="json")
    return C.FieldAnalysisResult.model_validate(out).model_dump(mode="json")


def handler(event: dict, context: Any = None) -> dict:
    return analyze(event)
