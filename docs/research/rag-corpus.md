# FieldWatch agronomy RAG: candidate corpus and license review

Status: research draft for founder approval. Nothing has been downloaded into the product, nothing built, no AWS calls.
Research date: 2026-10-16. Every URL below was fetched on that date (HTTP 200, title confirmed) unless marked **unverified**. This is a research summary, not legal advice; the "commercial verdict" is deliberately conservative.

## 0. Bottom line

1. Only a small slice of the useful Extension material is clearly safe for a commercial product today: **USDA RMA (public domain)** and **University of Minnesota Crop News blog posts (CC BY-SA 4.0, stated per post)**. That is 14 documents: strong on crop insurance, hail appraisal and flood survival; thin elsewhere.
2. The best Nebraska material (**UNL CropWatch**, ~33 docs here) carries **no reuse statement at all**, so default copyright (Board of Regents) applies. Treat it as **needs permission**. Same for **Crop Protection Network** (disease work), **Purdue**, **K-State** and the **ISU Encyclopedia**.
3. **Iowa State ICM News** has a real republish rule ("may be republished without further permission if published as written" with credit), but it covers only ICM News articles, and chunking/summarizing may not count as "as written". Ask.
4. Recommendation: send permission requests to UNL IANR, CPN and ISU, and build Tier 1 now. Tier 2 stays staged (not indexed in anything farmer-facing) until permission is in writing.

Candidate list size (section 1): **92 documents**. Tier 1 (clear now or clear with attribution): **14**. Tier 2 (permission needed before indexing in a commercial product): **70**. Avoid (explicit all-rights-reserved, listed for completeness): **8**. A "~40-80" corpus is reachable only if permission is granted; Tier 1 alone is thin and has no disease, pivot, planter or herbicide coverage.

## 1. Proposed initial corpus

Status key: **OK** = public domain; **OK-attr** = open license, attribution required (CC BY-SA also has ShareAlike, see section 2); **ASK** = needs permission (no license stated, or terms insufficient for commercial use); **AVOID** = explicit all-rights-reserved, do not use without a license. Dates are as shown on the page (published, or "REVISED"/"updated"); "page meta" means a machine-readable date on the page that may be a last-modified date, not the original publication date. Condition ids are in section 3.

### 1.1 Hail damage and replant decisions (condition: hail)

| # | Title | Source | URL | Date | Status | Why it matters |
|---|---|---|---|---|---|---|
| H1 | Assessing Early-Season Hail Damage to Corn and Soybean | UNL CropWatch | https://cropwatch.unl.edu/assessing-early-season-hail-damage-corn-and-soybean/ | 2014-06-03, updated 2026-05-27 | ASK | Core Nebraska stand/defoliation guide, current. Wait 7-10 days; corn growing point below ground to about V5-V6 |
| H2 | Mid-Season Hail Damage Assessments in Corn and Soybeans | UNL CropWatch | https://cropwatch.unl.edu/2018/mid-season-hail-damage-assessments-corn-and-soybeans | REVISED 2021-06-25 | ASK | Defoliation to yield-loss guidance by stage |
| H3 | Hail Damage to Soybean in Reproductive Stages and Options | UNL CropWatch | https://cropwatch.unl.edu/2023/hail-damage-soybean-reproductive-stages-and-options | 2023-07-14 | ASK | R1+ soybean losses and options |
| H4 | Assessing Plant Recovery and Replant Considerations | UNL CropWatch | https://cropwatch.unl.edu/2022/assessing-plant-recovery-and-replant-considerations | 2022-05-27 | ASK | Replant economics |
| H5 | Severe Storms and Hail-Damaged Crops: Steps for Making the Right Decisions | UNL CropWatch | https://cropwatch.unl.edu/2018/severe-storms-and-hail-damaged-crops-making-right-decision-replants | 2018-06-14 | ASK | Decision steps |
| H6 | Hail damage to corn and soybeans in August | UMN Crop News blog | https://blog-crop-news.extension.umn.edu/2023/08/hail-damage-to-corn-and-soybeans-in.html | 2023-08-15 | OK-attr (CC BY-SA 4.0) | Late-season hail, Goss's wilt risk, soybean vulnerability near R5.5 |
| H7 | Assessing hail damage to corn | K-State Agronomy eUpdate | https://eupdate.agronomy.ksu.edu/article_new/assessing-hail-damage-to-corn-553-1 | undated | ASK | Regional cross-check |
| H8 | Assessing hail damage on soybeans | K-State Agronomy eUpdate | https://eupdate.agronomy.ksu.edu/article_new/assessing-hail-damage-on-soybeans-459-2 | undated | ASK | Determinate vs indeterminate loss |
| H9 | Soybean damage and replanting | UMN Extension | https://extension.umn.edu/soybean-planting/soybean-damage-and-replanting | undated | AVOID ((c) Regents, all rights reserved) | Soybean replant thresholds |
| H10 | Corn Loss Adjustment Handbook, FCIC-25080 (2024) | USDA RMA | https://rma.usda.gov/sites/default/files/handbooks/2024-25080-Corn-Loss-Adjustment-Handbook.pdf | Last-Modified 2024-03-21; PDF, ~2.0 MB | OK | How adjusters appraise hail/stand loss; the "official" numbers behind claims. 2024 is the newest corn edition I found (2026 and 2025 URL guesses were 404) |
| H11 | Soybean Loss Adjustment Standards Handbook, FCIC-25440 (2026) | USDA RMA | https://www.rma.usda.gov/sites/default/files/2025-12/2026-25440-Soybean-Loss-Adjustment-Standards-Handbook.pdf | Last-Modified 2025-12-16; PDF, ~2.3 MB | OK | Soybean hail/plant-damage appraisal, node and R-stage identification |

