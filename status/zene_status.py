#!/usr/bin/env python3
"""Zene work status: one window that says what every lane is doing (owner 2026-10-19: "make a ui that i can quickly
check on to see what is generally going on and pin it to my task manager").

Reads only local state, every 15 s: the fix/QA loops (pids, logs, queue files), running agent processes (OpenCode
sessions, Luna/Codex sessions, classified by their parent), Claude lanes and trains from ORCH-STATUS.json (written by
the orchestrating Claude session), usage (qwen/USAGE.json from the usage monitor, Codex from the reset watcher's
state), Clef re-triage and feature scoping logs, and BENCHMARK.json. It never starts, stops or changes anything.

  zene_status.py          the window (PySide6)
  zene_status.py --text   the same, as plain text

The "All bugs & features" link opens a filterable list of every unit with history and a per-unit timeline (rounds, reviews,
escalations, port jobs, trains); it reads the loop's files and never writes them.
"""
import datetime
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
Q = HOME / ".local/share/zene-bunny-fix/qwen"
PROG = HOME / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
ORCH = Q / "ORCH-STATUS.json"
CODEX = HOME / ".local/share/codex-banked-reset/state.json"
TS = re.compile(r"^\[(\d\d)-(\d\d) (\d\d:\d\d:\d\d)\] (.*)$")


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def jrows(p):
    try:
        return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]
    except OSError:
        return []


def alive(pidfile, needle):
    try:
        pid = Path(pidfile).read_text().split()[0]
        return needle in Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, IndexError):
        return False


def tail_events(path, n):
    out = []
    try:
        lines = Path(path).read_text(errors="replace").splitlines()[-400:]
    except OSError:
        return out
    year = datetime.date.today().year
    for line in lines:
        m = TS.match(line)
        if m:
            t = datetime.datetime.strptime(f"{year}-{m[1]}-{m[2]} {m[3]}", "%Y-%m-%d %H:%M:%S")
            out.append((t, m[4]))
    return out[-n:] if n else out


