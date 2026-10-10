"""Hooks: the evidence trail and the dashboard feed fall out of Strands lifecycle events."""
import json
import time
from collections.abc import Callable

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

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
