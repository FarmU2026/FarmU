"""Run an investigation, pause for the farmer, resume with their reply.

Sessions persist to disk (FileSessionManager) so a reply that arrives an hour later,
or on a different process, resumes the same paused agent. Swap in S3SessionManager
or AgentCore Memory for production.
"""
from dataclasses import dataclass

from strands.session import FileSessionManager

from .agents import build_supervisor
from .config import SESSIONS
from .hooks import ToolTimeline, print_event
from .schemas import Diagnosis

LIMITS = {"turns": 20}


@dataclass
class Step:
    status: str  # "waiting_for_farmer" | "done"
    text: str  # the SMS to send
    diagnosis: Diagnosis | None = None
    interrupt_id: str | None = None
    timeline: list[dict] | None = None


def _session(investigation_id: str) -> FileSessionManager:
    SESSIONS.mkdir(exist_ok=True)
    return FileSessionManager(session_id=f"fieldwatch-{investigation_id}", storage_dir=str(SESSIONS))


def _to_step(result, timeline: ToolTimeline) -> Step:
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
    return Step(status="done", text=diag.to_text() if diag else str(result), diagnosis=diag, timeline=timeline.events)


def start(investigation_id: str, field_name: str, emit=print_event) -> Step:
    """Kick off an investigation. Usually ends paused on an approval interrupt."""
    timeline = ToolTimeline(emit)
    agent = build_supervisor(timeline, session_manager=_session(investigation_id))
    result = agent(f"Investigate the field called '{field_name}'.", limits=LIMITS)
    return _to_step(result, timeline)


def reply(investigation_id: str, interrupt_id: str, farmer_text: str, emit=print_event) -> Step:
    """The farmer texted back. Resume the paused agent with their answer and finish the diagnosis."""
    timeline = ToolTimeline(emit)
    agent = build_supervisor(timeline, session_manager=_session(investigation_id))
    result = agent(
        [{"interruptResponse": {"interruptId": interrupt_id, "response": farmer_text}}],
        limits=LIMITS,
        structured_output_model=Diagnosis,
    )
    return _to_step(result, timeline)
