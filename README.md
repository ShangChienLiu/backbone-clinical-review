# Backbone · Clinical evidence ledger

A Python and SQLite prototype for auditing what care a patient received. Import the documents once, then answer new patient and cohort questions from saved evidence. Each computed answer can be traced to source lines.

[Run it](#run-locally) · [System design](#system-design-whiteboard) · [HTTP contract](#http-contract-proposed) · [Scale plan](#design-for-500000) · [Validation](#why-these-designs-earned-their-place)

## Run locally

**macOS (Apple Silicon).** Download [the release ZIP](https://github.com/ShangChienLiu/backbone-clinical-review/releases/latest), unzip it, and double-click **Start.command**. The release includes its Python runtime and the saved demo database.

Choose **1** for the five original questions, **2** for a patient review, **3** for a collection review, **4** to import a document folder, or **5** to save the last review or full abstraction as JSON. Reviews, imports, and exports run locally. The working database and exports are under `.backbone/` next to the launcher. Use `--workspace /path/to/folder` to store them elsewhere.

**From source.** Run `python3 terminal.py` with Python 3.10 or newer. The guided terminal and the scriptable CLI share the same SQLite review engine. The bundled text files are ready to import. Text-layer PDFs use Poppler's `pdftotext`, basic DOCX extraction is supported, and scanned PDFs require OCR text.

```sh
python3 clinical.py --db review.sqlite ingest documents
python3 clinical.py --db review.sqlite answer questions.json
python3 clinical.py --db review.sqlite ask 'How much therapy did Rowan receive January 19–25, 2026?'
python3 clinical.py --db review.sqlite export > abstraction.json

# Later, import a folder containing the new delivery.
python3 clinical.py --db review.sqlite ingest /path/to/new-delivery
```

`ask --patient NAME --start YYYY-MM-DD --end YYYY-MM-DD` supplies an explicit scope. A question in `answer` may inherit the preceding question's patient and date window. Use [the saved combined database](outputs/combined.sqlite) for questions about the three-patient example.

## System design whiteboard

### What runs today

The write and read paths use one Python program and one SQLite file. Each CLI invocation runs in one process. A SQLite write transaction publishes the new source claims, revised encounters, and affected weeks together.

![Documents and questions use two Python paths and one shared SQLite ledger](docs/diagrams/system-design.png)

<details>
<summary>Mermaid source</summary>

```mermaid
flowchart TB
    files["New or corrected documents"] --> ingest
    question["Patient or cohort question"] --> review
    ingest["Ingest module<br/>Hash, extract, reconcile"] -->|Atomic update| ledger
    review["Review module<br/>Reuse saved weeks or evaluate options"] <-->|Scoped evidence| ledger
    review --> result["Verdict, numbers, and source lines"]
    ledger[("SQLite<br/>Claims, encounters, and weeks")]

    classDef entry fill:#e8f1ff,stroke:#31578a,color:#172841
    classDef store fill:#fff3d9,stroke:#967025,color:#392600
    classDef compute fill:#f4f6f8,stroke:#64748b,color:#152536
    class files,question,result entry
    class ledger store
    class ingest,review compute
```

</details>

- **Write.** SHA-256 identifies byte-identical inputs. A new alias is saved if the path changed. A new document is extracted into source-anchored claims. Encounter fingerprints identify affected projections. Plan and coverage changes can refresh a week without rewriting its encounters.
- **Read.** The query uses saved encounters and week results. A different date window or a stale algorithm version causes the week to be computed from stored claims and events. The numeric path runs Python and SQLite with zero model calls.

Code entry points are [guided terminal](terminal.py), [scriptable CLI](clinical.py), [ingest and storage](ledger.py), [encounter reconciliation](reconcile.py), and [review queries](review.py).

### Schema in the shipped SQLite file

![Five SQLite data tables, with keys and dependencies between claims and projections](docs/diagrams/schema.png)

<details>
<summary>Mermaid source</summary>

```mermaid
erDiagram
    direction LR
    DOCUMENTS ||--o{ ALIASES : "has paths"
    DOCUMENTS ||--o{ CLAIMS : "supplies"
    CLAIMS }o..o{ EVENTS : "supports"
    EVENTS }o..o{ PATIENT_WEEKS : "rolls up into"
    CLAIMS }o..o{ PATIENT_WEEKS : "supplies plan and coverage"

    DOCUMENTS {
        text digest PK
        text path
        text document_id
        text patient_id
        text patient
        text body
        text warnings
    }
    ALIASES {
        text path PK
        text digest PK,FK
    }
    CLAIMS {
        integer id PK
        text digest FK
        text patient_id
        text date
        text kind
        json payload
    }
    EVENTS {
        text patient_id PK
        text event PK
        text date
        json payload
        text input_hash
    }
    PATIENT_WEEKS {
        text patient_id PK
        text monday PK
        json payload
    }
```

</details>

JSON payloads are stored as SQLite text. `claims.payload` includes the assertion, role, verbatim excerpt, and source line. Plans, coverage and measures are claim kinds. `events.payload` records resolved fields and alternatives. `patient_weeks.payload` stores the result, coverage, witnesses and solver version. Dotted arrows show how results are derived. They are logical dependencies. The two foreign keys in the diagram point to `documents.digest`. A separate `meta` table records the extractor version. [SQLite DDL](ledger.py)

### HTTP contract (proposed)

The repository ships a CLI. These three routes describe a future HTTP adapter over the same ingest and review logic.

| Route | Request | Response |
| --- | --- | --- |
| `POST /v1/ingest-jobs` | `manifest_id` and `Idempotency-Key`. The manifest references immutable input objects and checksums in the caller's collection. | `202` with a job ID |
| `GET /v1/ingest-jobs/{id}` | Job ID | State, file errors, recomputation counts, and the committed revision when successful |
| `POST /v1/reviews` | Query type, inclusive date range, and patient ID for a patient review. Optional expected revision and evidence detail. | `200` with the read revision, typed result, and source anchors |

Review types map to the existing queries: `weekly_goal`, `dose_summary`, `day_reconstruction`, `symptom_evidence`, `plan_change`, and `consecutive_below_goal`. Evidence detail defaults to `summary`. Use `detail: "evidence"` for the full trace. The adapter validates authorization, the date range and the query type. A repeated ingest request with the same key and manifest returns the same job. Reusing the key with a different manifest returns `409`. The adapter checks an optional revision against the committed revision inside the same read snapshot used for the answer. A mismatch returns `409` so the caller can restart. The revision in a successful response identifies the snapshot actually read.

**Example patient review request**

```json
{
  "type": "weekly_goal",
  "patient_id": "HG-M042",
  "range": {"start": "2026-01-26", "end": "2026-01-30"},
  "detail": "evidence"
}
```

**Selected response fields**

```json
{
  "revision": "r17",
  "status": "cannot_determine",
  "coverage_complete": false,
  "goal": {"days": 3, "minutes": 150},
  "witnesses": [
    {"passes": false, "days": 3, "minutes": 145, "disputed_source": "BH-D111:7"},
    {"passes": true, "days": 3, "minutes": 155, "disputed_source": "BH-D110:7"}
  ]
}
```

The revision is illustrative. The two witnesses use the recorded signed alternatives. `disputed_source` identifies the assertion that changes the result. The full evidence response would include the remaining source anchors and evidence excerpts. [Computed DEV-03](outputs/development-answers.json)

### One review request, end to end

![A review request pins a database revision and returns saved or computed evidence](docs/diagrams/review-request.png)

<details>
<summary>Mermaid source</summary>

```mermaid
%%{init: {"sequence": {"mirrorActors": false}}}%%
sequenceDiagram
    autonumber
    actor U as Reviewer
    participant A as HTTP adapter (proposed)
    participant R as Review module
    participant D as Clinical store
    participant C as Certainty solver

    U->>A: POST /v1/reviews
    A->>A: Authorize and validate typed query
    A->>D: Open one consistent read snapshot and pin revision
    A->>R: Review scoped request
    R->>D: Read plan, coverage, events, and saved week
    D-->>R: Evidence and results at the pinned revision
    alt Saved week applies
        R->>R: Use its stored verdict and proof
    else New scope or stale solver version
        R->>C: Evaluate bounds and relevant alternatives
        C-->>R: Verdict and supporting witnesses
    end
    R-->>A: Result and source anchors
    A-->>U: 200 with revision and evidence
```

</details>

The proposed adapter keeps every read for one request in the same snapshot. If a signed correction commits while that request runs, its result finishes on the prior revision. A later request reads the new revision. The ingest worker declares a revision ready only after the associated projections commit. If a required source fails, the job reports the error and the previous revision stays active. A successful manifest tells us which files arrived. A shortfall still requires clinical coverage evidence for that week.

### Design for 500,000

**500,000 documents is a storage and ingestion problem.** The existing study measured one amendment in a **6,500-document** synthetic collection. Incremental update took **1.735 ms** locally versus **2,131.540 ms** for a fresh rebuild. The saved clinical outputs matched. Use immutable source objects and a manifest of changes so routine ingestion visits new deliveries. Keep indexed patient, date and week keys, and replay only affected projections. The current implementation still reads the affected patient's claims to regroup them. A 260-week chart measured **5.154 ms** for one amendment, making encounter-scoped lookup the next candidate if long charts dominate. These are local measurements on generated layouts. [Validation report](outputs/design-validation.md)

The 6,000-file synthetic run spent **3.82 of 5.07 seconds** in extraction in `ledger.ingest()`. If a 500,000- or million-file corpus has similar layouts, per-document extraction is the first cold-import bottleneck to investigate. Batch work outside the publish transaction and key extraction reuse by content and extractor version. A million-file run and production document mix still need measurement. [Synthetic scale measurements](outputs/synthetic-scale.json)

**500,000 requests needs a time window.** As a sizing example, **500,000 requests/day** is **5.8 requests/s** on average. A hypothetical 10× peak is about **58 requests/s**. Separate patient lookups from cohort scans in the load test. Measure HTTP p95, contention, import lag, and response size under concurrent writes. The local ingest benchmark does not establish an HTTP capacity or latency target.

![One HTTP API and an ingest worker publish and read revisions in a shared database](docs/diagrams/scale-topology.png)

<details>
<summary>Mermaid source</summary>

```mermaid
flowchart TB
    manifests["Source objects and change manifests"] --> jobs["Database-backed ingest jobs"]
    jobs --> worker["One ingest worker"]
    readers["Patient and cohort requests"] --> api["One HTTP API"]
    worker -->|Publish a revision| db
    api <-->|Read one revision| db
    db[("Clinical database<br/>Week results and source anchors")]

    classDef edge fill:#e8f1ff,stroke:#31578a,color:#172841
    classDef durable fill:#fff3d9,stroke:#967025,color:#392600
    classDef compute fill:#f4f6f8,stroke:#64748b,color:#152536
    class manifests,jobs,db durable
    class readers edge
    class worker,api compute
```

</details>

A database-backed job table is enough to start. The worker and API can share a deployment. Introduce each extra piece when the corresponding bottleneck is measured.

| Observed limit | Next change |
| --- | --- |
| Backfills delay requests or imports fall behind | Run the ingest worker separately. Track the oldest job and retry by manifest identity. |
| Long charts make updates expensive | Index claim dependencies by patient and encounter. Expand to other dates only for affected shared evidence or plan periods. |
| A single writer or shared-file deployment limits concurrency | Move the same tables and transaction semantics to PostgreSQL. Serialize concurrent changes for the same patient. |
| Cohort queries repeatedly read long event payloads | Read compact patient-week rows first. Page results at one revision and fetch supporting evidence for selected findings. |
| Identical queries still dominate the read budget | Cache by collection, access scope, data revision, normalized query, evidence detail, and solver version. |

The deployment preserves the same ingest and review interfaces as the workload grows. Each new process has a measured reason to exist.

## Why these designs earned their place

| Design | Evidence |
| --- | --- |
| **Preserve the source and recompute the projection** | A late departure amendment changes one encounter and one week. A byte-identical retransmission changes neither. An attendance coverage update can recompute one week and zero encounters. [Incremental work counters](outputs/benchmarks.json) |
| **Evaluate days and minutes in the same scenario** | A goal of 3 days and 150 minutes fails in both `(3, 145)` and `(2, 155)`. Separate bounds hide that fact. The joint solver agreed with exhaustive enumeration in **640/640** trials. The independent bounds baseline agreed in **520/640**. The difference came from the 120 deliberately correlated cases. [Validation study](outputs/design-validation.md) |
| **Finalize minutes once later choices cannot overlap them** | The solver preserves the attendance day and any clock positions that can still overlap. On a synthetic **16,384-combination** case, the previous solver reached its search limit. The new solver proved the shortfall in a median of **0.033 ms** and matched the exhaustive verdict. [Stress measurements](outputs/design-validation.json) |
| **Require coverage evidence for a shortfall** | In 120 withheld-document trials, treating the remaining records as complete caused 40 wrong definite verdicts. The coverage rule avoided all 40 and retained the 40 results already proven to meet the goal. [Validation study](outputs/design-validation.md) |

The case supporting a verdict is inspectable as dates, intervals and source lines. This is the prototype's use of explainable certain answers. [Amendola and Libkin, Explainable Certain Answers](https://ijcai.org/proceedings/2018/233)

## Results and reproducibility

- **Rowan.** 12 sessions on 11 days. The signed January 26 alternatives yield **585 or 595 minutes** overall. Jan 5 fell below goal and Jan 19 met it. Jan 12 lacks full coverage and Jan 26 spans the goal boundary. [Five development answers](outputs/development-review.md)
- **Multiple patients and a plan change.** [Collection review](outputs/evaluation-review.md) and [computed plan-change snapshot](outputs/plan-change.json) show the same abstraction handles the additional example charts.
- **Sources.** `documents/` has 31 development records. Two public mirrors match each other and both question files exactly match the provided `questions.json`. The organizer's Drive folder was unavailable. `evaluation-documents/` is a separate public example collection. [Pinned sources and checksums](source-manifest.json)
- **Tests and benchmarks.** The synthetic fixture schema has three fields, `id`, `add` and `expect`. The curated suite passed 23/23 deliveries. The development benchmark records initial and cached ingestion, warm and fresh-process queries, saved database size, and model use. [Fixture design](tests/edge_cases.md), [benchmark results](outputs/benchmarks.json)

```sh
python3 -m unittest discover -s tests -v
python3 edge_suite.py
python3 validate_design.py
sh reproduce.sh reproduced
```

## Limits and design review

Text extraction uses known document patterns. New layouts require a blinded recall and linkage study. The solver applies an explicit state limit and preserves an unresolved outcome if it cannot finish. Cohort uncertainty flags identify adjacent weeks for review. The current proof solver evaluates one weekly goal at a time.

Codex helped build and test the prototype. Gemini 3.8 Flash (Medium) independently reviewed the schema and the write/read diagrams. GPT‑OSS 120B (Medium) reviewed the API, job handling and concurrent reads. Both also reviewed the draft. The design uses one shared interface and checks the requested revision inside the same snapshot as its evidence reads. Proposed capacity estimates and deployment choices require a load test. Model-assisted fixture generation is documented separately. [Fixture generation record](tests/edge_cases.md)
