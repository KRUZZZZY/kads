#!/usr/bin/env python3
"""Reset feature units parked before brief v2 (owner 2026-10-19: "the features that where parked prior to the new
briefs and feature system, reset them please").

A unit qualifies when it is a donation goal (plans/DONATION-GOALS.md) or has an escalation (ESCALATIONS.json) whose
last step happened before brief v2 went live (V2_AT), and nothing is working on it now (no state record that is still
open, no process inside its worktree). Reset = the old branch is kept as feat/050-<id>-oc-pre-v2, the worktree (if any)
moves to a fresh feat/050-<id>-oc on the fix base with its build directory kept (uncommitted leftovers saved as a patch
in runs/_reset-2026-10-19/), the escalation and donation rows are removed, so features_queue.py queues the unit again
at its Clef start tier, round 1, with a brief-v2 prompt.

  reset_parked_features.py            dry run: lists what would change
  reset_parked_features.py --apply    does it
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROG = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
LINE = PROG / "zene-cachyos"
GOALS = PROG / "plans/DONATION-GOALS.md"
V2_AT = "2026-10-19T15:01"
OPEN = ("accept", "confirm")          # final outcomes; anything else in state/ means the unit is still in play
ARCHIVE = ROOT / "runs/_reset-2026-10-19"


def git(cwd, *a, check=False):
    r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def busy_trees():
    out = set()
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                cwd = str((p / "cwd").resolve())
            except OSError:
                continue
            m = re.search(r"/zene-feat-(q050-[\d.]+)", cwd)
            if m:
                out.add(m.group(1).upper())
    return out


def main(apply):
    goals_text = GOALS.read_text() if GOALS.exists() else ""
    goals = set(re.findall(r"^\| (Q050-\d{3}) \|", goals_text, re.M))
    esc = json.loads((ROOT / "ESCALATIONS.json").read_text())
    old_esc = {q for q, e in esc.items() if max((h.get("at", "") for h in e.get("history") or []), default="") < V2_AT}
    busy = busy_trees()
    # done already: accepted (ports included) or built on the fix base - their state records may have been archived
    done = {re.sub(r"(-port)+$", "", l.split("\t")[0]) for l in (ROOT / "FEATURES-ACCEPTED.tsv").read_text().splitlines() if l.strip()}
    done |= set(re.findall(r"Q050-\d{3}\b", git(LINE, "log", "--format=%s %b", (ROOT / "FIX-BASE").read_text().strip())))
    picked, skipped = [], []
    for q in sorted(goals | old_esc):
        if q in done:
            skipped.append((q, "already accepted or built"))
            continue
        st = ROOT / "state" / f"{q}.json"
        rec = json.loads(st.read_text()) if st.exists() else None
        if rec and not str(rec.get("outcome", "")).lower().startswith(OPEN):
            skipped.append((q, f"in play ({rec.get('outcome')}, round {rec.get('round')})"))
        elif q in busy:
            skipped.append((q, "a process is working in its worktree"))
        elif rec:
            skipped.append((q, f"already {rec.get('outcome')}"))
        else:
            picked.append(q)
    print(f"{len(picked)} to reset: {' '.join(picked)}")
    for q, why in skipped:
        print(f"  skip {q}: {why}")
    if not apply:
        return
    base = (ROOT / "FIX-BASE").read_text().strip()
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    for q in picked:
        branch = f"feat/050-{q}-oc"
        wt = PROG / f"zene-feat-{q.lower()}"
        if git(LINE, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            keep = f"{branch}-pre-v2"
            if git(LINE, "rev-parse", "--verify", "--quiet", f"refs/heads/{keep}"):
                keep = f"{keep}-2"
            git(LINE, "branch", "-m", branch, keep, check=True)
        if wt.exists():
            dirty = git(wt, "status", "--porcelain")
            if dirty:
                (ARCHIVE / f"{q}-leftover.patch").write_text(git(wt, "diff", "HEAD"))
                (ARCHIVE / f"{q}-leftover-status.txt").write_text(dirty)
                git(wt, "reset", "-q", "--hard")
                git(wt, "clean", "-fdq", "-e", "build")
            git(wt, "checkout", "-q", "-B", branch, base, check=True)
        for p in (ROOT / "rework").glob(f"{q}.json"):
            shutil.move(str(p), str(ARCHIVE / p.name))
        esc.pop(q, None)
        print(f"reset {q}")
    (ROOT / "ESCALATIONS.json").write_text(json.dumps(esc, indent=1) + "\n")
    if goals & set(picked):
        lines = [l for l in goals_text.splitlines(keepends=True)
                 if not any(l.startswith(f"| {q} |") for q in picked)]
        GOALS.write_text("".join(lines))
    subprocess.run([sys.executable, str(ROOT / "features_queue.py")])


if __name__ == "__main__":
    main("--apply" in sys.argv)
