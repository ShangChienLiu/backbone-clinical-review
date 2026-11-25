"""Measure end-to-end behavior with synthetic replicas of the evaluation corpus."""
import argparse
import json
import statistics
import tempfile
import time
from contextlib import closing
from pathlib import Path

from ledger import database, ingest
from review import cohort_review, window_review


def elapsed_ms(operation):
    start = time.perf_counter()
    value = operation()
    return (time.perf_counter() - start) * 1000, value


def run(root, replicas, templates):
    docs = root / f"source-{replicas}"
    for index in range(replicas):
        folder = docs / f"{index:06}"
        folder.mkdir(parents=True)
        for original, note in templates.items():
            changed = note.replace("LR-58831", f"SYN-T{index:06}").replace("LR-60714", f"SYN-N{index:06}")
            (folder / original).write_text(changed)
    target = root / f"scale-{replicas}.sqlite"
    with closing(database(target, create=True)) as db:
        cold = ingest(db, docs)
        assert cold["new_documents"] == 24 * replicas and cold["duplicate_files"] == replicas
        assert cold["patient_replays"] == replicas * 2 and not cold["errors"]
        cached = ingest(db, docs)
        assert cached["new_documents"] == cached["patient_replays"] == 0
        cohort_ms, cohort = elapsed_ms(lambda: cohort_review(db, "2026-03-02", "2026-03-29"))
        times = [
            elapsed_ms(lambda: window_review(db, "SYN-T000000", "2026-03-02", "2026-03-29"))[0]
            for _ in range(30)]
    assert len(cohort["two_consecutive_below"]) == replicas
    assert len(cohort["eligibility_depends_on_unresolved_documentation"]) == replicas
    return {"synthetic": True, "patients": replicas * 2, "files": cold["files"],
            "unique_documents": cold["new_documents"], "cold_ingest_ms": round(cold["elapsed_seconds"] * 1000, 3),
            "cold_extraction_ms": round(cold["extraction_seconds"] * 1000, 3),
            "cold_replay_ms": round(cold["reconciliation_seconds"] * 1000, 3),
            "cached_full_tree_rescan_ms": round(cached["elapsed_seconds"] * 1000, 3),
            "single_patient_median_ms": round(statistics.median(times), 3),
            "collection_query_ms": round(cohort_ms, 3), "sqlite_bytes": target.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replicas", nargs="*", type=int, default=[10, 50, 250])
    args = parser.parse_args()
    if any(n < 1 for n in args.replicas):
        parser.error("Replica counts must be positive")
    templates = {x.name: x.read_text() for x in Path("evaluation-documents").glob("*.txt")}
    with tempfile.TemporaryDirectory(prefix="clinical-scale-") as temp:
        print(json.dumps([run(Path(temp), count, templates) for count in args.replicas], indent=2))


if __name__ == "__main__":
    main()
