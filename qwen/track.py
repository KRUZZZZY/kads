#!/usr/bin/env python3
"""Read-only tracker for the Qwen fixer + Luna QA: one row per bug, newest first.

  python3 track.py            table of every bug touched so far
  python3 track.py --now      what each loop is doing right now
  python3 track.py <cluster>  full history of one bug (every round, file paths)
Reads state/, runs/, qa/ and logs/ only; never writes.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}


def rounds(slug):
    out = []
    for d in sorted(ROOT.glob(f"runs/{slug}.r*"), key=lambda p: int(p.name.rsplit(".r", 1)[1])):
        rec = load(d / "record.json")
        qa = load(ROOT / "qa/done" / f"{d.name}.json").get("qa") or load(ROOT / "qa/pending" / f"{d.name}.json").get("qa") or {}
        pending = (ROOT / "qa/pending" / f"{d.name}.json").exists()
        out.append({"round": d.name.rsplit(".r", 1)[1], "dir": d, "rec": rec, "qa": qa, "qa_pending": pending})
    return out


def table():
    states = sorted(ROOT.glob("state/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not states:
        print("no bugs touched yet")
        return
    print(f"{'bug':<16} {'rnd':>3} {'tier':<14} {'qwen says':<15} {'cmts':>4} {'min':>5} {'luna QA':<12} {'outcome':<16} title")
    for p in states:
        st = load(p)
        rs = rounds(p.stem)
        last = rs[-1] if rs else {"rec": {}, "qa": {}, "qa_pending": False, "round": "-"}
        rec, qa = last["rec"], last["qa"]
        qa_v = qa.get("verdict") or ("waiting" if last["qa_pending"] else "-")
        print(f"{p.stem:<16} {last['round']:>3} {str(rec.get('tier') or st.get('tier') or '-'):<14} "
              f"{str(rec.get('qwen_verdict') or '-'):<15} {len(rec.get('commits') or []):>4} "
              f"{str(rec.get('minutes') or '-'):>5} {qa_v:<12} {str(st.get('outcome', '-')):<16} "
              f"{(st.get('title') or rec.get('title') or '')[:60]}")
    acc = (ROOT / "ACCEPTED.tsv")
    print(f"\naccepted branches: {len(acc.read_text().splitlines()) if acc.exists() else 0} (ACCEPTED.tsv)")


def now():
    for stream in ("fix", "qa"):
        pid = ROOT / f"{stream}.pid"
        alive = pid.exists() and Path(f"/proc/{pid.read_text().strip()}").exists()
        lines = (ROOT / "logs" / f"{stream}.log").read_text().splitlines() if (ROOT / "logs" / f"{stream}.log").exists() else []
        print(f"{stream}: {'RUNNING' if alive else 'not running'} - last: {lines[-1] if lines else '(no log yet)'}")
    for flag in ("GPU-REQUEST", "GPU-FREE", "STOP", "STOP-FIX", "STOP-QA", "FIX-DRAINED"):
        if (ROOT / flag).exists():
            print(f"[{flag}] {(ROOT / flag).read_text().strip()[:80]}")
    pend = sorted(p.stem for p in (ROOT / "qa/pending").glob("*.json"))
    rew = sorted(p.stem for p in (ROOT / "rework").glob("*.json"))
    print(f"QA waiting: {pend or 'none'}   reworks queued: {rew or 'none'}")


def one(cluster):
    slug = str(cluster)
    st = load(ROOT / "state" / f"{slug}.json")
    print(f"bug {slug}: {st.get('title', '')}\noutcome: {st.get('outcome', 'untouched')}  branch: {st.get('branch', '-')}  "
          f"worktree: {st.get('worktree', '-')}")
    for r in rounds(slug):
        rec, qa = r["rec"], r["qa"]
        print(f"\n-- round {r['round']} ({rec.get('harness', 'mcode')}, rc={rec.get('rc')}, {rec.get('minutes')} min)")
        print(f"   qwen verdict: {rec.get('qwen_verdict')}   commits: {rec.get('commits')}")
        print(f"   files: {rec.get('files')}")
        if qa:
            print(f"   luna QA: {qa.get('verdict')} ({qa.get('minutes')} min) - {str(qa.get('summary', ''))[:300]}")
            print(f"   review: {qa.get('review')}")
        print(f"   brief/session/logs: {r['dir']}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    now() if arg == "--now" else one(arg) if arg else table()
