"""Regenerate catalog/REVIEW.md, the agronomy weights review sheet, from the catalog JSON.

    cd agent; python catalog/review_sheet.py          # writes catalog/REVIEW.md
    python catalog/review_sheet.py --check            # exit 1 if REVIEW.md is stale

Read-only over the catalog and agents.py (parsed with `ast`, nothing imported but the scorer). No network.
Do not edit REVIEW.md by hand: change the JSON or this script and rerun.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))

from fieldwatch import contracts as C  # noqa: E402
from fieldwatch import scoring  # noqa: E402

CATALOG = AGENT / "catalog" / "conditions.json"
AGENTS_PY = AGENT / "fieldwatch" / "agents.py"
OUT = AGENT / "catalog" / "REVIEW.md"
BIG = 2.5
INCONSISTENT_RATIO = 2.0
# Keys whose name says "absent / fine", so normally sent as `refutes` (a heuristic, label only).
_NEG = re.compile(r"(^no_|_none$|_ok$|_normal_|not_|well_drained|_full$|no_lesions)")


def _agents_literals(path: Path = AGENTS_PY) -> tuple[dict[str, list[str]], list[tuple]]:
    """WEIGHT_KEYS and PATTERN_RULES from agents.py source, without importing strands."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(tgt, ast.Name) and tgt.id in ("WEIGHT_KEYS", "PATTERN_RULES"):
                found[tgt.id] = ast.literal_eval(node.value)
    return found["WEIGHT_KEYS"], found["PATTERN_RULES"]  # type: ignore[return-value]


def key_audit(catalog_weights: dict[str, set[str]], weight_keys: dict[str, list[str]], rules: list[tuple],
              extra_code_keys: dict[str, set[str]] | None = None) -> dict:
    """Cross-check the catalog against what agents can emit.

    code: emitted deterministically (PATTERN_RULES, hail watch). llm: only offered to the specialists' prompt via
    WEIGHT_KEYS. dead = in catalog but in neither. orphans = emittable but absent from the catalog (ignored by scorer).
    """
    code: dict[str, set[str]] = {}
    for _feat, _min, hyp, key, _why in rules:
        code.setdefault(hyp, set()).add(key)
    for h, ks in ({"hail": {"hail_swath_overlap_high"}} if extra_code_keys is None else extra_code_keys).items():
        code.setdefault(h, set()).update(ks)
    llm = {h: set(ks) for h, ks in weight_keys.items()}
    dead, orphans, llm_only = [], [], []
    for cid, ks in catalog_weights.items():
        for k in sorted(ks):
            if k not in code.get(cid, set()) and k not in llm.get(cid, set()):
                dead.append((cid, k))
            elif k not in code.get(cid, set()):
                llm_only.append((cid, k))
    for cid in sorted(set(llm) | set(code)):
        for k in sorted(llm.get(cid, set()) | code.get(cid, set())):
            if k not in catalog_weights.get(cid, set()):
                orphans.append((cid, k))
    return {"dead": dead, "orphans": orphans, "llm_only": llm_only, "code": code}


# ---------- worked examples ----------

_T = "2026-07-20T12:00:00Z"


def _bundle(items: list[tuple[str, str, str]]) -> list[C.EvidenceItem]:
    return [C.EvidenceItem(evidence_id=f"e{i:02d}", hypothesis=h, direction=d, weight_key=k,  # type: ignore[arg-type]
                           source="example", value={}, reason=k.replace("_", " "), created_at=_T)
            for i, (h, d, k) in enumerate(items, 1)]


EXAMPLES: list[dict] = [
    {"name": "Pivot ring", "crop": "corn", "stage": "R2", "irrigated": True,
     "items": [("pivot_failure", "supports", "ring_pattern_strong"), ("drought", "refutes", "rain_normal_30d"),
               ("pivot_failure", "supports", "rain_normal_30d"), ("hail", "refutes", "no_hail_mesh"),
               ("ponding", "refutes", "lowspot_overlap_none")]},
    {"name": "Hail swath", "crop": "corn", "stage": "R2", "irrigated": True,
     "items": [("hail", "supports", "hail_mesh_large"), ("hail", "supports", "hail_swath_overlap_high"),
               ("hail", "supports", "sudden_drop_after_storm"), ("drought", "refutes", "rain_normal_30d"),
               ("pivot_failure", "refutes", "ring_pattern_weak")]},
    {"name": "Ponding in low spot", "crop": "soybeans", "stage": "V4", "irrigated": False,
     "items": [("ponding", "supports", "lowspot_overlap_high"), ("ponding", "supports", "heavy_rain_7d"),
               ("ponding", "supports", "soil_poorly_drained"), ("hail", "refutes", "no_hail_mesh"),
               ("drought", "refutes", "rain_normal_30d")]},
    {"name": "Uniform drought", "crop": "corn", "stage": "R2", "irrigated": False,
     "items": [("drought", "supports", "stress_uniform_field"), ("drought", "supports", "rain_deficit_30d"),
               ("drought", "supports", "drought_monitor_d1_plus"), ("drought", "supports", "heat_days_over_35c"),
               ("hail", "refutes", "no_hail_mesh"), ("ponding", "refutes", "lowspot_overlap_none")]},
    {"name": "Healthy field", "crop": "corn", "stage": "V8", "irrigated": True,
     "items": [("unknown", "supports", "no_pattern_found"), ("drought", "refutes", "drought_monitor_none"),
               ("drought", "refutes", "rain_normal_30d"), ("hail", "refutes", "no_hail_mesh"),
               ("ponding", "refutes", "lowspot_overlap_none"),
               ("disease_pest", "refutes", "no_disease_reports_county")]},
]


