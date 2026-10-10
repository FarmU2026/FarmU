"""Web API (WS12): today's UI routes (/api/...), reply hand-off and the Twilio webhook, against moto."""
import json
import sys
import types

import pytest

from cloud.api import _reply, dashboard, farms, pages, routes, sms_inbound
from test_api_helpers import BUCKET, NORTH_PIVOT_LATLON, aws, call, event, put_analysis, signup  # noqa: F401

FARM = "4025550100"
FIELD = f"fld_{FARM}_north-pivot"


@pytest.mark.parametrize("group,module", [("farms", farms), ("pages", pages), ("dashboard", dashboard),
                                          ("sms_inbound", sms_inbound)])
def test_route_table_matches_routers(group, module):
    assert module.router.route_keys == routes.ROUTES[group]


# ---------- POST /api/farms ----------


def test_create_farm_shape_and_storage(aws):
    code, farm, _ = signup(farms)
    assert code == 200
    # Today's UI reads these keys.
    assert farm["farm_id"] == FARM
    assert farm["farmer"]["name"] == "Dad"
    f = farm["fields"][0]
    assert f["id"] == FIELD and f["name"] == "North Pivot" and f["crop"] == "corn"
    assert f["polygon"][0] == [-96.548, 42.168]  # stored lon/lat
    assert 550 < f["acres"] < 620  # ~1.3 km x 1.8 km
    assert "stats" not in f  # no pass yet
    # Welcome lands in the thread when no send Lambda is configured.
    assert [m["who"] for m in farm["thread"]] == ["fieldwatch"]
    assert "watching 1 field" in farm["thread"][0]["text"]

    person = aws.table("farms").get_item(Key={"farm_id": FARM, "item": f"PERSON#per_{FARM}"})["Item"]
    assert person["phone_e164"] == "+14025550100" and person["role"] == "owner"
    field = aws.table("fields").get_item(Key={"farm_id": FARM, "field_id": FIELD})["Item"]
    assert field["status"] == "building" and len(field["polygon"]) == 4
    geo = json.loads(aws.s3.get_object(Bucket=BUCKET, Key=f"fields/{FIELD}/geometry.geojson")["Body"].read())
    assert geo["geometry"]["coordinates"][0][0] == geo["geometry"]["coordinates"][0][-1]


def test_create_farm_starts_context_build_and_scene_check(aws, monkeypatch):
    sm = aws.sfn.create_state_machine(name="fw-context-builder", definition=json.dumps(
        {"StartAt": "x", "States": {"x": {"Type": "Pass", "End": True}}}),
        roleArn="arn:aws:iam::123456789012:role/fw-sfn")["stateMachineArn"]
    monkeypatch.setenv("FIELDWATCH_CONTEXT_SM_ARN", sm)
    monkeypatch.setenv("FIELDWATCH_SCENE_QUEUE_URL", aws.queue_url)
    code, _, _ = signup(farms)
    assert code == 200
    execs = aws.sfn.list_executions(stateMachineArn=sm)["executions"]
    assert len(execs) == 1
    inp = json.loads(aws.sfn.describe_execution(executionArn=execs[0]["executionArn"])["input"])
    assert inp == {"schema_version": "1", "farm_id": FARM, "field_ids": [FIELD]}
    farm_item = aws.table("farms").get_item(Key={"farm_id": FARM, "item": "FARM"})["Item"]
    assert farm_item["context_build_id"] == execs[0]["name"]
    msgs = aws.queue_messages()
    assert msgs[0]["type"] == "check" and msgs[0]["field_ids"] == [FIELD] and msgs[0]["reason"] == "signup"


def test_resignup_replaces_fields(aws):
    signup(farms, fields=[{"name": "A", "polygon": NORTH_PIVOT_LATLON}, {"name": "A", "polygon": NORTH_PIVOT_LATLON}])
    _, farm, _ = signup(farms, fields=[{"name": "B", "polygon": NORTH_PIVOT_LATLON}])
    assert [f["name"] for f in farm["fields"]] == ["B"]


@pytest.mark.parametrize("body", [
    {"phone": "123", "fields": [{"name": "x", "polygon": NORTH_PIVOT_LATLON}]},
    {"phone": "4025550100", "fields": [{"name": "x", "polygon": NORTH_PIVOT_LATLON[:2]}]},
    {"phone": "4025550100", "fields": []},
    {"phone": "4025550100"},
])
def test_create_farm_invalid(aws, body):
    code, out, _ = call(farms, "POST", "/api/farms", body)
    assert code == 400 and out["error"]["code"] == "INVALID_INPUT"


def test_bad_json_and_unknown_route(aws):
    resp = farms.handler({"rawPath": "/api/farms", "requestContext": {"http": {"method": "POST"}}, "body": "{nope"}, None)
    assert resp["statusCode"] == 400
    code, out, _ = call(farms, "GET", "/api/nothing")
    assert code == 404 and out["error"]["code"] == "NOT_FOUND"


# ---------- GET /api/farms/{id} ----------


