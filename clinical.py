"""Command-line interface for an auditable clinical record review."""
import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from ledger import database, dump, find_patient, ingest, patients
from review import route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("clinical.sqlite"))
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("ingest")
    load.add_argument("documents", type=Path)
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    for flag in ("patient", "start", "end"):
        ask.add_argument("--" + flag)
    batch = commands.add_parser("answer")
    batch.add_argument("questions", type=Path)
    commands.add_parser("export")
    commands.add_parser("patients")
    args = parser.parse_args()
    try:
        with closing(database(args.db, create=args.command == "ingest")) as db:
            if args.command == "ingest":
                output = ingest(db, args.documents)
            elif args.command == "ask":
                patient_id = find_patient(db, args.patient) if args.patient else None
                output, _ = route(db, args.question, patient_id=patient_id, start=args.start, end=args.end)
            elif args.command == "answer":
                context = None
                output = []
                for item in json.loads(args.questions.read_text()):
                    started = time.perf_counter()
                    answer, context = route(db, item["question"], context=context)
                    output.append({**item, "answer": answer, "elapsed_ms": (time.perf_counter() - started) * 1000})
            elif args.command == "export":
                output = dump(db)
            else:
                output = patients(db)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2 if args.command == "ingest" and output["errors"] else 0
    except (ValueError, OSError, sqlite3.Error) as err:
        print(str(err), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
