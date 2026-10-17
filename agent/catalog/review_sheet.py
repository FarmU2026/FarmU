"""Regenerate catalog/REVIEW.md, the agronomy review sheet, from the catalog JSON and the code that evaluates it.

    cd agent; python catalog/review_sheet.py          # writes catalog/REVIEW.md
    python catalog/review_sheet.py --check            # exit 1 if REVIEW.md is stale

Read-only over the catalog; it imports only the scorer and the evidence engine (no network, no AWS, no Strands).
Do not edit REVIEW.md by hand: change the JSON or this script and rerun. A test fails when REVIEW.md is stale.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))

from fieldwatch import contracts as C  # noqa: E402
from fieldwatch import evidence as E  # noqa: E402
from fieldwatch import scoring  # noqa: E402

CATALOG = AGENT / "catalog" / "conditions.json"
OUT = AGENT / "catalog" / "REVIEW.md"
BIG = 2.5


def key_audit(catalog_weights: dict[str, set[str]], resolvers: set[str], llm: dict[str, set[str]]) -> dict:
    """Cross-check the catalog against the evidence engine.

    dead = in the catalog, no resolver evaluates it. orphans = a resolver for a key no condition has.
    photo_farmer = keys an LLM may supply (fixed fields); every other key is computed by `evidence.evaluate`.
    """
    in_catalog = {k for ks in catalog_weights.values() for k in ks}
    dead = [(c, k) for c, ks in sorted(catalog_weights.items()) for k in sorted(ks) if k not in resolvers]
    orphans = sorted(resolvers - in_catalog)
    photo_farmer = [(c, k) for c, ks in sorted(llm.items()) for k in sorted(ks)]
    return {"dead": dead, "orphans": orphans, "photo_farmer": photo_farmer}


def _pairs(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(f"`{c}.{k}`" for c, k in pairs) or "none"


def _keys(keys: list[str]) -> str:
    return ", ".join(f"`{k}`" for k in keys) or "none"


# ---------- worked examples ----------

_REGION_FEATURES = dict(ring_score=0.0, wedge_score=0.0, edge_band_score=0.0, stripe_score=0.0, lowspot_overlap=0.2,
                        sandy_soil_overlap=0.1, hail_swath_overlap=None)


def _analysis(date: str = "2026-07-20", acres: float = 12.0, state: C.CropState | None = None, ndre_p90: float | None = None,
              previous: str | None = None, delta: float | None = None, **features) -> C.FieldAnalysisResult:
    region = C.AnomalyRegion(region_id="r1", geometry={"type": "Polygon", "coordinates": []}, area_acres=acres,
                             centroid=(-96.54, 42.15), score=3.0, where="south-west part of the field",
                             features=C.PatternFeatures(**{**_REGION_FEATURES, **features}))
    ind = None
    if ndre_p90 is not None:
        stat = C.IndexStats(mean=0.4, p10=0.3, p50=0.4, p90=ndre_p90)
        ind = C.Indices(ndvi=stat, ndre=stat, ndmi=stat)
    return C.FieldAnalysisResult(
        field=C.FieldRef(farm_id="f", field_id="x"), scene_id="s", date=date, status="ok", clear_fraction=0.95, indices=ind,
        anomaly=C.AnomalySummary(field_score=3.0, delta_since_last=delta, regions=[region]),
        comparison=C.OverlayComparison(scene_date=date, previous_date=previous) if previous else None, crop_state=state)


def _vegetative(label: str) -> C.CropState:
    return C.CropState(state="vegetative", stage_label=label, diagnosable=True)


def _inputs(crop="corn", stage="V8", irrigated=False, rain=(3.0, 3.0), prev=None, **kw) -> E.FieldInputs:
    ctx = C.FieldContext(field_id="x", built_at="2026-07-01T00:00:00Z", crop_history={"2025": prev} if prev else {})
    wx = {"rain_in": rain[0], "normal_rain_in": rain[1]}
    analysis = kw.pop("analysis", None) or _analysis(state=_vegetative(stage))
    return E.FieldInputs(field_id="x", field_name="the east 80", crop=crop, irrigated=irrigated, acres=80.0,
                         analysis=analysis, context=ctx, weather=wx, stage=stage, **kw)


def examples() -> list[tuple[str, str, E.FieldInputs]]:
    return [
        ("Observed failure: wet month, soybeans last year, nothing seen in the field",
         "Old catalog: nitrogen 69%, 'likely', from rain (+1.2) and soybean rotation (+0.8, counted the wrong way).",
         _inputs(rain=(6.0, 3.0), prev="soybeans")),
        ("Pivot ring on an irrigated field", "",
         _inputs(stage="R2 (blister)", irrigated=True, analysis=_analysis(state=_vegetative("R2"), ring_score=0.82),
                 hail_watches=[])),
        ("Hail swath with a sudden drop", "",
         _inputs(stage="V10", analysis=_analysis(state=_vegetative("V10"), hail_swath_overlap=0.8, previous="2026-07-10",
                                                 delta=-0.14),
                 hail=C.HailReport(field_id="x", start="2026-07-01", end="2026-07-20", max_hail_mm=40.0,
                                   pct_area_over_threshold=60, peak_date="2026-07-14"))),
        ("Red-edge low at V10, farmer silent", "Nitrogen may be named at most as 'possible', never 'likely'.",
         _inputs(stage="V10", region_ndre=0.34, analysis=_analysis(state=_vegetative("V10"), ndre_p90=0.42))),
        ("Red-edge low at V10 and the farmer says nitrogen went on short", "The farmer's report lifts the cap.",
         _inputs(stage="V10", region_ndre=0.34, farmer=E.FarmerInputs(n_applied="short"),
                 analysis=_analysis(state=_vegetative("V10"), ndre_p90=0.42))),
        ("Irregular patches, no photo", "Disease or insects is at most 'possible' (scout for it).",
         _inputs(stage="R2 (blister)", analysis=_analysis(state=_vegetative("R2")))),
        ("Whole field stressed in a dry month (dryland, R2)", "",
         _inputs(stage="R2 (blister)", rain=(1.0, 3.0), analysis=_analysis(state=_vegetative("R2"), acres=70.0))),
        ("Weak area in the low ground after heavy rain", "",
         _inputs(stage="V4", rain=(6.0, 3.0), analysis=_analysis(state=_vegetative("V4"), lowspot_overlap=0.8))),
    ]


def example_rows() -> list[dict]:
    rows = []
    for name, note, inputs in examples():
        vec, det = E.determine(inputs)
        rows.append({"name": name, "note": note, "vec": vec, "det": det, "inputs": inputs})
    return rows


# ---------- markdown ----------


def _dollars(x: float) -> str:
    return f"${x:,.0f}"


def loss_section(lt: scoring.LossTables) -> list[str]:
    lines = ["## 5. Loss table sanity ($/acre at default yield and price)", "",
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


def _who(w: dict) -> str:
    return {"photo": "photo (LLM, fixed field)", "farmer": "farmer (LLM, fixed field)"}.get(w["source"], "code")


def _constants() -> list[str]:
    rows = [
        ("REGIONAL_CAP", scoring.REGIONAL_CAP, "[design] most log-odds regional evidence (weather, county reports) may add or take away per condition, in total (about 2x odds)"),
        ("STATIC_CAP", scoring.STATIC_CAP, "[design] same for static context (soil, terrain, flood zone, crop history) (about 4.5x odds)"),
        ("FIELD_CAP", scoring.FIELD_CAP, "memo 5.2: clamp on this field's own evidence per condition"),
        ("NAME_THRESHOLD", scoring.NAME_THRESHOLD, "[design] probability a condition needs to be named"),
        ("NAME_MARGIN", scoring.NAME_MARGIN, "[design] lead over the runner-up it needs"),
        ("POSSIBLE_MIN", scoring.POSSIBLE_MIN, "[design] probability a candidate needs to be listed as 'possible'"),
        ("RULED_OUT_BELOW", scoring.RULED_OUT_BELOW, "probability under which a condition is shown as ruled out"),
        ("LOGIT_CLAMP", scoring.LOGIT_CLAMP, "final log-odds clamp"),
        ("SUDDEN_DROP", E.SUDDEN_DROP, "memo 3.5 [design]: median crop-health change between passes that counts as abrupt"),
        ("SI_STRONG / SI_BORDERLINE", f"{E.SI_STRONG} / {E.SI_BORDERLINE}", "memo 3.2: sufficiency index cuts (0.95 from Varvel 1997; unverified for satellite pixels)"),
        ("RING / WEDGE / EDGE / STRIPE strong", f"{E.RING_STRONG}", "[design] analyzer pattern score at or above this counts as the shape"),
        ("RAIN deficit / normal / excess", f"{E.RAIN_DEFICIT_BELOW} / {E.RAIN_NORMAL_RANGE[0]}-{E.RAIN_NORMAL_RANGE[1]} / {E.RAIN_EXCESS_AT}", "[design] 30-day rain over normal"),
        ("HEAVY_RAIN_7D_MM", E.HEAVY_RAIN_7D_MM, "catalog: about 50 mm in a week"),
        ("HAIL_LARGE_MM / HAIL_MODERATE_MM", f"{E.HAIL_LARGE_MM} / {E.HAIL_MODERATE_MM}", "catalog: 25 mm and 12 mm"),
        ("UNIFORM_SHARE", E.UNIFORM_SHARE, "[design] problem area covers this share of the field = field-wide stress"),
        ("LOW_AWC_CM", E.LOW_AWC_CM, "[design] area-weighted available water capacity under this = sandy / droughty"),
    ]
    return ["| constant | value | meaning |", "|---|---|---|", *[f"| `{n}` | {v} | {t} |" for n, v, t in rows]]


def build() -> str:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    weights = {cid: set(c["weights"]) for cid, c in raw["conditions"].items()}
    audit = key_audit(weights, set(E.RESOLVERS), scoring.llm_keys())
    total_keys = len({k for ks in weights.values() for k in ks})
    total_pairs = sum(len(v) for v in weights.values())

    L = [f"# Review sheet for the agronomist (catalog {raw['version']})", "",
         "Generated by `agent/catalog/review_sheet.py` from `conditions.json`, `loss_tables.json`, the scorer and the "
         "evidence engine. Do not edit by hand; rerun the script. Every number is a DRAFT. Source of the design: "
         "`docs/research/grounding.md`. Weight = log-likelihood ratio: +w on `supports`, -w on `refutes` "
         "(1.0 = odds x2.7, 2.0 = x7.4, 3.0 = x20).", "",
         "## 0. How FieldWatch decides, in five steps", "",
         "1. **Code evaluates every evidence key, every run** (`fieldwatch/evidence.py`): `present`, `absent` or "
         "`not_evaluable` (the data is missing). No language model chooses a key. A model may only read a photo or "
         "the farmer's words into the fixed photo and farmer keys below.",
         "2. **Each key has a class.** `field` = from this field's own pixels, geometry, event overlap, or an observation "
         "of this field (photo, farmer). `static` = local context that does not change with the season (soil, terrain, "
         "flood zone, crop history). `regional` = weather, drought category, county disease reports.",
         f"3. **Caps.** Regional evidence in total moves a condition by at most +-{scoring.REGIONAL_CAP:g} log-odds, static "
         f"+-{scoring.STATIC_CAP:g}, field +-{scoring.FIELD_CAP:g}. Items in a correlated **cluster** (rain, low ground, "
         "drainage, flood zone ...) count once: the strongest, per direction.",
         f"4. **Naming rule.** A condition is named only if it is inside its **stage window**, nothing **rules it out**, "
         f"at least one **required field-evidence** key is present, and its probability is at least "
         f"{scoring.NAME_THRESHOLD:.0%} and {scoring.NAME_MARGIN:.0%} ahead of the runner-up. Some conditions are capped at "
         "'possible' until the farmer or a photo confirms.",
         "5. **Otherwise: not determined.** The farmer is told what was ruled out, what is possible, and is offered a "
         "scouting pin. FieldWatch never guesses a cause. Farmer text uses the tiers likely / possible / not determined, "
         "never percentages (the probabilities are not calibrated yet).", "",
         f"The catalog has {total_pairs} (condition, key) pairs over {total_keys} distinct keys; "
         f"{len(audit['photo_farmer'])} are photo or farmer inputs (read by a model into fixed fields), the rest are "
         f"computed in code. Dead keys (in the catalog, no resolver): {_pairs(audit['dead'])}. "
         f"Orphan resolvers: {_keys(audit['orphans'])}.", "",
         "## 1. Design values to ratify (all `[design]` unless a source is named)", ""]
    L += _constants()
    L += ["", "### What changed from catalog 2026-10-10.2", "",
          "- Evidence keys now carry `direction`, `evidence_class`, optional `cluster`, and `how` (how code decides). The "
          "scorer ignores an evidence item whose direction contradicts the catalog.",
          "- **Removed bonuses (priors kept):** nitrogen corn +0.5 and its stage terms (+0.3, +0.3, -1.0): no source, and "
          "they alone made nitrogen 'likely' on rain (memo 5.2). All other `stage_adjust` terms are replaced by the hard stage "
          "windows (memo 5.3). Crop and irrigation terms stay.",
          "- **Removed keys:** `nitrogen.pattern_uniform_pale` (uniform paleness is not nitrogen-specific, memo 3.2), "
          "`nitrogen.ndre_low_ndvi_normal` (replaced by the sufficiency-index keys `n_red_edge_si_low` and "
          "`n_red_edge_si_borderline`), `unknown.no_pattern_found` and `unknown.clear_pattern_found` (not-determined is now "
          "an outcome, not a competing hypothesis).",
          "- **Direction corrections:** `pivot_failure.stress_uniform_field` now *refutes* (the old code counted it as support "
          "while the reason said the opposite); `nitrogen.prior_crop_soybeans` now *refutes* (rotation credit lowers the "
          "need; the old run counted it as support).",
          "- **New required evidence per condition**, stage windows, rule-outs, tier caps and scouting checklists (below).", ""]

    L += ["## 2. Conditions", ""]
    for cid, c in raw["conditions"].items():
        if cid == "unknown":
            L += [f"### {cid} ({c['label']})", "", c["prior_reason"], ""]
            continue
        adj = []
        if c["crop_adjust"] and any(c["crop_adjust"].values()):
            adj.append("crop " + ", ".join(f"{k} {v:+g}" for k, v in c["crop_adjust"].items() if v))
        if c["irrigated_adjust"]:
            adj.append("irrigated " + ", ".join(f"{k} {v:+g}" for k, v in c["irrigated_adjust"].items()))
        win = "; ".join(f"{crop} {lo} to {hi}" for crop, (lo, hi) in c["stage_windows"].items())
        cap = c.get("tier_cap")
        L += [f"### {cid} ({c['label']}): prior {c['prior']:.0%}", "",
              f"- Prior reason: {c['prior_reason']}",
              ("- Adjust (log-odds): " + "; ".join(adj)) if adj else "- No adjustments.",
              *(["- Requires an irrigated field."] if c["requires_irrigated"] else []),
              f"- **Stage window (hard gate):** {win or 'none'}" + (" (stage must be known)" if c.get("needs_known_stage") else ""),
              f"- **Required field evidence (one of):** {_keys(c['required_field_evidence'])}",
              f"- **Rules it out:** {_keys(c['rules_out'])}",
              *([f"- **Capped at '{cap['max_tier']}'** unless one of {_keys(cap['lifted_by_any'])}. {cap['why']}"] if cap else []),
              f"- **Scouting checklist:** {c['scout_for']}", "",
              "| weight_key | w | dir | class | cluster | computed by | how | reason | source | ? |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for k, w in sorted(c["weights"].items(), key=lambda t: (t[1]["evidence_class"], -t[1]["weight"], t[0])):
            flag = " **BIG**" if w["weight"] > BIG else ""
            L.append(f"| `{k}` | {w['weight']:g}{flag} | {'+' if w['direction'] == 'supports' else '-'} | {w['evidence_class']} | "
                     f"{w.get('cluster') or ''} | {_who(w)} | {w['how']} | {w['reason']} | {w['source']} | |")
        L.append("")

    L += ["## 3. Worked examples", "",
          "Each runs the evidence engine on hand-built inputs, then the scorer. Counts are over the "
          f"{total_pairs} (condition, key) pairs. Probabilities are internal; farmers see only the tier.", ""]
    for r in example_rows():
        det, vec = r["det"], r["vec"]
        L += [f"### {r['name']}", ""]
        if r["note"]:
            L += [r["note"], ""]
        counts = vec.by_state()
        top = ", ".join(f"{c.condition} ({c.tier}, {c.probability:.0%})" for c in det.candidates) or "none"
        L += [f"- Outcome: **{det.outcome}**" + (f", named: `{det.condition}`" if det.condition else ""),
              f"- Candidates: {top}",
              f"- Evidence: {counts['present']} present, {counts['absent']} absent, {counts['not_evaluable']} not evaluable.",
              "- Present: " + (", ".join(f"{'+' if x.direction == 'supports' else '-'}{x.condition}.{x.key}" for x in vec.present()) or "none"),
              ""]
    L += ["## 4. Questions for the agronomist", "",
          "1. Ratify or replace the stage windows above (memo 3.2 to 3.9 give the sources; soybean windows are the least sourced).",
          "2. Nitrogen: V6-V12 only, 'possible' until the farmer says N was short or gives application records. Is SI < 0.90 "
          "(strong) and < 0.95 (borderline) the right cut for a red-edge index of the stressed area against the field's own top "
          "10%? What N history or soil-test evidence should lift the cap besides a farmer report?",
          "3. Ponding is named from low-ground overlap alone; the memo wants a heavy-rain event as a necessary gate. Add one?",
          "4. Planter skips need a photo, a farmer report or a stripe-width match to reach the naming bar (stripe alone is 64%). Confirm.",
          "5. Drought is named from field-wide stress; the memo asks for an NDMI trend structured by soil water capacity "
          "(needs a second pass; not computed yet).",
          "6. Priors are the old hand-set values; replace with RMA cause-of-loss base rates when pulled (memo 5.3).",
          "7. Hail loss rows (below) are still DRAFT until the FCIC handbook exhibits are loaded (memo section 7, item 5).", ""]
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
