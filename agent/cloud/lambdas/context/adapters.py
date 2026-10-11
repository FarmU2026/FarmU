"""Context workflow adapters. Source clients run without Strands installed.

Every call into Kurt's lane (WS4) goes through `fieldwatch.interfaces.context()` (the `fieldwatch.context`
facade: real NRCS soil, 3DEP terrain, CSB crop history and neighbors, EDDMapS disease reports, economics, with
their defaults and caching). The stage Lambdas stay independent: one facade function per stage.

Two things are adapters rather than plain calls:

* `baseline()`: Aaron's lane (WS1), through `fieldwatch.interfaces.build_baseline_fn()`:

      fieldwatch.analysis.baseline.build_baseline(*, field_id: str, polygon: list[list[float]],
          tile_ids: list[str], crop: str, bucket: str | None) -> contracts.FieldBaseline | dict

  Until that module exists the adapter falls back to a stub baseline (cohort ids from the cached field
  context, no parquet key, seasons []), marked `"stub": true`.
* `_registry()`: soil and field context are called through `fieldwatch.registry.BY_NAME` when it imports, so a
  registry entry replaced or stubbed later is honored. `fieldwatch.registry` imports `fieldwatch.tools`, which
  imports the Strands SDK, and the Strands SDK is not in the stage Lambdas' bundle (pip's cross-platform
  resolution fails on Windows hosts). When the registry can't be imported the adapters call the context
  facade directly. `Result.stub` is true when the registry entry is itself a stub (and for the baseline
  fallback), so the state machine output shows when a stage ran on sample data.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from fieldwatch import contracts as C
from fieldwatch import interfaces

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


def _call(tool: str, **kwargs) -> Result:
    by_name = _registry()
    if by_name is None:
        return Result(getattr(interfaces.context(), tool)(**kwargs), False)
    spec = by_name[tool]
    out = spec.fn(**kwargs)
    if isinstance(out, dict) and spec.output_model is not None:
        out = spec.output_model.model_validate(out)
    return Result(out, bool(spec.stub))


def get_soil_profile(field_id: str) -> Result:
    return _call("get_soil_profile", field_id=field_id)


def get_field_context(field_id: str) -> Result:
    return _call("get_field_context", field_id=field_id)


def get_disease_reports(county_fips: str, crop: str, season: int) -> Result:
    return Result(interfaces.context().disease_reports(county_fips, crop, season), False)


def baseline(field_id: str, polygon: list, tile_ids: list[str], crop: str) -> Result:
    """Result.value is a contracts.FieldBaseline as a dict (the stage stores it as FieldContext.baseline)."""
    build = interfaces.build_baseline_fn()  # None until WS1 lands; a missing dependency of it still raises
    if build is not None:
        out = build(field_id=field_id, polygon=polygon, tile_ids=tile_ids, crop=crop,
                    bucket=os.environ.get("BUCKET_NAME"))
        value = C.FieldBaseline.model_validate(out.model_dump() if hasattr(out, "model_dump") else dict(out))
        return Result(value.model_dump(mode="json"), False)
    stub = C.FieldBaseline(cohort_field_ids=list(interfaces.context().cached_cohort_field_ids(field_id)), stub=True)
    return Result(stub.model_dump(mode="json"), True)


def get_terrain_summary(field_id):
    return Result(interfaces.context().get_terrain_summary(field_id), False)


def get_crop_context(field_id):
    return Result(interfaces.context().get_crop_context(field_id), False)


def get_economics(field_id):
    return Result(interfaces.context().get_economics(field_id), False)
