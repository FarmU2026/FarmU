"""Per-pixel z-scores against prior seasons and connected problem regions. Pure numpy/scipy."""
from __future__ import annotations

import warnings

import numpy as np
from scipy import ndimage

Z_CAP = 10.0
SIGMA_FLOOR = 0.02        # NDVI units; keeps a very uniform field from inflating noise into z
SEASON_SD_FLOOR = 0.05    # NDVI units; with 2-3 prior seasons the spread between seasons is itself noisy


def baseline_mean(history_ndvi: list[np.ndarray]) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(np.stack(history_ndvi), axis=0)


def season_sd(history_ndvi: list[np.ndarray]) -> float:
    """Spread of the field median across prior seasons (floored)."""
    meds = [float(np.nanmedian(h)) for h in history_ndvi if np.isfinite(h).any()]
    sd = float(np.std(meds, ddof=1)) if len(meds) >= 2 else 0.0
    return max(sd, SEASON_SD_FLOOR)


def pixel_z(diff: np.ndarray) -> np.ndarray:
    """Z of (current - baseline) relative to the field's own typical departure.

    Removing the field median means a year-wide shift (weather, rotation, planting date) does not light up
    every pixel; that shift is judged separately as z_baseline and the uniform-drop gate."""
    d = diff[np.isfinite(diff)]
    if d.size == 0:
        return np.full(diff.shape, np.nan, "float32")
    med = np.median(d)
    sigma = max(1.4826 * float(np.median(np.abs(d - med))), SIGMA_FLOOR)
    return np.clip((diff - med) / sigma, -Z_CAP, Z_CAP).astype("float32")


def smooth_nan(z: np.ndarray, size: int = 3) -> np.ndarray:
    """Mean filter that ignores NaN pixels (clouds, outside the field)."""
    ok = np.isfinite(z)
    num = ndimage.uniform_filter(np.where(ok, z, 0.0), size=size, mode="nearest")
    den = ndimage.uniform_filter(ok.astype("float32"), size=size, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(ok & (den > 0.5), num / den, np.nan)
    return out.astype("float32")


def find_regions(z: np.ndarray, z_threshold: float, min_pixels: int) -> tuple[list[np.ndarray], np.ndarray]:
    """Connected areas where smoothed z <= -z_threshold, at least `min_pixels` big, largest first.
    Also returns the smoothed z map."""
    zs = smooth_nan(z)
    # smoothing denoises; the raw check stops the blur from growing the region past the stressed pixels
    bad = np.isfinite(zs) & (zs <= -z_threshold) & (z <= -z_threshold / 2)
    bad = ndimage.binary_opening(bad, structure=ndimage.generate_binary_structure(2, 1))
    labels, n = ndimage.label(bad, structure=np.ones((3, 3)))
    masks = [labels == i for i in range(1, n + 1)]
    masks = [m for m in masks if m.sum() >= min_pixels]
    masks.sort(key=lambda m: -int(m.sum()))
    return masks, zs
