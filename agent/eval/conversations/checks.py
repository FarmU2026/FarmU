"""Deterministic checks on one farmer-facing answer. No network, no model: pure functions over text and the record.

`run_checks(checks, ctx)` returns one `CheckResult` per check (passed True/False, or None when it could not be
evaluated, e.g. `consistent_with_record` on a field with no investigation). The optional LLM judge lives in judge.py
and is never mixed into these results.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------- the investigation record


@dataclass
class Record:
    """What we told ourselves about the field: the stored diagnosis (+ scorer explanation) of its latest check."""

    investigation_id: str = ""
    condition: str | None = None
    what: str = ""
    where: str = ""
    why: str = ""
    how_sure: str = ""
    ruled_out: list[str] = field(default_factory=list)
    hypotheses: list[dict] = field(default_factory=list)  # [{condition, probability, status}]
    explanation: dict | None = None  # explanation_json from the investigation item, when the dashboard token is available

    @classmethod
    def from_sources(cls, entry: dict | None, item: dict | None = None) -> "Record":
        """`entry`: a farm payload `diagnoses` item. `item`: GET /v1/investigations/{id} (richer; may be None)."""
        import json as _json

        def loads(value: Any) -> Any:
            if isinstance(value, (dict, list)):
                return value
            try:
                return _json.loads(value or "null")
            except (ValueError, TypeError):
                return None

        diag = {**(entry or {})}
        stored = loads((item or {}).get("diagnosis_json"))
        if isinstance(stored, dict):
            diag = {**diag, **{k: v for k, v in stored.items() if v not in (None, "", [])}}
        expl = loads((item or {}).get("explanation_json"))
        hyps = [h for h in diag.get("hypotheses") or [] if isinstance(h, dict)]
        return cls(
            investigation_id=str((item or {}).get("investigation_id") or diag.get("investigation_id") or ""),
            condition=(diag.get("condition") or (item or {}).get("condition") or None),
            what=str(diag.get("what") or ""), where=str(diag.get("where") or ""), why=str(diag.get("why") or ""),
            how_sure=str(diag.get("how_sure") or (expl or {}).get("how_sure") or ""),
            ruled_out=[str(x) for x in diag.get("ruled_out") or []],
            hypotheses=hyps, explanation=expl if isinstance(expl, dict) else None)

    def text(self) -> str:
        parts = [self.what, self.where, self.why, self.how_sure, " ".join(self.ruled_out), self.condition or ""]
        if self.explanation:
            parts.append(_json_text(self.explanation))
        return "\n".join(p for p in parts if p)

    def ruled_out_conditions(self) -> set[str]:
        out = {h.get("condition") for h in self.hypotheses if h.get("status") == "ruled_out"}
        why = (self.explanation or {}).get("why") or {}
        out |= {r.get("condition") for r in why.get("ruled_out") or [] if isinstance(r, dict)}
        return {c for c in out if c}

    def allowed_conditions(self) -> set[str]:
        """The record's cause plus every hypothesis it still treats as live."""
        live = {h.get("condition") for h in self.hypotheses if h.get("status") != "ruled_out"}
        return {c for c in {self.condition, *live} if c and c != "unknown"}

    def percents(self) -> set[int]:
        out = {round(float(h["probability"]) * 100) for h in self.hypotheses if _is_number(h.get("probability"))}
        for h in (self.explanation or {}).get("hypotheses") or []:
            if isinstance(h, dict) and _is_number(h.get("percent")):
                out.add(round(float(h["percent"])))
        return out

    def probabilities(self) -> set[float]:
        return {float(h["probability"]) for h in self.hypotheses if _is_number(h.get("probability"))}

    def to_dict(self) -> dict:
        return asdict(self)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _json_text(obj: Any) -> str:
    """Every string and number inside a JSON value, space separated (numbers kept so they count as known)."""
    if isinstance(obj, dict):
        return " ".join(_json_text(v) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(_json_text(v) for v in obj)
    if obj is None or isinstance(obj, bool):
        return ""
    return str(obj)


# ---------------------------------------------------------------- vocabulary

CONDITION_WORDS: dict[str, re.Pattern] = {
    "pivot_failure": re.compile(r"\bpivot\b|wheel track|irrigation (?:system|equipment|breakdown)|sprinkler", re.I),
    "hail": re.compile(r"\bhail", re.I),
    "drought": re.compile(r"drought|dry spell|lack of rain|too dry", re.I),
    "ponding": re.compile(r"ponding|standing water|water ?logg|flooded|drowned out", re.I),
    "nitrogen": re.compile(r"nitrogen|\bn (?:deficien|loss|stress)|leached", re.I),
    "herbicide_drift": re.compile(r"herbicide|\bdrift|chemical (?:injury|damage)|dicamba|2,4-d", re.I),
    "planter_skip": re.compile(r"planter|skipped (?:rows|seed)|\bskips?\b|seeding|missed rows|poor stand", re.I),
    "disease_pest": re.compile(r"disease|\bpests?\b|fung|insect|blight|\brust\b|aphid|beetle|worm|mite|leaf spot", re.I),
    "sandy_soil_water_stress": re.compile(r"\bsandy\b|\bsand\b|light soil|water stress|drains fast", re.I),
}
NEGATION = re.compile(r"\b(?:no|not|isn'?t|aren'?t|wasn'?t|doesn'?t|don'?t|didn'?t|never|nothing|none|without|neither|nor|"
                      r"ruled out|rule out|ruling out|rules? out|unlikely|can'?t|cannot)\b", re.I)
# a phrase maps to a tier; the first listed that matches wins (specific before general)
TIER_PHRASES: list[tuple[str, re.Pattern]] = [
    ("not sure yet", re.compile(r"not (?:very |too |totally |entirely |completely )?(?:sure|certain|confident)|unsure|"
                                r"uncertain|can'?t (?:be )?(?:sure|certain)|not yet sure|still checking", re.I)),
    ("pretty sure", re.compile(r"pretty (?:sure|confident|certain)|very (?:sure|confident|certain)|quite (?:sure|confident)|"
                               r"highly confident|almost certain", re.I)),
    ("fairly sure", re.compile(r"fairly (?:sure|confident|certain)|reasonably (?:sure|confident)|moderately (?:sure|confident)|"
                               r"somewhat (?:sure|confident)", re.I)),
]
DIRECTIONS: list[tuple[str, re.Pattern]] = [
    ("north", re.compile(r"\bnorth|\bn\.?e\b|\bn\.?w\b", re.I)),
    ("south", re.compile(r"\bsouth|\bs\.?e\b|\bs\.?w\b", re.I)),
    ("east", re.compile(r"(?<![a-z])(?:north|south)?east|\bn\.?e\b|\bs\.?e\b", re.I)),
    ("west", re.compile(r"(?<![a-z])(?:north|south)?west|\bn\.?w\b|\bs\.?w\b", re.I)),
    ("center", re.compile(r"\bcent(?:er|re|ral)\b|\bmiddle\b|\bmidfield\b", re.I)),
    ("edge", re.compile(r"\bedge|\bborder|\bperimeter|\bfence\b|\bheadland", re.I)),
    ("corner", re.compile(r"\bcorner", re.I)),
]

INTERNAL_IDS = re.compile(r"\b(?:inv|per|fld|apr|act|anm|scn|frm|usr)_\w+|\binv-[\w-]+|\bscene[_-]id\b", re.I)
INDEX_NUMBERS = re.compile(r"(?<![\d.])-?0\.\d{2,3}(?!\d)")
JARGON = re.compile(r"\b(?:ndvi|ndre|gndvi|evi|swir|sentinel-?2|z-?scores?|sigma|index values?)\b", re.I)
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent\b)", re.I)
PRODUCT_ADVICE: list[re.Pattern] = [
    # an application rate: "150 lb N/ac", "2 qt per acre", "30 gallons an acre"
    re.compile(r"\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?(?:lbs?\.?|pounds?|oz\.?|ounces?|qts?\.?|quarts?|pints?|gal\.?|gallons?|"
               r"units?|kg|liters?|l)\b(?:\s*(?:of\s+\w+\s*)?(?:/|per|an|a)\s*(?:ac\b|acre)|\s+(?:of\s+)?(?:n|nitrogen|urea|uan)\b)",
               re.I),
    re.compile(r"\d+(?:\.\d+)?\s*(?:lbs?|pounds?|units?|gal|gallons?|oz|qts?)\s*/\s*(?:ac|acre|a)\b", re.I),
    # product and active-ingredient names
    re.compile(r"\b(?:glyphosate|roundup|atrazine|dicamba|mesotrione|tembotrione|2,4-d|urea|anhydrous|uan|"
               r"ammonium (?:nitrate|sulfate|thiosulfate)|ats|aqua ammonia|headline|trivapro|miravis|quadris|prosaro|"
               r"delaro|tilt|warrior|lambda[- ]cyhalothrin|bifenthrin|chlorpyrifos|pyrethroid|azoxystrobin|"
               r"pyraclostrobin|propiconazole|sidedress(?:ing)? with)\b", re.I),
]

