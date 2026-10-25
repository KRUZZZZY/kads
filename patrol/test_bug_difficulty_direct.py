#!/usr/bin/env python3
"""Offline tests for bug_difficulty_direct.py (fake model calls)."""
import unittest

import bug_difficulty_direct as D


def reply(hard, simple=None, conc=None, multi=None, test=None):
    v = {"n_hard": hard, "n_simple": 1 - hard if simple is None else simple, "n_conc": hard if conc is None else conc,
         "n_multi": hard if multi is None else multi, "n_test": hard if test is None else test}
    return {k: {"type": "noul", "noul": x} for k, x in v.items()}


class DirectDifficultyTest(unittest.TestCase):
    ITEM = {"cluster": "1", "title": "t", "file": "src/core/Mixer.cpp", "text": "BUG 1: race on the audio thread"}

    def test_five_questions_one_request(self):
        calls = []
        f = D.features(self.ITEM, post_fn=lambda b: calls.append(b) or reply(0.8))
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]["questions"]), set(D.QUESTIONS))
        self.assertEqual(len(D.QUESTIONS), 5)
        self.assertAlmostEqual(f["n_hard"], 0.8)

    def test_score_is_zero_for_easy_and_one_for_hard(self):
        hard = D.features(self.ITEM, post_fn=lambda b: reply(1.0, simple=0.0))
        easy = D.features(self.ITEM, post_fn=lambda b: reply(0.0, simple=1.0))
        self.assertAlmostEqual(D.score(hard), 1.0)
        self.assertAlmostEqual(D.score(easy), 0.0)

    def test_simple_fix_answer_is_inverted(self):
        base = {"n_hard": .5, "n_simple": 1.0, "n_conc": .5, "n_multi": .5, "n_test": .5}
        self.assertLess(D.score(base), D.score({**base, "n_simple": 0.0}))

    def test_missing_answer_raises(self):
        bad = reply(.5)
        del bad["n_conc"]
        with self.assertRaises(KeyError):
            D.features(self.ITEM, post_fn=lambda b: bad)


if __name__ == "__main__":
    unittest.main()
