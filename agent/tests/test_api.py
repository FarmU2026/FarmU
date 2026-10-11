"""Web API (WS12): today's UI routes (/api/...), reply hand-off and the Twilio webhook, against moto."""
import json
from fieldwatch import contracts as C

import pytest

from cloud.api import _approvals, _reply, dashboard, farms, pages, routes, sms_inbound
from test_api_helpers import BUCKET, NORTH_PIVOT_LATLON, aws, call, event, put_analysis, put_approval, signup  # noqa: F401

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
    monkeypatch.setenv("FIELDWATCH_CHECK_FN", "fw-scene-check-latest")
    code, _, _ = signup(farms)
    assert code == 200
    execs = aws.sfn.list_executions(stateMachineArn=sm)["executions"]
    assert len(execs) == 1
    inp = json.loads(aws.sfn.describe_execution(executionArn=execs[0]["executionArn"])["input"])
    assert inp == {"schema_version": "1", "farm_id": FARM, "field_ids": [FIELD]}
    farm_item = aws.table("farms").get_item(Key={"farm_id": FARM, "item": "FARM"})["Item"]
    assert farm_item["context_build_id"] == execs[0]["name"]
    (fn, kind, payload), = [c for c in aws.lambda_calls if c[0] == "fw-scene-check-latest"]
    req = C.CheckLatestRequest.model_validate(payload)
    assert kind == "Event" and req.field_ids == [FIELD] and req.reason == "signup" and req.farm_id == FARM
    assert aws.queue_messages() == []  # the API never writes to fw-scene-queue


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


def test_check_now_invokes_check_latest(aws, monkeypatch):
    signup(farms)
    monkeypatch.setenv("FIELDWATCH_CHECK_FN", "fw-scene-check-latest")
    code, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/check")
    assert code == 200 and farm["check_queued"] is True and farm["last_checked"]
    (_, kind, payload), = aws.lambda_calls
    assert kind == "Event" and C.CheckLatestRequest.model_validate(payload).reason == "check_now"
    assert payload["farm_id"] == FARM and payload["field_ids"] == [FIELD]


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
                      "farm_id": FARM, "field": FIELD, "anomaly": None,
                      "question": None, "about_investigation_id": None}


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


COMPARISON = {
    "scene_date": "2026-07-18", "previous_date": "2026-07-13", "baseline_dates": ["2023-07-15", "2024-07-18", "2025-07-16"],
    "baseline_method": "median", "median_departure_ndvi": -0.01,
    "departure_scale": {"stat": "z", "vmin": -3.0, "vmax": 3.0, "unit": "standard deviations"},
    "change_scale": {"stat": "ndvi_diff", "vmin": -0.25, "vmax": 0.25, "unit": "NDVI"},
}


def with_comparison_overlays(aws, **extra):
    from test_api_helpers import REPO

    ov = json.loads((REPO / "agent" / "fixtures" / "contracts" / "field_analysis_result.json").read_text())["overlays"]
    for kind in ("departure", "change", "normal"):
        ov[kind] = {"key": f"analysis/north-pivot/2026-07-18/s2/{kind}.png", "bounds": ov["health"]["bounds"]}
    return put_analysis(aws, FIELD, overlays=ov, comparison=COMPARISON, **extra)


def test_overlay_key_resolves_the_comparison_kinds():
    from cloud.api import store

    row = {"overlays": {k: {"key": f"a/{k}.png", "bounds": [0, 0, 1, 1]}
                        for k in ("true_color", "health", "mask", "departure", "change", "normal")}}
    assert [store.overlay_key(row, k) for k in ("departure", "change", "normal", "truecolor", "mask")] == [
        "a/departure.png", "a/change.png", "a/normal.png", "a/true_color.png", "a/mask.png"]
    assert store.overlay_key({"overlays": {"health": {"key": "h"}}}, "departure") is None   # older rows: no image
    assert store.overlay_kinds(row) == ["true_color", "health", "mask", "departure", "change", "normal"]
    assert store.overlay_kinds({"overlays": {"true_color": {"key": "t"}}}) == ["true_color"]


