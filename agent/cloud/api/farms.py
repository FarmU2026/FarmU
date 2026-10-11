"""Today's UI routes (agent/web/server.py) on DynamoDB/S3/EventBridge, same JSON shapes.

    POST /api/farms                                   create farm + people + fields; start context build; scene check
    GET  /api/farms/{farm_id}                         farm + fields (+ stats) + thread (THREAD# items)
    POST /api/farms/{farm_id}/check                   invoke the scenes check-latest Lambda for the farm's fields
    POST /api/farms/{farm_id}/fields/{field_id}/ask   StartInvestigation on the bus
    POST /api/farms/{farm_id}/reply                   phone panel reply (mirror reply)
    GET  /api/farms/{farm_id}/fields/{field_id}/{image}   {kind}.png -> 302 presigned S3 URL
    GET  /api/status

Handler: cloud.api.farms.handler. No auth, like today's local app (see report: risks).
"""
from __future__ import annotations

import os
import re

from pydantic import BaseModel, Field

from fieldwatch import config
from fieldwatch import contracts as C
from fieldwatch import interfaces
from fieldwatch.interfaces import Env

from . import _aws, _jobs, _reply, store
from ._http import ApiError, Request, Router, log, redirect

router = Router("farms")
handler = router.handler


class FieldIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    crop: str = "corn"
    polygon: list[list[float]] = Field(description="[[lat, lon], ...] as clicked on the map")


class FarmIn(BaseModel):
    phone: str
    farmer: str = "Farmer"
    fields: list[FieldIn] = Field(min_length=1)


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=480)


def _crop(c: str) -> str:
    c = (c or "corn").lower()
    return {"soybean": "soybeans", "soy": "soybeans", "beans": "soybeans"}.get(c, c)


@router.route("POST", "/api/farms")
def create_farm(req: Request):
    body: FarmIn = req.model(FarmIn)
    if len(re.sub(r"\D", "", body.phone)) < 10:
        raise ApiError("INVALID_INPUT", "phone: enter a 10-digit phone number")
    for f in body.fields:
        if len(f.polygon) < 3 or any(len(p) != 2 for p in f.polygon):
            raise ApiError("INVALID_INPUT", f"field {f.name!r} needs at least 3 [lat, lon] points")

    now = _aws.now_iso()
    phone = store.e164(body.phone)
    # Signing up again with the same phone finds the same farm (GSI by-phone); a new phone gets a random id.
    existing = store.person_by_phone(phone)
    if existing and existing.get("role") == "owner":
        farm_id, person_id = existing["farm_id"], existing["person_id"]
    else:
        farm_id = store.new_farm_id()
        person_id = store.owner_person_id(farm_id)
    farms = _aws.table("farms")
    farms.put_item(Item=_aws.to_ddb(interfaces.FarmItem(
        farm_id=farm_id, name=f"{body.farmer}'s farm", farmer_name=body.farmer, phone=phone,
        owner_person_id=person_id, timezone="America/Chicago", quiet_hours={"start": "21:00", "end": "06:00"},
        muted=False, created_at=now).to_item()))
    farms.put_item(Item=_aws.to_ddb(interfaces.PersonItem(
        farm_id=farm_id, item=interfaces.person_item_key(person_id), person_id=person_id, name=body.farmer,
        phone_e164=phone, role="owner", channel_pref="sms", opt_in_at=now, opted_out=False).to_item()))

    # Re-signing up with the same phone replaces the field list (as the local app does).
    fields_tbl = _aws.table("fields")
    seen: set[str] = set()
    fields: list[dict] = []
    for f in body.fields:
        base = f"fld_{farm_id.removeprefix('farm_')}_{store.slug(f.name)}"
        fid, n = base, 2
        while fid in seen:
            fid, n = f"{base}-{n}", n + 1
        seen.add(fid)
        polygon = [[lon, lat] for lat, lon in f.polygon]  # stored lon/lat
        item = {
            "farm_id": farm_id, "field_id": fid, "name": f.name, "crop": _crop(f.crop), "crop_current": _crop(f.crop),
            "acres": store.acres_latlon(f.polygon), "irrigation": "unknown", "irrigated": None,
            "polygon": polygon, "status": "building", "created_at": now,
        }
        if _aws.bucket():
            key = f"fields/{fid}/geometry.geojson"
            _aws.client("s3").put_object(Bucket=_aws.bucket(), Key=key, ContentType="application/geo+json",
                                         Body=_aws.dumps({"type": "Feature", "properties": {"field_id": fid, "name": f.name},
                                                          "geometry": store.geojson_polygon(polygon)}).encode())
            item["geometry_key"] = key
        fields.append(item)
    for old in store.list_fields(farm_id):
        if old["field_id"] not in seen:
            fields_tbl.delete_item(Key={"farm_id": farm_id, "field_id": old["field_id"]})
    with fields_tbl.batch_writer() as bw:
        for item in fields:
            bw.put_item(Item=_aws.to_ddb(item))

    welcome = (f"Hi {body.farmer}, FieldWatch here. I am watching {len(fields)} field(s) and will text you "
               f"after every clear satellite pass. Reply STOP anytime.")
    sent = _jobs.send_message(C.SendMessageRequest(
        farm_id=farm_id, person_id=person_id, body=welcome, urgency="urgent",
        idempotency_key=f"{farm_id}#welcome#{now[:10]}"))
    if not sent:
        store.put_thread_item(farm_id, "fieldwatch", welcome, kind="welcome", source="api")

    try:
        build_id = _jobs.start_context_build(farm_id, [f["field_id"] for f in fields])
        if build_id:
            farms.update_item(Key={"farm_id": farm_id, "item": "FARM"},
                              UpdateExpression="SET context_build_id = :b", ExpressionAttributeValues={":b": build_id})
    except Exception:  # noqa: BLE001 - the farm is saved; the build can be restarted
        log.exception("context build start failed for %s", farm_id)
    try:
        _jobs.scene_check(farm_id, fields, reason="signup")
    except Exception:  # noqa: BLE001
        log.exception("scene check failed for %s", farm_id)
    return store.farm_view(farm_id)


