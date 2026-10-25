#!/usr/bin/env python3
"""Luna (OpenAI/Codex) pacing (owner 2026-10-25: pace "everything bug with open ai").

Luna burned its whole weekly window in about two days at a time, then every loop starved until the reset. The usage
monitor calls step() every 10 minutes; it writes LUNA-PACE.json, which qwenfix.luna_cap() and feature_scope.py obey.
LUNA-CAP stays the owner's ceiling: the pace can only lower it.

  budget = (100 - RESERVE - used) / hours to the weekly reset      (% per hour that lasts to the reset)
  burn   = rise of luna_weekly_used over the last BURN_WINDOW_H    (from usage-history.jsonl)

Shedding order, one level at a time (owner: Luna is mainly the bug reviewer; it fixes only while the other fixers'
quotas are out):
  level 0  everything as LUNA-CAP says
  level 1  feature/bug scoping paused (ahead-of-time work; a backlog of briefs already exists)
  level 2  Luna fixers halved
  level 3  no Luna fixers
  level 4  Luna reviewers capped at 1 (reviews never stop)
Up a level when burn > budget x 1.2 (at most every 30 min); down when burn < budget x 0.7 (at most every 60 min).
Independently, Luna fixers are 0 while DeepSeek has a provider with quota (fix_only_when_others_out).
Off switch: LUNA-PACE-OFF (the file is removed, so nothing is capped).
"""
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "LUNA-PACE.json"
HISTORY = ROOT / "usage-history.jsonl"
RESERVE = 5.0
BURN_WINDOW_H = 2.0
UP, DOWN = 1.2, 0.7
UP_EVERY_S, DOWN_EVERY_S = 30 * 60, 60 * 60
MAX_LEVEL = 4
FIX_ONLY_WHEN_OTHERS_OUT = True
STALE_S = 3600          # readers ignore a pace older than this (the monitor stopped): nothing is capped then


def burn_rate(history, used, now, window_h=BURN_WINDOW_H):
    """% per hour over the window, or None without at least an hour of rows since the last reset."""
    rows = [(r["_t"], r["luna_weekly_used"]) for r in history
            if r.get("luna_weekly_used") is not None and now - r["_t"] <= window_h * 3600]
    rows.append((now, used))
    start = 0
    for i in range(1, len(rows)):
        if rows[i][1] < rows[i - 1][1]:      # a weekly reset inside the window: measure after it
            start = i
    rows = rows[start:]
    dt_h = (rows[-1][0] - rows[0][0]) / 3600
    return None if dt_h < 1.0 else max(0.0, rows[-1][1] - rows[0][1]) / dt_h


def deepseek_available(usage, provider_limits, now):
    """True while DeepSeek can run on some provider: the OpenCode Go route is not rested, or OpenDesign has quota."""
    go_ok = not usage.get("opencode_rested")
    od = provider_limits.get("opendesign") or {}
    od_ok = not (od.get("exhausted") and float(od.get("reset_at") or 0) > now)
    return go_ok or od_ok


def decide(prev, used, reset_ts, burn, others_available, now):
    hours_left = max(0.5, (reset_ts - now) / 3600) if reset_ts else None
    budget = max(0.0, 100 - RESERVE - used) / hours_left if hours_left else None
    level, changed = int(prev.get("level") or 0), float(prev.get("changed") or 0)
    why = "steady"
    if used >= 100 - RESERVE:
        level, why = MAX_LEVEL, f"{used}% used, inside the {RESERVE:.0f}% reserve"
    elif budget is not None and burn is not None:
        if burn > budget * UP and level < MAX_LEVEL and now - changed >= UP_EVERY_S:
            level, why = level + 1, f"burn {burn:.1f}%/h > budget {budget:.1f}%/h"
        elif burn < budget * DOWN and level > 0 and now - changed >= DOWN_EVERY_S:
            level, why = level - 1, f"burn {burn:.1f}%/h < budget {budget:.1f}%/h"
    if level != int(prev.get("level") or 0):
        changed = now
    return {"level": level, "changed": changed, "why": why, "used": used, "hours_left": hours_left,
            "budget_pct_h": None if budget is None else round(budget, 2),
            "burn_pct_h": None if burn is None else round(burn, 2),
            "scope": level < 1,
            "fix_factor": 0.0 if level >= 3 or (FIX_ONLY_WHEN_OTHERS_OUT and others_available) else 0.5 if level == 2 else 1.0,
            "qa_max": 1 if level >= 4 else None,
            "others_available": others_available}


def step(used, reset_ts, usage=None, now=None):
    """Called by usage_monitor.main(); returns the pace written (or None when switched off)."""
    now = now or time.time()
    if (ROOT / "LUNA-PACE-OFF").exists():
        OUT.unlink(missing_ok=True)
        return None
    try:
        prev = json.loads(OUT.read_text())
    except (OSError, ValueError):
        prev = {}
    history = []
    try:
        for line in HISTORY.read_text().splitlines()[-200:]:
            r = json.loads(line)
            r["_t"] = time.mktime(time.strptime(r["at"][:19], "%Y-%m-%dT%H:%M:%S"))
            history.append(r)
    except (OSError, ValueError, KeyError):
        pass
    try:
        limits = json.loads((ROOT.parent / "dsh/home/provider-limits.json").read_text())
    except (OSError, ValueError):
        limits = {}
    pace = decide(prev, used, reset_ts, burn_rate(history, used, now), deepseek_available(usage or {}, limits, now), now)
    pace["at"] = now
    OUT.write_text(json.dumps(pace, indent=1) + "\n")
    return pace


def current():
    """The pace in force, or {} when there is none, it is switched off, or it is stale."""
    try:
        p = json.loads(OUT.read_text())
        return p if time.time() - float(p.get("at") or 0) <= STALE_S else {}
    except (OSError, ValueError):
        return {}


def cap(kind, ceiling):
    """LUNA-CAP's value for kind ("fix"/"qa") lowered by the pace."""
    p = current()
    if not p:
        return ceiling
    if kind == "fix":
        f = float(p.get("fix_factor", 1.0))
        return 0 if f <= 0 or ceiling <= 0 else max(1, int(ceiling * f))
    if kind == "qa" and p.get("qa_max") is not None:
        return min(ceiling, max(1, int(p["qa_max"])))
    return ceiling


def scoping_allowed():
    return bool(current().get("scope", True))
