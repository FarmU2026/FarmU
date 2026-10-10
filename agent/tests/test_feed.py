"""WS13 live feed: event -> FeedMessage mapping, thread history (moto), SigV4 publishing (fake transport)."""
import json
from pathlib import Path

import boto3
import pytest
from botocore.credentials import Credentials
from moto import mock_aws

from cloud.feed import handler as H
from cloud.feed import mapping as M
from cloud.feed import publisher as P
from cloud.feed import send_sample
from fieldwatch import contracts as C

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contracts"
FARM = "wohleb-farm"
INV = "inv-north-pivot-20260718"
NOW = "2026-07-18T17:21:05Z"


def fx(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def lookup(inv_id: str) -> str:
    assert inv_id == INV
    return FARM


def one(detail_type: str, detail: dict, **kw) -> M.Projection:
    return M.project(detail_type, detail, "evt-1", farm_lookup=lookup, **kw)


def types(p: M.Projection) -> list[str]:
    return [m.type for m in p.messages]


@pytest.fixture(autouse=True)
def fake_aws(monkeypatch):
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(Path("nonexistent-credentials")))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(Path("nonexistent-config")))
    H._farm_cache.clear()


# ---------- mapping ----------


def test_every_message_is_a_valid_feed_message_for_the_cloud_backend():
    for detail_type, detail in send_sample.build(FARM):
        for m in one(detail_type, detail).messages:
            C.FeedMessage.model_validate(m.model_dump())
            assert m.backend == "cloud" and m.farm_id == FARM
            assert m.payload["event_id"]


def test_the_ten_feed_events_are_all_mapped():
    assert set(M.DETAIL_TYPES) == {
        "SceneAnalyzed", "AnomalyDetected", "WatchOpened", "InvestigationProgress", "InvestigationConcluded",
        "ReplyReceived", "ApprovalRecorded", "ActionCompleted", "MessageSent", "MessageReceived"}


def test_scene_analyzed():
    d = {"schema_version": "1", "occurred_at": NOW, "field": {"farm_id": FARM, "field_id": "north-pivot"},
         "scene_id": "S2B_x", "date": "2026-07-18", "status": "ok", "max_score": 3.1, "region_count": 1,
         "analysis_key": "analysis/north-pivot/2026-07-18/s2/result.json"}
    p = one("SceneAnalyzed", d)
    assert types(p) == ["scene_analyzed", "field_status"]
    assert p.messages[0].payload["max_score"] == 3.1 and p.messages[1].payload["status"] == "ok"
    assert not p.thread
    assert list(p.publishes()) == [f"/fieldwatch/farms/{FARM}/feed"]


def test_anomaly_detected_becomes_field_status_with_regions_and_overlays():
    p = one("AnomalyDetected", fx("anomaly_detected"))
    (m,) = p.messages
    assert m.type == "field_status" and m.payload["status"] == "anomaly" and m.payload["replay"] is True
    assert m.payload["regions"][0]["region_id"] == "r1" and "geometry" in m.payload["regions"][0]
    assert m.payload["overlays"]["true_color"]["key"].endswith("true_color.png")
    assert m.payload["event_id"] == "anm-north-pivot-20260718"


def test_watch_opened_one_message_per_field_on_its_own_farm():
    d = {"schema_version": "1", "occurred_at": NOW, "watch": {
        "watch_id": "w1", "type": "hail", "opened_at": NOW, "expires_at": "2026-07-25T00:00:00Z",
        "detail": {"max_hail_mm": 38},
        "fields": [{"farm_id": "a", "field_id": "f1"}, {"farm_id": "a", "field_id": "f2"},
                   {"farm_id": "b", "field_id": "f3"}]}}
    p = one("WatchOpened", d)
    assert [(m.farm_id, m.payload["field_id"]) for m in p.messages] == [("a", "f1"), ("a", "f2"), ("b", "f3")]
    assert p.messages[0].payload["watch_type"] == "hail" and p.messages[0].payload["status"] == "watching"
    assert set(p.publishes()) == {"/fieldwatch/farms/a/feed", "/fieldwatch/farms/b/feed"}


