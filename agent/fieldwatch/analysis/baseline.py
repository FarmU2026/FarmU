"""build_baseline(): the per-pixel prior-season baseline for one field (context builder's baseline stage).

Same weeks (+-SEASON_WINDOW_DAYS) of the 2-3 prior seasons (D7), using the analyzer's grid, loader and index
code. The result is one compressed .npz stored via an injected writer (`NullWriter` when no bucket,
`S3Writer` otherwise; no boto3 here) at the stable key `context/{field_id}/baseline.npz`. It needs only numpy
(the geo image has no pyarrow/pandas). Contents (np.load(..., allow_pickle=False)):

    ndvi, ndre, ndmi   float32 (H, W): mean over the usable seasons, NaN where no season was clear
    ndvi_seasons       float32 (S, H, W): per-season NDVI, same order as `seasons`
    inside             bool (H, W): buffered field mask
    meta               unicode scalar holding JSON: field_id, crop, as_of, seasons, scenes{year: {scene_id,
                       date}}, grid{shape, res_m, crs, transform(6 affine coeffs), bounds_lonlat}, tile_ids
"""
from __future__ import annotations

import io
import json
import logging
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np

from .. import contracts as C
from .. import interfaces
from . import analyzer
from .indices import compute_indices
from .loader import SceneLoader, StacLoader
from .regions import baseline_mean
from .s3_writer import S3Writer
from .writer import NullWriter, OverlayWriter

log = logging.getLogger(__name__)

PRIOR_SEASONS = 3
MIN_CLEAR_FRACTION = 0.5
INTERIOR_BUFFER_M = 10.0


def baseline_key(field_id: str) -> str:
    return f"context/{field_id}/baseline.npz"


def _season(ctx, as_of: date, years_back: int):
    """(year, IndexStack) for the clearest scene near the same date `years_back` ago, or None."""
    target = analyzer._years_ago(as_of, years_back)
    found = ctx.loader.search(ctx.bbox, target - timedelta(days=analyzer.SEASON_WINDOW_DAYS),
                              target + timedelta(days=analyzer.SEASON_WINDOW_DAYS),
                              max_cloud=analyzer.CANDIDATE_MAX_CLOUD, limit=10)
    found.sort(key=lambda s: abs((analyzer._scene_date(s) - target).days))
    hit, _ = ctx.first_clear(found, MIN_CLEAR_FRACTION)
    return (target.year, hit[1]) if hit else None


def build_baseline(*, field_id: str, polygon: list[list[float]], tile_ids: list[str], crop: str,
                   bucket: str | None, as_of: date | str | None = None, loader: SceneLoader | None = None,
                   writer: OverlayWriter | None = None, seasons: int = PRIOR_SEASONS) -> C.FieldBaseline:
    """Compute and store the baseline. Raises on real failures (loader/network/writer errors)."""
    if len(polygon) < 3:
        raise ValueError(f"baseline {field_id}: polygon needs at least 3 lon/lat points")
    if writer is None:
        writer = S3Writer(bucket) if bucket else NullWriter()
    loader = loader or StacLoader()
    ref = date.fromisoformat(as_of) if isinstance(as_of, str) else (as_of or date.today())

    grid, inside = analyzer.grid_for_polygon(polygon)
    ctx = analyzer._Ctx(loader, grid, inside, SimpleNamespace(polygon=polygon, interior_buffer_m=INTERIOR_BUFFER_M))
    found = [r for r in (_season(ctx, ref, n) for n in range(1, seasons + 1)) if r is not None]
    cohort = list(interfaces.context().cached_cohort_field_ids(field_id))
    if not found:
        log.warning("baseline %s: no clear prior-season scene near %s", field_id, ref)
        return C.FieldBaseline(key=None, seasons=[], cohort_field_ids=cohort, stub=False)

    stacks = [st for _, st in found]
    arrays = {
        "ndvi": baseline_mean([s.ndvi for s in stacks]),
        "ndre": baseline_mean([s.ndre for s in stacks]),
        "ndmi": baseline_mean([s.ndmi for s in stacks]),
        "ndvi_seasons": np.stack([s.ndvi for s in stacks]),
        "inside": stacks[0].inside,
    }
    years = [y for y, _ in found]
    meta = {
        "field_id": field_id, "crop": crop, "as_of": ref.isoformat(), "seasons": years, "tile_ids": list(tile_ids),
        "scenes": {str(y): {"scene_id": s.scene_id, "date": s.date.isoformat()} for y, s in found},
        "grid": {"shape": list(grid.shape), "res_m": grid.res_m, "crs": grid.crs,
                 "transform": list(grid.transform)[:6], "bounds_lonlat": list(grid.bounds_lonlat())},
    }
    buf = io.BytesIO()
    np.savez_compressed(buf, meta=np.array(json.dumps(meta)), **arrays)
    key = baseline_key(field_id)
    writer.put(key, buf.getvalue(), "application/octet-stream")
    return C.FieldBaseline(key=key, seasons=years, cohort_field_ids=cohort, stub=False)


def load_baseline(data: bytes) -> tuple[dict, dict[str, np.ndarray]]:
    """Read back what `build_baseline` stored: (meta dict, arrays)."""
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        meta = json.loads(str(z["meta"]))
        return meta, {k: z[k] for k in z.files if k != "meta"}
