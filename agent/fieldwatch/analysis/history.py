"""Imagery tools: `get_index_history` and `compare_to_cohort`. Pure analysis code (no web, no AWS).

Baseline is the same weeks in prior seasons (docs/decisions.md), and every call is as-of a date so a replay never
sees later scenes. Scenes come from an injectable `SceneLoader` (default: Earth Search STAC).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .analyzer import CANDIDATE_MAX_CLOUD, SEASON_WINDOW_DAYS, SEASONS, _bbox, _scene_date, _years_ago, grid_for_polygon
from .fieldref import FieldInfo, RegionResolver, geometry_mask, parse_date, resolve_field, resolve_region
from .grid import IndexStack
from .indices import compute_indices
from .loader import SceneLoader, StacLoader
from .regions import SEASON_SD_FLOOR

IndexName = Literal["ndvi", "ndre", "ndmi"]
MIN_CLEAR = 0.7
MAX_POINTS = 16
COHORT_MAX = 8
COHORT_MIN_N = 3
COHORT_SD_FLOOR = 0.03  # NDVI units
LOOKBACK_DAYS = 30
Z_CAP = 10.0


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndexPoint(_Out):
    date: str
    scene_id: str
    value: float = Field(description="Field median of the index on this clear pass")
    region_value: float | None = Field(default=None, description="Median inside the problem region, when given")
    expected: float | None = Field(default=None, description="Same-weeks mean of the field median in prior seasons")
    z: float | None = Field(default=None, description="(value - expected) / spread between seasons; None without history")
    clear_fraction: float
    baseline_seasons: int = 0


class IndexHistory(_Out):
    field_id: str
    index: IndexName
    start: str
    end: str
    as_of: str
    status: Literal["ok", "no_scenes", "error"]
    points: list[IndexPoint] = Field(default_factory=list)
    region_id: str | None = None
    note: str | None = None


class CohortComparison(_Out):
    field_id: str
    as_of: str
    status: Literal["ok", "no_cohort", "no_scene", "error"]
    scene_date: str | None = None
    field_value: float | None = Field(default=None, description="Field median NDVI on the scene date")
    cohort_median: float | None = None
    cohort_n: int = 0
    z_cohort: float | None = Field(default=None, description="None unless at least 3 cohort fields were read")
    cohort_field_ids: list[str] = Field(default_factory=list)
    note: str | None = None


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 4)


def _median(st: IndexStack, index: str, mask: np.ndarray | None = None) -> float | None:
    a = getattr(st, index)
    if mask is not None:
        a = np.where(mask, a, np.nan)
    a = a[np.isfinite(a)]
    return float(np.median(a)) if a.size else None


def _clear_stack(loader: SceneLoader, scenes, grid, inside, min_clear: float, max_reads: int = 4):
    """First scene (given order, one per date) with enough clear pixels -> (SceneInfo, IndexStack) or None."""
    seen: set[date] = set()
    for s in scenes:
        d = _scene_date(s)
        if d in seen:
            continue
        if len(seen) >= max_reads:
            break
        seen.add(d)
        st = compute_indices(loader.read(s, grid), inside)
        if st.clear_fraction >= min_clear:
            return s, st
    return None


def _prior_season_medians(loader: SceneLoader, info: FieldInfo, grid, inside, d: date, index: str) -> list[float]:
    bbox = _bbox(info.polygon)
    out = []
    for y in range(1, SEASONS + 1):
        target = _years_ago(d, y)
        found = loader.search(bbox, target - timedelta(days=SEASON_WINDOW_DAYS), target + timedelta(days=SEASON_WINDOW_DAYS),
                              max_cloud=CANDIDATE_MAX_CLOUD, limit=10)
        found.sort(key=lambda s: abs((_scene_date(s) - target).days))
        hit = _clear_stack(loader, found, grid, inside, MIN_CLEAR)
        m = _median(hit[1], index) if hit else None
        if m is not None:
            out.append(m)
    return out


def _spaced(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    idx = np.linspace(0, len(items) - 1, n).round().astype(int)
    return [items[i] for i in sorted(set(idx))]


def get_index_history(field_id: str, start: str, end: str, index: IndexName = "ndvi", *, as_of: str | None = None,
                      region_id: str | None = None, region_geometry: dict | None = None,
                      loader: SceneLoader | None = None, region_resolver: RegionResolver | None = None,
                      min_clear_fraction: float = MIN_CLEAR, max_points: int = MAX_POINTS) -> IndexHistory:
    """Field-median (and problem-region median) index per clear pass between start and end, oldest first.

    `end` is clamped to `as_of` when given, so a replay never reads past its date. `expected` and `z` compare each
    pass with the same weeks in up to 3 prior seasons. Data problems come back as status, not exceptions."""
    as_of = as_of or end
    last = min(parse_date(end), parse_date(as_of))
    base = IndexHistory(field_id=field_id, index=index, start=start, end=last.isoformat(), as_of=as_of,
                        status="no_scenes", region_id=region_id)
    try:
        info = resolve_field(field_id)
        base.field_id = info.field_id
        loader = loader or StacLoader()
        grid, inside = grid_for_polygon(info.polygon)
        region = resolve_region(info, region_id, region_geometry, as_of, region_resolver)
        if (region_id or region_geometry) and region is None:
            base.note = f"region {region_id!r} not found as of {as_of}; field-only history returned"
        rmask = geometry_mask(region, grid) if region else None
        scenes = loader.search(_bbox(info.polygon), parse_date(start), last, max_cloud=CANDIDATE_MAX_CLOUD, limit=40)
        by_date: dict[date, list] = {}
        for s in sorted(scenes, key=_scene_date):
            by_date.setdefault(_scene_date(s), []).append(s)
        points: list[IndexPoint] = []
        for d, group in _spaced(sorted(by_date.items()), max_points):
            hit = _clear_stack(loader, group, grid, inside, min_clear_fraction, max_reads=len(group))
            if hit is None:
                continue
            s, st = hit
            v = _median(st, index)
            if v is None:
                continue
            prior = _prior_season_medians(loader, info, grid, inside, d, index)
            expected = float(np.mean(prior)) if prior else None
            z = None
            if expected is not None:
                sd = max(float(np.std(prior, ddof=1)) if len(prior) >= 2 else 0.0, SEASON_SD_FLOOR)
                z = round(float(np.clip((v - expected) / sd, -Z_CAP, Z_CAP)), 2)
            points.append(IndexPoint(date=d.isoformat(), scene_id=s.scene_id, value=round(v, 4),
                                     region_value=None if rmask is None else _round(_median(st, index, rmask)),
                                     expected=_round(expected), z=z, clear_fraction=round(st.clear_fraction, 3),
                                     baseline_seasons=len(prior)))
        base.points = points
        if points:
            base.status = "ok"
        else:
            base.note = base.note or "no clear passes in the range"
        return base
    except ValueError as e:
        base.status, base.note = "error", str(e)
        return base
    except (OSError, TimeoutError) as e:
        base.status, base.note = "error", f"upstream error: {type(e).__name__}: {e}"
        return base


# ---------- cohort ----------

CohortPolygons = Callable[[FieldInfo, list[str]], "dict[str, list[list[float]]]"]


def _default_cohort_ids(info: FieldInfo, as_of: str) -> list[str]:
    from .. import interfaces

    return list(interfaces.context().get_field_context(info.field_id).cohort_field_ids)


def _default_cohort_polygons(info: FieldInfo, ids: list[str]) -> dict[str, list[list[float]]]:
    """Cohort ids that are fields of this farm resolve here. Neighbor ids from USDA CSB have no polygon seam yet
    (proposed: ContextAPI.get_cohort_polygons), so they are skipped."""
    from .. import farmdata

    out = {}
    for f in farmdata.farm(info.field_id)["fields"]:
        if f.get("id") in ids and f.get("polygon"):
            out[f["id"]] = f["polygon"]
    return out


def compare_to_cohort(field_id: str, as_of: str, *, loader: SceneLoader | None = None,
                      cohort_ids: Callable[[FieldInfo, str], list[str]] | None = None,
                      cohort_polygons: CohortPolygons | None = None,
                      min_clear_fraction: float = MIN_CLEAR) -> CohortComparison:
    """Field NDVI vs same-crop neighbors on the same pass (latest clear scene on or before as_of).

    No neighbors is a normal answer (status "no_cohort"), not an error. z_cohort needs at least 3 neighbors."""
    out = CohortComparison(field_id=field_id, as_of=as_of, status="no_cohort")
    try:
        info = resolve_field(field_id)
        out.field_id = info.field_id
        all_ids = [i for i in (cohort_ids or _default_cohort_ids)(info, as_of) if i != info.field_id]
        if not all_ids:
            out.note = "no same-crop neighbor fields on record for this field"
            return out
        polys = (cohort_polygons or _default_cohort_polygons)(info, all_ids[:COHORT_MAX * 3])
        ids = [i for i in all_ids if i in polys][:COHORT_MAX]
        if not ids:
            out.note = f"{len(all_ids)} neighbor ids on record but no boundaries available to read them"
            return out
        loader = loader or StacLoader()
        end = parse_date(as_of)
        grid, inside = grid_for_polygon(info.polygon)
        scenes = loader.search(_bbox(info.polygon), end - timedelta(days=LOOKBACK_DAYS), end,
                               max_cloud=CANDIDATE_MAX_CLOUD, limit=12)
        scenes.sort(key=_scene_date, reverse=True)
        hit = _clear_stack(loader, scenes, grid, inside, min_clear_fraction, max_reads=6)
        if hit is None:
            out.status, out.note = "no_scene", f"no clear pass over the field in the {LOOKBACK_DAYS} days to {as_of}"
            return out
        scene, st = hit
        sd = _scene_date(scene)
        out.scene_date = sd.isoformat()
        out.field_value = _round(_median(st, "ndvi"))
        vals, used = [], []
        for cid in ids:
            g2, in2 = grid_for_polygon(polys[cid])
            cand = loader.search(_bbox(polys[cid]), sd - timedelta(days=3), min(sd + timedelta(days=3), end),
                                 max_cloud=CANDIDATE_MAX_CLOUD, limit=6)
            cand.sort(key=lambda s: abs((_scene_date(s) - sd).days))
            h2 = _clear_stack(loader, cand, g2, in2, min_clear_fraction, max_reads=3)
            m = _median(h2[1], "ndvi") if h2 else None
            if m is not None:
                vals.append(m)
                used.append(cid)
        out.cohort_n, out.cohort_field_ids = len(vals), used
        if not vals:
            out.note = "neighbor fields had no clear pass near the same date"
            return out
        out.status = "ok"
        med = float(np.median(vals))
        out.cohort_median = round(med, 4)
        if len(vals) >= COHORT_MIN_N and out.field_value is not None:
            mad = 1.4826 * float(np.median(np.abs(np.array(vals) - med)))
            out.z_cohort = round(float(np.clip((out.field_value - med) / max(mad, COHORT_SD_FLOOR), -Z_CAP, Z_CAP)), 2)
        else:
            out.note = f"only {len(vals)} neighbor(s) read; need {COHORT_MIN_N} for a z-score"
        return out
    except ValueError as e:
        out.status, out.note = "error", str(e)
        return out
    except (OSError, TimeoutError) as e:
        out.status, out.note = "error", f"upstream error: {type(e).__name__}: {e}"
        return out
