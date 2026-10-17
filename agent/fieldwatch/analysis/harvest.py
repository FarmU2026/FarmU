"""Harvest and sudden-drop detection by change (research memo docs/research/grounding.md, change 3 and 3.5). Pure numpy.

Harvest is not a whole-field median: a half-harvested field keeps a high median. It is an abrupt per-pixel NDVI drop,
from a mature-canopy value, in coherent blocks with long straight edges (combine passes). One such block makes the
field `harvest_in_progress`; most of the field low makes it `harvested`.

The same machinery finds the one late-season exception, a sudden patchy drop on a still-green field (hail).

All thresholds are the [design] starting values from the memo: tune them here, in one place.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .grid import IndexStack

# ---- thresholds -----------------------------------------------------------------------------------------------
HARVEST_DROP_NDVI = 0.25         # memo 3.1 table [design]: drop between clear passes (Thurston corn fell 0.43; peak noise +-0.04)
HARVEST_PRE_NDVI_MIN = 0.45      # [design] pre-drop value must be a mature canopy (dried-down corn still sits above ~0.5)
HARVEST_POST_NDVI_MAX = 0.45     # [design] dropped pixels end at stubble/bare level (Thurston harvested corn 0.31-0.36)
HARVEST_MIN_ACRES = 2.0          # memo 3.1 table: "about 2 acres" of coherent pixels
HARVEST_STRAIGHT_SHARE = 0.55    # [design] share of a block's outline that is in long straight runs
HARVEST_STRAIGHT_MIN_PX = 5      # [design] a "long" straight run: 5 px = 50 m (combine header is ~1 px wide)
HARVEST_DOMINANT_EDGES = 0.75    # [design] share of the outline in the 4 longest edges (a block has a few; a round blob has many short ones)
HARVEST_WHOLE_FIELD_SHARE = 0.8  # [design] a block covering this much of the field needs no straight-edge proof
HARVESTED_NDVI = 0.40            # memo 3.1 table: harvest-level NDVI
HARVESTED_SHARE = 0.90           # memo 3.1 table: >= 90% of valid pixels below HARVESTED_NDVI
HAIL_DROP_NDVI = 0.12            # memo 3.5 [design]: 0.10-0.15 over one revisit, relative to the field-wide change
HAIL_PREV_SLACK = 0.05           # [design] the dropped pixels were within this of the field median on the previous pass
HAIL_MAX_GAP_DAYS = 21           # [design] the clean pass must be this recent (memo: stale passes prefer silence)
SMOOTH_OPEN = ndimage.generate_binary_structure(2, 1)


@dataclass
class HarvestFinding:
    low_share: float          # share of valid pixels under HARVESTED_NDVI
    dropped_share: float      # share of valid pixels inside accepted drop blocks
    acres: float              # area of the accepted drop blocks
    blocks: int
    mask: np.ndarray | None = None

    @property
    def harvested(self) -> bool:
        return self.low_share >= HARVESTED_SHARE or self.dropped_share >= HARVESTED_SHARE

    @property
    def in_progress(self) -> bool:
        return self.blocks > 0 and not self.harvested


@dataclass
class SuddenDrop:
    mask: np.ndarray
    acres: float
    median_drop: float        # median NDVI change inside the block, relative to the field-wide change (negative)
    previous_date: str


def _blocks(mask: np.ndarray, min_px: int) -> list[np.ndarray]:
    opened = ndimage.binary_opening(mask, structure=SMOOTH_OPEN)
    labels, n = ndimage.label(opened, structure=np.ones((3, 3)))
    return [m for m in (labels == i for i in range(1, n + 1)) if int(m.sum()) >= min_px]


def edge_straightness(mask: np.ndarray) -> tuple[float, float]:
    """(share of the outline in long straight runs, share in its 4 longest edges).

    A combine-cut block is bounded by a few long straight edges; a round hail patch or a noise blob simplifies to many
    short ones. The outline is simplified (Douglas-Peucker, 1 px) so a diagonal row direction still gives straight runs."""
    from rasterio.features import shapes
    from shapely.geometry import Polygon

    polys = [Polygon(g["coordinates"][0]) for g, v in shapes(mask.astype("uint8"), mask=mask, connectivity=8) if v]
    if not polys:
        return 0.0, 0.0
    ring = np.asarray(max(polys, key=lambda p: p.area).simplify(1.0).exterior.coords)
    seg = np.diff(ring, axis=0)
    length = np.hypot(seg[:, 0], seg[:, 1])
    total = float(length.sum())
    if total <= 0:
        return 0.0, 0.0
    longest = np.sort(length)[::-1][:4]
    return float(length[length >= HARVEST_STRAIGHT_MIN_PX].sum() / total), float(longest.sum() / total)


def detect_harvest(current: IndexStack, ref_ndvi: np.ndarray | None, *, late: bool,
                   min_acres: float = HARVEST_MIN_ACRES) -> HarvestFinding | None:
    """Look for harvest on `current`. `ref_ndvi` is the per-pixel NDVI before it (the best of the earlier passes this
    season, so a block cut a pass ago is still seen); None when there is no earlier pass. Only runs `late` in the
    season. Returns None when nothing points at harvest."""
    if not late:
        return None
    valid = current.valid & np.isfinite(current.ndvi)
    n_valid = int(valid.sum())
    if n_valid == 0:
        return None
    cur = current.ndvi
    low_share = float((valid & (cur < HARVESTED_NDVI)).sum() / n_valid)
    kept = np.zeros(valid.shape, bool)
    blocks = 0
    if ref_ndvi is not None:
        ok = valid & np.isfinite(ref_ndvi)
        with np.errstate(invalid="ignore"):
            cand = ok & (ref_ndvi - cur >= HARVEST_DROP_NDVI) & (ref_ndvi >= HARVEST_PRE_NDVI_MIN) \
                & (cur <= HARVEST_POST_NDVI_MAX)
        min_px = max(1, math.ceil(min_acres / current.grid.pixel_acres))
        for m in _blocks(cand, min_px):
            if m.sum() >= HARVEST_WHOLE_FIELD_SHARE * n_valid:
                straight, dominant = 1.0, 1.0
            else:
                straight, dominant = edge_straightness(m)
            if straight >= HARVEST_STRAIGHT_SHARE and dominant >= HARVEST_DOMINANT_EDGES:
                kept |= m
                blocks += 1
    if not blocks and low_share < HARVESTED_SHARE:
        return None
    return HarvestFinding(low_share=round(low_share, 3), dropped_share=round(float(kept.sum()) / n_valid, 3),
                          acres=round(float(kept.sum()) * current.grid.pixel_acres, 2), blocks=blocks, mask=kept)


def detect_sudden_drop(current: IndexStack, previous: IndexStack | None, *, min_acres: float,
                       previous_clean: bool = True) -> SuddenDrop | None:
    """A patchy NDVI drop since the previous clear pass that the rest of the field did not share (hail-like).

    The previous pass must be recent and clean: the dropped pixels were healthy on it (no earlier weak patch, so this
    is new) and `previous_clean` (no problem region on it, decided by the caller against the baseline)."""
    if previous is None or not previous_clean or (current.date - previous.date).days > HAIL_MAX_GAP_DAYS:
        return None
    both = current.valid & previous.valid
    if not both.any():
        return None
    change = np.where(both, current.ndvi - previous.ndvi, np.nan)
    rel = change - np.nanmedian(change)
    prev_med = float(np.nanmedian(previous.ndvi[both]))
    with np.errstate(invalid="ignore"):
        cand = both & (rel <= -HAIL_DROP_NDVI) & (previous.ndvi >= prev_med - HAIL_PREV_SLACK)
    min_px = max(1, math.ceil(min_acres / current.grid.pixel_acres))
    blocks = _blocks(cand, min_px)
    if not blocks:
        return None
    mask = np.any(blocks, axis=0)
    return SuddenDrop(mask=mask, acres=round(float(mask.sum()) * current.grid.pixel_acres, 2),
                      median_drop=round(float(np.median(rel[mask])), 3), previous_date=previous.date.isoformat())
