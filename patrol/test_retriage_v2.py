#!/usr/bin/env python3
"""Offline tests for retriage_v2.py (pure functions; no Clef, git or GPU)."""
import unittest

import retriage_v2 as R


def row(cluster, p, base="b1"):
    return {"cluster": cluster, "base": base, "revision": base + "rev", "answers": {"still_present": {"noul": p}}}


class ActionTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(R.action(0.05), "already_fixed_likely")
        self.assertEqual(R.action(0.25), "already_fixed_likely")
        self.assertEqual(R.action(0.26), "stale_check")
        self.assertEqual(R.action(0.5), "stale_check")
        self.assertEqual(R.action(0.51), "open")
        self.assertEqual(R.action(None), "open")      # no answer is never a reason to skip a bug

    def test_missing_or_malformed_answer_is_none(self):
        self.assertIsNone(R.still_present({}))
        self.assertIsNone(R.still_present({"still_present": {"noul": "x"}}))
        self.assertIsNone(R.still_present(None))


class BuildTest(unittest.TestCase):
    def test_new_rows_win_over_old_and_unanswered_rows_are_ignored(self):
        old = [row("1", .9, "old"), row("2", .1, "old"), {"cluster": "3", "answers": {}}]
        new = [row("1", .1, "new")]
        s = R.build(old, new)
        self.assertEqual(s["1"]["action"], "already_fixed_likely")
        self.assertEqual(s["1"]["base"], "new")
        self.assertEqual(s["2"]["action"], "already_fixed_likely")
        self.assertNotIn("3", s)

    def test_refresh_order_unseen_first_then_lowest_and_untouched_only(self):
        scores = R.build([row("a", .4), row("b", .1), row("c", .9), row("d", .2)], [])
        order = R.refresh_order(scores, {"a", "b", "c", "x"}, ["x", "gone"], 10)
        self.assertEqual(order, ["x", "b", "a", "c"])   # d is touched, gone is not queued
        self.assertEqual(R.refresh_order(scores, {"a", "b", "c", "x"}, ["x"], 2), ["x", "b"])


if __name__ == "__main__":
    unittest.main()
