#!/usr/bin/env python3
"""Hourly autopilot for the fix workflow (owner 2026-10-24: "i am away from my pc most of the time ... automate it").

Recovers held issues that need no judgement, within fixed limits, and alerts the owner only when the whole pipeline
is stuck. It never deletes a worktree or branch, never rewrites commits, and never spends a model session itself:
a recovered issue goes back to the normal dispatcher and its normal budgets.

  1. blocked-build on an owned fix tree with no commits of its own: fast-forward the tree to the newest train
     (git merge --ff-only), which changes its build stamp so the dispatcher retries it once. At most
     max_auto_revives per issue.
  2. blocked-diagnostic because the diagnostic session failed or returned no plan (not a "blocked"/"split"
     verdict): released after a cooldown so the dispatcher runs the diagnostic again, once per issue; the issue's
     diagnostic budget still applies.
  3. merge train killed by the OOM killer: halve its ctest parallelism (merge/BAR-JOBS, floor 2).
  4. Alerts (notify-me, each at most once per its window): fix/QA loop down on two runs in a row; no train
     promoted for stall_alert_h; train OOM-killed again at the parallelism floor.

Everything it does goes to logs/autopilot.log; the daily digest summarises it. Settings: WORKFLOW.json "autopilot".
`autopilot.py --dry-run` prints what it would do.
"""
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

import issue_ledger as L

PROG = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
OWNED_TREES = ("zene-qwen-", "zene-feat-")       # integration trees are pinned to their target: never moved
DEFAULTS = {"max_auto_revives": 2, "diagnostic_retry_after_h": 6, "max_auto_diagnostic_retries": 1,
            "stall_alert_h": 72, "bar_jobs_default": 8, "bar_jobs_floor": 2}


def settings():
    cfg = (L.read(L.ROOT / "WORKFLOW.json") or {}).get("autopilot") or {}
    return {k: cfg.get(k, v) if isinstance(cfg.get(k, v), int) else v for k, v in DEFAULTS.items()}


def log(msg, dry=False):
    line = f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}"
    if dry:
        print("DRY", line)
        return
    (L.ROOT / "logs").mkdir(exist_ok=True)
    with open(L.ROOT / "logs/autopilot.log", "a") as stream:
        stream.write(line + "\n")


def notify(text, dry=False):
    log(f"alert: {text}", dry)
    if not dry and L.ROOT == L.LIVE_ROOT:
        subprocess.run(["notify-me", text], capture_output=True, timeout=60)


def state():
    return L.read(L.ROOT / "AUTOPILOT-STATE.json") or {}


def git(wt, *args):
    r = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def blocked_since(data):
    at = max((e.get("at", "") for e in data.get("events", []) if e.get("kind") in ("blocked", "infrastructure")), default="")
    try:
        return datetime.datetime.fromisoformat(at)
    except ValueError:
        return None


def ledgers():
    for path in sorted((L.ROOT / "issues").glob("*.json")):
        data = L.read(path)
        if data.get("id"):
            yield data


