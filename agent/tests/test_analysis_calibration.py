"""Calibration of the analyzer's gates and thresholds. The synthetic arrays mirror numbers measured on real
Sentinel-2 scenes of Thurston Co. NE fields (see fieldwatch/analysis/CALIBRATION.md); the live tests rerun the
real cases (FIELDWATCH_LIVE=1, network, a few minutes)."""
import os
from datetime import date, timedelta

import numpy as np
import pytest
from scipy import ndimage

from fieldwatch import contracts as C
from fieldwatch.analysis import analyze, gates, regions
from fieldwatch.analysis.core import analyze_stacks
from fieldwatch.analysis.grid import Grid, IndexStack, Pivot
from fieldwatch.analysis.writer import NullWriter

N = 100
GRID = Grid(shape=(N, N), res_m=10.0, origin=(-96.54, 42.16))
YY, XX = np.mgrid[0:N, 0:N]
R_M = np.hypot(YY - 49.5, XX - 49.5) * 10.0
LL = GRID.pix_to_lonlat(49.5, 49.5)
PIVOT = Pivot((float(LL[0]), float(LL[1])), 480.0)


def smooth(seed, amp):
    f = ndimage.gaussian_filter(np.random.default_rng(seed).normal(size=(N, N)), 5)
    return f / f.std() * amp


def stack(ndvi, d, sid):
    valid = np.ones((N, N), bool)
    return IndexStack(scene_id=sid, date=d, grid=GRID, ndvi=ndvi.astype("float32"), ndre=ndvi * 0.5, ndmi=ndvi * 0.45,
                      valid=valid, inside=valid.copy(), clear_fraction=1.0)


def mid_july(cur_drop_mask=None, drop=0.0, d=date(2026, 7, 17), prev_delta=0.0, sigma=0.045):
    """Real 2026-07-17 numbers: pixel sigma of (now - baseline) 0.035-0.047, one prior season with a haze edge
    (its own pixel sigma 0.09), peak canopy NDVI ~0.88."""
    tex = smooth(1, 0.02)
    cur = 0.88 + tex + smooth(2, sigma * 0.6) + np.random.default_rng(3).normal(0, sigma * 0.5, (N, N))
    if cur_drop_mask is not None:
        cur = np.where(cur_drop_mask, cur - drop, cur)
    hist = [
        stack(0.87 + tex + np.random.default_rng(10 + i).normal(0, 0.01, (N, N)), date(2025 - i, 7, 18), f"h{i}")
        for i in range(2)
    ]
    outlier = 0.87 + tex + np.random.default_rng(12).normal(0, 0.01, (N, N)) + smooth(13, 0.09)
    hist.append(stack(outlier, date(2023, 7, 18), "h2"))
    prev = stack(cur + prev_delta * -1 + np.random.default_rng(14).normal(0, 0.01, (N, N)), d - timedelta(days=5), "prev")
    return stack(cur, d, "cur"), hist, prev


def emits(res, z=2.0, min_acres=1.2):
    return (res.status == "ok" and not any([res.gates.senescence, res.gates.harvest, res.gates.regional])
            and any(r.score >= z and r.area_acres >= min_acres for r in res.anomaly.regions))


def test_baseline_median_resists_one_odd_season():
    base = [np.full((4, 4), 0.85), np.full((4, 4), 0.87)]
    odd = np.full((4, 4), 0.30)
    assert regions.baseline_mean(base + [odd])[0, 0] == pytest.approx(0.85)  # median of three
    assert regions.baseline_mean(base)[0, 0] == pytest.approx(0.86)          # mean of two


def test_ring_on_noisy_mid_july_scene_is_flagged():
    ring = (R_M > 300) & (R_M < 360)
    cur, hist, prev = mid_july(ring, drop=0.15)
    res = analyze_stacks(cur, hist, prev, crop="corn", pivot=PIVOT)
    assert emits(res)
    top = max(res.anomaly.regions, key=lambda r: r.area_acres)
    assert top.features.ring_score > 0.5 and top.area_acres > 4


def test_clean_noisy_mid_july_scene_is_quiet():
    cur, hist, prev = mid_july()
    res = analyze_stacks(cur, hist, prev, crop="corn", pivot=PIVOT)
    assert not emits(res), [(r.area_acres, r.score) for r in res.anomaly.regions]


def test_small_weak_blip_is_dropped_but_small_strong_patch_kept():
    assert not regions.strong_enough(1.5, 2.4, 2.0)   # real clean-field blips were 1.2-2.5 ac at score 2.2-2.7
    assert regions.strong_enough(1.5, 3.4, 2.0)
    assert regions.strong_enough(6.0, 2.1, 2.0)       # a bigger region only needs the plain threshold
    assert not regions.strong_enough(6.0, 1.9, 2.0)


