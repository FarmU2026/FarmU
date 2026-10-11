"""Hypothesis scorer and impact estimator (workstream 5). Deterministic: the LLM never sets a number.

- `catalog/conditions.json`: prior per condition (adjusted by crop, growth stage group and irrigation) and a
  weight (log-likelihood ratio, positive magnitude) per `weight_key`. An `EvidenceItem` with direction `supports`
  adds the weight to the condition's log-odds, `refutes` subtracts it.
- `catalog/loss_tables.json`: yield-loss fraction ranges by condition x crop x stage group x severity, plus crop
  yield/price defaults. Every number is a DRAFT pending Caleb's agronomy review.

Pure code: no network, no AWS. Both catalogs are validated with Pydantic on first load and cached.
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import contracts as C
from .config import ROOT

CATALOG_DIR = ROOT / "catalog"
LIKELY_ABOVE = 0.6
RULED_OUT_BELOW = 0.05
LOGIT_CLAMP = 8.0
STAGE_GROUPS = ("early", "vegetative", "flowering", "grain_fill", "maturity")
_SEVERITIES = ("low", "medium", "high")


# ---------- catalog models (validated at load) ----------


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Weight(_M):
    weight: float = Field(gt=0, description="Log-likelihood ratio magnitude; direction on the evidence sets the sign")
    reason: str = Field(min_length=3)
    source: str


class CatalogCondition(_M):
    label: str
    prior: float = Field(gt=0, lt=1)
    prior_reason: str
    crop_adjust: dict[str, float] = Field(default_factory=dict)
    irrigated_adjust: dict[Literal["true", "false"], float] = Field(default_factory=dict)
    stage_adjust: dict[str, float] = Field(default_factory=dict)
    requires_irrigated: bool = False
    weights: dict[str, Weight]


class Catalog(_M):
    version: str
    status: str
    sources: dict[str, str]
    stage_groups: dict[str, str]
    conditions: dict[str, CatalogCondition]

    @model_validator(mode="after")
    def _check(self) -> "Catalog":
        import typing
        want = set(typing.get_args(C.ConditionId))
        if set(self.conditions) != want:
            raise ValueError(f"conditions must be exactly {sorted(want)}; diff {sorted(set(self.conditions) ^ want)}")
        if "DRAFT" not in self.status:
            raise ValueError("status must say DRAFT until agronomy review")
        for cid, c in self.conditions.items():
            for key, w in c.weights.items():
                if w.source not in self.sources:
                    raise ValueError(f"{cid}.{key}: unknown source {w.source}")
            bad = set(c.stage_adjust) - set(STAGE_GROUPS)
            if bad:
                raise ValueError(f"{cid}: unknown stage groups {bad}")
        return self


class LossRange(_M):
    low: tuple[float, float]
    medium: tuple[float, float]
    high: tuple[float, float]

    @model_validator(mode="after")
    def _check(self) -> "LossRange":
        for sev in _SEVERITIES:
            lo, hi = getattr(self, sev)
            if not 0 <= lo <= hi <= 1:
                raise ValueError(f"{sev} range {lo}-{hi} must satisfy 0 <= low <= high <= 1")
        return self


class LossRow(_M):
    condition: str
    crop: str
    stage_group: str
    loss: LossRange
    source: str
    note: str = ""


class CropDefault(_M):
    yield_per_acre: float = Field(gt=0)
    price_usd: float = Field(gt=0)
    unit: str
    source: str
    note: str = ""


class DaysScaling(_M):
    min: float
    max: float
    conditions: list[str]


class LossTables(_M):
    version: str
    status: str
    sources: dict[str, str]
    loss_fraction_meaning: str
    reference_days: int = Field(gt=0)
    days_scaling: DaysScaling
    crop_defaults: dict[str, CropDefault]
    rows: list[LossRow]

    @model_validator(mode="after")
    def _check(self) -> "LossTables":
        for r in self.rows:
            if r.source not in self.sources:
                raise ValueError(f"row {r.condition}/{r.crop}/{r.stage_group}: unknown source {r.source}")
        return self


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    return Catalog.model_validate(json.loads((CATALOG_DIR / "conditions.json").read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def load_loss_tables() -> LossTables:
    return LossTables.model_validate(json.loads((CATALOG_DIR / "loss_tables.json").read_text(encoding="utf-8")))


# ---------- stage ----------

_STAGE_RE = re.compile(r"^\s*(ve|vt|v(\d+)|r(\d+))\b", re.I)
_STAGE_WORDS = (
    ("emerg", "early"), ("seedling", "early"), ("tassel", "flowering"), ("silk", "flowering"), ("flower", "flowering"),
    ("bloom", "flowering"), ("blister", "flowering"), ("milk", "grain_fill"), ("dough", "grain_fill"),
    ("dent", "grain_fill"), ("pod", "grain_fill"), ("fill", "grain_fill"), ("matur", "maturity"),
    ("black layer", "maturity"), ("vegetative", "vegetative"),
)


def stage_group(stage: str | None) -> str | None:
    """Map a growth stage in words ('R2 (blister)', 'V8', 'VT') to one of STAGE_GROUPS, or None if unknown."""
    if not stage:
        return None
    m = _STAGE_RE.match(stage)
    if m:
        tok = m.group(1).lower()
        if tok == "ve":
            return "early"
        if tok == "vt":
            return "flowering"
        if m.group(2):
            return "early" if int(m.group(2)) <= 5 else "vegetative"
        n = int(m.group(3))
        return "flowering" if n <= 2 else "grain_fill" if n <= 5 else "maturity"
    s = stage.lower()
    for word, grp in _STAGE_WORDS:
        if word in s:
            return grp
    return None


# ---------- scoring ----------


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def status_for(probability: float) -> Literal["open", "likely", "ruled_out"]:
    if probability < RULED_OUT_BELOW:
        return "ruled_out"
    if probability > LIKELY_ABOVE:
        return "likely"
    return "open"


def _prior_logit(cid: str, crop: str, stage: str | None, irrigated: bool | None) -> float:
    c = load_catalog().conditions[cid]
    x = _logit(c.prior) + c.crop_adjust.get(crop, 0.0)
    if irrigated is not None:
        x += c.irrigated_adjust.get("true" if irrigated else "false", 0.0)
    grp = stage_group(stage)
    if grp:
        x += c.stage_adjust.get(grp, 0.0)
    return x


def prior_probability(condition: str, crop: str, stage: str | None = None, irrigated: bool | None = None) -> float:
    return _sigmoid(_prior_logit(condition, crop, stage, irrigated))


def _score(req: C.ScoreRequest) -> dict[str, dict[str, Any]]:
    cat = load_catalog()
    evidence = sorted(req.evidence, key=lambda e: e.evidence_id)  # order-independent
    detail: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    for cid in sorted(cat.conditions):
        detail[cid] = {"prior": prior_probability(cid, req.crop, req.stage, req.irrigated),
                       "logit": _prior_logit(cid, req.crop, req.stage, req.irrigated), "applied": [], "ignored": []}
    for e in evidence:
        d = detail[e.hypothesis]
        w = cat.conditions[e.hypothesis].weights.get(e.weight_key)
        base = {"evidence_id": e.evidence_id, "weight_key": e.weight_key, "direction": e.direction}
        if w is None:
            d["ignored"].append({**base, "why": "weight_key not in catalog for this condition"})
            continue
        key = (e.hypothesis, e.weight_key, e.direction)
        if key in seen:
            d["ignored"].append({**base, "why": "same weight_key and direction already counted"})
            continue
        seen.add(key)
        delta = w.weight if e.direction == "supports" else -w.weight
        d["logit"] += delta
        d["applied"].append({**base, "delta_log_odds": round(delta, 3), "reason": e.reason or w.reason})
    for cid, d in detail.items():
        d["logit"] = max(-LOGIT_CLAMP, min(LOGIT_CLAMP, d["logit"]))
        d["probability"] = round(_sigmoid(d["logit"]), 4)
        d["status"] = status_for(d["probability"])
        d["applied"].sort(key=lambda a: (-abs(a["delta_log_odds"]), a["evidence_id"]))
    return detail


def _listed(cid: str, d: dict[str, Any], req: C.ScoreRequest) -> bool:
    """Show a condition if evidence touched it, or it is a live possibility for this field."""
    if d["applied"] or d["ignored"]:
        return True
    if cid == "unknown":
        return False
    if load_catalog().conditions[cid].requires_irrigated and req.irrigated is False:
        return False
    return True


def _as_request(req: C.ScoreRequest | None, kwargs: dict) -> C.ScoreRequest:
    return req if req is not None else C.ScoreRequest.model_validate(kwargs)


def score_hypotheses(req: C.ScoreRequest | None = None, **kwargs: Any) -> C.ScoreResult:
    """Log-odds scoring: prior (crop, stage, irrigation) + signed weight of each distinct evidence item, then sigmoid.

    Evidence whose weight_key is not in the catalog for its condition is ignored; a repeated (condition,
    weight_key, direction) counts once. Sorted by probability, then condition name. Accepts a ScoreRequest or its
    fields as keyword arguments (the tool-registry calling convention).
    """
    req = _as_request(req, kwargs)
    detail = _score(req)
    hyps = [C.Hypothesis(condition=cid, probability=d["probability"], status=d["status"])  # type: ignore[arg-type]
            for cid, d in detail.items() if _listed(cid, d, req)]
    hyps.sort(key=lambda h: (-h.probability, h.condition))
    return C.ScoreResult(hypotheses=hyps, catalog_version=load_catalog().version)


def explain(req: C.ScoreRequest | None = None, **kwargs: Any) -> dict[str, dict[str, Any]]:
    """Per condition: prior, final probability and status, evidence that moved it (largest first) and evidence ignored."""
    req = _as_request(req, kwargs)
    detail = _score(req)
    return {cid: {"prior": round(d["prior"], 4), "probability": d["probability"], "status": d["status"],
                  "applied": d["applied"], "ignored": d["ignored"]}
            for cid, d in detail.items() if _listed(cid, d, req)}


# ---------- impact ----------


def _find_row(condition: str, crop: str, grp: str | None) -> LossRow:
    rows = load_loss_tables().rows
    for want_crop, want_grp in ((crop, grp), ("any", "any")):
        for r in rows:
            if r.condition == condition and r.crop == want_crop and r.stage_group == want_grp:
                return r
    raise LookupError(f"no loss row for {condition}")  # the catalog test guarantees an any/any row


def estimate_impact(req: C.ImpactRequest | None = None, *, crop: str | None = None,
                    expected_yield: float | None = None, price: float | None = None,
                    **fields: Any) -> C.ImpactEstimate:
    """Dollar range = acres x yield x price x loss fraction (low..high) from loss_tables.json.

    ImpactRequest carries no crop, yield or price: pass `crop` (default corn), and `expected_yield` / `price`
    from FieldContext when known; otherwise the table's crop defaults are used and the basis says so.
    Duration-sensitive conditions (water stress, ponding) scale with days_until_fixed against the reference week.
    """
    req = req if req is not None else C.ImpactRequest.model_validate(fields)
    lt = load_loss_tables()
    crop_key = (crop or "corn").lower()
    cd = lt.crop_defaults.get(crop_key) or lt.crop_defaults["other"]
    y = expected_yield if expected_yield is not None else cd.yield_per_acre
    p = price if price is not None else cd.price_usd
    grp = stage_group(req.stage)
    row = _find_row(req.condition, crop_key, grp)
    lo, hi = getattr(row.loss, req.severity)
    factor = 1.0
    if req.condition in lt.days_scaling.conditions:
        factor = max(lt.days_scaling.min, min(lt.days_scaling.max, req.days_until_fixed / lt.reference_days))
    lo, hi = min(1.0, lo * factor), min(1.0, hi * factor)
    value = max(req.affected_acres, 0.0) * y * p
    low_usd, high_usd = round(value * lo), round(value * hi)
    stage_txt = f"at {req.stage}" if req.stage else "at an unknown stage"
    days_txt = f", {req.days_until_fixed} days until fixed" if factor != 1.0 or req.condition in lt.days_scaling.conditions else ""
    row_txt = f"{row.crop}/{row.stage_group}" if row.crop != "any" else "all-crop envelope"
    basis = (f"{y:g} {cd.unit}/ac x ${p:.2f}/{cd.unit} x {req.affected_acres:g} ac, {lo:.0%}-{hi:.0%} loss "
             f"({req.condition.replace('_', ' ')}, {req.severity} severity {stage_txt}{days_txt}); "
             f"loss row {row_txt}, source: {lt.sources[row.source]}. DRAFT numbers"
             + ("" if expected_yield is not None and price is not None else "; default yield/price used where not given")
             + (f"; crop assumed {crop_key}" if crop is None else ""))
    return C.ImpactEstimate(affected_acres=req.affected_acres, low_usd=float(low_usd), high_usd=float(high_usd), basis=basis)
