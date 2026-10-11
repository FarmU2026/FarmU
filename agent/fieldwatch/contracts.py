"""Shared contracts: every payload that crosses a workstream boundary.

Transcribed from docs/implementation-specs.md (field tables) with the overrides in docs/decisions.md.
Each workstream builds against these models and the sample files in fixtures/contracts/.
Change a model only in its own small PR, and tell the team: other lanes depend on it.

Conventions: times are ISO 8601 UTC strings, dates YYYY-MM-DD, coordinates [lon, lat] (EPSG:4326),
bounds [west, south, east, north], money in USD, area in acres, distance in meters, hail in mm.
"""
from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal["1"] = "1"


class Contract(BaseModel):
    """Base for all contracts: unknown fields are rejected so drift shows up in tests."""

    model_config = ConfigDict(extra="forbid")


# ---------- basic types ----------

ISOTime = str
ISODate = str
Bounds = tuple[float, float, float, float]
LonLat = tuple[float, float]

Crop = Literal["corn", "soybeans", "wheat", "milo", "alfalfa", "other"]
ConditionId = Literal[
    "pivot_failure", "hail", "drought", "ponding", "nitrogen",
    "herbicide_drift", "planter_skip", "disease_pest", "sandy_soil_water_stress", "unknown",
]
AgentName = Literal["supervisor", "imagery", "environment", "agronomy"]


class FieldRef(Contract):
    farm_id: str
    field_id: str


class ErrorDetail(Contract):
    code: str
    message: str
    retryable: bool = False


class ErrorBody(Contract):
    error: ErrorDetail


# ---------- analysis ----------


class PatternFeatures(Contract):
    """All scores 0-1. Computed by the field analyzer (workstream 1)."""

    ring_score: float = Field(ge=0, le=1, description="Stress on a ring around the pivot center")
    wedge_score: float = Field(ge=0, le=1, description="Concentrated in one angular sector")
    edge_band_score: float = Field(ge=0, le=1, description="Concentrated along one field edge")
    stripe_score: float = Field(ge=0, le=1, description="Straight parallel bands")
    lowspot_overlap: float = Field(ge=0, le=1, description="Share of region inside the low-spot mask")
    sandy_soil_overlap: float = Field(ge=0, le=1, description="Share of region inside sandy soil zones")
    hail_swath_overlap: float | None = Field(default=None, description="None when no open hail watch")


class AnomalyRegion(Contract):
    region_id: str
    geometry: dict[str, Any] = Field(description="GeoJSON Polygon, lon/lat")
    area_acres: float
    centroid: LonLat
    score: float = Field(description="Combined anomaly score; higher is more unusual")
    features: PatternFeatures


class OverlayRef(Contract):
    key: str = Field(description="S3 key (cloud) or path under agent/data (local)")
    bounds: Bounds


class IndexStats(Contract):
    mean: float
    p10: float
    p50: float
    p90: float


class Indices(Contract):
    ndvi: IndexStats
    ndre: IndexStats
    ndmi: IndexStats


class AnomalySummary(Contract):
    field_score: float
    z_baseline: float | None = Field(default=None, description="vs same weeks in prior seasons; None without history")
    z_cohort: float | None = Field(default=None, description="vs same-crop neighbors; None without neighbors")
    delta_since_last: float | None = Field(default=None, description="median NDVI change since the previous clear pass")
    regions: list[AnomalyRegion] = Field(default_factory=list)


class Gates(Contract):
    senescence: bool
    harvest: bool
    regional: bool


class Overlays(Contract):
    true_color: OverlayRef
    health: OverlayRef
    mask: OverlayRef | None = None
    departure: OverlayRef | None = Field(default=None, description="smoothed z vs this field's normal (the statistic regions are found on)")
    change: OverlayRef | None = Field(default=None, description="NDVI now minus the previous clear pass; None without a previous pass")
    normal: OverlayRef | None = Field(default=None, description="baseline NDVI (same palette as health); None without a baseline")


