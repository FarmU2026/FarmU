"""Typed outputs. The text message, the web page and the dollar figure all come from one object."""
from pydantic import BaseModel, Field

from .contracts import ConditionId, Hypothesis


class SpecialistResponse(BaseModel):
    specialist: str = Field(description="imagery, weather, or soil")
    finding: str = Field(description="One or two plain sentences, no jargon")
    supports: list[str] = Field(default_factory=list, description="Hypotheses this evidence supports")
    rules_out: list[str] = Field(default_factory=list, description="Hypotheses this evidence rules out")
    confidence: float = Field(ge=0, le=1)


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
    # --- interfaces repair (B5): what InvestigationConcluded needs. Optional, so older outputs still validate.
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
        return (
            f"{self.field_name}: {what}. {why}. "
            f"At risk if not fixed: ${self.dollars_low:,}-${self.dollars_high:,}. {tail}"
        )
