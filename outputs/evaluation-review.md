# Collection and plan-change evaluation

The evaluation uses two fictional charts from the separate example corpus. The chart-level calculations are persisted in [collection-review.json](collection-review.json). The case source names and digests are in the [manifest](../source-manifest.json).

## Weekly plan eligibility, March 2–29, 2026

| Patient | Week of Mar 2 | Week of Mar 9 | Week of Mar 16 | Week of Mar 23 |
|---|---|---|---|---|
| Theodore Lindqvist | 1 day, 50 min · Not met | 3 days, 199 min · Met | 2 days, 160 min · Not met | 3 days, 165 or 185 min · Cannot determine |
| Nadia Brennan | 2 days, 130 min · Met | 1 day, 50 min · Not met | 2 days, 95 min · Not met | 1 day, 80 min confirmed · Cannot determine |

**Nadia meets the two-consecutive-below criterion** for March 9 and 16. The goal is two days and 105 minutes. On March 16 week the 45-minute individual contact and 50-minute group contact total 95 minutes, despite the later plan review’s claim that the goal was met that week. The March 24 draft would contribute another day and 50 minutes if independently confirmed. [Plan LRC-301 line 23](../evaluation-documents/LRC-301_master_treatment_plan_brennan_2026-02-23.txt#L23), [LRC-305 line 6](../evaluation-documents/LRC-305_individual_progress_note_brennan_2026-03-17.txt#L6), [LRC-306 lines 11–16](../evaluation-documents/LRC-306_client_visit_register_brennan_2026-03.txt#L11), [LRC-307 lines 6–12](../evaluation-documents/LRC-307_individual_note_in_progress_brennan_2026-03-24.txt#L6), [LRC-308 line 11](../evaluation-documents/LRC-308_30day_plan_review_brennan_2026-03-27.txt#L11).

**Theodore’s inclusion depends on March 23 documentation.** The updated goal is three days and 180 minutes starting March 16. Two signed March 23 records describe one encounter with 30 or 50 patient minutes. The other counted contacts that week total 135 minutes, yielding 165 or 185. If 30 is confirmed, Theodore has two consecutive weeks below goal. If 50 is confirmed, the March 23 week meets the goal. [LRC-208 lines 7–19](../evaluation-documents/LRC-208_treatment_plan_update_2026-03-12.txt#L7), [LRC-212 line 8](../evaluation-documents/LRC-212_individual_session_raman_2026-03-23.txt#L8), [LRC-213 lines 8–10](../evaluation-documents/LRC-213_consulting_psychologist_note_2026-03-23.txt#L8), [LRC-216 lines 20–23](../evaluation-documents/LRC-216_attendance_disposition_report_2026-03.txt#L20).

## Theodore before and after the plan change

Both comparison windows are two calendar weeks. The prior plan called for two days and 120 minutes per week. The update increased the goal to three days and 180 minutes and added family psychotherapy every other week. [LRC-201 line 33](../evaluation-documents/LRC-201_treatment_plan_initial_lindqvist_2026-02-16.txt#L33), [LRC-208 lines 7–19](../evaluation-documents/LRC-208_treatment_plan_update_2026-03-12.txt#L7), [computed plan change](plan-change.json).

| Service | Mar 2–15 sessions / minutes | Mar 16–29 sessions / minutes |
|---|---:|---:|
| Group | 3 / 200 | 3 / 210 |
| Individual | 1 / 49 | 2 / 80 or 100 |
| Family | 0 / 0 | 1 / 35 |
| Total | **4 / 249** | **6 / 325 or 345** |

Distinct therapy days increased from four to five. Patient therapy increased by 76 or 96 minutes, depending on the conflicting March 23 start time.

## Nadia’s distinct assessments

Documented PHQ-9 scores were 19 on Feb 20, 17 on Mar 3, 15 on Mar 10, and 12 on Mar 17. The Mar 27 review cites 16 for Mar 3, while the primary note records the in-session total of 17. Documented GAD-7 scores were 16 on Feb 20, 15 on Mar 3, and 11 on Mar 17. The unsigned Mar 24 note proposes PHQ-9 10 and GAD-7 9. Those values remain unverified. The March records also describe fewer panic attacks and progress using the bus, with continued exposure work. [LRC-301 line 9](../evaluation-documents/LRC-301_master_treatment_plan_brennan_2026-02-23.txt#L9), [LRC-302 lines 9–12](../evaluation-documents/LRC-302_individual_soap_note_brennan_2026-03-03.txt#L9), [LRC-304 line 10](../evaluation-documents/LRC-304_individual_note_brennan_2026-03-10.txt#L10), [LRC-305 lines 8–14](../evaluation-documents/LRC-305_individual_progress_note_brennan_2026-03-17.txt#L8), [LRC-307 lines 6–12](../evaluation-documents/LRC-307_individual_note_in_progress_brennan_2026-03-24.txt#L6), [LRC-308 lines 14–16](../evaluation-documents/LRC-308_30day_plan_review_brennan_2026-03-27.txt#L14), [computed assessments](evaluation-symptoms.json).


## Combined cohort

Across the combined January and March date range, Nadia has confirmed back-to-back below-goal weeks. Rowan remains conditional because the desk export closes on Jan 16 and the week of Jan 12 runs through Jan 18. Theodore remains conditional on the disputed March 23 start time. The query keeps the effective plan and review dates separate for each chart. [Combined three-patient result](combined-collection-review.json).
