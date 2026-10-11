"""The cross-lane seams in fieldwatch/interfaces.py: one event registry, one publish path, one error type."""
import io
import json
import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from fieldwatch import contracts as C
from fieldwatch import interfaces as I

AGENT = Path(__file__).resolve().parents[1]
REPO = AGENT.parent
SAMPLES = AGENT / "fixtures" / "contracts"


def sample(name):
    return json.loads((SAMPLES / f"{name}.json").read_text())


# ---------- one publish path ----------


def test_no_direct_put_events_outside_interfaces():
    """Every EventBridge PutEvents goes through interfaces.publish_event(s)."""
    offenders = []
    for path in list(AGENT.rglob("*.py")) + list((REPO / "infra").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if "/tests/" in f"/{rel}" or "node_modules" in rel or "cdk.out" in rel or rel.endswith("fieldwatch/interfaces.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\.put_events\s*\(", text) or re.search(r"\bdef\s+put_events\b", text):
            offenders.append(rel)
    assert not offenders, f"call interfaces.publish_event instead of put_events in: {offenders}"


@mock_aws
def test_publish_event_validates_and_puts():
    events = boto3.client("events", region_name="us-west-2")
    events.create_event_bus(Name="fieldwatch")
    sqs = boto3.client("sqs", region_name="us-west-2")
    url = sqs.create_queue(QueueName="tap")["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    events.put_rule(Name="all", EventBusName="fieldwatch", EventPattern=json.dumps({"source": [{"prefix": ""}]}))
    events.put_targets(Rule="all", EventBusName="fieldwatch", Targets=[{"Id": "q", "Arn": arn}])

    ev = C.ApprovalRecorded.model_validate(sample("approval_recorded"))
    I.publish_event(ev, client=events, bus="fieldwatch")
    msgs = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10)["Messages"]
    env = json.loads(msgs[0]["Body"])
    assert env["detail-type"] == "ApprovalRecorded" and env["source"] == I.Sources.NOTIFY  # canonical producer
    assert I.parse_event(env) == ev


def test_publish_event_rejects_wrong_source_and_unregistered_models():
    ev = C.SceneAnalyzed(occurred_at="2026-07-18T00:00:00Z", field=C.FieldRef(farm_id="f", field_id="x"),
                         scene_id="s", date="2026-07-18", status="ok", max_score=1.0, region_count=0)
    with pytest.raises(I.FieldwatchError) as e:
        I.publish_event(ev, source=I.Sources.API, client=object())
    assert e.value.code == "INVALID_INPUT"
    with pytest.raises(I.FieldwatchError):
        I.publish_event(C.FieldRef(farm_id="f", field_id="x"), client=object())


def test_publish_events_batches_and_reports_rejections():
    calls = []

    class Fake:
        def put_events(self, Entries):  # noqa: N803
            calls.append(len(Entries))
            return {"FailedEntryCount": 0, "Entries": [{}] * len(Entries)}

    evs = [C.MrmsFileAvailable.model_validate(sample("mrms_file_available"))] * 14
    assert I.publish_events(evs, source=I.Sources.REPLAY, client=Fake()) == 14
    assert calls == [10, 4]

    class Rejecting:
        def put_events(self, Entries):  # noqa: N803
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "Throttled"}]}

    with pytest.raises(I.FieldwatchError) as e:
        I.publish_event(evs[0], source=I.Sources.REPLAY, client=Rejecting())
    assert e.value.code == "UPSTREAM_ERROR" and e.value.retryable


# ---------- the registry covers every event the code and infra produce or consume ----------


def _infra_detail_types() -> set[str]:
    found = set()
    for path in (REPO / "infra" / "stacks").glob("*.py"):
        text = path.read_text()
        for block in re.findall(r"(?:DETAIL_TYPES|RULES)\s*=\s*\[(.*?)\n\]", text, re.S):
            found |= set(re.findall(r'"([A-Z][A-Za-z]+)"', block))
    return found


def test_registry_covers_infra_rules_and_feed():
    from cloud.feed import mapping

    infra = _infra_detail_types()
    assert infra, "found no EventBridge rules in infra/stacks"
    assert infra <= set(I.EVENT_REGISTRY), f"rules match unregistered events: {infra - set(I.EVENT_REGISTRY)}"
    assert set(mapping.EVENT_MODELS) <= set(I.EVENT_REGISTRY)
    for name, model in mapping.EVENT_MODELS.items():
        assert I.EVENT_REGISTRY[name].model is model


def test_registry_names_match_models_and_sources():
    for name, spec in I.EVENT_REGISTRY.items():
        assert spec.model.__name__ == name
        assert spec.sources and all(s.startswith("fieldwatch.") for s in spec.sources)


# ---------- errors ----------


def test_one_error_type_with_the_full_code_list():
    from fieldwatch import approvals

    assert approvals.FieldwatchError is I.FieldwatchError
    for code in ("NOT_FOUND", "POLICY_DENIED", "ALREADY_DECIDED", "EXPIRED", "INVALID_INPUT", "RATE_LIMITED",
                 "QUIET_HOURS", "UPSTREAM_TIMEOUT", "UPSTREAM_ERROR", "NOT_IMPLEMENTED", "TOKEN_EXPIRED",
                 "TOKEN_SCOPE", "INTERNAL"):
        assert code in I.ERROR_CODES and code in I.HTTP_STATUS
    e = I.FieldwatchError("ALREADY_DECIDED", "already approved", decided_by="dad")
    assert e.body()["error"]["decided_by"] == "dad" and e.http_status == 409 and not e.retryable


