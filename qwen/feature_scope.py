#!/usr/bin/env python3
"""Scoping briefs for 0.5.0 feature units, written before a builder gets the unit.

Owner 2026-10-19: "are the features well scoped and explained before being fed to the models? or is it just like 'go
do this' ... models get a more comprehensive brief of what we want, and some pre-existing code that they can pull on".
Until now a builder got FEATURE-BRIEF.md (rules, build recipe) plus the unit's queue row (title, goal, acceptance).

Here a read-only Luna session (the plan with most headroom) reads the code at FIX-BASE (worktree zene-scope, detached,
code only) and the planning docs, and writes research/reviews/feature-briefs/<id>.md: the user-visible behaviour,
the existing code to build on (file:line), a proposed design, a test plan per acceptance criterion, pitfalls, and a
scope verdict. qwenfix.feature_brief inlines it for the OpenCode builders; Claude lane prompts inline it too.

Keeps AHEAD briefs ready in queue order (FEATURE-QUEUE.jsonl, then CLAUDE-QUEUE.jsonl, interleaved), WORKERS at a
time. Stop with STOP-SCOPE. Usage: feature_scope.py [--once] [--ahead N] [--workers N] [--only Q050-xxx ...]
"""
import argparse
import concurrent.futures
import datetime
import json
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROG = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
TREE = PROG / "zene-scope"
OUT = PROG / "research/reviews/feature-briefs"
BUG_OUT = PROG / "research/reviews/bug-briefs"   # owner 2026-10-19: "make sure the bug fixes are scoped well"
LOG = ROOT / "logs/feature-scope.log"

PROMPT = """You are scoping ONE feature unit of Zene Studio (an LMMS-derived DAW, C++20 / Qt, GPLv2+, with an agent
control surface: JSON-RPC commands registered in ControlRegistry and exposed to agents over MCP). You do NOT implement
it. A cheaper model (or Claude) will implement it next, alone, with only your brief, the project rules and the unit
below. Give it the WHAT and the WHERE, and leave the HOW open: the implementer is expected to make its own design
choices (UI layout, naming, internal structure, algorithm) and may depart from your suggestions whenever it still meets
the outcome and the rules. Be concrete about behaviour and about existing code; be light about design. Verify every
pointer, and keep the brief under ~150 lines.

The code to read is this working tree ({tree}, detached at {base}). Planning material (read what is relevant):
- {prog}/plans/SPEC-050.md (0.5.0 spec), {prog}/plans/PLAN-050-MASTER.md, {prog}/plans/QUEUE-050.md (the unit list
  with context), {prog}/research/ableton-gap/SPEC-zene-studio.md (product spec); grep {prog}/research and
  {prog}/plans for the unit's member ids ({members}) and title words to find where the requirement came from.
- Owner UX decisions: grep {prog}/plans for the area.
- Docs of units already built, as examples of finished work: {tree}/docs/050-features/*.md.
- Reviews of previous failed attempts at this unit, if any: {reviews}
- A previous attempt's commits, if any: `git log --oneline {base}..feat/050-{qid}-oc` / `git diff {base}...feat/050-{qid}-oc`
  (the next implementer continues on that branch: say what of it to keep and what is wrong, per the reviews).

## The unit
```json
{unit}
```

## Write the brief in exactly these sections (Markdown)
1. **Outcome**: what a musician sees and does when this is done (2-6 concrete scenarios, GUI and agent), and what is
   explicitly out of scope. Resolve vague words in the goal into checkable behaviour.
2. **Already there**: what of this exists today (partially or fully), with file:line. If the unit is already done,
   say so and stop after section 6.
3. **Build on**: the existing code to reuse, each with file:line and one line on how: the closest finished feature to
   copy the pattern from (command registration + schema + reversibility class, undo step, GUI menu/action wiring,
   project save/load, the matching test); helpers and models to call instead of writing new ones.
4. **Suggested approach (non-binding)**: a short sketch, at most ~15 lines: the few decisions that matter and one
   reasonable way to make them. Name only the hard constraints as constraints: any new or changed command's
   reversibility class (true_inverse / snapshot / irreversible / not_mutating), old-project compatibility, what must
   never run on the audio thread, and portability hazards (Qt 5.15 + Qt 6.8 MSVC + macOS < 10.15 builds). Do not
   write code or specify the change line by line; where a choice is open, say it is open.
5. **Test plan**: one row per acceptance criterion: what must be proven, the kind of test (QtTest unit /
   control-socket transcript test / render comparison) and the neighbouring test file whose pattern fits. Name the
   CMake targets and plug-in modules a test needs. The implementer designs the actual tests.
6. **Scope verdict**: one of `OK`, `SPLIT` (too big for one attempt: propose the split), `ALREADY-DONE`, or
   `UNCLEAR` (list the decisions a human must make), then size (S/M/L) and the 3 most likely ways an implementer
   gets it wrong. For `SPLIT`, end the brief with ONE fenced json block the queue turns into stages, each small
   enough for one attempt, independently mergeable and useful on its own, in build order (2-4 stages):
   ```json
   {{"split": [{{"title": "...", "goal": "...", "acceptance": ["...", "..."]}}, ...]}}
   ```
   Together the stages must cover every acceptance criterion of the unit.

Read-only: change nothing. Verify file:line pointers against this tree before writing them. Output only the brief.
"""


