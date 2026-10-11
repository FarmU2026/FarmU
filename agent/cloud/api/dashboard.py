"""Dashboard routes (operator; `Authorization: Bearer <token>` from POST /v1/auth/dashboard).

    POST /v1/auth/dashboard                 {passcode} -> {token, expires_at}  (passcode: SSM /fieldwatch/dashboard-passcode)
    GET  /v1/farms/{farm_id}/overview       -> {fields: [{field_id, name, geometry, status, latest_score}], open_investigations}
    GET  /v1/investigations/{investigation_id}  -> the fw-investigations item
    POST /v1/replay                         {scene_id | field_ids+as_of, farm_id?} -> 202 {replay_id}   (1 call / 30 s)
                                            invokes the scenes replay Lambda (contracts.ReplayRequest)
    POST /v1/replay/reset                   {farm_id?} -> 202 (scenes reset Lambda, contracts.ResetRequest)
    POST /v1/mirror/reply                   {person_id, text, farm_id?} -> 202 (runs the reply hand-off as an SMS)

Handler: cloud.api.dashboard.handler.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key
from pydantic import BaseModel, Field

from fieldwatch import contracts as C
from fieldwatch import links
from fieldwatch.interfaces import OPEN_INVESTIGATION_STATUSES, Env

from . import _auth, _aws, _jobs, _reply, store
from ._http import ApiError, Request, Response, Router

router = Router("dashboard")
handler = router.handler

TOKEN_TTL_HOURS = 12
REPLAY_GAP_S = 30


def _passcode_param() -> str:
    return os.getenv("FIELDWATCH_DASHBOARD_PASSCODE_PARAM", "/fieldwatch/dashboard-passcode")


class PasscodeIn(BaseModel):
    passcode: str = Field(min_length=1, max_length=200)


@router.route("POST", "/v1/auth/dashboard")
def auth_dashboard(req: Request):
    body: PasscodeIn = req.model(PasscodeIn)
    want = _aws.ssm_param(_passcode_param(), required=False)
    if not want:
        raise ApiError("NOT_IMPLEMENTED", "Dashboard passcode is not set up", 501)
    if not hmac.compare_digest(body.passcode.encode(), want.encode()):
        raise ApiError("INVALID_INPUT", "Wrong passcode", 401)
    token = links.sign("*", "dashboard", "operator", ttl_hours=TOKEN_TTL_HOURS)
    exp = links.verify(token, scope="dashboard").exp
    return {"token": token, "expires_at": datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def _recent_investigations(field_id: str, limit: int = 10) -> list[dict]:
    page = _aws.table("investigations").query(IndexName="by-field", KeyConditionExpression=Key("field_id").eq(field_id),
                                              ScanIndexForward=False, Limit=limit)
    return [_aws.from_ddb(i) for i in page.get("Items", [])]


@router.route("GET", "/v1/farms/{farm_id}/overview")
def overview(req: Request):
    claims = _auth.dashboard(req)
    farm_id = req.params["farm_id"]
    _auth.same_farm(claims, farm_id)
    store.get_farm_item(farm_id)
    fields, open_invs = [], []
    for f in store.list_fields(farm_id):
        row = store.latest_analysis(f["field_id"])
        fields.append({
            "field_id": f["field_id"], "name": f.get("name"), "acres": f.get("acres"), "crop": f.get("crop"),
            "geometry": store.geojson_polygon(f.get("polygon") or []), "status": f.get("status", "building"),
            "latest_score": row.get("max_score") if row else None,
            "latest_scene_date": row.get("date") if row else None,
        })
        for inv in _recent_investigations(f["field_id"]):
            if str(inv.get("status", "open")) in OPEN_INVESTIGATION_STATUSES:
                open_invs.append({k: inv.get(k) for k in ("investigation_id", "field_id", "status", "created_at",
                                                          "anomaly_id") if inv.get(k) is not None})
    return {"farm_id": farm_id, "fields": fields, "open_investigations": open_invs}


@router.route("GET", "/v1/investigations/{investigation_id}")
def investigation(req: Request):
    _auth.dashboard(req)
    inv = _aws.get_item("investigations", investigation_id=req.params["investigation_id"])
    if not inv:
        raise ApiError("NOT_FOUND", "No such investigation")
    return inv


class ReplayIn(BaseModel):
    scene_id: str | None = Field(default=None, min_length=3, max_length=200)
    field_ids: list[str] | None = None
    as_of: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    farm_id: str | None = None


def _rate_limit(name: str, gap_s: int) -> None:
    tbl = _aws.table("idempotency")
    now = _aws.epoch()
    try:
        tbl.put_item(Item={"key": f"{name}#lock", "locked_until": now + gap_s, "expires_at": now + 86400},
                     ConditionExpression="attribute_not_exists(#k) OR locked_until < :now",
                     ExpressionAttributeNames={"#k": "key"}, ExpressionAttributeValues={":now": now})
    except tbl.meta.client.exceptions.ConditionalCheckFailedException:
        raise ApiError("RATE_LIMITED", f"One replay every {gap_s} seconds; try again shortly", 429,
                       retryable=True) from None


@router.route("POST", "/v1/replay")
def replay(req: Request):
    _auth.dashboard(req)
    body: ReplayIn = req.model(ReplayIn)
    if not body.scene_id and not (body.field_ids and body.as_of):
        raise ApiError("INVALID_INPUT", "Send scene_id, or field_ids with as_of")
    if not _jobs.configured(Env.REPLAY_FN):
        raise ApiError("NOT_IMPLEMENTED", "Replay is not wired up yet (no replay function)", 501)
    _rate_limit("replay", REPLAY_GAP_S)
    replay_id = store.new_id("rpl_")
    _jobs.replay(C.ReplayRequest(replay_id=replay_id, scene_id=body.scene_id, field_ids=body.field_ids,
                                 as_of=body.as_of, farm_id=body.farm_id, requested_at=_aws.now_iso()))
    return Response(status=202, body={"replay_id": replay_id})


class ResetIn(BaseModel):
    farm_id: str | None = None


@router.route("POST", "/v1/replay/reset")
def replay_reset(req: Request):
    _auth.dashboard(req)
    body: ResetIn = req.model(ResetIn)
    result = _jobs.reset(C.ResetRequest(farm_id=body.farm_id, requested_at=_aws.now_iso()))
    if result is None:
        raise ApiError("NOT_IMPLEMENTED", "Reset is not wired up yet (no reset function)", 501)
    return Response(status=202, body={"status": "reset", "result": result})


class MirrorReplyIn(BaseModel):
    person_id: str
    text: str = Field(min_length=1, max_length=480)
    farm_id: str | None = None


@router.route("POST", "/v1/mirror/reply")
def mirror_reply(req: Request):
    claims = _auth.dashboard(req)
    body: MirrorReplyIn = req.model(MirrorReplyIn)
    _auth.same_farm(claims, body.farm_id)
    person = store.person_by_id(body.person_id, body.farm_id)
    if not person or not person.get("phone_e164"):
        raise ApiError("NOT_FOUND", "No such person")
    result = _reply.handle(person["phone_e164"], body.text, "mirror")
    return Response(status=202, body={"status": "accepted", "result": result})
