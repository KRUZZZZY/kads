#!/usr/bin/env python3
"""Offline tests for dedup.py and dedup_eval.py (no Ollama or Clef needed)."""
import json
import pathlib
import tempfile
import unittest

import numpy as np

import dedup
import dedup_eval


def finding(file, pass_no, title="t"):
    return {"file": file, "pass": pass_no, "title": title,
            "text": f"BUG: {title}\n  what: w\n  when: x\n  fix: y\n  confidence: high"}


def unit(*v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


class ClusterTest(unittest.TestCase):
    def test_same_pass_findings_are_never_merged(self):
        f = [finding("a.cpp", 1), finding("a.cpp", 1), finding("a.cpp", 2)]
        X = np.stack([unit(1, 0), unit(1, 0), unit(1, 0)])  # identical text
        clusters = dedup.cluster(f, X, {}, 0.84, 0.94, 0.4)
        for c in clusters:
            passes = [f[m]["pass"] for m in c["members"]]
            self.assertEqual(len(passes), len(set(passes)))
        self.assertEqual(len(clusters), 2)

    def test_findings_in_different_files_are_never_merged(self):
        f = [finding("a.cpp", 1), finding("b.cpp", 2)]
        X = np.stack([unit(1, 0), unit(1, 0)])
        self.assertEqual(len(dedup.cluster(f, X, {}, 0.84, 0.94, 0.4)), 2)

    def test_band_uses_the_judge_and_edges_use_cosine(self):
        f = [finding("a.cpp", 1), finding("a.cpp", 2), finding("a.cpp", 3)]
        c = 0.90  # inside the band
        X = np.stack([unit(1, 0), unit(c, np.sqrt(1 - c * c)), unit(0, 1)])
        merged = dedup.cluster(f, X, {(0, 1): 0.95}, 0.84, 0.94, 0.4)
        self.assertIn([0, 1], [m["members"] for m in merged])
        apart = dedup.cluster(f, X, {(0, 1): 0.10}, 0.84, 0.94, 0.4)
        self.assertNotIn([0, 1], [m["members"] for m in apart])

    def test_band_pairs_skip_same_pass_and_out_of_band(self):
        f = [finding("a.cpp", 1), finding("a.cpp", 1), finding("a.cpp", 2)]
        c = 0.90
        X = np.stack([unit(1, 0), unit(c, np.sqrt(1 - c * c)), unit(c, -np.sqrt(1 - c * c))])
        # 0-1 is in band but same pass; 1-2 has cosine c^2 - (1 - c^2) = 0.62, below the band.
        self.assertEqual(sorted(dedup.band_pairs(f, X, 0.84, 0.94)), [(0, 2)])

    def test_representative_is_the_latest_pass(self):
        f = [finding("a.cpp", 3), finding("a.cpp", 7), finding("a.cpp", 5)]
        X = np.stack([unit(1, 0)] * 3)
        (c,) = dedup.cluster(f, X, {}, 0.84, 0.94, 0.4)
        self.assertEqual(c["representative"], 1)


class EvalTest(unittest.TestCase):
    def gold(self, files, bridges=None):
        d = tempfile.mkdtemp()
        p = pathlib.Path(d) / "g.json"
        p.write_text(json.dumps({"files": files, "bridges": bridges or {}}))
        return str(p)

    def test_perfect_prediction_scores_one(self):
        g = self.gold({"a.cpp": [[0, 1], [2]]})
        s = dedup_eval.score({0: "x", 1: "x", 2: "y"}, g)
        self.assertEqual((s["pair_precision"], s["pair_recall"], s["bcubed_f1"]), (1.0, 1.0, 1.0))

    def test_false_merge_and_miss_are_counted(self):
        g = self.gold({"a.cpp": [[0, 1], [2]]})
        s = dedup_eval.score({0: "x", 1: "y", 2: "y"}, g)
        self.assertEqual((s["tp"], s["fp"], s["fn"]), (0, 1, 1))

    def test_bridge_pairs_are_not_scored(self):
        g = self.gold({"a.cpp": [[0, 1], [2, 3]]}, {"1": [2, 3]})
        merged_with_second = {0: "x", 1: "z", 2: "z", 3: "z"}
        strict = dedup_eval.score(merged_with_second, g)
        lenient = dedup_eval.score(merged_with_second, g, multilabel=True)
        self.assertEqual(strict["fp"], 0)       # 1-2 and 1-3 are bridge pairs
        self.assertEqual(strict["fn"], 1)       # 0-1 is still required in strict mode
        self.assertEqual(lenient["fn"], 0)      # a two-defect report may join either group


if __name__ == "__main__":
    unittest.main()
