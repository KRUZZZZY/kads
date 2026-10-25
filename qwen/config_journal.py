#!/usr/bin/env python3
"""Config journal: an audit trail for the live knob files (research/code-factories, 2026-10-25).

Why: routing is steered by about 35 small files in qwen/ that every loop re-reads (LUNA-CAP, BUG-PREFER.json,
MULTI-SESSION.json, FEATURE-TIERS.json, holds and stops ...). They are edited by hand and by scripts, and the only
history is the 170 `*.bak-pre-<reason>` copies lying around; nobody can answer "what changed at 14:00 and why did
the fixers stop?". Worse, most readers do `except ValueError: use the default`, so a typo in a knob file is
silently ignored.

Idea: GitOps-style change journal (every change recorded with a diff) and Gas Town's append-only work ledger
(gastownhall/gastown, MIT; see vendor/NOTICE.md). Nothing is copied.

record(root) compares every watched file with the last snapshot and appends one JSON line per addition, change or
removal to CONFIG-JOURNAL.jsonl (with a short unified diff), and returns one-line notes. A watched file is a small
(<= 16 KB) knob file in the qwen folder: UPPER-CASE names, extension .json/.txt or none; state files (STATUS, STATE,
caches, catalogs, FIX-BASE ...) and backups are skipped. A .json file that no longer parses is reported as INVALID,
once per change.

The hourly autopilot calls record() when the knob file exists (absent = off): qwen/CONFIG-JOURNAL.json
    {"enabled": true, "ignore": ["EXTRA-STATE-FILE"]}

  python3 config_journal.py record [qwen-root]     take a snapshot now and print the notes
  python3 config_journal.py log [qwen-root] [N]    the last N journal entries (default 20)
"""
import difflib
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOB = ROOT / "CONFIG-JOURNAL.json"
MAX_BYTES = 16 * 1024
NAME = re.compile(r"^[A-Z][A-Z0-9_]*(?:-[A-Za-z0-9_.]+)*(?:\.(?:json|txt))?$")
SKIP = re.compile(r"\.(bak|lifted|ended|superseded|dropped|cleared)|STATUS|STATE|USAGE\.json|BENCHMARK|cache|catalog|"
                  r"ESCALATIONS|BUG-RANK|ROOT-CAUSE|BASELINE|SPLIT-UNITS|FIX-BASE|FEATURE-EVAL|LANE-CLAIMED|ACCEPTED|"
                  r"CONFIG-JOURNAL|PROBES", re.I)
STATE = "CONFIG-JOURNAL-STATE.json"
JOURNAL = "CONFIG-JOURNAL.jsonl"


def config():
    try:
        cfg = json.loads(KNOB.read_text())
    except (OSError, ValueError):
        return {"enabled": False, "ignore": []}
    return {"enabled": False, "ignore": [], **cfg}


def watched(root, ignore=()):
    out = {}
    for p in sorted(Path(root).iterdir()):
        if not p.is_file() or p.name in ignore or not NAME.match(p.name) or SKIP.search(p.name):
            continue
        try:
            if p.stat().st_size <= MAX_BYTES:
                out[p.name] = p.read_text(errors="replace")
        except OSError:
            continue
    return out


def valid(name, text):
    if not name.endswith(".json"):
        return True
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def short_diff(old, new):
    lines = list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), "before", "after", lineterm="", n=0))
    return "\n".join(lines[:40])[:2000]


def record(root=ROOT, dry=False, now=None):
    root = Path(root)
    cfg = config()
    now = now or time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
    try:
        last = json.loads((root / STATE).read_text())
    except (OSError, ValueError):
        last = None
    current = watched(root, set(cfg["ignore"]))
    notes, entries = [], []
    baseline = last is None
    if baseline:
        notes.append(f"config journal: baseline of {len(current)} knob files recorded")
        last = {}
    for name, text in current.items():
        old = last.get(name)
        if old is not None and old == text:
            continue
        kind = "added" if old is None else "changed"
        if not baseline:
            entries.append({"at": stamp, "file": name, "kind": kind, "old": digest(old) if old is not None else None,
                            "new": digest(text), "diff": short_diff(old, text)})
            notes.append(f"config {kind}: {name} ({(text.strip().replace(chr(10), ' ')[:80]) or 'empty'})")
        if not valid(name, text):
            notes.append(f"config INVALID JSON: {name} - the loops ignore it and use their defaults")
    for name in sorted(set(last) - set(current)):
        entries.append({"at": stamp, "file": name, "kind": "removed", "old": digest(last[name]), "new": None,
                        "diff": short_diff(last[name], "")})
        notes.append(f"config removed: {name}")
    if not dry:
        if entries:
            with open(root / JOURNAL, "a") as f:
                f.writelines(json.dumps(e) + "\n" for e in entries)
        (root / STATE).write_text(json.dumps(current))
    return notes


def tail(root=ROOT, n=20):
    try:
        lines = (Path(root) / JOURNAL).read_text().splitlines()[-n:]
    except OSError:
        return []
    return [json.loads(l) for l in lines if l.strip()]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT
    if cmd == "record":
        print("\n".join(record(root)) or "no change")
    elif cmd == "log":
        for e in tail(root, int(sys.argv[3]) if len(sys.argv) > 3 else 20):
            print(e["at"], e["kind"], e["file"])
            print("   " + e["diff"].replace("\n", "\n   ") if e["diff"] else "")
    else:
        print(__doc__)