# ---------- rows ----------


def test_thread_item_round_trip():
    t = I.ThreadItem(farm_id="farm_1", id="msg-1", at="2026-07-18T17:00:00Z", who="farmer", text="YES",
                     kind="message", investigation_id="inv-1")
    item = t.to_item()
    assert item["item"] == "THREAD#2026-07-18T17:00:00Z#msg-1" and "id" not in item and "field_id" not in item
    assert I.ThreadItem.from_item(item) == t


def test_approval_record_accepts_notify_rows():
    row = {**sample("approval"), "farm_id": "wohleb-farm", "created_at": "2026-07-18T17:00:00Z",
           "expires_at_epoch": 1_800_000_000}
    assert I.ApprovalRecord.model_validate(row).to_contract() == C.Approval.model_validate(sample("approval"))


def test_open_statuses_cover_what_the_agent_writes():
    for status in ("queued", "running", "resuming", "awaiting_reply"):
        assert status in I.OPEN_INVESTIGATION_STATUSES
    assert "concluded" not in I.OPEN_INVESTIGATION_STATUSES


def test_new_farm_ids_carry_no_phone_digits():
    a, b = I.new_farm_id(), I.new_farm_id()
    assert a.startswith("farm_") and a != b and len(a) == 31
    assert not re.search(r"\d{7,}", a)  # never a phone number


# ---------- notify over Lambda ----------


class _FakeLambda:
    def __init__(self, body, error=None):
        self.body, self.error, self.calls = body, error, []

    def invoke(self, **kw):
        self.calls.append(kw)
        out = {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(self.body).encode())}
        if self.error:
            out["FunctionError"] = "Unhandled"
        return out


def test_lambda_notify_send_and_mirror():
    result = sample("send_message_result")
    fake = _FakeLambda(result)
    svc = I.LambdaNotify("fw-notify-send", "fw-notify-mirror", client=fake)
    assert isinstance(svc, I.NotifyAPI)
    req = C.SendMessageRequest.model_validate(sample("send_message_request"))
    assert svc.send(req) == C.SendMessageResult.model_validate(result)
    assert fake.calls[0]["FunctionName"] == "fw-notify-send"
    with pytest.raises(I.FieldwatchError):  # mirror ids must be namespaced
        svc.reply(C.InboundMessage.model_validate(sample("inbound_message")))
    bad = I.LambdaNotify("fw-notify-send", client=_FakeLambda({"errorMessage": "Unknown farm or recipient",
                                                                "errorType": "ValueError"}, error=True))
    with pytest.raises(I.FieldwatchError) as e:
        bad.send(req)
    assert e.value.code == "INVALID_INPUT"


def test_notify_is_configured_by_env(monkeypatch):
    monkeypatch.delenv("FIELDWATCH_SEND_FN", raising=False)
    assert I.notify() is None
    monkeypatch.setenv("FIELDWATCH_SEND_FN", "fw-notify-send")
    assert isinstance(I.notify(), I.LambdaNotify)


def test_scene_queue_message_shapes():
    item = sample("scene_queue_message")["stac_item"]
    assert C.SceneQueueMessage.parse(json.dumps(item)).replay is False
    sns = {"Type": "Notification", "Message": json.dumps(item)}
    assert C.SceneQueueMessage.parse(sns).stac_item["id"] == item["id"]
    env = C.SceneQueueMessage.parse(sample("scene_queue_message"))
    assert env.replay and env.replay_id == "rpl_0001" and env.as_of == "2026-07-18"
    with pytest.raises(ValueError):
        C.SceneQueueMessage.parse({"type": "check", "farm_id": "x"})  # the API's old envelope is not accepted


# ---------- notify -> approvals (cloud/lambdas/notify/runtime.py adapter) ----------


def test_notify_decision_adapter_maps_names_and_does_not_double_emit(tmp_path, monkeypatch):
    from cloud.lambdas.notify import runtime
    from fieldwatch import approvals

    approvals.use_store(approvals.LocalStore(tmp_path))
    emitted = []
    approvals.set_event_sink(lambda kind, detail: emitted.append(kind))
    monkeypatch.setattr(runtime, "_person_phone", lambda approval_id, person_id: "+14025550100")
    a = approvals.create_pending("inv-1", "work_order", "Send a repair ticket", "dad", sent_to_phone="402 555 0100",
                                 farm_id="farm_x")
    # exactly the call Notifications.reply makes (tests/test_notify.py pins it)
    out = runtime.record_decision(approval_id=a.approval_id, person_id="dad", decision="approved", channel="sms")
    assert out.status == "approved" and out.decided_by == "dad" and emitted == []
    again = runtime.record_decision(approval_id=a.approval_id, person_id="dad", decision="declined", channel="sms")
    assert again.status == "approved"  # already decided: the stored record, no exception


# ---------- agents.py tools= (investigation passes toolset.tools_for(...)) ----------


def test_supervisor_takes_tools_by_name_and_keeps_local_write_tools():
    from fieldwatch import agents, toolset
    from fieldwatch import tools as T

    remote = toolset.local_tools(names=["get_weather"])  # stands in for the Gateway's copy
    picked = agents._pick(remote, T.list_fields, T.get_weather)
    assert picked[0] is T.list_fields and picked[1] is remote[0]
    assert agents._pick(None, T.list_fields) == [T.list_fields]