### 1.2 Ponding, flooding, drown-out (condition: ponding)

| # | Title | Source | URL | Date | Status | Why |
|---|---|---|---|---|---|---|
| P1 | Corn and Soybean Survival in Saturated and Flooded Soils | UNL CropWatch | https://cropwatch.unl.edu/corn-and-soybean-survival-saturated-and-flooded-soils | REVISED 2024-05-24 (orig. 2015-05-07) | ASK | The survival-by-stage reference |
| P2 | Crop Impacts and Options After Mid-season Flooding | UNL CropWatch | https://cropwatch.unl.edu/2019/crop-impacts-mid-season-flooding | REVISED 2024-07-02 (orig. 2019-07-12) | ASK | Mid-season outcomes; keep records, call adjuster |
| P3 | Flooding and Ponding in Corn | UNL CropWatch | https://cropwatch.unl.edu/2018/flooding-and-ponding-corn/ | 2018-06-22 | ASK | N loss with flooding |
| P4 | Corn Survival in Ponded or Flooded Fields | UNL CropWatch | https://cropwatch.unl.edu/2017/corn-survival-saturated-soils/ | 2017-05-19 | ASK | Older overlap with P1; low priority |
| P5 | Flooded Fields and Saturated Conditions Impact Crops | UMN Crop News blog | https://blog-crop-news.extension.umn.edu/2018/07/flooded-fields-and-saturated-conditions.html | 2018-07 (from URL) | OK-attr (CC BY-SA 4.0) | Flooding by stage |
| P6 | Continued Rainfall and Excessively Wet Field Conditions | UMN Crop News blog | https://blog-crop-news.extension.umn.edu/2018/06/continued-rainfall-and-excessively-wet.html | 2018-06 (from URL) | OK-attr (CC BY-SA 4.0) | Wet-soil disease and N |
| P7 | Effects of Flooding or Ponding on Corn Prior to Tasseling | Purdue Corny News | https://www.agry.purdue.edu/ext/corn/news/timeless/pondingyoungcorn.html | undated | ASK | Check for new whorl leaves 3-5 days after water drains |
| P8 | Potential Impacts of Flooding on Corn Growth and Yield | Purdue Agriculture | https://ag.purdue.edu/news/department/agry/kernel-news/2021/07/impacts-flooding-corn-growth-yield.html | 2021-07 (from URL) | ASK | Flood timing vs yield |
| P9 | Soybean response to standing water and saturated soils | K-State eUpdate | https://eupdate.agronomy.ksu.edu/article/soybean-response-to-standing-water-and-saturated-soils-642-1 | 2025-05-22 issue | ASK | Silt on leaves, root rot |
| P10 | Flooded corn | UMN Extension | https://extension.umn.edu/agriculture/crop-production/corn/flooded-corn | undated | AVOID (all rights reserved) | Supplemental N guidance |
| P11 | Prevented Planting Insurance Provisions: Flood fact sheet | USDA RMA | https://www.rma.usda.gov/sites/default/files/2024-02/Prevented-Planting-Insurance-Provisions-Flood-Fact-Sheet.pdf | PDF metadata modified 2025-09-23; 1 page, 863 KB | OK | Prevented-planting rules after flooding |

### 1.3 Nitrogen (condition: nitrogen)

| # | Title | Source | URL | Date | Status | Why |
|---|---|---|---|---|---|---|
| N1 | Nitrogen for Corn: N Stress, How to Detect? | UNL CropWatch | https://cropwatch.unl.edu/nitrogen-corn-n-stress-how-detect/ | 2025-09-09 | ASK | Inverted-V yellowing diagnosis |
| N2 | Nitrogen Rescue Treatments in Corn | UNL CropWatch | https://cropwatch.unl.edu/2016/nitrogen-rescue-treatments-corn/ | 2016-06-17 | ASK | Rescue N and yield response (older: flag) |
| N3 | Nitrogen Fertilizer Stabilizers in Corn | UNL CropWatch | https://cropwatch.unl.edu/2018/nitrogen-fertilizer-stabilizers-corn/ | 2018-04-19 | ASK | Loss prevention |
| N4 | Wet spring weather and nitrogen loss: what has happened to my pre-plant nitrogen? | UMN Crop News blog | https://blog-crop-news.extension.umn.edu/2017/06/wet-spring-weather-and-nitrogen-loss.html | 2017-06 (from URL) | OK-attr (CC BY-SA 4.0) | Nitrate vs ammonium loss logic |
| N5 | Soggy Soils, N Loss, & Supplemental Nitrogen Fertilizer for Corn | Purdue Corny News | https://www.agry.purdue.edu/ext/corn/news/articles.04/NitrogenLoss-0602.html | about 2004-06-02 (from filename) | ASK | N-loss estimate; old |

Nitrogen is also covered by P3, P5 and P6.

### 1.4 Drought, water stress, sandy soils (conditions: drought, sandy_soil_water_stress)

