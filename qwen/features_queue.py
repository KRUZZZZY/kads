#!/usr/bin/env python3
"""Build FEATURE-QUEUE.jsonl: the 0.5.0 queue units the OpenCode fixers build (owner, 2026-10-15: "assign agents to
them based on length and difficulty"; Muse Spark keeps full worktree access).

Source: plans/queue-050.jsonl (units, size, priority) + the Clef difficulty routing in
research/reviews/clef-difficulty-2026-10-13/delegation.jsonl (start tier). The free/DeepSeek and Muse Spark tiers are
queued here; the Sonnet tier goes to Claude lanes. Units already built on the newest line (their id in its log), units
claimed by a lane (LANE-CLAIMED file, one id per line) and units whose dependency is not built yet are left out.
Order: priority, then easiest first. Re-run any time; qwenfix reads the file live (the QA loop re-runs it after
every verdict).

Escalation (Clef delegation policy: one tier up per failed two-round attempt): a unit whose state is parked is moved
up: free -> muse (re-queued here, same branch, rounds continue, the last review as notes); muse -> sonnet (written to
SONNET-QUEUE.jsonl for a Claude lane, and left out here). ESCALATIONS.json records each step.
"""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROG = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
LINE = PROG / "zene-cachyos"
# Owner 2026-10-19 ("scale everything down, so sonnet gets more muse stuff, muse gets more deepseek stuff, and deepseek
# gets less, and maybe opus gets some muse stuff"): the start tier comes from the unit's Clef difficulty percentile and
# the live cuts in FEATURE-TIER-CUTS.json (was: delegation.jsonl's start_tier, cuts ~0.32 / 0.92 and no Opus start).
CUTS_DEFAULT = {"free_below": 0.20, "muse_below": 0.65, "sonnet_below": 0.90}
# The bottom/middle tier models themselves come from FEATURE-TIERS.json (qwenfix); this is only the preference string.
PREFER = {"free": "opencode-go/mimo-v2.6-flash,mimo-v2.6-flash-free",
          "muse": "opencode-go/deepseek-v4.1-flash"}      # 2026-10-19 evening: DeepSeek V4.1 Flash 1017 replaces Muse Spark


def start_tier(pct):
    try:
        cuts = {**CUTS_DEFAULT, **json.loads((ROOT / "FEATURE-TIER-CUTS.json").read_text())}
    except (OSError, ValueError):
        cuts = CUTS_DEFAULT
    return ("free" if pct < cuts["free_below"] else "muse" if pct < cuts["muse_below"]
            else "sonnet" if pct < cuts["sonnet_below"] else "opus")


NEXT = {"free": "muse", "muse": "sonnet", "sonnet": "opus"}   # owner 2026-10-16: one tier per failure; sonnet/opus are Claude lanes


BRIEFS = PROG / "research/reviews/feature-briefs"


def split_stages(q):
    """Owner 2026-10-19 ("split oversized features automatically"): a scoping brief whose verdict is SPLIT ends with
    {"split": [{"title", "goal", "acceptance"}, ...]}; those become stage units Q050-xxx.1, .2, ... in build order."""
    f = BRIEFS / f"{q}.md"
    if not f.exists():
        return []
    text = f.read_text()
    if not re.search(r"\*\*SPLIT|verdict\W+SPLIT", text):
        return []
    for block in reversed(re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)):
        try:
            stages = json.loads(block).get("split") or []
        except ValueError:
            continue
        if len(stages) >= 2 and all(st.get("title") and st.get("acceptance") for st in stages):
            return stages
    return []


def donate(q, rec):
    """Append a unit the Opus tier could not finish to plans/DONATION-GOALS.md (skipped by main() from then on)."""
    goals = PROG / "plans/DONATION-GOALS.md"
    text = goals.read_text() if goals.exists() else ""
    if f"| {q} |" in text:
        return
    qa = rec.get("qa") or {}
    row = (f"| {q} | {rec.get('title', '')} | Top tier (Opus) could not finish it: {qa.get('verdict', rec.get('outcome'))} "
           f"after round {rec.get('round')} (added automatically {time.strftime('%Y-%m-%d')}). | `{rec.get('branch', '-')}`; "
           f"review {qa.get('review', '-')} | {str(qa.get('rework_instructions', '') or qa.get('summary', ''))[:300]} | owner to set |")
    lines = text.rstrip("\n").split("\n")
    last = max((i for i, l in enumerate(lines) if l.startswith("| Q050-")), default=len(lines) - 1)
    lines.insert(last + 1, row)
    goals.write_text("\n".join(lines) + "\n")


