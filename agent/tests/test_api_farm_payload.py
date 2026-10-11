"""GET /api/farms/{id}: `diagnoses`, `pending` and the signed report / work-order links (cloud farm payload).

The local harness (web/server.py) returns `diagnoses` (oldest first, Diagnosis.model_dump() each) and `pending`; the
redesigned front end reads them. The cloud builds them from fw-investigations, fw-approvals and fw-actions.
"""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from cloud.api import _aws, _diagnoses, farms, pages
from fieldwatch import links
from fieldwatch.schemas import Diagnosis
from test_api_helpers import NORTH_PIVOT_LATLON, aws, call, put_analysis, put_approval, signup  # noqa: F401

FARM = "4025550100"
FIELD = f"fld_{FARM}_north-pivot"
PERSON = f"per_{FARM}"
HYPS = [{"condition": "pivot_failure", "probability": 0.82, "status": "likely"},
        {"condition": "drought", "probability": 0.05, "status": "ruled_out"}]
BASE_KEYS = {"farm_id", "name", "phone", "farmer", "fields", "thread", "muted", "last_checked", "pending", "backend"}
DIAGNOSIS_KEYS = set(Diagnosis.model_fields)
EXTRA_KEYS = {"investigation_id", "field_id", "decision", "approval_id", "action_type", "report_url", "work_order_url",
              "work_order_status", "completed_at", "created_at", "updated_at"}


def diagnosis(**over) -> dict:
    return {**Diagnosis(field_name="North Pivot", what="The outer span is not watering", where="south-east corner",
                        why="Dry strip lines up with the pivot; rain was normal", ruled_out=["drought"],
                        dollars_low=9000, dollars_high=21000, how_sure="pretty sure",
                        recommended_action="repair_work_order", action_detail="Check the end tower nozzles",
                        condition="pivot_failure", affected_acres=38.5, hypotheses=HYPS).model_dump(), **over}


def put_inv(aws, inv_id, created, status="concluded", diag=None, **extra):
    item = {"investigation_id": inv_id, "farm_id": FARM, "field_id": FIELD, "status": status, "created_at": created,
            "updated_at": created, **extra}
    if diag is not None:
        item["diagnosis_json"] = json.dumps(diag)
    aws.table("investigations").put_item(Item=_aws.to_ddb(item))


def put_work_order(aws, approval_id, inv_id, **over):
    action_id = "act-" + approval_id.removeprefix("apr-")
    item = {"action_id": action_id, "approval_id": approval_id, "investigation_id": inv_id, "type": "work_order",
            "assignee_person_id": "per_brother", "pin": [-96.54, 42.1535], "status": "sent",
            "instructions": "Check the end tower nozzles", "farm_id": FARM, "field_id": FIELD, **over}
    aws.table("actions").put_item(Item=_aws.to_ddb(item))
    return action_id


def get_farm():
    code, farm, _ = call(farms, "GET", f"/api/farms/{FARM}", route_key="GET /api/farms/{farm_id}")
    assert code == 200, farm
    return farm


def token_of(url):
    return parse_qs(urlparse(url).query)["t"][0]


@pytest.fixture
def seeded(aws):
    """Five investigations on one field, oldest to newest:
    open work order, finished work order, declined, a question run, and one waiting for the farmer's YES."""
    signup(farms)
    put_analysis(aws, FIELD)
    put_inv(aws, "inv-1", "2026-07-10T10:00:00Z", diag=diagnosis())
    put_approval(aws, "apr-aaa", investigation_id="inv-1", sent_to_person_id=PERSON, status="approved",
                 created_at="2026-07-10T10:01:00Z")
    put_work_order(aws, "apr-aaa", "inv-1")

    put_inv(aws, "inv-2", "2026-07-12T10:00:00Z", diag=diagnosis(dollars_low=1000, dollars_high=2000))
    put_approval(aws, "apr-bbb", investigation_id="inv-2", sent_to_person_id=PERSON, status="approved",
                 created_at="2026-07-12T10:01:00Z")
    put_work_order(aws, "apr-bbb", "inv-2", status="done", done_at="2026-07-13T15:00:00Z", completed_by="per_brother")

    put_inv(aws, "inv-3", "2026-07-14T10:00:00Z", diag=diagnosis(recommended_action="scouting_pin"))
    put_approval(aws, "apr-ccc", investigation_id="inv-3", sent_to_person_id=PERSON, status="declined",
                 action_type="scout_pin", created_at="2026-07-14T10:01:00Z")

    # a farmer's free-text question: never a recommendation, even with a stored answer
    put_inv(aws, "inv-q-1", "2026-07-16T10:00:00Z", kind="question", diag=diagnosis(what="A question"))

    put_inv(aws, "inv-4", "2026-07-18T10:00:00Z", status="awaiting_reply", pending_interrupt_id="int-9",
            last_text="North Pivot: send a repair ticket? YES / NO", condition="pivot_failure")
    put_approval(aws, "apr-ddd", investigation_id="inv-4", sent_to_person_id=PERSON, summary="Send a repair ticket",
                 created_at="2026-07-18T10:01:00Z")
    return aws