REFUSAL_HINT = re.compile(r"label|extension|agronomist|crop advisor|crop consultant|co-?op|can'?t (?:give|recommend|tell)|"
                          r"cannot (?:give|recommend)|don'?t (?:give|recommend)|not able to (?:recommend|give)", re.I)


# ---------------------------------------------------------------- text helpers


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def strip_phrases(text: str, phrases: list[str]) -> str:
    """Remove field names etc. so "north pivot" is not read as a pivot failure in the north."""
    for p in sorted({p for p in phrases if p}, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(p)}(?!\w)", " ", text, flags=re.I)
        text = re.sub(rf"(?<!\w){re.escape(p)}'?s(?!\w)", " ", text, flags=re.I)
    return text


def claimed_causes(answer: str, ignore: list[str] | None = None) -> set[str]:
    """Condition ids the answer asserts as a cause: keywords in sentences with no negation/ruling-out cue."""
    out: set[str] = set()
    for sent in sentences(strip_phrases(answer, ignore or [])):
        if NEGATION.search(sent):
            continue
        out |= {c for c, rx in CONDITION_WORDS.items() if rx.search(sent)}
    return out


def mentioned_causes(answer: str, ignore: list[str] | None = None) -> set[str]:
    text = strip_phrases(answer, ignore or [])
    return {c for c, rx in CONDITION_WORDS.items() if rx.search(text)}


