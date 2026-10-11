"""Link-token routes for the phone pages (docs/implementation-specs.md "Web API and live feed").

    GET  /v1/reports/{investigation_id}            report      -> contracts.ReportView
    POST /v1/approvals/{approval_id}/decision      report      {decision: approve|decline, reason?} -> {status}
    POST /v1/uploads                               upload      {kind, content_type, field_id, investigation_id?, location?}
    GET  /v1/uploads/{upload_id}                   upload      -> {status, message?}
    GET  /v1/work-orders/{action_id}               work_order  -> contracts.WorkOrderView
    POST /v1/work-orders/{action_id}/done          work_order  {photo_upload_id?} -> 200; emits ActionCompleted
    POST /v1/farms/{farm_id}/people                onboard     {name, phone_e164, role, channel_pref} -> 201

Every route reads the token from `?t=`. Handler: cloud.api.pages.handler.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from boto3.dynamodb.conditions import Attr
from pydantic import BaseModel, Field

from fieldwatch import contracts as C
from fieldwatch import interfaces
from fieldwatch.interfaces import FieldwatchError

from . import _approvals, _auth, _aws, store
from ._http import ApiError, Request, Response, Router, json_response

router = Router("pages")
handler = router.handler

CONDITION_LABELS = {
    "pivot_failure": "Pivot not watering",
    "hail": "Hail",
    "drought": "Dry weather",
    "ponding": "Standing water",
    "nitrogen": "Low nitrogen",
    "herbicide_drift": "Weed-spray drift",
    "planter_skip": "Planter skips",
    "disease_pest": "Disease or insects",
    "sandy_soil_water_stress": "Sandy soil drying out",
    "unknown": "Something else",
}
UPLOAD_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/heic": "heic", "image/webp": "webp",
                "application/pdf": "pdf"}


def _investigation(investigation_id: str) -> dict:
    inv = _aws.get_item("investigations", investigation_id=investigation_id)
    if not inv:
        raise ApiError("NOT_FOUND", "No such report")
    return inv


def _field_ref(inv: dict) -> tuple[str | None, str | None]:
    f = inv.get("field")
    if isinstance(f, dict):
        return f.get("farm_id"), f.get("field_id")
    return inv.get("farm_id"), inv.get("field_id") or (f if isinstance(f, str) else None)


def _approval_for(inv: dict) -> dict | None:
    if inv.get("approval_id"):
        return _aws.get_item("approvals", approval_id=inv["approval_id"])
    items = _aws.scan_all(_aws.table("approvals"), FilterExpression=Attr("investigation_id").eq(inv["investigation_id"]))
    items.sort(key=lambda a: str(a.get("created_at") or a.get("expires_at") or ""), reverse=True)
    return items[0] if items else None


def _approval_status(a: dict) -> str:
    st = a.get("status", "pending")
    if st == "pending" and str(a.get("expires_at", "9999")) < _aws.now_iso():
        return "expired"
    return st


def _reason(inv: dict, condition: str, status: str) -> str:
    want = "refutes" if status == "ruled_out" else "supports"
    ev = [e for e in inv.get("evidence") or [] if e.get("hypothesis") == condition]
    pick = next((e for e in ev if e.get("direction") == want), ev[0] if ev else None)
    return (pick or {}).get("reason", "")


def build_report(inv: dict) -> C.ReportView:
    farm_id, field_id = _field_ref(inv)
    field = (_aws.get_item("fields", farm_id=farm_id, field_id=field_id) if farm_id and field_id else None) \
        or (store.find_field(field_id) if field_id else None) or {}
    anomaly = inv.get("anomaly") or {}
    overlays = inv.get("overlays") or anomaly.get("overlays")
    row = {"overlays": overlays, "date": inv.get("scene_date") or anomaly.get("date")} if overlays else None
    if row is None and field_id:
        row = store.latest_analysis(field_id, need_overlays=True)
    if not row:
        raise ApiError("NOT_FOUND", "The images for this report are not ready yet", retryable=True)

    def url(kind: str) -> str | None:
        key = store.overlay_key(row, kind)
        return _aws.presign_get(key, expires=3600) if key else None

    diagnosis = inv.get("diagnosis") or {}
    summary = diagnosis.get("summary") or inv.get("summary") or "We are still looking at this field."
    name = field.get("name") or field_id or "Your field"
    hyps = sorted(inv.get("hypotheses") or [], key=lambda h: -float(h.get("probability", 0)))
    checked = [C.CheckedItem(condition=h["condition"], label=CONDITION_LABELS.get(h["condition"], h["condition"]),
                             status=h.get("status", "open"), reason=_reason(inv, h["condition"], h.get("status", "open")))
               for h in hyps]

    action = None
    appr = _approval_for(inv)
    if appr:
        action = C.ReportAction(approval_id=appr["approval_id"],
                                label=appr.get("summary") or (inv.get("proposed_action") or {}).get("summary") or "Approve",
                                status=_approval_status(appr), decided_by=appr.get("decided_by"))

    regs = store.regions(anomaly) or store.regions(row)
    if regs:
        top = max(regs, key=lambda r: float(r.get("score") or 0))
        lon, lat = top.get("centroid") or store.centroid(field.get("polygon") or [[0, 0]])
    elif field.get("polygon"):
        lon, lat = store.centroid(field["polygon"])
    else:
        lon, lat = 0.0, 0.0
    bounds = store.overlay_bounds(row) or [lon, lat, lon, lat]
    sent_at, updated_at = inv.get("message_sent_at"), inv.get("updated_at")
    return C.ReportView(
        field=C.ReportField(field_id=field_id or "", name=name, acres=float(field.get("acres") or 0)),
        scene_date=row.get("date") or "",
        overlays=C.ReportOverlays(true_color_url=url("true_color") or "", health_url=url("health") or "",
                                  mask_url=url("mask"), bounds=tuple(bounds)),
        summary=C.ReportSummary(headline=inv.get("headline") or f"{name}: {summary}",
                                detail=diagnosis.get("detail") or inv.get("detail") or summary),
        checked=checked,
        impact=C.ImpactEstimate(**inv["impact"]) if inv.get("impact") else None,
        action=action,
        navigate_url=store.maps_url(lon, lat),
        updated_since_sent=bool(sent_at and updated_at and str(updated_at) > str(sent_at)),
    )


@router.route("GET", "/v1/reports/{investigation_id}")
def get_report(req: Request):
    inv_id = req.params["investigation_id"]
    claims = _auth.link(req, "report", subject_id=inv_id)
    inv = _investigation(inv_id)
    _auth.same_farm(claims, _field_ref(inv)[0])
    return build_report(inv).model_dump(mode="json")


class DecisionIn(BaseModel):
    decision: Literal["approve", "decline"]
    reason: str | None = Field(default=None, max_length=500)


@router.route("POST", "/v1/approvals/{approval_id}/decision")
def decide(req: Request):
    approval_id = req.params["approval_id"]
    claims = _auth.link(req, "report")
    body: DecisionIn = req.model(DecisionIn)
    appr = _aws.get_item("approvals", approval_id=approval_id)
    if not appr:
        raise ApiError("NOT_FOUND", "No such approval")
    if appr.get("investigation_id") != claims.subject_id:
        raise ApiError("TOKEN_SCOPE", "This link is for a different report", 403)
    status = _approval_status(appr)
    if status != "pending":
        who = appr.get("decided_by")
        raise ApiError("ALREADY_DECIDED", f"Already {status}" + (f" by {who}" if who else "")
                       + (f" at {appr['decided_at']}" if appr.get("decided_at") else ""))
    decision = "approved" if body.decision == "approve" else "declined"
    _approvals.record_decision(approval_id, decision, appr.get("sent_to_person_id") or "web", "web")
    return {"status": decision}


# ---------- uploads ----------


class Location(BaseModel):
    lon: float
    lat: float
    accuracy_m: float | None = None


class UploadIn(BaseModel):
    kind: Literal["photo", "document"]
    content_type: str
    field_id: str
    investigation_id: str | None = None
    location: Location | None = None


@router.route("POST", "/v1/uploads")
def create_upload(req: Request):
    claims = _auth.link(req, "upload")
    body: UploadIn = req.model(UploadIn)
    ext = UPLOAD_TYPES.get(body.content_type.lower())
    if not ext:
        raise ApiError("INVALID_INPUT", f"content_type must be one of {sorted(UPLOAD_TYPES)}")
    if claims.subject_id not in (body.field_id, body.investigation_id):
        raise ApiError("TOKEN_SCOPE", "This link is for a different field", 403)
    field = store.find_field(body.field_id)
    if not field:
        raise ApiError("NOT_FOUND", "No such field")
    _auth.same_farm(claims, field["farm_id"])
    if not _aws.bucket():
        raise ApiError("NOT_IMPLEMENTED", "Uploads bucket is not configured", 501)
    upload_id = store.new_id("upl_")
    key = f"uploads/{upload_id}/original.{ext}"
    expires = 900
    _aws.table("uploads").put_item(Item=_aws.to_ddb({
        "upload_id": upload_id, "kind": body.kind, "farm_id": field["farm_id"], "field_id": body.field_id,
        "investigation_id": body.investigation_id, "location": body.location.model_dump() if body.location else None,
        "content_type": body.content_type, "s3_key": key, "status": "pending", "created_at": _aws.now_iso(),
        "expires_at": _aws.epoch() + 365 * 86400,
    }))
    exp_iso = (datetime.now(timezone.utc) + timedelta(seconds=expires)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json_response({"upload_id": upload_id, "put_url": _aws.presign_put(key, body.content_type, expires),
                          "expires_at": exp_iso, "content_type": body.content_type}, 201)


@router.route("GET", "/v1/uploads/{upload_id}")
def get_upload(req: Request):
    claims = _auth.link(req, "upload")
    up = _aws.get_item("uploads", upload_id=req.params["upload_id"])
    if not up:
        raise ApiError("NOT_FOUND", "No such upload")
    _auth.same_farm(claims, up.get("farm_id"))
    out: dict[str, Any] = {"status": up.get("status", "pending")}
    if up.get("message"):
        out["message"] = up["message"]
    return out


# ---------- work orders ----------


def _action(action_id: str) -> dict:
    act = _aws.get_item("actions", action_id=action_id)
    if not act:
        raise ApiError("NOT_FOUND", "No such work order")
    return act


@router.route("GET", "/v1/work-orders/{action_id}")
def get_work_order(req: Request):
    action_id = req.params["action_id"]
    claims = _auth.link(req, "work_order", subject_id=action_id)
    act = _action(action_id)
    inv = _aws.get_item("investigations", investigation_id=act.get("investigation_id", "")) or {}
    farm_id, field_id = _field_ref(inv) if inv else (act.get("farm_id"), act.get("field_id"))
    _auth.same_farm(claims, farm_id or act.get("farm_id"))
    field_id = act.get("field_id") or field_id
    field = (store.find_field(field_id) if field_id else None) or {}
    pin = act.get("pin") or (store.centroid(field["polygon"]) if field.get("polygon") else None)
    if not pin:
        raise ApiError("NOT_FOUND", "This work order has no location yet")
    return C.WorkOrderView(
        action_id=action_id, field_name=act.get("field_name") or field.get("name") or field_id or "the field",
        pin=tuple(pin), instructions=act.get("instructions", ""), status=act.get("status", "sent"),
        navigate_url=store.maps_url(pin[0], pin[1]),
    ).model_dump(mode="json")


class DoneIn(BaseModel):
    photo_upload_id: str | None = None


@router.route("POST", "/v1/work-orders/{action_id}/done")
def work_order_done(req: Request):
    action_id = req.params["action_id"]
    _auth.link(req, "work_order", subject_id=action_id)
    body: DoneIn = req.model(DoneIn)
    act = _action(action_id)
    if act.get("status") == "done":
        return {"status": "done", "done_at": act.get("done_at")}
    # The actions lane (WS8) owns the conditional flip to done and emits ActionCompleted exactly once.
    try:
        done = interfaces.actions().mark_done(action_id, act.get("assignee_person_id") or "work_order_link",
                                              photo_upload_id=body.photo_upload_id)
    except FieldwatchError as e:
        raise ApiError(e.code, e.message, e.http_status, e.retryable) from None
    return {"status": done.status, "done_at": done.done_at}


# ---------- onboarding: people ----------


class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone_e164: str
    role: Literal["owner", "operator", "repairs", "scout", "other"] = "other"
    channel_pref: Literal["sms", "voice", "none"] = "sms"


@router.route("POST", "/v1/farms/{farm_id}/people")
def add_person(req: Request):
    farm_id = req.params["farm_id"]
    claims = _auth.link(req, "onboard")
    _auth.same_farm(claims, farm_id)
    store.get_farm_item(farm_id)
    body: PersonIn = req.model(PersonIn)
    phone = store.e164(body.phone_e164)
    if len(phone) < 11:
        raise ApiError("INVALID_INPUT", "phone_e164: enter a full phone number")
    person_id = store.new_id("per_")
    _aws.table("farms").put_item(Item=_aws.to_ddb({
        "farm_id": farm_id, "item": f"PERSON#{person_id}", "person_id": person_id, "name": body.name,
        "phone_e164": phone, "role": body.role, "channel_pref": body.channel_pref, "opted_out": False,
        "opt_in": "pending", "created_at": _aws.now_iso(),
    }))
    return Response(status=201, body={"person_id": person_id, "opt_in": "pending"})
