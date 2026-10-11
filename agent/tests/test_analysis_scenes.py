"""Scene finder: fake loader, no network. One live test."""
import json
import os
from datetime import date

import numpy as np
import pytest

from fieldwatch import contracts as C
from fieldwatch.analysis import scenes as S
from fieldwatch.analysis.grid import SceneData

POLY = S.polygon_from_bbox((-96.548, 42.152, -96.532, 42.168))


class FakeLoader:
    """scenes: list of (date, cloud fraction of the field, tile id). Counts reads."""

    def __init__(self, scenes):
        self.scenes = scenes
        self.reads = 0

    def search(self, bbox, start, end, max_cloud=100.0, limit=20):
        out = [C.SceneInfo(scene_id=f"S2_{d:%Y%m%d}_{t}", datetime=f"{d.isoformat()}T17:30:00Z", tile_id=t,
                           cloud_cover=cl * 100) for d, cl, t in self.scenes if start <= d <= end]
        return sorted(out, key=lambda s: s.datetime, reverse=True)[:limit]

    def read(self, scene, grid):
        self.reads += 1
        d = date.fromisoformat(scene.datetime[:10])
        cl = next(c for dd, c, t in self.scenes if dd == d and t == scene.tile_id)
        h, w = grid.shape
        nir = np.full((h, w), 0.4, "float32")
        red = np.full((h, w), 0.05, "float32")
        scl = np.full((h, w), 4, np.uint8)
        scl[: int(round(h * cl))] = 9
        return SceneData(scene.scene_id, d, grid, red=red, nir=nir, rededge=nir * 0.7, swir=nir * 0.8, scl=scl,
                         tile_id=scene.tile_id)


SCENES = [
    (date(2025, 6, 1), 0.0, "14TPM"),
    (date(2025, 6, 6), 0.9, "14TPM"),   # cloudy
    (date(2025, 6, 11), 0.0, "14TPM"),  # last clear before the event
    (date(2025, 6, 11), 0.0, "14TQM"),  # same pass, other tile: deduped
    (date(2025, 6, 21), 0.5, "14TPM"),  # cloudy
    (date(2025, 6, 26), 0.0, "14TPM"),  # first clear after
    (date(2025, 7, 6), 0.0, "14TPM"),
]


def test_find_clear_scenes_filters_and_dedupes():
    ld = FakeLoader(SCENES)
    out = S.find_clear_scenes(POLY, date(2025, 6, 1), date(2025, 7, 31), loader=ld)
    assert [s.date.isoformat() for s in out] == ["2025-06-01", "2025-06-11", "2025-06-26", "2025-07-06"]
    assert all(s.clear_fraction >= 0.8 for s in out)
    assert ld.reads == 6  # one read per date, not per tile


def test_include_cloudy_and_threshold():
    out = S.find_clear_scenes(POLY, date(2025, 6, 1), date(2025, 7, 31), loader=FakeLoader(SCENES), include_cloudy=True)
    assert len(out) == 6
    assert [s.clear for s in out] == [True, False, True, False, True, True]
    strict = S.find_clear_scenes(POLY, date(2025, 6, 1), date(2025, 7, 31), loader=FakeLoader(SCENES), min_clear=0.99)
    assert len(strict) == 4


def test_bracket_event():
    br = S.bracket_event(POLY, date(2025, 6, 20), loader=FakeLoader(SCENES))
    assert br.before.date == date(2025, 6, 11) and br.after.date == date(2025, 6, 26)
    assert br.before.scene.scene_id.startswith("S2_20250611")


def test_bracket_missing_side():
    br = S.bracket_event(POLY, date(2025, 6, 20), loader=FakeLoader(SCENES), before_days=3, after_days=45)
    assert br.before is None and br.after.date == date(2025, 6, 26)


def test_polygon_helpers():
    p = S.polygon_from_point(42.16, -96.54, 160)
    lons, lats = [x[0] for x in p], [x[1] for x in p]
    side_m = (max(lats) - min(lats)) * 110_574.0
    assert abs(side_m ** 2 / 4046.856 - 160) < 1
    assert S.polygon_from_geojson({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}) == [(0, 0), (1, 0), (1, 1), (0, 0)]
    with pytest.raises(ValueError):
        S.polygon_from_geojson({"type": "Point", "coordinates": [0, 0]})


def test_draft_scenario_validates():
    sc = S.draft_scenario("flood-x", "demo_farm.json", "f1", date(2025, 6, 26))
    C.Scenario.model_validate(json.loads(sc.model_dump_json()))
    assert sc.as_of == "2025-06-26" and sc.expected.alert


def test_cli_scenario_out(tmp_path, monkeypatch):
    from eval import find_scenes

    monkeypatch.setattr(S, "StacLoader", lambda: FakeLoader(SCENES))
    out = tmp_path / "d.json"
    rc = find_scenes.main(["--bbox", "-96.548,42.152,-96.532,42.168", "--event-date", "2025-06-20",
                           "--scenario-out", str(out)])
    assert rc == 0
    assert C.Scenario.model_validate_json(out.read_text()).as_of == "2025-06-26"


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("FIELDWATCH_LIVE"), reason="needs network")
def test_live_find_scenes():
    out = S.find_clear_scenes(POLY, date(2025, 7, 1), date(2025, 7, 31), min_clear=0.5)
    assert out and all(0.5 <= s.clear_fraction <= 1 for s in out)
