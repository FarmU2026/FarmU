"""Crop-state machine: GDD math, stage mapping, precedence, harvest and hail detection on synthetic stacks, planting
dates. Offline: synthetic arrays and a fake loader, no network."""
from datetime import date, timedelta

import numpy as np
import pytest
from scipy import ndimage

from fieldwatch import contracts as C
from fieldwatch.analysis import analyze, harvest, phenology, planting_defaults
from fieldwatch.analysis import gates as G
from fieldwatch.analysis.core import analyze_stacks
from fieldwatch.analysis.grid import Grid, IndexStack
from fieldwatch.analysis.phenology import Planting, StateEvidence, classify_state
from fieldwatch.analysis.writer import NullWriter
from cloud.lambdas.scenes import decide

N = 100
GRID = Grid(shape=(N, N), res_m=10.0, origin=(-96.54, 42.16))
YY, XX = np.mgrid[0:N, 0:N]


def _tex(seed, amp):
    f = ndimage.gaussian_filter(np.random.default_rng(seed).normal(size=(N, N)), 6)
    return f / f.std() * amp


TEX = _tex(1, 0.02)


def stack(level, d, sid=None, seed=0, noise=0.01):
    nd = (level + TEX + np.random.default_rng(seed + d.toordinal()).normal(0, noise, (N, N))).astype("float32")
    ones = np.ones((N, N), bool)
    return IndexStack(scene_id=sid or f"s{d}", date=d, grid=GRID, ndvi=nd, ndre=nd * 0.5, ndmi=nd * 0.45,
                      valid=ones.copy(), inside=ones.copy(), clear_fraction=1.0)


def with_patch(st, patch, drop):
    st.ndvi = np.where(patch, st.ndvi - drop, st.ndvi).astype("float32")
    return st


def history(year_dates, level):
    return [stack(level, date(y, m, d), sid=f"h{y}", seed=100 + y) for y, m, d in year_dates]


def planting(gdd, source="state_default", when=date(2026, 5, 5)):
    return Planting(date=when, source=source, gdd=gdd)


def result_of(core, d="2026-07-18"):
    """Wrap a CoreResult the way the analyzer does, so decide.evaluate can run on it."""
    ref = C.OverlayRef(key="k", bounds=(0, 0, 1, 1))
    return C.FieldAnalysisResult(field=C.FieldRef(farm_id="f", field_id="x"), scene_id="s", date=d, status=core.status,
                                 clear_fraction=1.0, indices=core.indices, anomaly=core.anomaly, gates=core.gates,
                                 overlays=C.Overlays(true_color=ref, health=ref), crop_state=core.crop_state)


def emits(core):
    return decide.evaluate(result_of(core), 2.0, 1.2).emit


# ---------- GDD math and stages ----------


def test_day_gdd_applies_the_86_50_caps():
    assert phenology.day_gdd(60, 80) == pytest.approx(20.0)
    assert phenology.day_gdd(40, 95) == pytest.approx(18.0)      # low raised to 50, high capped at 86: (86+50)/2-50
    assert phenology.day_gdd(70, 100) == pytest.approx(28.0)     # cap on the high only
    assert phenology.day_gdd(30, 45) == 0.0                      # cold day adds nothing
    assert phenology.day_gdd(90, 99) == pytest.approx(36.0)      # both capped at 86


def test_gdd_between_sums_inclusive_and_skips_missing_days():
    days = [(date(2026, 5, 1) + timedelta(days=i), 60.0, 80.0) for i in range(10)]
    days[3] = (days[3][0], None, None)
    assert phenology.gdd_between(days, date(2026, 5, 1), date(2026, 5, 10)) == pytest.approx(180.0)
    assert phenology.gdd_between(days, date(2026, 5, 1), date(2026, 5, 3)) == pytest.approx(60.0)
    assert phenology.gdd_between(days, date(2027, 1, 1), date(2027, 1, 5)) is None


