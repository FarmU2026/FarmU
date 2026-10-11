"""Reads and writes shared by the API route groups (fw-farms, fw-fields, fw-scene-analyses, ...).

Item shapes (the models live in fieldwatch.interfaces; other lanes read them):

fw-farms  FARM               interfaces.FarmItem: farm_id, item="FARM", name, farmer_name, phone, owner_person_id,
                             timezone (IANA; notify's quiet hours), quiet_hours {start, end}, muted, created_at, ...
fw-farms  PERSON#<person_id> interfaces.PersonItem: person_id, name, phone_e164 (GSI by-phone), role, channel_pref,
                             opt_in_at, opted_out
fw-fields                    farm_id, field_id, name, crop, crop_current, acres, irrigated, irrigation,
                             polygon [[lon, lat], ...] (closed ring not required), geometry_key?, status
fw-farms  THREAD#<at>#<id>   interfaces.ThreadItem, written by the projector (WS13). The API reads them in sort-key
                             order and only writes one itself (the welcome) when no send Lambda is configured.

Farm ids are `farm_` + a random, time-sortable token (interfaces.new_farm_id): never the phone number, which
would leak into URLs and AppSync channel names. The owner's phone finds the farm again via GSI by-phone.
Older farms keep their ids (any farm_id string is accepted).
"""
from __future__ import annotations

import math
import re
import uuid
from typing import Any

from boto3.dynamodb.conditions import Key

from fieldwatch import interfaces

from . import _aws
from ._http import ApiError

THREAD_PREFIX = "THREAD#"
THREAD_LIMIT = 200


# ---------- ids and geometry ----------


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "field"


def new_farm_id() -> str:
    return interfaces.new_farm_id()


def owner_person_id(farm_id: str) -> str:
    return "per_" + farm_id.removeprefix("farm_")


def e164(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:20]}"


def acres_latlon(latlon: list[list[float]]) -> int:
    """Shoelace area on a local flat projection (same as agent/web/server.py)."""
    lat0 = math.radians(sum(p[0] for p in latlon) / len(latlon))
    m_lat, m_lon = 111_320.0, 111_320.0 * math.cos(lat0)
    xy = [((p[1] - latlon[0][1]) * m_lon, (p[0] - latlon[0][0]) * m_lat) for p in latlon]
    n = len(xy)
    area = abs(sum(xy[i][0] * xy[(i + 1) % n][1] - xy[(i + 1) % n][0] * xy[i][1] for i in range(n))) / 2
    return round(area / 4046.86)


def centroid(polygon_lonlat: list[list[float]]) -> tuple[float, float]:
    pts = polygon_lonlat[:-1] if len(polygon_lonlat) > 1 and polygon_lonlat[0] == polygon_lonlat[-1] else polygon_lonlat
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def geojson_polygon(polygon_lonlat: list[list[float]]) -> dict:
    ring = [list(p) for p in polygon_lonlat]
    if ring and ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return {"type": "Polygon", "coordinates": [ring]}


def maps_url(lon: float, lat: float) -> str:
    return f"https://maps.google.com/?q={lat:.5f},{lon:.5f}"


def direction(from_lonlat: tuple[float, float], to_lonlat: tuple[float, float]) -> str:
    """'north-east part' etc. for where a weak area sits relative to the field center."""
    dx, dy = to_lonlat[0] - from_lonlat[0], to_lonlat[1] - from_lonlat[1]
    if abs(dx) < 1e-5 and abs(dy) < 1e-5:
        return "middle"
    ns = "north" if dy > 0 else "south"
    ew = "east" if dx > 0 else "west"
    if abs(dy) > 2 * abs(dx):
        return f"{ns} part"
    if abs(dx) > 2 * abs(dy):
        return f"{ew} part"
    return f"{ns}-{ew} part"


# ---------- farms, people, fields ----------


def get_farm_item(farm_id: str) -> dict:
    farm = _aws.get_item("farms", farm_id=farm_id, item="FARM")
    if not farm:
        raise ApiError("NOT_FOUND", "No such farm")
    return farm


