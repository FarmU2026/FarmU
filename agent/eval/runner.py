"""Scenario evaluation runner (WS11): "demo ready" means every scenario passes, three runs in a row.

    cd agent
    python -m eval.runner                      # local mode, every scenario in eval/scenarios/*.json
    python -m eval.runner --scenario hero --repeat 3 --json out.json
    python -m eval.runner --mode cloud --api https://<id>.execute-api.us-west-2.amazonaws.com

A scenario (`contracts.Scenario`) names a farm fixture, a field, an as-of date and the expected outcome. A *runner*
turns one scenario into an `Observation` (what actually happened); `evaluate()` compares it with the expectation and
produces a `ScenarioResult` with a pass/fail line per criterion. The two runners:

  LocalRunner  analyzer (via interfaces) -> the scene pipeline's decide rule -> investigation.run_request with a
               recording Recorder -> scripted replies. No cloud; the analyzer reads imagery and the agent calls
               Bedrock, so use it from a configured dev machine. Every external piece is injectable (LocalDeps).
  CloudRunner  the deployed dashboard API: auth -> reset -> replay -> poll overview / investigation -> mirror replies.
               All HTTP goes through one injectable `http(method, url, json, headers) -> (status, dict)`.

Exit code: 0 only when every scenario passes on every repeat; 1 when something failed; 2 on usage or setup errors.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import BaseModel, Field, ValidationError

from fieldwatch import config, interfaces
from fieldwatch import contracts as C

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
FIXTURES_DIR = config.FIXTURES
REPLAY_GAP_S = 30  # cloud.api.dashboard.REPLAY_GAP_S: the API allows one replay per 30 s
REPLY_KEYS = {"text", "after", "person_id"}
# investigation.Diagnosis.recommended_action -> contract action type (same table as investigation._ACTION_TYPE;
# duplicated so cloud mode does not import Strands)
ACTION_TYPES = {"repair_work_order": "work_order", "scouting_pin": "scout_pin", "request_photo": "photo_request",
                "insurance_notice": "insurance_notice", "none": "none"}


class RunnerError(Exception):
    """A setup or run problem that should be reported plainly (bad scenario file, API refused us, ...)."""


# ---------------------------------------------------------------- scenarios


def validate_replies(scenario: C.Scenario) -> None:
    """`scripted_replies` items are {"text": "YES"} with optional "after" ("alert") and "person_id" (cloud only)."""
    for i, reply in enumerate(scenario.scripted_replies):
        text = reply.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"scripted_replies[{i}] needs a non-empty string \"text\"")
        if reply.get("after", "alert") != "alert":
            raise ValueError(f"scripted_replies[{i}].after must be \"alert\" (the only supported trigger)")
        extra = set(reply) - REPLY_KEYS
        if extra:
            raise ValueError(f"scripted_replies[{i}] has unknown keys {sorted(extra)}; allowed: {sorted(REPLY_KEYS)}")


def load_scenarios(directory: Path = SCENARIOS_DIR, only: list[str] | None = None) -> list[C.Scenario]:
    """Every `*.json` in the directory except names starting with `_`, validated; errors name the file."""
    found: list[C.Scenario] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            scenario = C.Scenario.model_validate(json.loads(path.read_text(encoding="utf-8")))
            validate_replies(scenario)
        except json.JSONDecodeError as e:
            raise RunnerError(f"{path.name}: not valid JSON ({e})") from None
        except (ValidationError, ValueError) as e:
            raise RunnerError(f"{path.name}: invalid scenario: {e}") from None
        found.append(scenario)
    ids = [s.scenario_id for s in found]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise RunnerError(f"duplicate scenario_id in {directory}: {', '.join(dupes)}")
    if only:
        missing = [i for i in only if i not in ids]
        if missing:
            raise RunnerError(f"unknown scenario(s) {', '.join(missing)}; known: {', '.join(ids) or 'none'}")
        found = [s for s in found if s.scenario_id in only]
    return found


def load_farm(scenario: C.Scenario, fixtures: Path = FIXTURES_DIR) -> tuple[Path, dict]:
    path = (fixtures / scenario.farm_fixture).resolve()
    if fixtures.resolve() not in path.parents:
        raise RunnerError(f"{scenario.scenario_id}: farm_fixture must be a path under agent/fixtures/")
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RunnerError(f"{scenario.scenario_id}: cannot read farm fixture {scenario.farm_fixture}: {e}") from None


def find_field(farm: dict, scenario: C.Scenario) -> dict:
    for f in farm.get("fields", []):
        if f.get("id") == scenario.field_id:
            return f
    known = [f.get("id") for f in farm.get("fields", [])]
    raise RunnerError(f"{scenario.scenario_id}: field {scenario.field_id!r} is not in {scenario.farm_fixture} ({known})")


# ---------------------------------------------------------------- observations and results


class Observation(BaseModel):
    """What a run actually did. `alert` = an investigation started; times are seconds from the trigger."""

    alert: bool = False
    top_condition: str | None = None
    action_type: str | None = None
    trigger_to_message_s: float | None = None
    investigation_id: str | None = None
    messages: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    error: str | None = None


class Criterion(BaseModel):
    name: str
    passed: bool
    reason: str


class ScenarioResult(BaseModel):
    scenario_id: str
    mode: str
    repeat: int = 1
    expected: C.ScenarioExpected
    observed_alert: bool
    observed_top_condition: str | None = None
    observed_action_type: str | None = None
    trigger_to_message_s: float | None = None
    investigation_id: str | None = None
    criteria: list[Criterion]
    passed: bool
    duration_s: float = 0.0
    error: str | None = None
    notes: list[str] = Field(default_factory=list)


def _check(name: str, expected: Any, observed: Any) -> Criterion:
    ok = expected == observed
    return Criterion(name=name, passed=ok, reason=f"expected {expected}, saw {observed}" if not ok else f"{observed}")


def evaluate(scenario: C.Scenario, obs: Observation, *, mode: str = "local", repeat: int = 1,
             duration_s: float = 0.0) -> ScenarioResult:
    """Pure comparison of one observation with the scenario's expectation."""
    exp = scenario.expected
    criteria: list[Criterion] = []
    if obs.error:
        criteria.append(Criterion(name="run", passed=False, reason=obs.error))
    criteria.append(_check("alert", exp.alert, obs.alert))
    if exp.top_condition is not None:
        criteria.append(_check("top_condition", exp.top_condition, obs.top_condition))
    if exp.action_type is not None:
        criteria.append(_check("action_type", exp.action_type, obs.action_type))
    if exp.alert:
        t = obs.trigger_to_message_s
        if t is None:
            criteria.append(Criterion(name="trigger_to_message", passed=False, reason="no message to the farmer was seen"))
        else:
            criteria.append(Criterion(
                name="trigger_to_message", passed=t <= exp.max_trigger_to_message_s,
                reason=f"{t:.1f}s (limit {exp.max_trigger_to_message_s:g}s)"))
    return ScenarioResult(
        scenario_id=scenario.scenario_id, mode=mode, repeat=repeat, expected=exp, observed_alert=obs.alert,
        observed_top_condition=obs.top_condition, observed_action_type=obs.action_type,
        trigger_to_message_s=obs.trigger_to_message_s, investigation_id=obs.investigation_id, criteria=criteria,
        passed=all(c.passed for c in criteria), duration_s=round(duration_s, 2), error=obs.error, notes=obs.notes)


