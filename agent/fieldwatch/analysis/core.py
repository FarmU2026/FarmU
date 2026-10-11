"""The numeric core: index stacks in, contract pieces out. Pure numpy; no network, no AWS, no web.

    analyze_stacks(current, history, previous, crop=..., pivot=..., layers=...) -> CoreResult

`history` is one IndexStack per prior season (same weeks), `previous` the last clear pass before `current`.
All stacks share one Grid.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from rasterio.features import shapes
from shapely.geometry import Polygon as ShPolygon
from shapely.geometry import mapping

from .. import contracts as C
from . import gates as G
from . import patterns, regions
from .grid import Grid, IndexStack, Layers, Pivot


@dataclass
class CoreResult:
    status: str  # "ok" | "no_baseline"
    indices: C.Indices
    anomaly: C.AnomalySummary
    gates: C.Gates
    z: np.ndarray | None = None                       # smoothed z map (NaN outside valid pixels)
    region_masks: list[np.ndarray] = field(default_factory=list)  # aligned with anomaly.regions


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


def analyze_stacks(current: IndexStack, history: list[IndexStack], previous: IndexStack | None, *,
                   crop: str = "corn", pivot: Pivot | None = None, layers: Layers | None = None,
                   z_threshold: float = 2.0, min_region_acres: float = 1.2) -> CoreResult:
    grid = current.grid
    indices = index_stats(current)
    delta = None
    if previous is not None:
        both = current.valid & previous.valid
        if both.any():
            delta = round(float(np.median(current.ndvi[both] - previous.ndvi[both])), 3)
    senescence, harvest = G.seasonal(crop, current.date, delta_since_last=delta, ndvi_p50=indices.ndvi.p50)

    if not history:
        return CoreResult(
            status="no_baseline", indices=indices,
            anomaly=C.AnomalySummary(field_score=0.0, z_baseline=None, z_cohort=None, delta_since_last=delta, regions=[]),
            gates=C.Gates(senescence=senescence, harvest=harvest, regional=False),
        )

    hist_ndvi = [h.ndvi for h in history]
    base = regions.baseline_mean(hist_ndvi)
    diff = np.where(current.valid, current.ndvi - base, np.nan)
    finite = diff[np.isfinite(diff)]
    z_base = None
    if finite.size:
        z_base = round(float(np.median(finite)) / regions.season_sd(hist_ndvi), 2)

    zpix = regions.pixel_z(diff)
    min_px = max(1, int(np.ceil(min_region_acres / grid.pixel_acres)))
    masks, zs = regions.find_regions(zpix, z_threshold, min_px)
    masks = [m for m in masks if regions.strong_enough(float(m.sum()) * grid.pixel_acres,
                                                       float(-np.nanmean(zs[m])), z_threshold)]

    feats = patterns.features_for(masks, grid, current.inside, pivot, layers)
    out_regions = []
    for i, (m, f) in enumerate(zip(masks, feats), start=1):
        out_regions.append(C.AnomalyRegion(
            region_id=f"r{i}", geometry=region_geometry(m, grid),
            area_acres=round(float(m.sum()) * grid.pixel_acres, 2), centroid=_centroid(m, grid),
            score=round(float(-np.nanmean(zs[m])), 2), features=C.PatternFeatures(**f),
        ))
    regional = G.uniform_drop(diff, z_base, z_threshold)
    score = max((r.score for r in out_regions), default=0.0)
    return CoreResult(
        status="ok", indices=indices,
        anomaly=C.AnomalySummary(field_score=score, z_baseline=z_base, z_cohort=None,
                                 delta_since_last=delta, regions=out_regions),
        gates=C.Gates(senescence=senescence, harvest=harvest, regional=regional),
        z=zs, region_masks=masks,
    )
