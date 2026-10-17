"""The evidence engine: every evidence key in the catalog, evaluated in code, every run (grounding memo 5.4).

`evaluate(FieldInputs)` walks the whole catalog and resolves each (condition, key) to one of

    present        the fact holds; it supports or refutes the condition (the direction is the catalog's)
    absent         the data is there and the fact does not hold
    not_evaluable  the data needed is missing (no photo, no weather, no region, outside the stage window ...)

Same inputs, same vector: nothing here reads a clock, a network or a model. An LLM never picks evidence keys. It may
add evidence only for keys whose catalog source is `photo` or `farmer`, as fixed fields (see `FarmerInputs`,
`PhotoFindings` and `agents.EvidenceLedger`).

Evidence classes (set per key in catalog/conditions.json):
    field     from this field's own pixels, geometry and event overlap, or an observation of this field (photo, farmer)
    static    local context that does not change with the season: soil, terrain, flood zone, crop history
    regional  weather, drought category and county disease reports

How each key is computed is the `how` text of the key in the catalog (REVIEW.md prints it); the thresholds are the
constants below. `FieldInputs` is plain data: `field_inputs.py` fills it from the stores, tests build it by hand.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Callable, Literal

from . import contracts as C
from . import scoring

# ---- thresholds ([design] unless a source is named; the catalog `how` text states them in words) ----
RING_STRONG, RING_WEAK = 0.6, 0.3  # analyzer pattern scores, 0-1
WEDGE_STRONG = EDGE_STRONG = STRIPE_STRONG = 0.6
EDGE_NONE, STRIPE_NONE = 0.25, 0.2
LOWSPOT_HIGH, LOWSPOT_NONE = 0.5, 0.15
SANDY_HIGH, SANDY_NONE, SANDY_ZONES = 0.5, 0.15, 0.7
HAIL_SWATH_HIGH, HAIL_SWATH_NONE = 0.5, 0.1
UNIFORM_SHARE = 0.7  # the problem area covers this share of the field acres: stress is field-wide
PATCHY_MAX_SCORE = 0.3
HAIL_LARGE_MM, HAIL_MODERATE_MM = 25.0, 12.0  # catalog: "at or above 25 mm", "12-25 mm"
SUDDEN_DROP = -0.10  # memo 3.5: abrupt drop of about 0.10-0.15 over one revisit
RAIN_DEFICIT_BELOW, RAIN_NORMAL_RANGE, RAIN_EXCESS_AT = 0.6, (0.7, 1.3), 1.5
HEAVY_RAIN_7D_MM = 50.0
HEAT_DAYS = 3
WINDY_KPH, WINDY_DAYS = 24.0, 2
HUMID_RAIN_RATIO, HUMID_TMAX_C = 1.3, (20.0, 32.0)
SOIL_SHARE = 0.5  # share of the field a soil / flood class must cover
LOW_AWC_CM = 12.0  # area-weighted available water capacity, top 100 cm
STREAM_NEAR_M = 100.0
SI_STRONG, SI_BORDERLINE = 0.90, 0.95  # memo 3.2 (Varvel 0.95; 0.90 strong); unverified for satellite pixels

State = Literal["present", "absent", "not_evaluable"]


# ---------- inputs ----------


@dataclass
class FarmerInputs:
    """What the farmer told us, as fixed fields (a farmer's free text is turned into these, never read as keys)."""

    n_applied: Literal["short", "full"] | None = None
    pivot: Literal["down", "ok"] | None = None
    neighbor_spray: bool | None = None
    planter_issue: bool | None = None
    n_rate_lb_ac: float | None = None  # recorded for the farm record; interpreted into n_applied by the caller


@dataclass
class PhotoFindings:
    """A photo (or a scout's note) read into the closed checklist: the photo weight_keys that were seen."""

    taken: bool = False
    keys: frozenset[str] = frozenset()


@dataclass
class FieldInputs:
    field_id: str = ""
    field_name: str = ""
    crop: str = "other"
    irrigated: bool | None = None
    acres: float | None = None
    stage: str | None = None  # growth stage in words, used when crop_state has no label
    as_of: str | None = None  # ISO date; defaults to the analysis date
    analysis: C.FieldAnalysisResult | None = None
    crop_state: C.CropState | None = None  # falls back to analysis.crop_state
    region_ndre: float | None = None  # red-edge index of the problem area (median); needed for the nitrogen SI
    stripe_width_matches: bool | None = None
    context: C.FieldContext | None = None
    weather: Any = None  # C.WeatherSummary, or the get_weather tool's dict
    hail: C.HailReport | None = None
    hail_watches: list[dict] | None = None  # None = hail was not checked; [] = checked, none open
    drought: C.DroughtStatus | None = None
    disease_reports: list[C.DiseaseReport] | None = None  # None = not checked
    farmer: FarmerInputs = field(default_factory=FarmerInputs)
    photo: PhotoFindings = field(default_factory=PhotoFindings)

    # -- derived
    def state(self) -> C.CropState | None:
        return self.crop_state or (self.analysis.crop_state if self.analysis else None)

    def stage_label(self) -> str | None:
        cs = self.state()
        return (cs.stage_label if cs and cs.stage_label else None) or self.stage or (self.context.stage if self.context else None)

    def stage_span(self) -> tuple[float, float] | None:
        cs = self.state()
        return scoring.stage_span(self.stage_label(), cs.state if cs else None)

    def date(self) -> str | None:
        return self.as_of or (self.analysis.date if self.analysis else None)

    def region(self) -> C.AnomalyRegion | None:
        a = self.analysis
        regions = a.anomaly.regions if a is not None and a.status == "ok" and a.anomaly else []
        return max(regions, key=lambda r: (r.score, r.region_id)) if regions else None


# ---------- one fact ----------


@dataclass(frozen=True)
class Fact:
    state: State
    value: Any = None
    reason: str = ""


def _yes(value: Any, reason: str) -> Fact:
    return Fact("present", value, reason)


def _no(value: Any, reason: str = "") -> Fact:
    return Fact("absent", value, reason)


def _na(reason: str) -> Fact:
    return Fact("not_evaluable", None, reason)


@dataclass(frozen=True)
class KeyResult:
    condition: str
    key: str
    state: State
    direction: str
    evidence_class: str
    value: Any
    reason: str


@dataclass
class _Wx:
    rain_mm: float | None = None
    normal_mm: float | None = None
    rain_7d_mm: float | None = None
    big_day_mm: float | None = None
    days_over_35c: int | None = None
    mean_tmax_c: float | None = None
    windy_days: int | None = None

    @property
    def ratio(self) -> float | None:
        return self.rain_mm / self.normal_mm if self.rain_mm is not None and self.normal_mm else None


def _weather(raw: Any) -> _Wx | None:
    """Numbers from a WeatherSummary or from the get_weather tool's dict (inches, Fahrenheit)."""
    if raw is None:
        return None
    if isinstance(raw, dict) and "daily" in raw:
        try:
            raw = C.WeatherSummary.model_validate(raw)
        except Exception:  # noqa: BLE001 - unusable shape reads as no weather
            return None
    if isinstance(raw, C.WeatherSummary):
        days = sorted(raw.daily, key=lambda d: d.date)
        tmax = [d.tmax_c for d in days if d.tmax_c is not None]
        wind = [d.wind_max_kph for d in days if d.wind_max_kph is not None]
        return _Wx(rain_mm=raw.total_precip_mm, normal_mm=raw.normal_precip_mm,
                   rain_7d_mm=sum(d.precip_mm for d in days[-7:]) if len(days) >= 7 else None,
                   big_day_mm=max((d.precip_mm for d in days), default=None), days_over_35c=raw.days_over_35c,
                   mean_tmax_c=sum(tmax) / len(tmax) if tmax else None,
                   windy_days=sum(1 for w in wind if w >= WINDY_KPH) if wind else None)
    if isinstance(raw, dict):
        def mm(v):
            return None if v is None else float(v) * 25.4

        normal = raw.get("normal_rain_in") or raw.get("normal_rain_in_30d")
        days95 = raw.get("days_over_95f")
        return _Wx(rain_mm=mm(raw.get("rain_in")), normal_mm=mm(normal), big_day_mm=mm(raw.get("biggest_rain_day_in")),
                   days_over_35c=int(days95) if days95 is not None else None)
    return None


class _Ctx:
    """Values derived once per evaluation."""

    def __init__(self, i: FieldInputs):
        self.i = i
        self.region = i.region()
        self.f = self.region.features if self.region else None
        self.wx = _weather(i.weather)
        self.span = i.stage_span()

    def no_region(self) -> str:
        a = self.i.analysis
        if a is None:
            return "there is no satellite check on file for this field"
        if a.status != "ok":
            return f"the latest satellite pass was not usable ({a.status.replace('_', ' ')})"
        return "no problem area was flagged in the latest satellite pass"


# ---------- resolvers (one per key; the catalog says which condition, direction, weight and class) ----------


def _feature(name: str, lo: float | None = None, hi: float | None = None, text: str = "") -> Callable[[_Ctx], Fact]:
    """present when the problem area's pattern feature is >= lo (and < hi, when given); absent otherwise."""

    def fn(c: _Ctx) -> Fact:
        if c.f is None:
            return _na(c.no_region())
        v = getattr(c.f, name)
        if v is None:
            return _na(f"{name.replace('_', ' ')} was not computed (no open hail watch)")
        ok = (lo is None or v >= lo) and (hi is None or v < hi)
        return Fact("present" if ok else "absent", {name: v}, text if ok else "")

    return fn


def _feature_below(name: str, below: float, text: str) -> Callable[[_Ctx], Fact]:
    return _feature(name, None, below, text)


def _uniform(c: _Ctx) -> Fact:
    if c.region is None:
        return _na(c.no_region())
    if not c.i.acres:
        return _na("the field size is not known")
    share = round(c.region.area_acres / c.i.acres, 2)
    return Fact("present" if share >= UNIFORM_SHARE else "absent", {"share_of_field": share},
                "The weak area covers most of the field." if share >= UNIFORM_SHARE else "")


def _localized(c: _Ctx) -> Fact:
    if c.f is None:
        return _na(c.no_region())
    top = max(c.f.ring_score, c.f.wedge_score, c.f.edge_band_score, c.f.stripe_score)
    return Fact("present" if top >= RING_STRONG else "absent", {"strongest_shape_score": top},
                "The weak area has a sharp shape (ring, slice, edge or bands)." if top >= RING_STRONG else "")


def _patchy(c: _Ctx) -> Fact:
    if c.f is None:
        return _na(c.no_region())
    shapes = (c.f.ring_score, c.f.wedge_score, c.f.edge_band_score, c.f.stripe_score)
    ok = max(shapes) < PATCHY_MAX_SCORE and c.f.lowspot_overlap < LOWSPOT_HIGH and c.f.sandy_soil_overlap < SANDY_HIGH
    return Fact("present" if ok else "absent", {"strongest_shape_score": max(shapes)},
                "The weak area is irregular patches that no field feature explains." if ok else "")


def _rain(band: str) -> Callable[[_Ctx], Fact]:
    def fn(c: _Ctx) -> Fact:
        r = c.wx.ratio if c.wx else None
        if r is None:
            return _na("30-day rain and the normal were not available")
        lo, hi = RAIN_NORMAL_RANGE
        ok = r < RAIN_DEFICIT_BELOW if band == "deficit" else lo <= r <= hi if band == "normal" else r >= RAIN_EXCESS_AT
        text = {"deficit": "Rain was well below normal.", "normal": "Rain was about normal.",
                "excess": "Rain was well above normal."}[band]
        return Fact("present" if ok else "absent", {"rain_mm_30d": round(c.wx.rain_mm), "normal_mm_30d": round(c.wx.normal_mm)},
                    text if ok else "")

    return fn


def _heavy_rain(c: _Ctx) -> Fact:
    wx = c.wx
    if wx is None:
        return _na("weather was not available")
    if wx.rain_7d_mm is not None:
        ok, v = wx.rain_7d_mm >= HEAVY_RAIN_7D_MM, {"rain_mm_7d": round(wx.rain_7d_mm)}
    elif wx.big_day_mm is not None and wx.big_day_mm >= HEAVY_RAIN_7D_MM:
        ok, v = True, {"biggest_day_mm": round(wx.big_day_mm)}
    else:
        return _na("daily rain for the last week was not available")
    return Fact("present" if ok else "absent", v, "Heavy rain fell in the last week." if ok else "")


def _heat(c: _Ctx) -> Fact:
    if c.wx is None or c.wx.days_over_35c is None:
        return _na("hot-day counts were not available")
    ok = c.wx.days_over_35c >= HEAT_DAYS
    return Fact("present" if ok else "absent", {"days_over_35c": c.wx.days_over_35c},
                "Several days topped 95 F." if ok else "")


def _wind(c: _Ctx) -> Fact:
    if c.wx is None or c.wx.windy_days is None:
        return _na("daily wind was not available")
    ok = c.wx.windy_days >= WINDY_DAYS
    return Fact("present" if ok else "absent", {"windy_days": c.wx.windy_days}, "There were several breezy days." if ok else "")


def _humid(c: _Ctx) -> Fact:
    wx = c.wx
    if wx is None or wx.ratio is None or wx.mean_tmax_c is None:
        return _na("rain and temperature were not both available")
    ok = wx.ratio >= HUMID_RAIN_RATIO and HUMID_TMAX_C[0] <= wx.mean_tmax_c <= HUMID_TMAX_C[1]
    return Fact("present" if ok else "absent", {"rain_ratio": round(wx.ratio, 2), "mean_high_c": round(wx.mean_tmax_c, 1)},
                "The month was wet and mild, which favors leaf disease." if ok else "")


def _dry(c: _Ctx) -> Fact:
    f = _rain("deficit")(c)
    return Fact(f.state, f.value, "The month was dry, which slows leaf disease." if f.state == "present" else "")


def _hail_numbers(i: FieldInputs):
    """(checked, max hail mm, unsized watch open, ground reports, peak date)."""
    if i.hail is None and i.hail_watches is None:
        return False, 0.0, False, 0, None
    mm, unsized, peak = 0.0, False, None
    if i.hail is not None:
        mm, peak = float(i.hail.max_hail_mm), i.hail.peak_date
    for w in i.hail_watches or []:
        if w.get("type", "hail") != "hail":
            continue
        d = w.get("detail") or {}
        size = d.get("max_hail_mm", d.get("max_mm"))
        if size is None:
            unsized = True
        else:
            mm = max(mm, float(size))
        peak = peak or d.get("peak_date")
    ground = len(i.hail.ground_reports) if i.hail is not None else 0
    return True, mm, unsized, ground, peak


def _hail_size(kind: str) -> Callable[[_Ctx], Fact]:
    def fn(c: _Ctx) -> Fact:
        checked, mm, unsized, _, _ = _hail_numbers(c.i)
        if not checked:
            return _na("hail was not checked")
        watches = bool([w for w in c.i.hail_watches or [] if w.get("type", "hail") == "hail"])
        if kind == "large":
            ok, text = mm >= HAIL_LARGE_MM, "Radar showed large hail over this field."
        elif kind == "moderate":
            ok = HAIL_MODERATE_MM <= mm < HAIL_LARGE_MM or (unsized and mm < HAIL_MODERATE_MM)
            text = "Radar showed hail over this field."
        else:
            ok, text = mm == 0 and not watches, "No hail was seen over this field."
        return Fact("present" if ok else "absent", {"max_hail_mm": mm, "open_hail_watches": int(watches)}, text if ok else "")

    return fn


def _hail_ground(c: _Ctx) -> Fact:
    checked, _, _, ground, _ = _hail_numbers(c.i)
    if c.i.hail is None:
        return _na("hail ground reports were not checked")
    return Fact("present" if ground else "absent", {"ground_reports": ground}, "Hail was reported near the field." if ground else "")


def _drop_after_storm(c: _Ctx) -> Fact:
    a, (_, _, _, _, peak) = c.i.analysis, _hail_numbers(c.i)
    if a is None or a.status != "ok" or a.anomaly is None or a.anomaly.delta_since_last is None:
        return _na("no change since the last clear pass was available")
    prev = a.comparison.previous_date if a.comparison else None
    if not peak or not prev:
        return _na("the storm date or the previous pass date was not available")
    ok = a.anomaly.delta_since_last <= SUDDEN_DROP and prev <= peak <= a.date
    return Fact("present" if ok else "absent", {"change_since_last": a.anomaly.delta_since_last, "storm_date": peak},
                "Crop health dropped sharply right after the storm." if ok else "")


def _drought(kind: str) -> Callable[[_Ctx], Fact]:
    def fn(c: _Ctx) -> Fact:
        d = c.i.drought
        if d is None:
            return _na("the Drought Monitor was not checked")
        ok = d.category in ("D1", "D2", "D3", "D4") if kind == "d1" else d.category == "none"
        return Fact("present" if ok else "absent", {"category": d.category},
                    ("The county is in drought." if kind == "d1" else "The county shows no drought.") if ok else "")

    return fn


def _units(c: _Ctx):
    soil = c.i.context.soil if c.i.context else None
    units = soil.map_units if soil else []
    total = sum(u.pct_area for u in units)
    return units, total


def _soil_share(pred: Callable[[C.SoilMapUnit], bool], text: str) -> Callable[[_Ctx], Fact]:
    def fn(c: _Ctx) -> Fact:
        units, total = _units(c)
        if not units or total <= 0:
            return _na("no soil survey is on file")
        share = sum(u.pct_area for u in units if pred(u)) / total
        ok = share >= SOIL_SHARE
        return Fact("present" if ok else "absent", {"share_of_field": round(share, 2)}, text if ok else "")

    return fn


def _low_awc(c: _Ctx) -> Fact:
    units, total = _units(c)
    if not units or total <= 0:
        return _na("no soil survey is on file")
    awc = sum(u.awc_0_100_cm * u.pct_area for u in units) / total
    ok = awc < LOW_AWC_CM
    return Fact("present" if ok else "absent", {"awc_cm": round(awc, 1)}, "The soil holds little water." if ok else "")


def _water(c: _Ctx) -> C.WaterContext | None:
    return c.i.context.water if c.i.context else None


def _floodplain(c: _Ctx) -> Fact:
    w = _water(c)
    if w is None or w.floodplain_pct is None:
        return _na("flood-zone mapping was not available")
    ok = w.floodplain_pct >= 50
    return Fact("present" if ok else "absent", {"floodplain_pct": w.floodplain_pct},
                "Much of the field is in a mapped flood zone." if ok else "")


def _outside_floodplain(c: _Ctx) -> Fact:
    w = _water(c)
    if w is None or w.floodplain_pct is None:
        return _na("flood-zone mapping was not available")
    ok = w.floodplain_pct == 0 and bool(w.flood_zones)
    return Fact("present" if ok else "absent", {"floodplain_pct": w.floodplain_pct}, "The field is outside mapped flood zones." if ok else "")


def _stream_wetland(c: _Ctx) -> Fact:
    w = _water(c)
    if w is None or (w.nearest_stream_m is None and w.wetland_pct is None):
        return _na("stream and wetland mapping was not available")
    ok = (w.nearest_stream_m is not None and w.nearest_stream_m <= STREAM_NEAR_M) or bool(w.wetland_pct)
    return Fact("present" if ok else "absent", {"nearest_stream_m": w.nearest_stream_m, "wetland_pct": w.wetland_pct},
                "A stream or wetland borders the field." if ok else "")


def _norm_crop(v: str | None) -> str | None:
    s = (v or "").strip().lower()
    return "corn" if s.startswith("corn") else "soybeans" if s.startswith("soy") else s or None


def _prev_crop(c: _Ctx) -> tuple[str | None, str | None]:
    d, hist = c.i.date(), (c.i.context.crop_history if c.i.context else {})
    if not d or not hist:
        return None, "last year's crop is not on file"
    prev = _norm_crop(hist.get(str(int(d[:4]) - 1)))
    return (prev, None) if prev else (None, "last year's crop is not on file")


def _rotation(kind: str) -> Callable[[_Ctx], Fact]:
    def fn(c: _Ctx) -> Fact:
        prev, why = _prev_crop(c)
        if prev is None:
            return _na(why)
        ok = prev == "corn" and _norm_crop(c.i.crop) == "corn" if kind == "corn" else prev == "soybeans"
        text = "Corn followed corn." if kind == "corn" else "Soybeans were grown here last year."
        return Fact("present" if ok else "absent", {"last_year": prev}, text if ok else "")

    return fn


def _county(kind: str) -> Callable[[_Ctx], Fact]:
    def fn(c: _Ctx) -> Fact:
        reps = c.i.disease_reports
        if reps is None:
            return _na("county disease reports were not checked")
        confirmed = [r for r in reps if r.status == "confirmed_this_year"]
        history = [r for r in reps if r.status == "history"]
        if kind == "confirmed":
            ok, text = bool(confirmed), "Disease was confirmed in the county this season."
        elif kind == "history":
            ok, text = bool(history), "The county has had this disease in past seasons."
        else:
            ok, text = not confirmed, "No disease has been confirmed in the county this season."
        return Fact("present" if ok else "absent", {"confirmed": len(confirmed), "history": len(history)}, text if ok else "")

    return fn


def _si(kind: str) -> Callable[[_Ctx], Fact]:
    """Sufficiency index: red-edge of the problem area / the field's own top-10% (P90) reading. Corn V6-V12 only."""

    def fn(c: _Ctx) -> Fact:
        ok_stage, why = scoring.window_check("nitrogen", c.i.crop, c.span)
        if not ok_stage:
            return _na(f"the red-edge check for nitrogen is only read in the V6-V12 window ({why})")
        if c.region is None:
            return _na(c.no_region())
        ind = c.i.analysis.indices if c.i.analysis else None
        if c.i.region_ndre is None or ind is None or ind.ndre.p90 <= 0:
            return _na("the problem area's red-edge reading and the field's best-tenth reading were not both available")
        si = c.i.region_ndre / ind.ndre.p90
        ok = si < SI_STRONG if kind == "low" else SI_STRONG <= si < SI_BORDERLINE
        text = ("The weak area reads well below the field's best crop on the red-edge band."
                if kind == "low" else "The weak area reads a little below the field's best crop on the red-edge band.")
        return Fact("present" if ok else "absent", {"si": round(si, 3)}, text if ok else "")

    return fn


def _width(c: _Ctx) -> Fact:
    if c.i.stripe_width_matches is None:
        return _na("the stripe width and the implement width were not available")
    ok = bool(c.i.stripe_width_matches)
    return Fact("present" if ok else "absent", {}, "The band spacing matches the planter or sprayer." if ok else "")


def _photo(key: str) -> Callable[[_Ctx], Fact]:
    def fn(c: _Ctx) -> Fact:
        if not c.i.photo.taken:
            return _na("no photo has been taken")
        ok = key in c.i.photo.keys
        return Fact("present" if ok else "absent", {"photo_finding": key}, "The photo shows this." if ok else "")

    return fn


def _farmer(attr: str, want: Any, text: str) -> Callable[[_Ctx], Fact]:
    def fn(c: _Ctx) -> Fact:
        v = getattr(c.i.farmer, attr)
        if v is None:
            return _na("the farmer has not said")
        return Fact("present" if v == want else "absent", {attr: v}, text if v == want else "")

    return fn


RESOLVERS: dict[str, Callable[[_Ctx], Fact]] = {
    "ring_pattern_strong": _feature("ring_score", RING_STRONG, None, "The weak area is a ring on the outer edge, the path of the pivot's last span."),
    "ring_pattern_weak": _feature("ring_score", RING_WEAK, RING_STRONG, "The weak area is partly a ring on the outer edge."),
    "wedge_pattern_strong": _feature("wedge_score", WEDGE_STRONG, None, "The weak area is one slice of the circle, the way a failed sprinkler section looks."),
    "edge_pattern_strong": _feature("edge_band_score", EDGE_STRONG, None, "The weak area runs along one edge of the field."),
    "edge_pattern_none": _feature_below("edge_band_score", EDGE_NONE, "The weak area is not along a field edge."),
    "stripe_pattern_strong": _feature("stripe_score", STRIPE_STRONG, None, "The weak area is straight parallel bands."),
    "stripe_pattern_none": _feature_below("stripe_score", STRIPE_NONE, "There are no straight planter bands."),
    "stripe_width_matches_implement": _width,
    "lowspot_overlap_high": _feature("lowspot_overlap", LOWSPOT_HIGH, None, "The weak area sits in the low spots of the field."),
    "lowspot_overlap_none": _feature_below("lowspot_overlap", LOWSPOT_NONE, "The weak area is not in the low spots."),
    "sandy_soil_overlap_high": _feature("sandy_soil_overlap", SANDY_HIGH, None, "The weak area sits on sandy soil."),
    "soil_not_sandy": _feature_below("sandy_soil_overlap", SANDY_NONE, "The weak area is not on sandy soil."),
    "pattern_follows_soil_zones": _feature("sandy_soil_overlap", SANDY_ZONES, None, "The weak area follows the sandy soil zones."),
    "hail_swath_overlap_high": _feature("hail_swath_overlap", HAIL_SWATH_HIGH, None, "The weak area lines up with where hail fell."),
    "hail_swath_no_overlap": _feature_below("hail_swath_overlap", HAIL_SWATH_NONE, "The weak area is outside where hail fell."),
    "stress_uniform_field": _uniform,
    "pattern_localized": _localized,
    "patchy_irregular_pattern": _patchy,
    "rain_deficit_30d": _rain("deficit"),
    "rain_normal_30d": _rain("normal"),
    "rain_excess_30d": _rain("excess"),
    "heavy_rain_7d": _heavy_rain,
    "heat_days_over_35c": _heat,
    "wind_during_spray_window": _wind,
    "humid_wet_weather_30d": _humid,
    "dry_weather_30d": _dry,
    "hail_mesh_large": _hail_size("large"),
    "hail_mesh_moderate": _hail_size("moderate"),
    "no_hail_mesh": _hail_size("none"),
    "hail_ground_report_near": _hail_ground,
    "sudden_drop_after_storm": _drop_after_storm,
    "drought_monitor_d1_plus": _drought("d1"),
    "drought_monitor_none": _drought("none"),
    "soil_poorly_drained": _soil_share(lambda u: "poorly" in u.drainage_class.lower(), "The soil drains poorly."),
    "soil_well_drained": _soil_share(lambda u: u.drainage_class.strip().lower() == "well drained", "The soil drains well."),
    "soil_low_awc": _low_awc,
    "floodplain_overlap_high": _floodplain,
    "outside_floodplain": _outside_floodplain,
    "stream_or_wetland_adjacent": _stream_wetland,
    "corn_after_corn": _rotation("corn"),
    "prior_crop_soybeans": _rotation("soy"),
    "disease_confirmed_county": _county("confirmed"),
    "disease_history_county": _county("history"),
    "no_disease_reports_county": _county("none"),
    "n_red_edge_si_low": _si("low"),
    "n_red_edge_si_borderline": _si("borderline"),
    "farmer_reports_pivot_down": _farmer("pivot", "down", "The farmer says the pivot was down."),
    "farmer_reports_pivot_ok": _farmer("pivot", "ok", "The farmer says the pivot ran normally."),
    "farmer_reports_n_short": _farmer("n_applied", "short", "The farmer says less nitrogen went on than planned."),
    "farmer_reports_n_full": _farmer("n_applied", "full", "The farmer says the full nitrogen rate went on."),
    "farmer_reports_neighbor_spray": _farmer("neighbor_spray", True, "The farmer says a neighbor sprayed nearby."),
    "farmer_reports_planter_issue": _farmer("planter_issue", True, "The farmer remembers a planter or sprayer problem."),
}
for _cond in ("photo_leaf_shredding", "photo_standing_water", "photo_v_shaped_firing", "photo_leaf_cupping", "photo_gaps_in_row",
              "photo_tar_spot_lesions", "photo_gray_leaf_spot_lesions", "photo_sds_symptoms", "photo_insect_feeding",
              "photo_no_lesions"):
    RESOLVERS[_cond] = _photo(_cond)


# ---------- the vector ----------


@dataclass
class EvidenceVector:
    results: list[KeyResult]
    as_of: str | None = None

    def present(self) -> list[KeyResult]:
        return [r for r in self.results if r.state == "present"]

    def by_state(self) -> dict[str, int]:
        out = {"present": 0, "absent": 0, "not_evaluable": 0}
        for r in self.results:
            out[r.state] += 1
        return out

    def get(self, condition: str, key: str) -> KeyResult | None:
        return next((r for r in self.results if r.condition == condition and r.key == key), None)

    def items(self) -> list[C.EvidenceItem]:
        """The present keys as scorer input. Ids and times are derived from the key and the data date, so the same
        inputs give the same items."""
        stamp = f"{self.as_of or '1970-01-01'}T00:00:00Z"
        return [C.EvidenceItem(evidence_id=f"code-{r.condition}-{r.key}", hypothesis=r.condition,  # type: ignore[arg-type]
                               direction=r.direction, weight_key=r.key, source="evidence_engine",  # type: ignore[arg-type]
                               value=r.value if r.value is not None else {}, reason=r.reason or r.key.replace("_", " "),
                               created_at=stamp)
                for r in self.present()]

    def digest(self) -> str:
        """A stable fingerprint of the whole evaluation (the stability tests compare these)."""
        blob = json.dumps([[r.condition, r.key, r.state, r.direction, r.evidence_class, r.value, r.reason]
                           for r in self.results], sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:16]

    def not_evaluable(self) -> dict[str, str]:
        """key -> why, for the keys the data could not speak to (first reason per key)."""
        out: dict[str, str] = {}
        for r in self.results:
            if r.state == "not_evaluable":
                out.setdefault(r.key, r.reason)
        return out


def evaluate(inputs: FieldInputs) -> EvidenceVector:
    """Evaluate every key of every condition in the catalog for this field. Deterministic."""
    cat = scoring.load_catalog()
    ctx = _Ctx(inputs)
    facts: dict[str, Fact] = {}
    results: list[KeyResult] = []
    for cid in sorted(cat.conditions):
        for key, w in cat.conditions[cid].weights.items():
            if key not in facts:
                facts[key] = RESOLVERS[key](ctx)
            f = facts[key]
            results.append(KeyResult(cid, key, f.state, w.direction, w.evidence_class, f.value, f.reason))
    return EvidenceVector(results, inputs.date())


def score_request(inputs: FieldInputs, vector: EvidenceVector, extra: list[C.EvidenceItem] | None = None,
                  investigation_id: str = "inv-local") -> C.ScoreRequest:
    crop = inputs.crop if inputs.crop in C.Crop.__args__ else "other"
    ids = {(e.hypothesis, e.weight_key) for e in vector.items()}
    more = [e for e in extra or [] if (e.hypothesis, e.weight_key) not in ids]
    return C.ScoreRequest(investigation_id=investigation_id, crop=crop, stage=inputs.stage_label(),  # type: ignore[arg-type]
                          irrigated=inputs.irrigated, evidence=[*vector.items(), *more])


def determine(inputs: FieldInputs, extra: list[C.EvidenceItem] | None = None) -> tuple[EvidenceVector, scoring.Determination]:
    """Evidence vector and determination for a field (the one call the agents and tests use)."""
    vec = evaluate(inputs)
    return vec, scoring.determine(score_request(inputs, vec, extra), crop_state=inputs.state())


def situation(inputs: FieldInputs) -> str:
    """Why there may be nothing to diagnose: ok | season_over | no_analysis | no_clear_pass | no_problem_area."""
    cs = inputs.state()
    if cs is not None and not cs.diagnosable and not scoring.hail_allowed(cs):
        return "season_over"
    a = inputs.analysis
    if a is None:
        return "no_analysis"
    if a.status != "ok":
        return "no_clear_pass"
    return "ok" if inputs.region() is not None else "no_problem_area"


# ---------- the season gate (ask path) ----------

_STATE_PHRASE = {
    "harvested": "looks harvested",
    "harvest_in_progress": "looks like harvest is under way",
    "senescing": "is maturing for the season",
    "pre_emergence": "has not come up yet",
}


def _nice_date(iso: str | None) -> str:
    try:
        d = _date.fromisoformat(str(iso))
    except ValueError:
        return ""
    return f"{d:%b} {d.day}"


def season_message(state: C.CropState | None, field_name: str, as_of: str | None) -> str | None:
    """Plain words for a field FieldWatch has paused checks on, or None when checks may run (no state, diagnosable, or
    hail-only, which still allows hail). Used by the on-demand gate and the chat context."""
    if state is None or state.diagnosable or scoring.hail_allowed(state):
        return None
    name = " ".join(str(field_name).split()) or "The field"
    subject = f"The {name}" if "field" in name.lower() else name[:1].upper() + name[1:]
    phrase = _STATE_PHRASE.get(state.state, "is not in a state FieldWatch can check right now")
    when = f" as of {_nice_date(as_of)}" if _nice_date(as_of) else ""
    return f"{subject} {phrase}{when}, so FieldWatch has paused problem checks for this season."
