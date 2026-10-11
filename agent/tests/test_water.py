"""Water context: FEMA flood zones, NHD streams, NWI wetlands. Fake ArcGIS responses; one live test is skipped in CI."""
import json
from pathlib import Path

import pytest
import requests
from shapely.geometry import box, mapping

from fieldwatch import contracts as C
from fieldwatch.context import water

# ~0.8 km x 1.1 km field in NE Nebraska
W, S, E, N = -96.548, 42.152, -96.538, 42.162
FIELD = box(W, S, E, N)
MID = (W + E) / 2


def poly(b, **props):
    return {"type": "Feature", "geometry": mapping(box(*b)), "properties": props}


def line(coords, **props):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": props}


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"HTTP {self.status}")

    def json(self):
        return self.body


class FakeSession:
    """Routes by URL; a value that is an Exception is raised, a list of bodies is consumed in order."""

    def __init__(self, fema=None, nhd=None, nwi=None, nhd_small=None):
        self.routes = {water.FEMA_URL: fema, water.NWI_URL: nwi, water.NHD_URLS[0]: nhd, water.NHD_URLS[1]: nhd_small}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        body = self.routes[url]
        if isinstance(body, Exception):
            raise body
        return Resp(collection() if body is None else body)


def zones(*features):
    return collection(*features)


def sfha(b, zone="AE"):
    return poly(b, FLD_ZONE=zone, ZONE_SUBTY=None, SFHA_TF="T")


def minimal(b):
    return poly(b, FLD_ZONE="X", ZONE_SUBTY="AREA OF MINIMAL FLOOD HAZARD", SFHA_TF="F")


def run(**kwargs):
    return water.get_water_context("f1", FIELD, session=FakeSession(**kwargs))


# ---------- FEMA ----------


def test_field_fully_inside_flood_zone():
    out = run(fema=zones(sfha((W - 0.01, S - 0.01, E + 0.01, N + 0.01))))
    assert out.floodplain_pct == pytest.approx(100.0)
    assert out.flood_zones == ["AE"]


def test_field_half_inside_flood_zone_uses_equal_area():
    out = run(fema=zones(sfha((W - 0.01, S - 0.01, MID, N + 0.01)), minimal((MID, S - 0.01, E + 0.01, N + 0.01))))
    assert out.floodplain_pct == pytest.approx(50.0, abs=1.0)
    assert out.flood_zones == ["AE", "X"]


def test_overlapping_flood_polygons_are_not_double_counted():
    out = run(fema=zones(sfha((W - 0.01, S - 0.01, MID, N + 0.01), "A"), sfha((W - 0.01, S - 0.01, MID, N + 0.01), "AE")))
    assert out.floodplain_pct == pytest.approx(50.0, abs=1.0)


def test_field_outside_flood_zones():
    out = run(fema=zones(minimal((W - 0.01, S - 0.01, E + 0.01, N + 0.01)),
                         sfha((E + 0.005, S, E + 0.02, N))))  # a flood zone that only touches the envelope
    assert out.floodplain_pct == 0
    assert out.flood_zones == ["X"]


def test_no_fema_coverage_is_zero_percent_and_no_zones():
    out = run()
    assert out.floodplain_pct == 0 and out.flood_zones == []


# ---------- streams ----------


def test_stream_crossing_the_field_is_zero_metres_and_named():
    out = run(nhd=collection(line([[W - 0.01, S + 0.005], [E + 0.01, S + 0.005]], gnis_name="Cow Creek", ftype=460)))
    assert out.nearest_stream_m == 0
    assert out.nearest_stream_name == "Cow Creek"


def test_stream_300_m_away():
    lat_offset = 300 / 111_200  # metres to degrees of latitude
    far = line([[W - 0.01, N + lat_offset], [E + 0.01, N + lat_offset]], gnis_name="Elk Creek", ftype=460)
    nearer = line([[W - 0.01, N + 4 * lat_offset], [E + 0.01, N + 4 * lat_offset]], gnis_name="Other", ftype=460)
    out = run(nhd=collection(nearer, far))
    assert out.nearest_stream_m == pytest.approx(300, rel=0.02)
    assert out.nearest_stream_name == "Elk Creek"


def test_unnamed_stream_and_no_stream():
    unnamed = run(nhd=collection(line([[W, N + 0.001], [E, N + 0.001]], gnis_name=None, ftype=460)))
    assert unnamed.nearest_stream_m > 0 and unnamed.nearest_stream_name is None
    none = run()
    assert none.nearest_stream_m is None and none.nearest_stream_name is None


def test_stream_search_envelope_is_buffered_and_bounded():
    session = FakeSession()
    water.get_water_context("f1", FIELD, session=session)
    url, params, timeout = next(c for c in session.calls if c[0] == water.NHD_URLS[0])
    west, south, east, north = map(float, params["geometry"].split(","))
    assert west < W and east > E and south < S and north > N
    assert params["f"] == "geojson" and params["outSR"] == 4326 and params["inSR"] == 4326
    assert params["resultRecordCount"] <= 500 and timeout == 20


def test_large_scale_layer_failure_falls_back_to_small_scale():
    small = collection(line([[W - 0.01, S + 0.005], [E + 0.01, S + 0.005]], GNIS_NAME="Cow Creek", FTYPE="StreamRiver"))
    out = run(nhd=requests.ReadTimeout("slow"), nhd_small=small)
    assert out.nearest_stream_m == 0 and out.nearest_stream_name == "Cow Creek"
    assert "USGS NHD streams" in out.sources


