"""Historical hazard evidence. MESH is millimetres; area shares are fractions.

USDM service statisticsType=1 is cumulative (D2 means D2 or worse).
We store exclusive percentages and choose the worst cumulative category covering
at least half the county. A small severe pocket alone does not label the county.
Sources: https://droughtmonitor.unl.edu/DmData/DataDownload/WebServiceInfo.aspx
https://mesonet.agron.iastate.edu/geojson/sbw.py?help
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
import requests
from fieldwatch.contracts import DroughtStatus, HailReport

RULE = 'worst category whose cumulative (category or worse) area covers at least 50% of the county; pct_by_category stores exclusive percentages'
MRMS_BUCKET = 'noaa-mrms-pds'
DAILY = 'MESH_Max_1440min_00.50'


def dates(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first: raise ValueError('end precedes start')
    while first <= last:
        yield first
        first += timedelta(days=1)


def map_date(value):
    text = str(value)
    if len(text) == 8 and text.isdigit(): return datetime.strptime(text, '%Y%m%d').date()
    return date.fromisoformat(text[:10])


def drought_status(county_fips, as_of, rows):
    cutoff = date.fromisoformat(as_of)
    usable = [(map_date(row.get('MapDate', row.get('mapDate', row.get('date', '')))), row) for row in rows]
    usable = [(day, row) for day, row in usable if day <= cutoff]
    if not usable: raise ValueError('no drought observations on or before as_of')
    day, row = max(usable, key=lambda pair: pair[0])
    lower = {key.lower(): value for key, value in row.items()}
    cumulative = [float(lower[f'd{i}']) for i in range(5)]
    if any(not 0 <= v <= 100 for v in cumulative) or any(a < b for a,b in zip(cumulative, cumulative[1:])):
        raise ValueError('invalid cumulative drought percentages')
    exclusive = {'none': 100-cumulative[0], **{f'D{i}': cumulative[i]-(cumulative[i+1] if i<4 else 0) for i in range(5)}}
    category = next((f'D{i}' for i in range(4,-1,-1) if cumulative[i] >= 50), 'none')
    return DroughtStatus(county_fips=county_fips, as_of=day.isoformat(), category=category,
                         pct_by_category=exclusive, rule=RULE)


def get_drought(county_fips, as_of, *, http_get=requests.get):
    if len(county_fips) != 5 or not county_fips.isdigit(): raise ValueError('county_fips must be five digits')
    cutoff = date.fromisoformat(as_of)
    resp = http_get('https://usdmdataservices.unl.edu/api/CountyStatistics/GetDroughtSeverityStatisticsByAreaPercent',
        params=dict(aoi=county_fips, startdate=(cutoff-timedelta(days=21)).isoformat(), enddate=as_of, statisticsType=1),
        headers={'Accept':'application/json'}, timeout=30)
    resp.raise_for_status()
    return drought_status(county_fips, as_of, resp.json())


def read_mesh(url, polygon):
    """GDAL's GRIB driver through rasterio; reads a window and excludes -1 nodata.

    all_touched includes small farm fields that contain no 0.01-degree pixel centre.
    The fraction is an approximation at MRMS grid resolution.
    """
    import numpy as np
    import rasterio
    from rasterio.features import geometry_mask, geometry_window
    from rasterio.warp import transform_geom
    from rasterio.errors import WindowError
    geometry = {'type':'Polygon', 'coordinates':[polygon+[polygon[0]] if polygon[-1] != polygon[0] else polygon]}
    with rasterio.open(url) as src:
        geom = transform_geom('EPSG:4326', src.crs, geometry)
        try: window = geometry_window(src, [geom])
        except WindowError: return None
        values = src.read(1, window=window, masked=True).astype(float).filled(float("nan"))
        mask = geometry_mask([geom], transform=src.window_transform(window), out_shape=values.shape, invert=True, all_touched=True)
        values[~mask | ~np.isfinite(values) | (values < 0)] = np.nan
        return values


def summarize_hail(field_id, start, end, grids, ground_reports=None, threshold_mm=25.4):
    import numpy as np
    peak, peak_day, combined = 0., None, None
    for day, grid in grids:
        if grid is None: continue
        grid = np.asarray(grid, dtype=float)
        grid = np.where(np.isfinite(grid) & (grid >= 0), grid, np.nan)
        if combined is not None and combined.shape != grid.shape: raise ValueError('MESH grids must align')
        combined = grid if combined is None else np.fmax(combined, grid)
        maximum = float(np.nanmax(grid)) if np.isfinite(grid).any() else 0.
        if maximum > peak: peak, peak_day = maximum, day
    valid = combined[np.isfinite(combined)] if combined is not None else np.array([])
    if not len(valid): raise ValueError('no valid MRMS pixels over the field; unavailable is not zero hail')
    return HailReport(field_id=field_id, start=start, end=end, max_hail_mm=peak, peak_date=peak_day,
        pct_area_over_threshold=float(np.count_nonzero(valid >= threshold_mm)/len(valid)),
        threshold_mm=threshold_mm, ground_reports=ground_reports or [])


def nearby_reports(polygon, reports):
    from shapely.geometry import Polygon, Point
    import math
    lat = sum(p[1] for p in polygon)/len(polygon)
    # Local equirectangular projection for a 10km proximity test to the boundary.
    project = lambda x,y,z=None: (x*111195*math.cos(math.radians(lat)), y*111195)
    field = Polygon([project(x,y) for x,y in polygon])
    result = []
    for row in reports:
        try:
            lon, latitude = float(row['Lon']), float(row['Lat'])
        except (KeyError, TypeError, ValueError): continue
        if not (-180 <= lon <= 180 and -90 <= latitude <= 90): continue
        distance = field.distance(Point(*project(lon,latitude)))
        if distance <= 10000:
            try: size = float(row['Size'])*.254
            except (KeyError, TypeError, ValueError): size = None
            result.append({**row, 'distance_m':distance, 'size_mm':size})
    return result


def spc_reports(day, *, http_get=requests.get):
    resp = http_get(f'https://www.spc.noaa.gov/climo/reports/{day:%y%m%d}_rpts_hail.csv', timeout=30)
    if resp.status_code == 404: return []
    resp.raise_for_status()
    result=[]
    for row in csv.DictReader(io.StringIO(resp.text)):
        try:
            hour = int(row['Time'][:2]); minute = int(row['Time'][2:])
            if not (0 <= hour < 24 and 0 <= minute < 60): continue
        except (KeyError, TypeError, ValueError): continue
        utc_day = day+timedelta(days=1) if hour < 12 else day
        result.append({**row, 'date':utc_day.isoformat()})
    return result


def daily_url(day):
    # A 24-hour product ending at midnight of the following UTC day.
    valid = day + timedelta(days=1)
    key = f'CONUS/{DAILY}/{valid:%Y%m%d}/MRMS_{DAILY}_{valid:%Y%m%d}-000000.grib2.gz'
    return f'/vsigzip//vsicurl/https://{MRMS_BUCKET}.s3.amazonaws.com/{key}'


def get_hail(field_id, start, end, *, polygon=None, mesh_reader=read_mesh, report_reader=spc_reports):
    if polygon is None:
        from .tools import field_polygon
        polygon = field_polygon(field_id)
    days = list(dates(start,end))
    grids = [(day.isoformat(), mesh_reader(daily_url(day),polygon)) for day in days]
    reports = [row for day in [days[0]-timedelta(days=1),*days] for row in report_reader(day) if start <= row.get('date',day.isoformat()) <= end]
    return summarize_hail(field_id,start,end,grids,nearby_reports(polygon,reports))


def get_warnings(polygon, start, end, *, http_get=requests.get):
    from shapely.geometry import shape, Polygon
    list(dates(start,end))
    resp = http_get('https://mesonet.agron.iastate.edu/geojson/sbw.geojson',
        params=dict(sts=start+'T00:00:00Z', ets=(date.fromisoformat(end)+timedelta(days=1)).isoformat()+'T00:00:00Z'), timeout=30)
    resp.raise_for_status()
    field = Polygon(polygon)
    return [feature for feature in resp.json()['features'] if feature.get('geometry') and shape(feature['geometry']).intersects(field)]
