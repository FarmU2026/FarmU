"""The numeric core: index stacks in, contract pieces out. Pure numpy; no network, no AWS, no web.

    analyze_stacks(current, history, previous, crop=..., pivot=..., layers=..., season=..., planting=...) -> CoreResult

`history` is one IndexStack per prior season (same weeks), `previous` the last clear pass before `current`,
`season` other clear passes earlier this season. All stacks share one Grid.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
from rasterio.features import shapes
from scipy import ndimage
from shapely.geometry import Polygon as ShPolygon
from shapely.geometry import mapping

from .. import contracts as C
from . import gates as G
from . import harvest as H
from . import patterns, phenology, regions
from .grid import Grid, IndexStack, Layers, Pivot

BASELINE_SENESCENT_NDVI = 0.70  # [design] a late-season prior-season baseline below this was already drying down


@dataclass
class CoreResult:
    status: str  # "ok" | "no_baseline"
    indices: C.Indices
    anomaly: C.AnomalySummary
    gates: C.Gates
    z: np.ndarray | None = None                       # smoothed z map (NaN outside valid pixels)
    region_masks: list[np.ndarray] = field(default_factory=list)  # aligned with anomaly.regions
    # Arrays the overlays are drawn from (render only; nothing here feeds the decision):
    baseline: np.ndarray | None = None                # per-pixel NDVI expected for this date (regions.baseline_mean)
    change: np.ndarray | None = None                  # NDVI now - previous pass where both are clear, else NaN
    median_departure: float | None = None             # median of (NDVI now - baseline) over valid pixels
    baseline_method: str | None = None                # "median" (>= 3 seasons) or "mean"
    crop_state: C.CropState | None = None


def _stats(a: np.ndarray, valid: np.ndarray) -> C.IndexStats:
    v = a[valid & np.isfinite(a)]
    if v.size == 0:
        return C.IndexStats(mean=0.0, p10=0.0, p50=0.0, p90=0.0)
    p10, p50, p90 = np.percentile(v, [10, 50, 90])
    return C.IndexStats(mean=round(float(v.mean()), 3), p10=round(float(p10), 3),
                        p50=round(float(p50), 3), p90=round(float(p90), 3))


def index_stats(st: IndexStack) -> C.Indices:
    return C.Indices(ndvi=_stats(st.ndvi, st.valid), ndre=_stats(st.ndre, st.valid), ndmi=_stats(st.ndmi, st.valid))


def region_geometry(mask: np.ndarray, grid: Grid) -> dict:
    """GeoJSON Polygon (lon/lat) of the largest piece of the mask, holes kept."""
    polys = []
    for geom, val in shapes(mask.astype("uint8"), mask=mask, connectivity=8):
        if val:
            polys.append(ShPolygon(geom["coordinates"][0], geom["coordinates"][1:]))
    poly = max(polys, key=lambda p: p.area).simplify(0.4)  # pixel units
    def ring(coords):
        xs, ys = zip(*coords)
        lons, lats = grid.pix_to_lonlat(np.array(ys) - 0.5, np.array(xs) - 0.5)  # corner -> center coordinates
        return [[round(float(a), 6), round(float(b), 6)] for a, b in zip(lons, lats)]
    out = mapping(poly)
    return {"type": "Polygon", "coordinates": [ring(r) for r in [out["coordinates"][0], *out["coordinates"][1:]]]}


def _centroid(mask: np.ndarray, grid: Grid) -> tuple[float, float]:
    rr, cc = np.nonzero(mask)
    lon, lat = grid.pix_to_lonlat(rr.mean(), cc.mean())
    return round(float(lon), 6), round(float(lat), 6)


def where_in_field(mask: np.ndarray, inside: np.ndarray, grid: Grid) -> str | None:
    """Plain-words location of a region, computed in lon/lat (so raster orientation can't flip it): which third of
    the field's north-south and east-west extent its center falls in. Agents use this instead of guessing a
    direction from coordinates (Oct 11: an agent called a south-west patch the "north-east corner")."""
    rr, cc = np.nonzero(inside)
    if not len(rr) or not mask.any():
        return None
    if mask.sum() >= 0.5 * len(rr):
        return "much of the field"
    lons, lats = grid.pix_to_lonlat(rr, cc)
    lon, lat = _centroid(mask, grid)
    x = (lon - lons.min()) / max(float(lons.max() - lons.min()), 1e-9)
    y = (lats.max() - lat) / max(float(lats.max() - lats.min()), 1e-9)  # 0 = north edge
    ns = "north" if y < 1 / 3 else "south" if y > 2 / 3 else ""
    ew = "west" if x < 1 / 3 else "east" if x > 2 / 3 else ""
    part = f"{ns}-{ew}" if ns and ew else ns or ew
    return f"{part} part of the field" if part else "middle of the field"


def _previous_clean(previous: IndexStack, hist_ndvi: list[np.ndarray], z_threshold: float, min_px: int) -> bool:
    """No problem region on the previous pass against the same baseline (True without a baseline)."""
    if not hist_ndvi:
        return True
    diff = np.where(previous.valid, previous.ndvi - regions.baseline_mean(hist_ndvi), np.nan)
    masks, zs = regions.find_regions(regions.pixel_z(diff), z_threshold, min_px)
    return not any(regions.strong_enough(float(m.sum()) * previous.grid.pixel_acres, float(-np.nanmean(zs[m])), z_threshold)
                   for m in masks)


def _crop_state(current: IndexStack, history: list[IndexStack], previous: IndexStack | None, season: list[IndexStack], *,
                crop: str, planting: phenology.Planting, indices: C.Indices, legacy_senescence: bool,
                z_threshold: float, min_region_acres: float):
    """Gather the evidence (season curve, harvest blocks, sudden drop) and ask phenology.classify_state.
    Returns (CropState, hail mask or None)."""
    pool = {s.scene_id: s for s in [*season, *([previous] if previous is not None else [])] if s.date < current.date}
    earlier = sorted(pool.values(), key=lambda s: s.date)
    curve = [(s.date, v) for s in earlier if (v := phenology.field_median(s.ndvi, s.valid)) is not None]
    p50 = indices.ndvi.p50 if current.valid.any() else None
    peak = max([v for _, v in curve] + ([p50] if p50 is not None else []), default=None)
    notes = list(planting.notes)
    late = crop != "alfalfa" and (legacy_senescence or phenology.late_window(crop, current.date, planting.gdd))
    ref = None
    if earlier:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            ref = np.nanmax(np.stack([s.ndvi for s in earlier]), axis=0)
    finding = H.detect_harvest(current, ref, late=late)
    sudden = None
    if late and previous is not None and not (finding and (finding.harvested or finding.in_progress)):
        min_px = max(1, int(np.ceil(min_region_acres / current.grid.pixel_acres)))
        clean = _previous_clean(previous, [h.ndvi for h in history], z_threshold, min_px)
        sudden = H.detect_sudden_drop(current, previous, min_acres=min_region_acres, previous_clean=clean)
    if late and history:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            base_med = float(np.nanmedian(regions.baseline_mean([h.ndvi for h in history])))
        if base_med < BASELINE_SENESCENT_NDVI:
            notes.append("prior seasons were already drying down on these dates, so same-week comparison is not used")
    state = phenology.classify_state(phenology.StateEvidence(
        crop=crop, as_of=current.date, p50=p50, peak=peak, earlier=curve, gdd=planting.gdd, planting_date=planting.date,
        planting_source=planting.source, legacy_senescence=legacy_senescence, harvest=finding, sudden_drop=sudden,
        notes=notes))
    hail = sudden.mask if sudden is not None and state.hail_only and phenology.HAIL_EXCEPTION in state.reasons else None
    return state, hail


def analyze_stacks(current: IndexStack, history: list[IndexStack], previous: IndexStack | None, *,
                   crop: str = "corn", pivot: Pivot | None = None, layers: Layers | None = None,
                   z_threshold: float = 2.0, min_region_acres: float = 1.2,
                   season: list[IndexStack] | None = None, planting: phenology.Planting | None = None) -> CoreResult:
    """`season`: other clear passes earlier this season (the season curve and the harvest reference); `planting`: the
    planting date, its source and the degree days since it. Both optional: without them the state is judged from the
    dates and the imagery alone. Regions below the same-week baseline are still described when the crop state is past
    diagnosis, but `crop_state.diagnosable` is False and nothing downstream may name a cause from them."""
    grid = current.grid
    indices = index_stats(current)
    delta = None
    change = None
    if previous is not None:
        both = current.valid & previous.valid
        change = np.where(both, current.ndvi - previous.ndvi, np.nan).astype("float32")
        if both.any():
            delta = round(float(np.median(current.ndvi[both] - previous.ndvi[both])), 3)
    senescence, harvest = G.seasonal(crop, current.date, delta_since_last=delta, ndvi_p50=indices.ndvi.p50)
    state, hail_mask = _crop_state(current, history, previous, season or [], crop=crop,
                                   planting=planting or phenology.Planting(), indices=indices,
                                   legacy_senescence=senescence, z_threshold=z_threshold,
                                   min_region_acres=min_region_acres)
    senescence, harvest = G.fold_state(senescence, harvest, state.state)

    if not history:
        return CoreResult(
            status="no_baseline", indices=indices,
            anomaly=C.AnomalySummary(field_score=0.0, z_baseline=None, z_cohort=None, delta_since_last=delta, regions=[]),
            gates=C.Gates(senescence=senescence, harvest=harvest, regional=False), change=change, crop_state=state,
        )

    hist_ndvi = [h.ndvi for h in history]
    base = regions.baseline_mean(hist_ndvi)
    diff = np.where(current.valid, current.ndvi - base, np.nan)
    finite = diff[np.isfinite(diff)]
    z_base = None
    median_departure = None
    if finite.size:
        median_departure = round(float(np.median(finite)), 3)
        z_base = round(float(np.median(finite)) / regions.season_sd(hist_ndvi), 2)

    zpix = regions.pixel_z(diff)
    min_px = max(1, int(np.ceil(min_region_acres / grid.pixel_acres)))
    masks, zs = regions.find_regions(zpix, z_threshold, min_px)
    masks = [m for m in masks if regions.strong_enough(float(m.sum()) * grid.pixel_acres,
                                                       float(-np.nanmean(zs[m])), z_threshold)]
    scores = [float(-np.nanmean(zs[m])) for m in masks]
    if hail_mask is not None:  # the one late-season exception: report the hail patch, not timing noise elsewhere
        grown = ndimage.binary_dilation(hail_mask, iterations=1)
        pairs = [(m, sc) for m, sc in zip(masks, scores) if (m & grown).sum() >= 0.25 * m.sum()]
        if not pairs and change is not None:
            zc = regions.pixel_z(change)
            pairs = [(hail_mask, float(-np.nanmean(zc[hail_mask])))]
        masks, scores = [m for m, _ in pairs], [sc for _, sc in pairs]

    feats = patterns.features_for(masks, grid, current.inside, pivot, layers)
    out_regions = []
    for i, (m, f, sc) in enumerate(zip(masks, feats, scores), start=1):
        out_regions.append(C.AnomalyRegion(
            region_id=f"r{i}", geometry=region_geometry(m, grid),
            area_acres=round(float(m.sum()) * grid.pixel_acres, 2), centroid=_centroid(m, grid),
            score=round(sc, 2), features=C.PatternFeatures(**f),
            where=where_in_field(m, current.inside, grid),
        ))
    regional = G.uniform_drop(diff, z_base, z_threshold)
    score = max((r.score for r in out_regions), default=0.0)
    return CoreResult(
        status="ok", indices=indices,
        anomaly=C.AnomalySummary(field_score=score, z_baseline=z_base, z_cohort=None,
                                 delta_since_last=delta, regions=out_regions),
        gates=C.Gates(senescence=senescence, harvest=harvest, regional=regional),
        z=zs, region_masks=masks, baseline=base, change=change, median_departure=median_departure,
        baseline_method="median" if len(hist_ndvi) >= 3 else "mean", crop_state=state,
    )
