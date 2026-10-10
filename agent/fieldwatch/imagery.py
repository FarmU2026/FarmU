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
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"
BANDS = {"red": "B04.tif", "green": "B03.tif", "blue": "B02.tif", "nir": "B08.tif"}
RENDER_PX = 512

BBox = tuple[float, float, float, float]  # west, south, east, north in degrees


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


def _read_band(href: str, bbox: BBox) -> np.ndarray:
    with rasterio.open(href) as src:
        b = transform_bounds("EPSG:4326", src.crs, *bbox)
        win = from_bounds(*b, src.transform)
        return src.read(1, window=win).astype("float32")


def _stretch(x: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(x, 2), np.percentile(x, 98)
    return np.clip((x - lo) / max(hi - lo, 1e-6), 0, 1)


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).resize((RENDER_PX, RENDER_PX), Image.NEAREST).save(buf, format="PNG")
    return buf.getvalue()


def render_field(bbox: BBox, scene: Scene) -> FieldView:
    """Read the field window from one scene and render the two images plus summary numbers."""
    bands = {k: _read_band(scene.href_base + f, bbox) for k, f in BANDS.items()}
    r, g, b, n = (bands[k] for k in ("red", "green", "blue", "nir"))

    rgb = (np.dstack([_stretch(r), _stretch(g), _stretch(b)]) * 255).astype("uint8")

    ndvi = (n - r) / (n + r + 1e-6)
    t = np.clip((ndvi + 0.1) / 0.9, 0, 1)  # -0.1 (bare) .. 0.8 (lush)
    heat = np.dstack([(1 - t) * 180 + 40, t * 160 + 60, np.full_like(t, 40)]).astype("uint8")

    # Where is the weak spot? Split into a 3x3 grid and name the lowest cell.
    h, w = ndvi.shape
    cells = {}
    rows, cols = ("north", "middle", "south"), ("west", "center", "east")
    for i, rn in enumerate(rows):
        for j, cn in enumerate(cols):
            cell = ndvi[i * h // 3 : (i + 1) * h // 3, j * w // 3 : (j + 1) * w // 3]
            cells[f"{rn}-{cn}"] = float(np.median(cell)) if cell.size else float("nan")
    weakest = min(cells, key=cells.get)

    stats = {
        "scene_date": scene.date.isoformat(),
        "cloud_cover_pct": round(scene.cloud_cover, 1),
        "pixels": [int(h), int(w)],
        "health_index_median": round(float(np.median(ndvi)), 2),
        "health_index_low_10pct": round(float(np.percentile(ndvi, 10)), 2),
        "health_index_high_90pct": round(float(np.percentile(ndvi, 90)), 2),
        "share_of_field_below_0_3": round(float((ndvi < 0.3).mean()), 2),
        "weakest_area": weakest,
        "grid_medians": {k: round(v, 2) for k, v in cells.items()},
    }
    return FieldView(scene=scene, truecolor_png=_png(rgb), health_png=_png(heat), stats=stats)


def look(bbox: BBox) -> FieldView:
    scenes = latest_scenes(bbox)
    if not scenes:
        raise LookupError("no cloud-free Sentinel-2 scene over this field in the last 60 days")
    return render_field(bbox, scenes[0])
