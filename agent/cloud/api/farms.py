"""Today's UI routes (agent/web/server.py) on DynamoDB/S3/EventBridge, same JSON shapes.

    POST /api/farms                                   create farm + people + fields; start context build; scene check
    GET  /api/farms/{farm_id}                         farm + fields (+ stats) + thread (THREAD# items)
    POST   /api/farms/{farm_id}/fields                add a field {name, crop, polygon}; returns the farm view
    PATCH  /api/farms/{farm_id}/fields/{field_id}     edit {name?, crop?, polygon?, planting_date?}; field_id never changes
    DELETE /api/farms/{farm_id}/fields/{field_id}     stop watching the field (history rows are kept)
    POST /api/farms/{farm_id}/check                   invoke the scenes check-latest Lambda for the farm's fields
    POST /api/farms/{farm_id}/fields/{field_id}/ask   StartInvestigation on the bus
    POST /api/farms/{farm_id}/reply                   phone panel reply (mirror reply)
    GET  /api/farms/{farm_id}/fields/{field_id}/{image}   {kind}.png -> 302 presigned S3 URL
    GET  /api/status

Handler: cloud.api.farms.handler. No auth, like today's local app (see report: risks).
"""
from __future__ import annotations

import math
import os
import re
from datetime import date, timedelta
from typing import get_args

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
    planting_date: str | None = Field(default=None, description="optional YYYY-MM-DD; blank = the state's typical date")


class FarmIn(BaseModel):
    phone: str
    farmer: str = "Farmer"
    fields: list[FieldIn] = Field(min_length=1)


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=480)


class FieldAddIn(BaseModel):
    name: str = Field(max_length=80)
    crop: str = "corn"
    polygon: list[list[float]]
    planting_date: str | None = None


class FieldPatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    crop: str | None = None
    polygon: list[list[float]] | None = None
    planting_date: str | None = Field(default=None, description="YYYY-MM-DD; null or blank clears it")


