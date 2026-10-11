"""Decide (state machine step 3, also the per-field error recorder).

Rule (docs/implementation-specs.md "Decision"): emit AnomalyDetected when status == "ok", at least one region has
score >= z_threshold and area_acres >= min_region_acres, and no gate is set. Always writes the fw-scene-analyses row
(`date#source` sort key) and emits SceneAnalyzed.

Idempotency (docs "Replay correctness" 6): every emit is preceded by a conditional put in fw-idempotency, so running the
same scene twice without a reset is a no-op.
  analyzed#<field_id>#<scene_id>   gates SceneAnalyzed (not claimed for status "error", so a retry can succeed)
  anomaly#<field_id>#<date>        gates AnomalyDetected; anomaly_id = anm-<field_id>-<yyyymmdd>
A claim is released if the PutEvents that follows it fails, so the retry can emit.
Two tiles, one field (docs rule 5): the row for a date keeps the scene with the higher clear fraction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fieldwatch import contracts as C

from . import common as cm
from .common import log


@dataclass
class Decision:
    emit: bool
    reason: str
    regions: list[C.AnomalyRegion] = field(default_factory=list)
    anomaly_id: str | None = None


def anomaly_id_for(field_id: str, date: str) -> str:
    return f"anm-{field_id}-{date.replace('-', '')}"


def evaluate(result: C.FieldAnalysisResult, z_threshold: float, min_region_acres: float,
             field_id: str | None = None, date: str | None = None) -> Decision:
    """Pure decision rule. Regions are the ones that pass both thresholds, strongest first.

    field_id/date override the result's own (the request and scene are authoritative for keys)."""
    if result.status != "ok":
        return Decision(False, f"status is {result.status}")
    gates = result.gates
    if gates is not None:
        tripped = [name for name in ("senescence", "harvest", "regional") if getattr(gates, name)]
        if tripped:
            return Decision(False, "gate set: " + ", ".join(tripped))
    regions = result.anomaly.regions if result.anomaly else []
    passing = sorted((r for r in regions if r.score >= z_threshold and r.area_acres >= min_region_acres),
                     key=lambda r: r.score, reverse=True)
    if not passing:
        best = max((r.score for r in regions), default=None)
        why = "no regions" if best is None else f"no region passes (best score {best:g}, need {z_threshold:g} and {min_region_acres:g} acres)"
        return Decision(False, why)
    if result.overlays is None:
        return Decision(False, "regions pass but the analysis has no overlays")
    return Decision(True, f"{len(passing)} region(s) pass; top score {passing[0].score:g}", passing,
                    anomaly_id_for(field_id or result.field.field_id, date or result.date))


def _write_row(ref: C.FieldRef, scene: dict, replay: bool, row: dict) -> bool:
    """Put the fw-scene-analyses row unless another scene with a higher clear fraction already holds the date."""
    date, source = cm.scene_date(scene), scene["source"]
    item = {
        "field_id": ref.field_id, "date_source": f"{date}#{source}", "farm_id": ref.farm_id,
        "scene_id": scene["scene_id"], "source": source, "date": date, "replay": replay,
        "updated_at": cm.iso(cm.now()), "expires_at": cm.epoch_in(cm.SCENE_ANALYSIS_TTL_DAYS), **row,
    }
    try:
        cm.table("scene-analyses").put_item(
            Item=cm.to_ddb(item),
            ConditionExpression="attribute_not_exists(field_id) OR scene_id = :sid OR clear_fraction < :cf",
            ExpressionAttributeValues=cm.to_ddb({":sid": scene["scene_id"], ":cf": row["clear_fraction"]}),
        )
        return True
    except cm.ddb().meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _claim_and_emit(key: str, model: C.EventBase, **attrs: Any) -> bool:
    if not cm.claim(key, **attrs):
        return False
    try:
        cm.emit(model)
    except Exception:
        cm.release(key)
        raise
    return True


