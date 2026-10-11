"""WS5 part B: supervisor + specialists reasoning. Scripted model, no network, no Bedrock."""
import dataclasses
import json
import re
import uuid

import pytest
from strands.models.model import Model

import fieldwatch.agents as agents
from fieldwatch import config
from fieldwatch import contracts as C
from fieldwatch import registry
from fieldwatch.hooks import ToolTimeline
from fieldwatch.schemas import Diagnosis, how_sure_phrase, scrub_percentages

FIXTURES = config.FIXTURES / "contracts"
NOW = "2026-07-18T17:21:05Z"


def _events(tool_name, payload):
    tid = f"tu-{uuid.uuid4().hex[:8]}"
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tid, "name": tool_name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}},
    ]


def _text_events(text):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}},
    ]


def ev(eid, hyp, direction, key, source, value, reason):
    return {"evidence_id": eid, "hypothesis": hyp, "direction": direction, "weight_key": key, "source": source,
            "value": value, "reason": reason, "created_at": "2000-01-01T00:00:00Z"}


RESPONSES = {
    "imagery": {"agent": "imagery", "notes": "Ring on the outer edge.", "tool_calls": 99, "evidence": [
        ev("x", "pivot_failure", "supports", "ring_pattern_strong", "get_scene_analysis", {"ring_score": 0.82},
           "The weak area is a ring on the outer edge.")]},
    "environment": {"agent": "environment", "notes": "Rain normal.", "tool_calls": 0, "evidence": [
        ev("x", "drought", "refutes", "rain_normal_30d", "get_weather", {"rain_in": 3.1}, "Rain was normal."),
        ev("x", "hail", "refutes", "no_hail_mesh", "get_weather", {"hail_reported": False}, "No hail.")]},
    "agronomy": {"agent": "agronomy", "notes": "No disease reports.", "tool_calls": 0, "evidence": [
        ev("x", "disease_pest", "refutes", "no_disease_reports_county", "get_disease_reports", {}, "No disease nearby.")]},
}
SPECIALIST_TOOL_STEP = {"imagery": ("get_scene_analysis", {"field_id": "north-pivot"}),
                        "environment": ("get_weather", {"field_id": "north-pivot"}), "agronomy": None}

DIAGNOSIS = {
    "field_name": "north pivot",
    "what": "The crop in a ring on the outer edge is stressed, down about 23% from normal.",
    "where": "outer ring, last tower",
    "why": "Rain was normal and the soil is uniform, so a nozzle likely is not watering. We are 95% sure.",
    "ruled_out": ["drought", "hail"],
    "dollars_low": 1,
    "dollars_high": 2,
    "how_sure": "not sure yet",
    "recommended_action": "none",
    "action_detail": "Check the nozzles.",
}


class ScriptedModel(Model):
    """Plays a fixed tool sequence per agent role, then emits that role's structured output."""

    def __init__(self, supervisor_steps, log):
        self.supervisor_steps, self.log = supervisor_steps, log

    def update_config(self, **kw):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kw):  # pragma: no cover
        yield {"output": output_model()}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        names = {t["name"] for t in (tool_specs or [])}
        turn = sum(1 for m in messages for c in m.get("content", []) if "toolResult" in c)
        if "SpecialistResponse" in names:
            role = re.search(r"You are the (\w+) specialist", system_prompt).group(1)
            self.log.append(role)
            step = SPECIALIST_TOOL_STEP[role]
            assert not names & {"create_work_order", "create_scout_pin", "request_photo"}, "specialists are read-only"
            events = _events(*step) if step and turn == 0 else _events("SpecialistResponse", RESPONSES[role])
        elif "Diagnosis" in names:
            self.log.append("supervisor")
            steps = self.supervisor_steps
            events = _events(*steps[turn]) if turn < len(steps) else _events("Diagnosis", DIAGNOSIS)
        else:
            events = _text_events("ok")
        for e in events:
            yield e


FULL_RUN = [
    ("list_fields", {}),
    ("precheck_evidence", {"field_id": "north-pivot"}),
    ("ask_imagery", {"question": "What does the pattern look like?", "field_id": "north-pivot"}),
    ("ask_environment", {"question": "Does weather explain it?", "field_id": "north-pivot"}),
    ("ask_agronomy", {"question": "Any disease reports?", "field_id": "north-pivot"}),
    ("score_hypotheses", {"crop": "corn", "stage": "R2 (blister)", "irrigated": True}),
    ("estimate_impact", {"field_id": "north-pivot", "condition": "pivot_failure", "affected_acres": 12.1}),
]
FAST_RUN = [
    ("list_fields", {}),
    ("precheck_evidence", {"field_id": "north-pivot"}),
    ("estimate_impact", {"field_id": "north-pivot", "condition": "pivot_failure", "affected_acres": 12.1}),
]


