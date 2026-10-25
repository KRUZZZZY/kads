#!/usr/bin/env python3
"""Conflict forecast and freshness gate for the merge train (research/code-factories, 2026-10-25).

Why: the 2026-10-24 methodology audit found 20% of work sessions re-merging fixes that were already accepted, because
their branches went stale while they waited for a train (new-p12-34 became six -port generations). A train today is
due when 25 branches wait or the oldest has waited 24 h; nothing looks at *how stale* a waiting branch is.

Ideas (clean-room, see vendor/NOTICE.md): Uber SubmitQueue's conflict analyzer (changes that touch disjoint files
need no ordering; overlapping ones do), Zuul's speculative gating, and Shopify River's "freshness-gated merge queue"
(2026-09-02: the repository moves while a patch waits, so a patch is re-checked against the head before it merges).

What it does, using only read-only git plumbing (no merges, no checkouts):
  forecast(repo, base, cands)  per waiting branch: commits the base moved since the branch forked, the files both the
                               base and the branch changed ("hot" overlap), the files shared with other waiting
                               branches, and its age;
  order(forecasts)             merge the most exposed branches first, in the freshest tree; the rest keep their order;
  early_due(forecasts, cfg)    a reason to start a train now when enough waiting branches are stale.

Knob file (absent = off): qwen/FRESHNESS-GATE.json
    {"enabled": true, "order": true, "early_train": true,
     "min_stale": 5, "min_behind": 30, "min_overlap_files": 1, "min_wait_h": 3}
  early_train: start a train when at least min_stale waiting branches are stale (the base moved >= min_behind commits
               and touched >= min_overlap_files of the branch's files) and the oldest of those waited >= min_wait_h.
  order:       reorder the train's candidates (after the oldest-40 cut, so nothing is starved) most exposed first.

  python3 conflict_forecast.py report      forecast for the branches waiting for the current FIX-BASE (read-only)
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOB = ROOT / "FRESHNESS-GATE.json"
DEFAULTS = {"enabled": False, "order": True, "early_train": True, "min_stale": 5, "min_behind": 30,
            "min_overlap_files": 1, "min_wait_h": 3}


# Files every branch touches and the train already resolves (union merge driver, regenerated registries, per-unit
# reports). Counting them would make every branch "share files" with every other one.
GENERATED = re.compile(r"^(tests/(all-sources|fork-sources|upstream-modifications)\.txt|tests/CMakeLists\.txt|README\.md|"
                       r"docs/RELEASE-NOTES[^/]*\.md|docs/KNOWN-LIMITATIONS\.md|docs/qwen-fixes-[^/]*/.*|docs/050-features/.*|"
                       r"tools/mcp-zene-control/zene_control/commands_snapshot\.json)$")


def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(KNOB.read_text()))
    except (OSError, ValueError):
        cfg["enabled"] = False
    return cfg


def git(repo, *a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


def files_between(repo, a, b):
    return {f for f in git(repo, "diff", "--name-only", a, b).splitlines() if f and not GENERATED.match(f)}


def forecast(repo, base, cands, now=None):
    """cands: dicts with at least "commit" (and usually "cluster"/"branch"). Returns one dict per candidate, same order."""
    now = now or time.time()
    out, mine_by = [], []
    for c in cands:
        mb = git(repo, "merge-base", base, c["commit"])
        behind = int(git(repo, "rev-list", "--count", f"{mb}..{base}") or 0) if mb else 0
        hot = files_between(repo, mb, base) if mb and behind else set()
        mine = files_between(repo, mb, c["commit"]) if mb else set()
        stamp = git(repo, "show", "-s", "--format=%ct", c["commit"])
        out.append({"cluster": c.get("cluster"), "branch": c.get("branch"), "commit": c["commit"], "behind": behind,
                    "overlap": sorted(hot & mine), "files": len(mine),
                    "age_h": round((now - int(stamp)) / 3600, 1) if stamp.isdigit() else None})
        mine_by.append(mine)
    for i, f in enumerate(out):
        f["shared_with"] = sum(1 for j, m in enumerate(mine_by) if j != i and mine_by[i] & m)
    return out


def exposure(f):
    """How likely a branch is to conflict: files the head already changed under it, plus waiting branches it shares files with."""
    return len(f["overlap"]) + f["shared_with"]


def is_stale(f, cfg):
    return f["behind"] >= cfg["min_behind"] and len(f["overlap"]) >= cfg["min_overlap_files"]


def order(forecasts, cands):
    """The candidates, most exposed first; equal exposure keeps the incoming (oldest first) order (stable sort)."""
    rank = {f["commit"]: exposure(f) for f in forecasts}
    return sorted(cands, key=lambda c: -rank.get(c["commit"], 0))


def early_due(forecasts, cfg):
    """None, or a one-line reason to start a train before the usual 25-branch / 24 h rule says so."""
    stale = [f for f in forecasts if is_stale(f, cfg)]
    if len(stale) < cfg["min_stale"]:
        return None
    waited = max((f["age_h"] or 0) for f in stale)
    if waited < cfg["min_wait_h"]:
        return None
    worst = max(stale, key=exposure)
    return (f"{len(stale)} waiting branches are stale (base moved >= {cfg['min_behind']} commits under them and "
            f"touched their files; worst {worst['cluster']}: {worst['behind']} behind, {len(worst['overlap'])} hot files)")


if __name__ == "__main__":
    if sys.argv[1:2] == ["report"]:
        sys.path.insert(0, str(ROOT))
        import merge_train as MT
        base = (ROOT / "FIX-BASE").read_text().strip()
        cands = MT.candidates(base)
        fcs = forecast(MT.WT, base, cands)
        cfg = {**DEFAULTS, **config()}
        print(f"{len(fcs)} branches waiting for {base}; stale by the knob thresholds: {sum(is_stale(f, cfg) for f in fcs)}")
        for f in sorted(fcs, key=lambda f: -exposure(f))[:30]:
            print(f"{str(f['cluster']):28} behind {f['behind']:5} hot-overlap {len(f['overlap']):3} "
                  f"shared-with {f['shared_with']:3} age {f['age_h']} h")
        print("early train:", early_due(fcs, cfg) or "no")
    else:
        print(__doc__)