@pytest.mark.parametrize("gdd,state,label", [
    (50, "vegetative", "VE (est.)"), (475, "vegetative", "V6 (est.)"), (870, "vegetative", "V12 (est.)"),
    (1135, "reproductive", "VT (est.)"), (1700, "reproductive", "R2 (est.)"), (2700, "reproductive", "R6 (est.)")])
def test_corn_stage_mapping(gdd, state, label):
    assert phenology.stage_for("corn", gdd) == (state, label)


def test_soybean_stage_mapping():
    assert phenology.stage_for("soybeans", 400)[0] == "vegetative"
    assert phenology.stage_for("soybeans", 1000) == ("reproductive", "R2 (est.)")


def test_green_up_needs_a_bracketing_pair_and_backs_out_planting():
    curve = [(date(2026, 5, 1), 0.12), (date(2026, 5, 15), 0.20), (date(2026, 5, 29), 0.50)]
    g = phenology.green_up_date(curve)
    assert date(2026, 5, 19) <= g <= date(2026, 5, 21)
    assert phenology.green_up_date([(date(2026, 5, 20), 0.5), (date(2026, 5, 30), 0.7)]) is None
    assert phenology.green_up_date([(date(2026, 4, 1), 0.1), (date(2026, 6, 1), 0.6)]) is None  # gap too wide
    temps = [(date(2026, 4, 1) + timedelta(days=i), 55.0, 77.0) for i in range(120)]  # 16 GDD a day
    planted = phenology.planting_from_green_up(curve, temps, "corn")
    assert planted == g - timedelta(days=phenology.GREEN_UP_LAG_DAYS + 6)  # 110 GDD = 7 days at 16/day, counted inclusively


# ---------- precedence ----------


def ev(**kw):
    base = dict(crop="corn", as_of=date(2026, 7, 18), p50=0.85, peak=0.86)
    base.update(kw)
    return StateEvidence(**base)


def test_precedence_harvest_beats_senescence_beats_stage():
    h = harvest.HarvestFinding(low_share=0.95, dropped_share=0.0, acres=0, blocks=0)
    assert classify_state(ev(as_of=date(2026, 10, 8), gdd=2800, legacy_senescence=True, harvest=h)).state == "harvested"
    part = harvest.HarvestFinding(low_share=0.5, dropped_share=0.5, acres=20, blocks=1)
    s = classify_state(ev(as_of=date(2026, 10, 8), gdd=2800, legacy_senescence=True, harvest=part))
    assert s.state == "harvest_in_progress" and not s.diagnosable and "harvest passes detected on 2026-10-08" in s.reasons[0]
    assert classify_state(ev(gdd=2400)).state == "senescing"            # 89% of maturity
    assert classify_state(ev(legacy_senescence=True, gdd=1500)).state == "senescing"
    assert classify_state(ev(gdd=1500)).state == "reproductive"
    assert classify_state(ev(gdd=900)).state == "vegetative"


def test_pre_emergence_by_gdd_or_bare_soil():
    s = classify_state(ev(as_of=date(2026, 5, 3), gdd=40, p50=0.15, peak=0.15))
    assert s.state == "pre_emergence" and not s.diagnosable
    assert classify_state(ev(as_of=date(2026, 5, 3), gdd=None, p50=0.15, peak=0.15)).state == "pre_emergence"
    # imagery wins when it is already green
    assert classify_state(ev(as_of=date(2026, 5, 20), gdd=40, p50=0.6)).state == "vegetative"
    # a low median in July is a failed stand, not "not emerged": stay diagnosable as before
    assert classify_state(ev(gdd=None, p50=0.15, peak=0.8)).diagnosable


def test_unknown_mid_season_stays_diagnosable_and_late_without_gdd_is_hail_only():
    s = classify_state(ev())
    assert s.state == "unknown" and s.diagnosable and s.planting_date_source == "unknown"
    late = classify_state(ev(as_of=date(2026, 8, 28)))
    assert late.state == "reproductive" and late.hail_only and not late.diagnosable


