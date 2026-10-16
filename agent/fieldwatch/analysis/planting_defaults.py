"""State-typical planting dates for the crop-state machine's default (founder decision: ask the farmer, otherwise use
the state's typical 50%-planted date).

Data: reference/planting_defaults.json, the multi-year median date each state's corn / soybeans reached 50% planted in USDA
NASS Crop Progress. `agent/scripts/build_planting_defaults.py` regenerates it from the NASS Quick Stats API; the
committed copy was built by hand from published summaries (see "provenance" in the file; unverified entries are marked).

The state is found from the field's coordinates with coarse outlines (a few km off at borders and rivers, which moves a
median planting date by well under a day). Fields outside the listed states get no default.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).parent / "reference" / "planting_defaults.json"

# Coarse state outlines, (lon, lat). Neighbors share their river/border chains so a point lands in exactly one.
_NE_SD = [(-98.5, 43.0), (-97.9, 42.8), (-97.2, 42.77), (-96.48, 42.5)]
_MO_RIVER = [(-96.48, 42.5), (-96.28, 42.0), (-96.1, 41.5), (-95.93, 41.27), (-95.85, 40.9), (-95.77, 40.6),
             (-95.6, 40.3), (-95.31, 40.0)]
_IA_SD = [(-96.48, 42.5), (-96.6, 42.8), (-96.55, 43.2), (-96.45, 43.5)]
_IA_MS = [(-91.22, 43.5), (-91.1, 43.0), (-90.65, 42.5), (-90.2, 41.8), (-91.0, 41.4), (-91.1, 41.0), (-91.42, 40.4)]
_IL_MS = [(-91.42, 40.4), (-90.7, 39.2), (-90.2, 38.7), (-89.5, 37.8), (-89.14, 37.0)]
STATES: dict[str, list[tuple[float, float]]] = {
    "NE": [(-104.05, 43.0), *_NE_SD, *_MO_RIVER[1:], (-102.05, 40.0), (-102.05, 41.0), (-104.05, 41.0)],
    "IA": [(-96.45, 43.5), *_IA_MS, (-91.5, 40.6), (-95.77, 40.6), *_MO_RIVER[4::-1], *_IA_SD[1:-1]],
    "SD": [(-104.05, 45.94), (-96.56, 45.94), *_IA_SD[::-1], *_NE_SD[::-1][1:], (-104.05, 43.0)],
    "KS": [(-102.05, 40.0), (-95.31, 40.0), (-94.6, 39.1), (-94.62, 37.0), (-102.05, 37.0)],
    "MN": [(-97.2, 49.0), (-89.5, 48.0), (-90.4, 47.3), (-92.3, 46.65), (-92.3, 46.1), (-92.75, 45.6), (-92.8, 44.75),
           (-92.0, 44.4), (-91.3, 44.0), (-91.22, 43.5), (-96.45, 43.5), (-96.55, 45.9), (-96.8, 47.0)],
    "IL": [(-90.64, 42.5), (-87.8, 42.5), (-87.5, 41.7), (-87.53, 39.4), (-87.6, 38.7), (-88.0, 37.8), (-88.45, 37.1),
           (-89.14, 37.0), (-89.5, 37.8), (-90.2, 38.7), (-90.7, 39.2), (-91.42, 40.4), (-91.0, 41.4), (-90.2, 41.8)],
    "MO": [(-95.77, 40.6), (-91.42, 40.4), (-90.7, 39.2), (-90.2, 38.7), (-89.5, 37.8), (-89.5, 37.3), (-89.1, 36.5),
           (-89.7, 36.0), (-90.37, 36.0), (-90.15, 36.5), (-94.6, 36.5), (-94.6, 39.1), (-95.3, 39.9)],
}


def _inside(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Ray casting."""
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def state_for_point(lon: float, lat: float) -> str | None:
    """Two-letter state of the listed ones containing the point, else None."""
    return next((s for s, ring in STATES.items() if _inside(lon, lat, ring)), None)


@lru_cache(maxsize=1)
def _table() -> dict:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def default_planting_date(crop: str, lon: float, lat: float, year: int) -> date | None:
    """The state's median 50%-planted date for `crop` in `year`, or None (crop not tracked, state not listed)."""
    state = state_for_point(lon, lat)
    entry = _table().get("crops", {}).get(crop, {}).get(state or "")
    if not entry:
        return None
    month, day = (int(x) for x in entry["median_50pct_planted"].split("-"))
    return date(year, month, day)


def provenance(crop: str, state: str) -> dict:
    return _table().get("crops", {}).get(crop, {}).get(state, {})