# ---------- shape ----------


def test_diagnoses_oldest_first_with_the_harness_keys(seeded):
    farm = get_farm()
    ds = farm["diagnoses"]
    assert [d["investigation_id"] for d in ds] == ["inv-1", "inv-2", "inv-3", "inv-4"]  # the last one is the newest
    for d in ds:
        assert set(d) == DIAGNOSIS_KEYS | EXTRA_KEYS
    d = ds[0]
    assert d["field_name"] == "North Pivot" and d["what"] == "The outer span is not watering"
    assert (d["dollars_low"], d["dollars_high"], d["how_sure"]) == (9000, 21000, "pretty sure")
    assert d["recommended_action"] == "repair_work_order" and d["action_detail"] == "Check the end tower nozzles"
    assert d["condition"] == "pivot_failure" and d["affected_acres"] == 38.5 and d["ruled_out"] == ["drought"]
    assert d["hypotheses"] == HYPS
    assert d["field_id"] == FIELD and d["created_at"] == "2026-07-10T10:00:00Z"
    # exactly what Diagnosis.model_dump() gives, plus the extras
    assert {k: d[k] for k in DIAGNOSIS_KEYS} == Diagnosis.model_validate(diagnosis()).model_dump(mode="json")


def test_question_runs_are_not_listed(seeded):
    farm = get_farm()
    assert "inv-q-1" not in [d["investigation_id"] for d in farm["diagnoses"]]
    assert all(d["what"] != "A question" for d in farm["diagnoses"])


def test_existing_payload_keys_are_unchanged(seeded):
    farm = get_farm()
    assert BASE_KEYS <= set(farm) and set(farm) == BASE_KEYS | {"diagnoses"}
    assert farm["farm_id"] == FARM and farm["farmer"] == {"name": "Dad", "channel": "text"}
    assert farm["backend"] == "cloud" and farm["muted"] is False
    f = farm["fields"][0]
    assert f["id"] == FIELD and f["name"] == "North Pivot" and f["crop"] == "corn" and "stats" in f
    assert farm["thread"][0]["kind"] == "welcome"


def test_a_farm_without_investigations(aws):
    signup(farms)
    farm = get_farm()
    assert farm["diagnoses"] == [] and farm["pending"] is None
    assert BASE_KEYS <= set(farm)


def test_bounded_to_the_newest_five(aws):
    signup(farms)
    for n in range(1, 9):
        put_inv(aws, f"inv-{n}", f"2026-07-{10 + n:02d}T10:00:00Z", diag=diagnosis(what=f"Run {n}"))
    ds = get_farm()["diagnoses"]
    assert [d["what"] for d in ds] == [f"Run {n}" for n in range(4, 9)]


def test_other_statuses_and_other_farms_are_skipped(seeded):
    put_inv(seeded, "inv-run", "2026-07-19T10:00:00Z", status="running")
    put_inv(seeded, "inv-fail", "2026-07-19T11:00:00Z", status="failed")
    put_inv(seeded, "inv-nodiag", "2026-07-19T12:00:00Z", status="concluded")
    seeded.table("investigations").put_item(Item={**_aws.to_ddb({
        "investigation_id": "inv-other", "farm_id": "farm_other", "field_id": FIELD, "status": "concluded",
        "created_at": "2026-07-19T13:00:00Z"}), "diagnosis_json": json.dumps(diagnosis())})
    assert [d["investigation_id"] for d in get_farm()["diagnoses"]] == ["inv-1", "inv-2", "inv-3", "inv-4"]


# ---------- states ----------


def test_states(seeded):
    by = {d["investigation_id"]: d for d in get_farm()["diagnoses"]}
    assert by["inv-1"]["decision"] == "approved" and by["inv-1"]["work_order_status"] == "open"
    assert by["inv-1"]["completed_at"] is None and by["inv-1"]["action_type"] == "work_order"
    assert by["inv-2"]["decision"] == "approved" and by["inv-2"]["work_order_status"] == "done"
    assert by["inv-2"]["completed_at"] == "2026-07-13T15:00:00Z"
    assert by["inv-3"]["decision"] == "declined" and by["inv-3"]["approval_id"] == "apr-ccc"
    assert by["inv-3"]["work_order_url"] is None and by["inv-3"]["work_order_status"] is None
    assert by["inv-4"]["decision"] == "pending"