def test_investigation_progress_with_evidence_fans_out_and_writes_a_step_thread_item():
    p = one("InvestigationProgress", fx("investigation_progress"))
    assert types(p) == ["investigation_step", "evidence_added"]
    step, ev = p.messages
    assert step.agent == "environment" and step.investigation_id == INV and step.payload["seq"] == 3
    assert ev.payload["evidence"]["evidence_id"] == "ev2"
    (t,) = p.thread
    assert (t.who, t.kind, t.text, t.id) == ("agent", "step", "Checked rainfall since June 18: normal.", f"{INV}#3")


def test_investigation_progress_with_hypotheses():
    d = fx("investigation_progress")
    d["evidence"] = None
    d["hypotheses"] = [{"condition": "drought", "probability": 0.1, "status": "open"}]
    p = one("InvestigationProgress", d)
    assert types(p) == ["investigation_step", "hypotheses_updated"]
    assert p.messages[1].payload["hypotheses"][0]["condition"] == "drought"


def test_concluded():
    p = one("InvestigationConcluded", fx("investigation_concluded"))
    (m,) = p.messages
    assert m.type == "concluded" and m.payload["condition"] == "pivot_failure"
    assert m.payload["impact"]["high_usd"] == 14000.0 and m.payload["proposed_action"]["action_type"] == "work_order"
    (t,) = p.thread
    assert (t.who, t.kind, t.field_id, t.investigation_id) == ("fieldwatch", "concluded", "north-pivot", INV)


def test_reply_received_is_dashboard_only_with_no_thread_item():
    p = one("ReplyReceived", fx("reply_received"))
    (m,) = p.messages
    assert m.type == "investigation_step" and m.payload["step"] == "reply" and m.payload["intent"] == "more"
    assert not p.thread
    assert list(p.publishes()) == [f"/fieldwatch/farms/{FARM}/feed"]  # the text itself arrives as MessageReceived


def test_approval_recorded_and_action_completed_are_action_created():
    p = one("ApprovalRecorded", fx("approval_recorded"))
    (m,) = p.messages
    assert m.type == "action_created" and m.payload["status"] == "approved" and m.farm_id == FARM
    assert p.thread[0].text == "dad approved the plan."
    p = one("ActionCompleted", {"schema_version": "1", "occurred_at": NOW, "action_id": "act-1",
                                "investigation_id": INV, "completed_by": "brother"})
    assert types(p) == ["action_created"] and p.messages[0].payload["status"] == "done"
    assert p.thread[0].id == "act-1#done" and p.thread[0].kind == "action"


def test_message_sent_and_received_go_to_feed_and_phone():
    sent = {"schema_version": "1", "occurred_at": NOW, "message_id": "msg-1", "farm_id": FARM, "person_id": "dad",
            "body": "Part of the north pivot looks dry.", "investigation_id": INV, "status": "sent", "provider": "twilio"}
    recv = {"schema_version": "1", "occurred_at": NOW, "message_id": "msg-2", "farm_id": FARM, "person_id": "dad",
            "body": "YES"}
    p = one("MessageSent", sent)
    assert types(p) == ["message_out"] and p.messages[0].payload["text"] == sent["body"]
    assert set(p.publishes()) == {f"/fieldwatch/farms/{FARM}/feed", f"/fieldwatch/farms/{FARM}/phone"}
    assert (p.thread[0].who, p.thread[0].kind) == ("fieldwatch", "message")
    p = one("MessageReceived", recv)
    assert types(p) == ["message_in"] and (p.thread[0].who, p.thread[0].kind) == ("farmer", "message")
    assert p.messages[0].investigation_id is None


def test_phone_channel_gets_exactly_what_the_thread_stores():
    for detail_type, detail in send_sample.build(FARM):
        p = one(detail_type, detail)
        phone = p.publishes().get(f"/fieldwatch/farms/{FARM}/phone", [])
        assert len(phone) == len(p.thread), detail_type


def test_unknown_event_and_unknown_investigation():
    with pytest.raises(KeyError):
        M.project("PhotoReceived", {}, "x")
    with pytest.raises(M.UnknownFarm):
        M.project("InvestigationProgress", fx("investigation_progress"), "x")


