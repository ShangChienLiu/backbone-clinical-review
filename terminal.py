"""Guided terminal for the Backbone take-home."""

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from ledger import database, dump, get_claims, ingest, patients
from review import cohort_review, plan_changes, route, symptom_review, window_review

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent.parent
else:
    ROOT = Path(__file__).resolve().parent

MENU = """
Backbone · Clinical review
  1  Development questions
  2  Patient review
  3  Collection review
  4  Import documents
  5  Export JSON
  0  Exit"""

DEV_TITLES = ("Therapy sessions", "Therapy minutes", "Weekly goal",
              "Reconstruct Jan 19 and Jan 21", "Symptoms and assessments")


def open_workspace(folder):
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "clinical.sqlite"
    if not target.exists():
        # Publish the complete bundled snapshot even if two launchers start together.
        with tempfile.NamedTemporaryFile(dir=folder) as snapshot:
            with (ROOT / "outputs" / "combined.sqlite").open("rb") as source:
                shutil.copyfileobj(source, snapshot)
            snapshot.flush()
            try:
                os.link(snapshot.name, target)
            except FileExistsError:
                pass
    return database(target)


def minutes(record):
    low, high = record["minute_min"], record["minute_max"]
    options = record["minute_values"]
    if len(options) == 2:
        return f"{options[0]:g} or {options[1]:g} min"
    if high is None:
        return f"at least {low:g} min (upper bound unknown)"
    return f"{low:g} min" if low == high else f"{low:g}–{high:g} min"


def confirmed_count(low, high):
    if low == high:
        return str(low)
    if high is None:
        return f"at least {low} confirmed"
    return f"{low} confirmed (up to {high})"


def show_dose(record):
    sessions = record["sessions_confirmed"]
    session_upper = sessions + record["sessions_possible_additional"]
    days, day_upper = record["days_confirmed"], record["days_possible_upper"]
    session_label = "session" if sessions == session_upper == 1 else "sessions"
    day_label = "day" if days == day_upper == 1 else "days"
    print(f'{confirmed_count(sessions, session_upper)} {session_label}'
          f' · {confirmed_count(days, day_upper)} therapy {day_label} · {minutes(record)}')


def citation(source):
    return f'{source["document_id"] or source["path"]}:{source["line"]}'


def show_weeks(result, goals=True):
    for week in result["weeks"]:
        print(f'  Week of {week["monday"]}  {week["days_confirmed"]} confirmed days'
              f' · {minutes(week)}', end="")
        if not goals:
            print()
            continue
        print(f' · {week["goal_status"]}')
        if week["plan"]:
            goal = week["plan"]["value"]
            print(f'    Goal {goal["days"]} days / {goal["minutes"]:g} min [{citation(week["plan"])}]')
        proof = week["certainty"]
        if proof["pass_possible"] and proof["fail_possible"]:
            print("    The available evidence permits outcomes on both sides of the goal.")
        if week["coverage"] and proof["method"] == "exhaustive upper bounds":
            print(f'    Attendance coverage [{citation(week["coverage"])}]')
        for note in week["limitations"]:
            print(f"    {note}")


def show_event(event, detail=False):
    print(f'  {event["date"] or "Date unresolved"} {event["event"]} · {event["service"]}'
          f' · {event["attendance"]} · {minutes(event)}')
    for issue in event["issues"]:
        print(f"    {issue}")
    relevant = (claim for claim in event["claims"] if claim["kind"] != "observation")
    sources = {}
    for claim in relevant:
        sources.setdefault(citation(claim), claim["quote"])
    if detail:
        for anchor, quote in sources.items():
            print(f"    [{anchor}] {quote}")
    elif sources:
        print("    Sources " + ", ".join(sources))


def show_assessments(result, with_observations=False):
    if not result["assessments"]:
        print("No assessments documented in this window.")
    for item in result["assessments"]:
        score = item["score"] if item["score"] is not None else item["status"]
        anchors = ", ".join(dict.fromkeys(citation(source) for source in item["sources"]))
        print(f'  {item["date"]} {item["instrument"]} {score} [{anchors}]')
    for issue in result["issues"]:
        print(f"  {issue}")
    if with_observations:
        for claim in result["observations"]:
            print(f'  {claim["date"]} [{citation(claim)}] {claim["quote"]}')
    elif result["observations"]:
        print("  Symptom notes and source quotes are available in the JSON export.")


