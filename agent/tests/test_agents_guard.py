"""WS5: DiagnosisGuard makes every number scorer/impact-derived; fast-path evidence and threshold; acres from the region."""
import copy
import json

import fieldwatch.agents as agents
from fieldwatch import config
from fieldwatch import contracts as C
from fieldwatch import scoring
from fieldwatch.hooks import ToolTimeline
from fieldwatch.schemas import Diagnosis

import test_agents as TA
from test_agents import harness  # noqa: F401  (fixture)

FIXTURES = config.FIXTURES / "contracts"
IMPACT = json.loads((FIXTURES / "impact_estimate.json").read_text())

LLM_NUMBERS = {
    "condition": "hail", "affected_acres": 999.0, "dollars_low": 111, "dollars_high": 222,
    "hypotheses": [{"condition": "hail", "probability": 0.99, "status": "likely"}],
}


def _ring():
    return TA._ring_context()


def _analysis(**features):
    a = json.loads((FIXTURES / "field_analysis_result.json").read_text())
    a["anomaly"]["regions"][0]["features"].update(features)
    return a


def test_guard_overwrites_llm_hypotheses_condition_acres_and_dollars(harness, monkeypatch):  # noqa: F811
    monkeypatch.setitem(TA.DIAGNOSIS, "condition", LLM_NUMBERS["condition"])
    for k, v in LLM_NUMBERS.items():
        monkeypatch.setitem(TA.DIAGNOSIS, k, v)
    diag = harness(TA.FULL_RUN, top=0.86).structured_output
    assert diag.condition == "pivot_failure"
    assert [(h.condition, h.probability, h.status) for h in diag.hypotheses] == [
        ("pivot_failure", 0.86, "likely"), ("drought", 0.03, "ruled_out"), ("hail", 0.02, "ruled_out")]
    assert diag.affected_acres == IMPACT["affected_acres"]
    assert (diag.dollars_low, diag.dollars_high) == (round(IMPACT["low_usd"]), round(IMPACT["high_usd"]))
    assert diag.how_sure == "pretty sure"


def test_guard_clears_dollars_and_acres_when_no_impact_was_estimated(harness, monkeypatch):  # noqa: F811
    for k, v in LLM_NUMBERS.items():
        monkeypatch.setitem(TA.DIAGNOSIS, k, v)
    steps = [s for s in TA.FULL_RUN if s[0] != "estimate_impact"]
    diag = harness(steps, top=0.86).structured_output
    assert (diag.dollars_low, diag.dollars_high, diag.affected_acres) == (0, 0, None)
    assert diag.condition == "pivot_failure" and diag.hypotheses


def test_guard_empties_hypotheses_and_condition_when_nothing_was_scored(harness, monkeypatch):  # noqa: F811
    for k, v in LLM_NUMBERS.items():
        monkeypatch.setitem(TA.DIAGNOSIS, k, v)
    diag = harness([("list_fields", {})]).structured_output
    assert diag.hypotheses == [] and diag.condition is None
    assert (diag.dollars_low, diag.dollars_high, diag.affected_acres) == (0, 0, None)


class _Msg:
    """Minimal event for DiagnosisGuard.after with tool results in the given shape."""

    def __init__(self, diag, messages):
        self.result = type("R", (), {"structured_output": diag})()
        self.agent = type("A", (), {"messages": messages})()


def _diag():
    return Diagnosis(**{**TA.DIAGNOSIS, **LLM_NUMBERS})


def _messages(score_content, impact_content):
    return [
        {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "a", "name": "fieldwatch___score_hypotheses", "input": {}}},
            {"toolUse": {"toolUseId": "b", "name": "fieldwatch___estimate_impact", "input": {}}}]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "a", "content": score_content}},
            {"toolResult": {"toolUseId": "b", "content": impact_content}}]},
    ]


SCORE = {"hypotheses": [{"condition": "ponding", "percent": 71, "status": "likely"}], "top_condition": "ponding",
         "how_sure": "fairly sure"}
IMP = {"low_usd": 1000.4, "high_usd": 2000.6, "affected_acres": 7.5, "basis": "x"}


def test_guard_reads_gateway_json_text_json_blocks_and_double_encoded_text():
    for sc, im in (
        ([{"text": json.dumps(SCORE)}], [{"text": json.dumps(IMP)}]),
        ([{"json": SCORE}], [{"json": IMP}]),
        ([{"text": json.dumps(json.dumps(SCORE))}], [{"text": json.dumps(json.dumps(IMP))}]),
    ):
        d = _diag()
        agents.DiagnosisGuard().after(_Msg(d, _messages(sc, im)))
        assert d.condition == "ponding" and d.hypotheses[0].probability == 0.71
        assert (d.dollars_low, d.dollars_high, d.affected_acres) == (1000, 2001, 7.5)
        assert d.how_sure == "fairly sure"


def test_guard_ignores_unparseable_results():
    d = _diag()
    agents.DiagnosisGuard().after(_Msg(d, _messages([{"text": "not json"}], [{"text": "Error: boom"}])))
    assert d.hypotheses == [] and d.condition is None and (d.dollars_low, d.dollars_high) == (0, 0)


# ---------- acres from the region ----------


