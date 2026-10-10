"""Network lookups LoadBoundary needs: Sentinel-2 grid tiles (Earth Search STAC) and the county FIPS.

Both go through the module-level functions `stac_search` and `county_lookup`; tests replace those.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import requests

from .common import BoundaryError, TransientError

log = logging.getLogger(__name__)

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-c1-l2a"  # docs/implementation-specs.md: the scene filter keys on this collection's grid:code
FCC_AREA = "https://geo.fcc.gov/api/census/area"
CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"


def stac_search(body: dict) -> dict:
    try:
        r = requests.post(EARTH_SEARCH, json=body, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        raise TransientError(f"Earth Search request failed: {exc}") from exc


def tile_ids_for(geometry: dict, *, today: date | None = None) -> list[str]:
    """Distinct Sentinel-2 grid:code values (e.g. 'MGRS-14TPM') of scenes that intersect the field.

    Searches the last 60 days, then the last year if that finds nothing (tile footprints are fixed, so any
    scene over the field shows which tiles cover it). Uses `intersects` with the polygon rather than its
    bbox so a field near a tile edge doesn't pick up a tile that doesn't actually cover it.
    """
    today = today or date.today()
    for days in (60, 365):
        body = {
            "collections": [COLLECTION],
            "intersects": geometry,
            "datetime": f"{(today - timedelta(days=days)).isoformat()}T00:00:00Z/{today.isoformat()}T23:59:59Z",
            "limit": 100,
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        }
        tiles = {
            f["properties"]["grid:code"]
            for f in stac_search(body).get("features", [])
            if f.get("properties", {}).get("grid:code")
        }
        if tiles:
            return sorted(tiles)
    raise BoundaryError("no Sentinel-2 scenes found over this field")


def _get_json(url: str, params: dict) -> dict:
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def county_lookup(lon: float, lat: float) -> str | None:
    """5-digit county FIPS for a point (FCC Area API, then the Census geocoder), or None.

    Best effort: the disease stage needs it, but a failed lookup must not fail the field.
    """
    try:
        res = _get_json(FCC_AREA, {"lat": lat, "lon": lon, "format": "json"}).get("results") or []
        if res and res[0].get("county_fips"):
            return str(res[0]["county_fips"])
    except (requests.RequestException, ValueError) as exc:
        log.warning("FCC county lookup failed: %s", exc)
    try:
        geo = _get_json(CENSUS_GEOCODER, {"x": lon, "y": lat, "benchmark": "Public_AR_Current",
                                          "vintage": "Current_Current", "format": "json"})
        counties = geo.get("result", {}).get("geographies", {}).get("Counties") or []
        if counties and counties[0].get("GEOID"):
            return str(counties[0]["GEOID"])
    except (requests.RequestException, ValueError) as exc:
        log.warning("Census county lookup failed: %s", exc)
    return None