def test_comparison_images_are_served_by_the_existing_image_route(aws):
    signup(farms)
    with_comparison_overlays(aws)
    for kind in ("departure", "change", "normal"):
        code, _, headers = call(farms, "GET", f"/api/farms/{FARM}/fields/{FIELD}/{kind}.png")
        assert code == 302 and f"/s2/{kind}.png" in headers["Location"]


def test_comparison_image_missing_on_an_older_analysis(aws):
    signup(farms)
    put_analysis(aws, FIELD)  # the fixture row has no departure / change / normal
    assert call(farms, "GET", f"/api/farms/{FARM}/fields/{FIELD}/truecolor.png")[0] == 302
    for kind in ("departure", "change", "normal"):
        assert call(farms, "GET", f"/api/farms/{FARM}/fields/{FIELD}/{kind}.png")[0] == 404


def test_farm_payload_carries_the_overlay_captions_additively(aws):
    signup(farms)
    with_comparison_overlays(aws)
    code, farm, _ = call(farms, "GET", f"/api/farms/{FARM}", route_key="GET /api/farms/{farm_id}")
    f = farm["fields"][0]
    assert code == 200 and f["stats"]["scene_date"] == "2026-07-18" and f["last_scene_date"] == "2026-07-18"
    assert f["overlay_kinds"] == ["true_color", "health", "mask", "departure", "change", "normal"]
    assert f["overlay_date"] == "2026-07-18"
    assert f["comparison"]["previous_date"] == "2026-07-13"
    assert f["comparison"]["baseline_dates"] == ["2023-07-15", "2024-07-18", "2025-07-16"]
    assert f["comparison"]["departure_scale"]["vmax"] == 3.0


def test_farm_payload_for_an_older_row_lists_only_its_images(aws):
    signup(farms)
    put_analysis(aws, FIELD)
    f = call(farms, "GET", f"/api/farms/{FARM}", route_key="GET /api/farms/{farm_id}")[1]["fields"][0]
    assert f["overlay_kinds"] == ["true_color", "health", "mask"] and "comparison" not in f


def test_farm_payload_captions_follow_the_images_not_a_newer_cloudy_row(aws):
    signup(farms)
    with_comparison_overlays(aws)
    put_analysis(aws, FIELD, date="2026-07-20", status="too_cloudy", overlays=None)
    f = call(farms, "GET", f"/api/farms/{FARM}", route_key="GET /api/farms/{farm_id}")[1]["fields"][0]
    assert f["overlay_date"] == "2026-07-18" and f["comparison"]["scene_date"] == "2026-07-18"
    assert "stats" not in f  # unchanged: stats come from the newest row, which is not ok


def test_farm_payload_without_any_analysis_has_no_overlay_keys(aws):
    signup(farms)
    f = call(farms, "GET", f"/api/farms/{FARM}", route_key="GET /api/farms/{farm_id}")[1]["fields"][0]
    assert "overlay_kinds" not in f and "comparison" not in f


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


def test_reply_yes_records_the_approval_through_the_approvals_module(aws):
    signup(farms)
    put_approval(aws, "apr_1", investigation_id="inv_1")
    _, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "Yes"})
    assert farm["reply"] == {"status": "approval_recorded", "intent": "yes", "approval_id": "apr_1"}
    row = aws.table("approvals").get_item(Key={"approval_id": "apr_1"})["Item"]
    assert row["status"] == "approved" and row["decided_by"] == f"per_{FARM}" and row["channel"] == "sms"
    recorded = [e[1] for e in aws.events if e[0] == "ApprovalRecorded"]
    assert [(r["approval_id"], r["decision"], r["channel"]) for r in recorded] == [("apr_1", "approved", "sms")]
    assert "ReplyReceived" not in [e[0] for e in aws.events]  # one YES, one resume


