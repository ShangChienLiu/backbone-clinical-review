"""Behavior of the saved abstraction over successive document deliveries."""
from contextlib import closing
from itertools import product
from random import Random
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from certainty import determine
from ledger import database, ingest
from review import window_review

PATIENT = "ZZ-M100"
START, END = "2026-04-06", "2026-04-12"
PLAN = (
    "Document ID: ZZ-D1\nOutpatient treatment plan\nPatient: Ellis Demo | MRN: ZZ-M100\n"
    "Episode dates: 2026-04-06 to 2026-04-12\n"
    "Weekly goal: 3 therapy days and 150 minutes of therapy.\nSigned: D. Provider\n"
)
REGISTER = (
    "Document ID: ZZ-D2\nFinal attendance register\nPatient: Ellis Demo | MRN: ZZ-M100\n"
    "Coverage period: 2026-04-06 to 2026-04-12. This is the complete attendance register.\n"
    "Signed: R. Provider\n"
)


def visit(doc, encounter, day, clock):
    return (
        f"Document ID: {doc}\nIndividual psychotherapy\nPatient: Ellis Demo | MRN: ZZ-M100\n"
        f"Service date: {day} | Encounter {encounter}\n"
        f"Patient contact: {clock}\nSigned: D. Provider\n"
    )


class SavedReviewTest(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.path = Path(workspace.name)
        self.ledger = self.path / "review.sqlite"
        self.batch = 0

    def deliver(self, *documents):
        self.batch += 1
        folder = self.path / str(self.batch)
        folder.mkdir()
        for index, text in enumerate(documents):
            (folder / f"{index}.txt").write_text(text)
        with closing(database(self.ledger, create=True)) as db:
            result = ingest(db, folder)
        self.assertEqual(result["errors"], [])
        return result

    def week(self):
        with closing(database(self.ledger)) as db:
            return window_review(db, PATIENT, START, END)["weeks"][0]

    def test_joint_timing_and_date_prove_a_shortfall(self):
        self.deliver(
            PLAN, REGISTER,
            visit("ZZ-D3", "ZZ-E1", START, "09:00–09:45"),
            visit("ZZ-D4", "ZZ-E2", "2026-04-08", "10:00–10:50"),
            visit("ZZ-D5", "ZZ-E3", START, "11:00–12:00"),
            visit("ZZ-D6", "ZZ-E3", "2026-04-07", "11:00–11:50"),
        )
        week = self.week()
        self.assertEqual(week["goal_status"], "not met")
        self.assertEqual(week["certainty"]["method"], "joint scenarios")
        self.assertFalse(week["certainty"]["pass_possible"])
        self.assertTrue(week["certainty"]["fail_possible"])
        self.assertIn(week["certainty"]["witnesses"]["fail"]["minutes"], (145, 155))
        self.assertTrue(any(c["evidence"] for c in week["certainty"]["witnesses"]["fail"]["events"]))

    def test_conflicting_duration_has_witnesses_on_both_sides(self):
        self.deliver(
            PLAN, REGISTER,
            visit("ZZ-D3", "ZZ-E1", START, "09:00–09:45"),
            visit("ZZ-D4", "ZZ-E2", "2026-04-08", "10:00–10:50"),
            visit("ZZ-D5", "ZZ-E3", "2026-04-07", "11:00–12:00"),
            visit("ZZ-D6", "ZZ-E3", "2026-04-07", "11:00–11:50"),
        )
        week = self.week()
        self.assertEqual(week["goal_status"], "cannot determine")
        self.assertEqual(week["certainty"]["witnesses"]["pass"]["minutes"], 155)
        self.assertEqual(week["certainty"]["witnesses"]["fail"]["minutes"], 145)
        for outcome in ("pass", "fail"):
            self.assertEqual(week["certainty"]["witnesses"][outcome]["days"], 3)

    def test_one_amendment_recomputes_one_encounter_and_one_week(self):
        unchanged = visit("ZZ-D4", "ZZ-E2", "2026-04-13", "10:00–10:30")
        original = visit("ZZ-D3", "ZZ-E1", START, "09:00–09:45")
        self.deliver(original, unchanged)
        self.assertEqual(self.week()["minute_values"], [45])
        amendment = (
            "Document ID: ZZ-D5\nClinical addendum | Individual psychotherapy\n"
            "Patient: Ellis Demo | MRN: ZZ-M100\n"
            "Applies to encounter ZZ-E1, service date 2026-04-06\n"
            "Corrected patient-contact: 09:00–09:35.\nSigned: D. Provider\n"
        )
        updated = self.deliver(amendment)
        self.assertEqual(updated["encounters_recomputed"], 1)
        self.assertEqual(updated["weeks_recomputed"], 1)
        self.assertEqual(self.week()["minute_values"], [35])
        retry = self.deliver(amendment)
        self.assertEqual(retry["new_documents"], 0)
        self.assertEqual(retry["encounters_recomputed"], 0)
        self.assertEqual(retry["weeks_recomputed"], 0)
        self.assertEqual(self.week()["minute_values"], [35])

    def test_episode_dates_do_not_attest_complete_attendance(self):
        self.deliver(PLAN, visit("ZZ-D3", "ZZ-E1", START, "09:00–09:45"))
        self.assertEqual(self.week()["plan"]["value"], {"days": 3, "minutes": 150})
        self.assertEqual(self.week()["goal_status"], "cannot determine")
        partial = REGISTER.replace("This is the complete attendance register.", "This is a partial attendance extract.")
        self.deliver(partial)
        self.assertEqual(self.week()["goal_status"], "cannot determine")
        self.deliver(REGISTER)
        self.assertEqual(self.week()["goal_status"], "not met")


def test_public_witness_solver_matches_exhaustive_unions():
    rng = Random(20260406)
    coverage = {"document_id": "ZZ-D2", "path": "completeness.txt", "line": 4}
    totals = {"days_confirmed": 0, "days_possible_upper": 7, "minute_min": 0, "minute_max": None}
    observed = set()
    for _ in range(160):
        target_days, target_minutes = rng.randint(1, 3), rng.randint(1, 50)
        plan = {**coverage, "value": {"days": target_days, "minutes": target_minutes}}
        events = []
        for event_id in range(rng.randint(1, 5)):
            options = [{"date": None, "intervals": [], "evidence": []}]
            if rng.choice([False, True]):
                options.clear()
            for _ in range(rng.randint(1, 3)):
                date = f"2026-04-{rng.randint(6, 8):02d}"
                start = rng.randrange(20)
                options.append({"date": date, "intervals": [[start, start + rng.randint(1, 20)]],
                                "evidence": []})
            events.append({"event": str(event_id), "attendance": "unresolved", "joint_options": options})

        possible = set()
        for combination in product(*(e["joint_options"] for e in events)):
            active = {a["date"] for a in combination if a["date"]}
            minute_union = {(a["date"], minute) for a in combination if a["date"]
                            for begin, end in a["intervals"] for minute in range(begin, end)}
            possible.add(len(active) >= target_days and len(minute_union) >= target_minutes)
        expected = "met" if possible == {True} else "not met" if possible == {False} else "cannot determine"
        actual, proof = determine(events, totals, plan, coverage, True, START, END)
        assert actual == expected and proof["pass_possible"] == (True in possible) and proof["fail_possible"] == (False in possible)
        observed.add(actual)
    assert observed == {"met", "not met", "cannot determine"}


class ScenarioOracleTest(unittest.TestCase):
    def test_compressed_states_match_independent_exhaustive_oracle(self):
        test_public_witness_solver_matches_exhaustive_unions()


if __name__ == "__main__":
    unittest.main()
