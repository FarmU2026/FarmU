"""Drives conversations against the deployed API: send a farmer text, poll the farm thread for the answer, check it.

Assumed API shapes (see agent/cloud/api/farms.py, store.py, _diagnoses.py):
  GET  /api/farms/{farm_id}                  -> {fields: [{id, name, ...}], thread: [{at, who, kind, text,
                                                investigation_id?, field_id?}], diagnoses: [{field_id, investigation_id,
                                                condition, what, where, why, how_sure, ruled_out, hypotheses, ...}]}
  POST /api/farms/{farm_id}/reply {"text"}   -> farm payload (+ "reply")
  POST /api/farms/{farm_id}/fields/{id}/ask  -> farm payload + "investigation_id"
  POST /v1/auth/dashboard {"passcode"}       -> {"token"}               (only to enrich the record; optional)
  GET  /v1/investigations/{id}               -> the investigation item (diagnosis_json, explanation_json)

All HTTP goes through one injectable `http(method, url, json=, headers=) -> (status, dict)` (eval.runner.requests_http).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from eval.runner import requests_http

from . import judge as judge_mod
from .cases import Case
from .checks import CheckContext, CheckResult, Record, _json_text, run_checks, signature

QUESTION_PREFIX = "inv-q-"
HttpFn = Callable[..., "tuple[int, dict]"]


class EvalError(Exception):
    """A problem that ends one case (setup failed, field not found, API refused us) but not the whole run."""


@dataclass
class EvalConfig:
    api: str
    farm_id: str
    passcode: str | None = None
    turn_timeout_s: float = 120
    setup_timeout_s: float = 300
    poll_s: float = 3
    field_map: dict[str, str] = field(default_factory=dict)  # case field name -> deployed field name


def _err(data: dict) -> str:
    e = data.get("error")
    return str(e.get("message") if isinstance(e, dict) else e or data.get("raw") or data)[:200]


def _thread_key(item: dict) -> tuple:
    return (item.get("at"), item.get("who"), item.get("kind"), item.get("text"))


class ChatEval:
    def __init__(self, cfg: EvalConfig, *, http: HttpFn = requests_http, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, judge_client: Any = None, judge_model: str | None = None,
                 out: Callable[[str], None] = print):
        self.cfg, self.http, self.sleep, self.clock, self.out = cfg, http, sleep, clock, out
        self.judge_client, self.judge_model = judge_client, judge_model
        self.token: str | None = None
        self._login_tried = False
        self.warnings: list[str] = []

    # ---- http
    def _call(self, method: str, path: str, body: dict | None = None, *, auth: bool = False) -> tuple[int, dict]:
        url = self.cfg.api.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else None
        return self.http(method, url, json=body, headers=headers)

    def _must(self, what: str, method: str, path: str, body: dict | None = None) -> dict:
        status, data = self._call(method, path, body)
        if status not in (200, 202):
            raise EvalError(f"{what} failed (HTTP {status}): {_err(data)}")
        return data

    def _ensure_token(self) -> bool:
        """Dashboard login, once. Without a passcode the record comes from the farm payload alone."""
        if self.token:
            return True
        if self._login_tried or not self.cfg.passcode:
            return False
        self._login_tried = True
        status, data = self._call("POST", "/v1/auth/dashboard", {"passcode": self.cfg.passcode})
        if status == 200 and data.get("token"):
            self.token = str(data["token"])
            return True
        self.warnings.append(f"dashboard login failed (HTTP {status}); records come from the farm payload only")
        return False

    def farm_view(self) -> dict:
        return self._must("farm payload", "GET", f"/api/farms/{self.cfg.farm_id}")

    # ---- field and record
    def resolve_field(self, view: dict, name: str) -> dict:
        wanted = self.cfg.field_map.get(name, name).strip().lower()
        fields = view.get("fields") or []
        for key in ("name", "id"):
            for f in fields:
                if str(f.get(key, "")).strip().lower() == wanted:
                    return f
        partial = [f for f in fields if wanted in str(f.get("name", "")).lower()]
        if len(partial) == 1:
            return partial[0]
        known = ", ".join(repr(f.get("name")) for f in fields) or "none"
        raise EvalError(f"field {name!r} not found on farm {self.cfg.farm_id} (fields: {known}); "
                        f"use --field-map \"{name}=<field name>\"")

    @staticmethod
    def latest_entry(view: dict, field_id: str) -> dict | None:
        mine = [d for d in view.get("diagnoses") or [] if d.get("field_id") == field_id]
        return mine[-1] if mine else None  # `diagnoses` is oldest first

    def _record(self, entry: dict | None) -> Record | None:
        if not entry:
            return None
        item = None
        inv = entry.get("investigation_id")
        if inv and self._ensure_token():
            status, data = self._call("GET", f"/v1/investigations/{inv}", auth=True)
            if status == 200:
                item = data
        return Record.from_sources(entry, item)

    def _wait_alert(self, field_id: str, inv: str) -> None:
        deadline = self.clock() + self.cfg.setup_timeout_s
        while True:
            view = self.farm_view()
            sent = any(i.get("investigation_id") == inv and i.get("who") != "farmer" and i.get("kind") in (None, "message", "concluded")
                       for i in view.get("thread") or [])
            entry = next((d for d in view.get("diagnoses") or [] if d.get("investigation_id") == inv), None)
            if sent and entry:
                return
            if self.clock() >= deadline:
                if sent:
                    return  # the alert went out; the diagnosis row may lag, the record will fall back to what exists
                raise EvalError(f"setup: no alert for the investigation on {field_id} within {self.cfg.setup_timeout_s:g}s")
            self.sleep(self.cfg.poll_s)

    def prepare(self, case: Case, view: dict, fld: dict) -> Record | None:
        fid = fld["id"]
        entry = self.latest_entry(view, fid)
        if case.setup.get("ask_first") and (case.setup.get("fresh") or entry is None):
            data = self._must("ask", "POST", f"/api/farms/{self.cfg.farm_id}/fields/{fid}/ask")
            inv = data.get("investigation_id")
            if not inv:
                raise EvalError("setup: ask did not return an investigation_id")
            self._wait_alert(fid, str(inv))
            entry = self.latest_entry(self.farm_view(), fid)
        record = self._record(entry)
        if case.record == "required" and record is None:
            raise EvalError(f"precondition: {fld.get('name')!r} has no investigation yet (use setup.ask_first)")
        if case.record == "none" and record is not None:
            raise EvalError(f"precondition: {fld.get('name')!r} has an investigation ({record.investigation_id}); "
                            "this case expects a field with no diagnosis (use --field-map to point at one)")
        return record

    def context_text(self, view: dict, fld: dict, record: Record | None) -> str:
        parts = [record.text() if record else "", _json_text({k: v for k, v in fld.items() if k != "polygon"})]
        for item in view.get("thread") or []:
            if (item.get("who") != "farmer" and not str(item.get("investigation_id") or "").startswith(QUESTION_PREFIX)
                    and item.get("field_id") in (None, fld["id"])):
                parts.append(str(item.get("text") or ""))
        return "\n".join(p for p in parts if p)

    # ---- one turn
    @staticmethod
    def find_answer(new_items: list[dict]) -> dict | None:
        cands = [i for i in new_items if i.get("who") in ("fieldwatch", "agent") and (i.get("kind") or "message") == "message"
                 and str(i.get("text") or "").strip()
                 and (not i.get("investigation_id") or str(i["investigation_id"]).startswith(QUESTION_PREFIX))]
        cands.sort(key=lambda i: (not str(i.get("investigation_id") or "").startswith(QUESTION_PREFIX), str(i.get("at"))))
        return cands[0] if cands else None

    def send(self, text: str) -> dict:
        """Send one farmer text and wait for the answer. {answer, answered, elapsed_s, new_thread, reply}."""
        before = {_thread_key(i) for i in self.farm_view().get("thread") or []}
        t0 = self.clock()
        sent = self._must("reply", "POST", f"/api/farms/{self.cfg.farm_id}/reply", {"text": text})
        deadline = t0 + self.cfg.turn_timeout_s
        while True:
            thread = self.farm_view().get("thread") or []
            new = [i for i in thread if _thread_key(i) not in before and i.get("who") != "farmer"]
            hit = self.find_answer(new)
            if hit:
                return {"answer": str(hit["text"]), "answered": True, "elapsed_s": round(self.clock() - t0, 2),
                        "new_thread": new, "reply": sent.get("reply")}
            if self.clock() >= deadline:
                return {"answer": "", "answered": False, "elapsed_s": round(self.clock() - t0, 2), "new_thread": new,
                        "reply": sent.get("reply")}
            self.sleep(self.cfg.poll_s)

    # ---- one case
    def run_case(self, case: Case, repeat: int = 1) -> dict:
        res: dict[str, Any] = {"name": case.name, "repeat": repeat, "field": case.field, "field_id": None,
                               "status": "ok", "error": None, "record": None, "turns": [], "passed": False}
        try:
            view = self.farm_view()
            fld = self.resolve_field(view, case.field)
            res["field_id"], res["field"] = fld["id"], fld.get("name") or fld["id"]
            record = self.prepare(case, view, fld)
            res["record"] = record.to_dict() if record else None
            ctx_text = self.context_text(self.farm_view(), fld, record)
            ignore = [str(fld.get("name") or ""), str(fld["id"])]
            answers: list[str] = []
            for i, turn in enumerate(case.turns):
                say = turn.say.replace("{field}", str(res["field"]))
                sent = self.send(say)
                answers.append(sent["answer"])
                res["turns"].append(self._grade(i, say, turn, sent, record, ctx_text, ignore, answers))
        except EvalError as e:
            res["status"], res["error"] = "error", str(e)
        res["passed"] = res["status"] == "ok" and all(
            c["passed"] is not False for t in res["turns"] for c in t["checks"])
        return res

    def _grade(self, index: int, say: str, turn: Any, sent: dict, record: Record | None, ctx_text: str, ignore: list[str],
               answers: list[str]) -> dict:
        checks: list[CheckResult] = []
        expect = turn.checks.get("expect_answer", True)
        if expect and not sent["answered"]:
            checks.append(CheckResult("answered", False, f"no answer within {self.cfg.turn_timeout_s:g}s"))
        elif not expect:
            checks.append(CheckResult("expect_answer", not sent["answered"],
                                      "stayed quiet" if not sent["answered"] else f"answered: {sent['answer'][:80]!r}"))
        else:
            checks.append(CheckResult("answered", True, f"in {sent['elapsed_s']:.1f}s"))
            ctx = CheckContext(answer=sent["answer"], question=say, elapsed_s=sent["elapsed_s"], record=record,
                               context_text=ctx_text, ignore=ignore, answers=answers[:-1], new_thread=sent["new_thread"])
            checks += run_checks(turn.checks, ctx)
        out = {"index": index, "say": say, "answer": sent["answer"], "answered": sent["answered"],
               "elapsed_s": sent["elapsed_s"], "checks": [c.to_dict() for c in checks],
               "signature": signature(sent["answer"], ignore) if sent["answer"] else None,
               "new_thread": sent["new_thread"], "reply": sent.get("reply"), "judge": None}
        if self.judge_client is not None and turn.rubric and sent["answered"]:
            out["judge"] = judge_mod.judge_answer(
                self.judge_client, say, sent["answer"], turn.rubric, model=self.judge_model,
                record_summary=judge_mod.record_summary(record.to_dict() if record else None))
        return out

    # ---- many cases
    def run(self, cases: list[Case], repeat: int = 1) -> dict:
        started = datetime.now(timezone.utc)
        results: list[dict] = []
        for n in range(1, repeat + 1):
            for case in cases:
                self.out(f"[run {n}/{repeat}] {case.name} ...")
                try:
                    results.append(self.run_case(case, n))
                except Exception as e:  # noqa: BLE001 - one broken case must not stop the suite
                    results.append({"name": case.name, "repeat": n, "field": case.field, "field_id": None, "status": "error",
                                    "error": f"{type(e).__name__}: {e}"[:300], "record": None, "turns": [], "passed": False})
        return {"started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "api": self.cfg.api, "farm_id": self.cfg.farm_id, "repeat": repeat,
                "judge": self.judge_client is not None,
                "judge_model": (self.judge_model or judge_mod.model_id()) if self.judge_client is not None else None,
                "warnings": self.warnings, "cases": results}
