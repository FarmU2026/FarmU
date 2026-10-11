"""web/agents.json (the "How FieldWatch thinks" page data) must match the agent code.

The generator runs in a subprocess with the model env vars pinned, so a developer's agent/.env cannot change the
committed file. Every tool, specialist and data source is checked against the code it claims to come from.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fieldwatch import agents as A
from fieldwatch import config
from fieldwatch import tools as T
from fieldwatch.agents import EvidenceLedger
from fieldwatch.hooks import ToolTimeline
from web_export import agents_page as P

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO = AGENT_DIR.parent
COMMITTED = REPO / "web" / "agents.json"


def _generated_text() -> str:
    env = {**os.environ, "FIELDWATCH_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
           "FIELDWATCH_SUPERVISOR_MODEL": ""}
    out = subprocess.run([sys.executable, "-m", "web_export.agents_page", "--stdout"], cwd=AGENT_DIR, env=env,
                         capture_output=True, text=True, check=True, timeout=120)
    return out.stdout


@pytest.fixture(scope="module")
def page() -> dict:
    return json.loads(COMMITTED.read_text(encoding="utf-8"))


def _names(block: list[dict]) -> list[str]:
    return [t["name"] for t in block]


def test_committed_json_is_exactly_what_the_generator_writes():
    assert COMMITTED.read_text(encoding="utf-8") == _generated_text(), (
        "web/agents.json is stale: run `python -m web_export.agents_page` from agent/ and commit the result")


def test_every_specialist_in_roster_appears_with_its_tools(page):
    assert [s["name"] for s in page["specialists"]] == list(A.SPECIALIST_TOOLS)
    for s in page["specialists"]:
        assert _names(s["tools"]) == A.SPECIALIST_TOOLS[s["name"]]


def test_each_listed_tool_is_one_the_agent_really_has(page):
    # Build the agents the way the code does and compare tool names, so the page cannot list a tool that is absent.
    for s in page["specialists"]:
        agent = A._specialist(s["name"], A._pick(A.SPECIALIST_TOOLS[s["name"]], None), ToolTimeline())
        assert sorted(agent.tool_names) == sorted(_names(s["tools"])), s["name"]
    supervisor = A.build_supervisor(ToolTimeline())
    listed = _names(page["supervisor"]["tools"]) + _names(page["scorer"]["tools"]) + _names(page["approval"]["tools"])
    assert sorted(supervisor.tool_names) == sorted(listed)


def test_supervisor_and_scorer_tool_lists_match_the_code(page):
    assert set(A.SUPERVISOR_WRITE_TOOLS) == set(_names(page["approval"]["tools"]))
    assert set(A.SUPERVISOR_READ_TOOLS) <= set(_names(page["supervisor"]["tools"]))
    assert _names(page["scorer"]["tools"]) == ["precheck_evidence", "score_hypotheses", "estimate_impact"]
    assert {t.tool_name for t in T.MESSAGE_TOOLS} <= set(_names(page["supervisor"]["tools"]))


def test_write_tools_say_they_need_the_farmers_yes(page):
    for t in page["approval"]["tools"]:
        assert t["gate"] == P.GATE


def test_every_tool_has_a_description_from_code(page):
    objects = P.tool_objects()
    for block in [page["supervisor"]["tools"], page["scorer"]["tools"], page["approval"]["tools"],
                  *[s["tools"] for s in page["specialists"]]]:
        for t in block:
            assert t["label"] and t["description"], t["name"]
            assert t["description"] == P.first_paragraph(objects[t["name"]].tool_spec["description"])


def test_every_source_is_named_in_the_code_it_claims(page):
    ids = {s["id"] for s in P.SOURCES}
    for src in P.SOURCES:
        path = REPO / src["code"]
        assert path.is_file(), src["code"]
        assert src["check"] in path.read_text(encoding="utf-8"), f"{src['id']}: {src['check']!r} not in {src['code']}"
        assert src["id"] in ids
    for name, keys in P.TOOL_SOURCES.items():
        assert set(keys) <= ids, name
    for row in page["sources"]:
        assert row["used_by"] or row.get("recall"), f"{row['id']} is listed but nothing uses it"


def test_every_data_source_in_a_tool_is_on_the_page(page):
    shown = {s["id"] for s in page["sources"]}
    used = {k for keys in P.TOOL_SOURCES.values() for k in keys}
    assert used <= shown
    assert shown - used == {"memory"}  # memory feeds the supervisor's prompt, not a tool


def test_services_appear_in_infra_or_agentcore_project(page):
    for svc in P.SERVICES:
        for code, check in svc["evidence"]:
            path = REPO / code
            assert path.is_file(), code
            assert check in path.read_text(encoding="utf-8"), f"{svc['names']}: {check!r} not in {code}"
    assert [s["name"] for s in page["services"]] == [" + ".join(s["names"]) for s in P.SERVICES]


def test_models_come_from_the_code_defaults(page):
    assert page["models"]["specialists"] == P.model_name(config.MODEL_ID)
    assert page["supervisor"]["model"] == P.model_name(A.SUPERVISOR_MODEL_ID or config.MODEL_ID)


def test_model_names_are_friendly_for_known_ids():
    assert P.model_name("us.anthropic.claude-haiku-4-5-20251001-v1:0") == "Claude Haiku 4.5"
    assert P.model_name("us.anthropic.claude-sonnet-4-5-20250929-v1:0") == "Claude Sonnet 4.5"
    assert P.model_name("some.unknown-model") == "some.unknown-model"