def test_reply_no_declines(aws):
    signup(farms)
    put_approval(aws, "apr_1")
    _, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "NO"})
    assert farm["reply"]["status"] == "approval_recorded"
    assert aws.table("approvals").get_item(Key={"approval_id": "apr_1"})["Item"]["status"] == "declined"


def test_reply_yes_when_the_approval_was_already_taken_is_not_an_error(aws, monkeypatch):
    signup(farms)
    put_approval(aws, "apr_1")
    # The pending lookup sees it pending; the conditional write finds it decided (someone else won the race).
    monkeypatch.setattr(_reply, "_pending_approval", lambda person_id: {"approval_id": "apr_1", "investigation_id": "inv_1"})
    aws.table("approvals").update_item(Key={"approval_id": "apr_1"}, UpdateExpression="SET #s = :a, decided_by = :d",
                                       ExpressionAttributeNames={"#s": "status"},
                                       ExpressionAttributeValues={":a": "approved", ":d": "per_brother"})
    _, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "YES"})
    assert farm["reply"]["status"] == "approval_not_recorded" and farm["reply"]["code"] == "ALREADY_DECIDED"
    assert "per_brother" in farm["reply"]["message"]
    assert [e[0] for e in aws.events] == ["MessageReceived"]  # no ReplyReceived, no second ApprovalRecorded


def test_reply_yes_falls_back_to_reply_received_when_approvals_cannot_be_imported(aws, monkeypatch):
    monkeypatch.setattr(_approvals, "module", lambda: None)
    signup(farms)
    put_approval(aws, "apr_1", investigation_id="inv_1")
    call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "Yes"})
    rr = [e[1] for e in aws.events if e[0] == "ReplyReceived"][0]
    assert rr["intent"] == "yes" and rr["approval_id"] == "apr_1" and rr["investigation_id"] == "inv_1"
    assert aws.table("approvals").get_item(Key={"approval_id": "apr_1"})["Item"]["status"] == "pending"


def test_reply_prefers_the_notify_lane(aws, monkeypatch):
    """With WS9 deployed, a phone-panel reply goes to its mirror Lambda as an InboundMessage."""
    monkeypatch.setenv("FIELDWATCH_SEND_FN", "fw-notify-send")
    monkeypatch.setenv("FIELDWATCH_MIRROR_FN", "fw-notify-mirror")
    signup(farms)
    aws.lambda_calls.clear()
    aws.lambda_responses["fw-notify-mirror"] = {"status": "received", "message_id": "msg-in-1", "response_text": ""}
    _, farm, _ = call(farms, "POST", f"/api/farms/{FARM}/reply", {"text": "YES"})
    (fn, kind, payload), = aws.lambda_calls
    msg = C.InboundMessage.model_validate(payload)
    assert fn == "fw-notify-mirror" and kind == "RequestResponse"
    assert msg.from_phone == "+14025550100" and msg.body == "YES" and msg.provider_message_id.startswith("mirror:")
    assert farm["reply"]["status"] == "received" and aws.events == []


def test_sms_inbound_forwards_to_the_notify_reply_lambda(aws, monkeypatch):
    monkeypatch.setenv("FIELDWATCH_NOTIFY_REPLY_FN", "fw-notify-reply")
    aws.lambda_responses["fw-notify-reply"] = {"statusCode": 200, "headers": {"content-type": "application/xml"},
                                               "body": "<Response></Response>"}
    code, body, headers = _twilio(FORM)
    (fn, _, payload), = aws.lambda_calls
    assert fn == "fw-notify-reply" and payload["rawPath"] == "/sms/inbound" and code == 200
    assert body == "<Response></Response>" and aws.events == []


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


