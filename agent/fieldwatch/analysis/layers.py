"""Context layers (low spots, sandy soil, hail swath) rasterized onto the analyzer's grid.

The numpy core stays pure: this module turns lane outputs into the boolean `Layers` arrays and is the only
place that does I/O. Every source is optional and isolated: a failing source is logged and that layer is
left None (overlap 0 / None); the analysis itself never fails because of a layer.

Sources (all reached through `fieldwatch.interfaces`):
- low spots: `context().get_terrain_summary(field_id).lowspot_key` -> single-band uint8 GeoTIFF (1 = depression,
  255 = outside field) on the 3DEP projected grid, local path or S3 key.
- sandy soil: `context().get_soil_profile(field_id).zones_key` -> GeoJSON FeatureCollection (lon/lat) whose
  features carry `properties.sandy`.
- hail: `HailReport` is summary-only (no geometry), so the contracts cannot build a swath. When
  `interfaces.hazards()` exists and offers `get_hail_swath` (proposed, see `HAIL_SWATH_PROPOSAL`) it is used;
  until then inject a `hail` source or the layer stays None.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .. import config
from .. import contracts as C
from .grid import Grid, Layers

log = logging.getLogger("fieldwatch.analysis.layers")

# (request, grid, inside) -> bool array on the grid, or None when the layer is unavailable.
MaskSource = Callable[[C.FieldAnalysisRequest, Grid, np.ndarray], "np.ndarray | None"]

HAIL_SWATH_PROPOSAL = """Proposed addition to fieldwatch/interfaces.py (WS3 hail swath geometry):
    class HazardsAPI(Protocol):
        def get_hail_swath(self, field_id: str, start: str, end: str, *,
                           polygon: list[tuple[float, float]] | None = None, threshold_mm: float = 25.4) -> dict: ...
        # GeoJSON FeatureCollection (lon/lat) of areas where MESH max >= threshold_mm over [start, end]
    CANONICAL["hazards"] = "fieldwatch.hazards"
    def hazards() -> HazardsAPI: ...
