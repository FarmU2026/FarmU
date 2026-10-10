"""Approvals + actions, on both backends (local JSON file and DynamoDB via moto). No network, no real AWS."""
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from fieldwatch import actions, approvals, contracts as C
from fieldwatch.approvals import FieldwatchError
from fieldwatch.registry import BY_NAME

T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
PHONE = "+14025550100"


def _make_tables(ddb):
    ddb.create_table(
        TableName="fw-approvals", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                              for n in ("approval_id", "sent_to_person_id", "status")],
        KeySchema=[{"AttributeName": "approval_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "by-person-status",
            "KeySchema": [{"AttributeName": "sent_to_person_id", "KeyType": "HASH"},
                          {"AttributeName": "status", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}}])
    ddb.create_table(
        TableName="fw-actions", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "action_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "action_id", "KeyType": "HASH"}])


@pytest.fixture(scope="module")
def moto_ddb():
    """One moto server and one pair of tables for the module (creating them per test costs seconds)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AWS_ACCESS_KEY_ID", "test")
        mp.setenv("AWS_SECRET_ACCESS_KEY", "test")
        with mock_aws():
            res = boto3.resource("dynamodb", region_name="us-west-2")
            _make_tables(res)
            yield res


def _empty(table, key):
    for item in table.scan().get("Items", []):
        table.delete_item(Key={key: item[key]})


@pytest.fixture(params=["local", "dynamo"])
def env(request, tmp_path, monkeypatch):
    """A fresh store per test, a frozen clock, and captured events."""
    now = {"t": T0}
    monkeypatch.setattr(approvals, "clock", lambda: now["t"])
    events = []
    approvals.set_event_sink(lambda kind, detail: events.append((kind, detail)))
    if request.param == "local":
        approvals.use_store(approvals.LocalStore(tmp_path))
    else:
        res = request.getfixturevalue("moto_ddb")
        _empty(res.Table("fw-approvals"), "approval_id")
        _empty(res.Table("fw-actions"), "action_id")
        approvals.use_store(approvals.DynamoStore(res, prefix="fw-"))
    yield now, events
    approvals.use_store(None)
    approvals.set_event_sink(None)


class FakeSender:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, **kw):
        self.calls.append(kw)
        return {"ok": self.ok, "detail": "fake"}


def pending(**kw):
    args = dict(investigation_id="inv-1", action_type="work_order", summary="Send Brother a repair ticket",
                sent_to_person_id="dad", ttl_hours=24, sent_to_phone=PHONE)
    args.update(kw)
    return approvals.create_pending(**args)


def approve(a, who="dad", channel="sms", phone=PHONE):
    return approvals.record_decision(a.approval_id, "approve", who, channel, from_phone=phone)


def work_order(a, sender, **kw):
    args = dict(approval_id=a.approval_id, investigation_id=a.investigation_id, assignee_person_id="brother",
                field_id="north-pivot", field_name="north pivot", instructions="Check the nozzles on the last tower",
                pin=(-96.54, 42.15), assignee_phone="402 555 0111", sender=sender)
    args.update(kw)
    return actions.create_work_order(**args)


def code(fn, *a, **kw) -> str:
    with pytest.raises(FieldwatchError) as e:
        fn(*a, **kw)
    return e.value.code


# ---------- approvals ----------


def test_pending_then_approved_then_one_action(env):
    _, events = env
    a = pending()
    assert a.status == "pending" and a.expires_at == "2026-07-19T12:00:00Z"
    assert approvals.get(a.approval_id).status == "pending"
    assert approvals.latest_pending_for("dad").approval_id == a.approval_id

    done = approve(a)
    assert (done.status, done.decided_by, done.channel) == ("approved", "dad", "sms")
    assert approvals.latest_pending_for("dad") is None
    assert [k for k, _ in events] == ["ApprovalRecorded"]
    C.ApprovalRecorded.model_validate(events[0][1])  # matches the contract

    sender = FakeSender()
    act = work_order(done, sender)
    assert act.type == "work_order" and act.status == "sent" and act.approval_id == a.approval_id
    assert len(sender.calls) == 1


def test_second_yes_is_already_decided_with_who_and_when(env):
    a = pending()
    approve(a)
    with pytest.raises(FieldwatchError) as e:
        approve(a)
    assert e.value.code == "ALREADY_DECIDED"
    assert e.value.details["decided_by"] == "dad" and e.value.details["decided_at"] == "2026-07-18T12:00:00Z"
    assert "dad" in e.value.message
    # a NO after a YES does not flip it either
    assert code(approvals.record_decision, a.approval_id, "decline", "dad", "web") == "ALREADY_DECIDED"
    assert approvals.get(a.approval_id).status == "approved"


def test_wrong_person_or_phone_rejected(env):
    _, events = env
    a = pending()
    assert code(approvals.record_decision, a.approval_id, "approve", "brother", "web") == "POLICY_DENIED"
    assert code(approvals.record_decision, a.approval_id, "approve", "dad", "sms", from_phone="+14025559999") == "POLICY_DENIED"
    assert code(approvals.record_decision, a.approval_id, "approve", "dad", "sms") == "INVALID_INPUT"  # no number given
    assert approvals.get(a.approval_id).status == "pending" and events == []
    # the right number in a different format is the same number
    assert approve(a, phone="(402) 555-0100").status == "approved"


def test_web_decision_needs_no_phone(env):
    a = pending()
    assert approvals.record_decision(a.approval_id, "decline", "dad", "web").status == "declined"


def test_expired_rejected_and_reads_as_expired(env):
    now, events = env
    a = pending(ttl_hours=1)
    now["t"] = T0 + timedelta(hours=2)
    assert approvals.get(a.approval_id).status == "expired"
    assert approvals.latest_pending_for("dad") is None
    assert code(approve, a) == "EXPIRED"
    assert approvals.get(a.approval_id).status == "expired" and events == []
    assert code(approve, a) == "EXPIRED"


def test_unknown_approval_and_bad_input(env):
    assert code(approvals.record_decision, "apr-nope", "approve", "dad", "web") == "NOT_FOUND"
    a = pending()
    assert code(approvals.record_decision, a.approval_id, "maybe", "dad", "web") == "INVALID_INPUT"
    assert code(pending, action_type="wire_money") == "INVALID_INPUT"


def test_latest_pending_is_the_newest(env):
    now, _ = env
    first = pending(summary="first")
    now["t"] = T0 + timedelta(minutes=5)
    second = pending(summary="second")
    assert approvals.latest_pending_for("dad").approval_id == second.approval_id
    approve(second)
    assert approvals.latest_pending_for("dad").approval_id == first.approval_id


def test_create_pending_with_explicit_id_is_idempotent(env):
    aid = approvals.approval_id_for("inv-1", "work_order", "s")
    a = pending(summary="s", approval_id=aid)
    approve(a)
    again = pending(summary="s", approval_id=aid)
    assert again.approval_id == aid and again.status == "approved"  # not reset to pending


# ---------- actions ----------


def test_action_requires_approved_approval(env):
    sender = FakeSender()
    a = pending()
    assert code(work_order, a, sender) == "POLICY_DENIED"  # still pending
    declined = approvals.record_decision(a.approval_id, "decline", "dad", "web")
    assert code(work_order, declined, sender) == "POLICY_DENIED"
    assert code(actions.create_work_order, approval_id="apr-missing", investigation_id="inv-1",
                assignee_person_id="b", field_id="f", instructions="x", sender=sender) == "POLICY_DENIED"
    assert sender.calls == []


def test_action_requires_matching_investigation_and_type(env):
    sender = FakeSender()
    a = approve(pending())
    assert code(work_order, a, sender, investigation_id="inv-OTHER") == "POLICY_DENIED"
    assert code(actions.create_scout_pin, approval_id=a.approval_id, investigation_id="inv-1", person_id="dad",
                pin=(-96.5, 42.1), what_to_check="x", sender=sender) == "POLICY_DENIED"  # approved a work order
    assert sender.calls == []


def test_action_rejected_when_approval_expired(env):
    now, _ = env
    a = approve(pending(ttl_hours=1))
    now["t"] = T0 + timedelta(hours=3)
    assert code(work_order, a, FakeSender()) == "POLICY_DENIED"


def test_action_creation_is_idempotent_on_approval_id(env):
    now, _ = env
    sender = FakeSender()
    a = approve(pending())
    first = work_order(a, sender)
    now["t"] = T0 + timedelta(hours=30)  # a retry after the approval's window still returns the same action
    second = work_order(a, sender)
    assert first == second and first.action_id == actions.action_id_for(a.approval_id)
    assert len(sender.calls) == 1  # one approval, one action, one text
    assert actions.get_action(first.action_id) == first


def test_one_approval_cannot_back_two_different_actions(env):
    sender = FakeSender()
    a = approve(pending())
    first = work_order(a, sender)
    assert first.type == "work_order"
    assert code(actions.create_scout_pin, approval_id=a.approval_id, investigation_id="inv-1", person_id="dad",
                pin=(-96.5, 42.1), what_to_check="x", sender=sender) == "POLICY_DENIED"


def test_work_order_text_goes_to_the_assignee(env):
    sender = FakeSender()
    act = work_order(approve(pending()), sender)
    (call,) = sender.calls
    assert call["person_id"] == "brother" and call["phone"] == "402 555 0111"
    assert "Check the nozzles on the last tower" in call["body"] and "north pivot" in call["body"]
    assert "maps.google.com/?q=42.15,-96.54" in call["body"]
    assert len(call["body"]) < 300
    assert call["idempotency_key"] == f"{act.action_id}#text" and call["investigation_id"] == "inv-1"
    assert actions.delivery(act.action_id)["delivered"] is True


def test_failed_delivery_is_retried_not_lost(env):
    a = approve(pending())
    bad = FakeSender(ok=False)
    act = work_order(a, bad)
    assert actions.delivery(act.action_id)["delivered"] is False
    good = FakeSender()
    assert work_order(a, good) == act  # same action; the text goes out now
    assert len(good.calls) == 1 and actions.delivery(act.action_id)["delivered"] is True
    work_order(a, good)
    assert len(good.calls) == 1


def test_declined_sends_nothing(env):
    sender = FakeSender()
    a = approvals.record_decision(pending().approval_id, "no", "dad", "sms", from_phone=PHONE)
    assert a.status == "declined"
    assert code(work_order, a, sender) == "POLICY_DENIED"
    assert sender.calls == []


def test_mark_done_emits_once(env):
    _, events = env
    act = work_order(approve(pending()), FakeSender())
    events.clear()
    done = actions.mark_done(act.action_id, "brother")
    assert done.status == "done" and done.done_at == "2026-07-18T12:00:00Z"
    assert actions.mark_done(act.action_id, "brother").status == "done"
    assert [k for k, _ in events] == ["ActionCompleted"]
    assert C.ActionCompleted.model_validate(events[0][1]).completed_by == "brother"
    assert code(actions.mark_done, "act-nope", "brother") == "NOT_FOUND"


def test_no_action_completed_before_done(env):
    _, events = env
    work_order(approve(pending()), FakeSender())
    assert "ActionCompleted" not in [k for k, _ in events]


def test_scout_pin_and_insurance_notice(env):
    sender = FakeSender()
    a = approve(pending(action_type="scout_pin", summary="Scout the NE corner"))
    pin = actions.create_scout_pin(approval_id=a.approval_id, investigation_id="inv-1", person_id="dad",
                                   pin=(-96.5, 42.1), what_to_check="yellow leaves", field_name="north pivot",
                                   phone=PHONE, sender=sender)
    assert pin.type == "scout_pin" and pin.pin == (-96.5, 42.1) and len(sender.calls) == 1

    b = approve(pending(action_type="insurance_notice", summary="Possible hail loss on the north pivot"))
    note = actions.draft_insurance_notice(approval_id=b.approval_id, investigation_id="inv-1", person_id="dad")
    assert note.type == "insurance_notice" and "DRAFT" in note.instructions and "hail loss" in note.instructions
    assert len(sender.calls) == 1  # a draft is not texted to anyone
    assert code(actions.draft_insurance_notice, approval_id=a.approval_id, investigation_id="inv-1",
                person_id="dad") == "POLICY_DENIED"  # wrong type


def test_work_order_view(env):
    act = work_order(approve(pending()), FakeSender())
    v = actions.work_order_view(act.action_id)
    assert (v.field_name, v.pin, v.status) == ("north pivot", (-96.54, 42.15), "sent")
    assert v.navigate_url == "https://maps.google.com/?q=42.15,-96.54"
    assert code(actions.work_order_view, "act-nope") == "NOT_FOUND"


# ---------- registry (Gateway schemas) ----------


@pytest.mark.parametrize("name", ["create_work_order", "create_scout_pin", "draft_insurance_notice"])
def test_registry_write_tools_require_both_ids(name):
    spec = BY_NAME[name]
    assert spec.access == "write" and not spec.stub
    schema = spec.input_model.model_json_schema()
    assert {"approval_id", "investigation_id"} <= set(schema["required"])
    assert schema["additionalProperties"] is False
    with pytest.raises(Exception):
        spec.input_model.model_validate({"investigation_id": "inv-1"})
    with pytest.raises(Exception):  # malformed approval id
        spec.input_model.model_validate({"investigation_id": "inv-1", "approval_id": "../x", "person_id": "d",
                                         "pin": [0, 0], "what_to_check": "x", "field_id": "f",
                                         "assignee_person_id": "a", "instructions": "i"})


def test_registry_fn_runs_end_to_end(env):
    a = approve(pending())
    spec = BY_NAME["create_work_order"]
    args = spec.input_model(investigation_id="inv-1", approval_id=a.approval_id, field_id="north-pivot",
                            assignee_person_id="brother", instructions="Check nozzles", pin=(-96.5, 42.1))
    out = spec.fn(**args.model_dump(), sender=FakeSender())
    assert isinstance(out, C.Action) and out.status == "sent"


# ---------- backend plumbing ----------


def test_dynamo_backend_selected_by_env(monkeypatch, moto_ddb):
    monkeypatch.setenv("FIELDWATCH_APPROVALS_BACKEND", "dynamo")
    approvals._store_cache.clear()
    approvals.use_store(None)
    events = boto3.client("events", region_name="us-west-2")
    events.create_event_bus(Name="fieldwatch")
    assert isinstance(approvals.store(), approvals.DynamoStore)
    a = approvals.create_pending("inv-9", "work_order", "s", "dad", 1)
    assert approvals.record_decision(a.approval_id, "yes", "dad", "web").status == "approved"  # put_events works
    approvals._store_cache.clear()


def test_unknown_backend_is_an_error(monkeypatch):
    monkeypatch.setenv("FIELDWATCH_APPROVALS_BACKEND", "carrier-pigeon")
    approvals.use_store(None)
    assert code(approvals.store) == "INVALID_INPUT"


def test_default_sender_falls_back_to_sms(monkeypatch):
    sent = []
    import fieldwatch.sms as sms

    monkeypatch.setattr(sms, "send", lambda to, text: sent.append((to, text)) or {"ok": True, "detail": "x"})
    res = actions.default_sender(person_id="brother", phone="402 555 0111", body="hi", farm_id="f",
                                 investigation_id="inv-1", idempotency_key="k")
    assert res["ok"] and sent == [("402 555 0111", "hi")]
    assert actions.default_sender(person_id="nobody", phone=None, body="hi", farm_id="f",
                                  investigation_id="inv-1", idempotency_key="k")["ok"] is False


def test_default_sender_prefers_notify_send(monkeypatch):
    from fieldwatch import interfaces

    got = []

    class FakeNotify:
        def send(self, req):
            got.append(req)
            return C.SendMessageResult(message_id="m1", status="sent", provider="twilio")

        def reply(self, message):
            return {}

    interfaces.use_notify(FakeNotify())
    res = actions.default_sender(person_id="brother", phone=None, body="hi", farm_id="f", investigation_id="inv-1",
                                 idempotency_key="k", link={"scope": "work_order", "subject_id": "a", "ttl_hours": 72})
    assert res["ok"] and isinstance(got[0], C.SendMessageRequest) and got[0].person_id == "brother"


# ---------- the local agent loop uses the same records ----------


def test_agent_interrupt_carries_approval_id_and_yes_makes_one_action(tmp_path, monkeypatch):
    from test_smoke import FakeModel

    import fieldwatch.agents as agents
    import fieldwatch.investigation as inv

    monkeypatch.setattr(agents, "model", lambda: FakeModel())
    monkeypatch.setattr(inv, "SESSIONS", tmp_path / "sessions")
    approvals.use_store(approvals.LocalStore(tmp_path))
    sent = []
    monkeypatch.setattr(actions, "default_sender", lambda **kw: sent.append(kw) or {"ok": True, "detail": "x"})
    try:
        events = []
        step = inv.start("inv-local1", "north pivot", emit=events.append)
        assert step.status == "waiting_for_farmer"
        a = approvals.latest_pending_for("Dad")
        assert a and a.investigation_id == "inv-local1" and a.action_type == "work_order"
        inv.reply("inv-local1", step.interrupt_id, "YES", emit=events.append)
        assert approvals.get(a.approval_id).status == "approved"
        act = actions.get_action(actions.action_id_for(a.approval_id))
        assert act and act.assignee_person_id == "Brother"
        assert len(sent) == 1 and sent[0]["person_id"] == "Brother"
    finally:
        approvals.use_store(None)


def test_agent_decline_records_declined_and_sends_nothing(tmp_path, monkeypatch):
    from test_smoke import FakeModel

    import fieldwatch.agents as agents
    import fieldwatch.investigation as inv

    monkeypatch.setattr(agents, "model", lambda: FakeModel())
    monkeypatch.setattr(inv, "SESSIONS", tmp_path / "sessions")
    approvals.use_store(approvals.LocalStore(tmp_path))
    sent = []
    monkeypatch.setattr(actions, "default_sender", lambda **kw: sent.append(kw) or {"ok": True, "detail": "x"})
    try:
        step = inv.start("inv-local2", "north pivot", emit=lambda e: None)
        a = approvals.latest_pending_for("Dad")
        inv.reply("inv-local2", step.interrupt_id, "no", emit=lambda e: None)
        assert approvals.get(a.approval_id).status == "declined"
        assert actions.get_action(actions.action_id_for(a.approval_id)) is None and sent == []
    finally:
        approvals.use_store(None)


# ---------- Cedar policy file ----------


def test_cedar_policy_covers_registry():
    import re
    from pathlib import Path

    from fieldwatch.registry import TOOLS

    text = (Path(__file__).resolve().parents[2] / "infra" / "policies" / "cedar" / "fieldwatch-gateway.cedar").read_text()
    blocks = {m.group(1): m.group(2) for m in re.finditer(r"// ---- policy: (\S+) ----\n(.*?)(?=\n// ---- policy:|\Z)", text, re.S)}
    assert set(blocks) == {"fieldwatch-read-tools", "fieldwatch-write-tools-need-approval", "fieldwatch-messaging-tools"}
    tools_in = {n: set(re.findall(r'\{\{TARGET\}\}___(\w+)"', b)) for n, b in blocks.items()}
    for spec in TOOLS:
        home = "fieldwatch-write-tools-need-approval" if spec.access == "write" else "fieldwatch-read-tools"
        assert spec.name in tools_in[home], f"{spec.name} missing from the Cedar {home} policy"
        for other, names in tools_in.items():
            assert other == home or spec.name not in names, f"{spec.name} also in {other}"
    guard = blocks["fieldwatch-write-tools-need-approval"]
    assert "context.input.approval_id like" in guard and "context.input.investigation_id" in guard
    assert "when" in guard and "when" not in blocks["fieldwatch-read-tools"]
