"""Field add / edit / delete on the deployed API (cloud.api.farms), against moto."""
import json

import pytest

from fieldwatch import contracts as C
from cloud.api import farms
from test_api_helpers import BUCKET, NORTH_PIVOT_LATLON, aws, call, put_analysis, signup  # noqa: F401

FARM = "4025550100"
FIELD = f"fld_{FARM}_north-pivot"
SQUARE = [[42.10, -96.60], [42.10, -96.59], [42.09, -96.59], [42.09, -96.60]]
URL = f"/api/farms/{FARM}/fields"


def _names(farm):
    return {f["id"]: f["name"] for f in farm["fields"]}


def _sm(aws):
    return aws.sfn.create_state_machine(name="fw-context-builder", definition=json.dumps(
        {"StartAt": "x", "States": {"x": {"Type": "Pass", "End": True}}}),
        roleArn="arn:aws:iam::123456789012:role/fw-sfn")["stateMachineArn"]


def test_add_field(aws):
    signup(farms)
    code, farm, _ = call(farms, "POST", URL, {"name": "South 40", "crop": "soybeans", "polygon": SQUARE})
    assert code == 200
    assert farm["farm_id"] == FARM and "thread" in farm  # same shape as GET /api/farms/{id}
    new = next(f for f in farm["fields"] if f["name"] == "South 40")
    assert new["id"] == f"fld_{FARM}_south-40" and new["crop"] == "soybeans" and new["status"] == "building"
    assert new["polygon"][0] == [-96.60, 42.10] and new["acres"] > 0
    assert FIELD in _names(farm)
    row = aws.table("fields").get_item(Key={"farm_id": FARM, "field_id": new["id"]})["Item"]
    assert row["geometry_key"] == f"fields/{new['id']}/geometry.geojson"
    aws.s3.get_object(Bucket=BUCKET, Key=row["geometry_key"])


def test_add_starts_setup_for_that_field_only(aws, monkeypatch):
    sm = _sm(aws)
    signup(farms)
    monkeypatch.setenv("FIELDWATCH_CONTEXT_SM_ARN", sm)
    monkeypatch.setenv("FIELDWATCH_CHECK_FN", "fw-scene-check-latest")
    call(farms, "POST", URL, {"name": "South 40", "polygon": SQUARE})
    (ex,) = aws.sfn.list_executions(stateMachineArn=sm)["executions"]
    assert json.loads(aws.sfn.describe_execution(executionArn=ex["executionArn"])["input"])["field_ids"] == [
        f"fld_{FARM}_south-40"]
    (_, _, payload), = [c for c in aws.lambda_calls if c[0] == "fw-scene-check-latest"]
    assert C.CheckLatestRequest.model_validate(payload).field_ids == [f"fld_{FARM}_south-40"]
    assert aws.table("farms").get_item(Key={"farm_id": FARM, "item": "FARM"})["Item"]["context_build_id"] == ex["name"]


@pytest.mark.parametrize("body,fragment", [
    ({"name": "", "polygon": SQUARE}, "name"),
    ({"name": "   ", "polygon": SQUARE}, "name"),
    ({"name": "north pivot", "polygon": SQUARE}, "already"),  # unique, case-insensitive
    ({"name": "X", "polygon": SQUARE[:2]}, "polygon"),
    ({"name": "X", "polygon": [[1, 2, 3]] * 3}, "polygon"),
    ({"name": "X", "polygon": [[142, 2], [1, 2], [3, 4]]}, "polygon"),
    ({"name": "X", "crop": "pineapple", "polygon": SQUARE}, "crop"),
    ({"polygon": SQUARE}, "name"),
])
def test_add_invalid(aws, body, fragment):
    signup(farms)
    code, out, _ = call(farms, "POST", URL, body)
    assert code == 400 and out["error"]["code"] == "INVALID_INPUT" and fragment in out["error"]["message"]
    assert len(aws.table("fields").scan()["Items"]) == 1


def test_add_unknown_farm(aws):
    code, out, _ = call(farms, "POST", "/api/farms/nope/fields", {"name": "X", "polygon": SQUARE})
    assert code == 404 and out["error"]["code"] == "NOT_FOUND"


