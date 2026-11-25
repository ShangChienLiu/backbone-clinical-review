"""Smoke test the guided menu through its normal command-line entry point."""

import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from test_incremental_certainty import PATIENT, PLAN

ROOT = Path(__file__).resolve().parents[1]


class TerminalTest(unittest.TestCase):
    def test_original_questions_and_saved_import(self):
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            workspace = folder / "review workspace"
            delivery = folder / "new delivery"
            delivery.mkdir()
            (delivery / "plan.txt").write_text(PLAN, encoding="utf-8")

            def run(commands):
                return subprocess.run(
                    [sys.executable, ROOT / "terminal.py", "--workspace", workspace],
                    cwd=folder, input="\n".join([*commands, "0", ""]), text=True,
                    capture_output=True, check=True,
                ).stdout

            commands = [choice for item in range(1, 6) for choice in ("1", str(item))]
            output = run([*commands, "5", "1", "4", shlex.quote(str(delivery))])
            for item in range(1, 6):
                self.assertIn(f"DEV-0{item} ·", output)
            self.assertIn("585 or 595 min", output)
            self.assertIn("1 new · 0 reused", output)
            report, = (workspace / "exports").glob("review-*.json")
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["id"], "DEV-05")

            output = run(["4", str(delivery), "2", ""])
            self.assertIn("0 new · 1 reused", output)
            self.assertIn(PATIENT, output)
