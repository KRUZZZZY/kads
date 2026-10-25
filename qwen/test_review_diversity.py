"""review_diversity regressions: fake review records in a temporary folder, no models."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import review_diversity as R


class FamilyTests(unittest.TestCase):
    def test_families(self):
        self.assertEqual(R.family("gpt-6-luna"), "openai")
        self.assertEqual(R.family("claude-haiku-5-5"), "anthropic")
        self.assertEqual(R.family("opencode-go/deepseek-v4.1-flash"), "deepseek")
        self.assertEqual(R.family("opencode/space-bunny-free"), R.UNKNOWN)
        self.assertEqual(R.family("custom_provider:strata/qwen3.8-flash-next-coder"), "alibaba")

    def test_unknown_is_never_the_same_family(self):
        self.assertFalse(R.same_family("opencode/beam-free", "opencode/cat-jam-free"))
        self.assertTrue(R.same_family("gpt-6-luna", "codex gpt-6-luna max"))

    def test_extra_table_extends_the_builtin_one(self):
        self.assertEqual(R.family("opencode/beam-free", {"beam": "beam-lab"}), "beam-lab")


class PolicyTests(unittest.TestCase):
    luna_fix = {"cluster": "1", "model": "gpt-6-luna"}
    old_fix = {"cluster": "2"}                       # before 2026-10-08: no model, Luna's
    ds_fix = {"cluster": "3", "model": "opencode-go/deepseek-v4.1-flash"}

    def test_off_by_default(self):
        self.assertTrue(R.reviewer_ok(self.luna_fix, "luna", cfg={"enabled": False}))
        with mock.patch.object(R, "KNOB", Path("/nonexistent/REVIEW-DIVERSITY.json")):
            self.assertTrue(R.reviewer_ok(self.luna_fix, "luna"))

    def test_require_blocks_same_family_forever(self):
        cfg = {"enabled": True, "mode": "require", "max_wait_h": 6}
        self.assertFalse(R.reviewer_ok(self.luna_fix, "luna", 100, cfg))
        self.assertFalse(R.reviewer_ok(self.old_fix, "luna", 100, cfg))
        self.assertTrue(R.reviewer_ok(self.luna_fix, "haiku", 0, cfg))
        self.assertTrue(R.reviewer_ok(self.ds_fix, "luna", 0, cfg))

    def test_prefer_releases_after_the_wait(self):
        cfg = {"enabled": True, "mode": "prefer", "max_wait_h": 6}
        self.assertFalse(R.reviewer_ok(self.luna_fix, "luna", 1, cfg))
        self.assertFalse(R.reviewer_ok(self.luna_fix, "luna", None, cfg))
        self.assertTrue(R.reviewer_ok(self.luna_fix, "luna", 6.5, cfg))

    def test_waited_hours_uses_file_age(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text("{}")
            self.assertAlmostEqual(R.waited_hours(p, now=p.stat().st_mtime + 7200), 2.0, places=3)
            self.assertIsNone(R.waited_hours(Path(d) / "missing.json"))


class AuditTests(unittest.TestCase):
    def test_audit_counts_pairs_and_outcomes(self):
        with tempfile.TemporaryDirectory() as d:
            done = Path(d) / "qa" / "done"
            done.mkdir(parents=True)
            rows = [("gpt-6-luna", "gpt-6-luna", "ACCEPT"), ("gpt-6-luna", "gpt-6-luna", "REWORK"),
                    ("claude-haiku-5-5", "gpt-6-luna", "ACCEPT-NOTE"), ("opencode/beam-free", "gpt-6-luna", "REWORK")]
            for i, (a, r, v) in enumerate(rows):
                (done / f"{i}.json").write_text(json.dumps({"model": a, "qa": {"reviewer_model": r, "verdict": v}}))
            (done / "bad.json").write_text("not json")
            (done / "nov.json").write_text(json.dumps({"model": "x", "qa": {}}))
            res = R.audit(d)
            self.assertEqual(res["same"], {"n": 2, "accept": 1, "rework": 1})
            self.assertEqual(res["cross"], {"n": 2, "accept": 1, "rework": 1})
            self.assertEqual(res["pairs"]["anthropic -> openai"]["n"], 1)


class QaLoopHookTests(unittest.TestCase):
    def test_diversity_ok_never_raises(self):
        import qwenfix
        with mock.patch.object(qwenfix.review_diversity, "reviewer_ok", side_effect=RuntimeError("boom")):
            self.assertTrue(qwenfix.diversity_ok({"model": "gpt-6-luna"}, "luna", Path("/nonexistent")))
        with tempfile.TemporaryDirectory() as d:
            knob = Path(d) / "K.json"
            knob.write_text('{"enabled": true, "mode": "require"}')
            with mock.patch.object(R, "KNOB", knob):
                self.assertFalse(qwenfix.diversity_ok({"model": "gpt-6-luna"}, "luna", Path(d)))
                self.assertTrue(qwenfix.diversity_ok({"model": "claude-haiku-5-5"}, "luna", Path(d)))


if __name__ == "__main__":
    unittest.main()
