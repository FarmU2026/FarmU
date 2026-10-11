"""fieldwatch.farmdata: the agent's local tools read the fixture file locally and DynamoDB in AWS."""
import boto3
import pytest
from moto import mock_aws

from fieldwatch import farmdata
from fieldwatch import tools as T
from test_api_helpers import create_tables

REGION = "us-west-2"
FARM = "farm_01testfarm"


def test_fixture_backend_is_the_default(monkeypatch):
    monkeypatch.delenv("FIELDWATCH_DATA_BACKEND", raising=False)
    assert farmdata.backend() == "fixture"
    assert farmdata.farm()["farm_id"] == "wohleb-farm"
    assert T._field("north pivot")["id"] == "north-pivot"


@pytest.fixture
def dynamo(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_REGION": REGION,
                 "AWS_DEFAULT_REGION": REGION, "FIELDWATCH_DATA_BACKEND": "dynamo", "FIELDWATCH_TABLE_PREFIX": "fw-",
                 "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent", "AWS_CONFIG_FILE": "/nonexistent"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        create_tables(boto3.client("dynamodb", region_name=REGION))
        db = boto3.resource("dynamodb", region_name=REGION)
        db.Table("fw-farms").put_item(Item={"farm_id": FARM, "item": "FARM", "farmer_name": "Dad",
                                            "owner_person_id": "per_dad", "timezone": "America/Chicago"})
        db.Table("fw-farms").put_item(Item={"farm_id": FARM, "item": "PERSON#per_dad", "person_id": "per_dad",
                                            "name": "Dad", "phone_e164": "+14025550100", "role": "owner"})
        db.Table("fw-fields").put_item(Item={"farm_id": FARM, "field_id": "fld_np", "name": "North Pivot",
                                             "crop_current": "corn", "acres": 130, "irrigation": "center pivot"})
        db.Table("fw-field-context").put_item(Item={"field_id": "fld_np", "expected_yield_bu_ac": 210,
                                                    "price_usd_bu": "4.2", "soil": {"field_id": "fld_np", "map_units": [
                                                        {"name": "Holdrege silt loam", "pct_area": 100,
                                                         "drainage_class": "Well drained", "sandy": False}]}})
        db.Table("fw-scene-analyses").put_item(Item={"field_id": "fld_np", "date_source": "2026-07-18#s2",
                                                     "status": "ok", "max_score": 3, "clear_fraction": 1,
                                                     "regions": [{"area_acres": 12}]})
        yield db


def test_dynamo_backend_reads_the_farm_in_context(dynamo):
    with farmdata.use_farm(FARM):
        farm = farmdata.farm()
        assert farm["farmer"] == {"id": "per_dad", "name": "Dad"} and farm["people"][0]["phone"] == "+14025550100"
        f = T._field("north pivot")
        assert f["id"] == "fld_np" and f["soil"]["type"] == "Holdrege silt loam"
        assert f["latest_scan"]["problem_acres"] == 12 and f["expected_yield_bu_ac"] == 210
        assert T._farmer_id() == "per_dad"


def test_dynamo_backend_finds_the_farm_from_the_field(dynamo):
    assert farmdata.farm("fld_np")["farm_id"] == FARM  # the Gateway tool Lambda: no farm in context
    with pytest.raises(LookupError):
        farmdata.farm()
