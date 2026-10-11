"""Infer a center pivot from a field polygon when the request carries none.

Nothing in the cloud writes a `pivot` for a field, so without this the ring/wedge scores are always 0.
Method (pure numpy, deterministic): project the ring to local meters, take the area centroid, and let the
pivot circle be the largest circle around that centroid that stays inside the polygon (distance to the
nearest boundary segment). A field is accepted as a center-pivot field when
  * the ring is simple enough to trust (>= 3 distinct vertices, centroid inside the polygon),
  * the polygon is nearly convex (area / convex-hull area >= MIN_SOLIDITY), which rejects L-shapes,
  * area / (pi r^2) is between MIN_FILL and MAX_FILL: ~1.0 for a circle, 4/pi = 1.27 for a square quarter
    section; the upper bound allows rectangles up to about 1.37:1 and rejects long or thin fields,
  * the radius is between MIN_RADIUS_M and MAX_RADIUS_M (a plausible pivot, ~17 to ~250 acres).
Partial pivots (half / three-quarter circles) are not inferred; those fields return None.
An explicit pivot in the request always wins (see resolve_pivot).
"""
from __future__ import annotations

import logging
import math

import numpy as np

from .grid import M_PER_DEG_LAT, M_PER_DEG_LON_EQ, Pivot

log = logging.getLogger(__name__)

MIN_SOLIDITY = 0.9
MIN_FILL = 0.9
MAX_FILL = 1.75
MIN_RADIUS_M = 150.0
MAX_RADIUS_M = 900.0


def _local_xy(polygon) -> np.ndarray:
    pts = np.asarray([(p[0], p[1]) for p in polygon], dtype="float64")
    if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    lon0, lat0 = pts[:, 0].mean(), pts[:, 1].mean()
    x = (pts[:, 0] - lon0) * M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
    y = (pts[:, 1] - lat0) * M_PER_DEG_LAT
    return np.column_stack([x, y]), (lon0, lat0)


def _hull_area(xy: np.ndarray) -> float:
    pts = sorted(set(map(tuple, xy)))
    if len(pts) < 3:
        return 0.0

    def half(seq):
        h = []
        for p in seq:
            while len(h) >= 2 and ((h[-1][0] - h[-2][0]) * (p[1] - h[-2][1])
                                   - (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0])) <= 0:
                h.pop()
            h.append(p)
        return h

    hull = half(pts)[:-1] + half(pts[::-1])[:-1]
    return abs(_shoelace(np.asarray(hull)))


def _shoelace(xy: np.ndarray) -> float:
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _centroid(xy: np.ndarray, signed_area: float) -> tuple[float, float]:
    x, y = xy[:, 0], xy[:, 1]
    x1, y1 = np.roll(x, -1), np.roll(y, -1)
    cross = x * y1 - x1 * y
    return (float(np.sum((x + x1) * cross) / (6 * signed_area)),
            float(np.sum((y + y1) * cross) / (6 * signed_area)))


def _dist_to_boundary(xy: np.ndarray, c: tuple[float, float]) -> float:
    a = xy
    b = np.roll(xy, -1, axis=0)
    ab = b - a
    ap = np.asarray(c) - a
    denom = np.maximum((ab * ab).sum(axis=1), 1e-12)
    t = np.clip((ap * ab).sum(axis=1) / denom, 0.0, 1.0)
    closest = a + ab * t[:, None]
    return float(np.hypot(*(closest - np.asarray(c)).T).min())


def _inside(xy: np.ndarray, c: tuple[float, float]) -> bool:
    x, y = xy[:, 0], xy[:, 1]
    x1, y1 = np.roll(x, -1), np.roll(y, -1)
    straddle = (y > c[1]) != (y1 > c[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = x + (c[1] - y) * (x1 - x) / (y1 - y)
    return bool(np.count_nonzero(straddle & (c[0] < xi)) % 2)


def infer_pivot(polygon) -> Pivot | None:
    """Pivot (lon/lat center, radius_m) for a plausibly center-pivot polygon, else None."""
    if not polygon or len(polygon) < 3:
        return None
    xy, (lon0, lat0) = _local_xy(polygon)
    if len(xy) < 3:
        return None
    signed = _shoelace(xy)
    area = abs(signed)
    if area < 1.0:
        return None
    cx, cy = _centroid(xy, signed)
    if not _inside(xy, (cx, cy)):
        return None
    hull = _hull_area(xy)
    if hull <= 0 or area / hull < MIN_SOLIDITY:
        return None
    r = _dist_to_boundary(xy, (cx, cy))
    if not MIN_RADIUS_M <= r <= MAX_RADIUS_M:
        return None
    fill = area / (math.pi * r * r)
    if not MIN_FILL <= fill <= MAX_FILL:
        return None
    lon = lon0 + cx / (M_PER_DEG_LON_EQ * math.cos(math.radians(lat0)))
    lat = lat0 + cy / M_PER_DEG_LAT
    return Pivot((float(lon), float(lat)), float(r))


def resolve_pivot(request_pivot: dict | None, polygon) -> Pivot | None:
    """Explicit request pivot if given, else an inferred one (logged), else None."""
    explicit = Pivot.from_request(request_pivot)
    if explicit is not None:
        return explicit
    inferred = infer_pivot(polygon)
    if inferred is not None:
        log.info("pivot inferred from polygon: center=(%.6f, %.6f) radius_m=%.0f", *inferred.center_lonlat,
                 inferred.radius_m)
    return inferred
