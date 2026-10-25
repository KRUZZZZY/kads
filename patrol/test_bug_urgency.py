#!/usr/bin/env python3
"""Offline tests for bug_urgency_sort.py and clef_client.py (fake model calls; no Ollama, GPU or network)."""
import json
import pathlib
import tempfile
import unittest

import bug_urgency_sort as U
import clef_client as C


def fake_answers(urgent):
    """A canned nine-question reply: `urgent` in 0..1 drives every urgency answer."""
    ans = {n: {"type": "noul", "noul": urgent} for n, (k, _) in U.QUESTIONS.items() if k == "noul"}
    ans["noul_low"] = {"type": "noul", "noul": 1 - urgent}
    ans["score5"] = {"type": "score", "score": 4 * urgent}
    ans["choice4"] = {"type": "choice", "probabilities": {"A": urgent, "B": 0.0, "C": 1 - urgent, "D": 0.0}}
    return ans


class ClefClientTest(unittest.TestCase):
    def test_post_fn_gets_the_wire_body(self):
        seen = []
        C.post({"bug": "x"}, {"q": {"type": "noul", "instructions": "?"}}, post_fn=lambda b: seen.append(b) or {"q": {}})
        self.assertEqual(seen[0]["model"], "clef-flash")
        self.assertEqual(seen[0]["state"], {"bug": "x"})
        self.assertIn("q", seen[0]["questions"])

    def test_cache_survives_reload_and_never_rewrites(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "c.jsonl"
            c = C.Cache(p)
            c.put("a", {"v": 1})
            c.put("b", 2)
            c2 = C.Cache(p)
            self.assertEqual((c2.get("a"), c2.get("b"), c2.get("zzz")), ({"v": 1}, 2, None))
            self.assertEqual(len(p.read_text().splitlines()), 2)


class FeatureTest(unittest.TestCase):
    def test_nine_questions_one_request(self):
        calls = []
        item = {"cluster": "1", "title": "t", "file": "src/core/Mixer.cpp", "text": "BUG 1: audio thread locks"}
        f = U.features(item, post_fn=lambda b: calls.append(b) or fake_answers(0.9))
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]["questions"]), set(U.NAMES))
        self.assertEqual(len(U.NAMES), 9)
        self.assertAlmostEqual(f["noul_top"], 0.9)
        self.assertAlmostEqual(f["score5"], 0.9)
        self.assertAlmostEqual(f["choice4"], (3 * 0.9 + 0.1) / 3)
        self.assertEqual(f["path_low"], 0.0)

    def test_path_prior_flags_tests_and_vendored_code(self):
        for path, low in (("tests/src/core/FooTest.cpp", 1.0), ("src/3rdparty/ableton-link/x.h", 1.0),
                          ("plugins/Foo/asio/impl.h", 1.0), ("src/core/Mixer.cpp", 0.0), ("src/gui/Latest.cpp", 0.0)):
            f = U.features({"cluster": "1", "title": "t", "file": path, "text": "x"}, post_fn=lambda b: fake_answers(.5))
            self.assertEqual(f["path_low"], low, path)

    def test_missing_answer_is_an_error_not_a_default(self):
        bad = fake_answers(.5)
        del bad["n_rt"]
        with self.assertRaises(KeyError):
            U.features({"cluster": "1", "title": "t", "file": "f", "text": "x"}, post_fn=lambda b: bad)

    def test_score_orders_urgent_above_minor_and_penalises_tests(self):
        w = (["noul_top", "n_top2", "path_low"], [0.5, 0.5, -0.5], 0.0)
        hi = U.score({"noul_top": .9, "n_top2": .9, "path_low": 0}, w)
        lo = U.score({"noul_top": .1, "n_top2": .1, "path_low": 0}, w)
        lo_test = U.score({"noul_top": .9, "n_top2": .9, "path_low": 1}, w)
        self.assertGreater(hi, lo)
        self.assertGreater(hi, lo_test)

    def test_weights_file_roundtrip_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "w.json"
            p.write_text(json.dumps({"names": ["noul_top"], "w": [2.0], "bias": 1.0}))
            self.assertEqual(U.score({"noul_top": .5}, U.load_weights(p)), 2.0)
            self.assertEqual(U.load_weights(pathlib.Path(d) / "missing.json")[0][0], "noul_top")


class RankingTest(unittest.TestCase):
    def test_percentile_is_insertion_position(self):
        asc = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(U.percentile(asc, 0.0), 0.0)
        self.assertEqual(U.percentile(asc, 5.0), 1.0)
        self.assertEqual(U.percentile(asc, 2.5), 0.5)
        self.assertEqual(U.percentile(asc, 2.0), 0.375)   # tie splits the difference

    def test_group_takes_max_member_and_members_inherit_its_pct(self):
        bugs = {c: {"title": c, "file": "f"} for c in "abcd"}
        members = {"G1": ["a", "b"]}
        rows, pct = U.rank_units(bugs, members, {"a": 0.1, "b": 0.9, "c": 0.5, "d": 0.2})
        self.assertEqual([r["cluster"] for r in rows], ["G1", "c", "d"])
        self.assertEqual(pct["a"], pct["b"], pct["G1"])
        self.assertGreater(pct["G1"], pct["c"])
        self.assertGreater(pct["c"], pct["d"])

    def test_pct_counts_a_new_bug_among_stored_ones(self):
        bugs = {c: {} for c in "abc"}
        _, p1 = U.rank_units(bugs, {}, {"a": 1.0, "b": 2.0})
        _, p2 = U.rank_units(bugs, {}, {"a": 1.0, "b": 2.0, "c": 3.0})
        self.assertLess(p1["b"], 1.01)
        self.assertEqual(p2["c"], max(p2.values()))


class JournalTest(unittest.TestCase):
    def test_resume_skips_scored_bugs(self):
        with tempfile.TemporaryDirectory() as d:
            J = U.Journal(run=d, digest="dig1")
            J.add("a", {"noul_top": .5}, .5, "dig1")
            J2 = U.Journal(run=d, digest="dig1")
            self.assertIn("a", J2.rows)

    def test_digest_change_refuses_to_mix_scores(self):
        with tempfile.TemporaryDirectory() as d:
            J = U.Journal(run=d, digest="dig1")
            J.add("a", {}, .5, "dig1")
            with self.assertRaises(SystemExit):
                U.Journal(run=d, digest="dig2")

    def test_rescore_keeps_old_journal_and_starts_new(self):
        with tempfile.TemporaryDirectory() as d:
            U.Journal(run=d, digest="dig1").add("a", {}, .5, "dig1")
            J = U.Journal(run=d, digest="dig2", rescore=True)
            self.assertEqual(J.rows, {})
            self.assertTrue((pathlib.Path(d) / "scores.dig1.jsonl").exists())
            self.assertEqual(json.loads((pathlib.Path(d) / "meta.json").read_text())["digest"], "dig2")


if __name__ == "__main__":
    unittest.main()
