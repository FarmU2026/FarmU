"""Gates: reasons not to raise a problem even when pixels look odd. Pure functions."""
from __future__ import annotations

from datetime import date, timedelta

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


# Calendar onsets are the default; a dry or hot year starts drying down earlier, so the canopy itself can also
# open the gate. Real scenes (Thurston Co. NE, 2026): corn NDVI median 0.89 late July, 0.86 on Sep 10, 0.78 on
# Sep 18, 0.35 on Oct 5. Changes since the previous pass during peak canopy stay within +-0.04.
DRYDOWN_LEAD_DAYS = 14        # the canopy test applies from this many days before the calendar onset
DRYDOWN_DELTA = -0.05         # median NDVI change since the previous pass that means "the whole canopy is drying"
HARVEST_NDVI_P50 = 0.35       # a field median this low after mid-September is harvested or dead, not a problem area


def seasonal(crop: str, d: date, *, delta_since_last: float | None = None,
             ndvi_p50: float | None = None) -> tuple[bool, bool]:
    """(senescence, harvest) by scene date and crop, opened early by the canopy itself (see constants above).
    Alfalfa is cut on a cycle, so neither applies."""
    onset = SENESCENCE_ONSET.get(crop, SENESCENCE_ONSET["other"])
    senescence = _on_or_after(d, onset)
    harvest = _on_or_after(d, HARVEST_ONSET.get(crop, HARVEST_ONSET["other"]))
    if onset is not None and not senescence:
        early = date(d.year, *onset) - timedelta(days=DRYDOWN_LEAD_DAYS)
        if d >= early and delta_since_last is not None and delta_since_last <= DRYDOWN_DELTA:
            senescence = True
    if onset is not None and not harvest and d >= date(d.year, 9, 15) and ndvi_p50 is not None             and ndvi_p50 <= HARVEST_NDVI_P50:
        senescence = harvest = True
    return senescence, harvest


def uniform_drop(diff: np.ndarray, z_baseline: float | None, z_threshold: float,
                 min_drop: float = 0.05, min_share: float = 0.6) -> bool:
    """Whole-field drop vs the same weeks in prior seasons: the field median is unusually low AND most pixels
    dropped together. That points at weather or a region-wide cause, not a problem area inside this field.
    `diff` is current minus baseline NDVI over valid pixels."""
    d = diff[np.isfinite(diff)]
    if z_baseline is None or d.size == 0:
        return False
    return bool(z_baseline <= -z_threshold and (d < -min_drop).mean() >= min_share)


# States after which the season's stress diagnosis is over (phenology.py); they open the legacy gates too.
SENESCENT_STATES = ("senescing", "harvest_in_progress", "harvested")
HARVEST_STATES = ("harvest_in_progress", "harvested")


def fold_state(senescence: bool, harvest: bool, state: str) -> tuple[bool, bool]:
    """Fold the crop state into the legacy (senescence, harvest) flags: the state can only open a gate, never close one,
    so everything the calendar rules held back stays held back."""
    return senescence or state in SENESCENT_STATES, harvest or state in HARVEST_STATES
