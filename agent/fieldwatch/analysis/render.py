"""Overlay PNGs: true color, crop health (NDVI), a mask of problem areas / unusable pixels, and the three that show
what the decision compares: departure from this field's normal, change since the previous pass, and the normal itself.
All are RGBA, transparent outside the field, north-up on the grid's lon/lat bounds.

Fixed color scales (so images from different dates and fields are comparable; the same numbers go to the UI in
`contracts.OverlayComparison`):
  departure  z of (NDVI now - baseline), 3x3 smoothed, the map the regions are found on: -3 .. +3
             (regions open at z <= -2). Dark red = 3 or more below, light neutral = 0, dark blue = 3 or more above.
             z is relative to the field's own typical departure, so a year-wide shift shows in the caption's
             median departure, not as color.
  change     NDVI now - NDVI at the previous clear pass: -0.25 .. +0.25 (same colors).
  normal     baseline NDVI with the `health` palette.
Gray = in the field but no usable value (clouds, shadow, buffer, or no data for that comparison)."""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

from .grid import IndexStack, SceneData

TARGET_PX = 512
DEPARTURE_Z_RANGE = 3.0      # departure: z at the strongest color
CHANGE_NDVI_RANGE = 0.25     # change: NDVI difference at the strongest color
INVALID_RGBA = (120, 120, 120, 140)
DIVERGING_ALPHA = 235
# (position -1..+1, rgb): red and orange below normal, light neutral at 0, teal to blue above.
_DIVERGING = [(-1.0, (165, 15, 21)), (-0.5, (239, 120, 70)), (0.0, (242, 240, 232)),
              (0.5, (110, 190, 200)), (1.0, (33, 82, 168))]


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


def _health_rgba(ndvi: np.ndarray, usable: np.ndarray, field_mask: np.ndarray) -> np.ndarray:
    t = np.clip((np.nan_to_num(ndvi, nan=-0.1) + 0.1) / 0.9, 0, 1)  # -0.1 bare .. 0.8 lush
    rgb = np.dstack([(1 - t) * 180 + 40, t * 160 + 60, np.full_like(t, 40)]).astype("uint8")
    alpha = np.where(field_mask & usable, 255, np.where(field_mask, 70, 0)).astype("uint8")
    return np.dstack([rgb, alpha])


def health(stack: IndexStack, field_mask: np.ndarray) -> bytes:
    return _png(_health_rgba(stack.ndvi, stack.valid, field_mask))


def normal(baseline: np.ndarray, field_mask: np.ndarray) -> bytes:
    """The baseline NDVI (this field's expected value for the date) in the `health` palette."""
    return _png(_health_rgba(baseline, np.isfinite(baseline), field_mask))


def diverging_rgb(value: np.ndarray, vmax: float) -> np.ndarray:
    """(h, w, 3) uint8 colors for `value` on the fixed scale -vmax..+vmax (NaN is drawn as neutral; mask it yourself)."""
    t = np.clip(np.nan_to_num(value, nan=0.0) / vmax, -1.0, 1.0)
    xs = [p for p, _ in _DIVERGING]
    return np.dstack([np.interp(t, xs, [c[i] for _, c in _DIVERGING]) for i in range(3)]).round().astype("uint8")


def _diverging(value: np.ndarray, field_mask: np.ndarray, vmax: float) -> bytes:
    ok = field_mask & np.isfinite(value)
    out = np.dstack([diverging_rgb(value, vmax), np.full(value.shape, DIVERGING_ALPHA, "uint8")])
    out[field_mask & ~ok] = INVALID_RGBA
    out[~field_mask] = 0
    return _png(out)


def departure(z: np.ndarray, field_mask: np.ndarray) -> bytes:
    """Per-pixel z vs this field's normal (CoreResult.z), fixed scale +-DEPARTURE_Z_RANGE."""
    return _diverging(z, field_mask, DEPARTURE_Z_RANGE)


def change(delta: np.ndarray, field_mask: np.ndarray) -> bytes:
    """Per-pixel NDVI now - previous clear pass (CoreResult.change), fixed scale +-CHANGE_NDVI_RANGE."""
    return _diverging(delta, field_mask, CHANGE_NDVI_RANGE)


def mask(stack: IndexStack, field_mask: np.ndarray, region_masks: list[np.ndarray]) -> bytes:
    h, w = stack.valid.shape
    out = np.zeros((h, w, 4), "uint8")
    out[field_mask & ~stack.valid] = INVALID_RGBA  # clouds, shadow, buffer
    for m in region_masks:
        out[m] = (230, 40, 40, 150)
    return _png(out)
