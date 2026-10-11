"""NASS annual yield (county median, then district, then state) / national price; explicitly dated fallback assumptions."""
from datetime import date
from statistics import median
import os
import requests
DEFAULTS={"corn":(205,4.20),"soybeans":(62,10.50),"wheat":(55,5.80),"milo":(110,4.00)}
COMMODITIES={"corn":"CORN","soybeans":"SOYBEANS","wheat":"WHEAT","milo":"SORGHUM"}
URL="https://quickstats.nass.usda.gov/api/api_GET/"
YIELD_YEARS=5  # county estimates are sparse and noisy: take the median of the latest few published years
MIN_YEARS=3
_param_cache={}

def ssm_value(name,client=None):
    """SecureString parameter value, cached per process; "" on any error (callers fall back to the defaults)."""
    if name in _param_cache: return _param_cache[name]
    try:
        if client is None:
            import boto3
            client=boto3.client("ssm")
        value=client.get_parameter(Name=name,WithDecryption=True)["Parameter"]["Value"]
    except Exception: return ""
    _param_cache[name]=value
    return value

def resolve_key(api_key=None,ssm_client=None):
    """Explicit key, else env NASS_API_KEY, else the SSM parameter named by env NASS_API_KEY_PARAM."""
    if api_key is not None: return api_key
    key=os.getenv("NASS_API_KEY","")
    param=os.getenv("NASS_API_KEY_PARAM","")
    return key or (ssm_value(param,ssm_client) if param else "")

def observations(rows,as_of):
    """(year, published date, value) of annual observations that existed at `as_of`, skipping suppressed values."""
    for row in rows:
        try:
            # Annual data in the as-of year may not yet have been published.
            if int(row["year"]) >= date.fromisoformat(as_of).year: continue
            published=str(row.get("load_time",""))[:10]
            if published and published > as_of: continue
            value=float(str(row["Value"]).replace(",",""))
            if value>0: yield int(row["year"]),published,value
        except (ValueError,KeyError): continue

def latest_value(rows,as_of):
    return max(observations(rows,as_of),default=None)

def yearly_values(rows,as_of):
    """{year: value}, the most recently loaded observation per year."""
    best={}
    for year,published,value in observations(rows,as_of): best[year]=max(best.get(year,(published,value)),(published,value))
    return {y:v for y,(_,v) in best.items()}

def median_recent(rows,as_of,minimum):
    """(median of the latest YIELD_YEARS usable years, first year, last year), or None with fewer than `minimum`."""
    values=yearly_values(rows,as_of)
    years=sorted(values)[-YIELD_YEARS:]
    if len(years)<minimum or not years: return None
    return round(median(values[y] for y in years),1),years[0],years[-1]

def economics(crop,county_fips,as_of,api_key=None,session=requests,ssm_client=None):
    api_key=resolve_key(api_key,ssm_client)
    yield_,price=DEFAULTS.get(crop,(None,None))
    out={"expected_yield_bu_ac":yield_,"price_usd_bu":price,"price_as_of":None,
         "sources":[f"FieldWatch crop defaults ({crop}); planning assumptions, not current market quotes"]}
    if not api_key or crop not in COMMODITIES or not county_fips: return out
    if len(county_fips)!=5 or not county_fips.isdigit(): raise ValueError("county_fips must contain five digits")
    year_limit=str(date.fromisoformat(as_of).year-1)
    def query(stat,unit,level,**where):
        params={"key":api_key,"commodity_desc":COMMODITIES[crop],"statisticcat_desc":stat,
            "unit_desc":unit,"agg_level_desc":level,"freq_desc":"ANNUAL","reference_period_desc":"YEAR",
            "domain_desc":"TOTAL","source_desc":"SURVEY","prodn_practice_desc":"ALL PRODUCTION PRACTICES",
            "class_desc":"ALL CLASSES","util_practice_desc":"GRAIN" if crop in ("corn","milo") else "ALL UTILIZATION PRACTICES","year__LE":year_limit,"format":"JSON",**where}
        response=session.get(URL,params=params,timeout=30); response.raise_for_status()
        return response.json().get("data",[])
    state=county_fips[:2]
    # Yield: county median of the latest published years; else the Agricultural Statistics District the county
    # belongs to (code taken from the county rows); else the state.
    county_rows=query("YIELD","BU / ACRE","COUNTY",state_fips_code=state,county_code=county_fips[2:])
    def label(rows,key,default,suffix): return next((f"{str(r[key]).title()}{suffix}" for r in rows if r.get(key)),default)
    found=median_recent(county_rows,as_of,MIN_YEARS)
    where=label(county_rows,"county_name","County"," County")
    if not found:
        asd=next((str(r["asd_code"]) for r in county_rows if r.get("asd_code")),None)
        if asd:
            asd_rows=query("YIELD","BU / ACRE","AGRICULTURAL DISTRICT",state_fips_code=state,asd_code=asd)
            found=median_recent(asd_rows,as_of,MIN_YEARS)
            where=label(asd_rows or county_rows,"asd_desc",f"District {asd}"," District")
    if not found:
        state_rows=query("YIELD","BU / ACRE","STATE",state_fips_code=state)
        found=median_recent(state_rows,as_of,1)
        where=label(state_rows,"state_name","State"," (state)")
    if found:
        out["expected_yield_bu_ac"]=found[0]
        out["sources"].append(f"USDA NASS Quick Stats yield, {where} median {found[1]}-{found[2]}")
    value=latest_value(query("PRICE RECEIVED","$ / BU","NATIONAL"),as_of)
    if value:
        out["price_usd_bu"]=value[2]
        out["sources"].append(f"USDA NASS Quick Stats price received {value[0]}")
        out["price_as_of"]=value[1] or f"{value[0]+1}-01-01"
    return out