def test_sms_inbound_without_token_param_is_rejected_unless_unsigned_is_allowed(aws, monkeypatch):
    signup(farms)
    monkeypatch.delenv("FIELDWATCH_ALLOW_UNSIGNED_SMS", raising=False)
    assert _twilio(FORM)[0] == 403 and _twilio(FORM, headers={"X-Twilio-Signature": "anything"})[0] == 403
    assert aws.events == []
    monkeypatch.setenv("FIELDWATCH_ALLOW_UNSIGNED_SMS", "true")  # only the exact value 1 counts
    assert _twilio(FORM)[0] == 403 and aws.events == []


def test_sms_inbound_unsigned_dev_mode_still_works_and_dedupes(aws, monkeypatch):
    signup(farms)
    monkeypatch.setenv("FIELDWATCH_ALLOW_UNSIGNED_SMS", "1")
    code, body, headers = _twilio(FORM, b64=True)
    assert code == 200 and headers["Content-Type"] == "text/xml" and "<Response>" in body
    assert [e[0] for e in aws.events] == ["MessageReceived", "ReplyReceived"]
    _twilio(FORM)  # Twilio retry with the same MessageSid
    assert len(aws.events) == 2


def test_sms_inbound_signature(aws, monkeypatch):
    monkeypatch.setenv("FIELDWATCH_ALLOW_UNSIGNED_SMS", "1")  # a configured token still wins: dev mode never bypasses it
    signup(farms)
    aws.ssm.put_parameter(Name="/fieldwatch/twilio-auth-token", Value="tw-secret", Type="SecureString")
    url = "https://abc123.execute-api.us-west-2.amazonaws.com/sms/inbound"
    good = sms_inbound.twilio_signature("tw-secret", url, FORM)
    assert _twilio(FORM, headers={"X-Twilio-Signature": "bad"})[0] == 403
    assert _twilio(FORM)[0] == 403  # no signature header at all
    assert _twilio(FORM, headers={"X-Twilio-Signature": "bäd"})[0] == 403  # non-ASCII must not 500
    assert aws.events == []
    assert _twilio(FORM, headers={"X-Twilio-Signature": good})[0] == 200
    assert [e[0] for e in aws.events] == ["MessageReceived", "ReplyReceived"]


def test_twilio_signature_known_vector():
    # Example from Twilio's security docs.
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234", "From": "+12349013030",
              "To": "+18005551212"}
    sig = sms_inbound.twilio_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params)
    assert sig == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


def test_sms_inbound_unknown_number(aws, monkeypatch):
    monkeypatch.setenv("FIELDWATCH_ALLOW_UNSIGNED_SMS", "1")
    code, _, _ = _twilio({**FORM, "From": "+19995550000"})
    assert code == 200 and aws.events == []


def test_lambda_event_with_route_key(aws):
    signup(farms)
    resp = farms.handler(event("GET", f"/api/farms/{FARM}", route_key="GET /api/farms/{farm_id}") | {
        "pathParameters": {"farm_id": FARM}}, None)
    assert resp["statusCode"] == 200 and json.loads(resp["body"])["farm_id"] == FARM


# ---------- farm ids (no phone numbers in ids) ----------


def test_new_farms_get_random_ids_and_the_phone_finds_them_again(aws, monkeypatch):
    from cloud.api import store
    from fieldwatch import interfaces

    monkeypatch.setattr(store, "new_farm_id", interfaces.new_farm_id)  # undo the fixture's pinned id
    _, first, _ = signup(farms, phone="(402) 555-0177")
    fid = first["farm_id"]
    assert fid.startswith("farm_") and "5550177" not in fid and "4025550177" not in json.dumps(first["fields"])
    farm = aws.table("farms").get_item(Key={"farm_id": fid, "item": "FARM"})["Item"]
    assert farm["timezone"] == "America/Chicago" and "time_zone" not in farm  # notify reads `timezone`
    _, again, _ = signup(farms, phone="402-555-0177", fields=[{"name": "B", "polygon": NORTH_PIVOT_LATLON}])
    assert again["farm_id"] == fid and [f["name"] for f in again["fields"]] == ["B"]