@router.route("GET", "/api/farms/{farm_id}")
def get_farm(req: Request):
    return store.farm_view(req.params["farm_id"])


@router.route("POST", "/api/farms/{farm_id}/check")
def check_now(req: Request):
    farm_id = req.params["farm_id"]
    store.get_farm_item(farm_id)
    fields = store.list_fields(farm_id)
    try:
        queued = _jobs.scene_check(farm_id, fields, reason="check_now") is not None
    except Exception:  # noqa: BLE001 - the button should not 500 because the scenes lane is down
        log.exception("check-latest invoke failed for %s", farm_id)
        queued = False
    _aws.table("farms").update_item(Key={"farm_id": farm_id, "item": "FARM"},
                                    UpdateExpression="SET last_checked = :t", ExpressionAttributeValues={":t": _aws.now_iso()})
    return {**store.farm_view(farm_id), "check_queued": queued}


def _start_investigation(farm_id: str, field_id: str) -> str:
    investigation_id = store.new_id("inv_")
    _aws.put_event("StartInvestigation", C.StartInvestigation(
        investigation_id=investigation_id, farm_id=farm_id, field=field_id))
    return investigation_id


@router.route("POST", "/api/farms/{farm_id}/fields/{field_id}/ask")
def ask(req: Request):
    farm_id, field_id = req.params["farm_id"], req.params["field_id"]
    store.get_farm_item(farm_id)
    store.get_field(farm_id, field_id)
    inv = _start_investigation(farm_id, field_id)
    return {**store.farm_view(farm_id), "investigation_id": inv}


@router.route("POST", "/api/farms/{farm_id}/reply")
def reply(req: Request):
    farm_id = req.params["farm_id"]
    body: ReplyIn = req.model(ReplyIn)
    text = body.text.strip()
    if text.upper() == "ASK" or text.upper().startswith("ASK "):
        # Like the local app: ASK [field] starts an investigation on that field (default: the first).
        fields = store.list_fields(store.get_farm_item(farm_id)["farm_id"])
        if not fields:
            raise ApiError("NOT_FOUND", "This farm has no fields")
        want = text[4:].strip().lower()
        field = next((f for f in fields if want and want in (f["field_id"].lower(), str(f.get("name", "")).lower())),
                     fields[0])
        inv = _start_investigation(farm_id, field["field_id"])
        return {**store.farm_view(farm_id), "investigation_id": inv}
    result = _reply.handle(store.owner_phone(farm_id), text, "mirror")
    return {**store.farm_view(farm_id), "reply": result}


@router.route("GET", "/api/farms/{farm_id}/fields/{field_id}/{image}")
def image(req: Request):
    farm_id, field_id, image_name = req.params["farm_id"], req.params["field_id"], req.params["image"]
    if not image_name.endswith(".png"):
        raise ApiError("NOT_FOUND", "No such image")
    store.get_field(farm_id, field_id)
    row = store.latest_analysis(field_id, need_overlays=True)
    key = store.overlay_key(row, image_name[:-4]) if row else None
    if not key:
        raise ApiError("NOT_FOUND", "No image yet")
    return redirect(_aws.presign_get(key, expires=300))


@router.route("GET", "/api/status")
def status(req: Request):  # noqa: ARG001
    provider = os.getenv("FIELDWATCH_SMS_PROVIDER", "none")
    return {
        "sms_provider": provider,
        "sms_from": os.getenv("FIELDWATCH_SMS_FROM") or None,
        "model": config.MODEL_ID,
        "backend": "cloud",
        "check_latest": bool(os.getenv(Env.CHECK_FN)),
        "replay": bool(os.getenv(Env.REPLAY_FN)),
        "notify": bool(os.getenv(Env.SEND_FN)),
        "context_workflow": bool(os.getenv(Env.CONTEXT_SM_ARN)),
    }
