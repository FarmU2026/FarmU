"""The only place the stage Lambdas reach the field-context and baseline functions (other lanes' code).

Everything goes through `fieldwatch.registry.BY_NAME[...]` (get_soil_profile, get_field_context,
get_disease_reports): those entries are stubs returning contract samples until Kurt's lane (WS4) replaces
their `fn`, and nothing here changes when that happens.

Two things are adapters rather than plain registry calls:

* `baseline()`: Aaron's lane (WS1), through `fieldwatch.interfaces.AnalysisAPI.build_baseline`:

      fieldwatch.analysis.baseline.build_baseline(*, field_id: str, polygon: list[list[float]],
          tile_ids: list[str], crop: str, bucket: str | None) -> contracts.FieldBaseline | dict

  Until that module exists the adapter falls back to the registry stub (cohort ids from the
  get_field_context sample, no parquet key, seasons []), marked `"stub": true`.
* `_registry()`: `fieldwatch.registry` imports `fieldwatch.tools`, which imports the Strands SDK, and the
  Strands SDK is not in the stage Lambdas' bundle (pip's cross-platform resolution fails on Windows hosts).
  When the registry can't be imported the adapters read the same contract samples the registry stubs
  return, directly from fixtures/contracts/. Every result carries `stub: true` in that case or when the
  registry entry is itself a stub, so the state machine output shows when a stage ran on sample data.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from fieldwatch import contracts as C
from fieldwatch import interfaces
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
        log.warning("fieldwatch.registry unavailable (%s); reading contract samples directly", exc)
        return None


def _sample(name: str, model):
    return model.model_validate(json.loads((FIXTURES / "contracts" / f"{name}.json").read_text()))


def _call(tool: str, sample: str, model, **kwargs) -> Result:
    by_name = _registry()
    if by_name is None:
        return Result(_sample(sample, model), True)
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
    res = _call("get_disease_reports", "disease_report", C.DiseaseReport,
                county_fips=county_fips, crop=crop, season=season)
    reports = res.value if isinstance(res.value, list) else [res.value]  # one report or a list of them
    return Result(reports, res.stub)


def baseline(field_id: str, polygon: list, tile_ids: list[str], crop: str) -> Result:
    """Result.value is a contracts.FieldBaseline as a dict (the stage stores it as FieldContext.baseline)."""
    build = interfaces.build_baseline_fn()  # None until WS1 lands; a missing dependency of it still raises
    if build is not None:
        out = build(field_id=field_id, polygon=polygon, tile_ids=tile_ids, crop=crop,
                    bucket=os.environ.get("BUCKET_NAME"))
        value = C.FieldBaseline.model_validate(out.model_dump() if hasattr(out, "model_dump") else dict(out))
        return Result(value.model_dump(mode="json"), False)
    ctx = get_field_context(field_id)
    stub = C.FieldBaseline(cohort_field_ids=list(ctx.value.cohort_field_ids), stub=True)
    return Result(stub.model_dump(mode="json"), True)
