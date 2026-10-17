"""The diagnosis redesign end to end (docs/research/grounding.md): evidence in code, the naming rule, tiers, stability,
the season gate on the ask path, and facts that come from code. Offline: fake model, no network, no AWS credentials."""
import json

import pytest
from moto import mock_aws

import fieldwatch.agents as agents
import fieldwatch.investigation as inv
from fieldwatch import contracts as C
from fieldwatch import evidence as E
from fieldwatch import farmdata, runfacts, scoring
from fieldwatch import tools as T
from fieldwatch.config import FIXTURES
from fieldwatch.schemas import Diagnosis, scrub_internal_ids
from runtime_support import FakeModel, _results_seen, _text, _tool_use

from test_farmer_chat import FakeNotify, _env, question_request, world  # noqa: F401  (fixtures and helpers)

FEATURES = dict(ring_score=0.0, wedge_score=0.0, edge_band_score=0.0, stripe_score=0.0, lowspot_overlap=0.2,
                sandy_soil_overlap=0.1, hail_swath_overlap=None)


def analysis(acres=12.0, state=None, ndre_p90=None, previous=None, delta=None, where="south-west part of the field",
             **features) -> C.FieldAnalysisResult:
    region = C.AnomalyRegion(region_id="r1", geometry={"type": "Polygon", "coordinates": []}, area_acres=acres,
                             centroid=(-96.54, 42.15), score=3.0, where=where,
                             features=C.PatternFeatures(**{**FEATURES, **features}))
    ind = None
    if ndre_p90 is not None:
        st = C.IndexStats(mean=0.4, p10=0.3, p50=0.4, p90=ndre_p90)
        ind = C.Indices(ndvi=st, ndre=st, ndmi=st)
    return C.FieldAnalysisResult(
        field=C.FieldRef(farm_id="f", field_id="x"), scene_id="s", date="2026-07-20", status="ok", clear_fraction=0.95,
        indices=ind, anomaly=C.AnomalySummary(field_score=3.0, delta_since_last=delta, regions=[region]),
        comparison=C.OverlayComparison(scene_date="2026-07-20", previous_date=previous) if previous else None,
        crop_state=state)


def veg(label):
    return C.CropState(state="vegetative", stage_label=label, diagnosable=True)


def inputs(crop="corn", stage="V8", irrigated=False, rain=(3.0, 3.0), prev=None, soil=None, **kw) -> E.FieldInputs:
    ctx = C.FieldContext(field_id="x", built_at="2026-07-01T00:00:00Z", crop_history={"2025": prev} if prev else {},
                         soil=soil)
    a = kw.pop("analysis", None) or analysis(state=veg(stage))
    return E.FieldInputs(field_id="x", field_name="the east 80", crop=crop, irrigated=irrigated, acres=80.0, analysis=a,
                         context=ctx, weather={"rain_in": rain[0], "normal_rain_in": rain[1]}, stage=stage, **kw)


def tiers(det):
    return {c.condition: c.tier for c in det.candidates}


# ---------- the observed failure ----------


def test_wet_month_soybean_rotation_and_no_field_evidence_is_not_determined_not_nitrogen():
    vec, det = E.determine(inputs(rain=(6.0, 3.0), prev="soybeans"))
    assert det.outcome == "not_determined" and det.condition is None
    assert "nitrogen" not in tiers(det)  # never even "possible" on weather and rotation alone
    present = {(r.condition, r.key) for r in vec.present()}
    assert ("nitrogen", "rain_excess_30d") in present and ("nitrogen", "prior_crop_soybeans") in present
    assert det.detail["nitrogen"]["probability"] < 0.2


def test_nitrogen_is_never_supported_by_regional_weather_alone():
    req = C.ScoreRequest(investigation_id="i", crop="corn", stage="V8", evidence=[
        C.EvidenceItem(evidence_id="e1", hypothesis="nitrogen", direction="supports", weight_key="rain_excess_30d",
                       source="t", value={}, reason="r", created_at="2026-07-20T00:00:00Z"),
        C.EvidenceItem(evidence_id="e2", hypothesis="nitrogen", direction="supports", weight_key="corn_after_corn",
                       source="t", value={}, reason="r", created_at="2026-07-20T00:00:00Z")])
    det = scoring.determine(req)
    assert det.condition != "nitrogen" and "nitrogen" not in tiers(det)


