"""Overlay PNGs: true color, crop health (NDVI), and a mask of problem areas / unusable pixels.
All are RGBA, transparent outside the field, north-up on the grid's lon/lat bounds."""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

from .grid import IndexStack, SceneData

TARGET_PX = 512


def _png(rgba: np.ndarray) -> bytes:
    h, w = rgba.shape[:2]
    k = max(1, TARGET_PX // max(h, w))
    img = Image.fromarray(rgba, "RGBA")
    if k > 1:
        img = img.resize((w * k, h * k), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _stretch(x: np.ndarray, ok: np.ndarray) -> np.ndarray:
    v = x[ok & np.isfinite(x)]
    if v.size == 0:
        return np.zeros(x.shape, "float32")
    lo, hi = np.percentile(v, 2), np.percentile(v, 98)
    return np.nan_to_num(np.clip((x - lo) / max(hi - lo, 1e-6), 0, 1))


def true_color(scene: SceneData, field_mask: np.ndarray) -> bytes:
    ok = field_mask
    g = scene.green if scene.green is not None else scene.red
    b = scene.blue if scene.blue is not None else scene.red
    rgb = np.dstack([_stretch(scene.red, ok), _stretch(g, ok), _stretch(b, ok)])
    alpha = np.where(field_mask, 255, 0).astype("uint8")
    return _png(np.dstack([(rgb * 255).astype("uint8"), alpha]))


def health(stack: IndexStack, field_mask: np.ndarray) -> bytes:
    t = np.clip((np.nan_to_num(stack.ndvi, nan=-0.1) + 0.1) / 0.9, 0, 1)  # -0.1 bare .. 0.8 lush
    rgb = np.dstack([(1 - t) * 180 + 40, t * 160 + 60, np.full_like(t, 40)]).astype("uint8")
    alpha = np.where(field_mask & stack.valid, 255, np.where(field_mask, 70, 0)).astype("uint8")
    return _png(np.dstack([rgb, alpha]))


def mask(stack: IndexStack, field_mask: np.ndarray, region_masks: list[np.ndarray]) -> bytes:
    h, w = stack.valid.shape
    out = np.zeros((h, w, 4), "uint8")
    out[field_mask & ~stack.valid] = (120, 120, 120, 140)  # clouds, shadow, buffer
    for m in region_masks:
        out[m] = (230, 40, 40, 150)
    return _png(out)
