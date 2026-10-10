"""WS7: the AgentCore CLI project is wired to the real agent and holds no account IDs or third-party endpoints."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1]
PROJECT = AGENT / "cloud" / "agentcore"
CODE = PROJECT / "app" / "fieldwatch_agent"
SKIP_DIRS = {"node_modules", ".cli-tools", "staging", "__pycache__"}


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PROJECT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _project_files():
    staged = {CODE / "app.py", CODE / "fieldwatch", CODE / "catalog", CODE / "fixtures"}
    for p in PROJECT.rglob("*"):
        if p.is_file() and not (SKIP_DIRS & set(p.relative_to(PROJECT).parts)) and p.suffix in {".json", ".py", ".md", ".ts", ".toml"}:
            if not any(s == p or s in p.parents for s in staged):
                yield p


@pytest.fixture(scope="module")
def spec():
    return json.loads((PROJECT / "agentcore" / "agentcore.json").read_text())


def test_runtime_deploys_the_real_agent(spec):
    (rt,) = spec["runtimes"]
    assert (rt["build"], rt["protocol"], rt["entrypoint"], rt["runtimeVersion"]) == ("CodeZip", "HTTP", "app.py", "PYTHON_3_12")
    assert rt["codeLocation"] == "app/fieldwatch_agent/" and rt["networkMode"] == "PUBLIC"
    assert spec["memories"] == [] and spec["agentCoreGateways"] == []


def test_tags_and_tracing(spec):
    assert spec["tags"]["project"] == "fieldwatch" and spec["runtimes"][0]["tags"]["project"] == "fieldwatch"
    assert spec["runtimes"][0]["instrumentation"]["enableOtel"] is True


def test_model_and_backend_come_from_env(spec):
    env = {e["name"]: e["value"] for e in spec["runtimes"][0]["envVars"]}
    assert env["FIELDWATCH_MODEL"].startswith("us.") and env["FIELDWATCH_SSM_BOOTSTRAP"] == "1"
    assert env["FIELDWATCH_EVENT_BUS"] == "fieldwatch"
    assert not any(re.search(r"\d{12}", v) for v in env.values())


def test_template_leftovers_are_gone():
    assert not (CODE / "main.py").exists() and not (CODE / "mcp_client").exists() and not (CODE / "model").exists()
    for p in _project_files():
        assert p.suffix == ".md" or "mcp.exa.ai" not in p.read_text(errors="ignore"), p


def test_no_account_ids_committed():
    assert not (PROJECT / "agentcore" / "aws-targets.json").is_file() or "aws-targets.json" in (PROJECT / "agentcore" / ".gitignore").read_text()
    for p in _project_files():
        text = p.read_text(errors="ignore")
        found = [m for m in re.findall(r"(?<![\w-])\d{12}(?![\w-])", text) if m != "123456789012"]
        assert not found, f"{p}: {found}"
    assert "aws-targets.json" in (PROJECT / "agentcore" / ".gitignore").read_text()


def test_runtime_role_policy_covers_what_the_agent_does(spec):
    rel = spec["runtimes"][0]["additionalPolicies"][0]
    doc = json.loads((CODE / rel).read_text())
    actions = {a for s in doc["Statement"] for a in s["Action"]}
    assert {"bedrock:InvokeModel", "bedrock-agentcore:InvokeGateway", "events:PutEvents", "dynamodb:UpdateItem",
            "s3:PutObject", "s3:GetObject", "ssm:GetParameter"} <= actions
    resources = json.dumps(doc)
    assert "table/fw-investigations" in resources and "event-bus/fieldwatch" in resources and "/sessions/*" in resources
    assert "link-signing-key" not in resources and "table/fw-farms" not in resources  # least privilege


def test_stage_copies_the_real_source_without_duplicating_it(tmp_path):
    stage = _load("stage")
    code = tmp_path / "code"
    staged = stage.stage(AGENT, code)
    assert {"app.py", "fieldwatch", "catalog", "fixtures/contracts", "fixtures/demo_farm.json"} <= set(staged)
    assert (code / "app.py").read_text() == (AGENT / "app.py").read_text()
    assert (code / "fieldwatch" / "investigation.py").read_text() == (AGENT / "fieldwatch" / "investigation.py").read_text()
    assert (code / "fixtures" / "contracts" / "start_investigation.json").exists()
    assert not list(code.rglob("__pycache__"))
    stage.stage(AGENT, code)  # idempotent


def test_staged_paths_are_gitignored():
    ignore = (CODE / ".gitignore").read_text().splitlines()
    assert {"app.py", "fieldwatch/", "catalog/", "fixtures/"} <= set(ignore)


def test_aws_targets_built_from_the_caller_account():
    prep = _load("prepare_deploy")
    (t,) = prep.build_targets("210987654321")
    assert t == {"name": "default", "description": "FieldWatch (Caleb's account)", "account": "210987654321", "region": "us-west-2"}
    for bad in ("", "1234", "abcdefghijkl", "1234567890123"):
        with pytest.raises(ValueError):
            prep.build_targets(bad)
