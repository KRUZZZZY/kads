"""The freshness-gate hooks in merge_train.py: off without the knob, never fatal."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conflict_forecast as C
import merge_train as MT


class HookTests(unittest.TestCase):
    cands = [{"cluster": "a", "commit": "1"}, {"cluster": "b", "commit": "2"}]

    def test_off_without_knob(self):
        with mock.patch.object(C, "KNOB", Path("/nonexistent/FRESHNESS-GATE.json")):
            self.assertEqual(MT.freshness_gate("base", self.cands), (None, None))

    def test_gate_returns_reason_and_order_when_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            knob = Path(d) / "K.json"
            knob.write_text('{"enabled": true}')
            fcs = [{"cluster": "a", "commit": "1", "behind": 50, "overlap": ["x.cpp"], "shared_with": 0, "age_h": 9},
                   {"cluster": "b", "commit": "2", "behind": 50, "overlap": ["y.cpp", "z.cpp"], "shared_with": 1, "age_h": 9}]
            with mock.patch.object(C, "KNOB", knob), mock.patch.object(C, "forecast", return_value=fcs), \
                    mock.patch.object(MT, "log", lambda m: None):
                cfg, reason = MT.freshness_gate("base", self.cands)
                self.assertTrue(cfg["enabled"])
                self.assertIsNone(reason)                       # only 2 stale, default min_stale is 5
                knob.write_text('{"enabled": true, "min_stale": 2}')
                self.assertIn("2 waiting branches are stale", MT.freshness_gate("base", self.cands)[1])
                self.assertEqual([c["cluster"] for c in MT.freshness_order("base", self.cands)], ["b", "a"])

    def test_a_failing_forecast_never_stops_the_train(self):
        with tempfile.TemporaryDirectory() as d:
            knob = Path(d) / "K.json"
            knob.write_text('{"enabled": true}')
            with mock.patch.object(C, "KNOB", knob), mock.patch.object(C, "forecast", side_effect=OSError("git gone")), \
                    mock.patch.object(MT, "log", lambda m: None):
                self.assertEqual(MT.freshness_gate("base", self.cands), (None, None))
                self.assertEqual(MT.freshness_order("base", self.cands), self.cands)


if __name__ == "__main__":
    unittest.main()