class OverlayScale(Contract):
    """Fixed color scale of a diverging overlay, so images from different dates are comparable."""

    stat: str = Field(description="what a pixel encodes, e.g. 'z' or 'ndvi_diff'")
    vmin: float = Field(description="value drawn as the strongest red (below normal / decline)")
    vmax: float = Field(description="value drawn as the strongest blue (above normal / gain); 0 is the light neutral")
    unit: str = ""


class OverlayComparison(Contract):
    """What the departure / change / normal overlays compare, for honest captions."""

    scene_date: ISODate = Field(description="date of the scene analyzed (the 'now' in every overlay)")
    previous_date: ISODate | None = Field(default=None, description="previous clear pass used by `change`; None if none")
    baseline_dates: list[ISODate] = Field(default_factory=list, description="scene dates of the prior seasons behind `normal` and `departure`, oldest first")
    baseline_method: Literal["median", "mean"] | None = Field(default=None, description="per-pixel combination of the baseline seasons")
    median_departure_ndvi: float | None = Field(default=None, description="field-wide median of (NDVI now - baseline); `departure` is relative to this")
    departure_scale: OverlayScale | None = None
    change_scale: OverlayScale | None = None


class SceneInfo(Contract):
    scene_id: str
    source: str = "sentinel-2-l2a"
    datetime: ISOTime
    tile_id: str
    cloud_cover: float
    assets: dict[str, str] = Field(default_factory=dict, description="band name -> COG href")


class FieldAnalysisRequest(Contract):
    schema_version: Literal["1"] = SCHEMA_VERSION
    field: FieldRef
    polygon: list[LonLat] = Field(description="Field boundary ring, lon/lat")
    crop: Crop = "corn"
    as_of: ISODate = Field(description="Analyze the latest clear scene on or before this date (replay)")
    scene: SceneInfo | None = Field(default=None, description="Pin a specific scene; otherwise search")
    interior_buffer_m: float = 10.0
    pivot: dict[str, Any] | None = Field(default=None, description="{center: [lon, lat], radius_m}")
    open_watch_ids: list[str] = Field(default_factory=list)
    min_clear_fraction: float = 0.7
    min_region_acres: float = 1.2
    z_threshold: float = 2.0


class FieldAnalysisResult(Contract):
    schema_version: Literal["1"] = SCHEMA_VERSION
    field: FieldRef
    scene_id: str
    date: ISODate
    status: Literal["ok", "too_cloudy", "no_baseline", "error"]
    clear_fraction: float
    indices: Indices | None = None
    anomaly: AnomalySummary | None = None
    gates: Gates | None = None
    overlays: Overlays | None = None
    comparison: OverlayComparison | None = None
    analysis_key: str | None = None
    error: ErrorDetail | None = None


# ---------- context and hazards ----------


class SoilMapUnit(Contract):
    mukey: str
    name: str
    pct_area: float
    texture_0_30: str
    texture_30_100: str
    awc_0_100_cm: float
    drainage_class: str
    hydro_group: str
    sandy: bool


class SoilProfile(Contract):
    field_id: str
    map_units: list[SoilMapUnit]
    zones_key: str | None = None
    source: str = "NRCS Soil Data Access"


class TerrainSummary(Contract):
    slope_mean_pct: float
    lowspot_pct: float
    lowspot_key: str | None = None
    source: str = "USGS 3DEP"


class WaterContext(Contract):
    """Mapped surface water around a field: FEMA flood zones, NHD streams, NWI wetlands. Each source is optional;
    `sources` names what answered (and what failed), so a missing source never reads as 'no water'."""

    floodplain_pct: float | None = Field(default=None, ge=0, le=100,
                                         description="Share of the field inside a Special Flood Hazard Area; None = FEMA unavailable")
    flood_zones: list[str] = Field(default_factory=list, description="FEMA zone codes present, e.g. ['AE', 'X']")
    nearest_stream_m: float | None = Field(default=None, ge=0, description="From the field edge; 0 if one crosses it")
    nearest_stream_name: str | None = None
    wetland_pct: float | None = Field(default=None, ge=0, le=100,
                                      description="Share of the field mapped as wetland (NWI); None = NWI unavailable")
    wetland_types: list[str] = Field(default_factory=list, description="NWI wetland types present")
    sources: list[str] = Field(default_factory=list)


