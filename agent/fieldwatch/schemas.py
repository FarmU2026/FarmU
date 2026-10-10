"""Typed outputs. The text message, the web page and the dollar figure all come from one object."""
from pydantic import BaseModel, Field


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

    def to_text(self) -> str:
        """The SMS. Under ~300 chars, five things: what, where, why, stake, what to do."""
        ask = {
            "repair_work_order": "Send a repair ticket?",
            "scouting_pin": "Send a scouting pin?",
            "request_photo": "Can you snap a leaf photo?",
            "insurance_notice": "Draft a note to your insurance agent?",
            "none": "No action needed.",
        }[self.recommended_action]
        return (
            f"{self.field_name}: {self.what} {self.why} "
            f"At risk if not fixed: ${self.dollars_low:,}-${self.dollars_high:,}. "
            f"{ask} YES / NO / MORE"
        )
