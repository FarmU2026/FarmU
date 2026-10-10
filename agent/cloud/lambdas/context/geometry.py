"""Field polygon normalization (pure shapely, no AWS): repair, acres, centroid, bbox, interior buffer.

Areas are computed in a local equal-spacing projection around the polygon (meters per degree of latitude
and longitude on the WGS84 ellipsoid at the centroid). For fields up to a few km across the error is far
below 0.1%.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely import affinity, make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

from .common import BoundaryError

SQM_PER_ACRE = 4046.8564224
INTERIOR_BUFFER_M = 10.0  # data-pipelines A1: keep edge pixels from mixing in road or neighbor signal
MIN_ACRES = 0.1
MAX_ACRES = 20000.0


def _meters_per_degree(lat_deg: float) -> tuple[float, float]:
    """(meters per degree of latitude, meters per degree of longitude) on the WGS84 ellipsoid."""
    phi = math.radians(lat_deg)
    lat_m = 111132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    lon_m = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi)
    return lat_m, lon_m


@dataclass
class NormalizedField:
    polygon: list[list[float]]  # closed ring, counter-clockwise, [[lon, lat], ...]
    acres: float
    centroid: tuple[float, float]  # (lon, lat)
    bbox: tuple[float, float, float, float]  # west, south, east, north
    repaired: bool  # True when the drawn ring was invalid or unclosed and had to be fixed
    interior_polygon: list[list[float]] | None = None  # ~10 m inward buffer; None for very small fields
    notes: list[str] = field(default_factory=list)

    def geojson(self) -> dict:
        return {"type": "Polygon", "coordinates": [self.polygon]}


def _ring_from_input(raw) -> list[tuple[float, float]]:
    """Accept [[lon, lat], ...] (possibly Decimal values) or a GeoJSON Polygon dict."""
    if isinstance(raw, dict):
        if raw.get("type") != "Polygon" or not raw.get("coordinates"):
            raise BoundaryError("polygon must be [[lon, lat], ...] or a GeoJSON Polygon")
        raw = raw["coordinates"][0]
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        raise BoundaryError("polygon needs at least 3 points")
    ring = []
    for pt in raw:
        try:
            lon, lat = float(pt[0]), float(pt[1])
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            raise BoundaryError(f"bad polygon point {pt!r}") from exc
        if not (math.isfinite(lon) and math.isfinite(lat)) or abs(lon) > 180 or abs(lat) > 90:
            raise BoundaryError(f"polygon point out of range: {pt!r} (expected [lon, lat])")
        ring.append((lon, lat))
    return ring


def normalize_polygon(raw) -> NormalizedField:
    ring = _ring_from_input(raw)
    unclosed = ring[0] != ring[-1]
    poly = Polygon(ring)
    notes: list[str] = []
    repaired = not poly.is_valid
    if repaired:
        fixed = make_valid(poly)
        parts = [g for g in getattr(fixed, "geoms", [fixed]) if isinstance(g, (Polygon, MultiPolygon))]
        polys = [p for g in parts for p in (g.geoms if isinstance(g, MultiPolygon) else [g])]
        if not polys:
            raise BoundaryError("polygon has no area")
        poly = max(polys, key=lambda p: p.area)
        if len(polys) > 1:
            notes.append(f"kept the largest of {len(polys)} pieces after repair")
    if poly.is_empty or poly.area == 0:
        raise BoundaryError("polygon has no area")
    poly = orient(Polygon(poly.exterior), 1.0)  # drop holes (a drawn field has none), counter-clockwise

    c = poly.centroid
    lat_m, lon_m = _meters_per_degree(c.y)

    def to_m(g):
        return affinity.scale(affinity.translate(g, -c.x, -c.y), lon_m, lat_m, origin=(0, 0))

    local = to_m(poly)
    acres = local.area / SQM_PER_ACRE
    if acres < MIN_ACRES:
        raise BoundaryError(f"polygon is only {acres:.3f} acres")
    if acres > MAX_ACRES:
        raise BoundaryError(f"polygon is {acres:.0f} acres, too big for one field")

    interior = None
    inner = local.buffer(-INTERIOR_BUFFER_M)
    if not inner.is_empty:
        if isinstance(inner, MultiPolygon):
            inner = max(inner.geoms, key=lambda p: p.area)
        interior = [[round(x / lon_m + c.x, 7), round(y / lat_m + c.y, 7)] for x, y in inner.exterior.coords]

    ring_out = [[round(x, 7), round(y, 7)] for x, y in poly.exterior.coords]
    minx, miny, maxx, maxy = poly.bounds
    return NormalizedField(
        polygon=ring_out,
        acres=round(acres, 2),
        centroid=(round(c.x, 7), round(c.y, 7)),
        bbox=(round(minx, 7), round(miny, 7), round(maxx, 7), round(maxy, 7)),
        repaired=repaired or unclosed,
        interior_polygon=interior,
        notes=notes,
    )