class WeatherDay(Contract):
    date: ISODate
    precip_mm: float
    tmax_c: float | None = None
    tmin_c: float | None = None
    et0_mm: float | None = None
    wind_max_kph: float | None = None


class WeatherSummary(Contract):
    field_id: str
    start: ISODate
    end: ISODate
    daily: list[WeatherDay]
    total_precip_mm: float
    normal_precip_mm: float | None = None
    days_over_35c: int = 0
    source: str = "Open-Meteo archive"


class HailReport(Contract):
    field_id: str
    start: ISODate
    end: ISODate
    max_hail_mm: float = Field(description="MRMS MESH max over the field; 0 if none")
    pct_area_over_threshold: float
    threshold_mm: float = 25.4
    peak_date: ISODate | None = None
    ground_reports: list[dict[str, Any]] = Field(default_factory=list, description="SPC reports within 10 km")
    source: str = "NOAA MRMS MESH"


class DroughtStatus(Contract):
    county_fips: str
    as_of: ISODate
    category: Literal["none", "D0", "D1", "D2", "D3", "D4"]
    pct_by_category: dict[str, float]
    rule: str = Field(description="How category was derived from the percentages")
    source: str = "US Drought Monitor"


class Watch(Contract):
    watch_id: str
    type: Literal["hail", "drought", "nws_alert"]
    fields: list[FieldRef]
    detail: dict[str, Any] = Field(default_factory=dict)
    opened_at: ISOTime
    expires_at: ISOTime


class DiseaseReport(Contract):
    disease: str
    county_fips: str
    status: Literal["confirmed_this_year", "history"]
    first_observed: ISODate | None = None
    last_observed: ISODate | None = None
    source: str = "EDDMapS (Corn ipmPIPE)"


class FieldBaseline(Contract):
    """What the context builder's baseline stage stores (WS1 `build_baseline`; see interfaces.AnalysisAPI)."""

    key: str | None = Field(default=None, description="S3 key of baseline.parquet under context/{field_id}/")
    seasons: list[int | str] = Field(default_factory=list, description="Prior seasons used (same weeks)")
    cohort_field_ids: list[str] = Field(default_factory=list)
    stub: bool = Field(default=False, description="True when the stage ran on sample data")


