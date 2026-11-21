import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "lifeos.py"


class CompanyTimeCLITest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.ledger = root / "runtime" / "company-time.json"
        self.environment = {
            **os.environ,
            "LIFEOS_HOME": str(self.ledger.parent),
            "LIFEOS_CONFIG": str(root / "config.json"),
        }

    def command(self, *args):
        return [sys.executable, str(SCRIPT), "company-time", "set", *args]

    def set_time(self, *args, check=True):
        return subprocess.run(
            self.command(*args), env=self.environment,
            capture_output=True, text=True, check=check,
        )

    def read_ledger(self):
        return json.loads(self.ledger.read_text(encoding="utf-8"))

    def test_create_backfill_and_correct_the_same_cross_month_record(self):
        self.set_time("2026-10-31", "--start", "10:04")
        self.assertIsNone(self.read_ledger()["2026-10-31"]["ended_at"])

        result = self.set_time("2026-10-31", "--end", "2026-11-01 00:40")
        self.assertIn("2026-11-01 00:40", result.stdout)
        self.set_time("2026-10-31", "--end", "2026-11-01 00:32")
        self.assertEqual(
            {"2026-10-31": {"started_at": "2026-10-31T10:04+08:00", "ended_at": "2026-11-01T00:32+08:00"}},
            self.read_ledger(),
        )
        self.assertEqual(0o600, self.ledger.stat().st_mode & 0o777)
        self.assertFalse((self.ledger.parent / "entries.json").exists())

    def test_invalid_change_keeps_the_previous_record(self):
        self.set_time("2026-10-31", "--start", "10:00")
        before = self.ledger.read_bytes()
        for args in (
            (),
            ("--end", "00:40"),
            ("--start", "2026-11-01 10:00"),
            ("--start", "25:00"),
        ):
            result = self.set_time("2026-10-31", *args, check=False)
            self.assertNotEqual(0, result.returncode, args)
            self.assertEqual(before, self.ledger.read_bytes())

    def test_concurrent_partial_updates_are_merged(self):
        day = "2026-11-20"
        with subprocess.Popen(self.command(day, "--start", "10:04"), env=self.environment) as start:
            self.set_time(day, "--end", "2026-11-21 00:40")
            self.assertEqual(0, start.wait(timeout=10))
        self.assertEqual(
            {"started_at": "2026-11-20T10:04+08:00", "ended_at": "2026-11-21T00:40+08:00"},
            self.read_ledger()[day],
        )