# ---------- nitrogen: possible until the farmer confirms ----------


def n_inputs(stage="V10", **kw):
    return inputs(stage=stage, region_ndre=0.34, analysis=analysis(state=veg(stage), ndre_p90=0.42), **kw)


def test_nitrogen_with_red_edge_evidence_in_v10_is_possible_never_likely():
    vec, det = E.determine(n_inputs())
    assert ("nitrogen", "n_red_edge_si_low") in {(r.condition, r.key) for r in vec.present()}
    assert tiers(det).get("nitrogen") == "possible" and det.outcome != "likely"
    assert det.condition != "nitrogen" or det.outcome == "possible"


def test_nitrogen_named_from_a_photo_without_the_farmer_is_still_only_possible():
    _, det = E.determine(n_inputs(), extra=[C.EvidenceItem(
        evidence_id="p1", hypothesis="nitrogen", direction="supports", weight_key="photo_v_shaped_firing",
        source="photo", value={}, reason="V-shaped yellowing", created_at="2026-07-20T00:00:00Z")])
    assert det.condition == "nitrogen" and det.outcome == "possible" and tiers(det)["nitrogen"] == "possible"


def test_nitrogen_with_the_farmers_n_short_report_can_be_likely():
    _, det = E.determine(n_inputs(farmer=E.FarmerInputs(n_applied="short")))
    assert det.outcome == "likely" and det.condition == "nitrogen"


@pytest.mark.parametrize("stage", ["V4", "V13", "R2 (blister)"])
def test_nitrogen_is_not_named_outside_v6_v12(stage):
    vec, det = E.determine(n_inputs(stage=stage, farmer=E.FarmerInputs(n_applied="short")))
    assert vec.get("nitrogen", "n_red_edge_si_low").state == "not_evaluable"
    assert det.condition != "nitrogen" and det.detail["nitrogen"]["status"] == "ruled_out"
    assert any("V6 to V12" in g for g in det.detail["nitrogen"]["gate"])


def test_nitrogen_needs_a_known_stage_and_is_off_for_soybeans():
    _, det = E.determine(inputs(stage=None, farmer=E.FarmerInputs(n_applied="short"), analysis=analysis(ndre_p90=0.42),
                                region_ndre=0.34))
    assert det.detail["nitrogen"]["status"] == "ruled_out"
    _, det = E.determine(inputs(crop="soybeans", stage="V4", farmer=E.FarmerInputs(n_applied="short")))
    assert det.condition != "nitrogen"


# ---------- other conditions ----------


def test_hail_with_a_field_drop_and_the_stage_in_window_is_named():
    a = analysis(state=veg("V10"), hail_swath_overlap=0.8, previous="2026-07-10", delta=-0.14)
    hail = C.HailReport(field_id="x", start="2026-07-01", end="2026-07-20", max_hail_mm=40.0, pct_area_over_threshold=60,
                        peak_date="2026-07-14")
    vec, det = E.determine(inputs(stage="V10", analysis=a, hail=hail))
    assert vec.get("hail", "sudden_drop_after_storm").state == "present"
    assert det.outcome == "likely" and det.condition == "hail"


def test_hail_is_still_allowed_when_the_season_is_otherwise_over():
    state = C.CropState(state="senescing", diagnosable=False, hail_only=True, reasons=["hail_exception=True"])
    a = analysis(state=state, hail_swath_overlap=0.8)
    hail = C.HailReport(field_id="x", start="2026-08-01", end="2026-08-20", max_hail_mm=40.0, pct_area_over_threshold=60)
    _, det = E.determine(inputs(stage="R5", analysis=a, hail=hail))
    assert det.condition == "hail"
    gone = C.CropState(state="harvested", diagnosable=False)
    _, det = E.determine(inputs(stage="R6", analysis=analysis(state=gone, hail_swath_overlap=0.8), hail=hail))
    assert det.outcome == "not_determined" and all(d["status"] == "ruled_out" for d in det.detail.values())