# ---------------------------------------------------------------- local mode


class Capture:
    """What a recording Recorder saw: outbound messages with their times, and the concluded event."""

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.messages: list[tuple[float, str]] = []
        self.concluded: dict | None = None

    def message(self, text: str) -> None:
        self.messages.append((self.clock(), text))

    def on_status(self, status: str, attrs: dict) -> None:
        """Recorder.set_status: a `message_sent_at` attribute means deliver() sent the step's text just before."""
        if attrs.get("message_sent_at") and attrs.get("last_text"):
            self.message(str(attrs["last_text"]))

    def on_event(self, model: Any) -> None:
        if type(model).__name__ == "InvestigationConcluded":
            self.concluded = model.model_dump(mode="json")


InvestigateFn = Callable[[C.StartInvestigation | C.ResumeInvestigation, Capture], C.AgentStep]


def default_analyze(request: C.FieldAnalysisRequest) -> C.FieldAnalysisResult:
    api = interfaces.analysis()
    if api is None or getattr(api, "analyze", None) is None:
        raise RunnerError("the field analyzer (fieldwatch.analysis.analyze) is not available")
    return api.analyze(request)


def default_decide(result: C.FieldAnalysisResult, z_threshold: float, min_region_acres: float, field_id: str,
                   date: str):
    """The scene pipeline's decision rule, reused as-is (cloud/lambdas/scenes/decide.py `evaluate`)."""
    from cloud.lambdas.scenes import decide

    return decide.evaluate(result, z_threshold, min_region_acres, field_id, date)


