"""Live Sentinel-2 imagery for a field, from the free Earth Search catalog on AWS. No account needed.

Flow: STAC search (latest cloud-free scene over the field) -> windowed read of four 10 m bands
straight from the cloud-optimized GeoTIFFs -> two PNGs (true color, crop-health heatmap) plus
numbers. The PNGs go to a vision model as tool-result images; the numbers keep it honest.

Sentinel-2 revisits every 2-5 days and scenes land here within about a day, so "latest" means
the most recent clear pass, not this minute.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import rasterio
from PIL import Image
from pystac_client import Client
from rasterio.features import geometry_mask
from rasterio.warp import transform_bounds, transform_geom
from rasterio.windows import from_bounds

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"
BANDS = {"red": "B04.tif", "green": "B03.tif", "blue": "B02.tif", "nir": "B08.tif"}
RENDER_PX = 512

BBox = tuple[float, float, float, float]  # west, south, east, north in degrees
Polygon = list[tuple[float, float]]  # [(lon, lat), ...] ring, first point need not repeat


def polygon_bbox(poly: Polygon, pad: float = 0.0005) -> BBox:
    lons = [p[0] for p in poly]
    lats = [p[1] for p in poly]
    return (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)


@dataclass
class Scene:
    id: str
    date: date
    cloud_cover: float
    href_base: str  # folder holding B02.tif ... B08.tif


@dataclass
class FieldView:
    scene: Scene
    truecolor_png: bytes
    health_png: bytes
    stats: dict


def latest_scenes(bbox: BBox, days_back: int = 60, max_cloud: float = 20, limit: int = 5) -> list[Scene]:
    """Most recent clear scenes covering the box, newest first."""
    start = (date.today() - timedelta(days=days_back)).isoformat()
    cat = Client.open(STAC_URL)
    search = cat.search(
        collections=[COLLECTION],
        bbox=list(bbox),
        datetime=f"{start}/{date.today().isoformat()}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
        sortby=[{"field": "properties.datetime", "direction": "desc"}],
        max_items=limit,
    )
    out = []
    for item in search.items():
        red = item.assets["red"].href
        out.append(
            Scene(
                id=item.id,
                date=item.datetime.date(),
                cloud_cover=float(item.properties.get("eo:cloud_cover", 0)),
                href_base=red.rsplit("/", 1)[0] + "/",
            )
        )
    return out


def _read_band(href: str, bbox: BBox, poly: Polygon | None = None):
    """Band window as float32, plus a boolean mask that is True inside the field (or everywhere)."""
    with rasterio.open(href) as src:
        b = transform_bounds("EPSG:4326", src.crs, *bbox)
        win = from_bounds(*b, src.transform)
        arr = src.read(1, window=win).astype("float32")
        inside = np.ones(arr.shape, dtype=bool)
        if poly:
            ring = list(poly) + [poly[0]]
            geom = transform_geom("EPSG:4326", src.crs, {"type": "Polygon", "coordinates": [ring]})
            inside = ~geometry_mask([geom], out_shape=arr.shape, transform=src.window_transform(win))
        return arr, inside


def _stretch(x: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(x, 2), np.percentile(x, 98)
    return np.clip((x - lo) / max(hi - lo, 1e-6), 0, 1)


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).resize((RENDER_PX, RENDER_PX), Image.NEAREST).save(buf, format="PNG")
    return buf.getvalue()


def render_field(bbox: BBox, scene: Scene, poly: Polygon | None = None) -> FieldView:
    """Read the field window from one scene and render the two images plus summary numbers.

    With a polygon, pixels outside the field are dimmed in the pictures and excluded from the numbers.
    """
    bands = {k: _read_band(scene.href_base + f, bbox, poly) for k, f in BANDS.items()}
    (r, inside), (g, _), (b, _), (n, _) = (bands[k] for k in ("red", "green", "blue", "nir"))
    if not inside.any():
        inside = np.ones_like(inside)

    rgb = (np.dstack([_stretch(r), _stretch(g), _stretch(b)]) * 255).astype("uint8")
    rgb[~inside] = (rgb[~inside] * 0.35).astype("uint8")

    ndvi = (n - r) / (n + r + 1e-6)
    t = np.clip((ndvi + 0.1) / 0.9, 0, 1)  # -0.1 (bare) .. 0.8 (lush)
    heat = np.dstack([(1 - t) * 180 + 40, t * 160 + 60, np.full_like(t, 40)]).astype("uint8")
    heat[~inside] = 60

    # Where is the weak spot? Split into a 3x3 grid and name the lowest cell (inside the field only).
    h, w = ndvi.shape
    cells = {}
    rows, cols = ("north", "middle", "south"), ("west", "center", "east")
    for i, rn in enumerate(rows):
        for j, cn in enumerate(cols):
            sl = (slice(i * h // 3, (i + 1) * h // 3), slice(j * w // 3, (j + 1) * w // 3))
            vals = ndvi[sl][inside[sl]]
            if vals.size:
                cells[f"{rn}-{cn}"] = float(np.median(vals))
    weakest = min(cells, key=cells.get) if cells else "unknown"
    field = ndvi[inside]

    stats = {
        "scene_date": scene.date.isoformat(),
        "cloud_cover_pct": round(scene.cloud_cover, 1),
        "pixels": [int(h), int(w)],
        "field_pixels": int(inside.sum()),
        "health_index_median": round(float(np.median(field)), 2),
        "health_index_low_10pct": round(float(np.percentile(field, 10)), 2),
        "health_index_high_90pct": round(float(np.percentile(field, 90)), 2),
        "share_of_field_below_0_3": round(float((field < 0.3).mean()), 2),
        "weakest_area": weakest,
        "grid_medians": {k: round(v, 2) for k, v in cells.items()},
    }
    return FieldView(scene=scene, truecolor_png=_png(rgb), health_png=_png(heat), stats=stats)


def look(bbox: BBox, poly: Polygon | None = None, newer_than: str | None = None) -> FieldView | None:
    """Latest clear view of the field. With newer_than (ISO date), returns None unless a newer scene exists."""
    scenes = latest_scenes(bbox)
    if not scenes:
        raise LookupError("no cloud-free Sentinel-2 scene over this field in the last 60 days")
    if newer_than and scenes[0].date.isoformat() <= newer_than:
        return None
    return render_field(bbox, scenes[0], poly)
