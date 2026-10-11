"""NASS economics: key resolution (env, SSM) and the county-median / district / state yield fallback. Fake session only."""
import importlib

import pytest

# fieldwatch.context re-exports the function under the module's name, so import the module explicitly
eco = importlib.import_module("fieldwatch.context.economics")

COUNTY, ASD, STATE, PRICE = "COUNTY", "AGRICULTURAL DISTRICT", "STATE", "NATIONAL"


def rows(values, **extra):
    return [{"year": str(y), "Value": str(v), "load_time": f"{y + 1}-02-01 12:00:00", **extra} for y, v in values.items()]


class Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": self.data}


class FakeNass:
    """Answers by agg_level_desc; `calls` keeps every params dict."""

    def __init__(self, county=(), asd=(), state=(), price=None):
        self.by_level = {COUNTY: list(county), ASD: list(asd), STATE: list(state),
                         PRICE: price if price is not None else rows({2024: 4.1})}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return Resp(self.by_level[params["agg_level_desc"]])

    def levels(self):
        return [c["agg_level_desc"] for c in self.calls]


def county_rows(values):
    return rows(values, county_name="THURSTON", asd_code="40", asd_desc="NORTHEAST")


def run(session, **kw):
    return eco.economics("corn", "31173", "2025-07-01", api_key="secret-test-key", session=session, **kw)


def test_county_median_of_latest_five_years():
    session = FakeNass(county=county_rows({2016: 100, 2018: 190, 2019: 180, 2020: 200, 2021: 205.9, 2022: 121.4}))
    out = run(session)
    assert out["expected_yield_bu_ac"] == 190  # median of 2018-2022 (190, 180, 200, 205.9, 121.4); 2016 is too old
    assert "USDA NASS Quick Stats yield, Thurston County median 2018-2022" in out["sources"]
    assert session.levels() == [COUNTY, PRICE]  # no district or state query needed
    assert out["price_usd_bu"] == 4.1 and out["price_as_of"] == "2025-02-01"


def test_county_with_three_years_is_enough_and_future_years_are_ignored():
    session = FakeNass(county=county_rows({2020: 200, 2021: 210, 2022: 150, 2025: 999}))
    out = run(session)
    assert out["expected_yield_bu_ac"] == 200
    assert "Thurston County median 2020-2022" in out["sources"][1]


def test_sparse_county_falls_back_to_district_using_the_county_rows_asd_code():
    session = FakeNass(county=county_rows({2021: 205.9, 2022: 121.4}),  # only two years: too noisy
                       asd=rows({2020: 190, 2021: 200, 2022: 150}, asd_desc="NORTHEAST"))
    out = run(session)
    assert out["expected_yield_bu_ac"] == 190
    assert "USDA NASS Quick Stats yield, Northeast District median 2020-2022" in out["sources"]
    assert session.levels() == [COUNTY, ASD, PRICE]
    asd_call = session.calls[1]
    assert asd_call["asd_code"] == "40" and asd_call["state_fips_code"] == "31" and "county_code" not in asd_call


def test_state_fallback_when_district_is_sparse_too():
    session = FakeNass(county=county_rows({2022: 121.4}), asd=rows({2022: 150}),
                       state=rows({2019: 190, 2020: 200, 2021: 210, 2022: 160}, state_name="NEBRASKA"))
    out = run(session)
    assert out["expected_yield_bu_ac"] == 195
    assert "USDA NASS Quick Stats yield, Nebraska (state) median 2019-2022" in out["sources"]
    assert session.levels() == [COUNTY, ASD, STATE, PRICE]
    assert session.calls[2]["state_fips_code"] == "31"


def test_no_county_rows_skips_district_and_uses_state():
    session = FakeNass(state=rows({2022: 160}, state_name="NEBRASKA"))
    out = run(session)
    assert out["expected_yield_bu_ac"] == 160
    assert session.levels() == [COUNTY, STATE, PRICE]


def test_nothing_published_keeps_defaults_but_takes_the_price():
    out = run(FakeNass())
    assert out["expected_yield_bu_ac"] == 205 and "defaults" in out["sources"][0]
    assert out["price_usd_bu"] == 4.1


def test_unpublished_loads_are_excluded_from_the_median():
    late = [{"year": "2024", "Value": "50", "load_time": "2026-02-01 00:00:00", "county_name": "THURSTON"}]
    session = FakeNass(county=county_rows({2021: 200, 2022: 210, 2023: 220}) + late)
    assert run(session)["expected_yield_bu_ac"] == 210


def test_key_never_appears_in_sources():
    out = run(FakeNass(county=county_rows({2020: 1, 2021: 2, 2022: 3})))
    assert not any("secret-test-key" in s for s in out["sources"])


# ---------- key resolution ----------


class FakeSsm:
    def __init__(self, value="ssm-secret", error=None):
        self.value, self.error, self.calls = value, error, []

    def get_parameter(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("NASS_API_KEY", raising=False)
    monkeypatch.delenv("NASS_API_KEY_PARAM", raising=False)
    monkeypatch.setattr(eco, "_param_cache", {})


def test_key_from_ssm_is_decrypted_cached_and_used(monkeypatch):
    monkeypatch.setenv("NASS_API_KEY_PARAM", "/fieldwatch/nass-api-key")
    ssm = FakeSsm()
    session = FakeNass(county=county_rows({2020: 200, 2021: 210, 2022: 220}))
    out = eco.economics("corn", "31173", "2025-07-01", session=session, ssm_client=ssm)
    eco.economics("corn", "31173", "2025-07-01", session=session, ssm_client=ssm)
    assert out["expected_yield_bu_ac"] == 210
    assert ssm.calls == [{"Name": "/fieldwatch/nass-api-key", "WithDecryption": True}]  # second call hits the cache
    assert session.calls[0]["key"] == "ssm-secret"


def test_env_key_wins_over_ssm(monkeypatch):
    monkeypatch.setenv("NASS_API_KEY", "env-key")
    monkeypatch.setenv("NASS_API_KEY_PARAM", "/p")
    ssm = FakeSsm()
    assert eco.resolve_key(None, ssm) == "env-key" and ssm.calls == []


def test_ssm_error_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("NASS_API_KEY_PARAM", "/p")
    session = FakeNass()
    out = eco.economics("corn", "31173", "2025-07-01", session=session, ssm_client=FakeSsm(error=RuntimeError("denied")))
    assert out["expected_yield_bu_ac"] == 205 and out["price_as_of"] is None and "defaults" in out["sources"][0]
    assert session.calls == []  # no NASS request without a key
    assert eco._param_cache == {}  # a failure is not cached, so a later invocation retries


def test_no_key_and_no_param_gives_defaults_without_touching_ssm():
    ssm = FakeSsm()
    session = FakeNass()
    out = eco.economics("corn", "31173", "2025-07-01", session=session, ssm_client=ssm)
    assert out["expected_yield_bu_ac"] == 205 and ssm.calls == [] and session.calls == []


def test_explicit_empty_key_means_no_key(monkeypatch):
    monkeypatch.setenv("NASS_API_KEY", "env-key")
    assert eco.economics("corn", "31173", "2025-07-01", api_key="", session=FakeNass())["price_as_of"] is None
