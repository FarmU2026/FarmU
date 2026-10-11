"""Scene fetching: STAC search on Earth Search (sentinel-2-c1-l2a) and windowed COG reads onto a Grid.

Injectable: the analyzer only needs `search(...)` and `read(...)` (see `SceneLoader`). Public, no AWS credentials.
C1 bands are uint16 with nodata 0, scale 0.0001 and offset -0.1 (docs/spike-findings.md); each item's asset
metadata is used when present. Red/NIR are 10 m; red edge, SWIR and SCL are 20 m and are resampled (nearest)
onto the 10 m grid.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Protocol

import numpy as np

from .. import contracts as C
from .grid import Grid, SceneData

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-c1-l2a"
BAND_ASSETS = {"red": "red", "green": "green", "blue": "blue", "nir": "nir",
               "rededge": "rededge1", "swir": "swir16", "scl": "scl"}
STAC_TO_BAND = {stac: band for band, stac in BAND_ASSETS.items()}
DEFAULT_SCALE, DEFAULT_OFFSET = 0.0001, -0.1


class SceneLoader(Protocol):
    def search(self, bbox: tuple[float, float, float, float], start: date, end: date,
               max_cloud: float = 100.0, limit: int = 20) -> list[C.SceneInfo]:
        """Scenes covering bbox with start <= date <= end, newest first."""

    def read(self, scene: C.SceneInfo, grid: Grid) -> SceneData:
        """Bands resampled onto `grid` (reflectance, NaN where nodata)."""


def _read_band(href: str, grid: Grid, scale: float | None, offset: float | None) -> np.ndarray:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    h, w = grid.shape
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                      GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="1"):
        with rasterio.open(href) as src, WarpedVRT(
            src, crs=grid.crs, transform=grid.transform, width=w, height=h,
            resampling=Resampling.nearest, src_nodata=0, nodata=0,
        ) as vrt:
            dn = vrt.read(1)
    if scale is None:  # the SCL class band stays integer
        return dn.astype("uint8")
    out = dn.astype("float32") * scale + offset
    out[dn == 0] = np.nan
    return out


class StacLoader:
    def __init__(self, stac_url: str = STAC_URL, collection: str = COLLECTION, max_workers: int = 7):
        self.stac_url = stac_url
        self.collection = collection
        self.max_workers = max_workers
        self._scaling: dict[str, dict[str, tuple[float, float]]] = {}

    def search(self, bbox, start: date, end: date, max_cloud: float = 100.0, limit: int = 20) -> list[C.SceneInfo]:
        from pystac_client import Client
        from shapely.geometry import box, shape

        want = box(*bbox)
        search = Client.open(self.stac_url).search(
            collections=[self.collection], bbox=list(bbox),
            datetime=f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
            query={"eo:cloud_cover": {"lt": max_cloud}},
            sortby=[{"field": "properties.datetime", "direction": "desc"}],
            max_items=limit * 3,
        )
        out: list[C.SceneInfo] = []
        for item in search.items():
            if item.geometry and not shape(item.geometry).buffer(1e-6).contains(want):
                continue  # tile footprint only partly covers the field: another tile will cover it
            if not all(a in item.assets for a in BAND_ASSETS.values()):
                continue
            scaling = {}
            for band, name in BAND_ASSETS.items():
                rb = (item.assets[name].extra_fields.get("raster:bands") or [{}])[0]
                scaling[band] = (float(rb.get("scale", DEFAULT_SCALE)), float(rb.get("offset", DEFAULT_OFFSET)))
            self._scaling[item.id] = scaling
            out.append(C.SceneInfo(
                scene_id=item.id, datetime=item.datetime.strftime("%Y-%m-%dT%H:%M:%SZ"),
                tile_id=str(item.properties.get("grid:code", "")).replace("MGRS-", ""),
                cloud_cover=float(item.properties.get("eo:cloud_cover", 0.0)),
                assets={b: item.assets[n].href for b, n in BAND_ASSETS.items()},
            ))
            if len(out) >= limit:
                break
        return out

    def read(self, scene: C.SceneInfo, grid: Grid) -> SceneData:
        scaling = self._scaling.get(scene.scene_id, {})
        jobs = {}
        with ThreadPoolExecutor(self.max_workers) as pool:
            for name, href in scene.assets.items():
                # Our own search keys assets by band ("rededge"); scenes pinned by the scene pipeline arrive
                # keyed by STAC asset name ("rededge1"). Accept both.
                band = name if name in BAND_ASSETS else STAC_TO_BAND.get(name)
                if band is None or band in jobs:
                    continue
                sc, off = (None, None) if band == "scl" else scaling.get(band, (DEFAULT_SCALE, DEFAULT_OFFSET))
                jobs[band] = pool.submit(_read_band, href, grid, sc, off)
            arrays = {b: f.result() for b, f in jobs.items()}
        return SceneData(
            scene_id=scene.scene_id, date=date.fromisoformat(scene.datetime[:10]), grid=grid,
            red=arrays["red"], nir=arrays["nir"], rededge=arrays["rededge"], swir=arrays["swir"],
            scl=arrays["scl"], green=arrays.get("green"), blue=arrays.get("blue"),
            tile_id=scene.tile_id, cloud_cover=scene.cloud_cover,
        )
