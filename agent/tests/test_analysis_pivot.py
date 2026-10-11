"""Pivot inference from the field polygon. No network (live check is opt-in)."""
import math
import os
from datetime import date

import numpy as np
import pytest
from scipy import ndimage

from fieldwatch import contracts as C
from fieldwatch.analysis.analyzer import analyze_stacks
from fieldwatch.analysis.grid import Grid, IndexStack, Pivot
from fieldwatch.analysis.pivot import infer_pivot, resolve_pivot

LON0, LAT0 = -96.54, 42.16
M_LAT = 110_574.0
M_LON = 111_320.0 * math.cos(math.radians(LAT0))


def ll(x, y):
    return (LON0 + x / M_LON, LAT0 + y / M_LAT)


def square(side):
    h = side / 2
    return [ll(-h, -h), ll(h, -h), ll(h, h), ll(-h, h)]


def circle(r, n=72):
    return [ll(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def dist_m(a, b):
    return math.hypot((a[0] - b[0]) * M_LON, (a[1] - b[1]) * M_LAT)


def test_quarter_section_square():
    p = infer_pivot(square(805))  # half-mile square = 160 acres
    assert p is not None
    assert 380 < p.radius_m < 425
    assert dist_m(p.center_lonlat, (LON0, LAT0)) < 2


def test_circle_polygon():
    p = infer_pivot(circle(400))
    assert p is not None and 390 < p.radius_m <= 401


def test_deterministic_and_closed_ring():
    poly = square(805)
    assert infer_pivot(poly) == infer_pivot(poly + [poly[0]])


def test_long_thin_field_rejected():
    h = [ll(-800, -150), ll(800, -150), ll(800, 150), ll(-800, 150)]
    assert infer_pivot(h) is None
    assert infer_pivot([ll(-800, -400), ll(800, -400), ll(800, 400), ll(-800, 400)]) is None  # 2:1


def test_l_shape_rejected():
    s = 800
    el = [ll(0, 0), ll(s, 0), ll(s, s / 3), ll(s / 3, s / 3), ll(s / 3, s), ll(0, s)]
    assert infer_pivot(el) is None
    big_l = [ll(0, 0), ll(s, 0), ll(s, s / 2), ll(s / 2, s / 2), ll(s / 2, s), ll(0, s)]
    assert infer_pivot(big_l) is None


def test_degenerate_and_tiny():
    assert infer_pivot([]) is None
    assert infer_pivot(square(60)) is None  # a garden, not a pivot
    assert infer_pivot([ll(0, 0), ll(100, 0), ll(200, 0)]) is None


def test_explicit_pivot_wins():
    explicit = {"center": [-96.5, 42.1], "radius_m": 123.0}
    p = resolve_pivot(explicit, square(805))
    assert p == Pivot((-96.5, 42.1), 123.0)
    assert resolve_pivot(None, square(805)) is not None
    assert resolve_pivot({}, circle(400)) is not None
    assert resolve_pivot(None, [ll(-800, -150), ll(800, -150), ll(800, 150), ll(-800, 150)]) is None


def test_inference_logged(caplog):
    with caplog.at_level("INFO", logger="fieldwatch.analysis.pivot"):
        resolve_pivot(None, square(805))
    assert "pivot inferred" in caplog.text


# ---- end to end: ring stress on an inferred pivot -------------------------------------------------

N = 100
GRID = Grid(shape=(N, N), res_m=10.0, origin=(LON0 - 500 / M_LON, LAT0 + 500 / M_LAT))
YY, XX = np.mgrid[0:N, 0:N]
TEXTURE = ndimage.gaussian_filter(np.random.default_rng(1).normal(size=(N, N)), 6)
TEXTURE = TEXTURE / TEXTURE.std() * 0.02


def _stack(ndvi, d, sid):
    valid = np.ones((N, N), bool)
    nd = ndvi.astype("float32")
    return IndexStack(scene_id=sid, date=d, grid=GRID, ndvi=nd, ndre=nd * 0.5, ndmi=nd * 0.45,
                      valid=valid, inside=valid.copy(), clear_fraction=1.0)


def test_ring_on_inferred_pivot_scores_high():
    pivot = infer_pivot(square(900))
    assert pivot is not None
    center_px = GRID.lonlat_to_pix(*pivot.center_lonlat)
    r_m = np.hypot(YY - center_px[0], XX - center_px[1]) * 10.0
    ring = (r_m > 0.8 * pivot.radius_m) & (r_m < 0.95 * pivot.radius_m)
    rng = np.random.default_rng
    hist = [_stack(0.82 + off + TEXTURE * 0.8 + rng(100 + i).normal(0, 0.01, (N, N)), date(y, 7, 17), f"h{y}")
            for i, (y, off) in enumerate([(2025, -0.04), (2024, -0.02), (2023, -0.06)])]
    prev = _stack(0.82 + TEXTURE + rng(9).normal(0, 0.01, (N, N)), date(2026, 7, 13), "prev")
    cur_ndvi = 0.82 + TEXTURE + rng(5).normal(0, 0.01, (N, N))
    cur = _stack(np.where(ring, cur_ndvi - 0.25, cur_ndvi), date(2026, 7, 18), "cur")

    res = analyze_stacks(cur, hist, prev, crop="corn", pivot=resolve_pivot(None, square(900)))
    assert res.anomaly.regions
    best = max(res.anomaly.regions, key=lambda r: r.area_acres * r.score)
    assert best.features.ring_score > 0.6

    none = analyze_stacks(cur, hist, prev, crop="corn", pivot=None)
    assert max(r.features.ring_score for r in none.anomaly.regions) == 0


@pytest.mark.live
def test_live_inferred_pivot_on_demo_field():
    """Demo north-pivot polygon (bbox of the fixture field, no pivot) through the real analyzer."""
    if os.getenv("FIELDWATCH_LIVE") != "1":
        pytest.skip("set FIELDWATCH_LIVE=1")
    from fieldwatch.analysis import analyze
    from fieldwatch.analysis.writer import NullWriter

    poly = [(-96.548, 42.152), (-96.532, 42.152), (-96.532, 42.168), (-96.548, 42.168)]
    p = infer_pivot(poly)
    print("inferred", p)
    res = analyze(C.FieldAnalysisRequest(field=C.FieldRef(farm_id="f", field_id="north-pivot"),
                                         polygon=poly, as_of="2025-08-22"), writer=NullWriter())
    print(res.status, [(r.area_acres, r.features.ring_score) for r in res.anomaly.regions] if res.anomaly else None)
