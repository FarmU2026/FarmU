"""County fixtures copied from EDDMapS annualprogress on October 10, 2026."""
import json
import pytest
import requests
from fieldwatch.context import disease

TAR_SPOT = {"us": {
    "31173": {"LastObservationDate": "09-21-2023", "EradicationStatus": 1,
              "Type": "History", "ObservationDate": "10-06-2021"},
    "31113": {"LastObservationDate": "09-23-2026", "EradicationStatus": 1,
              "Type": "Certain", "ObservationDate": "09-16-2026"},
}, "ca": {}, "mx": {}}
SOUTHERN_RUST = {"us": {
    "31173": {"LastObservationDate": "07-23-2020", "EradicationStatus": 1,
              "Type": "History", "ObservationDate": "07-26-2018"},
}, "ca": {}, "mx": {}}
FEEDS = {84963: TAR_SPOT, 17596: SOUTHERN_RUST, 17739: {"us": {}, "ca": {}, "mx": {}}}

class FeedSession:
    def __init__(self, payloads=FEEDS, error=None):
        self.calls = []
        self.payloads = iter(payloads.values())
        self.error = error

    def get(self, url, *, headers, timeout):
        self.calls.append((url, headers, timeout))
        payload, error = next(self.payloads), self.error
        class Response:
            def raise_for_status(self):
                if error:
                    raise error
            def json(self):
                return payload
        return Response()

@pytest.fixture(autouse=True)
def no_config(monkeypatch):
    monkeypatch.delenv("FIELDWATCH_EDDMAPS_COUNTY_URL", raising=False)
    monkeypatch.delenv("FIELDWATCH_EDDMAPS_SNAPSHOT", raising=False)

def test_public_feed_thurston_and_default_requests():
    session = FeedSession()
    reports = disease.disease_reports("31173", "corn", 2026, session)
    assert [(r.disease, r.status, r.first_observed, r.last_observed) for r in reports] == [
        ("southern rust", "history", "2018-07-26", "2020-07-23"),
        ("tar spot", "history", "2021-10-06", "2023-09-21"),
    ]
    assert [call[0] for call in session.calls] == [
        f"https://maps.eddmaps.org/annualprogress/data.cfm?sub={species}&year=2026"
        for species in disease.SPECIES.values()]
    assert all(call[1] == disease.HEADERS and call[2] == 30 for call in session.calls)

def test_actual_certain_and_future_filter():
    report, = disease.parse_reports(TAR_SPOT, "tar spot", "31113", 2026)
    assert report.status == "confirmed_this_year"
    assert report.first_observed == "2026-09-16"
    assert report.last_observed == "2026-09-23"
    assert disease.parse_reports(TAR_SPOT, "tar spot", "31113", 2025) == []
    assert disease.parse_reports(TAR_SPOT, "tar spot", "31173", 2022) == []
    assert disease.parse_reports(TAR_SPOT, "tar spot", "31113", 2027)[0].status == "history"

def test_only_us_county_records():
    payload = {"us": {}, "ca": {"31173": TAR_SPOT["us"]["31173"]}, "mx": {}}
    assert disease.parse_reports(payload, "tar spot", "31173", 2026) == []

def test_public_fips_keys_can_omit_leading_zero():
    payload = {"us": {"1003": {"LastObservationDate": "06-27-2025",
               "EradicationStatus": 1, "Type": "History", "ObservationDate": "06-30-2016"}}}
    report, = disease.parse_reports(payload, "southern rust", "01003", 2026)
    assert report.county_fips == "01003"
    assert report.first_observed == "2016-06-30"

@pytest.mark.parametrize("payload", [{"error": "unavailable"}, {"us": []},
                                     {"us": {"31173": None}}, {"us": {"31173": {}}},
                                     {"us": {"bad": TAR_SPOT["us"]["31173"]}},
                                     {"us": {"31173": dict(TAR_SPOT["us"]["31173"], Type="Unknown")}},
                                     {"data": None}, "html"])
def test_bad_feed_is_failure_not_empty_evidence(payload):
    with pytest.raises(ValueError, match="EDDMapS"):
        disease.disease_reports("31173", "corn", 2026, FeedSession({84963: payload}))

def test_http_error_propagates():
    with pytest.raises(requests.HTTPError):
        disease.disease_reports("31173", "corn", 2026, FeedSession(error=requests.HTTPError("403")))

def test_override_url_and_requested_year(monkeypatch):
    monkeypatch.setenv("FIELDWATCH_EDDMAPS_COUNTY_URL", "https://example.test/{species}?year={season}")
    session = FeedSession()
    disease.disease_reports("31173", "corn", 2025, session)
    assert session.calls[0][0] == "https://example.test/84963?year=2025"

def test_snapshot_and_single_report_seam(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(FEEDS))
    monkeypatch.setenv("FIELDWATCH_EDDMAPS_SNAPSHOT", str(path))
    reports = disease.disease_reports("31173", "corn", 2026)
    assert disease.get_disease_reports("31173", "corn", 2026) == reports[0]
    assert disease.disease_reports("31001", "corn", 2026) == []
    with pytest.raises(LookupError):
        disease.get_disease_reports("31001", "corn", 2026)
    assert disease.get_disease_reports("31113", "corn", 2026).status == "confirmed_this_year"
    monkeypatch.setenv("FIELDWATCH_EDDMAPS_COUNTY_URL", disease.DEFAULT_COUNTY_URL)
    session = FeedSession()
    assert disease.disease_reports("31173", "corn", 2026, session) == reports
    assert len(session.calls) == 3

@pytest.mark.parametrize("raw", ["null", "[]", "42"])
def test_invalid_snapshot_never_falls_back_to_network(tmp_path, monkeypatch, raw):
    path = tmp_path / "snapshot.json"
    path.write_text(raw)
    monkeypatch.setenv("FIELDWATCH_EDDMAPS_SNAPSHOT", str(path))
    session = FeedSession()
    with pytest.raises(ValueError, match="snapshot"):
        disease.disease_reports("31173", "corn", 2026, session)
    assert session.calls == []

def test_unsupported_crop_and_county_validation():
    session = FeedSession()
    assert disease.disease_reports("31173", "soybeans", 2026, session) == []
    assert session.calls == []
    with pytest.raises(ValueError, match="five digits"):
        disease.disease_reports("173", "corn", 2026, session)

@pytest.mark.live
def test_live_thurston_county_2026():
    class LiveSession:
        def get(self, url, **kwargs):
            response = requests.get(url, **kwargs)
            response.raise_for_status()
            data = response.json()
            print(url, "HTTP", response.status_code, "US counties", len(data["us"]),
                  "Thurston", json.dumps(data["us"].get("31173")))
            return response
    reports = disease.disease_reports("31173", "corn", 2026, LiveSession())
    print("DiseaseReports", json.dumps([r.model_dump(mode="json") for r in reports]))
    assert all(r.county_fips == "31173" for r in reports)
    assert all(not r.last_observed or r.last_observed[:4] <= "2026" for r in reports)