def test_late_window_uses_gdd_and_vetoes_a_late_planted_calendar():
    assert phenology.late_window("corn", date(2026, 8, 25))
    assert not phenology.late_window("corn", date(2026, 8, 25), gdd=1200)      # planted late: 44% of maturity
    assert phenology.late_window("corn", date(2026, 7, 25), gdd=2100)          # hot year, early
    assert not phenology.late_window("alfalfa", date(2026, 9, 25))


def test_senescence_decline_is_ignored_when_a_sudden_drop_explains_it():
    drop = harvest.SuddenDrop(mask=np.zeros((2, 2), bool), acres=5, median_drop=-0.3, previous_date="2026-08-22")
    e = dict(as_of=date(2026, 8, 28), p50=0.78, peak=0.88, gdd=2050)
    assert classify_state(ev(**e)).state == "senescing"
    s = classify_state(ev(sudden_drop=drop, **e))
    assert s.state == "reproductive" and s.hail_only and phenology.HAIL_EXCEPTION in s.reasons


def test_earlier_harvest_is_sticky():
    earlier = [(date(2026, 8, 20), 0.88), (date(2026, 10, 5), 0.33)]
    s = classify_state(ev(as_of=date(2026, 11, 2), p50=0.5, peak=0.88, earlier=earlier, legacy_senescence=True))
    assert s.state == "harvested" and "2026-10-05" in s.reasons[0]


def test_hail_token_matches_decide():
    assert decide.HAIL_EXCEPTION == phenology.HAIL_EXCEPTION


def test_fold_state_only_opens_gates():
    assert G.fold_state(False, False, "harvest_in_progress") == (True, True)
    assert G.fold_state(True, False, "vegetative") == (True, False)
    assert G.fold_state(False, False, "pre_emergence") == (False, False)


# ---------- harvest on synthetic stacks ----------

HIST_OCT = history([(2025, 10, 6), (2024, 10, 4), (2023, 10, 5)], 0.5)
PEAK_PASSES = [stack(0.86, date(2026, 8, 5)), stack(0.84, date(2026, 8, 25)), stack(0.70, date(2026, 9, 20))]


def late_run(current, previous, season=None, hist=HIST_OCT, plant=None):
    return analyze_stacks(current, hist, previous, crop="corn", season=season or PEAK_PASSES, planting=plant or planting(2800))


def test_full_harvest_is_harvested_and_not_diagnosable():
    prev = stack(0.62, date(2026, 10, 1))
    res = late_run(stack(0.33, date(2026, 10, 8)), prev)
    cs = res.crop_state
    assert cs.state == "harvested" and not cs.diagnosable and res.gates.harvest and res.gates.senescence
    assert not emits(res)


@pytest.mark.parametrize("when,prev_when", [(date(2026, 10, 2), date(2026, 9, 27)), (date(2026, 10, 15), date(2026, 10, 8))])
def test_partial_harvest_with_a_straight_edge_is_harvest_in_progress(when, prev_when):
    """The observed failure: half the field drops ~0.4 between passes, the median stays high."""
    half = XX >= 50
    prev = stack(0.62, prev_when)
    cur = with_patch(stack(0.62, when), half, 0.40)
    res = late_run(cur, prev)
    cs = res.crop_state
    assert cs.state == "harvest_in_progress" and not cs.diagnosable
    assert res.indices.ndvi.p50 > 0.35  # whole-field median alone would not have caught it
    assert any("harvest passes detected" in r for r in cs.reasons)
    assert not emits(res)


def test_a_block_cut_a_pass_ago_is_still_seen_through_the_season_reference():
    """Pass 1 already had the east third cut; on pass 2 only a new strip drops. The reference is the season's best pixel."""
    cut = XX >= 66
    prev = with_patch(stack(0.62, date(2026, 10, 8)), cut, 0.40)
    cur = with_patch(stack(0.62, date(2026, 10, 15)), XX >= 50, 0.40)
    res = late_run(cur, prev)
    assert res.crop_state.state == "harvest_in_progress"


