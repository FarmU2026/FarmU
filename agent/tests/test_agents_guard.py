"""WS5: DiagnosisGuard makes every number and fact scorer/impact/code-derived; fast-path evidence and threshold; acres from the region."""
import json

import fieldwatch.agents as agents
from fieldwatch import config, runfacts
from fieldwatch import contracts as C
from fieldwatch import evidence as E
from fieldwatch import scoring
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
    for k, v in LLM_NUMBERS.items():
        monkeypatch.setitem(TA.DIAGNOSIS, k, v)
    diag = harness(TA.FAST_RUN, context=_ring()).structured_output
    assert diag.condition == "pivot_failure" and diag.outcome == "likely"
    top = diag.hypotheses[0]
    assert (top.condition, top.status) == ("pivot_failure", "likely") and 0.8 <= top.probability < 1
    assert not any(h.condition == "hail" and h.probability == 0.99 for h in diag.hypotheses)  # the model's 99% is gone
    assert diag.affected_acres == IMPACT["affected_acres"]
    assert (diag.dollars_low, diag.dollars_high) == (round(IMPACT["low_usd"]), round(IMPACT["high_usd"]))
    assert diag.how_sure == "likely"


def test_guard_clears_dollars_and_acres_when_no_impact_was_estimated(harness, monkeypatch):  # noqa: F811
    for k, v in LLM_NUMBERS.items():
        monkeypatch.setitem(TA.DIAGNOSIS, k, v)
    steps = [s for s in TA.FAST_RUN if s[0] != "estimate_impact"]
    diag = harness(steps, context=_ring()).structured_output
    assert (diag.dollars_low, diag.dollars_high, diag.affected_acres) == (0, 0, None)
    assert diag.condition == "pivot_failure" and diag.hypotheses


def test_guard_empties_hypotheses_and_condition_when_nothing_was_scored(harness, monkeypatch):  # noqa: F811
    for k, v in LLM_NUMBERS.items():
        monkeypatch.setitem(TA.DIAGNOSIS, k, v)
    diag = harness([("list_fields", {})]).structured_output
    assert diag.hypotheses == [] and diag.condition is None and diag.outcome is None
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


SCORE = {"hypotheses": [{"condition": "ponding", "percent": 71, "status": "open"}], "top_condition": "ponding",
         "how_sure": "likely", "outcome": "likely"}
IMP = {"low_usd": 1000.4, "high_usd": 2000.6, "affected_acres": 7.5, "basis": "x"}


def test_guard_reads_gateway_json_text_json_blocks_and_double_encoded_text():
    for sc, im in (
        ([{"text": json.dumps(SCORE)}], [{"text": json.dumps(IMP)}]),
        ([{"json": SCORE}], [{"json": IMP}]),
        ([{"text": json.dumps(json.dumps(SCORE))}], [{"text": json.dumps(json.dumps(IMP))}]),
    ):
        d = _diag()
        agents.DiagnosisGuard().after(_Msg(d, _messages(sc, im)))
        assert d.condition == "ponding" and d.hypotheses[0].probability == 0.71 and d.outcome == "likely"
        assert (d.dollars_low, d.dollars_high, d.affected_acres) == (1000, 2001, 7.5)
        assert d.how_sure == "likely"


def test_guard_ignores_unparseable_results():
    d = _diag()
    agents.DiagnosisGuard().after(_Msg(d, _messages([{"text": "not json"}], [{"text": "Error: boom"}])))
    assert d.hypotheses == [] and d.condition is None and (d.dollars_low, d.dollars_high) == (0, 0)


# ---------- facts in code ----------


def test_guard_sets_the_location_from_the_flagged_region_and_scrubs_ids_and_figures():
    d = _diag()
    d.where = "the north-east corner"
    d.why = "Scouting pin sent to per_01 (pin act-9f2c1). About 40 acres, $9,999 at risk, 85% sure."
    d.what = "Weak area of 33 acres"
    facts = runfacts.RunFacts(field_name="north pivot", where="south-west part of the field")
    with runfacts.use(facts):
        agents.DiagnosisGuard().after(_Msg(d, _messages([{"text": json.dumps(SCORE)}], [{"text": json.dumps(IMP)}])))
    assert d.where == "south-west part of the field"
    for bad in ("per_01", "act-9f2c1", "40 acres", "9,999", "85%", "33 acres"):
        assert bad not in " ".join([d.why, d.what, d.where, d.action_detail]), bad


def test_guard_keeps_the_acres_and_dollars_the_impact_tool_produced():
    d = _diag()
    d.why = "About 8 acres at risk, $1,000-$2,001 on the table."
    agents.DiagnosisGuard().after(_Msg(d, _messages([{"text": json.dumps(SCORE)}], [{"text": json.dumps(IMP)}])))
    assert "8 acres" in d.why and "$1,000-$2,001" in d.why


NOT_DETERMINED = {
    "hypotheses": [{"condition": "disease_pest", "percent": 27, "status": "open", "tier": "possible"}],
    "outcome": "not_determined", "top_condition": None, "how_sure": "not determined yet", "situation": "ok",
    "possible": [{"condition": "disease_pest", "label": "possible disease or insects"}],
    "ruled_out": [{"condition": "Hail damage", "reason": "No hail was seen"}, {"condition": "Pivot not watering", "reason": "x"}],
    "scout_for": ["lesions on the leaves and insects"],
}