def _request(ex: dict) -> C.ScoreRequest:
    return C.ScoreRequest(investigation_id="review", crop=ex["crop"], stage=ex["stage"], irrigated=ex["irrigated"],
                          evidence=_bundle(ex["items"]))


def _run(ex: dict) -> list[tuple[str, float, str]]:
    return [(h.condition, h.probability, h.status) for h in scoring.score_hypotheses(_request(ex)).hypotheses]


def _how_sure(p: float) -> str:
    return "pretty sure" if p >= 0.8 else "fairly sure" if p >= 0.55 else "not sure, need a photo"


def run_examples() -> list[dict]:
    """Score each bundle, then again with the top condition's largest applied weight halved (catalog restored)."""
    out = []
    for ex in EXAMPLES:
        base = _run(ex)
        detail = scoring.explain(_request(ex))
        top = next((c for c, _p, _s in base if detail[c]["applied"]), base[0][0])  # highest-ranked with evidence
        applied = detail[top]["applied"]
        row = {"ex": ex, "base": base, "halved_key": None, "halved": None}
        if applied:
            key = applied[0]["weight_key"]
            w = scoring.load_catalog().conditions[top].weights[key]
            old = w.weight
            try:
                w.weight = old / 2
                row["halved"] = _run(ex)
            finally:
                w.weight = old
            row["halved_key"] = (top, key, old)
        out.append(row)
    return out


# ---------- markdown ----------


def _fmt_list(pairs) -> str:
    return ", ".join(f"`{c}.{k}`" for c, k in pairs) or "none"


def _dollars(x: float) -> str:
    return f"${x:,.0f}"


def loss_section(lt: scoring.LossTables) -> list[str]:
    lines = ["## 3. Loss table sanity ($/acre at default yield and price)", "",
             "Per condition x crop: lowest low-severity to highest high-severity dollar loss across stage groups, "
             f"for {lt.reference_days} days of problem (duration-sensitive conditions scale x{lt.days_scaling.min}"
             f" to x{lt.days_scaling.max}: {', '.join(lt.days_scaling.conditions)}). Medium = range of the medium "
             "band. Worst stage is where the high-severity ceiling peaks. `any` rows are valued at corn.", ""]
    val = {c: d.yield_per_acre * d.price_usd for c, d in lt.crop_defaults.items()}
    crops = {r.crop for r in lt.rows}
    lines += ["Value at stake per acre: " + ", ".join(f"{c} {_dollars(v)}" for c, v in val.items() if c in crops), ""]
    lines += ["| condition | crop | low..high $/ac | medium $/ac | worst stage | max loss | note |",
              "|---|---|---|---|---|---|---|"]
    flags = []
    for cond in sorted({r.condition for r in lt.rows}):
        for crop in sorted({r.crop for r in lt.rows if r.condition == cond}):
            rows = [r for r in lt.rows if r.condition == cond and r.crop == crop]
            v = val["corn"] if crop == "any" else val[crop]
            lo = min(r.loss.low[0] for r in rows) * v
            hi_row = max(rows, key=lambda r: r.loss.high[1])
            hi = hi_row.loss.high[1] * v
            m_lo = min(r.loss.medium[0] for r in rows) * v
            m_hi = max(r.loss.medium[1] for r in rows) * v
            note = "valued at corn" if crop == "any" else ""
            if hi_row.loss.high[1] >= 0.6:
                note = (note + "; " if note else "") + "ceiling >= 60%"
                flags.append(f"`{cond}/{crop}` {hi_row.loss.high[1]:.0%} at {hi_row.stage_group}.")
            lines.append(f"| {cond} | {crop} | {_dollars(lo)}..{_dollars(hi)} | {_dollars(m_lo)}..{_dollars(m_hi)} | "
                         f"{hi_row.stage_group} | {hi_row.loss.high[1]:.0%} | {note} |")
    lines.append("")
    if flags:
        lines += ["Ceilings to eyeball: " + " ".join(flags), ""]
    return lines