| # | Title | Source | URL | Date | Status | Why |
|---|---|---|---|---|---|---|
| D1 | Drought-stressed Crops and Yield Potential | UNL CropWatch | https://cropwatch.unl.edu/2023/drought-stressed-crops-and-yield-potential/ | 2023-06-23 | ASK | Leaf-rolling and stage sensitivity |
| D2 | Vegetative Growth Stage Irrigation, Is It Needed This Year? | UNL CropWatch | https://cropwatch.unl.edu/2020/vegetative-growth-stage-irrigation-it-needed-year/ | 2020-06-19 | ASK | Early irrigation decision |
| D3 | Early season drought effects on corn and soybean | UMN Crop News blog | https://blog-crop-news.extension.umn.edu/2021/06/early-season-drought-effects-on-corn.html | 2021 (URL); page metadata 2023-06-16 | OK-attr (CC BY-SA 4.0) | Recovery after rain |
| D4 | Soil-Water-Plant Interaction Basics | UNL CropWatch | https://cropwatch.unl.edu/2024/soil-water-plant-interaction-basics/ | 2024-06-12 | ASK | Available water by soil type |
| D5 | Scheduling the Last Few Irrigations of the Season | UNL CropWatch | https://cropwatch.unl.edu/2022/scheduling-last-few-irrigations-season/ | 2022-08-25 | ASK | Allowable depletion targets; sandy-soil storage |

### 1.5 Center pivot and irrigation troubleshooting (condition: pivot_failure)

| # | Title | Source | URL | Date | Status | Why |
|---|---|---|---|---|---|---|
| V1 | Common Problems Regarding Center Pivot Operating Pressure and Uniformity | UNL CropWatch | https://cropwatch.unl.edu/2020/common-problems-regarding-center-pivot-operating-pressure-and-uniformity/ | 2020-01-22 | ASK | Nozzles, regulators, pressure |
| V2 | Center Pivot End Gun Considerations | UNL CropWatch | https://cropwatch.unl.edu/2020/center-pivot-end-gun-considerations/ | 2020-08-20 | ASK | End-gun pressure faults |
| V3 | The Importance of Maintaining High Uniformity of Water Application of Center Pivot | UNL CropWatch | https://cropwatch.unl.edu/2023/importance-maintaining-high-uniformity-water-application-center-pivot/ | 2023-11-30 | ASK | Catch-can test |
| V4 | Sprinkler Irrigation System Maintenance for Improved Uniformity and Application Efficiency | UNL CropWatch | https://cropwatch.unl.edu/2016/sprinkler-irrigation-system-maintenance-improved-uniformity-and-application-efficiency/ | 2016-08-23 | ASK | Maintenance checklist |
| V5 | Checklist for Maintaining Your Pivot | UNL CropWatch | https://cropwatch.unl.edu/checklist-maintaining-your-pivot/ | 2013-03-14 | ASK | Old; low priority |

### 1.6 Planter and stand problems (condition: planter_skip)

| # | Title | Source | URL | Date | Status | Why |
|---|---|---|---|---|---|---|
| PL1 | Replanting Corn: Things To Do and Think About | UNL CropWatch | https://cropwatch.unl.edu/replanting-corn-things-do-and-think-about/ | 2019-05-08, updated 2024-05-24 | ASK | Replant economics |
| PL2 | Timely Field Stand Counts for Corn, Soybean and Sorghum | UNL CropWatch | https://cropwatch.unl.edu/timely-field-stand-counts-corn-soybean-and-sorghum/ | 2025-06-04, updated 2026-06-04 | ASK | Stand-count method |
| PL3 | Evaluating Corn Stands | ISU ICM News | https://crops.extension.iastate.edu/cropnews/2019/05/evaluating-corn-stands | 2019-05 (from URL); page meta 2025-10-28 | ASK (see ICM rule, section 2) | Row-to-row vs within-row emergence |
| PL4 | Corn replant checklist | ISU ICM Encyclopedia | https://crops.extension.iastate.edu/encyclopedia/corn-replant-checklist | page meta 2026-06-10 | ASK (Encyclopedia not covered by the ICM News rule) | Decision checklist |
| PL5 | Thin Soybean Stands: Should I Replant, Fill In, or Leave Them Alone? (SPS-104-W) | Purdue Extension | https://www.extension.purdue.edu/extmedia/SPS/SPS-104-W.pdf | 2007-11; 6 pages, 768 KB | ASK | Soybean stand math; old but evergreen |
| PL6 | Early Corn Growth May be Slow, but Don't Throw in the Towel | UNL CropWatch | https://cropwatch.unl.edu/early-corn-growth-may-be-slow-dont-throw-towel/ | 2011-04-26 | ASK | Stale (15 years); use only if nothing better |

### 1.7 Herbicide drift and injury (condition: herbicide_drift)

| # | Title | Source | URL | Date | Status | Why |
|---|---|---|---|---|---|---|
| HD1 | Dicamba Injury Symptoms on Sensitive Crops | UNL CropWatch | https://cropwatch.unl.edu/2017/dicamba-injury-symptoms-sensitive-crops/ | 2017-06-28 | ASK | Symptom ID |
| HD2 | Crop Injury Associated with Growth Regulator Herbicides | ISU ICM News | https://crops.extension.iastate.edu/cropnews/2018/07/crop-injury-associated-growth-regulator-herbicides | 2018-07 (from URL); page meta 2025-04-07 | ASK (ICM rule) | 2,4-D vs dicamba |
| HD3 | Identifying Common Herbicide Symptoms in Soybean | ISU ICM | https://crops.extension.iastate.edu/post/identifying-common-herbicide-symptoms-soybean | page meta 2024-10-30 | ASK | Symptom key; photos have separate copyright |
| HD4 | Not all drift is created equal | ISU ICM | https://crops.extension.iastate.edu/post/not-all-drift-created-equal | page meta 2025-04-04 | ASK | Drift sensitivity |
| HD5 | Differentiating 2,4-D and Dicamba Injury on Soybeans (PPP-126) | Purdue Plant & Pest Programs | https://ag.purdue.edu/department/extension/ppp/resources/ppp-publications/mobile/ppp-126/differentiating-2-4-d-and-dicamba-injury-on-soybeans.html | undated | ASK | Practitioner key |

