import numpy as np
import pytest
from shapely.geometry import box, mapping
from fieldwatch.context.soil import summarize_horizons
from fieldwatch.context.terrain import analyze_dem
from fieldwatch.context.boundaries import match_fields
from fieldwatch.context.disease import parse_reports
from fieldwatch.context.economics import economics

def test_horizons_depth_overlap_and_sandy():
    rows = [{"top":0,"bottom":20,"awc":0.1,"texture":"sand","sand":90},
            {"top":20,"bottom":120,"awc":0.2,"texture":"loam","sand":40}]
    out = summarize_horizons(rows)
    assert out["awc_0_100_cm"] == pytest.approx(18)
    assert out["texture_0_30"] == "sand"
    assert out["texture_30_100"] == "loam"
    assert out["sandy"] is False

def test_dem_flat_and_depression():
    flat = np.ones((7,7)) * 10
    summary, mask = analyze_dem(flat, np.ones_like(flat,dtype=bool),1,1)
    assert summary.lowspot_pct == 0
    flat[3,3] = 9
    summary, mask = analyze_dem(flat, np.ones_like(flat,dtype=bool),1,1)
    assert mask[3,3] and summary.lowspot_pct > 0

def test_csb_overlap_history_and_neighbors_as_of():
    field = box(0,0,100,100)
    features = [
        {"geometry":mapping(field),"properties":{"CSBID":"self","CDL2024":5,"CDL2025":1,"CNTY":"31173"}},
        {"geometry":mapping(box(200,0,300,100)),"properties":{"CSBID":"near","CDL2025":1}},
        {"geometry":mapping(box(400,0,500,100)),"properties":{"CSBID":"soy","CDL2025":5}},
    ]
    result = match_fields(field,features,"corn",2025)
    assert result["crop_history"] == {"2024":"soybeans","2025":"corn"}
    assert result["cohort_field_ids"] == ["near"]
    assert result["county_fips"] == "31173"
    assert match_fields(field, features,"corn",2024)["cohort_field_ids"] == []

def test_disease_filters_county_and_future_seasons():
    rows = [{"county_fips":"31173","status":"Certain","first_observed":"2026-07-01","last_observed":"2026-08-01"},
            {"county_fips":"31001","status":"Certain","first_observed":"2026-07-01"}]
    assert len(parse_reports(rows,"tar spot","31173",2026)) == 1
    assert parse_reports(rows,"tar spot","31173",2025) == []

def test_economics_without_key_and_units():
    out = economics("corn", "31173", "2025-07-01", api_key="")
    assert out["expected_yield_bu_ac"] == 205
    assert out["price_as_of"] is None
    assert "defaults" in out["sources"][0]
    assert economics("alfalfa","31173","2025-07-01",api_key="")["price_usd_bu"] is None


def test_dem_wide_flat_basin_and_nodata_outlet():
    dem = np.full((9,9),10.0)
    dem[2:7,2:7] = 9
    summary, mask = analyze_dem(dem,np.ones_like(dem,dtype=bool),1,1)
    assert mask.sum() == 25
    dem[0:3,4] = np.nan  # unobserved cells must not close a drainage outlet
    summary, mask = analyze_dem(dem,np.ones_like(dem,dtype=bool),1,1)
    assert mask.sum() == 0

def test_depth_texture_ignores_horizons_outside_band():
    out = summarize_horizons([{"top":100,"bottom":200,"texture":"sand","awc":.1,"sand":90}])
    assert out["texture_0_30"] == "unknown"

def test_farm_csb_cache_reads_projected_envelope_once(tmp_path,monkeypatch):
    from fieldwatch.context.boundaries import load_farm
    from fieldwatch import config
    monkeypatch.setenv("FIELDWATCH_CONTEXT_CACHE",str(tmp_path))
    monkeypatch.delenv("BUCKET_NAME",raising=False)
    monkeypatch.setattr(config,"BUCKET","")
    calls=[]
    def reader(bounds):
        calls.append(bounds)
        assert abs(bounds[0]) > 1000 and bounds[2]-bounds[0] > 20000
        return []
    polygon=box(-97,41,-96.99,41.01)
    assert load_farm("farm",[polygon],reader)==[]
    assert load_farm("farm",[polygon],reader)==[]
    assert len(calls)==1

def test_soil_live_client_clips_zones_and_component_weights(tmp_path,monkeypatch):
    from fieldwatch.context import soil
    from fieldwatch import config
    monkeypatch.setenv("FIELDWATCH_CONTEXT_CACHE",str(tmp_path))
    monkeypatch.setattr(config,"BUCKET","")
    monkeypatch.delenv("BUCKET_NAME",raising=False)
    class Response:
        def raise_for_status(self): pass
        def json(self): return self.data
    class Session:
        def post(self,url,json,timeout):
            response=Response()
            if "mupolygongeo" in json["query"]:
                response.data={"Table":[["mukey","geometry"],["1",box(-97,41,-96.99,41.01).wkt]]}
            else:
                response.data={"Table":[["mukey","muname","cokey","comppct_r","drainagecl","hydgrp","hzdept_r","hzdepb_r","awc_r","sandtotal_r","texture"],
                    ["1","test","a",75,"well drained","B",0,100,.2,30,"loam"],
                    ["1","test","b",25,"well drained","B",0,100,.1,90,"sand"]]}
            return response
    result=soil.get_profile("test",box(-97,41,-96.995,41.01),Session())
    assert result.map_units[0].pct_area == pytest.approx(100)
    assert result.map_units[0].awc_0_100_cm == pytest.approx(17.5)
    assert not result.map_units[0].sandy
    import json
    from pathlib import Path
    zones=json.loads(Path(result.zones_key).read_text())
    assert zones["features"][0]["properties"]["mukey"] == "1"

def test_nass_selection_excludes_unpublished_and_suppressed_data():
    from fieldwatch.context.economics import latest_value
    rows=[{"year":"2024","load_time":"2025-02-01","Value":"200"},
          {"year":"2025","load_time":"2026-02-01","Value":"250"},
          {"year":"2024","load_time":"2025-02-01","Value":"(D)"}]
    assert latest_value(rows,"2025-07-01")[2] == 200

def test_disease_authorized_snapshot_zero_reports_and_history(tmp_path,monkeypatch):
    from fieldwatch.context.disease import disease_reports, get_disease_reports
    snapshot=tmp_path/"county.json"
    snapshot.write_text('{"84963":[{"FIPS":"31173","status":"History","last_observed":"2024-07-01"}]}')
    monkeypatch.setenv("FIELDWATCH_EDDMAPS_SNAPSHOT",str(snapshot))
    monkeypatch.delenv("FIELDWATCH_EDDMAPS_COUNTY_URL",raising=False)
    reports=disease_reports("31173","corn",2025)
    assert len(reports)==1 and reports[0].status=="history"
    assert disease_reports("31173","soybeans",2025)==[]
    with pytest.raises(LookupError): get_disease_reports("31001","corn",2025)

def test_cache_reader_filters_extensions_and_returns_partial(monkeypatch):
    import fieldwatch.context as context
    monkeypatch.setattr(context.common,"field",lambda fid:{"field_id":fid})
    monkeypatch.setattr(context,"cloud",lambda:True)
    class Table:
        def get_item(self,**kwargs):
            return {"Item":{"field_id":"f","built_at":"2025-07-01T00:00:00Z","drought":{"category":"D2"},"baseline":{"key":"b"}}}
    monkeypatch.setattr(context.common,"table",lambda name:Table())
    result=context.get_field_context("f")
    assert result.field_id=="f" and result.terrain is None
