"""Case files: one JSON file per conversation under `cases/`.

    {
      "name": "problem-why",                      # unique; defaults to the file name
      "description": "...",
      "field": "north pivot",                     # the field NAME in the farm payload (see run.py --field-map)
      "record": "required",                       # required | none | any (default any): precondition on the field's record
      "setup": {"ask_first": true},               # optional: ask about the field and wait for the alert first
      "turns": [
        {"say": "Why do you think that about {field}?",     # {field} is replaced with the field's name
         "checks": {"must_mention_any": ["pivot"], "consistent_with_record": true},
         "rubric": "Explains the cause using the evidence in the record, in plain words."}
      ]
    }

`cases/_defaults.json` holds checks applied to every turn (a case's own `defaults` and a turn's `checks` override it;
`must_not_mention` lists are concatenated). Files starting with `_` are not cases.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CASES_DIR = Path(__file__).resolve().parent / "cases"

# check name -> expected JSON type(s). `consistent_with_record` / `no_new_numbers` accept true or an options object.
CHECK_TYPES: dict[str, tuple[type, ...]] = {
    "must_mention_any": (list,),
    "must_not_mention": (list,),
    "max_chars": (int,),
    "ascii_only": (bool,),
    "answers_within_s": (int, float),
    "consistent_with_record": (bool, dict),
    "no_new_numbers": (bool, dict),
    "no_cause_claimed": (bool,),
    "no_action_items": (bool,),
    "consistent_with_turn": (int, dict),
    "expect_answer": (bool,),
}
RECORD_MODES = ("required", "none", "any")
SPECIAL_PATTERNS = ("@internal_ids", "@index_numbers", "@jargon", "@percentages", "@product_advice")
CASE_KEYS = {"name", "description", "field", "record", "setup", "turns", "defaults"}
SETUP_KEYS = {"ask_first", "fresh"}
TURN_KEYS = {"say", "checks", "rubric"}


class CaseError(Exception):
    """A case file that cannot be used; the message names the file."""


@dataclass
class Turn:
    say: str
    checks: dict[str, Any] = field(default_factory=dict)
    rubric: str | None = None


@dataclass
class Case:
    name: str
    field: str
    turns: list[Turn]
    description: str = ""
    record: str = "any"
    setup: dict[str, Any] = field(default_factory=dict)
    source: str = ""


def _merge_checks(base: dict, over: dict) -> dict:
    out = {**base, **over}
    if "must_not_mention" in base or "must_not_mention" in over:
        merged: list = []
        for pat in [*base.get("must_not_mention", []), *over.get("must_not_mention", [])]:
            if pat not in merged:
                merged.append(pat)
        out["must_not_mention"] = merged
    return out


def validate_checks(checks: Any, where: str, turn_index: int | None = None) -> dict:
    if not isinstance(checks, dict):
        raise CaseError(f"{where}: checks must be an object")
    for key, value in checks.items():
        if key not in CHECK_TYPES:
            raise CaseError(f"{where}: unknown check {key!r}; known: {', '.join(sorted(CHECK_TYPES))}")
        if not isinstance(value, CHECK_TYPES[key]) or (isinstance(value, bool) and bool not in CHECK_TYPES[key]):
            raise CaseError(f"{where}: check {key!r} has the wrong type ({type(value).__name__})")
    for key in ("must_mention_any", "must_not_mention"):
        patterns = checks.get(key)
        if patterns is None:
            continue
        if key == "must_mention_any" and not patterns:
            raise CaseError(f"{where}: must_mention_any needs at least one pattern")
        for pat in patterns:
            if not isinstance(pat, str) or not pat:
                raise CaseError(f"{where}: {key} entries must be non-empty strings")
            if pat.startswith("@"):
                if key == "must_mention_any" or pat not in SPECIAL_PATTERNS:
                    raise CaseError(f"{where}: unknown pattern {pat!r}; known: {', '.join(SPECIAL_PATTERNS)}")
                continue
            try:
                re.compile(pat)
            except re.error as e:
                raise CaseError(f"{where}: {key} pattern {pat!r} is not a valid regex ({e})") from None
    if "max_chars" in checks and checks["max_chars"] < 1:
        raise CaseError(f"{where}: max_chars must be positive")
    if "answers_within_s" in checks and checks["answers_within_s"] <= 0:
        raise CaseError(f"{where}: answers_within_s must be positive")
    if "consistent_with_turn" in checks:
        spec = checks["consistent_with_turn"]
        idx = spec if isinstance(spec, int) else spec.get("turn")
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
            raise CaseError(f"{where}: consistent_with_turn needs a turn index (0-based)")
        if turn_index is not None and idx >= turn_index:
            raise CaseError(f"{where}: consistent_with_turn must point at an earlier turn (got {idx})")
        if isinstance(spec, dict):
            dims = spec.get("dims", [])
            if not isinstance(dims, list) or any(d not in ("causes", "tier", "where") for d in dims):
                raise CaseError(f"{where}: consistent_with_turn.dims must be a list of causes/tier/where")
    return checks


def parse_case(data: Any, source: str, defaults: dict | None = None) -> Case:
    if not isinstance(data, dict):
        raise CaseError(f"{source}: a case must be a JSON object")
    extra = set(data) - CASE_KEYS
    if extra:
        raise CaseError(f"{source}: unknown keys {sorted(extra)}; allowed: {sorted(CASE_KEYS)}")
    name = data.get("name") or Path(source).stem
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise CaseError(f"{source}: name must be letters, digits, '-', '_' or '.'")
    field_name = data.get("field")
    if not isinstance(field_name, str) or not field_name.strip():
        raise CaseError(f"{source}: field (a field name) is required")
    record = data.get("record", "any")
    if record not in RECORD_MODES:
        raise CaseError(f"{source}: record must be one of {', '.join(RECORD_MODES)}")
    setup = data.get("setup") or {}
    if not isinstance(setup, dict) or set(setup) - SETUP_KEYS:
        raise CaseError(f"{source}: setup must be an object with keys from {sorted(SETUP_KEYS)}")
    if any(not isinstance(v, bool) for v in setup.values()):
        raise CaseError(f"{source}: setup values must be true/false")
    if record == "none" and setup.get("ask_first"):
        raise CaseError(f"{source}: record 'none' cannot be combined with setup.ask_first")
    raw_turns = data.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise CaseError(f"{source}: turns must be a non-empty list")
    base = validate_checks(_merge_checks(defaults or {}, data.get("defaults") or {}), f"{source} defaults")
    turns: list[Turn] = []
    for i, t in enumerate(raw_turns):
        where = f"{source} turn {i}"
        if not isinstance(t, dict) or set(t) - TURN_KEYS:
            raise CaseError(f"{where}: a turn is an object with keys from {sorted(TURN_KEYS)}")
        say = t.get("say")
        if not isinstance(say, str) or not say.strip():
            raise CaseError(f"{where}: say must be a non-empty string")
        rubric = t.get("rubric")
        if rubric is not None and not isinstance(rubric, str):
            raise CaseError(f"{where}: rubric must be a string")
        own = validate_checks(t.get("checks") or {}, where, i)
        merged = _merge_checks(base, own)
        # a turn that expects silence has no answer to run the content checks on
        turns.append(Turn(say=say, checks=validate_checks(merged, where, i), rubric=rubric))
    return Case(name=name, field=field_name.strip(), turns=turns, description=str(data.get("description") or ""),
                record=record, setup=dict(setup), source=source)


def load_defaults(directory: Path = CASES_DIR) -> dict:
    path = directory / "_defaults.json"
    if not path.exists():
        return {}
    try:
        return validate_checks(json.loads(path.read_text(encoding="utf-8")), path.name)
    except json.JSONDecodeError as e:
        raise CaseError(f"{path.name}: not valid JSON ({e})") from None


def load_cases(directory: Path = CASES_DIR, only: list[str] | None = None) -> list[Case]:
    """Every `*.json` in the directory except names starting with `_`, validated. `only` selects by case name."""
    defaults = load_defaults(directory)
    cases: list[Case] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise CaseError(f"{path.name}: not valid JSON ({e})") from None
        cases.append(parse_case(data, path.name, defaults))
    names = [c.name for c in cases]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise CaseError(f"duplicate case name in {directory}: {', '.join(dupes)}")
    if only:
        missing = [n for n in only if n not in names]
        if missing:
            raise CaseError(f"unknown case(s) {', '.join(missing)}; known: {', '.join(names) or 'none'}")
        cases = [c for c in cases if c.name in only]
    return cases