def development(db):
    for number, title in enumerate(DEV_TITLES, 1):
        print(f"  {number}  {title}")
    pick = input("Question [Enter to return] ").strip()
    if not pick:
        return None
    if pick not in {"1", "2", "3", "4", "5"}:
        raise ValueError("Choose a question from 1 to 5.")
    questions = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
    item = questions[int(pick) - 1]
    first, context = route(db, questions[0]["question"])
    result = first if pick == "1" else route(db, item["question"], context=context)[0]
    print(f'\n{item["id"]} · {DEV_TITLES[int(pick) - 1]}')
    if pick == "1":
        show_dose(result["total"])
        for service, counts in result["total"]["by_service"].items():
            print(f'  {service}: {counts["confirmed"]} confirmed')
        print(f'  Scope: {result["scope"]}.')
        for event in result["events"]:
            if event["issues"] or event["attendance"] != "attended":
                show_event(event)
    elif pick == "2":
        show_dose(result["total"])
        low, high = result["total"]["hour_min"], result["total"]["hour_max"]
        hours = f"{low:.2f}" if low == high else (
            f"{low:.2f}–{high:.2f}" if high is not None else f"at least {low:.2f}"
        )
        print(f"  Overall: {hours} hours")
        show_weeks(result, goals=False)
        for event in result["events"]:
            if event["minute_min"] != event["minute_max"]:
                show_event(event)
    elif pick == "3":
        show_weeks(result)
    elif pick == "4":
        for day in result["days"]:
            print(day["window"][0])
            show_dose(day["total"])
            for event in day["events"]:
                show_event(event, detail=True)
    else:
        show_assessments(result)
        signed = [claim for claim in result["observations"] if claim["role"] == "signed"]
        if signed:
            print("  Episode notes")
            for claim in (signed[0], signed[-1]) if len(signed) > 1 else signed:
                print(f'  {claim["date"]} [{citation(claim)}] {claim["quote"]}')
        if result["focus_observations"]:
            print("  January 19 contact")
        for claim in result["focus_observations"]:
            print(f'  [{citation(claim)}] {claim["quote"]}')
        print("  These records show the documented course. Attributing change to one treatment would require other evidence.")
    return {**item, "answer": result}


def input_window(db, patient_id=None):
    sql = "SELECT MIN(date), MAX(date) FROM claims"
    if patient_id:
        default = db.execute(sql + " WHERE patient_id=?", (patient_id,)).fetchone()
        for episode in get_claims(db, patient_id, ("episode",)):
            first, last = episode["value"]
            default = min(default[0] or first, first), max(default[1] or last, last)
    else:
        default = db.execute(sql).fetchone()
    dates = []
    for label, fallback in zip(("Start", "End"), default):
        value = input(f"{label} [{fallback or 'YYYY-MM-DD'}] ").strip() or fallback
        try:
            dates.append(date.fromisoformat(value or "").isoformat())
        except ValueError:
            raise ValueError(f"{label} must be a valid date (YYYY-MM-DD).") from None
    if dates[0] > dates[1]:
        raise ValueError("Start must be on or before end.")
    return dates


def patient_review(db):
    available = list(patients(db).items())
    for index, (key, names) in enumerate(available, 1):
        print(f"  {index}  {', '.join(names)} ({key})")
    selection = input("Patient number [Enter to return] ").strip()
    if not selection:
        return None
    if not selection.isdecimal() or not 1 <= int(selection) <= len(available):
        raise ValueError("Choose a listed patient number.")
    patient_id, _ = available[int(selection) - 1]
    start, end = input_window(db, patient_id)
    print("  1  Therapy and goals    2  Symptoms    3  Plan changes")
    selection = input("Review [1] ").strip() or "1"
    if selection == "1":
        result = window_review(db, patient_id, start, end)
        show_dose(result["total"])
        print(f'  Scope: {result["scope"]}.')
        show_weeks(result)
    elif selection == "2":
        result = symptom_review(db, patient_id, start, end)
        show_assessments(result, with_observations=True)
    elif selection == "3":
        result = {"patient_id": patient_id, "window": [start, end],
                  "changes": plan_changes(db, patient_id, start, end)}
        if not result["changes"]:
            print("No plan changes documented in this window.")
        for change in result["changes"]:
            print(f'  Effective {change["effective_date"]}')
            for label, key in (("Before", "previous_plan"), ("After", "new_plan")):
                plan = change[key]
                print(f'    {label}: {plan["value"]["days"]} days / {plan["value"]["minutes"]:g} min '
                      f'[{citation(plan)}]')
    else:
        raise ValueError("Choose a review from 1 to 3.")
    return result


