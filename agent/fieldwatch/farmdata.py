"""Farm and field data for the agent's in-process tools (tools.py), from one of two backends.

    fixture  (default) the farm file config.FARM_FILE (agent/fixtures/demo_farm.json): local runs and tests.
    dynamo   FIELDWATCH_DATA_BACKEND=dynamo (the Runtime agent, the Gateway tool Lambda): fw-farms (FARM and
             PERSON# items), fw-fields, fw-field-context and fw-scene-analyses, read with boto3.

Both return the farm in the fixture file's shape, so tools.py doesn't care where it came from:

    {"farm_id", "farmer": {"id", "name"}, "people": [{"id", "name", "role", "phone"}],
     "fields": [{"id", "name", "crop", "acres", "irrigation", "polygon", "soil"?, "latest_scan"?,
                 "expected_yield_bu_ac"?, "price_per_bu"?}]}

Which farm: the one set with `use_farm(farm_id)` (investigation.run_request sets it from the request), else the
farm that owns the field asked about (fw-fields GSI by-field). Table rows follow interfaces.FarmItem / PersonItem.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
from decimal import Decimal
from typing import Any, Iterator

from . import config
from .interfaces import FARM_ITEM, PERSON_PREFIX, Env, Indexes, table_name

_FARM: contextvars.ContextVar[str | None] = contextvars.ContextVar("fieldwatch_farm_id", default=None)
_CACHE: dict[str, dict] = {}


def backend() -> str:
    return os.getenv(Env.DATA_BACKEND, "fixture").strip().lower() or "fixture"


@contextlib.contextmanager
def use_farm(farm_id: str | None) -> Iterator[None]:
    """Tools called inside this block read this farm (dynamo backend). Clears the per-run cache."""
    token = _FARM.set(farm_id or None)
    _CACHE.clear()
    try:
        yield
    finally:
        _FARM.reset(token)
        _CACHE.clear()


def current_farm_id() -> str | None:
    return _FARM.get()


# ---------- dynamo backend ----------


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_plain(x) for x in v]
    return v


def _table(short: str):
    import boto3

    return boto3.resource("dynamodb", region_name=os.getenv(Env.REGION) or config.REGION).Table(table_name(short))


def _query(short: str, **kwargs) -> list[dict]:
    out, tbl = [], _table(short)
    while True:
        page = tbl.query(**kwargs)
        out.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return [_plain(i) for i in out]
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def farm_for_field(field_id: str) -> str | None:
    from boto3.dynamodb.conditions import Key

    rows = _query("fields", IndexName=Indexes.BY_FIELD, KeyConditionExpression=Key("field_id").eq(field_id))
    return rows[0]["farm_id"] if rows else None


def _soil(ctx: dict) -> dict | None:
    soil = ctx.get("soil") or {}
    units = soil.get("map_units") or []
    if not units:
        return None
    main = max(units, key=lambda u: u.get("pct_area", 0))
    sandy = [u for u in units if u.get("sandy")]
    note = (f"sandy pockets ({sum(u.get('pct_area', 0) for u in sandy):.0f}% of the field)" if sandy
            else "uniform across field" if len(units) == 1 else f"{len(units)} soil types in the field")
    return {"type": main.get("name"), "drainage": str(main.get("drainage_class", "")).lower(), "note": note,
            "map_units": units}


def _latest_scan(field_id: str) -> dict | None:
    from boto3.dynamodb.conditions import Key

    page = _table("scene-analyses").query(KeyConditionExpression=Key("field_id").eq(field_id),
                                          ScanIndexForward=False, Limit=1)
    rows = [_plain(i) for i in page.get("Items", [])]
    if not rows:
        return None
    r = rows[0]
    regions = r.get("regions") or []
    return {"date": r.get("date") or str(r.get("date_source", "")).split("#")[0], "status": r.get("status"),
            "max_score": r.get("max_score"), "problem_areas": len(regions),
            "problem_acres": round(sum(float(x.get("area_acres") or 0) for x in regions), 1),
            "cloud_free": (r.get("clear_fraction") or 0) >= 0.7}


def latest_analysis(field_id: str) -> dict | None:
    """The newest fw-scene-analyses row for a field as one flat dict (a nested `result` is merged in), or None.
    Dynamo backend only: the fixture farm carries a one-line summary, not an analysis."""
    if backend() != "dynamo":
        return None
    from boto3.dynamodb.conditions import Key

    page = _table("scene-analyses").query(KeyConditionExpression=Key("field_id").eq(field_id),
                                          ScanIndexForward=False, Limit=10)
    for raw in page.get("Items", []):
        row = _plain(raw)
        flat = {**(row.get("result") or {}), **{k: v for k, v in row.items() if k != "result"}}
        flat.setdefault("date", str(row.get("date_source", "")).split("#")[0])
        flat["field_id"] = field_id
        return flat
    return None


def field_context_row(field_id: str) -> dict | None:
    """The fw-field-context item for a field (dynamo backend), or None."""
    if backend() != "dynamo":
        return None
    item = _table("field-context").get_item(Key={"field_id": field_id}).get("Item")
    return _plain(item) if item else None


def _load_dynamo(farm_id: str) -> dict:
    from boto3.dynamodb.conditions import Key

    items = _query("farms", KeyConditionExpression=Key("farm_id").eq(farm_id))
    farm = next((i for i in items if i.get("item") == FARM_ITEM), None)
    if farm is None:
        raise LookupError(f"unknown farm {farm_id!r}")
    people = [{"id": p.get("person_id"), "name": p.get("name"), "role": p.get("role"), "phone": p.get("phone_e164"),
               "channel": p.get("channel_pref")}
              for p in items if str(p.get("item", "")).startswith(PERSON_PREFIX)]
    owner = farm.get("owner_person_id")
    fields = []
    for f in _query("fields", KeyConditionExpression=Key("farm_id").eq(farm_id)):
        ctx = _plain(_table("field-context").get_item(Key={"field_id": f["field_id"]}).get("Item") or {})
        out = {"id": f["field_id"], "name": f.get("name") or f["field_id"],
               "crop": f.get("crop_current") or f.get("crop") or "corn", "acres": f.get("acres"),
               "irrigation": f.get("irrigation") or ("center pivot" if f.get("pivot") else "unknown"),
               "polygon": f.get("polygon") or []}
        extras = {"soil": _soil(ctx), "latest_scan": _latest_scan(f["field_id"]),
                  "expected_yield_bu_ac": ctx.get("expected_yield_bu_ac"), "price_per_bu": ctx.get("price_usd_bu")}
        out.update({k: v for k, v in extras.items() if v is not None})
        fields.append(out)
    return {"farm_id": farm_id, "farmer": {"id": owner, "name": farm.get("farmer_name") or "Farmer"},
            "people": people, "fields": fields}


# ---------- public ----------


def farm(field_id: str | None = None) -> dict:
    """The current farm in the fixture shape. With the dynamo backend and no farm set, `field_id` picks it."""
    if backend() != "dynamo":
        return json.loads(config.FARM_FILE.read_text())
    farm_id = current_farm_id() or (farm_for_field(field_id) if field_id else None)
    if not farm_id:
        raise LookupError("no farm in context: run tools inside farmdata.use_farm(farm_id) or pass a field id")
    if current_farm_id() is None:  # a warm Gateway Lambda: no run to scope a cache to, read fresh
        return _load_dynamo(farm_id)
    if farm_id not in _CACHE:
        _CACHE[farm_id] = _load_dynamo(farm_id)
    return _CACHE[farm_id]