def decide_field(event: dict) -> dict:
    """event: {result, request, scene, replay, replay_id}."""
    request = C.FieldAnalysisRequest.model_validate(event["request"])
    result = C.FieldAnalysisResult.model_validate(event["result"])
    scene = event["scene"]
    replay = bool(event.get("replay"))
    ref = request.field
    if result.field != ref or result.scene_id != scene["scene_id"]:
        log.warning("analyzer returned %s/%s for request %s/%s; keying on the request",
                    result.field.field_id, result.scene_id, ref.field_id, scene["scene_id"])
    date = cm.scene_date(scene)
    decision = evaluate(result, request.z_threshold, request.min_region_acres, ref.field_id, date)
    regions = result.anomaly.regions if result.anomaly else []
    max_score = max((r.score for r in regions), default=result.anomaly.field_score if result.anomaly else 0.0)

    stored = _write_row(ref, scene, replay, {
        "status": result.status, "clear_fraction": result.clear_fraction, "max_score": max_score,
        "region_count": len(regions), "regions": [r.model_dump(mode="json") for r in regions],
        "overlays": result.overlays.model_dump(mode="json") if result.overlays else None,
        # Dates and color scales behind the departure / change / normal overlays (captions); None on older rows.
        "comparison": result.comparison.model_dump(mode="json") if result.comparison else None,
        "analysis_key": result.analysis_key, "emit": decision.emit, "reason": decision.reason,
        "anomaly_id": decision.anomaly_id, "open_watch_ids": request.open_watch_ids,
        # The analyzer reports its own failures as data (status "error" + error detail); keep the detail.
        "error": result.error.model_dump(mode="json") if result.error else None,
    })
    if result.error:
        log.error("analyzer error for %s on %s: %s", ref.field_id, scene["scene_id"], result.error.message)
    out: dict[str, Any] = {"field_id": ref.field_id, "status": result.status, "emit": decision.emit,
                           "reason": decision.reason}
    if not stored:
        out.update(status="superseded", emit=False, reason="another scene with a higher clear fraction holds this date")
        return out

    claim_attrs = {"farm_id": ref.farm_id, "replay": replay, "scene_id": scene["scene_id"]}
    analyzed = C.SceneAnalyzed(
        occurred_at=cm.iso(cm.now()), field=ref, scene_id=scene["scene_id"], date=date, status=result.status,
        max_score=max_score, region_count=len(regions), analysis_key=result.analysis_key)
    analyzed_key = f"analyzed#{ref.field_id}#{scene['scene_id']}"
    if result.status == "error":
        cm.emit(analyzed)
    else:
        _claim_and_emit(analyzed_key, analyzed, **claim_attrs)

    if decision.emit:
        anomaly = C.AnomalyDetected(
            occurred_at=cm.iso(cm.now()), anomaly_id=decision.anomaly_id, field=ref, scene_id=scene["scene_id"],
            date=date, regions=decision.regions, overlays=result.overlays, open_watch_ids=request.open_watch_ids,
            replay=replay)
        if _claim_and_emit(f"anomaly#{ref.field_id}#{date}", anomaly, anomaly_id=decision.anomaly_id, **claim_attrs):
            out["emitted_anomaly_id"] = decision.anomaly_id
        else:
            out["reason"] += " (anomaly for this field and date already emitted)"
    return out


def record_error(event: dict) -> dict:
    """event: {error: {Error, Cause}, scene, field, replay, replay_id}. Called from the state machine's Catch."""
    ref = C.FieldRef(**event["field"])
    scene = event["scene"]
    err = event.get("error") or {}
    detail = {"code": str(err.get("Error", "ERROR")), "message": str(err.get("Cause", ""))[:500]}
    log.error("field %s/%s failed on scene %s: %s", ref.farm_id, ref.field_id, scene["scene_id"], detail)
    _write_row(ref, scene, bool(event.get("replay")), {
        "status": "error", "clear_fraction": 0, "max_score": 0, "region_count": 0, "regions": [], "overlays": None,
        "comparison": None, "analysis_key": None, "emit": False, "reason": "error", "error": detail,
    })
    cm.emit(C.SceneAnalyzed(occurred_at=cm.iso(cm.now()), field=ref, scene_id=scene["scene_id"],
                            date=cm.scene_date(scene), status="error", max_score=0.0, region_count=0))
    return {"field_id": ref.field_id, "status": "error", "error": detail}


def handler(event: dict, context: Any = None) -> dict:
    if "result" not in event and "error" in event:
        return record_error(event)
    return decide_field(event)