BUG_PROMPT = """You are scoping ONE reported bug in Zene Studio (an LMMS-derived DAW, C++20 / Qt, with a JSON-RPC agent
control surface). You do NOT fix it. A cheaper model will fix it next, alone, with your brief and the report below.
Keep the brief short (under ~60 lines): locate, confirm, explain; leave the fix itself to the fixer.

The code is this working tree ({tree}, detached at {base}). The report was written against revision {rev}
(`git show {rev}:<path>` shows the code it read); line numbers may have moved since.

## The report
```json
{item}
```

## Write exactly these sections (Markdown)
1. **Still present?** `YES`, `NO - already fixed` (cite the code at {base} that fixes it, file:line) or `NOT A BUG`
   (cite the contract or code that makes the behaviour intended). If not YES, stop after this section.
2. **Where**: the defect's current location(s), file:line at {base}, and the call path that reaches it.
3. **Why**: the root cause in two or three sentences.
4. **Fix direction (non-binding)**: one or two sentences; name constraints only (audio thread: no allocation, locks,
   waits, I/O or signals; behaviour others rely on; Qt 5.15 / MSVC portability).
5. **Regression test**: what a test must show to fail before the fix and pass after, and the neighbouring test file
   whose pattern fits (CMake target name).

Read-only: change nothing. Verify every pointer. Output only the brief.
"""


def bug_order(limit):
    """The fixer's own next bugs: qwenfix's queue order, untouched (no state, not tried by Luna-easy)."""
    import importlib.util
    import sys
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("qwenfix_scope", ROOT / "qwenfix.py")
    q = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q)
    skip = q.luna_easy_attempted()
    out = []
    for item in q.adaptive.order(q.load_queue()):
        s = q.slug(item["cluster"])
        if s in skip or (q.D["state"] / f"{s}.json").exists():
            continue
        out.append((s, item))
        if len(out) >= limit:
            break
    return out


def scope_bug(s, item, base):
    keep = {k: item.get(k) for k in ("cluster", "title", "file", "line", "text", "clef_tier", "difficulty", "realness")}
    rev = item.get("revision") or item.get("original_revision") or base
    prompt = BUG_PROMPT.format(tree=TREE, base=base, rev=rev, item=json.dumps(keep, ensure_ascii=False, indent=1))
    run = BUG_OUT / "runs" / s
    run.mkdir(parents=True, exist_ok=True)
    out = run / "brief.md"
    t0 = time.time()
    with open(run / "run.log", "w") as lf:
        rc = subprocess.run([str(PROG / "tools/luna/luna-exec"), "-C", str(TREE), "-o", str(out), "--effort", "max",
                             "--read-only", prompt], stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            timeout=2400).returncode
    text = out.read_text() if out.exists() else ""
    if rc == 0 and "Still present" in text:
        (BUG_OUT / f"{s}.md").write_text(f"<!-- scoped by gpt-6-luna (read-only) at {base} on {datetime.date.today()} -->\n" + text)
        return s, f"ok in {(time.time() - t0) / 60:.1f} min"
    return s, f"FAILED rc={rc} ({len(text)} bytes) - see {run}/run.log"


def log(msg):
    line = f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def rows(path):
    p = ROOT / path
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def order():
    """Queue order, interleaving the OpenCode feature queue and the Claude queue."""
    # stages of a split unit (Q050-xxx.n) use their parent's brief, which planned the split
    oc = [r["cluster"] for r in rows("FEATURE-QUEUE.jsonl") if "." not in str(r["cluster"])]
    cl = [r["queue_id"] for r in rows("CLAUDE-QUEUE.jsonl") if "." not in str(r["queue_id"])]
    out = []
    for i in range(max(len(oc), len(cl))):
        out += [x for x in (cl[i] if i < len(cl) else None, oc[i] if i < len(oc) else None) if x and x not in out]
    return out


def refresh_tree(base):
    subprocess.run(["git", "-C", str(TREE), "checkout", "-q", "--detach", base], check=True)


