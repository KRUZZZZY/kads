#!/usr/bin/env python3
"""luna_pace: Luna weekly-usage pacing (2026-10-25). Pure functions plus a temp LUNA-PACE.json; no models."""
import json, tempfile, time, unittest
from pathlib import Path
import luna_pace as P

H = 3600.0


class PaceTest(unittest.TestCase):
    def test_burn_rate_measures_after_a_reset(self):
        now = 10 * H
        hist = [{"_t": now - 1.9 * H, "luna_weekly_used": 90}, {"_t": now - 1.5 * H, "luna_weekly_used": 0},
                {"_t": now - 0.5 * H, "luna_weekly_used": 2}]
        self.assertAlmostEqual(P.burn_rate(hist, 4, now), 4 / 1.5)
        self.assertIsNone(P.burn_rate([], 4, now))          # under an hour of data: no judgement

    def test_steps_up_when_burning_too_fast_and_holds_otherwise(self):
        now = 100 * H
        reset = now + 100 * H                                 # budget = (100-5-30)/100 = 0.65 %/h
        p = P.decide({}, 30, reset, 2.4, False, now)
        self.assertEqual((p["level"], p["scope"], p["fix_factor"]), (1, False, 1.0))
        q = P.decide(p, 31, reset, 2.4, False, now + 600)     # inside the 30-min guard
        self.assertEqual(q["level"], 1)
        q = P.decide(p, 31, reset, 2.4, False, now + UP_GUARD)
        self.assertEqual((q["level"], q["fix_factor"]), (2, 0.5))
        r = P.decide({"level": 2, "changed": now}, 31, reset, 0.2, False, now + 3700)
        self.assertEqual(r["level"], 1)

    def test_reserve_and_other_fixers(self):
        now = 0.0
        p = P.decide({}, 96, now + 10 * H, None, False, now)
        self.assertEqual((p["level"], p["qa_max"], p["fix_factor"]), (4, 1, 0.0))
        p = P.decide({}, 10, now + 100 * H, 0.1, True, now)  # DeepSeek has quota: Luna only reviews
        self.assertEqual((p["level"], p["fix_factor"]), (0, 0.0))

    def test_deepseek_available(self):
        now = 1000.0
        self.assertFalse(P.deepseek_available({"opencode_rested": ["x"]}, {"opendesign": {"exhausted": True, "reset_at": 2000}}, now))
        self.assertTrue(P.deepseek_available({"opencode_rested": ["x"]}, {"opendesign": {"exhausted": True, "reset_at": 500}}, now))
        self.assertTrue(P.deepseek_available({"opencode_rested": []}, {}, now))

    def test_cap_lowers_but_never_raises_and_ignores_stale(self):
        with tempfile.TemporaryDirectory() as t:
            old = P.OUT
            P.OUT = Path(t) / "LUNA-PACE.json"
            try:
                self.assertEqual(P.cap("fix", 2), 2)          # no pace file
                P.OUT.write_text(json.dumps({"at": time.time(), "fix_factor": 0.5, "qa_max": 1, "scope": False}))
                self.assertEqual((P.cap("fix", 2), P.cap("fix", 1), P.cap("fix", 0), P.cap("qa", 3)), (1, 1, 0, 1))
                self.assertFalse(P.scoping_allowed())
                P.OUT.write_text(json.dumps({"at": time.time() - 2 * H, "fix_factor": 0.0}))
                self.assertEqual(P.cap("fix", 2), 2)
                self.assertTrue(P.scoping_allowed())
            finally:
                P.OUT = old


UP_GUARD = P.UP_EVERY_S

if __name__ == "__main__":
    unittest.main()
