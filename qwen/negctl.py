#!/usr/bin/env python3
"""Mechanical negative control (owner 2026-10-19: verify "the new test fails without the change" before review).

prove(wt, base, run_dir, report_text) reverts the branch's product changes (not tests, docs or manifests) in the
working tree, builds the affected test targets, runs the tests the branch changed or the report names, restores HEAD,
rebuilds and runs them again. Result: {"ok": True/False/None, "tests": [...], "reverted_rc": n, "restored_rc": n,
"detail": "..."} - ok None means it could not be judged (no product change, no identifiable test, build trouble).
A build failure with the product reverted counts as the test failing (it cannot pass without the change).
"""
import os
import re
import subprocess
from pathlib import Path

HOME = Path.home()
PRODUCT = re.compile(r"\.(cpp|h|hpp|c|cc)$")
NOT_PRODUCT = re.compile(r"^(tests/|docs/|data/manual/|tools/mcp-zene-control/)")


def env(wt):
    e = dict(os.environ)
    e.pop("LMMS_PLUGIN_DIR", None)
    deps = HOME / ".local/zene-deps/root/usr/lib"
    e.update(QT_QPA_PLATFORM="offscreen", CCACHE_DIR=str(HOME / ".cache/ccache-bunny"), CCACHE_NOHASHDIR="true",
             CCACHE_BASEDIR=str(HOME / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"),
             PKG_CONFIG_PATH=f"{deps}/pkgconfig",
             LD_LIBRARY_PATH=f"{deps}:{HOME / '.local/zene-deps/onnxruntime-linux-x64-1.28.0/lib'}:{wt}/third_party/wasmtime/lib")
    return e


def sh(cmd, cwd, log, e, timeout=3600):
    with open(log, "w") as f:
        try:
            return subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, env=e, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            return 124


def prove(wt, base, run_dir, report_text=""):
    wt, run_dir = Path(wt), Path(run_dir)
    out = run_dir / "negctl"
    out.mkdir(exist_ok=True)
    git = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True).stdout
    changed = [f for f in git("diff", "--name-only", f"{base}..HEAD").splitlines() if f]
    product = [f for f in changed if PRODUCT.search(f) and not NOT_PRODUCT.search(f)]
    test_src = [f for f in changed if f.startswith("tests/") and re.search(r"\.(cpp|py)$", f)]
    if not product or not test_src or git("status", "--porcelain").strip():
        return {"ok": None, "detail": "no product change, no changed test, or a dirty tree"}
    build = wt / "build"
    if not (build / "tests").exists():
        return {"ok": None, "detail": "no build tree"}
    known = set(re.findall(r"Test #\d+: (\S+)", subprocess.run(["ctest", "-N"], cwd=build / "tests", capture_output=True,
                                                                text=True).stdout))
    named = set()
    for m in re.findall(r"ctest\s+(?:-\S+\s+)*-R\s+['\"]?\^?([\w|()$^.-]+)", report_text):
        named |= {x.strip("^$()") for x in re.split(r"[|]", m)}
    stems = {Path(f).stem for f in test_src}
    tests = sorted(t for t in known if t in stems or t in named)
    if not tests:
        return {"ok": None, "detail": f"no ctest test matched the changed tests ({', '.join(sorted(stems))[:200]})"}
    targets = ["zene"] + [t for t in tests if t in stems and Path(next(f for f in test_src if Path(f).stem == t)).suffix == ".cpp"]
    e = env(wt)
    regex = "^(" + "|".join(re.escape(t) for t in tests) + ")$"
    patch = out / "product.patch"
    patch.write_text(git("diff", f"{base}..HEAD", "--", *product))
    result = {"tests": tests, "product_files": product}
    try:
        if subprocess.run(["git", "-C", str(wt), "apply", "-R", str(patch)], capture_output=True).returncode != 0:
            return {"ok": None, "detail": "could not reverse the product diff", **result}
        rb = sh(["cmake", "--build", "build", "--target", *targets, "--", "-j4"], wt, out / "build-reverted.log", e)
        rr = sh(["ctest", "-R", regex, "--output-on-failure", "--timeout", "600"], build / "tests", out / "test-reverted.log", e) if rb == 0 else 1
    finally:
        subprocess.run(["git", "-C", str(wt), "checkout", "HEAD", "--", *product], capture_output=True)
    sb = sh(["cmake", "--build", "build", "--target", *targets, "--", "-j4"], wt, out / "build-restored.log", e)
    sr = sh(["ctest", "-R", regex, "--output-on-failure", "--timeout", "600"], build / "tests", out / "test-restored.log", e) if sb == 0 else 1
    clean = not git("status", "--porcelain").strip()
    result.update(reverted_build_rc=rb, reverted_rc=rr, restored_rc=sr, tree_clean=clean)
    if not clean:
        return {"ok": None, "detail": "tree not clean after restoring - not judged", **result}
    if rr == 0:
        return {"ok": False, "detail": f"{', '.join(tests)} PASS with the product change reverted - they do not prove the fix", **result}
    if sr != 0:
        return {"ok": None, "detail": f"the tests also fail WITH the change restored (rc {sr}) - see {out}", **result}
    how = "the build fails without the change" if rb != 0 else f"they fail without it (rc {rr})"
    return {"ok": True, "detail": f"{', '.join(tests)}: {how} and pass with it", **result}
