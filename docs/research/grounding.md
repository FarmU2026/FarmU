# Grounding FieldWatch diagnoses in agronomy and remote-sensing standards

Status: research memo, no code changed. Written 2026-10-16 for the analyzer / scorer redesign.

## How to read this memo (honesty notes)

- Source labels: **[read]** = I opened the page and read it; **[snippet]** = seen only through a search-result summary, not read in full; **[cite-only]** = I confirmed the citation exists (title/journal) but did not see the content. Anything labeled **unverified** is my judgment or a number I could not trace to a source.
- I could not read several documents (Iowa State in-season N PDF, RMA corn handbook, an MDPI page that returned 403). Where a number lives in one of those, I name the document and say "look it up" rather than quoting it.
- **Thresholds that are FieldWatch design choices (not from literature) are tagged `[design]`.** They must be calibrated on your own scenes (the Thurston Co. archive in `CALIBRATION.md` is the right fixture set). Published literature almost never gives Sentinel-2 field-scale thresholds for these conditions; it gives mechanisms, stage windows, and loss tables. The memo is built on that division: literature supplies *when a condition can exist and what rules it out*; your data supplies *the pixel thresholds*.

---

## 1. Summary: the 10 most important changes, ranked by impact on the observed failures

Failure A = harvested/mature fields flagged. Failure B = "everything is nitrogen". Failure C = run-to-run instability.