def test_round_blob_is_not_harvest_but_a_straight_block_is():
    blob = np.hypot(YY - 50, XX - 50) < 14
    straight = (YY > 30) & (YY < 60) & (XX > 20) & (XX < 70)
    a = stack(0.7, date(2026, 10, 1))
    assert harvest.edge_straightness(straight)[0] > 0.9 and harvest.edge_straightness(straight)[1] > 0.9
    assert harvest.edge_straightness(blob)[1] < harvest.HARVEST_DOMINANT_EDGES
    cur = with_patch(stack(0.7, date(2026, 10, 8)), blob, 0.5)
    f = harvest.detect_harvest(cur, a.ndvi, late=True)
    assert f is None or f.blocks == 0


def test_a_diagonal_straight_edge_counts():
    diag = (XX + YY) > 100
    cur = with_patch(stack(0.7, date(2026, 10, 8)), diag, 0.5)
    f = harvest.detect_harvest(cur, stack(0.7, date(2026, 10, 1)).ndvi, late=True)
    assert f is not None and f.blocks == 1


def test_no_harvest_detection_before_the_late_window():
    cur = with_patch(stack(0.8, date(2026, 7, 18)), XX >= 50, 0.5)
    assert harvest.detect_harvest(cur, stack(0.8, date(2026, 7, 13)).ndvi, late=False) is None


# ---------- mid-season problem still detected, late hail exception ----------

HIST_JUL = history([(2025, 7, 17), (2024, 7, 16), (2023, 7, 18)], 0.82)
PATCH = np.hypot(YY - 30, XX - 60) < 8


def test_mid_july_problem_patch_stays_diagnosable_and_emits():
    cur = with_patch(stack(0.82, date(2026, 7, 18)), PATCH, 0.25)
    prev = stack(0.82, date(2026, 7, 13))
    res = analyze_stacks(cur, HIST_JUL, prev, crop="corn", planting=planting(1500), season=[stack(0.5, date(2026, 6, 10))])
    cs = res.crop_state
    assert cs.state == "reproductive" and cs.diagnosable and not cs.hail_only and cs.gdd_since_planting == 1500
    assert cs.planting_date == "2026-05-05" and cs.planting_date_source == "state_default"
    assert res.anomaly.regions and emits(res)


def test_unknown_state_mid_season_is_unchanged():
    cur = with_patch(stack(0.82, date(2026, 7, 18)), PATCH, 0.25)
    res = analyze_stacks(cur, HIST_JUL, stack(0.82, date(2026, 7, 13)), crop="corn")
    assert res.crop_state.state == "unknown" and res.crop_state.diagnosable and emits(res)


HIST_AUG = history([(2025, 8, 26), (2024, 8, 27), (2023, 8, 25)], 0.84)


def aug_pair(prev_patch=False):
    prev = stack(0.84, date(2026, 8, 22))
    if prev_patch:
        prev = with_patch(prev, PATCH, 0.3)
    return with_patch(stack(0.84, date(2026, 8, 28)), PATCH, 0.30), prev


def test_late_august_hail_like_drop_on_a_clean_scene_emits_with_hail_only():
    cur, prev = aug_pair()
    res = analyze_stacks(cur, HIST_AUG, prev, crop="corn", planting=planting(2050), season=[stack(0.86, date(2026, 8, 5))])
    cs = res.crop_state
    assert cs.state == "reproductive" and cs.hail_only and not cs.diagnosable
    assert phenology.HAIL_EXCEPTION in cs.reasons
    assert res.anomaly.regions and emits(res)
    assert not res.gates.senescence


