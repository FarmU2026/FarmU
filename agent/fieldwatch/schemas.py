"""Typed outputs. The text message, the web page and the dollar figure all come from one object."""
import re
from typing import Literal

from pydantic import BaseModel, Field

from . import contracts as C
from .contracts import ConditionId, Hypothesis


# The plan's specialist contract lives in contracts.py (agent, evidence items, notes, tool_calls).
# Re-exported so `from fieldwatch.schemas import SpecialistResponse` keeps working.
SpecialistResponse = C.SpecialistResponse

SMS_CHARS = 320  # one farmer text: FieldWatch alerts aim for this length

# Farmer-facing certainty is a tier, never a percentage (docs/research/grounding.md 5.5): the scorer's probabilities
# are not calibrated yet, so they stay in the record and the tests and out of the words.
TIER_PHRASE = {"likely": "likely", "possible": "possible, not confirmed", "not_determined": "not determined yet"}


def how_sure_phrase(tier: "str | float | None") -> str:
    """Plain-words certainty from the scorer's tier ('likely', 'possible' or 'not_determined'). The LLM never picks
    this. A bare probability is mapped through the scorer's naming thresholds (kept for older callers)."""
    from . import scoring

    if isinstance(tier, (int, float)):
        tier = ("likely" if tier >= scoring.NAME_THRESHOLD else "possible" if tier >= scoring.POSSIBLE_MIN
                else "not_determined")
    return TIER_PHRASE.get(str(tier), TIER_PHRASE["not_determined"])


_PERCENT = re.compile(r"(?:\b(?:about|roughly|around|nearly|almost)\s+|~\s*)?(\d+(?:\.\d+)?)\s?(?:%|percent\b)", re.I)
_LEAD = r"(?:\b(?:about|roughly|around|nearly|almost|up to|over)\s+)?"
_DOLLARS = re.compile(_LEAD + r"\$\s?(\d[\d,]*(?:\.\d+)?)(?:\s?(?:-|to)\s?\$?\s?(\d[\d,]*(?:\.\d+)?))?", re.I)
_ACRES = re.compile(_LEAD + r"(\d+(?:\.\d+)?)\s?(?:-\s?(\d+(?:\.\d+)?)\s?)?acres?\b", re.I)
# Internal ids mean nothing to a farmer: per_01, inv-q-9690ce2c, act-3f2a, apr-x1. Needs a digit, so words
# like "per-acre" stay.
INTERNAL_ID = re.compile(r"(?:\s+(?:to|for|by|from))?\s*\(?\b(?:per|act|apr|inv|fld|pin|msg)[_-](?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\b\)?",
                         re.I)


def _tidy(text: str) -> str:
    out = re.sub(r"\s{2,}", " ", text).replace(" .", ".").replace(" ,", ",").replace("( ", "(").replace(" )", ")")
    return out.strip()


def scrub_percentages(text: str, allowed: set[int]) -> str:
    """Remove any percentage the scorer did not produce. `allowed` holds whole-number percents from score results."""

    def drop(m: re.Match) -> str:
        return m.group(0) if round(float(m.group(1))) in allowed else ""

    return _tidy(_PERCENT.sub(drop, text))


def scrub_internal_ids(text: str) -> str:
    return _tidy(INTERNAL_ID.sub("", text))


def scrub_figures(text: str, *, acres: float | None = None, low_usd: float | None = None,
                  high_usd: float | None = None) -> str:
    """Keep only the acres and dollar figures the impact tool produced (a figure matching them, to the whole number, stays)."""
    okay_usd = {round(v) for v in (low_usd, high_usd) if v is not None}

    def usd(m: re.Match) -> str:
        nums = [round(float(g.replace(",", ""))) for g in m.groups() if g]
        return m.group(0) if nums and all(n in okay_usd for n in nums) else ""

    def ac(m: re.Match) -> str:
        nums = [float(g) for g in m.groups() if g]
        return m.group(0) if acres is not None and all(round(n) == round(acres) for n in nums) else ""

    return _tidy(_ACRES.sub(ac, _DOLLARS.sub(usd, text)))