def _score(top: float):
    return C.ScoreResult(
        hypotheses=[C.Hypothesis(condition="pivot_failure", probability=top, status="likely" if top > 0.6 else "open"),
                    C.Hypothesis(condition="drought", probability=0.03, status="ruled_out"),
                    C.Hypothesis(condition="hail", probability=0.02, status="ruled_out")],
        catalog_version="test")


@pytest.fixture
def harness(monkeypatch):
    """Scripted model + recording scorer/impact in the registry. Returns a runner."""
    calls = {"score": [], "impact": [], "roles": []}

    def run(steps, top=0.86, context=None):
        monkeypatch.setattr(agents, "model", lambda: ScriptedModel(steps, calls["roles"]))

        def scorer(**req):
            calls["score"].append(C.ScoreRequest.model_validate(req))
            return _score(top)

        def impact(**req):
            calls["impact_extra"] = {k: req.pop(k) for k in ("crop", "expected_yield", "price") if k in req}
            calls["impact"].append(C.ImpactRequest.model_validate(req))
            return C.ImpactEstimate.model_validate_json((FIXTURES / "impact_estimate.json").read_text())

        for name, fn in (("score_hypotheses", scorer), ("estimate_impact", impact)):
            monkeypatch.setitem(registry.BY_NAME, name, dataclasses.replace(registry.BY_NAME[name], fn=fn))
        sup = agents.build_supervisor(ToolTimeline(lambda e: None), context=context)
        return sup("Investigate the field called 'north pivot'.", structured_output_model=Diagnosis)

    run.calls = calls
    return run


def _ring_context():
    analysis = json.loads((FIXTURES / "field_analysis_result.json").read_text())
    return {"analysis": analysis, "crop": "corn", "stage": "R2 (blister)", "irrigated": True}


# ---------- specialists ----------


def test_specialist_tool_lists_are_read_only_and_known():
    for name, tool_names in agents.SPECIALIST_TOOLS.items():
        for t in tool_names:
            spec = registry.BY_NAME.get(t)
            assert (spec and spec.access == "read") or t == "get_scene_analysis", (name, t)


def test_specialist_returns_valid_specialist_response(monkeypatch):
    log = []
    monkeypatch.setattr(agents, "model", lambda: ScriptedModel([], log))
    ledger = agents.EvidenceLedger()
    ask_imagery, ask_environment, ask_agronomy = agents.build_specialists(ToolTimeline(lambda e: None), ledger)
    out = {}
    for t, who in ((ask_imagery, "imagery"), (ask_environment, "environment"), (ask_agronomy, "agronomy")):
        out[who] = C.SpecialistResponse.model_validate_json(t(question="Look at it.", field_id="north-pivot"))
        assert out[who].agent == who
    assert out["imagery"].tool_calls == 1 and out["agronomy"].tool_calls == 0  # real counts, not the model's
    ids = [e.evidence_id for r in out.values() for e in r.evidence]
    assert len(ids) == 4 and len(set(ids)) == 4
    assert all(e.created_at != "2000-01-01T00:00:00Z" for r in out.values() for e in r.evidence)
    assert len(ledger.evidence) == 4 and ledger.called == ["imagery", "environment", "agronomy"]


def test_registry_wrapper_exposes_stub_tools_with_their_schema():
    t = agents._strands_tool(registry.BY_NAME["get_hail"])
    assert t.tool_name == "get_hail"
    assert t.tool_spec["inputSchema"]["json"]["required"] == ["field_id", "start", "end"]
    out = t(field_id="north-pivot", start="2026-07-01", end="2026-07-18")
    assert C.HailReport.model_validate(out)


def test_ledger_does_not_double_count_the_same_fact():
    ledger = agents.EvidenceLedger()
    item = C.EvidenceItem(**ev("a", "hail", "refutes", "no_hail_mesh", "get_hail", {}, "No hail."))
    assert len(ledger.add([item, item.model_copy(update={"evidence_id": "b"})])) == 1


# ---------- supervisor ----------


def test_supervisor_scores_the_union_of_specialist_evidence(harness):
    result = harness(FULL_RUN)
    (req,) = harness.calls["score"]
    assert sorted(e.hypothesis for e in req.evidence) == ["disease_pest", "drought", "hail", "pivot_failure"]
    assert req.crop == "corn" and req.stage == "R2 (blister)" and req.irrigated is True
    assert harness.calls["roles"].count("imagery") >= 1
    assert {"imagery", "environment", "agronomy"} <= set(harness.calls["roles"])
    assert harness.calls["impact"][0].condition == "pivot_failure"
    assert harness.calls["impact_extra"] == {"crop": "corn"}
    assert isinstance(result.structured_output, Diagnosis)


