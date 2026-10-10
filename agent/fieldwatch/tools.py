"""Read tools (evidence) and write tools (actions).

Read tools return facts from fixtures today; swap the bodies for real APIs
(Sentinel-2, Open-Meteo, SSURGO) without touching the agents.

Write tools pause the agent with a Strands interrupt until a human says YES.
"""
import json
from datetime import date, timedelta

import requests
from strands import tool
from strands.types.tools import ToolContext, ToolResult

from . import config

CROP_DEFAULTS = {  # expected yield (bu/ac) and price ($/bu) when the farmer gave none
    "corn": (205, 4.20),
    "soybeans": (62, 10.50),
    "wheat": (55, 5.80),
    "milo": (110, 4.00),
    "alfalfa": (5, 180.0),
}


def _farm() -> dict:
    """Read the farm file at call time, so the web UI can point at a different farm per request."""
    return json.loads(config.FARM_FILE.read_text())


def _centroid(f: dict) -> tuple[float, float]:
    """(lat, lon) from the polygon or bbox."""
    if f.get("polygon"):
        lons = [p[0] for p in f["polygon"]]
        lats = [p[1] for p in f["polygon"]]
        return sum(lats) / len(lats), sum(lons) / len(lons)
    w, s_, e, n = f["bbox"]
    return (s_ + n) / 2, (w + e) / 2


def _field(field_id: str) -> dict:
    for f in _farm()["fields"]:
        if f["id"] == field_id or f["name"].lower() == field_id.lower():
            return f
    raise ValueError(f"unknown field {field_id!r}; known: {[f['id'] for f in _farm()['fields']]}")


# ---------- read tools: one per specialist ----------


@tool
def list_fields() -> list[dict]:
    """List every field on the farm with its id, the name the farmer uses, crop and acres."""
    keys = ("id", "name", "crop", "acres", "irrigation")
    return [{k: f.get(k, "unknown") for k in keys} for f in _farm()["fields"]]


@tool
def look_at_field(field_id: str) -> ToolResult:
    """Pull the latest cloud-free satellite pass over a field and LOOK at it.

    Returns two images (true color, then a crop-health heatmap where green is vigorous and brown is
    bare or stressed) plus summary numbers. Pixels outside the field boundary are dimmed. Use this to
    describe the shape and location of any problem area. Scenes are 2-5 days apart, so the date matters.
    """
    from . import imagery  # heavy imports stay off the fast path

    f = _field(field_id)
    try:
        poly = [tuple(p) for p in f["polygon"]] if f.get("polygon") else None
        bbox = tuple(f["bbox"]) if f.get("bbox") else imagery.polygon_bbox(poly)
        view = imagery.look(bbox, poly)
    except Exception as e:  # network, no scene, etc.
        return {"status": "error", "content": [{"text": f"could not fetch imagery for {f['name']}: {e}"}]}
    return {
        "status": "success",
        "content": [
            {"text": f"Latest clear pass over the {f['name']} ({f.get('crop', 'crop')}, "
                     f"{f.get('irrigation', 'irrigation unknown')}). "
                     f"Image 1 is true color, image 2 is crop health. Numbers: {json.dumps(view.stats)}"},
            {"image": {"format": "png", "source": {"bytes": view.truecolor_png}}},
            {"image": {"format": "png", "source": {"bytes": view.health_png}}},
        ],
    }


@tool
def get_scene_analysis(field_id: str) -> dict:
    """Pre-computed analysis of the latest satellite pass, if the pipeline has run for this field."""
    f = _field(field_id)
    if not f.get("latest_scan"):
        return {"field": f["name"], "note": "no pre-computed analysis on file; use look_at_field"}
    return {"field": f["name"], "irrigation": f.get("irrigation", "unknown"), **f["latest_scan"]}


@tool
def get_weather(field_id: str) -> dict:
    """Last 30 days of weather at a field from Open-Meteo: daily rain, max temperature, totals vs a normal."""
    f = _field(field_id)
    if f.get("weather_30d"):  # fixture farms carry canned weather
        return {"field": f["name"], **f["weather_30d"]}
    lat, lon = _centroid(f)
    end = date.today() - timedelta(days=2)  # archive lags a couple of days
    start = end - timedelta(days=30)
    try:
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "daily": "precipitation_sum,temperature_2m_max",
                "precipitation_unit": "inch", "temperature_unit": "fahrenheit", "timezone": "America/Chicago",
            },
            timeout=20,
        ).json()["daily"]
    except Exception as e:
        return {"field": f["name"], "error": f"weather unavailable: {e}"}
    rain = [x or 0 for x in resp["precipitation_sum"]]
    temps = [x for x in resp["temperature_2m_max"] if x is not None]
    return {
        "field": f["name"],
        "period": f"{start} to {end}",
        "rain_in": round(sum(rain), 2),
        "normal_rain_in_30d": 3.0,
        "days_with_rain": sum(1 for x in rain if x >= 0.1),
        "biggest_rain_day_in": round(max(rain), 2),
        "max_temp_f": max(temps) if temps else None,
        "days_over_95f": sum(1 for t in temps if t >= 95),
        "hail_reported": None,
        "source": "open-meteo.com archive",
    }


@tool
def get_soil(field_id: str) -> dict:
    """Soil survey for a field: soil type, drainage, and any notes on variability within the field."""
    f = _field(field_id)
    if not f.get("soil"):
        return {"field": f["name"], "note": "no soil survey on file for this field yet"}
    return {"field": f["name"], **f["soil"]}


@tool
def estimate_loss(field_id: str, affected_acres: float, yield_loss_fraction: float) -> dict:
    """Dollar impact if nothing is done. yield_loss_fraction is 0-1 for the affected acres only."""
    f = _field(field_id)
    y_def, p_def = CROP_DEFAULTS.get(f.get("crop", "").lower(), (150, 5.0))
    per_acre = f.get("expected_yield_bu_ac", y_def) * f.get("price_per_bu", p_def)
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
