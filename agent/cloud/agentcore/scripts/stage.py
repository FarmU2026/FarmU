#!/usr/bin/env python3
"""Copy the real FieldWatch agent into the CLI project's codeLocation (app/fieldwatch_agent/).

The CodeZip is built from that folder, so the deployed code is agent/app.py plus agent/fieldwatch (and the
tool catalog and contract samples) without a second copy of the source in git: the staged files are gitignored.
Run it before every `agentcore deploy` (prepare_deploy.py does), or by hand: python scripts/stage.py
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
AGENT = PROJECT.parents[1]  # agent/
CODE = PROJECT / "app" / "fieldwatch_agent"

# source (relative to agent/) -> destination (relative to the codeLocation)
FILES = {"app.py": "app.py", "fixtures/demo_farm.json": "fixtures/demo_farm.json"}
TREES = {"fieldwatch": "fieldwatch", "catalog": "catalog", "fixtures/contracts": "fixtures/contracts"}
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", "sessions", "data")


def stage(agent: Path = AGENT, code: Path = CODE) -> list[str]:
    """Replace the staged copies under `code` with fresh ones. Returns the staged paths."""
    staged = []
    for dest in TREES.values():
        target = code / dest
        if target.exists():
            shutil.rmtree(target)  # only the named staged folders, never the code folder itself
    for src, dest in TREES.items():
        if (agent / src).exists():
            shutil.copytree(agent / src, code / dest, ignore=_IGNORE)
            staged.append(dest)
    for src, dest in FILES.items():
        (code / dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(agent / src, code / dest)
        staged.append(dest)
    return staged


if __name__ == "__main__":
    print("staged:", ", ".join(stage()), file=sys.stderr)
