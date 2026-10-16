"""Read tools (evidence) and write tools (actions).

Farm and field facts come from `fieldwatch.farmdata`: the fixture file locally, DynamoDB (fw-farms, fw-fields,
fw-field-context, fw-scene-analyses) with FIELDWATCH_DATA_BACKEND=dynamo.

Write tools pause the agent with a Strands interrupt until a human says YES. `send_farmer_message` is the one
write tool without an approval: it texts through the notify lane (interfaces.notify()).
"""
import hashlib
import json
import re
from datetime import date, timedelta

import requests
from strands import tool
from strands.types.tools import ToolContext, ToolResult

from . import actions, approvals, farmdata, interfaces, runfacts, schemas
from . import contracts as C
from .contracts import Approval

CROP_DEFAULTS = {  # expected yield (bu/ac) and price ($/bu) when the farmer gave none
    "corn": (205, 4.20),
    "soybeans": (62, 10.50),
    "wheat": (55, 5.80),
    "milo": (110, 4.00),
    "alfalfa": (5, 180.0),
}


def _farm(field_id: str | None = None) -> dict:
    """The current farm, read at call time (fixture file locally, DynamoDB in AWS; see farmdata)."""
    return farmdata.farm(field_id)


def _centroid(f: dict) -> tuple[float, float]:
    """(lat, lon) from the polygon or bbox."""
    if f.get("polygon"):
        lons = [p[0] for p in f["polygon"]]
        lats = [p[1] for p in f["polygon"]]
        return sum(lats) / len(lats), sum(lons) / len(lons)
    w, s_, e, n = f["bbox"]
    return (s_ + n) / 2, (w + e) / 2


def _field(field_id: str) -> dict:
    fields = _farm(field_id)["fields"]
    for f in fields:
        if f["id"] == field_id or f["name"].lower() == field_id.lower():
            return f
    raise ValueError(f"unknown field {field_id!r}; known: {[f['id'] for f in fields]}")


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
# The question becomes a pending record in the approvals store (fieldwatch.approvals) and its approval_id rides in
# the interrupt reason. On YES the tool calls fieldwatch.actions with that approval_id, so the same checks apply
# here and behind the Gateway. In the cloud the reply handler records the decision before the agent resumes;
# locally the farmer's reply to the interrupt is the decision and the tool records it.


def _investigation_id(tool_context: ToolContext) -> str:
    sm = getattr(tool_context.agent, "_session_manager", None)
    sid = str(getattr(sm, "session_id", "") or "")
    return sid.removeprefix("fieldwatch-") or "local"


def _farmer_id() -> str:
    farmer = _farm().get("farmer", {})
    return str(farmer.get("id") or farmer.get("name") or "farmer")


def _approve(tool_context: ToolContext, action_type: str, summary: str) -> Approval:
    """Pause until the farmer answers; returns the approval record (status approved, declined or expired).

    The approval id is derived from (investigation, type, summary), so the tool re-running when its interrupt
    resumes finds the same record instead of asking again."""
    inv = _investigation_id(tool_context)
    farmer = _farmer_id()
    aid = approvals.approval_id_for(inv, action_type, summary)
    a = approvals.create_pending(inv, action_type, summary, farmer, approval_id=aid, farm_id=_farm().get("farm_id"))
    if a.status != "pending":
        return a  # already answered (by the reply handler) or past saying
    answer = tool_context.interrupt(
        name=f"approve_{action_type}",
        reason={"action": action_type, "summary": summary, "approval_id": aid, "investigation_id": inv,
                "sent_to_person_id": farmer},
    )
    a = approvals.get(aid) or a
    if a.status == "pending":  # local harness: the reply to the interrupt is the decision
        decision = "approved" if str(answer).strip().upper() == "YES" else "declined"
        try:
            a = approvals.record_decision(aid, decision, farmer, "web")
        except approvals.FieldwatchError:
            a = approvals.get(aid) or a
    return a


def _not_approved(a, what: str) -> str:
    if a.status == "declined":
        return f"Farmer declined. Do not send the {what}."
    return f"Not sent: the approval is {a.status}. Ask again if it still matters."


def _lon_lat(f: dict) -> tuple[float, float]:
    lat, lon = _centroid(f)
    return round(lon, 6), round(lat, 6)


def _run_facts() -> runfacts.RunFacts | None:
    return runfacts.current()


def _pin_of(f: dict) -> tuple[float, float]:
    """Where the pin goes: the flagged region's centroid when the run knows it, else the middle of the field."""
    facts = _run_facts()
    if facts is not None and facts.centroid:
        return round(facts.centroid[0], 6), round(facts.centroid[1], 6)
    return _lon_lat(f)


def _plain_text(text: str) -> str:
    """The model's words for a pin or work order with the facts only code may state removed: ids, percents, and any
    acres or dollars other than the impact tool's."""
    facts = _run_facts()
    out = schemas.scrub_internal_ids(str(text))
    out = schemas.scrub_percentages(out, set())
    return schemas.scrub_figures(out, acres=facts.acres if facts else None, low_usd=facts.low_usd if facts else None,
                                 high_usd=facts.high_usd if facts else None)