def test_oversized_payload_drops_geometry():
    d = fx("anomaly_detected")
    ring = [[-96.5 + i * 1e-6, 42.1 + i * 1e-6] for i in range(12000)]
    d["regions"][0]["geometry"]["coordinates"] = [ring + [ring[0]]]
    (m,) = one("AnomalyDetected", d).messages
    assert m.payload["trimmed"] is True and "regions" not in m.payload


def test_thread_item_shape_matches_web_server_post():
    t = one("MessageSent", {"schema_version": "1", "occurred_at": NOW, "message_id": "m1", "farm_id": FARM,
                            "person_id": "dad", "body": "hi", "investigation_id": INV, "status": "sent",
                            "provider": "x"}).thread[0]
    assert t.to_item() == {"farm_id": FARM, "item": f"THREAD#{NOW}#m1", "who": "fieldwatch", "text": "hi",
                           "kind": "message", "at": NOW, "investigation_id": INV}


# ---------- SigV4 publishing with a fake transport ----------


class FakeTransport:
    def __init__(self, response=(200, '{"successful":[{"identifier":"x","index":0}],"failed":[]}')):
        self.calls = []
        self.response = response

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        return self.response


CREDS = Credentials("AKIDEXAMPLE", "secret", "session-token")
HOST = "abc123.appsync-api.us-west-2.amazonaws.com"


def feed_msgs(n: int) -> list[C.FeedMessage]:
    return [C.FeedMessage(type="message_out", ts=NOW, farm_id=FARM, backend="cloud", payload={"i": i}) for i in range(n)]


def test_publish_request_is_a_signed_appsync_events_post():
    tr = FakeTransport()
    sent = P.FeedPublisher(HOST, "us-west-2", CREDS, tr).publish(f"/fieldwatch/farms/{FARM}/feed", feed_msgs(2))
    assert sent == 2 and len(tr.calls) == 1
    url, headers, body = tr.calls[0]
    assert url == f"https://{HOST}/event"
    doc = json.loads(body)
    assert doc["channel"] == f"/fieldwatch/farms/{FARM}/feed"
    assert all(isinstance(e, str) for e in doc["events"])  # events are JSON *strings*
    assert json.loads(doc["events"][1])["payload"] == {"i": 1}
    h = {k.lower(): v for k, v in headers.items()}
    assert h["content-type"] == "application/json" and h["x-amz-security-token"] == "session-token"
    auth = h["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/") and "/us-west-2/appsync/aws4_request" in auth
    assert "host" in auth.split("SignedHeaders=")[1].split(",")[0] and "Signature=" in auth
    assert "x-amz-date" in h


def test_publish_batches_in_fives():
    tr = FakeTransport()
    assert P.FeedPublisher(HOST, "us-west-2", CREDS, tr).publish("/fieldwatch/farms/x/feed", feed_msgs(12)) == 12
    assert [len(json.loads(c[2])["events"]) for c in tr.calls] == [5, 5, 2]


@pytest.mark.parametrize("response", [(403, '{"errors":[{"errorType":"UnauthorizedException"}]}'),
                                      (200, '{"successful":[],"failed":[{"identifier":"x","index":0,"code":1}]}')])
def test_publish_failure_raises(response):
    with pytest.raises(P.PublishError):
        P.FeedPublisher(HOST, "us-west-2", CREDS, FakeTransport(response)).publish("/fieldwatch/farms/x/feed", feed_msgs(1))


# ---------- handler end to end with moto + fake transport ----------


@pytest.fixture
def tables():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        farms = ddb.create_table(
            TableName="fw-farms", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "farm_id", "AttributeType": "S"},
                                  {"AttributeName": "item", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "farm_id", "KeyType": "HASH"}, {"AttributeName": "item", "KeyType": "RANGE"}])
        invs = ddb.create_table(
            TableName="fw-investigations", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}])
        invs.put_item(Item={"investigation_id": INV, "field": {"farm_id": FARM, "field_id": "north-pivot"}})
        yield farms, invs


def envelope(detail_type: str, detail: dict, eid: str = "eb-1") -> dict:
    return {"id": eid, "source": "fieldwatch.test", "detail-type": detail_type, "detail": detail}


