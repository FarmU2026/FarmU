"""Stage Lambdas 2..n: each computes one part of FieldContext and UpdateItems ONLY its own attributes
into fw-field-context (key: field_id). A failing stage raises; the state machine's per-stage Catch turns that
into {stage, ok: false} so the other stages stay usable and the field finishes `partial`.

Attribute ownership (FieldContext in contracts.py is flat, so these are its top-level names):

    soil        -> soil
    terrain     -> terrain
    water       -> water                                                (FEMA flood zones, NHD streams, NWI wetlands)
    crop        -> crop_history, cohort_field_ids, county_fips, stage   (crop history + same-crop neighbors)
    disease     -> disease_reports                                      (list of DiseaseReport)
    economics   -> expected_yield_bu_ac, price_usd_bu, price_as_of
    baseline    -> baseline                                             ({key, seasons, cohort_field_ids})

Finalize owns schema_version, field_id and built_at. Event: {schema_version, farm_id, field_id, boundary}.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import requests

from . import adapters
from .common import FIELD_CONTEXT_TABLE, StageError, TransientError, table, to_ddb

log = logging.getLogger(__name__)

# compute(event) -> (values by attribute name, used_stub)
Compute = Callable[[dict], tuple[dict, bool]]


@dataclass(frozen=True)
class Stage:
    name: str
    attributes: tuple[str, ...]
    compute: Compute


def _dump(model):
    return model.model_dump(mode="json")


def _soil(event):
    res = adapters.get_soil_profile(event["field_id"])
    return {"soil": _dump(res.value)}, res.stub


def _terrain(event):
    res = adapters.get_terrain_summary(event["field_id"])
    if res.value is None:
        raise StageError("no terrain data for this field")
    return {"terrain": _dump(res.value)}, res.stub


def _water(event):
    res = adapters.get_water_context(event["field_id"])
    return {"water": _dump(res.value)}, res.stub


def _crop(event):
    res = adapters.get_crop_context(event["field_id"])
    values = res.value
    if not values.get("crop_history") and not values.get("cohort_field_ids"):
        raise StageError("no crop history or neighbors for this field")
    values["county_fips"] = event.get("boundary", {}).get("county_fips") or values.get("county_fips")
    return {key: values.get(key) for key in ("crop_history", "cohort_field_ids", "county_fips", "stage")}, res.stub


def _disease(event):
    boundary = event.get("boundary", {})
    county = boundary.get("county_fips")
    if not county:
        raise StageError("county unknown, can't look up disease reports")
    season = int(event.get("season") or date.today().year)
    res = adapters.get_disease_reports(county, boundary.get("crop", "other"), season)
    return {"disease_reports": [_dump(r) for r in res.value]}, res.stub


def _economics(event):
    res = adapters.get_economics(event["field_id"])
    values = res.value
    if values.get("expected_yield_bu_ac") is None and values.get("price_usd_bu") is None:
        raise StageError("no yield or price for this field")
    return {key: values.get(key) for key in ("expected_yield_bu_ac", "price_usd_bu", "price_as_of")}, res.stub


def _baseline(event):
    boundary = event.get("boundary", {})
    res = adapters.baseline(event["field_id"], boundary.get("polygon", []), boundary.get("tile_ids", []),
                            boundary.get("crop", "other"))
    return {"baseline": res.value}, res.stub


STAGES: dict[str, Stage] = {s.name: s for s in [
    Stage("soil", ("soil",), _soil),
    Stage("terrain", ("terrain",), _terrain),
    Stage("water", ("water",), _water),
    Stage("crop", ("crop_history", "cohort_field_ids", "county_fips", "stage"), _crop),
    Stage("disease", ("disease_reports",), _disease),
    Stage("economics", ("expected_yield_bu_ac", "price_usd_bu", "price_as_of"), _economics),
    Stage("baseline", ("baseline",), _baseline),
]}


def write_own_attributes(field_id: str, stage: Stage, values: dict) -> list[str]:
    """UpdateItem SET for exactly the stage's own attributes (None values are skipped)."""
    stray = set(values) - set(stage.attributes)
    if stray:
        raise StageError(f"stage {stage.name} tried to write attributes it does not own: {sorted(stray)}")
    values = {k: v for k, v in values.items() if v is not None}
    if not values:
        raise StageError(f"stage {stage.name} produced nothing")
    names = {f"#a{i}": k for i, k in enumerate(values)}
    vals = {f":v{i}": v for i, v in enumerate(values.values())}
    table(FIELD_CONTEXT_TABLE).update_item(
        Key={"field_id": field_id},
        UpdateExpression="SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(values))),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=to_ddb(vals),
    )
    return sorted(values)


def run_stage(stage: Stage, event: dict) -> dict:
    try:
        values, stub = stage.compute(event)
    except (requests.RequestException, TimeoutError, ConnectionError) as exc:
        raise TransientError(f"{stage.name}: {exc}") from exc
    written = write_own_attributes(event["field_id"], stage, values)
    log.info("stage %s wrote %s for %s (stub=%s)", stage.name, written, event["field_id"], stub)
    return {"stage": stage.name, "ok": True, "attributes": written, "stub": stub}


def _make_handler(name: str):
    def handler(event, _context=None):
        return run_stage(STAGES[name], event)
    handler.__name__ = f"{name}_handler"
    return handler


soil_handler = _make_handler("soil")
terrain_handler = _make_handler("terrain")
water_handler = _make_handler("water")
crop_handler = _make_handler("crop")
disease_handler = _make_handler("disease")
economics_handler = _make_handler("economics")
baseline_handler = _make_handler("baseline")
