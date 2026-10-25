#!/usr/bin/env python3
"""Send a bug branch back to Luna for a fresh review of the whole branch instead of another fixer round.

For a fix whose code and regression a review already verified but which was bounced for a reason that turned out to be
a loop mistake (owner 2026-10-20: gates red on the train on purpose, report-only problems, a native macOS run).

  rereview.py <slug> [<slug> ...]            dry run
  rereview.py --apply <slug> [<slug> ...]    does it
  --note "<text>"                            context for the reviewer (shown in its brief)

Per slug: the queued rework job (rework/<slug>.json) moves to runs/_rereview-2026-10-20/, and qa/pending gets a review
record covering the first reviewed round's head_before .. the branch head now, at the next round number. Refuses when a
process is working in the bug's worktree or nothing is queued for it.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "runs/_rereview-2026-10-20"


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True).stdout.strip()


def busy(wt):
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if str((p / "cwd").resolve()).startswith(str(wt)):
                    return True
            except OSError:
                pass
    return False


def plan(slug, note=""):
    reviews = []
    for f in (ROOT / "qa/done").glob(f"{slug}.r*.json"):
        m = re.search(r"\.r(\d+)$", f.stem)
        if m:
            reviews.append((int(m.group(1)), json.loads(f.read_text())))
    if not reviews:
        return None, "no earlier review"
    reviews.sort(key=lambda r: r[0])
    first, last = reviews[0][1], reviews[-1][1]
    job = ROOT / "rework" / f"{slug}.json"
    if not job.exists():
        return None, "no queued rework job (a fixer may be on it, or it was escalated elsewhere)"
    wt = Path(last["worktree"])
    if not wt.exists():
        return None, f"worktree {wt} is gone"
    if busy(wt):
        return None, "a process is working in its worktree"
    if git(wt, "status", "--porcelain"):
        return None, "worktree not clean"
    head = git(wt, "rev-parse", "HEAD")
    rnd = max(json.loads(job.read_text()).get("round") or 0, reviews[-1][0] + 1)
    rec = {k: v for k, v in last.items() if k not in ("qa", "outcome", "qa_failures", "qa_stage", "qa_code")}
    rec.update(round=rnd, head_before=first["head_before"], head_after=head, kind="fix", qwen_verdict="FIXED",
               commits=git(wt, "log", "--reverse", "--format=%h %s", f"{first['head_before']}..{head}").splitlines(),
               rereview="2026-10-20: re-review of the whole branch after the review-brief fixes (gates red on purpose, "
                        "report-only problems, no native macOS/Windows runs, the whole branch is reviewed rather than one round)"
                        + (f". {note}" if note else ""))
    return (job, rec), f"r{rnd}: {first['head_before'][:12]}..{head[:12]} ({len(rec['commits'])} commits)"


def main(apply, slugs, note=""):
    for slug in slugs:
        p, why = plan(slug, note)
        print(f"{slug}: {why}" if p else f"{slug}: skipped - {why}")
        if p and apply:
            job, rec = p
            ARCHIVE.mkdir(parents=True, exist_ok=True)
            shutil.move(str(job), str(ARCHIVE / job.name))
            (ROOT / "qa/pending" / f"{slug}.r{rec['round']}.json").write_text(json.dumps(rec, indent=1))


if __name__ == "__main__":
    args = sys.argv[1:]
    note = args[args.index("--note") + 1] if "--note" in args else ""
    rest = [a for i, a in enumerate(args) if not a.startswith("--") and not (i and args[i - 1] == "--note")]
    main("--apply" in args, rest, note)
