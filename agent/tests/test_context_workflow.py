"""Context builder workflow (WS15): LoadBoundary, stage ownership, Finalize, and the state machine shape.

No network and no AWS: DynamoDB is moto, the STAC/county lookups are faked, the CDK stack is synthesized
with inline Lambda code.
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from cloud.lambdas.context import adapters, finalize, geo_lookup, load_boundary, stages
from cloud.lambdas.context.common import (BoundaryError, StageError, from_ddb, normalize_crop, to_ddb)
from cloud.lambdas.context.geometry import normalize_polygon
from fieldwatch import contracts as C

FARM = "farm-1"
FIELD = "north-pivot"
# A quarter section (half a mile square = 160 acres) at 41N, -97E: 804.672 m is 0.0072455 deg of latitude
# and 0.0095634 deg of longitude there (WGS84: 111,053.9 and 84,140 m per degree).
QUARTER = [[-97.0, 41.0], [-97.0 + 0.0095634, 41.0], [-97.0 + 0.0095634, 41.0 + 0.0072455],
           [-97.0, 41.0 + 0.0072455], [-97.0, 41.0]]


# ---------- geometry ----------


def test_quarter_section_is_160_acres():
    nf = normalize_polygon(QUARTER)
    assert nf.acres == pytest.approx(160, rel=0.01)
    assert nf.centroid == (pytest.approx(-96.99522, abs=1e-4), pytest.approx(41.00362, abs=1e-4))
    assert nf.bbox[0] == pytest.approx(-97.0) and nf.bbox[3] == pytest.approx(41.0072455)
    assert not nf.repaired
    # the ~10 m interior buffer is smaller than the field and nested inside it
    inner = normalize_polygon(nf.interior_polygon)
    assert 0.9 * nf.acres < inner.acres < nf.acres
    assert inner.bbox[0] > nf.bbox[0] and inner.bbox[2] < nf.bbox[2]


def test_unclosed_ring_and_decimals_are_accepted():
    ring = [[Decimal(str(x)), Decimal(str(y))] for x, y in QUARTER[:-1]]
    nf = normalize_polygon(ring)
    assert nf.acres == pytest.approx(160, rel=0.01)
    assert nf.polygon[0] == nf.polygon[-1] and nf.repaired


def test_bowtie_is_repaired_to_its_largest_piece():
    bowtie = [[-97.0, 41.0], [-96.99, 41.01], [-96.99, 41.0], [-97.0, 41.01], [-97.0, 41.0]]
    nf = normalize_polygon(bowtie)
    assert nf.repaired and nf.acres > 0


@pytest.mark.parametrize("bad", [
    [[-97, 41], [-96.9, 41]],                       # two points
    [[41.0, -97.0], [41.0, -96.9], [41.1, -96.9]],  # lat/lon swapped: latitude -97 is out of range
    [[-97, 41], [-97, 41], [-97, 41]],              # no area
    "not a polygon",
    [[-97, 41], [-97, 41.0001], [-96.9999, 41.0]],  # a few square meters
])
def test_bad_polygons_raise(bad):
    with pytest.raises(BoundaryError):
        normalize_polygon(bad)


def test_normalize_crop():
    assert [normalize_crop(x) for x in ("Corn", "soybean", "Sorghum", "turnips", None)] == [
        "corn", "soybeans", "milo", "other", "other"]


# ---------- moto fixtures ----------


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-west-2"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.client("dynamodb")
        for name, keys in {"fw-fields": ["farm_id", "field_id"], "fw-tile-index": ["tile_id", "field_id"],
                           "fw-field-context": ["field_id"]}.items():
            ddb.create_table(
                TableName=name,
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in keys],
                KeySchema=[{"AttributeName": k, "KeyType": "HASH" if i == 0 else "RANGE"} for i, k in enumerate(keys)],
                BillingMode="PAY_PER_REQUEST")
        yield boto3.resource("dynamodb")


def put_field(aws, field_id=FIELD, polygon=QUARTER, **extra):
    item = {"farm_id": FARM, "field_id": field_id, "name": "North Pivot", "crop": "Corn", "acres": 150,
            "status": "saved", **extra}
    if polygon is not None:
        item["polygon"] = polygon
    aws.Table("fw-fields").put_item(Item=to_ddb(item))


def get_field(aws, field_id=FIELD):
    return from_ddb(aws.Table("fw-fields").get_item(Key={"farm_id": FARM, "field_id": field_id})["Item"])


def get_context(aws, field_id=FIELD):
    item = aws.Table("fw-field-context").get_item(Key={"field_id": field_id}).get("Item")
    return from_ddb(item) if item else None


def tile_rows(aws):
    rows = aws.Table("fw-tile-index").scan()["Items"]
    return sorted((r["tile_id"], r["field_id"], r["farm_id"]) for r in rows)


@pytest.fixture
def fake_net(monkeypatch):
    """Replace the two network lookups. calls[] records the STAC request bodies."""
    calls = []
    scenes = {"tiles": ["MGRS-14TPM", "MGRS-14TPM", "MGRS-14TQM"]}

    def stac(body):
        calls.append(body)
        return {"features": [{"properties": {"grid:code": t}} for t in scenes["tiles"]]}

    monkeypatch.setattr(geo_lookup, "stac_search", stac)
    monkeypatch.setattr(geo_lookup, "county_lookup", lambda lon, lat: "31173")
    stac.calls, stac.scenes = calls, scenes
    return stac


# ---------- LoadBoundary ----------


def test_load_boundary_writes_tiles_and_field(aws, fake_net):
    put_field(aws)
    out = load_boundary.handler({"schema_version": "1", "farm_id": FARM, "field_id": FIELD})

    assert out["acres"] == pytest.approx(160, rel=0.03)
    assert out["tile_ids"] == ["MGRS-14TPM", "MGRS-14TQM"]
    assert out["county_fips"] == "31173" and out["crop"] == "corn"
    assert tile_rows(aws) == [("MGRS-14TPM", FIELD, FARM), ("MGRS-14TQM", FIELD, FARM)]

    item = get_field(aws)
    assert item["status"] == "building" and item["name"] == "North Pivot"  # web API attributes survive
    assert item["acres"] == pytest.approx(160, rel=0.03)
    assert item["tile_ids"] == ["MGRS-14TPM", "MGRS-14TQM"] and item["county_fips"] == "31173"
    assert item["centroid"]["lat"] == pytest.approx(41.0036, abs=1e-3)
    assert "interior_polygon" in item and item["polygon"] == [list(map(float, p)) for p in QUARTER]  # not rewritten

    body = fake_net.calls[0]
    assert body["collections"] == ["sentinel-2-c1-l2a"] and body["intersects"]["type"] == "Polygon"


def test_load_boundary_rerun_drops_stale_tiles_and_repairs_polygon(aws, fake_net):
    put_field(aws, polygon=QUARTER[:-1])  # unclosed ring: repaired and written back closed
    load_boundary.handler({"farm_id": FARM, "field_id": FIELD})
    fake_net.scenes["tiles"] = ["MGRS-14TQM"]
    load_boundary.handler({"farm_id": FARM, "field_id": FIELD})
    assert tile_rows(aws) == [("MGRS-14TQM", FIELD, FARM)]
    assert get_field(aws)["polygon"][0] == get_field(aws)["polygon"][-1]


def test_load_boundary_county_lookup_failure_is_not_fatal(aws, fake_net, monkeypatch):
    monkeypatch.setattr(geo_lookup, "county_lookup", lambda lon, lat: None)
    put_field(aws)
    assert load_boundary.handler({"farm_id": FARM, "field_id": FIELD})["county_fips"] is None
    assert "county_fips" not in get_field(aws)


@pytest.mark.parametrize("setup", ["missing", "no_polygon", "bad_polygon", "no_scenes", "bad_schema"])
def test_load_boundary_errors(aws, fake_net, setup):
    event = {"schema_version": "1", "farm_id": FARM, "field_id": FIELD}
    if setup == "no_polygon":
        put_field(aws, polygon=None)
    elif setup == "bad_polygon":
        put_field(aws, polygon=[[1, 2]])
    elif setup == "no_scenes":
        put_field(aws)
        fake_net.scenes["tiles"] = []
    elif setup == "bad_schema":
        put_field(aws)
        event["schema_version"] = "2"
    with pytest.raises(BoundaryError):
        load_boundary.handler(event)
    assert tile_rows(aws) == []


# ---------- stages ----------

EVENT = {"schema_version": "1", "farm_id": FARM, "field_id": FIELD,
         "boundary": {"county_fips": "31173", "crop": "corn", "tile_ids": ["MGRS-14TPM"], "polygon": QUARTER,
                      "acres": 160, "centroid": {"lon": -97.0, "lat": 41.0}}}
OWNED = {name: set(s.attributes) for name, s in stages.STAGES.items()}


def test_stage_ownership_is_disjoint_and_matches_contract():
    owned = [a for attrs in OWNED.values() for a in attrs]
    assert len(owned) == len(set(owned))
    flat = set(C.FieldContext.model_fields) - {"schema_version", "field_id", "built_at", "sources"}
    assert set(owned) == flat  # disease_reports and baseline are in the contract now


def test_stored_context_validates_against_the_contract():
    sample = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "field_context.json").read_text())
    stub = stages.STAGES["baseline"].compute({"field_id": "north-pivot", "boundary": {}})[0]
    C.FieldContext.model_validate({**sample, **stub})


@pytest.mark.parametrize("name", list(stages.STAGES))
def test_each_stage_writes_only_its_own_attributes(aws, name):
    handler = getattr(stages, f"{name}_handler")
    out = handler(EVENT)
    assert out["ok"] and out["stage"] == name
    item = get_context(aws)
    assert set(item) == OWNED[name] | {"field_id"}
    assert set(out["attributes"]) == OWNED[name]


def test_stage_does_not_touch_other_stages_attributes(aws):
    aws.Table("fw-field-context").put_item(Item={"field_id": FIELD, "soil": {"keep": "me"}, "other": 1})
    stages.terrain_handler(EVENT)
    stages.crop_handler(EVENT)
    item = get_context(aws)
    assert item["soil"] == {"keep": "me"} and item["other"] == 1
    assert item["terrain"]["lowspot_pct"] == 3.5 and item["crop_history"] == {"2024": "soybeans", "2025": "corn"}


def test_all_stages_assemble_the_contract_sample(aws):
    for name in stages.STAGES:
        getattr(stages, f"{name}_handler")(EVENT)
    item = get_context(aws)
    sample = json.loads((Path(C.__file__).parents[1] / "fixtures/contracts/field_context.json").read_text())
    for key in ("soil", "terrain", "crop_history", "cohort_field_ids", "county_fips", "expected_yield_bu_ac",
                "price_usd_bu", "price_as_of", "stage"):
        assert item[key] == sample[key], key
    assert item["disease_reports"][0]["disease"] == "tar spot"
    assert item["baseline"]["cohort_field_ids"] == sample["cohort_field_ids"]
    C.SoilProfile.model_validate(item["soil"])
    C.TerrainSummary.model_validate(item["terrain"])


def test_registry_stubs_are_reported(aws):
    out = stages.soil_handler(EVENT)
    assert out["stub"] is True  # registry entry is still a stub; flips to False when Kurt's lands


def test_real_registry_function_is_used_when_it_replaces_the_stub(aws, monkeypatch):
    from dataclasses import replace

    from fieldwatch import registry
    calls = []

    def real(field_id):
        calls.append(field_id)
        return C.SoilProfile(field_id=field_id, map_units=[])

    monkeypatch.setitem(registry.BY_NAME, "get_soil_profile",
                        replace(registry.BY_NAME["get_soil_profile"], fn=real, stub=False))
    out = stages.soil_handler(EVENT)
    assert calls == [FIELD] and out["stub"] is False
    assert get_context(aws)["soil"]["map_units"] == []


def test_adapter_falls_back_to_contract_samples_without_registry(aws, monkeypatch):
    monkeypatch.setattr(adapters, "_registry", lambda: None)
    out = stages.economics_handler(EVENT)
    assert out["stub"] is True and get_context(aws)["price_usd_bu"] == 4.2


def test_baseline_uses_waiting_analysis_module_when_present(aws, monkeypatch):
    import types
    mod = types.ModuleType("fieldwatch.analysis.baseline")
    seen = {}

    def build_baseline(**kwargs):
        seen.update(kwargs)
        return {"key": "context/north-pivot/baseline.parquet", "seasons": [2023, 2024, 2025], "cohort_field_ids": ["a"]}

    mod.build_baseline = build_baseline
    monkeypatch.setitem(sys.modules, "fieldwatch.analysis.baseline", mod)
    out = stages.baseline_handler(EVENT)
    assert out["stub"] is False
    assert seen["field_id"] == FIELD and seen["tile_ids"] == ["MGRS-14TPM"] and seen["crop"] == "corn"
    assert get_context(aws)["baseline"]["seasons"] == [2023, 2024, 2025]


def test_baseline_stub_when_analysis_module_absent(aws):
    out = stages.baseline_handler(EVENT)
    base = get_context(aws)["baseline"]
    assert out["stub"] is True and base["stub"] is True and base["key"] is None and base["seasons"] == []


def test_disease_stage_needs_a_county(aws):
    event = {**EVENT, "boundary": {**EVENT["boundary"], "county_fips": None}}
    with pytest.raises(StageError):
        stages.disease_handler(event)
    assert get_context(aws) is None


def test_failing_stage_writes_nothing_and_raises(aws, monkeypatch):
    def boom(field_id):
        raise RuntimeError("Soil Data Access is down")

    monkeypatch.setattr(adapters, "get_soil_profile", boom)
    with pytest.raises(RuntimeError):
        stages.soil_handler(EVENT)
    assert get_context(aws) is None


def test_network_errors_become_transient(aws, monkeypatch):
    import requests

    def down(field_id):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(adapters, "get_soil_profile", down)
    with pytest.raises(stages.TransientError):
        stages.soil_handler(EVENT)


def test_stage_cannot_write_foreign_attributes(aws):
    rogue = stages.Stage("rogue", ("soil",), lambda e: ({"soil": {}, "terrain": {}}, False))
    with pytest.raises(StageError):
        stages.run_stage(rogue, EVENT)
    assert get_context(aws) is None


# ---------- Finalize ----------


def ok(name, stub=False):
    return {"stage": name, "ok": True, "stub": stub}


def bad(name):
    return {"stage": name, "ok": False, "error": "boom"}


ALL = list(stages.STAGES)


@pytest.mark.parametrize("stage_results,status,failed", [
    ([ok(n) for n in ALL], "ready", []),
    ([ok(n) if n != "soil" else bad(n) for n in ALL], "partial", ["soil"]),
    ([bad(n) for n in ALL], "failed", ALL),
    ([{"stage": "load_boundary", "ok": False}], "failed", ["load_boundary"]),
])
def test_finalize_statuses(aws, stage_results, status, failed):
    put_field(aws)
    out = finalize.handler({"farm_id": FARM, "field_id": FIELD, "stages": stage_results})
    assert out["field_id"] == FIELD and out["status"] == status and out["failed_stages"] == failed
    item = get_field(aws)
    assert item["status"] == ("failed" if status == "failed" else "ready")
    assert item["context_status"] == status and item["context_failed_stages"] == failed
    ctx = get_context(aws)
    if status == "failed":
        assert ctx is None
    else:
        assert ctx["schema_version"] == "1" and ctx["built_at"].endswith("Z")


def test_finalize_reports_stub_stages_and_keeps_stage_data(aws):
    put_field(aws)
    stages.soil_handler(EVENT)
    out = finalize.handler({"farm_id": FARM, "field_id": FIELD, "stages": [ok("soil", True), ok("terrain")]})
    assert out["stub_stages"] == ["soil"] and get_context(aws)["soil"]["source"] == "NRCS Soil Data Access"


# ---------- whole workflow, handlers chained the way the state machine chains them ----------


def run_field(field_id, farm_id=FARM):
    """Mirrors the state machine: LoadBoundary (Catch -> failed), stages each caught, Finalize."""
    ev = {"schema_version": "1", "farm_id": farm_id, "field_id": field_id}
    try:
        boundary = load_boundary.handler(ev)
    except Exception:
        return finalize.handler({**ev, "stages": [{"stage": "load_boundary", "ok": False}]})
    results = []
    for name in stages.STAGES:
        try:
            results.append(getattr(stages, f"{name}_handler")({**ev, "boundary": boundary}))
        except Exception as exc:
            results.append({"stage": name, "ok": False, "error": type(exc).__name__})
    return finalize.handler({**ev, "stages": results})


def test_workflow_ready_partial_and_failed_fields(aws, fake_net, monkeypatch):
    put_field(aws, "good")
    put_field(aws, "partial")
    put_field(aws, "broken", polygon=[[1, 2]])
    monkeypatch.setattr(adapters, "get_disease_reports",
                        lambda county_fips, crop, season: (_ for _ in ()).throw(RuntimeError("EDDMapS down")))
    real_soil = adapters.get_soil_profile
    monkeypatch.setattr(adapters, "get_soil_profile",
                        lambda fid: (_ for _ in ()).throw(RuntimeError("SDA down")) if fid == "partial" else real_soil(fid))

    results = [run_field(f) for f in ("good", "partial", "broken")]
    by = {r["field_id"]: r for r in results}
    assert by["good"]["status"] == "partial" and by["good"]["failed_stages"] == ["disease"]  # disease is down for all
    assert by["partial"]["failed_stages"] == ["soil", "disease"]
    assert by["broken"] == {"field_id": "broken", "status": "failed", "failed_stages": ["load_boundary"],
                            "stub_stages": []}

    assert get_field(aws, "good")["status"] == "ready" and get_field(aws, "broken")["status"] == "failed"
    assert "soil" in get_context(aws, "good") and "soil" not in get_context(aws, "partial")
    assert "terrain" in get_context(aws, "partial")  # the other stages stayed usable
    assert {r[1] for r in tile_rows(aws)} == {"good", "partial"}


# ---------- state machine definition ----------


def _definition(stack_template) -> dict:
    machines = stack_template.find_resources("AWS::StepFunctions::StateMachine")
    assert len(machines) == 1
    props = next(iter(machines.values()))["Properties"]
    assert props["StateMachineType"] == "STANDARD"
    body = props["DefinitionString"]
    parts = body["Fn::Join"][1] if isinstance(body, dict) else [body]
    return json.loads("".join(p if isinstance(p, str) else "ARN" for p in parts))


@pytest.fixture(scope="module")
def machine():
    pytest.importorskip("aws_cdk")
    infra = str(Path(__file__).resolve().parents[2] / "infra")
    sys.path.insert(0, infra)
    try:
        import aws_cdk as cdk
        from aws_cdk import aws_lambda as lambda_
        from aws_cdk.assertions import Template
        from stacks import context as ctx_stack
        from stacks.data_plane import DataPlane

        def inline_lambda(scope, cid, *, handler, **_kw):
            return lambda_.Function(scope, cid, runtime=lambda_.Runtime.PYTHON_3_12, handler=handler,
                                    code=lambda_.Code.from_inline("def handler(e, c): return e"))

        original = ctx_stack.python_lambda
        ctx_stack.python_lambda = inline_lambda
        try:
            app = cdk.App()
            data = DataPlane(app, "FieldWatch-DataPlane")
            stack = ctx_stack.Context(app, "FieldWatch-Context", data=data)
        finally:
            ctx_stack.python_lambda = original
        template = Template.from_stack(stack)
        yield _definition(template), template, ctx_stack
    finally:
        sys.path.remove(infra)


def test_state_machine_map_over_fields(machine):
    definition, _, _ = machine
    states = definition["States"]
    fe = states[definition["StartAt"]]
    assert definition["StartAt"] == "ForEachField" and fe["Type"] == "Map"
    assert fe["MaxConcurrency"] == 5 and fe["ItemsPath"] == "$.field_ids" and fe["ResultPath"] == "$.results"
    assert fe["ItemSelector"]["field_id.$"] == "$$.Map.Item.Value" and fe["ItemSelector"]["farm_id.$"] == "$.farm_id"
    assert states[fe["Next"]]["Type"] == "Pass" and states[fe["Next"]]["End"]
    assert states[fe["Next"]]["Parameters"] == {"farm_id.$": "$.farm_id", "results.$": "$.results"}


def test_state_machine_per_field_chain_and_catches(machine):
    definition, _, _ = machine
    proc = definition["States"]["ForEachField"]["ItemProcessor"]
    inner = proc["States"]
    assert proc["StartAt"] == "LoadBoundaryTask"

    load = inner["LoadBoundaryTask"]
    assert load["Next"] == "BuildContext" and load["ResultPath"] == "$.boundary"
    assert load["Catch"][0]["ErrorEquals"] == ["States.ALL"] and load["Catch"][0]["Next"] == "BoundaryFailed"
    assert inner["BoundaryFailed"]["Next"] == "FinalizeTask"
    assert any("Lambda.TooManyRequestsException" in r["ErrorEquals"] for r in load["Retry"])

    par = inner["BuildContext"]
    assert par["Type"] == "Parallel" and par["ResultPath"] == "$.stages" and par["Next"] == "FinalizeTask"
    assert len(par["Branches"]) == 6
    seen = set()
    for branch in par["Branches"]:
        task = branch["States"][branch["StartAt"]]
        assert task["Type"] == "Task"
        catch = task["Catch"][0]
        assert catch["ErrorEquals"] == ["States.ALL"] and catch["ResultPath"] == "$.error"
        failed = branch["States"][catch["Next"]]
        assert failed["Type"] == "Pass" and failed["End"] and failed["Parameters"]["ok"] is False
        seen.add(failed["Parameters"]["stage"])
    assert seen == set(stages.STAGES)

    fin = inner["FinalizeTask"]
    assert fin["End"] and fin["Catch"][0]["Next"] == "FinalizeFailed"
    assert inner["FinalizeFailed"]["Parameters"]["status"] == "failed"


def test_stack_wiring(machine):
    _, template, ctx_stack = machine
    template.has_output("StateMachineArn", {})
    assert len(template.find_resources("AWS::Lambda::Function")) == 8
    handlers = {r["Properties"]["Handler"] for r in template.find_resources("AWS::Lambda::Function").values()}
    assert "cloud.lambdas.context.load_boundary.handler" in handlers and "cloud.lambdas.context.finalize.handler" in handlers
    for attr in ctx_stack.STAGE_LAMBDAS.values():
        assert f"cloud.lambdas.context.stages.{attr}" in handlers
        assert callable(getattr(stages, attr))  # every deployed handler exists