def list_fields(farm_id: str) -> list[dict]:
    return _aws.query_pk("fields", "farm_id", farm_id)


def get_field(farm_id: str, field_id: str) -> dict:
    f = _aws.get_item("fields", farm_id=farm_id, field_id=field_id)
    if not f:
        raise ApiError("NOT_FOUND", "No such field")
    return f


def find_field(field_id: str) -> dict | None:
    """By field_id alone (GSI by-field)."""
    items = _aws.query_all(_aws.table("fields"), IndexName="by-field",
                           KeyConditionExpression=Key("field_id").eq(field_id))
    if not items:
        return None
    # GSI items are projected ALL by default; refetch to be safe about projections.
    it = items[0]
    return _aws.get_item("fields", farm_id=it["farm_id"], field_id=field_id) or it


def list_people(farm_id: str) -> list[dict]:
    return _aws.query_all(_aws.table("farms"),
                          KeyConditionExpression=Key("farm_id").eq(farm_id) & Key("item").begins_with("PERSON#"))


def person_by_phone(phone: str) -> dict | None:
    items = _aws.query_all(_aws.table("farms"), IndexName="by-phone",
                           KeyConditionExpression=Key("phone_e164").eq(e164(phone)))
    people = [i for i in items if str(i.get("item", "")).startswith("PERSON#")]
    return people[0] if people else None


def person_by_id(person_id: str, farm_id: str | None = None) -> dict | None:
    if farm_id:
        return _aws.get_item("farms", farm_id=farm_id, item=f"PERSON#{person_id}")
    from boto3.dynamodb.conditions import Attr
    items = _aws.scan_all(_aws.table("farms"), FilterExpression=Attr("item").eq(f"PERSON#{person_id}"))
    return items[0] if items else None


def thread(farm_id: str) -> list[dict]:
    items = _aws.query_all(_aws.table("farms"),
                           KeyConditionExpression=Key("farm_id").eq(farm_id) & Key("item").begins_with(THREAD_PREFIX))
    out = []
    for it in items[-THREAD_LIMIT:]:
        interfaces.ThreadItem.from_item(it)  # the shared row shape; a malformed row fails loudly here
        entry = {k: v for k, v in it.items() if k not in ("farm_id", "item", "expires_at")}
        entry.setdefault("at", it["item"][len(THREAD_PREFIX):].split("#")[0])
        entry.setdefault("who", "fieldwatch")
        entry.setdefault("text", "")
        out.append(entry)
    return out


def put_thread_item(farm_id: str, who: str, text: str, **extra) -> None:
    row = interfaces.ThreadItem(farm_id=farm_id, id=uuid.uuid4().hex[:8], at=_aws.now_iso(), who=who, text=text,
                                **extra)
    _aws.table("farms").put_item(Item=_aws.to_ddb(row.to_item()))


# ---------- scene analyses ----------


def latest_analysis(field_id: str, *, need_overlays: bool = False) -> dict | None:
    """Newest fw-scene-analyses row for a field (sort key date_source = 'YYYY-MM-DD#source').

    Accepts either a flat row (status, overlays, indices, regions, max_score ...) or one that nests a
    FieldAnalysisResult under `result`; returns a flat dict either way.
    """
    page = _aws.table("scene-analyses").query(KeyConditionExpression=Key("field_id").eq(field_id),
                                              ScanIndexForward=False, Limit=25)
    for raw in page.get("Items", []):
        row = _aws.from_ddb(raw)
        flat = {**(row.get("result") or {}), **{k: v for k, v in row.items() if k != "result"}}
        if "date" not in flat:
            flat["date"] = str(row.get("date_source", "")).split("#")[0]
        if need_overlays and not overlay_key(flat, "true_color"):
            continue
        return flat
    return None


_KIND_ALIASES = {"truecolor": "true_color", "true_color": "true_color", "health": "health", "mask": "mask"}