def escalate(tier_of):
    """Move parked feature units one tier up; returns {unit: {"tier": t, "round_base": n, "notes": text}}."""
    esc_path = ROOT / "ESCALATIONS.json"
    esc = json.loads(esc_path.read_text()) if esc_path.exists() else {}
    arch = ROOT / "parked/_escalated"
    arch.mkdir(parents=True, exist_ok=True)
    for state in (ROOT / "state").glob("Q050-*.json"):
        q = state.stem
        rec = json.loads(state.read_text())
        if not str(rec.get("outcome", "")).startswith(("parked", "no-result", "build-failed")):
            continue
        cur = esc.get(q, {}).get("tier") or tier_of.get(q)
        nxt = NEXT.get(cur)
        if nxt is None:
            # owner 2026-10-19 ("nothing should go into human review"): the top tier could not get it working, so it
            # becomes a donation goal by the owner's 10-16 rule (amount: owner to set) and leaves the queue
            if cur == "opus":
                donate(q, rec)
                arch = ROOT / "parked/_donation"
                arch.mkdir(parents=True, exist_ok=True)
                for src in (state, ROOT / "parked" / state.name):
                    if src.exists():
                        shutil.move(str(src), str(arch / f"{src.parent.name}-{src.stem}-{int(time.time())}.json"))
            continue
        qa = rec.get("qa") or {}
        notes = (f"This unit was attempted by another model ({rec.get('model')}) for {rec.get('round')} round(s) and did "
                 f"not pass review. Its commits are already on this branch; keep what is right, fix what the review asks.\n"
                 f"Last review verdict: {qa.get('verdict', rec.get('outcome'))}\n{qa.get('summary', '')}\n\n"
                 f"What to do:\n{qa.get('rework_instructions', '')}\nFull review: {qa.get('review', '-')}")
        step = {"tier": nxt, "round_base": int(rec.get("round") or 0), "notes": notes,
                "history": esc.get(q, {}).get("history", []) + [{"from": cur, "to": nxt, "at": time.strftime("%Y-%m-%dT%H:%M"),
                                                                  "outcome": rec.get("outcome"), "model": rec.get("model")}]}
        esc[q] = step
        for src in (state, ROOT / "parked" / state.name):
            if src.exists():
                shutil.move(str(src), str(arch / f"{src.parent.name}-{src.stem}-{int(time.time())}.json"))
        if nxt == "sonnet":
            with open(ROOT / "SONNET-QUEUE.jsonl", "a") as f:
                f.write(json.dumps({"queue_id": q, "branch": rec.get("branch"), "at": step["history"][-1]["at"],
                                    "notes": notes}) + "\n")
    esc_path.write_text(json.dumps(esc, indent=1) + "\n")
    return esc