def test_guard_writes_the_words_of_a_diagnosis_nobody_could_name():
    d = _diag()
    d.what, d.why = "It is probably nitrogen", "Because it rained"
    facts = runfacts.RunFacts(where="south-west part of the field", region_acres=12.1)
    with runfacts.use(facts):
        agents.DiagnosisGuard().after(_Msg(d, _messages([{"text": json.dumps(NOT_DETERMINED)}], [{"text": json.dumps(IMP)}])))
    assert d.outcome == "not_determined" and d.condition == "unknown" and d.how_sure == "not determined yet"
    assert d.what == "Weak area in the south-west part of the field, cause not determined"
    assert d.ruled_out == ["hail damage", "pivot not watering"] and d.possible == ["possible disease or insects"]
    assert d.recommended_action == "scouting_pin" and d.action_detail.startswith("Walk the spot")
    assert d.dollars_low == d.dollars_high == 0  # no cause, no loss estimate
    text = d.to_text()
    assert "nitrogen" not in text.lower() and "cause not determined" in text and "Ruled out: hail damage" in text
    assert "Possible: possible disease or insects" in text and text.endswith("Send a scouting pin? YES / NO / MORE")
    assert len(text) <= 320 and "%" not in text


def test_guard_says_so_when_there_is_nothing_to_scout():
    d = _diag()
    nothing = {**NOT_DETERMINED, "situation": "no_problem_area", "possible": []}
    agents.DiagnosisGuard().after(_Msg(d, _messages([{"text": json.dumps(nothing)}], [{"text": json.dumps(IMP)}])))
    assert d.recommended_action == "none" and d.what.startswith("No problem area found")
    assert d.possible == [] and "No action needed" in d.to_text()


# ---------- acres from the region ----------


def test_estimate_impact_uses_the_top_region_area_not_the_model_acres(harness):  # noqa: F811
    steps = [("list_fields", {}), ("precheck_evidence", {"field_id": "north-pivot"}),
             ("estimate_impact", {"field_id": "north-pivot", "condition": "pivot_failure", "affected_acres": 500})]
    harness(steps, context=_ring())
    assert harness.calls["impact"][-1].affected_acres == 12.1  # fixture region area


def test_estimate_impact_keeps_model_acres_without_an_analysis(harness):  # noqa: F811
    steps = [("list_fields", {}),
             ("estimate_impact", {"field_id": "north-pivot", "condition": "pivot_failure", "affected_acres": 33.0})]
    harness(steps)
    assert harness.calls["impact"][0].affected_acres == 33.0


def test_estimate_impact_records_the_figures_on_the_runs_facts(harness):  # noqa: F811
    facts = runfacts.RunFacts()
    with runfacts.use(facts):
        harness(TA.FAST_RUN, context=_ring())
    assert (facts.acres, facts.low_usd, facts.high_usd) == (IMPACT["affected_acres"], IMPACT["low_usd"], IMPACT["high_usd"])


# ---------- fast path ----------


def _fast(**ctx):
    context = {**_ring(), **ctx}
    ledger = agents.EvidenceLedger(crop="corn")
    pre = agents.build_scoring_tools(ledger, context)[0]
    return ledger, json.loads(pre(field_id="north-pivot"))


def test_default_threshold_is_the_documented_one():
    assert agents.FAST_PATH_THRESHOLD == 0.8


def test_fast_path_fires_on_strong_ring_with_normal_rain_and_no_hail():
    ledger, out = _fast()
    present = {(r.condition, r.direction, r.key) for r in ledger.vector.present()}
    assert ("pivot_failure", "supports", "ring_pattern_strong") in present
    assert ("pivot_failure", "supports", "rain_normal_30d") in present
    assert ("drought", "refutes", "rain_normal_30d") in present
    assert ("hail", "refutes", "no_hail_mesh") in present
    assert out["fast_path"] is True and out["top_condition"] == "pivot_failure" and out["outcome"] == "likely"
    assert out["how_sure"] == "likely" and "percent" in out["hypotheses"][0]


def test_fast_path_does_not_fire_on_a_single_weak_pattern():
    weak = {"ring_score": 0.0, "wedge_score": 0.0, "edge_band_score": 0.0, "stripe_score": 0.0,
            "lowspot_overlap": 0.0, "sandy_soil_overlap": 0.0}
    for feat, val in (("lowspot_overlap", 0.7), ("sandy_soil_overlap", 0.7), ("ring_score", 0.4),
                      ("hail_swath_overlap", 0.8), ("edge_band_score", 0.8), ("stripe_score", 0.8)):
        for irrigated in (True, None):
            _, out = _fast(analysis=_analysis(**{**weak, feat: val}), irrigated=irrigated)
            assert out["fast_path"] is False, (feat, irrigated, out["outcome"], out["hypotheses"][:2])


def test_fast_path_does_not_fire_on_a_dryland_ring():
    _, out = _fast(irrigated=False)
    assert out["fast_path"] is False and out["top_condition"] != "pivot_failure"


def test_fast_path_does_not_fire_when_an_open_hail_watch_rivals_the_ring():
    _, out = _fast(open_watches=[{"type": "hail", "detail": {"max_hail_mm": 45}}])
    assert out["fast_path"] is False


def test_every_catalog_key_is_evaluated_each_run_and_only_catalog_keys():
    cat = scoring.load_catalog().conditions
    vec = E.evaluate(E.FieldInputs(analysis=C.FieldAnalysisResult.model_validate(_analysis())))
    assert {(r.condition, r.key) for r in vec.results} == {(c, k) for c, v in cat.items() for k in v.weights}
    assert set(E.RESOLVERS) == {k for v in cat.values() for k in v.weights}