def default_investigate(req: C.StartInvestigation | C.ResumeInvestigation, capture: Capture) -> C.AgentStep:
    """investigation.run_request with a Recorder that keeps everything in memory (no DynamoDB, no EventBridge)."""
    from fieldwatch import investigation as inv

    class CaptureRecorder(inv.Recorder):
        def set_status(self, status, *, remove=(), **attrs):
            capture.on_status(status, attrs)

        def progress(self, *args, **kwargs):
            pass

        def _put(self, model):
            capture.on_event(model)

    rec = CaptureRecorder(req.investigation_id, getattr(req, "farm_id", "") or "x", getattr(req, "field", "") or "x")
    return inv.run_request(req, rec)


@dataclass
class LocalDeps:
    analyze: Callable[[C.FieldAnalysisRequest], C.FieldAnalysisResult] = default_analyze
    decide: Callable[..., Any] = default_decide
    investigate: InvestigateFn = default_investigate
    clock: Callable[[], float] = time.monotonic
    new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:8]
    fixtures: Path = FIXTURES_DIR


def polygon_of(f: dict) -> list[list[float]]:
    """Field ring [lon, lat] from the fixture's `polygon`, else its `bbox` [west, south, east, north]."""
    if f.get("polygon"):
        ring = [[float(p[0]), float(p[1])] for p in f["polygon"]]
        return ring[:-1] if len(ring) > 3 and ring[0] == ring[-1] else ring
    if f.get("bbox"):
        w, s, e, n = (float(v) for v in f["bbox"])
        return [[w, s], [e, s], [e, n], [w, n]]
    raise RunnerError(f"field {f.get('id')!r} has neither a polygon nor a bbox")


def build_request(scenario: C.Scenario, farm: dict, f: dict) -> C.FieldAnalysisRequest:
    crop = str(f.get("crop") or "").lower()
    crop = crop if crop in ("corn", "soybeans", "wheat", "milo", "alfalfa") else "other"
    return C.FieldAnalysisRequest(
        field=C.FieldRef(farm_id=farm["farm_id"], field_id=scenario.field_id), polygon=polygon_of(f), crop=crop,
        as_of=scenario.as_of)


def _anomaly_event(req: C.FieldAnalysisRequest, result: C.FieldAnalysisResult, decision: Any,
                   occurred_at: str) -> C.AnomalyDetected | None:
    """The AnomalyDetected the pipeline would have emitted; None when the pieces needed are not there."""
    if result.overlays is None or not decision.regions:
        return None
    return C.AnomalyDetected(
        occurred_at=occurred_at, anomaly_id=decision.anomaly_id or f"anm-{req.field.field_id}-{req.as_of}",
        field=req.field, scene_id=result.scene_id, date=req.as_of, regions=decision.regions,
        overlays=result.overlays, open_watch_ids=[], replay=True)


def _action_from_diagnosis(diag: dict | None) -> str | None:
    return ACTION_TYPES.get(str((diag or {}).get("recommended_action"))) if diag else None


