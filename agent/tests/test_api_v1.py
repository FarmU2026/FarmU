"""Web API (WS12): /v1 routes (link-token pages and dashboard) against moto."""
import json
from pathlib import Path

import pytest

from cloud.api import _approvals, _aws, dashboard, farms, pages
from cloud.api._http import ApiError
from fieldwatch import contracts as C
from fieldwatch import links
from test_api_helpers import aws, call, put_analysis, put_approval, signup  # noqa: F401

FARM = "4025550100"
FIELD = f"fld_{FARM}_north-pivot"
INV = "inv_north"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contracts"


def tok(scope, subject, farm=FARM, ttl=1.0):
    return links.sign(farm, scope, subject, ttl_hours=ttl)


@pytest.fixture
def seeded(aws):
    signup(farms)
    put_analysis(aws, FIELD)
    concluded = json.loads((FIXTURES / "investigation_concluded.json").read_text())
    aws.table("investigations").put_item(Item=_aws.to_ddb({
        "investigation_id": INV, "field": {"farm_id": FARM, "field_id": FIELD}, "field_id": FIELD,
        "status": "concluded", "created_at": "2026-07-18T17:21:05Z",
        "diagnosis": {"condition": concluded["condition"], "summary": concluded["summary"]},
        "hypotheses": concluded["hypotheses"], "impact": concluded["impact"],
        "proposed_action": concluded["proposed_action"],
        "evidence": [{"evidence_id": "e1", "hypothesis": "drought", "direction": "refutes", "weight_key": "rain_normal",
                      "source": "get_weather", "value": {"rain_mm_30d": 112}, "reason": "Rain over the last month was normal.",
                      "created_at": "2026-07-18T17:21:05Z"}],
    }))
    put_approval(aws, "apr_1", investigation_id=INV, sent_to_person_id=f"per_{FARM}",
                 summary="Send Brother a repair ticket")
    aws.table("actions").put_item(Item=_aws.to_ddb({
        "action_id": "act_1", "approval_id": "apr_1", "investigation_id": INV, "type": "work_order",
        "assignee_person_id": "per_brother", "pin": [-96.54, 42.1535], "status": "sent",
        "instructions": "Check the outer span nozzles and end tower on the north pivot"}))
    return aws


# ---------- reports ----------


def test_report_view(seeded):
    code, body, _ = call(pages, "GET", f"/v1/reports/{INV}", query={"t": tok("report", INV)})
    assert code == 200, body
    view = C.ReportView.model_validate(body)
    assert view.field.name == "North Pivot" and view.scene_date == "2026-07-18"
    assert "true_color.png" in view.overlays.true_color_url and "X-Amz-Signature" in view.overlays.health_url
    assert view.checked[0].condition == "pivot_failure" and view.checked[0].label == "Pivot not watering"
    drought = next(c for c in view.checked if c.condition == "drought")
    assert drought.status == "ruled_out" and drought.reason == "Rain over the last month was normal."
    assert view.action.approval_id == "apr_1" and view.action.status == "pending"
    assert view.impact.low_usd == 9000.0
    assert view.navigate_url == "https://maps.google.com/?q=42.15350,-96.54000"


@pytest.mark.parametrize("token_args,status,code", [
    (("work_order", INV), 403, "TOKEN_SCOPE"),
    (("report", "inv_other"), 403, "TOKEN_SCOPE"),
    (("report", INV, "other-farm"), 403, "TOKEN_SCOPE"),
    (("report", INV, FARM, -1), 401, "TOKEN_EXPIRED"),
])
def test_report_token_errors(seeded, token_args, status, code):
    got, body, _ = call(pages, "GET", f"/v1/reports/{INV}", query={"t": tok(*token_args)})
    assert got == status and body["error"]["code"] == code


def test_report_missing_token_and_missing_report(seeded):
    assert call(pages, "GET", f"/v1/reports/{INV}")[1]["error"]["code"] == "TOKEN_SCOPE"
    got, body, _ = call(pages, "GET", "/v1/reports/inv_x", query={"t": tok("report", "inv_x")})
    assert got == 404 and body["error"]["code"] == "NOT_FOUND"


# ---------- approval decisions ----------


def test_decision_without_approvals_module_is_501(seeded, monkeypatch):
    monkeypatch.setattr(_approvals, "module", lambda: None)  # the import-failure path
    got, body, _ = call(pages, "POST", "/v1/approvals/apr_1/decision", {"decision": "approve"},
                        query={"t": tok("report", INV)})
    assert got == 501 and body["error"]["code"] == "NOT_IMPLEMENTED"


