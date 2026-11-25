"""Independent, seeded validation of the ledger and weekly certainty."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
import hashlib
from itertools import product
import json
from pathlib import Path
import platform
from random import Random
import shutil
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from certainty import determine
from ledger import database, ingest
from review import window_review

SEED = 20261125
START = date(2026, 4, 6)
END = START + timedelta(days=6)
DAYS = [(START + timedelta(days=i)).isoformat() for i in range(7)]
ANCHOR = {"document_id": "VA-D1", "path": "synthetic.txt", "line": 3}
UNRESOLVED = "cannot determine"


def elapsed(fn):
    start = perf_counter()
    value = fn()
    return (perf_counter() - start) * 1000, value


def option(day, minute=0, length=0):
    return {"date": DAYS[day] if day is not None else None,
            "intervals": [[minute, minute + length]] if length else [], "evidence": [ANCHOR]}


def chart(choices):
    return [{"event": f"VA-E{i}", "attendance": "unresolved", "joint_options": group}
            for i, group in enumerate(choices)]


def measure(by_visit):
    active_days = {visit["date"] for visit in by_visit if visit["date"]}
    occupied = {(visit["date"], minute) for visit in by_visit if visit["date"]
                for a, b in visit["intervals"] for minute in range(a, b)}
    return len(active_days), len(occupied)


def oracle(groups, target):
    values = {measure(world) for world in product(*groups)}
    outcomes = {days >= target["days"] and minutes >= target["minutes"] for days, minutes in values}
    return verdict(outcomes), {
        "days_confirmed": min(d for d, _ in values), "days_possible_upper": max(d for d, _ in values),
        "minute_min": min(m for _, m in values), "minute_max": max(m for _, m in values),
    }


def verdict(outcomes):
    return "met" if outcomes == {True} else "not met" if outcomes == {False} else UNRESOLVED


def run_solver(groups, bounds, target, complete=True):
    plan = {**ANCHOR, "value": target}
    return determine(chart(groups), bounds, plan, ANCHOR if complete else None,
                     complete, START.isoformat(), END.isoformat())


def independent_bounds(bounds, target):
    if bounds["days_confirmed"] >= target["days"] and bounds["minute_min"] >= target["minutes"]:
        return "met"
    if bounds["days_possible_upper"] < target["days"] or bounds["minute_max"] < target["minutes"]:
        return "not met"
    return UNRESOLVED


def check_witnesses(groups, target, proof):
    for label, witness in proof["witnesses"].items():
        world = witness["events"]
        assert len(world) == len(groups)
        assert all({k: v for k, v in a.items() if k != "event"} in legal
                   for a, legal in zip(world, groups))
        days, minutes = measure(world)
        assert (days, minutes) == (witness["days"], witness["minutes"])
        assert (days >= target["days"] and minutes >= target["minutes"]) == (label == "pass")
    return len(proof["witnesses"])


def exactness_trials():
    rng = Random(SEED)
    strata = {}
    for stratum, count in (("mixed", 400), ("correlated_shortfall", 120), ("true_boundary", 120)):
        results, methods = Counter(), Counter()
        for _ in range(count):
            if stratum == "mixed":
                groups = []
                for _ in range(rng.randint(2, 6)):
                    alternatives = [option(rng.randrange(5), rng.randrange(90), rng.randint(10, 65))
                                    for _ in range(rng.randint(1, 3))]
                    if rng.randrange(4) == 0:
                        alternatives.append(option(None))
                    groups.append(alternatives)
                target = {"days": rng.randint(1, 4), "minutes": rng.randint(30, 155)}
            else:
                first, second = rng.randint(30, 55), rng.randint(35, 65)
                borderline = rng.randint(30, 65)
                groups = [[option(0, 480, first)], [option(2, 480, second)],
                          [option(1, 650, borderline - 1),
                           option(0 if stratum == "correlated_shortfall" else 1, 650, borderline + 1)]]
                target = {"days": 3, "minutes": first + second + borderline}
            expected, bounds = oracle(groups, target)
            simple = independent_bounds(bounds, target)
            actual, proof = run_solver(groups, bounds, target)
            results["trials"] += 1
            results[f"oracle_{expected}"] += 1
            results["bounds_correct"] += simple == expected
            results["joint_correct"] += actual == expected
            results["extra_decisive"] += simple == UNRESOLVED and actual in {"met", "not met"}
            results["incorrect_definite"] += actual != expected and actual in {"met", "not met"}
            results["witnesses_checked"] += check_witnesses(groups, target, proof)
            methods[proof["method"]] += 1
        assert results["joint_correct"] == count, (stratum, dict(results))
        strata[stratum] = {**results, "methods": dict(methods)}
    return strata


def compression_trial():
    # Every choice matters to clock occupancy, but the repeated overlap admits
    # only a few distinct minute unions after each decision.
    groups = [[option(0, 480, 44), option(0, 480, 46)] for _ in range(17)]
    target = {"days": 1, "minutes": 45}
    brute_ms, (expected, bounds) = elapsed(lambda: oracle(groups, target))
    runs = [elapsed(lambda: run_solver(groups, bounds, target)) for _ in range(25)]
    assert all(result == expected and check_witnesses(groups, target, proof) == 2
               for _, (result, proof) in runs)
    return {"possible_combinations": 2 ** len(groups), "expected": expected,
            "exhaustive_ms_single_run": round(brute_ms, 3),
            "joint_median_ms_25_runs": round(median(t for t, _ in runs), 3)}


def active_mask_trial():
    groups = [[option(0, 350 + 10 * i, 2), option(0, 354 + 10 * i, 2)]
              for i in range(13)]
    groups += [[option(2, 480, 69)], [option(0, 660, 60), option(1, 660, 50)]]
    target = {"days": 3, "minutes": 150}
    oracle_ms, (expected, bounds) = elapsed(lambda: oracle(groups, target))
    assert expected == "not met" and independent_bounds(bounds, target) == UNRESOLVED
    trials = [elapsed(lambda: run_solver(groups, bounds, target)) for _ in range(25)]
    observed, proof = trials[0][1]
    assert all(r[0] == observed for _, r in trials)
    assert observed == expected and check_witnesses(groups, target, proof) == 1
    return {"possible_combinations": 2 ** 14, "expected": expected, "independent_bounds": UNRESOLVED,
            "actual": observed, "proof_method": proof["method"],
            "oracle_ms_single_run": round(oracle_ms, 3),
            "solver_median_ms_25_runs": round(median(ms for ms, _ in trials), 3),
            "witnesses": {k: [v["days"], v["minutes"]] for k, v in proof["witnesses"].items()}}


def withholding_trials():
    rng = Random(SEED + 1)
    stats = Counter()
    target = {"days": 3, "minutes": 150}
    for i in range(120):
        if i % 3 == 0:
            lengths = [rng.randint(45, 55), rng.randint(45, 55), rng.randint(60, 75)]
        elif i % 3 == 1:
            lengths = [rng.randint(24, 34) for _ in range(3)]
        else:
            lengths = [rng.randint(50, 60) for _ in range(3)] + [rng.randint(25, 35)]
        full = [[option(day, 480, length)] for day, length in enumerate(lengths)]
        true, full_bounds = oracle(full, target)
        assert run_solver(full, full_bounds, target)[0] == true
        partial = full[:-1]
        _, partial_bounds = oracle(partial, target)
        naive = run_solver(partial, partial_bounds, target, complete=True)[0]
        guarded = run_solver(partial, partial_bounds, target, complete=False)[0]
        stats["trials"] += 1
        stats["assumed_complete_false_definite"] += naive != UNRESOLVED and naive != true
        stats["guarded_false_definite"] += guarded != UNRESOLVED and guarded != true
        stats["guarded_unresolved"] += guarded == UNRESOLVED
        stats["guarded_retained_met"] += guarded == "met"
    assert stats["assumed_complete_false_definite"] == 40
    assert stats["guarded_false_definite"] == 0 and stats["guarded_retained_met"] == 40
    return dict(stats)


def build_documents(root, patients, weeks):
    original, update, all_sources = [root / name for name in ("base", "delta", "all")]
    for folder in (original, update, all_sources):
        folder.mkdir(parents=True)
    end = START + timedelta(days=weeks * 7 - 1)
    for p in range(patients):
        stem = f"p{p:04}"
        identity = f"Patient: Case {p:04} | MRN: VA-M{p:04}"
        files = {
            f"{stem}-plan.txt": f"Document ID: VA-D{p}P\nOutpatient treatment plan\n{identity}\n"
                                 f"Episode dates: {START} to {end}\n"
                                 "Weekly goal: 1 therapy day and 50 minutes of therapy.\nSigned: Synthetic clinician\n",
            f"{stem}-coverage.txt": f"Document ID: VA-D{p}C\nFinal attendance register\n{identity}\n"
                                     f"Coverage period: {START} to {end}. Complete attendance register.\nSigned: Synthetic staff\n",
        }
        for w in range(weeks):
            day = START + timedelta(days=w * 7)
            files[f"{stem}-e{w:04}.txt"] = (
                f"Document ID: VA-D{p}W{w}\nIndividual psychotherapy\n{identity}\n"
                f"Service date: {day} | Encounter VA-E{p}W{w}\n"
                "Patient contact: 09:00–09:45\nSigned: Synthetic clinician\n"
            )
        for name, body in files.items():
            (original / name).write_text(body)
            (all_sources / name).hardlink_to(original / name)
    amendment = (
        "Document ID: VA-D999999\nClinical addendum | Individual psychotherapy\n"
        "Patient: Case 0000 | MRN: VA-M0000\n"
        f"Applies to encounter VA-E0W0, service date {START}\n"
        "Corrected patient-contact: 09:00–09:55.\nSigned: Synthetic clinician\n"
    )
    (update / "z-amendment.txt").write_text(amendment)
    (all_sources / "z-amendment.txt").hardlink_to(update / "z-amendment.txt")
    return original, update, all_sources, end


def clinical_fingerprint(db, patients, end):
    fingerprint = hashlib.sha256()
    for index in range(patients):
        result = window_review(db, f"VA-M{index:04}", START.isoformat(), end.isoformat())
        assert len(result["events"]) == (end - START).days // 7 + 1
        if index == 0:
            assert result["weeks"][0]["goal_status"] == "met"
            assert result["weeks"][0]["minute_values"] == [55]
        fingerprint.update(json.dumps(result, sort_keys=True).encode())
    return fingerprint.hexdigest()


def scaling_trial(root, patients, weeks, repetitions):
    original, update, all_sources, end = build_documents(root, patients, weeks)
    baseline = root / "base.sqlite"
    with closing(database(baseline, create=True)) as db:
        base_ingest = ingest(db, original)
        assert not base_ingest["errors"]
        assert window_review(db, "VA-M0000", DAYS[0], DAYS[-1])["weeks"][0]["goal_status"] == "not met"
    modes = ("incremental", "full_patient_replay", "full_collection_rebuild")
    timings, counters, fingerprints = {m: [] for m in modes}, {}, set()
    for run in range(repetitions):
        order = modes[run % len(modes):] + modes[:run % len(modes)]
        for mode in order:
            target = root / f"{mode}-{run}.sqlite"
            if mode != "full_collection_rebuild":
                shutil.copyfile(baseline, target)
            with closing(database(target, create=True)) as db:
                if mode == "full_patient_replay":
                    db.execute("UPDATE events SET input_hash=NULL WHERE patient_id='VA-M0000'")
                    db.commit()
                ms, stats = elapsed(lambda: ingest(db, all_sources if mode == "full_collection_rebuild" else update))
                assert not stats["errors"]
            with closing(database(target)) as db:
                fingerprints.add(clinical_fingerprint(db, patients, end))
            target.unlink()
            timings[mode].append(ms)
            counters[mode] = {k: stats[k] for k in ("new_documents", "encounters_recomputed", "weeks_recomputed")}
    assert len(fingerprints) == 1
    assert counters["incremental"] == {"new_documents": 1, "encounters_recomputed": 1, "weeks_recomputed": 1}
    assert counters["full_patient_replay"]["encounters_recomputed"] == weeks
    return {"patients": patients, "weeks_per_patient": weeks,
            "base_documents": base_ingest["new_documents"], "clinical_snapshot_match": True,
            "modes": {m: {"median_ms": round(median(timings[m]), 3),
                          "trials_ms": [round(v, 3) for v in timings[m]], **counters[m]} for m in modes}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("Repetitions must be positive.")
    with TemporaryDirectory(prefix="backbone-design-validation-") as temp:
        root = Path(temp)
        output = {
            "seed": SEED, "python": platform.python_version(), "platform": platform.platform(),
            "exactness": exactness_trials(), "compression": compression_trial(), "withholding": withholding_trials(),
            "active_mask": active_mask_trial(),
            "incremental": [scaling_trial(root / f"p{p}-w{w}", p, w, args.repetitions)
                            for p, w in [(1, 12), (100, 12), (1, 260), (250, 24)]],
        }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
