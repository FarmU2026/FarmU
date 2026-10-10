"""WS7: the session starter routes bus events to Runtime invocations. DynamoDB is moto; the Runtime is a fake."""
import io
import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from cloud.lambdas.session_starter import handler as H
from fieldwatch import config
from fieldwatch import contracts as C
from runtime_support import REGION, make_investigations_table

FIXTURES = config.FIXTURES / "contracts"
ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fieldwatch_agent-AbC123"


def sample(name, **kw):
    return {**json.loads((FIXTURES / f"{name}.json").read_text()), **kw}


def envelope(kind, detail, source="fieldwatch.test"):
    return {"detail-type": kind, "source": source, "detail": detail}


class FakeRuntime:
    def __init__(self, ack=None, fail=None):
        self.calls, self.ack, self.fail = [], ack or {"accepted": True}, fail

    def invoke_agent_runtime(self, **kw):
        if self.fail:
            raise self.fail
        self.calls.append(kw)
        return {"response": io.BytesIO(json.dumps(self.ack).encode())}

    def payload(self, i=-1):
        return json.loads(self.calls[i]["payload"])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def world():
    with mock_aws():
        table = make_investigations_table()
        runtime = FakeRuntime()
        yield H.Starter(table, runtime, lambda: ARN), table, runtime


def pending(table, inv="inv-north-pivot-20260718", interrupt="int-abc123", status="awaiting_reply"):
    table.put_item(Item={"investigation_id": inv, "farm_id": "wohleb-farm", "field_id": "north-pivot",
                         "created_at": "2026-07-18T17:21:05Z", "status": status, "pending_interrupt_id": interrupt})


# ---------- ids ----------


def test_session_id_is_at_least_33_chars():
    assert H.session_id_for("inv-north-pivot-20260718") == "fieldwatch-session-inv-north-pivot-20260718"
    for short in ("a", "abc", "inv-1"):
        sid = H.session_id_for(short)
        assert len(sid) >= 33 and sid.startswith("fieldwatch-session-")
        assert sid == H.session_id_for(short)  # deterministic: a resume lands on the same session


def test_investigation_id_derives_from_anomaly_id():
    assert H.investigation_id_for_anomaly("anm-north-pivot-20260718") == "inv-north-pivot-20260718"
    assert H.investigation_id_for_anomaly("anm_01HZX") == "inv_01HZX"
    assert H.investigation_id_for_anomaly("odd") == "inv-odd"


# ---------- starting ----------


def test_anomaly_creates_item_and_starts_agent(world):
    starter, table, runtime = world
    out = starter.handle(envelope("AnomalyDetected", sample("anomaly_detected"), "fieldwatch.decision"))

    assert out["routed"] == "start" and out["investigation_id"] == "inv-north-pivot-20260718"
    item = table.get_item(Key={"investigation_id": "inv-north-pivot-20260718"})["Item"]
    assert item["status"] == "queued" and item["farm_id"] == "wohleb-farm" and item["field_id"] == "north-pivot"
    assert item["anomaly_id"] == "anm-north-pivot-20260718"

    call = runtime.calls[0]
    assert call["agentRuntimeArn"] == ARN and len(call["runtimeSessionId"]) >= 33
    assert call["runtimeSessionId"] == "fieldwatch-session-inv-north-pivot-20260718"
    req = C.StartInvestigation.model_validate(runtime.payload())  # exactly the Runtime's contract
    assert req.field == "north-pivot" and req.anomaly.anomaly_id == "anm-north-pivot-20260718"


def test_duplicate_anomaly_does_not_start_twice(world):
    starter, table, runtime = world
    ev = envelope("AnomalyDetected", sample("anomaly_detected"))
    starter.handle(ev)
    table.update_item(Key={"investigation_id": "inv-north-pivot-20260718"},
                      UpdateExpression="SET #s = :r", ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":r": "running"})
    assert starter.handle(ev)["routed"] == "duplicate"
    assert len(runtime.calls) == 1


def test_retry_after_failed_invoke_starts_once_the_runtime_is_back(world):
    starter, table, runtime = world
    ev = envelope("AnomalyDetected", sample("anomaly_detected"))
    starter.runtime = FakeRuntime(fail=ClientError({"Error": {"Code": "ThrottlingException"}}, "InvokeAgentRuntime"))
    with pytest.raises(ClientError):
        starter.handle(ev)  # item stays "queued", EventBridge retries
    starter.runtime = runtime
    assert starter.handle(ev)["routed"] == "start" and len(runtime.calls) == 1


def test_api_start_investigation_event(world):
    starter, table, runtime = world
    out = starter.handle(envelope("StartInvestigation", sample("start_investigation"), "fieldwatch.api"))
    assert out["routed"] == "start"
    assert C.StartInvestigation.model_validate(runtime.payload()).investigation_id == "inv-north-pivot-20260718"


def test_start_investigation_from_other_sources_is_dropped(world):
    starter, table, runtime = world
    assert starter.handle(envelope("StartInvestigation", sample("start_investigation"), "evil.source"))["routed"] == "dropped"
    assert not runtime.calls


def test_runtime_rejection_raises(world):
    starter, table, runtime = world
    starter.runtime = FakeRuntime(ack={"error": {"code": "BAD_REQUEST", "message": "x", "retryable": False}})
    with pytest.raises(RuntimeError):
        starter.handle(envelope("StartInvestigation", sample("start_investigation"), "fieldwatch.api"))


# ---------- resuming ----------


