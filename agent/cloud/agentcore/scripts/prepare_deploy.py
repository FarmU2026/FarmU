#!/usr/bin/env python3
"""Everything to do before `agentcore deploy`, from this folder's parent (agent/cloud/agentcore):

    python scripts/prepare_deploy.py            # then: npx agentcore deploy

1. Stages the agent source (scripts/stage.py).
2. Writes agentcore/aws-targets.json from the CURRENT AWS credentials' account (aws sts get-caller-identity),
   so no account ID is ever committed (the file is gitignored). Region is us-west-2 (docs/decisions.md D3).

Deploy only as Caleb / the deploy workflow (docs/decisions.md D3). This script reads your account ID and writes
local files; it deploys nothing.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage  # noqa: E402

REGION = "us-west-2"
TARGETS = stage.PROJECT / "agentcore" / "aws-targets.json"


def account_from_sts() -> str:
    out = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return out


def build_targets(account: str, region: str = REGION) -> list[dict]:
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError(f"expected a 12-digit AWS account ID, got {account!r}")
    return [{"name": "default", "description": "FieldWatch (Caleb's account)", "account": account, "region": region}]


def main() -> None:
    staged = stage.stage()
    print("staged:", ", ".join(staged), file=sys.stderr)
    account = account_from_sts()
    TARGETS.write_text(json.dumps(build_targets(account), indent=2) + "\n")
    print(f"wrote {TARGETS} for account ending ...{account[-4:]}, region {REGION}", file=sys.stderr)


if __name__ == "__main__":
    main()
