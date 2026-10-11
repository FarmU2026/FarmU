"""Departure / change / normal overlays: renderers, metadata, and proof that drawing them changes no decision."""
import hashlib
import io
import json
from datetime import date

import numpy as np
import pytest
from PIL import Image

from fieldwatch import contracts as C
from fieldwatch.analysis import analyze, render
from fieldwatch.analysis.core import analyze_stacks
from fieldwatch.analysis.writer import NullWriter
from test_analysis_core import HIST, N, PREV, XX, YY, current, run
from test_analysis_pipeline import FakeLoader, MemWriter, req, scenes

# ---------- renderers ----------


def decode(png: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(png)).convert("RGBA"))


def px(img: np.ndarray, r: int, c: int) -> tuple:
    k = img.shape[0] // 4  # the test arrays are 4x4; each pixel is blown up k x k
    return tuple(int(v) for v in img[r * k + k // 2, c * k + k // 2])


FIELD = np.array([[1, 1, 1, 1], [1, 1, 1, 1], [1, 1, 1, 0], [1, 1, 0, 0]], bool)


def sample(scale):
    return np.array([[-scale, -scale / 2, 0, scale / 2],
                     [scale, -5 * scale, 5 * scale, 0],
                     [np.nan, 0, 0, 0], [0, 0, 0, 0]], "float32")


@pytest.mark.parametrize("fn,scale", [(render.departure, render.DEPARTURE_Z_RANGE),
                                      (render.change, render.CHANGE_NDVI_RANGE)])
def test_diverging_overlays(fn, scale):
    img = decode(fn(sample(scale), FIELD))
    assert img.shape[:2] == (512, 512)
    red_end, orange, neutral, teal = px(img, 0, 0), px(img, 0, 1), px(img, 0, 2), px(img, 0, 3)
    blue_end = px(img, 1, 0)
    # -max: strong red; -max/2: orange; 0: light neutral; +max/2: teal; +max: strong blue
    assert red_end[0] > 150 and red_end[1] < 40 and red_end[2] < 40 and red_end[3] == render.DIVERGING_ALPHA
    assert orange[0] > 220 and 90 < orange[1] < 150 and orange[2] < 100
    assert min(neutral[:3]) > 220 and neutral[3] == render.DIVERGING_ALPHA
    assert teal[2] > teal[0] + 60 and teal[1] > teal[0]
    assert blue_end[2] > 150 and blue_end[0] < 60 and blue_end[3] == render.DIVERGING_ALPHA
    assert px(img, 1, 1) == red_end and px(img, 1, 2) == blue_end      # clipped past the fixed scale
    assert px(img, 2, 0) == render.INVALID_RGBA                        # NaN inside the field: gray
    assert px(img, 2, 3)[3] == 0 and px(img, 3, 3)[3] == 0 and px(img, 3, 2)[3] == 0   # outside the field: clear


def test_diverging_scale_is_monotonic_and_light_at_zero():
    z = np.linspace(-3, 3, 7, dtype="float32").reshape(1, 7)
    rgb = render.diverging_rgb(z, 3.0)[0].astype(int)
    assert rgb[0, 0] > rgb[-1, 0] and rgb[0, 2] < rgb[-1, 2]  # red channel falls, blue rises
    assert (rgb[3] >= 230).all()                              # z = 0 is light


def test_normal_uses_the_health_palette():
    base = np.full((4, 4), 0.8, "float32")
    base[0, 0] = np.nan
    stack_like = type("S", (), {"ndvi": base, "valid": np.isfinite(base)})()
    assert render.normal(base, FIELD) == render.health(stack_like, FIELD)
    img = decode(render.normal(base, FIELD))
    assert px(img, 0, 0)[3] == 70 and px(img, 1, 1)[3] == 255 and px(img, 3, 3)[3] == 0


def test_health_png_is_unchanged_by_the_refactor():
    ndvi = np.linspace(-0.1, 0.9, 16, dtype="float32").reshape(4, 4)
    st = type("S", (), {"ndvi": ndvi, "valid": FIELD})()
    img = decode(render.health(st, FIELD))
    t = np.clip((ndvi + 0.1) / 0.9, 0, 1)
    want = np.dstack([(1 - t) * 180 + 40, t * 160 + 60, np.full_like(t, 40)]).astype("uint8")
    assert px(img, 0, 0)[:3] == tuple(int(v) for v in want[0, 0]) and px(img, 1, 1)[3] == 255


# ---------- the core hands over the arrays the decision used ----------


def test_core_exposes_baseline_departure_and_change():
    patch = np.hypot(YY - 30, XX - 60) < 8
    r = run(current(patch=patch, drop=0.2))
    assert r.baseline.shape == (N, N) and r.baseline_method == "median"
    assert r.z is not None and np.nanmin(r.z[patch]) < -2 and abs(np.nanmedian(r.z)) < 0.5
    assert r.change.shape == (N, N) and abs(np.nanmedian(r.change)) < 0.02 and np.nanmean(r.change[patch]) < -0.1
    assert r.median_departure is not None
    assert analyze_stacks(current(), HIST[:2], PREV).baseline_method == "mean"
    r3 = analyze_stacks(current(), [], None)
    assert r3.status == "no_baseline" and r3.z is None and r3.baseline is None and r3.change is None


def test_change_is_nan_where_either_pass_is_invalid():
    valid = np.ones((N, N), bool)
    valid[:20] = False
    r = run(current(valid=valid))
    assert np.isnan(r.change[:20]).all() and np.isfinite(r.change[20:]).all()


# ---------- detection is unchanged (goldens recorded on main before the overlays existed) ----------


def _detection_only(o):
    """Drop descriptive fields added after the goldens were recorded (a region's `where` is words, not detection)."""
    if isinstance(o, dict):
        return {k: _detection_only(v) for k, v in o.items() if k != "where"}
    if isinstance(o, list):
        return [_detection_only(v) for v in o]
    return o


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(_detection_only(obj), sort_keys=True).encode()).hexdigest()[:16]


def test_core_detection_unchanged():
    patch = np.hypot(YY - 30, XX - 60) < 8
    r = run(current(patch=patch, drop=0.2))
    assert digest({"anomaly": r.anomaly.model_dump(mode="json"), "gates": r.gates.model_dump(),
                   "indices": r.indices.model_dump(), "status": r.status}) == "482b3fb43f2780a2"
    assert (r.anomaly.field_score, len(r.anomaly.regions), r.anomaly.z_baseline) == (8.73, 1, 0.78)
    r2 = analyze_stacks(current(), [], PREV)
    assert digest({"anomaly": r2.anomaly.model_dump(mode="json"), "gates": r2.gates.model_dump(),
                   "status": r2.status}) == "77194bc152842fd1"


DETECTION_KEYS = ("anomaly", "gates", "indices", "date", "clear_fraction", "status", "scene_id")


def test_pipeline_detection_unchanged_with_and_without_a_writer():
    loader = FakeLoader(scenes(), problem_on=date(2026, 7, 18))
    quiet = analyze(req(), loader=loader, writer=NullWriter()).model_dump(mode="json")
    drawn = analyze(req(), loader=loader, writer=MemWriter()).model_dump(mode="json")
    assert {k: drawn[k] for k in DETECTION_KEYS} == {k: quiet[k] for k in DETECTION_KEYS}
    assert digest({k: drawn[k] for k in DETECTION_KEYS}) == "44de497c52c3ea5d"  # recorded before the overlays
    assert drawn["anomaly"]["field_score"] == 9.41


def test_a_failing_extra_overlay_never_fails_the_analysis(monkeypatch):
    def boom(*a):
        raise ValueError("boom")

    monkeypatch.setattr(render, "departure", boom)
    res = analyze(req(), loader=FakeLoader(scenes(), problem_on=date(2026, 7, 18)), writer=MemWriter())
    assert res.status == "ok" and res.overlays.departure is None and res.overlays.change is not None
    assert res.comparison.departure_scale is None and res.comparison.change_scale is not None


# ---------- pipeline: files and metadata ----------


def test_pipeline_writes_the_new_overlays_and_metadata():
    w = MemWriter()
    res = analyze(req(), loader=FakeLoader(scenes(), problem_on=date(2026, 7, 18)), writer=w)
    C.FieldAnalysisResult.model_validate(res.model_dump())
    prefix = "analysis/north-pivot/2026-07-18/s2/"
    size = Image.open(io.BytesIO(w.files[res.overlays.health.key])).size
    for kind in ("departure", "change", "normal"):
        ref = getattr(res.overlays, kind)
        assert ref.key == f"{prefix}{kind}.png" and ref.bounds == res.overlays.health.bounds
        assert Image.open(io.BytesIO(w.files[ref.key])).size == size
    dep = decode(w.files[res.overlays.departure.key])
    assert dep[..., 3].max() > 0 and len({tuple(p) for p in dep.reshape(-1, 4)}) > 5   # not blank
    # the ring problem is red in the departure image and a decline in the change image
    assert ((dep[..., 0] > dep[..., 2] + 60) & (dep[..., 3] > 200)).any()
    chg = decode(w.files[res.overlays.change.key])
    assert ((chg[..., 0] > chg[..., 2] + 60) & (chg[..., 3] > 200)).any()
    cmp_ = res.comparison
    assert cmp_.scene_date == "2026-07-18" and cmp_.previous_date == "2026-07-13"
    assert cmp_.baseline_dates == ["2023-07-15", "2024-07-15", "2025-07-15"] and cmp_.baseline_method == "median"
    assert (cmp_.departure_scale.stat, cmp_.departure_scale.vmin, cmp_.departure_scale.vmax) == ("z", -3.0, 3.0)
    assert (cmp_.change_scale.stat, cmp_.change_scale.vmin, cmp_.change_scale.vmax) == ("ndvi_diff", -0.25, 0.25)
    assert cmp_.median_departure_ndvi is not None
    # the JSON round trip keeps it (it is stored on the fw-scene-analyses row as JSON)
    again = C.FieldAnalysisResult.model_validate(json.loads(res.model_dump_json()))
    assert again.comparison == cmp_ and again.overlays.departure == res.overlays.departure


def test_without_a_previous_pass_there_is_no_change_image():
    sc = {date(y, 7, 15): 0.0 for y in (2023, 2024, 2025)} | {date(2026, 7, 18): 0.0}
    res = analyze(req(), loader=FakeLoader(sc), writer=MemWriter())
    assert res.status == "ok" and res.overlays.change is None and res.comparison.change_scale is None
    assert res.comparison.previous_date is None
    assert res.overlays.departure is not None and res.overlays.normal is not None


def test_without_a_baseline_only_change_is_drawn():
    sc = {date(2026, 7, 13): 0.0, date(2026, 7, 18): 0.0}
    res = analyze(req(), loader=FakeLoader(sc), writer=MemWriter())
    assert res.status == "no_baseline"
    assert res.overlays.departure is None and res.overlays.normal is None and res.overlays.change is not None
    assert res.comparison.baseline_dates == [] and res.comparison.baseline_method is None
    assert res.comparison.departure_scale is None and res.comparison.previous_date == "2026-07-13"


def test_cloudy_and_error_results_have_no_comparison():
    res = analyze(req(), loader=FakeLoader({date(2026, 7, 18): 0.9, date(2026, 7, 15): 0.8}), writer=MemWriter())
    assert res.status == "too_cloudy" and res.comparison is None and res.overlays is None
    assert analyze(req(), loader=FakeLoader({}), writer=MemWriter()).comparison is None


@pytest.mark.live
def test_live_demo_box_overlays(tmp_path):
    """Real Earth Search + COG reads for the demo box (public data, network, ~1-2 min): the comparison images exist."""
    import os

    if os.getenv("FIELDWATCH_LIVE") != "1":
        pytest.skip("set FIELDWATCH_LIVE=1")
    from fieldwatch.analysis.writer import LocalWriter

    res = analyze(req(as_of="2026-07-18", pivot=None), writer=LocalWriter(tmp_path))
    assert res.status == "ok", res.error
    assert res.comparison.previous_date and len(res.comparison.baseline_dates) >= 2
    for kind in ("departure", "change", "normal"):
        img = decode((tmp_path / getattr(res.overlays, kind).key).read_bytes())
        assert img[..., 3].max() > 0 and len({tuple(p) for p in img.reshape(-1, 4)}) > 10


def test_region_where_is_computed_from_lon_lat():
    patch = np.hypot(YY - 30, XX - 60) < 8
    r = run(current(patch=patch, drop=0.2))
    assert r.anomaly.regions[0].where and r.anomaly.regions[0].where.endswith("of the field")
