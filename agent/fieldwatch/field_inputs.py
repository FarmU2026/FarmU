"""Gather a field's inputs for the evidence engine (evidence.FieldInputs) from the stores and tools we have.

Best effort and never raises: whatever cannot be read stays None and the engine reports the keys that needed it as
`not_evaluable`. Fixture farms (local runs, tests) give the canned weather and hail flag of demo_farm.json and no
satellite analysis; the dynamo backend reads the latest fw-scene-analyses row (which carries the parallel lane's
`crop_state`) and fw-field-context, and asks the weather, hail and drought tools for the rest.

`readers` lets a test or a caller supply any part directly (a callable per part: analysis, context, weather, hail,
drought), so no network is touched.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

from . import contracts as C
from . import evidence as E
from . import farmdata, runfacts

log = logging.getLogger(__name__)

HAIL_LOOKBACK_DAYS = 21


def irrigated_from(text: Any) -> bool | None:
    s = str(text or "").strip().lower()
    if not s or s == "unknown":
        return None
    return not any(w in s for w in ("dryland", "dry land", "rainfed", "rain-fed", "none", "no irrigation"))


def analysis_from_row(row: dict | None, anomaly: C.AnomalyDetected | None = None) -> C.FieldAnalysisResult | None:
    """A FieldAnalysisResult from a fw-scene-analyses row (flat dict) and/or the AnomalyDetected event of this run.
    Missing parts stay None. The row may carry `indices` and `crop_state` (the analysis lane writes them)."""
    row = row or {}
    if not row and anomaly is None:
        return None

    def part(model, value):
        if not value:
            return None
        try:
            return model.model_validate(value)
        except Exception:  # noqa: BLE001 - one bad part must not hide the rest
            log.warning("could not read %s from the analysis row", model.__name__)
            return None

    regions: list[C.AnomalyRegion] = []
    if anomaly is not None:
        regions = list(anomaly.regions)
    else:
        for r in row.get("regions") or []:
            got = part(C.AnomalyRegion, r)
            if got is not None:
                regions.append(got)
    ref = anomaly.field if anomaly is not None else C.FieldRef(farm_id=str(row.get("farm_id", "")), field_id=str(row.get("field_id", "")))
    when = anomaly.date if anomaly is not None else str(row.get("date") or "")
    if not when:
        return None
    summary = C.AnomalySummary(field_score=float(row.get("max_score") or (max((r.score for r in regions), default=0.0))),
                               z_baseline=row.get("z_baseline"), z_cohort=row.get("z_cohort"),
                               delta_since_last=row.get("delta_since_last"), regions=regions)
    try:
        return C.FieldAnalysisResult(
            field=ref, scene_id=str(row.get("scene_id") or (anomaly.scene_id if anomaly else "")), date=when,
            status=row.get("status") or "ok", clear_fraction=float(row.get("clear_fraction") or 0.0),
            indices=part(C.Indices, row.get("indices")), anomaly=summary, comparison=part(C.OverlayComparison, row.get("comparison")),
            crop_state=part(C.CropState, row.get("crop_state")))
    except Exception:  # noqa: BLE001
        log.warning("could not build the analysis for %s", ref.field_id, exc_info=True)
        return None


def _context_from_row(row: dict | None) -> C.FieldContext | None:
    if not row:
        return None
    try:
        return C.FieldContext.model_validate({k: v for k, v in row.items() if k in C.FieldContext.model_fields})
    except Exception:  # noqa: BLE001
        log.warning("could not read the field context", exc_info=True)
        return None


def _guard(fn: Callable[[], Any], what: str) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a missing input is not_evaluable, not a failed investigation
        log.warning("could not read %s", what, exc_info=True)
        return None


def load_field_inputs(field: str, *, anomaly: C.AnomalyDetected | None = None, readers: dict[str, Callable] | None = None,
                      farmer: E.FarmerInputs | None = None, photo: E.PhotoFindings | None = None,
                      parts: set[str] | None = None) -> E.FieldInputs:
    """FieldInputs for a field (id or the farmer's name for it). Never raises. `parts={"analysis"}` reads only the
    field and its latest satellite analysis (the crop-state gate and the chat context need nothing else)."""
    from . import tools as T

    rd = readers or {}
    f = _guard(lambda: T._field(field), "the field") or {}
    fid = f.get("id") or field
    inputs = E.FieldInputs(field_id=fid, field_name=f.get("name") or field, crop=str(f.get("crop") or "other").lower(),
                           irrigated=irrigated_from(f.get("irrigation")), acres=f.get("acres"),
                           farmer=farmer or E.FarmerInputs(), photo=photo or E.PhotoFindings())
    row = (rd["analysis"](fid) if "analysis" in rd else _guard(lambda: farmdata.latest_analysis(fid), "the latest analysis"))
    inputs.analysis = analysis_from_row(row, anomaly)
    inputs.region_ndre = (row or {}).get("region_ndre")
    if parts is not None and parts <= {"analysis"}:
        return inputs
    ctx_row = rd["context"](fid) if "context" in rd else _guard(lambda: farmdata.field_context_row(fid), "the field context")
    inputs.context = ctx_row if isinstance(ctx_row, C.FieldContext) else _context_from_row(ctx_row)
    if isinstance(ctx_row, dict) and "disease_reports" in ctx_row:
        inputs.disease_reports = [r for r in (_guard(lambda: C.DiseaseReport.model_validate(x), "a disease report")
                                              for x in ctx_row.get("disease_reports") or []) if r is not None]
    wx = rd["weather"](fid) if "weather" in rd else _guard(lambda: T.get_weather(fid), "the weather")
    inputs.weather = wx if isinstance(wx, (C.WeatherSummary, dict)) and not (isinstance(wx, dict) and "error" in wx) else None
    # hail: a fixture farm only says whether hail was reported; the dynamo backend asks the hail tool
    canned = f.get("weather_30d") or {}
    if "hail" in rd:
        got = rd["hail"](fid)
        inputs.hail = got if isinstance(got, C.HailReport) else None
    elif "hail_reported" in canned:
        inputs.hail_watches = ([{"type": "hail", "detail": {"peak_date": canned.get("hail_date")}}]
                               if canned["hail_reported"] else [])
    elif farmdata.backend() == "dynamo" and inputs.analysis is not None:
        end = date.fromisoformat(inputs.analysis.date)
        start = (end - timedelta(days=HAIL_LOOKBACK_DAYS)).isoformat()
        inputs.hail = _guard(lambda: _hail(fid, start, end.isoformat()), "hail")
    if "drought" in rd:
        got = rd["drought"](fid)
        inputs.drought = got if isinstance(got, C.DroughtStatus) else None
    elif farmdata.backend() == "dynamo" and inputs.context and inputs.context.county_fips and inputs.analysis is not None:
        inputs.drought = _guard(lambda: _drought(inputs.context.county_fips, inputs.analysis.date), "the Drought Monitor")
    return inputs


def _hail(field_id: str, start: str, end: str) -> C.HailReport | None:
    from . import hazards

    out = hazards.get_hail(field_id, start, end)
    return out if isinstance(out, C.HailReport) else C.HailReport.model_validate(out)


def _drought(county: str, as_of: str) -> C.DroughtStatus | None:
    from . import hazards

    out = hazards.get_drought(county, as_of)
    return out if isinstance(out, C.DroughtStatus) else C.DroughtStatus.model_validate(out)


def facts_for(inputs: E.FieldInputs, det) -> runfacts.RunFacts:
    """The code-owned facts of a run: the flagged region's words and place, its acres, the scorer's outcome, and what
    a person walking the spot should look for (from the catalog, for the leading candidates)."""
    region = inputs.region()
    scout = "; ".join(dict.fromkeys(c.scout_for for c in det.candidates if c.scout_for))
    return runfacts.RunFacts(
        field_id=inputs.field_id, field_name=inputs.field_name, where=(region.where if region else None),
        centroid=tuple(region.centroid) if region else None,  # type: ignore[arg-type]
        region_acres=region.area_acres if region else None, outcome=det.outcome, scout_for=scout,
        possible=[c.label for c in det.candidates if not c.named or c.tier == "possible"])
