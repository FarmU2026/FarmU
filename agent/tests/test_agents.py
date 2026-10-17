"""WS5 part B: supervisor + specialists reasoning. Scripted model, no network, no Bedrock.

The evidence is computed by code (fieldwatch/evidence.py) and scored by the real scorer: nothing here fakes a
probability. The scripted model only plays the tool sequence of each role, then emits that role's structured output."""
import dataclasses
import json
import re
import uuid

import pytest
from strands.models.model import Model

import fieldwatch.agents as agents
from fieldwatch import config
from fieldwatch import contracts as C
from fieldwatch import evidence as E
from fieldwatch import registry, scoring
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


# What the specialists say. Only the photo item is theirs to give; the ring and the hail refute are facts the code
# owns, and the ledger drops them.
RESPONSES = {
    "imagery": {"agent": "imagery", "notes": "Ring on the outer edge.", "tool_calls": 99, "evidence": [
        ev("x", "pivot_failure", "supports", "ring_pattern_strong", "get_scene_analysis", {"ring_score": 0.82},
           "The weak area is a ring on the outer edge.")]},
    "environment": {"agent": "environment", "notes": "Rain normal.", "tool_calls": 0, "evidence": [
        ev("x", "hail", "refutes", "no_hail_mesh", "get_weather", {"hail_reported": False}, "No hail.")]},
    "agronomy": {"agent": "agronomy", "notes": "No disease reports.", "tool_calls": 0, "evidence": [
        ev("x", "disease_pest", "refutes", "photo_no_lesions", "photo", {"photo": "clean leaves"}, "The leaves in the photo are clean.")]},
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
    ("score_hypotheses", {}),
    ("estimate_impact", {"field_id": "north-pivot", "condition": "pivot_failure", "affected_acres": 12.1}),
]
FAST_RUN = [
    ("list_fields", {}),
    ("precheck_evidence", {"field_id": "north-pivot"}),
    ("estimate_impact", {"field_id": "north-pivot", "condition": "pivot_failure", "affected_acres": 12.1}),
]


@pytest.fixture
def harness(monkeypatch):
    """Scripted model; the registry's impact tool is recorded, the scorer is the real one. Returns a runner."""
    calls = {"impact": [], "roles": []}

    def run(steps, context=None):
        monkeypatch.setattr(agents, "model", lambda: ScriptedModel(steps, calls["roles"]))

        def impact(**req):
            calls["impact_extra"] = {k: req.pop(k) for k in ("crop", "expected_yield", "price") if k in req}
            calls["impact"].append(C.ImpactRequest.model_validate(req))
            return C.ImpactEstimate.model_validate_json((FIXTURES / "impact_estimate.json").read_text())

        monkeypatch.setitem(registry.BY_NAME, "estimate_impact",
                            dataclasses.replace(registry.BY_NAME["estimate_impact"], fn=impact))
        sup = agents.build_supervisor(ToolTimeline(lambda e: None), context=context)
        run.agent = sup
        return sup("Investigate the field called 'north pivot'.", structured_output_model=Diagnosis)

    run.calls = calls
    return run


def _ring_context():
    analysis = json.loads((FIXTURES / "field_analysis_result.json").read_text())
    return {"analysis": analysis, "crop": "corn", "stage": "R2 (blister)", "irrigated": True,
            "open_watches": [], "weather": {"rain_in": 3.1, "normal_rain_in": 3.0}, "field_acres": 130}


def _weak_context():
    """A faint ring: something is there, but not enough to name."""
    ctx = _ring_context()
    ctx["analysis"]["anomaly"]["regions"][0]["features"].update(ring_score=0.4)
    return ctx


def _score_payloads(agent):
    return agents._tool_payloads(agent.messages, "score_hypotheses")


# ---------- specialists ----------


def test_specialist_tool_lists_are_read_only_and_known():
    for name, tool_names in agents.SPECIALIST_TOOLS.items():
        for t in tool_names:
            spec = registry.BY_NAME.get(t)
            assert (spec and spec.access == "read") or t == "get_scene_analysis", (name, t)