def revive_build(data, cfg, prog, dry=False):
    """A blocked-build issue whose own tree has no commits is moved to the newest train once more."""
    tree = Path(data.get("blocked_tree") or "")
    if tree.parent != prog or not tree.name.startswith(OWNED_TREES) or not (tree / ".git").exists():
        return None      # the lane tree (moves with every train by itself) or a pinned integration tree
    if int(data.get("auto_revives", 0)) >= cfg["max_auto_revives"]:
        return None
    base = (L.ROOT / "FIX-BASE").read_text().strip() if (L.ROOT / "FIX-BASE").exists() else ""
    rc, tip = git(tree, "rev-parse", "--verify", f"{base}^{{commit}}")
    _, head = git(tree, "rev-parse", "HEAD")
    if rc or not tip or tip == head:
        return None      # no newer train to move to
    if subprocess.run(["git", "-C", str(tree), "merge-base", "--is-ancestor", head, tip]).returncode != 0:
        return f"{data['id']}: tree {tree.name} has commits of its own - left for a person"
    if git(tree, "status", "--porcelain")[1]:
        return f"{data['id']}: tree {tree.name} is dirty - left for a person"
    if dry:
        return f"{data['id']}: would fast-forward {tree.name} to {base}"
    with L.claim({"cluster": data["id"]}) as owned:
        if not owned:
            return None
        rc, out = git(tree, "merge", "--ff-only", "--quiet", tip)
        if rc:
            return f"{data['id']}: fast-forward of {tree.name} failed ({out[-200:]})"
        with L.edit({"cluster": data["id"]}) as live:
            live["auto_revives"] = int(live.get("auto_revives", 0)) + 1
            live["events"].append({"key": f"autopilot:{time.time_ns()}", "kind": "autopilot",
                                   "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "reason": f"fast-forwarded {tree.name} to {base} for one more build attempt"})
    return f"revived {data['id']}: {tree.name} fast-forwarded to {base}; the dispatcher retries it"


def retry_diagnostic(data, cfg, dry=False):
    """A diagnostic that crashed or gave no plan is run again once; a real "blocked"/"split" verdict is kept."""
    if not str(data.get("reason", "")).startswith("Diagnostic failed or gave no actionable plan"):
        return None
    if int(data.get("auto_diagnostic_retries", 0)) >= cfg["max_auto_diagnostic_retries"]:
        return None
    since = blocked_since(data)
    if since and datetime.datetime.now() - since < datetime.timedelta(hours=cfg["diagnostic_retry_after_h"]):
        return None
    if dry:
        return f"{data['id']}: would release the failed diagnostic for one more run"
    with L.edit({"cluster": data["id"]}) as live:
        if live.get("status") != "blocked-diagnostic":
            return None
        live.update(status="ready", reason="Autopilot: the diagnostic session failed; running it once more.")
        live.pop("blocked_stamp", None)
        live["auto_diagnostic_retries"] = int(live.get("auto_diagnostic_retries", 0)) + 1
        live["events"].append({"key": f"autopilot:{time.time_ns()}", "kind": "autopilot",
                               "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": "failed diagnostic released once"})
    return f"diagnostic retry for {data['id']}: the last one failed without a plan"


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True).stdout


def tune_train(st, cfg, dry=False):
    """After an OOM kill of the merge train, halve its ctest -j (merge/BAR-JOBS); alert when already at the floor."""
    props = dict(l.split("=", 1) for l in systemctl("show", "zene-merge-train", "-p", "Result",
                                                    "-p", "ExecMainExitTimestamp").splitlines() if "=" in l)
    if props.get("Result") != "oom-kill" or props.get("ExecMainExitTimestamp") == st.get("train_oom_seen"):
        return
    path = L.ROOT / "merge/BAR-JOBS"
    try:
        jobs = int(path.read_text().strip())
    except (OSError, ValueError):
        jobs = cfg["bar_jobs_default"]
    if jobs <= cfg["bar_jobs_floor"]:
        notify(f"Zene merge train was OOM-killed again at ctest -j{jobs} (the floor). It needs a person: "
               f"less parallel work while it runs, or more memory.", dry)
    else:
        new = max(cfg["bar_jobs_floor"], jobs // 2)
        log(f"merge train OOM-killed ({props.get('ExecMainExitTimestamp')}): ctest -j{jobs} -> -j{new} for the next train", dry)
        if not dry:
            path.parent.mkdir(exist_ok=True)
            path.write_text(f"{new}\n")
    st["train_oom_seen"] = props.get("ExecMainExitTimestamp")


def watch_services(st, dry=False):
    down = [name for name in ("zene-fix", "zene-qa") if systemctl("is-active", name).strip() != "active"]
    still = sorted(set(down) & set(st.get("services_down", [])))
    if still and still != st.get("services_alerted"):
        notify(f"Zene {' and '.join(still)} has been down for over an hour (systemd restart did not bring it back).", dry)
        st["services_alerted"] = still
    if not still:
        st.pop("services_alerted", None)
    st["services_down"] = down


def watch_trains(st, cfg, dry=False):
    path = L.ROOT / "FIX-BASE"
    if not path.exists():
        return
    age_h = (time.time() - path.stat().st_mtime) / 3600
    last = st.get("stall_alert_at", 0)
    if age_h > cfg["stall_alert_h"] and time.time() - last > 24 * 3600:
        notify(f"Zene: no merge train promoted for {age_h:.0f} h (fix base {path.read_text().strip()}). "
               f"Last lines are in qwen/logs/merge-train.log.", dry)
        st["stall_alert_at"] = time.time()


def run(dry=False, prog=PROG):
    if (L.ROOT / "AUTOPILOT-OFF").exists():
        return []
    cfg = settings()
    st = state()
    done = []
    for data in ledgers():
        note = None
        if data.get("status") == "blocked-build":
            note = revive_build(data, cfg, prog, dry)
        elif data.get("status") == "blocked-diagnostic":
            note = retry_diagnostic(data, cfg, dry)
        if note:
            log(note, dry)
            done.append(note)
    tune_train(st, cfg, dry)
    watch_services(st, dry)
    watch_trains(st, cfg, dry)
    if not dry:
        L.write(L.ROOT / "AUTOPILOT-STATE.json", st)
    return done


if __name__ == "__main__":
    for line in run(dry="--dry-run" in sys.argv):
        print(line)
