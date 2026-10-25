#!/usr/bin/env python3
"""Autoscaling for the bug-fix pipeline (owner, 2026-10-15: "get another reviewer and set up autoscaling").

Two pressures decide how many sessions run:

- the QA backlog (qa/pending): reviews are the slow step, so a growing backlog adds Haiku reviewers beside
  Luna and, past a threshold, slows then holds new fixer sessions until the reviewers catch up;
- free memory: every new session needs headroom (a Haiku session starts a 5-6 GB clangd), so starts are
  capped by MemAvailable minus a floor.

Knobs live in AUTOSCALE.json (re-read every loop; missing keys take the defaults below). Each loop writes what
it decided to AUTOSCALE-STATUS.json under its own key. Pure functions; qwenfix.py calls them.
"""
import json
import math
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOBS = ROOT / "AUTOSCALE.json"
STATUS = ROOT / "AUTOSCALE-STATUS.json"

DEFAULTS = {
    "enabled": True,
    # reviewers: Luna keeps its LUNA-CAP qa slots; Haiku reviewers are added on top as the backlog grows
    "haiku_review_min": 0,
    "haiku_review_max": 3,
    "haiku_review_per_backlog": 3,    # one Haiku reviewer per this many pending reviews beyond Luna's slots
    # Luna reviewers (owner 2026-10-20: "let luna autoscale to meet the review requirements"): one per this many
    # reviews waiting or running, at least luna_review_min, at most LUNA-CAP qa
    "luna_review_min": 1,
    "luna_review_per_backlog": 2,
    # fixers: above backlog_slow new fixer sessions are halved; at backlog_hold none start (reworks included)
    "backlog_slow": 12,
    "backlog_hold": 24,
    # memory (GB): what one new session needs, and what is always left free
    "ram_floor_gb": 4.0,
    "ram_haiku_gb": 7.0,
    "ram_free_model_gb": 2.5,
    "ram_luna_gb": 1.5,
}


def knobs():
    try:
        return {**DEFAULTS, **json.loads(KNOBS.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def available_ram_gb():
    try:
        m = re.search(r"^MemAvailable:\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M)
        return int(m.group(1)) / 1_000_000 if m else float("inf")
    except OSError:
        return float("inf")


def ram_starts(need_gb, k=None):
    """How many more sessions needing need_gb each fit above the floor right now."""
    k = k or knobs()
    spare = available_ram_gb() - float(k["ram_floor_gb"])
    return max(0, math.floor(spare / max(need_gb, 0.1)))


def haiku_reviewers(pending, luna_slots, k=None):
    """Target number of Haiku reviewers for this backlog (before the memory check)."""
    k = k or knobs()
    if not k["enabled"]:
        return int(k["haiku_review_min"])
    beyond = max(0, pending - luna_slots)
    want = math.ceil(beyond / max(1, int(k["haiku_review_per_backlog"])))
    return max(int(k["haiku_review_min"]), min(int(k["haiku_review_max"]), want))


def luna_reviewers(backlog, cap, k=None):
    """Luna review sessions for this backlog (reviews waiting plus running), between luna_review_min and cap."""
    k = k or knobs()
    if not k["enabled"]:
        return cap
    want = math.ceil(backlog / max(1, int(k["luna_review_per_backlog"])))
    return max(min(int(k["luna_review_min"]), cap), min(cap, want))


def fixer_factor(pending, k=None):
    """1.0 normally, 0.5 past backlog_slow, 0.0 at backlog_hold."""
    k = k or knobs()
    if not k["enabled"]:
        return 1.0
    if pending >= int(k["backlog_hold"]):
        return 0.0
    if pending >= int(k["backlog_slow"]):
        return 0.5
    return 1.0


def scale(cap, factor):
    """Apply a factor to a capacity; a non-zero factor keeps at least one session."""
    if factor <= 0 or cap <= 0:
        return 0
    return max(1, math.floor(cap * factor))


def report(loop, decision):
    try:
        cur = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        cur = {}
    cur[loop] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ram_available_gb": round(available_ram_gb(), 1),
                 **decision}
    tmp = STATUS.with_suffix(f".{loop}.tmp")
    tmp.write_text(json.dumps(cur, indent=1) + "\n")
    tmp.replace(STATUS)