def _row(aws, approval_id):
    return aws.table("approvals").get_item(Key={"approval_id": approval_id})["Item"]


def test_decision_records_via_approvals(seeded):
    got, body, _ = call(pages, "POST", "/v1/approvals/apr_1/decision", {"decision": "decline", "reason": "busy"},
                        query={"t": tok("report", INV)})
    assert got == 200 and body == {"status": "declined"}
    row = _row(seeded, "apr_1")
    assert (row["status"], row["decided_by"], row["channel"]) == ("declined", f"per_{FARM}", "web")
    assert [(e[0], e[1]["approval_id"], e[1]["decision"], e[1]["channel"]) for e in seeded.events
            if e[0] == "ApprovalRecorded"] == [("ApprovalRecorded", "apr_1", "declined", "web")]


def test_decision_approve(seeded):
    got, body, _ = call(pages, "POST", "/v1/approvals/apr_1/decision", {"decision": "approve"},
                        query={"t": tok("report", INV)})
    assert got == 200 and body == {"status": "approved"} and _row(seeded, "apr_1")["status"] == "approved"


def test_decision_already_decided(seeded, monkeypatch):
    put_approval(seeded, "apr_2", investigation_id=INV, sent_to_person_id="per_x", status="approved",
                 decided_by="per_x", decided_at="2026-07-18T18:00:00Z", channel="sms")
    got, body, _ = call(pages, "POST", "/v1/approvals/apr_2/decision", {"decision": "approve"},
                        query={"t": tok("report", INV)})
    assert got == 409 and body["error"]["code"] == "ALREADY_DECIDED" and "per_x" in body["error"]["message"]
    # Race: the page's read said pending, the conditional write inside record_decision finds it decided.
    put_approval(seeded, "apr_race", investigation_id=INV, sent_to_person_id="per_x", status="approved",
                 decided_by="per_brother", decided_at="2026-07-18T18:00:00Z", channel="sms")
    monkeypatch.setattr(pages, "_approval_status", lambda a: "pending")
    got, body, _ = call(pages, "POST", "/v1/approvals/apr_race/decision", {"decision": "approve"},
                        query={"t": tok("report", INV)})
    assert got == 409 and body["error"]["code"] == "ALREADY_DECIDED"
    assert "per_brother" in body["error"]["message"] and "2026-07-18T18:00:00Z" in body["error"]["message"]


def test_decision_errors(seeded):
    q = {"t": tok("report", "inv_other")}
    assert call(pages, "POST", "/v1/approvals/apr_1/decision", {"decision": "approve"}, query=q)[0] == 403
    q = {"t": tok("report", INV)}
    assert call(pages, "POST", "/v1/approvals/apr_1/decision", {"decision": "maybe"}, query=q)[1]["error"]["code"] == "INVALID_INPUT"
    assert call(pages, "POST", "/v1/approvals/apr_9/decision", {"decision": "approve"}, query=q)[0] == 404
    assert _row(seeded, "apr_1")["status"] == "pending"


def test_decision_expired_is_conflict(seeded):
    put_approval(seeded, "apr_old", investigation_id=INV, expires_at="2020-01-01T00:00:00Z")
    got, body, _ = call(pages, "POST", "/v1/approvals/apr_old/decision", {"decision": "approve"},
                        query={"t": tok("report", INV)})
    assert got == 409 and body["error"]["code"] == "ALREADY_DECIDED" and "expired" in body["error"]["message"].lower()


