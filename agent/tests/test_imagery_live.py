"""Hits the real Earth Search catalog. Skipped unless FIELDWATCH_LIVE=1 (network, ~10 s)."""
import os

import pytest

from fieldwatch import imagery
from fieldwatch.tools import look_at_field

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.getenv("FIELDWATCH_LIVE") != "1", reason="set FIELDWATCH_LIVE=1")]


def test_latest_scene_and_render():
    bbox = (-96.548, 42.152, -96.532, 42.168)
    scenes = imagery.latest_scenes(bbox)
    assert scenes, "no clear scene in 60 days"
    view = imagery.render_field(bbox, scenes[0])
    assert view.truecolor_png[:8] == b"\x89PNG\r\n\x1a\n"
    assert 0 < view.stats["pixels"][0] < 400
    assert -1 <= view.stats["health_index_median"] <= 1


def test_tool_returns_two_images():
    result = look_at_field(field_id="north-pivot")
    assert result["status"] == "success", result
    kinds = [next(iter(c)) for c in result["content"]]
    assert kinds == ["text", "image", "image"]
