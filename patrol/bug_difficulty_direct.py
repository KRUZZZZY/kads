#!/usr/bin/env python3
"""Direct (non-pairwise) difficulty score for every queued bug: one Clef request per bug, five yes/no questions.

Replaces the ~40,000-call pairwise merge sort of bug_difficulty_sort.py (about 5 h) with n calls (~0.4 s per bug when the
GPU is not contended, ~30 min for the whole queue) and one call for each new bug instead of ~12 binary-insertion
comparisons. Validated against what actually happened to 1,322 bugs the fix loop accepted (research/reviews/bug-urgency/
difficulty-direct.out): the mean of the five answers separates "needed rework" from "accepted first round" at AUC 0.640
(the pairwise rank: 0.626), correlates with the minutes the fix took at Spearman 0.333 (pairwise: 0.298), and agrees with
the pairwise percentile at Spearman 0.76 (a single question, n_hard, agrees at 0.90 but predicts minutes less well).

Same contract as bug_difficulty_sort.py: qwen/BUG-RANK-DIRECT.json is {cluster: percentile}, 0 = easiest; a root-cause
group takes the MAX of its members (a group is handed out as one job). It never writes BUG-RANK.json: swap the file name
in qwenfix.bug_rank() when the owner accepts it. Resumable journal (research/reviews/bug-difficulty-direct/run/), Clef
digest pinned in meta.json (a changed digest refuses to mix; --rescore starts a new journal).

Usage: bug_difficulty_direct.py [--limit N] [--dry-run] [--rescore] [--no-lease]
"""
import argparse
import contextlib
import json
import re
import time

import bug_urgency_sort as U
import clef_client as C

PROG, QWEN = U.PROG, U.QWEN
OUT = PROG / "research/reviews/bug-difficulty-direct"
RUN = OUT / "run"
CTX = U.CTX
QUESTIONS = {
    "n_hard": "Is this bug HARD to fix correctly: it needs deep reasoning, realtime/concurrency or audio-DSP correctness, "
              "changes across several subsystems, or subtle edge cases, so a weaker coding agent would likely fail? Judge "
              "the difficulty of a correct fix with a regression test, not importance.",
    "n_simple": "Is the correct fix a small local change (a guard, a missing check, a one-line type or constant) in one "
                "function that a weak coding agent can make and test easily?",
    "n_conc": "Does fixing this bug require reasoning about threads, locks, lifetimes, ownership or realtime constraints?",
    "n_multi": "Would a correct fix touch several files or subsystems, or need a design decision about behaviour?",
    "n_test": "Is it difficult to write a deterministic automated regression test for this bug (needs audio devices, GUI, "
              "timing, plug-ins or a race)?",
}
INVERTED = {"n_simple"}
TEXT_CHARS = 900


def text(item):
    body = re.sub(r"\s+", " ", re.sub(r"^BUG \d+:\s*", "", str(item.get("text") or "")))
    return f"{item.get('title', '')}. File: {item.get('file', '?')}. {body}"[:TEXT_CHARS]


def features(item, post_fn=None):
    qs = {n: {"type": "noul", "instructions": q} for n, q in QUESTIONS.items()}
    ans = C.post({"context": CTX, "bug": text(item)}, qs, post_fn=post_fn)
    return {n: float(ans[n]["noul"]) for n in QUESTIONS}


def score(f):
    """Mean of the five answers, n_simple inverted; 0 = easy, 1 = hard."""
    return sum((1 - f[n]) if n in INVERTED else f[n] for n in QUESTIONS) / len(QUESTIONS)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--no-lease", action="store_true")
    a = ap.parse_args()
    bugs, members = U.queue_units()
    digest = U.clef_digest()
    J = U.Journal(run=RUN, digest=digest, rescore=a.rescore)
    new = [c for c in bugs if c not in J.rows]
    if a.limit:
        new = new[:a.limit]
    print(f"{len(bugs)} bugs; {len(J.rows)} already scored; {len(new)} to score", flush=True)
    if a.dry_run or (not new and not J.rows):
        return
    t0 = time.time()
    if new:
        if a.no_lease:
            lease = contextlib.nullcontext()
        else:
            import triage_new as T
            lease = T.GpuLease(QWEN)
        with lease:
            for n, c in enumerate(new, 1):
                f = features(bugs[c])
                J.add(c, f, score(f), digest)
                if n % 200 == 0:
                    print(f"{n}/{len(new)} scored ({(time.time() - t0) / n:.2f} s/bug)", flush=True)
    scores = {c: score(r["features"]) for c, r in J.rows.items()}
    rows, pct = U.rank_units(bugs, members, scores)
    # rank_units sorts most-urgent-first and reports pct of "higher is more"; for difficulty that IS "harder = higher".
    (RUN / "ranked.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (QWEN / "BUG-RANK-DIRECT.json").write_text(json.dumps(pct))
    (RUN / "summary.json").write_text(json.dumps({"bugs": len(bugs), "units": len(rows), "new_clef_calls": len(new),
                                                  "seconds": round(time.time() - t0), "digest": digest,
                                                  "updated": U.now()}, indent=1))
    print(f"ranked {len(rows)} units ({len(new)} new Clef calls, {round(time.time() - t0)} s)")


if __name__ == "__main__":
    main()
