#!/usr/bin/env python3
"""Daily digest on Telegram (owner 2026-10-19: "a daily digest" - fixes and features accepted, trains, usage, anything
stuck). Run by zene-digest.timer at 09:00; `digest.py --print` shows it without sending."""
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROG = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
TS = re.compile(r"^\[(\d\d)-(\d\d) (\d\d:\d\d:\d\d)\] (.*)$")


def events(path, since):
    out = []
    try:
        for line in Path(path).read_text(errors="replace").splitlines():
            m = TS.match(line)
            if m:
                t = datetime.datetime.strptime(f"{since.year}-{m[1]}-{m[2]} {m[3]}", "%Y-%m-%d %H:%M:%S")
                if t >= since:
                    out.append(m[4])
    except OSError:
        pass
    return out


def jload(p, d=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return d


def workflow_lines(since):
    """What the autopilot did, what became held, and what stopped holding the train (2026-10-24 workflow)."""
    out = []
    auto = events(ROOT / "logs/autopilot.log", since)
    revived = sum(1 for e in auto if e.startswith("revived "))
    rediag = sum(1 for e in auto if e.startswith("diagnostic retry"))
    tuned = [e for e in auto if e.startswith("merge train OOM")]
    alerts = sum(1 for e in auto if e.startswith("alert:"))
    held = []
    try:
        for line in (ROOT / "issues/held.log").read_text().splitlines():
            row = json.loads(line)
            if row.get("at", "") >= since.isoformat(timespec="seconds"):
                held.append(f"{row['id']} ({row['status'].replace('blocked-', '')})")
    except (OSError, ValueError):
        pass
    total = {}
    for path in (ROOT / "issues").glob("*.json"):
        status = (jload(path, {}) or {}).get("status", "")
        if status.startswith("blocked") or status == "budget-exhausted":
            total[status] = total.get(status, 0) + 1
    if revived or rediag or tuned or alerts:
        out.append(f"Autopilot: {revived} stuck build(s) moved to the new train, {rediag} failed diagnostic(s) re-run"
                   + (f"; {tuned[-1]}" if tuned else "") + (f"; {alerts} alert(s) sent" if alerts else ""))
    if held:
        out.append(f"Newly held ({len(held)}, nothing needed from you - they wait or the autopilot retries): "
                   + ", ".join(sorted(set(held))[:8]) + (" ..." if len(set(held)) > 8 else ""))
    if total:
        out.append("Held overall: " + ", ".join(f"{n} {s.replace('blocked-', 'blocked ')}" for s, n in sorted(total.items())))
    overdue = [e for e in events(ROOT / "logs/merge-train.log", since) if e.startswith("not waiting for")]
    if overdue:
        out.append("Train: " + overdue[-1])
    return out


def weekly_scorecard():
    """Monday: the new workflow against its rollout baseline, and this week against last week."""
    try:
        import workflow_report
        data = workflow_report.refresh()
    except Exception as exc:
        return [f"Weekly scorecard unavailable: {exc!r}"[:200]]
    since = data.get("since_rollout") or {}
    integrated, work = since.get("newly_integrated_issues", 0), since.get("work_sessions", 0)
    snap_path = ROOT / "DIGEST-WEEK.json"
    last = jload(snap_path, {}) or {}
    week_int = integrated - last.get("integrated", 0)
    week_work = work - last.get("work", 0)
    trains = [e for e in events(ROOT / "logs/merge-train.log", datetime.datetime.now() - datetime.timedelta(days=7))
              if "promoted" in e]
    if "--print" not in sys.argv:      # a preview must not move the week's starting point
        snap_path.write_text(json.dumps({"date": str(datetime.date.today()), "integrated": integrated, "work": work}) + "\n")
    per = lambda w, i: f"{w / i:.1f}" if i else "n/a"
    return [f"Week: {week_int} issues newly integrated, {week_work} work sessions ({per(week_work, week_int)} per issue), "
            f"{len(trains)} train(s) promoted, {data.get('delivered', {}).get('held_issues', 0)} held",
            f"Since rollout ({str(since.get('started_at', '?'))[:10]}): {integrated} integrated, {work} work sessions "
            f"({per(work, integrated)} per issue), {since.get('review_sessions', 0)} reviews"]


def main():
    since = datetime.datetime.now() - datetime.timedelta(hours=24)
    qa = events(ROOT / "logs/qa.log", since)
    fx = events(ROOT / "logs/fix.log", since)
    acc_f = sum(1 for e in qa if re.match(r"Q050-\S+: ACCEPT", e))
    acc_b = sum(1 for e in qa if re.match(r"(?!Q050)\S+: (ACCEPT|CONFIRM)", e))
    batched = sum(int(m) for e in qa for m in re.findall(r"\(\+(\d+) batched", e))
    back = sum(1 for e in qa if "REWORK - sent back" in e)
    esc = sum(1 for e in fx + qa if "escalated to level" in e or "escalates a tier" in e)
    bounce = sum(1 for e in fx if "pre-submit check FAIL" in e)
    found = 0
    for line in (PROG / "research/reviews/luna-new-triage/TRIAGED-PASSES").read_text().splitlines():
        r = json.loads(line)
        if r.get("triaged", "") >= since.isoformat(timespec="seconds"):
            found += r.get("queued", 0)
    trains = [e for e in events(ROOT / "logs/merge-train.log", since) if "promoted" in e]
    u = jload(ROOT / "USAGE.json", {})
    cl = (u.get("claude") or {}).get("seven_day", {})
    go = (u.get("opencode_go") or {}).get("monthly", {})
    svc = subprocess.run(["systemctl", "--user", "is-active", "zene-fix", "zene-qa", "zene-patrol", "zene-triage", "zene-scope"],
                         capture_output=True, text=True).stdout.split()
    down = [n for n, st in zip(("fix", "qa", "patrol", "triage", "scope"), svc) if st != "active"]
    lanes = jload(ROOT / "LANES.json", {})
    on = [l["name"] for l in lanes.get("lanes", []) if l.get("enabled", True)]
    holds = [f.name for f in ROOT.glob("*-HOLD")]
    lines = [f"Zene digest {datetime.date.today():%a %d %b}",
             f"Accepted: {acc_b} bug fixes (+{batched} in batches), {acc_f} features; {back} sent back, {esc} escalated, "
             f"{bounce} pre-review bounces",
             f"Found: {found} new bugs triaged by Clef; fix base {(ROOT / 'FIX-BASE').read_text().strip()}"
             + (f"; trains promoted: {len(trains)}" if trains else ""),
             f"Queues: {len(list((ROOT / 'rework').glob('*.json')))} reworks, {len(list((ROOT / 'qa/pending').glob('*.json')))} in QA, "
             f"Claude lanes {'on: ' + ', '.join(on) if on else 'off'}",
             f"Usage: Claude week {cl.get('percent', '?')}%, Luna week {u.get('luna_weekly_used', '?')}%, "
             f"OpenCode Go month {go.get('percent', '?')}%"]
    lines += workflow_lines(since)
    if datetime.date.today().weekday() == 0 or "--week" in sys.argv:
        lines += weekly_scorecard()
    if down or holds:
        lines.append("Attention: " + "; ".join(([f"services down: {', '.join(down)}"] if down else [])
                                               + ([f"holds: {', '.join(holds)}"] if holds else [])))
    text = "\n".join(lines)
    if "--print" in sys.argv:
        print(text)
    else:
        subprocess.run(["notify-me", text], timeout=60)


if __name__ == "__main__":
    main()