@contextlib.contextmanager
def farm_file(path: Path) -> Iterator[None]:
    """Point the in-process tools (farmdata -> config.FARM_FILE) at the scenario's fixture for one run."""
    old = config.FARM_FILE
    config.FARM_FILE = path
    try:
        yield
    finally:
        config.FARM_FILE = old


class LocalRunner:
    mode = "local"

    def __init__(self, deps: LocalDeps | None = None):
        self.deps = deps or LocalDeps()

    def run(self, scenario: C.Scenario) -> Observation:
        d = self.deps
        path, farm = load_farm(scenario, d.fixtures)
        f = find_field(farm, scenario)
        request = build_request(scenario, farm, f)
        obs = Observation()
        t0 = d.clock()  # trigger: the start of analysis
        result = d.analyze(request)
        decision = d.decide(result, request.z_threshold, request.min_region_acres, scenario.field_id, scenario.as_of)
        if not decision.emit:
            obs.notes.append(f"no alert: {decision.reason}")
            return obs
        obs.alert = True
        inv_id = f"inv-eval-{scenario.scenario_id}-{d.new_id()}"
        obs.investigation_id = inv_id
        capture = Capture(d.clock)
        start = C.StartInvestigation(
            investigation_id=inv_id, farm_id=farm["farm_id"], field=scenario.field_id,
            anomaly=_anomaly_event(request, result, decision, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        with farm_file(path):
            step = self._step(start, capture)
            for i, reply in enumerate(scenario.scripted_replies):
                if step.status != "waiting_for_farmer":
                    obs.notes.append(f"{len(scenario.scripted_replies) - i} scripted reply(ies) unused: agent was {step.status}")
                    break
                step = self._step(C.ResumeInvestigation(investigation_id=inv_id, interrupt_id=step.interrupt_id or "",
                                                         text=reply["text"]), capture)
        if step.status == "failed":
            obs.error = f"investigation failed: {step.error.message if step.error else 'unknown error'}"
        obs.messages = [text for _, text in capture.messages]
        if capture.messages:
            obs.trigger_to_message_s = round(capture.messages[0][0] - t0, 3)
        concluded = capture.concluded
        if concluded:
            obs.top_condition = concluded.get("condition")
            obs.action_type = (concluded.get("proposed_action") or {}).get("action_type")
        else:
            obs.top_condition = (step.diagnosis or {}).get("condition")
            obs.action_type = _action_from_diagnosis(step.diagnosis)
            obs.notes.append("investigation did not conclude (still waiting for the farmer)" if
                             step.status == "waiting_for_farmer" else "no InvestigationConcluded event captured")
        return obs

    def _step(self, req: C.StartInvestigation | C.ResumeInvestigation, capture: Capture) -> C.AgentStep:
        before = len(capture.messages)
        step = self.deps.investigate(req, capture)
        if step.text and len(capture.messages) == before:  # the recorder did not see the send; use the step's return
            capture.message(step.text)
        return step


# ---------------------------------------------------------------- cloud mode

HttpFn = Callable[..., "tuple[int, dict]"]


def requests_http(method: str, url: str, json: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    """The real HTTP function: (status, parsed JSON body). Network failures come back as status 0."""
    import requests

    try:
        resp = requests.request(method, url, json=json, headers=headers, timeout=30)
    except requests.RequestException as e:
        return 0, {"error": {"message": f"{type(e).__name__}: {e}"}}
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:200]}
    return resp.status_code, data if isinstance(data, dict) else {"raw": data}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _err(data: dict) -> str:
    e = data.get("error")
    return str(e.get("message") if isinstance(e, dict) else e or data.get("raw") or data)[:200]


@dataclass
class CloudConfig:
    api: str
    passcode: str
    farm_id: str | None = None
    person_id: str | None = None
    timeout_s: float = 180
    poll_s: float = 3
    settle_s: float = 20  # a no-alert scenario passes once the scene analysis shows up and this long passes quietly
    replay_gap_s: float = REPLAY_GAP_S + 2


class CloudRunner:
    mode = "cloud"

    def __init__(self, cfg: CloudConfig, *, http: HttpFn = requests_http, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], datetime] = _utcnow,
                 fixtures: Path = FIXTURES_DIR):
        self.cfg, self.http, self.sleep, self.clock, self.wall, self.fixtures = cfg, http, sleep, clock, wall, fixtures
        self.token: str | None = None
        self._last_replay: float | None = None

    # -- http
    def _call(self, method: str, path: str, body: dict | None = None, *, auth: bool = True) -> tuple[int, dict]:
        url = self.cfg.api.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else None
        status, data = self.http(method, url, json=body, headers=headers)
        if status == 401 and auth:  # the 12-hour dashboard token expired: log in again once
            self._login()
            status, data = self.http(method, url, json=body, headers={"Authorization": f"Bearer {self.token}"})
        return status, data

    def _login(self) -> None:
        status, data = self._call("POST", "/v1/auth/dashboard", {"passcode": self.cfg.passcode}, auth=False)
        if status != 200 or not data.get("token"):
            raise RunnerError(f"dashboard login failed (HTTP {status}): {_err(data)}")
        self.token = str(data["token"])

    def _must(self, what: str, method: str, path: str, body: dict | None = None) -> dict:
        status, data = self._call(method, path, body)
        if status not in (200, 202):
            raise RunnerError(f"{what} failed (HTTP {status}): {_err(data)}")
        return data

    # -- pieces
    def _overview(self, farm_id: str, field_id: str) -> tuple[set[str], list[dict], tuple]:
        """(ids of open investigations on the field, those rows, the field's (latest_scene_date, latest_score))."""
        data = self._must("overview", "GET", f"/v1/farms/{farm_id}/overview")
        rows = [i for i in data.get("open_investigations", [])
                if i.get("field_id") == field_id and i.get("kind") != "question"]  # question runs are not investigations
        row = next((f for f in data.get("fields", []) if f.get("field_id") == field_id), {})
        return {str(i.get("investigation_id")) for i in rows}, rows, (row.get("latest_scene_date"), row.get("latest_score"))

    def _replay(self, body: dict) -> tuple[float, datetime]:
        """POST /v1/replay, spaced by the API's rate limit; returns the (monotonic, wall) trigger times."""
        for attempt in range(3):
            if self._last_replay is not None:
                wait = self.cfg.replay_gap_s - (self.clock() - self._last_replay)
                if wait > 0:
                    self.sleep(wait)
            trigger = (self.clock(), self.wall())
            status, data = self._call("POST", "/v1/replay", body)
            self._last_replay = self.clock()
            if status in (200, 202):
                return trigger
            if status != 429 or attempt == 2:
                raise RunnerError(f"replay failed (HTTP {status}): {_err(data)}")
        raise RunnerError("replay failed")  # unreachable

    def _person_id(self, reply: dict, item: dict, farm: dict) -> str:
        farmer = farm.get("farmer") or {}
        return str(reply.get("person_id") or self.cfg.person_id or item.get("person_id")
                   or farmer.get("id") or farmer.get("name") or "farmer")

    # -- one scenario
    def run(self, scenario: C.Scenario) -> Observation:
        cfg = self.cfg
        _, farm = load_farm(scenario, self.fixtures)
        find_field(farm, scenario)
        farm_id = cfg.farm_id or farm["farm_id"]
        if self.token is None:
            self._login()
        self._must("reset", "POST", "/v1/replay/reset", {"farm_id": farm_id})
        known, _, signature = self._overview(farm_id, scenario.field_id)
        t_mono, t_wall = self._replay({"field_ids": [scenario.field_id], "as_of": scenario.as_of, "farm_id": farm_id})
        deadline = t_mono + cfg.timeout_s

        obs = Observation()
        inv_id: str | None = None
        item: dict = {}
        replies = list(scenario.scripted_replies)
        answered: set[str] = set()
        first_sent: str | None = None
        first_seen_s: float | None = None
        analysis_seen: float | None = None
        finished = False
        while not finished:
            _, rows, sig = self._overview(farm_id, scenario.field_id)
            if inv_id is None:
                new = sorted((r for r in rows if str(r.get("investigation_id")) not in known),
                             key=lambda r: str(r.get("created_at") or ""))
                inv_id = str(new[0]["investigation_id"]) if new else None
            if inv_id is not None:
                status, data = self._call("GET", f"/v1/investigations/{inv_id}")
                if status == 200:
                    item = data
                if item.get("last_text") and item["last_text"] not in obs.messages:
                    obs.messages.append(str(item["last_text"]))
                if first_sent is None and item.get("message_sent_at"):
                    first_sent = str(item["message_sent_at"])
                    first_seen_s = self.clock() - t_mono
                state, pending = item.get("status"), item.get("pending_interrupt_id")
                waiting = state in ("awaiting_reply", "awaiting_upload") and pending not in answered
                if waiting and replies:
                    reply = replies.pop(0)
                    body = {"person_id": self._person_id(reply, item, farm), "text": reply["text"], "farm_id": farm_id}
                    self._must("mirror reply", "POST", "/v1/mirror/reply", body)
                    answered.add(str(pending))
                elif state in ("concluded", "failed") or (waiting and not replies):
                    finished = True
            else:
                if sig != signature and analysis_seen is None:
                    analysis_seen = self.clock()
                if (not scenario.expected.alert and analysis_seen is not None
                        and self.clock() - analysis_seen >= cfg.settle_s):
                    break  # the scene was analyzed and no investigation followed
            if not finished and self.clock() >= deadline:
                obs.error = (f"timed out after {cfg.timeout_s:g}s waiting for the investigation to finish "
                             f"(status {item.get('status')})" if inv_id else
                             f"timed out after {cfg.timeout_s:g}s: " + ("no investigation started" if scenario.expected.alert
                                                                         else "no scene analysis was seen"))
                break
            if not finished:
                self.sleep(cfg.poll_s)

        if inv_id is None:
            return obs
        obs.alert, obs.investigation_id = True, inv_id
        diag = item.get("diagnosis") if isinstance(item.get("diagnosis"), dict) else None
        if diag is None and item.get("diagnosis_json"):
            with contextlib.suppress(ValueError, TypeError):
                diag = json.loads(item["diagnosis_json"])
        obs.top_condition = item.get("condition") or (diag or {}).get("condition")
        obs.action_type = (item.get("proposed_action") or {}).get("action_type") or _action_from_diagnosis(diag)
        sent = _parse_time(first_sent)
        if sent is not None:
            obs.trigger_to_message_s = round(max(0.0, (sent - t_wall).total_seconds()), 3)
            if (sent - t_wall).total_seconds() < 0:
                obs.notes.append("message_sent_at is before the replay request: clock skew, reported as 0")
        elif first_seen_s is not None:
            obs.trigger_to_message_s = round(first_seen_s, 3)
            obs.notes.append("message_sent_at unusable; time is when polling first saw the message")
        return obs