def test_specialists_may_only_add_photo_and_farmer_evidence(monkeypatch):
    log = []
    monkeypatch.setattr(agents, "model", lambda: ScriptedModel([], log))
    ledger = agents.EvidenceLedger()
    ask_imagery, ask_environment, ask_agronomy = agents.build_specialists(ToolTimeline(lambda e: None), ledger)
    out = {}
    for t, who in ((ask_imagery, "imagery"), (ask_environment, "environment"), (ask_agronomy, "agronomy")):
        out[who] = json.loads(t(question="Look at it.", field_id="north-pivot"))
        assert out[who]["agent"] == who
    assert out["imagery"]["tool_calls"] == 1 and out["agronomy"]["tool_calls"] == 0  # real counts, not the model's
    # the ring and the hail refute are code's facts: dropped. The photo finding is kept, as a fixed field.
    assert out["imagery"]["evidence"] == [] and out["imagery"]["dropped"] == 1
    assert out["environment"]["evidence"] == [] and out["environment"]["dropped"] == 1
    assert [e.weight_key for e in ledger.evidence] == ["photo_no_lesions"]
    kept = ledger.evidence[0]
    assert kept.source == "photo" and kept.created_at != "2000-01-01T00:00:00Z" and kept.evidence_id.startswith("agr-")
    assert sorted(ledger.rejected) == ["hail.no_hail_mesh", "pivot_failure.ring_pattern_strong"]
    assert ledger.called == ["imagery", "environment", "agronomy"]


def test_registry_wrapper_exposes_stub_tools_with_their_schema():
    t = agents._strands_tool(registry.BY_NAME["get_hail"])
    assert t.tool_name == "get_hail"
    assert t.tool_spec["inputSchema"]["json"]["required"] == ["field_id", "start", "end"]
    out = t(field_id="north-pivot", start="2026-07-01", end="2026-07-18")
    assert C.HailReport.model_validate(out)


def _item(i, hyp, direction, key):
    return C.EvidenceItem(**ev(f"a{i}", hyp, direction, key, "photo", {}, "r"))


def test_ledger_does_not_double_count_the_same_fact():
    ledger = agents.EvidenceLedger()
    item = _item(1, "disease_pest", "supports", "photo_tar_spot_lesions")
    assert len(ledger.add([item, item.model_copy(update={"evidence_id": "b"})])) == 1


def test_ledger_rejects_code_keys_and_flipped_directions():
    ledger = agents.EvidenceLedger()
    added = ledger.add([_item(1, "hail", "refutes", "no_hail_mesh"),  # code computes hail
                        _item(2, "disease_pest", "refutes", "photo_tar_spot_lesions"),  # the catalog says supports
                        _item(3, "nitrogen", "supports", "farmer_reports_n_short")])
    assert [e.weight_key for e in added] == ["farmer_reports_n_short"]
    assert added[0].source == "farmer_reply" and len(ledger.rejected) == 2


# ---------- supervisor ----------


def test_supervisor_scores_the_codes_evidence_plus_the_specialists_photo_findings(harness):
    result = harness(FULL_RUN, context=_weak_context())
    assert isinstance(result.structured_output, Diagnosis)
    assert {"imagery", "environment", "agronomy"} <= set(harness.calls["roles"])  # a faint ring is not decisive
    final = _score_payloads(harness.agent)[-1]
    assert final["outcome"] in ("possible", "not_determined") and final["top_condition"] != "pivot_failure"
    assert final["evidence_count"] >= 7  # the code's present keys, plus the specialists' photo finding
    assert harness.calls["impact"][0].condition == "pivot_failure"
    assert harness.calls["impact_extra"] == {"crop": "corn"}


def test_supervisor_has_no_self_made_numbers(harness):
    diag = harness(FAST_RUN, context=_ring_context()).structured_output
    # the scorer named the ring "likely"; the model's own "95%" and "23%" are removed and its how_sure is replaced
    assert diag.outcome == "likely" and diag.how_sure == "likely" and diag.condition == "pivot_failure"
    text = " ".join([diag.what, diag.where, diag.why, diag.action_detail])
    assert "95" not in text and "23" not in text and "%" not in text
    impact = json.loads((FIXTURES / "impact_estimate.json").read_text())
    assert (diag.dollars_low, diag.dollars_high) == (round(impact["low_usd"]), round(impact["high_usd"]))
    assert "%" not in diag.to_text()


