"""EDDMapS county records. No reports means no county evidence, never a negative diagnosis."""
from datetime import date
import json
import os
import requests
from fieldwatch.contracts import DiseaseReport
SPECIES={"tar spot":84963,"southern rust":17596,"gray leaf spot":17739}

def iso(value):
    if not value: return None
    value=str(value).split("T")[0].split(" ")[0]
    try: return date.fromisoformat(value).isoformat()
    except ValueError:
        from datetime import datetime
        for fmt in ("%m/%d/%Y","%m/%d/%y"):
            try: return datetime.strptime(value,fmt).date().isoformat()
            except ValueError: pass
        raise ValueError(f"unrecognized EDDMapS observation date: {value}")

def parse_reports(rows,disease,county_fips,season):
    out=[]
    for row in rows:
        county=str(row.get("county_fips",row.get("FIPS",row.get("fips","")))).zfill(5)
        if county != county_fips: continue
        first=iso(row.get("first_observed",row.get("firstObservation")))
        last=iso(row.get("last_observed",row.get("lastObservation")))
        if first and int(first[:4])>season: continue
        status=row.get("status",row.get("Status",""))
        current=last and int(last[:4])==season and str(status).lower() in ("certain","confirmed","confirmed_this_year")
        if last and int(last[:4])>season: continue # don't leak future observations into replay
        out.append(DiseaseReport(disease=disease,county_fips=county,status="confirmed_this_year" if current else "history",
                                  first_observed=first,last_observed=last))
    return out

def disease_reports(county_fips,crop,season,session=requests):
    if len(county_fips)!=5 or not county_fips.isdigit(): raise ValueError("county_fips must contain five digits")
    if crop!="corn": return []
    # EDDMapS public map routes may require permission. Use its verified county JSON URL
    # supplied by the data owner, or a licensed snapshot exported from that same feed.
    template=os.getenv("FIELDWATCH_EDDMAPS_COUNTY_URL")
    snapshot=os.getenv("FIELDWATCH_EDDMAPS_SNAPSHOT")
    if not template and not snapshot:
        raise RuntimeError("Set FIELDWATCH_EDDMAPS_COUNTY_URL or FIELDWATCH_EDDMAPS_SNAPSHOT to an authorized EDDMapS county feed")
    all_reports=[]
    data=json.loads(__import__("pathlib").Path(snapshot).read_text()) if snapshot else None
    for disease,species in SPECIES.items():
        if template:
            url=template.format(species=species,season=int(season))
            response=session.get(url,timeout=30); response.raise_for_status()
            rows=response.json()
        else: rows=data.get(str(species),[])
        if isinstance(rows,dict): rows=rows.get("data",rows.get("records",[]))
        all_reports.extend(parse_reports(rows,disease,county_fips,int(season)))
    return sorted(all_reports,key=lambda r:(r.status!="confirmed_this_year",r.disease))

def get_disease_reports(county_fips,crop,season):
    # Preserve the existing registry's single DiseaseReport output shape.
    reports=disease_reports(county_fips,crop,season)
    if not reports: raise LookupError("No EDDMapS reports for this county/crop/season")
    return reports[0]