# ---------------------------------------------------------------- running and reporting


def run_suite(scenarios: list[C.Scenario], runner: Any, repeat: int = 1, *, clock: Callable[[], float] = time.monotonic,
              out: Callable[[str], None] = print) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for n in range(1, repeat + 1):
        for sc in scenarios:
            out(f"[run {n}/{repeat}] {sc.scenario_id} ...")
            t = clock()
            try:
                obs = runner.run(sc)
            except Exception as e:  # a broken scenario must not stop the suite
                obs = Observation(error=f"{type(e).__name__}: {e}"[:300])
            results.append(evaluate(sc, obs, mode=runner.mode, repeat=n, duration_s=clock() - t))
    return results


def _yn(v: bool) -> str:
    return "yes" if v else "no"


def format_report(results: list[ScenarioResult], repeat: int) -> str:
    head = ["SCENARIO", "RUN", "ALERT(exp/saw)", "CONDITION", "ACTION", "T2M(s)", "RESULT"]
    rows = [[r.scenario_id, str(r.repeat), f"{_yn(r.expected.alert)}/{_yn(r.observed_alert)}",
             r.observed_top_condition or "-", r.observed_action_type or "-",
             "-" if r.trigger_to_message_s is None else f"{r.trigger_to_message_s:.1f}",
             "PASS" if r.passed else "FAIL"] for r in results]
    widths = [max(len(x[i]) for x in [head, *rows]) for i in range(len(head))]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt = lambda cells: "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"  # noqa: E731
    lines = [line, fmt(head), line, *[fmt(r) for r in rows], line]
    for r in results:
        bad = [f"{c.name}: {c.reason}" for c in r.criteria if not c.passed]
        if bad:
            lines.append(f"FAIL {r.scenario_id} (run {r.repeat}): " + "; ".join(bad))
        for note in r.notes:
            lines.append(f"note {r.scenario_id} (run {r.repeat}): {note}")
    passed = sum(r.passed for r in results)
    green = sum(all(r.passed for r in results if r.repeat == n) for n in range(1, repeat + 1))
    summary = f"{passed}/{len(results)} scenario runs passed; {green}/{repeat} repeat(s) fully green"
    if repeat >= 3 and green == repeat:
        summary += " - pitch rule met (3 green runs in a row)"
    return "\n".join([*lines, summary])


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m eval.runner", description="Run FieldWatch scenarios and check outcomes.")
    p.add_argument("--mode", choices=("local", "cloud"), default="local")
    p.add_argument("--scenario", action="append", metavar="ID", help="scenario id (repeatable; default all)")
    p.add_argument("--repeat", type=int, default=1, metavar="N", help="runs of the whole suite (pitch rule: 3 green)")
    p.add_argument("--api", default=os.getenv("FIELDWATCH_API_URL"), help="cloud: API base URL (env FIELDWATCH_API_URL)")
    p.add_argument("--farm-id", help="cloud: farm id (default: the scenario fixture's farm_id)")
    p.add_argument("--person-id", default=os.getenv("FIELDWATCH_EVAL_PERSON_ID"),
                   help="cloud: person id for scripted replies (env FIELDWATCH_EVAL_PERSON_ID)")
    p.add_argument("--timeout", type=float, default=180, metavar="SECONDS", help="cloud: per-scenario wait")
    p.add_argument("--json", metavar="PATH", help="also write machine-readable results here")
    return p


