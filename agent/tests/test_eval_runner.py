"""WS11 evaluation runner: scenario loading, pass/fail criteria, the local flow and the cloud flow, all with fakes.

No network, no AWS, no Bedrock: the analyzer, decision rule, investigation runner, HTTP and clocks are injected.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval import runner as R
from fieldwatch import contracts as C

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"


def scenario(**over) -> C.Scenario:
    base = {"scenario_id": "hero", "farm_fixture": "demo_farm.json", "field_id": "north-pivot", "as_of": "2026-07-18",
            "expected": {"alert": True, "top_condition": "pivot_failure", "action_type": "work_order",
                         "max_trigger_to_message_s": 60},
            "scripted_replies": [{"text": "YES"}]}
    base.update(over)
    return C.Scenario.model_validate(base)


def write(dirpath: Path, name: str, data) -> None:
    dirpath.joinpath(name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------- loading


def test_shipped_scenarios_load_and_template_is_skipped_but_valid():
    ids = [s.scenario_id for s in R.load_scenarios()]
    assert "demo-fixture-pivot" in ids
    assert not any(i.startswith("REPLACE") for i in ids)
    C.Scenario.model_validate_json((R.SCENARIOS_DIR / "_template.json").read_text())  # the template stays valid


def test_shipped_demo_scenario_points_at_a_real_field():
    sc = next(s for s in R.load_scenarios() if s.scenario_id == "demo-fixture-pivot")
    _, farm = R.load_farm(sc)
    f = R.find_field(farm, sc)
    req = R.build_request(sc, farm, f)
    assert req.field.farm_id == farm["farm_id"] and req.as_of == sc.as_of and len(req.polygon) == 4


def test_loader_skips_underscore_files_and_filters_by_id(tmp_path):
    raw = scenario().model_dump(mode="json")
    write(tmp_path, "a.json", raw)
    write(tmp_path, "b.json", {**raw, "scenario_id": "other"})
    write(tmp_path, "_skip.json", "not even json")
    assert [s.scenario_id for s in R.load_scenarios(tmp_path)] == ["hero", "other"]
    assert [s.scenario_id for s in R.load_scenarios(tmp_path, ["other"])] == ["other"]
    with pytest.raises(R.RunnerError, match="unknown scenario"):
        R.load_scenarios(tmp_path, ["nope"])


@pytest.mark.parametrize("content,needle", [
    ("{broken", "bad.json: not valid JSON"),
    ({"scenario_id": "x"}, "bad.json: invalid scenario"),
    ({**scenario().model_dump(mode="json"), "scripted_replies": [{"nope": 1}]}, "bad.json: invalid scenario"),
    ({**scenario().model_dump(mode="json"), "scripted_replies": [{"text": "YES", "after": "later"}]}, "after"),
    ({**scenario().model_dump(mode="json"), "scripted_replies": [{"text": "YES", "bogus": 1}]}, "unknown keys"),
])
def test_loader_names_the_bad_file(tmp_path, content, needle):
    write(tmp_path, "bad.json", content)
    with pytest.raises(R.RunnerError) as e:
        R.load_scenarios(tmp_path)
    assert needle in str(e.value) and "bad.json" in str(e.value)


def test_loader_rejects_duplicate_ids(tmp_path):
    write(tmp_path, "a.json", scenario().model_dump(mode="json"))
    write(tmp_path, "b.json", scenario().model_dump(mode="json"))
    with pytest.raises(R.RunnerError, match="duplicate"):
        R.load_scenarios(tmp_path)


def test_farm_fixture_must_stay_under_fixtures():
    with pytest.raises(R.RunnerError, match="under agent/fixtures"):
        R.load_farm(scenario(farm_fixture="../requirements.txt"))
    with pytest.raises(R.RunnerError, match="cannot read"):
        R.load_farm(scenario(farm_fixture="missing.json"))


# ---------------------------------------------------------------- criteria


def obs(**kw) -> R.Observation:
    base = dict(alert=True, top_condition="pivot_failure", action_type="work_order", trigger_to_message_s=12.0,
                investigation_id="inv-1")
    base.update(kw)
    return R.Observation(**base)


def crit(result: R.ScenarioResult, name: str) -> R.Criterion:
    return next(c for c in result.criteria if c.name == name)


def test_all_criteria_pass():
    res = R.evaluate(scenario(), obs())
    assert res.passed and {c.name for c in res.criteria} == {"alert", "top_condition", "action_type", "trigger_to_message"}
    assert res.observed_top_condition == "pivot_failure" and res.investigation_id == "inv-1"


@pytest.mark.parametrize("override,name,words", [
    (dict(alert=False), "alert", "expected True, saw False"),
    (dict(top_condition="hail"), "top_condition", "expected pivot_failure, saw hail"),
    (dict(top_condition=None), "top_condition", "saw None"),
    (dict(action_type="scout_pin"), "action_type", "expected work_order, saw scout_pin"),
    (dict(trigger_to_message_s=61.0), "trigger_to_message", "61.0s (limit 60s)"),
    (dict(trigger_to_message_s=None), "trigger_to_message", "no message"),
])
def test_each_criterion_fails_with_a_plain_reason(override, name, words):
    res = R.evaluate(scenario(), obs(**override))
    assert not res.passed
    c = crit(res, name)
    assert not c.passed and words in c.reason


def test_time_limit_boundary_passes():
    assert R.evaluate(scenario(), obs(trigger_to_message_s=60.0)).passed


def test_unset_expectations_are_not_checked():
    sc = scenario(expected={"alert": True})
    res = R.evaluate(sc, obs(top_condition="hail", action_type="x"))
    assert res.passed and {c.name for c in res.criteria} == {"alert", "trigger_to_message"}


def test_no_alert_expected_skips_timing_and_fails_on_a_false_alarm():
    sc = scenario(expected={"alert": False}, scripted_replies=[])
    assert R.evaluate(sc, R.Observation()).passed
    res = R.evaluate(sc, obs())
    assert not res.passed and [c.name for c in res.criteria] == ["alert"]


def test_run_error_fails_the_scenario():
    sc = scenario(expected={"alert": False}, scripted_replies=[])
    res = R.evaluate(sc, R.Observation(error="boom"))
    assert not res.passed and crit(res, "run").reason == "boom" and res.error == "boom"


# ---------------------------------------------------------------- local flow


class Tick:
    """Monotonic clock that moves 1 s each time it is read."""

    def __init__(self):
        self.t = 100.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def sample_result() -> C.FieldAnalysisResult:
    return C.FieldAnalysisResult.model_validate_json((SAMPLES / "field_analysis_result.json").read_text())


def decision(emit: bool, reason: str = "r") -> SimpleNamespace:
    regions = sample_result().anomaly.regions if emit else []
    return SimpleNamespace(emit=emit, reason=reason, regions=regions, anomaly_id="anm-north-pivot-20260718")


def fake_investigator(calls: list, *, conclude=True, first="waiting_for_farmer"):
    def investigate(req, capture):
        calls.append(req)
        if isinstance(req, C.StartInvestigation):
            capture.message("Send a repair ticket? YES / NO")
            return C.AgentStep(status=first, text="Send a repair ticket? YES / NO", investigation_id=req.investigation_id,
                               interrupt_id="int-1" if first == "waiting_for_farmer" else None)
        capture.message("Repair ticket sent.")
        if conclude:
            capture.concluded = {"condition": "pivot_failure", "proposed_action": {"action_type": "work_order"}}
        return C.AgentStep(status="done", text="Repair ticket sent.", investigation_id=req.investigation_id)
    return investigate


def local_runner(*, emit=True, calls=None, analyze_calls=None, **kw) -> R.LocalRunner:
    calls = [] if calls is None else calls
    seen = [] if analyze_calls is None else analyze_calls

    def analyze(req):
        seen.append(req)
        return sample_result()

    deps = R.LocalDeps(analyze=analyze, decide=lambda *a, **k: decision(emit), investigate=fake_investigator(calls, **kw),
                       clock=Tick(), new_id=lambda: "abc")
    return R.LocalRunner(deps)


def test_local_alert_path_runs_investigation_and_feeds_scripted_replies():
    calls, seen = [], []
    runner = local_runner(calls=calls, analyze_calls=seen)
    o = runner.run(scenario(scripted_replies=[{"text": "YES", "after": "alert"}]))
    # analyzer got a request built from the fixture's north pivot field
    assert seen[0].field == C.FieldRef(farm_id="wohleb-farm", field_id="north-pivot") and seen[0].as_of == "2026-07-18"
    assert seen[0].polygon[0] == (-96.548, 42.152) and seen[0].crop == "corn"
    # investigation: start then one resume carrying the scripted reply and the interrupt id
    assert isinstance(calls[0], C.StartInvestigation) and calls[0].anomaly is not None
    assert calls[0].investigation_id == "inv-eval-hero-abc" == o.investigation_id
    assert isinstance(calls[1], C.ResumeInvestigation) and (calls[1].text, calls[1].interrupt_id) == ("YES", "int-1")
    assert o.alert and o.top_condition == "pivot_failure" and o.action_type == "work_order"
    assert o.messages == ["Send a repair ticket? YES / NO", "Repair ticket sent."]
    assert o.error is None and o.trigger_to_message_s is not None
    assert R.evaluate(scenario(), o).passed


def test_local_trigger_to_message_is_first_message_minus_analysis_start():
    o = local_runner().run(scenario())
    # clock reads: t0 (101), then the first message (102) in the fake investigator
    assert o.trigger_to_message_s == pytest.approx(1.0)


def test_local_no_alert_path_never_starts_an_investigation():
    calls = []
    o = local_runner(emit=False, calls=calls).run(scenario(expected={"alert": False}, scripted_replies=[]))
    assert not o.alert and calls == [] and o.investigation_id is None and "no alert" in o.notes[0]
    assert R.evaluate(scenario(expected={"alert": False}), o).passed


def test_local_extra_scripted_replies_are_reported_not_sent():
    calls = []
    o = local_runner(calls=calls).run(scenario(scripted_replies=[{"text": "YES"}, {"text": "MORE"}]))
    assert len(calls) == 2 and any("unused" in n for n in o.notes)


def test_local_without_replies_stops_at_the_question():
    calls = []
    o = local_runner(calls=calls).run(scenario(scripted_replies=[]))
    assert len(calls) == 1 and o.top_condition is None and any("did not conclude" in n for n in o.notes)
    assert not R.evaluate(scenario(scripted_replies=[]), o).passed


def test_local_failed_investigation_is_an_error():
    def investigate(req, capture):
        return C.AgentStep(status="failed", text="", investigation_id=req.investigation_id,
                           error=C.ErrorDetail(code="AGENT_ERROR", message="model unavailable"))

    deps = R.LocalDeps(analyze=lambda r: sample_result(), decide=lambda *a, **k: decision(True), investigate=investigate,
                       clock=Tick())
    o = R.LocalRunner(deps).run(scenario())
    assert o.error == "investigation failed: model unavailable"


def test_local_unknown_field_is_a_clear_error():
    with pytest.raises(R.RunnerError, match="not in demo_farm.json"):
        local_runner().run(scenario(field_id="nope"))


def test_default_decide_reuses_the_scene_pipeline_rule():
    from cloud.lambdas.scenes import decide

    res = sample_result()
    got = R.default_decide(res, 2.0, 1.2, "north-pivot", "2026-07-18")
    want = decide.evaluate(res, 2.0, 1.2, "north-pivot", "2026-07-18")
    assert (got.emit, got.reason, got.anomaly_id) == (want.emit, want.reason, want.anomaly_id)
    assert not R.default_decide(res, 99.0, 1.2, "north-pivot", "2026-07-18").emit


def test_default_investigate_records_in_memory_through_run_request(monkeypatch):
    """The real run_request with the capturing Recorder: no DynamoDB / EventBridge, agent calls stubbed out."""
    from fieldwatch import investigation as inv
    from fieldwatch.schemas import Diagnosis

    diag = Diagnosis(field_name="north pivot", what="Outer edge is dry", where="outer span", why="pivot nozzle likely out",
                     dollars_low=9000, dollars_high=14000, how_sure="pretty sure", recommended_action="repair_work_order",
                     action_detail="check the end tower", condition="pivot_failure", affected_acres=12.0)
    monkeypatch.setattr(inv, "start", lambda i, f, emit=None, context="": inv.Step("waiting_for_farmer", "Send a ticket? YES / NO",
                                                                         interrupt_id="int-9"))
    monkeypatch.setattr(inv, "reply", lambda i, it, t, emit=None, **kw: inv.Step("done", diag.to_text(True), diagnosis=diag))
    monkeypatch.setattr(inv, "deliver", lambda rec, step: SimpleNamespace(message_id="m1") if step.text else None)
    capture = R.Capture(Tick())
    step = R.default_investigate(C.StartInvestigation(investigation_id="inv-x", farm_id="wohleb-farm", field="north-pivot"),
                                 capture)
    assert step.status == "waiting_for_farmer" and capture.messages[0][1] == "Send a ticket? YES / NO"
    R.default_investigate(C.ResumeInvestigation(investigation_id="inv-x", interrupt_id="int-9", text="YES"), capture)
    assert len(capture.messages) == 2
    assert capture.concluded["condition"] == "pivot_failure"
    assert capture.concluded["proposed_action"]["action_type"] == "work_order"


# ---------------------------------------------------------------- cloud flow


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.base = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s

    def wall(self) -> datetime:
        return self.base + timedelta(seconds=self.now - 1000.0)

    def iso(self, offset_s: float) -> str:
        return (self.base + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeApi:
    """A scripted dashboard API. `inv_item(polls)` returns the investigation row at that poll of the investigation."""

    def __init__(self, tm: FakeTime, *, inv_at_poll: int | None = 2, item_fn=None, replay_status=202, login_status=200):
        self.tm, self.log, self.inv_at_poll = tm, [], inv_at_poll
        self.item_fn, self.replay_status, self.login_status = item_fn, replay_status, login_status
        self.overview_calls = 0
        self.inv_gets = 0
        self.replied = False
        self.analysis = False

    def __call__(self, method, url, json=None, headers=None):
        path = url.split("execute-api.test", 1)[1]
        self.log.append((method, path, json, (headers or {}).get("Authorization")))
        if path == "/v1/auth/dashboard":
            return (self.login_status, {"token": "tok", "expires_at": "x"} if self.login_status == 200
                    else {"error": {"message": "Wrong passcode"}})
        if path == "/v1/replay/reset":
            return 202, {"status": "reset"}
        if path == "/v1/replay":
            return self.replay_status, {"replay_id": "rpl_1"} if self.replay_status == 202 else {"error": {"message": "nope"}}
        if path.startswith("/v1/farms/") and path.endswith("/overview"):
            self.overview_calls += 1
            replayed = any(p == "/v1/replay" for _, p, _, _ in self.log)
            started = replayed and self.inv_at_poll is not None and self.overview_calls >= self.inv_at_poll
            return 200, {"farm_id": "wohleb-farm", "open_investigations": (
                [{"investigation_id": "inv-new", "field_id": "north-pivot", "status": "queued",
                  "created_at": "2026-10-10T12:00:05Z"},
                 {"investigation_id": "inv-other", "field_id": "johnson-quarter", "status": "queued", "created_at": "z"}]
                if started else []), "fields": [
                {"field_id": "north-pivot", "latest_scene_date": "2026-07-18" if self.analysis else None,
                 "latest_score": 3.1 if self.analysis else None}]}
        if path == "/v1/investigations/inv-new":
            self.inv_gets += 1
            return 200, self.item_fn(self)
        if path == "/v1/mirror/reply":
            self.replied = True
            return 202, {"status": "accepted"}
        return 404, {"error": {"message": f"no route {path}"}}

    def paths(self):
        return [p for _, p, _, _ in self.log]


def cloud_item(api: FakeApi) -> dict:
    tm = api.tm
    if not api.replied:
        return {"investigation_id": "inv-new", "status": "awaiting_reply", "pending_interrupt_id": "int-1",
                "last_text": "Send a repair ticket? YES / NO", "message_sent_at": tm.iso(14), "person_id": "p-dad"}
    return {"investigation_id": "inv-new", "status": "concluded", "pending_interrupt_id": None,
            "last_text": "Repair ticket sent.", "message_sent_at": tm.iso(40), "condition": "pivot_failure",
            "diagnosis_json": json.dumps({"recommended_action": "repair_work_order"})}


def cloud_runner(api: FakeApi, tm: FakeTime, **cfg) -> R.CloudRunner:
    config = R.CloudConfig(api="https://x.execute-api.test", passcode="pw", **cfg)
    return R.CloudRunner(config, http=api, sleep=tm.sleep, clock=tm.clock, wall=tm.wall)


def test_cloud_full_flow_auth_reset_replay_poll_reply_result():
    tm = FakeTime()
    api = FakeApi(tm, item_fn=cloud_item)
    o = cloud_runner(api, tm).run(scenario())
    paths = api.paths()
    order = [paths.index(p) for p in ("/v1/auth/dashboard", "/v1/replay/reset", "/v1/replay", "/v1/mirror/reply")]
    assert order == sorted(order)
    login = next(c for c in api.log if c[1] == "/v1/auth/dashboard")
    assert login[2] == {"passcode": "pw"} and login[3] is None
    reset = next(c for c in api.log if c[1] == "/v1/replay/reset")
    assert reset[2] == {"farm_id": "wohleb-farm"} and reset[3] == "Bearer tok"
    replay = next(c for c in api.log if c[1] == "/v1/replay")
    assert replay[2] == {"field_ids": ["north-pivot"], "as_of": "2026-07-18", "farm_id": "wohleb-farm"}
    reply = next(c for c in api.log if c[1] == "/v1/mirror/reply")
    assert reply[2] == {"person_id": "p-dad", "text": "YES", "farm_id": "wohleb-farm"}  # person from the investigation row
    # the other field's investigation was ignored, the new one followed
    assert o.alert and o.investigation_id == "inv-new"
    assert o.top_condition == "pivot_failure" and o.action_type == "work_order"
    assert o.messages == ["Send a repair ticket? YES / NO", "Repair ticket sent."]
    # first message at +14 s server time, replay triggered at +0
    assert o.trigger_to_message_s == pytest.approx(14.0) and o.error is None
    assert R.evaluate(scenario(), o, mode="cloud").passed


def test_cloud_polls_every_three_seconds():
    tm = FakeTime()
    api = FakeApi(tm, inv_at_poll=3, item_fn=cloud_item)
    cloud_runner(api, tm).run(scenario())
    assert 3 in tm.slept and tm.slept.count(3) >= 2


def test_cloud_farm_id_override_and_person_precedence():
    tm = FakeTime()
    api = FakeApi(tm, item_fn=cloud_item)
    cloud_runner(api, tm, farm_id="farm-9", person_id="cfg-person").run(
        scenario(scripted_replies=[{"text": "YES", "person_id": "reply-person"}]))
    assert next(c for c in api.log if c[1] == "/v1/replay/reset")[2] == {"farm_id": "farm-9"}
    assert next(c for c in api.log if c[1] == "/v1/mirror/reply")[2]["person_id"] == "reply-person"
    api2 = FakeApi(FakeTime(), item_fn=cloud_item)
    cloud_runner(api2, api2.tm, person_id="cfg-person").run(scenario())
    assert next(c for c in api2.log if c[1] == "/v1/mirror/reply")[2]["person_id"] == "cfg-person"


def test_cloud_timeout_when_no_investigation_starts():
    tm = FakeTime()
    api = FakeApi(tm, inv_at_poll=None, item_fn=cloud_item)
    o = cloud_runner(api, tm, timeout_s=30).run(scenario())
    assert not o.alert and "timed out after 30s" in o.error and "no investigation started" in o.error
    res = R.evaluate(scenario(), o, mode="cloud")
    assert not res.passed and not crit(res, "run").passed


def test_cloud_timeout_while_waiting_for_the_conclusion():
    tm = FakeTime()
    stuck = lambda api: {"status": "running", "last_text": None}  # noqa: E731
    api = FakeApi(tm, item_fn=stuck)
    o = cloud_runner(api, tm, timeout_s=20).run(scenario())
    assert o.alert and "timed out" in o.error and "status running" in o.error


def test_cloud_no_alert_scenario_passes_after_analysis_settles():
    tm = FakeTime()
    api = FakeApi(tm, inv_at_poll=None, item_fn=cloud_item)
    sc = scenario(scenario_id="healthy", expected={"alert": False}, scripted_replies=[])
    seen_replay = []
    orig = api.__call__

    def http(method, url, json=None, headers=None):
        out = orig(method, url, json=json, headers=headers)
        if url.endswith("/v1/replay"):
            seen_replay.append(True)
        api.analysis = bool(seen_replay) and tm.now > 1008  # the scene analysis row appears a few seconds later
        return out

    runner = R.CloudRunner(R.CloudConfig(api="https://x.execute-api.test", passcode="pw", settle_s=10), http=http,
                           sleep=tm.sleep, clock=tm.clock, wall=tm.wall)
    o = runner.run(sc)
    assert not o.alert and o.error is None
    assert R.evaluate(sc, o, mode="cloud").passed


def test_cloud_no_alert_scenario_without_any_analysis_times_out():
    tm = FakeTime()
    api = FakeApi(tm, inv_at_poll=None, item_fn=cloud_item)
    sc = scenario(expected={"alert": False}, scripted_replies=[])
    o = cloud_runner(api, tm, timeout_s=15).run(sc)
    assert "no scene analysis was seen" in o.error and not R.evaluate(sc, o).passed


def test_cloud_replays_are_spaced_by_the_rate_limit():
    tm = FakeTime()
    api = FakeApi(tm, item_fn=cloud_item)
    stamps: list[float] = []

    def http(method, url, json=None, headers=None):
        if url.endswith("/v1/replay"):
            stamps.append(tm.now)
        return api(method, url, json=json, headers=headers)

    runner = R.CloudRunner(R.CloudConfig(api="https://x.execute-api.test", passcode="pw"), http=http, sleep=tm.sleep,
                           clock=tm.clock, wall=tm.wall)
    runner.run(scenario())
    api.replied = False
    runner.run(scenario())
    assert len(stamps) == 2 and stamps[1] - stamps[0] >= R.REPLAY_GAP_S


def test_cloud_retries_a_429_replay_then_gives_up():
    tm = FakeTime()
    api = FakeApi(tm, replay_status=429, item_fn=cloud_item)
    with pytest.raises(R.RunnerError, match="replay failed \\(HTTP 429\\)"):
        cloud_runner(api, tm).run(scenario())
    assert api.paths().count("/v1/replay") == 3


def test_cloud_login_failure_is_a_clear_error():
    tm = FakeTime()
    api = FakeApi(tm, login_status=401, item_fn=cloud_item)
    with pytest.raises(R.RunnerError, match="dashboard login failed \\(HTTP 401\\): Wrong passcode"):
        cloud_runner(api, tm).run(scenario())


def test_cloud_expired_token_logs_in_again():
    tm = FakeTime()
    api = FakeApi(tm, item_fn=cloud_item)
    state = {"expired": True}
    orig = api.__call__

    def http(method, url, json=None, headers=None):
        if url.endswith("/overview") and state.pop("expired", False):
            api.log.append((method, "/v1/farms/x/overview", json, "expired"))
            return 401, {"error": {"message": "expired"}}
        return orig(method, url, json=json, headers=headers)

    runner = R.CloudRunner(R.CloudConfig(api="https://x.execute-api.test", passcode="pw"), http=http, sleep=tm.sleep,
                           clock=tm.clock, wall=tm.wall)
    o = runner.run(scenario())
    assert o.alert and api.paths().count("/v1/auth/dashboard") == 2


def test_requests_http_wraps_errors_without_network(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "request", boom)
    status, data = R.requests_http("GET", "http://x")
    assert status == 0 and "no route" in data["error"]["message"]


# ---------------------------------------------------------------- suite, report, CLI


class StubRunner:
    mode = "local"

    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def run(self, sc):
        self.calls.append(sc.scenario_id)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def scenario_dir(tmp_path, *ids) -> Path:
    for i in ids:
        write(tmp_path, f"{i}.json", scenario(scenario_id=i).model_dump(mode="json"))
    return tmp_path


def test_main_exit_codes_table_and_json(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    scenario_dir(sdir, "a", "b")
    lines: list[str] = []
    stub = StubRunner([obs(), obs(top_condition="hail")])
    out_json = tmp_path / "out.json"
    code = R.main(["--json", str(out_json)], runner=stub, scenarios_dir=sdir, out=lines.append)
    text = "\n".join(lines)
    assert code == 1 and stub.calls == ["a", "b"]
    assert "| SCENARIO" in text and "PASS" in text and "FAIL" in text
    assert "FAIL b (run 1): top_condition: expected pivot_failure, saw hail" in text
    assert "1/2 scenario runs passed; 0/1 repeat(s) fully green" in text
    data = json.loads(out_json.read_text())
    assert data["passed"] is False and data["mode"] == "local" and len(data["results"]) == 2
    assert data["results"][1]["criteria"][1]["passed"] is False


def test_main_repeat_three_green_meets_pitch_rule(tmp_path):
    scenario_dir(tmp_path, "a")
    lines: list[str] = []
    stub = StubRunner([obs(), obs(), obs()])
    code = R.main(["--repeat", "3", "--scenario", "a"], runner=stub, scenarios_dir=tmp_path, out=lines.append)
    assert code == 0 and stub.calls == ["a"] * 3
    assert "3/3 repeat(s) fully green - pitch rule met" in "\n".join(lines)


def test_main_one_red_repeat_fails_the_whole_run(tmp_path):
    scenario_dir(tmp_path, "a")
    stub = StubRunner([obs(), obs(alert=False), obs()])
    lines: list[str] = []
    assert R.main(["--repeat", "3"], runner=stub, scenarios_dir=tmp_path, out=lines.append) == 1
    assert "2/3 repeat(s) fully green" in lines[-1] and "pitch rule" not in lines[-1]


def test_a_crashing_scenario_is_reported_and_the_suite_continues(tmp_path):
    scenario_dir(tmp_path, "a", "b")
    stub = StubRunner([RuntimeError("kaboom"), obs()])
    lines: list[str] = []
    assert R.main([], runner=stub, scenarios_dir=tmp_path, out=lines.append) == 1
    assert stub.calls == ["a", "b"] and "RuntimeError: kaboom" in "\n".join(lines)


def test_main_setup_errors_exit_2(tmp_path, monkeypatch):
    lines: list[str] = []
    assert R.main([], runner=StubRunner([]), scenarios_dir=tmp_path, out=lines.append) == 2  # no scenarios
    assert "no scenarios found" in lines[-1]
    scenario_dir(tmp_path, "a")
    assert R.main(["--scenario", "zzz"], runner=StubRunner([]), scenarios_dir=tmp_path, out=lines.append) == 2
    assert R.main(["--repeat", "0"], runner=StubRunner([]), scenarios_dir=tmp_path, out=lines.append) == 2
    monkeypatch.delenv("FIELDWATCH_API_URL", raising=False)
    monkeypatch.delenv("FIELDWATCH_DASHBOARD_PASSCODE", raising=False)
    assert R.main(["--mode", "cloud"], scenarios_dir=tmp_path, out=lines.append) == 2
    assert "--api" in lines[-1]
    assert R.main(["--mode", "cloud", "--api", "https://x"], scenarios_dir=tmp_path, out=lines.append) == 2
    assert "FIELDWATCH_DASHBOARD_PASSCODE" in lines[-1]


def test_make_runner_reads_env_and_flags(monkeypatch):
    monkeypatch.setenv("FIELDWATCH_API_URL", "https://env.example")
    monkeypatch.setenv("FIELDWATCH_DASHBOARD_PASSCODE", "pw")
    args = R.build_parser().parse_args(["--mode", "cloud", "--farm-id", "f1", "--timeout", "45"])
    runner = R.make_runner(args)
    assert isinstance(runner, R.CloudRunner)
    assert (runner.cfg.api, runner.cfg.passcode, runner.cfg.farm_id, runner.cfg.timeout_s) == ("https://env.example", "pw", "f1", 45)
    assert isinstance(R.make_runner(R.build_parser().parse_args([])), R.LocalRunner)