def test_disease_without_a_photo_is_at_most_possible_and_with_a_photo_it_can_be_named():
    ctx_reports = [C.DiseaseReport(disease="tar spot", county_fips="31053", status="confirmed_this_year")]
    base = inputs(stage="R2 (blister)", rain=(4.5, 3.0), disease_reports=ctx_reports)
    _, det = E.determine(base)
    assert det.condition != "disease_pest" and det.outcome != "likely"
    base.photo = E.PhotoFindings(taken=True, keys=frozenset({"photo_tar_spot_lesions"}))
    _, det = E.determine(base)
    assert det.condition == "disease_pest" and det.outcome == "likely"


def test_drift_is_never_named_from_imagery_alone():
    a = analysis(state=veg("V6"), edge_band_score=0.9)
    _, det = E.determine(inputs(crop="soybeans", stage="V4", analysis=analysis(state=veg("V4"), edge_band_score=0.9)))
    assert det.condition != "herbicide_drift" or det.outcome == "possible"
    _, det = E.determine(inputs(crop="soybeans", stage="V4", analysis=analysis(state=veg("V4"), edge_band_score=0.9),
                                farmer=E.FarmerInputs(neighbor_spray=True)))
    assert det.condition == "herbicide_drift" and det.outcome == "likely"
    assert a.anomaly.regions[0].features.edge_band_score == 0.9


# ---------- caps and clusters ----------


def item(i, hyp, direction, key):
    return C.EvidenceItem(evidence_id=f"e{i}", hypothesis=hyp, direction=direction, weight_key=key, source="t", value={},
                          reason="r", created_at="2026-07-20T00:00:00Z")


def score(*items, crop="corn", stage=None, irrigated=None):
    return scoring.explain(C.ScoreRequest(investigation_id="i", crop=crop, stage=stage, irrigated=irrigated,
                                          evidence=list(items)))


def test_correlated_items_count_once_the_strongest():
    one = score(item(1, "ponding", "supports", "soil_poorly_drained"))["ponding"]["probability"]
    many = score(item(1, "ponding", "supports", "soil_poorly_drained"), item(2, "ponding", "supports", "floodplain_overlap_high"),
                 item(3, "ponding", "supports", "heavy_rain_7d"), item(4, "ponding", "supports", "rain_excess_30d"))
    ign = {i["weight_key"]: i["why"] for i in many["ponding"]["ignored"]}
    assert many["ponding"]["probability"] == pytest.approx(
        score(item(1, "ponding", "supports", "floodplain_overlap_high"))["ponding"]["probability"])
    assert len(ign) == 3 and all("same cluster" in w for w in ign.values()) and one == many["ponding"]["probability"]
    assert [a["weight_key"] for a in many["ponding"]["applied"]] == ["floodplain_overlap_high"]


def test_regional_evidence_is_capped_and_static_evidence_is_capped():
    ex = score(item(1, "drought", "supports", "rain_deficit_30d"), item(2, "drought", "supports", "drought_monitor_d1_plus"),
               item(3, "drought", "supports", "heat_days_over_35c"))["drought"]
    assert ex["class_totals"]["regional"] == scoring.REGIONAL_CAP == 0.7
    assert sum(a["delta_log_odds"] for a in ex["applied"]) == pytest.approx(0.7, abs=0.01) and all(a["capped"] for a in ex["applied"])
    st = score(item(1, "nitrogen", "supports", "corn_after_corn"), item(2, "nitrogen", "supports", "sandy_soil_overlap_high"))
    assert st["nitrogen"]["class_totals"].get("static", 0) <= scoring.STATIC_CAP
    # weather alone can never name a condition: no required field evidence
    assert score(item(1, "drought", "supports", "rain_deficit_30d"))["drought"]["tier"] is None


def test_an_items_direction_must_match_the_catalog():
    ex = score(item(1, "hail", "supports", "no_hail_mesh"))["hail"]
    assert ex["applied"] == [] and "direction does not match" in ex["ignored"][0]["why"]


# ---------- stability ----------


def test_same_inputs_give_identical_evidence_and_outcome():
    runs = [E.determine(n_inputs(farmer=E.FarmerInputs(n_applied="short"))) for _ in range(3)]
    assert len({v.digest() for v, _ in runs}) == 1
    assert len({(d.outcome, d.condition, tuple((c.condition, c.tier, c.probability) for c in d.candidates)) for _, d in runs}) == 1
    assert [e.model_dump() for e in runs[0][0].items()] == [e.model_dump() for e in runs[1][0].items()]
    # and the order the evidence arrives in does not matter
    req = E.score_request(n_inputs(), runs[0][0])
    rev = req.model_copy(update={"evidence": list(reversed(req.evidence))})
    assert scoring.determine(req).detail == scoring.determine(rev).detail


