"""Pixel grids and the arrays the numeric core works on. Pure numpy; no network."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np

M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQ = 111_320.0
ACRE_M2 = 4046.856


@dataclass
class Grid:
    """A north-up grid of square pixels (`res_m` meters). Row 0 is the north edge.

    Real grids are UTM (`crs` and `transform` set, so reads are exact). Synthetic grids have
    `crs=None` and map pixels to lon/lat with a local linear approximation around `origin`.
    """

    shape: tuple[int, int]
    res_m: float = 10.0
    origin: tuple[float, float] = (-96.54, 42.16)  # lon, lat of the north-west corner (synthetic)
    crs: str | None = None
    transform: object | None = None  # rasterio Affine for crs grids

    @property
    def pixel_acres(self) -> float:
        return self.res_m**2 / ACRE_M2

    def _lin(self):
        lon0, lat0 = self.origin
        dlat = self.res_m / M_PER_DEG_LAT
        dlon = self.res_m / (M_PER_DEG_LON_EQ * math.cos(math.radians(lat0)))
        return lon0, lat0, dlon, dlat

    def pix_to_lonlat(self, rows, cols):
        """Pixel centers (row, col; floats allowed) to (lons, lats) arrays."""
        rows = np.asarray(rows, dtype="float64")
        cols = np.asarray(cols, dtype="float64")
        if self.crs is None:
            lon0, lat0, dlon, dlat = self._lin()
            return lon0 + (cols + 0.5) * dlon, lat0 - (rows + 0.5) * dlat
        from rasterio.warp import transform as warp_transform

        xs, ys = self.transform * (cols + 0.5, rows + 0.5)
        lons, lats = warp_transform(self.crs, "EPSG:4326", np.ravel(xs).tolist(), np.ravel(ys).tolist())
        return np.reshape(lons, rows.shape), np.reshape(lats, rows.shape)

    def lonlat_to_pix(self, lon: float, lat: float) -> tuple[float, float]:
        """(row, col) as floats where integer + 0.5 is a pixel center."""
        if self.crs is None:
            lon0, lat0, dlon, dlat = self._lin()
            return (lat0 - lat) / dlat - 0.5, (lon - lon0) / dlon - 0.5
        from rasterio.warp import transform as warp_transform

        xs, ys = warp_transform("EPSG:4326", self.crs, [lon], [lat])
        col, row = (~self.transform) * (xs[0], ys[0])
        return row - 0.5, col - 0.5

    def bounds_lonlat(self) -> tuple[float, float, float, float]:
        h, w = self.shape
        rr = np.array([-0.5, -0.5, h - 0.5, h - 0.5])
        cc = np.array([-0.5, w - 0.5, -0.5, w - 0.5])
        lons, lats = self.pix_to_lonlat(rr, cc)
        return (float(lons.min()), float(lats.min()), float(lons.max()), float(lats.max()))


@dataclass
class SceneData:
    """Surface reflectance (0..1, NaN where nodata) plus the SCL class band, all on one Grid."""

    scene_id: str
    date: date
    grid: Grid
    red: np.ndarray
    nir: np.ndarray
    rededge: np.ndarray
    swir: np.ndarray
    scl: np.ndarray
    green: np.ndarray | None = None
    blue: np.ndarray | None = None
    tile_id: str = ""
    cloud_cover: float = 0.0


@dataclass
class IndexStack:
    """Indices for one scene. `valid` = inside the (buffered) field and clear; indices are NaN elsewhere."""

    scene_id: str
    date: date
    grid: Grid
    ndvi: np.ndarray
    ndre: np.ndarray
    ndmi: np.ndarray
    valid: np.ndarray
    inside: np.ndarray  # buffered field mask, clouds ignored
    clear_fraction: float


@dataclass
class Layers:
    """Optional context rasters on the same grid (True = in the zone). Missing layers give 0 / None overlap."""

    sandy_soil: np.ndarray | None = None
    lowspot: np.ndarray | None = None
    hail: np.ndarray | None = None


@dataclass
class Pivot:
    center_lonlat: tuple[float, float]
    radius_m: float

    @classmethod
    def from_request(cls, d: dict | None) -> "Pivot | None":
        if not d or "center" not in d or not d.get("radius_m"):
            return None
        return cls((float(d["center"][0]), float(d["center"][1])), float(d["radius_m"]))


@dataclass
class Region:
    """Internal region record; converted to the contract's AnomalyRegion by the core."""

    mask: np.ndarray
    area_acres: float
    score: float
    extra: dict = field(default_factory=dict)