def test_late_august_without_the_drop_or_with_an_unclean_previous_does_not_emit():
    clean = analyze_stacks(stack(0.84, date(2026, 8, 28)), HIST_AUG, stack(0.84, date(2026, 8, 22)), crop="corn",
                           planting=planting(2050))
    assert clean.crop_state.hail_only and phenology.HAIL_EXCEPTION not in clean.crop_state.reasons and not emits(clean)
    cur, prev = aug_pair(prev_patch=True)  # the patch was already weak on the previous pass: not new
    res = analyze_stacks(cur, HIST_AUG, prev, crop="corn", planting=planting(2050))
    assert phenology.HAIL_EXCEPTION not in res.crop_state.reasons and not emits(res)


def test_same_drop_once_senescence_has_started_is_silent():
    hist = history([(2025, 9, 12), (2024, 9, 11), (2023, 9, 13)], 0.8)
    cur = with_patch(stack(0.8, date(2026, 9, 12)), PATCH, 0.3)
    res = analyze_stacks(cur, hist, stack(0.8, date(2026, 9, 6)), crop="corn", planting=planting(2500))
    assert res.crop_state.state == "senescing" and not emits(res)


def test_pre_emergence_is_not_diagnosable():
    cur = stack(0.15, date(2026, 5, 3))
    hist = history([(2025, 5, 2), (2024, 5, 3), (2023, 5, 4)], 0.15)
    res = analyze_stacks(cur, hist, None, crop="corn", planting=planting(40))
    assert res.crop_state.state == "pre_emergence" and not emits(res)


# ---------- decide ----------


def _res(**cs):
    ref = C.OverlayRef(key="k", bounds=(0, 0, 1, 1))
    region = C.AnomalyRegion(region_id="r1", geometry={"type": "Polygon", "coordinates": []}, area_acres=5, centroid=(0, 0),
                             score=4.0, features=C.PatternFeatures(ring_score=0, wedge_score=0, edge_band_score=0,
                                                                   stripe_score=0, lowspot_overlap=0, sandy_soil_overlap=0))
    return C.FieldAnalysisResult(
        field=C.FieldRef(farm_id="f", field_id="x"), scene_id="s", date="2026-07-18", status="ok", clear_fraction=1.0,
        anomaly=C.AnomalySummary(field_score=4.0, regions=[region]), gates=C.Gates(senescence=False, harvest=False, regional=False),
        overlays=C.Overlays(true_color=ref, health=ref), crop_state=C.CropState(**cs) if cs else None)


def test_decide_obeys_the_crop_state():
    assert decide.evaluate(_res(), 2.0, 1.2).emit                                              # no state: as before
    assert decide.evaluate(_res(state="vegetative", diagnosable=True), 2.0, 1.2).emit
    assert decide.evaluate(_res(state="unknown", diagnosable=True), 2.0, 1.2).emit
    for state in ("pre_emergence", "senescing", "harvest_in_progress", "harvested"):
        d = decide.evaluate(_res(state=state, diagnosable=False), 2.0, 1.2)
        assert not d.emit and state in d.reason
    late = dict(state="reproductive", diagnosable=False, hail_only=True)
    d = decide.evaluate(_res(**late, reasons=["late in the season"]), 2.0, 1.2)
    assert not d.emit and "hail" in d.reason
    assert decide.evaluate(_res(**late, reasons=["x", "hail_exception=True"]), 2.0, 1.2).emit
    # the exception never overrides a legacy gate
    blocked = _res(**late, reasons=["hail_exception=True"])
    blocked.gates.regional = True
    assert not decide.evaluate(blocked, 2.0, 1.2).emit


# ---------- planting date and defaults ----------