# ---------- wetlands ----------


def test_wetlands_share_and_types():
    out = run(nwi=collection(
        poly((W, S, MID, N), WETLAND_TYPE="Freshwater Emergent Wetland", ATTRIBUTE="PEM1A"),
        poly((W, S, MID, N), WETLAND_TYPE="Freshwater Pond", ATTRIBUTE="PABGx"),   # same footprint: counted once
        poly((E + 0.01, S, E + 0.02, N), WETLAND_TYPE="Riverine", ATTRIBUTE="R2UBH")))  # outside the field
    assert out.wetland_pct == pytest.approx(50.0, abs=1.0)
    assert out.wetland_types == ["Freshwater Emergent Wetland", "Freshwater Pond"]


def test_no_wetlands():
    out = run()
    assert out.wetland_pct == 0 and out.wetland_types == []


# ---------- failures ----------


def test_one_service_failing_returns_the_others():
    out = run(fema=requests.ConnectionError("FEMA down"),
              nhd=collection(line([[W, S + 0.005], [E, S + 0.005]], gnis_name="Cow Creek")),
              nwi=collection(poly((W, S, MID, N), WETLAND_TYPE="Riverine")))
    assert out.floodplain_pct is None and out.flood_zones == []  # unknown, not zero
    assert out.nearest_stream_m == 0 and out.wetland_pct == pytest.approx(50.0, abs=1.0)
    assert out.sources == ["FEMA NFHL flood zones (unavailable)", "USGS NHD streams", "USFWS NWI wetlands"]


def test_arcgis_error_body_counts_as_failure():
    out = run(nwi={"error": {"code": 500, "message": "Service unavailable"}})
    assert out.wetland_pct is None and "USFWS NWI wetlands (unavailable)" in out.sources
    assert out.floodplain_pct == 0  # the other sources still answered


def test_all_services_failing_raises_a_retryable_error():
    boom = requests.ReadTimeout("timed out")
    with pytest.raises(ConnectionError, match="no water source answered"):
        run(fema=boom, nhd=boom, nhd_small=boom, nwi=boom)


def test_polygon_as_lonlat_list_and_invalid_polygon():
    ring = [list(p) for p in FIELD.exterior.coords]
    assert water.get_water_context("f1", ring, session=FakeSession()).sources[0] == "FEMA NFHL flood zones"
    with pytest.raises(ValueError):
        water.get_water_context("f1", [[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]], session=FakeSession())


def test_equal_area_projection_preserves_area():
    c = FIELD.centroid
    area = water.to_metres(FIELD, water.equal_area(c.x, c.y)).area
    # 0.01 deg x 0.01 deg at 42.157 N: 82.5 km/deg of longitude by 111.2 km/deg of latitude
    assert area == pytest.approx(0.01 * 82_450 * 0.01 * 111_200, rel=0.01)


def test_contract_sample_matches_output_shape():
    sample = json.loads((Path(C.__file__).parents[1] / "fixtures/contracts/water_context.json").read_text())
    assert set(run().model_dump()) == set(sample)


# ---------- live ----------


@pytest.mark.live
def test_live_services_for_ne_nebraska():
    """Real FEMA / NHD / NWI answers for a field north of Thurston, NE (keyless public services)."""
    out = water.get_water_context("thurston", box(-96.548, 42.152, -96.532, 42.168))
    assert out.sources == ["FEMA NFHL flood zones", "USGS NHD streams", "USFWS NWI wetlands"]
    assert out.flood_zones and out.floodplain_pct is not None and 0 <= out.floodplain_pct <= 100
    assert out.wetland_pct is not None and out.nearest_stream_m is not None


# ---------- facade, stage and agent view ----------


def test_facade_loads_the_field_polygon(monkeypatch):
    import fieldwatch.context as context
    seen = {}
    monkeypatch.setattr(context.common, "field", lambda field_id: {"field_id": field_id, "polygon": [list(p) for p in FIELD.exterior.coords]})
    monkeypatch.setattr(water, "get_water_context", lambda field_id, polygon, **kw: seen.update(id=field_id, area=polygon.area) or "ok")
    assert context.get_water_context("f9") == "ok"
    assert seen["id"] == "f9" and seen["area"] == pytest.approx(FIELD.area)


def test_stage_retries_when_every_source_is_down(monkeypatch):
    from cloud.lambdas.context import adapters, stages
    from cloud.lambdas.context.common import TransientError
    import fieldwatch.context as context
    monkeypatch.setattr(adapters, "_registry", lambda: None)
    monkeypatch.setattr(context, "get_water_context", lambda field_id: (_ for _ in ()).throw(ConnectionError("all down")))
    with pytest.raises(TransientError):
        stages.water_handler({"field_id": "f1", "boundary": {}})


def test_water_is_part_of_the_stored_field_context():
    """get_field_context in AWS mode keeps every FieldContext attribute of the DynamoDB row, so `water` reaches the agent."""
    assert "water" in C.FieldContext.model_fields
    sample = json.loads((Path(C.__file__).parents[1] / "fixtures/contracts/field_context.json").read_text())
    row = {**sample, "water": {**sample["water"], "floodplain_pct": __import__("decimal").Decimal("62.5")}}
    kept = {k: v for k, v in row.items() if k in C.FieldContext.model_fields}
    assert C.FieldContext.model_validate(kept).water.floodplain_pct == 62.5
