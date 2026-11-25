"""Run reproducibility checks and benchmark the two source collections."""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from ledger import database, get_claims, ingest
from reconcile import replay
from review import cohort_review, plan_changes, symptom_review, window_review

JAN = ("2026-01-05", "2026-01-30")
MARCH = ("2026-03-02", "2026-03-29")
ROWAN, THEO, NADIA = "HG-M042", "LR-58831", "LR-60714"


def timing_ms(callback):
    start = time.perf_counter_ns()
    answer = callback()
    return (time.perf_counter_ns() - start) / 1_000_000, answer


def distribution(values):
    values = sorted(values)
    rank = min(len(values) - 1, (len(values) * 95 + 99) // 100 - 1)
    return {"n": len(values), "median_ms": round(statistics.median(values), 3), "p95_ms": round(values[rank], 3)}


def check_rows(rowan, cohort, changes, symptoms):
    total = rowan["total"]
    assert (total["sessions_confirmed"], total["days_confirmed"], total["minute_values"]) == (12, 11, [585, 595])
    assert {key: row["confirmed"] for key, row in total["by_service"].items()} == {"family": 2, "group": 5, "individual": 5}
    assert [w["minute_values"] for w in rowan["weeks"]] == [[140], [120], [180], [145, 155]]
    assert rowan["episode_scope"]["value"] == list(JAN)
    assert [w["goal_status"] for w in rowan["weeks"]] == ["not met", "cannot determine", "met", "cannot determine"]
    assert [(e["date"], e["minute_values"]) for e in rowan["events"] if e["attendance"] == "attended"] == [
        ("2026-01-05", [50]), ("2026-01-06", [45]), ("2026-01-09", [45]),
        ("2026-01-12", [75]), ("2026-01-14", [45]),
        ("2026-01-19", [60]), ("2026-01-19", [30]), ("2026-01-21", [45]),
        ("2026-01-22", [45]), ("2026-01-26", [40, 50]), ("2026-01-29", [75]), ("2026-01-30", [30])]
    expected = {THEO: [[50], [199], [160], [165, 185]], NADIA: [[130], [50], [95], [80, 130]]}
    for p in cohort["patients"]:
        assert [w["minute_values"] for w in p["weeks"]] == expected[p["patient_id"]]
    assert [x["patient_id"] for x in cohort["two_consecutive_below"]] == [NADIA]
    assert [x["patient_id"] for x in cohort["eligibility_depends_on_unresolved_documentation"]] == [THEO]
    change, = changes
    assert change["effective_date"] == "2026-03-16"
    assert change["before"]["total"]["minute_values"] == [249]
    assert change["after"]["total"]["minute_values"] == [325, 345]
    indexed = {(r["date"], r["instrument"]): r for r in symptoms["assessments"]}
    assert indexed["2026-03-03", "PHQ-9"]["score"] == 17
    assert indexed["2026-03-03", "PHQ-9"]["retrospective_scores"] == [16]
    assert indexed["2026-03-24", "PHQ-9"]["score"] is None
    assert indexed["2026-03-24", "PHQ-9"]["draft_scores"] == [10]


def benchmark(source, root, name, query, repetitions):
    elapsed, extract_ms, reconcile_ms = [], [], []
    first_load = None
    for repeat in range(repetitions):
        target = root / f"{name}-{repeat}.sqlite"
        with closing(database(target, create=True)) as db:
            fresh = ingest(db, source)
        assert not fresh["errors"], fresh
        first_load = first_load or fresh
        elapsed.append(fresh["elapsed_seconds"] * 1000)
        extract_ms.append(fresh["extraction_seconds"] * 1000)
        reconcile_ms.append(fresh["reconciliation_seconds"] * 1000)
    target = root / f"{name}-0.sqlite"
    cached = []
    with closing(database(target)) as db:
        for _ in range(repetitions):
            ms, result = timing_ms(lambda: ingest(db, source))
            assert result["new_documents"] == result["patient_replays"] == result["claims_added"] == 0
            cached.append(ms)
        _ = query(db)
        warm = [timing_ms(lambda: query(db))[0] for _ in range(repetitions * 10)]
    reopened = []
    for _ in range(repetitions):
        ms, worker = timing_ms(lambda: subprocess.run(
            [sys.executable, "clinical.py", "--db", str(target), "ask",
             "Which patients had consecutive weeks below their plan, March 2–29, 2026?" if name == "evaluation" else
             "How much therapy did Rowan receive January 5–30, 2026?"],
            check=True, capture_output=True))
        assert json.loads(worker.stdout)
        reopened.append(ms)
    return target, {"files": first_load["files"], "unique_documents": first_load["new_documents"],
                    "duplicate_files": first_load["duplicate_files"], "claims": first_load["claims_added"],
                    "sqlite_bytes": target.stat().st_size, "cold_ingest": distribution(elapsed),
                    "cold_extraction": distribution(extract_ms), "cold_reconciliation": distribution(reconcile_ms),
                    "cached_ingest": distribution(cached), "warm_query": distribution(warm),
                    "separate_process_query": distribution(reopened),
                    "model_calls": 0, "model_tokens": 0, "model_api_cost_usd": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("Use at least one repetition")
    with tempfile.TemporaryDirectory(prefix="clinical-review-") as temporary:
        root = Path(temporary)
        rowan_path, rowan_metrics = benchmark(
            Path("documents"), root, "rowan", lambda db: window_review(db, ROWAN, *JAN), args.repetitions)
        evaluation_path, evaluation_metrics = benchmark(
            Path("evaluation-documents"), root, "evaluation", lambda db: cohort_review(db, *MARCH), args.repetitions)
        with closing(database(rowan_path)) as db:
            rowan = window_review(db, ROWAN, *JAN)
            measurements = symptom_review(db, ROWAN, *JAN)["assessments"]
            assert [(m["date"], m["score"]) for m in measurements] == [
                ("2026-01-05", 18), ("2026-01-16", 14), ("2026-01-30", 10)]
            assert measurements[1]["assessment_ids"] == ["HG-Q116"]
            assert {c["document_id"] for c in measurements[1]["sources"]} == {"BH-D013", "BH-D014"}
            original = get_claims(db, ROWAN)
            selected = {e["event"]: e for e in rowan["events"]}["HG-E110"]
            without_correction = next(e for e in replay([c for c in original if c["role"] != "correction"]) if e["event"] == selected["event"])
            assert selected["minute_values"] == [60] and without_correction["minute_values"] == [75]
            duplicate = root / "duplicate"
            duplicate.mkdir()
            shutil.copyfile(Path("documents") / selected["claims"][0]["path"], duplicate / "rescan.txt")
            duplicate_result = ingest(db, duplicate)
            assert duplicate_result["duplicate_files"] == 1 and duplicate_result["patient_replays"] == 0
            assert rowan == window_review(db, ROWAN, *JAN)
        # Create an independent db, ingest the correction after the other documents, and reopen it.
        copied = root / "before-correction"
        copied.mkdir()
        increment = root / "increment"
        increment.mkdir()
        correction_source = next(Path("documents").glob("*attendance_correction*"))
        for source in Path("documents").glob("*.txt"):
            shutil.copyfile(source, (increment if source == correction_source else copied) / source.name)
        incremental_db = root / "incremental.sqlite"
        with closing(database(incremental_db, create=True)) as db:
            ingest(db, copied)
            old = window_review(db, ROWAN, *JAN)
            assert old["total"]["minute_values"] == [585, 595]  # overlapping times are counted once
            old_group = next(e for e in old["events"] if e["event"] == selected["event"])
            assert old_group["minute_values"] == [75]
            fixed = ingest(db, increment)
            assert fixed["new_documents"] == fixed["patient_replays"] == 1
            assert fixed["encounters_recomputed"] == fixed["weeks_recomputed"] == 1
        with closing(database(incremental_db)) as db:
            assert rowan == window_review(db, ROWAN, *JAN)
        with closing(database(incremental_db)) as db:
            addendum = root / "new-contact-addendum"
            addendum.mkdir()
            (addendum / "addendum.txt").write_text(
                "Document ID: TEST-D1\n"
                "Clinical addendum | Individual psychotherapy\n"
                "Rowan Mercer | MRN: HG-M042\n"
                "Applies to encounter HG-E115, service date January 26, 2026\n"
                "Corrected patient-contact: 09:10–09:50. Replaces the original starting time.\n"
                "Electronically signed: Test clinician, January 31, 2026\n"
            )
            assert ingest(db, addendum)["new_documents"] == 1
            revised = window_review(db, ROWAN, *JAN)
            assert revised["total"]["minute_values"] == [585]
            assert revised["weeks"][-1]["goal_status"] == "cannot determine"
            assert not revised["weeks"][-1]["complete_week_claimed"]
            final_week_register = root / "confirmed-final-week-register"
            final_week_register.mkdir()
            (final_week_register / "register.txt").write_text(
                "Document ID: TEST-D2\nFinal attendance register\n"
                "Rowan Mercer | MRN: HG-M042\n"
                "Coverage period: 2026-01-26 to 2026-01-30. Complete attendance register.\n"
                "Signed: Test clinician, January 31, 2026\n"
            )
            coverage_update = ingest(db, final_week_register)
            assert coverage_update["encounters_recomputed"] == 0
            assert coverage_update["weeks_recomputed"] == 1
            assert window_review(db, ROWAN, *JAN)["weeks"][-1]["goal_status"] == "not met"
            assert revised["total"]["sessions_confirmed"] == 12
        with closing(database(evaluation_path)) as db:
            cohort = cohort_review(db, *MARCH)
            changes = plan_changes(db, THEO, *MARCH)
            symptoms = symptom_review(db, NADIA, "2026-02-20", MARCH[1])
        check_rows(rowan, cohort, changes, symptoms)
        print(json.dumps({
            "environment": {"python": platform.python_version(), "system": platform.platform(), "processor": platform.machine()},
            "corpora": {"rowan_development": rowan_metrics, "external_evaluation": evaluation_metrics},
            "correction_ablation": {
                "encounter": selected["event"], "cross_checked_minutes": 60,
                "with_field_specific_correction": selected["minute_values"],
                "without_correction": without_correction["minute_values"],
                "absolute_error_minutes": 15
            },
            "incremental_work": {
                "late_departure_amendment": {key: fixed[key] for key in ("new_documents", "encounters_recomputed", "weeks_recomputed", "elapsed_seconds")},
                "complete_register": {key: coverage_update[key] for key in ("new_documents", "encounters_recomputed", "weeks_recomputed", "elapsed_seconds")},
                "duplicate_retransmission": {key: duplicate_result[key] for key in ("new_documents", "encounters_recomputed", "weeks_recomputed", "elapsed_seconds")},
            },
            "assertions": {
                "reference_review_results": "passed", "byte_duplicate_invariance": "passed",
                "incremental_correction_and_restart": "passed", "synthetic_signed_contact_addendum": "passed",
                "incremental_scope_update": "passed",
                "two_cohort_classifications": "passed",
                "plan_change_and_distinct_assessments": "passed"
            }
        }, indent=2))


if __name__ == "__main__":
    main()
