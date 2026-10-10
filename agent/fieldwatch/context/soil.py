"""NRCS Soil Data Access polygons and component-weighted horizon summaries."""
from __future__ import annotations
import json
from collections import defaultdict
import requests
from shapely import wkt
from shapely.geometry import mapping, shape
from rasterio.warp import transform_geom
from fieldwatch.contracts import SoilMapUnit, SoilProfile
from .common import write_artifact
SDA_URL = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"

def query(sql, session=requests):
    response = session.post(SDA_URL,json={"query":sql,"format":"JSON+COLUMNNAME"},timeout=60)
    response.raise_for_status()
    rows = response.json().get("Table")
    if not rows:
        return []
    return [dict(zip(rows[0], row)) for row in rows[1:]]

def summarize_horizons(rows):
    def texture(lo,hi):
        weights = defaultdict(float)
        for r in rows:
            depth = max(0,min(float(r["bottom"]),hi)-max(float(r["top"]),lo))
            if depth > 0:
                weights[r.get("texture") or "unknown"] += depth
        return max(weights,key=weights.get) if weights else "unknown"
    awc = sand_sum = sand_depth = 0.0
    for r in rows:
        d = max(0,min(float(r["bottom"]),100)-max(float(r["top"]),0))
        if r.get("awc") is not None: awc += d * float(r["awc"])
        if r.get("sand") is not None:
            sand_sum += d * float(r["sand"]); sand_depth += d
    return {"texture_0_30":texture(0,30),"texture_30_100":texture(30,100),
            "awc_0_100_cm":awc,"sandy":bool(sand_depth and sand_sum/sand_depth >= 70)}

def get_profile(field_id, field_polygon, session=requests):
    # Geometry is generated from numeric coordinates, never interpolated farmer text.
    aoi = field_polygon.wkt
    rows = query("SELECT mukey, mupolygongeo.STAsText() AS geometry FROM mupolygon "
                 "WHERE mupolygongeo.STIntersects(geometry::STGeomFromText('" + aoi + "',4326)) = 1",session)
    clipped = []
    for row in rows:
        geom = wkt.loads(row["geometry"]).intersection(field_polygon)
        if not geom.is_empty:
            area = shape(transform_geom("EPSG:4326","EPSG:5070",mapping(geom))).area
            clipped.append((str(row["mukey"]),geom,area))
    if not clipped: raise LookupError("Soil Data Access has no map units for this field")
    keys = sorted({k for k,_,_ in clipped})
    if not all(k.isdigit() for k in keys): raise ValueError("invalid soil map-unit key")
    joined = query("SELECT m.mukey,m.muname,c.cokey,c.comppct_r,c.drainagecl,c.hydgrp,"
        "h.hzdept_r,h.hzdepb_r,h.awc_r,h.sandtotal_r,"
        "(SELECT TOP 1 t.texdesc FROM chtexturegrp t WHERE t.chkey=h.chkey AND t.rvindicator='Yes') AS texture "
        "FROM mapunit m JOIN component c ON c.mukey=m.mukey LEFT JOIN chorizon h ON h.cokey=c.cokey "
        "WHERE m.mukey IN (" + ",".join(keys) + ") AND c.comppct_r > 0",session)
    area_by_key=defaultdict(float)
    for k,_,area in clipped: area_by_key[k] += area
    units=[]
    for key in keys:
        data=[r for r in joined if str(r["mukey"])==key]
        components=defaultdict(list)
        for r in data: components[str(r["cokey"])].append(r)
        if not components: raise LookupError(f"no soil horizons for {key}")
        summaries=[]
        for rows_ in components.values():
            h=[{"top":r["hzdept_r"],"bottom":r["hzdepb_r"],"awc":r["awc_r"],
                "sand":r["sandtotal_r"],"texture":r["texture"]} for r in rows_
               if r["hzdept_r"] is not None and r["hzdepb_r"] is not None]
            if h:
                summaries.append((float(rows_[0]["comppct_r"]),summarize_horizons(h),rows_[0]))
        if not summaries: raise LookupError(f"no usable soil horizons for {key}")
        dominant=max(summaries,key=lambda x:x[0])
        weight=sum(p for p,_,_ in summaries)
        s=dict(dominant[1])
        s["awc_0_100_cm"]=sum(p*h["awc_0_100_cm"] for p,h,_ in summaries)/weight
        s["sandy"]=sum(p for p,h,_ in summaries if h["sandy"])/weight >= 0.5
        units.append(SoilMapUnit(mukey=key,name=dominant[2]["muname"],
            pct_area=100*area_by_key[key]/sum(area_by_key.values()),
            drainage_class=dominant[2].get("drainagecl") or "unknown",
            hydro_group=dominant[2].get("hydgrp") or "unknown",**s))
    sandy={u.mukey:u.sandy for u in units}
    zones={"type":"FeatureCollection","features":[{"type":"Feature","geometry":mapping(g),
            "properties":{"mukey":k,"sandy":sandy[k]}} for k,g,_ in clipped]}
    key=write_artifact(f"context/{field_id}/soil_zones.geojson",json.dumps(zones).encode(),"application/geo+json")
    return SoilProfile(field_id=field_id,map_units=units,zones_key=key)