def make_runner(args: argparse.Namespace) -> Any:
    if args.mode == "local":
        return LocalRunner()
    passcode = os.getenv("FIELDWATCH_DASHBOARD_PASSCODE")
    if not args.api:
        raise RunnerError("cloud mode needs --api or env FIELDWATCH_API_URL")
    if not passcode:
        raise RunnerError("cloud mode needs env FIELDWATCH_DASHBOARD_PASSCODE")
    return CloudRunner(CloudConfig(api=args.api, passcode=passcode, farm_id=args.farm_id, person_id=args.person_id,
                                   timeout_s=args.timeout))


def main(argv: list[str] | None = None, *, runner: Any = None, scenarios_dir: Path = SCENARIOS_DIR,
         out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    if args.repeat < 1:
        out("--repeat must be at least 1")
        return 2
    try:
        scenarios = load_scenarios(scenarios_dir, args.scenario)
        if not scenarios:
            raise RunnerError(f"no scenarios found in {scenarios_dir} (files starting with _ are skipped)")
        runner = runner or make_runner(args)
    except RunnerError as e:
        out(f"error: {e}")
        return 2
    results = run_suite(scenarios, runner, args.repeat, out=out)
    out(format_report(results, args.repeat))
    if args.json:
        payload = {"mode": runner.mode, "repeat": args.repeat, "passed": all(r.passed for r in results),
                   "results": [r.model_dump(mode="json") for r in results]}
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
