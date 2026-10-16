"""Markdown summary of a results file, for pasting into a PR.

    python -m eval.conversations.report results/<file>.json [--max-failures N]

Pass rates count deterministic checks only (checks that could not be evaluated are left out). Judge scores, when the
run used `--judge`, are in their own section and never change a pass rate.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable


def _checks(data: dict):
    for case in data.get("cases", []):
        for turn in case.get("turns", []):
            for chk in turn.get("checks", []):
                yield case, turn, chk


def pass_rates(data: dict) -> dict[str, tuple[int, int]]:
    """check type -> (passed, evaluated)."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for _, _, chk in _checks(data):
        if chk.get("passed") is None:
            continue
        counts[chk["type"]][1] += 1
        counts[chk["type"]][0] += bool(chk["passed"])
    return {k: (v[0], v[1]) for k, v in sorted(counts.items())}


def _signature_key(sig: dict | None) -> str | None:
    if not sig:
        return None
    return json.dumps(sig, sort_keys=True)


def stability(data: dict) -> dict[str, dict]:
    """Per case: how often repeats agree on what they claim. For each turn, the share of repeats whose
    (cause, tier, place) equals the most common one; the case figure is the mean over its turns.
    Needs --repeat >= 2; otherwise runs = 1 and agreement is None."""
    by_case: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    runs: dict[str, int] = defaultdict(int)
    for case in data.get("cases", []):
        if case.get("status") != "ok":
            continue
        runs[case["name"]] += 1
        for turn in case.get("turns", []):
            key = _signature_key(turn.get("signature"))
            if key is not None:
                by_case[case["name"]][turn["index"]].append(key)
    out: dict[str, dict] = {}
    for name, n in runs.items():
        turns = by_case.get(name, {})
        rates = [max(Counter(keys).values()) / len(keys) for keys in turns.values() if len(keys) >= 2]
        disagree = [i for i, keys in turns.items() if len(keys) >= 2 and len(set(keys)) > 1]
        out[name] = {"runs": n, "agreement": (sum(rates) / len(rates)) if rates else None, "unstable_turns": sorted(disagree)}
    return out


def _pct(a: int, b: int) -> str:
    return "-" if not b else f"{100 * a / b:.0f}% ({a}/{b})"


def _quote(text: str) -> str:
    return "> " + text.replace("\n", "\n> ") if text else "> (no answer)"


def render(data: dict, max_failures: int = 25) -> str:
    cases = data.get("cases", [])
    rates = pass_rates(data)
    total_p = sum(p for p, _ in rates.values())
    total_n = sum(n for _, n in rates.values())
    runs_ok = sum(1 for c in cases if c.get("passed"))
    lines = [
        "## Conversation eval",
        "",
        f"- API: `{data.get('api')}`  farm: `{data.get('farm_id')}`",
        f"- Run: {data.get('started_at')} to {data.get('finished_at')}, repeat x{data.get('repeat', 1)}"
        + (f", judge `{data.get('judge_model')}`" if data.get("judge") else ""),
        f"- Case runs fully passing: **{runs_ok}/{len(cases)}**; deterministic checks passing: **{_pct(total_p, total_n)}**",
    ]
    for w in data.get("warnings") or []:
        lines.append(f"- Warning: {w}")
    lines += ["", "### Pass rate per check type", "", "| check | passed |", "| --- | --- |"]
    lines += [f"| `{k}` | {_pct(p, n)} |" for k, (p, n) in rates.items()] or ["| (no checks ran) | - |"]

    stab = stability(data)
    lines += ["", "### Cases", "", "| case | runs | checks passed | status | stability |", "| --- | --- | --- | --- | --- |"]
    per_case: dict[str, list] = defaultdict(list)
    for c in cases:
        per_case[c["name"]].append(c)
    for name, runs in per_case.items():
        evaluated = [chk for r in runs for t in r.get("turns", []) for chk in t["checks"] if chk.get("passed") is not None]
        ok = sum(1 for chk in evaluated if chk["passed"])
        errors = [r for r in runs if r.get("status") != "ok"]
        status = "PASS" if all(r.get("passed") for r in runs) else ("ERROR" if errors and len(errors) == len(runs) else "FAIL")
        s = stab.get(name, {})
        agreement = s.get("agreement")
        stab_cell = "-" if agreement is None else f"{100 * agreement:.0f}%" + (
            f" (turns {', '.join(map(str, s['unstable_turns']))} differ)" if s["unstable_turns"] else "")
        lines.append(f"| `{name}` | {len(runs)} | {_pct(ok, len(evaluated))} | {status} | {stab_cell} |")
    measured = [s["agreement"] for s in stab.values() if s["agreement"] is not None]
    if measured:
        lines += ["", f"Mean same-question agreement across repeats: **{100 * sum(measured) / len(measured):.0f}%** "
                      f"over {len(measured)} case(s)."]
    else:
        lines += ["", "Stability across repeats not measured (run with `--repeat 3`)."]

    errors = [c for c in cases if c.get("status") != "ok"]
    if errors:
        lines += ["", "### Cases that could not run", ""]
        lines += [f"- `{c['name']}` (run {c['repeat']}): {c.get('error')}" for c in errors]

    failures = [(c, t, chk) for c, t, chk in _checks(data) if chk.get("passed") is False]
    lines += ["", f"### Failures ({len(failures)})", ""]
    if not failures:
        lines.append("None.")
    shown: set[tuple] = set()
    count = 0
    for c, t, _ in failures:
        key = (c["name"], c["repeat"], t["index"])
        if key in shown:
            continue
        if count >= max_failures:
            lines.append(f"... {len({(c['name'], c['repeat'], t['index']) for c, t, _ in failures}) - count} more turn(s) with failures not shown")
            break
        shown.add(key)
        count += 1
        bad = [chk for chk in t["checks"] if chk.get("passed") is False]
        lines += [f"**{c['name']}** run {c['repeat']}, turn {t['index']}", "",
                  f"Farmer: {t['say']}", "", _quote(t.get("answer", "")), "",
                  *[f"- FAIL `{chk['type']}`: {chk['detail']}" for chk in bad], ""]

    judged = [(c, t) for c in cases for t in c.get("turns", []) if t.get("judge")]
    if data.get("judge"):
        lines += ["### Judge (advisory, not part of pass/fail)", ""]
        scored = [(c, t) for c, t in judged if t["judge"].get("score") is not None]
        if scored:
            mean = sum(t["judge"]["score"] for _, t in scored) / len(scored)
            lines += [f"Mean score **{mean:.2f}/5** over {len(scored)} judged answer(s); {len(judged) - len(scored)} unscored.", "",
                      "| case | turn | score | reason |", "| --- | --- | --- | --- |"]
            for c, t in sorted(scored, key=lambda ct: ct[1]["judge"]["score"])[:15]:
                lines.append(f"| `{c['name']}` | {t['index']} | {t['judge']['score']} | {t['judge'].get('reason', '')} |")
        else:
            lines.append("No judge scores were produced.")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.conversations.report", description="Markdown summary of a results file.")
    p.add_argument("results", help="a results/<timestamp>.json file")
    p.add_argument("--max-failures", type=int, default=25, help="failing turns to print with transcripts")
    args = p.parse_args(argv)
    try:
        data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out(f"error: cannot read {args.results}: {e}")
        return 2
    out(render(data, args.max_failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
