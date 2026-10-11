"""Numeric core of the field analyzer on synthetic arrays. No network."""
from datetime import date

import numpy as np
import pytest
from scipy import ndimage

from fieldwatch import contracts as C
from fieldwatch.analysis import gates
from fieldwatch.analysis.core import analyze_stacks
from fieldwatch.analysis.grid import Grid, IndexStack, Layers, Pivot, SceneData
from fieldwatch.analysis.indices import compute_indices

N = 100  # 100 x 100 px at 10 m = 1 km square
GRID = Grid(shape=(N, N), res_m=10.0, origin=(-96.54, 42.16))
YY, XX = np.mgrid[0:N, 0:N]
CENTER = (49.5, 49.5)
PIVOT_LL = GRID.pix_to_lonlat(*CENTER)
PIVOT = Pivot((float(PIVOT_LL[0]), float(PIVOT_LL[1])), 480.0)
R_M = np.hypot(YY - CENTER[0], XX - CENTER[1]) * 10.0
ANG = np.degrees(np.arctan2(-(YY - CENTER[0]), XX - CENTER[1])) % 360


def _smooth(seed, amp):
    f = ndimage.gaussian_filter(np.random.default_rng(seed).normal(size=(N, N)), 6)
    return f / f.std() * amp


TEXTURE = _smooth(1, 0.02)


def stack(ndvi, d=date(2026, 7, 18), valid=None, sid="s"):
    valid = np.ones((N, N), bool) if valid is None else valid
    nd = np.where(valid, ndvi, np.nan).astype("float32")
    return IndexStack(scene_id=sid, date=d, grid=GRID, ndvi=nd, ndre=nd * 0.5, ndmi=nd * 0.45,
                      valid=valid, inside=np.ones((N, N), bool), clear_fraction=float(valid.mean()))


def season(year, offset=-0.04, seed=0):
    rng = np.random.default_rng(100 + seed)
    return stack(0.82 + offset + TEXTURE * 0.8 + rng.normal(0, 0.01, (N, N)), d=date(year, 7, 17), sid=f"h{year}")


def current(patch=None, drop=0.25, seed=5, **kw):
    ndvi = 0.82 + TEXTURE + np.random.default_rng(seed).normal(0, 0.01, (N, N))
    if patch is not None:
        ndvi = np.where(patch, ndvi - drop, ndvi)
    return stack(ndvi, **kw)


HIST = [season(2025, seed=1), season(2024, -0.02, seed=2), season(2023, -0.06, seed=3)]
PREV = stack(0.82 + TEXTURE + np.random.default_rng(9).normal(0, 0.01, (N, N)), d=date(2026, 7, 13), sid="prev")


def run(cur, **kw):
    kw.setdefault("crop", "corn")
    kw.setdefault("pivot", PIVOT)
    return analyze_stacks(cur, HIST, PREV, **kw)


def top(res):
    assert res.anomaly.regions, "expected a region"
    return max(res.anomaly.regions, key=lambda r: r.area_acres * r.score)


def feats(region):
    f = region.features
    return {"ring": f.ring_score, "wedge": f.wedge_score, "edge": f.edge_band_score, "stripe": f.stripe_score}


def emits(res, z=2.0, min_acres=1.2):
    """Same rule as the decide step in docs/implementation-specs.md."""
    return (res.status == "ok" and not any([res.gates.senescence, res.gates.harvest, res.gates.regional])
            and any(r.score >= z and r.area_acres >= min_acres for r in res.anomaly.regions))


def test_healthy_field_has_no_regions():
    res = run(current())
    assert res.status == "ok"
    assert res.anomaly.regions == []
    assert not any([res.gates.senescence, res.gates.harvest, res.gates.regional])
    assert abs(res.anomaly.z_baseline) < 2
    assert abs(res.anomaly.delta_since_last) < 0.03


def test_ring_detected():
    res = run(current((R_M > 300) & (R_M < 360)))
    f = feats(top(res))
    assert f["ring"] > 0.6 and f["ring"] == max(f.values()), f
    assert top(res).area_acres > 5
    assert res.anomaly.delta_since_last < 0


def test_wedge_detected():
    res = run(current((ANG > 30) & (ANG < 60) & (R_M > 80) & (R_M < 470)))
    f = feats(top(res))
    assert f["wedge"] > 0.6 and f["wedge"] == max(f.values()), f


def test_edge_band_detected():
    res = run(current(XX < 5))
    f = feats(top(res))
    assert f["edge"] > 0.6 and f["edge"] == max(f.values()), f


def test_stripes_detected():
    patch = np.zeros((N, N), bool)
    for c in (28, 52, 76):
        patch[:, c:c + 3] = True
    res = run(current(patch))
    assert len(res.anomaly.regions) >= 2
    f = feats(top(res))
    assert f["stripe"] > 0.6 and f["stripe"] == max(f.values()), f