def test_get_farm_with_stats_and_thread(aws):
    signup(farms)
    put_analysis(aws, FIELD, "2026-07-11")
    put_analysis(aws, FIELD, "2026-07-18")
    aws.table("farms").put_item(Item={"farm_id": FARM, "item": "THREAD#2026-07-18T17:30:00Z#m2", "at": "2026-07-18T17:30:00Z",
                                      "who": "farmer", "text": "YES"})
    code, farm, _ = call(farms, "GET", f"/api/farms/{FARM}", route_key="GET /api/farms/{farm_id}")
    assert code == 200
    st = farm["fields"][0]["stats"]
    assert st["scene_date"] == "2026-07-18"
    assert st["health_index_median"] == 0.83 and st["health_index_low_10pct"] == 0.52
    assert st["weakest_area"].startswith("south")
    assert 0 < st["share_of_field_below_0_3"] < 1
    # Sort-key order: the July item comes before today's welcome.
    assert farm["thread"][0] == {"at": "2026-07-18T17:30:00Z", "who": "farmer", "text": "YES"}
    assert farm["thread"][1]["kind"] == "welcome"


def test_get_missing_farm(aws):
    code, out, _ = call(farms, "GET", "/api/farms/nope")
    assert code == 404 and out == {"error": {"code": "NOT_FOUND", "message": "No such farm", "retryable": False}}


# ---------- check, ask, images, status ----------


def test_check_now_sends_to_queue(aws, monkeypatch):
    signup(farms)
    monkeypatch.setenv("FIELDWATCH_SCENE_QUEUE_URL", aws.queue_url)
    code, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/check")
    assert code == 200 and farm["check_queued"] is True and farm["last_checked"]
    (msg,) = aws.queue_messages()
    assert msg["type"] == "check" and msg["farm_id"] == FARM and msg["fields"][0]["polygon"][0] == [-96.548, 42.168]


def test_check_without_queue_is_harmless(aws):
    signup(farms)
    code, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/check")
    assert code == 200 and farm["check_queued"] is False


def test_ask_emits_start_investigation(aws):
    signup(farms)
    code, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/fields/{FIELD}/ask")
    assert code == 200 and farm["investigation_id"].startswith("inv_")
    (dt, detail), = [e for e in aws.events if e[0] == "StartInvestigation"]
    assert detail == {"schema_version": "1", "action": "start", "investigation_id": farm["investigation_id"],
                      "farm_id": FARM, "field": FIELD, "anomaly": None}


def test_ask_unknown_field(aws):
    signup(farms)
    code, out, _ = call(farms, "POST", f"/api/farms/{FARM}/fields/nope/ask")
    assert code == 404


def test_image_redirects_to_presigned_url(aws):
    signup(farms)
    put_analysis(aws, FIELD)
    code, _, headers = call(farms, "GET", f"/api/farms/{FARM}/fields/{FIELD}/truecolor.png")
    assert code == 302
    assert "analysis/north-pivot/2026-07-18/s2/true_color.png" in headers["Location"]
    assert "Signature" in headers["Location"] or "X-Amz-Signature" in headers["Location"]
    code, _, headers = call(farms, "GET", f"/api/farms/{FARM}/fields/{FIELD}/health.png")
    assert code == 302 and "health.png" in headers["Location"]


def test_image_missing(aws):
    signup(farms)
    assert call(farms, "GET", f"/api/farms/{FARM}/fields/{FIELD}/truecolor.png")[0] == 404
    put_analysis(aws, FIELD)
    assert call(farms, "GET", f"/api/farms/{FARM}/fields/{FIELD}/truecolor.jpg")[0] == 404
    assert call(farms, "GET", f"/api/farms/{FARM}/fields/{FIELD}/other.png")[0] == 404


def test_status(aws):
    code, st, _ = call(farms, "GET", "/api/status")
    assert code == 200 and st["sms_provider"] == "none" and "model" in st


# ---------- reply (web phone panel) ----------


def test_reply_more_falls_back_to_reply_received(aws):
    signup(farms)
    code, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "more"})
    assert code == 200 and farm["reply"]["intent"] == "more"
    types_ = [e[0] for e in aws.events]
    assert types_ == ["MessageReceived", "ReplyReceived"]
    rr = aws.events[1][1]
    assert rr["farm_id"] == FARM and rr["person_id"] == f"per_{FARM}" and rr["intent"] == "more"


def test_reply_ask_starts_investigation_on_named_field(aws):
    signup(farms, fields=[{"name": "North Pivot", "polygon": NORTH_PIVOT_LATLON},
                          {"name": "South", "polygon": NORTH_PIVOT_LATLON}])
    code, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "ASK south"})
    assert code == 200
    (_, detail), = [e for e in aws.events if e[0] == "StartInvestigation"]
    assert detail["field"] == f"fld_{FARM}_south"


def test_reply_stop_mutes(aws):
    signup(farms)
    _, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "STOP"})
    assert farm["muted"] is True
    assert [e[0] for e in aws.events] == ["MessageReceived"]
    _, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "start"})
    assert farm["muted"] is False


