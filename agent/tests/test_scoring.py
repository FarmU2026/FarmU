"""Scorer, condition catalog and impact (workstream 5). No network."""
import json
import typing

import pytest

from fieldwatch import contracts as C
from fieldwatch import registry, scoring
from fieldwatch.config import FIXTURES

ALL_CONDITIONS = set(typing.get_args(C.ConditionId))


def sample_request() -> C.ScoreRequest:
    return C.ScoreRequest.model_validate(json.loads((FIXTURES / "contracts" / "score_request.json").read_text()))


def ev(i, cond, direction, key, **kw):
    return C.EvidenceItem(evidence_id=f"e{i}", hypothesis=cond, direction=direction, weight_key=key,
                          source="test", value={}, reason="r", created_at="2026-07-18T00:00:00Z", **kw)


def by_cond(res):
    return {h.condition: h for h in res.hypotheses}


# ---------- catalog ----------

def test_catalog_validates_and_is_cached():
    cat = scoring.load_catalog()
    assert cat is scoring.load_catalog()
    assert "DRAFT" in cat.status and "Caleb" in cat.status
    assert set(cat.conditions) == ALL_CONDITIONS


def test_every_weight_has_reason_and_known_source():
    cat = scoring.load_catalog()
    for cid, cond in cat.conditions.items():
        assert 0 < cond.prior < 1
        for key, w in cond.weights.items():
            assert w.weight > 0 and w.reason, (cid, key)
            assert w.source in cat.sources, (cid, key, w.source)


def test_sample_weight_keys_exist_in_catalog():
    cat = scoring.load_catalog()
    for e in sample_request().evidence:
        assert e.weight_key in cat.conditions[e.hypothesis].weights


def test_every_condition_has_loss_rows():
    lt = scoring.load_loss_tables()
    for cid in ALL_CONDITIONS:
        rows = [r for r in lt.rows if r.condition == cid]
        assert rows, cid
        assert any(r.crop == "any" and r.stage_group == "any" for r in rows), cid
        for r in rows:
            for sev in ("low", "medium", "high"):
                lo, hi = getattr(r.loss, sev)
                assert 0 <= lo <= hi <= 1
            assert r.source in lt.sources


def test_loss_severity_is_monotonic():
    for r in scoring.load_loss_tables().rows:
        assert r.loss.low[1] <= r.loss.high[1] and r.loss.low[0] <= r.loss.high[0]


# ---------- scoring ----------

def test_sample_request_ranks_sensibly():
    res = scoring.score_hypotheses(sample_request())
    assert isinstance(res, C.ScoreResult)
    h = by_cond(res)
    assert res.hypotheses[0].condition == "pivot_failure"
    assert h["pivot_failure"].status == "likely"
    for cond in ("drought", "hail", "sandy_soil_water_stress"):
        assert h[cond].status == "ruled_out"
    assert res.catalog_version == scoring.load_catalog().version


def test_thresholds():
    # large refuting weight pushes below 0.05; large supporting above 0.6
    low = scoring.score_hypotheses(C.ScoreRequest(investigation_id="i", crop="corn", evidence=[
        ev(1, "hail", "refutes", "no_hail_mesh")]))
    assert by_cond(low)["hail"].probability < 0.05 and by_cond(low)["hail"].status == "ruled_out"
    high = scoring.score_hypotheses(C.ScoreRequest(investigation_id="i", crop="corn", evidence=[
        ev(1, "hail", "supports", "hail_mesh_large"), ev(2, "hail", "supports", "hail_swath_overlap_high")]))
    # "likely" is the outcome of the naming rule, not a probability band: here hail has a field-evidence item
    # (radar over the field) and is inside its stage window only when the stage is unknown or in V6-R5.
    assert by_cond(high)["hail"].probability > scoring.NAME_THRESHOLD and by_cond(high)["hail"].status == "likely"
    assert scoring.status_for(0.05) == "open" and scoring.status_for(0.0499) == "ruled_out"


def test_no_evidence_returns_priors():
    req = C.ScoreRequest(investigation_id="i", crop="corn", stage="V8", irrigated=True, evidence=[])
    res = scoring.score_hypotheses(req)
    assert res.hypotheses
    for h in res.hypotheses:
        assert h.probability == pytest.approx(scoring.prior_probability(h.condition, "corn", "V8", True), abs=1e-4)
        assert h.status in ("open", "ruled_out", "likely")
    assert all(h.status == "open" for h in res.hypotheses)


def test_pivot_failure_dropped_when_not_irrigated():
    res = scoring.score_hypotheses(C.ScoreRequest(investigation_id="i", crop="corn", irrigated=False, evidence=[]))
    assert "pivot_failure" not in by_cond(res)
    assert scoring.prior_probability("pivot_failure", "corn", None, False) < 0.01


def test_unknown_evidence_ignored():
    base = scoring.score_hypotheses(sample_request())
    req = sample_request()
    req.evidence.append(ev(99, "hail", "supports", "totally_made_up_key"))
    res = scoring.score_hypotheses(req)
    assert res.hypotheses == base.hypotheses
    exp = scoring.explain(req)
    assert any(x["weight_key"] == "totally_made_up_key" for x in exp["hail"]["ignored"])


def test_duplicate_evidence_counted_once():
    one = C.ScoreRequest(investigation_id="i", crop="corn", evidence=[ev(1, "hail", "supports", "hail_mesh_large")])
    two = C.ScoreRequest(investigation_id="i", crop="corn", evidence=[
        ev(1, "hail", "supports", "hail_mesh_large"), ev(2, "hail", "supports", "hail_mesh_large")])
    assert scoring.score_hypotheses(one) == scoring.score_hypotheses(two)