def test_small_patch_ignored():
    res = run(current((abs(YY - 20) < 3) & (abs(XX - 20) < 3)))  # ~0.2 ha
    assert res.anomaly.regions == []


def test_region_geometry_and_contract():
    res = run(current((abs(YY - 30) < 10) & (abs(XX - 70) < 10)))
    r = top(res)
    assert r.geometry["type"] == "Polygon"
    assert 7 < r.area_acres < 11  # 20 x 20 px = 4 ha ~ 9.9 ac
    lon, lat = r.centroid
    assert -96.54 < lon < -96.53 and 42.15 < lat < 42.16
    C.AnomalySummary.model_validate(res.anomaly.model_dump())


def test_clouds_excluded_from_indices_and_regions():
    patch = (abs(YY - 30) < 10) & (abs(XX - 70) < 10)
    scl = np.full((N, N), 4, np.uint8)
    scl[20:40, 60:80] = 9  # cloud sits on the stressed patch
    ones = np.full((N, N), 0.3, "float32")
    scene = SceneData("s", date(2026, 7, 18), GRID, red=ones * 0.5, nir=ones * 1.4, rededge=ones, swir=ones * 1.2, scl=scl)
    st = compute_indices(scene, np.ones((N, N), bool), interior_buffer_m=10)
    assert not st.valid[30, 70] and np.isnan(st.ndvi[30, 70])
    assert 0.8 < st.clear_fraction < 0.95
    # cloud-masked pixels (NaN) can't create a region
    cur = current(patch)
    mask = np.ones((N, N), bool)
    mask[15:45, 55:85] = False
    res = run(stack(np.where(mask, cur.ndvi, np.nan), valid=mask))
    assert res.anomaly.regions == []
    assert res.indices.ndvi.mean > 0.7  # stats ignore NaN


def test_scl_classes_and_buffer():
    scl = np.full((N, N), 4, np.uint8)
    scl[0:10, 0:10] = 3   # shadow
    scl[50, 50] = 10      # cirrus
    ones = np.full((N, N), 0.3, "float32")
    scene = SceneData("s", date(2026, 7, 18), GRID, red=ones * 0.5, nir=ones * 1.4, rededge=ones, swir=ones, scl=scl)
    st = compute_indices(scene, np.ones((N, N), bool), 10)
    assert not st.valid[5, 5] and not st.valid[50, 50] and st.valid[80, 80]
    assert not st.inside[0, 50] and st.inside[2, 50]  # 10 m interior buffer trims the edge ring
    assert st.ndvi[80, 80] == pytest.approx((0.42 - 0.15) / (0.42 + 0.15), abs=1e-4)


def test_senescence_and_harvest_gates():
    assert gates.seasonal("corn", date(2026, 9, 28)) == (True, False)
    assert gates.seasonal("corn", date(2026, 10, 12)) == (True, True)
    assert gates.seasonal("corn", date(2026, 7, 18)) == (False, False)
    assert gates.seasonal("soybeans", date(2026, 9, 20))[0] is True
    assert gates.seasonal("alfalfa", date(2026, 10, 20)) == (False, False)


def test_late_september_corn_is_quiet():
    # late season: the canopy yellows unevenly; the gate must say "not a problem"
    d = date(2026, 9, 28)
    res = run(current((R_M > 300) & (R_M < 360), drop=0.3, d=d))
    assert res.gates.senescence is True and res.gates.harvest is False
    assert emits(res) is False


def test_uniform_drop_trips_gate():
    cur = stack(0.82 + TEXTURE - 0.2 + np.random.default_rng(7).normal(0, 0.01, (N, N)))
    res = run(cur)
    assert res.anomaly.z_baseline < -2
    assert res.gates.regional is True
    assert res.anomaly.regions == []
    assert emits(res) is False


def test_real_problem_emits_in_summer():
    res = run(current((R_M > 300) & (R_M < 360)))
    assert emits(res) is True


def test_overlaps_with_layers():
    patch = (abs(YY - 30) < 10) & (abs(XX - 70) < 10)
    layers = Layers(lowspot=patch.copy(), sandy_soil=np.zeros((N, N), bool), hail=np.ones((N, N), bool))
    f = top(run(current(patch), layers=layers)).features
    assert f.lowspot_overlap > 0.9 and f.sandy_soil_overlap == 0 and f.hail_swath_overlap == pytest.approx(1.0)
    f0 = top(run(current(patch))).features
    assert f0.hail_swath_overlap is None and f0.lowspot_overlap == 0


def test_no_baseline_status():
    res = analyze_stacks(current((R_M > 300) & (R_M < 360)), [], PREV, crop="corn", pivot=PIVOT)
    assert res.status == "no_baseline"
    assert res.anomaly.z_baseline is None and res.anomaly.regions == []
    assert res.indices is not None
