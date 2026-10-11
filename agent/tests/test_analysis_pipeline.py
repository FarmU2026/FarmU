"""analyze(): loader + writer wiring with a fake loader. No network."""
import os
from datetime import date

import numpy as np
import pytest

from fieldwatch import contracts as C
from fieldwatch.analysis import analyze
from fieldwatch.analysis.grid import SceneData
from fieldwatch.analysis.writer import LocalWriter, NullWriter

POLY = [(-96.548, 42.152), (-96.532, 42.152), (-96.532, 42.168), (-96.548, 42.168)]


class MemWriter:
    def __init__(self):
        self.files = {}

    def put(self, key, data, content_type="image/png"):
        self.files[key] = data


class FakeLoader:
    """Scenes keyed by date -> cloud fraction; optional ring problem on one date."""

    def __init__(self, scenes, problem_on=None):
        self.scenes = scenes
        self.problem_on = problem_on

    def search(self, bbox, start, end, max_cloud=100.0, limit=20):
        out = []
        for d, cl in sorted(self.scenes.items(), reverse=True):
            if start <= d <= end:
                out.append(C.SceneInfo(scene_id=f"S2_{d:%Y%m%d}", datetime=f"{d.isoformat()}T17:30:00Z",
                                       tile_id="14TPM", cloud_cover=cl * 100))
        return out[:limit]

    def read(self, scene, grid):
        d = date.fromisoformat(scene.datetime[:10])
        h, w = grid.shape
        yy, xx = np.mgrid[0:h, 0:w]
        rng = np.random.default_rng(d.toordinal())
        tex = np.sin(yy / 17.0) * 0.015 + np.cos(xx / 23.0) * 0.015
        ndvi = 0.82 + tex + rng.normal(0, 0.01, (h, w))
        if self.problem_on == d:
            r = np.hypot(yy - h / 2, xx - w / 2)
            ndvi = np.where((r > 0.30 * h) & (r < 0.36 * h), ndvi - 0.28, ndvi)
        nir = np.full((h, w), 0.4, "float32")
        red = (nir * (1 - ndvi) / (1 + ndvi)).astype("float32")
        scl = np.full((h, w), 4, np.uint8)
        cl = self.scenes[d]
        if cl:
            scl[: int(h * cl)] = 9
        return SceneData(scene.scene_id, d, grid, red=red, nir=nir, rededge=nir * 0.7, swir=nir * 0.8, scl=scl,
                         green=red * 1.4, blue=red * 0.7, tile_id="14TPM")


def req(as_of="2026-07-18", **kw):
    base = dict(field=C.FieldRef(farm_id="f", field_id="north-pivot"), polygon=POLY, crop="corn", as_of=as_of,
                pivot={"center": [-96.54, 42.16], "radius_m": 800})
    base.update(kw)
    return C.FieldAnalysisRequest(**base)


def scenes(extra=None):
    s = {date(y, 7, 15): 0.0 for y in (2023, 2024, 2025)}
    s[date(2026, 7, 13)] = 0.0
    s[date(2026, 7, 18)] = 0.0
    s.update(extra or {})
    return s


def test_ring_problem_end_to_end():
    w = MemWriter()
    res = analyze(req(), loader=FakeLoader(scenes(), problem_on=date(2026, 7, 18)), writer=w)
    C.FieldAnalysisResult.model_validate(res.model_dump())
    assert res.status == "ok" and res.date == "2026-07-18" and res.scene_id == "S2_20260718"
    assert res.clear_fraction > 0.95
    assert res.anomaly.regions and res.anomaly.regions[0].features.ring_score > 0.5
    assert res.anomaly.delta_since_last is not None  # median change: a 12% ring barely moves it
    assert res.analysis_key == "analysis/north-pivot/2026-07-18/s2/"
    kinds = ("true_color", "health", "mask", "departure", "change", "normal")
    assert set(w.files) == {getattr(res.overlays, k).key for k in kinds}
    assert all(v[:8] == b"\x89PNG\r\n\x1a\n" for v in w.files.values())
    assert res.overlays.health.bounds[0] < -96.54 < res.overlays.health.bounds[2]


def test_healthy_end_to_end_is_quiet():
    res = analyze(req(), loader=FakeLoader(scenes()), writer=NullWriter())
    assert res.status == "ok" and res.anomaly.regions == []


def test_cloudy_latest_scene_falls_back_to_previous_clear():
    res = analyze(req(), loader=FakeLoader(scenes({date(2026, 7, 18): 0.6, date(2026, 7, 16): 0.0})), writer=NullWriter())
    assert res.status == "ok" and res.date == "2026-07-16"


def test_too_cloudy():
    sc = {date(2026, 7, 18): 0.9, date(2026, 7, 15): 0.8}
    res = analyze(req(), loader=FakeLoader(sc), writer=NullWriter())
    assert res.status == "too_cloudy" and res.indices is None and res.clear_fraction < 0.7
    C.FieldAnalysisResult.model_validate(res.model_dump())


