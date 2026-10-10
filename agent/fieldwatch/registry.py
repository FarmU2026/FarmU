"""The tool registry: the one list both backends use (docs/decisions.md D1).

- Local mode: agents call `fn` in-process.
- Gateway mode (workstream 6): each entry becomes a Lambda-backed Gateway tool whose input schema is
  `input_model.model_json_schema()`, so local and cloud tools can't drift apart.

Entries marked `stub=True` return the sample from fixtures/contracts/ until their workstream replaces
`fn` with the real implementation. Keep `fn` free of web-app and AWS-SDK imports where possible so it
can run inside a Lambda unchanged.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import actions as A
from . import contracts as C
from . import tools as T
from .config import FIXTURES

Access = Literal["read", "write"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldIdIn(_In):
    field_id: str = Field(description="Field id from list_fields (or the farmer's name for it)")


class FieldRangeIn(_In):
    field_id: str
    start: C.ISODate
    end: C.ISODate


class DroughtIn(_In):
    county_fips: str
    as_of: C.ISODate


class DiseaseIn(_In):
    county_fips: str
    crop: C.Crop
    season: int


class LossIn(_In):
    field_id: str
    affected_acres: float
    yield_loss_fraction: float = Field(ge=0, le=1)


APPROVAL_ID_PATTERN = r"^apr-[A-Za-z0-9_-]{3,64}$"  # keep in step with infra/policies/cedar/fieldwatch-write-tools.cedar


class _WriteIn(_In):
    """Every write tool requires both ids. The Gateway rejects a call without them (spike finding 23) and the
    Cedar policy checks their shape; the tool itself checks the approval's status, owner, type and expiry."""

    investigation_id: str = Field(min_length=1)
    approval_id: str = Field(pattern=APPROVAL_ID_PATTERN,
                             description="Required: id of an approved approval for this exact action")


class WorkOrderIn(_WriteIn):
    field_id: str
    assignee_person_id: str
    instructions: str
    pin: C.LonLat | None = None


class ScoutPinIn(_WriteIn):
    person_id: str
    pin: C.LonLat
    what_to_check: str


class InsuranceNoticeIn(_WriteIn):
    person_id: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    access: Access
    input_model: type[BaseModel]
    fn: Callable
    output_model: type[BaseModel] | None = None
    workstream: int = 0
    stub: bool = False
    notes: str = field(default="")


def _sample(name: str, model: type[BaseModel]) -> Callable:
    """A stub that returns the contract sample, validated, so other lanes can build before the real thing lands."""

    def fn(**_kwargs):
        return model.model_validate(json.loads((FIXTURES / "contracts" / f"{name}.json").read_text()))

    fn.__name__ = f"stub_{name}"
    return fn


TOOLS: list[ToolSpec] = [
    # --- live today (tools.py); outputs still untyped dicts, typed by their workstream ---
    ToolSpec("list_fields", "List every field on the farm with its id, name, crop and acres.", "read",
             _In, T.list_fields),
    ToolSpec("look_at_field", "Latest clear satellite pass over a field as two images plus summary numbers.", "read",
             FieldIdIn, T.look_at_field, workstream=1, notes="Becomes a view over analyze_field's overlays."),
    ToolSpec("get_weather", "Last 30 days of weather at a field (Open-Meteo).", "read",
             FieldIdIn, T.get_weather, output_model=None, workstream=3, notes="Type to C.WeatherSummary; take start/end for replay."),
    ToolSpec("get_soil", "Soil survey for a field.", "read", FieldIdIn, T.get_soil, workstream=4,
             notes="Fixture-only today; replace with Soil Data Access (C.SoilProfile)."),
    ToolSpec("estimate_loss", "Crude dollar impact (yield x price x fraction).", "read", LossIn, T.estimate_loss,
             workstream=5, notes="Superseded by estimate_impact."),
    # --- planned: stubs return contract samples until the workstream lands ---
    ToolSpec("analyze_field", "Deterministic analysis of the latest clear scene on or before as_of: masked indices, "
             "baseline and change z-scores, problem regions with pattern features, gates.", "read",
             C.FieldAnalysisRequest, _sample("field_analysis_result", C.FieldAnalysisResult),
             output_model=C.FieldAnalysisResult, workstream=1, stub=True),
    ToolSpec("get_hail", "Max radar hail size (mm) over a field for a date range, plus nearby ground reports.", "read",
             FieldRangeIn, _sample("hail_report", C.HailReport), output_model=C.HailReport, workstream=3, stub=True),
    ToolSpec("get_drought", "US Drought Monitor category for a county as of a date.", "read",
             DroughtIn, _sample("drought_status", C.DroughtStatus), output_model=C.DroughtStatus, workstream=3, stub=True),
    ToolSpec("get_soil_profile", "Soil map units for a field from NRCS Soil Data Access.", "read",
             FieldIdIn, _sample("soil_profile", C.SoilProfile), output_model=C.SoilProfile, workstream=4, stub=True),
    ToolSpec("get_field_context", "Everything static about a field: soil, terrain, crop history, neighbors, economics.",
             "read", FieldIdIn, _sample("field_context", C.FieldContext), output_model=C.FieldContext, workstream=4, stub=True),
    ToolSpec("get_disease_reports", "County disease confirmations for the season (EDDMapS).", "read",
             DiseaseIn, _sample("disease_report", C.DiseaseReport), output_model=C.DiseaseReport, workstream=4, stub=True),
    ToolSpec("score_hypotheses", "Deterministic hypothesis probabilities from evidence items and the condition catalog.",
             "read", C.ScoreRequest, _sample("score_result", C.ScoreResult), output_model=C.ScoreResult, workstream=5, stub=True),
    ToolSpec("estimate_impact", "Dollar range for a condition on the affected acres, from published loss tables.", "read",
             C.ImpactRequest, _sample("impact_estimate", C.ImpactEstimate), output_model=C.ImpactEstimate, workstream=5, stub=True),
    # --- write tools (workstream 8): need an approved approval_id; see actions.py ---
    ToolSpec("create_work_order", "Send a repair work order to the assignee. Requires an approved approval_id.", "write",
             WorkOrderIn, A.create_work_order, output_model=C.Action, workstream=8,
             notes="Texts the assignee via notify.send. Idempotent on approval_id. Locally the agent uses tools.create_work_order, which calls this."),
    ToolSpec("create_scout_pin", "Drop a scouting pin with what to look for and text it to the person who will walk it. "
             "Requires an approved approval_id.", "write", ScoutPinIn, A.create_scout_pin, output_model=C.Action,
             workstream=8, notes="Idempotent on approval_id."),
    ToolSpec("draft_insurance_notice", "Draft a notice of loss for the farmer to review and send. Requires an approved "
             "approval_id.", "write", InsuranceNoticeIn, A.draft_insurance_notice, output_model=C.Action, workstream=8,
             notes="Sends nothing; the draft is in Action.instructions. document_key (S3) is not implemented yet."),
]

BY_NAME = {t.name: t for t in TOOLS}
