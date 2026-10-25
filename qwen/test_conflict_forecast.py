"""conflict_forecast regressions: real temporary git repositories, no merges are performed by the module."""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import conflict_forecast as C


class Repo:
    def __init__(self, path):
        self.p = Path(path)
        self.g("init", "-q", "-b", "base")
        self.g("config", "user.email", "t@example.invalid")
        self.g("config", "user.name", "T")
        self.commit("init", {"a.cpp": "a\n", "b.cpp": "b\n", "c.cpp": "c\n"})

    def g(self, *a):
        return subprocess.run(["git", "-C", str(self.p), *a], capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, msg, files):
        for n, t in files.items():
            (self.p / n).parent.mkdir(parents=True, exist_ok=True)
            (self.p / n).write_text(t)
        self.g("add", "-A")
        self.g("commit", "-qm", msg)
        return self.g("rev-parse", "HEAD")

    def branch(self, name, files, frm="base"):
        self.g("switch", "-q", "-c", name, frm)
        sha = self.commit(name, files)
        self.g("switch", "-q", "base")
        return {"cluster": name, "branch": name, "commit": sha}


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Repo(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_branch_has_no_overlap(self):
        a = self.r.branch("fix-a", {"a.cpp": "a1\n"})
        fc = C.forecast(self.r.p, "base", [a])[0]
        self.assertEqual((fc["behind"], fc["overlap"], fc["shared_with"]), (0, [], 0))

    def test_base_moving_under_a_branch_is_seen_only_for_shared_files(self):
        a = self.r.branch("fix-a", {"a.cpp": "a1\n"})
        b = self.r.branch("fix-b", {"b.cpp": "b1\n"})
        self.r.commit("other work", {"a.cpp": "a2\n"})
        self.r.commit("more", {"c.cpp": "c2\n"})
        fa, fb = C.forecast(self.r.p, "base", [a, b])
        self.assertEqual((fa["behind"], fa["overlap"]), (2, ["a.cpp"]))
        self.assertEqual((fb["behind"], fb["overlap"]), (2, []))

    def test_files_shared_between_waiting_branches(self):
        a = self.r.branch("fix-a", {"a.cpp": "a1\n"})
        a2 = self.r.branch("fix-a2", {"a.cpp": "a9\n", "b.cpp": "b9\n"})
        c = self.r.branch("fix-c", {"c.cpp": "c1\n"})
        fa, fa2, fc = C.forecast(self.r.p, "base", [a, a2, c])
        self.assertEqual((fa["shared_with"], fa2["shared_with"], fc["shared_with"]), (1, 1, 0))

    def test_generated_registries_are_not_counted(self):
        a = self.r.branch("fix-a", {"a.cpp": "a1\n", "tests/all-sources.txt": "a\n"})
        b = self.r.branch("fix-b", {"b.cpp": "b1\n", "tests/all-sources.txt": "b\n"})
        self.r.commit("regen", {"tests/all-sources.txt": "z\n"})
        fa, fb = C.forecast(self.r.p, "base", [a, b])
        self.assertEqual((fa["overlap"], fa["shared_with"], fb["shared_with"]), ([], 0, 0))

    def test_order_puts_exposed_first_and_is_stable(self):
        cands = [self.r.branch(f"f{i}", {f"n{i}.cpp": "x\n"}) for i in range(3)]
        hot = self.r.branch("hot", {"a.cpp": "hot\n"})
        self.r.commit("base moves", {"a.cpp": "moved\n"})
        allc = cands + [hot]
        fcs = C.forecast(self.r.p, "base", allc)
        self.assertEqual([c["cluster"] for c in C.order(fcs, allc)], ["hot", "f0", "f1", "f2"])

    def test_early_due_needs_enough_stale_and_old_enough(self):
        cands = [self.r.branch(f"s{i}", {"a.cpp": f"v{i}\n"}) for i in range(3)]
        for i in range(4):
            self.r.commit(f"move {i}", {"a.cpp": f"m{i}\n"})
        cfg = {**C.DEFAULTS, "enabled": True, "min_stale": 3, "min_behind": 4, "min_wait_h": 1}
        old = time.time() + 5 * 3600           # pretend it is five hours later
        fcs = C.forecast(self.r.p, "base", cands, now=old)
        self.assertIn("3 waiting branches are stale", C.early_due(fcs, cfg))
        self.assertIsNone(C.early_due(fcs, {**cfg, "min_stale": 4}))
        self.assertIsNone(C.early_due(fcs, {**cfg, "min_behind": 5}))
        self.assertIsNone(C.early_due(C.forecast(self.r.p, "base", cands, now=time.time()), cfg))   # too young

    def test_knob_absent_means_off(self):
        with mock.patch.object(C, "KNOB", Path(self.tmp.name) / "nope.json"):
            self.assertFalse(C.config()["enabled"])


if __name__ == "__main__":
    unittest.main()
