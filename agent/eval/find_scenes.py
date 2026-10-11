"""Find clear Sentinel-2 dates for a field and bracket an event (e.g. a flood). Run from agent/:

  python -m eval.find_scenes --point 42.16,-96.54 --acres 80 --year 2025
  python -m eval.find_scenes --geojson field.json --event-date 2025-06-20 --analyze --out-dir scenes_out
  python -m eval.find_scenes --bbox W,S,E,N --event-date 2025-06-20 --analyze --scenario-out draft.json

Public data only (Earth Search); no AWS. See eval/README.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from fieldwatch import contracts as C
from fieldwatch.analysis import scenes as S


def _glue_negatives(argv):
    """argparse reads `--bbox -96.5,...` as a flag; glue the value on with `=`."""
    out, i = [], 0
    argv = list(sys.argv[1:] if argv is None else argv)
    while i < len(argv):
        if argv[i] in ("--bbox", "--point") and i + 1 < len(argv):
            out.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def _args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--geojson", help="polygon GeoJSON file (Polygon, Feature or FeatureCollection)")
    g.add_argument("--bbox", help="west,south,east,north in degrees")
    g.add_argument("--point", help="lat,lon (needs --acres)")
    p.add_argument("--acres", type=float, help="field size for --point")
    p.add_argument("--event-date", type=date.fromisoformat, help="YYYY-MM-DD: bracket this date")
    p.add_argument("--year", type=int, help="list clear dates Apr 1 to Oct 31 of this year")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--window-days", type=int, default=45, help="search this far each side of --event-date")
    p.add_argument("--min-clear", type=float, default=S.DEFAULT_MIN_CLEAR)
    p.add_argument("--all", action="store_true", help="also list dates below --min-clear")
    p.add_argument("--analyze", action="store_true", help="run analyze() on the after-date (or the last clear date)")
    p.add_argument("--crop", default="corn")
    p.add_argument("--out-dir", default="scenes_out", help="overlay PNG folder for --analyze")
    p.add_argument("--scenario-out", help="write a draft contracts.Scenario JSON here")
    p.add_argument("--scenario-id", default="flood-draft")
    p.add_argument("--farm-fixture", default="demo_farm.json")
    p.add_argument("--field-id", default="REPLACE-WITH-FIELD-ID")
    a = p.parse_args(_glue_negatives(argv))
    if a.point and not a.acres:
        p.error("--point needs --acres")
    if not (a.event_date or a.year or (a.start and a.end)):
        p.error("give --event-date, --year, or --start and --end")
    return a


def _polygon(a):
    if a.geojson:
        return S.polygon_from_geojson(json.loads(Path(a.geojson).read_text()))
    if a.bbox:
        return S.polygon_from_bbox([float(v) for v in a.bbox.split(",")])
    lat, lon = (float(v) for v in a.point.split(","))
    return S.polygon_from_point(lat, lon, a.acres)


def _table(scenes, mark=None):
    print(f"{'date':<11}{'clear':>7}{'tile cloud':>12}  {'tile':<6}scene_id")
    for s in scenes:
        flag = "" if s.clear else "  (below threshold)"
        tag = f"  <- {mark[s.scene_id]}" if mark and s.scene_id in mark else ""
        print(f"{s.date.isoformat():<11}{s.clear_fraction:>7.0%}{s.tile_cloud_cover:>11.0f}%  {s.tile_id:<6}{s.scene_id}{flag}{tag}")


def _analyze(a, polygon, scene):
    from fieldwatch.analysis import analyze
    from fieldwatch.analysis.writer import LocalWriter

    req = C.FieldAnalysisRequest(field=C.FieldRef(farm_id="scene-finder", field_id=a.field_id), polygon=polygon,
                                 crop=a.crop, as_of=scene.date.isoformat(), scene=scene.scene,
                                 min_clear_fraction=a.min_clear)
    res = analyze(req, writer=LocalWriter(a.out_dir))  # layers: none wired here, so overlaps read 0
    print(f"\nanalyze({scene.date}): status={res.status} clear={res.clear_fraction:.0%}")
    if res.error:
        print(f"  error {res.error.code}: {res.error.message}")
        return res
    if res.anomaly:
        an = res.anomaly
        print(f"  field_score={an.field_score:.2f} z_baseline={an.z_baseline} delta_since_last={an.delta_since_last}")
        print(f"  regions: {len(an.regions)} (low-spot overlap is 0.00 when no low-spot layer is supplied)")
        for r in an.regions:
            f = r.features
            print(f"   {r.region_id}: {r.area_acres:.1f} ac score={r.score:.2f} at ({r.centroid[0]:.5f},{r.centroid[1]:.5f})"
                  f" ring={f.ring_score:.2f} wedge={f.wedge_score:.2f} edge={f.edge_band_score:.2f}"
                  f" stripe={f.stripe_score:.2f} lowspot={f.lowspot_overlap:.2f} sandy={f.sandy_soil_overlap:.2f}")
    if res.indices:
        print(f"  NDVI mean={res.indices.ndvi.mean:.2f} p10={res.indices.ndvi.p10:.2f} p90={res.indices.ndvi.p90:.2f}")
    if res.analysis_key:
        print(f"  overlays: {Path(a.out_dir) / res.analysis_key}")
    return res


def main(argv=None) -> int:
    a = _args(argv)
    polygon = _polygon(a)
    loader = S.StacLoader()
    if a.event_date:
        br = S.bracket_event(polygon, a.event_date, min_clear=a.min_clear, before_days=a.window_days,
                             after_days=a.window_days, loader=loader, include_cloudy=a.all)
        mark = {}
        if br.before:
            mark[br.before.scene_id] = "BEFORE (nearest clear)"
        if br.after:
            mark[br.after.scene_id] = "AFTER (nearest clear)"
        print(f"Event {a.event_date}, +/-{a.window_days} days, min clear {a.min_clear:.0%}")
        _table(br.scenes, mark)
        print(f"\nbefore: {br.before.date if br.before else 'none'}   after: {br.after.date if br.after else 'none'}")
        chosen = br.after or br.before
    else:
        start = a.start or date(a.year, 4, 1)
        end = a.end or date(a.year, 10, 31)
        scenes = S.find_clear_scenes(polygon, start, end, min_clear=a.min_clear, loader=loader, include_cloudy=a.all)
        print(f"{start} to {end}, min clear {a.min_clear:.0%}: {sum(s.clear for s in scenes)} clear dates")
        _table(scenes)
        chosen = next((s for s in reversed(scenes) if s.clear), None)
    if a.analyze:
        if chosen is None:
            print("nothing to analyze: no clear scene")
        else:
            _analyze(a, polygon, chosen)
    if a.scenario_out:
        if chosen is None:
            print("no clear scene: draft scenario not written")
            return 1
        sc = S.draft_scenario(a.scenario_id, a.farm_fixture, a.field_id, chosen.date)
        Path(a.scenario_out).write_text(json.dumps(sc.model_dump(mode="json"), indent=2) + "\n")
        print(f"\ndraft scenario -> {a.scenario_out} (set field_id, farm_fixture, expected.top_condition/action_type)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
