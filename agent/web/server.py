"""FieldWatch web: sign up a farm (phone + field boundaries), get live satellite updates and agent texts.

Run from agent/:   python -m uvicorn web.server:app --reload --port 8000
Then open          http://localhost:8000

State is one JSON file per farm in agent/data/farms/ (gitignored). A background thread checks the
satellite catalog every CHECK_EVERY seconds; a new clear pass over any field becomes an update in
that farm's phone thread. "Ask FieldWatch" runs the Strands investigation on a field; its approval
question lands in the thread, and YES / NO resumes the paused agent.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fieldwatch import config, imagery
from fieldwatch.investigation import reply as agent_reply
from fieldwatch.investigation import start as agent_start

CHECK_EVERY = 600  # seconds between catalog checks
FARMS = config.DATA / "farms"
IMAGES = config.DATA / "images"
LOCK = threading.Lock()

app = FastAPI(title="FieldWatch")
STATIC = Path(__file__).parent / "static"


# ---------- models ----------


class FieldIn(BaseModel):
    name: str
    crop: str = "corn"
    polygon: list[list[float]] = Field(description="[[lat, lon], ...] as clicked on the map")


class FarmIn(BaseModel):
    phone: str
    farmer: str = "Farmer"
    fields: list[FieldIn]


class ReplyIn(BaseModel):
    text: str


# ---------- storage ----------


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "field"


def _acres(latlon: list[list[float]]) -> int:
    """Shoelace area on a local flat projection; plenty accurate for a field."""
    import math

    lat0 = math.radians(sum(p[0] for p in latlon) / len(latlon))
    m_per_deg_lat, m_per_deg_lon = 111_320.0, 111_320.0 * math.cos(lat0)
    xy = [((p[1] - latlon[0][1]) * m_per_deg_lon, (p[0] - latlon[0][0]) * m_per_deg_lat) for p in latlon]
    area = abs(sum(xy[i][0] * xy[(i + 1) % len(xy)][1] - xy[(i + 1) % len(xy)][0] * xy[i][1] for i in range(len(xy)))) / 2
    return round(area / 4046.86)


def _path(farm_id: str) -> Path:
    return FARMS / f"{farm_id}.json"


def load(farm_id: str) -> dict:
    p = _path(farm_id)
    if not p.exists():
        raise HTTPException(404, "no such farm")
    return json.loads(p.read_text())


def save(farm: dict) -> None:
    FARMS.mkdir(parents=True, exist_ok=True)
    _path(farm["farm_id"]).write_text(json.dumps(farm, indent=2))


def post(farm: dict, who: str, text: str, **extra) -> None:
    farm.setdefault("thread", []).append({"at": datetime.now().isoformat(timespec="seconds"), "who": who, "text": text, **extra})


# ---------- imagery ----------


def _poly(field: dict) -> list[tuple[float, float]]:
    return [tuple(p) for p in field["polygon"]]


def pull_field(farm: dict, field: dict, only_if_newer: bool = True) -> bool:
    """Fetch the latest clear scene for one field. Returns True if a new scene was stored."""
    poly = _poly(field)
    newer_than = field.get("last_scene_date") if only_if_newer else None
    view = imagery.look(imagery.polygon_bbox(poly), poly, newer_than=newer_than)
    if view is None:
        return False
    d = IMAGES / farm["farm_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{field['id']}_truecolor.png").write_bytes(view.truecolor_png)
    (d / f"{field['id']}_health.png").write_bytes(view.health_png)
    field["last_scene_date"] = view.stats["scene_date"]
    field["stats"] = view.stats
    s = view.stats
    post(
        farm,
        "fieldwatch",
        f"New satellite pass over the {field['name']} ({s['scene_date']}, {s['cloud_cover_pct']:.0f}% cloud). "
        f"Crop health {s['health_index_median']:.2f}, weakest in the {s['weakest_area']}. "
        f"Reply ASK to have me take a closer look.",
        field_id=field["id"],
    )
    return True


def pull_farm(farm_id: str, only_if_newer: bool = True) -> None:
    with LOCK:
        farm = load(farm_id)
        for field in farm["fields"]:
            try:
                pull_field(farm, field, only_if_newer)
            except Exception as e:  # keep going for the other fields
                post(farm, "fieldwatch", f"Could not fetch imagery for the {field['name']}: {e}", field_id=field["id"])
        farm["last_checked"] = datetime.now().isoformat(timespec="seconds")
        save(farm)


def watcher() -> None:
    while True:
        time.sleep(CHECK_EVERY)
        for p in FARMS.glob("*.json"):
            try:
                pull_farm(p.stem)
            except Exception:
                pass


threading.Thread(target=watcher, daemon=True).start()


# ---------- agent ----------


def _events_to_thread(farm: dict, inv_id: str):
    def emit(e: dict) -> None:
        if e["kind"] == "tool_start":
            post(farm, "agent", f"{e['agent']} is checking {e['tool'].replace('_', ' ')}", investigation_id=inv_id, kind="step")
    return emit


def investigate(farm_id: str, field_id: str) -> dict:
    with LOCK:
        farm = load(farm_id)
        field = next((f for f in farm["fields"] if f["id"] == field_id), None)
        if not field:
            raise HTTPException(404, "no such field")
        config.FARM_FILE = _path(farm_id)  # the tools read this farm
        inv_id = uuid.uuid4().hex[:8]
        post(farm, "farmer", "ASK", field_id=field_id)
        step = agent_start(inv_id, field["name"], emit=_events_to_thread(farm, inv_id))
        farm["pending"] = {"investigation_id": inv_id, "interrupt_id": step.interrupt_id} if step.status == "waiting_for_farmer" else None
        post(farm, "fieldwatch", step.text, investigation_id=inv_id, field_id=field_id)
        if step.diagnosis:
            farm.setdefault("diagnoses", []).append(step.diagnosis.model_dump())
        save(farm)
        return farm


def farmer_reply(farm_id: str, text: str) -> dict:
    with LOCK:
        farm = load(farm_id)
        post(farm, "farmer", text)
        pending = farm.get("pending")
        if not pending:
            post(farm, "fieldwatch", "Nothing is waiting on you right now. Reply ASK to check a field.")
            save(farm)
            return farm
        config.FARM_FILE = _path(farm_id)
        step = agent_reply(pending["investigation_id"], pending["interrupt_id"], text, emit=_events_to_thread(farm, pending["investigation_id"]))
        farm["pending"] = {"investigation_id": pending["investigation_id"], "interrupt_id": step.interrupt_id} if step.status == "waiting_for_farmer" else None
        post(farm, "fieldwatch", step.text, investigation_id=pending["investigation_id"])
        if step.diagnosis:
            farm.setdefault("diagnoses", []).append(step.diagnosis.model_dump())
        save(farm)
        return farm


# ---------- routes ----------


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/farms")
def create_farm(body: FarmIn):
    farm_id = re.sub(r"\D", "", body.phone)[-10:] or uuid.uuid4().hex[:8]
    fields = []
    for f in body.fields:
        if len(f.polygon) < 3:
            raise HTTPException(400, f"field {f.name!r} needs at least 3 points")
        fields.append({
            "id": _slug(f.name),
            "name": f.name,
            "crop": f.crop,
            "acres": _acres(f.polygon),
            "irrigation": "unknown",
            "polygon": [[lon, lat] for lat, lon in f.polygon],  # stored as lon/lat for the imagery code
        })
    farm = {
        "farm_id": farm_id,
        "phone": body.phone,
        "farmer": {"name": body.farmer, "channel": "text"},
        "fields": fields,
        "thread": [],
    }
    post(farm, "fieldwatch", f"Hi {body.farmer}, FieldWatch here. I am watching {len(fields)} field(s) and will text you after every clear satellite pass. Reply STOP anytime.")
    save(farm)
    pull_farm(farm_id, only_if_newer=False)
    return load(farm_id)


@app.get("/api/farms/{farm_id}")
def get_farm(farm_id: str):
    return load(farm_id)


@app.post("/api/farms/{farm_id}/check")
def check_now(farm_id: str):
    pull_farm(farm_id)
    return load(farm_id)


@app.get("/api/farms/{farm_id}/fields/{field_id}/{kind}.png")
def image(farm_id: str, field_id: str, kind: str):
    p = IMAGES / farm_id / f"{field_id}_{kind}.png"
    if not p.exists():
        raise HTTPException(404, "no image yet")
    return Response(p.read_bytes(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/farms/{farm_id}/fields/{field_id}/ask")
def ask(farm_id: str, field_id: str):
    return investigate(farm_id, field_id)


@app.post("/api/farms/{farm_id}/reply")
def reply(farm_id: str, body: ReplyIn):
    text = body.text.strip()
    if text.upper() == "ASK":
        farm = load(farm_id)
        return investigate(farm_id, farm["fields"][0]["id"])
    return farmer_reply(farm_id, text)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
