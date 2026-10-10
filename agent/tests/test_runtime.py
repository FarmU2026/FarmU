"""WS7: the Runtime entrypoint, session persistence and the events the agent emits. No network, no real AWS."""
import json
import threading
import uuid

import boto3
import pytest
from moto import mock_aws

import app as runtime_app
import fieldwatch.agents as agents
import fieldwatch.investigation as inv
from fieldwatch import config
from fieldwatch import contracts as C
from runtime_support import FakeModel, REGION, drain, make_bus_with_queue, make_investigations_table

FIXTURES = config.FIXTURES / "contracts"
EVENT_MODELS = {"InvestigationProgress": C.InvestigationProgress, "InvestigationConcluded": C.InvestigationConcluded}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(agents, "model", lambda: FakeModel())
    monkeypatch.setattr(inv, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(config, "REGION", REGION)
    monkeypatch.setattr(config, "SESSIONS_BUCKET", "")
    monkeypatch.setattr(config, "TOOLS_BACKEND", "local")
    # the write tools go through the approvals store; keep it off agent/data so runs don't see each other's approvals
    monkeypatch.setenv("FIELDWATCH_APPROVALS_BACKEND", "local")
    monkeypatch.setenv("FIELDWATCH_APPROVALS_DIR", str(tmp_path / "approvals"))


def _start_payload(**kw):
    return {**json.loads((FIXTURES / "start_investigation.json").read_text()), **kw}


def _resume_payload(**kw):
    return {**json.loads((FIXTURES / "resume_investigation.json").read_text()), **kw}


# ---------- payload parsing ----------


def test_parse_start_and_resume_samples():
    assert isinstance(runtime_app.parse_request(_start_payload()), C.StartInvestigation)
    r = runtime_app.parse_request(_resume_payload())
    assert isinstance(r, C.ResumeInvestigation) and r.text == "YES" and r.approval_id == "apr-001"


def test_parse_unwraps_the_cli_prompt_envelope():
    r = runtime_app.parse_request({"prompt": json.dumps(_resume_payload())})
    assert isinstance(r, C.ResumeInvestigation)
    with pytest.raises(ValueError):
        runtime_app.parse_request({"prompt": "just chatting"})


def test_parse_defaults_to_start():
    p = _start_payload()
    p.pop("action")
    assert isinstance(runtime_app.parse_request(p), C.StartInvestigation)


@pytest.mark.parametrize("payload", [
    {"action": "dance", "investigation_id": "x"},
    {"action": "start", "investigation_id": "x"},  # farm_id and field missing
    {"action": "reply", "investigation_id": "x", "interrupt_id": "i", "text": "YES", "surprise": 1},  # extra field
    "not an object",
])
def test_parse_rejects_bad_payloads(payload):
    with pytest.raises(ValueError):
        runtime_app.parse_request(payload)


def test_entrypoint_returns_error_body_for_bad_payload():
    out = runtime_app.invoke({"action": "dance"})
    assert out["error"]["code"] == "BAD_REQUEST" and out["error"]["retryable"] is False


# ---------- background task flow with a fake agent ----------


def test_ack_then_busy_ping_then_idle(monkeypatch):
    started, release, ran = threading.Event(), threading.Event(), []

    def fake_run(req, recorder=None):
        ran.append(req)
        started.set()
        assert release.wait(10)

    monkeypatch.setattr(inv, "run_request", fake_run)
    ack = runtime_app.invoke(_start_payload(investigation_id="inv-bg-0000000000000001"))

    assert ack == {"accepted": True, "investigation_id": "inv-bg-0000000000000001", "action": "start"}
    assert started.wait(10)
    assert runtime_app.app.get_current_ping_status().value == "HealthyBusy"
    # a duplicate while it runs is refused, not run twice
    dup = runtime_app.invoke(_start_payload(investigation_id="inv-bg-0000000000000001"))
    assert dup["accepted"] is False and dup["reason"] == "already_running"

    release.set()
    assert runtime_app.wait_idle(10)
    assert runtime_app.app.get_current_ping_status().value == "Healthy"
    assert len(ran) == 1 and isinstance(ran[0], C.StartInvestigation)


def test_background_crash_still_frees_the_ping(monkeypatch):
    def boom(req, recorder=None):
        raise RuntimeError("model fell over")

    monkeypatch.setattr(inv, "run_request", boom)
    runtime_app.invoke(_resume_payload(investigation_id="inv-crash-000000000000001"))
    assert runtime_app.wait_idle(10)
    assert runtime_app.app.get_current_ping_status().value == "Healthy"


def test_sync_mode_returns_agent_step(monkeypatch):
    monkeypatch.setenv("FIELDWATCH_RUNTIME_SYNC", "1")
    monkeypatch.setattr(inv, "run_request", lambda req, recorder=None: C.AgentStep(
        status="done", text="ok", investigation_id=req.investigation_id, backend="runtime"))
    out = runtime_app.invoke(_start_payload())
    assert C.AgentStep.model_validate(out).status == "done"


# ---------- session manager selection ----------


def test_file_sessions_without_bucket(tmp_path):
    sm = inv.session_manager("abc")
    assert type(sm).__name__ == "FileSessionManager"


@mock_aws
def test_s3_sessions_with_bucket(monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_BUCKET", "fw-test-bucket")
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket="fw-test-bucket", CreateBucketConfiguration={"LocationConstraint": REGION})
    sm = inv.session_manager("abc")
    assert type(sm).__name__ == "S3SessionManager"
    assert sm.bucket == "fw-test-bucket" and sm.prefix == "sessions/"


@mock_aws
def test_interrupt_survives_a_new_process_via_s3(monkeypatch):
    """Start, drop everything in memory, reply later: the paused agent comes back from S3 sessions/."""
    monkeypatch.setattr(config, "SESSIONS_BUCKET", "fw-test-bucket")
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="fw-test-bucket", CreateBucketConfiguration={"LocationConstraint": REGION})
    inv_id = uuid.uuid4().hex[:8]

    step = inv.start(inv_id, "north pivot", emit=lambda e: None)
    assert step.status == "waiting_for_farmer"
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="fw-test-bucket").get("Contents", [])]
    assert keys and all(k.startswith("sessions/session_fieldwatch-") for k in keys)
    assert not (inv.SESSIONS / f"session_fieldwatch-{inv_id}").exists()  # nothing written to local disk

    done = inv.reply(inv_id, step.interrupt_id, "YES", emit=lambda e: None)
    assert done.status == "done" and done.diagnosis.dollars_high == 14000