def test_every_key_is_evaluated_and_only_the_data_decides_present_absent_or_not_evaluable():
    vec = E.evaluate(inputs())
    assert len(vec.results) == sum(len(c.weights) for c in scoring.load_catalog().conditions.values())
    assert {r.state for r in vec.results} <= {"present", "absent", "not_evaluable"}
    assert vec.get("hail", "no_hail_mesh").state == "not_evaluable"  # hail was never checked
    assert vec.get("ponding", "soil_poorly_drained").state == "not_evaluable"  # no soil survey
    assert {r.evidence_class for r in vec.results} == {"field", "static", "regional"}


# ---------- the season gate on the ask path ----------


def harvested_row(**kw):
    state = C.CropState(state="harvested", diagnosable=False, reasons=["most of the field dropped below harvest level"], **kw)
    return {"field_id": "north-pivot", "farm_id": "wohleb-farm", "date": "2026-10-08", "status": "ok", "scene_id": "s",
            "regions": [], "crop_state": state.model_dump()}


class Exploding(FakeModel):
    async def stream(self, *a, **kw):
        raise AssertionError("the model must not run for a paused season")
        yield  # pragma: no cover


@mock_aws
def test_on_demand_check_of_a_harvested_field_concludes_without_the_agents(monkeypatch):
    monkeypatch.setattr(agents, "model", lambda: Exploding())
    monkeypatch.setattr(farmdata, "latest_analysis", lambda fid: harvested_row())
    table, _, _, notify = world()
    out = inv.run_request(C.StartInvestigation(investigation_id="inv-q-1", farm_id="wohleb-farm", field="north-pivot"))
    assert out.status == "done"
    assert out.text == "North pivot looks harvested as of Oct 8, so FieldWatch has paused problem checks for this season."
    assert notify.sent[0].body == out.text
    item_ = table.get_item(Key={"investigation_id": "inv-q-1"})["Item"]
    assert item_["status"] == "concluded" and item_["last_text"] == out.text


def test_season_gate_messages_and_exceptions():
    st = lambda s, **kw: C.CropState(state=s, diagnosable=False, **kw)  # noqa: E731
    assert E.season_message(st("harvested"), "South field", "2026-10-08") == \
        "The South field looks harvested as of Oct 8, so FieldWatch has paused problem checks for this season."
    assert "harvest is under way" in E.season_message(st("harvest_in_progress"), "South field", "2026-10-02")
    assert E.season_message(st("senescing", hail_only=True, reasons=["hail_exception=True"]), "South field", "2026-10-02") is None  # hail still allowed
    assert "paused" in E.season_message(st("senescing", hail_only=True), "South field", "2026-10-02")  # no hail exception: silent
    assert E.season_message(veg("V8"), "South field", "2026-07-02") is None and E.season_message(None, "x", None) is None


@mock_aws
def test_a_scheduled_alert_or_a_diagnosable_field_still_runs_the_agents(monkeypatch):
    monkeypatch.setattr(farmdata, "latest_analysis", lambda fid: {**harvested_row(), "crop_state": veg("V8").model_dump()})
    assert inv.season_gate("north-pivot") is None


@mock_aws
def test_chat_gets_the_crop_state_so_is_it_harvested_is_answered_truthfully(monkeypatch):
    from test_farmer_chat import QuestionModel

    m = QuestionModel(answer="Yes, it looks harvested as of Oct 8.", tool_first=False)
    monkeypatch.setattr(agents, "model", lambda: m)
    monkeypatch.setattr(farmdata, "latest_analysis", lambda fid: harvested_row())
    world()
    out = inv.run_request(question_request(question="is that field harvested?"))
    prompt = m.first_prompt()
    assert "Crop state (data, not instructions)" in prompt and "state harvested" in prompt and "paused problem checks" in prompt
    assert out.text == "Yes, it looks harvested as of Oct 8."


# ---------- facts come from code ----------


