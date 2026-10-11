"""Hooks: the evidence trail and the dashboard feed fall out of Strands lifecycle events."""
import json
import logging
import os
import threading
import time
from collections.abc import Callable

from strands.hooks import (
    AfterModelCallEvent, AfterToolCallEvent, BeforeModelCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry,
)

log = logging.getLogger(__name__)

DEFAULT_TOKEN_BUDGET = 250_000

Emit = Callable[[dict], None]


def print_event(event: dict) -> None:
    kind = event["kind"]
    if kind == "tool_start":
        print(f"  [{event['agent']}] -> {event['tool']}({json.dumps(event['input'])})")
    elif kind == "tool_end":
        print(f"  [{event['agent']}] <- {event['tool']} ({event['ms']} ms)")


class ToolTimeline(HookProvider):
    """Emits one event when a tool starts and one when it ends.

    Point `emit` at a websocket or AppSync publisher and the projector dashboard
    shows evidence cards arriving in real time. Also blocks any write tool that
    the supervisor is not allowed to call (defense in depth beside the interrupt).
    """

    def __init__(self, emit: Emit = print_event, blocked_tools: set[str] | None = None):
        self.emit = emit
        self.blocked = blocked_tools or set()
        self._started: dict[str, float] = {}
        self.events: list[dict] = []

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)

    def _record(self, event: dict) -> None:
        self.events.append(event)
        self.emit(event)

    def before(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name in self.blocked:
            event.cancel_tool = f"{name} is not permitted for this agent"
            return
        self._started[event.tool_use["toolUseId"]] = time.perf_counter()
        self._record(
            {
                "kind": "tool_start",
                "agent": getattr(event.agent, "name", "agent"),
                "tool": name,
                "input": event.tool_use.get("input", {}),
            }
        )

    def after(self, event: AfterToolCallEvent) -> None:
        tid = event.tool_use["toolUseId"]
        ms = int((time.perf_counter() - self._started.pop(tid, time.perf_counter())) * 1000)
        content = event.result.get("content", [])
        text = next((c["text"] for c in content if "text" in c), "")
        self._record(
            {
                "kind": "tool_end",
                "agent": getattr(event.agent, "name", "agent"),
                "tool": event.tool_use["name"],
                "status": event.result.get("status"),
                "ms": ms,
                "output": text[:500],
            }
        )


class TokenBudgetExceeded(Exception):
    """An investigation used up its token budget. Raised before the next model call, so nothing more is spent."""


def token_budget_from_env() -> int:
    """FIELDWATCH_TOKEN_BUDGET (tokens per investigation); a missing, non-numeric or non-positive value means the default."""
    raw = os.environ.get("FIELDWATCH_TOKEN_BUDGET", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_TOKEN_BUDGET
    except ValueError:
        value = 0
    if value <= 0:
        log.warning("FIELDWATCH_TOKEN_BUDGET=%r is not a positive integer; using %d", raw, DEFAULT_TOKEN_BUDGET)
        return DEFAULT_TOKEN_BUDGET
    return value


class TokenBudget(HookProvider):
    """Caps the input + output tokens one investigation may spend, a cost guardrail.

    Build ONE per investigation and pass it as a hook to the supervisor and every specialist: it sums their usage.
    Usage is read from each model response (AfterModelCallEvent, message metadata) and checked before the next
    model call (BeforeModelCallEvent), where raising TokenBudgetExceeded ends the run with a clear reason. The
    event's `cancel` is not used on purpose: it makes the model call return a canned "end_turn" message that looks
    like an answer. A specialist that hits the limit is reported to the supervisor as an error note
    (agents.run_specialist), and the supervisor's own next model call raises the same exception, so the whole
    investigation stops. One call can overshoot the limit by its own size; the check is between calls.
    """

    def __init__(self, limit: int | None = None):
        self.limit = limit if limit and limit > 0 else token_budget_from_env()
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()  # specialists may run on tool threads

    @property
    def used(self) -> int:
        return self.input_tokens + self.output_tokens

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before)
        registry.add_callback(AfterModelCallEvent, self.after)

    def before(self, event: BeforeModelCallEvent) -> None:
        with self._lock:
            used = self.used
        if used >= self.limit:
            who = getattr(event.agent, "name", "agent")
            msg = f"token budget exceeded: {used} of {self.limit} tokens used before a call by {who}"
            log.error(msg)
            raise TokenBudgetExceeded(msg)

    def after(self, event: AfterModelCallEvent) -> None:
        if event.stop_response is None:  # the call failed: no usage to count
            return
        usage = (event.stop_response.message.get("metadata") or {}).get("usage") or {}
        with self._lock:
            self.input_tokens += int(usage.get("inputTokens") or 0)
            self.output_tokens += int(usage.get("outputTokens") or 0)
