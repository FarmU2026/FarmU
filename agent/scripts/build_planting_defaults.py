"""Rebuild fieldwatch/analysis/reference/planting_defaults.json from USDA NASS Quick Stats (weekly Crop Progress).

For each state and crop, take every year's weekly "percent planted" series, interpolate the date it crosses 50%, and
keep the median of those dates over the last YEARS years (month-day of that median).

How to run (needs a free key from https://quickstats.nass.usda.gov/api):

    cd agent
    NASS_API_KEY=... python scripts/build_planting_defaults.py            # writes the data file
    NASS_API_KEY=... python scripts/build_planting_defaults.py --years 10 --states NE,IA,KS,SD,MN,IL,MO

The key is read from the environment only; never commit it. Review the diff of the JSON before committing.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

API = "https://quickstats.nass.usda.gov/api/api_GET/"
OUT = Path(__file__).resolve().parents[1] / "fieldwatch" / "analysis" / "reference" / "planting_defaults.json"
CROPS = {"corn": "CORN", "soybeans": "SOYBEANS"}
DEFAULT_STATES = "NE,IA,KS,SD,MN,IL,MO"


def crossing(points: list[tuple[date, float]], level: float = 50.0) -> date | None:
    """Date a rising series first reaches `level`, linearly interpolated between the two weeks that straddle it."""
    pts = sorted(points)
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if v0 < level <= v1:
            return d0 + timedelta(days=round((level - v0) / (v1 - v0) * (d1 - d0).days))
    return None


def median_mmdd(dates: list[date]) -> str:
    """Median of the dates' day-of-year, shown as MM-DD in a non-leap year."""
    doy = int(round(statistics.median(d.timetuple().tm_yday - (1 if d.year % 4 == 0 and d.month > 2 else 0) for d in dates)))
    return (date(2001, 1, 1) + timedelta(days=doy - 1)).strftime("%m-%d")


def fetch(key: str, commodity: str, state: str, first_year: int, *, http_get=requests.get) -> list[dict]:
    r = http_get(API, params={
        "key": key, "source_desc": "SURVEY", "sector_desc": "CROPS", "commodity_desc": commodity,
        "statisticcat_desc": "PROGRESS", "unit_desc": "PCT PLANTED", "agg_level_desc": "STATE", "state_alpha": state,
        "year__GE": first_year, "freq_desc": "WEEKLY", "format": "JSON"}, timeout=60)
    r.raise_for_status()
    return r.json().get("data", [])


def state_median(rows: list[dict]) -> tuple[str, list[int]] | None:
    by_year: dict[int, list[tuple[date, float]]] = {}
    for row in rows:
        try:
            by_year.setdefault(int(row["year"]), []).append(
                (datetime.strptime(row["week_ending"], "%Y-%m-%d").date(), float(row["Value"])))
        except (KeyError, ValueError):
            continue  # "(D)" withheld values and the like
    dates = {y: c for y, pts in by_year.items() if (c := crossing(pts))}
    return (median_mmdd(list(dates.values())), sorted(dates)) if dates else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--states", default=DEFAULT_STATES)
    args = ap.parse_args(argv)
    key = os.environ.get("NASS_API_KEY")
    if not key:
        print("set NASS_API_KEY (free key: https://quickstats.nass.usda.gov/api)", file=sys.stderr)
        return 2
    first = date.today().year - args.years
    out = {"_comment": "Typical 50%-planted date per state. Regenerate with agent/scripts/build_planting_defaults.py.",
           "method": f"median over years of the date each year's weekly NASS percent-planted series crosses 50% (linear interpolation), {first}-{date.today().year - 1}",
           "source": "USDA NASS Quick Stats API (Crop Progress, PCT PLANTED, weekly, state level)",
           "generated": date.today().isoformat(), "crops": {}}
    for crop, commodity in CROPS.items():
        for state in args.states.split(","):
            res = state_median(fetch(key, commodity, state, first))
            if res:
                out["crops"].setdefault(crop, {})[state] = {"median_50pct_planted": res[0], "years": res[1], "verified": True}
            else:
                print(f"no data for {crop} {state}", file=sys.stderr)
    OUT.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
