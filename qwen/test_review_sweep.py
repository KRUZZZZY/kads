#!/usr/bin/env python3
"""Fixes from the 2026-10-25 Luna sweep of the review code (research/reviews/luna-review-sweep-2026-10-25). Temporary
repos, no models."""
import json, os, pathlib, subprocess, sys, tempfile
sys.path.insert(0, os.path.expanduser("~/.local/share/zene-bunny-fix/qwen"))
import negctl
import qwenfix as Q

J = lambda d: "```json\n" + json.dumps(d) + "\n```"

# parse_verdict: the last verdict block only; malformed or odd verdicts are no verdict (never an earlier ACCEPT)
assert Q.parse_verdict("x " + J({"verdict": "accept"}) + " y " + J({"verdict": "REWORK"}))["verdict"] == "REWORK"
assert Q.parse_verdict(J({"verdict": "ACCEPT"}) + '\n```json\n{"verdict": "REWORK", oops}\n```') is None
assert Q.parse_verdict(J({"verdict": 3})) is None
assert Q.parse_verdict(J({"verdict": " "})) is None
assert Q.parse_verdict(J({"verdict": "ACCEPT"}) + J({"other": 1}))["verdict"] == "ACCEPT"   # a non-verdict block after it
assert Q.parse_verdict("no block") is None
print("parse_verdict OK")

# assertions that cannot fail
for line in ["QVERIFY(true);", "QVERIFY2(true, \"x\");", "assert True", "self.assertTrue(1)", "QCOMPARE(a, a);",
             "EXPECT_TRUE(true);", "assert(1);"]:
    assert Q.TEST_TRIVIAL.search(line), line
for line in ["QVERIFY(ok);", "QCOMPARE(actual, expected);", "self.assertEqual(1, x)", "assert x == 1", "QVERIFY(trueish());"]:
    assert not Q.TEST_TRIVIAL.search(line), line
print("TEST_TRIVIAL OK")

# negctl: tests named in a report; ctest name -> the binary it really runs
assert negctl.report_tests("ran `ctest -R '^(FooTest|BarTest)$' --output-on-failure` EXIT=0") == {"FooTest", "BarTest"}
with tempfile.TemporaryDirectory() as t:
    b = pathlib.Path(t) / "build" / "tests"; b.mkdir(parents=True)
    (b / "CTestTestfile.cmake").write_text('add_test(Lv2KeyTest "/x/build/tests/ControlDeviceCatalogueTest")\n'
                                           'add_test(SameTest "/x/build/tests/SameTest")\n'
                                           'add_test(PyTest "/usr/bin/python3" "/x/tests/p.py")\n')
    assert negctl.test_executables(pathlib.Path(t) / "build") == {
        "Lv2KeyTest": "ControlDeviceCatalogueTest", "SameTest": "SameTest", "PyTest": "python3"}
print("negctl helpers OK")


# verify_qa_fix: binary files and can't-fail assertions are refused before anything is built
def sh(d, *a): return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()


Q.qa_fix_cfg = lambda: {"enabled": True}
with tempfile.TemporaryDirectory() as t:
    repo = pathlib.Path(t)
    sh(repo, "init", "-q"); sh(repo, "config", "user.name", "t"); sh(repo, "config", "user.email", "t@t")
    (repo / "tests").mkdir(); (repo / "tests" / "FooTest.cpp").write_text("QCOMPARE(actual, expected);\n")
    (repo / "src.cpp").write_text("int a;\n"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "base")
    base = sh(repo, "rev-parse", "HEAD")
    rec = {"worktree": str(repo), "head_after": base, "head_before": base, "kind": "feature"}
    (repo / "tests" / "FooTest.cpp").write_text("QVERIFY(true);\n"); sh(repo, "commit", "-qam", "weaken")
    ok, why = Q.verify_qa_fix(rec, {}, repo / "logs")
    assert not ok and "cannot fail" in why, why
    sh(repo, "reset", "-q", "--hard", base)
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 4000); sh(repo, "add", "."); sh(repo, "commit", "-qm", "binary")
    ok, why = Q.verify_qa_fix(rec, {}, repo / "logs")
    assert not ok and "binary" in why, why
print("verify_qa_fix binary / trivial-assert OK")
print("ALL OK")