def tier_of(text: str) -> str | None:
    for tier, rx in TIER_PHRASES:
        if rx.search(text):
            return tier
    return None


def record_tier(how_sure: str) -> str | None:
    """The record's how_sure ('pretty sure' ...) as a tier; None if it says nothing recognizable."""
    low = how_sure.lower()
    for tier in ("not sure yet", "fairly sure", "pretty sure"):
        if tier in low:
            return tier
    return tier_of(how_sure)


def directions_of(text: str, ignore: list[str] | None = None) -> set[str]:
    text = strip_phrases(text, ignore or [])
    return {d for d, rx in DIRECTIONS if rx.search(text)}


def signature(answer: str, ignore: list[str] | None = None) -> dict:
    """The claim an answer makes, for comparing answers: cause(s), certainty tier, location."""
    return {"causes": sorted(claimed_causes(answer, ignore)), "tier": tier_of(answer),
            "where": sorted(directions_of(answer, ignore))}


def numbers_in(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"(?<![\w.])\$?(\d[\d,]*(?:\.\d+)?)", text):
        raw = m.group(1).replace(",", "").rstrip(".")
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- the checks


@dataclass
class CheckResult:
    type: str
    passed: bool | None  # None: not evaluated (reason in detail)
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckContext:
    answer: str
    question: str = ""
    elapsed_s: float | None = None
    record: Record | None = None
    context_text: str = ""  # everything the answer may legitimately quote (record, field facts, the alert message)
    ignore: list[str] = field(default_factory=list)  # field names to ignore when reading causes and places
    answers: list[str] = field(default_factory=list)  # earlier answers in this conversation, by turn index
    new_thread: list[dict] = field(default_factory=list)  # thread items that appeared during this turn


