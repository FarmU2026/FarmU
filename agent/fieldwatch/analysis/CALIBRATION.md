# Analyzer calibration (WS1)

Real Sentinel-2 (Earth Search `sentinel-2-c1-l2a`, T14TPM) scenes, run through the live analyzer on the Thurston Co. NE
demo area (the "north pivot" box, lon -96.548..-96.532, lat 42.152..42.168, corn) and two neighbours. Baseline is the
same weeks of the 3 prior seasons. "Emit" = the decide rule: status ok, no gate, a region with score >= 2 and >= 1.2 ac.

## What the real scenes showed

- The demo box is not one field: it holds ~6 fields (corn, soy, a pale harvested or bare strip, a grass waterway).
  Whole-box runs therefore flag whatever differs from the baseline: that is a polygon problem, not an analyzer one.
  The sub-fields (NW, SW) are uniform corn and make the clean cases; the SE field holds the real problem case.
- **Real problem (case d):** in the SE field a trapezoid of ~13 ac has no crop in 2026: 8.8 ac score 3.7 on
  2026-06-22 (emerging), 13.5 ac score 6.0 on 2026-07-17, 13.9 ac / 9.4 on 07-27, 13.6 ac / 9.5 on 08-01, 11.6 ac / 4.4 on 08-21.
  Not in the 2025 baseline. It is the demo-date (07-17) hit and persists, so repeat texts belong to the watch logic.
- NDVI saturates at peak canopy: pixel sigma of (now - baseline) is 0.009-0.012 in late July-August (the 0.02
  SIGMA_FLOOR governs), but 0.035-0.047 on 2026-07-17 because one prior season's mid-July scene has its own
  pixel sigma up to 0.09 (growth-stage and crop-rotation mismatch). The demo date is the noisiest date, so it needed the most care.
- Canopy trajectory (NW/SW corn median NDVI): 0.89 (07-17), 0.90 (08-01), 0.88 (08-16), 0.86 (09-10), 0.78 (09-18, -0.09 vs previous pass), 0.35 (10-05).
- No real whole-field regional drop occurred in 2025-26 on these fields (z_baseline between -1.6 and +3.9 on clean
  dates), so the regional rule keeps its thresholds (z_baseline <= -2 and 60% of pixels down 0.05). It is covered by the synthetic test.

## Injection test on real noise

Real NW and SW field scenes (7 dates) with a known NDVI drop subtracted in a ring (8 ac), a wedge (2.7 ac) and patches
(3 and 8 ac). Detected as emit, summer dates, drops of 0.08 / 0.12 NDVI:

| shape | before (mean baseline, no small tier) | after |
|---|---|---|
| 3 ac patch | 7/8, 7/8 | 7/8, 8/8 |
| 8 ac patch | 7/8, 8/8 | 8/8, 8/8 |
| ring 8 ac | 6/8, 7/8 | 6/8, 7/8 |
| wedge 2.7 ac | 7/8, 8/8 | 5/8, 7/8 |
| clean fields, emits | 0 (but many 1-3 ac blips on real mixed polygons) | 0 |

The 07-17 ring is the weak spot (0.12 drop is missed on the NW field, 8 ac found on the SW field); 0.2 is found on both.
Tried and rejected: growing regions to 0.6-0.85 x the z threshold (more ring recovered, but 2-3 spurious emits on clean fields).

## Changes (and why)

| Change | File | Reason |
|---|---|---|
| Baseline = per-pixel median when >= 3 prior seasons (mean for 2) | regions.py | one odd season (haze edge, rotation, its own problem) leaked into the mean; 07-17 ring/patch detection improved (3 ac patch at 0.08 on 07-17: 1 of 2 fields detected -> 2 of 2) |
| Small regions (< 2.5 ac) need score >= 3.0 | regions.py, core.py | real clean/mixed fields gave 1.2-2.5 ac blips at score 2.2-2.7; strong small patches (>= 3) still pass |
| Senescence also opens from 14 days before the calendar onset when the median NDVI fell >= 0.05 since the previous pass | gates.py, core.py | hot/dry years dry down before Sep 5 (corn) / Sep 1 (soy); the calendar alone would text about a drying canopy |
| Harvest/senescence both open after Sep 15 when the field median NDVI <= 0.35 | gates.py, core.py | harvested or dead canopy is not a problem area; early-harvested fields before the Oct 5 / Sep 25 dates |
| Wedge score multiplied by a radial-elongation factor (radial extent vs arc width) | patterns.py | a compact blob in a narrow angle far from the pivot is not a wedge (real blobs scored wedge 0.37-0.83; real sectors stay 0.83) |
| z threshold 2.0, min region 1.2 ac, 3x3 smoothing, regional rule, ring/edge/stripe cutoffs: unchanged | | no real case argued for moving them (clean fields quiet; 0.08 NDVI drop already detected) |

