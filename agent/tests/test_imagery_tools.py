"""get_index_history, compare_to_cohort, test_overlap with fake loaders and providers. No network."""
import os
from datetime import date, timedelta

import numpy as np
import pytest

from fieldwatch import contracts as C
from fieldwatch import registry
from fieldwatch.analysis import history, overlap
from fieldwatch.analysis.analyzer import grid_for_polygon
from fieldwatch.analysis.fieldref import resolve_field
from fieldwatch.analysis.grid import SceneData

FIELD = "north-pivot"
AS_OF = "2026-07-18"
INFO = resolve_field(FIELD)
W, S, E, N = INFO.raw["bbox"]


class FakeLoader:
    """Scenes every 5 days. value(scene_id, grid) -> NDVI array; red/nir chosen so (nir-red)/(nir+red) = ndvi."""

    def __init__(self, value, cloudy_dates=()):
        self.value, self.cloudy, self.searches = value, set(cloudy_dates), []

    def search(self, bbox, start, end, max_cloud=100.0, limit=20):
        self.searches.append((start, end))
        out, d = [], start
        while d <= end:
            if (d - date(2020, 1, 1)).days % 5 == 0:
                out.append(C.SceneInfo(scene_id=f"{bbox[0]:.4f}|{d.isoformat()}", datetime=f"{d.isoformat()}T17:00:00Z",
                                       tile_id="15TVG", cloud_cover=5.0))
            d += timedelta(days=1)
        return sorted(out, key=lambda s: s.datetime, reverse=True)[:limit]

    def read(self, scene, grid):
        key, d = scene.scene_id.split("|")
        ndvi = self.value(key, date.fromisoformat(d), grid)
        nir = np.full(grid.shape, 0.5, "float32")
        red = (nir * (1 - ndvi) / (1 + ndvi)).astype("float32")
        flat = np.full(grid.shape, 0.3, "float32")
        scl = np.full(grid.shape, 9 if date.fromisoformat(d) in self.cloudy else 4, "uint8")
        return SceneData(scene_id=scene.scene_id, date=date.fromisoformat(d), grid=grid, red=red, nir=nir,
                         rededge=flat, swir=flat, scl=scl)


def region_geojson(frac=0.2):
    """A square in the middle of the field (lon/lat)."""
    cx, cy, hw, hh = (W + E) / 2, (S + N) / 2, (E - W) * frac, (N - S) * frac
    return {"type": "Polygon", "coordinates": [[[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw, cy + hh],
                                                 [cx - hw, cy + hh], [cx - hw, cy - hh]]]}


def season_value(key, d, grid):
    base = 0.80 if d.year < 2026 else 0.60  # this season is well below prior seasons
    arr = np.full(grid.shape, base, "float32")
    return arr


# ---------- get_index_history ----------


def test_history_points_expected_and_z(monkeypatch):
    out = history.get_index_history(FIELD, "2026-07-01", "2026-07-18", "ndvi", loader=FakeLoader(season_value))
    assert out.status == "ok" and out.field_id == FIELD
    assert out.points and all(p.date <= AS_OF for p in out.points)
    assert [p.date for p in out.points] == sorted(p.date for p in out.points)
    p = out.points[-1]
    assert p.value == pytest.approx(0.60, abs=0.01) and p.expected == pytest.approx(0.80, abs=0.01)
    assert p.baseline_seasons == 3 and p.z < -2


def test_history_as_of_clamps_end():
    ld = FakeLoader(season_value)
    out = history.get_index_history(FIELD, "2026-07-01", "2026-08-30", "ndvi", as_of="2026-07-10", loader=ld)
    assert out.end == "2026-07-10" and all(p.date <= "2026-07-10" for p in out.points)
    assert all(e <= date(2026, 7, 10) or e.year < 2026 for _, e in ld.searches)


def test_history_region_median_and_skips_cloudy():
    bad = region_geojson(0.2)

    def val(key, d, grid):
        from fieldwatch.analysis.fieldref import geometry_mask

        arr = season_value(key, d, grid)
        if d.year == 2026:
            arr = np.where(geometry_mask(bad, grid), arr - 0.2, arr)
        return arr

    clear = history.get_index_history(FIELD, "2026-07-01", AS_OF, "ndvi", loader=FakeLoader(val))
    drop = date.fromisoformat(clear.points[0].date)
    out = history.get_index_history(FIELD, "2026-07-01", AS_OF, "ndvi", region_geometry=bad,
                                    loader=FakeLoader(val, cloudy_dates={drop}))
    assert drop.isoformat() not in [p.date for p in out.points] and len(out.points) == len(clear.points) - 1
    p = out.points[-1]
    assert p.region_value == pytest.approx(p.value - 0.2, abs=0.03)