class FieldContext(Contract):
    """Per-field facts gathered by the context workstream (4); each part may be missing.

    Stored flat in fw-field-context: each context-builder stage writes its own attributes."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    field_id: str
    built_at: ISOTime
    soil: SoilProfile | None = None
    terrain: TerrainSummary | None = None
    water: WaterContext | None = None
    crop_history: dict[str, str] = Field(default_factory=dict, description="year -> crop, from USDA CSB")
    cohort_field_ids: list[str] = Field(default_factory=list)
    county_fips: str | None = None
    expected_yield_bu_ac: float | None = None
    price_usd_bu: float | None = None
    price_as_of: ISODate | None = None
    stage: str | None = Field(default=None, description="Growth stage in words, e.g. 'R2 (blister)'")
    disease_reports: list[DiseaseReport] = Field(default_factory=list, description="County reports (EDDMapS)")
    baseline: FieldBaseline | None = None
    sources: list[str] = Field(default_factory=list)


# ---------- reasoning ----------


class EvidenceItem(Contract):
    evidence_id: str
    hypothesis: ConditionId
    direction: Literal["supports", "refutes"]
    weight_key: str = Field(description="Key into catalog/conditions.json weights")
    source: str = Field(description="Tool name, 'photo', 'document' or 'farmer_reply'")
    value: Any = Field(description="The raw fact, e.g. {'rain_mm_30d': 112}")
    reason: str = Field(description="One plain-language sentence")
    created_at: ISOTime


class Hypothesis(Contract):
    condition: ConditionId
    probability: float = Field(ge=0, le=1, description="From the scorer, never from an LLM")
    status: Literal["open", "likely", "ruled_out"]


class ScoreRequest(Contract):
    investigation_id: str
    crop: Crop
    stage: str | None = None
    irrigated: bool | None = None
    evidence: list[EvidenceItem]


class ScoreResult(Contract):
    hypotheses: list[Hypothesis]
    catalog_version: str


class ImpactRequest(Contract):
    field_id: str
    condition: ConditionId
    affected_acres: float
    stage: str | None = None
    severity: Literal["low", "medium", "high"] = "medium"
    days_until_fixed: int = 7


class ImpactEstimate(Contract):
    affected_acres: float
    low_usd: float
    high_usd: float
    basis: str = Field(description="Inputs in words: yield, price, loss share and its source")


class SpecialistResponse(Contract):
    """Plan contract for specialists. Replaces fieldwatch.schemas.SpecialistResponse in workstream 5/7."""

    agent: Literal["imagery", "environment", "agronomy"]
    evidence: list[EvidenceItem]
    notes: str = Field(description="Short, for the supervisor only")
    tool_calls: int


class ProposedAction(Contract):
    action_type: Literal["work_order", "scout_pin", "photo_request", "insurance_notice", "none"]
    summary: str
    assignee_person_id: str | None = None


# ---------- approvals and actions ----------


class Approval(Contract):
    approval_id: str
    investigation_id: str
    action_type: Literal["work_order", "scout_pin", "photo_request", "insurance_notice"]
    summary: str
    sent_to_person_id: str
    status: Literal["pending", "approved", "declined", "expired"]
    decided_by: str | None = None
    decided_at: ISOTime | None = None
    channel: Literal["sms", "web", "voice"] | None = None
    expires_at: ISOTime


class Action(Contract):
    action_id: str
    approval_id: str
    investigation_id: str
    type: Literal["work_order", "scout_pin", "photo_request", "insurance_notice"]
    assignee_person_id: str | None = None
    pin: LonLat | None = None
    instructions: str
    status: Literal["sent", "done"]
    done_at: ISOTime | None = None


# ---------- messaging ----------


class SendMessageRequest(Contract):
    schema_version: Literal["1"] = SCHEMA_VERSION
    farm_id: str
    person_id: str
    investigation_id: str | None = None
    body: str = Field(max_length=480, description="Aim for under ~300 GSM-7 characters; no emoji")
    link: dict[str, Any] | None = Field(default=None, description="{scope, subject_id, ttl_hours}")
    approval: dict[str, Any] | None = Field(default=None, description="{action_type, ttl_hours}: creates a pending approval")
    urgency: Literal["low", "actionable", "urgent"] = "actionable"
    idempotency_key: str


class SendMessageResult(Contract):
    message_id: str
    approval_id: str | None = None
    status: Literal["sent", "deferred_quiet_hours", "rate_limited", "mirror_only", "opted_out"]
    provider: str
    scheduled_for: ISOTime | None = None


class InboundMessage(Contract):
    provider_message_id: str
    from_phone: str
    body: str
    received_at: ISOTime
    in_reply_to: str | None = None


# ---------- events (EventBridge bus "fieldwatch"; detail-type = class name) ----------


class EventBase(Contract):
    schema_version: Literal["1"] = SCHEMA_VERSION
    occurred_at: ISOTime


class SceneAnalyzed(EventBase):
    field: FieldRef
    scene_id: str
    date: ISODate
    status: Literal["ok", "too_cloudy", "no_baseline", "error"]
    max_score: float
    region_count: int
    analysis_key: str | None = None


class AnomalyDetected(EventBase):
    anomaly_id: str
    field: FieldRef
    scene_id: str
    date: ISODate
    regions: list[AnomalyRegion]
    overlays: Overlays
    open_watch_ids: list[str] = Field(default_factory=list)
    replay: bool = False


class WatchOpened(EventBase):
    watch: Watch


class InvestigationProgress(EventBase):
    investigation_id: str
    seq: int
    agent: AgentName
    step: Literal["plan", "tool_call", "evidence", "score", "message", "waiting", "done", "error"]
    text: str
    tool: str | None = None
    latency_ms: int | None = None
    evidence: EvidenceItem | None = None
    hypotheses: list[Hypothesis] | None = None


class InvestigationConcluded(EventBase):
    investigation_id: str
    field: FieldRef
    condition: ConditionId
    summary: str
    hypotheses: list[Hypothesis]
    impact: ImpactEstimate | None = None
    proposed_action: ProposedAction
    message_id: str | None = None
    fast_path: bool = False


class ReplyReceived(EventBase):
    reply_id: str
    farm_id: str
    person_id: str
    intent: Literal["yes", "no", "more", "photo", "ask", "stop", "start", "help", "free_text"]
    text: str
    investigation_id: str | None = None
    approval_id: str | None = None


class ApprovalRecorded(EventBase):
    approval_id: str
    investigation_id: str
    decision: Literal["approved", "declined"]
    decided_by: str
    channel: Literal["sms", "web", "voice"]
    farm_id: str | None = Field(default=None, description="Lets the projector skip the investigation lookup")


ActionType = Literal["work_order", "scout_pin", "photo_request", "insurance_notice"]


class ActionCreated(EventBase):
    """A write tool recorded an action behind an approval (actions.py). The projector shows `action_created`."""

    action_id: str
    approval_id: str
    investigation_id: str
    type: ActionType
    summary: str = Field(description="Plain words for the feed, e.g. the work-order instructions")
    farm_id: str | None = None
    assignee_person_id: str | None = None
    field_name: str | None = None
    delivered: bool = False


class ActionCompleted(EventBase):
    action_id: str
    investigation_id: str
    completed_by: str
    farm_id: str | None = None
    photo_upload_id: str | None = None


class MrmsFileAvailable(EventBase):
    """A 24-hour MRMS hail-size file to process (replay backfill today; the hazards lane's hail filter consumes it)."""

    bucket: str
    key: str
    product: str
    valid_time: ISOTime


class UploadLocation(Contract):
    lon: float
    lat: float
    accuracy_m: float | None = None


class UploadReceived(EventBase):
    """Shared shape of PhotoReceived / DocumentReceived (intake lane, WS17)."""

    upload_id: str
    kind: Literal["photo", "document"]
    field: FieldRef
    investigation_id: str | None = None
    s3_key: str
    location: UploadLocation | None = None
    result: dict[str, Any] | None = Field(default=None, description="PhotoResult or DocumentResult (intake lane)")


class PhotoReceived(UploadReceived):
    kind: Literal["photo"] = "photo"


class DocumentReceived(UploadReceived):
    kind: Literal["document"] = "document"


class MessageSent(EventBase):
    message_id: str
    farm_id: str
    person_id: str
    body: str
    investigation_id: str | None = None
    status: str
    provider: str


class MessageReceived(EventBase):
    message_id: str
    farm_id: str
    person_id: str
    body: str
    investigation_id: str | None = None


# ---------- agent runtime ----------


class StartInvestigation(Contract):
    schema_version: Literal["1"] = SCHEMA_VERSION
    action: Literal["start"] = "start"
    investigation_id: str
    farm_id: str
    field: str = Field(description="Field id or the farmer's name for it")
    anomaly: AnomalyDetected | None = None
    question: str | None = Field(
        default=None, max_length=480,
        description="Set for a question run: the farmer's free-text message (data, not instructions). The agent "
                    "answers it read-only instead of running a diagnosis.")
    about_investigation_id: str | None = Field(
        default=None, description="Question runs: an existing investigation the question may refer to (context only)")


class ResumeInvestigation(Contract):
    schema_version: Literal["1"] = SCHEMA_VERSION
    action: Literal["reply"] = "reply"
    investigation_id: str
    interrupt_id: str
    text: str
    approval_id: str | None = None


AgentRequest = Union[StartInvestigation, ResumeInvestigation]


class AgentStep(Contract):
    """What the agent returns for start/reply (mirrors investigation.Step)."""

    status: Literal["waiting_for_farmer", "done", "failed"]
    text: str
    investigation_id: str
    interrupt_id: str | None = None
    diagnosis: dict[str, Any] | None = None
    hypotheses: list[Hypothesis] | None = None
    backend: Literal["local", "runtime"] = "local"
    error: ErrorDetail | None = None


# ---------- scene pipeline requests (API -> scenes lane, WS2) ----------


class SceneQueueMessage(Contract):
    """Every message body fw-scene-queue accepts, normalized by `parse()`. On the wire a body is one of:

    - a STAC item (Earth Search new-item message, or the check-latest Lambda), optionally wrapped in an SNS
      `{"Type": "Notification", "Message": "<json>"}` envelope: parsed as replay=False;
    - this model as JSON with replay=True (the replay Lambda; `stac_item` is the item to analyze).

    Nothing else goes on the queue: "check now" invokes the check-latest Lambda (CheckLatestRequest), and
    replay / reset invoke their Lambdas (ReplayRequest, ResetRequest).
    """

    replay: bool = False
    replay_id: str | None = None
    stac_item: dict[str, Any]
    field_ids: list[str] | None = Field(default=None, description="Only these fields (default: every registered one)")
    as_of: ISODate | None = Field(default=None, description="Replay date the analysis treats as 'today'")

    @classmethod
    def parse(cls, body: "str | dict[str, Any]") -> "SceneQueueMessage":
        """Raw SQS body -> SceneQueueMessage. Raises ValueError for anything that is not an accepted shape."""
        import json

        data = json.loads(body) if isinstance(body, str) else body
        if isinstance(data, dict) and data.get("Type") == "Notification" and "Message" in data:
            data = json.loads(data["Message"])
        if not isinstance(data, dict):
            raise ValueError("message is not a JSON object")
        if data.get("replay") and isinstance(data.get("stac_item"), dict):
            return cls(replay=True, replay_id=data.get("replay_id"), stac_item=data["stac_item"],
                       field_ids=data.get("field_ids"), as_of=data.get("as_of"))
        if data.get("type") == "Feature" or "assets" in data:
            return cls(replay=False, stac_item=data)
        raise ValueError("message is neither a STAC item nor a replay envelope")


class ReplayRequest(Contract):
    """Invoke payload of the replay Lambda (fw-scene-replay): scene_id, or field_ids with as_of."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    replay_id: str | None = None
    scene_id: str | None = None
    field_ids: list[str] | None = None
    as_of: ISODate | None = None
    farm_id: str | None = None
    delay_s: int | None = Field(default=None, ge=0, le=900)
    skip_hail: bool = False
    requested_at: ISOTime | None = None


class ResetRequest(Contract):
    """Invoke payload of the reset Lambda (fw-scene-reset)."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    farm_id: str | None = None
    requested_at: ISOTime | None = None


class CheckLatestRequest(Contract):
    """Invoke payload of the check-latest Lambda (fw-scene-check-latest). Empty = every registered tile."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    farm_id: str | None = None
    field_ids: list[str] | None = Field(default=None, description="Only the tiles these fields sit on")
    reason: Literal["schedule", "signup", "check_now"] = "schedule"
    requested_at: ISOTime | None = None


# ---------- web feed ----------


class FeedMessage(Contract):
    type: Literal[
        "field_status", "scene_analyzed", "investigation_step", "evidence_added",
        "hypotheses_updated", "concluded", "message_out", "message_in", "action_created",
    ]
    ts: ISOTime
    farm_id: str
    investigation_id: str | None = None
    agent: AgentName | None = None
    backend: Literal["local", "cloud"] = "local"
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------- web API views (WS12 serves, WS14 renders) ----------


class ReportField(Contract):
    field_id: str
    name: str
    acres: float


class ReportOverlays(Contract):
    true_color_url: str
    health_url: str
    mask_url: str | None = None
    bounds: Bounds


class ReportSummary(Contract):
    headline: str
    detail: str


class CheckedItem(Contract):
    condition: ConditionId
    label: str = Field(description="Plain words, e.g. 'Pivot not watering'")
    status: Literal["open", "likely", "ruled_out"]
    reason: str


class ReportAction(Contract):
    approval_id: str
    label: str
    status: Literal["pending", "approved", "declined", "expired"]
    decided_by: str | None = None


class ReportView(Contract):
    """GET /v1/reports/{investigation_id}: the field report page."""

    field: ReportField
    scene_date: ISODate
    overlays: ReportOverlays
    summary: ReportSummary
    checked: list[CheckedItem]
    impact: ImpactEstimate | None = None
    action: ReportAction | None = None
    navigate_url: str = Field(description="Opens the phone's maps app at the problem area")
    updated_since_sent: bool = False


class WorkOrderView(Contract):
    """GET /v1/work-orders/{action_id}: the repair person's page."""

    action_id: str
    field_name: str
    pin: LonLat
    instructions: str
    status: Literal["sent", "done"]
    navigate_url: str


# ---------- evaluation ----------


class ScenarioExpected(Contract):
    alert: bool
    top_condition: ConditionId | None = None
    action_type: str | None = None
    max_trigger_to_message_s: float = 60


class Scenario(Contract):
    schema_version: Literal["1"] = SCHEMA_VERSION
    scenario_id: str
    farm_fixture: str = Field(description="Path under agent/fixtures/")
    field_id: str
    as_of: ISODate
    expected: ScenarioExpected
    scripted_replies: list[dict[str, Any]] = Field(default_factory=list)


# Map fixture file name -> model, used by tests/test_contracts.py
FIXTURE_MODELS: dict[str, type[Contract]] = {
    "field_analysis_request": FieldAnalysisRequest,
    "field_analysis_result": FieldAnalysisResult,
    "field_context": FieldContext,
    "soil_profile": SoilProfile,
    "water_context": WaterContext,
    "weather_summary": WeatherSummary,
    "hail_report": HailReport,
    "drought_status": DroughtStatus,
    "disease_report": DiseaseReport,
    "score_request": ScoreRequest,
    "score_result": ScoreResult,
    "impact_request": ImpactRequest,
    "impact_estimate": ImpactEstimate,
    "specialist_response": SpecialistResponse,
    "approval": Approval,
    "action": Action,
    "send_message_request": SendMessageRequest,
    "send_message_result": SendMessageResult,
    "inbound_message": InboundMessage,
    "anomaly_detected": AnomalyDetected,
    "investigation_progress": InvestigationProgress,
    "investigation_concluded": InvestigationConcluded,
    "reply_received": ReplyReceived,
    "approval_recorded": ApprovalRecorded,
    "action_created": ActionCreated,
    "action_completed": ActionCompleted,
    "mrms_file_available": MrmsFileAvailable,
    "photo_received": PhotoReceived,
    "scene_queue_message": SceneQueueMessage,
    "replay_request": ReplayRequest,
    "reset_request": ResetRequest,
    "check_latest_request": CheckLatestRequest,
    "start_investigation": StartInvestigation,
    "resume_investigation": ResumeInvestigation,
    "agent_step": AgentStep,
    "feed_message": FeedMessage,
    "report_view": ReportView,
    "work_order_view": WorkOrderView,
    "scenario": Scenario,
}
