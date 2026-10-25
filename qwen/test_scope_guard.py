"""scope_guard regressions: pure functions plus one run through submit_check on a temporary repository."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scope_guard as G

ON = {"enabled": True, "fail": False, "max_product_lines": 200}
ITEM = {"cluster": "9", "file": "src/core/Mixer.cpp", "batch": [{"cluster": "10", "file": "src/core/Track.cpp"}]}


class PureTests(unittest.TestCase):
    def test_off_or_no_finding_file_says_nothing(self):
        self.assertIsNone(G.evaluate(ITEM, ["src/gui/X.cpp"], "", "", {**ON, "enabled": False}))
        self.assertIsNone(G.evaluate({"cluster": "1"}, ["src/gui/X.cpp"], "", "", ON))

    def test_neighbourhood_is_in_scope(self):
        changed = ["src/core/Mixer.cpp", "src/core/Other.cpp", "include/Mixer.h", "tests/src/core/MixerTest.cpp",
                   "src/core/CMakeLists.txt", "docs/qwen-fixes-2026-10/cluster-9.md", "include/Track.h"]
        self.assertEqual(G.out_of_scope(changed, G.finding_files(ITEM)), [])

    def test_wandering_patch_is_listed(self):
        changed = ["src/core/Mixer.cpp", "src/gui/MixerView.cpp", "plugins/Wasm/Host.cpp", "tests/src/core/MixerTest.cpp"]
        self.assertEqual(G.out_of_scope(changed, G.finding_files(ITEM)), ["src/gui/MixerView.cpp", "plugins/Wasm/Host.cpp"])

    def test_files_named_in_the_report_are_allowed(self):
        report = "Also changed `src/gui/MixerView.cpp` because the label caches the old value."
        self.assertEqual(G.out_of_scope(["src/gui/MixerView.cpp"], G.finding_files(ITEM), report), [])

    def test_large_patch(self):
        numstat = "150\t60\tsrc/core/Mixer.cpp\n300\t0\ttests/src/core/MixerTest.cpp\n-\t-\tdata/x.png\n"
        self.assertEqual(G.product_lines(numstat), 210)
        state, detail = G.evaluate(ITEM, ["src/core/Mixer.cpp"], numstat, "", ON)
        self.assertEqual(state, "WARN")
        self.assertIn("210 product lines", detail)

    def test_fail_mode(self):
        state, _ = G.evaluate(ITEM, ["src/gui/MixerView.cpp"], "", "", {**ON, "fail": True})
        self.assertEqual(state, "FAIL")


class SubmitCheckTests(unittest.TestCase):
    def test_row_appears_in_the_pre_submit_check_only_when_enabled(self):
        import submit_check
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            wt = d / "wt"
            wt.mkdir()

            def g(*a):
                subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)
            g("init", "-q", "-b", "base")
            g("config", "user.email", "t@example.invalid")
            g("config", "user.name", "T")
            (wt / "README.md").write_text("x\n")
            g("add", "-A")
            g("commit", "-qm", "init")
            g("switch", "-q", "-c", "fix")
            for f in ("src/core/Mixer.cpp", "src/gui/Wandering.cpp"):
                (wt / f).parent.mkdir(parents=True, exist_ok=True)
                (wt / f).write_text("int x;\n")
            g("add", "-A")
            g("commit", "-qm", "fix")
            (d / "FIX-BASE").write_text("base\n")
            knob = d / "SCOPE-GUARD.json"
            labels = lambda: [r[1] for r in submit_check.check(wt, "9", ITEM)[0]]     # noqa: E731
            with mock.patch.object(submit_check, "ROOT", d), mock.patch.object(G, "KNOB", knob):
                self.assertNotIn("patch stays inside the finding's scope", labels())
                knob.write_text('{"enabled": true}')
                res = {r[1]: r for r in submit_check.check(wt, "9", ITEM)[0]}
                row = res["patch stays inside the finding's scope"]
                self.assertEqual(row[0], "WARN")
                self.assertIn("src/gui/Wandering.cpp", row[2])


if __name__ == "__main__":
    unittest.main()
