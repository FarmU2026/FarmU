"""Cloud/shadow masking from the SCL band and the three crop indices. Pure numpy."""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from .grid import IndexStack, SceneData

# SCL classes kept as clear: 4 vegetation, 5 not vegetated, 6 water.
# Dropped: 0 no data, 1 saturated, 2 dark/topographic shadow, 3 cloud shadow, 7 unclassified,
# 8/9 cloud (medium/high), 10 thin cirrus, 11 snow/ice.
CLEAR_CLASSES = (4, 5, 6)
CLOUD_DILATE_PX = 2  # SCL is 20 m; grow the bad area to catch edges and shadow fringes


def clear_mask(scl: np.ndarray, dilate_px: int = CLOUD_DILATE_PX) -> np.ndarray:
    ok = np.isin(scl, CLEAR_CLASSES)
    if dilate_px > 0:
        bad = ndimage.binary_dilation(~ok, iterations=dilate_px)
        ok = ~bad
    return ok


def interior_mask(inside: np.ndarray, res_m: float, buffer_m: float) -> np.ndarray:
    """Field pixels at least `buffer_m` from the boundary (drops mixed edge pixels)."""
    if buffer_m <= 0:
        return inside.copy()
    # distance from a pixel center to the field edge (pixel centers are half a pixel inside the boundary)
    edt = ndimage.distance_transform_edt(np.pad(inside, 1, constant_values=False))[1:-1, 1:-1]
    out = inside & ((edt - 0.5) * res_m >= buffer_m - 1e-6)
    return out if out.any() else inside.copy()


def _nd(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return (a - b) / (a + b)


def compute_indices(scene: SceneData, inside: np.ndarray, interior_buffer_m: float = 10.0) -> IndexStack:
    buffered = interior_mask(inside, scene.grid.res_m, interior_buffer_m)
    clear = clear_mask(scene.scl)
    finite = np.isfinite(scene.red) & np.isfinite(scene.nir) & np.isfinite(scene.rededge) & np.isfinite(scene.swir)
    valid = buffered & clear & finite
    ndvi = np.where(valid, _nd(scene.nir, scene.red), np.nan).astype("float32")
    ndre = np.where(valid, _nd(scene.nir, scene.rededge), np.nan).astype("float32")
    ndmi = np.where(valid, _nd(scene.nir, scene.swir), np.nan).astype("float32")
    n = int(buffered.sum())
    return IndexStack(
        scene_id=scene.scene_id, date=scene.date, grid=scene.grid,
        ndvi=ndvi, ndre=ndre, ndmi=ndmi, valid=valid, inside=buffered,
        clear_fraction=float(valid.sum() / n) if n else 0.0,
    )