def test_state_lookup_and_defaults():
    assert planting_defaults.state_for_point(-96.68, 42.17) == "NE"       # the founder's house field
    assert planting_defaults.state_for_point(-93.6, 41.6) == "IA"
    assert planting_defaults.state_for_point(-120.0, 47.0) is None
    d = planting_defaults.default_planting_date("corn", -96.68, 42.17, 2027)
    assert d == date(2027, 5, 7)
    assert planting_defaults.default_planting_date("corn", -120.0, 47.0, 2026) is None
    assert planting_defaults.default_planting_date("wheat", -96.68, 42.17, 2026) is None
    for crop in ("corn", "soybeans"):
        for st in ("NE", "IA", "KS", "SD", "MN", "IL", "MO"):
            assert planting_defaults.provenance(crop, st)["verified"] is True


def test_build_script_helpers():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_planting_defaults.py"
    spec = importlib.util.spec_from_file_location("build_planting_defaults", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pts = [(date(2025, 5, 4), 30.0), (date(2025, 5, 11), 60.0)]
    assert mod.crossing(pts) == date(2025, 5, 9)
    rows = [{"year": str(y), "week_ending": f"{y}-05-{d:02d}", "Value": str(v)}
            for y in (2023, 2024, 2025) for d, v in ((4, 30), (11, 60))] + [{"year": "2025", "week_ending": "2025-05-18", "Value": "(D)"}]
    assert mod.state_median(rows) == ("05-09", [2023, 2024, 2025])


# ---------- analyzer wiring: planting date, weather, season passes ----------


POLY = [(-96.548, 42.152), (-96.532, 42.152), (-96.532, 42.168), (-96.548, 42.168)]


class SeasonLoader:
    """Scene dates -> NDVI level (constant 0.82 unless given); no clouds. Texture is shared so passes agree."""

    def __init__(self, levels):
        self.levels = levels

    def search(self, bbox, start, end, max_cloud=100.0, limit=20):
        return [C.SceneInfo(scene_id=f"S2_{d:%Y%m%d}", datetime=f"{d.isoformat()}T17:30:00Z", tile_id="14TPM", cloud_cover=1.0)
                for d in sorted(self.levels, reverse=True) if start <= d <= end][:limit]

    def read(self, scene, grid):
        from fieldwatch.analysis.grid import SceneData
        d = date.fromisoformat(scene.datetime[:10])
        h, w = grid.shape
        yy, xx = np.mgrid[0:h, 0:w]
        ndvi = self.levels[d] + np.sin(yy / 17.0) * 0.015 + np.cos(xx / 23.0) * 0.015
        nir = np.full((h, w), 0.4, "float32")
        red = (nir * (1 - ndvi) / (1 + ndvi)).astype("float32")
        return SceneData(scene.scene_id, d, grid, red=red, nir=nir, rededge=nir * 0.7, swir=nir * 0.8,
                         scl=np.full((h, w), 4, np.uint8), tile_id="14TPM")


def warm(lat, lon, start, end):
    return [(start + timedelta(days=i), 60.0, 80.0) for i in range((end - start).days + 1)]  # 20 GDD a day


def req(as_of, **kw):
    return C.FieldAnalysisRequest(field=C.FieldRef(farm_id="f", field_id="x"), polygon=POLY, crop="corn", as_of=as_of, **kw)


def levels(extra=None):
    out = {date(y, 7, 15): 0.82 for y in (2023, 2024, 2025)}
    out.update({date(2026, 6, 10): 0.60, date(2026, 7, 13): 0.82, date(2026, 7, 18): 0.82})
    out.update(extra or {})
    return out


def test_analyzer_uses_the_state_default_and_degree_days():
    res = analyze(req("2026-07-18"), loader=SeasonLoader(levels()), writer=NullWriter(), weather=warm)
    cs = res.crop_state
    assert cs.planting_date == "2026-05-07" and cs.planting_date_source == "state_default"   # NE corn
    assert cs.gdd_since_planting == 20 * 73 and cs.state == "reproductive" and cs.diagnosable
    C.FieldAnalysisResult.model_validate(res.model_dump())


def test_analyzer_prefers_the_farmer_date_and_ignores_last_years():
    res = analyze(req("2026-07-18", planting_date="2026-04-25"), loader=SeasonLoader(levels()), writer=NullWriter(), weather=warm)
    assert res.crop_state.planting_date == "2026-04-25" and res.crop_state.planting_date_source == "farmer"
    old = analyze(req("2026-07-18", planting_date="2025-05-01"), loader=SeasonLoader(levels()), writer=NullWriter(), weather=warm)
    assert old.crop_state.planting_date_source == "state_default"
    assert any("2025" in r for r in old.crop_state.reasons)


def test_analyzer_refines_the_default_from_green_up():
    lv = levels({date(2026, 5, 10): 0.12, date(2026, 5, 24): 0.20, date(2026, 6, 4): 0.55})
    res = analyze(req("2026-07-18"), loader=SeasonLoader(lv), writer=NullWriter(), weather=warm)
    cs = res.crop_state
    assert cs.planting_date_source == "green_up" and date(2026, 5, 10) <= date.fromisoformat(cs.planting_date) <= date(2026, 6, 4)


def test_analyzer_without_weather_keeps_the_date_but_not_the_stage():
    res = analyze(req("2026-07-18"), loader=SeasonLoader(levels()), writer=NullWriter(), weather=None)
    cs = res.crop_state
    assert cs.planting_date == "2026-05-07" and cs.gdd_since_planting is None and cs.state == "unknown" and cs.diagnosable


def test_analyzer_survives_a_weather_failure():
    def boom(*a):
        raise OSError("no network")
    res = analyze(req("2026-07-18"), loader=SeasonLoader(levels()), writer=NullWriter(), weather=boom)
    assert res.status in ("ok", "no_baseline") and res.crop_state.gdd_since_planting is None


def test_analyzer_end_to_end_partial_harvest_in_october():
    lv = {date(y, 10, 6): 0.5 for y in (2023, 2024, 2025)}
    lv.update({date(2026, 6, 10): 0.6, date(2026, 8, 5): 0.86, date(2026, 8, 25): 0.84, date(2026, 9, 28): 0.70, date(2026, 10, 14): 0.7})

    class Half(SeasonLoader):
        def read(self, scene, grid):
            data = super().read(scene, grid)
            if scene.datetime.startswith("2026-10-14"):
                h, w = grid.shape
                cols = np.arange(w)[None, :] >= w // 2
                nir = data.nir
                red = np.where(cols, nir * (1 - 0.3) / (1 + 0.3), data.red).astype("float32")
                data.red = red
            return data

    res = analyze(req("2026-10-15"), loader=Half(lv), writer=NullWriter(), weather=warm)
    assert res.crop_state.state == "harvest_in_progress" and not res.crop_state.diagnosable


# ---------- live: the founder's house field, harvested in early October 2026 ----------

HOUSE = [(-96.6823963, 42.1768555), (-96.6824607, 42.170132), (-96.6756816, 42.1718169), (-96.6761535, 42.1768714),
         (-96.6823963, 42.1768555)]


def _house(as_of):
    import os
    if os.getenv("FIELDWATCH_LIVE") != "1":
        pytest.skip("set FIELDWATCH_LIVE=1")
    return analyze(req(as_of).model_copy(update={"polygon": HOUSE}), writer=NullWriter())


@pytest.mark.live
def test_live_house_field_after_harvest_is_not_diagnosable():
    res = _house("2026-10-10")
    assert res.status in ("ok", "no_baseline") and res.crop_state is not None, res.error
    print("house 2026-10-10:", res.date, res.crop_state.model_dump())
    assert res.crop_state.state in ("senescing", "harvest_in_progress", "harvested") and not res.crop_state.diagnosable


@pytest.mark.live
def test_live_house_field_in_july_is_diagnosable():
    res = _house("2026-07-18")
    assert res.crop_state is not None, res.error
    print("house 2026-07-18:", res.date, res.crop_state.model_dump())
    assert res.crop_state.diagnosable and res.crop_state.state in ("vegetative", "reproductive", "unknown")