def test_supervisor_has_no_self_made_numbers(harness):
    diag = harness(FULL_RUN, top=0.86).structured_output
    # scorer said 86%, so "pretty sure"; the model's own "95%" and "23%" are removed, its how_sure is replaced
    assert diag.how_sure == "pretty sure"
    text = " ".join([diag.what, diag.where, diag.why, diag.action_detail])
    assert "95" not in text and "23" not in text and "%" not in text
    assert (diag.dollars_low, diag.dollars_high) == (round(json.loads((FIXTURES / "impact_estimate.json").read_text())["low_usd"]),
                                                     round(json.loads((FIXTURES / "impact_estimate.json").read_text())["high_usd"]))


def test_supervisor_says_not_sure_when_scorer_is_unsure(harness):
    assert harness(FULL_RUN, top=0.4).structured_output.how_sure == "not sure yet"


def test_supervisor_tools_never_include_specialist_only_or_unknown_writes():
    sup_tools = {t.tool_name for t in agents.build_scoring_tools(agents.EvidenceLedger())}
    assert sup_tools == {"precheck_evidence", "score_hypotheses", "estimate_impact"}


# ---------- fast path (R1) ----------


def test_fast_path_skips_specialists_when_precomputed_evidence_is_decisive(harness):
    result = harness(FAST_RUN, top=0.93, context=_ring_context())
    assert not {"imagery", "environment", "agronomy"} & set(harness.calls["roles"])
    (req,) = harness.calls["score"]
    assert [e.weight_key for e in req.evidence if e.direction == "supports"] == ["ring_pattern_strong"]
    assert all(e.direction == "refutes" for e in req.evidence if e.weight_key != "ring_pattern_strong")
    assert harness.calls["impact"] and isinstance(result.structured_output, Diagnosis)


def test_specialists_run_when_the_first_score_is_not_decisive(harness):
    harness(FULL_RUN, top=0.7, context=_ring_context())
    assert {"imagery", "environment", "agronomy"} <= set(harness.calls["roles"])
    assert len(harness.calls["score"]) == 2  # precheck, then the full score
    # ring evidence deduplicated with imagery's: ring + 3 specialist refutes + the pattern-absence refutes from precheck
    ring = [e for e in harness.calls["score"][1].evidence if e.weight_key == "ring_pattern_strong"]
    assert len(ring) == 1


def test_specialist_tools_refuse_once_the_fast_path_is_decisive(monkeypatch):
    log = []
    monkeypatch.setattr(agents, "model", lambda: ScriptedModel([], log))
    ledger = agents.EvidenceLedger(fast_path=True)
    ask_imagery, *_ = agents.build_specialists(ToolTimeline(lambda e: None), ledger)
    assert "skipped" in json.loads(ask_imagery(question="q"))
    assert log == [] and ledger.evidence == []


def test_no_fast_path_without_precomputed_analysis(harness):
    harness(FULL_RUN, top=0.99)
    assert "imagery" in harness.calls["roles"]  # nothing precomputed: specialists run


# ---------- pure helpers ----------


def test_evidence_from_analysis_and_hail_watch():
    analysis = json.loads((FIXTURES / "field_analysis_result.json").read_text())
    items = agents.evidence_from_analysis(analysis, [{"type": "hail", "detail": {"max_hail_mm": 40}}])
    supports = [(i.hypothesis, i.weight_key) for i in items if i.direction == "supports"]
    assert supports == [("pivot_failure", "ring_pattern_strong"), ("hail", "hail_swath_overlap_high")]
    analysis["status"] = "too_cloudy"
    assert agents.evidence_from_analysis(analysis) == []


def test_how_sure_phrase_thresholds():
    assert [how_sure_phrase(p) for p in (0.95, 0.8, 0.6, 0.3)] == [
        "pretty sure", "pretty sure", "fairly sure", "not sure yet"]


def test_scrub_percentages_keeps_only_allowed():
    assert scrub_percentages("86% sure, roughly 40% worse, 12 acres", {86}) == "86% sure, worse, 12 acres"
    assert scrub_percentages("no numbers here", set()) == "no numbers here"


def test_supervisor_prompt_follows_farmer_text_rules():
    for banned in ("NDVI is", "z-score is"):
        assert banned not in agents.SUPERVISOR_PROMPT
    assert "You never make up how sure we are" in agents.SUPERVISOR_PROMPT


def test_every_weight_key_we_emit_is_in_the_scorer_vocabulary():
    vocab = {k for keys in agents.WEIGHT_KEYS.values() for k in keys}
    assert {r[3] for r in agents.PATTERN_RULES} <= vocab
    assert {e["weight_key"] for r in RESPONSES.values() for e in r["evidence"]} <= vocab