def main():
    units = {r["queue_id"]: r for r in map(json.loads, open(PROG / "plans/queue-050.jsonl"))}
    route = {r["queue_id"]: r for r in map(json.loads,
             open(PROG / "research/reviews/clef-difficulty-2026-10-13/delegation.jsonl"))}
    base = (ROOT / "FIX-BASE").read_text().strip()   # 2026-10-19: was hard-wired to train 5
    built = set()
    log = subprocess.run(["git", "-C", str(LINE), "log", "--format=%s %b", base],
                         capture_output=True, text=True, errors="replace", check=True).stdout
    built |= set(re.findall(r"Q050-\d{3}(?:\.\d+)?", log)) & (set(units) | {f"{q}.{n}" for q in units for n in range(1, 6)})
    on_base = set(built)   # a dependency counts only once it is on the base a new branch starts from
    built |= set(l.split("\t")[0] for l in open(ROOT / "FEATURES-ACCEPTED.tsv")) if (ROOT / "FEATURES-ACCEPTED.tsv").exists() else set()
    claimed = set((ROOT / "LANE-CLAIMED").read_text().split()) if (ROOT / "LANE-CLAIMED").exists() else set()
    # owner 2026-10-16: features parked as donation goals (plans/DONATION-GOALS.md) are never queued
    goals = PROG / "plans/DONATION-GOALS.md"
    claimed |= set(re.findall(r"^\| (Q050-\d{3}) \|", goals.read_text(), re.M)) if goals.exists() else set()
    by_title = {r["title"]: q for q, r in units.items()}
    esc = escalate({q: start_tier(route[q]["pct"]) for q in units})
    splits = {}
    for q in list(units):
        if q in built or q in claimed or q in esc or (ROOT / "state" / f"{q}.json").exists():
            continue
        stages = split_stages(q)
        if not stages:
            continue
        parent = units.pop(q)
        splits[q] = []
        for n, st in enumerate(stages, 1):
            sid = f"{q}.{n}"
            units[sid] = {**parent, "queue_id": sid, "title": f"{parent.get('title')} - stage {n}/{len(stages)}: {st['title']}",
                          "goal": st.get("goal") or parent.get("goal"), "acceptance": st["acceptance"],
                          "depends_on": ([f"{q}.{n - 1}"] if n > 1 else list(parent.get("depends_on") or [])),
                          "split_of": q, "stage": n, "stages": len(stages)}
            route[sid] = route[q]
            splits[q].append(sid)
    (ROOT / "SPLIT-UNITS.json").write_text(json.dumps(splits, indent=1) + "\n")
    rows, claude = [], []
    for q, u in units.items():
        tier = esc.get(q, {}).get("tier") or start_tier(route[q]["pct"])
        if q in built or q in claimed:
            continue
        if tier not in PREFER:
            # Claude lanes (LANES.json, run by the fix loop since 2026-10-19) take units from CLAUDE-QUEUE.jsonl in
            # this order; a row is a full feature item, like FEATURE-QUEUE.jsonl's.
            deps = [by_title.get(t, t) for t in u.get("depends_on") or []]
            if tier in ("sonnet", "opus") and any(d not in on_base for d in deps):
                continue
            claude.append({"queue_id": q, "cluster": q, "kind": "feature", "worktree": str(LINE), "tier": tier,
                           "clef_tier": f"feature:{tier}", "feature_tier": tier, "difficulty": route[q]["clef_difficulty"],
                           "rank": route[q]["rank"], "pct": route[q]["pct"],
                           "round_base": esc.get(q, {}).get("round_base", 0), "escalation_notes": esc.get(q, {}).get("notes"),
                           "escalated": q in esc, "branch": f"feat/050-{q}-oc" if q in esc else None,
                           **{k: u.get(k) for k in ("title", "goal", "acceptance", "type", "size", "priority", "area",
                                                    "why", "member_ids")}})
            continue
        deps = [by_title.get(t, t) for t in u.get("depends_on") or []]
        if any(d not in on_base for d in deps):
            continue
        rows.append({"cluster": q, "kind": "feature", "worktree": str(LINE), "clef_tier": f"feature:{tier}",
                     "feature_tier": tier, "prefer_models": PREFER[tier], "difficulty": route[q]["clef_difficulty"],
                     "rank": route[q]["rank"], "round_base": esc.get(q, {}).get("round_base", 0),
                     "escalation_notes": esc.get(q, {}).get("notes"), **{k: u.get(k) for k in (
                         "title", "goal", "acceptance", "type", "size", "priority", "area", "why", "member_ids")}})
    rows.sort(key=lambda r: (r["priority"], r["rank"]))
    (ROOT / "FEATURE-QUEUE.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    # CLAUDE-FIRST.txt (one id per line, in order): units the owner wants before everything else, escalated ones
    # included (2026-10-19 owner approved the hotspot plan: "lets do it", then "allow singular claude lanes")
    first = (ROOT / "CLAUDE-FIRST.txt").read_text().split() if (ROOT / "CLAUDE-FIRST.txt").exists() else []
    claude.sort(key=lambda r: (first.index(r["queue_id"]) if r["queue_id"] in first else len(first),
                               not r["escalated"], r["priority"], r["rank"]))
    (ROOT / "CLAUDE-QUEUE.jsonl").write_text("".join(json.dumps(r) + "\n" for r in claude))
    print(f"{len(rows)} units queued ({sum(r['feature_tier'] == 'free' for r in rows)} free tier, "
          f"{sum(r['feature_tier'] == 'muse' for r in rows)} Muse tier); Claude queue {len(claude)} "
          f"({sum(r['tier'] == 'sonnet' for r in claude)} Sonnet, {sum(r['tier'] == 'opus' for r in claude)} Opus); "
          f"built {len(built)}, claimed {len(claimed)}")


if __name__ == "__main__":
    main()
