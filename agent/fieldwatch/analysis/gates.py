"""Gates: reasons not to raise a problem even when pixels look odd. Pure functions."""
from __future__ import annotations

from datetime import date

import numpy as np

# (month, day) when a crop's canopy normally starts to dry down, and when harvest normally starts (Midwest).
SENESCENCE_ONSET = {
    "corn": (9, 5), "soybeans": (9, 1), "milo": (9, 15), "wheat": (6, 25), "other": (9, 10), "alfalfa": None,
}
HARVEST_ONSET = {
    "corn": (10, 5), "soybeans": (9, 25), "milo": (10, 5), "wheat": (7, 10), "other": (10, 5), "alfalfa": None,
}


def _on_or_after(d: date, md: tuple[int, int] | None) -> bool:
    return md is not None and (d.month, d.day) >= md


def seasonal(crop: str, d: date) -> tuple[bool, bool]:
    """(senescence, harvest) by scene date and crop. Alfalfa is cut on a cycle, so neither applies."""
    return (
        _on_or_after(d, SENESCENCE_ONSET.get(crop, SENESCENCE_ONSET["other"])),
        _on_or_after(d, HARVEST_ONSET.get(crop, HARVEST_ONSET["other"])),
    )


def uniform_drop(diff: np.ndarray, z_baseline: float | None, z_threshold: float,
                 min_drop: float = 0.05, min_share: float = 0.6) -> bool:
    """Whole-field drop vs the same weeks in prior seasons: the field median is unusually low AND most pixels
    dropped together. That points at weather or a region-wide cause, not a problem area inside this field.
    `diff` is current minus baseline NDVI over valid pixels."""
    d = diff[np.isfinite(diff)]
    if z_baseline is None or d.size == 0:
        return False
    return bool(z_baseline <= -z_threshold and (d < -min_drop).mean() >= min_share)
