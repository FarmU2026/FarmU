"""Run an investigation, pause for the farmer, resume with their reply.

Sessions persist so a reply that arrives an hour later, on a different process or a different Runtime
microVM, resumes the same paused agent:
  - FIELDWATCH_SESSIONS_BUCKET set  -> S3SessionManager in that bucket under `sessions/` (Runtime)
  - unset                           -> FileSessionManager under agent/sessions (local harness, tests)

`start()` / `reply()` are the local-harness API (FastAPI dev server, tests). `run_request()` is what the
Runtime entrypoint (app.py) runs in the background: it wraps them with a Recorder that writes status to
`fw-investigations` and emits InvestigationProgress / InvestigationConcluded on the `fieldwatch` bus.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from strands.session import FileSessionManager

from . import config, interfaces
from . import contracts as C
from .agents import build_supervisor
from .config import SESSIONS
from .hooks import ToolTimeline, print_event
from .schemas import Diagnosis

log = logging.getLogger(__name__)

LIMITS = {"turns": 20}
SESSIONS_PREFIX = "sessions/"
SOURCE = interfaces.Sources.AGENT


@dataclass
class Step:
    status: str  # "waiting_for_farmer" | "done"
    text: str  # the SMS to send
    diagnosis: Diagnosis | None = None
    interrupt_id: str | None = None
    timeline: list[dict] | None = None


# ---------------------------------------------------------------- sessions and agent wiring


def session_manager(investigation_id: str):
    """S3 when config.SESSIONS_BUCKET is set, local files otherwise. Same session id either way."""
    session_id = f"fieldwatch-{investigation_id}"
    if config.SESSIONS_BUCKET:
        from strands.session import S3SessionManager

        return S3SessionManager(
            session_id=session_id, bucket=config.SESSIONS_BUCKET, prefix=SESSIONS_PREFIX, region_name=config.REGION
        )
    SESSIONS.mkdir(exist_ok=True)
    return FileSessionManager(session_id=session_id, storage_dir=str(SESSIONS))


_session = session_manager  # old name, kept for callers of the previous version


def _gateway_tools():
    """Tools from the Gateway lane's toolset when it exists and the backend isn't local; else None."""
    if config.TOOLS_BACKEND == "local":
        return None
    try:
        # import_module, not `from . import toolset`: that form returns the package attribute once the
        # submodule has been imported anywhere, so sys.modules can't make it fail (or swap it) at call time.
        toolset = importlib.import_module(f"{__package__}.toolset")
    except ImportError:
        log.warning("FIELDWATCH_TOOLS_BACKEND=%s but fieldwatch.toolset is missing; using local tools", config.TOOLS_BACKEND)
        return None
    try:
        return toolset.tools_for(config.TOOLS_BACKEND, access=("read", "write"))
    except TypeError:
        return toolset.tools_for(config.TOOLS_BACKEND)


def _build(timeline: ToolTimeline, sm):
    """build_supervisor with Gateway tools when it can take them (agents.py needs a `tools` parameter)."""
    tools = _gateway_tools()
    if tools is not None and "tools" in inspect.signature(build_supervisor).parameters:
        return build_supervisor(timeline, session_manager=sm, tools=tools)
    if tools is not None:
        log.warning("build_supervisor has no `tools` parameter yet; running with its built-in tools")
    return build_supervisor(timeline, session_manager=sm)


def _to_step(result, timeline: ToolTimeline, approved: bool | None = None) -> Step:
    if result.stop_reason == "interrupt" and result.interrupts:
        it = result.interrupts[0]
        reason = it.reason if isinstance(it.reason, dict) else {"summary": str(it.reason)}
        return Step(
            status="waiting_for_farmer",
            text=f"{reason.get('summary')} YES / NO",
            interrupt_id=it.id,
            timeline=timeline.events,
        )
    diag = result.structured_output
    text = diag.to_text(approved) if diag else re.sub(r"<thinking>.*?</thinking>\s*", "", str(result), flags=re.S).strip()
    return Step(status="done", text=text, diagnosis=diag, timeline=timeline.events)


def start(investigation_id: str, field_name: str, emit=print_event) -> Step:
    """Kick off an investigation. Usually ends paused on an approval interrupt."""
    timeline = ToolTimeline(emit)
    agent = _build(timeline, session_manager(investigation_id))
    result = agent(
        f"Investigate the field called '{field_name}'.",
        limits=LIMITS,
        structured_output_model=Diagnosis,
    )
    return _to_step(result, timeline)


def reply(investigation_id: str, interrupt_id: str, farmer_text: str, emit=print_event) -> Step:
    """The farmer texted back. Resume the paused agent with their answer and finish the diagnosis."""
    timeline = ToolTimeline(emit)
    agent = _build(timeline, session_manager(investigation_id))
    result = agent(
        [{"interruptResponse": {"interruptId": interrupt_id, "response": farmer_text}}],
        limits=LIMITS,
        structured_output_model=Diagnosis,
    )
    return _to_step(result, timeline, approved=farmer_text.strip().upper().startswith("Y"))


def to_agent_step(step: Step, investigation_id: str) -> C.AgentStep:
    return C.AgentStep(
        status=step.status,
        text=step.text,
        investigation_id=investigation_id,
        interrupt_id=step.interrupt_id,
        diagnosis=step.diagnosis.model_dump() if step.diagnosis else None,
        backend="runtime",
    )


# ---------------------------------------------------------------- recording: fw-investigations + the bus


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ToolTimeline agent names -> contract AgentName (the plan renames the specialists)
_AGENT = {"supervisor": "supervisor", "imagery": "imagery", "weather": "environment", "soil": "agronomy"}
_PLAIN = {
    "list_fields": "Looking up the fields",
    "look_at_field": "Looking at the latest satellite pass",
    "get_scene_analysis": "Reading the satellite check results",
    "get_weather": "Checking recent weather",
    "get_soil": "Checking the soil",
    "estimate_loss": "Estimating what is at stake",
    "ask_imagery": "Asking the satellite specialist",
    "ask_weather": "Asking the weather specialist",
    "ask_soil": "Asking the soil specialist",
    "create_work_order": "Preparing a repair ticket",
    "create_scout_pin": "Preparing a scouting pin",
    "request_photo": "Preparing a photo request",
}

_CONDITION_WORDS = [  # best-effort until Diagnosis carries the scorer's condition
    ("pivot_failure", ("pivot", "nozzle", "irrigation", "tower", "sprinkler")),
    ("hail", ("hail",)),
    ("drought", ("drought", "dry", "rain")),
    ("sandy_soil_water_stress", ("sandy", "soil")),
    ("disease_pest", ("disease", "pest", "fungus", "rust", "blight")),
    ("ponding", ("ponding", "flood", "standing water")),
    ("nitrogen", ("nitrogen",)),
    ("herbicide_drift", ("drift", "herbicide")),
    ("planter_skip", ("planter", "skip")),
]
_ACTION_TYPE = {
    "repair_work_order": "work_order",
    "scouting_pin": "scout_pin",
    "request_photo": "photo_request",
    "insurance_notice": "insurance_notice",
    "none": "none",
}


def infer_condition(diag: Diagnosis) -> str:
    text = f"{diag.what} {diag.why}".lower()
    for condition, words in _CONDITION_WORDS:
        if any(w in text for w in words):
            return condition
    return "unknown"


def impact_of(diag: Diagnosis) -> C.ImpactEstimate | None:
    """The dollar range the supervisor copied from estimate_loss / estimate_impact, when it gave the acres."""
    if not diag.affected_acres:
        return None
    return C.ImpactEstimate(affected_acres=diag.affected_acres, low_usd=diag.dollars_low, high_usd=diag.dollars_high,
                            basis=f"{diag.affected_acres:g} affected acres, from the loss estimate tool")


class Recorder:
    """Writes an investigation's state to fw-investigations and its steps to the `fieldwatch` bus.

    Every AWS call is best-effort (logged, never raised): a dashboard hiccup must not kill an investigation.
    Clients are created lazily so importing this module needs no credentials.
    """

    def __init__(self, investigation_id: str, farm_id: str = "", field: str = "", *, events=None, table=None):
        self.investigation_id = investigation_id
        self.farm_id = farm_id
        self.field = field
        self.bus = config.EVENT_BUS
        self._events = events
        self._table = table
        self._seq = 0
        if not (farm_id and field):
            item = self.load()
            self.farm_id = farm_id or item.get("farm_id", "")
            self.field = field or item.get("field_id", "")

    # -- clients
    @property
    def events(self):
        if self._events is None:
            import boto3

            self._events = boto3.client("events", region_name=config.REGION)
        return self._events

    @property
    def table(self):
        if self._table is None:
            import boto3

            self._table = boto3.resource("dynamodb", region_name=config.REGION).Table(f"{config.TABLE_PREFIX}investigations")
        return self._table

    # -- the investigation item
    def load(self) -> dict:
        try:
            return self.table.get_item(Key={"investigation_id": self.investigation_id}).get("Item") or {}
        except Exception:
            log.exception("could not read investigation %s", self.investigation_id)
            return {}

    def set_status(self, status: str, *, remove: tuple[str, ...] = (), **attrs) -> None:
        attrs = {"status": status, "updated_at": _now(), **{k: v for k, v in attrs.items() if v is not None}}
        names = {f"#{k}": k for k in attrs}
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in attrs)
        if remove:
            names.update({f"#r_{k}": k for k in remove})
            expr += " REMOVE " + ", ".join(f"#r_{k}" for k in remove)
        try:
            self.table.update_item(
                Key={"investigation_id": self.investigation_id},
                UpdateExpression=expr,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={f":{k}": v for k, v in attrs.items()},
            )
        except Exception:
            log.exception("could not update investigation %s", self.investigation_id)

    def _next_seq(self) -> int:
        """Atomic counter on the item, so seq keeps rising across resumes and microVM restarts."""
        try:
            out = self.table.update_item(
                Key={"investigation_id": self.investigation_id},
                UpdateExpression="ADD progress_seq :one",
                ExpressionAttributeValues={":one": 1},
                ReturnValues="UPDATED_NEW",
            )
            self._seq = int(out["Attributes"]["progress_seq"])
        except Exception:
            self._seq += 1
        return self._seq

    # -- bus events
    def _put(self, model: C.EventBase) -> None:
        try:
            interfaces.publish_event(model, source=SOURCE, client=self.events, bus=self.bus)
        except Exception:
            log.exception("could not emit %s", type(model).__name__)

    def progress(self, agent: str, step: str, text: str, *, tool: str | None = None, latency_ms: int | None = None) -> None:
        self._put(C.InvestigationProgress(
            occurred_at=_now(), investigation_id=self.investigation_id, seq=self._next_seq(),
            agent=agent, step=step, text=text, tool=tool, latency_ms=latency_ms,
        ))

    def on_hook_event(self, e: dict) -> None:
        """ToolTimeline emit target: tool_start / tool_end -> InvestigationProgress."""
        tool, agent = e.get("tool", ""), _AGENT.get(e.get("agent", "supervisor"), "supervisor")
        label = _PLAIN.get(tool, f"Using {tool.replace('_', ' ')}")
        if e["kind"] == "tool_start":
            self.progress(agent, "tool_call", label, tool=tool)
        elif e["kind"] == "tool_end":
            ok = e.get("status") != "error"
            self.progress(agent, "tool_call", f"Done: {label.lower()}" if ok else f"That did not work: {label.lower()}",
                          tool=tool, latency_ms=e.get("ms"))

    def concluded(self, diag: Diagnosis, message_id: str | None = None) -> None:
        """condition, hypotheses and affected acres come from the Diagnosis (the scorer's output copied through);
        the condition falls back to a keyword guess and impact is omitted when the agent didn't give acres."""
        self._put(C.InvestigationConcluded(
            occurred_at=_now(), investigation_id=self.investigation_id,
            field=C.FieldRef(farm_id=self.farm_id, field_id=self.field),
            condition=diag.condition or infer_condition(diag),
            summary=f"{diag.what.rstrip('.')}. {diag.why.rstrip('.')}.",
            hypotheses=list(diag.hypotheses),
            impact=impact_of(diag),
            proposed_action=C.ProposedAction(
                action_type=_ACTION_TYPE.get(diag.recommended_action, "none"), summary=diag.action_detail,
            ),
            message_id=message_id,
        ))


# ---------------------------------------------------------------- the Runtime's unit of work


def run_request(req: C.StartInvestigation | C.ResumeInvestigation, recorder: Recorder | None = None) -> C.AgentStep:
    """Run one start or resume to its next pause or its end. Never raises: failures become AgentStep(failed)."""
    inv = req.investigation_id
    rec = recorder or Recorder(inv, getattr(req, "farm_id", ""), getattr(req, "field", ""))
    resuming = isinstance(req, C.ResumeInvestigation)
    rec.set_status("running", remove=("pending_interrupt_id",))
    rec.progress("supervisor", "plan", "Picking the investigation back up" if resuming else "Starting to look into this field")
    try:
        from . import farmdata

        with farmdata.use_farm(rec.farm_id or None):  # tools read this farm (FIELDWATCH_DATA_BACKEND=dynamo)
            if resuming:
                step = reply(inv, req.interrupt_id, req.text, emit=rec.on_hook_event)
            else:
                step = start(inv, req.field, emit=rec.on_hook_event)
    except Exception as exc:
        log.exception("investigation %s failed", inv)
        # a failed resume keeps its interrupt, so the starter can try the same answer again
        extra = {"pending_interrupt_id": req.interrupt_id} if resuming else {}
        rec.set_status("failed", error=f"{type(exc).__name__}: {exc}"[:300], **extra)
        rec.progress("supervisor", "error", "Something went wrong and the investigation stopped")
        return C.AgentStep(
            status="failed", text="", investigation_id=inv, backend="runtime",
            error=C.ErrorDetail(code="AGENT_ERROR", message=str(exc)[:300], retryable=False),
        )

    out = to_agent_step(step, inv)
    sent = deliver(rec, step)  # the question or the final message goes out through the notify lane
    message = {"message_id": sent.message_id, "message_sent_at": _now()} if sent else {}
    if step.status == "waiting_for_farmer":
        rec.set_status("awaiting_reply", pending_interrupt_id=step.interrupt_id, last_text=step.text, **message)
        rec.progress("supervisor", "waiting", "Waiting for the farmer's answer")
    else:
        diag = step.diagnosis
        rec.set_status(
            "concluded", last_text=step.text,
            diagnosis_json=json.dumps(out.diagnosis) if out.diagnosis else None,
            condition=(diag.condition or infer_condition(diag)) if diag else None, **message,
        )
        if diag:
            rec.concluded(diag, message_id=sent.message_id if sent else None)
        rec.progress("supervisor", "done", "Investigation finished")
    return out


def deliver(rec: Recorder, step: Step) -> C.SendMessageResult | None:
    """Send the step's text to the farmer with tools.deliver_message (the send_farmer_message tool's path).
    Idempotent per question / conclusion. Best-effort: a failed text is logged, never fails the investigation."""
    if not step.text:
        return None
    from . import farmdata
    from . import tools as T

    kind = f"ask#{step.interrupt_id}" if step.status == "waiting_for_farmer" else "concluded"
    try:
        with farmdata.use_farm(rec.farm_id or None):
            return T.deliver_message(person_id=T._farmer_id(), body=step.text, investigation_id=rec.investigation_id,
                                     idempotency_key=f"{rec.investigation_id}#{kind}", farm_id=rec.farm_id or None)
    except Exception:
        log.exception("could not send the message for %s", rec.investigation_id)
        return None
