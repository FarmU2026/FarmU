"""Mapped surface water for a field: FEMA flood zones, USGS NHD streams, USFWS NWI wetlands.

Three public, keyless ArcGIS REST services queried by envelope (EPSG:4326, GeoJSON out). Areas and distances are
computed in a Lambert azimuthal equal-area projection centred on the field (a small spherical implementation, so
the Lambda bundle needs only shapely, not pyproj). Each source is optional: one failing leaves its fields None
and is named in `sources`; only when all three fail does this raise (ConnectionError, which the stage retries).
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from shapely.geometry import Polygon, shape
import shapely
from shapely.ops import unary_union

from fieldwatch.contracts import WaterContext

FEMA_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"  # S_FLD_HAZ_AR
NWI_URL = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0/query"
NHD_URLS = ["https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6/query",  # Flowline - Large Scale
            "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/4/query"]  # Flowline - Small Scale
STREAM_SEARCH_DEG = 0.02  # ~2 km envelope around the field; farther streams are not reported
MAX_RECORDS = 500
TIMEOUT_S = 20
EARTH_R = 6371007.2  # authalic radius, m
FEMA_LABEL, NHD_LABEL, NWI_LABEL = "FEMA NFHL flood zones", "USGS NHD streams", "USFWS NWI wetlands"


def equal_area(lon0: float, lat0: float):
    """Function (lon, lat) -> (x, y) metres, Lambert azimuthal equal-area centred on lon0/lat0."""
    sin0, cos0, lam0 = math.sin(math.radians(lat0)), math.cos(math.radians(lat0)), math.radians(lon0)

    def project(lon, lat, z=None):
        lam, phi = np.radians(np.asarray(lon, float)) - lam0, np.radians(np.asarray(lat, float))
        k = np.sqrt(2 / (1 + sin0 * np.sin(phi) + cos0 * np.cos(phi) * np.cos(lam)))
        return (EARTH_R * k * np.cos(phi) * np.sin(lam),
                EARTH_R * k * (cos0 * np.sin(phi) - sin0 * np.cos(phi) * np.cos(lam)))
    return project


def to_metres(geom, project):
    """A lon/lat geometry in the equal-area projection."""
    return shapely.transform(geom, lambda xy: np.column_stack(project(xy[:, 0], xy[:, 1])))


def _features(url, bounds, out_fields, session):
    """[(shapely geometry in lon/lat, properties)] for features intersecting the envelope."""
    response = session.get(url, params={
        "geometry": ",".join(f"{v:.6f}" for v in bounds), "geometryType": "esriGeometryEnvelope", "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects", "outFields": out_fields, "outSR": 4326, "f": "geojson",
        "resultRecordCount": MAX_RECORDS}, timeout=TIMEOUT_S)
    response.raise_for_status()
    body = response.json()
    if "error" in body:  # ArcGIS reports many failures as HTTP 200 with an error object
        raise RuntimeError(f"ArcGIS error: {body['error']}")
    out = []
    for feature in body.get("features", []):
        if feature.get("geometry"):
            geom = shape(feature["geometry"])
            out.append((geom if geom.is_valid else geom.buffer(0), feature.get("properties") or {}))
    return out


def _overlap(field_m, project, items):
    """(percent of the field covered by the union of the items' polygons, [(items) that touch the field])."""
    touching, pieces = [], []
    for geom, props in items:
        piece = to_metres(geom, project).intersection(field_m)
        if piece.area > 0:
            touching.append(props)
            pieces.append(piece)
    covered = unary_union(pieces).area if pieces else 0.0
    return min(100.0, 100 * covered / field_m.area), touching


def _fema(field, field_m, project, session):
    rows = _features(FEMA_URL, field.bounds, "FLD_ZONE,ZONE_SUBTY,SFHA_TF", session)
    sfha = [(g, p) for g, p in rows if str(p.get("SFHA_TF", "")).upper() == "T"]
    pct, _ = _overlap(field_m, project, sfha)
    _, touching = _overlap(field_m, project, rows)
    return {"floodplain_pct": pct, "flood_zones": sorted({str(p["FLD_ZONE"]) for p in touching if p.get("FLD_ZONE")})}


def _nwi(field, field_m, project, session):
    rows = _features(NWI_URL, field.bounds, "WETLAND_TYPE,ATTRIBUTE", session)
    pct, touching = _overlap(field_m, project, rows)
    return {"wetland_pct": pct, "wetland_types": sorted({str(p["WETLAND_TYPE"]) for p in touching if p.get("WETLAND_TYPE")})}


def _nhd(field, field_m, project, session):
    w, s, e, n = field.bounds
    box = (w - STREAM_SEARCH_DEG, s - STREAM_SEARCH_DEG, e + STREAM_SEARCH_DEG, n + STREAM_SEARCH_DEG)
    try:
        lines = _features(NHD_URLS[0], box, "gnis_name,ftype,fcode", session)
    except (requests.RequestException, RuntimeError, ValueError):  # the large-scale layer is the slow, flaky one
        lines = _features(NHD_URLS[1], box, "GNIS_NAME,FTYPE,FCODE", session)
    best = None
    for geom, props in lines:
        distance = to_metres(geom, project).distance(field_m)  # 0 when the stream crosses or enters the field
        if best is None or distance < best[0]:
            best = (distance, {k.lower(): v for k, v in props.items()}.get("gnis_name"))
    if best is None:
        return {"nearest_stream_m": None, "nearest_stream_name": None}
    return {"nearest_stream_m": round(best[0], 1), "nearest_stream_name": best[1] or None}


def get_water_context(field_id, polygon_lonlat, *, session=requests) -> WaterContext:
    """Flood-zone share, nearest stream and wetland share for a field polygon (shapely Polygon or [[lon, lat], ...])."""
    field = polygon_lonlat if hasattr(polygon_lonlat, "bounds") else Polygon(polygon_lonlat)
    if not field.is_valid or field.is_empty:
        raise ValueError("invalid field polygon")
    centre = field.centroid
    project = equal_area(centre.x, centre.y)
    field_m = to_metres(field, project)
    jobs = [(FEMA_LABEL, _fema), (NHD_LABEL, _nhd), (NWI_LABEL, _nwi)]

    def run(job):
        label, fn = job
        try:
            return label, fn(field, field_m, project, session), None
        except Exception as exc:  # one source down must not take the others with it
            return label, None, f"{type(exc).__name__}: {exc}"[:200]

    with ThreadPoolExecutor(len(jobs)) as pool:
        results = list(pool.map(run, jobs))
    values, sources, failures = {}, [], []
    for label, out, error in results:
        if error:
            sources.append(f"{label} (unavailable)")
            failures.append(f"{label}: {error}")
        else:
            sources.append(label)
            values.update(out)
    if len(failures) == len(jobs):
        raise ConnectionError(f"no water source answered for {field_id}: " + "; ".join(failures))
    return WaterContext(**values, sources=sources)