### 1.8 Diseases (condition: disease_pest)

| # | Title | Source | URL | Date | Status | Why |
|---|---|---|---|---|---|---|
| DS1 | Corn Foliar Disease Diagnostic Poster and Postcard | CPN | https://cropprotectionnetwork.org/publications/corn-foliar-disease-diagnostic-poster | 2025-09-11 | ASK | One-stop ID for foliar diseases |
| DS2 | An Overview of Northern Corn Leaf Blight | CPN | https://cropprotectionnetwork.org/publications/an-overview-of-northern-corn-leaf-blight | 2023-04-20 | ASK | NCLB scouting and risk |
| DS3 | Goss's Wilt of Corn | CPN Encyclopedia | https://cropprotectionnetwork.org/encyclopedia/gosss-wilt-of-corn | 2018-01-01 (looks like a placeholder date) | ASK | Bacterial; fungicides ineffective |
| DS4 | Tar Spot of Corn | CPN Encyclopedia | https://cropprotectionnetwork.org/encyclopedia/tar-spot-of-corn | 2018-01-01 (placeholder) | ASK | |
| DS5 | Gray Leaf Spot of Corn | CPN Encyclopedia | https://cropprotectionnetwork.org/encyclopedia/gray-leaf-spot-of-corn | 2018-01-01 (placeholder) | ASK | |
| DS6 | Southern Rust of Corn | CPN Encyclopedia | https://cropprotectionnetwork.org/encyclopedia/southern-rust-of-corn | 2018-01-01 (placeholder) | ASK | |
| DS7 | Sudden Death Syndrome of Soybean | CPN Encyclopedia | https://cropprotectionnetwork.org/encyclopedia/sudden-death-syndrome-of-soybean | 2018-01-01 (placeholder) | ASK | |
| DS8 | White Mold of Soybean | CPN Encyclopedia | https://cropprotectionnetwork.org/encyclopedia/white-mold-of-soybean | 2018-01-01 (placeholder) | ASK | |
| DS9 | Frogeye Leaf Spot of Soybean | CPN Encyclopedia | https://cropprotectionnetwork.org/encyclopedia/frogeye-leaf-spot-of-soybean | 2018-01-01 (placeholder) | ASK | |
| DS10 | Fungicide Application Reminders to Optimize Management of Tar Spot and Return on Investment in Corn | CPN | https://cropprotectionnetwork.org/publications/fungicide-application-reminders-to-optimize-management-of-tar-spot-and-return-on-investment-in-corn | 2024-06-14 | ASK | Timing (VT-R1), product classes |
| DS11 | Tar Spot Prediction in Corn: The Weather Matters | CPN | https://cropprotectionnetwork.org/publications/tar-spot-prediction-in-corn-the-weather-matters | 2023-12-18 | ASK | Risk factors |
| DS12 | Fungicide Efficacy for Control of Corn Diseases | CPN | https://cropprotectionnetwork.org/publications/fungicide-efficacy-for-control-of-corn-diseases | 2026-02-23 | ASK | Efficacy tables (chunk carefully) |
| DS13 | Fungicide Efficacy for Control of Soybean Foliar Diseases | CPN | https://cropprotectionnetwork.org/publications/fungicide-efficacy-for-control-of-soybean-foliar-diseases | 2026-03-11 | ASK | |
| DS14 | Corn Disease Loss Estimates, US and Ontario, 2025 | CPN | https://cropprotectionnetwork.org/publications/corn-disease-loss-estimates-from-the-united-states-and-ontario-canada-2025 | 2026-03-06 | ASK | Which diseases matter this year |
| DS15 | Soybean Disease Loss Estimates, US and Ontario, 2025 | CPN | https://cropprotectionnetwork.org/publications/soybean-disease-loss-estimates-from-the-united-states-and-ontario-canada-2025 | 2026-03-06 | ASK | |
| DS16 | Sudden Death Syndrome in Soybean: Tips for Diagnosis and Management | UNL CropWatch | https://cropwatch.unl.edu/sudden-death-syndrome-soybean-tips-diagnosis-and-management/ | 2025-09-04 | ASK | Nebraska SDS |
| DS17 | Making Fungicide Decisions for Corn and Soybean in 2026 | UNL CropWatch | https://cropwatch.unl.edu/making-fungicide-decisions-corn-and-soybean-2026/ | 2026-06-19 | ASK | Nebraska decision guide (seasonal) |
| DS18 | Late Season Soybean Diseases Widespread in Areas of Nebraska | UNL CropWatch | https://cropwatch.unl.edu/2019/late-season-soybean-diseases-widespread-areas-nebraska/ | 2019-09-13 | ASK | White mold, SDS, frogeye scouting |

### 1.9 Insects (condition: disease_pest)

