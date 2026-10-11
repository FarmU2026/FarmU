"""Imagery/environment tool `test_overlap`: how much of a problem region sits on a layer, versus chance.

Layers: `field_edge` and `pivot_ring` are geometry-only and built in here from the field polygon. The rest
(`lowspot`, `soil_sandy`, `hail_swath`) come from an injectable `LayerProvider` (the planned analysis/layers.py
should implement `get_layer`); without a provider they return status "layer_unavailable", not an error.
Geometry is intersected in local metres with shapely. No web or AWS imports.
"""
from __future__ import annotations

import math
from typing import Literal, Protocol

import numpy as np
import shapely
from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import Point, Polygon, shape

from .fieldref import FieldInfo, RegionResolver, resolve_field, resolve_region

LayerName = Literal["field_edge", "pivot_ring", "lowspot", "soil_sandy", "hail_swath"]
BUILTIN_LAYERS = ("field_edge", "pivot_ring")
EDGE_BAND_M = 30.0
RING_INNER_FRAC = 0.75   # pivot ring = outer 25% of the pivot radius
PIXEL_M2 = 100.0         # 10 m Sentinel-2 pixel: the effective sample size for the chance test
ACRE_M2 = 4046.856
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQ = 111_320.0


class LayerProvider(Protocol):
    def get_layer(self, field_id: str, layer: str, polygon: list[list[float]]) -> dict | None:
        """GeoJSON geometry (lon/lat) of the layer's zone inside/around the field, or None if there is no data."""


class LayerOverlap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    layer: LayerName
    status: Literal["ok", "layer_unavailable", "region_not_found", "error"]
    region_id: str | None = None
    overlap: float | None = Field(default=None, description="Share of the problem region inside the layer, 0..1")
    expected_by_chance: float | None = Field(default=None, description="Share of the whole field inside the layer")
    enrichment: float | None = Field(default=None, description="overlap / expected_by_chance")
    p_value: float | None = Field(default=None, description="One-sided binomial P(overlap this high by chance); "
                                  "treats 10 m pixels as independent so it flatters big regions")
    region_acres: float | None = None
    overlap_acres: float | None = None
    note: str | None = None


def _projector(lon0: float, lat0: float):
    kx = M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))

    def f(coords):
        return np.column_stack([(coords[:, 0] - lon0) * kx, (coords[:, 1] - lat0) * M_PER_DEG_LAT])

    return f


def shp_transform(proj, geom):
    return shapely.transform(geom, proj)


def _poly_m(ring, proj) -> Polygon:
    return shp_transform(proj, Polygon([tuple(p) for p in ring]))


def builtin_layer(info: FieldInfo, layer: str, field_m: Polygon, proj, *, edge_band_m: float = EDGE_BAND_M,
                  ring_inner_frac: float = RING_INNER_FRAC):
    """Geometry (metres) for a geometry-only layer, or (None, reason)."""
    if layer == "field_edge":
        band = field_m.difference(field_m.buffer(-edge_band_m))
        return (band, None) if not band.is_empty else (field_m, None)  # a field narrower than the band is all edge
    if layer == "pivot_ring":
        if info.pivot and info.pivot.get("center") and info.pivot.get("radius_m"):
            c = shp_transform(proj, Point(*info.pivot["center"]))
            r = float(info.pivot["radius_m"])
        elif "pivot" in info.irrigation.lower():
            c = field_m.centroid  # center pivot irrigation with no pivot record: assume the inscribed circle
            r = c.distance(field_m.exterior)
        else:
            return None, "field is not irrigated by a center pivot"
        ring = c.buffer(r).difference(c.buffer(r * ring_inner_frac))
        return ring, None
    return None, None


def test_overlap(field_id: str, layer: LayerName, *, region_id: str | None = None, region_geometry: dict | None = None,
                 as_of: str | None = None, layers: LayerProvider | None = None,
                 region_resolver: RegionResolver | None = None) -> LayerOverlap:
    """Overlap of a problem region with a layer, with the share expected by chance and a p-value.

    Give `region_id` (resolved by re-running the analysis as of `as_of`) or a GeoJSON `region_geometry`."""
    out = LayerOverlap(field_id=field_id, layer=layer, status="error", region_id=region_id)
    try:
        from scipy.stats import binomtest

        info = resolve_field(field_id)
        out.field_id = info.field_id
        if not (region_id or region_geometry):
            out.note = "give region_id or region_geometry"
            return out
        if region_geometry is None and not as_of:
            out.note = "as_of is required with region_id"
            return out
        geom = resolve_region(info, region_id, region_geometry, as_of or "", region_resolver)
        if geom is None:
            out.status, out.note = "region_not_found", f"no region {region_id!r} found as of {as_of}"
            return out

        lon0, lat0 = Polygon(info.polygon).centroid.coords[0]
        proj = _projector(lon0, lat0)
        field_m = _poly_m(info.polygon, proj)
        region_m = shp_transform(proj, shape(geom)).intersection(field_m)
        if region_m.is_empty or region_m.area <= 0:
            out.status, out.note = "region_not_found", "region does not fall inside the field"
            return out

        zone, reason = None, None
        provided = layers.get_layer(info.field_id, layer, info.polygon) if layers is not None else None
        if provided:
            zone = shp_transform(proj, shape(provided))
        elif layer in BUILTIN_LAYERS:
            zone, reason = builtin_layer(info, layer, field_m, proj)
        if zone is None:
            out.status = "layer_unavailable"
            out.note = reason or f"no {layer} data for this field (layer provider not available or has no data)"
            return out

        zone = zone.intersection(field_m)
        inter = region_m.intersection(zone).area
        overlap = inter / region_m.area
        expected = zone.area / field_m.area
        n = max(1, round(region_m.area / PIXEL_M2))
        k = min(n, round(overlap * n))
        p = float(binomtest(k, n, min(max(expected, 1e-9), 1 - 1e-9), alternative="greater").pvalue)
        out.status = "ok"
        out.overlap, out.expected_by_chance = round(overlap, 4), round(expected, 4)
        out.enrichment = round(overlap / expected, 2) if expected > 0 else None
        out.p_value = round(p, 6)
        out.region_acres, out.overlap_acres = round(region_m.area / ACRE_M2, 2), round(inter / ACRE_M2, 2)
        return out
    except ValueError as e:
        out.note = str(e)
        return out
    except (OSError, TimeoutError) as e:
        out.note = f"upstream error: {type(e).__name__}: {e}"
        return out


test_overlap.__test__ = False  # the tool name starts with "test_": keep pytest from collecting it when imported