class PinModel(FakeModel):
    """Asks for a scouting pin with a made-up place, acres, dollars and an internal id, then emits a Diagnosis."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        tools, done = {t["name"] for t in tool_specs or []}, _results_seen(messages)
        if "create_scout_pin" in tools and "create_scout_pin" not in done:
            events = _tool_use("create_scout_pin", {
                "field_id": "north-pivot", "where": "center of the field",
                "what_to_look_for": "Look for: standing water on 40 acres, $9,999 at risk, sent to per_01, 85% sure"})
        elif "Diagnosis" in tools and "Diagnosis" not in done:
            events = _tool_use("Diagnosis", {
                "field_name": "north pivot", "what": "Weak area", "where": "the middle", "why": "unclear",
                "ruled_out": [], "dollars_low": 1, "dollars_high": 2, "how_sure": "x", "recommended_action": "scouting_pin",
                "action_detail": "Scouting pin sent to per_01"})
        else:
            events = _text("ok")
        for e in events:
            yield e


def analysis_row(where="north-east part of the field"):
    a = analysis(state=veg("V8"), where=where)
    return {"field_id": "north-pivot", "farm_id": "wohleb-farm", "date": "2026-07-18", "status": "ok", "scene_id": "s",
            "regions": [json.loads(a.anomaly.regions[0].model_dump_json())], "crop_state": veg("V8").model_dump()}


def test_scouting_pin_location_comes_from_the_region_and_no_internal_ids_reach_the_farmer(tmp_path, monkeypatch):
    from fieldwatch import actions, approvals

    monkeypatch.setattr(agents, "model", lambda: PinModel())
    monkeypatch.setattr(inv, "SESSIONS", tmp_path / "sessions")
    approvals.use_store(approvals.LocalStore(tmp_path))
    pins = []
    monkeypatch.setattr(actions, "default_sender", lambda **kw: pins.append(kw) or {"ok": True, "detail": "x"})
    readers = {"analysis": lambda fid: analysis_row()}
    try:
        events = []
        step = inv.start("inv-pin-1", "north pivot", emit=events.append, readers=readers)
        assert step.status == "waiting_for_farmer"
        assert "north-east part of the field" in step.text and "center of the field" not in step.text
        for bad in ("per_01", "9,999", "40 acres", "85%", "%"):
            assert bad not in step.text, bad
        assert step.facts["where"] == "north-east part of the field"
        done = inv.reply("inv-pin-1", step.interrupt_id, "YES", emit=events.append,
                         facts=runfacts.RunFacts.from_dict(step.facts))
        ended = [e for e in events if e["kind"] == "tool_end" and e["tool"] == "create_scout_pin"][-1]["output"]
        assert "north-east part of the field" in ended and "act-" not in ended and "per_01" not in ended
        assert done.status == "done" and "per_01" not in done.text
        assert done.diagnosis.where == "north-east part of the field" and "per_01" not in done.diagnosis.action_detail
        assert pins and pins[-1]["body"] and "per_01" not in pins[-1]["body"]
    finally:
        approvals.use_store(None)


def test_the_pin_is_the_flagged_regions_centroid_not_the_field_middle(monkeypatch):
    facts = runfacts.RunFacts(centroid=(-96.54, 42.15), where="north-east part of the field")
    f = T._field("north-pivot")
    assert T._pin_of(f) != (-96.54, 42.15)
    with runfacts.use(facts):
        assert T._pin_of(f) == (-96.54, 42.15)


def test_internal_ids_are_scrubbed_but_ordinary_words_stay():
    assert scrub_internal_ids("Scouting pin sent to per_01.") == "Scouting pin sent."
    assert scrub_internal_ids("Check inv-q-9690ce2c and (act-3f2a) per-acre, farm-wide") == "Check and per-acre, farm-wide"


def test_a_diagnosis_text_is_one_sms(monkeypatch):
    d = Diagnosis(field_name="north pivot", what="w" * 200, where="north-east part of the field", why="y" * 200,
                  ruled_out=["hail", "pivot"], dollars_low=0, dollars_high=0, how_sure="not determined yet",
                  recommended_action="scouting_pin", action_detail="x", outcome="not_determined",
                  possible=["possible disease or insects"])
    text = d.to_text()
    assert len(text) <= 320 and text.endswith("Send a scouting pin? YES / NO / MORE")
