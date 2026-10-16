"""Hypothesis scorer and impact estimator (workstream 5). Deterministic: the LLM never sets a number.

- `catalog/conditions.json`: prior per condition (adjusted by crop and irrigation), and per `weight_key` a weight (a
  log-likelihood ratio magnitude), the direction it moves the condition, its evidence class (`field`, `static`,
  `regional`) and an optional correlated-cluster name. Per condition it also lists the evidence a name requires, the
  stage window the condition can exist in, what rules it out and the highest tier it may show.
- `catalog/loss_tables.json`: yield-loss fraction ranges by condition x crop x stage group x severity, plus crop
  yield/price defaults. Every number is a DRAFT pending Caleb's agronomy review.

How a field gets a diagnosis (docs/research/grounding.md, section 5):
  1. every evidence item is scored as prior + signed weight (log-odds), but regional evidence in total is capped at
     +-REGIONAL_CAP, static evidence at +-STATIC_CAP and field evidence at +-FIELD_CAP per condition, and correlated
     items (a `cluster`) count once, the strongest, per direction;
  2. a condition may be NAMED only if it is inside its stage window, has no ruling-out item, has at least one required
     field-evidence item, and its probability clears NAME_THRESHOLD and leads the runner-up by NAME_MARGIN;
  3. otherwise the outcome is "not_determined", with the leading candidates listed as "possible".
Farmer-facing text uses the tiers (likely / possible / not determined), never the probabilities.

Pure code: no network, no AWS. Both catalogs are validated with Pydantic on first load and cached.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import contracts as C
from .config import ROOT

CATALOG_DIR = ROOT / "catalog"

# ---- the constants block: every [design] value of the scorer lives here (memo 5.1, 5.2) ----
REGIONAL_CAP = 0.7  # [design] total regional (weather, county reports) log-odds per condition, either sign (about 2x odds)
STATIC_CAP = 1.5  # [design] total static (soil, terrain, flood zone, history) log-odds per condition (about 4.5x odds)
FIELD_CAP = 4.0  # memo 5.2 clamp for this field's own evidence per condition
NAME_THRESHOLD = 0.65  # [design] probability a condition needs to be named
NAME_MARGIN = 0.15  # [design] lead the named condition needs over the runner-up
POSSIBLE_MIN = 0.30  # [design] probability a not-named candidate needs to be listed as "possible"
POSSIBLE_MAX = 3  # leading candidates listed
RULED_OUT_BELOW = 0.05
LOGIT_CLAMP = 8.0
CLASS_CAPS = {"regional": REGIONAL_CAP, "static": STATIC_CAP, "field": FIELD_CAP}
LLM_SOURCES = ("photo", "farmer")  # the only catalog sources an LLM may supply evidence for (fixed fields)
STAGE_GROUPS = ("early", "vegetative", "flowering", "grain_fill", "maturity")
_SEVERITIES = ("low", "medium", "high")

Tier = Literal["likely", "possible"]
Outcome = Literal["likely", "possible", "not_determined"]
EvidenceClass = Literal["field", "static", "regional"]


# ---------- catalog models (validated at load) ----------


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Weight(_M):
    weight: float = Field(gt=0, description="Log-likelihood ratio magnitude; `direction` sets the sign")
    direction: Literal["supports", "refutes"]
    evidence_class: EvidenceClass
    cluster: str | None = Field(default=None, description="Correlated items share a cluster: only the strongest counts")
    reason: str = Field(min_length=3)
    source: str
    how: str = Field(default="", description="How code decides the item is present (shown in REVIEW.md)")


class TierCap(_M):
    max_tier: Literal["possible"]
    lifted_by_any: list[str]
    why: str


class CatalogCondition(_M):
    label: str
    label_possible: str | None = None
    prior: float = Field(gt=0, lt=1)
    prior_reason: str
    crop_adjust: dict[str, float] = Field(default_factory=dict)
    irrigated_adjust: dict[Literal["true", "false"], float] = Field(default_factory=dict)
    stage_adjust: dict[str, float] = Field(default_factory=dict)
    requires_irrigated: bool = False
    stage_windows: dict[str, tuple[str, str]] = Field(
        default_factory=dict, description="crop (or 'any') -> [first stage, last stage]; outside it the condition cannot be named")
    needs_known_stage: bool = False
    required_field_evidence: list[str] = Field(default_factory=list)
    possible_if_any: list[str] = Field(default_factory=list)
    tier_cap: TierCap | None = None
    rules_out: list[str] = Field(default_factory=list, description="own refuting keys, or 'condition.key' for another condition's supporting key")
    scout_for: str = ""
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
                if w.source in LLM_SOURCES and w.evidence_class != "field":
                    raise ValueError(f"{cid}.{key}: photo and farmer evidence is field evidence")
            bad = set(c.stage_adjust) - set(STAGE_GROUPS)
            if bad:
                raise ValueError(f"{cid}: unknown stage groups {bad}")
            for crop, (lo, hi) in c.stage_windows.items():
                if _token_rank(lo) is None or _token_rank(hi) is None or _token_rank(lo) > _token_rank(hi):
                    raise ValueError(f"{cid}.stage_windows.{crop}: bad window {lo}-{hi}")
            if cid != "unknown" and not c.required_field_evidence:
                raise ValueError(f"{cid}: required_field_evidence is empty")
            for key in [*c.required_field_evidence, *c.possible_if_any]:
                w = c.weights.get(key)
                if w is None or w.direction != "supports" or w.evidence_class != "field":
                    raise ValueError(f"{cid}: {key} must be a supporting field-evidence key of the condition")
            if c.tier_cap:
                missing = [k for k in c.tier_cap.lifted_by_any if k not in c.weights]
                if missing:
                    raise ValueError(f"{cid}.tier_cap: unknown keys {missing}")
            for r in c.rules_out:
                other, _, key = r.rpartition(".")
                target = self.conditions.get(other or cid)
                w = target.weights.get(key) if target else None
                want_dir = "supports" if other else "refutes"
                if w is None or w.direction != want_dir:
                    raise ValueError(f"{cid}.rules_out: {r} must name a {want_dir} key")
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


def llm_keys() -> dict[str, set[str]]:
    """condition -> keys whose evidence an LLM may supply (photo and farmer inputs, as fixed fields)."""
    return {cid: {k for k, w in c.weights.items() if w.source in LLM_SOURCES}
            for cid, c in load_catalog().conditions.items()}


# ---------- stage ----------

_STAGE_RE = re.compile(r"^\s*(ve|vt|v(\d+)|r(\d+))\b", re.I)
_STAGE_WORDS = (
    ("emerg", "early"), ("seedling", "early"), ("tassel", "flowering"), ("silk", "flowering"), ("flower", "flowering"),
    ("bloom", "flowering"), ("blister", "flowering"), ("milk", "grain_fill"), ("dough", "grain_fill"),
    ("dent", "grain_fill"), ("pod", "grain_fill"), ("fill", "grain_fill"), ("matur", "maturity"),
    ("black layer", "maturity"), ("vegetative", "vegetative"),
)
# One number line for corn and soybean stages: VE 0, V<n> n, VT 60, R<n> 60+n. Words only give a span of it.
_VT_RANK = 60.0
_GROUP_SPAN = {"early": (0.0, 5.0), "vegetative": (6.0, 59.0), "flowering": (60.0, 62.0),
               "grain_fill": (63.0, 65.0), "maturity": (66.0, 99.0)}
_STATE_SPAN = {"vegetative": (0.0, 59.0), "reproductive": (60.0, 99.0), "senescing": (66.0, 99.0),
               "harvest_in_progress": (66.0, 99.0), "harvested": (66.0, 99.0)}


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


def _token_rank(token: str) -> float | None:
    m = _STAGE_RE.match(token or "")
    if not m:
        return None
    tok = m.group(1).lower()
    return 0.0 if tok == "ve" else _VT_RANK if tok == "vt" else float(m.group(2)) if m.group(2) else _VT_RANK + int(m.group(3))


def stage_span(stage: str | None, state: str | None = None) -> tuple[float, float] | None:
    """Where on the stage number line a stage label (or, failing that, a crop state) puts the crop: a point for an
    exact label ('V10'), a span for words ('vegetative'), None when nothing is known."""
    if stage:
        r = _token_rank(stage)
        if r is not None:
            return (r, r)
        grp = stage_group(stage)
        if grp:
            return _GROUP_SPAN[grp]
    return _STATE_SPAN.get(state or "")


def window_check(cid: str, crop: str, span: tuple[float, float] | None) -> tuple[bool, str]:
    """(inside, why not). Hard gate: outside the window the condition cannot be named. Unknown stage passes unless the
    condition needs a known stage; a stage that only partly overlaps the window passes the same way."""
    c = load_catalog().conditions[cid]
    if not c.stage_windows:
        return True, ""
    win = c.stage_windows.get(crop) or c.stage_windows.get("any")
    if win is None:
        if span is None and not c.needs_known_stage:
            return True, ""
        return False, f"it does not apply to {crop}"
    wlo, whi = _token_rank(win[0]), _token_rank(win[1])
    text = f"{win[0]} to {win[1]}"
    if span is None:
        return (not c.needs_known_stage), ("the growth stage is not known and it only applies from " + text if c.needs_known_stage else "")
    lo, hi = span
    if hi < wlo or lo > whi:
        return False, f"it only applies from {text}"
    if lo >= wlo and hi <= whi:
        return True, ""
    return (not c.needs_known_stage), ("the growth stage is not pinned down and it only applies from " + text if c.needs_known_stage else "")


# ---------- scoring ----------


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def status_for(probability: float) -> Literal["open", "ruled_out"]:
    """Probability-only status. 'likely' is not a probability band any more: it is the named, uncapped outcome."""
    return "ruled_out" if probability < RULED_OUT_BELOW else "open"


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


_CLASS_ORDER = {"field": 0, "static": 1, "regional": 2}


def _score(req: C.ScoreRequest, crop_state: C.CropState | None = None) -> dict[str, dict[str, Any]]:
    cat = load_catalog()
    stage = (crop_state.stage_label if crop_state and crop_state.stage_label else None) or req.stage
    span = stage_span(stage, crop_state.state if crop_state else None)
    detail: dict[str, dict[str, Any]] = {}
    for cid in sorted(cat.conditions):
        detail[cid] = {"prior": prior_probability(cid, req.crop, req.stage, req.irrigated),
                       "logit": _prior_logit(cid, req.crop, req.stage, req.irrigated), "applied": [], "ignored": [],
                       "accepted": [], "gate": [], "rule_outs": [], "class_totals": {}}
    seen: set[tuple[str, str]] = set()
    for e in sorted(req.evidence, key=lambda e: e.evidence_id):  # order-independent
        d = detail[e.hypothesis]
        w = cat.conditions[e.hypothesis].weights.get(e.weight_key)
        base = {"evidence_id": e.evidence_id, "weight_key": e.weight_key, "direction": e.direction}
        if w is None:
            d["ignored"].append({**base, "why": "weight_key not in catalog for this condition"})
        elif e.direction != w.direction:
            d["ignored"].append({**base, "why": f"direction does not match the catalog ({w.direction})"})
        elif (e.hypothesis, e.weight_key) in seen:
            d["ignored"].append({**base, "why": "same weight_key and direction already counted"})
        else:
            seen.add((e.hypothesis, e.weight_key))
            d["accepted"].append({**base, "w": w, "reason": e.reason or w.reason})
    for cid, d in detail.items():
        cond = cat.conditions[cid]
        # 1. correlated items: the strongest per (cluster, direction) counts; the rest are shown as ignored
        winners: dict[tuple[str, str], dict] = {}
        for a in d["accepted"]:
            if a["w"].cluster:
                k = (a["w"].cluster, a["direction"])
                rank = (-min(a["w"].weight, CLASS_CAPS[a["w"].evidence_class]), -a["w"].weight,
                        _CLASS_ORDER[a["w"].evidence_class], a["weight_key"])
                if k not in winners or rank < winners[k][0]:
                    winners[k] = (rank, a)
        live = []
        for a in d["accepted"]:
            if a["w"].cluster and winners[(a["w"].cluster, a["direction"])][1] is not a:
                best = winners[(a["w"].cluster, a["direction"])][1]["weight_key"]
                d["ignored"].append({"evidence_id": a["evidence_id"], "weight_key": a["weight_key"],
                                     "direction": a["direction"],
                                     "why": f"same cluster ({a['w'].cluster}) as {best}: only the strongest counts"})
            else:
                live.append(a)
        # 2. class totals with caps
        nets: dict[str, float] = {}
        for a in live:
            nets[a["w"].evidence_class] = nets.get(a["w"].evidence_class, 0.0) + (a["w"].weight if a["direction"] == "supports" else -a["w"].weight)
        scale = {}
        for cls, net in nets.items():
            cap = CLASS_CAPS[cls]
            scale[cls] = cap / abs(net) if abs(net) > cap else 1.0
            d["class_totals"][cls] = round(max(-cap, min(cap, net)), 3)
            d["logit"] += max(-cap, min(cap, net))
        for a in live:
            delta = (a["w"].weight if a["direction"] == "supports" else -a["w"].weight) * scale[a["w"].evidence_class]
            d["applied"].append({"evidence_id": a["evidence_id"], "weight_key": a["weight_key"], "direction": a["direction"],
                                 "delta_log_odds": round(delta, 3), "class": a["w"].evidence_class,
                                 "reason": a["reason"], "capped": scale[a["w"].evidence_class] < 1.0})
        d["logit"] = max(-LOGIT_CLAMP, min(LOGIT_CLAMP, d["logit"]))
        d["probability"] = round(_sigmoid(d["logit"]), 4)
        d["applied"].sort(key=lambda a: (-abs(a["delta_log_odds"]), a["evidence_id"]))
    # 3. gates and ruling-out, which need every condition's evidence
    present = {(cid, a["weight_key"]): a for cid, d in detail.items() for a in d["accepted"]}
    for cid, d in detail.items():
        cond = cat.conditions[cid]
        if cid == "unknown":
            d["gate"].append("not a hypothesis: it is the outcome when no condition can be named")
            d["status"] = "ruled_out"
            continue
        if cond.requires_irrigated and req.irrigated is False:
            d["gate"].append("the field is not irrigated")
        if crop_state is not None and not crop_state.diagnosable and not (crop_state.hail_only and cid == "hail"):
            d["gate"].append(f"the season is over for this field ({crop_state.state.replace('_', ' ')})")
        elif not (crop_state is not None and crop_state.hail_only and cid == "hail"):
            ok, why = window_check(cid, req.crop, span)
            if not ok:
                d["gate"].append(why)
        for r in cond.rules_out:
            other, _, key = r.rpartition(".")
            a = present.get((other or cid, key))
            if a is not None:
                d["rule_outs"].append({"weight_key": r, "reason": a["reason"]})
        d["status"] = "ruled_out" if d["gate"] or d["rule_outs"] or d["probability"] < RULED_OUT_BELOW else "open"
    return detail


@dataclass
class Candidate:
    condition: str
    label: str
    probability: float
    tier: Tier
    named: bool
    reasons: list[str] = field(default_factory=list)
    scout_for: str = ""


@dataclass
class Determination:
    """What the scorer concluded: the outcome tier, the named condition (if any) and the leading candidates."""

    outcome: Outcome
    condition: str | None
    probability: float | None
    candidates: list[Candidate]
    detail: dict[str, dict[str, Any]]
    catalog_version: str

    @property
    def top(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def ruled_out(self, limit: int = 3) -> list[dict[str, str]]:
        """Conditions the evidence or a gate ruled out, strongest reason first, for 'what we checked'."""
        cat = load_catalog().conditions
        out = []
        for cid, d in sorted(self.detail.items(), key=lambda kv: kv[1]["probability"]):
            if cid == "unknown" or d["status"] != "ruled_out":
                continue
            why = (d["rule_outs"][0]["reason"] if d["rule_outs"] else d["gate"][0] if d["gate"] else
                   next((a["reason"] for a in d["applied"] if a["delta_log_odds"] < 0), ""))
            if why:
                out.append({"condition": cat[cid].label, "reason": " ".join(str(why).split())[:160]})
        return out[:limit]


def determine(req: C.ScoreRequest | None = None, *, crop_state: C.CropState | None = None, **kwargs: Any) -> Determination:
    """Score the evidence and apply the naming rule. See the module docstring."""
    req = _as_request(req, kwargs)
    cat = load_catalog()
    detail = _score(req, crop_state)
    eligible = {cid: d for cid, d in detail.items() if cid != "unknown" and d["status"] == "open"}

    def has_field_support(cid: str, keys: list[str]) -> bool:
        return any(a["direction"] == "supports" and a["w"].evidence_class == "field"
                   and (not keys or a["weight_key"] in keys) for a in detail[cid]["accepted"])

    named: str | None = None
    ranked = sorted(eligible, key=lambda c: (-eligible[c]["probability"], c))
    for cid in ranked:
        d, cond = eligible[cid], cat.conditions[cid]
        if not any(a["weight_key"] in cond.required_field_evidence and a["direction"] == "supports" for a in d["accepted"]):
            continue
        rivals = [eligible[o]["probability"] for o in ranked if o != cid]
        if d["probability"] >= NAME_THRESHOLD and d["probability"] - (rivals[0] if rivals else 0.0) >= NAME_MARGIN:
            named = cid
            break
    cands: list[Candidate] = []

    def cand(cid: str, tier: Tier, is_named: bool) -> Candidate:
        d, cond = detail[cid], cat.conditions[cid]
        return Candidate(cid, cond.label if tier == "likely" else (cond.label_possible or f"possible {cond.label.lower()}"),
                         d["probability"], tier, is_named,
                         [" ".join(str(a["reason"]).split())[:160] for a in d["applied"] if a["delta_log_odds"] > 0],
                         cond.scout_for)

    if named is not None:
        cond = cat.conditions[named]
        capped = bool(cond.tier_cap) and not any(
            a["weight_key"] in cond.tier_cap.lifted_by_any and a["direction"] == "supports" for a in detail[named]["accepted"])
        cands.append(cand(named, "possible" if capped else "likely", True))
    for cid in ranked:
        cond = cat.conditions[cid]
        if cid == named or eligible[cid]["probability"] < POSSIBLE_MIN:
            continue
        if has_field_support(cid, cond.possible_if_any):
            cands.append(cand(cid, "possible", False))
    first = cands[0] if cands else None
    cands = cands[: max(POSSIBLE_MAX, 1)]
    if first is not None and first.named:
        outcome: Outcome = first.tier
        return Determination(outcome, first.condition, first.probability, cands, detail, cat.version)
    return Determination("not_determined", None, None, cands[:POSSIBLE_MAX], detail, cat.version)


def _listed(cid: str, d: dict[str, Any], req: C.ScoreRequest) -> bool:
    """Show a condition if evidence touched it, or it is a live possibility for this field. `unknown` is never listed."""
    if cid == "unknown":
        return False
    if d["applied"] or d["ignored"]:
        return True
    if load_catalog().conditions[cid].requires_irrigated and req.irrigated is False:
        return False
    return True


def _as_request(req: C.ScoreRequest | None, kwargs: dict) -> C.ScoreRequest:
    return req if req is not None else C.ScoreRequest.model_validate(kwargs)


def result_of(det: Determination, req: C.ScoreRequest) -> C.ScoreResult:
    """A Determination as the contract's ScoreResult (see `score_hypotheses` for what the statuses mean)."""
    likely = det.condition if det.outcome == "likely" else None
    hyps = [C.Hypothesis(condition=cid, probability=d["probability"],  # type: ignore[arg-type]
                         status="ruled_out" if d["status"] == "ruled_out" else "likely" if cid == likely else "open")
            for cid, d in det.detail.items() if _listed(cid, d, req)]
    hyps.sort(key=lambda h: (-h.probability, h.condition))
    return C.ScoreResult(hypotheses=hyps, catalog_version=det.catalog_version)


