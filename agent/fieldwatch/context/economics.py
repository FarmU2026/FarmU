"""NASS annual county yield / national price; explicitly dated fallback assumptions."""
from datetime import date
import os
import requests
DEFAULTS={"corn":(205,4.20),"soybeans":(62,10.50),"wheat":(55,5.80),"milo":(110,4.00)}
COMMODITIES={"corn":"CORN","soybeans":"SOYBEANS","wheat":"WHEAT","milo":"SORGHUM"}
URL="https://quickstats.nass.usda.gov/api/api_GET/"

def latest_value(rows,as_of):
    usable=[]
    for row in rows:
        try:
            # Annual data in the as-of year may not yet have been published.
            if int(row["year"]) >= date.fromisoformat(as_of).year: continue
            published=str(row.get("load_time",""))[:10]
            if published and published > as_of: continue
            value=float(str(row["Value"]).replace(",",""))
            if value>0: usable.append((int(row["year"]),published,value))
        except (ValueError,KeyError): continue
    return max(usable,default=None)

def economics(crop,county_fips,as_of,api_key=None,session=requests):
    api_key=os.getenv("NASS_API_KEY","") if api_key is None else api_key
    yield_,price=DEFAULTS.get(crop,(None,None))
    out={"expected_yield_bu_ac":yield_,"price_usd_bu":price,"price_as_of":None,
         "sources":[f"FieldWatch crop defaults ({crop}); planning assumptions, not current market quotes"]}
    if not api_key or crop not in COMMODITIES or not county_fips: return out
    if len(county_fips)!=5 or not county_fips.isdigit(): raise ValueError("county_fips must contain five digits")
    for attribute,stat,unit,level in [("expected_yield_bu_ac","YIELD","BU / ACRE","COUNTY"),
                                     ("price_usd_bu","PRICE RECEIVED","$ / BU","NATIONAL")]:
        params={"key":api_key,"commodity_desc":COMMODITIES[crop],"statisticcat_desc":stat,
            "unit_desc":unit,"agg_level_desc":level,"freq_desc":"ANNUAL","reference_period_desc":"YEAR",
            "domain_desc":"TOTAL","source_desc":"SURVEY","prodn_practice_desc":"ALL PRODUCTION PRACTICES",
            "class_desc":"ALL CLASSES","util_practice_desc":"GRAIN" if crop in ("corn","milo") else "ALL UTILIZATION PRACTICES","year__LE":str(date.fromisoformat(as_of).year-1),"format":"JSON"}
        if level=="COUNTY": params.update(state_fips_code=county_fips[:2],county_code=county_fips[2:])
        response=session.get(URL,params=params,timeout=30); response.raise_for_status()
        value=latest_value(response.json().get("data",[]),as_of)
        if value:
            out[attribute]=value[2]
            out["sources"].append(f"USDA NASS Quick Stats {stat.lower()} {value[0]}")
            if attribute=="price_usd_bu": out["price_as_of"]=value[1] or f"{value[0]+1}-01-01"
    return out
