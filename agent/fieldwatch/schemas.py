"""Typed outputs. The text message, the web page and the dollar figure all come from one object."""
import re

from pydantic import BaseModel, Field

from . import contracts as C
from .contracts import ConditionId, Hypothesis


# The plan's specialist contract lives in contracts.py (agent, evidence items, notes, tool_calls).
# Re-exported so `from fieldwatch.schemas import SpecialistResponse` keeps working.
SpecialistResponse = C.SpecialistResponse

PRETTY_SURE = 0.8  # scorer probability at or above which we say "pretty sure"
FAIRLY_SURE = 0.55


def how_sure_phrase(top_probability: float) -> str:
    """Plain-words certainty, derived from the scorer's top probability. The LLM never picks this."""
    if top_probability >= PRETTY_SURE:
        return "pretty sure"
    if top_probability >= FAIRLY_SURE:
        return "fairly sure"
    return "not sure, need a photo"


_PERCENT = re.compile(r"(?:\b(?:about|roughly|around|nearly|almost)\s+|~\s*)?(\d+(?:\.\d+)?)\s?(?:%|percent\b)", re.I)


def scrub_percentages(text: str, allowed: set[int]) -> str:
    """Remove any percentage the scorer did not produce. `allowed` holds whole-number percents from score results."""

    def drop(m: re.Match) -> str:
        return m.group(0) if round(float(m.group(1))) in allowed else ""

    out = _PERCENT.sub(drop, text)
    return re.sub(r"\s{2,}", " ", out).replace(" .", ".").replace(" ,", ",").strip()


class Diagnosis(BaseModel):
    field_name: str = Field(description="The name the farmer uses for the field")
    what: str = Field(description="What is wrong, in plain words a farmer would say")
    where: str = Field(description="Where in the field")
    why: str = Field(description="Why we think so, citing what was checked and ruled out")
    ruled_out: list[str] = Field(default_factory=list)
    dollars_low: int = Field(description="Low end of the loss estimate if nothing is done")
    dollars_high: int = Field(description="High end of the loss estimate if nothing is done")
    how_sure: str = Field(description="'pretty sure', 'fairly sure', or 'not sure, need a photo'")
    recommended_action: str = Field(
        description="One of: repair_work_order, scouting_pin, request_photo, insurance_notice, none"
    )
    action_detail: str = Field(description="What the person doing the action should check or do")
    # --- interfaces repair: what InvestigationConcluded needs. Optional, so older outputs still validate.
    condition: ConditionId | None = Field(default=None, description="The likely condition id from the catalog")
    affected_acres: float | None = Field(default=None, description="Acres the dollar range was estimated on")
    hypotheses: list[Hypothesis] = Field(
        default_factory=list,
        description="Copy exactly from the last score_hypotheses result; leave empty if it was not called. "
                    "Never write your own probabilities.",
    )

    def to_text(self, approved: bool | None = None) -> str:
        """The SMS. Under ~300 chars, five things: what, where, why, stake, what to do.

        approved=None: the action is still a question. True/False: the farmer already answered.
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
        stake = f"At risk if not fixed: ${self.dollars_low:,}-${self.dollars_high:,}. " if self.dollars_high else ""
        return f"{self.field_name}: {what}. {why}. {stake}{tail}"
