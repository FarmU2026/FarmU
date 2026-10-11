"""Scene finder: which Sentinel-2 dates are clear over a field, and the clear dates either side of an event.

Reuses the analyzer's own definition of "clear" (SCL cloud/shadow mask inside the 10 m-buffered field, from
`indices.compute_indices`) so a date listed here is a date `analyze()` will accept. Same-pass tiles (one date) are
deduplicated: the loader only returns tiles that fully cover the field and they share the same clouds, so the first
tile per date is read. Injectable loader (`SceneLoader`): tests use a fake, no network.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import contracts as C
from .analyzer import CANDIDATE_MAX_CLOUD, _bbox, grid_for_polygon
from .indices import compute_indices
from .loader import SceneLoader, StacLoader

DEFAULT_MIN_CLEAR = 0.8
M2_PER_ACRE = 4046.856


@dataclass
class ClearScene:
    scene_id: str
    date: date
    clear_fraction: float  # inside the 10 m-buffered field, after the cloud/shadow mask
    tile_cloud_cover: float  # whole-tile cloud percent from the catalog (context only)
    tile_id: str
    scene: C.SceneInfo = field(repr=False)
    clear: bool = True  # clear_fraction >= the min_clear it was searched with


@dataclass
class Bracket:
    event_date: date
    before: ClearScene | None
    after: ClearScene | None
    scenes: list[ClearScene]  # every date searched that was kept, oldest first


def _scene_date(s: C.SceneInfo) -> date:
    return date.fromisoformat(s.datetime[:10])


def polygon_from_bbox(bbox) -> list[tuple[float, float]]:
    w, s, e, n = bbox
    return [(w, s), (e, s), (e, n), (w, n), (w, s)]


def polygon_from_point(lat: float, lon: float, acres: float) -> list[tuple[float, float]]:
    """A square of `acres` centered on the point (planning-grade, for a quick look)."""
    side = math.sqrt(acres * M2_PER_ACRE)
    dlat = side / 2 / 110_574.0
    dlon = side / 2 / (111_320.0 * math.cos(math.radians(lat)))
    return polygon_from_bbox((lon - dlon, lat - dlat, lon + dlon, lat + dlat))


def polygon_from_geojson(obj: dict) -> list[tuple[float, float]]:
    """First polygon ring of a Polygon / MultiPolygon / Feature / FeatureCollection."""
    t = obj.get("type")
    if t == "FeatureCollection":
        return polygon_from_geojson(obj["features"][0])
    if t == "Feature":
        return polygon_from_geojson(obj["geometry"])
    if t == "Polygon":
        ring = obj["coordinates"][0]
    elif t == "MultiPolygon":
        ring = obj["coordinates"][0][0]
    else:
        raise ValueError(f"expected a Polygon GeoJSON, got {t!r}")
    return [(p[0], p[1]) for p in ring]


def find_clear_scenes(polygon, start: date, end: date, *, min_clear: float = DEFAULT_MIN_CLEAR,
                      loader: SceneLoader | None = None, interior_buffer_m: float = 10.0,
                      include_cloudy: bool = False, max_workers: int = 4, limit: int = 200) -> list[ClearScene]:
    """Per-date scenes in [start, end] with clear fraction >= `min_clear`, oldest first.

    `include_cloudy=True` also returns the dates below the threshold (marked `clear=False`).
    """
    loader = loader or StacLoader()
    grid, inside = grid_for_polygon(polygon)
    found = loader.search(_bbox(polygon), start, end, max_cloud=CANDIDATE_MAX_CLOUD, limit=limit)
    by_date: dict[date, C.SceneInfo] = {}
    for s in sorted(found, key=lambda s: s.datetime):
        if start <= _scene_date(s) <= end:
            by_date.setdefault(_scene_date(s), s)  # same pass: keep the first tile

    def measure(s: C.SceneInfo) -> ClearScene:
        st = compute_indices(loader.read(s, grid), inside, interior_buffer_m)
        return ClearScene(scene_id=s.scene_id, date=_scene_date(s), clear_fraction=round(st.clear_fraction, 3),
                          tile_cloud_cover=s.cloud_cover, tile_id=s.tile_id, scene=s,
                          clear=st.clear_fraction >= min_clear)

    with ThreadPoolExecutor(max_workers) as pool:
        measured = sorted(pool.map(measure, by_date.values()), key=lambda m: m.date)
    return [m for m in measured if m.clear or include_cloudy]


def bracket_event(polygon, event_date: date, *, min_clear: float = DEFAULT_MIN_CLEAR,
                  before_days: int = 45, after_days: int = 45, loader: SceneLoader | None = None,
                  include_cloudy: bool = False, **kw) -> Bracket:
    """Nearest clear scene on or before the event date and nearest strictly after it (None if none in the window)."""
    scenes = find_clear_scenes(polygon, event_date - timedelta(days=before_days),
                               event_date + timedelta(days=after_days), min_clear=min_clear, loader=loader,
                               include_cloudy=include_cloudy, **kw)
    before = [s for s in scenes if s.clear and s.date <= event_date]
    after = [s for s in scenes if s.clear and s.date > event_date]
    return Bracket(event_date=event_date, before=before[-1] if before else None,
                   after=after[0] if after else None, scenes=scenes)


def draft_scenario(scenario_id: str, farm_fixture: str, field_id: str, as_of: date,
                   alert: bool = True) -> C.Scenario:
    """A validated draft `contracts.Scenario` (fill in top_condition / action_type by hand)."""
    return C.Scenario.model_validate({
        "scenario_id": scenario_id, "farm_fixture": farm_fixture, "field_id": field_id,
        "as_of": as_of.isoformat(),
        "expected": {"alert": alert, "top_condition": None, "action_type": None, "max_trigger_to_message_s": 60},
        "scripted_replies": [{"after": "alert", "text": "YES"}] if alert else [],
    })