| # | Title | Source | URL | Date | Status | Why |
|---|---|---|---|---|---|---|
| I1 | Scouting and Treating Western Bean Cutworm | UNL CropWatch | https://cropwatch.unl.edu/2018/scouting-and-treating-scouting-and-treating-western-bean-cutworm/ | 2018-07-11 | ASK | Scout at 25% flight; 5-8% infested plants where no effective Bt |
| I2 | Degree-Days for Prediction of Western Bean Cutworm Flight in 2026 | UNL CropWatch | https://cropwatch.unl.edu/degree-days-prediction-western-bean-cutworm-flight-2026/ | 2026-06-30 / 2026-07-16 | ASK | Annual, seasonal |
| I3 | Scout Now for Corn Rootworm Beetles to Assess Potential Risk of Future Damage | UNL CropWatch | https://cropwatch.unl.edu/2017/scout-now-corn-rootworm-beetles-assess-potential-risk-future-damage/ | 2017, REVISED 2022-07-19 | ASK | Beetle scouting |
| I4 | Scouting and Managing Soybean Aphids | UNL CropWatch | https://cropwatch.unl.edu/2018/scouting-and-managing-soybean-aphids/ | 2018-08-02 | ASK | 250/plant, 80% infested, rising |
| I5 | Scout Corn and Soybean for Japanese Beetles | UNL CropWatch | https://cropwatch.unl.edu/2018/scout-corn-and-soybean-japanese-beetles/ | 2018-06-25 | ASK | Defoliation and silk-clipping thresholds |
| I6 | Is it too late to Spray Soybean Aphids? | ISU ICM News | https://crops.extension.iastate.edu/cropnews/2024/08/it-too-late-spray-soybean-aphids | 2024-08 (from URL); page meta 2024-08-26 | ASK (ICM rule) | Late-season timing |
| I7 | Japanese Beetle Adults Now Emerging | ISU ICM News | https://crops.extension.iastate.edu/cropnews/2023/06/japanese-beetle-adults-now-emerging | 2023-06 (from URL) | ASK (ICM rule) | |
| I8 | Japanese Beetle in Corn and Soybean | ISU ICM Encyclopedia | https://crops.extension.iastate.edu/encyclopedia/japanese-beetle-corn-and-soybean | page meta 2026-07-29 | ASK | |
| I9 | Time to start thinking about early season scouting for soybean aphids | UMN Crop News blog | https://blog-crop-news.extension.umn.edu/2025/06/time-to-start-thinking-about-early.html | 2025-06-23 | OK-attr (CC BY-SA 4.0) | |
| I10 | Field Notes: Insect updates and management considerations | UMN Crop News blog | https://blog-crop-news.extension.umn.edu/2025/08/field-notes-insect-updates-and.html | 2025-08-11 | OK-attr (CC BY-SA 4.0) | |
| I11 | Scouting for corn rootworm; Scouting for soybean aphid; European corn borer in Minnesota field corn (3 docs) | UMN Extension | https://extension.umn.edu/corn-pest-management/scouting-corn-rootworm ; https://extension.umn.edu/soybean-pest-management/scouting-soybean-aphid ; https://extension.umn.edu/corn-pest-management/european-corn-borer-minnesota-field-corn | undated | AVOID (all rights reserved) | The only European corn borer page found: see gaps |

### 1.10 Growth staging and harvest/maturity (condition: harvest/maturity; staging tags apply to all)

| # | Title | Source | URL | Date | Status | Why |
|---|---|---|---|---|---|---|
| G1 | Corn Growth Stages | ISU ICM Encyclopedia | https://crops.extension.iastate.edu/encyclopedia/corn-growth-stages | page meta 2026-06-15 | ASK | V/R definitions, 50% whole-field rule |
| G2 | Soybean Growth Stages | ISU ICM Encyclopedia | https://crops.extension.iastate.edu/encyclopedia/soybean-growth-stages | page meta 2026-06-10 | ASK | |
| G3 | Do you know your crop growth stages? | ISU ICM | https://crops.extension.iastate.edu/post/do-you-know-your-crop-growth-stages | page meta 2025-12-30 | ASK | |
| G4 | Corn growth and development; Soybean growth stages (2 docs) | UMN Extension | https://extension.umn.edu/growing-corn/growth-and-development ; https://extension.umn.edu/growing-soybean/soybean-growth-stages | undated | AVOID | |
| G5 | Corn Field Guide | ISU Extension | https://crops.extension.iastate.edu/files/inline-files/corn-field-guide.pdf | 2009; PDF 2.4 MB | AVOID ("2009 Iowa State University of Science and Technology All rights reserved"; photos credited to individuals) | |
| HV1 | Harvest 2025 Prep Guide: Minimize Losses in Disease-Stressed Corn | UNL CropWatch | https://cropwatch.unl.edu/harvest-2025-prep-guide-minimize-losses-disease-stressed-corn/ | 2025-09-19 | ASK | Harvest order for stressed fields |
| HV2 | How fast can corn dry down? | ISU ICM Encyclopedia | https://crops.extension.iastate.edu/encyclopedia/how-fast-can-corn-dry-down | page meta 2014-12-04 | ASK | Drydown rates |
| HV3 | Grain Drying Q&A | UNL CropWatch | https://cropwatch.unl.edu/grain-drying-qa/ | 2009-10-12 | ASK | Stale |

The RMA handbooks (H10, H11) contain public-domain corn and soybean stage definitions (V/R, node counting). Use them as the open staging backbone until ISU/UNL permission is in hand.

