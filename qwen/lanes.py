#!/usr/bin/env python3
"""Claude feature lanes of the fix loop (owner 2026-10-19: "a simple system where you can just add whatever lane and
remove whatever lane you want"). The loop reads LANES.json live; this edits it.

  lanes.py                                 list lanes, what each is doing, and the Claude queue head
  lanes.py add NAME MODEL [--effort high] [--tiers sonnet,opus]
  lanes.py remove NAME                     a running session finishes; its unit's rework then waits for a lane
  lanes.py enable NAME | disable NAME

MODEL: any Claude model id. Sonnet 5.5 requests route to Opus 5.5 medium (owner 2026-10-24).
TIERS: which CLAUDE-QUEUE.jsonl tiers the lane takes.
A lane runs one unit at a time and stays busy while that unit is in review or rework.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE = ROOT / "LANES.json"


def load():
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {"lanes": []}


def save(cfg):
    FILE.write_text(json.dumps(cfg, indent=1) + "\n")


def holding(name):
    for f in (ROOT / "state").glob("Q050-*.json"):
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if r.get("lane") == name and not str(r.get("outcome", "")).lower().startswith(
                ("accept", "confirm", "parked", "no-result", "driver-error", "build-failed")):
            return f"{r.get('cluster')} ({r.get('outcome')}, round {r.get('round')})"
    return None


def main(argv):
    cfg = load()
    lanes = cfg.setdefault("lanes", [])
    if not argv:
        if not lanes:
            print("no lanes")
        for l in lanes:
            print(f"{l['name']:12} {l['model']:20} effort {l.get('effort', 'high'):6} tiers {','.join(l.get('tiers') or ['sonnet', 'opus']):12} "
                  f"{'enabled ' if l.get('enabled', True) else 'DISABLED'}  holds: {holding(l['name']) or '-'}")
        q = [json.loads(x) for x in (ROOT / "CLAUDE-QUEUE.jsonl").read_text().splitlines()[:6]] if (ROOT / "CLAUDE-QUEUE.jsonl").exists() else []
        print("queue head:", ", ".join(f"{r['queue_id']}({r['tier']})" for r in q))
        return 0
    cmd, name = argv[0], (argv[1] if len(argv) > 1 else None)
    if cmd == "add" and len(argv) >= 3:
        opts = dict(zip(argv[3::2], argv[4::2]))
        model, effort = argv[2], opts.get("--effort", "high")
        if model == "claude-sonnet-5-5":
            model, effort = "claude-opus-5-5", "medium"
        lanes[:] = [l for l in lanes if l["name"] != name] + [{
            "name": name, "model": model, "effort": effort,
            "tiers": opts.get("--tiers", "sonnet,opus").split(","), "enabled": True}]
    elif cmd == "remove" and name:
        lanes[:] = [l for l in lanes if l["name"] != name]
    elif cmd in ("enable", "disable") and name:
        for l in lanes:
            if l["name"] == name:
                l["enabled"] = cmd == "enable"
    else:
        print(__doc__)
        return 2
    save(cfg)
    return main([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