def test_pending_run_is_a_stub_and_sets_the_farm_pending(seeded):
    farm = get_farm()
    assert farm["pending"] == {"investigation_id": "inv-4", "interrupt_id": "int-9"}  # the harness shape
    stub = farm["diagnoses"][-1]
    assert set(stub) == DIAGNOSIS_KEYS | EXTRA_KEYS
    assert stub["what"] == "North Pivot: send a repair ticket? YES / NO" and stub["field_name"] == "North Pivot"
    assert stub["dollars_high"] == 0 and stub["hypotheses"] == [] and stub["condition"] == "pivot_failure"
    assert stub["recommended_action"] == "repair_work_order" and stub["action_detail"] == "Send a repair ticket"
    assert stub["report_url"] and stub["work_order_url"] is None


def test_the_answer_moves_a_pending_run_on(seeded):
    seeded.table("approvals").update_item(Key={"approval_id": "apr-ddd"}, UpdateExpression="SET #s = :s",
                                          ExpressionAttributeNames={"#s": "status"},
                                          ExpressionAttributeValues={":s": "declined"})
    seeded.table("investigations").update_item(
        Key={"investigation_id": "inv-4"}, UpdateExpression="SET #s = :c, diagnosis_json = :d REMOVE pending_interrupt_id",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": "concluded", ":d": json.dumps(diagnosis(what="Declined by the farmer"))})
    farm = get_farm()
    assert farm["pending"] is None
    assert (farm["diagnoses"][-1]["what"], farm["diagnoses"][-1]["decision"]) == ("Declined by the farmer", "declined")


def test_an_expired_ask_is_not_pending(seeded):
    seeded.table("approvals").update_item(Key={"approval_id": "apr-ddd"}, UpdateExpression="SET expires_at = :e",
                                          ExpressionAttributeValues={":e": "2020-01-01T00:00:00Z"})
    farm = get_farm()
    assert farm["pending"] is None and farm["diagnoses"][-1]["decision"] == "expired"


def test_no_action_needed(aws):
    signup(farms)
    put_inv(aws, "inv-1", "2026-07-10T10:00:00Z", diag=diagnosis(recommended_action="none"))
    d, = get_farm()["diagnoses"]
    assert d["decision"] == "none" and d["work_order_url"] is None and d["report_url"]


def test_concluded_without_an_approval_record(aws):
    signup(farms)
    put_inv(aws, "inv-1", "2026-07-10T10:00:00Z", diag=diagnosis())
    d, = get_farm()["diagnoses"]
    assert d["decision"] is None and d["approval_id"] is None


def test_an_investigation_that_names_its_approval(aws):
    signup(farms)
    put_inv(aws, "inv-1", "2026-07-10T10:00:00Z", diag=diagnosis(), approval_id="apr-zzz")
    put_approval(aws, "apr-zzz", investigation_id="inv-1", sent_to_person_id="per_somebody_else", status="declined")
    assert get_farm()["diagnoses"][0]["decision"] == "declined"


# ---------- links ----------


def test_links_verify_with_the_pages_own_checks(seeded):
    by = {d["investigation_id"]: d for d in get_farm()["diagnoses"]}
    r = urlparse(by["inv-1"]["report_url"])
    assert r.path == "/report.html" and parse_qs(r.query)["inv"] == ["inv-1"]  # web/report.js reads ?inv=
    claims = links.verify(token_of(by["inv-1"]["report_url"]), scope="report", subject_id="inv-1")
    assert claims.farm_id == FARM
    w = urlparse(by["inv-1"]["work_order_url"])
    assert w.path == "/workorder.html" and parse_qs(w.query)["id"] == ["act-aaa"]  # web/workorder.js reads ?id=
    claims = links.verify(token_of(by["inv-1"]["work_order_url"]), scope="work_order", subject_id="act-aaa")
    assert claims.farm_id == FARM
    assert by["inv-2"]["work_order_url"] and "act-bbb" in by["inv-2"]["work_order_url"]


def test_links_are_single_purpose(seeded):
    d = {d["investigation_id"]: d for d in get_farm()["diagnoses"]}["inv-1"]
    rep, wo = token_of(d["report_url"]), token_of(d["work_order_url"])
    for token, scope, subject in ((rep, "work_order", "inv-1"), (rep, "upload", "inv-1"), (rep, "dashboard", None),
                                  (rep, "report", "inv-2"), (wo, "report", "act-aaa"), (wo, "work_order", "act-bbb")):
        with pytest.raises(links.LinkError) as e:
            links.verify(token, scope=scope, subject_id=subject)
        assert e.value.code == "TOKEN_SCOPE"
    # the pages refuse them too
    code, body, _ = call(pages, "GET", "/v1/work-orders/act-aaa", query={"t": rep})
    assert code == 403 and body["error"]["code"] == "TOKEN_SCOPE"
    code, body, _ = call(pages, "GET", "/v1/reports/inv-2", query={"t": rep})
    assert code == 403 and body["error"]["code"] == "TOKEN_SCOPE"


def test_links_expire_within_a_day_and_are_stable_between_polls(seeded):
    import time
    first = {d["investigation_id"]: d for d in get_farm()["diagnoses"]}["inv-1"]
    again = {d["investigation_id"]: d for d in get_farm()["diagnoses"]}["inv-1"]
    assert first["report_url"] == again["report_url"] and first["work_order_url"] == again["work_order_url"]
    exp = links.verify(token_of(first["report_url"]), scope="report").exp
    assert 22 * 3600 < exp - time.time() <= 24 * 3600
    with pytest.raises(links.LinkError) as e:
        links.verify(token_of(first["report_url"]), scope="report", now=time.time() + 25 * 3600)
    assert e.value.code == "TOKEN_EXPIRED"


def test_the_pages_accept_the_links(seeded):
    by = {d["investigation_id"]: d for d in get_farm()["diagnoses"]}
    code, body, _ = call(pages, "GET", "/v1/reports/inv-1", query={"t": token_of(by["inv-1"]["report_url"])})
    assert code == 200 and body["field"]["name"] == "North Pivot", body
    code, body, _ = call(pages, "GET", "/v1/work-orders/act-aaa", query={"t": token_of(by["inv-1"]["work_order_url"])})
    assert code == 200 and body["action_id"] == "act-aaa" and body["status"] == "sent", body


def test_marking_the_work_order_done_shows_in_the_payload(seeded):
    d = {d["investigation_id"]: d for d in get_farm()["diagnoses"]}["inv-1"]
    assert d["work_order_status"] == "open"
    code, body, _ = call(pages, "POST", "/v1/work-orders/act-aaa/done", {}, query={"t": token_of(d["work_order_url"])})
    assert code == 200 and body["status"] == "done", body
    d = {d["investigation_id"]: d for d in get_farm()["diagnoses"]}["inv-1"]
    assert d["work_order_status"] == "done" and d["completed_at"] == body["done_at"] and d["completed_at"]
    assert d["work_order_url"]  # the page still opens, now showing "Marked done"


def test_links_use_the_public_url_when_set(seeded, monkeypatch):
    monkeypatch.setenv("FIELDWATCH_PUBLIC_URL", "https://d111.cloudfront.net/")
    d = get_farm()["diagnoses"][0]
    assert d["report_url"].startswith("https://d111.cloudfront.net/report.html?inv=inv-1&t=")
    assert d["work_order_url"].startswith("https://d111.cloudfront.net/workorder.html?id=act-aaa&t=")


def test_without_a_signing_key_the_farm_still_loads(seeded, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no key")

    monkeypatch.setattr(links, "sign", boom)
    ds = get_farm()["diagnoses"]
    assert len(ds) == 4 and all(d["report_url"] is None and d["work_order_url"] is None for d in ds)
    assert ds[0]["work_order_status"] == "open"


def test_a_failure_building_diagnoses_does_not_break_the_farm(seeded, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("table gone")

    monkeypatch.setattr(_diagnoses, "recent_investigations", boom)
    farm = get_farm()
    assert farm["diagnoses"] == [] and farm["pending"] is None and farm["fields"]


def test_one_query_per_field_and_no_scans(seeded, monkeypatch):
    signup(farms, fields=[{"name": "North Pivot", "polygon": NORTH_PIVOT_LATLON},
                          {"name": "South", "polygon": [[p[0] - 1, p[1]] for p in NORTH_PIVOT_LATLON]}])
    calls = []
    real = _diagnoses.recent_investigations
    monkeypatch.setattr(_diagnoses, "recent_investigations", lambda fid, limit=10: calls.append(fid) or real(fid, limit))
    monkeypatch.setattr(_aws, "scan_all", lambda *a, **k: pytest.fail("the farm payload must not scan"))
    get_farm()
    assert len(calls) == 2 and len(set(calls)) == 2
