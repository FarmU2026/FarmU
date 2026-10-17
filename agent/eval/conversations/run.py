"""Run the conversation eval against a deployed FieldWatch API.

    cd agent
    export FIELDWATCH_API_URL=https://<id>.execute-api.us-west-2.amazonaws.com
    export FIELDWATCH_DASHBOARD_PASSCODE=<passcode>        # optional: lets the runner read full investigation records
    python -m eval.conversations.run --farm-id <farm id> --repeat 3 --judge

Exit code: 0 when every case passed, 1 when a check failed or a case could not run, 2 on usage or setup errors.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import judge as judge_mod
from . import report
from .cases import CASES_DIR, CaseError, load_cases
from .engine import ChatEval, EvalConfig

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m eval.conversations.run",
                                description="Send scripted farmer conversations to FieldWatch and check the answers.")
    p.add_argument("--api", default=os.getenv("FIELDWATCH_API_URL"), help="API base URL (env FIELDWATCH_API_URL)")
    p.add_argument("--farm-id", required=True, help="farm to chat with")
    p.add_argument("--case", action="append", metavar="NAME", help="case name (repeatable; default all)")
    p.add_argument("--repeat", type=int, default=1, metavar="N", help="run every case N times; N>=2 measures stability")
    p.add_argument("--judge", action="store_true", help="also score answers with the Bedrock judge (needs AWS credentials)")
    p.add_argument("--timeout", type=float, default=120, metavar="SECONDS", help="wait per farmer turn for the answer")
    p.add_argument("--setup-timeout", type=float, default=300, metavar="SECONDS", help="wait for a setup investigation alert")
    p.add_argument("--poll", type=float, default=3, metavar="SECONDS", help="seconds between thread polls")
    p.add_argument("--field-map", action="append", default=[], metavar="CASE_FIELD=FIELD",
                   help="use deployed field FIELD where a case says CASE_FIELD (repeatable)")
    p.add_argument("--cases-dir", default=str(CASES_DIR), help="directory of case files")
    p.add_argument("--out-dir", default=str(RESULTS_DIR), help="where results/<timestamp>.json is written")
    p.add_argument("--list", action="store_true", help="list the cases and exit")
    return p


def parse_field_map(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair or not pair.split("=", 1)[0].strip() or not pair.split("=", 1)[1].strip():
            raise ValueError(f"--field-map expects CASE_FIELD=FIELD, got {pair!r}")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def table(data: dict) -> str:
    stab = report.stability(data)
    head = ["CASE", "RUN", "TURNS", "CHECKS", "STABILITY", "RESULT"]
    rows = []
    for c in data["cases"]:
        evaluated = [k for t in c["turns"] for k in t["checks"] if k.get("passed") is not None]
        ok = sum(1 for k in evaluated if k["passed"])
        agree = stab.get(c["name"], {}).get("agreement")
        result = "PASS" if c["passed"] else ("ERROR" if c["status"] != "ok" else "FAIL")
        rows.append([c["name"], str(c["repeat"]), str(len(c["turns"])), f"{ok}/{len(evaluated)}",
                     "-" if agree is None else f"{100 * agree:.0f}%", result])
    widths = [max(len(x[i]) for x in [head, *rows]) for i in range(len(head))]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt = lambda cells: "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"  # noqa: E731
    lines = [line, fmt(head), line, *[fmt(r) for r in rows], line]
    for c in data["cases"]:
        if c["status"] != "ok":
            lines.append(f"ERROR {c['name']} (run {c['repeat']}): {c['error']}")
        for t in c["turns"]:
            for k in t["checks"]:
                if k.get("passed") is False:
                    lines.append(f"FAIL {c['name']} (run {c['repeat']}) turn {t['index']} {k['type']}: {k['detail']}")
    n_ok = sum(1 for c in data["cases"] if c["passed"])
    lines.append(f"{n_ok}/{len(data['cases'])} case runs passed")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, http: Any = None, sleep: Callable[[float], None] | None = None,
         judge_client: Any = None, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    if args.repeat < 1:
        out("--repeat must be at least 1")
        return 2
    try:
        cases = load_cases(Path(args.cases_dir), args.case)
        field_map = parse_field_map(args.field_map)
    except (CaseError, ValueError) as e:
        out(f"error: {e}")
        return 2
    if args.list:
        for c in cases:
            out(f"{c.name:28} {len(c.turns)} turn(s)  field={c.field!r}  {c.description}")
        return 0
    if not cases:
        out(f"error: no cases found in {args.cases_dir}")
        return 2
    if not args.api:
        out("error: need --api or env FIELDWATCH_API_URL")
        return 2
    cfg = EvalConfig(api=args.api, farm_id=args.farm_id, passcode=os.getenv("FIELDWATCH_DASHBOARD_PASSCODE"),
                     turn_timeout_s=args.timeout, setup_timeout_s=args.setup_timeout, poll_s=args.poll, field_map=field_map)
    kwargs: dict[str, Any] = {"out": out}
    if http is not None:
        kwargs["http"] = http
    if sleep is not None:
        kwargs["sleep"] = sleep
    if args.judge:
        try:
            kwargs["judge_client"] = judge_client or judge_mod.make_client()
        except Exception as e:  # noqa: BLE001
            out(f"error: cannot create the Bedrock client for --judge: {e}")
            return 2
    runner = ChatEval(cfg, **kwargs)
    data = runner.run(cases, args.repeat)
    out(table(data))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    out(f"wrote {path}")
    out(f"summary: python -m eval.conversations.report {path}")
    return 0 if all(c["passed"] for c in data["cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
