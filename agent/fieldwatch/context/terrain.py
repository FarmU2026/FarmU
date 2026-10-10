"""Windowed 1 m USGS 3DEP elevation, slope and local depression mask."""
import numpy as np
import requests
from rasterio import open as raster_open
from rasterio.features import geometry_mask
from rasterio.io import MemoryFile
from rasterio.windows import from_bounds, Window
from rasterio.warp import transform_bounds,transform_geom
from shapely.geometry import mapping
from fieldwatch.contracts import TerrainSummary
from .common import write_artifact
TNM_URL="https://tnmaccess.nationalmap.gov/api/v1/products"

def analyze_dem(dem, field_mask, xres, yres, depression_m=0.15):
    dem=np.asarray(dem,dtype=float)
    valid=field_mask & np.isfinite(dem)
    if not valid.any(): raise LookupError("no valid elevation inside field")
    # Priority-flood depression filling covers broad, flat basins as well as point pits.
    # Raster edges and cells bordering nodata are drainage outlets, not walls.
    import heapq
    finite=np.isfinite(dem)
    visited=~finite
    filled=dem.copy()
    queue=[]
    height,width=dem.shape
    offsets=[(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
    for y,x in zip(*np.where(finite)):
        outlet=y in (0,height-1) or x in (0,width-1)
        if not outlet:
            outlet=any(not finite[y+dy,x+dx] for dy,dx in offsets)
        if outlet:
            visited[y,x]=True
            heapq.heappush(queue,(dem[y,x],y,x))
    while queue:
        level,y,x=heapq.heappop(queue)
        for dy,dx in offsets:
            ny,nx=y+dy,x+dx
            if not (0<=ny<height and 0<=nx<width) or visited[ny,nx]: continue
            visited[ny,nx]=True
            filled[ny,nx]=max(dem[ny,nx],level)
            heapq.heappush(queue,(filled[ny,nx],ny,nx))
    low=valid & ((filled-dem)>=depression_m)
    gy,gx=np.gradient(dem,yres,xres)
    slopes=100*np.hypot(gx,gy)
    slope_valid=valid & np.isfinite(slopes)
    if not slope_valid.any(): raise LookupError("insufficient valid elevation for slope")
    return TerrainSummary(slope_mean_pct=float(np.mean(slopes[slope_valid])),
        lowspot_pct=100*float(low.sum())/float(valid.sum())),low

def get_terrain(field_id, polygon, session=requests):
    response=session.get(TNM_URL,params={"datasets":"Digital Elevation Model (DEM) 1 meter",
        "bbox":",".join(map(str,polygon.bounds)),"prodFormats":"GeoTIFF","max":100},timeout=60)
    response.raise_for_status()
    tiles=[]
    w,s,e,n=polygon.bounds
    for item in response.json().get("items",[]):
        b=item.get("boundingBox",{})
        if b and float(b["minX"])<=w and float(b["maxX"])>=e and float(b["minY"])<=s and float(b["maxY"])>=n:
            tiles.append(item)
    if not tiles: raise LookupError("no single 3DEP 1 m tile contains this field")
    href=sorted(tiles,key=lambda i:i.get("dateCreated",""),reverse=True)[0]["downloadURL"]
    with raster_open(href) as ds:
        if not ds.crs or ds.crs.is_geographic: raise ValueError("3DEP elevation requires projected meter grid")
        bounds=transform_bounds("EPSG:4326",ds.crs,*polygon.bounds)
        bounds=(bounds[0]-30,bounds[1]-30,bounds[2]+30,bounds[3]+30)
        win=from_bounds(*bounds,transform=ds.transform).round_offsets().round_lengths()
        win=win.intersection(Window(0,0,ds.width,ds.height))
        if win.width*win.height>4_000_000: raise ValueError("field elevation window exceeds memory limit")
        dem=ds.read(1,window=win,masked=True).astype("float64").filled(np.nan)
        tr=ds.window_transform(win)
        geom=transform_geom("EPSG:4326",ds.crs,mapping(polygon))
        mask=geometry_mask([geom],dem.shape,tr,invert=True)
        summary,low=analyze_dem(dem,mask,abs(tr.a),abs(tr.e))
        with MemoryFile() as mem:
            with mem.open(driver="GTiff",height=dem.shape[0],width=dem.shape[1],count=1,
                dtype="uint8",crs=ds.crs,transform=tr,nodata=255,compress="deflate") as out:
                out.write(np.where(mask,low.astype("uint8"),255).astype("uint8"),1)
            key=write_artifact(f"context/{field_id}/lowspot.tif",mem.read(),"image/tiff")
    return summary.model_copy(update={"lowspot_key":key})