def test_history_other_indices_and_empty_range():
    out = history.get_index_history(FIELD, "2026-07-01", AS_OF, "ndre", loader=FakeLoader(season_value))
    assert out.index == "ndre" and out.points
    empty = history.get_index_history(FIELD, "2026-07-01", AS_OF, "ndvi",
                                      loader=FakeLoader(season_value, cloudy_dates={date(2026, 7, d) for d in range(1, 19)}))
    assert empty.status == "no_scenes" and empty.points == []


def test_history_unknown_field_is_status_not_exception():
    out = history.get_index_history("nope", "2026-07-01", AS_OF, "ndvi", loader=FakeLoader(season_value))
    assert out.status == "error" and "unknown field" in out.note


# ---------- compare_to_cohort ----------


def neighbor_polys():
    return {f"n{i}": [[W + 0.05 * (i + 1), S], [E + 0.05 * (i + 1), S], [E + 0.05 * (i + 1), N], [W + 0.05 * (i + 1), N]]
            for i in range(4)}


def test_cohort_ok_with_z():
    polys = neighbor_polys()

    def val(key, d, grid):
        mine = abs(float(key) - W) < 1e-3
        return np.full(grid.shape, 0.55 if mine else 0.78, "float32") + (0 if mine else 0.01 * (hash(key) % 3))

    out = history.compare_to_cohort(FIELD, AS_OF, loader=FakeLoader(val), cohort_ids=lambda i, a: list(polys),
                                    cohort_polygons=lambda i, ids: polys)
    assert out.status == "ok" and out.cohort_n == 4
    assert out.field_value == pytest.approx(0.55, abs=0.01) and out.cohort_median > 0.77
    assert out.z_cohort is not None and out.z_cohort < -2


def test_cohort_none_is_explicit_result_not_error():
    out = history.compare_to_cohort(FIELD, AS_OF, loader=FakeLoader(season_value), cohort_ids=lambda i, a: [])
    assert out.status == "no_cohort" and out.cohort_n == 0 and out.z_cohort is None and out.note


def test_cohort_default_ids_come_from_field_context(monkeypatch):
    from fieldwatch import interfaces

    class Ctx:
        def get_field_context(self, fid):
            return C.FieldContext(field_id=fid, built_at="2026-07-01T00:00:00Z", cohort_field_ids=[])

    monkeypatch.setattr(interfaces, "context", lambda: Ctx())
    out = history.compare_to_cohort(FIELD, AS_OF, loader=FakeLoader(season_value))
    assert out.status == "no_cohort"


def test_cohort_too_few_neighbors_has_no_z():
    polys = dict(list(neighbor_polys().items())[:2])
    out = history.compare_to_cohort(FIELD, AS_OF, loader=FakeLoader(season_value), cohort_ids=lambda i, a: list(polys),
                                    cohort_polygons=lambda i, ids: polys)
    assert out.status == "ok" and out.cohort_n == 2 and out.z_cohort is None


# ---------- test_overlap ----------


class FakeLayers:
    def __init__(self, geom):
        self.geom = geom

    def get_layer(self, field_id, layer, polygon):
        return self.geom if layer == "lowspot" else None


def half_field():
    cx = (W + E) / 2
    return {"type": "Polygon", "coordinates": [[[W, S], [cx, S], [cx, N], [W, N], [W, S]]]}


def edge_region():
    # strip along the west edge, 20 m wide
    return {"type": "Polygon", "coordinates": [[[W, S + 0.002], [W + 0.0003, S + 0.002], [W + 0.0003, N - 0.002],
                                                 [W, N - 0.002], [W, S + 0.002]]]}


def test_overlap_provider_layer():
    reg = {"type": "Polygon", "coordinates": [[[W + 0.001, S + 0.004], [W + 0.004, S + 0.004], [W + 0.004, S + 0.008],
                                               [W + 0.001, S + 0.008], [W + 0.001, S + 0.004]]]}
    out = overlap.test_overlap(FIELD, "lowspot", region_geometry=reg, layers=FakeLayers(half_field()))
    assert out.status == "ok" and out.overlap == pytest.approx(1.0, abs=0.01)
    assert out.expected_by_chance == pytest.approx(0.5, abs=0.02)
    assert out.enrichment == pytest.approx(2.0, abs=0.1) and out.p_value < 0.01
    assert out.region_acres > 0


