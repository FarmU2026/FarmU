"""Read tools (evidence) and write tools (actions).

Read tools return facts from fixtures today; swap the bodies for real APIs
(Sentinel-2, Open-Meteo, SSURGO) without touching the agents.

Write tools pause the agent with a Strands interrupt until a human says YES.
"""
import json
from datetime import date

from strands import tool
from strands.types.tools import ToolContext, ToolResult

from .config import FIXTURES


def _farm() -> dict:
    return json.loads((FIXTURES / "demo_farm.json").read_text())


def _field(field_id: str) -> dict:
    for f in _farm()["fields"]:
        if f["id"] == field_id or f["name"].lower() == field_id.lower():
            return f
    raise ValueError(f"unknown field {field_id!r}; known: {[f['id'] for f in _farm()['fields']]}")


# ---------- read tools: one per specialist ----------


@tool
def list_fields() -> list[dict]:
    """List every field on the farm with its id, the name the farmer uses, crop and acres."""
    return [{k: f[k] for k in ("id", "name", "crop", "acres", "irrigation")} for f in _farm()["fields"]]


@tool
def get_scene_analysis(field_id: str) -> dict:
    """Latest satellite pass for a field: the stress pattern seen, how far crop health dropped, and the date."""
    f = _field(field_id)
    return {"field": f["name"], "irrigation": f["irrigation"], **f["latest_scan"]}


@tool
def look_at_field(field_id: str) -> ToolResult:
    """Pull the latest cloud-free satellite pass over a field and LOOK at it.

    Returns two images (true color, then a crop-health heatmap where green is vigorous and brown is
    bare or stressed) plus summary numbers. Use this to describe the shape and location of any
    problem area. Scenes are 2-5 days apart, so the date matters.
    """
    from . import imagery  # heavy imports stay off the fast path

    f = _field(field_id)
    try:
        view = imagery.look(tuple(f["bbox"]))
    except Exception as e:  # network, no scene, etc.
        return {"status": "error", "content": [{"text": f"could not fetch imagery for {f['name']}: {e}"}]}
    return {
        "status": "success",
        "content": [
            {"text": f"Latest clear pass over the {f['name']} ({f['crop']}, {f['irrigation']}). "
                     f"Image 1 is true color, image 2 is crop health. Numbers: {json.dumps(view.stats)}"},
            {"image": {"format": "png", "source": {"bytes": view.truecolor_png}}},
            {"image": {"format": "png", "source": {"bytes": view.health_png}}},
        ],
    }


@tool
def get_weather(field_id: str) -> dict:
    """Last 30 days of weather at a field: rain vs normal, hail reports, max temperature."""
    f = _field(field_id)
    return {"field": f["name"], **f["weather_30d"]}


@tool
def get_soil(field_id: str) -> dict:
    """Soil survey for a field: soil type, drainage, and any notes on variability within the field."""
    f = _field(field_id)
    return {"field": f["name"], **f["soil"]}


@tool
def estimate_loss(field_id: str, affected_acres: float, yield_loss_fraction: float) -> dict:
    """Dollar impact if nothing is done. yield_loss_fraction is 0-1 for the affected acres only."""
    f = _field(field_id)
    per_acre = f["expected_yield_bu_ac"] * f["price_per_bu"]
    mid = affected_acres * yield_loss_fraction * per_acre
    return {"field": f["name"], "low": int(mid * 0.7), "high": int(mid * 1.3), "per_acre_value": int(per_acre)}


# ---------- write tools: every one of these needs a YES ----------


def _approve(tool_context: ToolContext, action: str, summary: str) -> str:
    """Pause until a human replies. Returns 'YES' or 'NO'."""
    answer = tool_context.interrupt(
        name=f"approve_{action}",
        reason={"action": action, "summary": summary},
    )
    return str(answer).strip().upper()


@tool(context=True)
def create_work_order(field_id: str, assignee: str, what_to_check: str, tool_context: ToolContext) -> str:
    """Send a repair work order with a map pin to the person who does repairs. Requires farmer approval."""
    f = _field(field_id)
    summary = f"Send {assignee} a repair ticket for the {f['name']}: {what_to_check}"
    if _approve(tool_context, "work_order", summary) != "YES":
        return "Farmer declined. Do not send the work order."
    order = {"id": f"wo-{date.today():%Y%m%d}-{f['id']}", "to": assignee, "field": f["name"], "check": what_to_check}
    return f"Work order sent: {json.dumps(order)}"


@tool(context=True)
def create_scout_pin(field_id: str, where: str, what_to_look_for: str, tool_context: ToolContext) -> str:
    """Drop a scouting pin with coordinates and what to look for. Requires farmer approval."""
    f = _field(field_id)
    summary = f"Scouting pin on the {f['name']} ({where}): look for {what_to_look_for}"
    if _approve(tool_context, "scout_pin", summary) != "YES":
        return "Farmer declined."
    return f"Scouting pin sent for {f['name']}: {where}. Look for: {what_to_look_for}"


@tool(context=True)
def request_photo(field_id: str, where: str, tool_context: ToolContext) -> str:
    """Ask the farmer to snap a leaf photo near a spot when the satellite can't tell what's wrong."""
    f = _field(field_id)
    summary = f"Ask for a leaf photo from the {f['name']} near {where}"
    if _approve(tool_context, "photo", summary) != "YES":
        return "Farmer declined."
    return f"Photo request sent with upload link for {f['name']} ({where})."


READ_TOOLS = [list_fields, look_at_field, get_scene_analysis, get_weather, get_soil, estimate_loss]
WRITE_TOOLS = [create_work_order, create_scout_pin, request_photo]
