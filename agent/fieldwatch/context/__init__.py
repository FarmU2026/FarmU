"""Domain facade used by the registry and context workflow."""
import json
import os
from datetime import date
from fieldwatch import config
from fieldwatch.contracts import FieldContext
from . import common
from .economics import economics
from .disease import get_disease_reports, disease_reports

def cloud():
    return bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("FIELDWATCH_CONTEXT_BACKEND")=="cloud")

def get_soil_profile(field_id):
    from . import soil
    item=common.field(field_id)
    return soil.get_profile(item["field_id"],common.polygon(item))

def get_terrain_summary(field_id):
    from . import terrain
    item=common.field(field_id)
    return terrain.get_terrain(item["field_id"],common.polygon(item))

def get_crop_context(field_id,as_of=None):
    from . import boundaries
    item=common.field(field_id)
    polygon=common.polygon(item)
    if cloud():
        from boto3.dynamodb.conditions import Key
        rows=[]
        kwargs={"KeyConditionExpression":Key("farm_id").eq(item["farm_id"])}
        while True:
            page=common.table("fields").query(**kwargs)
            rows.extend(page.get("Items",[]))
            if not page.get("LastEvaluatedKey"): break
            kwargs["ExclusiveStartKey"]=page["LastEvaluatedKey"]
    else:
        rows=json.loads(config.FARM_FILE.read_text())["fields"]
    features=boundaries.load_farm(item["farm_id"],[common.polygon(f) for f in rows])
    season=date.fromisoformat(as_of).year if as_of else date.today().year
    result=boundaries.match_fields(boundaries.projected(polygon),features,item.get("crop","other"),min(season,2025))
    result["county_fips"]=item.get("county_fips") or result["county_fips"]
    result["stage"]=item.get("stage")
    return result

def get_economics(field_id,as_of=None):
    item=common.field(field_id)
    county=item.get("county_fips")
    if not county and cloud():
        cached=common.table("field-context").get_item(Key={"field_id":item["field_id"]}).get("Item",{})
        county=cached.get("county_fips")
    return economics(item.get("crop","other"),county,as_of or date.today().isoformat())

def get_field_context(field_id):
    item=common.field(field_id)
    if cloud():
        cached=common.table("field-context").get_item(Key={"field_id":item["field_id"]}).get("Item")
        if cached and cached.get("built_at"):
            value={k:v for k,v in cached.items() if k in FieldContext.model_fields}
            return FieldContext.model_validate(value)
    crop=get_crop_context(item["field_id"])
    economy=get_economics(item["field_id"])
    sources=["NRCS Soil Data Access","USGS 3DEP 1 m","USDA CSB 2018-2025"]+economy.pop("sources")
    return FieldContext(field_id=item["field_id"],built_at=common.now(),soil=get_soil_profile(item["field_id"]),
        terrain=get_terrain_summary(item["field_id"]),sources=sources,**crop,**economy)


def cached_cohort_field_ids(field_id):
    if not cloud():
        return []
    row=common.table("field-context").get_item(Key={"field_id":field_id}).get("Item",{})
    return list(row.get("cohort_field_ids",[]))