## Cases, before and after

Columns: as_of, scene date used, clear fraction, z_baseline, field NDVI median, gates after tuning, result before and after.
"EMIT" would text the farmer; "GATED" means regions exist but a gate holds them back.

| Case | as_of | scene date | clear | z_base | NDVI p50 | gates (sen/harv/reg) | before | after |
|---|---|---|---|---|---|---|---|---|
| NW field (uniform corn) | 2025-07-18 | 2025-07-15 | 1.00 | 0.9 | 0.911 | 000 | quiet | quiet |
| NW field (uniform corn) | 2025-08-12 | 2025-08-11 | 1.00 | -1.56 | 0.791 | 000 | quiet | quiet |
| NW field (uniform corn) | 2026-06-25 | 2026-06-22 | 0.79 | -1.09 | 0.382 | 000 | quiet | quiet |
| NW field (uniform corn) | 2026-07-05 | 2026-07-05 | 1.00 | -1.28 | 0.723 | 000 | quiet | quiet |
| NW field (uniform corn) | 2026-07-17 | 2026-07-17 | 1.00 | -0.01 | 0.889 | 000 | quiet | quiet |
| NW field (uniform corn) | 2026-07-27 | 2026-07-27 | 1.00 | 0.15 | 0.893 | 000 | quiet | quiet |
| NW field (uniform corn) | 2026-08-10 | 2026-08-01 | 1.00 | 1.76 | 0.932 | 000 | quiet | quiet |
| NW field (uniform corn) | 2026-08-25 | 2026-08-21 | 1.00 | 1.09 | 0.946 | 000 | quiet | quiet |
| NW field (uniform corn) | 2026-09-12 | 2026-09-10 | 1.00 | 0.46 | 0.916 | 100 | quiet (gated) | quiet (gated) |
| NW field (uniform corn) | 2026-09-22 | 2026-09-18 | 1.00 | 1.93 | 0.858 | 100 | quiet (gated) | quiet (gated) |
| NW field (uniform corn) | 2026-10-01 | 2026-09-18 | 1.00 | 1.93 | 0.858 | 100 | quiet (gated) | quiet (gated) |
| NW field (uniform corn) | 2026-10-08 | 2026-10-05 | 1.00 | 0.76 | 0.345 | 110 | quiet (gated) | quiet (gated) |
| SW field (uniform corn) | 2025-07-18 | 2025-07-15 | 1.00 | 0.28 | 0.792 | 000 | 1 reg, top 2 ac s2.4 **EMIT** | quiet |
| SW field (uniform corn) | 2025-08-12 | 2025-08-11 | 1.00 | -0.63 | 0.89 | 000 | quiet | quiet |
| SW field (uniform corn) | 2026-06-25 | 2026-06-22 | 0.97 | 1.41 | 0.511 | 000 | 1 reg, top 2 ac s3.1 **EMIT** | 1 reg, top 2 ac s3.1 **EMIT** |
| SW field (uniform corn) | 2026-07-05 | 2026-07-05 | 1.00 | 0.34 | 0.875 | 000 | quiet | quiet |
| SW field (uniform corn) | 2026-07-17 | 2026-07-17 | 1.00 | 0.01 | 0.907 | 000 | quiet | quiet |
| SW field (uniform corn) | 2026-07-27 | 2026-07-27 | 1.00 | -0.7 | 0.874 | 000 | quiet | quiet |
| SW field (uniform corn) | 2026-08-10 | 2026-08-01 | 1.00 | 0.43 | 0.906 | 000 | quiet | quiet |
| SW field (uniform corn) | 2026-08-25 | 2026-08-21 | 1.00 | 0.61 | 0.904 | 000 | quiet | quiet |
| SW field (uniform corn) | 2026-09-12 | 2026-09-10 | 1.00 | 0.58 | 0.859 | 100 | quiet (gated) | quiet (gated) |
| SW field (uniform corn) | 2026-09-22 | 2026-09-18 | 1.00 | 1.26 | 0.778 | 100 | quiet (gated) | quiet (gated) |
| SW field (uniform corn) | 2026-10-01 | 2026-09-18 | 1.00 | 1.26 | 0.778 | 100 | quiet (gated) | quiet (gated) |
| SW field (uniform corn) | 2026-10-08 | 2026-10-05 | 1.00 | 0.48 | 0.345 | 110 | quiet (gated) | quiet (gated) |
| SE field (bare trapezoid in 2026) | 2025-07-18 | 2025-07-15 | 1.00 | 0.04 | 0.734 | 000 | quiet | quiet |
| SE field (bare trapezoid in 2026) | 2025-08-12 | 2025-08-11 | 0.96 | -0.25 | 0.885 | 000 | 1 reg, top 5 ac s2.7 **EMIT** | 1 reg, top 5 ac s2.7 **EMIT** |
| SE field (bare trapezoid in 2026) | 2026-06-25 | 2026-06-22 | 0.97 | 1.38 | 0.515 | 000 | 2 reg, top 9 ac s3.4 **EMIT** | 2 reg, top 9 ac s3.7 **EMIT** |
| SE field (bare trapezoid in 2026) | 2026-07-05 | 2026-07-05 | 1.00 | 0.63 | 0.82 | 000 | 1 reg, top 5 ac s3.7 **EMIT** | 1 reg, top 4 ac s3.0 **EMIT** |
| SE field (bare trapezoid in 2026) | 2026-07-17 | 2026-07-17 | 1.00 | 0.67 | 0.883 | 000 | 1 reg, top 13 ac s5.2 **EMIT** | 1 reg, top 14 ac s6.0 **EMIT** |
| SE field (bare trapezoid in 2026) | 2026-07-27 | 2026-07-27 | 1.00 | -0.48 | 0.86 | 000 | 1 reg, top 14 ac s9.4 **EMIT** | 1 reg, top 14 ac s9.4 **EMIT** |
| SE field (bare trapezoid in 2026) | 2026-08-10 | 2026-08-01 | 1.00 | 0.52 | 0.896 | 000 | 1 reg, top 14 ac s9.5 **EMIT** | 1 reg, top 14 ac s9.5 **EMIT** |
| SE field (bare trapezoid in 2026) | 2026-08-25 | 2026-08-21 | 1.00 | 0.66 | 0.892 | 000 | 1 reg, top 12 ac s4.2 **EMIT** | 1 reg, top 12 ac s4.4 **EMIT** |
| SE field (bare trapezoid in 2026) | 2026-09-12 | 2026-09-10 | 1.00 | 0.62 | 0.852 | 100 | quiet (gated) | quiet (gated) |
| SE field (bare trapezoid in 2026) | 2026-09-22 | 2026-09-18 | 1.00 | 2.28 | 0.763 | 100 | 1 reg, top 8 ac s2.5 GATED | 1 reg, top 5 ac s2.3 GATED |
| SE field (bare trapezoid in 2026) | 2026-10-01 | 2026-09-18 | 1.00 | 2.28 | 0.763 | 100 | 1 reg, top 8 ac s2.5 GATED | 1 reg, top 5 ac s2.3 GATED |
| SE field (bare trapezoid in 2026) | 2026-10-08 | 2026-10-05 | 1.00 | 3.11 | 0.362 | 110 | quiet (gated) | quiet (gated) |
| Whole demo box (6 fields) | 2025-07-18 | 2025-07-15 | 1.00 | 0.67 | 0.833 | 000 | quiet | quiet |
| Whole demo box (6 fields) | 2025-08-12 | 2025-08-11 | 0.98 | -0.78 | 0.838 | 000 | 2 reg, top 21 ac s2.5 **EMIT** | 1 reg, top 21 ac s2.5 **EMIT** |
| Whole demo box (6 fields) | 2026-06-25 | 2026-06-22 | 0.96 | 0.08 | 0.482 | 000 | quiet | quiet |
| Whole demo box (6 fields) | 2026-07-05 | 2026-07-05 | 1.00 | 0.36 | 0.806 | 000 | 2 reg, top 6 ac s2.2 **EMIT** | quiet |
| Whole demo box (6 fields) | 2026-07-17 | 2026-07-17 | 1.00 | 0.14 | 0.886 | 000 | 4 reg, top 34 ac s3.6 **EMIT** | 3 reg, top 41 ac s4.0 **EMIT** |
| Whole demo box (6 fields) | 2026-07-27 | 2026-07-27 | 1.00 | -0.5 | 0.865 | 000 | 4 reg, top 25 ac s8.4 **EMIT** | 4 reg, top 25 ac s8.3 **EMIT** |
| Whole demo box (6 fields) | 2026-08-10 | 2026-08-01 | 1.00 | 0.58 | 0.902 | 000 | 3 reg, top 24 ac s4.0 **EMIT** | 3 reg, top 28 ac s4.4 **EMIT** |
| Whole demo box (6 fields) | 2026-08-25 | 2026-08-21 | 1.00 | 0.77 | 0.902 | 000 | 4 reg, top 5 ac s2.4 **EMIT** | 4 reg, top 21 ac s2.9 **EMIT** |
| Whole demo box (6 fields) | 2026-09-12 | 2026-09-10 | 1.00 | 0.64 | 0.861 | 100 | 3 reg, top 27 ac s4.8 GATED | 1 reg, top 29 ac s6.0 GATED |
| Whole demo box (6 fields) | 2026-09-22 | 2026-09-18 | 1.00 | 1.5 | 0.776 | 100 | 2 reg, top 39 ac s3.1 GATED | 1 reg, top 33 ac s3.0 GATED |
| Whole demo box (6 fields) | 2026-10-01 | 2026-09-18 | 1.00 | 1.5 | 0.776 | 100 | 2 reg, top 39 ac s3.1 GATED | 1 reg, top 33 ac s3.0 GATED |
| Whole demo box (6 fields) | 2026-10-08 | 2026-10-05 | 1.00 | 1.08 | 0.352 | 110 | 1 reg, top 2 ac s2.2 GATED | quiet (gated) |
| Johnson box (soy, mixed) | 2025-07-18 | 2025-07-15 | 1.00 | 0.67 | 0.871 | 000 | 4 reg, top 29 ac s3.8 **EMIT** | 4 reg, top 29 ac s3.8 **EMIT** |
| Johnson box (soy, mixed) | 2025-08-12 | 2025-08-11 | 1.00 | -0.64 | 0.877 | 000 | 4 reg, top 13 ac s7.6 **EMIT** | 3 reg, top 13 ac s7.6 **EMIT** |
| Johnson box (soy, mixed) | 2026-06-25 | 2026-06-20 | 1.00 | 1.83 | 0.579 | 000 | 1 reg, top 7 ac s2.5 **EMIT** | 3 reg, top 24 ac s3.1 **EMIT** |
| Johnson box (soy, mixed) | 2026-07-05 | 2026-06-30 | 1.00 | 0.7 | 0.782 | 000 | 4 reg, top 10 ac s2.6 **EMIT** | 1 reg, top 8 ac s2.2 **EMIT** |
| Johnson box (soy, mixed) | 2026-07-17 | 2026-07-17 | 1.00 | 0.18 | 0.894 | 000 | 3 reg, top 23 ac s5.8 **EMIT** | 3 reg, top 25 ac s7.0 **EMIT** |
| Johnson box (soy, mixed) | 2026-07-27 | 2026-07-27 | 1.00 | -0.68 | 0.855 | 000 | 1 reg, top 37 ac s6.4 **EMIT** | 2 reg, top 43 ac s7.0 **EMIT** |
| Johnson box (soy, mixed) | 2026-08-10 | 2026-08-01 | 1.00 | 0.46 | 0.894 | 000 | 3 reg, top 33 ac s6.2 **EMIT** | 4 reg, top 37 ac s7.5 **EMIT** |
| Johnson box (soy, mixed) | 2026-08-25 | 2026-08-21 | 1.00 | 0.51 | 0.893 | 000 | quiet | 2 reg, top 6 ac s2.6 **EMIT** |
| Johnson box (soy, mixed) | 2026-09-12 | 2026-09-10 | 1.00 | 0.59 | 0.838 | 100 | 1 reg, top 8 ac s2.2 GATED | quiet (gated) |
| Johnson box (soy, mixed) | 2026-09-22 | 2026-09-18 | 1.00 | 1.6 | 0.74 | 100 | 1 reg, top 9 ac s2.4 GATED | 1 reg, top 10 ac s2.6 GATED |
| Johnson box (soy, mixed) | 2026-10-01 | 2026-09-18 | 1.00 | 1.6 | 0.74 | 100 | 1 reg, top 9 ac s2.4 GATED | 1 reg, top 10 ac s2.6 GATED |
| Johnson box (soy, mixed) | 2026-10-08 | 2026-10-05 | 1.00 | 1.2 | 0.333 | 110 | quiet (gated) | quiet (gated) |
| East 80 box (corn, mixed) | 2025-07-18 | 2025-07-15 | 1.00 | 1.99 | 0.897 | 000 | 1 reg, top 10 ac s3.9 **EMIT** | 1 reg, top 10 ac s3.9 **EMIT** |
| East 80 box (corn, mixed) | 2025-08-12 | 2025-08-08 | 1.00 | -0.3 | 0.838 | 000 | 1 reg, top 2 ac s3.3 **EMIT** | 1 reg, top 2 ac s3.3 **EMIT** |
| East 80 box (corn, mixed) | 2026-06-25 | 2026-06-22 | 1.00 | -1.01 | 0.464 | 000 | quiet | quiet |
| East 80 box (corn, mixed) | 2026-07-05 | 2026-07-05 | 1.00 | -0.41 | 0.764 | 000 | 1 reg, top 2 ac s2.7 **EMIT** | quiet |
| East 80 box (corn, mixed) | 2026-07-17 | 2026-07-17 | 1.00 | 0.06 | 0.888 | 000 | 1 reg, top 13 ac s5.2 **EMIT** | 1 reg, top 14 ac s6.6 **EMIT** |
| East 80 box (corn, mixed) | 2026-07-27 | 2026-07-27 | 1.00 | -0.19 | 0.867 | 000 | 1 reg, top 27 ac s4.5 **EMIT** | 1 reg, top 24 ac s4.2 **EMIT** |
| East 80 box (corn, mixed) | 2026-08-10 | 2026-08-01 | 1.00 | 1.13 | 0.917 | 000 | 2 reg, top 32 ac s8.7 **EMIT** | 1 reg, top 32 ac s6.4 **EMIT** |
| East 80 box (corn, mixed) | 2026-08-25 | 2026-08-21 | 1.00 | 1.49 | 0.937 | 000 | 2 reg, top 30 ac s4.3 **EMIT** | 2 reg, top 27 ac s3.2 **EMIT** |
| East 80 box (corn, mixed) | 2026-09-12 | 2026-09-10 | 1.00 | 3.72 | 0.879 | 100 | 1 reg, top 18 ac s2.7 GATED | 1 reg, top 8 ac s2.6 GATED |
| East 80 box (corn, mixed) | 2026-09-22 | 2026-09-18 | 1.00 | 6.95 | 0.754 | 100 | 1 reg, top 7 ac s2.2 GATED | 1 reg, top 8 ac s2.3 GATED |
| East 80 box (corn, mixed) | 2026-10-01 | 2026-09-18 | 1.00 | 6.95 | 0.754 | 100 | 1 reg, top 7 ac s2.2 GATED | 1 reg, top 8 ac s2.3 GATED |
| East 80 box (corn, mixed) | 2026-10-08 | 2026-10-05 | 1.00 | 1.45 | 0.309 | 110 | quiet (gated) | quiet (gated) |

Reading the table: NW/SW (uniform corn) never emit in summer, before or after, and end-of-season rows are all gated
(gates 1xx). The mixed-polygon rows keep emitting on bare ground, waterways and other crops inside the polygon;
that is correct for what is in the polygon, and the fix is a field boundary that matches one field.