def test_rename_and_crop_keep_the_id(aws):
    signup(farms)
    put_analysis(aws, FIELD)
    code, farm, _ = call(farms, "PATCH", f"{URL}/{FIELD}", {"name": "Home Quarter", "crop": "soy"})
    assert code == 200
    f = farm["fields"][0]
    assert f["id"] == FIELD and f["name"] == "Home Quarter" and f["crop"] == "soybeans"
    assert f["stats"]["scene_date"] == "2026-07-18"  # history is still attached to the same id
    row = aws.table("fields").get_item(Key={"farm_id": FARM, "field_id": FIELD})["Item"]
    assert row["crop_current"] == "soybeans" and [float(x) for x in row["polygon"][0]] == [-96.548, 42.168]  # polygon untouched
    geo = json.loads(aws.s3.get_object(Bucket=BUCKET, Key=row["geometry_key"])["Body"].read())
    assert geo["properties"]["name"] == "Home Quarter"


def test_rename_to_own_name_and_collision(aws):
    signup(farms, fields=[{"name": "A", "polygon": NORTH_PIVOT_LATLON}, {"name": "B", "polygon": SQUARE}])
    a = f"fld_{FARM}_a"
    assert call(farms, "PATCH", f"{URL}/{a}", {"name": "a"})[0] == 200  # same field, new case
    code, out, _ = call(farms, "PATCH", f"{URL}/{a}", {"name": "B"})
    assert code == 400 and out["error"]["code"] == "INVALID_INPUT"


def test_rename_only_does_not_restart_setup(aws, monkeypatch):
    signup(farms)
    monkeypatch.setenv("FIELDWATCH_CHECK_FN", "fw-scene-check-latest")
    call(farms, "PATCH", f"{URL}/{FIELD}", {"name": "Renamed"})
    assert not [c for c in aws.lambda_calls if c[0] == "fw-scene-check-latest"]


def test_redraw_resets_and_restarts_setup(aws, monkeypatch):
    sm = _sm(aws)
    signup(farms)
    monkeypatch.setenv("FIELDWATCH_CONTEXT_SM_ARN", sm)
    monkeypatch.setenv("FIELDWATCH_CHECK_FN", "fw-scene-check-latest")
    code, farm, _ = call(farms, "PATCH", f"{URL}/{FIELD}", {"polygon": SQUARE})
    assert code == 200
    f = farm["fields"][0]
    assert f["id"] == FIELD and f["polygon"][0] == [-96.60, 42.10] and f["status"] == "building" and f["acres"] < 300
    assert len(aws.sfn.list_executions(stateMachineArn=sm)["executions"]) == 1
    assert [c for c in aws.lambda_calls if c[0] == "fw-scene-check-latest"]


def test_patch_errors(aws):
    signup(farms)
    assert call(farms, "PATCH", f"{URL}/{FIELD}", {})[0] == 400
    assert call(farms, "PATCH", f"{URL}/{FIELD}", {"polygon": SQUARE[:2]})[0] == 400
    assert call(farms, "PATCH", f"{URL}/{FIELD}", {"crop": "kale"})[0] == 400
    assert call(farms, "PATCH", f"{URL}/{FIELD}", {"name": " "})[0] == 400
    code, out, _ = call(farms, "PATCH", f"{URL}/fld_missing", {"name": "x"})
    assert code == 404 and out["error"]["code"] == "NOT_FOUND"
    assert call(farms, "PATCH", "/api/farms/nope/fields/x", {"name": "x"})[0] == 404


def test_delete_field_keeps_history(aws):
    signup(farms, fields=[{"name": "A", "polygon": NORTH_PIVOT_LATLON}, {"name": "B", "polygon": SQUARE}])
    a = f"fld_{FARM}_a"
    put_analysis(aws, a)
    code, farm, _ = call(farms, "DELETE", f"{URL}/{a}")
    assert code == 200 and _names(farm) == {f"fld_{FARM}_b": "B"}
    assert call(farms, "DELETE", f"{URL}/{a}")[0] == 404
    assert call(farms, "DELETE", "/api/farms/nope/fields/x")[0] == 404
    assert aws.table("scene-analyses").scan()["Items"]  # history stays
    # Re-adding the same name must not inherit the old field's analyses.
    _, farm, _ = call(farms, "POST", URL, {"name": "A", "polygon": SQUARE})
    again = next(f for f in farm["fields"] if f["name"] == "A")
    assert again["id"] == f"{a}-2" and "stats" not in again