@pytest.mark.parametrize("args,status,code", [
    (("apr_1", "approved", "per_other", "web"), 403, "POLICY_DENIED"),
    (("apr_9", "approved", f"per_{FARM}", "web"), 404, "NOT_FOUND"),
    (("apr_1", "maybe", f"per_{FARM}", "web"), 400, "INVALID_INPUT"),
    (("apr_1", "approved", f"per_{FARM}", "carrier-pigeon"), 400, "INVALID_INPUT"),
    (("apr_old", "approved", f"per_{FARM}", "web"), 409, "ALREADY_DECIDED"),
])
def test_adapter_maps_error_codes(seeded, args, status, code):
    put_approval(seeded, "apr_old", investigation_id=INV, expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(ApiError) as exc:
        _approvals.record_decision(*args)
    assert (exc.value.status, exc.value.code) == (status, code)


def test_adapter_sms_needs_the_sending_number(seeded):
    with pytest.raises(ApiError) as exc:
        _approvals.record_decision("apr_1", "approved", f"per_{FARM}", "sms")
    assert exc.value.code == "INVALID_INPUT"
    assert _approvals.record_decision("apr_1", "approved", f"per_{FARM}", "sms", from_phone="+14025550100").status == "approved"


# ---------- uploads ----------


def test_upload_presigned_put_and_status(seeded):
    q = {"t": tok("upload", FIELD)}
    got, body, _ = call(pages, "POST", "/v1/uploads", {"kind": "photo", "content_type": "image/jpeg", "field_id": FIELD,
                                                      "location": {"lon": -96.54, "lat": 42.15}}, query=q)
    assert got == 201, body
    assert body["upload_id"].startswith("upl_") and "uploads/" in body["put_url"] and body["expires_at"].endswith("Z")
    item = seeded.table("uploads").get_item(Key={"upload_id": body["upload_id"]})["Item"]
    assert item["status"] == "pending" and item["s3_key"].endswith(".jpg") and item["farm_id"] == FARM
    got, st, _ = call(pages, "GET", f"/v1/uploads/{body['upload_id']}", query=q)
    assert got == 200 and st == {"status": "pending"}


def test_upload_errors(seeded):
    q = {"t": tok("upload", FIELD)}
    assert call(pages, "POST", "/v1/uploads", {"kind": "photo", "content_type": "text/html", "field_id": FIELD},
                query=q)[0] == 400
    assert call(pages, "POST", "/v1/uploads", {"kind": "photo", "content_type": "image/png", "field_id": "fld_other"},
                query=q)[0] == 403
    assert call(pages, "POST", "/v1/uploads", {"kind": "photo", "content_type": "image/png", "field_id": FIELD},
                query={"t": tok("upload", FIELD, farm="other")})[0] == 403


# ---------- work orders ----------


def test_work_order_view(seeded):
    got, body, _ = call(pages, "GET", "/v1/work-orders/act_1", query={"t": tok("work_order", "act_1")})
    assert got == 200, body
    view = C.WorkOrderView.model_validate(body)
    assert view.field_name == "North Pivot" and view.status == "sent" and view.pin == (-96.54, 42.1535)


def test_work_order_done_emits_once(seeded):
    q = {"t": tok("work_order", "act_1")}
    got, body, _ = call(pages, "POST", "/v1/work-orders/act_1/done", {"photo_upload_id": "upl_1"}, query=q)
    assert got == 200 and body["status"] == "done"
    done = [e[1] for e in seeded.events if e[0] == "ActionCompleted"]
    assert len(done) == 1 and done[0]["action_id"] == "act_1" and done[0]["completed_by"] == "per_brother"
    C.ActionCompleted.model_validate(done[0])
    assert call(pages, "POST", "/v1/work-orders/act_1/done", None, query=q)[0] == 200
    assert len([e for e in seeded.events if e[0] == "ActionCompleted"]) == 1
    item = seeded.table("actions").get_item(Key={"action_id": "act_1"})["Item"]
    assert item["status"] == "done" and item["photo_upload_id"] == "upl_1"


def test_work_order_wrong_token(seeded):
    assert call(pages, "GET", "/v1/work-orders/act_1", query={"t": tok("work_order", "act_2")})[0] == 403
    assert call(pages, "POST", "/v1/work-orders/act_1/done", None, query={"t": tok("report", "act_1")})[0] == 403


def test_add_person(seeded):
    got, body, _ = call(pages, "POST", f"/v1/farms/{FARM}/people",
                        {"name": "Brother", "phone_e164": "+14025550111", "role": "repairs", "channel_pref": "sms"},
                        query={"t": tok("onboard", FARM)})
    assert got == 201 and body["opt_in"] == "pending"
    item = seeded.table("farms").get_item(Key={"farm_id": FARM, "item": f"PERSON#{body['person_id']}"})["Item"]
    assert item["role"] == "repairs" and item["phone_e164"] == "+14025550111"


# ---------- dashboard ----------


def _login(aws_ns):
    aws_ns.ssm.put_parameter(Name="/fieldwatch/dashboard-passcode", Value="corn-2026", Type="SecureString")
    got, body, _ = call(dashboard, "POST", "/v1/auth/dashboard", {"passcode": "corn-2026"})
    assert got == 200, body
    return {"Authorization": f"Bearer {body['token']}"}


def test_dashboard_auth(seeded):
    assert call(dashboard, "POST", "/v1/auth/dashboard", {"passcode": "x"})[0] == 501  # no param yet
    seeded.ssm.put_parameter(Name="/fieldwatch/dashboard-passcode", Value="corn-2026", Type="SecureString")
    from cloud.api import _aws as a
    a._ssm_cache.clear()
    assert call(dashboard, "POST", "/v1/auth/dashboard", {"passcode": "wrong"})[0] == 401
    got, body, _ = call(dashboard, "POST", "/v1/auth/dashboard", {"passcode": "corn-2026"})
    assert got == 200 and links.verify(body["token"], scope="dashboard").farm_id == "*"
    assert call(dashboard, "GET", f"/v1/farms/{FARM}/overview")[0] == 401
    assert call(dashboard, "GET", f"/v1/farms/{FARM}/overview",
                headers={"Authorization": f"Bearer {tok('report', INV)}"})[0] == 403


def test_overview_and_investigation(seeded):
    h = _login(seeded)
    seeded.table("investigations").put_item(Item={"investigation_id": "inv_open", "field_id": FIELD, "status": "open",
                                                  "created_at": "2026-07-19T00:00:00Z"})
    got, body, _ = call(dashboard, "GET", f"/v1/farms/{FARM}/overview", headers=h)
    assert got == 200
    f = body["fields"][0]
    assert f["field_id"] == FIELD and f["latest_score"] == 3.1 and f["geometry"]["type"] == "Polygon"
    assert [i["investigation_id"] for i in body["open_investigations"]] == ["inv_open"]
    got, inv, _ = call(dashboard, "GET", f"/v1/investigations/{INV}", headers=h)
    assert got == 200 and inv["hypotheses"][0]["probability"] == 0.86
    assert call(dashboard, "GET", "/v1/investigations/nope", headers=h)[0] == 404


def test_replay_and_reset(seeded, monkeypatch):
    h = _login(seeded)
    assert call(dashboard, "POST", "/v1/replay", {"scene_id": "S2B_14TPM_20260718_0_L2A"}, headers=h)[0] == 501
    assert call(dashboard, "POST", "/v1/replay/reset", {"farm_id": FARM}, headers=h)[0] == 501
    monkeypatch.setenv("FIELDWATCH_REPLAY_FN", "fw-scene-replay")
    monkeypatch.setenv("FIELDWATCH_RESET_FN", "fw-scene-reset")
    seeded.lambda_responses["fw-scene-reset"] = {"farm_id": FARM, "deleted": {"fw-scene-analyses": 1}}
    assert call(dashboard, "POST", "/v1/replay", {"field_ids": [FIELD]}, headers=h)[0] == 400  # as_of missing
    got, body, _ = call(dashboard, "POST", "/v1/replay", {"scene_id": "S2B_14TPM_20260718_0_L2A", "field_ids": [FIELD]},
                        headers=h)
    assert got == 202 and body["replay_id"].startswith("rpl_")
    body_id = body["replay_id"]
    got, body, _ = call(dashboard, "POST", "/v1/replay", {"scene_id": "S2B_14TPM_20260718_0_L2A"}, headers=h)
    assert got == 429 and body["error"]["retryable"] is True
    got, out, _ = call(dashboard, "POST", "/v1/replay/reset", {"farm_id": FARM}, headers=h)
    assert got == 202 and out["result"]["deleted"] == {"fw-scene-analyses": 1}
    (rfn, rkind, rp), (sfn, skind, sp) = seeded.lambda_calls
    replay = C.ReplayRequest.model_validate(rp)
    assert (rfn, rkind) == ("fw-scene-replay", "Event") and replay.replay_id == body_id
    assert replay.scene_id == "S2B_14TPM_20260718_0_L2A" and replay.field_ids == [FIELD]
    assert (sfn, skind) == ("fw-scene-reset", "RequestResponse") and C.ResetRequest.model_validate(sp).farm_id == FARM
    assert seeded.queue_messages() == []  # replay envelopes are the scenes lane's to build


def test_mirror_reply(seeded):
    h = _login(seeded)
    got, body, _ = call(dashboard, "POST", "/v1/mirror/reply", {"person_id": f"per_{FARM}", "text": "MORE"}, headers=h)
    assert got == 202 and body["result"]["intent"] == "more"
    assert [e[0] for e in seeded.events] == ["MessageReceived", "ReplyReceived"]
    assert call(dashboard, "POST", "/v1/mirror/reply", {"person_id": "per_x", "text": "MORE"}, headers=h)[0] == 404