def dsh_provider(pid):
    """The provider a DeepSeek Harness session uses (2026-10-22 A/B: opencode-go or opendesign), from its environment."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return "?"
    return next((e.split(b"=", 1)[1].decode() for e in env if e.startswith(b"ZENE_DSH_PROVIDER=")), "opencode-go")


def item_of(path):
    name = Path(path).name
    if name.startswith("zene-feat-"):
        return name[len("zene-feat-"):].upper()
    if name.startswith("zene-integrate-"):
        return re.sub(r"-[0-9a-f]{10}$", "", name[len("zene-integrate-"):])
    if name.startswith("zene-qwen-"):
        return name[len("zene-qwen-"):]
    return name


def processes():
    """Running agent sessions, from ps: what runs, on which item, for how long."""
    ps = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,args="], capture_output=True, text=True).stdout
    procs = {}
    for line in ps.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            procs[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3])

    def ancestry(pid):
        chain, seen = [], 0
        while pid in procs and seen < 8:
            chain.append(procs[pid][2])
            pid = procs[pid][0]
            seen += 1
        return " | ".join(chain)

    sessions = []
    for pid, (ppid, et, args) in procs.items():
        if args.startswith(("node ", "/usr/bin/node ")) and "codex exec " in args:
            m = re.search(r" -C (\S+)", args)
            eff = re.search(r'model_reasoning_effort="?(\w+)', args)
            anc = ancestry(ppid)
            role = ("diagnosis" if "/diagnostics/" in args else "integration" if "/zene-integrate-" in args else "bug hunt" if "patrol.py" in anc else "scoping" if "feature_scope" in anc else "review" if "qwenfix.py qa" in anc or "--read-only" in anc
                    else "fix" if "qwenfix.py fix" in anc else "lane review" if "run-reviews" in anc else "other")
            scoped = re.search(r"(?:feature|bug)-briefs/runs/([\w.-]+)/", anc) or (
                re.search(r"(luna-patrol)", anc) if "patrol.py" in anc else None)
            sessions.append({"who": "Luna", "role": role, "item": scoped[1] if scoped else item_of(m[1]) if m else "?",
                             "detail": eff[1] if eff else "", "min": et / 60})
        elif " -p --model claude-" in args and "jail.py" not in args:
            m = re.search(r"--model (\S+)", args)
            eff = re.search(r"--effort (\S+)", args)
            try:
                cwd = Path(f"/proc/{pid}/cwd").resolve()
            except OSError:
                cwd = Path("?")
            item = item_of(cwd)
            sessions.append({"who": "Claude", "role": "feature" if item.startswith("Q050-") else "other", "item": item,
                             "detail": f"{m[1] if m else '?'} {eff[1] if eff else ''}", "min": et / 60})
        elif "opencode run" in args and "--model" in args and not args.startswith(("zsh", "/usr/bin/zsh", "bash")):
            m = re.search(r"--model (\S+)", args)
            d = re.search(r"--dir (\S+)", args)
            item = item_of(d[1]) if d else "?"
            sessions.append({"who": "OpenCode", "role": "feature" if item.startswith("Q050-") else "fix",
                             "item": item, "detail": m[1] if m else "?", "min": et / 60})
        elif args.startswith(("node ", "/usr/bin/node ")) and "dsh --profile headless" in args:
            # 2026-10-20: DeepSeek V4.1 Flash runs through DeepSeek Harness (qwen/DSH-HARNESS.json), not opencode run
            try:
                item = item_of(Path(f"/proc/{pid}/cwd").resolve())
            except OSError:
                item = "?"
            sessions.append({"who": "DeepSeek", "role": "feature" if item.startswith("Q050-") else "fix",
                             "item": item, "detail": f"deepseek-v4.1-flash via {dsh_provider(pid)} (DeepSeek Harness)",
                             "min": et / 60})
        elif args.strip() == "minimax-code" and "opencode_free.py run" in ancestry(ppid):
            # 2026-10-20: MiniMax M3.1 Flash runs through MiniMax Code (qwen/MCODE-HARNESS.json, mcode/run-mcode.sh).
            # mcode renames its process to a bare "minimax-code", so the loop's runner as parent is what marks it
            try:
                item = item_of(Path(f"/proc/{pid}/cwd").resolve())
            except OSError:
                item = "?"
            sessions.append({"who": "MiniMax", "role": "feature" if item.startswith("Q050-") else "fix",
                             "item": item, "detail": "opencode-go/minimax-m3.1-flash-free (MiniMax Code)", "min": et / 60})
    return sorted(sessions, key=lambda s: (s["who"], s["role"], -s["min"]))


def verdicts_24h():
    since = datetime.datetime.now() - datetime.timedelta(hours=24)
    c = {"accepted": 0, "confirmed": 0, "rework": 0, "parked": 0, "features accepted": 0}
    for t, msg in tail_events(Q / "logs/qa.log", 0):
        if t < since:
            continue
        head = msg.split(":", 1)
        rest = head[1] if len(head) > 1 else ""
        if re.match(r"\s*ACCEPT", rest) and "kept for" in rest:
            c["features accepted" if head[0].startswith("Q050-") else "accepted"] += 1
        elif re.match(r"\s*CONFIRM", rest):
            c["confirmed"] += 1
        elif "PARKED" in rest:
            c["parked"] += 1
        elif "REWORK - sent back" in rest:
            c["rework"] += 1
    return c


_PROGRESS = {"at": 0.0, "value": None}


def progress():
    """Owner 2026-10-19: "add an eta and a tracker ... how many bugs have been fixed, and based on the last 24 hours how long
    it will take to fix the rest, same with features". Bugs: a bug counts once, at its first accepted verdict (a fix or a
    confirmed already-fixed / false positive; port jobs and batch members fold into the bug they belong to); remaining =
    the fix loop's own queue (qwenfix.load_queue) minus those. Features: plans/queue-050.jsonl units; done = accepted or
    built on the fix base. Rates are the last 24 h; the bug ETA ignores newly found bugs, which are shown beside it.
    Cached for 2 minutes (the queue read takes most of a second)."""
    now = time.time()
    if _PROGRESS["value"] is not None and now - _PROGRESS["at"] < 120:
        return _PROGRESS["value"]
    day = now - 86400
    first = {}
    for f in (Q / "state").glob("*.json"):
        r = jload(f, {}) or {}
        if r.get("outcome") != "accepted":
            continue
        t = f.stat().st_mtime
        for c in [str(r.get("cluster"))] + [str(b.get("cluster")) for b in (r.get("item") or {}).get("batch") or []]:
            c = re.sub(r"(-port)+$", "", c)
            first[c] = min(first.get(c, t), t)
    bug_done = {c: t for c, t in first.items() if not c.startswith(("Q050-", "test-"))}
    ports_24h = sum(1 for f in (Q / "state").glob("*-port.json")
                    if f.stat().st_mtime >= day and (jload(f, {}) or {}).get("outcome") == "accepted")
    try:
        sys.path.insert(0, str(Q))
        import qwenfix
        queue = [str(i["cluster"]) for i in qwenfix.load_queue()]
    except Exception:
        queue = []
    bug_left = [c for c in queue if c not in bug_done]
    found = 0
    for r in jrows(PROG / "research/reviews/luna-new-triage/TRIAGED-PASSES"):
        if r.get("triaged", "") >= datetime.datetime.fromtimestamp(day).isoformat(timespec="seconds"):
            found += int(r.get("queued") or 0)
    units = {r["queue_id"] for r in jrows(PROG / "plans/queue-050.jsonl")}
    built = set()
    try:
        base = (Q / "FIX-BASE").read_text().strip()
        log = subprocess.run(["git", "-C", str(PROG / "zene-cachyos"), "log", "--format=%s %b", base],
                             capture_output=True, text=True, errors="replace", timeout=20).stdout
        built = set(re.findall(r"Q050-\d{3}\b(?!\.)", log)) & units
    except (OSError, subprocess.SubprocessError):
        pass
    feat_first = {}
    for c, t in first.items():
        if c.startswith("Q050-"):
            unit = c.split(".")[0]
            if "." in c:
                continue        # a stage of a split unit: the unit counts when its last stage lands (SPLIT-UNITS.json)
            feat_first[unit] = min(feat_first.get(unit, t), t)
    for parent, stages in (jload(Q / "SPLIT-UNITS.json", {}) or {}).items():
        times = [first.get(st) for st in stages]
        if stages and all(times):
            feat_first[parent] = max(times)
    goals_file = PROG / "plans/DONATION-GOALS.md"
    goals = set(re.findall(r"^\| (Q050-\d{3}) \|", goals_file.read_text(), re.M)) if goals_file.exists() else set()
    feat_done = (set(feat_first) | built) & units
    feat_left = units - feat_done - goals

    def track(done_n, left_n, rate, extra=None):
        eta = None if rate <= 0 else left_n / rate          # days
        return {"done": done_n, "left": left_n, "rate": rate, "eta_days": eta, **(extra or {})}

    value = {
        "bugs": track(len(bug_done), len(bug_left), sum(1 for t in bug_done.values() if t >= day),
                      {"found": found, "ports": ports_24h}),
        "features": track(len(feat_done), len(feat_left), sum(1 for t in feat_first.values() if t >= day),
                          {"goals": len(goals & units - feat_done)}),
    }
    _PROGRESS.update(at=now, value=value)
    return value


def fmt_eta(days):
    if days is None:
        return "no progress in the last 24 h"
    when = datetime.datetime.now() + datetime.timedelta(days=days)
    span = f"{days * 24:.0f} h" if days < 2 else f"{days:.1f} days" if days < 60 else f"{days / 30.4:.0f} months"
    return f"{span} (around {when:%a %d %b})"


def last_line(path):
    ev = tail_events(path, 1)
    return f"{ev[0][0]:%H:%M} {ev[0][1]}" if ev else "-"


def collect():
    d = {"at": datetime.datetime.now()}
    d["fix_alive"] = alive(Q / "fix.pid", "qwenfix.py fix")
    d["qa_alive"] = alive(Q / "qa.pid", "qwenfix.py qa")
    d["draining"] = (Q / "STOP-FIX").exists()
    d["luna_cap"] = (Q / "LUNA-CAP").read_text().strip() if (Q / "LUNA-CAP").exists() else "?"
    d["sessions"] = processes()
    d["orch"] = jload(ORCH, {})
    loop_lanes = []
    for l in (jload(Q / "LANES.json", {}) or {}).get("lanes", []):
        held = None
        for f in (Q / "state").glob("Q050-*.json"):
            r = jload(f, {}) or {}
            if r.get("lane") == l.get("name") and not str(r.get("outcome", "")).lower().startswith(
                    ("accept", "confirm", "parked", "no-result", "driver-error", "build-failed")):
                held = f"{r.get('cluster')} ({r.get('outcome')}, round {r.get('round')})"
        loop_lanes.append({**l, "holds": held})
    d["loop_lanes"] = loop_lanes
    d["claude_limit"] = (Q / "CLAUDE-LIMIT-UNTIL").read_text().strip() if (Q / "CLAUDE-LIMIT-UNTIL").exists() else ""
    d["usage"] = jload(Q / "USAGE.json", {})
    d["codex"] = (jload(CODEX, {}) or {}).get("last_usage") or {}
    d["hold"] = (Q / "OPENCODE-GO-HOLD").read_text().strip() if (Q / "OPENCODE-GO-HOLD").exists() else ""
    d["fix_base"] = (Q / "FIX-BASE").read_text().strip() if (Q / "FIX-BASE").exists() else "?"
    d["qa_pending"] = len(list((Q / "qa/pending").glob("*.json")))
    d["rework"] = len(list((Q / "rework").glob("*.json")))
    d["fq"] = jrows(Q / "FEATURE-QUEUE.jsonl")
    d["cq"] = jrows(Q / "CLAUDE-QUEUE.jsonl")
    d["accepted_total"] = sum(1 for _ in open(Q / "ACCEPTED.tsv")) if (Q / "ACCEPTED.tsv").exists() else 0
    d["features_total"] = sum(1 for _ in open(Q / "FEATURES-ACCEPTED.tsv")) if (Q / "FEATURES-ACCEPTED.tsv").exists() else 0
    d["v24"] = verdicts_24h()
    d["workflow"] = jload(Q / "WORKFLOW-STATUS.json", {}) or {}
    d["progress"] = progress()
    d["schedule"] = []
    try:
        import opencode_free
        for m, e in ((jload(Q / "MODEL-SCHEDULE.json", {}) or {}).get("models") or {}).items():
            if m in ((jload(Q / "OPENCODE-FREE.json", {}) or {}).get("models") or []):
                d["schedule"].append((m, opencode_free.off_schedule(m), e.get("why", "")))
    except Exception:
        pass
    d["clef"] = last_line(Q / "logs/clef-retriage.log")
    d["scope"] = last_line(Q / "logs/feature-scope.log")
    d["briefs"] = len(list((PROG / "research/reviews/feature-briefs").glob("Q050-*.md")))
    events = tail_events(Q / "logs/fix.log", 14) + tail_events(Q / "logs/qa.log", 14)
    d["events"] = sorted(events)[-14:]
    bench = (jload(Q / "BENCHMARK.json", {}) or {}).get("models", {})
    try:
        sys.path.insert(0, str(Q))
        import importlib
        import benchmark as B
        if getattr(B, "_loaded_mtime", None) != (Q / "benchmark.py").stat().st_mtime:   # pick up edits without a restart
            B = importlib.reload(B)
            B._loaded_mtime = (Q / "benchmark.py").stat().st_mtime
        active = {k: B.active_models(k) for k in ("bugs", "features")}
    except Exception:
        active = {k: set(bench) for k in ("bugs", "features")}
    d["bench"] = {kind: sorted(((m, v[kind]) for m, v in bench.items() if m in active[kind] and v.get(kind, {}).get("decided", 0) >= 3),
                               key=lambda r: -(r[1].get("weighted_success_rate") or 0))
                  for kind in ("bugs", "features")}
    return d


def pct_bar(p, width=20):
    p = max(0.0, min(100.0, float(p or 0)))
    n = round(p / 100 * width)
    return "█" * n + "░" * (width - n)


def fmt_reset(iso):
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone()
        return t.strftime("%a %-d %b %H:%M")   # owner 2026-10-22: a full date ("Sun 15 Nov"), never just a weekday
    except ValueError:
        try:
            return datetime.datetime.fromtimestamp(float(iso)).strftime("%a %-d %b %H:%M")
        except (TypeError, ValueError):
            return "?"


# 2026-10-22: OpenDesign Go, the second DeepSeek provider (owner A/B test). It publishes no usage API, so these rows are
# our own DeepSeek Harness steps sent to it (qwen/DSH-USAGE.jsonl, provider "opendesign") at DeepSeek list prices
# (peak doubled), against the Go plan's published DeepSeek V4.1 Flash allowance. Subscribed 2026-10-22.
# 2026-10-24 (owner: "the week limit [is] not accurate ... i dont think theres a monthly limit, just a weekly limit"):
# the plan has a 5-hour and a 7-day limit only. The 7-day one ran out (402 insufficient_balance) at $30.08 of this
# estimate on 2026-10-23, so the week cap is $30, not $60; the month row is gone. The 5-hour cap was never hit, so its
# $20 is still unverified. When OpenDesign has refused for an exhausted plan (dsh/home/provider-limits.json, written by
# opencode_free.run_model_dsh), the week shows 100 % with OpenDesign's own reset time, and its 7-day window is anchored
# on that reset instead of rolling.
OPENDESIGN_LIMITS = (("5 h", 5 * 3600, 20.0), ("week", 7 * 86400, 30.0))
OPENDESIGN_SINCE = datetime.datetime(2026, 10, 22).timestamp()
OPENDESIGN_STATE = HOME / ".local/share/zene-bunny-fix/dsh/home/provider-limits.json"


def opendesign_rows():
    rows, now, spent = [], time.time(), []
    try:
        for line in (Q / "DSH-USAGE.jsonl").read_text().splitlines():
            if '"opendesign"' in line:
                e = json.loads(line)
                if e.get("provider") == "opendesign":
                    spent.append((float(e.get("t", 0)), float(e.get("cost") or 0)))
    except (OSError, ValueError):
        return rows
    if not spent:
        return [("OpenDesign Go", 0, "no DeepSeek steps sent yet · estimate, OpenDesign has no usage API")]
    limit = (jload(OPENDESIGN_STATE, {}) or {}).get("opendesign") or {}
    reset = float(limit.get("reset_at") or 0)
    for name, span, cap in OPENDESIGN_LIMITS:
        anchored = name == "week" and reset > now
        start = max(reset - span if anchored else now - span, OPENDESIGN_SINCE)
        used = sum(c for t, c in spent if t >= start)
        steps = sum(1 for t, c in spent if t >= start and c > 0)
        if anchored and limit.get("exhausted"):
            rows.append((f"OpenDesign {name}", 100, f"exhausted (OpenDesign refuses: 402) · resets {fmt_reset(reset)} · "
                         f"ran out at ${used:.2f} of ~${cap:.0f} est. · {steps} steps"))
            continue
        rows.append((f"OpenDesign {name}", min(100, round(100 * used / cap)),
                     f"${used:.2f} of ~${cap:.0f} DeepSeek (est. at list price) · {steps} steps"
                     + (f" · resets {fmt_reset(reset)}" if anchored else "")))
    return rows


def opendesign_value():
    """The OpenDesign line beside PLANS.json's value rows (owner 2026-10-22: "show the opendesign value thing"): tokens per
    1 % of the weekly allowance, from our measured tokens per list-price dollar, and what a full week is worth at list
    prices against the $10/month price. 2026-10-24: the week cap is $30 (where it ran out) and there is no month cap."""
    tok = out = cost = 0.0
    try:
        for line in (Q / "DSH-USAGE.jsonl").read_text().splitlines():
            if '"opendesign"' in line:
                e = json.loads(line)
                if e.get("provider") == "opendesign":
                    tok += sum(float(e.get(k) or 0) for k in ("in", "out", "cr", "cw"))
                    out += float(e.get("out") or 0)
                    cost += float(e.get("cost") or 0)
    except (OSError, ValueError):
        return None
    if cost <= 0:
        return None
    week_cap, price = OPENDESIGN_LIMITS[1][2], 10 * 12 / 52
    return {"tokens_per_percent": tok / cost * week_cap / 100, "output_per_percent": out / cost * week_cap / 100,
            "tokens_per_window": tok / cost * week_cap, "output_per_window": out / cost * week_cap,
            "api_value_per_window": week_cap, "price_per_window": price, "value_multiple": round(week_cap / price, 1)}


def ram_rows():
    """2026-10-22 (owner: "fix the ram issue"): /tmp is a RAM tmpfs that agent scratch filled to 15/16 GB, held by zram
    swap. Memory, /tmp and swap as bars, so a fill shows up here before it hurts."""
    try:
        m = {k: int(v.split()[0]) * 1024 for k, v in (l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines())}
        st = os.statvfs("/tmp")
    except (OSError, ValueError):
        return []
    tmp_size = st.f_blocks * st.f_frsize
    tmp_used = tmp_size - st.f_bfree * st.f_frsize
    used = m["MemTotal"] - m["MemAvailable"]
    swap = m["SwapTotal"] - m["SwapFree"]
    g = lambda n: f"{n / 2 ** 30:.1f} GB"
    rows = [("RAM in use", round(100 * used / m["MemTotal"]), f"{g(m['MemAvailable'])} available of {g(m['MemTotal'])}"),
            ("/tmp (RAM)", round(100 * tmp_used / tmp_size), f"{g(tmp_used)} of {g(tmp_size)} · agent scratch belongs in ~/.cache/zene-tmp")]
    if m["SwapTotal"]:
        rows.append(("Swap (zram)", round(100 * swap / m["SwapTotal"]), f"{g(swap)} of {g(m['SwapTotal'])}"))
    return rows


def usage_rows(d):
    rows = []
    cl = (d["usage"].get("claude") or {})
    if cl:
        rows.append(("Claude week", cl["seven_day"]["percent"], f"resets {fmt_reset(cl['seven_day']['resets_at'])}"
))
        rows.append(("Claude 5 h", cl["five_hour"]["percent"], f"resets {fmt_reset(cl['five_hour']['resets_at'])}"
                     if cl["five_hour"].get("resets_at") else ""))
    for w, v in ((d["usage"].get("opencode_go") or {}).items()):
        rows.append((f"OpenCode Go {w}", v.get("percent", 0), f"{v.get('status')} · resets {fmt_reset(v.get('resetsAt'))}"))
    rows += opendesign_rows()
    rows += ram_rows()
    cx = d["codex"]
    if cx:
        rows.append(("Luna (Codex) week", 100 - cx.get("weekly_remaining_percent", 100),
                     f"resets {fmt_reset(cx.get('weekly_resets_at'))} · banked resets {cx.get('available_resets')}"))
    return rows


def text(d):
    out = [f"Zene work status - {d['at']:%a %H:%M:%S}"]
    out.append(f"Loops: fix {'UP' if d['fix_alive'] else 'DOWN'}{' (draining)' if d['draining'] else ''}, "
               f"QA {'UP' if d['qa_alive'] else 'DOWN'}; Luna caps {d['luna_cap']}; base {d['fix_base']}")
    if d["hold"]:
        out.append(f"OpenCode Go HOLD: {d['hold']}")
    out.append("Usage:")
    for name, p, note in usage_rows(d):
        out.append(f"  {name:20} {pct_bar(p)} {p:5.1f} %  {note}")
    out.append("Claude lanes:")
    for lane in d["orch"].get("lanes", []) or [{"name": "none running"}]:
        out.append(f"  {lane.get('name')}: {lane.get('model', '')} {' '.join(lane.get('units', []))} "
                   f"{lane.get('state', '')} {lane.get('started', '')}")
    out.append(f"Agent sessions ({len(d['sessions'])}):")
    for s in d["sessions"]:
        out.append(f"  {s['who']:8} {s['role']:11} {s['item']:24} {s['detail']:38} {s['min']:5.0f} min")
    out.append(f"Queues: QA pending {d['qa_pending']}, reworks {d['rework']}, features (OpenCode) {len(d['fq'])}, "
               f"Claude queue {len(d['cq'])}")
    out.append(f"Last 24 h: {d['v24']}; all time: {d['accepted_total']} fix verdicts, {d['features_total']} features")
    delivered = d.get("workflow", {}).get("delivered", {})
    if delivered:
        out.append(f"Integrated on current train: {delivered.get('source_fixes',0)} source fixes, "
                   f"{delivered.get('test_improvements',0)} test improvements, {delivered.get('features',0)} features. "
                   f"Claim closures separately: {delivered.get('claims_closed',0)}; held issues: {delivered.get('held_issues',0)}.")

    pb, pf = d["progress"]["bugs"], d["progress"]["features"]
    out.append(f"Bugs: {pb['done']} fixed, {pb['left']} left ({pct_bar(100 * pb['done'] / max(pb['done'] + pb['left'], 1))}); "
               f"last 24 h {pb['rate']} fixed (+{pb['ports']} ports), {pb['found']} new found; ETA {fmt_eta(pb['eta_days'])}")
    out.append(f"Features: {pf['done']} built, {pf['left']} left ({pct_bar(100 * pf['done'] / max(pf['done'] + pf['left'], 1))}); "
               f"last 24 h {pf['rate']} accepted; ETA {fmt_eta(pf['eta_days'])}" + (f"; {pf['goals']} parked as donation goals" if pf['goals'] else ""))
    for t in d["orch"].get("trains", []):
        out.append(f"Train: {t}")
    out.append(f"Clef re-triage: {d['clef']}")
    out.append(f"Scoping ({d['briefs']} briefs): {d['scope']}")
    out.append("Recent:")
    out += [f"  {t:%H:%M} {m[:150]}" for t, m in d["events"]]
    return "\n".join(out)


CSS = """
body { background:#14161a; color:#d7dae0; font-family:'Noto Sans',sans-serif; font-size:10.5pt; }
h2 { color:#ffffff; margin:10px 0 4px 0; font-size:12pt; }
td, th { padding:2px 8px 2px 0; vertical-align:top; }
th { color:#8b93a1; text-align:left; font-weight:normal; }
.ok { color:#7bd88f; } .bad { color:#ff6b6b; } .warn { color:#ffcc66; } .dim { color:#8b93a1; }
.mono { font-family:'Noto Sans Mono',monospace; }
"""


def esc(x):
    return html.escape(str(x))


def page(d):
    def state(ok, up="running", down="stopped"):
        return f"<span class='{'ok' if ok else 'bad'}'>{up if ok else down}</span>"
    h = [f"<html><head><style>{CSS}</style></head><body>"]
    h.append(f"<div class='dim'>updated {d['at']:%a %H:%M:%S} · refreshes every 15 s · <a href='zs:list' style='color:#7cb7ff'>All bugs &amp; features</a> "
             f"(history, repeats, escalations)</div>")
    notes = d["orch"].get("notes") or []
    if notes:
        h.append("<h2>Now</h2><ul>" + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul>")
    h.append("<h2>Loops</h2><table>")
    h.append(f"<tr><th>Fix loop</th><td>{state(d['fix_alive'])}{' <span class=warn>draining for a restart</span>' if d['draining'] else ''}</td></tr>")
    h.append(f"<tr><th>QA loop</th><td>{state(d['qa_alive'])}</td></tr>")
    h.append(f"<tr><th>Luna slots</th><td>{esc(d['luna_cap'])}</td></tr>")
    h.append(f"<tr><th>Base line</th><td>{esc(d['fix_base'])}</td></tr>")
    if d["hold"]:
        h.append(f"<tr><th>OpenCode Go</th><td class='warn'>on hold: {esc(d['hold'])}</td></tr>")
    for m, blocked, why in d.get("schedule", []):
        h.append(f"<tr><th>Schedule</th><td>{esc(m.split('/')[-1])}: "
                 + (f"<span class='warn'>paused - {esc(blocked)} ({esc(why)})</span>" if blocked else "<span class='ok'>allowed now</span>")
                 + "</td></tr>")
    h.append("</table>")
    h.append("<h2>Usage</h2><table>")
    pl = jload(Q / "PLANS.json", {}) or {}

    def mt(n):
        return "-" if n is None else f"{n / 1e6:.0f} M" if n >= 1e7 else f"{n / 1e6:.1f} M" if n >= 1e6 else f"{n / 1e3:.0f} k"
    for p_ in pl.get("plans", []):
        if p_.get("status") == "ok":
            h.append(f"<tr><th>{esc(p_['plan'])}</th><td class='dim'>1 % = {mt(p_['tokens_per_percent'])} tokens "
                     f"({mt(p_['output_per_percent'])} output)</td><td class='dim'>full week {mt(p_['tokens_per_window'])} "
                     f"({mt(p_['output_per_window'])} output) = ${p_['api_value_per_window']:.0f} at API prices for "
                     f"${p_['price_per_window']:.2f} (x{p_['value_multiple']})</td></tr>")
    od = opendesign_value()
    if od:
        h.append(f"<tr><th>OpenDesign (DeepSeek)</th><td class='dim'>1 % = {mt(od['tokens_per_percent'])} tokens "
                 f"({mt(od['output_per_percent'])} output)</td><td class='dim'>full week {mt(od['tokens_per_window'])} "
                 f"({mt(od['output_per_window'])} output) = ${od['api_value_per_window']:.0f} at API prices for "
                 f"${od['price_per_window']:.2f} (x{od['value_multiple']}) · est. from our steps</td></tr>")
    for name, p, note in usage_rows(d):
        cls = "bad" if p >= 95 else "warn" if p >= 80 else "ok"
        h.append(f"<tr><th>{esc(name)}</th><td class='mono {cls}'>{pct_bar(p)} {p:.0f} %</td><td class='dim'>{esc(note)}</td></tr>")
    h.append("</table>")
    h.append("<h2>Claude lanes</h2><table>")
    for l in d.get("loop_lanes", []):
        state = "disabled" if not l.get("enabled", True) else (f"holds {l['holds']}" if l.get("holds") else "free / building")
        h.append(f"<tr><th>{esc(l.get('name'))}</th><td>{esc(l.get('model'))} {esc(l.get('effort', ''))}</td>"
                 f"<td>tiers {esc(','.join(l.get('tiers') or []))}</td><td>{esc(state)}</td><td class='dim'>loop lane (LANES.json)</td></tr>")
    if d.get("claude_limit"):
        h.append(f"<tr><td colspan=5 class='warn'>Claude usage limit: lanes paused until {esc(d['claude_limit'])}</td></tr>")
    lanes = d["orch"].get("lanes") or []
    if not lanes and not d.get("loop_lanes"):
        h.append("<tr><td class='dim'>none running</td></tr>")
    for lane in lanes:
        h.append(f"<tr><th>{esc(lane.get('name'))}</th><td>{esc(lane.get('model', ''))}</td>"
                 f"<td>{esc(' '.join(lane.get('units', [])))}</td><td>{esc(lane.get('state', ''))}</td>"
                 f"<td class='dim'>since {esc(lane.get('started', ''))}</td></tr>")
    h.append("</table>")
    h.append(f"<h2>Agent sessions ({len(d['sessions'])})</h2><table><tr><th>who</th><th>doing</th><th>item</th><th>model / effort</th><th>min</th></tr>")
    for s in d["sessions"]:
        h.append(f"<tr><td>{esc(s['who'])}</td><td>{esc(s['role'])}</td><td>{esc(s['item'])}</td>"
                 f"<td class='dim'>{esc(s['detail'])}</td><td>{s['min']:.0f}</td></tr>")
    h.append("</table>")
    wf = d.get("workflow", {})
    delivered = wf.get("delivered", {})
    h.append("<h2>Delivered on current train</h2><table>")
    if wf:
        h.append(f"<tr><th>Integrated improvements</th><td>{delivered.get('source_fixes',0)} source fixes · "
                 f"{delivered.get('test_improvements',0)} test improvements · {delivered.get('features',0)} features</td></tr>")
        h.append(f"<tr><th>Separate closures</th><td>{delivered.get('claims_closed',0)} already-fixed / false-positive claims; "
                 f"{delivered.get('held_issues',0)} issues held by prerequisites or budgets</td></tr>")
        totals = wf.get("totals", {})
        h.append(f"<tr><th>Recorded efficiency</th><td>{esc(totals.get('integrated_improvements_per_million_reported_tokens'))} "
                 f"integrated improvements per million reported tokens</td></tr>")
        h.append(f"<tr><th>Evidence coverage</th><td class='dim'>{esc(wf.get('generated'))}: "
                 f"{totals.get('token_logs',0)} logs; tokens include input/cache and do not cover all reviews. "
                 f"Integration is checked against {esc(wf.get('base'))}.</td></tr>")
    else:
        h.append("<tr><td class='dim'>Delivery report not generated yet.</td></tr>")
    rollout = wf.get("since_rollout", {})
    if rollout:
        h.append(f"<tr><th>Since workflow rollout</th><td>{rollout.get('newly_integrated_issues',0)} newly integrated issues · "
                 f"{rollout.get('work_sessions',0)} work sessions · {rollout.get('review_sessions',0)} reviews · "
                 f"{rollout.get('recorded_session_minutes',0)} recorded session minutes</td></tr>")
    h.append("</table>")
    h.append("<h2>Accepted queue progress</h2><table>")
    for kind, label, verb in (("bugs", "Bugs", "accepted closures"), ("features", "0.5.0 features", "accepted units")):
        pr = d["progress"][kind]
        total = max(pr["done"] + pr["left"], 1)
        pc_done = 100 * pr["done"] / total
        note = (f"last 24 h: {pr['rate']} {verb}" + (f" (+{pr['ports']} port jobs re-landing earlier fixes)" if pr.get("ports") else "")
                + (f", {pr['found']} new bugs found" if kind == "bugs" else "")
                + (f" · {pr['goals']} parked as donation goals" if pr.get("goals") else ""))
        eta_cls = "dim" if pr["eta_days"] is None else "ok" if pr["eta_days"] < 14 else "warn"
        h.append(f"<tr><th>{label}</th><td class='mono ok'>{pct_bar(pc_done, 24)} {pc_done:.0f} %</td>"
                 f"<td>{pr['done']:,} {verb} · {pr['left']:,} left</td></tr>")
        h.append(f"<tr><th></th><td class='{eta_cls}'>ETA {esc(fmt_eta(pr['eta_days']))}</td><td class='dim'>{esc(note)}</td></tr>")
        if kind == "bugs" and pr["found"] > pr["rate"]:
            h.append("<tr><th></th><td colspan=2 class='warn'>more bugs were found than fixed in the last 24 h - "
                     "the backlog is growing, so this ETA is optimistic</td></tr>")
    h.append("</table>")
    v = d["v24"]
    h.append("<h2>Results</h2><table>")
    h.append(f"<tr><th>Last 24 h</th><td><span class='ok'>{v['accepted']} fixes accepted</span>, {v['confirmed']} confirmed already fixed, "
             f"<span class='ok'>{v['features accepted']} features accepted</span>, {v['rework']} sent back, "
             f"<span class='warn'>{v['parked']} parked</span></td></tr>")
    h.append(f"<tr><th>All time</th><td>{d['accepted_total']} fix verdicts accepted/confirmed, {d['features_total']} features accepted</td></tr>")
    h.append(f"<tr><th>Queues</th><td>QA pending {d['qa_pending']} · reworks {d['rework']} · features for OpenCode {len(d['fq'])} · "
             f"Claude queue {len(d['cq'])} ({sum(r.get('tier') == 'sonnet' for r in d['cq'])} Sonnet, {sum(r.get('tier') == 'opus' for r in d['cq'])} Opus)</td></tr>")
    for t in d["orch"].get("trains", []):
        h.append(f"<tr><th>Train</th><td>{esc(t)}</td></tr>")
    h.append(f"<tr><th>Clef re-triage</th><td class='dim'>{esc(d['clef'])}</td></tr>")
    h.append(f"<tr><th>Scoping briefs</th><td class='dim'>{d['briefs']} written · {esc(d['scope'])}</td></tr>")
    h.append("</table>")
    h.append("<h2>Recent events</h2><table>")
    for t, m in reversed(d["events"]):
        h.append(f"<tr><td class='dim'>{t:%H:%M}</td><td>{esc(m[:160])}</td></tr>")
    h.append("</table>")
    for kind, title in (("bugs", "Bug fixing"), ("features", "Feature building")):
        h.append(f"<h2>Benchmark: {title}</h2><div class='dim'>models in use, ≥3 decided; difficulty-weighted counts each task "
                 f"1× (easiest) to 3× (hardest) along Clef's curve</div><table><tr><th>model</th><th>decided</th><th>accepted</th>"
                 f"<th>difficulty-weighted</th><th>mean difficulty</th><th>first pass</th><th>min/attempt</th></tr>")
        for m, r in d["bench"].get(kind, []):
            def pc(x):
                return "-" if x is None else f"{x * 100:.0f} %"
            h.append(f"<tr><td>{esc(m)}</td><td>{r.get('decided')}</td><td>{pc(r.get('success_rate'))}</td>"
                     f"<td>{pc(r.get('weighted_success_rate'))}</td><td>{r.get('mean_difficulty') or '-'}</td>"
                     f"<td>{pc(r.get('first_pass_rate'))}</td><td>{r.get('median_minutes_per_attempt') or '-'}</td></tr>")
        h.append("</table>")
    h.append("</body></html>")
    return "".join(h)


# ------------------------------------------------------------------ bugs & features browser
# Owner 2026-10-21: "add the option to pull up the full list of bugs and features, and see their history on the status
# checker, like if they've been repeated or anything". Read-only. Every parsed file is cached by mtime, so a refresh only
# re-reads what changed (first build ~1 s, later ones tens of ms). A unit and its -port / -port-port jobs are one history.
_HC = {}                # path -> (mtime_ns, parsed)
_HIST = {"at": 0.0, "value": None, "sig": None}
STATUSES = ("queued", "fixing", "in review", "rework", "accepted", "merged", "parked", "deferred", "escalated")
REJECT = ("REWORK", "DISPUTE", "REJECT")


def cached(path, parse):
    path = path if isinstance(path, str) else str(path)
    try:
        m = os.stat(path).st_mtime_ns
    except OSError:
        _HC.pop(path, None)
        return None
    e = _HC.get(path)
    if e and e[0] == m:
        return e[1]
    try:
        v = parse(path)
    except (OSError, ValueError, KeyError, TypeError):
        v = None
    _HC[path] = (m, v)
    return v


def epoch(s):
    try:
        return datetime.datetime.fromisoformat(str(s)).timestamp()
    except ValueError:
        return 0.0


def root_of(u):
    return re.sub(r"(-port)+$", "", u)


def short_model(m):
    return re.sub(r"^(opencode-go/|opencode/|claude-)", "", str(m or "?")).replace("-flash-free", "-free")


def slim_state(p):
    r = json.loads(Path(p).read_text())
    it = r.get("item") or {}
    return {"outcome": r.get("outcome"), "round": r.get("round") or 0, "title": r.get("title") or it.get("title"),
            "model": r.get("model_name") or r.get("model"), "lane": r.get("lane"), "esc": r.get("escalation"),
            "batch_of": r.get("batch_of"), "started": r.get("started"), "finished": r.get("finished"),
            "verdict": (r.get("qa") or {}).get("verdict"), "clef": it.get("clef_tier"), "tier": r.get("tier"),
            "branch": r.get("branch"), "batch_members": [str(b.get("cluster")) for b in it.get("batch") or []]}


def slim_record(p):
    r = json.loads(Path(p).read_text())
    it = r.get("item") if isinstance(r.get("item"), dict) else {}
    return {"model": r.get("model_name") or r.get("model"), "harness": r.get("harness"), "engine": r.get("engine"),
            "minutes": r.get("minutes"), "commits": r.get("commits") or [], "files": len(r.get("files") or []),
            "rc": r.get("rc"), "verdict": r.get("qwen_verdict"), "outcome": r.get("outcome"), "neg": r.get("negctl"),
            "started": r.get("started"), "finished": r.get("finished"), "title": r.get("title") or it.get("title"),
            "batch": r.get("batch_of"), "leftovers": r.get("leftovers"), "round": r.get("round")}


def slim_rework(p):
    r = json.loads(Path(p).read_text())
    return {"round": r.get("round"), "notes": r.get("notes") or ""}


def slim_qa(p):
    r = json.loads(Path(p).read_text())
    q = r.get("qa") or {}
    return {"verdict": q.get("verdict"), "summary": q.get("summary") or "", "blocking": q.get("blocking") or [],
            "reviewer": q.get("reviewer_model") or q.get("reviewer"), "minutes": q.get("minutes"),
            "review": q.get("review"), "notes": q.get("notes") or [], "title": r.get("title"),
            "dropped": bool(q.get("fix_dropped") or r.get("rereview"))}


def whole(p, kind):
    def parse(p):
        t = Path(p).read_text()
        if kind == "json":
            return json.loads(t)
        if kind == "rows":
            return [json.loads(l) for l in t.splitlines() if l.strip()]
        return [l.split("\t") for l in t.splitlines() if l.strip()]
    return cached(p, parse) if p.exists() else None


def train_merges():
    """branch -> (train, time) for every automatic/manual train merge reachable from FIX-BASE, by one git log (re-run only when it moves)."""
    cwd = PROG / "zene-cachyos"
    try:
        base = (Q / "FIX-BASE").read_text().strip()
        rev = subprocess.run(["git", "-C", str(cwd), "rev-parse", base], capture_output=True, text=True, timeout=10).stdout.strip()
        if _HIST.get("rev") == rev:
            return _HIST["merges"]
        log = subprocess.run(["git", "-C", str(cwd), "log", "--merges", "--format=%ct\t%s", rev],
                             capture_output=True, text=True, errors="replace", timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return _HIST.get("merges", {})
    out = {}
    for line in log.splitlines():
        t, _, s = line.partition("\t")
        m = re.match(r"Merge (?:bugs/qwen-|feat/050-|port8/)(\S+?)(?:-oc)? \(accepted[^;]*; (?:050/m0-)?(train-\d+)", s)
        if m:
            out.setdefault(m[1], (m[2], float(t)))
            continue
        m = re.match(r"merge\(m0-(train-\d+)\): (?:feat/050-|bugs/qwen-)(\S+?)(?:-oc)? ", s)
        if m:
            out.setdefault(m[2], (m[1], float(t)))
    _HIST.update(rev=rev, merges=out)
    return out


def port_log():
    """unit -> [(time, text)] from logs/merge-train.log: port jobs filed, conflicts, reverts, exclusions."""
    def parse(p):
        year, out = datetime.date.today().year, {}
        for line in Path(p).read_text(errors="replace").splitlines():
            m = TS.match(line)
            if not m:
                continue
            t = datetime.datetime.strptime(f"{year}-{m[1]}-{m[2]} {m[3]}", "%Y-%m-%d %H:%M:%S").timestamp()
            msg = m[4]
            u = (re.match(r"(?:feature )?(\S+?):? (?:port job|needs a port)", msg) or re.match(r"(?:feature )?(\S+) needs a port", msg)
                 or re.match(r"(\S+): the port job", msg))
            if u:
                out.setdefault(u[1], []).append((t, msg[:300]))
        return out
    return cached(Q / "logs/merge-train.log", parse) or {}


def history_index():
    """{root: unit-group dict} for every unit that has any history. Cheap when nothing changed (stats only)."""
    now = time.time()
    if _HIST["value"] is not None and now - _HIST["at"] < 5:
        return _HIST["value"]
    U = {}

    def unit(u):
        return U.setdefault(u, {"id": u, "state": None, "runs": {}, "qa": {}, "pending": set(), "rework": None, "parked": False,
                                "queue": None, "title": None, "arch": []})
    for e in os.scandir(Q / "state"):
        if e.name.endswith(".json"):
            unit(e.name[:-5])["state"] = cached(e.path, slim_state)
    for e in os.scandir(Q / "runs"):
        if not e.is_dir():
            continue
        if e.name.startswith("_"):
            for s in os.scandir(e.path):
                m = re.match(r"(.+)\.r(\d+)(?:-\d+)?$", s.name)
                if m and s.is_dir():
                    rec = cached(s.path + "/record.json", slim_record)
                    if rec:
                        unit(m[1])["arch"].append((int(m[2]), e.name, s.path, rec))
            continue
        m = re.match(r"(.+)\.r(\d+)$", e.name)
        if m:
            rec = cached(e.path + "/record.json", slim_record)
            unit(m[1])["runs"][int(m[2])] = (e.path, rec, e.stat().st_mtime)
    for e in os.scandir(Q / "qa/done"):
        m = re.match(r"(.+)\.r(\d+)\.json$", e.name)
        if m:
            q = cached(e.path, slim_qa)
            if q:
                unit(m[1])["qa"][int(m[2])] = (q, e.stat().st_mtime, e.path)
    for e in os.scandir(Q / "qa/pending"):
        m = re.match(r"(.+)\.r(\d+)\.json$", e.name)
        if m:
            unit(m[1])["pending"].add(int(m[2]))
    for e in os.scandir(Q / "rework"):
        if e.name.endswith(".json"):
            unit(e.name[:-5])["rework"] = cached(e.path, slim_rework) or {}
    for e in os.scandir(Q / "parked"):
        if e.name.endswith(".json") and e.is_file():
            unit(e.name[:-5])["parked"] = True
    for qf in ("FEATURE-QUEUE.jsonl", "CLAUDE-QUEUE.jsonl"):
        for r in whole(Q / qf, "rows") or []:
            c = str(r.get("queue_id") or r.get("cluster"))
            unit(c)["queue"] = r
            unit(c)["title"] = r.get("title")
    acc = {}
    for f in ("ACCEPTED.tsv", "FEATURES-ACCEPTED.tsv"):
        for row in whole(Q / f, "tsv") or []:
            acc[row[0]] = row
    for row in acc.values():
        if row[0] not in U and re.fullmatch(r"Q050-[\d.]+", row[0]):
            unit(row[0])
    be, fe = whole(Q / "BUG-ESCALATIONS.json", "json") or {}, whole(Q / "ESCALATIONS.json", "json") or {}
    rank = whole(Q / "BUG-RANK.json", "json") or {}
    rej = {}
    for r in whole(Q / "REJECTIONS.jsonl", "rows") or []:
        rej.setdefault(str(r.get("cluster")), []).append(r)
    lanes = {}
    for r in whole(Q / "CLAUDE-LANES.jsonl", "rows") or []:
        lanes.setdefault(str(r.get("cluster")), []).append(r)
    try:
        excl = {re.sub(r"(^(bugs/qwen-|feat/050-))|(-oc$)", "", l.strip()) for l in (Q / "merge/MERGE-EXCLUDE.txt").read_text().splitlines()}
    except OSError:
        excl = set()
    merges, plog = train_merges(), port_log()
    for k in list(be) + list(fe) + list(rej) + list(lanes) + list(plog):
        unit(k)
    G = {}
    for u, x in U.items():
        x["be"], x["fe"], x["rej"], x["lanes"] = be.get(u), fe.get(u), rej.get(u, []), lanes.get(u, [])
        x["plog"], x["rank"], x["acc"], x["excluded"] = plog.get(u, []), rank.get(u), acc.get(u), u in excl
        x["merge"] = merges.get(u)
        G.setdefault(root_of(u), {"id": root_of(u), "units": []})["units"].append(x)
    for g in G.values():
        g["units"].sort(key=lambda x: (len(x["id"]), x["id"]))
        summarize(g, U, merges)
    _HIST.update(at=now, value=G)
    return G


def unit_status(x, U, merges):
    st = x["state"] or {}
    lead = st.get("batch_of")
    if lead and lead in U and lead != x["id"]:
        x = U[lead]          # a batch member is landed, reviewed and merged with its lead
        st = x["state"] or {}
    o = st.get("outcome") or ""
    if x["merge"]:
        return f"merged {x['merge'][0]}"
    if x["pending"] or o == "in-qa":
        return "in review"
    if any(rec is None and time.time() - mt < 4 * 3600 for _, rec, mt in x["runs"].values()):
        return "fixing"
    if o == "deferred":
        return "deferred"
    if x["parked"] or o.startswith("parked"):
        return "parked"
    if o == "escalated":
        return "escalated"
    if o == "accepted" or (x["acc"] and not o):
        return "accepted"
    if x["rework"] is not None or o == "rework":
        return "rework"
    return "queued"


def summarize(g, U, merges):
    us = g["units"]
    top = us[-1]
    g["kind"] = "feature" if g["id"].startswith("Q050-") else "bug"
    g["title"] = next((t for x in [us[0]] + us for t in (((x["state"] or {}).get("title")), x["title"]) if t), None) or \
        next((r["title"] for x in us for _, r, _ in x["runs"].values() if r and r.get("title")), None) or \
        next((r[4] for x in us if x["acc"] for r in [x["acc"]] if len(r) > 4), "(no title recorded)")
    g["status"] = unit_status(top, U, merges)       # the deepest port job is the live one
    rounds, models, rej, nesc, last = 0, [], set(), 0, 0.0
    for x in us:
        st = x["state"] or {}
        recs = [r for _, r, _ in x["runs"].values() if r] + [r for *_, r in x["arch"]]
        rounds += max(len(x["runs"]) + len(x["arch"]), st.get("round") or 0) + len(x["lanes"])
        for r in recs:
            if r.get("model") and short_model(r["model"]) not in models:
                models.append(short_model(r["model"]))
        for lr in x["lanes"]:
            if short_model(lr.get("model")) not in models:
                models.append(short_model(lr.get("model")))
        if st.get("model") and short_model(st["model"]) not in models:
            models.append(short_model(st["model"]))
        for n, (q, mt, _) in x["qa"].items():
            if q["verdict"] in REJECT:
                rej.add((x["id"], n))
        for r in x["rej"]:
            if not any(k[0] == x["id"] and k[1] == r.get("round") for k in rej):
                rej.add((x["id"], r.get("round"), "j"))
        h = ((x["be"] or {}).get("history") or []) + ((x["fe"] or {}).get("history") or [])
        nesc += len({str(e.get("at")) for e in h} | {str(e.get("at")) for e in (st.get("esc") or {}).get("history", [])})
        times = [mt for _, mt, _ in x["qa"].values()] + [mt for _, _, mt in x["runs"].values()] + [epoch(st.get("finished") or st.get("started"))] + \
            [epoch(r.get("finished") or r.get("started")) for r in recs] + [epoch(lr.get("finished") or lr.get("started")) for lr in x["lanes"]] + \
            ([x["merge"][1]] if x["merge"] else []) + [t for t, _ in x["plog"]]
        last = max([last] + times)
    g.update(rounds=rounds, models=models, rejections=len(rej), escalations=nesc, last=last, ports=len(us) - 1)
    lvl = [(x["be"] or {}).get("level") for x in us if x["be"]] or [None]
    tier = [(x["fe"] or {}).get("tier") for x in us if x["fe"]] or [(top["queue"] or {}).get("feature_tier")]
    g["level"] = f"L{max(l for l in lvl if l is not None)}" if any(l is not None for l in lvl) and g["kind"] == "bug" else \
        (tier[-1] or "") if g["kind"] == "feature" else ""
    g["repeated"] = g["rounds"] > 2 or g["rejections"] > 1 or g["ports"] > 0 or g["escalations"] > 0
    g["flags"] = [f for f, on in ((f"{g['rounds']} rounds", g["rounds"] > 2), (f"{g['rejections']} rejections", g["rejections"] > 1),
                                  (f"{g['ports']} port job{'s' * (g['ports'] > 1)}", g["ports"] > 0),
                                  (f"escalated x{g['escalations']}", g["escalations"] > 0)) if on]


def ago(t):
    if not t:
        return "-"
    s = time.time() - t
    return f"{s / 60:.0f} min ago" if s < 5400 else f"{s / 3600:.0f} h ago" if s < 172800 else f"{s / 86400:.0f} d ago"


def stamp(t):
    return datetime.datetime.fromtimestamp(t).strftime("%d %b %H:%M") if t else "-"


NEW = {"kind": "all", "status": "all", "troubled": False, "q": "", "sort": "activity", "page": 0}
PAGE = 400
SORTS = {"activity": lambda g: -g["last"], "rounds": lambda g: (-g["rounds"], -g["last"]), "rejections": lambda g: (-g["rejections"], -g["last"]),
         "id": lambda g: g["id"], "status": lambda g: (g["status"], -g["last"])}


def filtered(G, f):
    q = f["q"].strip().lower()
    out = [g for g in G.values()
           if (f["kind"] == "all" or g["kind"] == f["kind"])
           and (f["status"] == "all" or skey(g["status"]) == f["status"])
           and (not f["troubled"] or g["repeated"])
           and (not q or q in g["title"].lower() or any(q in x["id"].lower() for x in g["units"]))]
    return sorted(out, key=SORTS.get(f["sort"], SORTS["activity"]))


def skey(s):
    return "merged" if s.startswith("merged") else s


def stclass(s):
    return "ok" if s.startswith(("merged", "accepted")) else "warn" if s in ("rework", "parked", "deferred", "escalated") else "dim" if s == "queued" else ""


def list_page(d, f, paged=True):
    G = history_index()
    rows = filtered(G, f)
    cnt = {s: sum(1 for g in G.values() if skey(g["status"]) == s) for s in STATUSES}

    def pick(key, val, label, cur):
        return f"<b>{esc(label)}</b>" if cur == val else f"<a href='zs:set/{key}/{val}'>{esc(label)}</a>"
    h = [f"<html><head><style>{CSS} a {{ color:#7cb7ff; }} .row td {{ border-top:1px solid #23262d; }}</style></head><body>",
         f"<div><a href='zs:main'>&larr; status</a> &nbsp; <span class='dim'>updated {d['at']:%H:%M:%S} · {len(G):,} units with history, "
         f"{len(rows):,} shown</span></div><h2>All bugs &amp; features</h2>",
         "<div>Show: " + " · ".join(pick("kind", k, l, f["kind"]) for k, l in (("all", "all"), ("bug", "bugs"), ("feature", "features"))) + "</div>",
         "<div>Status: " + " · ".join(pick("status", "all", "any", f["status"]) if s == "all" else pick("status", s, f"{s} ({cnt[s]})", f["status"])
                                      for s in ("all",) + STATUSES) + "</div>",
         "<div>" + (f"<b class='warn'>repeated / troubled only: on</b> (<a href='zs:set/troubled/0'>show all</a>)" if f["troubled"]
                    else f"<a href='zs:set/troubled/1'>repeated / troubled only</a> <span class='dim'>(&gt;2 rounds, &gt;1 rejection, port jobs or escalations)</span>") + "</div>",
         "<div>Sort: " + " · ".join(pick("sort", k, l, f["sort"]) for k, l in (("activity", "last activity"), ("rounds", "rounds"),
                                                                              ("rejections", "rejections"), ("id", "id"), ("status", "status"))) + "</div>"]
    if f["q"]:
        h.append(f"<div>Search: <b>{esc(f['q'])}</b> (<a href='zs:set/q/'>clear</a>)</div>")
    start = f["page"] * PAGE if paged else 0
    shown = rows[start:start + PAGE] if paged else rows
    h.append("<table style='margin-top:6px' cellspacing=0><tr><th>id</th><th>title</th><th>status</th><th>rounds</th><th>models</th>"
             "<th>rej.</th><th>esc.</th><th>last</th><th></th></tr>")
    for g in shown:
        mark = f"<span class='warn'>&#8635; {esc(', '.join(g['flags']))}</span>" if g["repeated"] else ""
        h.append(f"<tr class='row'><td class='mono'><a href='zs:unit/{esc(g['id'])}'>{esc(g['id'])}</a></td><td>{esc(g['title'][:90])}</td>"
                 f"<td class='{stclass(g['status'])}'>{esc(g['status'])}</td><td>{g['rounds']}</td>"
                 f"<td class='dim'>{esc(', '.join(g['models'][:3]) + (' +%d' % (len(g['models']) - 3) if len(g['models']) > 3 else ''))}</td>"
                 f"<td>{g['rejections'] or ''}</td><td>{esc(g['level'])}{' x' + str(g['escalations']) if g['escalations'] else ''}</td>"
                 f"<td class='dim'>{esc(ago(g['last']))}</td><td>{mark}</td></tr>")
    h.append("</table>")
    if not rows:
        h.append("<div class='dim'>nothing matches</div>")
    if paged and len(rows) > PAGE:
        pages = (len(rows) + PAGE - 1) // PAGE
        h.append("<div style='margin-top:6px'>Page " + " ".join(f"<b>{i + 1}</b>" if i == f["page"] else f"<a href='zs:set/page/{i}'>{i + 1}</a>"
                                                               for i in range(pages)) + f" <span class='dim'>({PAGE} per page)</span></div>")
    h.append("</body></html>")
    return "".join(h)


def short_commit(c):
    return esc(str(c)[:110])


def unit_page(d, uid):
    G = history_index()
    g = G.get(root_of(uid))
    if not g:
        return f"<html><head><style>{CSS} a {{ color:#7cb7ff; }}</style></head><body><a href='zs:list'>&larr; list</a><h2>{esc(uid)}</h2><div class='bad'>no history for this unit</div></body></html>"
    ev = []          # (time, html)

    def add(t, body):
        ev.append((t or 0, body))
    for x in g["units"]:
        tag = f"<span class='mono dim'>{esc(x['id'])}</span> " if g["ports"] else ""
        for n, (path, rec, mt) in sorted(x["runs"].items()):
            if not rec:
                add(mt, f"{tag}<b>fix round {n}</b> <span class='warn'>running (no record yet)</span><br><span class='mono dim'>{esc(path)}</span>")
                continue
            neg = rec.get("neg")
            negs = "" if not neg else f" · negative control: <span class='{'ok' if neg.get('ok') else 'bad'}'>{'ok' if neg.get('ok') else 'failed'}</span> <span class='dim'>{esc(str(neg.get('detail', ''))[:200])}</span>"
            cm = "".join(f"<br>&nbsp;&nbsp;<span class='mono'>{short_commit(c)}</span>" for c in rec["commits"][:6]) + \
                (f"<br>&nbsp;&nbsp;<span class='dim'>+{len(rec['commits']) - 6} more</span>" if len(rec["commits"]) > 6 else "")
            add(epoch(rec.get("started")) or mt,
                f"{tag}<b>fix round {n}</b> · {esc(short_model(rec['model']))}"
                + (f" <span class='dim'>({esc(str(rec['harness'])[:70])})</span>" if rec.get("harness") else "")
                + f" · {rec['minutes'] or '?'} min · rc {esc(rec['rc'])} · fixer says <b>{esc(rec['verdict'] or rec['outcome'] or 'no verdict')}</b>"
                + f" · {len(rec['commits'])} commit{'s' * (len(rec['commits']) != 1)}, {rec['files']} files{negs}"
                + (f"<br><span class='dim'>batched under {esc(rec['batch'])}</span>" if rec.get("batch") else "")
                + cm + f"<br><span class='mono dim'>{esc(path)}</span>")
        for n, arch, path, rec in x["arch"]:
            add(epoch(rec.get("started")), f"{tag}<b>fix round {n}</b> <span class='dim'>(archived in {esc(arch)})</span> · {esc(short_model(rec['model']))} · "
                f"{rec['minutes'] or '?'} min · {esc(rec['verdict'] or rec['outcome'] or 'no verdict')}<br><span class='mono dim'>{esc(path)}</span>")
        for n, (q, mt, path) in sorted(x["qa"].items()):
            bl = "".join(f"<li><b>{esc(b.get('category', ''))}</b>: {esc(str(b.get('what', ''))[:420])}"
                         + (f"<br><span class='dim'>evidence: {esc(str(b.get('evidence', ''))[:380])}</span>" if b.get("evidence") else "") + "</li>"
                         for b in q["blocking"][:6])
            add(mt, f"{tag}<b>review of round {n}</b> · <b class='{'ok' if q['verdict'] in ('ACCEPT', 'ACCEPT-NOTE', 'ACCEPT-FIXED', 'CONFIRM') else 'warn'}'>"
                f"{esc(q['verdict'])}</b> · {esc(short_model(q['reviewer']))} · {q['minutes'] or '?'} min"
                + (f"<div>{esc(q['summary'][:500])}</div>" if q["summary"] else "") + (f"<ul>{bl}</ul>" if bl else "")
                + (f"<span class='mono dim'>{esc(q['review'])}</span>" if q.get("review") else ""))
        done_rounds = {n for n, (q, _, _) in x["qa"].items()}
        for r in x["rej"]:
            if r.get("round") in done_rounds and x["qa"][r["round"]][0]["blocking"]:
                continue
            add(epoch(r.get("at")), f"{tag}<b>evidenced rejection</b> (round {esc(r.get('round'))}, {esc(r.get('kind'))}) by {esc(short_model(r.get('reviewer')))}: "
                f"{esc(r.get('verdict'))}<ul>" + "".join(f"<li><b>{esc(b.get('category', ''))}</b>: {esc(str(b.get('what', ''))[:420])}</li>" for b in (r.get("blocking") or [])[:6]) + "</ul>")
        for n in sorted(x["pending"]):
            add(time.time(), f"{tag}<b>review of round {n}</b> <span class='warn'>waiting in qa/pending</span>")
        for lr in x["lanes"]:
            add(epoch(lr.get("finished") or lr.get("started")), f"{tag}<b>Claude round {esc(lr.get('round'))}</b> · {esc(short_model(lr.get('model')))} · "
                f"{lr.get('minutes') or '?'} min · {esc(lr.get('outcome'))} <span class='dim'>({esc(lr.get('lane', ''))})</span>")
        hist = {}
        for e in ((x["be"] or {}).get("history") or []) + ((x["fe"] or {}).get("history") or []) + ((x["state"] or {}).get("esc") or {}).get("history", []):
            hist.setdefault(str(e.get("at")), e)
        for e in hist.values():
            mv = f"level {e['to_level']}" if "to_level" in e else f"{e.get('from', '?')} to {e.get('to', '?')}"
            add(epoch(e.get("at")), f"{tag}<b class='warn'>escalated</b> to {esc(mv)}"
                + (f" at round {esc(e['round'])}" if e.get("round") else "") + f": {esc(str(e.get('reason') or e.get('outcome') or '')[:260])}"
                + (f"<br><span class='dim'>undone: {esc(str(e['undone'])[:220])}</span>" if e.get("undone") else ""))
        for t, msg in x["plog"]:
            add(t, f"{tag}<b>merge train</b> <span class='dim'>{esc(msg[:260])}</span>")
        if x["merge"]:
            add(x["merge"][1], f"{tag}<b class='ok'>merged onto {esc(x['merge'][0])}</b>")
        if x["excluded"]:
            add(time.time() - 1, f"{tag}<span class='warn'>listed in merge/MERGE-EXCLUDE.txt: kept off later trains</span>")
        if x["rework"]:
            add(time.time() - 1, f"{tag}<b>sent back</b> (rework file, next round {esc(x['rework'].get('round'))})<div class='dim'>{esc(str(x['rework'].get('notes'))[:500])}</div>")
    ev.sort(key=lambda e: e[0])
    top = g["units"][-1]
    st = top["state"] or {}
    h = [f"<html><head><style>{CSS} a {{ color:#7cb7ff; }} li {{ margin-bottom:3px; }}</style></head><body>",
         f"<div><a href='zs:main'>&larr; status</a> · <a href='zs:list'>&larr; all bugs &amp; features</a></div>",
         f"<h2><span class='mono'>{esc(g['id'])}</span> {esc(g['title'])}</h2>",
         "<table>",
         f"<tr><th>kind</th><td>{g['kind']}{' · ' + esc(g['level']) if g['level'] else ''}"
         + (f" · difficulty rank {g['units'][0]['rank'] * 100:.0f} %" if g["units"][0].get("rank") is not None else "")
         + (f" · clef {esc(st['clef'])}" if st.get("clef") else "") + "</td></tr>",
         f"<tr><th>now</th><td class='{stclass(g['status'])}'>{esc(g['status'])}</td></tr>",
         f"<tr><th>state</th><td>{esc(st.get('outcome') or 'no state file')}, round {esc(st.get('round') or '-')}, last activity {esc(stamp(g['last']))} ({esc(ago(g['last']))})</td></tr>",
         f"<tr><th>history</th><td>{g['rounds']} rounds · {g['rejections']} rejections · {g['escalations']} escalations · {g['ports']} port jobs"
         + (" <span class='warn'>&#8635; repeated</span>" if g["repeated"] else "") + "</td></tr>",
         f"<tr><th>models</th><td>{esc(', '.join(g['models']) or '-')}</td></tr>",
         "<tr><th>chain</th><td class='mono'>" + " &rarr; ".join(f"<a href='zs:unit/{esc(x['id'])}'>{esc(x['id'])}</a>" for x in g["units"]) + "</td></tr></table>",
         "<h2>Timeline, oldest first</h2><table>"]
    for t, body in ev:
        h.append(f"<tr><td class='dim' style='white-space:nowrap'>{esc(stamp(t) if t < time.time() - 5 else 'now')}</td><td>{body}</td></tr>")
    if not ev:
        h.append("<tr><td class='dim'>no recorded rounds or reviews yet</td></tr>")
    h.append("</table></body></html>")
    return "".join(h)


def window():
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QDesktopServices, QIcon
    from PySide6.QtWidgets import QApplication, QLineEdit, QTextBrowser, QVBoxLayout, QWidget
    app = QApplication(sys.argv)
    app.setApplicationName("Zene Status")
    app.setDesktopFileName("zene-status")
    win, view, search = QWidget(), QTextBrowser(), QLineEdit()
    win.setWindowTitle("Zene Status")
    win.setWindowIcon(QIcon.fromTheme("utilities-system-monitor"))
    box = QVBoxLayout(win)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(0)
    search.setPlaceholderText("search id or title (bugs & features list)")
    search.setClearButtonEnabled(True)
    box.addWidget(search)
    box.addWidget(view)
    view.setOpenLinks(False)        # zs: links switch views here; anything else goes to the browser
    win.resize(980, 1000)
    nav = {"view": "main", "uid": None, "f": dict(NEW), "shown": None}

    def render():
        if nav["view"] == "main":
            return page(collect())
        d = {"at": datetime.datetime.now()}
        return list_page(d, nav["f"]) if nav["view"] == "list" else unit_page(d, nav["uid"])

    def refresh():
        key = (nav["view"], nav["uid"], json.dumps(nav["f"], sort_keys=True))
        bar = view.verticalScrollBar().value() if key == nav["shown"] else 0       # the 15 s refresh keeps the place; navigating starts at the top
        search.setVisible(nav["view"] == "list")
        try:
            view.setHtml(render())
        except Exception as e:      # a half-written state file must not close the window
            view.setHtml(f"<pre>status error: {esc(repr(e))}</pre>")
        view.verticalScrollBar().setValue(bar)
        nav["shown"] = key

    def clicked(url):
        u = url.toString()
        if not u.startswith("zs:"):
            QDesktopServices.openUrl(url)
            return
        parts = u[3:].split("/", 2)
        if parts[0] == "main":
            nav["view"] = "main"
        elif parts[0] == "list":
            nav["view"] = "list"
        elif parts[0] == "unit":
            nav.update(view="unit", uid=parts[1] if len(parts) > 1 else "")
        elif parts[0] == "set" and len(parts) == 3:
            k, v = parts[1], parts[2]
            f = nav["f"]
            f[k] = (v == "1") if k == "troubled" else int(v) if k == "page" else v
            if k != "page":
                f["page"] = 0
            if k == "q":
                search.setText(v)
        refresh()

    def typed(t):
        nav["f"].update(q=t, page=0)
        refresh()

    view.anchorClicked.connect(clicked)
    search.textChanged.connect(typed)
    refresh()
    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(15000)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if "--text" in sys.argv:
        print(text(collect()))
    else:
        window()
