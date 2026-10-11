"""Shared helpers for the imagery tools: resolve a field, rasterize lon/lat polygons, inject a region resolver.

No web or AWS imports. Field facts come from `farmdata.farm` (the same path tools.py uses), never from guessed names.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import numpy as np

from .. import contracts as C
from .. import farmdata


@dataclass
class FieldInfo:
    field_id: str
    farm_id: str
    name: str
    crop: str
    polygon: list[list[float]]  # lon/lat ring
    irrigation: str = ""
    pivot: dict | None = None  # {center: [lon, lat], radius_m}, when the farm record has one
    raw: dict = field(default_factory=dict)

    @property
    def ref(self) -> C.FieldRef:
        return C.FieldRef(farm_id=self.farm_id, field_id=self.field_id)


def resolve_field(field_id: str) -> FieldInfo:
    """Look a field up by id or by the farmer's name for it. Raises ValueError for an unknown field."""
    farm = farmdata.farm(field_id)
    fields = farm["fields"]
    for f in fields:
        if f.get("id") == field_id or str(f.get("name", "")).lower() == field_id.lower():
            poly = f.get("polygon")
            if not poly and f.get("bbox"):
                w, s, e, n = f["bbox"]
                poly = [[w, s], [e, s], [e, n], [w, n]]
            if not poly:
                raise ValueError(f"field {field_id!r} has no polygon or bbox")
            return FieldInfo(field_id=f["id"], farm_id=farm.get("farm_id", ""), name=f.get("name", f["id"]),
                             crop=f.get("crop", "other"), polygon=[list(map(float, p)) for p in poly],
                             irrigation=str(f.get("irrigation", "")), pivot=f.get("pivot"), raw=f)
    raise ValueError(f"unknown field {field_id!r}; known: {[f.get('id') for f in fields]}")


def parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


# A region resolver turns (field, region_id, as_of) into a GeoJSON Polygon (lon/lat) or None when unknown.
RegionResolver = Callable[["FieldInfo", str, str], "dict | None"]


def analysis_region_resolver(info: FieldInfo, region_id: str, as_of: str) -> dict | None:
    """Default: run the analyzer as of `as_of` and return the named region's geometry."""
    from .analyzer import analyze

    crop = info.crop if info.crop in C.Crop.__args__ else "other"
    req = C.FieldAnalysisRequest(field=info.ref, polygon=[tuple(p) for p in info.polygon], crop=crop,
                                 as_of=as_of, pivot=info.pivot)
    res = analyze(req)
    for r in (res.anomaly.regions if res.anomaly else []):
        if r.region_id == region_id:
            return r.geometry
    return None


def resolve_region(info: FieldInfo, region_id: str | None, geometry: dict | None, as_of: str,
                   resolver: RegionResolver | None) -> dict | None:
    if geometry:
        return geometry
    if region_id:
        return (resolver or analysis_region_resolver)(info, region_id, as_of)
    return None


def geometry_mask(geometry: dict, grid: Any) -> np.ndarray:
    """Boolean mask of grid pixels inside a lon/lat GeoJSON geometry (grid must be a UTM `Grid`)."""
    from rasterio.features import geometry_mask as gm
    from rasterio.warp import transform_geom

    g = transform_geom("EPSG:4326", grid.crs, geometry)
    return gm([g], out_shape=grid.shape, transform=grid.transform, invert=True)