def build() -> str:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    cat = scoring.load_catalog()
    wk, rules = _agents_literals()
    weights = {cid: set(c["weights"]) for cid, c in raw["conditions"].items()}
    audit = key_audit(weights, wk, rules)
    code_keys = {(c, k) for c, ks in audit["code"].items() for k in ks}

    by_key: dict[str, dict[str, float]] = {}
    for cid, c in raw["conditions"].items():
        for k, w in c["weights"].items():
            by_key.setdefault(k, {})[cid] = w["weight"]
    reused = {k: v for k, v in by_key.items() if len(v) > 1}
    inconsistent = {k: v for k, v in reused.items() if max(v.values()) / min(v.values()) >= INCONSISTENT_RATIO}
    big = [(cid, k, w["weight"]) for cid, c in raw["conditions"].items() for k, w in c["weights"].items()
           if w["weight"] > BIG]
    total_keys = sum(len(v) for v in weights.values())

    L = [f"# Weights review sheet (catalog {raw['version']})", "",
         "Generated by `agent/catalog/review_sheet.py` from `conditions.json`, `loss_tables.json` and `agents.py`. "
         "Do not edit by hand; rerun the script. Every number is a DRAFT. Weight = log-likelihood ratio: "
         "+w on `supports`, -w on `refutes`; 1.0 = odds x2.7, 2.0 = x7.4, 3.0 = x20. "
         f"Status cutoffs: ruled_out < {scoring.RULED_OUT_BELOW:.0%}, likely > {scoring.LIKELY_ABOVE:.0%}; "
         "how_sure wording: >= 80% pretty sure, >= 55% fairly sure, else not sure. A repeat of the same "
         "(condition, key, direction) counts once. Final log-odds clamp at +-8.", "",
         "Column key: **dir** = usual direction by key name (heuristic): `+` supports, `-` refutes. "
         "**emit** = `code` if a PATTERN_RULE or the hail watch sends it deterministically, `llm` if it is only "
         "offered to the specialists' prompt. **flags**: `BIG` weight > 2.5, `DUP` key reused in another condition, "
         "`INC` reused with weights 2x or more apart, `DEAD` no agent can emit it. **?** = reviewer verdict.", "",
         "## 0. Discuss first", ""]

    sums = {}
    for crop, irr in (("corn", True), ("corn", False), ("soybeans", True), ("soybeans", False)):
        sums[(crop, irr)] = sum(scoring.prior_probability(cid, crop, "R2", irr) for cid, c in cat.conditions.items()
                                if not (c.requires_irrigated and irr is False))
    prior_sum = sum(c["prior"] for c in raw["conditions"].values())
    L += [f"- **Priors.** Raw priors sum to {prior_sum:.2f} (conditions are not exclusive, so about 1 is fine). "
          "After crop/stage/irrigation adjust at R2 they sum to (above 1 means a field starts with more than 100% of blame "
          "spread across causes; a healthy field then shows several 'open' conditions at 20%+): "
          + ", ".join(f"{c} {'irrigated' if i else 'dryland'} {s:.2f}" for (c, i), s in sums.items())
          + ". Pivot gets -6.0 log-odds when dryland (effectively off).",
          f"- **Dead keys** (in catalog, no agent can emit): {_fmt_list(audit['dead'])}.",
          f"- **Orphan keys** (agents can emit, not in catalog, so silently ignored): {_fmt_list(audit['orphans'])}.",
          f"- **LLM-only keys**: {len(audit['llm_only'])} of {total_keys} keys rely on a specialist choosing to "
          "emit them (no deterministic code path); only "
          + ", ".join(f"`{c}.{k}`" for c, k in sorted(code_keys)) + " are emitted by code.",
          f"- **Weights above {BIG}** ({len(big)}): "
          + ", ".join(f"`{c}.{k}` {w:g}" for c, k, w in sorted(big, key=lambda t: -t[2])) + ".",
          f"- **Reused keys with inconsistent strength** ({len(inconsistent)}): "
          + "; ".join(f"`{k}` " + ", ".join(f"{c} {w:g}" for c, w in sorted(v.items(), key=lambda t: -t[1]))
                      for k, v in inconsistent.items()) + ".",
          "- **Semantic oddities.** `drought.pattern_localized` is a *supports* weight for drought, but a localized "
          "pattern usually argues against uniform drought. `pivot_failure.rain_normal_30d` supports pivot failure "
          "(0.4) while the same key refutes drought (2.0): confirm intent. `nitrogen.rain_excess_30d` and "
          "`ponding.rain_excess_30d` fire on the same fact, so both rise. `sandy_soil_overlap_high` feeds both "
          "sandy_soil_water_stress (3.0) and nitrogen (0.8).",
          "- **Near-conclusive rule-outs.** `no_hail_mesh` refutes hail at 3.0 and `soil_not_sandy` 2.5: one refute at "
          "3.0 takes a 10% prior to about 0.5%, i.e. ruled_out. Confirm each is meant to be conclusive.",
          "", "### Pending: flood zone / stream / wetland", "",
          "Three draft weights are expected in another person's PR. After `git fetch`, they are not on any remote "
          "branch or in main as of this generation, so these rows are placeholders.", "",
          "| proposed key | condition | weight | dir | reason / source | ? |", "|---|---|---|---|---|---|",
          "| `flood_zone_...` (name TBD) | ponding? | TBD | + | TBD | |",
          "| `stream_...` (name TBD) | ponding? | TBD | + | TBD | |",
          "| `wetland_...` (name TBD) | ponding? | TBD | + | TBD | |",
          "", "Questions: which condition do they feed (ponding only, or also nitrogen loss or disease)? Do they "
          "double count with `lowspot_overlap_high` (3.0) and `soil_poorly_drained` (1.5)? Emitted by code (add a "
          "PATTERN_RULE and a WEIGHT_KEYS entry) or by a specialist?", ""]

    L += ["## 1. Conditions", ""]
    for cid, c in raw["conditions"].items():
        adj = []
        if c["crop_adjust"]:
            adj.append("crop " + ", ".join(f"{k} {v:+g}" for k, v in c["crop_adjust"].items() if v))
        if c["irrigated_adjust"]:
            adj.append("irrigated " + ", ".join(f"{k} {v:+g}" for k, v in c["irrigated_adjust"].items()))
        if c["stage_adjust"]:
            adj.append("stage " + ", ".join(f"{k} {v:+g}" for k, v in c["stage_adjust"].items()))
        L += [f"### {cid} ({c['label']}): prior {c['prior']:.0%}", "",
              f"Prior reason: {c['prior_reason']}  ",
              ("Adjust (log-odds): " + "; ".join(adj)) if adj else "No adjustments.",
              "Requires an irrigated field." if c["requires_irrigated"] else "", "",
              "| weight_key | w | dir | emit | flags | reason | source | ? |", "|---|---|---|---|---|---|---|---|"]
        for k, w in sorted(c["weights"].items(), key=lambda t: -t[1]["weight"]):
            flags = []
            if w["weight"] > BIG:
                flags.append("BIG")
            if k in reused:
                flags.append("DUP")
            if k in inconsistent:
                flags.append("INC")
            if (cid, k) in audit["dead"]:
                flags.append("DEAD")
            emit = "code" if (cid, k) in code_keys else "llm"
            d = "-" if _NEG.search(k) else "+"
            L.append(f"| `{k}` | {w['weight']:g} | {d} | {emit} | {' '.join(flags)} | {w['reason']} | "
                     f"{w['source']} | |")
        L.append("")

    L += ["## 2. Worked examples", "",
          "Evidence bundles run through `score_hypotheses`: probability and status per condition, then the same "
          "bundle with the top condition's largest applied weight halved. `+` = supports, `-` = refutes.", ""]
    for r in run_examples():
        ex = r["ex"]
        L += [f"### {ex['name']} ({ex['crop']}, {ex['stage']}, {'irrigated' if ex['irrigated'] else 'dryland'})", "",
              "Evidence: " + "; ".join(f"{'+' if d == 'supports' else '-'}{h}.{k}" for h, d, k in ex["items"]), ""]
        halved = {c: (p, s) for c, p, s in r["halved"]} if r["halved"] else {}
        L += ["| condition | prob | status | prob if halved | status if halved |", "|---|---|---|---|---|"]
        for c, p, s in r["base"][:5]:
            hp, hs = halved.get(c, (None, None))
            L.append(f"| {c} | {p:.0%} | {s} | " + (f"{hp:.0%} | {hs} |" if hp is not None else "- | - |"))
        if r["halved_key"]:
            c, k, old = r["halved_key"]
            hb = r["halved"][0]
            before = {x[0]: x[1] for x in r["base"]}[c]
            after = halved[c][0]
            L += ["", f"Halved `{c}.{k}` {old:g} to {old / 2:g}: {c} {before:.0%} to {after:.0%}; how_sure of the "
                      f"overall top `{_how_sure(r['base'][0][1])}` to `{_how_sure(hb[1])}`; top becomes {hb[0]} at "
                      f"{hb[1]:.0%}.", ""]
        else:
            L.append("")
    L += loss_section(scoring.load_loss_tables())
    return "\n".join(L).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if REVIEW.md is out of date")
    a = ap.parse_args()
    text = build()
    if a.check:
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("REVIEW.md up to date" if ok else "REVIEW.md is stale: run python catalog/review_sheet.py")
        return 0 if ok else 1
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
