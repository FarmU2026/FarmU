"""A scene pinned by the scene pipeline (assets keyed by STAC asset name) must load like one from our own search."""
import numpy as np

from fieldwatch import contracts as C
from fieldwatch.analysis import loader
from fieldwatch.analysis.analyzer import grid_for_polygon

POLY = [[-96.548, 42.168], [-96.532, 42.168], [-96.532, 42.152], [-96.548, 42.152], [-96.548, 42.168]]


def _scene(names):
    return C.SceneInfo(scene_id="S2A_T14TPM_20260717T172723_L2A", source="sentinel-2-c1-l2a",
                       datetime="2026-07-17T17:27:23Z", tile_id="14TPM", cloud_cover=0.0,
                       assets={n: f"https://example.test/{n}.tif" for n in names})


def _read(monkeypatch, names):
    read = []

    def fake(href, grid, scale, offset):
        read.append(href.rsplit("/", 1)[1])
        return np.ones(grid.shape, dtype="uint8" if scale is None else "float32")

    monkeypatch.setattr(loader, "_read_band", fake)
    grid, _ = grid_for_polygon(POLY)
    return loader.StacLoader().read(_scene(names), grid), read


def test_stac_asset_names_load(monkeypatch):
    data, read = _read(monkeypatch, ["blue", "green", "red", "rededge1", "nir", "nir08", "swir16", "scl"])
    assert data.rededge is not None and data.swir is not None
    assert "nir08" not in read  # not a band the analyzer uses


def test_band_names_still_load(monkeypatch):
    data, _ = _read(monkeypatch, list(loader.BAND_ASSETS))
    assert data.rededge is not None and data.swir is not None