def test_reply_yes_with_pending_approval_and_no_approvals_module(aws):
    signup(farms)
    aws.table("approvals").put_item(Item={
        "approval_id": "apr_1", "investigation_id": "inv_1", "sent_to_person_id": f"per_{FARM}", "status": "pending",
        "expires_at": "2999-01-01T00:00:00Z", "summary": "Send a repair ticket"})
    call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "Yes"})
    rr = [e[1] for e in aws.events if e[0] == "ReplyReceived"][0]
    assert rr["intent"] == "yes" and rr["approval_id"] == "apr_1" and rr["investigation_id"] == "inv_1"


def test_reply_yes_uses_record_decision_when_available(aws, monkeypatch):
    calls = []
    fake = types.ModuleType("fieldwatch.approvals")
    fake.record_decision = lambda **kw: calls.append(kw) or {"status": kw["decision"]}
    monkeypatch.setitem(sys.modules, "fieldwatch.approvals", fake)
    signup(farms)
    aws.table("approvals").put_item(Item={
        "approval_id": "apr_1", "investigation_id": "inv_1", "sent_to_person_id": f"per_{FARM}", "status": "pending",
        "expires_at": "2999-01-01T00:00:00Z"})
    _, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "NO"})
    assert calls == [{"approval_id": "apr_1", "decision": "declined", "decided_by": f"per_{FARM}", "channel": "sms"}]
    assert farm["reply"]["status"] == "approval_recorded"
    assert "ReplyReceived" not in [e[0] for e in aws.events]


def test_reply_prefers_notify_inbound_module(aws, monkeypatch):
    calls = []
    pkg = types.ModuleType("fieldwatch.notify.inbound")
    pkg.handle = lambda frm, body, channel: calls.append((frm, body, channel)) or {"status": "ok"}
    monkeypatch.setitem(sys.modules, "fieldwatch.notify.inbound", pkg)
    signup(farms)
    _, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "YES"})
    assert calls == [("+14025550100", "YES", "mirror")] and farm["reply"] == {"status": "ok"}
    assert aws.events == []


def test_reply_attaches_open_investigation(aws):
    signup(farms)
    from cloud.api import _aws
    aws.table("investigations").put_item(Item={"investigation_id": "inv_9", "field_id": FIELD, "status": "open",
                                               "created_at": _aws.now_iso()})
    call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "it rained a lot here"})
    rr = [e[1] for e in aws.events if e[0] == "ReplyReceived"][0]
    assert rr["intent"] == "free_text" and rr["investigation_id"] == "inv_9"


@pytest.mark.parametrize("text,intent", [("Y", "yes"), (" no. ", "no"), ("MORE", "more"), ("ask north", "ask"),
                                         ("Stop", "stop"), ("help", "help"), ("photo", "photo"), ("hmm", "free_text")])
def test_classify(text, intent):
    assert _reply.classify(text) == intent


# ---------- POST /sms/inbound (Twilio) ----------

FORM = {"From": "+14025550100", "Body": "MORE", "MessageSid": "SM123", "To": "+14025550199"}


def _twilio(form, headers=None, b64=False):
    return call(sms_inbound, "POST", "/sms/inbound", form=form, headers=headers, b64=b64)


def test_sms_inbound_without_token_param_still_works_and_dedupes(aws):
    signup(farms)
    code, body, headers = _twilio(FORM, b64=True)
    assert code == 200 and headers["Content-Type"] == "text/xml" and "<Response>" in body
    assert [e[0] for e in aws.events] == ["MessageReceived", "ReplyReceived"]
    _twilio(FORM)  # Twilio retry with the same MessageSid
    assert len(aws.events) == 2


def test_sms_inbound_signature(aws):
    signup(farms)
    aws.ssm.put_parameter(Name="/fieldwatch/twilio-auth-token", Value="tw-secret", Type="SecureString")
    url = "https://abc123.execute-api.us-west-2.amazonaws.com/sms/inbound"
    good = sms_inbound.twilio_signature("tw-secret", url, FORM)
    assert _twilio(FORM, headers={"X-Twilio-Signature": "bad"})[0] == 403
    assert aws.events == []
    assert _twilio(FORM, headers={"X-Twilio-Signature": good})[0] == 200
    assert [e[0] for e in aws.events] == ["MessageReceived", "ReplyReceived"]


def test_twilio_signature_known_vector():
    # Example from Twilio's security docs.
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234", "From": "+12349013030",
              "To": "+18005551212"}
    sig = sms_inbound.twilio_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params)
    assert sig == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


def test_sms_inbound_unknown_number(aws):
    code, _, _ = _twilio({**FORM, "From": "+19995550000"})
    assert code == 200 and aws.events == []


def test_lambda_event_with_route_key(aws):
    signup(farms)
    resp = farms.handler(event("GET", f"/api/farms/{FARM}", route_key="GET /api/farms/{farm_id}") | {
        "pathParameters": {"farm_id": FARM}}, None)
    assert resp["statusCode"] == 200 and json.loads(resp["body"])["farm_id"] == FARM