def test_approval_resumes_at_the_pending_interrupt(world):
    starter, table, runtime = world
    pending(table)
    out = starter.handle(envelope("ApprovalRecorded", sample("approval_recorded")))

    assert out["routed"] == "resume" and out["text"] == "YES"
    req = C.ResumeInvestigation.model_validate(runtime.payload())
    assert (req.interrupt_id, req.text, req.approval_id) == ("int-abc123", "YES", "apr-001")
    assert runtime.calls[0]["runtimeSessionId"] == "fieldwatch-session-inv-north-pivot-20260718"
    item = table.get_item(Key={"investigation_id": "inv-north-pivot-20260718"})["Item"]
    assert "pending_interrupt_id" not in item


def test_declined_approval_sends_no(world):
    starter, table, runtime = world
    pending(table)
    starter.handle(envelope("ApprovalRecorded", sample("approval_recorded", decision="declined")))
    assert runtime.payload()["text"] == "NO"


def test_reply_and_approval_for_same_approval_resume_once(world):
    starter, table, runtime = world
    pending(table)
    yes = sample("reply_received", intent="yes", text="YES", approval_id="apr-001")
    assert starter.handle(envelope("ReplyReceived", yes))["routed"] == "resume"
    # the second event arrives while the agent is running, then after it paused on a different question
    assert starter.handle(envelope("ApprovalRecorded", sample("approval_recorded")))["routed"] == "duplicate"
    table.update_item(Key={"investigation_id": "inv-north-pivot-20260718"},
                      UpdateExpression="SET pending_interrupt_id = :i", ExpressionAttributeValues={":i": "int-second"})
    assert starter.handle(envelope("ApprovalRecorded", sample("approval_recorded")))["routed"] == "duplicate"
    assert len(runtime.calls) == 1


def test_failed_resume_releases_the_claim_for_the_retry(world):
    starter, table, runtime = world
    pending(table)
    starter.runtime = FakeRuntime(fail=ClientError({"Error": {"Code": "ThrottlingException"}}, "InvokeAgentRuntime"))
    with pytest.raises(ClientError):
        starter.handle(envelope("ApprovalRecorded", sample("approval_recorded")))
    item = table.get_item(Key={"investigation_id": "inv-north-pivot-20260718"})["Item"]
    assert item["pending_interrupt_id"] == "int-abc123" and item["status"] == "awaiting_reply"
    starter.runtime = runtime
    assert starter.handle(envelope("ApprovalRecorded", sample("approval_recorded")))["routed"] == "resume"


def test_reply_no_resumes_but_yes_needs_an_approval(world):
    starter, table, runtime = world
    pending(table)
    assert starter.handle(envelope("ReplyReceived", sample("reply_received", intent="yes", text="YES")))["routed"] == "dropped"
    assert not runtime.calls
    assert starter.handle(envelope("ReplyReceived", sample("reply_received", intent="no", text="NO")))["routed"] == "resume"
    assert runtime.payload()["text"] == "NO"


def test_other_reply_intents_and_strangers_are_dropped(world):
    starter, table, runtime = world
    pending(table)
    assert starter.handle(envelope("ReplyReceived", sample("reply_received")))["routed"] == "dropped"  # MORE
    assert starter.handle(envelope("ReplyReceived", sample("reply_received", intent="no", investigation_id=None)))["routed"] == "dropped"
    assert not runtime.calls


def test_resume_while_agent_still_running_is_retried(world):
    starter, table, runtime = world
    table.put_item(Item={"investigation_id": "inv-north-pivot-20260718", "status": "running"})
    with pytest.raises(H.NotReady):
        starter.handle(envelope("ApprovalRecorded", sample("approval_recorded")))


def test_resume_with_nothing_pending_is_dropped(world):
    starter, table, runtime = world
    table.put_item(Item={"investigation_id": "inv-north-pivot-20260718", "status": "concluded"})
    assert starter.handle(envelope("ApprovalRecorded", sample("approval_recorded")))["routed"] == "dropped"


# ---------- record-only events and garbage ----------


def test_action_completed_and_photo_are_recorded(world):
    starter, table, runtime = world
    pending(table)
    done = {"schema_version": "1", "occurred_at": "2026-07-19T10:00:00Z", "action_id": "act-001",
            "investigation_id": "inv-north-pivot-20260718", "completed_by": "brother"}
    assert starter.handle(envelope("ActionCompleted", done))["routed"] == "recorded"
    photo = {"upload_id": "upl-1", "investigation_id": "inv-north-pivot-20260718"}
    assert starter.handle(envelope("PhotoReceived", photo))["routed"] == "recorded"
    item = table.get_item(Key={"investigation_id": "inv-north-pivot-20260718"})["Item"]
    assert item["last_event_type"] == "PhotoReceived" and item["pending_interrupt_id"] == "int-abc123"
    assert not runtime.calls
    assert starter.handle(envelope("PhotoReceived", {"upload_id": "u"}))["routed"] == "dropped"


def test_invalid_and_unknown_events_do_not_raise(world):
    starter, table, runtime = world
    assert starter.handle(envelope("AnomalyDetected", {"anomaly_id": "x"}))["reason"].startswith("invalid_payload")
    assert starter.handle(envelope("SomethingElse", {}))["reason"] == "unknown_event"
    assert not runtime.calls


def test_runtime_arn_comes_from_loader_once():
    calls = []
    s = H.Starter(None, None, lambda: calls.append(1) or ARN)
    assert s.runtime_arn == ARN and s.runtime_arn == ARN and len(calls) == 1