def score_hypotheses(req: C.ScoreRequest | None = None, *, crop_state: C.CropState | None = None, **kwargs: Any) -> C.ScoreResult:
    """Log-odds scoring (see the module docstring), as the contract's ScoreResult: probability and status per condition.

    Status `likely` means the condition passed the naming rule and may be shown as likely; `open` is everything else
    still in play; `ruled_out` is a probability under 5%, a ruling-out item or a stage/season gate. Evidence whose
    weight_key is not in the catalog for its condition, or whose direction contradicts the catalog, is ignored; a
    repeated (condition, weight_key) counts once. Accepts a ScoreRequest or its fields as keyword arguments (the
    tool-registry calling convention). `unknown` is not listed: "not determined" is an outcome, not a hypothesis.
    """
    req = _as_request(req, kwargs)
    return result_of(determine(req, crop_state=crop_state), req)


def explain(req: C.ScoreRequest | None = None, *, crop_state: C.CropState | None = None, **kwargs: Any) -> dict[str, dict[str, Any]]:
    """Per condition: prior, final probability and status, the tier it earned, evidence that moved it (largest first),
    evidence ignored, class totals, and why it was gated or ruled out."""
    req = _as_request(req, kwargs)
    det = determine(req, crop_state=crop_state)
    tiers = {c.condition: c.tier for c in det.candidates}
    return {cid: {"prior": round(d["prior"], 4), "probability": d["probability"], "status": d["status"],
                  "tier": tiers.get(cid), "named": cid == det.condition,
                  "applied": d["applied"], "ignored": d["ignored"], "class_totals": d["class_totals"],
                  "gate": d["gate"], "rule_outs": d["rule_outs"]}
            for cid, d in det.detail.items() if _listed(cid, d, req)}


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