def test_compact_blob_off_the_radius_is_not_a_wedge():
    blob = (abs(YY - 25) < 8) & (abs(XX - 80) < 8)    # compact patch far from the pivot centre
    cur, hist, prev = mid_july(blob, drop=0.3, sigma=0.02)
    res = analyze_stacks(cur, hist, prev, crop="corn", pivot=PIVOT)
    f = max(res.anomaly.regions, key=lambda r: r.area_acres).features
    assert f.wedge_score < 0.4, f


def test_drying_canopy_opens_senescence_gate_before_the_calendar():
    # real corn: median NDVI 0.88 (Aug 21) -> 0.86 (Sep 10) -> 0.78 (Sep 18): early drydown must not read as a problem
    assert gates.seasonal("corn", date(2026, 8, 24), delta_since_last=-0.07) == (True, False)
    assert gates.seasonal("corn", date(2026, 8, 24), delta_since_last=-0.02) == (False, False)
    assert gates.seasonal("corn", date(2026, 7, 20), delta_since_last=-0.2) == (False, False)  # peak summer: a drop is news
    assert gates.seasonal("corn", date(2026, 9, 20), ndvi_p50=0.30) == (True, True)             # already harvested
    assert gates.seasonal("corn", date(2026, 9, 20), ndvi_p50=0.77) == (True, False)
    assert gates.seasonal("alfalfa", date(2026, 9, 20), delta_since_last=-0.2, ndvi_p50=0.2) == (False, False)


def test_end_of_season_with_patchy_canopy_stays_quiet():
    # Oct 5 real field: median 0.35, patchy; Sep 18: median 0.78 and -0.086 since the previous pass
    for d, level in ((date(2026, 9, 18), 0.78), (date(2026, 10, 5), 0.35)):
        tex = smooth(5, 0.06)
        cur = stack(level + tex, d, "c")
        hist = [stack(level + 0.05 + smooth(20 + i, 0.06), date(2025 - i, d.month, d.day), f"h{i}") for i in range(3)]
        res = analyze_stacks(cur, hist, stack(level + 0.09 + tex, d - timedelta(days=8), "p"), crop="corn")
        assert not emits(res)
        assert res.gates.senescence


# ---- real scenes (public Earth Search; no AWS) ---------------------------------------------------------------

W, S_, E, N_ = -96.548, 42.152, -96.532, 42.168   # the demo "north pivot" box in the Thurston Co. NE cluster


def _box(x0, x1, y0, y1):
    w, e = W + (E - W) * x0, W + (E - W) * x1
    n, s = N_ - (N_ - S_) * y0, N_ - (N_ - S_) * y1
    return [(w, n), (e, n), (e, s), (w, s), (w, n)]


def _live(poly, as_of, crop="corn"):
    if os.getenv("FIELDWATCH_LIVE") != "1":
        pytest.skip("set FIELDWATCH_LIVE=1")
    req = C.FieldAnalysisRequest(field=C.FieldRef(farm_id="f", field_id="cal"), polygon=poly, crop=crop, as_of=as_of)
    return analyze(req, writer=NullWriter())


@pytest.mark.live
def test_live_bare_trapezoid_is_flagged_in_summer():
    """SE field of the box: a ~13 acre trapezoid with no crop on 2026-07-17 (score ~6, persists to late August)."""
    res = _live(_box(0.50, 0.92, 0.40, 0.80), "2026-07-17")
    assert res.status == "ok" and res.date == "2026-07-17" and emits(res)
    assert max(r.area_acres for r in res.anomaly.regions) > 8


@pytest.mark.live
@pytest.mark.parametrize("as_of", ["2026-07-17", "2026-07-27", "2026-08-10", "2026-08-25"])
def test_live_healthy_field_is_quiet_in_summer(as_of):
    res = _live(_box(0.04, 0.42, 0.04, 0.33), as_of)  # NW field: uniform corn all summer
    assert res.status == "ok" and not emits(res), res.anomaly.regions


@pytest.mark.live
@pytest.mark.parametrize("as_of", ["2026-09-22", "2026-10-08"])
def test_live_end_of_season_stays_quiet(as_of):
    for poly in (_box(0.04, 0.42, 0.04, 0.33), _box(0.04, 0.43, 0.42, 0.76), _box(0.50, 0.92, 0.40, 0.80), _box(0, 1, 0, 1)):
        res = _live(poly, as_of)
        assert res.status == "ok" and res.gates.senescence and not emits(res)