1. **Add a crop-state machine ahead of every anomaly rule (fixes A).** States: `pre-emergence -> vegetative -> reproductive -> senescing -> harvest-in-progress -> harvested`. Anomaly-vs-baseline diagnosis is only allowed in the vegetative and reproductive states. Entering `senescing` is one-way for the season (no re-arming). It applies to the scheduled path *and* the on-demand "ask" path (the ask path may still describe the imagery, but must say "season over / harvest under way" instead of naming a stress). Sections 3.1, 4.
2. **Stop comparing to the same calendar weeks of prior years once the canopy peaks (fixes A).** Late-season departures from a day-of-year baseline measure *timing* (planting date, hybrid, heat, harvest date), not damage. A field harvested earlier than in prior years is, by construction, "far below baseline". Use a phenology-aligned (GDD or NDVI-curve-aligned) baseline, same-crop years only, and switch the baseline off once senescence starts. Section 3.1.
3. **Detect harvest by change, not by whole-field median (fixes A).** The current gate needs median NDVI <= 0.35 after Sep 15 (or the calendar date), so a half-harvested field (median still high) or one harvested just before the calendar date is flagged. Replace with: per-pixel abrupt NDVI drop between consecutive clear passes, spatially coherent with straight (combine-pass) edges, after senescence onset; once any such cluster exists, the whole field is `harvest-in-progress` and all negative-departure alerts are suppressed. Section 3.1.
4. **Make "cause not determined" the default output, not a competitor in the log-odds race (fixes B).** A cause may be named only if it has at least one *necessary, field-specific* evidence item, is inside its valid stage window, has no ruling-out evidence, and clears a posterior and margin test. Otherwise output "weak area found, cause not determined" plus a ranked scouting checklist. Section 5.1.
5. **Separate evidence into three classes and cap the two context classes (fixes B).** Class A field-specific (this field's pixels, geometry, event overlap, farmer records, photos); Class B local static context (soil, terrain, drainage class); Class C regional/temporal context (30-day rain, drought monitor, county disease reports). Class C may only move the *prior* by a bounded amount and can never be the sole support for a condition. Today a corn field at vegetative stage reaches nitrogen p = 0.69 ("likely") from `rain_excess_30d` (+1.2) and `prior_crop_soybeans` (+0.8) alone, with zero field evidence (arithmetic in 5.2). Section 5.
6. **Nitrogen: require an N signature, valid only in a stage window, relative to a reference (fixes B).** The accepted standard is a sufficiency index (SI = target / well-fertilized reference) read in roughly V6-V12 with a red-edge index, not NDVI. Without a strip, a within-field P90 reference only finds *within-field* N variation; a uniformly deficient or uniformly stressed field is invisible to it, and any non-N stressor produces the same SI drop. So the system may say "pattern consistent with N" only when the pattern follows N-relevant geography (sandy/poorly drained soil, application boundaries, low spots with prior saturation) and water, hail, pivot, skip and disease alternatives are ruled out. Section 3.2.
7. **Compute evidence in code only; LLMs never choose keys (fixes C).** Deterministic extraction of a fixed evidence vector per field/date (every key evaluated, value `present / absent / not_evaluable`). LLMs are limited to closed-schema perception (photo checklist, farmer reply -> fixed fields). Same inputs must give identical scorer input. Section 5.4.
8. **Give each condition a stage-validity mask and a ruling-out list, as multiplicative gates rather than weights.** E.g. planter skips are only visible before canopy closure; hail needs an event in a time window *before* the drop; ponding needs a rain/saturation event and a topographic low; nothing is diagnosed after R6 (corn) / R7 (soy) except "mature". Section 3.
9. **De-correlate evidence.** `rain_excess_30d`, `heavy_rain_7d`, `soil_poorly_drained`, `lowspot_overlap` and `floodplain_overlap` are facets of one latent variable; summing them as independent log-likelihood ratios double counts. Group into clusters and take the strongest member per cluster. Section 5.2.
10. **Collect labels from day one and calibrate.** Log every alert with a feedback prompt (confirmed cause / not a problem / unknown). Report precision per condition, abstention rate, Brier score and a reliability diagram once you have a few hundred labeled alerts; until then do not display numeric probabilities as if calibrated (use "likely / possible / not determined"). Sections 5.5, 5.6.

---

## 2. What the current design does, and why it fails (code reading, brief)

- **Baseline:** `regions.baseline_mean` takes the per-pixel median of the same-date (+-10 days) NDVI of 3 prior seasons, whatever the crop was. `pixel_z` removes the field median, so a uniformly late/early field is not flagged on z, but a *partly* harvested field has a bimodal diff and the harvested pixels are exactly the connected, high-|z| region the detector is built to find.
- **Harvest gate:** `gates.seasonal` opens harvest on calendar (corn Oct 5, soy Sep 25) or when `ndvi_p50 <= 0.35` after Sep 15. A field harvested a few days before the calendar date, or half harvested, escapes both. Senescence opens early only if the *median* delta since the last pass is <= -0.05, again a whole-field statistic. `CALIBRATION.md` notes real harvested corn at median 0.31-0.36 and mixed polygons still producing regions that only the gate holds back.
- **Scorer:** `scoring._score` adds weights to a prior logit per condition independently; there is no normalization and no abstention. `unknown` has prior 0.10 and two positive-only weights (+1.0, +1.5), so it can never beat a condition that has any context evidence. Nitrogen starts at logit(0.12) + 0.5 (corn) + 0.3 (vegetative) = -1.19 (p = 0.23).
- **Evidence:** `evidence_from_analysis` is deterministic for pattern and rain; the specialist LLMs add the rest and pick `weight_key`s, which is the instability source.

---

## 3. Per condition

Format per condition: accepted method; Sentinel-2 rule(s); stage window; evidence required before naming; ruling-out evidence; citations. Index definitions used below: NDVI = (B8-B4)/(B8+B4); NDRE = (B8A-B5)/(B8A+B5) (B8A is the usual narrow NIR for red-edge indices; the code uses B8, acceptable, but note B5/B8A/B11 are native 20 m, so a 10 m interior buffer does not remove mixed pixels for NDRE/NDMI; consider a 20 m buffer for those); NDMI = (B8-B11)/(B8+B11) (the Gao-type NDMI, not McFeeters NDWI) [snippet: eos.com NDMI page].

### 3.1 Harvest / maturity (the "stop alerting" logic)

**Accepted methods**
- *Phenology from satellite.* Time-series methods on 10-30 m optical data (HLS / Sentinel-2) are accepted for planting, emergence, maturity and harvest dates. Reported accuracy [snippet unless noted]:
  - Emergence (green-up) from HLS with the WISE algorithm: about 3 days later than observed at field scale (Gao et al., 2021, Remote Sensing). A Maryland comparison 2019-2023: HLS and Sentinel-2 caught most emergences with mean difference < 3 d, MAD < 7 d, RMSE < 9 d.
  - Near-real-time fusion tracks NASS crop progress within <= 5 days, R2 > 0.85 (Shen et al., 2023, RSE).
  - Derivative DTW on Sentinel-2 NDVI: corn BBCH phenology RMSE < 6 days overall (Remote Sensing 2023).
  - Harvest dates: HLS double-logistic + derivative, corn harvest RMSE 6-15 days (Remote Sensing 17(17):2927, 2025, Brazil); JRC Sentinel-2 harvest-date maps vs machine RTK records, lag < 10 days for 65.7% of fields, 10-30 days for 26.1%, 3-year RMSE 16.1 days; NHPI harvest at 500 m MAE about 5 days vs USDA crop progress (Diao lab, ESSD CropPlantHarvest 2026).
  - Lesson: **timing from S2 alone is good to about +-1 week; it is never good enough to say "the crop is not yet mature" on a specific day.** Use it as one vote, not as a gate by itself.
- *GDD with planting date* (86/50 method: GDD = (min(Tmax,86F) + max(Tmin,50F))/2 - 50). Emergence takes about 90-120 GDD after planting [snippet: Nebraska-attributed reprint, No-Till Farmer]. Kentucky worked example: 116-day hybrid, about 1350 GDD to mid-pollination and 2820 GDD to black layer, so roughly 1470 GDD from pollination to R6 [snippet: UK CornGrowthStages]. Hybrid ratings are often emergence-to-maturity; add 100-150 GDD to get planting-to-maturity [snippet: Ohio State]. Silking to R6 is quoted as 66-70 days (Ohio State) vs 50-55 days (a seed-company rule of thumb, Rupp); they disagree, so do not use day counts. Use GDD, and treat hybrid variance as about +-150 GDD (unverified as a bound).
- *Maturity definitions.* Corn R6 = black layer, kernel moisture about 30-35% (reported range 25-40%); stress after black layer does not reduce harvested yield unless stalk or ear integrity is compromised [snippet: Ohio State / Purdue-based text / Farm Progress "black layer time arrives for 2024"]. Soybean R7 = one mature pod on 50% of plants (the soybean "black layer"); R8 = 95% of pods mature; seed moisture falls from about 60% at R7 to about 13% at R8 [snippet: UNL CropWatch "Understanding Ending Reproductive Stages in Soybean"].
- *Harvest = abrupt NDVI drop, often followed by bare soil/residue.* Used operationally by Planet's greening/harvest marker and EarthDaily's harvest detection (numeric parameters are not published on the pages I read) [read].
- *Residue vs bare soil vs senescent canopy.* The accepted spectral discriminator for non-photosynthetic vegetation is the lignin-cellulose absorption near 2100 nm (Cellulose Absorption Index, Daughtry et al.), absent in bare soil. Sentinel-2 has only two broad SWIR bands (B11 1610 nm, B12 2190 nm) and cannot resolve it; NBR2 = (B11-B12)/(B11+B12) is the S2 proxy, and it correlates poorly with residue under moist soil because it also responds to soil moisture [snippet]. Conclusion: **S2 can tell "green canopy" from "not green"; it cannot reliably tell "standing senescent crop" from "harvested stubble" from "bare soil" by spectra alone.** Use *temporal change and geometry* for that.

**State machine (proposed `[design]`; thresholds to calibrate)**

| State | Enter when (any of) | What it allows |
|---|---|---|
| vegetative / reproductive | green-up detected, GDD since planting < about 85% of hybrid-maturity GDD (if planting date known), NDVI not yet past the field's seasonal peak | anomaly-vs-baseline diagnosis allowed |
| senescing | NDVI has fallen >= 0.05 from the field's own seasonal peak AND date is past the calendar onset minus 14 d (existing logic, but *field-peak-relative*, not baseline-relative); OR GDD >= about 85-90% of maturity GDD | no new stress diagnoses; only "approaching maturity" status |
| harvest-in-progress | >= about 2 acres of coherent pixels whose NDVI dropped by >= about 0.25 between consecutive clear passes after senescence onset (`[design]`; Thurston corn fell 0.78 -> 0.35 in 17 days, i.e. 0.43, while normal peak-canopy pass-to-pass noise is +-0.04 per CALIBRATION.md), AND the dropped area is bounded by long straight edges or is the whole field | all stress alerts suppressed; optional "about 40% harvested" info |
| harvested | >= 90% of valid pixels below the field's harvest-level NDVI (suggest < 0.40 `[design]`; Thurston harvested corn 0.31-0.36) and no green-up | all alerts suppressed until next green-up; sticky |

Rules that matter for the observed failure:
- The *same* machine runs for scheduled alerts and for "ask" investigations (currently the ask path skips gates).
- A single clear pass after harvest is enough; do not require the median to fall below 0.35 (the reason the early-October field was flagged).
- Cloud gaps: if the last clear pass before senescence is > 10 days old, assume the state may have advanced and prefer silence; S2 revisit is about 5 days but cloud loss easily makes the effective gap 2-3 weeks.
- Partial-harvest geometry `[design]`: harvested/unharvested boundaries are straight, usually parallel to the row direction, with a ragged 1-2 pixel transition (combine headers are roughly 9-12 m). Identify by "abrupt drop + straight edge + contiguous block", not by stripe width.
- I found no published per-pixel partial-harvest method for corn or soybean [searched]. This rule set is a design, not a literature standard.

**Required evidence to name "harvested/mature (suppress)":** any one of the entering conditions. **To raise a stress alert in this phase:** none permitted (the one exception is a *pre-senescence* event such as hail followed by a sudden drop, assessed on the pre-senescence scenes).
**Ruling out stress explanations:** all of them after R6/R7, since post-black-layer stress does not change harvested yield. Residual real risk (lodging, stalk rot, ear drop) is not detectable (section 6).

### 3.2 Nitrogen deficiency (corn only; soybeans fix N)

**Accepted method.** In-season canopy sensing normalized to a well-fertilized (non-N-limited) reference: sufficiency index SI = reading(target) / reading(reference). Holland & Schepers (2010), "Derivation of a variable rate nitrogen application model for in-season fertilization of corn", Agronomy Journal 102:1415-1424 [cite-only; coefficients not read], is the UNL algorithm used in the Ag Leader OptRx system; UNL Project SENSE senses at about V8 with high-N reference strips [snippet: UNL CropWatch; Teten 2021 slides]. Iowa State guidance: build the reference by adding roughly 50-100% more N than normal for the rotation and divide readings by the reference average [snippet; the ISU PDF itself did not parse]. SI < 0.95 is the older rule from Varvel et al. (1997): recommend about 30 kg N/ha when SI < 0.95 at V8 [snippet, secondary source]. I found no current Extension document that makes 0.95 a universal cut.

**Hard cautions from the literature**
- Kitchen et al. (2010, Agronomy Journal, 16 Midwest fields, 2004-2007): sensor SI matched the economically optimal N rate in only about half the fields, and optimal N varied by more than 100 kg N/ha within 13 of 16 fields [snippet]. Even proximal, reference-normalized sensing is a coarse tool; Sentinel-2 at 10-20 m with a synthetic reference will be coarser.
- I found no peer-reviewed validation of an SI computed from Sentinel-2 against a well-fertilized strip in the Midwest [searched twice]. Closest evidence: red-edge chlorophyll index (CIre) tracked leaf/canopy N in irrigated Nebraska maize at 5 N rates, with Sentinel-2-simulated bands explaining about 87% of canopy N variance (Schlemmer et al., 2013) [snippet]; red-edge NDVI beat red NDVI for later-stage (V12) corn algorithms because red NDVI saturates (North Dakota, PMC4701256) [snippet]; SDSU Extension: NDRE detects N stress in V6-V12 [snippet]; Sentinel-2 maize NNI work (Boschetti et al., 2019) [snippet].
- NDVI saturates above roughly LAI 3 [snippet: Brazilian thesis]; CALIBRATION.md measured NDVI pixel sigma of only 0.009-0.012 at peak canopy. NDVI is useless for N from about V10 on; use red-edge.

**Stage window.** Valid reading about V6-V12 (up to VT with reduced confidence, `[design]`). Before V6 the soil background dominates; after VT, N remobilization and normal lower-leaf senescence make SI fall in healthy corn. Outside the window do not name N from imagery; allow only farmer-reported or tissue-test evidence.

**Reference approximation without a strip `[design]`.** Reference value = P90 (or P95) of NDRE/CIre over the clear interior pixels of the same field on the same date, after masking pixels already explained by other geometry (headlands, waterways, low spots). SI_pixel = value / reference. Start with SI < 0.95 (watch) and < 0.90 (strong); **unverified for satellite pixels and a percentile reference; calibrate on fields with known N history.** Failure modes of the percentile reference: (1) a uniformly N-short or uniformly stressed field has SI about 1 everywhere (false negative); (2) any non-N stress produces the same low SI (false positive); (3) planting-date and hybrid differences within a field look like N variation. A cohort reference (neighbor corn fields of the same planting window, via the field registry) is a better second reference than the field's own P90.

**Required evidence before naming N (all of):**
1. Corn, in the V6-V12 window (VT only with a flag).
2. Class A: a region (or whole field vs a reference cohort) with red-edge SI below threshold on at least 2 clear passes >= 5 days apart, not a single scene.
3. Class A/B: spatial pattern consistent with N loss or application: zones on sandy/low-OM soil, poorly drained or low-spot areas that saw saturation or heavy rain (denitrification, leaching), application-boundary straightness, or farmer-reported N rate/timing below recommendation. Weather alone (e.g. a wet 30 days) does not count.
4. Alternatives not supported: no event-timed sudden drop (hail), no ring/wedge on an irrigated field (pivot), no planter-skip stripes, no low-spot-only pattern with standing-water evidence (ponding).
Photo evidence (V-shaped yellowing from the leaf tip on older/lower leaves) is the cheapest confirmation (Iowa State, Sawyer) [snippet].

**Ruling out / look-alikes (never infer N from "pale" alone):** sulfur deficiency (new whorl leaves yellow, interveinal striping; most likely in cool, wet weather or on sandy low-OM soil) [snippet: ISU "Is it nitrogen or sulfur"]; waterlogged soil, cold temperatures, herbicide injury, potassium deficiency (older-leaf margins), anthracnose leaf blight [snippet]; drought; compaction (no source found); hybrid/planting-date differences; any soybean field (apply no N diagnosis; the current `crop_adjust` of -2.5 is reasonable). Hand-applying N to test strips is the ISU field test for N vs S [snippet].

### 3.3 Water stress / drought

**Accepted method.** Drought stress is diagnosed from weather and soil water balance first (Drought Monitor, soil moisture, ET demand, rainfall deficit), then confirmed by crop signs. Purdue (Nielsen): worst timing is about 2 weeks before to 2 weeks after silking; severe-stress yield loss up to about 3-4%/day pre-silk, up to about 8%/day at silking, up to about 6%/day in the two weeks after [snippet; older newsletters, "maximum under severe stress"]. Leaf rolling that starts at sunup and persists past dark is serious; midday rolling that recovers is not [snippet]. Visual signs are harder to read late in grain fill [snippet].

**Sentinel-2 signals.**
- NDMI (B8-B11) tracks canopy water content and is widely described as an early water-stress indicator because NIR/SWIR respond before greenness collapses and NDVI saturates [snippet: eos.com; Landsat NDWI tracked corn/soy vegetation water content better than NDVI, Jackson et al. 2004, RSE]. A local-scale drought study found growing-season max NDVI and NDMI the strongest indicators, 70-75% prediction rates [snippet].
- Rule `[design]`: NDMI falling relative to the field's own earlier pass (not relative to the prior-year baseline) while NDVI is still near peak, concentrated on terrain/soil units with low available water capacity (sandy, ridge tops, convex terrain; NRCS AWC layer already in the pipeline), in the stage window.
- **Window:** V8 to R5 corn; R1 to R5 soybean. Not valid after R6/R7.

**Required evidence to name drought:** (1) Class C context is allowed *as a prior modifier*: multi-week precipitation deficit or USDM D1+ at the county; (2) Class A: NDMI and/or NDVI decline with spatial structure following soil AWC/terrain, repeated on >= 2 passes; (3) for irrigated fields, no pivot geometry (rings/wedges) and no reported water-supply problem.
**Rules out:** event-timed sudden drop (hail); pattern following concave low spots after a rain event (ponding); ring/wedge on an irrigated field (pivot); any time after R6.
**Note:** "rain normal in 30 days" currently adds support to `pivot_failure`; that is context and should be a small prior modifier, not evidence.

### 3.4 Ponding / drown-out

**Accepted knowledge (duration tolerances).**
- Corn: germinating seed about 4 days of saturated/flooded soil; seedlings with < 6 leaves survive about 4 days under water if air temperature is below the high 70s F, about 1 day if above [read: ISU survival page; UNL CropWatch]. Growing point is below ground until about V6; V5 or smaller regrows in about a week with negligible yield loss, V6+ needs growing-point inspection (cream colored and firm = alive) [snippet: ISU 2026 storm-damage post; read: UNL]. Oxygen near zero after about 48 hours [read: UNL]. Waterlogging yield losses (Kaur et al., 2019, via UNL): V3, 7 days 16-38%; V6, 1-7 days 10-36%, 3-12 days 27-56% [read: UNL].
- Soybean: V2-V4, 2-3 days of waterlogging can cut yield 0-50%; nodulation damage after > 7-10 days; 7 days at V2 caused 29% loss (Linkemer et al., 1998), with V2, R1, R3, R5 most sensitive [read: UNL; snippet: ISU]. Per-day losses at V4/R1 from Scott et al. (1989) [snippet]. Survivors resume growth 3-5 days after water recedes [read: UNL].
- Follow-on N loss from denitrification/leaching is how ponding and N get confused; ISU IPM 42 links N loss to saturated/ponded warm soil [snippet].

**Sentinel-2 signals.** Optical imagery sees the *consequence* (stand loss, chlorosis, delayed growth) days to two weeks after the event, not the water itself, and standing water is usually cloud-covered during the storm. Open surface water shows as low NIR / high McFeeters NDWI on bare or sparse canopy, not under closed canopy. Sentinel-1 is the standard tool for surface water, but over vegetated fields the signal reverses (double-bounce raises backscatter, up to about 7 dB in C-band) and crop growth complicates it [snippet: Remote Sensing 14(15):3838]; no published corn/soy SAR ponding method was found.
- Rule `[design]`: region with persistent NDVI/NDMI deficit **overlapping a topographic low-spot/flow-accumulation mask and poorly drained soil**, beginning within about 3-14 days after a documented heavy-rain event (radar/precip; threshold to calibrate, e.g. >= 2 in in 24-48 h is a common trigger but I did not find it stated as a rule), at stages up to about R2 (stand/yield effects are strongest early).
- **Window:** emergence to about R2; later ponding is possible but lower impact.

**Required evidence to name ponding:** (1) a rain/saturation event within the lookback (Class C, but here a *necessary gate*, not a weight); (2) Class A+B: region geometry follows the low-spot mask (overlap >= about 0.6 `[design]`); (3) onset coincides with or follows the event (compare pre- and post-event scenes); not present before.
**Rules out:** region not in a low spot; no event; ring/wedge (pivot); straight stripes (planter); pattern already present before the event.

### 3.5 Hail

**Accepted methods.**
- *Assessment standards.* The formal standard is the USDA RMA/FCIC Corn Loss Adjustment Standards Handbook (FCIC-25080) with hail stand-reduction and defoliation charts by leaf stage (a chart for the 11th-17th leaf stages was added in the 2014 edition) [snippet; I could not read the exhibit values: **look them up in the current handbook before coding any loss number**]. Extension distillations [read: UNL CropWatch 2018, mid-season hail]:
  - Wait 7-10 days after the storm before assessing.
  - Corn: defoliation estimates start at V7 using the horizontal leaf method (which counts 1-2 leaves higher than the collar method, e.g. V10 = 12.5 leaves). Examples: at 13th leaf, 50% defoliation is about 10% loss and 100% about 34%; 100% leaf destruction at VT can mean complete loss. Hail-stripped V10-11 corn may look better in 10-14 days.
  - ISU: leaf loss rarely affects yield before V6; under 50% leaf area loss before V13 does not reduce yield [snippet: ISU "Hail Injury on Corn"].
  - Soybean (before R1): loss from percent of nodes cut/broken above the cotyledonary node, e.g. at V5: 25% cut -> 9.7%, 50% -> 15.6%, 75% -> 26.1%, 100% -> 80.9%; stand loss from 120k to 60k plants/ac cost about 14% (VC-R1) and about 35% (R2-R3.5); from R1 onward combine node loss with defoliation (EC128 worksheet); ISU: R4 up to about 60% and R5 up to about 75% loss [read: UNL; snippet: ISU "Late season hail damage"].
- *Optical remote sensing.* Hail damage appears as a sudden NDVI decrease in the swath, and a time-profile metric beats a single before/after difference: Ha et al. (2022, Remote Sensing 14(4):951) used the 32-day area under the curve (AUC) of NDVI, NDWI and PSRI on 54 Canadian Prairie fields (canola r = -0.90, RMSE 8.2; wheat r = -0.86, RMSE 12.3; lentil r = 0.80, RMSE 17.4) [snippet]. Angearu et al. (2022, Natural Hazards) used an S2 NDVI difference > 0.05 for the affected mask, radar reflectivity > 55 dBZ for medium-large hail, with NDVI drops of 0.4-0.5 where damage was heaviest [snippet]. Ursu et al. (2025, Remote Sensing) combined radar hail kinetic energy with S2 NDVI differencing [snippet]. Schillaci et al. (2022, Italian J. Agronomy) compared S2 with insurance assessments for hail in maize: best index 73.3% accuracy, NDVI worst of six at 65.3% (125 field surveys) [snippet]. I found no corn-specific recovery curve; Furlanetto et al. (2023, Precision Agriculture) showed S2 and UAV can estimate LAI under simulated maize defoliation [snippet].

**Rule `[design]`.** Name hail only if an event exists (MRMS MESH or SPC/ground report overlapping the field) **before** the first scene showing the drop; a clear pre-event scene shows no such deficit; the NDVI/NDRE drop is abrupt (>= about 0.10-0.15 over one revisit; Angearu's 0.05 is a detection floor for mapping, not a diagnosis bound; **calibrate**); the drop is spatially coincident with the swath (hail_swath_overlap high); and it then persists or recovers on a plausible curve. Assess damage 7-10 days after the event, not on the first post-storm pass.
**Window:** V6 (corn) / emergence (soy) to about R5. Drops after senescence onset are not hail.
**Required evidence:** event overlap (Class A, geospatial, not regional context) + pre/post change aligned in time. **Rules out:** no MESH/report in the field footprint; the drop begins before the event date; gradual decline; uniform whole-field drop without swath structure (that is weather/phenology/late planting); event outside the stage window.
**Note:** hail swaths are hundreds of meters to km wide and damage is patchy inside a field; the field may be only partly covered.

### 3.6 Pivot / irrigation failure

**Method.** Geometry is the signature: ring (a band at constant radius, e.g. plugged or wrong nozzle package, leaking span), wedge (a sector along a radius, e.g. a stopped/misaligned tower or a segment not applying water), plus non-irrigated corners of a square field [snippet: vendor examples of circular NDVI patterns from plugged nozzles; the pivot-*detection* literature (Hough transform + RF on Landsat 8; YOLO on S2/NAIP) finds pivots, it does not diagnose failure]. I found **no peer-reviewed or Extension validation of satellite pivot-failure diagnosis**, so everything here is a design hypothesis.
**Rule `[design]`:** irrigated field with known pivot center/radius; a region with high ring_score or wedge_score (current `patterns.py` features) **that is new this season** (the same location did not show at the same stage in prior years; otherwise it is a soil or design feature such as corners, a sandy arc, or a wheel track), persisting >= 2 passes, in V8-R5 for corn / R1-R5 for soy (highest water demand).
**Required evidence:** pivot geometry on an irrigated field + persistent geometric match + not present in prior years; farmer report or pivot telemetry is strong confirmation. **Rules out:** non-irrigated field (the catalog already uses -6.0); geometry following soil units rather than the pivot; the same pattern in previous years; rain-fed corners as the only anomaly.
**Context:** regional rain (deficit or normal) is a minor modifier of *visibility*, not evidence for the fault.

### 3.7 Planter skip / stand problems

**Method.** ISU: skips (no seed at a meter position) are the costliest singulation error; a seed-spacing SD of about 2 in is acceptable for a well-maintained planter; conventional meters skip heavily above 7.5 mph, with skips plus bounce often > 10% at 10 mph [snippet: ISU "High Speed Planting Technology"]. These are *within-row, plant-scale* errors: **S2 at 10 m cannot see them** (corn rows are 30 in = 0.76 m). What S2 can plausibly see: a missed planter pass, a stopped row unit across a long run (a 12-24-row planter is about 9-18 m wide, i.e. 1-2 pixels, so barely), a seed-rate or hybrid boundary, a planting-date boundary, and headland/end-row effects.
**Rule `[design]`:** straight stripes parallel to the field's row direction, widths near multiples of the planter width, present from emergence on the first clear scenes, in V2 to about V10 (before canopy closure and compensation hide stand differences; the catalog already has negative stage adjustments for flowering and later). Persist over >= 2 passes.
**Required evidence:** stripe geometry (current `stripe_score`) + stage <= about V10 + persistence + width within planter-width tolerance (registry/farmer); a photo of gaps in the row is the confirming evidence. **Rules out:** stripes appearing only after canopy closure with no early signal; any non-straight shape; patterns following soil/terrain; edge-hugging bands (headland compaction / edge effect). Do not use for soy beyond about V5: soybeans compensate heavily (UNL: gaps under 2 ft fill in) [read].

### 3.8 Herbicide drift

**Method.** No validated satellite method. Recent Illinois work detects dicamba-injured soybean from **drones** at very low rates (1/10,000 of label rate) 8 days after application; satellite analysis is stated as future work, with Sentinel-2/Landsat suggested by the authors as plausible [snippet: AgriNews / FertilizerDaily 2025; Niu et al. 2023 used a hyperspectral leaf scanner]. Symptoms (leaf cupping, strapping) are not S2-visible.
**What S2 can contribute:** a *gradient* anomaly decreasing with distance from one field edge, on the downwind side, appearing 1-3 weeks after a spray window, in soybean (dicamba/2,4-D sensitive). That geometry is also made by tree-line shade, headland compaction, field-edge effects, spray overlap and waterways. So S2 may say "edge-gradient anomaly: scout for drift (and for edge effects)", never "drift".
**Required evidence to name:** farmer report of a neighbor's application or a spray record + wind direction in the spray window + Class A edge gradient on the downwind side + photo of cupping/strapping. **Rules out:** gradient on the upwind side; the same gradient in prior years at the same place (structural); no neighbor spray activity.

### 3.9 Disease and insect pests

**Honest capability.** Most foliar diseases are **not** diagnosable from 10-20 m Sentinel-2. The only evidence I found for Sentinel-2 band sets is a maize gray leaf spot study using *proximally sensed* spectra resampled to S2 bands (overall accuracy 0.95 for healthy/intermediate/severe) [snippet: ISPRS Archives XLII-4/W18, 2019]; that does not test real S2 pixels, mixed pixels or confounding stress. I found nothing on tar spot, southern rust, SDS, soybean aphid, rootworm or cutworm at S2 scale. Treat S2 disease detection as **unverified/absent**.
**What works: risk models plus scouting.** Tar spot: Tarspotter and the Crop Disease Forecasting tool use weather and crop stage; susceptibility window about V10-R3; favoring conditions of about 30 days of mild temperature (64-73 F / 18-23 C) with intermittent wetness; the most accurate model reported 90.1% accuracy; a Wisconsin 75% action threshold (2020 model); the tools do not tell you the pathogen is present, scouting is still needed [snippet: CPN, Wisconsin IPCM, Purdue, UMN].
**Framing:** output "scout for tar spot / gray leaf spot / SDS" with the risk-model score, county confirmation (ipmPIPE / EDDMapS) and stage. Never "diagnosis". Disease can be *named* only with a photo (symptom checklist) or lab/scout confirmation; remote evidence only raises scouting priority.
**Rules out:** outside the susceptibility window; weather unfavorable for the pathogen; photo with no lesions; no county reports (weak only).
**Stage notes:** the disease prior should be low before V10 and may rise R1-R4; late-season senescence from disease and from normal maturity look the same in S2.

### 3.10 Unknown / cause not determined

Not a competing condition. It is the output when the naming gates in 5.1 fail. It always carries: (a) what was observed (area, location in words, persistence, change), (b) which conditions were ruled out and why, (c) the 2-3 cheapest discriminating checks (a photo of the V-shaped yellowing, ask about N rate, ask about sprayed neighbors, check the pivot).

---

## 4. Phenology inputs the system needs

| Input | Source | Why |
|---|---|---|
| Planting date | farmer/field registry; else inferred from NDVI green-up (WISE-type) minus about 100-125 GDD | GDD stage, emergence, maturity |
| Crop type per year | NASS CDL / CSB (already used) | same-crop baseline only |
| Stage estimate | GDD (86/50) since planting from daily Tmax/Tmin + NDVI-curve confirmation | stage-validity masks |
| Hybrid maturity (RM or GDD) | farmer; default regional midpoint | maturity window width (about +-150 GDD) |
| Field-peak NDVI | max of smoothed NDVI this season | senescence detection relative to the field itself |

Stage breakpoints used by the masks: corn V6 (growing point leaves the soil; flood and hail tolerance change), V7 (hail defoliation assessment begins), V13 (defoliation tolerance changes), VT/R1 (drought and hail most damaging), R6 (black layer). Soybean R1 (hail method changes), R5 (seed fill), R7 (mature). Horizontal-leaf and collar-leaf counts differ by 1-2 leaves [read: UNL].

---

## 5. Scorer redesign recommendations

### 5.1 Abstention rule
Name condition X only if **all** hold:
1. **Stage gate:** crop state is in the valid window for X (section 3).
2. **Necessity gate:** at least one Class A item flagged `necessary` for X is present (hail: event overlap; ponding: low-spot overlap plus a prior event; pivot: ring/wedge geometry on an irrigated field; planter: stripe geometry; N: red-edge SI region in window plus pattern; drought: NDMI/NDVI structured by AWC; disease: photo/confirmation).
3. **No ruling-out:** no item from X's `rules_out` list is present.
4. **Posterior test:** p(X) >= T_name and p(X) - p(runner-up) >= delta. Starting values `[design]`: T_name = 0.65, delta = 0.20; set them from the risk-coverage curve (El-Yaniv & Wiener 2010; Geifman & El-Yaniv 2017: choose the threshold that achieves a target precision, e.g. 0.8, and report coverage) once labels exist.
5. **Data sufficiency:** >= 2 clear passes supporting the pattern, except events (hail) where one pre- and one post-event pass suffices.

Otherwise output `cause not determined`. A "possible: A, B" list may be shown only as scouting priorities, not diagnoses. "Unknown" is the *absence of a passing gate*, not a hypothesis with its own prior. Because conditions co-occur, allow two named causes when both pass their gates independently.

### 5.2 Evidence classes and caps
| Class | Definition | Examples | Cap on total log-odds |
|---|---|---|---|
| A field-specific | derived from this field's pixels/geometry/event overlap, or an observation of this field | red-edge SI region, ring/wedge/stripe/edge scores, low-spot overlap, hail swath overlap, NDMI trend, photo checklist, farmer records | each item keeps its own weight; clamp the class at +-4 |
| B local static context | property of the place, not the season | soil texture/AWC/drainage class, TWI, floodplain, irrigation status, prior crop, corn-on-corn | +-1.5 total (about 4.5x odds) `[design]` |
| C regional temporal context | county/regional weather, USDM, disease reports | 30-day rain vs normal, USDM D1+, heat days, county tar-spot report | +-0.7 total (about 2x odds) `[design]`, and never the only support |

Mechanically, Class C enters as a prior modifier within the cap and is never counted as an observation. A condition supported only by classes B and C is reported as "context only" and cannot pass the necessity gate. This is a robustness choice, not a literature standard.

Current-catalog demonstration for the founder: nitrogen, corn, vegetative: logit(0.12) = -1.99; + corn 0.5 + vegetative 0.3 = -1.19 (p 0.23). Add `rain_excess_30d` (+1.2) -> 0.01 (p 0.50). Add `prior_crop_soybeans` (+0.8) -> 0.81 (p 0.69 > 0.6 = "likely"). Both items are class B/C; no field evidence is involved. Cluster fix: group `rain_excess_30d`, `heavy_rain_7d`, `soil_poorly_drained`, `floodplain_overlap_high`, `lowspot_overlap_high` as one correlated cluster (take the max). And `unknown`: logit(0.10) = -2.20; `no_pattern_found` +1.0 gives p 0.23; it cannot win, which is why the system never abstains.

### 5.3 Priors from published base rates
- RMA **Cause of Loss historical data** and Summary of Business files (state/county, commodity, month of loss) give the relative frequency and indemnity of insured perils: drought, hail, excess moisture/precipitation, heat, wind, frost, disease, insects and so on. I did **not** retrieve peril shares. I only confirmed the files exist and that the Nebraska 2025 profile reports about 10.2M insured corn acres and 4.6M soybean acres, with a 2012 loss ratio of 2.32 [snippet: RMA Nebraska State Profile 2025]. Action: aggregate claims (not dollars) by county/state x crop x month of loss to set *natural-peril* priors, then renormalize to the conditions FieldWatch models.
- Limits of RMA as a prior: it counts insured, claimed, mostly catastrophic losses; N deficiency, planter skips, pivot failure and drift are mostly **not** insurable causes and are undercounted; and it describes losses, not "flagged anomaly regions" (selection effect). It can rank natural perils (drought vs hail vs excess moisture) by region and month but cannot supply priors for management causes.
- Interim approach: set all management-cause priors to a flat low value (e.g. 0.05, `[design]`), let the necessity gates do the work, and replace priors with empirical frequencies of **labeled FieldWatch alerts** once you have roughly 100-200 per crop. The hand-set N prior (0.12 + 0.5 + 0.3) has no source.
- For stage-dependent priors use the validity windows (section 3) as hard zeros, not as small logit adjustments.

### 5.4 Stability (failure C)
- Compute the full evidence vector in code: every key for every condition is evaluated each run, with values `present / absent / not_evaluable`. Keys the data cannot speak to stay `not_evaluable` and contribute 0.
- LLMs only (a) read photos into a closed-schema checklist (e.g. symptom location on the leaf, lesion type, standing water yes/no/unknown) and (b) turn farmer text into fixed fields; temperature 0; the scorer rejects free-form `weight_key`s.
- Idempotence test in CI: same inputs -> same output hash on about 20 fixtures.
- `scoring._score` is already order-independent (it sorts evidence by id); the instability is entirely upstream.

### 5.5 Calibration plan (probabilities)
1. **Label every alert**: farmer/agronomist feedback with cause options matching the catalog + "no problem" + "don't know"; plus, when possible, yield-monitor maps, scouting notes, insurance claims.
2. **Metrics:** Brier score with Murphy (1973) decomposition (reliability - resolution + uncertainty; Siegert 2016 gives a simpler derivation) and a reliability diagram with bin counts shown (Guo et al. 2017 caution that diagrams hide bin occupancy; ECE depends on binning) [snippet].
3. **Sample sizes (my arithmetic, not from the literature):** a reliability diagram with 5 bins needs roughly 30+ outcomes per bin, i.e. about 150-300 labeled alerts; 10 bins about 300-500. Per-condition precision with a 95% binomial CI half-width of +-0.15 needs about 43 alerts of that condition (n = 1.96^2 x 0.25 / d^2), +-0.10 needs about 96. Rare conditions (hail, drift, pivot) will not reach this in a season, so calibrate by evidence class and by gate rather than per condition.
4. **Recalibrate** with Platt or temperature scaling at n >= about 200; use isotonic regression only at n >= about 1000 (common ML guidance, unverified here).
5. Until calibrated, show "likely / possible / not determined" with the evidence list, not percentages.

### 5.6 Validation plan (detection and diagnosis separately)
- **Task 1, anomaly detection** (is there a real in-field problem?). Truth: yield-monitor maps (best), scouting notes, farmer confirmation. Metrics: precision and recall per field-season, **false alerts per field per season** (the farmer-trust metric), and time to detect.
- **Task 2, cause attribution.** Truth: scouting/photos/records/insurance claim cause. Metrics: confusion matrix, precision per named cause, **abstention (coverage) rate**, risk-coverage curve. Report both "when it names a cause, how often it is right" and "how often it names one".
- **Regression fixtures (cheap, immediate):** archive the failing cases as tests: the early-October harvested field; a half-harvested field; a field harvested earlier than prior years; a soybean field harvested in late September; a hail-stripped field; a ponded low spot; normal late-season senescence. The `CALIBRATION.md` Thurston fields are already hand-labelled.
- **Sample sizes in the published S2 damage literature are small:** Ha et al. 54 fields (3 crops); Schillaci et al. 125 insurance surveys; a Finnish parcel-scale loss study (Hiremath et al., 2021) and ESA EO-INSURE target claims verification [snippet]. None gave a minimum. Do not claim accuracy from fewer than about 50-100 labeled alerts, and always report confidence intervals.
- Truth caveats: insurance loss assessments vary with adjuster experience (Schillaci et al. cite surveyor inaccuracy) [snippet]; yield-monitor data needs cleaning; farmer labels skew toward obvious causes.

---

## 6. What cannot be done with Sentinel-2 (and what is needed instead)

| Cannot (reliably) | Why | Needed instead |
|---|---|---|
| Within-row skips / doubles, stand count | 10 m pixel vs 0.76 m rows | drone/phone photo, scouting stand counts |
| Foliar disease diagnosis (tar spot, GLS, southern rust, SDS) and most insects | lesion scale far below pixel size; symptoms confounded with other stress; only proximal-sensor simulations exist | weather risk models (Tarspotter, ipmPIPE), photos, scouting |
| Herbicide drift identification | no validated satellite method; symptoms sub-pixel | drone multispectral (research stage), photos, spray records |
| Standing senescent crop vs stubble vs bare soil by spectra | only two broad SWIR bands; NBR2 moisture-sensitive | temporal change + geometry; Sentinel-1 (research stage); farmer harvest report |
| Standing water, ponding itself | cloud during storms; optical blind under canopy; SAR over crop ambiguous | Sentinel-1 (open water, early season), radar rain + DEM, farmer photo |
| Soil N status; N vs S vs other chlorosis | spectral confounding | leaf tissue test, strip tests, photos (V-shape vs interveinal), application records |
| Crop water stress at daily scale | 5-day revisit + clouds | Landsat thermal (LST, 16-day, 100 m) or ECOSTRESS canopy temperature; soil-moisture products; ET models |
| Lodging, stalk rot, ear drop after R6 | needs structure/texture | farmer/scouting, drone |
| Hail damage percent as an adjuster would measure | NDVI response vs defoliation/stand is crop- and stage-dependent; best S2 index 65-73% accuracy vs adjuster surveys in one study | field assessment per FCIC handbook; use S2 for *where to look* |
| Exact yield loss | not observable | yield monitor; loss tables labeled approximate |

Also: with clouds, the nominal 5-day revisit becomes a 2-3 week effective gap in wet seasons. Always compare to the *last clear pass*, state its date, and prefer silence when the data is stale.

---

## 7. Decision points for an agronomist or the founder

1. **Terminal-phase alert policy:** is it acceptable to send *no* stress alerts once a field enters `senescing`, even if the cause is real (e.g. hail on Aug 20 that shows on Sep 5)? Recommendation: allow only event-timed hail whose drop appears on pre-senescence scenes; everything else silent.
2. **Thresholds to ratify with local data:** harvest drop (about 0.25), harvested NDVI (< about 0.40), SI watch/strong (0.95 / 0.90), hail drop (0.10-0.15), ponding rainfall trigger, Class B/C caps (+-1.5 / +-0.7), T_name 0.65. All `[design]`; none is literature-fixed.
3. **N reference:** will you require farmer-supplied N rate/timing and ideally a marked reference strip, or accept the within-field P90 reference and label results "possible N variation, confirm"? An agronomist should sign off the V6-V12 window and the "does not indicate N" list.
4. **Planting date capture:** make it a required onboarding item? It drives GDD stage, which drives every validity window; inferring from green-up is good to about +-1 week.
5. **Hail loss numbers:** pull the FCIC Corn and Soybean Loss Adjustment Standards Handbook hail/defoliation exhibits (current edition) and replace the DRAFT `loss_tables.json` hail rows; have an agronomist confirm the horizontal-vs-collar leaf conversion.
6. **Disease framing:** confirm that disease/pest never appears as a named cause without photo or scout confirmation.
7. **Priors:** commission the RMA cause-of-loss aggregation (county x crop x month) and decide whether management-cause priors stay flat until real labels exist.
8. **Labeling workflow:** who labels (farmer one-tap feedback vs agronomist review), and budget for a yield-monitor ground-truth set (a few cooperating fields in 2027).

---

## 8. Sources

Labels: [read] opened and read; [snippet] search-result summary only; [cite-only] citation confirmed, content not seen. URLs were taken from search results; I did not verify that every link resolves.

**Phenology / harvest**
- Gao et al. (2021), within-season emergence (WISE) with HLS over the Corn Belt, Remote Sensing. [snippet] https://doaj.org/article/4fb2a38988dd42dd887258e4501a2e61 (matched by topic only; unverified that this is the exact paper)
- Shen et al. (2023), near-real-time crop progress via HLS/Sentinel-2 + geostationary fusion, Remote Sensing of Environment. [snippet] (no direct URL found)
- Phenology extraction with Derivative Dynamic Time Warping on Sentinel-2 NDVI, Remote Sensing 2023. [snippet] https://doaj.org/article/720ee76641bb43ee9c4920682179e907
- Field-scale sowing/harvest dates for corn and soybean with HLS (NDVI, EVI, WDRVI, NDWI), Remote Sensing 17(17):2927, 2025. [snippet] https://www.mdpi.com/2072-4292/17/17/2927
- JRC, Sentinel-2 harvest-date maps validated against machinery RTK records. [snippet] https://publications.jrc.ec.europa.eu/repository/handle/JRC143325
- Diao lab, CropPlantHarvest dataset / Normalized Harvest Phenology Index, ESSD 18, 4373, 2026. [snippet] https://essd.copernicus.org/articles/18/4373/2026/
- USDA NASS, Weekly Crop Progress Using VIIRS (2026). [snippet] https://www.nass.usda.gov/Research_and_Science/Cropland/docs/2026_Weekly_Crop_Progress_Using_VIIRS.pdf
- Planet, Greening/Harvest marker. [read] https://docs.planet.com/data/area-monitoring/markers/greening-harvest-marker/
- EarthDaily, Harvest Detection. [read] https://docs.earthdaily.com/agro/library/Harvest_Detection/
- Ohio State Extension AGF-101, corn growth stages / GDD. [snippet] https://ohioline.osu.edu/factsheet/agf-101
- University of Kentucky, Corn Growth Stages (2011). [snippet] https://graincrops.ca.uky.edu/files/corn/CornGrowthStages_2011.pdf
- UNL (via No-Till Farmer), How temperature affects corn emergence. [snippet] https://www.no-tillfarmer.com/articles/4718-how-temperature-affects-corn-emergence
- Farm Progress, Black layer time arrives for 2024 (R6 moisture). [snippet] https://www.farmprogress.com/corn/black-layer-time-arrives-for-2024
- UNL CropWatch, Understanding Ending Reproductive Stages in Soybean (2021). [snippet] https://cropwatch.unl.edu/2021/understanding-ending-reproductive-stages-soybean
- Residue/NPV indices: Daughtry et al. (CAI); Dennison et al. (2023) RSE, CINDI. [snippet] https://content.csbs.utah.edu/~pdennison/reprints/denn/2023_Dennison_etal_RSE.pdf ; https://www.usgs.gov/publications/modeling-global-indices-estimating-non-photosynthetic-vegetation-cover

**Nitrogen**
- Holland, K.H. and Schepers, J.S. (2010), Derivation of a variable rate nitrogen application model for in-season fertilization of corn, Agronomy Journal 102:1415-1424. [cite-only] listed at https://agronomy.unl.edu/schepers
- Kitchen, N.R. et al. (2010), Ground-based canopy reflectance sensing for variable-rate nitrogen corn fertilization, Agronomy Journal. [snippet] https://agris.fao.org/search/en/records/65de5a5e0f3e94b9e5ce2017
- Teten, S. (2021), Sensor-based N management, UNL North Central Crop Management Conf. [snippet] https://agronomy.unl.edu/workshops-fielddays/NCMC/2021/NutrientMgmt-Water/Teten_SensorNManagement_NCMC2021.pdf
- UNL CropWatch (2017), Introducing Project SENSE. [snippet] https://cropwatch.unl.edu/2017/introducing-project-sense
- Iowa State (Sawyer), In-season nitrogen stress / reference areas. [cite-only; PDF did not parse] https://www.agronext.iastate.edu/soilfertility/info/inseasonnstress.pdf
- Varvel et al. (1997) SI < 0.95 rule, via a Kentucky-hosted proposal. [snippet, secondary] https://www.nue.okstate.edu/Nitrogen_Conference2004/FFF_Proposal_2004.doc
- SDSU Extension P-00342, red-edge NDRE for N stress V6-V12. [snippet] https://extension.sdstate.edu/sites/default/files/2025-06/P-00342.pdf
- Schlemmer et al. (2013), Remote estimation of nitrogen and chlorophyll contents in maize at leaf and canopy levels, Int. J. Applied Earth Obs. Geoinf. [snippet] https://digitalcommons.unl.edu/agronomyfacpub/771
- Active-optical sensors, red NDVI vs red-edge NDVI, North Dakota corn. [snippet] https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4701256/
- Iowa State, Is it nitrogen or sulfur deficiency. [snippet] https://crops.extension.iastate.edu/post/it-nitrogen-or-sulfur-deficiency-symptoms

**Water stress / ponding**
- Purdue (Nielsen), Corn response to drought. [snippet] https://agry.purdue.edu/ext/corn/news/timeless/CornRespDrought.html ; https://agry.purdue.edu/ext/corn/news/articles.02/WTMDS-0717.html
- Jackson et al. (2004), Vegetation water content mapping using Landsat NDWI, Remote Sensing of Environment. [snippet] https://www.ars.usda.gov/ARSUserFiles/12298/pdf/jackson_rse04.pdf
- Local-scale agricultural drought monitoring with multi-sensor time series (NDVI/NDMI/SAR). [snippet] https://agris.fao.org/search/en/records/65dfe45963b8185d9cafd0be
- EOS, NDMI definition. [snippet] https://eos.com/make-an-analysis/ndmi/
- Iowa State (Elmore & Abendroth), Corn survival of flooding/ponding. [read] https://crops.extension.iastate.edu/corn/production/management/early/survival.html
- UNL CropWatch, Corn and soybean survival in saturated and flooded soils. [read] https://cropwatch.unl.edu/corn-and-soybean-survival-saturated-and-flooded-soils
- Iowa State, Ponding impacts on soybean growth and development. [snippet] https://crops.extension.iastate.edu/encyclopedia/ponding-impacts-soybean-growth-development
- Iowa State, Flooding impacts on corn growth and development. [snippet] https://crops.extension.iastate.edu/encyclopedia/flooding-impacts-corn-growth-development
- Iowa State, Storm damage, June 2026. [snippet] https://crops.extension.iastate.edu/post/storm-damage-june-2026
- SAR flood mapping over vegetation, Remote Sensing 14(15):3838. [snippet] https://www.mdpi.com/2072-4292/14/15/3838

**Hail**
- UNL CropWatch (2018), Mid-season hail damage assessments in corn and soybeans. [read] https://cropwatch.unl.edu/2018/mid-season-hail-damage-assessments-corn-and-soybeans
- UNL EC126 (hail). [snippet] https://extensionpubs.unl.edu/publication/ec126/na/pdf/view
- Iowa State, Hail injury on corn (2009); Late season hail damage to crops. [snippet] https://crops.extension.iastate.edu/cropnews/2009/06/hail-injury-corn ; https://crops.extension.iastate.edu/post/late-season-hail-damage-crops
- USDA RMA/FCIC, Corn Loss Adjustment Standards Handbook (FCIC-25080). [cite-only] https://rma.usda.gov/sites/default/files/handbooks/2020-25080-1H-Corn-Loss-Adjustment-Handbook.pdf
- Ha et al. (2022), Quantifying hail damage in crops using Sentinel-2 imagery, Remote Sensing 14(4):951. [snippet] https://www.mdpi.com/2072-4292/14/4/951
- Angearu et al. (2022), Natural Hazards 114. [snippet] https://ideas.repec.org/a/spr/nathaz/v114y2022i2d10.1007_s11069-022-05457-x.html
- Ursu et al. (2025), radar hail kinetic energy + S2 NDVI, Remote Sensing. [snippet] https://www.mdpi.com/2072-4292/17/23/3850 (listing; unverified match)
- Schillaci et al. (2022), Italian Journal of Agronomy, hail in maize vs insurance. [snippet] https://doaj.org/article/e9a9af5ae13745c6aab81f3840d49e42
- Furlanetto et al. (2023), simulated hail defoliation in maize, Precision Agriculture. [cite-only]

**Pivot / planter / drift / disease**
- Pivot NDVI patterns (vendor): Bushel, 5 issues detected with satellite and aerial imagery. [snippet] https://blog.bushelfarm.com/5-issues-detected-satellite-aerial-imagery
- Pivot detection (Hough + RF, YOLO), Remote Sensing 17(13):2276. [snippet] https://www.mdpi.com/2072-4292/17/13/2276/htm
- Iowa State, High-speed planting technology (skips, spacing SD). [snippet] https://crops.extension.iastate.edu/cropnews/2020/03/high-speed-planting-technology
- FertilizerDaily (2025), drone and satellite tool detects dicamba damage in soybeans. [snippet] https://www.fertilizerdaily.com/20250728-new-drone-and-satellite-based-tool-can-detect-dicamba-damage-in-soybeans-from-the-sky/
- Niu et al. (2023), early detection of dicamba and 2,4-D drift on soybean, hyperspectral, Remote Sensing. [cite-only]
- Crop Protection Network, Tar spot prediction in corn: the weather matters. [snippet] https://cropprotectionnetwork.org/publications/tar-spot-prediction-in-corn-the-weather-matters
- Wisconsin IPCM, tar spot update (75% action threshold, 2020 model). [snippet] https://ipcm.wisc.edu/blog/2020/07/wisconsin-corn-tar-spot-update
- Maize gray leaf spot with simulated S2 bands, ISPRS Archives XLII-4/W18 (2019). [snippet] https://isprs-archives.copernicus.org/articles/XLII-4-W18/1015/2019/

**Scorer, calibration, validation**
- Guo et al. (2017), On calibration of modern neural networks, ICML. [snippet] https://proceedings.mlr.press/v70/guo17a.html
- Murphy, A.H. (1973), A new vector partition of the probability score, J. Applied Meteorology. [cite-only]; Siegert (2016), Simplifying and generalising Murphy's Brier score decomposition. [snippet] https://ore.exeter.ac.uk/repository/handle/10871/34847
- El-Yaniv and Wiener (2010), On the foundations of noise-free selective classification, JMLR 11. [snippet] https://www.jmlr.org/papers/v11/el-yaniv10a.html ; Geifman and El-Yaniv, Selective classification for deep neural networks. [snippet] https://arxiv.org/pdf/1705.08500v1
- USDA RMA, Nebraska State Profile 2025; RMA Cause of Loss historical data and Summary of Business files (exact download URL not verified). [snippet] https://www.rma.usda.gov/sites/default/files/2026-03/Nebraska-State-Profile-2025.pdf
- Hiremath et al. (2021), Crop loss identification at field parcel scale using satellite remote sensing and ML (PLOS ONE); ESA EO-INSURE project. [snippet] https://acris.aalto.fi/ws/portalfiles/portal/78026603/Crop_loss_identification_at_field_parcel_scale_using_satellite_remote_sensing_and_machine_learning.pdf ; https://business.esa.int/projects/eo-insure