def fit_sms(parts: list[str], tail: str, limit: int = SMS_CHARS) -> str:
    """Join `parts` (most important first) and `tail` into one text of at most `limit` characters: the lowest-priority
    parts are dropped first, and the first part is shortened rather than lose the tail (the question to the farmer)."""
    parts = [p.strip() for p in parts if p and p.strip()]
    while parts and len(" ".join([*parts, tail]).strip()) > limit:
        if len(parts) == 1:
            room = limit - len(tail) - 1
            parts[0] = parts[0][: max(room - 3, 0)].rstrip(" ,;:") + "..." if room > 3 else ""
            break
        parts.pop()
    return " ".join([*parts, tail]).strip() if tail else " ".join(parts)


class FarmerReply(BaseModel):
    """The only thing a question run may send the farmer: one short plain text. The model fills this field through
    structured output, so its reasoning ("first I should...", "Here is my response") can never reach a farmer."""

    text: str = Field(description=f"The reply to the farmer: plain ASCII, at most {SMS_CHARS} characters, no percentages "
                                  "or index values, no internal ids, nothing but the reply itself")


# readings like "0.18-0.20", "an index of 0.4": raw index values mean nothing to a farmer (acres like "0.5 acres" stay)
_INDEX_VALUE = re.compile(
    r"(?:\b(?:readings?|values?|index|indices|scores?|ndvi|ndre|ndmi)\s+(?:of\s+|at\s+|was\s+|is\s+|near\s+)?)?"
    r"(?:(?:about|around|roughly|near)\s+)?-?[01]?\.\d{2,}(?:\s?(?:-|to)\s?-?[01]?\.\d+)?(?!\d)(?!\s?acres?)", re.I)
_ADVICE_Q = re.compile(r"\b(spray|fungicide|herbicide|insecticide|pesticide|chemical|product|rate|how much|how many|"
                       r"apply|application|fertili[sz]er|nitrogen|sidedress|what should i (?:put|use|do))\b", re.I)
_RATE = re.compile(r"\d+(?:\.\d+)?\s?(?:lbs?|pounds?|oz|ounces?|gal(?:lons?)?|pts?|pints?|qts?|quarts?|units?|kg|l|ml)\b"
                   r"(?:\s?(?:/|per)\s?(?:ac(?:re)?|a|ha))?", re.I)
ADVICE_ANSWER = ("I can't recommend a product or a rate from here. Check the product label, your local Extension office "
                 "or your agronomist or co-op, and have someone scout the problem area first.")


def is_advice_question(text: str) -> bool:
    return bool(_ADVICE_Q.search(text or ""))


def scrub_index_values(text: str) -> str:
    return _tidy(_INDEX_VALUE.sub("", text))