def test_no_baseline_when_no_prior_seasons():
    sc = {date(2026, 7, 13): 0.0, date(2026, 7, 18): 0.0}
    res = analyze(req(), loader=FakeLoader(sc), writer=NullWriter())
    assert res.status == "no_baseline" and res.indices is not None


def test_no_scene_is_error():
    res = analyze(req(), loader=FakeLoader({}), writer=NullWriter())
    assert res.status == "error" and res.error.code == "NO_SCENE"


def test_senescence_gate_in_pipeline():
    sc = {date(y, 9, 27): 0.0 for y in (2023, 2024, 2025)}
    sc.update({date(2026, 9, 22): 0.0, date(2026, 9, 28): 0.0})
    res = analyze(req(as_of="2026-09-29"), loader=FakeLoader(sc, problem_on=date(2026, 9, 28)), writer=NullWriter())
    assert res.status == "ok" and res.gates.senescence is True


def test_loader_failure_becomes_error_result():
    class Boom(FakeLoader):
        def search(self, *a, **k):
            raise ConnectionError("down")

    res = analyze(req(), loader=Boom({}), writer=NullWriter())
    assert res.status == "error" and res.error.retryable is True


def test_local_writer(tmp_path):
    LocalWriter(tmp_path).put("analysis/x/y.png", b"abc")
    assert (tmp_path / "analysis/x/y.png").read_bytes() == b"abc"


def test_s3_writer_is_lazy_and_puts():
    from fieldwatch.analysis import s3_writer

    class Client:
        calls = []

        def put_object(self, **kw):
            self.calls.append(kw)

    c = Client()
    s3_writer.S3Writer("bkt", client=c).put("analysis/a.png", b"x")
    assert c.calls == [dict(Bucket="bkt", Key="analysis/a.png", Body=b"x", ContentType="image/png")]


@pytest.mark.live
def test_live_summer_field():
    """Real Earth Search + COG reads for the demo field on a summer date (network, ~1 min)."""
    if os.getenv("FIELDWATCH_LIVE") != "1":
        pytest.skip("set FIELDWATCH_LIVE=1")
    res = analyze(req(as_of="2025-08-22"), writer=NullWriter())
    C.FieldAnalysisResult.model_validate(res.model_dump())
    assert res.status in ("ok", "no_baseline"), res.error
    assert res.date <= "2025-08-22" and res.clear_fraction >= 0.7
    assert 0.3 < res.indices.ndvi.p50 < 1.0
    assert res.gates.senescence is False


# ---------- overlays reach S3 only from the scene analyze Lambda ----------

def _fake_loader_for_registry(monkeypatch):
    import fieldwatch.analysis.analyzer as az
    monkeypatch.setattr(az, "StacLoader", lambda *a, **k: FakeLoader(scenes(), problem_on=date(2026, 7, 18)))


def _registry_request():
    return req().model_dump(mode="json")


def test_scene_analyze_handler_writes_overlays_to_s3(monkeypatch):
    import boto3
    from moto import mock_aws
    from cloud.lambdas.scenes import analyze as scene_analyze
    _fake_loader_for_registry(monkeypatch)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("FIELDWATCH_BUCKET", "fw-test")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.create_bucket(Bucket="fw-test", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        out = scene_analyze.handler(_registry_request())
        for kind in ("true_color", "health", "mask", "departure", "change", "normal"):
            key = out["overlays"][kind]["key"]
            assert key.startswith("analysis/") and key.endswith(f"/s2/{kind}.png")
            obj = s3.get_object(Bucket="fw-test", Key=key)
            assert obj["ContentType"] == "image/png" and obj["Body"].read()[:4] == b"\x89PNG"


def test_scene_analyze_handler_writes_nothing_without_bucket(monkeypatch):
    from cloud.lambdas.scenes import analyze as scene_analyze
    _fake_loader_for_registry(monkeypatch)
    monkeypatch.delenv("FIELDWATCH_BUCKET", raising=False)
    puts = []
    monkeypatch.setattr("fieldwatch.analysis.analyzer.NullWriter.put", lambda self, *a, **k: puts.append(a[0]))
    out = scene_analyze.handler(_registry_request())
    assert out["overlays"]["health"]["key"] and len(puts) == 6  # went to NullWriter, no S3 client was built


def test_registry_path_outside_scene_lambda_is_null_writer(monkeypatch):
    from fieldwatch import registry
    from fieldwatch.analysis.writer import NullWriter, current_writer
    monkeypatch.setenv("FIELDWATCH_BUCKET", "fw-test")  # the Gateway Lambda has this too; it must not matter
    assert isinstance(current_writer(), NullWriter)
    seen = {}
    import fieldwatch.analysis as an
    monkeypatch.setattr(an, "analyze", lambda r, writer=None, **k: seen.setdefault("w", writer) or C.FieldAnalysisResult)
    try:
        registry.analyze_field(**_registry_request())
    except Exception:
        pass
    assert isinstance(seen["w"], NullWriter)