def scope(q, units, base):
    u = units[q]
    reviews = sorted(str(p) for p in (ROOT / "qa/reviews").glob(f"{q}.r*.md")) or ["none"]
    prompt = PROMPT.format(tree=TREE, base=base, prog=PROG, qid=q, members=", ".join(u.get("member_ids") or []) or "-",
                           reviews=", ".join(reviews[-4:]), unit=json.dumps({"queue_id": q, **u}, ensure_ascii=False, indent=1))
    run = OUT / "runs" / q
    run.mkdir(parents=True, exist_ok=True)
    out = run / "brief.md"
    t0 = time.time()
    with open(run / "run.log", "w") as lf:
        rc = subprocess.run([str(PROG / "tools/luna/luna-exec"), "-C", str(TREE), "-o", str(out), "--effort", "max",
                             "--read-only", prompt], stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            timeout=3600).returncode
    text = out.read_text() if out.exists() else ""
    if rc == 0 and "Scope verdict" in text:
        (OUT / f"{q}.md").write_text(f"<!-- scoped by gpt-6-luna (read-only) at {base} on {datetime.date.today()} -->\n" + text)
        return q, f"ok in {(time.time() - t0) / 60:.1f} min"
    return q, f"FAILED rc={rc} ({len(text)} bytes) - see {run}/run.log"


LIMIT_RE = re.compile(r"hit your usage limit|usage limit|rate limit exceeded|try again at", re.I)
LIMIT_PAUSE_S = 900
FAILED_RETRY_S = 2 * 3600


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--ahead", type=int, default=24)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--bugs-ahead", type=int, default=12)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    BUG_OUT.mkdir(parents=True, exist_ok=True)
    # 2026-10-19: a failure used to be remembered for the life of the process, so one Luna usage-limit window (15:28)
    # failed every unit in view and the scoper slept from then on; the units were also read only at start, so units
    # added later were never scoped. Now: a usage-limit failure pauses scoping and is not held against the unit, any
    # other failure is retried after FAILED_RETRY_S, and the units are re-read every pass.
    failed = {}

    def held(x):
        return time.time() - failed.get(x, 0) < FAILED_RETRY_S

    def settle(x, res):
        """Record a result; True when it hit a usage limit (stop this pass, pause)."""
        if not res.startswith("FAILED"):
            return False
        run_log = Path(res.rsplit("see ", 1)[-1].strip())
        try:
            tail = run_log.read_text(errors="replace")[-3000:]
        except OSError:
            tail = ""
        if LIMIT_RE.search(tail):
            return True
        failed[x] = time.time()
        return False

    while not (ROOT / "STOP-SCOPE").exists():
        try:     # 2026-10-25: Luna pacing sheds scoping first (luna_pace.py level >= 1)
            import luna_pace
            if not luna_pace.scoping_allowed() and not a.only:
                time.sleep(600)
                continue
        except ImportError:
            pass
        units = {r["queue_id"]: {k: v for k, v in r.items() if k != "queue_id"}
                 for r in map(json.loads, open(PROG / "plans/queue-050.jsonl"))}
        base = (ROOT / "FIX-BASE").read_text().strip()
        refresh_tree(base)
        want = a.only or [q for q in order()[:a.ahead] if not (OUT / f"{q}.md").exists() and not held(q) and q in units]
        bugs = [] if a.only or not a.bugs_ahead else [(s, it) for s, it in bug_order(a.bugs_ahead)
                                                       if not (BUG_OUT / f"{s}.md").exists() and not held(s)]
        limited = False
        if bugs:
            log(f"scoping {len(bugs)} bug(s) at {base}: {' '.join(s for s, _ in bugs)}")
            with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
                for s_, res in pool.map(lambda b: scope_bug(b[0], b[1], base), bugs):
                    log(f"{s_}: {res}")
                    limited |= settle(s_, res)
        if limited:
            log(f"Luna usage limit - scoping paused {LIMIT_PAUSE_S // 60} min")
            time.sleep(LIMIT_PAUSE_S)
            continue
        if not want and bugs:
            continue
        if not want:
            if a.once or a.only:
                break
            time.sleep(600)
            continue
        log(f"scoping {len(want)} unit(s) at {base}: {' '.join(want)}")
        with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
            for q, res in pool.map(lambda q: scope(q, units, base), want):
                log(f"{q}: {res}")
                limited |= settle(q, res)
        if a.once or a.only:
            break
        if limited:
            log(f"Luna usage limit - scoping paused {LIMIT_PAUSE_S // 60} min")
            time.sleep(LIMIT_PAUSE_S)
    log("stopped")


if __name__ == "__main__":
    main()