def collection_review(db):
    result = cohort_review(db, *input_window(db), compact=True)
    print(f'  Patients with encounters: {len(result["patients"])}')
    for label, key in (
        ("Two consecutive weeks below goal", "two_consecutive_below"),
        ("Depends on unresolved evidence", "eligibility_depends_on_unresolved_documentation"),
    ):
        print(f"  {label}: {len(result[key])}")
        for item in result[key]:
            for pair in item["pairs"]:
                print(f'    {", ".join(item["names"])} · {pair["monday_first"]} and {pair["monday_second"]}'
                      f' · {" / ".join(pair["statuses"])}')
    return result


def input_path(value):
    path = Path(value).expanduser()
    if path.exists():
        return path
    try:
        parts = shlex.split(value)
    except ValueError:
        return path
    return Path(parts[0]).expanduser() if len(parts) == 1 else path


def import_documents(db):
    source = input("Document folder path [Enter to return] ").strip()
    if not source:
        return
    report = ingest(db, input_path(source))
    reused = report["cached_files"] + report["duplicate_files"]
    stored = report["stored_without_evidence"]
    new_without = report["new_documents"] - report["new_with_evidence"]
    print(f'  {report["new_with_evidence"]} new with evidence · {new_without} new without evidence'
          f' · {reused} reused · {len(report["errors"])} failed')
    print(f'  Recomputed {report["encounters_recomputed"]} encounters and {report["weeks_recomputed"]} weeks')
    reasons = {"missing_patient_id": "missing patient identifier", "no_supported_claims": "no supported claims"}
    for entry in stored:
        origin = "Reused" if entry["reused"] else "Stored"
        print(f'  {origin} without evidence: {entry["path"]} ({reasons[entry["reason"]]})')
    for error in report["errors"]:
        print(f'  {error["path"]}: {error["error"]}')
    flagged = {entry["path"] for entry in stored}
    for warning in report["warnings"]:
        if warning["path"] not in flagged:
            print(f'  {warning["path"]}: {"; ".join(warning["warnings"])}')


def export_json(db, folder, latest):
    print("  1  Last review    2  Full abstraction" if latest is not None else "  2  Full abstraction")
    pick = input("Export [Enter to return] ").strip()
    if not pick:
        return
    if pick == "1" and latest is not None:
        label, payload = "review", latest
    elif pick == "2":
        label, payload = "abstraction", dump(db)
    else:
        raise ValueError("Choose a listed export.")
    exports = folder / "exports"
    exports.mkdir(exist_ok=True)
    path = exports / f"{label}-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Saved {path}")


def menu(db, folder):
    history = None
    reviews = {"1": development, "2": patient_review, "3": collection_review}
    while True:
        print(MENU)
        selection = input("Choose 0–5 ").strip()
        try:
            if selection == "0":
                return
            if selection in reviews:
                result = reviews[selection](db)
                if result is not None:
                    history = result
            elif selection == "4":
                import_documents(db)
            elif selection == "5":
                export_json(db, folder, history)
            else:
                print("Choose a number from 0 to 5.")
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"  {error}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT / ".backbone",
                        help="folder for the working database and exports")
    args = parser.parse_args()
    try:
        folder = args.workspace.expanduser().resolve()
        with closing(open_workspace(folder)) as db:
            menu(db, folder)
    except (EOFError, KeyboardInterrupt):
        print()
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Backbone could not start. {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
