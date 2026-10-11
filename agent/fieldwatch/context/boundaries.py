"""USDA CSB 2018-2025; projected range reads and per-farm persistent cache."""
import hashlib
import json
import os
from pathlib import Path
from shapely.geometry import shape,mapping
from shapely.ops import unary_union
from rasterio.warp import transform_geom
from .common import bucket,write_artifact
CSB_URL="https://www.nass.usda.gov/Research_and_Science/Crop-Sequence-Boundaries/datasets/NationalCSB_2018-2025_rev23.zip"
CROPS={1:"corn",5:"soybeans",4:"milo",24:"wheat",21:"barley",36:"alfalfa"}

def projected(polygon):
    return shape(transform_geom("EPSG:4326","EPSG:5070",mapping(polygon)))

def load_farm(farm_id, polygons, reader=None):
    bounds=unary_union([projected(p) for p in polygons]).buffer(10000).bounds
    fingerprint=hashlib.sha256(json.dumps(bounds).encode()).hexdigest()[:16]
    safe=hashlib.sha256(str(farm_id).encode()).hexdigest()[:20]
    key=f"static/csb/{safe}/{fingerprint}-2018-2025.json"
    root=Path(os.getenv("FIELDWATCH_CONTEXT_CACHE",__import__("tempfile").gettempdir())) / "fieldwatch-csb"
    path=root/f"{safe}-{fingerprint}.json"
    if path.exists(): return json.loads(path.read_text())
    if bucket():
        import boto3
        from botocore.exceptions import ClientError
        try:
            payload=boto3.client("s3").get_object(Bucket=bucket(),Key=key)["Body"].read()
            path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(payload)
            return json.loads(payload)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("NoSuchKey","404"): raise
    if reader is None:
        import pyogrio
        def reader(bounds):
            source="/vsizip//vsicurl/"+CSB_URL
            layer=pyogrio.list_layers(source)[0][0]
            meta, _, geometry, columns=pyogrio.raw.read(source,layer=layer,bbox=bounds)
            if meta.get("crs") != "EPSG:5070":
                raise ValueError("USDA CSB source must be EPSG:5070")
            from shapely import from_wkb
            return [{"type":"Feature","geometry":mapping(from_wkb(g)),
                    "properties":{name:values[i].item() if hasattr(values[i],"item") else values[i]
                                  for name,values in zip(meta["fields"],columns)}}
                    for i,g in enumerate(geometry)]
    result=reader(bounds)
    payload=json.dumps(result).encode()
    path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(payload)
    if bucket(): write_artifact(key,payload,"application/json")
    return result

def match_fields(field,features,crop,year,radius_m=10000):
    candidates=[(shape(f["geometry"]),f["properties"]) for f in features]
    matches=[(g.intersection(field).area,g,p) for g,p in candidates if g.intersection(field).area > 0]
    if not matches: raise LookupError("no CSB field intersects the drawn field")
    _,own,properties=max(matches,key=lambda x:x[0])
    history={}
    for y in range(2018,min(2025,int(year))+1):
        value=properties.get(f"CDL{y}")
        if value is not None:
            history[str(y)]=CROPS.get(int(value),"other")
    # The source ends in 2025: don't claim 2026 crop observations as present.
    column=f"CDL{year}"
    cohort=[]
    for geom,p in candidates:
        value=p.get(column)
        if geom.intersection(field).area>0 or value is None: continue
        if CROPS.get(int(value),"other")==crop and geom.centroid.distance(field.centroid)<=radius_m:
            identifier=p.get("CSBID",p.get("CSB_ID",p.get("OBJECTID")))
            if identifier is not None: cohort.append(str(identifier))
    county=properties.get("CNTY") or properties.get("COUNTYFIPS")
    if county is None and properties.get("STATEFIPS") and properties.get("COUNTY"):
        county=str(properties["STATEFIPS"]).zfill(2)+str(properties["COUNTY"]).zfill(3)
    return {"crop_history":history,"cohort_field_ids":sorted(set(cohort)),
            "county_fips":str(county).zfill(5) if county is not None else None}
