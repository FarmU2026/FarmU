"""Every SSM parameter the Runtime agent loads at startup (app._SSM_ENV) must be created by some stack, or the agent
silently runs without it (Oct 10: no /fieldwatch/gateway-url, so every tool fell back to the local versions)."""
import ast
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
STACKS = AGENT.parent / "infra" / "stacks"
# Written by hand on purpose (see HANDOFF / agent/cloud/agentcore/README.md).
BY_HAND = {"/fieldwatch/runtime-arn"}


def _ssm_env() -> dict:
    tree = ast.parse((AGENT / "app.py").read_text())
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if getattr(target, "id", None) == "_SSM_ENV":
                return ast.literal_eval(node.value)
    raise AssertionError("app._SSM_ENV not found")


def test_every_bootstrap_parameter_is_created_by_a_stack():
    source = "\n".join(p.read_text() for p in STACKS.glob("*.py"))
    missing = [name for name in _ssm_env() if name not in BY_HAND and f'"{name}"' not in source]
    assert not missing, f"no stack creates {missing}"
