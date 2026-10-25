"""config_journal regressions: temporary folders only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autopilot as A
import config_journal as J
import issue_ledger as L


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "LUNA-CAP").write_text("fix=2 qa=2\n")
        (self.root / "BUG-PREFER.json").write_text('{"models": ["a"]}')
        (self.root / "STOP-FIX").write_text("")
        (self.root / "AUTOSCALE-STATUS.json").write_text("{}")       # state, not a knob
        (self.root / "BUG-PREFER.json.bak-pre-x").write_text("{}")     # a backup
        (self.root / "notes.md").write_text("x")
        (self.root / "BIG.json").write_text(" " * (J.MAX_BYTES + 1))

    def tearDown(self):
        self.tmp.cleanup()

    def test_watched_files(self):
        self.assertEqual(sorted(J.watched(self.root)), ["BUG-PREFER.json", "LUNA-CAP", "STOP-FIX"])

    def test_baseline_then_changes(self):
        self.assertIn("baseline of 3 knob files", J.record(self.root)[0])
        self.assertFalse((self.root / J.JOURNAL).exists())
        self.assertEqual(J.record(self.root), [])
        (self.root / "LUNA-CAP").write_text("fix=3 qa=2\n")
        (self.root / "PATROL-HOLD").write_text("held")
        (self.root / "STOP-FIX").unlink()
        notes = J.record(self.root)
        self.assertEqual(sorted(n.split(":")[0] + ":" + n.split(":")[1].split(" ")[1] for n in notes),
                         ["config added:PATROL-HOLD", "config changed:LUNA-CAP", "config removed:STOP-FIX"])
        entries = J.tail(self.root)
        by = {e["file"]: e for e in entries}
        self.assertIn("+fix=3 qa=2", by["LUNA-CAP"]["diff"])
        self.assertIn("-fix=2 qa=2", by["LUNA-CAP"]["diff"])
        self.assertEqual(by["STOP-FIX"]["kind"], "removed")

    def test_invalid_json_is_reported(self):
        J.record(self.root)
        (self.root / "BUG-PREFER.json").write_text('{"models": ["a"],}')
        notes = J.record(self.root)
        self.assertTrue(any("INVALID JSON: BUG-PREFER.json" in n for n in notes))

    def test_dry_run_writes_nothing(self):
        J.record(self.root, dry=True)
        self.assertFalse((self.root / J.STATE).exists())

    def test_ignore_list(self):
        self.assertEqual(sorted(J.watched(self.root, {"LUNA-CAP"})), ["BUG-PREFER.json", "STOP-FIX"])


class AutopilotHookTests(unittest.TestCase):
    def test_hook_is_off_without_the_knob_and_logs_when_on(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "LUNA-CAP").write_text("fix=2 qa=2\n")
            lines = []
            with mock.patch.object(L, "ROOT", root), mock.patch.object(A, "log", lambda m, dry=False: lines.append(m)):
                A.journal_config()
                self.assertEqual(lines, [])
                self.assertFalse((root / J.STATE).exists())
                (root / "CONFIG-JOURNAL.json").write_text('{"enabled": true}')
                A.journal_config()
                self.assertTrue(any("baseline" in m for m in lines))
                (root / "LUNA-CAP").write_text("fix=4 qa=2\n")
                A.journal_config()
                self.assertTrue(any(m.startswith("config changed: LUNA-CAP") for m in lines))


if __name__ == "__main__":
    unittest.main()
