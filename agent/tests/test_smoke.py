"""Exercises the whole loop with a scripted fake model, no AWS needed:
start -> supervisor calls create_work_order -> interrupt pauses -> session saved ->
reply("YES") resumes in a fresh agent -> work order sent -> Diagnosis emitted.

Run:  python -m pytest tests -q
"""
import json
import uuid

import pytest
from strands.models.model import Model

import fieldwatch.agents as agents
import fieldwatch.investigation as inv
from fieldwatch.investigation import reply, start


def _tool_use(name: str, payload: dict):
    tid = f"tu-{uuid.uuid4().hex[:8]}"
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tid, "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}},
    ]


def _text(text: str):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}},
    ]


def _results_seen(messages) -> set[str]:
    """Names of tools whose result is already in the conversation."""
    names = {}
    seen = set()
    for m in messages:
        for c in m.get("content", []):
            if "toolUse" in c:
                names[c["toolUse"]["toolUseId"]] = c["toolUse"]["name"]
            if "toolResult" in c:
                seen.add(names.get(c["toolResult"]["toolUseId"], "?"))
    return seen


DIAGNOSIS = {
    "field_name": "north pivot",
    "what": "The crop in a ring on the outer edge is stressed.",
    "where": "outer ring, last tower",
    "why": "Rain has been normal and the soil is uniform, so a nozzle or tower likely isn't watering.",
    "ruled_out": ["drought", "hail", "sandy soil"],
    "dollars_low": 9000,
    "dollars_high": 14000,
    "how_sure": "pretty sure",
    "recommended_action": "repair_work_order",
    "action_detail": "Check the nozzles and the end gun on the last tower.",
}


class FakeModel(Model):
    """Rule-based stand-in for Bedrock."""

    def update_config(self, **kw):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kw):  # pragma: no cover
        yield {"output": output_model()}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        tools = {t["name"] for t in (tool_specs or [])}
        done = _results_seen(messages)
        if "create_work_order" in tools and "create_work_order" not in done:
            events = _tool_use(
                "create_work_order",
                {"field_id": "north-pivot", "assignee": "Brother", "what_to_check": "nozzles on the last tower"},
            )
        elif "Diagnosis" in tools and "Diagnosis" not in done:
            events = _tool_use("Diagnosis", DIAGNOSIS)
        else:
            events = _text("ok")
        for e in events:
            yield e


@pytest.fixture(autouse=True)
def fake_model(monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "model", lambda: FakeModel())
    monkeypatch.setattr(inv, "SESSIONS", tmp_path / "sessions")


def test_interrupt_then_resume_produces_diagnosis():
    inv_id = uuid.uuid4().hex[:8]
    events = []
    step = start(inv_id, "north pivot", emit=events.append)

    assert step.status == "waiting_for_farmer"
    assert "repair ticket" in step.text and step.text.endswith("YES / NO")
    assert step.interrupt_id
    assert any(e["kind"] == "tool_start" and e["tool"] == "create_work_order" for e in events)

    step2 = reply(inv_id, step.interrupt_id, "YES", emit=events.append)

    assert step2.status == "done", step2
    assert step2.diagnosis is not None
    assert step2.diagnosis.dollars_high == 14000
    assert step2.text.startswith("north pivot:") and "Repair ticket sent." in step2.text
    assert len(step2.text) < 320
    sent = [e for e in events if e["kind"] == "tool_end" and e["tool"] == "create_work_order"]
    assert sent and "Work order sent" in sent[-1]["output"]


def test_decline_does_not_send():
    inv_id = uuid.uuid4().hex[:8]
    events = []
    step = start(inv_id, "north pivot", emit=events.append)
    step2 = reply(inv_id, step.interrupt_id, "no", emit=events.append)
    sent = [e for e in events if e["kind"] == "tool_end" and e["tool"] == "create_work_order"]
    assert sent and "declined" in sent[-1]["output"].lower()
    assert step2.status == "done"