def _valid_planting_date(value: str | None) -> str | None:
    """Optional planting date: blank -> None; else a real date from last year to a month ahead (a planned planting)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ApiError("INVALID_INPUT", "planting_date: use the form YYYY-MM-DD") from None
    today = date.today()
    if not (date(today.year - 1, 1, 1) <= d <= today + timedelta(days=30)):
        raise ApiError("INVALID_INPUT", "planting_date: must be this season's date (not in the far past or future)")
    return d.isoformat()


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
        _valid_planting_date(f.planting_date)

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
        if f.planting_date and f.planting_date.strip():
            item["planting_date"] = _valid_planting_date(f.planting_date)
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


_CROPS = set(get_args(C.Crop))


def _valid_crop(c: str | None) -> str:
    crop = _crop(c or "")
    if crop not in _CROPS:
        raise ApiError("INVALID_INPUT", f"crop: must be one of {', '.join(sorted(_CROPS))}")
    return crop


def _valid_name(name: str, others: list[dict]) -> str:
    name = (name or "").strip()
    if not name:
        raise ApiError("INVALID_INPUT", "name: give the field a name")
    if any(str(f.get("name", "")).strip().lower() == name.lower() for f in others):
        raise ApiError("INVALID_INPUT", f"name: this farm already has a field called {name!r}")
    return name


def _valid_polygon(polygon: list[list[float]]) -> list[list[float]]:
    if len(polygon) < 3 or any(len(p) != 2 or not all(math.isfinite(x) for x in p) for p in polygon):
        raise ApiError("INVALID_INPUT", "polygon: needs at least 3 [lat, lon] points")
    if any(not (-90 <= lat <= 90 and -180 <= lon <= 180) for lat, lon in polygon):
        raise ApiError("INVALID_INPUT", "polygon: points must be [lat, lon]")
    return polygon


def _new_field_id(farm_id: str, name: str, taken: set[str]) -> str:
    base = f"fld_{farm_id.removeprefix('farm_')}_{store.slug(name)}"
    fid, n = base, 2
    # Also skip ids of removed fields that still have scene history, so a new field never inherits old images.
    while fid in taken or store.latest_analysis(fid):
        fid, n = f"{base}-{n}", n + 1
    return fid


def _write_geometry(fid: str, name: str, polygon_lonlat: list[list[float]]) -> str | None:
    if not _aws.bucket():
        return None
    key = f"fields/{fid}/geometry.geojson"
    _aws.client("s3").put_object(Bucket=_aws.bucket(), Key=key, ContentType="application/geo+json",
                                 Body=_aws.dumps({"type": "Feature", "properties": {"field_id": fid, "name": name},
                                                  "geometry": store.geojson_polygon(polygon_lonlat)}).encode())
    return key


def _setup_field(farm_id: str, field: dict, reason: str) -> None:
    """The per-field setup sign-up does: context build (soil, history, floods ...) and a scene check."""
    try:
        build_id = _jobs.start_context_build(farm_id, [field["field_id"]])
        if build_id:
            _aws.table("farms").update_item(Key={"farm_id": farm_id, "item": "FARM"},
                                            UpdateExpression="SET context_build_id = :b",
                                            ExpressionAttributeValues={":b": build_id})
    except Exception:  # noqa: BLE001 - the edit is saved; the build can be restarted
        log.exception("context build start failed for %s/%s", farm_id, field["field_id"])
    try:
        _jobs.scene_check(farm_id, [field], reason=reason)
    except Exception:  # noqa: BLE001
        log.exception("scene check failed for %s/%s", farm_id, field["field_id"])


@router.route("POST", "/api/farms/{farm_id}/fields")
def add_field(req: Request):
    farm_id = req.params["farm_id"]
    body: FieldAddIn = req.model(FieldAddIn)
    store.get_farm_item(farm_id)
    existing = store.list_fields(farm_id)
    name = _valid_name(body.name, existing)
    crop = _valid_crop(body.crop)
    latlon = _valid_polygon(body.polygon)
    planting = _valid_planting_date(body.planting_date)
    fid = _new_field_id(farm_id, name, {f["field_id"] for f in existing})
    polygon = [[lon, lat] for lat, lon in latlon]
    item = {"farm_id": farm_id, "field_id": fid, "name": name, "crop": crop, "crop_current": crop,
            "acres": store.acres_latlon(latlon), "irrigation": "unknown", "irrigated": None,
            "polygon": polygon, "status": "building", "created_at": _aws.now_iso()}
    if planting:
        item["planting_date"] = planting
    key = _write_geometry(fid, name, polygon)
    if key:
        item["geometry_key"] = key
    _aws.table("fields").put_item(Item=_aws.to_ddb(item))
    _setup_field(farm_id, item, "signup")
    return store.farm_view(farm_id)


@router.route("PATCH", "/api/farms/{farm_id}/fields/{field_id}")
def update_field(req: Request):
    farm_id, field_id = req.params["farm_id"], req.params["field_id"]
    body: FieldPatchIn = req.model(FieldPatchIn)
    store.get_farm_item(farm_id)
    field = store.get_field(farm_id, field_id)
    sets: dict = {}
    if body.name is not None:
        sets["name"] = _valid_name(body.name, [f for f in store.list_fields(farm_id) if f["field_id"] != field_id])
    if body.crop is not None:
        sets["crop"] = sets["crop_current"] = _valid_crop(body.crop)
    if "planting_date" in body.model_fields_set:  # null / blank clears it (the state's typical date is used again)
        sets["planting_date"] = _valid_planting_date(body.planting_date)
    redrawn = body.polygon is not None
    if redrawn:
        latlon = _valid_polygon(body.polygon)
        sets["polygon"] = [[lon, lat] for lat, lon in latlon]
        sets["acres"] = store.acres_latlon(latlon)
        sets["status"] = "building"
    if not sets:
        raise ApiError("INVALID_INPUT", "send at least one of name, crop, polygon, planting_date")
    if redrawn or "name" in sets:
        key = _write_geometry(field_id, sets.get("name", field.get("name", field_id)),
                              sets.get("polygon") or field.get("polygon") or [])
        if key:
            sets["geometry_key"] = key
    names = {f"#a{i}": k for i, k in enumerate(sets)}
    _aws.table("fields").update_item(
        Key={"farm_id": farm_id, "field_id": field_id},
        UpdateExpression="SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(sets))),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":v{i}": _aws.to_ddb(v) for i, v in enumerate(sets.values())})
    if redrawn or ("crop" in sets and sets["crop"] != field.get("crop")):
        _setup_field(farm_id, {**field, **sets}, "signup")
    return store.farm_view(farm_id)


@router.route("DELETE", "/api/farms/{farm_id}/fields/{field_id}")
def delete_field(req: Request):
    farm_id, field_id = req.params["farm_id"], req.params["field_id"]
    store.get_farm_item(farm_id)
    store.get_field(farm_id, field_id)
    # Removing the fw-fields row is what stops the watching: check-latest and the context builder start from it.
    # Scene analyses, images and context rows are history keyed on field_id and are left in place.
    _aws.table("fields").delete_item(Key={"farm_id": farm_id, "field_id": field_id})
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
