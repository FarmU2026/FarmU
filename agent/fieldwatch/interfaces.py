"""Cross-lane seams: the one place a lane looks up how to call another lane.

Rule (CLAUDE.md): to call another lane, import from `fieldwatch.interfaces`. If what you need is not here,
propose it in a separate PR. Never guess another lane's names.

What lives here:
  1. Names: tables, SSM parameters, environment variables, event sources, fixed Lambda function names, bus.
  2. Errors: `FieldwatchError` and the full code list (the specs' "Gateway tools" and "Web API" codes).
  3. Events: `EVENT_REGISTRY` (detail-type -> producers, contract model, consumers) and `publish_event()`,
     the only code allowed to call EventBridge PutEvents (tests/test_interfaces.py enforces it).
  4. Rows shared across lanes: `FarmItem`, `PersonItem`, `ThreadItem`, `InvestigationRecord`,
     `ApprovalRecord`, `ActionRecord` (the DynamoDB items more than one lane reads or writes).
  5. Protocols for each lane's callable surface, with the canonical import path and a loader:
     NotifyAPI (WS9), ApprovalsAPI / ActionsAPI (WS8), AnalysisAPI (WS1), ToolsetAPI (WS6), ContextAPI (WS4).

This module imports only `contracts` and `config` at import time, so every lane (and every Lambda bundle)
can import it without pulling in another lane's dependencies.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import secrets
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from . import config
from . import contracts as C

log = logging.getLogger("fieldwatch.interfaces")


# =====================================================================================================
# 1. Names
# =====================================================================================================

EVENT_BUS = "fieldwatch"  # default; deployed code reads Env.EVENT_BUS (config.EVENT_BUS)
TABLE_PREFIX = "fw-"


class Tables:
    """Full DynamoDB table names (infra/stacks/data_plane.py). Use `table_name("farms")` to honor the prefix."""

    FARMS = "fw-farms"  # pk farm_id, sk item (FARM | PERSON#<id> | THREAD#<at>#<id>); GSI by-phone (phone_e164)
    FIELDS = "fw-fields"  # pk farm_id, sk field_id; GSI by-field (field_id)
    TILE_INDEX = "fw-tile-index"  # pk tile_id, sk field_id
    FIELD_CONTEXT = "fw-field-context"  # pk field_id (FieldContext, flat)
    SCENE_ANALYSES = "fw-scene-analyses"  # pk field_id, sk date_source ("YYYY-MM-DD#<source>")
    WATCHES = "fw-watches"  # pk field_id, sk opened_at_watch_id
    INVESTIGATIONS = "fw-investigations"  # pk investigation_id; GSI by-field (field_id, created_at)
    APPROVALS = "fw-approvals"  # pk approval_id; GSI by-person-status (sent_to_person_id, status)
    ACTIONS = "fw-actions"  # pk action_id; GSI by-assignee
    MESSAGES = "fw-messages"  # pk message_id; GSI by-provider-id
    UPLOADS = "fw-uploads"  # pk upload_id
    IDEMPOTENCY = "fw-idempotency"  # pk key


class Indexes:
    BY_PHONE = "by-phone"
    BY_FIELD = "by-field"
    BY_PERSON_STATUS = "by-person-status"
    BY_ASSIGNEE = "by-assignee"
    BY_PROVIDER_ID = "by-provider-id"


def table_name(short: str) -> str:
    """table_name("farms") -> "fw-farms" (prefix from FIELDWATCH_TABLE_PREFIX at call time)."""
    return os.getenv(Env.TABLE_PREFIX, config.TABLE_PREFIX) + short.removeprefix(TABLE_PREFIX)


class Ssm:
    """SSM parameters. SecureStrings are created by hand (CloudFormation can't create them)."""

    LINK_SIGNING_KEY = "/fieldwatch/link-signing-key"  # SecureString: HMAC key for farmer links + dashboard token
    NASS_API_KEY = "/fieldwatch/nass-api-key"  # SecureString: USDA NASS Quick Stats key (context economics stage)
    DASHBOARD_PASSCODE = "/fieldwatch/dashboard-passcode"  # SecureString: POST /v1/auth/dashboard
    TWILIO_AUTH_TOKEN = "/fieldwatch/twilio-auth-token"  # SecureString: Twilio REST + webhook signature
    RUNTIME_ARN = "/fieldwatch/runtime-arn"  # String: written by hand after `agentcore deploy`
    SESSIONS_BUCKET = "/fieldwatch/sessions-bucket"  # String: FieldWatch-Agent stack
    BUCKET = "/fieldwatch/bucket"  # String: data bucket for the Runtime agent
    GATEWAY_URL = "/fieldwatch/gateway-url"  # String: AgentCore Gateway MCP URL for the Runtime agent


class Env:
    """Environment variable names that cross lanes (set by the CDK stacks, or agent/.env locally)."""

    TABLE_PREFIX = "FIELDWATCH_TABLE_PREFIX"
    BUCKET = "FIELDWATCH_BUCKET"
    EVENT_BUS = "FIELDWATCH_EVENT_BUS"
    REGION = "AWS_REGION"
    # backends
    APPROVALS_BACKEND = "FIELDWATCH_APPROVALS_BACKEND"  # local | dynamo
    DATA_BACKEND = "FIELDWATCH_DATA_BACKEND"  # fixture | dynamo (farm/field data for the agent's local tools)
    TOOLS_BACKEND = "FIELDWATCH_TOOLS_BACKEND"  # local | gateway
    # other lanes' Lambdas and resources (function name or ARN)
    SEND_FN = "FIELDWATCH_SEND_FN"  # WS9 send (SendMessageRequest -> SendMessageResult)
    MIRROR_FN = "FIELDWATCH_MIRROR_FN"  # WS9 mirror reply (InboundMessage -> {status, message_id, response_text})
    NOTIFY_REPLY_FN = "FIELDWATCH_NOTIFY_REPLY_FN"  # WS9 Twilio webhook handler (raw API Gateway event)
    REPLAY_FN = "FIELDWATCH_REPLAY_FN"  # WS2 replay (ReplayRequest)
    RESET_FN = "FIELDWATCH_RESET_FN"  # WS2 reset (ResetRequest)
    CHECK_FN = "FIELDWATCH_CHECK_FN"  # WS2 check latest (CheckLatestRequest)
    SCENE_QUEUE_URL = "FIELDWATCH_SCENE_QUEUE_URL"  # WS2 fw-scene-queue (SceneQueueMessage bodies only)
    CONTEXT_SM_ARN = "FIELDWATCH_CONTEXT_SM_ARN"  # WS15 context builder state machine
    RUNTIME_ARN = "FIELDWATCH_RUNTIME_ARN"
    RUNTIME_ARN_PARAM = "FIELDWATCH_RUNTIME_ARN_PARAM"
    SESSIONS_BUCKET = "FIELDWATCH_SESSIONS_BUCKET"
    GATEWAY_URL = "FIELDWATCH_GATEWAY_URL"
    LINK_KEY_PARAM = "FIELDWATCH_LINK_KEY_PARAM"
    TWILIO_TOKEN_PARAM = "FIELDWATCH_TWILIO_TOKEN_PARAM"
    DASHBOARD_PASSCODE_PARAM = "FIELDWATCH_DASHBOARD_PASSCODE_PARAM"
    PUBLIC_URL = "FIELDWATCH_PUBLIC_URL"  # https front-end base for signed links
    TWILIO_WEBHOOK_URL = "FIELDWATCH_TWILIO_WEBHOOK_URL"


class Functions:
    """Fixed Lambda function names (set with function_name= in their stacks), for wiring and runbooks."""

    NOTIFY_SEND = "fw-notify-send"
    NOTIFY_REPLY = "fw-notify-reply"
    NOTIFY_MIRROR = "fw-notify-mirror"
    SCENE_REPLAY = "fw-scene-replay"
    SCENE_RESET = "fw-scene-reset"
    SCENE_CHECK_LATEST = "fw-scene-check-latest"
    SCENE_FILTER = "fw-scene-filter"


class Sources:
    """EventBridge `source` per producing component (docs/implementation-specs.md "Event catalog")."""

    SCENES = "fieldwatch.scenes"
    REPLAY = "fieldwatch.replay"
    HAZARDS = "fieldwatch.hazards"
    AGENT = "fieldwatch.agent"
    API = "fieldwatch.api"
    NOTIFY = "fieldwatch.notify"
    APPROVALS = "fieldwatch.approvals"
    INTAKE = "fieldwatch.intake"
    SAMPLE = "fieldwatch.sample"  # agent/cloud/feed/send_sample.py: allowed for every detail-type


# Front-end pages for signed links (web/): scope -> (page, query parameter carrying the subject id).
LINK_PAGES: dict[str, tuple[str, str]] = {
    "report": ("report.html", "inv"),
    "work_order": ("workorder.html", "id"),
    "upload": ("upload.html", "id"),
    "onboard": ("index.html", "id"),
}


# =====================================================================================================
# 2. Errors
# =====================================================================================================

ErrorCode = Literal[
    "NOT_FOUND", "POLICY_DENIED", "ALREADY_DECIDED", "EXPIRED", "INVALID_INPUT", "RATE_LIMITED", "QUIET_HOURS",
    "UPSTREAM_TIMEOUT", "UPSTREAM_ERROR", "NOT_IMPLEMENTED", "TOKEN_EXPIRED", "TOKEN_SCOPE", "INTERNAL",
]
ERROR_CODES: tuple[str, ...] = (
    "NOT_FOUND", "POLICY_DENIED", "ALREADY_DECIDED", "EXPIRED", "INVALID_INPUT", "RATE_LIMITED", "QUIET_HOURS",
    "UPSTREAM_TIMEOUT", "UPSTREAM_ERROR", "NOT_IMPLEMENTED", "TOKEN_EXPIRED", "TOKEN_SCOPE", "INTERNAL",
)
# HTTP status the web API answers with for each code.
HTTP_STATUS: dict[str, int] = {
    "NOT_FOUND": 404, "POLICY_DENIED": 403, "ALREADY_DECIDED": 409, "EXPIRED": 409, "INVALID_INPUT": 400,
    "RATE_LIMITED": 429, "QUIET_HOURS": 202, "UPSTREAM_TIMEOUT": 504, "UPSTREAM_ERROR": 502,
    "NOT_IMPLEMENTED": 501, "TOKEN_EXPIRED": 401, "TOKEN_SCOPE": 403, "INTERNAL": 500,
}
RETRYABLE_CODES = frozenset({"RATE_LIMITED", "UPSTREAM_TIMEOUT", "UPSTREAM_ERROR"})


class FieldwatchError(Exception):
    """The one error type lanes raise to each other. `code` is one of ERROR_CODES.

    `details` are extra keys for the error body (e.g. ALREADY_DECIDED carries decided_by, decided_at, status).
    """

    def __init__(self, code: str, message: str, *, retryable: bool | None = None, **details: Any):
        super().__init__(f"{code}: {message}")
        if code not in ERROR_CODES:
            log.warning("FieldwatchError with non-standard code %r", code)
        self.code, self.message, self.details = code, message, details
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def body(self) -> dict:
        """The spec's ErrorBody shape (plus any details)."""
        return {"error": {"code": self.code, "message": self.message, "retryable": self.retryable, **self.details}}


# =====================================================================================================
# 3. Events
# =====================================================================================================


@dataclass(frozen=True)
class EventSpec:
    detail_type: str
    model: type[BaseModel]
    sources: tuple[str, ...]  # allowed producers; the first is the canonical one
    consumers: tuple[str, ...] = ()

    @property
    def source(self) -> str:
        return self.sources[0]


def _spec(model: type[BaseModel], sources: Sequence[str], consumers: Sequence[str] = ()) -> EventSpec:
    return EventSpec(model.__name__, model, tuple(sources), tuple(consumers))


# Every event put on the `fieldwatch` bus or matched by a rule in infra/. detail-type == model class name.
EVENT_REGISTRY: dict[str, EventSpec] = {s.detail_type: s for s in [
    _spec(C.SceneAnalyzed, [Sources.SCENES], ["projector"]),
    _spec(C.AnomalyDetected, [Sources.SCENES], ["session starter", "projector"]),
    _spec(C.MrmsFileAvailable, [Sources.REPLAY, Sources.HAZARDS], ["hail filter (WS3)"]),
    _spec(C.WatchOpened, [Sources.HAZARDS], ["projector"]),
    _spec(C.StartInvestigation, [Sources.API], ["session starter"]),
    _spec(C.InvestigationProgress, [Sources.AGENT], ["projector"]),
    _spec(C.InvestigationConcluded, [Sources.AGENT], ["projector"]),
    _spec(C.ReplyReceived, [Sources.NOTIFY, Sources.API], ["session starter", "projector"]),
    _spec(C.ApprovalRecorded, [Sources.NOTIFY, Sources.APPROVALS, Sources.API], ["session starter", "projector"]),
    _spec(C.PhotoReceived, [Sources.INTAKE], ["session starter", "projector"]),
    _spec(C.DocumentReceived, [Sources.INTAKE], ["session starter", "projector"]),
    _spec(C.ActionCreated, [Sources.APPROVALS], ["projector"]),
    _spec(C.ActionCompleted, [Sources.APPROVALS, Sources.API], ["session starter", "projector"]),
    _spec(C.MessageSent, [Sources.NOTIFY], ["projector"]),
    _spec(C.MessageReceived, [Sources.NOTIFY, Sources.API], ["projector"]),
]}


def event_model(detail_type: str) -> type[BaseModel]:
    """detail-type -> its contract model. Raises FieldwatchError(INVALID_INPUT) for an unknown detail-type."""
    spec = EVENT_REGISTRY.get(detail_type)
    if spec is None:
        raise FieldwatchError("INVALID_INPUT", f"{detail_type!r} is not in interfaces.EVENT_REGISTRY")
    return spec.model


def parse_event(envelope: dict) -> BaseModel:
    """An EventBridge envelope ({detail-type, detail, ...}) -> the validated contract model."""
    return event_model(envelope.get("detail-type", "")).model_validate(envelope.get("detail") or {})


def _entry(model: BaseModel, source: str | None, bus: str) -> dict:
    detail_type = type(model).__name__
    spec = EVENT_REGISTRY.get(detail_type)
    if spec is None:
        raise FieldwatchError("INVALID_INPUT", f"{detail_type} is not a registered event (interfaces.EVENT_REGISTRY)")
    if not isinstance(model, spec.model):
        raise FieldwatchError("INVALID_INPUT", f"{detail_type} must be a {spec.model.__name__}")
    src = source or spec.source
    if src not in spec.sources and src != Sources.SAMPLE:
        raise FieldwatchError("INVALID_INPUT", f"{src} may not publish {detail_type} (allowed: {spec.sources})")
    detail = spec.model.model_validate(model.model_dump(mode="json")).model_dump_json()  # re-validate
    return {"EventBusName": bus, "Source": src, "DetailType": detail_type, "Detail": detail}


def event_bus() -> str:
    return os.getenv(Env.EVENT_BUS) or config.EVENT_BUS or EVENT_BUS


def publish_events(models: Iterable[BaseModel], *, source: str | None = None, client: Any = None,
                   bus: str | None = None) -> int:
    """Validate each event against EVENT_REGISTRY and put them on the bus (batches of 10). Returns the count.

    `source` defaults to the event's canonical producer and must be one of its registered producers.
    `client` is an EventBridge client (tests inject moto's or a fake); default boto3 in config.REGION.
    Raises FieldwatchError: INVALID_INPUT (unregistered event, wrong source) or UPSTREAM_ERROR (rejected).
    """
    bus = bus or event_bus()
    entries = [_entry(m, source, bus) for m in models]
    if not entries:
        return 0
    if client is None:
        import boto3

        client = boto3.client("events", region_name=os.getenv(Env.REGION) or config.REGION)
    for i in range(0, len(entries), 10):
        batch = entries[i:i + 10]
        resp = client.put_events(Entries=batch)  # the only PutEvents call in the codebase
        if resp.get("FailedEntryCount"):
            bad = [r for r in resp.get("Entries", []) if r.get("ErrorCode")]
            raise FieldwatchError("UPSTREAM_ERROR", f"EventBridge rejected {len(bad)} event(s): {bad[:2]}",
                                  retryable=True)
    return len(entries)


def publish_event(model: BaseModel, *, source: str | None = None, client: Any = None, bus: str | None = None) -> None:
    """Put one contract event on the `fieldwatch` bus. See publish_events."""
    publish_events([model], source=source, client=client, bus=bus)


# =====================================================================================================
# 4. Rows shared across lanes (DynamoDB items)
# =====================================================================================================


class Row(BaseModel):
    """Base for table rows: known attributes are typed; unknown ones are kept (other lanes may add some)."""

    model_config = ConfigDict(extra="allow")

    def to_item(self) -> dict[str, Any]:
        """JSON-able dict without None values (convert floats to Decimal at the boto3 boundary)."""
        return self.model_dump(mode="json", exclude_none=True)


FARM_ITEM = "FARM"
PERSON_PREFIX = "PERSON#"
THREAD_PREFIX = "THREAD#"


def person_item_key(person_id: str) -> str:
    return f"{PERSON_PREFIX}{person_id}"


class FarmItem(Row):
    """fw-farms (farm_id, item="FARM"). Written by the web API; read by notify (timezone, quiet_hours) and tools."""

    farm_id: str
    item: Literal["FARM"] = "FARM"
    name: str | None = None
    farmer_name: str | None = None
    phone: str | None = None
    owner_person_id: str | None = None
    timezone: str = "America/Chicago"  # IANA zone; notify's quiet hours and daily cap use it
    quiet_hours: dict[str, str] | None = Field(default=None, description='{"start": "21:00", "end": "07:00"}')
    muted: bool = False
    created_at: C.ISOTime | None = None
    last_checked: C.ISOTime | None = None
    context_build_id: str | None = None


class PersonItem(Row):
    """fw-farms (farm_id, item="PERSON#<person_id>"); GSI by-phone on phone_e164. Notify needs opt_in_at for real SMS."""

    farm_id: str
    item: str
    person_id: str
    name: str | None = None
    phone_e164: str
    role: str = "owner"  # owner | operator | repairs | scout | family | other
    channel_pref: str = "sms"
    opt_in_at: C.ISOTime | None = None
    opted_out: bool = False


class ThreadItem(Row):
    """fw-farms (farm_id, item="THREAD#<at>#<id>"): one line of the phone-panel thread.

    Written by the projector (WS13), and by the web API only for the welcome text when no send Lambda is
    configured. Read by the web API (GET /api/farms/{id} -> thread). Same event -> same key (idempotent)."""

    farm_id: str
    id: str
    at: C.ISOTime
    who: Literal["fieldwatch", "farmer", "agent"]
    text: str
    kind: str = "message"  # message | step | concluded | action | welcome
    investigation_id: str | None = None
    field_id: str | None = None

    @property
    def key(self) -> str:
        return f"{THREAD_PREFIX}{self.at}#{self.id}"

    def to_item(self) -> dict[str, Any]:
        item = {"item": self.key, **super().to_item()}
        item.pop("id", None)
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "ThreadItem":
        key = str(item.get("item", ""))
        at, _, tid = key[len(THREAD_PREFIX):].partition("#")
        data = {k: v for k, v in item.items() if k != "item"}
        data.setdefault("at", at)
        data.setdefault("id", tid or at)
        data.setdefault("who", "fieldwatch")
        data.setdefault("text", "")
        return cls.model_validate(data)


# fw-investigations status values. Writers use InvestigationStatus; readers use OPEN_INVESTIGATION_STATUSES,
# which also accepts names older code wrote, so a reply finds its investigation whoever created it.
InvestigationStatus = Literal["queued", "running", "resuming", "awaiting_reply", "awaiting_upload", "concluded", "failed"]
OPEN_INVESTIGATION_STATUSES = frozenset({
    "queued", "running", "resuming", "awaiting_reply", "awaiting_upload",
    "open", "started", "investigating", "waiting", "waiting_for_farmer",  # legacy names
})


class InvestigationRecord(Row):
    """fw-investigations (pk investigation_id; GSI by-field on field_id + created_at).

    Created by the session starter (status queued), updated by the agent's Recorder (running / awaiting_reply /
    concluded / failed, progress_seq, last_text, diagnosis), claimed by the starter on resume (resuming,
    handled_keys), read by the web API (report page, overview), notify (open investigations) and reset."""

    investigation_id: str
    farm_id: str
    field_id: str
    status: str = "queued"  # InvestigationStatus (legacy rows may hold other names)
    created_at: C.ISOTime
    updated_at: C.ISOTime | None = None
    anomaly_id: str | None = None
    replay: bool = False
    person_id: str | None = None
    pending_interrupt_id: str | None = None
    handled_keys: set[str] | None = None
    progress_seq: int | None = None
    last_text: str | None = None
    message_id: str | None = None
    message_sent_at: C.ISOTime | None = None
    approval_id: str | None = None
    condition: C.ConditionId | None = None
    diagnosis_json: str | None = None
    diagnosis: dict[str, Any] | None = None
    hypotheses: list[dict[str, Any]] | None = None
    evidence: list[dict[str, Any]] | None = None
    impact: dict[str, Any] | None = None
    proposed_action: dict[str, Any] | None = None
    error: str | None = None
    last_event: str | None = None
    last_event_type: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_INVESTIGATION_STATUSES


class ApprovalRecord(C.Approval):
    """fw-approvals item: the Approval contract plus table metadata. Written by approvals.create_pending and by
    notify's send (when a message asks a question); decided only by approvals.record_decision."""

    model_config = ConfigDict(extra="ignore")

    farm_id: str | None = None
    created_at: C.ISOTime | None = None
    sent_to_phone: str | None = None
    expires_at_epoch: int | None = None

    def to_contract(self) -> C.Approval:
        return C.Approval.model_validate(self.model_dump(include=set(C.Approval.model_fields)))


class ActionRecord(C.Action):
    """fw-actions item: the Action contract plus delivery and page metadata (actions.py, web API work orders)."""

    model_config = ConfigDict(extra="ignore")

    farm_id: str | None = None
    field_id: str | None = None
    field_name: str | None = None
    delivered: bool | None = None
    delivery_detail: str | None = None
    created_at: C.ISOTime | None = None
    completed_by: str | None = None
    photo_upload_id: str | None = None

    def to_contract(self) -> C.Action:
        return C.Action.model_validate(self.model_dump(include=set(C.Action.model_fields)))


_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def new_id(prefix: str) -> str:
    """ULID-ish id: prefix + 10 chars of millisecond time + 16 random chars (Crockford base32, lowercase).
    Sortable by creation time and carries no personal data (farm ids never contain a phone number)."""
    ms = int(time.time() * 1000)
    t = "".join(_CROCKFORD[(ms >> (5 * i)) & 31] for i in reversed(range(10)))
    return prefix + t + "".join(secrets.choice(_CROCKFORD) for _ in range(16))


def new_farm_id() -> str:
    return new_id("farm_")


# =====================================================================================================
# 5. Lane APIs (Protocols) and their canonical import paths
# =====================================================================================================

InboundResult = dict  # {"status": ..., "message_id"?: ..., "response_text": str}


@runtime_checkable
class NotifyAPI(Protocol):
    """WS9 (Kurt). Canonical implementation: `fieldwatch.notify.Notifications` (in-process, with injected
    store/provider/clock), built for AWS by `cloud.lambdas.notify.runtime.service()`. Other Lambdas reach it
    through `LambdaNotify` (the deployed fw-notify-send / fw-notify-mirror functions). Get one with `notify()`."""

    def send(self, request: C.SendMessageRequest) -> C.SendMessageResult: ...

    def reply(self, message: C.InboundMessage) -> InboundResult: ...


class ApprovalsAPI(Protocol):
    """WS8. Canonical module: `fieldwatch.approvals` (get it with `approvals()`)."""

    def create_pending(self, investigation_id: str, action_type: str, summary: str, sent_to_person_id: str,
                       ttl_hours: float = 24, *, approval_id: str | None = None, sent_to_phone: str | None = None,
                       farm_id: str | None = None) -> C.Approval: ...

    def get(self, approval_id: str) -> C.Approval | None: ...

    def latest_pending_for(self, person_id: str) -> C.Approval | None: ...

    def record_decision(self, approval_id: str, decision: str, decided_by_person_id: str, channel: str,
                        from_phone: str | None = None, *, emit: bool = True) -> C.Approval:
        """Raises FieldwatchError: NOT_FOUND, POLICY_DENIED, ALREADY_DECIDED, EXPIRED, INVALID_INPUT."""


class ActionsAPI(Protocol):
    """WS8. Canonical module: `fieldwatch.actions` (get it with `actions()`). Every create_* is idempotent on
    approval_id and raises FieldwatchError(POLICY_DENIED) unless the approval is approved and matches."""

    def create_work_order(self, *, approval_id: str, investigation_id: str, assignee_person_id: str, field_id: str,
                          instructions: str, pin: Any = None, field_name: str | None = None,
                          farm_id: str | None = None, assignee_phone: str | None = None,
                          sender: Any = None) -> C.Action: ...

    def create_scout_pin(self, *, approval_id: str, investigation_id: str, person_id: str, pin: Any,
                         what_to_check: str, field_name: str | None = None, farm_id: str | None = None,
                         phone: str | None = None, sender: Any = None) -> C.Action: ...

    def draft_insurance_notice(self, *, approval_id: str, investigation_id: str, person_id: str,
                               details: str | None = None) -> C.Action: ...

    def get_action(self, action_id: str) -> C.Action | None: ...

    def work_order_view(self, action_id: str) -> C.WorkOrderView: ...

    def mark_done(self, action_id: str, by: str, photo_upload_id: str | None = None) -> C.Action: ...


class AnalysisAPI(Protocol):
    """WS1 (Aaron). Canonical modules: `fieldwatch.analysis.analyze` (analyze) and `fieldwatch.analysis.baseline`
    (build_baseline). Until they exist, `analysis()` returns None and callers use the registry stubs."""

    def analyze(self, request: C.FieldAnalysisRequest) -> C.FieldAnalysisResult: ...

    def build_baseline(self, *, field_id: str, polygon: list[list[float]], tile_ids: list[str], crop: str,
                       bucket: str | None) -> C.FieldBaseline | dict: ...


class ToolsetAPI(Protocol):
    """WS6. Canonical module: `fieldwatch.toolset` (get it with `toolset()`)."""

    def tools_for(self, backend: str | None = None, *, access: Iterable[str] = ("read",),
                  names: Iterable[str] | None = None) -> Sequence: ...


class ContextAPI(Protocol):
    """WS4 (Kurt). Canonical module: `fieldwatch.context` (get it with `context()`). Each function is one
    component of FieldContext, so the context workflow's stages stay independent. Only the facade is part of the
    seam: the modules behind it (soil, terrain, boundaries, disease, economics) are Kurt's to reorganize."""

    def get_soil_profile(self, field_id: str) -> C.SoilProfile: ...

    def get_terrain_summary(self, field_id: str) -> C.TerrainSummary: ...

    def get_water_context(self, field_id: str) -> C.WaterContext: ...

    def get_crop_context(self, field_id: str, as_of: str | None = None) -> dict:
        """{crop_history, cohort_field_ids, county_fips, stage}"""

    def get_economics(self, field_id: str, as_of: str | None = None) -> dict:
        """{expected_yield_bu_ac, price_usd_bu, price_as_of, sources}"""

    def get_field_context(self, field_id: str) -> C.FieldContext: ...

    def disease_reports(self, county_fips: str, crop: str, season: int) -> list[C.DiseaseReport]: ...

    def cached_cohort_field_ids(self, field_id: str) -> list[str]: ...


CANONICAL = {
    "context": "fieldwatch.context",
    "notify": "fieldwatch.notify:Notifications",
    "approvals": "fieldwatch.approvals",
    "actions": "fieldwatch.actions",
    "analysis.analyze": "fieldwatch.analysis.analyze:analyze",
    "analysis.build_baseline": "fieldwatch.analysis.baseline:build_baseline",
    "toolset": "fieldwatch.toolset",
}


def approvals() -> ApprovalsAPI:
    return importlib.import_module("fieldwatch.approvals")  # type: ignore[return-value]


def actions() -> ActionsAPI:
    return importlib.import_module("fieldwatch.actions")  # type: ignore[return-value]


def toolset() -> ToolsetAPI:
    return importlib.import_module("fieldwatch.toolset")  # type: ignore[return-value]


def context() -> ContextAPI:
    """Kurt's field-context facade. Light to import (no Strands), so the stage Lambdas can use it."""
    return importlib.import_module(CANONICAL["context"])  # type: ignore[return-value]


def _optional(path: str):
    """'pkg.module:attr' -> the attribute, or None when that module does not exist yet (a missing *dependency*
    of an existing module still raises: that is a real failure, not a lane that hasn't landed)."""
    module_name, _, attr = path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name and module_name.startswith(exc.name):
            return None
        raise
    return getattr(module, attr, None)


class _Analysis:
    def __init__(self, analyze, build_baseline):
        self.analyze, self.build_baseline = analyze, build_baseline


def analysis() -> AnalysisAPI | None:
    """Aaron's analyzer and baseline, or None while either is missing."""
    analyze = _optional(CANONICAL["analysis.analyze"])
    baseline = _optional(CANONICAL["analysis.build_baseline"])
    if analyze is None and baseline is None:
        return None
    return _Analysis(analyze, baseline)  # type: ignore[return-value]


def build_baseline_fn():
    """Just the baseline builder (the context workflow needs only this), or None."""
    return _optional(CANONICAL["analysis.build_baseline"])


# ---------- notify: reaching WS9 from another Lambda / the agent ----------


class LambdaNotify:
    """NotifyAPI over the deployed WS9 Lambdas (synchronous invoke). Errors become FieldwatchError."""

    def __init__(self, send_fn: str, mirror_fn: str | None = None, client: Any = None):
        self.send_fn, self.mirror_fn, self._client = send_fn, mirror_fn, client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("lambda", region_name=os.getenv(Env.REGION) or config.REGION)
        return self._client

    def _invoke(self, fn: str, payload: dict) -> dict:
        try:
            out = self.client.invoke(FunctionName=fn, InvocationType="RequestResponse",
                                     Payload=json.dumps(payload).encode())
        except Exception as exc:  # network / throttling / permissions
            raise FieldwatchError("UPSTREAM_ERROR", f"invoking {fn} failed: {exc}", retryable=True) from exc
        raw = out["Payload"].read() if hasattr(out.get("Payload"), "read") else out.get("Payload") or b""
        body = json.loads(raw or b"null")
        if out.get("FunctionError"):
            msg = body.get("errorMessage") if isinstance(body, dict) else str(body)
            code = "INVALID_INPUT" if isinstance(body, dict) and body.get("errorType") in (
                "ValueError", "ValidationError") else "UPSTREAM_ERROR"
            raise FieldwatchError(code, f"{fn}: {msg}", retryable=code == "UPSTREAM_ERROR")
        return body or {}

    def send(self, request: C.SendMessageRequest) -> C.SendMessageResult:
        return C.SendMessageResult.model_validate(self._invoke(self.send_fn, request.model_dump(mode="json")))

    def reply(self, message: C.InboundMessage) -> InboundResult:
        if not self.mirror_fn:
            raise FieldwatchError("NOT_IMPLEMENTED", "no mirror reply Lambda configured (FIELDWATCH_MIRROR_FN)")
        if not message.provider_message_id.startswith("mirror:"):
            raise FieldwatchError("INVALID_INPUT", "mirror replies need a provider_message_id starting with mirror:")
        return self._invoke(self.mirror_fn, message.model_dump(mode="json"))


_notify_override: NotifyAPI | None = None


def use_notify(impl: NotifyAPI | None) -> None:
    """Inject a NotifyAPI (tests, the local harness). None restores the environment-based choice."""
    global _notify_override
    _notify_override = impl


def notify(*, lambda_client: Any = None) -> NotifyAPI | None:
    """The notify lane as configured: an injected implementation, else LambdaNotify when FIELDWATCH_SEND_FN is
    set (using `lambda_client` if given), else None (local runs: callers fall back to the legacy `fieldwatch.sms`
    or the phone mirror)."""
    if _notify_override is not None:
        return _notify_override
    send_fn = os.getenv(Env.SEND_FN, "").strip()
    if not send_fn:
        return None
    return LambdaNotify(send_fn, os.getenv(Env.MIRROR_FN, "").strip() or None, client=lambda_client)


def mirror_message_id() -> str:
    """Stable provider id for a reply typed into the phone mirror (WS9 requires the `mirror:` prefix)."""
    return "mirror:" + new_id("")
