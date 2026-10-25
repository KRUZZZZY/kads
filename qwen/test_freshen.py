#!/usr/bin/env python3
"""freshen(): a reused fix branch is merged up to FIX-BASE before the next round (2026-10-25). Temporary repos, no models."""
import os, subprocess, sys, tempfile, pathlib, importlib
sys.path.insert(0, os.path.expanduser("~/.local/share/zene-bunny-fix/qwen"))
import qwenfix as Q
def sh(d, *a): return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()
with tempfile.TemporaryDirectory() as t:
    t = pathlib.Path(t); repo = t / "r"; repo.mkdir(); qroot = t / "q"; qroot.mkdir()
    sh(repo, "init", "-q", "-b", "050/m0-train-1"); sh(repo, "config", "user.name", "t"); sh(repo, "config", "user.email", "t@t")
    (repo / "a.txt").write_text("one\n"); (repo / "b.txt").write_text("b\n"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "base")
    sh(repo, "switch", "-qc", "bugs/qwen-7"); (repo / "a.txt").write_text("fix\n"); sh(repo, "commit", "-qam", "fix")
    sh(repo, "switch", "-q", "050/m0-train-1"); sh(repo, "switch", "-qc", "050/m0-train-2")
    (repo / "b.txt").write_text("b2\n"); sh(repo, "commit", "-qam", "train change")
    sh(repo, "switch", "-q", "bugs/qwen-7")
    Q.ROOT = qroot; (qroot / "FIX-BASE").write_text("050/m0-train-2\n"); logs = []; Q.log = lambda *a: logs.append(a)
    rd = t / "run"; rd.mkdir()
    Q.freshen({"cluster": 7}, repo, "bugs/qwen-7", rd)
    assert sh(repo, "merge-base", "--is-ancestor", "050/m0-train-2", "HEAD") == "" and not (rd / "freshen.txt").exists(), logs
    print("clean merge OK:", logs[-1])
    # conflict case
    sh(repo, "switch", "-q", "050/m0-train-2"); sh(repo, "switch", "-qc", "050/m0-train-3")
    (repo / "a.txt").write_text("train edits a\n"); sh(repo, "commit", "-qam", "train a")
    sh(repo, "switch", "-q", "bugs/qwen-7"); (repo / "a.txt").write_text("fix v2\n"); sh(repo, "commit", "-qam", "rework")
    head = sh(repo, "rev-parse", "HEAD"); (qroot / "FIX-BASE").write_text("050/m0-train-3\n")
    Q.freshen({"cluster": 7}, repo, "bugs/qwen-7", rd)
    assert sh(repo, "rev-parse", "HEAD") == head and not sh(repo, "status", "--porcelain") and "a.txt" in (rd / "freshen.txt").read_text()
    print("conflict left for session OK:", logs[-1])
    # branch merged by the train before -> skipped
    (rd / "freshen.txt").unlink(); sh(repo, "switch", "-q", "050/m0-train-3"); sh(repo, "switch", "-qc", "050/m0-train-4")
    sh(repo, "merge", "-q", "-X", "theirs", "--no-edit", "-m", "Merge bugs/qwen-7 (accepted; 050/m0-train-4, automatic train)", "bugs/qwen-7")
    sh(repo, "switch", "-q", "bugs/qwen-7"); (repo / "c.txt").write_text("c\n"); sh(repo, "add", "c.txt"); sh(repo, "commit", "-qm", "more")
    head = sh(repo, "rev-parse", "HEAD"); (qroot / "FIX-BASE").write_text("050/m0-train-4\n"); n = len(logs)
    Q.freshen({"cluster": 7}, repo, "bugs/qwen-7", rd)
    assert sh(repo, "rev-parse", "HEAD") == head and len(logs) == n and not (rd / "freshen.txt").exists()
    print("previously merged branch skipped OK")
    # integration items skipped
    Q.freshen({"cluster": 7, "integration": {"x": 1}}, repo, "bugs/qwen-7", rd); print("integration skipped OK")
