"""The agent justifies its conclusion to the farmer (Oct 11: the first live text only said "Ask for a leaf photo ...
YES / NO", and asking "why?" afterwards got no reasons because none were kept).

- the scoring tools return `why`: the evidence reasons that moved the score (scoring.explain), and what lost out;
- the YES / NO text leads with the likely cause, how sure, the top reasons, acres and dollars;
- the step carries the explanation so the Recorder stores it and a later question run can explain it."""
import json
from types import SimpleNamespace

from fieldwatch import agents
from fieldwatch import contracts as C
from fieldwatch import investigation as inv
from fieldwatch.hooks import ToolTimeline


def _call(tool_id, name, payload):
    return [{"role": "assistant", "content": [{"toolUse": {"toolUseId": tool_id, "name": name, "input": {}}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": tool_id,
                                                          "content": [{"text": json.dumps(payload)}]}}]}]


def _agent(*calls):
    return SimpleNamespace(messages=[m for c in calls for m in c])


WHY = {"top_condition": "ponding", "for": ["The bare area sits in the field's lowest ground",
                                           "About 4 inches of rain fell in the week before"],
       "against": [], "ruled_out": [{"condition": "Pivot not watering", "reason": "No ring or wedge shape"}]}
SCORE = {"top_condition": "ponding", "outcome": "likely", "how_sure": "likely", "why": WHY,
         "hypotheses": [{"condition": "ponding", "percent": 64}, {"condition": "planter_skip", "percent": 21}]}
NOT_DETERMINED = {"top_condition": None, "outcome": "not_determined", "how_sure": "not determined yet", "why": {},
                  "ruled_out": [{"condition": "Hail damage", "reason": "No hail"}, {"condition": "Pivot not watering", "reason": "x"}],
                  "possible": [{"condition": "disease_pest", "label": "possible disease or insects"}],
                  "hypotheses": [{"condition": "disease_pest", "percent": 31}]}
IMPACT = {"affected_acres": 13.4, "low_usd": 3100.4, "high_usd": 7900.6, "basis": "..."}


def _interrupted(summary):
    it = SimpleNamespace(id="int-1", reason={"summary": summary})
    return SimpleNamespace(stop_reason="interrupt", interrupts=[it])


def test_text_leads_with_finding_and_reasons_then_the_action():
    agent = _agent(_call("t1", "score_hypotheses", SCORE), _call("t2", "estimate_impact", IMPACT))
    step = inv._to_step(_interrupted("Scouting pin on the South field (west side): look for standing water"),
                        ToolTimeline(lambda e: None), agent=agent)
    assert step.status == "waiting_for_farmer"
    assert step.text.startswith("Likely standing water / waterlogging. Why: The bare area sits in the "
                                "field's lowest ground; About 4 inches of rain fell in the week before.")
    assert "About 13 acres, $3,100-$7,901 at risk." in step.text
    assert step.text.endswith("look for standing water? YES / NO")
    assert "64" not in step.text and len(step.text) <= 320  # percents stay on the record; one SMS long
    assert step.explanation["why"] == WHY and step.explanation["impact"]["acres"] == 13.4


def test_gateway_prefixed_tool_names_count():
    expl = inv.explanation(_agent(_call("t1", "fieldwatch-tools___score_hypotheses", SCORE)))
    assert inv._finding(expl).startswith("Likely standing water")


def test_without_a_score_the_text_is_unchanged():
    step = inv._to_step(_interrupted("Work order: check the end tower"), ToolTimeline(lambda e: None),
                        agent=_agent())
    assert step.text == "Work order: check the end tower YES / NO" and step.explanation is None


def test_not_determined_text_says_what_was_ruled_out_and_what_is_possible():
    agent = _agent(_call("t1", "score_hypotheses", NOT_DETERMINED))
    step = inv._to_step(_interrupted("Scouting pin on the South field (west side): look for lesions"),
                        ToolTimeline(lambda e: None), agent=agent)
    assert step.text.startswith("Cause not determined. Ruled out: hail damage, pivot not watering. Possible: possible disease")
    assert step.text.endswith("look for lesions? YES / NO") and "%" not in step.text and len(step.text) <= 320


def test_possible_text_is_not_worded_as_likely():
    possible = {**SCORE, "outcome": "possible", "top_condition": "nitrogen",
                "possible": [{"condition": "nitrogen", "label": "possible nitrogen variation (please confirm)"}]}
    expl = inv.explanation(_agent(_call("t1", "score_hypotheses", possible)))
    assert inv._finding(expl).startswith("Possible nitrogen variation (please confirm).") and "Likely" not in inv._finding(expl)


def test_long_findings_give_way_before_the_question_does():
    long = {**SCORE, "why": {**WHY, "for": ["x" * 140, "y" * 140]}}
    agent = _agent(_call("t1", "score_hypotheses", long), _call("t2", "estimate_impact", IMPACT))
    step = inv._to_step(_interrupted("Scouting pin on the South field (west side): look for standing water"),
                        ToolTimeline(lambda e: None), agent=agent)
    assert len(step.text) <= 320 and step.text.endswith("look for standing water? YES / NO")


def test_unknown_top_condition_adds_nothing():
    expl = inv.explanation(_agent(_call("t1", "score_hypotheses", {**SCORE, "top_condition": "unknown"})))
    assert inv._finding(expl) == ""


def _ev(i, hyp, direction, key, reason):
    return C.EvidenceItem(evidence_id=f"e{i}", hypothesis=hyp, direction=direction, weight_key=key, source="test",
                          value={}, reason=reason, created_at="2026-07-18T00:00:00Z")


def test_score_reasons_come_from_the_evidence_that_moved_the_score():
    ledger = agents.EvidenceLedger(investigation_id="inv-1", crop="corn")
    ledger.add([_ev(1, "ponding", "supports", "photo_standing_water", "The photo shows water in the lowest ground")])
    agents.score_ledger(ledger)
    why = agents.score_reasons(ledger)
    assert why["top_condition"] == "ponding" and why["for"] == ["The photo shows water in the lowest ground"]
    assert agents.score_reasons(agents.EvidenceLedger(investigation_id="inv-2", crop="corn")) == {}
