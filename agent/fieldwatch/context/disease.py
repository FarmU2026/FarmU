"""EDDMapS county records. No reports means no county evidence, never a negative diagnosis."""
from datetime import date, datetime
import json
import os
from pathlib import Path

import requests

from fieldwatch.contracts import DiseaseReport

SPECIES = {"tar spot": 84963, "southern rust": 17596, "gray leaf spot": 17739}
DEFAULT_COUNTY_URL = "https://maps.eddmaps.org/annualprogress/data.cfm?sub={species}&year={season}"
# EDDMapS CloudFront blocks requests' default user agent; the public feed needs no token.
HEADERS = {"User-Agent": "FieldWatch/1.0 county disease context"}


def iso(value):
    if not value:
        return None
    value = str(value).split("T")[0].split(" ")[0]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        for fmt in ("%m-%d-%Y", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                pass
        raise ValueError(f"unrecognized EDDMapS observation date: {value}")


def county_rows(payload):
    """Normalize the public country/FIPS map or an existing exported row list.

    Fail on an unfamiliar/error payload rather than reporting no county evidence.
    Only US records belong to the county-FIPS contract.
    """
    if isinstance(payload, dict):
        if "us" in payload:
            counties = payload["us"]
            if not isinstance(counties, dict) or any(not isinstance(row, dict) for row in counties.values()):
                raise ValueError("EDDMapS us must be a county-FIPS object")
            for fips, row in counties.items():
                if len(str(fips)) not in (4, 5) or not str(fips).isdigit():
                    raise ValueError("EDDMapS US county keys must be four or five-digit FIPS")
                if not {"ObservationDate", "LastObservationDate", "Type"} <= row.keys():
                    raise ValueError("EDDMapS county is missing observation fields")
                if row["Type"] not in ("Certain", "History"):
                    raise ValueError("unrecognized EDDMapS county Type")
            return [dict(row, county_fips=str(fips)) for fips, row in counties.items()]
        if "data" in payload:
            payload = payload["data"]
        elif "records" in payload:
            payload = payload["records"]
        else:
            raise ValueError("unrecognized EDDMapS county feed schema")
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ValueError("EDDMapS county records must be a list of objects")
    return payload


def parse_reports(rows, disease, county_fips, season):
    out = []
    for row in county_rows(rows):
        county = str(row.get("county_fips", row.get("FIPS", row.get("fips", "")))).zfill(5)
        if county != county_fips:
            continue
        first = iso(row.get("ObservationDate", row.get("first_observed", row.get("firstObservation"))))
        last = iso(row.get("LastObservationDate", row.get("last_observed", row.get("lastObservation"))))
        if first and int(first[:4]) > season:
            continue
        if last and int(last[:4]) > season:
            continue  # don't leak future observations into replay
        status = row.get("Type", row.get("status", row.get("Status", "")))
        current = last and int(last[:4]) == season and str(status).lower() in (
            "certain", "confirmed", "confirmed_this_year")
        out.append(DiseaseReport(
            disease=disease, county_fips=county,
            status="confirmed_this_year" if current else "history",
            first_observed=first, last_observed=last))
    return out


def disease_reports(county_fips, crop, season, session=requests):
    if len(county_fips) != 5 or not county_fips.isdigit():
        raise ValueError("county_fips must contain five digits")
    if crop != "corn":
        return []
    season = int(season)
    template = os.getenv("FIELDWATCH_EDDMAPS_COUNTY_URL", "").strip()
    snapshot = os.getenv("FIELDWATCH_EDDMAPS_SNAPSHOT", "").strip()
    # An explicit URL retains precedence; otherwise an optional offline snapshot
    # wins over the public default. Existing species-keyed snapshots still work.
    data = json.loads(Path(snapshot).read_text()) if snapshot and not template else None
    if snapshot and not template and not isinstance(data, dict):
        raise ValueError("EDDMapS snapshot must be an object keyed by species ID")
    all_reports = []
    for disease, species in SPECIES.items():
        if data is None:
            url = (template or DEFAULT_COUNTY_URL).format(species=species, season=season)
            response = session.get(url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            rows = response.json()
        else:
            rows = data.get(str(species), [])
        all_reports.extend(parse_reports(rows, disease, county_fips, season))
    return sorted(all_reports, key=lambda r: (r.status != "confirmed_this_year", r.disease))


def get_disease_reports(county_fips, crop, season):
    # Preserve the existing registry's single DiseaseReport output shape.
    reports = disease_reports(county_fips, crop, season)
    if not reports:
        raise LookupError("No EDDMapS reports for this county/crop/season")
    return reports[0]
