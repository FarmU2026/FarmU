"""Context layers rasterized onto the analyzer grid. No network except the one live test."""
import json
import os
from datetime import date

import numpy as np
import pytest

from fieldwatch import contracts as C
from fieldwatch.analysis import analyze
from fieldwatch.analysis.analyzer import grid_for_polygon
from fieldwatch.analysis.grid import Layers
from fieldwatch.analysis import layers as L
from fieldwatch.analysis.writer import NullWriter

from test_analysis_pipeline import POLY, FakeLoader, req, scenes

GRID, INSIDE = grid_for_polygon(POLY)


def _box_geojson(lon0, lat0, lon1, lat1, **props):
    ring = [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]
    return {"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [ring]}}


def test_rasterize_geojson_covers_the_right_pixels():
    # the west half of the field
    fc = {"type": "FeatureCollection", "features": [_box_geojson(-96.55, 42.15, -96.54, 42.17, sandy=True)]}
    m = L.rasterize_geojson(fc, GRID)
    assert m.shape == GRID.shape and m.dtype == bool
    cols = np.where(m.any(axis=0))[0]
    lon_edge = GRID.pix_to_lonlat(np.full(2, GRID.shape[0] // 2), cols[[0, -1]])[0]
    assert abs(lon_edge[1] - (-96.54)) < 0.0003 and lon_edge[0] < -96.548
    frac_in_field = (m & INSIDE).sum() / INSIDE.sum()
    assert 0.4 < frac_in_field < 0.6


def test_rasterize_select_and_empty():
    fc = {"type": "FeatureCollection", "features": [_box_geojson(-96.55, 42.15, -96.53, 42.17, sandy=False)]}
    assert not L.rasterize_geojson(fc, GRID, select=lambda p: p.get("sandy")).any()


def test_rasterize_on_synthetic_grid():
    from fieldwatch.analysis.grid import Grid
    g = Grid(shape=(50, 50), res_m=10.0, origin=(-96.54, 42.16))
    lon0, lat0 = g.pix_to_lonlat(10, 10)
    lon1, lat1 = g.pix_to_lonlat(29, 29)
    m = L.rasterize_geojson(_box_geojson(float(lon0) - 1e-6, float(lat1) - 1e-6, float(lon1) + 1e-6,
                                         float(lat0) + 1e-6), g)
    assert m[10:30, 10:30].all() and m.sum() == 400


def test_resample_mask_finer_raster():
    from affine import Affine
    # 1 m raster over the NW 100 m x 100 m of the grid: left half set, with 255 nodata stripe
    gx, gy = GRID.transform.c, GRID.transform.f
    src = np.zeros((100, 100), "uint8")
    src[:, :50] = 1
    src[:, 90:] = 255
    out = L.resample_mask(src, Affine(1, 0, gx, 0, -1, gy), GRID.crs, GRID)
    assert out[:10, :5].all() and not out[:10, 5:10].any()
    assert not out[10:, :].any()


def test_lowspot_and_sandy_from_artifact_keys(tmp_path):
    import rasterio
    from affine import Affine
    gx, gy = GRID.transform.c, GRID.transform.f
    data = np.full((100, 100), 255, "uint8")
    data[:, :] = 0
    data[:50, :] = 1
    tif = tmp_path / "lowspot.tif"
    with rasterio.open(tif, "w", driver="GTiff", height=100, width=100, count=1, dtype="uint8", crs=GRID.crs,
                       transform=Affine(1, 0, gx, 0, -1, gy), nodata=255) as ds:
        ds.write(data, 1)
    low = L.lowspot_from_key(str(tif), GRID)
    assert low[:5, :10].all() and not low[5:10, :10].any()

    zones = tmp_path / "zones.geojson"
    zones.write_text(json.dumps({"type": "FeatureCollection", "features": [
        _box_geojson(-96.55, 42.15, -96.54, 42.17, sandy=True, mukey="1"),
        _box_geojson(-96.54, 42.15, -96.53, 42.17, sandy=False, mukey="2")]}))
    sandy = L.sandy_from_key(str(zones), GRID)
    assert 0.4 < (sandy & INSIDE).sum() / INSIDE.sum() < 0.6


def test_overlap_fraction():
    region = np.zeros((10, 10), bool)
    region[:, :4] = True
    layer = np.zeros((10, 10), bool)
    layer[:, :2] = True
    assert L.overlap_fraction(region, layer) == 0.5
    assert L.overlap_fraction(region, None) is None
    assert L.overlap_fraction(~region & ~region, layer) == 0.0


def test_provider_tolerates_failing_sources(caplog):
    def boom(*a):
        raise RuntimeError("3DEP down")

    good = np.zeros(GRID.shape, bool)
    good[5:9] = True
    p = L.LayerProvider(lowspot=boom, sandy_soil=lambda *a: good, hail=lambda *a: np.zeros((2, 2), bool))
    with caplog.at_level("WARNING"):
        out = p(req(), GRID, INSIDE)
    assert out.lowspot is None and out.hail is None and out.sandy_soil is not None
    assert "lowspot unavailable" in caplog.text and "hail unavailable" in caplog.text
    assert L.build_layers(boom, req(), GRID, INSIDE) is None


def test_default_hail_source_absent_until_interface_lands():
    # nothing in interfaces.py exposes hail geometry yet, so the layer must be None, not an import of hazards
    from fieldwatch import interfaces
    if hasattr(interfaces, "hazards"):
        pytest.skip("hazards() interface exists now")
    assert L.hail_source(req(), GRID, INSIDE) is None


def _ring_layers(req_, grid, inside):
    h, w = grid.shape
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(yy - h / 2, xx - w / 2)
    ring = (r > 0.30 * h) & (r < 0.36 * h)
    return Layers(lowspot=ring, sandy_soil=np.zeros(grid.shape, bool), hail=None)


def test_analyze_uses_provider_and_scorer_keys_can_fire():
    loader = FakeLoader(scenes(), problem_on=date(2026, 7, 18))
    res = analyze(req(), loader=loader, writer=NullWriter(), layer_provider=_ring_layers)
    C.FieldAnalysisResult.model_validate(res.model_dump())
    assert res.status == "ok" and res.anomaly.regions
    f = res.anomaly.regions[0].features
    assert f.lowspot_overlap > 0.5  # scorer rule: lowspot_overlap >= 0.5 -> lowspot_overlap_high
    assert f.sandy_soil_overlap == 0.0 and f.hail_swath_overlap is None
    base = analyze(req(), loader=loader, writer=NullWriter())  # no provider under a fake loader
    assert base.anomaly.regions[0].features.lowspot_overlap == 0.0


def test_analyze_survives_provider_failure():
    def boom(*a):
        raise RuntimeError("layer source exploded")

    res = analyze(req(), loader=FakeLoader(scenes(), problem_on=date(2026, 7, 18)), writer=NullWriter(),
                  layer_provider=boom)
    assert res.status == "ok" and res.anomaly.regions[0].features.lowspot_overlap == 0.0


@pytest.mark.live
def test_live_layers_for_demo_field():
    """Real 3DEP + SDA via interfaces.context() for the demo field's polygon (network)."""
    if os.getenv("FIELDWATCH_LIVE") != "1":
        pytest.skip("set FIELDWATCH_LIVE=1")
    out = L.LayerProvider()(req(), GRID, INSIDE)
    assert out.lowspot is not None or out.sandy_soil is not None
    for m in (out.lowspot, out.sandy_soil):
        if m is not None:
            assert m.shape == GRID.shape and (m & ~INSIDE).sum() <= 0.1 * m.sum() + 1
