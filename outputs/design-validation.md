# Validation of the clinical ledger and weekly inference

Run with `python3 validate_design.py`. The study uses synthetic documents and a fixed seed. The solver oracle enumerates admissible combinations and independently unions sets of occupied minutes. Database comparisons use complete public patient review outputs after reopening each database.

## Incremental updates

One signed amendment changes the first encounter from 45 to 55 minutes. The test compares normal incremental ingestion, forced replay of the entire affected patient, and a new database built from every file. Timings are local wall-clock medians of three runs. Database preparation, copying and output verification are outside the timed segment.

| Base documents | Patients | Weeks per patient | Incremental | Replay one patient | Rebuild collection |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 14 | 1 | 12 | 1.180 ms | 1.905 ms | 4.988 ms |
| 1,400 | 100 | 12 | 1.352 ms | 2.485 ms | 441.021 ms |
| 262 | 1 | 260 | 5.154 ms | 27.415 ms | 88.380 ms |
| 6,500 | 250 | 24 | 1.735 ms | 4.107 ms | 2131.540 ms |

All update strategies produced identical saved clinical outputs at every scale. Incremental processing extracted one file, recomputed one encounter and refreshed one week. The larger one-patient case took more time than the larger collection with shorter charts because patient-local regrouping still scans that patient's claims.

## Weekly certainty against the exhaustive oracle

| Scenario family | Trials | Separate bounds agree | Joint solver agrees |
| --- | ---: | ---: | ---: |
| Mixed random | 400 | 400 | 400 |
| Correlated shortfall | 120 | 0 | 120 |
| True boundary | 120 | 120 | 120 |

The bounds-only baseline receives the exact marginal minima and maxima from the oracle, which makes it a strong comparison for the logical test. The joint solver resolved all 120 correlated shortfalls left open by the baseline. It also returned 754 passing or failing witnesses that were independently verified against the input alternatives. The gain belongs to the deliberately constructed correlated subset. The mixed subset showed equal verdict accuracy.

## Incomplete charts

One visit was withheld in each of 120 trials. Treating the remaining chart as complete caused 40 wrong definite verdicts. Requiring affirmative attendance coverage for a shortfall produced 0 wrong definite verdicts, retained 40 proven passing cases and kept 80 unresolved. These trials explicitly remove the coverage guarantee when a document is withheld.

## Stress test and optimization

The alternative positions in the stress chart admit 16,384 complete combinations. The previous solver kept clock positions until each day closed and reached its state limit, returning an unresolved result in a median of 8.345 ms. The revised solver finalizes minutes once no later choice can overlap them. It proves the shortfall in a median of 0.033 ms. The independent exhaustive run confirms the verdict in 106.246 ms.

Attendance remains tracked until a date can no longer recur. With that rule, moving provably final clock minutes to the total preserves the two goal constraints. A state limit still produces an unresolved result on charts where the search cannot finish. The benchmarks use generated formats and do not estimate extraction recall or production throughput.

[Raw measurements](design-validation.json) · [Prior solver stress result](design-validation-mask-before.json) · [Original edge-suite rerun](edge-evaluation.json)
