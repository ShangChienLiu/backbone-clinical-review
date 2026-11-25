"""Run JSONL synthetic cases as ordered, restartable document deliveries."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from contextlib import closing
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from ledger import database, ingest
from review import symptom_review, window_review

PATIENT = "ZZ-M100"
START, END = "2026-04-06", "2026-04-12"
DEFAULT_CASES = Path(__file__).parent / "tests" / "edge_cases.jsonl"


def observe(db, keys):
    review = window_review(db, PATIENT, START, END)
    week, = review["weeks"]
    actual = {
        "sessions": week["sessions_confirmed"], "days": week["days_confirmed"],
        "minutes": week["minute_values"], "bounds": [week["minute_min"], week["minute_max"]],
        "goal": week["goal_status"], "complete": week["complete_week_claimed"],
        "encounters": {e["event"]: e["minute_values"] for e in review["events"]},
    }
    if "forms" in keys:
        assessments = symptom_review(db, PATIENT, START, END)["assessments"]
        actual["forms"] = sorted(
            [form, a["score"]] for a in assessments if a["instrument"] == "PHQ-9" for form in a["assessment_ids"])
    return {key: actual[key] for key in keys}


def validate(row, line):
    if set(row) != {"id", "add", "expect"} or not re.fullmatch(r"[a-z0-9-]+", row["id"]):
        raise ValueError(f"Invalid fixture identity or shape on line {line}")
    if not isinstance(row["add"], list) or not all(isinstance(d, str) and d.strip() for d in row["add"]):
        raise ValueError(f"Invalid documents on line {line}")
    if not isinstance(row["expect"], dict) or not row["expect"]:
        raise ValueError(f"Missing expectations on line {line}")


def evaluate(path):
    results = []
    known = defaultdict(set)
    with TemporaryDirectory(prefix="clinical-edges-") as folder:
        root = Path(folder)
        for line, row in enumerate(map(json.loads, path.read_text().splitlines()), 1):
            validate(row, line)
            ident = row["id"]
            batch = root / f"batch-{line}"
            batch.mkdir()
            target = root / f"{ident}.sqlite"
            expected = row["expect"]
            for index, doc in enumerate(row["add"], 1):
                (batch / f"{line}-{index}.txt").write_bytes(doc.encode())
            with closing(database(target, create=True)) as db:
                log = ingest(db, batch)
                retry = ingest(db, batch)
            # Opening a new connection uses the saved materialization.
            with closing(database(target)) as db:
                actual = observe(db, expected)
            digests = {hashlib.sha256(d.encode()).hexdigest() for d in row["add"]}
            new = len(digests - known[ident])
            known[ident].update(digests)
            counts = {key: log[key] for key in ("new_documents", "duplicate_files", "encounters_recomputed", "weeks_recomputed")}
            assert log["new_documents"] == new and not log["errors"], (ident, line, log)
            assert not any(retry[k] for k in ("new_documents", "encounters_recomputed", "weeks_recomputed")), retry
            if not new:
                assert counts["encounters_recomputed"] == counts["weeks_recomputed"] == 0, counts
            results.append({"id": ident, "line": line, "pass": expected == actual,
                            "expected": expected, "actual": actual, "work": counts})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="?", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args()
    results = evaluate(args.cases)
    failures = [r for r in results if not r["pass"]]
    print(json.dumps({
        "cases": len({r["id"] for r in results}), "steps": len(results),
        "passed_steps": len(results) - len(failures), "failures": failures, "results": results,
    }, indent=2, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