def test_supports_and_refutes_are_opposite():
    sup = by_cond(scoring.score_hypotheses(C.ScoreRequest(investigation_id="i", crop="corn", evidence=[
        ev(1, "drought", "supports", "rain_deficit_30d")])))["drought"].probability
    ref = by_cond(scoring.score_hypotheses(C.ScoreRequest(investigation_id="i", crop="corn", evidence=[
        ev(1, "drought", "refutes", "rain_normal_30d")])))["drought"].probability
    prior = scoring.prior_probability("drought", "corn", None, None)
    assert ref < prior < sup


def test_deterministic_and_order_independent():
    req = sample_request()
    a = scoring.score_hypotheses(req)
    b = scoring.score_hypotheses(req)
    rev = req.model_copy(update={"evidence": list(reversed(req.evidence))})
    assert a == b == scoring.score_hypotheses(rev)
    probs = [h.probability for h in a.hypotheses]
    assert probs == sorted(probs, reverse=True)


def test_explanation_lists_movers():
    exp = scoring.explain(sample_request())
    pivot = exp["pivot_failure"]
    assert pivot["applied"][0]["evidence_id"] == "ev1" and pivot["applied"][0]["delta_log_odds"] > 0
    assert exp["drought"]["applied"][0]["delta_log_odds"] < 0
    assert 0 < pivot["probability"] < 1


def test_stage_group():
    g = scoring.stage_group
    assert g(None) is None
    assert g("R2 (blister)") == "flowering" and g("V8") == "vegetative" and g("VT") == "flowering"
    assert g("V3") == "early" and g("R4") == "grain_fill" and g("R6 (physiological maturity)") == "maturity"
    assert g("silking") == "flowering" and g("???") is None


def test_stage_no_longer_nudges_the_prior_it_gates_the_name():
    # memo 5.3: stage-dependent priors are hard windows, not small log-odds bonuses
    for cid in ALL_CONDITIONS:
        assert scoring.load_catalog().conditions[cid].stage_adjust == {}
    assert scoring.prior_probability("planter_skip", "corn", "V2", None) == scoring.prior_probability("planter_skip", "corn", "R4", None)
    assert scoring.window_check("planter_skip", "corn", scoring.stage_span("V2"))[0]
    assert not scoring.window_check("planter_skip", "corn", scoring.stage_span("R4"))[0]


# ---------- impact ----------

def impact(**kw):
    base = dict(field_id="north-pivot", condition="pivot_failure", affected_acres=12.1, stage="R2 (blister)",
                severity="high", days_until_fixed=7)
    base.update(kw)
    return scoring.estimate_impact(C.ImpactRequest(**base))


def test_impact_range_and_basis():
    est = impact()
    assert isinstance(est, C.ImpactEstimate)
    assert 0 < est.low_usd <= est.high_usd
    assert "bu" in est.basis and "12.1" in est.basis and "R2" in est.basis


def test_impact_scales_with_acres_days_and_severity():
    a, b = impact(affected_acres=10), impact(affected_acres=20)
    assert b.low_usd == pytest.approx(2 * a.low_usd, rel=0.01) and b.high_usd == pytest.approx(2 * a.high_usd, rel=0.01)
    assert impact(days_until_fixed=14).high_usd > impact(days_until_fixed=3).high_usd
    assert impact(severity="low").high_usd < impact(severity="medium").high_usd < impact(severity="high").high_usd


def test_impact_one_time_conditions_ignore_days():
    h3 = impact(condition="hail", days_until_fixed=3)
    h30 = impact(condition="hail", days_until_fixed=30)
    assert h3 == h30


def test_impact_uses_crop_price_yield_overrides():
    base = impact()
    soy = scoring.estimate_impact(C.ImpactRequest(field_id="f", condition="pivot_failure", affected_acres=12.1,
                                                  stage="R2", severity="high"), crop="soybeans")
    assert soy.high_usd != base.high_usd
    bumped = scoring.estimate_impact(C.ImpactRequest(field_id="f", condition="pivot_failure", affected_acres=12.1,
                                                     stage="R2 (blister)", severity="high"), price=8.40)
    assert bumped.high_usd == pytest.approx(2 * base.high_usd, rel=0.01)


@pytest.mark.parametrize("cond", sorted(ALL_CONDITIONS))
@pytest.mark.parametrize("crop", ["corn", "soybeans", "wheat", "alfalfa", "other"])
def test_impact_every_condition_and_crop(cond, crop):
    est = scoring.estimate_impact(C.ImpactRequest(field_id="f", condition=cond, affected_acres=5, stage=None), crop=crop)
    assert 0 <= est.low_usd <= est.high_usd


def test_impact_zero_acres():
    est = impact(affected_acres=0)
    assert est.low_usd == 0 and est.high_usd == 0


# ---------- registry ----------

def test_registry_uses_real_scorer_and_impact():
    for name, model in (("score_hypotheses", C.ScoreResult), ("estimate_impact", C.ImpactEstimate)):
        spec = registry.BY_NAME[name]
        assert spec.stub is False
    args = registry.BY_NAME["score_hypotheses"].input_model.model_validate(
        json.loads((FIXTURES / "contracts" / "score_request.json").read_text()))
    out = registry.BY_NAME["score_hypotheses"].fn(**args.model_dump())
    assert isinstance(out, C.ScoreResult) and out.hypotheses[0].condition == "pivot_failure"
    iargs = registry.BY_NAME["estimate_impact"].input_model.model_validate(
        json.loads((FIXTURES / "contracts" / "impact_request.json").read_text()))
    assert isinstance(registry.BY_NAME["estimate_impact"].fn(**iargs.model_dump()), C.ImpactEstimate)