def test_overlap_field_edge_builtin():
    out = overlap.test_overlap(FIELD, "field_edge", region_geometry=edge_region())
    assert out.status == "ok" and out.overlap > 0.9 and out.expected_by_chance < 0.3 and out.p_value < 0.01
    inner = overlap.test_overlap(FIELD, "field_edge", region_geometry=region_geojson(0.1))
    assert inner.overlap == 0 and inner.p_value > 0.5


def test_overlap_pivot_ring_builtin_and_ring_region():
    # a region hugging the circle's outer edge on the east side
    cx, cy = (W + E) / 2, (S + N) / 2
    r_deg_lon = (E - W) / 2
    reg = {"type": "Polygon", "coordinates": [[[cx + r_deg_lon * 0.85, cy - 0.001], [cx + r_deg_lon * 0.98, cy - 0.001],
                                               [cx + r_deg_lon * 0.98, cy + 0.001], [cx + r_deg_lon * 0.85, cy + 0.001],
                                               [cx + r_deg_lon * 0.85, cy - 0.001]]]}
    out = overlap.test_overlap(FIELD, "pivot_ring", region_geometry=reg)
    assert out.status == "ok" and out.overlap > 0.5 > out.expected_by_chance
    center = overlap.test_overlap(FIELD, "pivot_ring", region_geometry=region_geojson(0.05))
    assert center.overlap == 0


def test_overlap_layer_unavailable_is_not_error():
    out = overlap.test_overlap(FIELD, "soil_sandy", region_geometry=region_geojson())
    assert out.status == "layer_unavailable" and out.overlap is None and out.note
    empty = overlap.test_overlap(FIELD, "lowspot", region_geometry=region_geojson(), layers=FakeLayers(None))
    assert empty.status == "layer_unavailable"


def test_overlap_region_id_resolution_and_inputs():
    seen = {}

    def resolver(info, rid, as_of):
        seen["args"] = (info.field_id, rid, as_of)
        return edge_region() if rid == "r1" else None

    out = overlap.test_overlap(FIELD, "field_edge", region_id="r1", as_of=AS_OF, region_resolver=resolver)
    assert out.status == "ok" and seen["args"] == (FIELD, "r1", AS_OF)
    assert overlap.test_overlap(FIELD, "field_edge", region_id="zz", as_of=AS_OF, region_resolver=resolver).status == "region_not_found"
    assert overlap.test_overlap(FIELD, "field_edge").status == "error"
    assert overlap.test_overlap(FIELD, "field_edge", region_id="r1").status == "error"  # as_of needed
    assert overlap.test_overlap("nope", "field_edge", region_geometry=edge_region()).status == "error"


# ---------- registry ----------


def test_registered_read_tools_with_models():
    for name in ("get_index_history", "compare_to_cohort", "test_overlap"):
        spec = registry.BY_NAME[name]
        assert spec.access == "read" and spec.workstream == 1 and not spec.stub
        assert "field_id" in spec.input_model.model_json_schema()["properties"]
    assert "as_of" in registry.CohortIn.model_fields
    from fieldwatch.agents import SPECIALIST_TOOLS

    assert {"get_index_history", "compare_to_cohort", "test_overlap"} <= set(SPECIALIST_TOOLS["imagery"])


def test_registry_wrapper_runs_without_network(monkeypatch):
    import fieldwatch.analysis.history as H

    monkeypatch.setattr(H, "StacLoader", lambda: FakeLoader(season_value))
    out = registry.BY_NAME["get_index_history"].fn(field_id=FIELD, start="2026-07-01", end=AS_OF, index="ndvi")
    assert out.status == "ok"


# ---------- live ----------

live = pytest.mark.skipif(os.getenv("FIELDWATCH_LIVE") != "1", reason="set FIELDWATCH_LIVE=1")


@pytest.mark.live
@live
def test_live_get_index_history():
    out = history.get_index_history(FIELD, "2025-07-01", "2025-07-31", "ndvi", max_points=3)
    assert out.status == "ok" and out.points


@pytest.mark.live
@live
def test_live_compare_to_cohort():
    out = history.compare_to_cohort(FIELD, "2025-07-18", cohort_ids=lambda i, a: [])
    assert out.status == "no_cohort"


@pytest.mark.live
@live
def test_live_test_overlap_pivot_ring():
    out = overlap.test_overlap(FIELD, "pivot_ring", region_geometry=region_geojson(0.1))
    assert out.status == "ok"
