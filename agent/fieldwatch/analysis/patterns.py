"""Pattern features for one problem region (all 0-1). Pure numpy.

ring: wide angular coverage around the pivot but a narrow radial band.
wedge: a narrow angular sector spanning a wide radial range.
edge band: elongated and hugging the field boundary.
stripe: straight, parallel, long thin bands away from the edge.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage

from .grid import Grid, Layers

EDGE_NEAR_M = 60.0
N_SECTORS = 24


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def shape_stats(mask: np.ndarray) -> dict:
    """Principal-axis aspect ratio, straightness (area over length x width) and orientation (degrees)."""
    rr, cc = np.nonzero(mask)
    pts = np.column_stack([cc, -rr]).astype("float64")
    pts -= pts.mean(axis=0)
    if len(pts) < 3:
        return {"aspect": 1.0, "fill": 1.0, "angle": 0.0}
    w, v = np.linalg.eigh(np.cov(pts.T))
    major = v[:, 1]
    aspect = math.sqrt(max(w[1], 1e-9) / max(w[0], 1e-3))
    proj = pts @ v
    length = np.percentile(proj[:, 1], 99) - np.percentile(proj[:, 1], 1) + 1.0
    width = math.sqrt(12.0 * max(float(proj[:, 0].var()), 1e-6))  # width of a uniform band with this spread
    fill = min(1.0, len(pts) / float(length * max(width, 1.0)))  # ~1 for a straight band, small for an arc
    angle = math.degrees(math.atan2(major[1], major[0])) % 180
    return {"aspect": aspect, "fill": fill, "angle": angle}


def _arc_covering(angles_deg: np.ndarray, frac: float = 0.9, bins: int = 72) -> float:
    """Smallest arc (degrees) that holds `frac` of the angles."""
    hist, _ = np.histogram(angles_deg % 360, bins=bins, range=(0, 360))
    need = frac * hist.sum()
    ext = np.concatenate([hist, hist])
    best = bins
    for i in range(bins):
        s = 0
        for j in range(bins):
            s += ext[i + j]
            if s >= need:
                best = min(best, j + 1)
                break
    return best * 360.0 / bins


def pivot_scores(mask: np.ndarray, grid: Grid, center_lonlat, radius_m: float) -> tuple[float, float]:
    row0, col0 = grid.lonlat_to_pix(*center_lonlat)
    rr, cc = np.nonzero(mask)
    dy = -(rr - row0) * grid.res_m
    dx = (cc - col0) * grid.res_m
    r = np.hypot(dx, dy)
    ang = np.degrees(np.arctan2(dy, dx)) % 360
    # ring: covers many sectors, thin in radius (half a ring already scores full coverage)
    counts, _ = np.histogram(ang, bins=N_SECTORS, range=(0, 360))
    coverage = float((counts >= 3).sum()) / N_SECTORS
    ring = _clip01(coverage / 0.5) * _clip01(1 - float(r.std()) / (0.2 * radius_m))
    # wedge: few degrees, long in radius
    arc = _arc_covering(ang)
    span = (np.percentile(r, 90) - np.percentile(r, 10)) / (0.5 * radius_m)
    # a sector runs along the radius; a compact blob (other crop, waterway end, low spot) that merely sits in a
    # narrow angle far from the pivot does not. Real irregular regions scored wedge 0.73-0.83 before this factor.
    tangential = max(float(np.median(r)) * math.radians(arc), 1.0)
    radial = float(np.percentile(r, 95) - np.percentile(r, 5))
    wedge = _clip01(1 - arc / 150.0) * _clip01(span) * _clip01((radial / tangential - 0.7) / 1.0)
    return ring, wedge


def edge_band(mask: np.ndarray, edge_dist_m: np.ndarray, aspect: float) -> float:
    near = float((edge_dist_m[mask] <= EDGE_NEAR_M).mean())
    return near * _clip01((aspect - 1.5) / 2.5)


def stripe(aspect: float, fill: float) -> float:
    return _clip01((aspect - 3.0) / 5.0) * _clip01((fill - 0.5) / 0.4)


def overlap(mask: np.ndarray, layer: np.ndarray | None) -> float | None:
    if layer is None:
        return None
    return float(layer[mask].mean())


def edge_distance_m(inside: np.ndarray, res_m: float) -> np.ndarray:
    return ndimage.distance_transform_edt(np.pad(inside, 1, constant_values=False))[1:-1, 1:-1] * res_m


def features_for(masks: list[np.ndarray], grid: Grid, inside: np.ndarray, pivot, layers: Layers | None) -> list[dict]:
    """One feature dict per region mask (keys match PatternFeatures)."""
    layers = layers or Layers()
    dist = edge_distance_m(inside, grid.res_m)
    shapes = [shape_stats(m) for m in masks]
    out = []
    for i, (m, sh) in enumerate(zip(masks, shapes)):
        ring = wedge = 0.0
        if pivot is not None:
            ring, wedge = pivot_scores(m, grid, pivot.center_lonlat, pivot.radius_m)
        edge = edge_band(m, dist, sh["aspect"])
        st = stripe(sh["aspect"], sh["fill"])
        # a band along the boundary or an arc of a ring is not a stripe pass
        st *= (1 - 0.8 * edge) * (1 - 0.5 * ring)
        if st > 0:
            for j, o in enumerate(shapes):
                if j != i and stripe(o["aspect"], o["fill"]) > 0.3:
                    d = abs(sh["angle"] - o["angle"])
                    if min(d, 180 - d) < 12:  # a parallel partner band
                        st = min(1.0, st * 1.2)
                        break
        low = overlap(m, layers.lowspot)
        sandy = overlap(m, layers.sandy_soil)
        hail = overlap(m, layers.hail)
        out.append({
            "ring_score": round(ring, 3), "wedge_score": round(wedge, 3),
            "edge_band_score": round(edge, 3), "stripe_score": round(_clip01(st), 3),
            "lowspot_overlap": round(low, 3) if low is not None else 0.0,
            "sandy_soil_overlap": round(sandy, 3) if sandy is not None else 0.0,
            "hail_swath_overlap": round(hail, 3) if hail is not None else None,
        })
    return out