# ---------- run_request: events and state against moto ----------


def _run(payload_kind="start", **kw):
    table = make_investigations_table()
    events, sqs, url = make_bus_with_queue()
    inv_id = "inv-north-pivot-20260718"
    table.put_item(Item={"investigation_id": inv_id, "farm_id": "wohleb-farm", "field_id": "north-pivot",
                         "created_at": "2026-07-18T17:21:05Z", "status": "queued"})
    return table, sqs, url, inv_id


def _envelopes(sqs, url):
    out = drain(sqs, url)
    for e in out:
        assert e["source"] == "fieldwatch.agent" and e["detail-type"] in EVENT_MODELS
    return out


@mock_aws
def test_start_emits_valid_progress_and_records_waiting():
    table, sqs, url, inv_id = _run()
    req = C.StartInvestigation.model_validate(_start_payload())
    out = inv.run_request(req)

    assert out.status == "waiting_for_farmer" and out.backend == "runtime" and out.interrupt_id
    C.AgentStep.model_validate(out.model_dump())

    details = [EVENT_MODELS[e["detail-type"]].model_validate(e["detail"]) for e in _envelopes(sqs, url)]
    progress = [d for d in details if isinstance(d, C.InvestigationProgress)]
    assert [p.seq for p in progress] == list(range(1, len(progress) + 1))  # strictly increasing from 1
    assert progress[0].step == "plan" and progress[-1].step == "waiting"
    assert any(p.tool == "create_work_order" for p in progress)
    assert {p.agent for p in progress} <= {"supervisor", "imagery", "environment", "agronomy"}
    assert not any(isinstance(d, C.InvestigationConcluded) for d in details)

    item = table.get_item(Key={"investigation_id": inv_id})["Item"]
    assert item["status"] == "awaiting_reply" and item["pending_interrupt_id"] == out.interrupt_id
    assert item["farm_id"] == "wohleb-farm"  # untouched fields survive updates