### 1.11 Crop insurance basics (USDA RMA / FCIC; supports every condition)

H10, H11 and P11 above also serve here.

| # | Title | Source | URL | Date / size | Status | Why |
|---|---|---|---|---|---|---|
| R3 | Replanting and Final Planting Dates (FAQ) | USDA RMA | https://www.rma.usda.gov/about-crop-insurance/frequently-asked-questions/replanting-final-planting-dates | Last-Modified 2026-10-16 | OK | Replant payment definition, "same crop" rule |
| R4 | Summary of Changes for the Coarse Grains Crop Provisions (25-0041) | USDA RMA | https://www.rma.usda.gov/sites/default/files/2024-10/Summary%20of%20Changes%20for%20the%20Coarse%20Grains%20Crop%20Provisions%2025-0041.pdf | 2024-10-07; 239 KB | OK | Corn replant payment and 90% test (via search summary). Add the full Coarse Grains Crop Provisions and the Soybean Crop Provisions: URLs not found, **unverified** |
| R5 | Common Crop Insurance Policy Basic Provisions 26-BR | USDA RMA | https://www.rma.usda.gov/sites/default/files/2025-11/Basic-Provisions-26-BR.pdf | 2026-02-11; 573 KB | OK | Notice of loss, replant, prevented planting. Newer 27-BR / 27.1-BR exist for the 2027 crop year (index by crop year) |
| R6 | Crop Insurance Handbook (FCIC-18010) | USDA RMA | https://www.rma.usda.gov/sites/default/files/2026-06/Crop%20Insurance%20Handbook.pdf | 2026-06-30; 10.5 MB | OK | Large reference; index selectively |

### 1.12 Counts by topic and tier

| Topic | Tier 1 (OK / OK-attr) | Tier 2 (ASK) | Avoid |
|---|---|---|---|
| Hail / replant | 3 (H6, H10, H11) | 7 | 1 |
| Ponding / flood | 3 (P5, P6, P11) | 7 | 1 |
| Nitrogen | 1 (N4) | 4 | 0 |
| Drought / sandy-soil water | 1 (D3) | 4 | 0 |
| Pivot / irrigation | 0 | 5 | 0 |
| Planter / stand | 0 | 6 | 0 |
| Herbicide drift | 0 | 5 | 0 |
| Disease | 0 | 18 | 0 |
| Insects | 2 (I9, I10) | 8 | 3 |
| Staging / harvest | 0 | 6 | 3 |
| Crop insurance (R3-R6) | 4 | 0 | 0 |
| **Total (92)** | **14** | **70** | **8** |

## 2. License summary by publisher

"Commercial verdict" means: may FieldWatch put this content in a commercial RAG and show cited snippets or summaries to farmers? Quotes are verbatim from pages I fetched.

