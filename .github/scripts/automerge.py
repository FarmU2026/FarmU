"""Merge open PRs into main once they pass, re-tested on top of the latest main.

Run by .github/workflows/automerge.yml. For each open, non-draft PR from this repo (oldest first):
  1. Skip if labeled `hold` or `needs-human`, or if any of its checks failed or are still running.
  2. If it touches a protected path (contracts, interfaces, workflows, IAM), label `needs-human` and skip.
  3. Merge it locally onto the current origin/main. Conflict -> label `needs-rebase`, comment, skip.
  4. Run the offline tests and `cdk synth` on the merged tree. Failure -> label `automerge-failed`, comment, skip.
  5. Squash-merge on GitHub (pinned to the tested head commit) and delete the branch.
After any merge, dispatch the deploy workflow (merges made with GITHUB_TOKEN don't trigger push workflows).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

PROTECTED = (
    "agent/fieldwatch/contracts.py",
    "agent/fieldwatch/interfaces.py",
    "agent/fixtures/contracts/",
    ".github/",
    "infra/policies/",
    "infra/stacks/access.py",
)
SKIP_LABELS = {"hold", "needs-human"}
LABELS = {
    "hold": "Automerge: leave this PR alone",
    "needs-human": "Automerge: touches contracts, workflows or IAM; a person merges it",
    "needs-rebase": "Automerge: conflicts with main",
    "automerge-failed": "Automerge: tests or synth failed on top of main",
}
OWN_CHECKS = {"merge"}  # this workflow's own job; never wait on it
PY = sys.executable
SYNTH_ENV = {
    "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent", "AWS_CONFIG_FILE": "/nonexistent",
    "CDK_DEFAULT_ACCOUNT": "123456789012", "CDK_DEFAULT_REGION": "us-west-2", "CDK_DISABLE_VERSION_CHECK": "1",
}


def sh(*args: str, cwd: str | None = None, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {})}, text=True,
                          capture_output=True, check=check)


def gh_json(*args: str):
    return json.loads(sh("gh", *args).stdout or "null")


def comment_once(number: int, marker: str, body: str) -> None:
    existing = gh_json("pr", "view", str(number), "--json", "comments")["comments"]
    if not any(marker in c["body"] for c in existing):
        sh("gh", "pr", "comment", str(number), "--body", f"{body}\n\n<!-- {marker} -->")


def label(number: int, name: str) -> None:
    sh("gh", "pr", "edit", str(number), "--add-label", name, check=False)


def unlabel(number: int, *names: str) -> None:
    for n in names:
        sh("gh", "pr", "edit", str(number), "--remove-label", n, check=False)


def checks_green(pr: dict) -> bool:
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("context") or ""
        if name in OWN_CHECKS:
            continue
        status = c.get("status") or c.get("state")
        conclusion = (c.get("conclusion") or c.get("state") or "").upper()
        if status and status.upper() not in ("COMPLETED", "SUCCESS"):
            return False
        if conclusion not in ("SUCCESS", "SKIPPED", "NEUTRAL"):
            return False
    return True


def verify(number: int, head_sha: str) -> tuple[bool, str]:
    """Merge the PR onto origin/main locally and run tests + synth. Returns (ok, reason)."""
    sh("git", "fetch", "-q", "origin", "main", f"pull/{number}/head:pr-{number}")
    sh("git", "checkout", "-q", "-B", "automerge-try", "origin/main")
    merge = sh("git", "merge", "--no-ff", "--no-edit", f"pr-{number}", check=False)
    if merge.returncode != 0:
        sh("git", "merge", "--abort", check=False)
        return False, "conflict"
    # The job installed main's requirements; pick up anything the PR adds (e.g. a new library).
    deps = sh(PY, "-m", "pip", "install", "-q", "-r", "agent/requirements-dev.txt", "-r", "infra/requirements.txt",
              check=False)
    if deps.returncode != 0:
        return False, "dependency install failed:\n```\n" + (deps.stdout + deps.stderr)[-3000:] + "\n```"
    tests = sh(PY, "-m", "pytest", "tests", "-q", "-m", "not live", "-p", "no:cacheprovider", cwd="agent", check=False)
    if tests.returncode != 0:
        return False, "tests failed:\n```\n" + (tests.stdout + tests.stderr)[-3000:] + "\n```"
    synth = sh("npx", "--yes", "aws-cdk@2", "synth", "--quiet", cwd="infra", env=SYNTH_ENV, check=False)
    if synth.returncode != 0:
        return False, "cdk synth failed:\n```\n" + (synth.stdout + synth.stderr)[-3000:] + "\n```"
    return True, "ok"


def main() -> int:
    for name, desc in LABELS.items():
        sh("gh", "label", "create", name, "--description", desc, "--force", check=False)
    owner = os.environ["GITHUB_REPOSITORY"].split("/")[0]
    prs = gh_json("pr", "list", "--base", "main", "--state", "open", "--limit", "50", "--json",
                  "number,title,isDraft,labels,files,headRefOid,headRepositoryOwner,statusCheckRollup,createdAt")
    merged = 0
    for pr in sorted(prs or [], key=lambda p: p["createdAt"]):
        n, labels = pr["number"], {l["name"] for l in pr["labels"]}
        if pr["isDraft"] or labels & SKIP_LABELS or (pr.get("headRepositoryOwner") or {}).get("login") != owner:
            continue
        files = [f["path"] for f in pr.get("files") or []]
        touched = sorted({p for p in files for prot in PROTECTED if p.startswith(prot)})
        if touched:
            label(n, "needs-human")
            comment_once(n, "automerge-protected", "Automerge skipped this PR because it touches shared or "
                         "security-sensitive paths: " + ", ".join(f"`{t}`" for t in touched) + ". A person merges it.")
            continue
        if not checks_green(pr):
            continue
        ok, reason = verify(n, pr["headRefOid"])
        if not ok:
            if reason == "conflict":
                label(n, "needs-rebase")
                comment_once(n, f"automerge-conflict-{pr['headRefOid'][:7]}",
                             "Automerge: this PR conflicts with `main`. Merge `main` into the branch and push.")
            else:
                label(n, "automerge-failed")
                comment_once(n, f"automerge-failed-{pr['headRefOid'][:7]}",
                             "Automerge: on top of the latest `main` this PR " + reason)
            continue
        unlabel(n, "needs-rebase", "automerge-failed")
        result = sh("gh", "pr", "merge", str(n), "--squash", "--delete-branch",
                    "--match-head-commit", pr["headRefOid"], check=False)
        if result.returncode == 0:
            merged += 1
            print(f"merged #{n}: {pr['title']}")
        else:
            print(f"merge of #{n} refused: {result.stderr.strip()[:300]}")
    if merged:
        sh("gh", "workflow", "run", "deploy.yml", "--ref", "main", check=False)
        print(f"{merged} PR(s) merged; deploy dispatched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
