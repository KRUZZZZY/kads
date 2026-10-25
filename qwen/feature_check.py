#!/usr/bin/env python3
"""One-command gate check for a feature (or fix) branch (owner 2026-10-19: brief optimisation).

  python3 ~/.local/share/zene-bunny-fix/qwen/feature_check.py <worktree>

Runs every gate a builder must pass before committing and prints one PASS/FAIL table:
  fork-sources, all-sources-reproduce, no-upstream-regression, check-namespace, no-tautology, doc-figures - exit 0;
  complexity (gate 4) and file-length (gate 7) - red on the train on purpose, so they PASS when no item they list is
  NEW compared with the same gate on the branch's base (the train it was cut from) in a file the branch changed.
  Base listings are computed once per base commit in a code-only checkout and cached.
Exit 0 only if every row passes. Each gate's full output goes to <worktree>/../<name>-feature-check/ (outside git).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROG = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
CACHE = ROOT / "gate-baselines"
SIMPLE = [("fork-sources", ["bash", "tests/fork-sources-gate.sh"]),
          ("all-sources-reproduce", ["bash", "tests/all-sources-reproduce.sh"]),
          ("no-upstream-regression", ["bash", "tests/no-upstream-regression-gate.sh"]),
          ("check-namespace", ["tests/scripted/check-namespace"]),
          ("no-tautology", ["bash", "tests/no-tautology-gate.sh"]),
          ("doc-figures", ["python3", "tests/doc-figures-gate.py"])]
RATCHET = [("complexity", ["bash", "tests/complexity-gate.sh", "--check"]),
           ("file-length", ["bash", "tests/file-length-gate.sh", "--check"])]
REG = re.compile(r"^REGRESSION: (.*)$", re.M)


def run(cmd, cwd, log):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    log.write_text(r.stdout + r.stderr)
    return r.returncode, r.stdout + r.stderr


def key(line):
    """A regression line without its numbers: `new function over target: name@file`, `new file over 500 lines: path`."""
    line = re.sub(r"@\d+-\d+@", "@", line)
    return re.sub(r"\s*\(\w*\s*\d+\)\s*$", "", line).strip()


def item_file(line):
    m = re.search(r"@([^@\s]+?)\s*(\(|$)", line) or re.search(r":\s*([\w./+-]+\.\w+)\s*\(", line)
    return m.group(1) if m else ""


def base_listings(base):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{base}.json"
    if f.exists():
        return json.loads(f.read_text())
    tree = CACHE / "tree"
    if not tree.exists():
        subprocess.run(["git", "-C", str(PROG / "zene-cachyos"), "worktree", "add", "--detach", str(tree), base],
                       capture_output=True, check=True)
    subprocess.run(["git", "-C", str(tree), "checkout", "-q", "--detach", base], check=True)
    out = {}
    for name, cmd in RATCHET:
        _, text = run(cmd, tree, CACHE / f"{base}-{name}.log")
        out[name] = sorted({key(l) for l in REG.findall(text)})
    f.write_text(json.dumps(out, indent=1))
    return out


def main():
    wt = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    logs = wt.parent / f"{wt.name}-feature-check"
    logs.mkdir(exist_ok=True)
    git = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True).stdout.strip()
    fix_base = (ROOT / "FIX-BASE").read_text().strip()
    base = git("merge-base", "HEAD", fix_base) or git("rev-parse", fix_base)
    changed = set(git("diff", "--name-only", f"{base}..HEAD").splitlines()) | set(
        l[3:] for l in git("status", "--porcelain").splitlines())
    rows, ok = [], True
    for name, cmd in SIMPLE:
        rc, _ = run(cmd, wt, logs / f"{name}.log")
        rows.append((name, rc == 0, f"exit {rc}"))
        ok &= rc == 0
    baseline = base_listings(base)
    for name, cmd in RATCHET:
        rc, text = run(cmd, wt, logs / f"{name}.log")
        mine = [l for l in REG.findall(text) if key(l) not in set(baseline.get(name, [])) and item_file(l) in changed]
        rows.append((name, not mine, "no new item in your files" if not mine else "NEW: " + "; ".join(mine[:4])))
        ok &= not mine
    w = max(len(r[0]) for r in rows)
    print(f"feature-check {wt.name} (base {base[:9]}, {len(changed)} changed files; logs in {logs})")
    for name, passed, note in rows:
        print(f"  {'PASS' if passed else 'FAIL'}  {name:<{w}}  {note}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