def _opts(spec: Any) -> dict:
    return spec if isinstance(spec, dict) else {}


def check_must_mention_any(patterns: list[str], ctx: CheckContext) -> CheckResult:
    for p in patterns:
        if re.search(p, ctx.answer, re.I):
            return CheckResult("must_mention_any", True, f"mentions /{p}/")
    return CheckResult("must_mention_any", False, "none of: " + ", ".join(patterns))


def check_must_not_mention(patterns: list[str], ctx: CheckContext) -> CheckResult:
    hits: list[str] = []
    allowed_pct = set(ctx.record.percents()) if ctx.record else set()
    allowed_pct |= {round(float(n)) for n in numbers_in(" ".join(m.group(0) for m in PERCENT.finditer(ctx.context_text)))}
    for p in patterns:
        if p == "@internal_ids":
            hits += [f"internal id {m.group(0)!r}" for m in INTERNAL_IDS.finditer(ctx.answer)]
        elif p == "@index_numbers":
            hits += [f"raw index number {m.group(0)!r}" for m in INDEX_NUMBERS.finditer(ctx.answer)]
        elif p == "@jargon":
            hits += [f"jargon {m.group(0)!r}" for m in JARGON.finditer(ctx.answer)]
        elif p == "@percentages":
            hits += [f"percentage {m.group(0)!r}" for m in PERCENT.finditer(ctx.answer)
                     if round(float(m.group(1))) not in allowed_pct]
        elif p == "@product_advice":
            for rx in PRODUCT_ADVICE:
                hits += [f"product/rate {m.group(0)!r}" for m in rx.finditer(ctx.answer)]
        else:
            m = re.search(p, ctx.answer, re.I)
            if m:
                hits.append(f"/{p}/ matched {m.group(0)!r}")
    return CheckResult("must_not_mention", not hits, "; ".join(dict.fromkeys(hits)) or "clean")


def check_max_chars(limit: int, ctx: CheckContext) -> CheckResult:
    n = len(ctx.answer)
    return CheckResult("max_chars", n <= limit, f"{n} chars (limit {limit})")


def check_ascii_only(ctx: CheckContext) -> CheckResult:
    bad = sorted({c for c in ctx.answer if ord(c) > 127})
    return CheckResult("ascii_only", not bad, "ascii" if not bad else "non-ascii: " + " ".join(repr(c) for c in bad[:5]))


def check_answers_within(limit: float, ctx: CheckContext) -> CheckResult:
    t = ctx.elapsed_s
    if t is None:
        return CheckResult("answers_within_s", False, "no timing")
    return CheckResult("answers_within_s", t <= limit, f"{t:.1f}s (limit {limit:g}s)")


def check_no_cause_claimed(ctx: CheckContext) -> CheckResult:
    causes = claimed_causes(ctx.answer, ctx.ignore)
    return CheckResult("no_cause_claimed", not causes,
                       "no cause asserted" if not causes else "asserts cause: " + ", ".join(sorted(causes)))


def check_no_action_items(ctx: CheckContext) -> CheckResult:
    acted = [i for i in ctx.new_thread if i.get("kind") in ("action", "concluded")
             or (i.get("investigation_id") and not str(i["investigation_id"]).startswith("inv-q-"))]
    return CheckResult("no_action_items", not acted,
                       "no action, approval or new investigation appeared" if not acted else
                       "appeared: " + "; ".join(f"{i.get('kind')} {str(i.get('text', ''))[:60]!r}" for i in acted))


def check_no_new_numbers(spec: Any, ctx: CheckContext) -> CheckResult:
    known = set(numbers_in(ctx.context_text)) | set(numbers_in(ctx.question))
    known |= {float(x) for x in _opts(spec).get("allow", [])}
    if ctx.record:
        known |= {float(p) for p in ctx.record.percents()} | ctx.record.probabilities()
    body = strip_phrases(ctx.answer, ctx.ignore)
    new = [n for n in numbers_in(body) if not any(abs(n - k) < 1e-9 for k in known)]
    return CheckResult("no_new_numbers", not new,
                       "every number is in the record/context" if not new else
                       "not in record/context: " + ", ".join(f"{n:g}" for n in dict.fromkeys(new)))