"""


# ---------------------------------------------------------------- rasterization (pure)

def _map_coords(coords, fn):
    if coords and isinstance(coords[0], (int, float)):
        return list(fn(coords[0], coords[1]))
    return [_map_coords(c, fn) for c in coords]


def rasterize_geojson(geojson: dict, grid: Grid, select: Callable[[dict], bool] | None = None) -> np.ndarray:
    """Burn lon/lat GeoJSON polygons (FeatureCollection / Feature / geometry) onto `grid` as a bool mask.

    `select(properties)` filters features. A pixel is set when its center is inside a polygon."""
    from affine import Affine
    from rasterio.features import rasterize

    if geojson.get("type") == "FeatureCollection":
        feats = geojson.get("features", [])
    elif geojson.get("type") == "Feature":
        feats = [geojson]
    else:
        feats = [{"type": "Feature", "geometry": geojson, "properties": {}}]
    feats = [f for f in feats if select is None or select(f.get("properties") or {})]
    if not feats:
        return np.zeros(grid.shape, bool)
    if grid.crs is not None:
        from rasterio.warp import transform_geom
        shapes = [transform_geom("EPSG:4326", grid.crs, f["geometry"]) for f in feats]
        transform = grid.transform
    else:  # synthetic grid: map lon/lat to pixel space and burn with an identity transform
        def to_px(lon, lat):
            r, c = grid.lonlat_to_pix(lon, lat)
            return c + 0.5, r + 0.5
        shapes = [{"type": f["geometry"]["type"], "coordinates": _map_coords(f["geometry"]["coordinates"], to_px)}
                  for f in feats]
        transform = Affine.identity()
    return rasterize([(s, 1) for s in shapes], out_shape=grid.shape, transform=transform, fill=0,
                     dtype="uint8").astype(bool)


def resample_mask(src: np.ndarray, src_transform, src_crs, grid: Grid) -> np.ndarray:
    """Resample a (usually finer) uint8 raster (1 = set; anything else, incl. 255 nodata, = unset) onto a
    projected `grid`; a pixel is set when at least half of its footprint is set."""
    from rasterio.warp import Resampling, reproject

    vals = (np.asarray(src) == 1).astype("float32")
    out = np.zeros(grid.shape, dtype="float32")
    reproject(vals, out, src_transform=src_transform, src_crs=src_crs, dst_transform=grid.transform,
              dst_crs=grid.crs, resampling=Resampling.average)
    return out >= 0.5


def overlap_fraction(region: np.ndarray, layer: np.ndarray | None) -> float | None:
    """Share of `region` covered by `layer` (None when the layer is missing)."""
    if layer is None:
        return None
    n = int(region.sum())
    return float((region & layer).sum()) / n if n else 0.0


# ---------------------------------------------------------------- artifact keys

def _read_bytes(key: str) -> bytes:
    p = Path(key)
    if p.exists():
        return p.read_bytes()
    bucket = os.getenv("BUCKET_NAME") or config.BUCKET
    if not bucket:
        raise FileNotFoundError(f"artifact not found locally and no bucket configured: {key}")
    import boto3
    return boto3.client("s3", region_name=config.REGION).get_object(Bucket=bucket, Key=key)["Body"].read()


def lowspot_from_key(key: str, grid: Grid) -> np.ndarray:
    from rasterio.io import MemoryFile
    with MemoryFile(_read_bytes(key)) as mem, mem.open() as ds:
        return resample_mask(ds.read(1), ds.transform, ds.crs, grid)


def sandy_from_key(key: str, grid: Grid) -> np.ndarray:
    return rasterize_geojson(json.loads(_read_bytes(key)), grid, select=lambda p: bool(p.get("sandy")))


# ---------------------------------------------------------------- default sources (via interfaces.py)

def lowspot_source(req, grid, inside):
    from .. import interfaces
    summary = interfaces.context().get_terrain_summary(req.field.field_id)
    return lowspot_from_key(summary.lowspot_key, grid) if summary.lowspot_key else None


def sandy_soil_source(req, grid, inside):
    from .. import interfaces
    profile = interfaces.context().get_soil_profile(req.field.field_id)
    return sandy_from_key(profile.zones_key, grid) if profile.zones_key else None


def hail_source(req, grid, inside):
    """Uses `interfaces.hazards().get_hail_swath` once that exists; until then the layer is None."""
    from .. import interfaces
    hazards = getattr(interfaces, "hazards", None)
    if hazards is None or not hasattr(hazards(), "get_hail_swath"):
        return None
    swath = hazards().get_hail_swath(req.field.field_id, f"{req.as_of[:4]}-01-01", req.as_of,
                                     polygon=[tuple(p) for p in req.polygon])
    return rasterize_geojson(swath, grid)


class LayerProvider:
    """Builds `Layers` from per-layer sources; any source may be replaced, or None to skip that layer."""

    def __init__(self, *, lowspot: MaskSource | None = lowspot_source,
                 sandy_soil: MaskSource | None = sandy_soil_source, hail: MaskSource | None = hail_source):
        self.sources = {"lowspot": lowspot, "sandy_soil": sandy_soil, "hail": hail}

    def __call__(self, req: C.FieldAnalysisRequest, grid: Grid, inside: np.ndarray) -> Layers:
        out = {}
        for name, src in self.sources.items():
            if src is None:
                continue
            try:
                mask = src(req, grid, inside)
                if mask is not None:
                    mask = np.asarray(mask, bool)
                    if mask.shape != grid.shape:
                        raise ValueError(f"layer shape {mask.shape} != grid {grid.shape}")
                out[name] = mask
            except Exception as e:  # noqa: BLE001 - a layer must never fail the analysis
                log.warning("layer %s unavailable for %s: %s: %s", name, req.field.field_id, type(e).__name__, e)
        return Layers(**out)


def build_layers(provider, req, grid, inside) -> Layers | None:
    """Run a provider defensively; None on any failure."""
    try:
        return provider(req, grid, inside)
    except Exception as e:  # noqa: BLE001
        log.warning("layer provider failed for %s: %s: %s", req.field.field_id, type(e).__name__, e)
        return None
