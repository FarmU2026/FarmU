"""build_baseline() with a fake loader and writer, plus the analyze-module import seam. No network."""
import importlib
import subprocess
import sys
from datetime import date

import pytest

from fieldwatch import interfaces
from fieldwatch.analysis import baseline as bl
from tests.test_analysis_pipeline import POLY, FakeLoader, MemWriter

AS_OF = date(2026, 7, 18)
SCENES = {date(2025, 7, 16): 0.0, date(2024, 7, 20): 0.0, date(2023, 7, 10): 0.0}


class FakeCtx:
    def __init__(self, ids):
        self.ids = ids

    def cached_cohort_field_ids(self, field_id):
        return list(self.ids)


def run(monkeypatch, scenes=SCENES, cohort=("a", "b"), **kw):
    monkeypatch.setattr(interfaces, "context", lambda: FakeCtx(cohort))
    w = MemWriter()
    out = bl.build_baseline(field_id="f1", polygon=POLY, tile_ids=["14TPM"], crop="corn", bucket=None,
                            as_of=AS_OF, loader=FakeLoader(scenes), writer=w, **kw)
    return out, w


def test_baseline_stores_npz_and_returns_contract(monkeypatch):
    out, w = run(monkeypatch)
    assert out.key == "context/f1/baseline.npz" and out.stub is False
    assert out.seasons == [2025, 2024, 2023]
    assert out.cohort_field_ids == ["a", "b"]
    meta, arr = bl.load_baseline(w.files[out.key])
    assert meta["seasons"] == [2025, 2024, 2023] and meta["grid"]["crs"].startswith("EPSG:326")
    assert arr["ndvi"].shape == tuple(meta["grid"]["shape"]) and arr["ndvi_seasons"].shape[0] == 3
    assert 0.7 < float(__import__("numpy").nanmean(arr["ndvi"])) < 0.9


def test_cloudy_season_skipped(monkeypatch):
    out, _ = run(monkeypatch, scenes={date(2025, 7, 16): 0.0, date(2024, 7, 20): 0.9})
    assert out.seasons == [2025]


def test_no_scenes_keeps_cohort_and_writes_nothing(monkeypatch):
    out, w = run(monkeypatch, scenes={})
    assert out.key is None and out.seasons == [] and out.cohort_field_ids == ["a", "b"] and not w.files


def test_null_writer_when_no_bucket(monkeypatch):
    monkeypatch.setattr(interfaces, "context", lambda: FakeCtx([]))
    out = bl.build_baseline(field_id="f1", polygon=POLY, tile_ids=[], crop="corn", bucket=None, as_of=AS_OF,
                            loader=FakeLoader(SCENES))
    assert out.key == "context/f1/baseline.npz"


def test_loader_failure_raises(monkeypatch):
    class Boom(FakeLoader):
        def search(self, *a, **k):
            raise OSError("down")

    monkeypatch.setattr(interfaces, "context", lambda: FakeCtx([]))
    with pytest.raises(OSError):
        bl.build_baseline(field_id="f1", polygon=POLY, tile_ids=[], crop="corn", bucket=None, as_of=AS_OF,
                          loader=Boom({}), writer=MemWriter())


def test_interfaces_see_both_callables():
    api = interfaces.analysis()
    assert api is not None and callable(api.analyze) and callable(api.build_baseline)


@pytest.mark.parametrize("order", [
    "import fieldwatch.analysis.analyze; import fieldwatch.analysis as p",
    "import fieldwatch.analysis as p; import fieldwatch.analysis.analyze",
    "import fieldwatch.analysis as p; import importlib; importlib.import_module('fieldwatch.analysis.analyze')",
    "from fieldwatch.analysis.analyze import analyze; import fieldwatch.analysis as p",
    "from fieldwatch import interfaces; interfaces.analysis(); import fieldwatch.analysis as p",
])
def test_package_attribute_is_function_in_every_import_order(order):
    code = (f"{order}\nimport types\nfrom fieldwatch.analysis import analyze\nfrom fieldwatch import interfaces\n"
            "assert isinstance(p.analyze, types.FunctionType), p.analyze\n"
            "assert isinstance(analyze, types.FunctionType)\n"
            "assert interfaces.analysis().analyze is analyze\n"
            "import fieldwatch.analysis.analyzer as m; assert m.analyze is analyze\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_registry_analyze_field_unaffected():
    importlib.import_module("fieldwatch.analysis.analyze")
    from fieldwatch.analysis import analyze
    assert callable(analyze) and not hasattr(analyze, "__path__")


@pytest.mark.live
def test_live_baseline():
    out = bl.build_baseline(field_id="live-1", polygon=POLY, tile_ids=["14TPM"], crop="corn", bucket=None,
                            as_of=date(2026, 7, 18))
    assert out.stub is False and out.seasons
