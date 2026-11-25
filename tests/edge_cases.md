# Synthetic incremental fixtures

Each line in [edge_cases.jsonl](edge_cases.jsonl) contains three fields.

| Field | Meaning |
| --- | --- |
| `id` | Isolated synthetic chart. A later row with the same ID reopens the same SQLite database. |
| `add` | Raw, plain-text documents to add in this batch. An identical string delivers the same file content again. |
| `expect` | Expected snapshot fields after the batch. Omitted fields are outside the assertion for that step. |

The runner uses one fictional patient and April 6–12, 2026 for every isolated chart. `minutes` lists supported totals for the documented encounters. `bounds` refers to those encounters, and `complete` indicates whether the review dates have an affirmative source for attendance coverage. A `null` upper bound means the available encounter evidence gives no finite upper bound. To add data for an existing case, append a line with the same ID and the next expected snapshot.

The collection contains **17 cases over 23 deliveries**. Gemini 3.8 Flash contributed six cases with ten deliveries. Claude Sonnet 4.6 contributed two complete cases with three deliveries from a timed-out generation. GPT-OSS 120B contributed four cases with four deliveries. Codex added five focused cases with six deliveries. Both attempts with a local Qwen model exhausted their generation budget without returning structured cases. The external prompts contained fictional examples only.

Before execution, case drafts were reviewed against the source text they provided. Plan vocabulary was normalized to the project's days-and-minutes goal and several source records were formatted as attested clinical evidence. Two GPT-OSS duration labels were recalculated from their source intervals. The telehealth example contains 20 + 15 minutes. The overlap example contains 45 + 45 − 15 minutes. The result is a curated regression suite.

Run from the project directory.

```sh
python3 edge_suite.py
python3 -m unittest discover -s tests -v
```

The runner uses normal ingestion and reopens the resulting database between deliveries. Each step also retries the same batch and checks whether the retry caused any new extraction, encounter reconciliation, or weekly computation. The source-bearing obligations, joint conflict regression, and exhaustive oracle also run through the standard test command.
