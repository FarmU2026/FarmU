"""Context workflow adapters. Source clients run without Strands installed.

Soil uses the registry when available; domain component functions keep terrain,
crop, economics and disease stages independent. The baseline implementation is
loaded lazily from WS1, with an explicitly marked fallback until it exists.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from fieldwatch import contracts as C
from fieldwatch.config import FIXTURES

log = logging.getLogger(__name__)


@dataclass
class Result:
    value: Any  # contract model (or list of them, or dict for baseline)
    stub: bool


def _registry():
    try:
        from fieldwatch.registry import BY_NAME
        return BY_NAME
    except ImportError as exc:  # strands (via fieldwatch.tools) missing in this runtime
        log.warning("fieldwatch.registry unavailable (%s); calling domain context functions directly", exc)
        return None


def _sample(name: str, model):
    return model.model_validate(json.loads((FIXTURES / "contracts" / f"{name}.json").read_text()))


def _call(tool: str, sample: str, model, **kwargs) -> Result:
    by_name = _registry()
    if by_name is None:
        from fieldwatch import context
        return Result(getattr(context, tool)(**kwargs), False)
    spec = by_name[tool]
    out = spec.fn(**kwargs)
    if isinstance(out, dict) and spec.output_model is not None:
        out = spec.output_model.model_validate(out)
    return Result(out, bool(spec.stub))


def get_soil_profile(field_id: str) -> Result:
    return _call("get_soil_profile", "soil_profile", C.SoilProfile, field_id=field_id)


def get_field_context(field_id: str) -> Result:
    return _call("get_field_context", "field_context", C.FieldContext, field_id=field_id)


def get_disease_reports(county_fips: str, crop: str, season: int) -> Result:
    from fieldwatch.context import disease_reports
    return Result(disease_reports(county_fips, crop, season), False)


def baseline(field_id: str, polygon: list, tile_ids: list[str], crop: str) -> Result:
    try:
        module = importlib.import_module("fieldwatch.analysis.baseline")
    except ModuleNotFoundError as exc:
        if exc.name != "fieldwatch.analysis.baseline":
            raise  # the module exists but one of its imports is missing (e.g. rasterio): a real failure
        module = None
    if module is not None:
        out = module.build_baseline(field_id=field_id, polygon=polygon, tile_ids=tile_ids, crop=crop,
                                    bucket=os.environ.get("BUCKET_NAME"))
        return Result(dict(out), False)
    from fieldwatch.context import cached_cohort_field_ids
    return Result({"key": None, "seasons": [], "cohort_field_ids": cached_cohort_field_ids(field_id), "stub": True},
                  True)


def get_terrain_summary(field_id):
    from fieldwatch.context import get_terrain_summary as compute
    return Result(compute(field_id), False)


def get_crop_context(field_id):
    from fieldwatch.context import get_crop_context as compute
    return Result(compute(field_id), False)


def get_economics(field_id):
    from fieldwatch.context import get_economics as compute
    return Result(compute(field_id), False)