def run(event, tables, tr):
    farms, invs = tables
    return H.process(event, publisher=P.FeedPublisher(HOST, "us-west-2", CREDS, tr),
                     farms_table=farms, investigations_table=invs)


def thread_items(farms):
    return sorted(farms.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("farm_id").eq(FARM)
        & boto3.dynamodb.conditions.Key("item").begins_with("THREAD#"))["Items"], key=lambda i: i["item"])


def test_handler_writes_thread_idempotently_and_publishes(tables):
    tr = FakeTransport()
    ev = envelope("InvestigationProgress", fx("investigation_progress"))
    out = run(ev, tables, tr)
    assert out == {"published": 3, "thread": 1}  # step -> feed + phone, evidence -> feed
    assert sorted(json.loads(c[2])["channel"] for c in tr.calls) == [
        f"/fieldwatch/farms/{FARM}/feed", f"/fieldwatch/farms/{FARM}/phone"]
    run(ev, tables, tr)  # redelivery
    (item,) = thread_items(tables[0])
    assert item == {"farm_id": FARM, "item": f"THREAD#{NOW}#{INV}#3", "who": "agent",
                    "text": "Checked rainfall since June 18: normal.", "kind": "step", "at": NOW,
                    "investigation_id": INV}


def test_handler_thread_orders_by_time(tables):
    tr = FakeTransport()
    base = {"schema_version": "1", "farm_id": FARM, "person_id": "dad", "investigation_id": INV}
    run(envelope("MessageReceived", {**base, "occurred_at": "2026-07-18T17:22:00Z", "message_id": "m2", "body": "MORE"}), tables, tr)
    run(envelope("MessageSent", {**base, "occurred_at": "2026-07-18T17:21:00Z", "message_id": "m1", "body": "Dry spot.",
                                 "status": "sent", "provider": "twilio"}), tables, tr)
    items = thread_items(tables[0])
    assert [(i["who"], i["text"]) for i in items] == [("fieldwatch", "Dry spot."), ("farmer", "MORE")]
    assert set(items[0]) == {"farm_id", "item", "who", "text", "kind", "at", "investigation_id"}


def test_handler_does_not_touch_other_farm_items(tables):
    farms = tables[0]
    farms.put_item(Item={"farm_id": FARM, "item": "FARM", "name": "Wohleb"})
    run(envelope("InvestigationConcluded", fx("investigation_concluded")), tables, FakeTransport())
    assert farms.get_item(Key={"farm_id": FARM, "item": "FARM"})["Item"]["name"] == "Wohleb"
    assert len(thread_items(farms)) == 1


def test_handler_ignores_other_events_and_retries_unknown_investigation(tables):
    tr = FakeTransport()
    assert run(envelope("MrmsFileAvailable", {}), tables, tr) == {"ignored": "MrmsFileAvailable"}
    assert not tr.calls
    d = fx("investigation_progress")
    d["investigation_id"] = "inv-missing"
    with pytest.raises(M.UnknownFarm):
        run(envelope("InvestigationProgress", d), tables, tr)


def test_handler_publish_failure_raises_after_thread_write(tables):
    with pytest.raises(P.PublishError):
        run(envelope("MessageReceived", {"schema_version": "1", "occurred_at": NOW, "message_id": "m9", "farm_id": FARM,
                                         "person_id": "dad", "body": "hi"}), tables, FakeTransport((500, "boom")))
    assert len(thread_items(tables[0])) == 1  # safe: a retry overwrites the same key


def test_lambda_entrypoint_signs_with_the_session_credentials(tables, monkeypatch):
    monkeypatch.setenv("APPSYNC_HTTP_HOST", HOST)
    calls = []
    monkeypatch.setattr(P, "urllib_transport", lambda u, h, b: calls.append((u, h, b)) or (200, '{"failed":[]}'))
    monkeypatch.setattr(H, "FeedPublisher", lambda host, region, creds: P.FeedPublisher(host, region, creds, P.urllib_transport))
    H.handler(envelope("MessageReceived", {"schema_version": "1", "occurred_at": NOW, "message_id": "m3", "farm_id": FARM,
                                           "person_id": "dad", "body": "hi"}))
    assert len(calls) == 2 and "/us-west-2/appsync/aws4_request" in calls[0][1]["Authorization"]