@mock_aws
def test_resume_concludes_and_seq_keeps_rising():
    table, sqs, url, inv_id = _run()
    first = inv.run_request(C.StartInvestigation.model_validate(_start_payload()))
    before = [C.InvestigationProgress.model_validate(e["detail"]) for e in _envelopes(sqs, url)]

    # a different process: the resume request carries only ids, so farm and field come from the item
    second = inv.run_request(C.ResumeInvestigation.model_validate(
        _resume_payload(interrupt_id=first.interrupt_id, investigation_id=inv_id)))
    assert second.status == "done" and second.diagnosis["dollars_high"] == 14000

    after = _envelopes(sqs, url)
    concluded = [C.InvestigationConcluded.model_validate(e["detail"]) for e in after if e["detail-type"] == "InvestigationConcluded"]
    assert len(concluded) == 1
    c = concluded[0]
    assert c.field == C.FieldRef(farm_id="wohleb-farm", field_id="north-pivot")
    assert c.condition == "pivot_failure" and c.proposed_action.action_type == "work_order"
    seqs = [b.seq for b in before] + [C.InvestigationProgress.model_validate(e["detail"]).seq
                                       for e in after if e["detail-type"] == "InvestigationProgress"]
    assert seqs == list(range(1, len(seqs) + 1))

    item = table.get_item(Key={"investigation_id": inv_id})["Item"]
    assert item["status"] == "concluded" and "pending_interrupt_id" not in item
    assert json.loads(item["diagnosis_json"])["dollars_low"] == 9000


@mock_aws
def test_failure_becomes_failed_step_and_keeps_interrupt(monkeypatch):
    table, sqs, url, inv_id = _run()

    def boom(*a, **k):
        raise RuntimeError("bedrock throttled")

    monkeypatch.setattr(inv, "reply", boom)
    out = inv.run_request(C.ResumeInvestigation.model_validate(_resume_payload(investigation_id=inv_id)))

    assert out.status == "failed" and out.error.code == "AGENT_ERROR"
    item = table.get_item(Key={"investigation_id": inv_id})["Item"]
    assert item["status"] == "failed" and item["pending_interrupt_id"] == "int-abc123"
    last = _envelopes(sqs, url)[-1]
    assert C.InvestigationProgress.model_validate(last["detail"]).step == "error"


def test_recorder_survives_missing_aws(monkeypatch):
    """No bus, no table: recording logs and carries on."""
    class Dead:
        def __getattr__(self, name):
            raise RuntimeError("no aws here")

    rec = inv.Recorder("inv-x", "farm", "fld", events=Dead(), table=Dead())
    rec.set_status("running")
    rec.progress("supervisor", "plan", "hello")


# ---------- tool backend ----------


def test_gateway_tools_fall_back_when_toolset_missing(monkeypatch):
    monkeypatch.setattr(config, "TOOLS_BACKEND", "gateway")
    import sys
    monkeypatch.setitem(sys.modules, "fieldwatch.toolset", None)  # makes `from . import toolset` raise ImportError
    assert inv._gateway_tools() is None


def test_gateway_tools_come_from_toolset_when_present(monkeypatch):
    import sys
    import types

    calls = []
    fake = types.ModuleType("fieldwatch.toolset")
    fake.tools_for = lambda backend=None, *, access=("read",), names=None: calls.append((backend, access)) or ["t"]
    monkeypatch.setitem(sys.modules, "fieldwatch.toolset", fake)
    monkeypatch.setattr(config, "TOOLS_BACKEND", "gateway")
    import fieldwatch
    monkeypatch.setattr(fieldwatch, "toolset", fake, raising=False)
    assert inv._gateway_tools() == ["t"] and calls == [("gateway", ("read", "write"))]


def test_ssm_bootstrap_sets_missing_env_only(monkeypatch):
    monkeypatch.delenv("FIELDWATCH_SESSIONS_BUCKET", raising=False)
    monkeypatch.setenv("FIELDWATCH_GATEWAY_URL", "keep-me")

    class Ssm:
        def get_parameter(self, Name):
            if Name == "/fieldwatch/bucket":
                raise KeyError("ParameterNotFound")
            return {"Parameter": {"Value": f"value-of{Name}"}}

    loaded = runtime_app.bootstrap_env_from_ssm(Ssm())
    assert loaded == {"FIELDWATCH_SESSIONS_BUCKET": "value-of/fieldwatch/sessions-bucket"}
    import os
    assert os.environ["FIELDWATCH_GATEWAY_URL"] == "keep-me"
    monkeypatch.delenv("FIELDWATCH_SESSIONS_BUCKET", raising=False)