| Publisher / series | What I found (quote and link) | Commercial verdict |
|---|---|---|
| **USDA (RMA, NRCS, NASS, ARS)** | USDA policy page: "Most information presented on the USDA Web site is considered public domain information." It asks for credit to "U.S. Department of Agriculture". Verified on the mirror https://usda.azureedge.us/node/6232; the canonical https://www.usda.gov/policies-and-links returned HTTP 403 to my scripted fetch. Third-party or copyrighted items are excluded and "labeled where possible". The RMA handbook PDFs I downloaded carry no copyright line. NASS PDFs returned 403 to scripted fetch (Cornell ESMIS mirror: https://usda.library.cornell.edu/concern/publications/8336h188j). NRCS policy page timed out: **unverified**. | **Yes.** Credit USDA; skip images and third-party content |
| **University of Minnesota Extension: Crop News blog** (blog-crop-news.extension.umn.edu) | Per-post footer, verified on 7 posts: "This work is licensed under a Creative Commons Attribution-ShareAlike 4.0 International License" (http://creativecommons.org/licenses/by-sa/4.0/). The sister blog https://blog-nwcrops.extension.umn.edu/ showed **no** such line, so it is not covered. | **With attribution.** CC BY-SA permits commercial use; attribution and ShareAlike apply. Whether RAG chunks or generated answers count as "adaptations" is unsettled: ask counsel. Re-check the license on every post at ingestion; images may be separately owned |
| **University of Minnesota Extension: extension.umn.edu pages** | Footer: "(c) 2026 Regents of the University of Minnesota. All rights reserved." (https://extension.umn.edu/agriculture/crop-production/corn/flooded-corn) | **No** without permission |
| **Iowa State ICM: ICM News articles** (crops.extension.iastate.edu/cropnews) | https://crops.extension.iastate.edu/icm-policies : articles may be republished without further permission "if published as written" and "if credit is given to the author, Integrated Crop Management News, and Iowa State University Extension and Outreach". "Other uses of ICM News articles require the author's permission." Images by Marlin E. Rice are prohibited without permission. | **Ask.** Verbatim, credited republishing is explicit; summarizing or chunking for an AI product is not clearly "as written". Commercial use is not addressed |
| **Iowa State ICM: Encyclopedia, /post/, /blog/ pages; PDFs** | No separate terms found. Corn Field Guide PDF: "2009 Iowa State University of Science and Technology All rights reserved." Another ISU PDF (847.pdf) embeds Crop Science material marked "All copyrights reserved", so ISU files can contain third-party copyright. | **Ask; avoid PDFs** |
| **ISU Extension Store** | https://shop.iastate.edu/ext-terms-conditions.html : "All digital media distributed on this site is copyrighted by Iowa State University Extension and Outreach." Reprints: "please email extstore@iastate.edu to request a permission form". Linking needs no permission; reposting does. | **No** without a permission form |
| **UNL CropWatch** (cropwatch.unl.edu, "published by IANR Media") | No terms-of-use, reuse or permission statement on any CropWatch page I fetched (home, subscribe, 8 articles). Footer says only "Established 1869 - Copyright 2026". Contact on the subscribe page: cropwatch@unl.edu. Archived UNL Extension publications carry "(c) ... The Board of Regents of the University of Nebraska on behalf of the University of Nebraska-Lincoln Extension. All rights reserved." (e.g. https://digitalcommons.unl.edu/extensionhist/2790, via search result). The only UNL Extension reuse text I found is for a different UNL site, https://food.unl.edu/terms-of-use : "You may reproduce the articles and educational content resources for personal or educational purposes." and "These items are not for use in a publication or resource for sale, including a commercial website." That is a good indicator of UNL's likely stance on commercial use. | **Ask; treat as No until written permission.** A general UNL Extension copyright page was not found (https://extension.unl.edu/copyright/ is 404) |
| **UNL NebGuides / Extension publications** | Not individually reviewed (no stable index found). Same Regents copyright applies. | **Ask** |
| **Crop Protection Network** (cropprotectionnetwork.org) | No content license or terms page found (https://cropprotectionnetwork.org/terms-of-use is 404; /about shows only "Copyright 2026"). Publications give "How to cite" lines with DOIs (10.31274/cpn-...), say "All photos were provided by and are the property of the authors and contributors", and carry a liability disclaimer. The HTML comment "Copyright 2006-2026 Setwise Technology LLC" is the website vendor's code notice, not the content license. Contact: https://cropprotectionnetwork.org/contact | **Ask** (unclear: images owned by individual authors) |
| **Purdue Extension / Purdue Ag** | "Copyright 2026 Purdue University. All Rights Reserved." (https://extension.entm.purdue.edu/newsletters/pestandcrop/); store: "Copyright 2024-2026 Purdue Extension - All rights reserved" (https://edustore.purdue.edu/). SPS-104-W PDF has no reuse text. | **No** without permission |
| **K-State Research and Extension: bookstore publications** | "Contents of this publication may be freely reproduced for educational purposes. All other rights reserved. In each case, credit ..." (https://bookstore.ksre.ksu.edu/pubs/kansas-corn-management-2025_MF3208.pdf, Dec 2024). | **Ask.** Educational-purpose reuse only; a commercial advisory product is arguably not "educational purposes" |
| **K-State Agronomy eUpdate** | Page footer only "(c) Kansas State University". K-State's copyright statement (https://www.k-state.edu/copyright.html, via search result, not fetched) says redistribution of copyrighted material requires owner permission. | **Ask** |
| **NCGA** | No document selected, not evaluated. | unclear: ask |
| **Ohio State, Wisconsin, Michigan State, NDSU** | Fetches returned scripts or blocked pages (MSU Incapsula block); not evaluated. | **unverified** |

Even on open-licensed sources, do not index images: many photos are individually copyrighted.

## 3. Metadata scheme

One JSON record per document; each chunk inherits the document fields.

```json
{
  "doc_id": "unl-cropwatch-corn-soy-flood-survival",
  "title": "Corn and Soybean Survival in Saturated and Flooded Soils",
  "publisher": "University of Nebraska-Lincoln Extension",
  "series": "CropWatch",
  "authors": ["Jenny Brhel", "Jim Specht", "Aaron Nygren", "Nathan Mueller"],
  "source_url": "https://cropwatch.unl.edu/corn-and-soybean-survival-saturated-and-flooded-soils",
  "doi": null,
  "published": "2015-05-07",
  "revised": "2024-05-24",
  "year": 2024,
  "retrieved_at": "2026-10-16",
  "content_hash": "sha256:...",
  "format": "html",
  "crop": ["corn", "soybean"],
  "condition_ids": ["ponding", "nitrogen"],
  "topic": "flood survival by growth stage",
  "growth_stage": ["VE-V6", "V7-V10", "VT-R1", "R2-R6"],
  "region": ["NE", "midwest"],
  "license_status": "ASK",
  "license_text": "verbatim quote or 'none found'",
  "license_url": "https://...",
  "commercial_ok": "yes | with-attribution | ask | no",
  "attribution_line": "Source: ...",
  "permission_ref": null,
  "staleness": "fresh | aging(>5y) | stale(>10y)"
}
```

Condition-id mapping used above: pivot_failure (V*), hail (H*), drought (D1-D3), ponding (P*), nitrogen (N*, P3, P5, P6), herbicide_drift (HD*), planter_skip (PL*), disease_pest (DS*, I*), sandy_soil_water_stress (D4, D5), harvest/maturity (HV*, G*). Staging docs apply to every condition: tag `growth_stage` and filter on crop + stage at retrieval. Region tag is the publisher's state (NE, IA, MN, IN, KS, US) so Nebraska results can rank first.

## 4. Gaps and outreach

| Gap | Detail | Suggested action |
|---|---|---|
| Open-licensed disease content | No openly licensed source for tar spot, GLS, NCLB, southern rust, Goss's wilt, SDS, white mold, frogeye | Ask CPN via https://cropprotectionnetwork.org/contact for a written license; ask which institution holds rights (the 10.31274 DOI prefix appears to be Iowa State's repository, so ISU may be the right party; unconfirmed) |
| Center-pivot / irrigation | Only UNL material found | Ask UNL IANR Media (cropwatch@unl.edu) |
| Planter skips / emergence | Only UNL, ISU, Purdue | Ask |
| Herbicide drift ID | All sources all-rights-reserved or silent | Ask UNL / ISU / Purdue |
| European corn borer | Only UMN extension.umn.edu (avoid) | Look for UMN blog posts or other sources next pass |
| Corn rootworm | UNL only (ask); UMN extension page (avoid) | Ask UNL |
| Soybean harvest/maturity (R7/R8, seed moisture) | No source found in my searches | Next research pass |
| Nebraska N rate guidance (MRTN) | Not searched in this pass | Next pass: UNL NebGuides (not catalogued) |
| NebGuides / UNL Extension publications | No stable index or license found | Request the UNL catalog and a blanket license |
| Purdue E-49 Managing Corn Rootworms | URL returned an HTML page, not a PDF | **unverified**; dropped |
| ISU soybean defoliator scouting guide | HTTP 403 to my fetch | **unverified**; dropped |

Outreach order: (1) UNL IANR (CropWatch + NebGuides): ask for a written, non-exclusive license for an AI knowledge base with citation and link-back, explicitly including commercial use; offer attribution, a link to the source on every answer, and no republishing of full text. (2) CPN. (3) ISU Extension (ICM News rule plus store permission form). (4) K-State (clarify "educational purposes"). (5) Purdue only if gaps remain.

## 5. Ingestion notes

**Fetching and citation**
- UNL CropWatch: Drupal site. `/sitemap.xml` returned 404. robots.txt is the stock Drupal file (disallows /admin, /search, /node/add and similar; no crawl delay, no AI directives). No RSS link was found (the subscribe page offers only a mailing list). Crawl from topic/taxonomy pages and ask UNL for a feed. Articles show `Published:` and `Updated:`/`REVISED:` lines: store both.
- CPN: no sitemap (404), no RSS found. Each publication has a DOI (e.g. https://doi.org/10.31274/cpn-20230425-0 resolves to the page): use the DOI as the canonical citation. Annual refresh: fungicide efficacy (Feb/Mar), loss estimates (Mar).
- UMN Crop News: Blogger Atom feed works (https://blog-crop-news.extension.umn.edu/feeds/posts/default, HTTP 200). Check the license footer per post.
- USDA RMA: stable PDFs with the year in the filename; handbooks reissue about each Nov-Dec. NASS blocks scripted clients with 403; use Cornell ESMIS.
- ISU: some endpoints return 403 or a bot-challenge page to scripted clients (extension.iastate.edu root). Identify the crawler honestly and ask for access.
- Always use an honest User-Agent, rate limit, and honor robots.txt.

**Chunking**
- Split HTML on headings; carry the heading path and article title in every chunk. Never split a threshold table or bullet list mid-row. Repeat crop and stage in each table row so a row stands alone ("Corn, V7-V10, flooded: survives 7-10 days if under 86F").
- Thresholds are the highest-value and easiest-to-garble facts: also extract them as structured fact cards (crop, pest/condition, threshold, unit, stage, source chunk id) and have agents quote the source sentence with a link.
- PDFs (RMA handbooks): `pdftotext -layout`; they are numbered paragraphs, so chunk by section number (for example "35C"), keep it in metadata, drop headers and footers.
- Flag conflicts between sources (UNL 4 days to V6 vs K-State 2-4 days vs Purdue about 4 days) and prefer Nebraska plus the most recent revision for Nebraska users.
- Do not index images or third-party figures.

**Citation and attribution**
- Every answer shows publisher, title, revised date and URL, plus the `attribution_line` (required for CC BY-SA and requested by USDA). Keep `license_text` and retrieval date so a takedown can remove by publisher.

**Refresh cadence**
- Re-crawl UNL, UMN, ISU, CPN weekly in season (Apr-Oct), monthly off-season; detect change by content hash and the Updated/REVISED line. RMA: check each Nov-Dec and each crop year (27-BR already exists). CPN: March. Flag documents over 5 years old (several UNL items here are 2011-2018) and over 10 years as stale; down-rank rather than drop unless superseded. Seasonal items (WBC degree days, fungicide decisions 2026) get a year tag and are not surfaced out of season.

## 6. Decisions the founder must make

1. **Permission strategy.** Approve permission requests to UNL IANR, CPN and ISU. Without them the product has 14 documents and no disease, pivot, planter or herbicide coverage.
2. **Risk appetite.** Whether to use Tier 2 content in a non-production sandbox while permission is pending. My recommendation: nothing farmer-facing until written permission.
3. **Fallback if permission is refused.** Choose between (a) citations and links only, no indexed text; (b) hand-written FieldWatch fact cards that state facts (thresholds, survival days) in our own words with a citation (facts are not copyrightable but wording, tables and figures are; have counsel confirm); or (c) dropping that publisher.
4. **CC BY-SA ShareAlike.** Decide whether to use the UMN blog posts, and get counsel's view on whether chunks and generated answers are "adaptations".
5. **ICM News.** Rely on the "as written" republish rule (verbatim whole articles only) or ask for explicit AI/commercial permission.
6. **Staleness policy.** Approve dropping or down-ranking documents older than 5 years.
7. **Regional scope.** Nebraska-first with Midwest fallback (K-State, Purdue, UMN, ISU), or Nebraska-only for the first release.
