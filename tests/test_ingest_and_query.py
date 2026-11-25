"""Regression cases curated from independent model-generated drafts."""

import json
from contextlib import closing
from pathlib import Path
from subprocess import run
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

from ledger import database, get_events, ingest
from review import cohort_review, route, window_review
from test_incremental_certainty import PATIENT, PLAN, visit

ROOT = Path(__file__).resolve().parents[1]
WEEKS = ("2026-04-06", "2026-04-19")
REGISTER = (
    "Document ID: ZZ-D900\nFinal attendance register\nPatient: Ellis Demo | MRN: ZZ-M100\n"
    "Coverage period: 2026-04-06 to 2026-04-19. This is the complete attendance register.\nSigned: D. Provider\n"
)


class SavedLedgerTest(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "clinical.sqlite"
        self.delivery = self.root / "delivery"
        self.delivery.mkdir()
        self.db = database(self.target, create=True)
        self.addCleanup(self.db.close)

    def add(self, name, text):
        (self.delivery / name).write_text(text, encoding="utf-8")

    def test_mixed_batch_reports_reused_unusable_and_retries_corrupt_docx(self):
        bad = self.delivery / "a-corrupt.docx"
        bad.write_bytes(b"This is not a ZIP archive.")
        self.add("b-no-id.txt", "Appointment note, 2026-04-06. Patient contact: 09:00-09:50.")
        self.add("c-no-claims.txt", "Patient: Ellis Demo | MRN: ZZ-M100\nTicket delivered to office.")
        self.add("d-visit.txt", visit("ZZ-D101", "ZZ-E101", "2026-04-06", "09:00-09:50"))

        first = ingest(self.db, self.delivery)
        self.assertEqual(first["new_documents"], 3)
        self.assertEqual(first["new_with_evidence"], 1)
        self.assertEqual([error["path"] for error in first["errors"]], [bad.name])
        self.assertEqual(first["stored_without_evidence"], [
            {"path": "b-no-id.txt", "reason": "missing_patient_id", "reused": False},
            {"path": "c-no-claims.txt", "reason": "no_supported_claims", "reused": False},
        ])
        self.assertEqual(window_review(self.db, PATIENT, *WEEKS)["total"]["minute_values"], [50])

        self.add("e-copy.txt", (self.delivery / "c-no-claims.txt").read_text(encoding="utf-8"))
        self.assertEqual(ingest(self.db, self.delivery)["patient_replays"], 0)
        with ZipFile(bad, "w") as document:
            paragraphs = visit("ZZ-D102", "ZZ-E102", "2026-04-08", "09:00-09:40").splitlines()
            xml = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in paragraphs)
            document.writestr("word/document.xml",
                              '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                              f"<w:body>{xml}</w:body></w:document>")

        repaired = ingest(self.db, self.delivery)
        self.assertEqual(repaired["errors"], [])
        self.assertEqual((repaired["new_with_evidence"], repaired["cached_files"]), (1, 4))
        self.assertEqual([entry["path"] for entry in repaired["stored_without_evidence"]],
                         ["b-no-id.txt", "c-no-claims.txt", "e-copy.txt"])
        self.assertTrue(all(entry["reused"] for entry in repaired["stored_without_evidence"]))
        self.assertEqual(window_review(self.db, PATIENT, *WEEKS)["total"]["minute_values"], [90])

    def test_medication_query_does_not_fall_through_to_therapy_summary(self):
        self.add("one.txt", visit("ZZ-D101", "ZZ-E101", "2026-04-06", "09:00-09:50"))
        self.add("two.txt", visit("ZZ-D102", "ZZ-E102", "2026-04-08", "09:00-09:40"))
        ingest(self.db, self.delivery)
        result, _ = route(self.db, "Week of 2026-04-06: total therapy minutes? I am not asking about medications.")
        self.assertEqual(result["total"]["minute_values"], [90])
        self.assertEqual([e["event"] for e in result["events"]], ["ZZ-E101", "ZZ-E102"])
        result, _ = route(self.db, "Week of 2026-04-06: total therapy minutes? I am not asking about symptoms.")
        self.assertEqual(result["total"]["minute_values"], [90])

        for question in (
            "Which medication doses were changed at this week's visits?",
            "What is the diagnosis code?",
            "Which therapist treated Ellis's symptoms?",
            "How many therapy sessions did Ellis attend, and what medications were prescribed?",
            "Which patients had three consecutive weeks below goal?",
        ):
            with self.subTest(question=question):
                response = run([sys.executable, ROOT / "clinical.py", "--db", self.target, "ask",
                                question, "--start", WEEKS[0], "--end", WEEKS[1]],
                               text=True, capture_output=True, check=False)
                self.assertEqual(response.returncode, 3)
                self.assertIn("Unsupported question", response.stderr)
                self.assertEqual(response.stdout, "")

    def test_named_patient_and_week_override_incidental_context(self):
        self.add("one.txt", visit("ZZ-D101", "ZZ-E101", "2026-04-06", "09:00-09:50"))
        self.add("two.txt", visit("ZZ-D201", "ZZ-E201", "2026-04-07", "10:00-10:45")
                 .replace("Ellis Demo | MRN: ZZ-M100", "Beta Test | MRN: ZZ-M200"))
        ingest(self.db, self.delivery)
        _, prior = route(self.db, "ZZ-M100 week of 2026-04-06: visits and minutes?")
        for prefix in ("For ZZ-M200 (not ZZ-M100)", "Not ZZ-M100. I mean ZZ-M200"):
            checked, _ = route(self.db, f"{prefix}, week of 2026-04-06: visits and minutes?", context=prior)
            self.assertEqual(checked["patient_id"], "ZZ-M200")
            self.assertEqual(checked["window"], ["2026-04-06", "2026-04-12"])
            self.assertEqual(checked["total"]["minute_values"], [45])
        empty, _ = route(self.db,
                         "ZZ-M200, week of 2026-03-30 (the week before the 2026-04-07 session): visits and minutes?",
                         context=prior)
        self.assertEqual(empty["window"], ["2026-03-30", "2026-04-05"])
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["weeks"][0]["goal_status"], "cannot determine")
        with self.assertRaisesRegex(ValueError, "conflicts with --patient"):
            route(self.db, "ZZ-M200 week of 2026-04-06: visits and minutes?", patient_id=PATIENT)

    def prepare_cohort(self, minutes_by_day):
        self.add("plan.txt", PLAN.replace("2026-04-12", "2026-04-19"))
        self.add("register.txt", REGISTER)
        for i, (day, minutes) in enumerate(minutes_by_day, 1):
            finish = f"{9 + minutes // 60:02d}:{minutes % 60:02d}"
            self.add(f"visit-{i}.txt", visit(f"ZZ-D{i}", f"ZZ-E{i:03d}", f"2026-04-{day:02d}", f"09:00-{finish}"))
        ingest(self.db, self.delivery)

    def compare_cohort(self, start, end):
        full = cohort_review(self.db, start, end)
        compact = cohort_review(self.db, start, end, compact=True)
        for key in ("two_consecutive_below", "eligibility_depends_on_unresolved_documentation"):
            self.assertEqual(compact[key], full[key])
        self.assertEqual([x["patient_id"] for x in compact["patients"]], [x["patient_id"] for x in full["patients"]])
        return compact

    def test_compact_cohort_reuses_exact_weeks_and_rechecks_clipped_weeks(self):
        self.prepare_cohort([(6, 50), (8, 75), (10, 75), (13, 45), (14, 45), (15, 45), (17, 15)])
        full = self.compare_cohort(*WEEKS)
        self.assertFalse(full["two_consecutive_below"])
        with patch("review.get_events", side_effect=AssertionError("Cached cohort should not load encounter payloads")):
            self.assertEqual(cohort_review(self.db, *WEEKS, compact=True), full)

        clipped = self.compare_cohort("2026-04-08", "2026-04-15")
        self.assertFalse(clipped["two_consecutive_below"])
        patient, = clipped["eligibility_depends_on_unresolved_documentation"]
        pair, = patient["pairs"]
        self.assertEqual(pair["statuses"], ["cannot determine", "cannot determine"])
        self.assertEqual(pair["events_first"] + pair["events_second"], ["ZZ-E002", "ZZ-E003", "ZZ-E004", "ZZ-E005", "ZZ-E006"])

    def test_compact_cohort_rejects_stale_solver_results(self):
        self.prepare_cohort([(6, 50), (8, 50), (13, 50), (14, 50), (16, 40)])
        week = "2026-04-13"
        payload, = self.db.execute("SELECT payload FROM patient_weeks WHERE monday=?", (week,)).fetchone()
        stale = json.loads(payload)
        stale["goal_status"] = "met"
        stale["certainty"]["algorithm_version"] = 0
        del stale["events"]
        del stale["review_start"]
        self.db.execute("UPDATE patient_weeks SET payload=? WHERE monday=?", (json.dumps(stale), week))
        result = self.compare_cohort(*WEEKS)
        patient, = result["two_consecutive_below"]
        pair, = patient["pairs"]
        self.assertEqual(pair["statuses"], ["not met", "not met"])

    def test_compact_cohort_reads_one_batch_and_handles_missing_rows(self):
        self.prepare_cohort([(6, 50), (8, 50), (13, 50), (14, 50), (16, 40)])
        for source in sorted(self.delivery.glob("*.txt")):
            self.add("other-" + source.name, source.read_text(encoding="utf-8")
                     .replace("Ellis Demo | MRN: ZZ-M100", "Beta Test | MRN: ZZ-M200"))
        ingest(self.db, self.delivery)
        expected = self.compare_cohort(*WEEKS)
        self.assertEqual(len(expected["two_consecutive_below"]), 2)
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            self.assertEqual(cohort_review(self.db, *WEEKS, compact=True), expected)
        finally:
            self.db.set_trace_callback(None)
        reads = [sql for sql in statements if "FROM patient_weeks" in sql]
        self.assertEqual(len(reads), 1)
        self.assertIn("json_extract", reads[0])

        self.db.execute("DELETE FROM patient_weeks WHERE patient_id=?", (PATIENT,))
        self.assertEqual(cohort_review(self.db, *WEEKS, compact=True), expected)
        self.db.execute("DELETE FROM patient_weeks")
        self.assertEqual(cohort_review(self.db, *WEEKS, compact=True), expected)
        with patch("review._projected_week_rows", return_value=None):
            self.assertEqual(cohort_review(self.db, *WEEKS, compact=True), expected)

    def test_compact_cohort_keeps_fallback_on_same_read_snapshot(self):
        self.prepare_cohort([(6, 50), (8, 50), (13, 50), (14, 50), (16, 40)])
        self.db.execute("PRAGMA journal_mode=WAL")
        expected = self.compare_cohort(*WEEKS)
        self.assertTrue(expected["two_consecutive_below"])
        with self.db:
            self.db.execute("DELETE FROM patient_weeks WHERE monday='2026-04-13'")
        amendment = self.root / "amendment"
        amendment.mkdir()
        (amendment / "correction.txt").write_text(
            "Document ID: ZZ-D199\nClinical addendum | Individual psychotherapy\n"
            "Patient: Ellis Demo | MRN: ZZ-M100\n"
            "Applies to encounter ZZ-E005, service date 2026-04-16\n"
            "Corrected patient-contact: 09:00-09:55.\nSigned: D. Provider\n", encoding="utf-8")
        applied = False

        def read_and_publish(db, patient, start, end):
            nonlocal applied
            if db is self.db and not applied:
                applied = True
                with closing(database(self.target)) as writer:
                    self.assertEqual(ingest(writer, amendment)["weeks_recomputed"], 1)
            return get_events(db, patient, start, end)

        with patch("review.get_events", side_effect=read_and_publish):
            self.assertEqual(cohort_review(self.db, *WEEKS, compact=True), expected)
        self.assertTrue(applied)
        after = self.compare_cohort(*WEEKS)
        self.assertFalse(after["two_consecutive_below"])
