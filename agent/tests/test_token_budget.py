"""Guardrail: a per-investigation token budget shared by the supervisor and its specialists (hooks.TokenBudget).
Scripted model, no network. Usage per model call is inflated to 100 tokens so the limits are easy to hit."""
import json

import pytest
from moto import mock_aws

import fieldwatch.agents as agents
import fieldwatch.investigation as inv
from fieldwatch import config
from fieldwatch import contracts as C
from fieldwatch.hooks import DEFAULT_TOKEN_BUDGET, TokenBudget, TokenBudgetExceeded, ToolTimeline, token_budget_from_env
from fieldwatch.schemas import Diagnosis
from runtime_support import REGION, make_bus_with_queue, make_investigations_table
from test_agents import ScriptedModel

PER_CALL = 100  # tokens the scripted model reports per call: 60 in + 40 out


class MeteredModel(ScriptedModel):
    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        async for e in super().stream(messages, tool_specs, system_prompt, **kw):
            if "metadata" in e:
                e = {"metadata": {**e["metadata"], "usage": {"inputTokens": 60, "outputTokens": 40, "totalTokens": PER_CALL}}}
            yield e


@pytest.fixture
def scripted(monkeypatch):
    log = []
    monkeypatch.setattr(agents, "model", lambda: MeteredModel([("ask_imagery", {"question": "Look.", "field_id": "north-pivot"}),
                                                              ("ask_environment", {"question": "Weather?", "field_id": "north-pivot"})], log))
    monkeypatch.setattr(agents, "supervisor_model", lambda: MeteredModel([("ask_imagery", {"question": "Look.", "field_id": "north-pivot"}),
                                                                         ("ask_environment", {"question": "Weather?", "field_id": "north-pivot"})], log))
    return log


def _is_budget_error(exc: BaseException | None) -> bool:
    while exc is not None:
        if isinstance(exc, TokenBudgetExceeded):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _run_supervisor(budget):
    sup = agents.build_supervisor(ToolTimeline(lambda e: None), hooks=[budget])
    return sup("Investigate the field called 'north pivot'.", structured_output_model=Diagnosis)


def test_within_budget_the_run_finishes_and_usage_is_summed_across_agents(scripted):
    budget = TokenBudget(100_000)
    result = _run_supervisor(budget)
    assert isinstance(result.structured_output, Diagnosis)
    calls = len(scripted)
    assert calls > 3 and {"supervisor", "imagery", "environment"} <= set(scripted)  # supervisor AND specialists ran
    assert budget.used == calls * PER_CALL
    assert (budget.input_tokens, budget.output_tokens) == (calls * 60, calls * 40)


def test_over_budget_the_run_stops_before_the_next_model_call(scripted):
    budget = TokenBudget(250)
    with pytest.raises(Exception, match="token budget exceeded") as caught:  # the SDK may wrap it in EventLoopException
        _run_supervisor(budget)
    assert _is_budget_error(caught.value)
    # checked between calls: at most one call past the limit, and nothing after the stop
    assert 250 <= budget.used <= 250 + PER_CALL
    assert len(scripted) == budget.used // PER_CALL


def test_one_budget_is_shared_by_specialists_and_a_spent_budget_blocks_the_next(monkeypatch):
    log = []
    monkeypatch.setattr(agents, "model", lambda: MeteredModel([], log))
    budget = TokenBudget(300)
    ask_imagery, ask_environment, ask_agronomy = agents.build_specialists(ToolTimeline(lambda e: None), hooks=[budget])
    first = json.loads(ask_imagery(question="Look.", field_id="north-pivot"))  # tool step + structured answer = 2 calls
    assert first["agent"] == "imagery" and budget.used == 2 * PER_CALL
    second = json.loads(ask_environment(question="Weather?", field_id="north-pivot"))  # call 3 ok (300), call 4 refused
    assert budget.used == 3 * PER_CALL and "token budget exceeded" in second["error"]
    third = json.loads(ask_agronomy(question="Disease?", field_id="north-pivot"))
    assert "TokenBudgetExceeded" in third["error"] and third["evidence"] == []  # reported as an error note, not a guess
    assert "agronomy" not in log  # its model was never called


def test_a_failed_model_call_counts_nothing_and_missing_usage_is_ignored():
    budget = TokenBudget(10)

    class Ev:  # AfterModelCallEvent shapes: failed call, and a response with no usage metadata
        def __init__(self, stop_response):
            self.stop_response = stop_response

    class Resp:
        def __init__(self, message):
            self.message = message

    budget.after(Ev(None))
    budget.after(Ev(Resp({"role": "assistant", "content": []})))
    assert budget.used == 0


def test_limit_comes_from_the_env_with_a_safe_default(monkeypatch):
    monkeypatch.delenv("FIELDWATCH_TOKEN_BUDGET", raising=False)
    assert token_budget_from_env() == DEFAULT_TOKEN_BUDGET == 250_000 == TokenBudget().limit
    monkeypatch.setenv("FIELDWATCH_TOKEN_BUDGET", "1234")
    assert TokenBudget().limit == 1234
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv("FIELDWATCH_TOKEN_BUDGET", bad)
        assert token_budget_from_env() == DEFAULT_TOKEN_BUDGET
    assert TokenBudget(77).limit == 77  # an explicit limit beats the env


@mock_aws
def test_run_request_turns_an_exhausted_budget_into_a_logged_failed_step(scripted, monkeypatch, tmp_path, caplog):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(inv, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(config, "REGION", REGION)
    monkeypatch.setattr(config, "SESSIONS_BUCKET", "")
    monkeypatch.setattr(config, "TOOLS_BACKEND", "local")
    monkeypatch.setenv("FIELDWATCH_APPROVALS_BACKEND", "local")
    monkeypatch.setenv("FIELDWATCH_APPROVALS_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("FIELDWATCH_TOKEN_BUDGET", "250")  # the scripted model spends 100 per call
    table = make_investigations_table()
    make_bus_with_queue()
    payload = json.loads((config.FIXTURES / "contracts" / "start_investigation.json").read_text())
    table.put_item(Item={"investigation_id": payload["investigation_id"], "farm_id": payload["farm_id"],
                         "field_id": "north-pivot", "created_at": "2026-07-18T17:21:05Z", "status": "queued"})

    with caplog.at_level("ERROR"):
        out = inv.run_request(C.StartInvestigation.model_validate(payload))

    assert out.status == "failed" and "token budget exceeded" in out.error.message
    item = table.get_item(Key={"investigation_id": payload["investigation_id"]})["Item"]
    assert item["status"] == "failed" and "token budget exceeded" in item["error"]
    assert "token budget exceeded" in caplog.text