def check_consistent_with_record(spec: Any, ctx: CheckContext) -> CheckResult:
    """Cause, certainty tier and location the answer states must match the investigation record."""
    rec, opts = ctx.record, _opts(spec)
    if rec is None or not (rec.condition or rec.what):
        return CheckResult("consistent_with_record", None, "no investigation record for this field")
    problems: list[str] = []
    notes: list[str] = []
    claimed = claimed_causes(ctx.answer, ctx.ignore)
    allowed = rec.allowed_conditions()
    if rec.condition and rec.condition != "unknown":
        wrong = sorted(claimed - allowed)
        if wrong:
            problems.append(f"says cause {', '.join(wrong)} but the record says {rec.condition}")
        elif claimed:
            notes.append("cause " + ", ".join(sorted(claimed)))
        if opts.get("require_cause") and not (claimed & allowed):
            problems.append(f"does not name the recorded cause ({rec.condition})")
    said_tier, rec_tier = tier_of(ctx.answer), record_tier(rec.how_sure)
    if said_tier and rec_tier and said_tier != rec_tier:
        problems.append(f"says '{said_tier}' but the record says '{rec_tier}'")
    elif said_tier and rec_tier:
        notes.append(f"tier {said_tier}")
    if opts.get("require_tier") and not said_tier:
        problems.append("does not say how sure we are")
    rec_where = directions_of(rec.where, ctx.ignore)
    said_where = directions_of(ctx.answer, ctx.ignore)
    if rec_where and said_where and not said_where <= rec_where:
        problems.append(f"places it {'/'.join(sorted(said_where))} but the record says '{rec.where}'")
    elif rec_where and said_where:
        notes.append("place " + "/".join(sorted(said_where)))
    if opts.get("require_where") and not said_where:
        problems.append("does not say where in the field")
    return CheckResult("consistent_with_record", not problems, "; ".join(problems) or ("matches record: " + ", ".join(notes) if notes else "nothing contradicts the record"))


def check_consistent_with_turn(spec: Any, ctx: CheckContext) -> CheckResult:
    """The answer makes the same claim (cause / tier / place) as an earlier answer to the same question."""
    idx = spec if isinstance(spec, int) else spec.get("turn")
    dims = _opts(spec).get("dims") or ["causes", "tier", "where"]
    if idx >= len(ctx.answers) or not ctx.answers[idx]:
        return CheckResult("consistent_with_turn", None, f"turn {idx} had no answer to compare with")
    a, b = signature(ctx.answers[idx], ctx.ignore), signature(ctx.answer, ctx.ignore)
    diffs = [f"{d}: {a[d]} vs {b[d]}" for d in dims if a[d] != b[d]]
    return CheckResult("consistent_with_turn", not diffs, "; ".join(diffs) or f"same {'/'.join(dims)} as turn {idx}")


def run_checks(checks: dict, ctx: CheckContext) -> list[CheckResult]:
    out: list[CheckResult] = []
    if "must_mention_any" in checks:
        out.append(check_must_mention_any(checks["must_mention_any"], ctx))
    if checks.get("must_not_mention"):
        out.append(check_must_not_mention(checks["must_not_mention"], ctx))
    if "max_chars" in checks:
        out.append(check_max_chars(checks["max_chars"], ctx))
    if checks.get("ascii_only"):
        out.append(check_ascii_only(ctx))
    if "answers_within_s" in checks:
        out.append(check_answers_within(checks["answers_within_s"], ctx))
    if checks.get("no_cause_claimed"):
        out.append(check_no_cause_claimed(ctx))
    if checks.get("no_action_items"):
        out.append(check_no_action_items(ctx))
    if checks.get("no_new_numbers"):
        out.append(check_no_new_numbers(checks["no_new_numbers"], ctx))
    if checks.get("consistent_with_record"):
        out.append(check_consistent_with_record(checks["consistent_with_record"], ctx))
    if "consistent_with_turn" in checks:
        out.append(check_consistent_with_turn(checks["consistent_with_turn"], ctx))
    return out