def test_estimate_impact_uses_the_top_region_area_not_the_model_acres(harness):  # noqa: F811
    harness(TA.FAST_RUN, top=0.93, context=_ring())  # the script passes 12.1; change it below
    steps = [("list_fields", {}), ("precheck_evidence", {"field_id": "north-pivot"}),
             ("estimate_impact", {"field_id": "north-pivot", "condition": "pivot_failure", "affected_acres": 500})]
    harness(steps, top=0.93, context=_ring())
    assert harness.calls["impact"][-1].affected_acres == 12.1  # fixture region area


def test_estimate_impact_keeps_model_acres_without_an_analysis(harness):  # noqa: F811
    steps = [("list_fields", {}),
             ("estimate_impact", {"field_id": "north-pivot", "condition": "pivot_failure", "affected_acres": 33.0})]
    harness(steps)
    assert harness.calls["impact"][0].affected_acres == 33.0


# ---------- fast path evidence and threshold ----------


def _fast(analysis, **ctx):
    ledger = agents.EvidenceLedger(crop="corn", stage=ctx.pop("stage", None), irrigated=ctx.pop("irrigated", None))
    context = {"analysis": analysis, **ctx}
    pre = agents.build_scoring_tools(ledger, context)[0]
    out = json.loads(pre(field_id="north-pivot"))
    return ledger, out


def test_default_threshold_is_the_documented_one():
    assert agents.FAST_PATH_THRESHOLD == 0.8


def test_fast_path_fires_on_strong_ring_with_normal_rain_and_no_hail_even_without_irrigation_info():
    ledger, out = _fast(_analysis(), open_watches=[], weather={"rain_in": 3.1, "normal_rain_in": 3.0})
    keys = {(e.hypothesis, e.direction, e.weight_key) for e in ledger.evidence}
    assert ("pivot_failure", "supports", "ring_pattern_strong") in keys
    assert ("pivot_failure", "supports", "rain_normal_30d") in keys
    assert ("drought", "refutes", "rain_normal_30d") in keys
    assert ("hail", "refutes", "no_hail_mesh") in keys
    assert ("ponding", "refutes", "lowspot_overlap_none") in keys
    assert out["fast_path"] is True and out["top_condition"] == "pivot_failure"


def test_fast_path_fires_on_strong_ring_when_irrigation_is_known():
    _, out = _fast(_analysis(), irrigated=True, stage="R2 (blister)")
    assert out["fast_path"] is True


def test_fast_path_does_not_fire_on_a_single_weak_pattern():
    weak = {"ring_score": 0.0, "wedge_score": 0.0, "edge_band_score": 0.0, "stripe_score": 0.0,
            "lowspot_overlap": 0.0, "sandy_soil_overlap": 0.0}
    for feat, val in (("lowspot_overlap", 0.7), ("sandy_soil_overlap", 0.7), ("ring_score", 0.4),
                      ("hail_swath_overlap", 0.8), ("edge_band_score", 0.8), ("stripe_score", 0.8)):
        for irrigated in (True, None):
            _, out = _fast(_analysis(**{**weak, feat: val}), irrigated=irrigated, stage="R2 (blister)")
            assert out["fast_path"] is False, (feat, irrigated, out)


def test_fast_path_does_not_fire_on_a_dryland_ring():
    _, out = _fast(_analysis(), irrigated=False)
    assert out["fast_path"] is False


def test_fast_path_does_not_fire_when_an_open_hail_watch_rivals_the_ring():
    _, out = _fast(_analysis(), irrigated=True, open_watches=[{"type": "hail", "detail": {"max_hail_mm": 45}}])
    assert out["fast_path"] is False


def test_evidence_covers_the_features_it_can_derive():
    a = _analysis(wedge_score=0.7, edge_band_score=0.1, stripe_score=0.7, hail_swath_overlap=0.02,
                  ring_score=0.4, lowspot_overlap=0.6, sandy_soil_overlap=0.6)
    keys = {(i.hypothesis, i.direction, i.weight_key) for i in agents.evidence_from_analysis(
        a, [], weather={"rain_in": 1.0, "normal_rain_in": 3.0}, field_acres=14)}
    assert {("pivot_failure", "supports", "ring_pattern_weak"), ("pivot_failure", "supports", "wedge_pattern_strong"),
            ("planter_skip", "supports", "stripe_pattern_strong"), ("ponding", "supports", "lowspot_overlap_high"),
            ("sandy_soil_water_stress", "supports", "sandy_soil_overlap_high"),
            ("herbicide_drift", "refutes", "edge_pattern_none"), ("hail", "refutes", "hail_swath_no_overlap"),
            ("drought", "supports", "rain_deficit_30d"), ("drought", "supports", "stress_uniform_field")} <= keys


def test_every_emitted_weight_key_is_in_the_catalog():
    cat = scoring.load_catalog().conditions
    a = _analysis(wedge_score=0.7, edge_band_score=0.1, stripe_score=0.1, hail_swath_overlap=0.02,
                  lowspot_overlap=0.01, sandy_soil_overlap=0.01)
    for weather in ({"rain_in": 3, "normal_rain_in": 3}, {"rain_in": 1, "normal_rain_in": 3}):
        for i in agents.evidence_from_analysis(a, [{"type": "hail"}], weather=weather, field_acres=14):
            assert i.weight_key in cat[i.hypothesis].weights, (i.hypothesis, i.weight_key)
    for _, _, hyp, key, _ in [*agents.PATTERN_RULES, *agents.ABSENCE_RULES]:
        assert key in cat[hyp].weights, (hyp, key)