@tool(context=True)
def create_work_order(field_id: str, assignee: str, what_to_check: str, tool_context: ToolContext) -> str:
    """Send a repair work order with a map pin to the person who does repairs. Requires farmer approval."""
    f = _field(field_id)
    facts = _run_facts()
    where = f" ({facts.where})" if facts and facts.where else ""
    check = _plain_text(what_to_check)
    summary = f"Send {assignee} a repair ticket for the {f['name']}{where}: {check}"
    a = _approve(tool_context, "work_order", summary)
    if a.status != "approved":
        return _not_approved(a, "work order")
    try:
        act = actions.create_work_order(
            approval_id=a.approval_id, investigation_id=a.investigation_id, assignee_person_id=assignee,
            field_id=f["id"], field_name=f["name"], instructions=check, pin=_pin_of(f),
            farm_id=_farm().get("farm_id"))
    except approvals.FieldwatchError as e:
        return f"Not sent: {e.message}"
    order = {"id": act.action_id, "to": assignee, "field": f["name"], "check": check,
             "text_delivered": actions.delivery(act.action_id)["delivered"]}
    return f"Work order sent: {json.dumps(order)}"


@tool(context=True)
def create_scout_pin(field_id: str, where: str, what_to_look_for: str, tool_context: ToolContext) -> str:
    """Drop a scouting pin with coordinates and what to look for. Requires farmer approval. The location is the
    flagged problem area's, set by FieldWatch."""
    f = _field(field_id)
    facts = _run_facts()
    where = facts.where if facts and facts.where else _plain_text(where)  # location: code's, not the model's
    look = re.sub(r"^\s*look for:?\s*", "", _plain_text(what_to_look_for), flags=re.I)  # the agent often repeats "Look for:"
    summary = f"Scouting pin on the {f['name']} ({where}): look for {look}"
    a = _approve(tool_context, "scout_pin", summary)
    if a.status != "approved":
        return _not_approved(a, "scouting pin")
    try:
        act = actions.create_scout_pin(
            approval_id=a.approval_id, investigation_id=a.investigation_id, person_id=_farmer_id(),
            pin=_pin_of(f), what_to_check=f"{where}: {look}", field_name=f["name"],
            farm_id=_farm().get("farm_id"))
    except approvals.FieldwatchError as e:
        return f"Not sent: {e.message}"
    return f"Scouting pin sent for {f['name']}: {where}. Look for: {look}."  # no internal ids in text the farmer may see


@tool(context=True)
def request_photo(field_id: str, where: str, tool_context: ToolContext) -> str:
    """Ask the farmer to snap a leaf photo near a spot when the satellite can't tell what's wrong."""
    f = _field(field_id)
    summary = f"Ask for a leaf photo from the {f['name']} near {where}"
    a = _approve(tool_context, "photo_request", summary)
    if a.status != "approved":
        return _not_approved(a, "photo request")
    return f"Photo request sent with upload link for {f['name']} ({where})."


# ---------- messaging: no approval needed (quiet hours and rate limits are the notify lane's job) ----------


def deliver_message(*, person_id: str, body: str, investigation_id: str | None = None, link: dict | None = None,
                    urgency: str = "actionable", idempotency_key: str | None = None,
                    farm_id: str | None = None) -> C.SendMessageResult:
    """Text a farm person through the notify lane (interfaces.NotifyAPI.send). The server-set fields of
    SendMessageRequest are filled here: farm_id from the current farm, idempotency_key from the content.
    Without a configured notify lane (local runs) the message is mirror-only: nothing leaves the machine."""
    farm_id = farm_id or farmdata.current_farm_id() or _farm().get("farm_id") or ""
    body = " ".join(body.split())[:480]
    key = idempotency_key or f"{investigation_id or 'adhoc'}#msg#{hashlib.sha1(body.encode()).hexdigest()[:12]}"
    req = C.SendMessageRequest(farm_id=farm_id, person_id=person_id, investigation_id=investigation_id, body=body,
                               link=link, urgency=urgency, idempotency_key=key)
    service = interfaces.notify()
    if service is None:
        return C.SendMessageResult(message_id="local-" + hashlib.sha1(key.encode()).hexdigest()[:16],
                                   status="mirror_only", provider="local")
    return service.send(req)


@tool
def send_farmer_message(person_id: str, body: str, investigation_id: str = "", urgency: str = "actionable") -> dict:
    """Text a person on the farm (no approval needed). Plain words, under ~300 characters, no emoji, the field
    name the farmer uses. person_id from the farm's people (the farmer is the default). urgency: low,
    actionable or urgent (urgent skips quiet hours)."""
    try:
        res = deliver_message(person_id=person_id or _farmer_id(), body=schemas.farmer_text(body),
                              investigation_id=investigation_id or None,
                              urgency=urgency if urgency in ("low", "actionable", "urgent") else "actionable")
    except interfaces.FieldwatchError as e:
        return {"status": "error", "code": e.code, "message": e.message}
    return res.model_dump(mode="json")


READ_TOOLS = [list_fields, look_at_field, get_scene_analysis, get_weather, get_soil, estimate_loss]
WRITE_TOOLS = [create_work_order, create_scout_pin]  # request_photo: off until photo intake exists
MESSAGE_TOOLS = [send_farmer_message]
