#!/usr/bin/env python3
"""Claude weekly-usage guard (owner, 2026-10-14: "stop the subagents at 20% weekly usage").

Every 10 minutes: read the plan usage /usage shows (api.anthropic.com/api/oauth/usage, with the OAuth token from
~/.claude/.credentials.json - sent only there, never printed or logged). At or above USAGE_STOP_PCT weekly the
Haiku fixers get no sessions (LUNA-CAP fix=0; the fix process's light driver is Haiku via FIX-ENGINE, so Luna QA and
the free OpenCode worker carry on) and USAGE-STOP records why. Below it (e.g. after the weekly reset) the normal cap
comes back. If the usage cannot be read, it fails closed (fix=0). One notification per state change.
Stop the guard: touch STOP-GUARD. Status: USAGE-GUARD.json."""
import datetime, json, os, subprocess, sys, time, urllib.request
from pathlib import Path
import notify as notify_backend

ROOT = Path(__file__).resolve().parent
STOP_PCT = float(os.environ.get("USAGE_STOP_PCT", "20"))
NORMAL_CAP = os.environ.get("USAGE_NORMAL_CAP", "fix=2 qa=2")
STOPPED_CAP = os.environ.get("USAGE_STOPPED_CAP", "fix=0 qa=2")
EVERY = int(os.environ.get("USAGE_POLL_SECONDS", "600"))


def usage():
    tok = json.load(open(Path.home() / ".claude/.credentials.json"))["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage",
                                 headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
                                          "User-Agent": "zene-usage-guard"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    return d["seven_day"]["utilization"], d["seven_day"]["resets_at"], d["five_hour"]["utilization"]


def notify(text):
    notify_backend.send(text)


def main():
    (ROOT / "usage-guard.pid").write_text(str(os.getpid()))
    last = None
    while not (ROOT / "STOP-GUARD").exists():
        now = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            week, resets, five = usage()
            state = "stopped" if week >= STOP_PCT else "normal"
            why = f"weekly {week:.0f}% (stop at {STOP_PCT:.0f}%), resets {resets}"
        except Exception as e:  # unreadable usage: fail closed
            week, resets, five, state, why = None, None, None, "stopped", f"usage unreadable: {type(e).__name__}"
        cap = STOPPED_CAP if state == "stopped" else NORMAL_CAP
        if (ROOT / "LUNA-CAP").read_text().strip() != cap:
            (ROOT / "LUNA-CAP").write_text(cap + "\n")
        if state == "stopped":
            (ROOT / "USAGE-STOP").write_text(f"{now} {why}\n")
        else:
            (ROOT / "USAGE-STOP").unlink(missing_ok=True)
        (ROOT / "USAGE-GUARD.json").write_text(json.dumps(
            {"checked": now, "weekly_pct": week, "five_hour_pct": five, "resets_at": resets, "state": state,
             "stop_pct": STOP_PCT, "cap": cap, "why": why}, indent=1) + "\n")
        if state != last and last is not None:
            notify(f"Zene: Claude subagents {'STOPPED' if state == 'stopped' else 'resumed'} - {why}")
        last = state
        time.sleep(EVERY)
    (ROOT / "usage-guard.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
