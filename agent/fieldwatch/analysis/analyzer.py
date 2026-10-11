"""analyze(): glue between the scene loader, the numeric core and the overlay writer.

Scene choice: the latest scene on or before `as_of` (30-day look-back) whose clear fraction inside the
10 m-buffered field is at least `min_clear_fraction`. Baseline: for each of the 3 prior seasons the clearest
scene within 10 days of the same calendar date. Previous pass: the latest clear scene before the chosen one
(within 40 days).
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np

from .. import contracts as C
from . import render
from .core import analyze_stacks
from .grid import Grid, IndexStack, Layers, Pivot, SceneData
from .indices import compute_indices
from .layers import LayerProvider, build_layers
from .loader import SceneLoader, StacLoader
from .pivot import resolve_pivot
from .writer import NullWriter, OverlayWriter

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 30
PREVIOUS_DAYS = 40
SEASONS = 3
SEASON_WINDOW_DAYS = 10
MAX_READS_PER_SLOT = 6
CANDIDATE_MAX_CLOUD = 90.0  # tile-wide cloud cover; the real test is the per-pixel clear fraction


class NoScene(Exception):
    pass


def grid_for_polygon(polygon, res_m: float = 10.0, pad_m: float = 20.0) -> tuple[Grid, np.ndarray]:
    """A UTM grid covering the polygon, plus the mask of pixels inside it."""
    from affine import Affine
    from rasterio.features import geometry_mask
    from rasterio.warp import transform as warp_transform

    lons = [p[0] for p in polygon]
    lats = [p[1] for p in polygon]
    zone = int((sum(lons) / len(lons) + 180) // 6) + 1
    crs = f"EPSG:{(32600 if sum(lats) >= 0 else 32700) + zone}"
    xs, ys = warp_transform("EPSG:4326", crs, lons, lats)
    x0 = math.floor((min(xs) - pad_m) / res_m) * res_m
    y1 = math.ceil((max(ys) + pad_m) / res_m) * res_m
    w = math.ceil((max(xs) + pad_m - x0) / res_m)
    h = math.ceil((y1 - (min(ys) - pad_m)) / res_m)
    transform = Affine(res_m, 0, x0, 0, -res_m, y1)
    ring = [list(p) for p in zip(xs, ys)]
    inside = geometry_mask([{"type": "Polygon", "coordinates": [ring + [ring[0]]]}], out_shape=(h, w),
                           transform=transform, invert=True)
    return Grid(shape=(h, w), res_m=res_m, crs=crs, transform=transform), inside


def _bbox(polygon, pad=0.0002):
    lons = [p[0] for p in polygon]
    lats = [p[1] for p in polygon]
    return (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)


def _scene_date(s: C.SceneInfo) -> date:
    return date.fromisoformat(s.datetime[:10])


def _years_ago(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year - n)
    except ValueError:  # Feb 29
        return d.replace(year=d.year - n, day=28)


class _Ctx:
    def __init__(self, loader: SceneLoader, grid: Grid, inside: np.ndarray, req: C.FieldAnalysisRequest):
        self.loader, self.grid, self.inside, self.req = loader, grid, inside, req
        self.bbox = _bbox(req.polygon)

    def read(self, scene: C.SceneInfo) -> tuple[SceneData, IndexStack]:
        data = self.loader.read(scene, self.grid)
        return data, compute_indices(data, self.inside, self.req.interior_buffer_m)

    def first_clear(self, scenes: list[C.SceneInfo], min_clear: float):
        """First scene (in the given order) that is clear enough, else (None, best-seen)."""
        best, tried = None, set()
        for s in scenes:
            if len(tried) >= MAX_READS_PER_SLOT:
                break
            if _scene_date(s) in tried:  # overlapping tiles of one overpass share the same clouds
                continue
            tried.add(_scene_date(s))
            data, st = self.read(s)
            if best is None or st.clear_fraction > best[1].clear_fraction:
                best = (data, st, s)
            if st.clear_fraction >= min_clear:
                return (data, st, s), best
        return None, best


def _season_stack(ctx: _Ctx, chosen: date, years_back: int, min_clear: float) -> IndexStack | None:
    target = _years_ago(chosen, years_back)
    found = ctx.loader.search(ctx.bbox, target - timedelta(days=SEASON_WINDOW_DAYS),
                              target + timedelta(days=SEASON_WINDOW_DAYS), max_cloud=CANDIDATE_MAX_CLOUD, limit=10)
    found.sort(key=lambda s: abs((_scene_date(s) - target).days))
    hit, _ = ctx.first_clear(found, min_clear)
    return hit[1] if hit else None


def _error(req, code, message, retryable=False, scene_id="none") -> C.FieldAnalysisResult:
    return C.FieldAnalysisResult(field=req.field, scene_id=scene_id, date=req.as_of, status="error",
                                 clear_fraction=0.0, error=C.ErrorDetail(code=code, message=message, retryable=retryable))


def analyze(request: C.FieldAnalysisRequest, *, loader: SceneLoader | None = None,
            writer: OverlayWriter | None = None, layers: Layers | None = None,
            layer_provider="auto") -> C.FieldAnalysisResult:
    """Analyze one field as of a date. Never raises: failures come back as status="error".

    Context layers: explicit `layers` win; else `layer_provider(req, grid, inside) -> Layers` (None = none).
    "auto" builds them from the other lanes via interfaces.py, but only on the production path (no injected
    loader), so fake-loader tests and replays never touch the network. Layer failures never fail the analysis."""
    if isinstance(layer_provider, str):
        layer_provider = LayerProvider() if loader is None else None
    try:
        return _analyze(request, loader or StacLoader(), writer or NullWriter(), layers, layer_provider)
    except NoScene as e:
        return _error(request, "NO_SCENE", str(e), retryable=False)
    except (OSError, TimeoutError) as e:  # network, COG reads (rasterio's IO errors subclass OSError)
        return _error(request, "UPSTREAM_ERROR", f"{type(e).__name__}: {e}", retryable=True)
    except Exception as e:  # noqa: BLE001 - contract says errors are data
        return _error(request, "ANALYSIS_ERROR", f"{type(e).__name__}: {e}")


def _analyze(req: C.FieldAnalysisRequest, loader: SceneLoader, writer: OverlayWriter, layers: Layers | None,
             layer_provider=None):
    grid, inside = grid_for_polygon(req.polygon)
    if layers is None and layer_provider is not None:
        layers = build_layers(layer_provider, req, grid, inside)
    ctx = _Ctx(loader, grid, inside, req)
    as_of = date.fromisoformat(req.as_of)

    if req.scene is not None:
        candidates = [req.scene]
    else:
        candidates = loader.search(ctx.bbox, as_of - timedelta(days=LOOKBACK_DAYS), as_of, max_cloud=CANDIDATE_MAX_CLOUD, limit=12)
    if not candidates:
        raise NoScene(f"no Sentinel-2 scene within {LOOKBACK_DAYS} days before {req.as_of}")

    hit, best = ctx.first_clear(candidates, req.min_clear_fraction)
    if hit is None:
        data, st, scene = best
        return C.FieldAnalysisResult(field=req.field, scene_id=scene.scene_id, date=st.date.isoformat(),
                                     status="too_cloudy", clear_fraction=round(st.clear_fraction, 3))
    data, cur, scene = hit
    chosen = cur.date

    prev_scenes = [s for s in loader.search(ctx.bbox, chosen - timedelta(days=PREVIOUS_DAYS),
                                            chosen - timedelta(days=1), max_cloud=CANDIDATE_MAX_CLOUD, limit=8) if _scene_date(s) < chosen]

    def prev_job():
        h, _ = ctx.first_clear(prev_scenes, req.min_clear_fraction)
        return h[1] if h else None

    with ThreadPoolExecutor(SEASONS + 1) as pool:
        pf = pool.submit(prev_job)
        sf = [pool.submit(_season_stack, ctx, chosen, y, req.min_clear_fraction) for y in range(1, SEASONS + 1)]
        previous = pf.result()
        history = [s for s in (f.result() for f in sf) if s is not None]

    core = analyze_stacks(cur, history, previous, crop=req.crop, pivot=resolve_pivot(req.pivot, req.polygon),
                          layers=layers, z_threshold=req.z_threshold, min_region_acres=req.min_region_acres)

    prefix = f"analysis/{req.field.field_id}/{chosen.isoformat()}/s2/"
    bounds = tuple(round(v, 6) for v in grid.bounds_lonlat())
    pngs = {
        "true_color": render.true_color(data, inside),
        "health": render.health(cur, inside),
        "mask": render.mask(cur, inside, core.region_masks),
    }
    # The comparison images are extras: a failure drawing one must never turn a finished analysis into an error.
    extras = {
        "departure": (core.z is not None, lambda: render.departure(core.z, inside)),
        "change": (core.change is not None, lambda: render.change(core.change, inside)),
        "normal": (core.baseline is not None, lambda: render.normal(core.baseline, inside)),
    }
    for name, (available, draw) in extras.items():
        if not available:
            continue
        try:
            pngs[name] = draw()
        except Exception:  # noqa: BLE001
            log.exception("could not draw the %s overlay for %s", name, req.field.field_id)
    refs = {}
    for name, png in pngs.items():
        key = f"{prefix}{name}.png"
        writer.put(key, png, "image/png")
        refs[name] = C.OverlayRef(key=key, bounds=bounds)
    return C.FieldAnalysisResult(
        field=req.field, scene_id=scene.scene_id, date=chosen.isoformat(), status=core.status,
        clear_fraction=round(cur.clear_fraction, 3), indices=core.indices, anomaly=core.anomaly,
        gates=core.gates, overlays=C.Overlays(**refs), analysis_key=prefix,
        comparison=_comparison(chosen, previous, history, core, refs),
    )


def _comparison(chosen: date, previous: IndexStack | None, history: list[IndexStack], core, refs: dict):
    """Dates and color scales behind the departure / change / normal overlays, for captions."""
    return C.OverlayComparison(
        scene_date=chosen.isoformat(),
        previous_date=previous.date.isoformat() if previous is not None else None,
        baseline_dates=sorted(h.date.isoformat() for h in history),
        baseline_method=core.baseline_method, median_departure_ndvi=core.median_departure,
        departure_scale=(C.OverlayScale(stat="z", vmin=-render.DEPARTURE_Z_RANGE, vmax=render.DEPARTURE_Z_RANGE,
                                        unit="standard deviations from this field's normal")
                         if "departure" in refs else None),
        change_scale=(C.OverlayScale(stat="ndvi_diff", vmin=-render.CHANGE_NDVI_RANGE, vmax=render.CHANGE_NDVI_RANGE,
                                     unit="NDVI change since the previous clear pass")
                      if "change" in refs else None),
    )
