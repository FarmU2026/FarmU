"""Crop state: what the crop is doing on the scene date, and whether stress diagnosis is allowed. Pure functions.

    classify_state(StateEvidence(...)) -> contracts.CropState

States and what they allow (research memo docs/research/grounding.md, 3.1 and 4; founder decisions):

    pre_emergence        not diagnosable   nothing to diagnose yet (GDD below emergence and no green canopy, or bare soil)
    vegetative           diagnosable       emergence to tassel (corn) / R1 (soybeans), by GDD stage
    reproductive         diagnosable       tassel/R1 until the late window; stress diagnosis allowed
    reproductive + late  hail_only         late window before senescence: only the hail exception may alert
    senescing            not diagnosable   from the start of maturity to the end of the season, one-way; silent
    harvest_in_progress  not diagnosable   a straight-edged block of harvest-level pixels exists
    harvested            not diagnosable   >= 90% of the field is at harvest level, or an earlier pass already was
    unknown              diagnosable       nothing known and nothing late: today's mid-season behavior is kept

Precedence (first match wins; every later rule is only reached if the earlier ones did not fire):
    1. alfalfa            -> unknown (cut on a cycle; no season to model)
    2. harvested          (imagery: harvest finding says so, or an earlier pass this season was harvested; sticky)
    3. harvest_in_progress (imagery: straight-edged drop blocks)
    4. senescing          any ONE vote: the legacy calendar/dry-down gate (gates.seasonal); GDD >= 85% of maturity;
                          the field median fell >= 0.05 below its own seasonal peak inside the late window, unless a
                          sudden patchy drop (hail) explains the fall
    5. pre_emergence      GDD below emergence without a green canopy, or (no GDD) bare soil early in the season
    6. stage              GDD -> vegetative / reproductive with a label; in the late window (calendar onset minus 14 days,
                          or GDD >= 75% of maturity) the state is reproductive with diagnosable=False, hail_only=True;
                          "hail_exception=True" is added to `reasons` when a sudden drop on a clean previous pass exists
    7. unknown            no planting date / GDD and not late: diagnosable, as before

Imagery outranks the calendar and GDD: GDD is good to about +-150 degree days of hybrid spread and NDVI timing to
about +-1 week (memo), so each is one vote, never a gate on its own, except that imagery of harvest wins outright.

Planting date: the farmer's, else the state's median 50%-planted date (planting_defaults), refined from the satellite
green-up when a bracketing pair of passes exists. The source is recorded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from .. import contracts as C
from . import gates
from .harvest import HARVESTED_NDVI, HarvestFinding, SuddenDrop

# ---- thresholds: ONE block; [design] = FieldWatch starting value to tune, otherwise the memo / Extension source ----
GDD_BASE_F = {"corn": 50.0, "soybeans": 50.0}   # corn: 86/50 method (memo 3.1, 4); soybeans: same, [design] (the memo gives no soybean table)
GDD_CAP_F = {"corn": 86.0, "soybeans": 86.0}
EMERGENCE_GDD = 100.0            # memo 3.1: emergence 90-120 GDD after planting
MATURITY_GDD = {"corn": 2700.0,  # planting to R6; memo: KY example 2820, hybrid spread +-150 [design default]
                "soybeans": 2350.0}  # [design] planting to R7 for the central Corn Belt
SENESCING_GDD_FRAC = 0.85        # memo 3.1 table: GDD >= about 85-90% of maturity
LATE_GDD_FRAC = 0.75             # [design] grain fill: baseline comparison stops being reliable, hail only
LATE_DATE_VETO_GDD_FRAC = 0.60   # [design] a late-planted field below this is not "late" on the calendar alone
SENESCENCE_PEAK_DECLINE = 0.05   # memo 3.1 table: NDVI >= 0.05 below the field's own seasonal peak
GREEN_UP_NDVI = 0.30             # [design] field median NDVI that counts as "green-up" (emerged canopy)
GREEN_UP_LAG_DAYS = 7            # [design] emergence to a 0.30 field median
PRE_EMERGENCE_NDVI = 0.25        # [design] bare-soil field median (no GDD available)
PRE_EMERGENCE_GREEN_NDVI = 0.40  # [design] GDD says not emerged but the field median is above this: imagery wins
PRE_EMERGENCE_LATEST = (6, 15)   # [design] after this date a low field median is a failed stand, not "not emerged yet"
PLANTING_TO_EMERGENCE_GDD = 110.0  # memo 4: planting = green-up minus about 100-125 GDD
GREEN_UP_MAX_GAP_DAYS = 21       # [design] the bracketing passes must be this close
PLANTING_WINDOW = ((4, 1), (7, 10))  # [design] refined/accepted planting dates fall in this month-day window

# GDD (from planting) -> stage anchors. Corn: VE 90-120 (memo); V6 475, V12 870, VT 1135, R1 1400, R2 1660, R3 1925,
# R4 2190, R5 2450, R6 2700 are the Iowa State Extension table values as I recall them [design: not re-verified here];
# the memo gives 1350 to mid-pollination and 2820 to black layer for one 116-day hybrid. Soybeans: [design], approximate.
CORN_VEG = [(EMERGENCE_GDD, 0.0), (475.0, 6.0), (870.0, 12.0), (1135.0, 17.0)]   # (gdd, leaf number)
CORN_REPRO = [(1135.0, "VT"), (1400.0, "R1"), (1660.0, "R2"), (1925.0, "R3"), (2190.0, "R4"), (2450.0, "R5"), (2700.0, "R6")]
SOY_VEG = [(120.0, 0.0), (330.0, 3.0), (560.0, 6.0), (800.0, 9.0)]
SOY_REPRO = [(800.0, "R1"), (950.0, "R2"), (1150.0, "R3"), (1350.0, "R4"), (1600.0, "R5"), (1950.0, "R6"), (2350.0, "R7")]

HAIL_EXCEPTION = "hail_exception=True"  # decide.py reads this exact token out of CropState.reasons

DayTemp = tuple[date, "float | None", "float | None"]  # (day, tmin_F, tmax_F)


# ---- growing degree days --------------------------------------------------------------------------------------
def day_gdd(tmin_f: float, tmax_f: float, crop: str = "corn") -> float:
    """86/50 method: highs capped at 86 F and lows raised to 50 F, then the mean above 50 F."""
    base, cap = GDD_BASE_F.get(crop, 50.0), GDD_CAP_F.get(crop, 86.0)
    hi = min(max(tmax_f, base), cap)
    lo = min(max(tmin_f, base), cap)
    return (hi + lo) / 2.0 - base


def gdd_between(days: list[DayTemp], start: date, end: date, crop: str = "corn") -> float | None:
    """Degree days from `start` through `end` (both included). Days with no temperature are skipped; None when no
    day in the range has data."""
    total, seen = 0.0, 0
    for d, lo, hi in days:
        if start <= d <= end and lo is not None and hi is not None:
            total += day_gdd(float(lo), float(hi), crop)
            seen += 1
    return round(total, 1) if seen else None


def stage_for(crop: str, gdd: float) -> tuple[str, str]:
    """(state, label) for corn or soybeans at `gdd` since planting: ("vegetative", "V8 (est.)") or ("reproductive", "R2 (est.)")."""
    veg, repro = (CORN_VEG, CORN_REPRO) if crop == "corn" else (SOY_VEG, SOY_REPRO)
    if gdd < veg[0][0]:
        return "vegetative", "VE (est.)"
    if gdd < repro[0][0]:
        n = float(np.interp(gdd, [g for g, _ in veg], [v for _, v in veg]))
        return "vegetative", f"V{int(n)} (est.)" if int(n) > 0 else "VE (est.)"
    label = [lab for g, lab in repro if gdd >= g][-1]
    return "reproductive", f"{label} (est.)"


def maturity_fraction(crop: str, gdd: float | None) -> float | None:
    mature = MATURITY_GDD.get(crop)
    return None if gdd is None or mature is None else gdd / mature


# ---- NDVI season curve ------------------------------------------------------------------------------------------
def field_median(ndvi: np.ndarray, valid: np.ndarray) -> float | None:
    v = ndvi[valid & np.isfinite(ndvi)]
    return float(np.median(v)) if v.size else None


def green_up_date(curve: list[tuple[date, float]]) -> date | None:
    """Date the field median first crosses GREEN_UP_NDVI, interpolated between the two passes that bracket it
    (None unless a pass below is followed within GREEN_UP_MAX_GAP_DAYS by one above)."""
    for (d0, v0), (d1, v1) in zip(curve, curve[1:]):
        if v0 < GREEN_UP_NDVI <= v1 and (d1 - d0).days <= GREEN_UP_MAX_GAP_DAYS:
            frac = (GREEN_UP_NDVI - v0) / (v1 - v0)
            return d0 + timedelta(days=round(frac * (d1 - d0).days))
    return None


def planting_from_green_up(curve: list[tuple[date, float]], days: list[DayTemp], crop: str) -> date | None:
    """Back out a planting date: green-up, minus the lag to emergence, minus the days needed for ~110 GDD before it."""
    g = green_up_date(curve)
    if g is None:
        return None
    day = g - timedelta(days=GREEN_UP_LAG_DAYS)
    by_day = {d: (lo, hi) for d, lo, hi in days}
    acc = 0.0
    while acc < PLANTING_TO_EMERGENCE_GDD and day > g - timedelta(days=60):
        lo, hi = by_day.get(day, (None, None))
        if lo is not None and hi is not None:
            acc += day_gdd(float(lo), float(hi), crop)
        day -= timedelta(days=1)
    if acc < PLANTING_TO_EMERGENCE_GDD:
        return None
    start, end = (date(g.year, *PLANTING_WINDOW[0]), date(g.year, *PLANTING_WINDOW[1]))
    return day + timedelta(days=1) if start <= day <= end else None


@dataclass
class Planting:
    """Where the planting date came from and the degree days since it. All None/unknown when nothing is known."""

    date: date | None = None
    source: str = "unknown"          # farmer | state_default | green_up | unknown
    gdd: float | None = None
    notes: list[str] = field(default_factory=list)


# ---- classification ---------------------------------------------------------------------------------------------
@dataclass
class StateEvidence:
    crop: str
    as_of: date
    p50: float | None                     # field median NDVI on the scene
    peak: float | None = None             # highest field median NDVI this season including this scene
    earlier: list[tuple[date, float]] = field(default_factory=list)  # earlier passes this season (date, field median NDVI)
    gdd: float | None = None
    planting_date: date | None = None
    planting_source: str = "unknown"
    legacy_senescence: bool = False       # gates.seasonal: calendar onset, early dry-down, or low median late
    harvest: HarvestFinding | None = None
    sudden_drop: SuddenDrop | None = None
    notes: list[str] = field(default_factory=list)


def _onset(crop: str, year: int) -> date | None:
    md = gates.SENESCENCE_ONSET.get(crop, gates.SENESCENCE_ONSET["other"])
    return date(year, *md) if md else None


def late_window(crop: str, as_of: date, gdd: float | None = None) -> bool:
    """The late season, before and including senescence: calendar onset minus 14 days (the same lead the dry-down test
    uses) or GDD >= 75% of maturity. A late-planted field (GDD known and low) is not late on the calendar alone."""
    frac = maturity_fraction(crop, gdd)
    if frac is not None and frac >= LATE_GDD_FRAC:
        return True
    onset = _onset(crop, as_of.year)
    if onset is None:
        return False
    by_date = as_of >= onset - timedelta(days=gates.DRYDOWN_LEAD_DAYS)
    return by_date and not (frac is not None and frac < LATE_DATE_VETO_GDD_FRAC)


def earlier_harvest(crop: str, earlier: list[tuple[date, float]]) -> date | None:
    """First earlier pass this season that was already harvest-level (late, field median under HARVESTED_NDVI after a
    mature canopy). Makes `harvested` sticky: a later cover-crop green-up must not re-arm diagnosis."""
    seen_peak = 0.0
    for d, v in earlier:
        if late_window(crop, d) and v < HARVESTED_NDVI and seen_peak - v >= 0.25:
            return d
        seen_peak = max(seen_peak, v)
    return None


def classify_state(ev: StateEvidence) -> C.CropState:
    gdd = ev.gdd
    base = dict(gdd_since_planting=None if gdd is None else round(gdd), planting_date_source=ev.planting_source,
                planting_date=ev.planting_date.isoformat() if ev.planting_date else None)
    reasons = list(ev.notes)
    frac = maturity_fraction(ev.crop, gdd)

    def done(state, *, diagnosable, label=None, hail_only=False, why=()):
        return C.CropState(state=state, stage_label=label, diagnosable=diagnosable, hail_only=hail_only,
                           reasons=[*reasons, *why], **base)

    if ev.crop == "alfalfa":
        return done("unknown", diagnosable=True, why=["alfalfa is cut on a cycle; no season model"])

    h = ev.harvest
    earlier = earlier_harvest(ev.crop, ev.earlier)
    if (h and h.harvested) or earlier:
        why = (f"{round(100 * h.low_share)}% of the field is at harvest level on {ev.as_of}" if h and h.harvested
               else f"the field was already at harvest level on {earlier}")
        return done("harvested", diagnosable=False, why=[why])
    if h and h.in_progress:
        return done("harvest_in_progress", diagnosable=False,
                    why=[f"harvest passes detected on {ev.as_of}: {h.acres:g} acres cut in straight-edged blocks"])

    sd = ev.sudden_drop
    decline = None if ev.peak is None or ev.p50 is None else ev.peak - ev.p50
    in_late = late_window(ev.crop, ev.as_of, gdd)
    votes = []
    if ev.legacy_senescence:
        votes.append("past the usual start of drying down for this crop")
    if frac is not None and frac >= SENESCING_GDD_FRAC:
        votes.append(f"{round(100 * frac)}% of the heat units to maturity have accumulated")
    if decline is not None and decline >= SENESCENCE_PEAK_DECLINE and in_late and sd is None:
        votes.append(f"the field's crop health is {decline:.2f} below its own peak this season")
    if votes:
        return done("senescing", diagnosable=False, why=votes)

    if gdd is not None and gdd < EMERGENCE_GDD:
        if ev.p50 is None or ev.p50 < PRE_EMERGENCE_GREEN_NDVI:
            return done("pre_emergence", diagnosable=False,
                        why=[f"{round(gdd)} heat units since planting, below the ~{round(EMERGENCE_GDD)} needed to emerge"])
        reasons.append("the satellite shows a green canopy earlier than the planting date implies")
    elif gdd is None and ev.p50 is not None and ev.p50 < PRE_EMERGENCE_NDVI and not in_late \
            and (ev.as_of.month, ev.as_of.day) <= PRE_EMERGENCE_LATEST:
        return done("pre_emergence", diagnosable=False, why=["the field is still bare soil"])

    if in_late:
        why = ["late in the season: only a sudden hail-like drop on a clean previous pass may alert"]
        if sd is not None:
            why += [HAIL_EXCEPTION, f"a patch of {sd.acres:g} acres lost {abs(sd.median_drop):.2f} crop health "
                                    f"since the clean pass on {sd.previous_date}"]
        label = stage_for(ev.crop, gdd)[1] if gdd is not None and ev.crop in MATURITY_GDD else None
        return done("reproductive", diagnosable=False, hail_only=True, label=label, why=why)

    if gdd is not None and ev.crop in MATURITY_GDD:
        state, label = stage_for(ev.crop, gdd)
        return done(state, diagnosable=True, label=label)
    return done("unknown", diagnosable=True, why=["no planting date or heat-unit count; stage not estimated"])