def test_supervisor_says_not_determined_when_the_evidence_does_not_name_a_cause(harness):
    diag = harness(FULL_RUN, context=_weak_context()).structured_output
    assert diag.outcome in ("possible", "not_determined") and diag.how_sure != "likely"
    assert diag.condition != "pivot_failure"


def test_supervisor_tools_never_include_specialist_only_or_unknown_writes():
    sup_tools = {t.tool_name for t in agents.build_scoring_tools(agents.EvidenceLedger())}
    assert sup_tools == {"precheck_evidence", "score_hypotheses", "estimate_impact"}


# ---------- fast path (R1) ----------


def test_fast_path_skips_specialists_when_the_codes_evidence_is_decisive(harness):
    result = harness(FAST_RUN, context=_ring_context())
    assert not {"imagery", "environment", "agronomy"} & set(harness.calls["roles"])
    assert harness.calls["impact"] and isinstance(result.structured_output, Diagnosis)


def test_specialists_run_when_the_first_score_is_not_decisive(harness):
    harness(FULL_RUN, context=_weak_context())
    assert {"imagery", "environment", "agronomy"} <= set(harness.calls["roles"])
    assert len(_score_payloads(harness.agent)) == 1  # the precheck is not a score_hypotheses call; this is the full one


def test_specialist_tools_refuse_once_the_fast_path_is_decisive(monkeypatch):
    log = []
    monkeypatch.setattr(agents, "model", lambda: ScriptedModel([], log))
    ledger = agents.EvidenceLedger(fast_path=True)
    ask_imagery, *_ = agents.build_specialists(ToolTimeline(lambda e: None), ledger)
    assert "skipped" in json.loads(ask_imagery(question="q"))
    assert log == [] and ledger.evidence == []


def test_no_fast_path_without_the_fields_inputs(harness):
    harness(FULL_RUN)
    assert "imagery" in harness.calls["roles"]  # nothing precomputed: specialists run


# ---------- pure helpers ----------


def test_top_region_is_the_strongest_usable_region():
    analysis = json.loads((FIXTURES / "field_analysis_result.json").read_text())
    assert agents.top_region(analysis).region_id == "r1"
    analysis["status"] = "too_cloudy"
    assert agents.top_region(analysis) is None


def test_how_sure_phrase_is_a_tier_never_a_number():
    assert [how_sure_phrase(t) for t in ("likely", "possible", "not_determined")] == [
        "likely", "possible, not confirmed", "not determined yet"]
    assert [how_sure_phrase(p) for p in (0.95, 0.66, 0.4, 0.1)] == [
        "likely", "likely", "possible, not confirmed", "not determined yet"]
    assert how_sure_phrase(None) == "not determined yet"


def test_scrub_percentages_keeps_only_allowed():
    assert scrub_percentages("86% sure, roughly 40% worse, 12 acres", {86}) == "86% sure, worse, 12 acres"
    assert scrub_percentages("no numbers here", set()) == "no numbers here"


def test_supervisor_prompt_follows_farmer_text_rules():
    for banned in ("NDVI is", "z-score is"):
        assert banned not in agents.SUPERVISOR_PROMPT
    assert "You never make up how sure we are" in agents.SUPERVISOR_PROMPT
    assert "NEVER guess a cause" in agents.SUPERVISOR_PROMPT


def test_the_llm_vocabulary_is_only_photo_and_farmer_keys():
    cat = scoring.load_catalog().conditions
    for cid, keys in agents.WEIGHT_KEYS.items():
        for k in keys:
            assert cat[cid].weights[k].source in ("photo", "farmer"), (cid, k)
    assert "ring_pattern_strong" not in {k for ks in agents.WEIGHT_KEYS.values() for k in ks}
    assert "photo_tar_spot_lesions" in agents.WEIGHT_KEY_TEXT and "rain_normal_30d" not in agents.WEIGHT_KEY_TEXT