def overlay_key(row: dict, kind: str) -> str | None:
    kind = _KIND_ALIASES.get(kind, kind)
    ref = (row.get("overlays") or {}).get(kind)
    if isinstance(ref, dict):
        return ref.get("key") or ref.get("s3_key")
    return ref if isinstance(ref, str) else None


def overlay_bounds(row: dict) -> list[float] | None:
    for ref in (row.get("overlays") or {}).values():
        if isinstance(ref, dict) and ref.get("bounds"):
            return list(ref["bounds"])
    return None


def regions(row: dict) -> list[dict]:
    return row.get("regions") or (row.get("anomaly") or {}).get("regions") or []


def stats_for(field: dict, row: dict | None) -> dict | None:
    """Map an analysis row to today's UI `stats` block (scene_date, health_index_median, ...)."""
    if not row or row.get("status") not in (None, "ok"):
        return None
    if isinstance(row.get("stats"), dict):
        return row["stats"]
    ndvi = (row.get("indices") or {}).get("ndvi") or {}
    regs = regions(row)
    weak_acres = sum(float(r.get("area_acres") or 0) for r in regs)
    acres = float(field.get("acres") or 0) or None
    if regs and field.get("polygon"):
        top = max(regs, key=lambda r: float(r.get("score") or 0))
        weakest = direction(centroid(field["polygon"]), tuple(top.get("centroid") or centroid(field["polygon"])))
    else:
        weakest = "no weak spot"
    return {
        "scene_date": row.get("date"),
        "cloud_cover_pct": round((1 - float(row.get("clear_fraction", 1))) * 100),
        "health_index_median": round(float(ndvi.get("p50", ndvi.get("mean", 0))), 2),
        "health_index_low_10pct": round(float(ndvi.get("p10", 0)), 2),
        "weakest_area": weakest,
        "share_of_field_below_0_3": round(min(1.0, weak_acres / acres), 3) if acres else 0,
        "max_score": row.get("max_score"),
    }


# ---------- farm view (today's GET /api/farms/{id} shape) ----------


def ui_field(field: dict, with_stats: bool = True) -> dict:
    out = {
        "id": field["field_id"],
        "name": field.get("name", field["field_id"]),
        "crop": field.get("crop") or field.get("crop_current") or "corn",
        "acres": field.get("acres"),
        "irrigation": field.get("irrigation", "unknown"),
        "polygon": field.get("polygon") or [],
        "status": field.get("status"),
    }
    if with_stats:
        row = latest_analysis(field["field_id"])
        st = stats_for(field, row)
        if st:
            out["stats"] = st
            out["last_scene_date"] = st.get("scene_date")
    return out


def farm_view(farm_id: str) -> dict:
    farm = get_farm_item(farm_id)
    owner = None
    if farm.get("owner_person_id"):
        owner = _aws.get_item("farms", farm_id=farm_id, item=f"PERSON#{farm['owner_person_id']}")
    return {
        "farm_id": farm_id,
        "name": farm.get("name"),
        "phone": farm.get("phone"),
        "farmer": {"name": farm.get("farmer_name", "Farmer"), "channel": "text"},
        "fields": [ui_field(f) for f in list_fields(farm_id)],
        "thread": thread(farm_id),
        "muted": bool(farm.get("muted") or (owner or {}).get("opted_out")),
        "last_checked": farm.get("last_checked"),
        "pending": None,
        "backend": "cloud",
    }


def owner_phone(farm_id: str) -> str:
    farm = get_farm_item(farm_id)
    if farm.get("owner_person_id"):
        p = _aws.get_item("farms", farm_id=farm_id, item=f"PERSON#{farm['owner_person_id']}")
        if p and p.get("phone_e164"):
            return p["phone_e164"]
    if farm.get("phone"):
        return e164(farm["phone"])
    raise ApiError("NOT_FOUND", "This farm has no phone on file")


def as_json(obj: Any) -> Any:
    return _aws.from_ddb(obj)