def trim_sms(text: str, limit: int = SMS_CHARS) -> str:
    """At most `limit` characters, cut at a sentence boundary (a word boundary plus '...' if one sentence is too long)."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)]
    if ends:
        return cut[:ends[-1]].strip()
    return cut[: max(limit - 3, 0)].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def farmer_text(text: str, *, question: str = "", limit: int = SMS_CHARS) -> str:
    """Model-written farmer text made safe in code: no percentages, internal ids or raw index values, ASCII, and at
    most `limit` characters. An advice question (what to spray, how much nitrogen) never gets a product or a rate."""
    out = scrub_internal_ids(scrub_index_values(scrub_percentages(str(text), set())))
    out = out.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-", 0x2026: "...", 0xA0: " "})
    out = out.encode("ascii", "ignore").decode("ascii")
    if is_advice_question(question) and _RATE.search(out):
        return ADVICE_ANSWER
    return trim_sms(out, limit)


class Diagnosis(BaseModel):
    field_name: str = Field(description="The name the farmer uses for the field")
    what: str = Field(description="What is wrong, in plain words a farmer would say")
    where: str = Field(description="Where in the field")
    why: str = Field(description="Why we think so, citing what was checked and ruled out")
    ruled_out: list[str] = Field(default_factory=list)
    dollars_low: int = Field(description="Low end of the loss estimate if nothing is done")
    dollars_high: int = Field(description="High end of the loss estimate if nothing is done")
    how_sure: str = Field(description="'likely', 'possible, not confirmed' or 'not determined yet' (set from the scorer)")
    recommended_action: str = Field(
        description="One of: repair_work_order, scouting_pin, request_photo, insurance_notice, none"
    )
    action_detail: str = Field(description="What the person doing the action should check or do")
    # --- interfaces repair: what InvestigationConcluded needs. Optional, so older outputs still validate.
    condition: ConditionId | None = Field(default=None, description="The named condition id from the catalog; 'unknown' when the cause is not determined")
    affected_acres: float | None = Field(default=None, description="Acres the dollar range was estimated on")
    hypotheses: list[Hypothesis] = Field(
        default_factory=list,
        description="Copy exactly from the last score_hypotheses result; leave empty if it was not called. "
                    "Never write your own probabilities.",
    )
    # --- the scorer's tier (set by DiagnosisGuard from the last score result; the model never picks it)
    outcome: Literal["likely", "possible", "not_determined"] | None = Field(
        default=None, description="Copy from score_hypotheses `outcome`; leave empty if it was not called.")
    possible: list[str] = Field(default_factory=list, description="Leading candidates when the cause is not determined")

    def to_text(self, approved: bool | None = None) -> str:
        """The SMS: one message of about 320 characters. what, where, why (or what was ruled out), stake, what to do.

        approved=None: the action is still a question. True/False: the farmer already answered. A diagnosis the
        scorer could not name ("not determined") says what was ruled out and proposes a scouting pin.
        """
        ask = {
            "repair_work_order": ("Send a repair ticket?", "Repair ticket sent."),
            "scouting_pin": ("Send a scouting pin?", "Scouting pin sent."),
            "request_photo": ("Can you snap a leaf photo?", "Photo link sent."),
            "insurance_notice": ("Draft a note to your insurance agent?", "Insurance note drafted."),
            "none": ("No action needed.", "No action needed."),
        }.get(self.recommended_action, ("Want me to act on this?", "Done."))
        if self.recommended_action == "none":
            tail = "No action needed. Reply MORE for details."
        elif approved is None:
            tail = f"{ask[0]} YES / NO / MORE"
        elif approved:
            tail = f"{ask[1]} Reply MORE for details."
        else:
            tail = "No action taken. Reply MORE for details."
        what = self.what.rstrip(".")
        why = self.why.rstrip(".")
        if self.outcome is None:  # no scorer tier (older output): the original shape
            stake = f"At risk if not fixed: ${self.dollars_low:,}-${self.dollars_high:,}." if self.dollars_high else ""
            return fit_sms([f"{self.field_name}: {what}.", f"{why}.", stake], tail)
        where = self.where.rstrip(".")
        place = f"{where[:1].upper()}{where[1:]}." if where and where.lower() not in what.lower() else ""
        if self.outcome == "not_determined":
            ruled = f"Ruled out: {', '.join(self.ruled_out[:3])}." if self.ruled_out else ""
            possible = f"Possible: {', '.join(self.possible[:3])}." if self.possible else ""
            return fit_sms([f"{self.field_name}: {what}.", place, ruled, possible], tail)
        stake = f"At risk if not fixed: ${self.dollars_low:,}-${self.dollars_high:,}." if self.dollars_high else ""
        return fit_sms([f"{self.field_name} ({self.how_sure}): {what}.", f"{why}.", place, stake], tail)
