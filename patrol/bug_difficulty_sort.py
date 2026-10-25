#!/usr/bin/env python3
"""Rank every queued bug by difficulty with Clef as a pairwise comparator, like the 0.5.0 feature queue.

Owner 2026-10-19: "there are wayyy too many that need context ... swap to 'is this harder than x', the same thing as
the feature queue". The old per-bug difficulty questions labelled ~92 % of the queue `needs_context`, which says
little; a pairwise "which of these two bugs is harder to fix correctly?" places every bug on one curve.

First run: merge sort of every bug in the fix loop's queue (qwenfix.load_queue), each comparison asked in BOTH orders
presented in a pair-stable random order, so position bias averages out over the sort (half the calls of
clef_difficulty_sort.py's both-orders method). Later runs: bugs not yet ranked (new patrol
passes) are placed by binary insertion into the existing order (~12 comparisons each). Comparisons are journalled, so
an interrupted run resumes without re-asking.

Writes research/reviews/bug-difficulty/{ranked.jsonl,comparisons.jsonl,summary.json} and qwen/BUG-RANK.json
({cluster: percentile}, 0 = easiest): qwenfix orders and routes the queue by it and benchmark.py weights bug
successes by it (1 + 2 x percentile). All Clef calls run inside one GpuLease (the GPU-REQUEST / GPU-FREE handshake).

Usage: bug_difficulty_sort.py [--limit N] [--dry-run]
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import triage_new as T  # noqa: E402  (GpuLease)

PROG = HERE.parents[1]
QWEN = pathlib.Path.home() / ".local/share/zene-bunny-fix/qwen"
OUT = PROG / "research/reviews/bug-difficulty"
HOST = "http://127.0.0.1:11434/v1/systemone"
QUESTION = ("These are two bug reports for a large C++20/Qt digital audio workstation (an LMMS fork with realtime "
            "audio threads, plug-in hosting, undo, project files and an agent control API). Which bug is MORE "
            "DIFFICULT to fix correctly: it needs deeper reasoning, realtime/concurrency or audio-DSP correctness, "
            "changes across several subsystems, or subtle edge cases, so that a weaker coding agent would likely "
            "fail? Judge the difficulty of a correct fix with a regression test, not the bug's importance.")
BUG_LINE = re.compile(r"^BUG \d+:\s*", re.M)


def log(msg):
    line = f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with (OUT / "run.log").open("a") as f:
        f.write(line + "\n")


def queue_items():
    """The units to rank: every root-cause group (ROOT-CAUSE-GROUPS.json, one job each) as one unit, plus every bug
    not in a group (owner 2026-10-19: rank after the grouping, since a group is handed out as one job)."""
    sys.path.insert(0, str(QWEN))
    spec = importlib.util.spec_from_file_location("qwenfix_sort", QWEN / "qwenfix.py")
    q = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q)
    bugs = {str(i["cluster"]): i for i in q.load_queue()}
    try:
        groups = json.loads((QWEN / "ROOT-CAUSE-GROUPS.json").read_text()).get("groups", [])
    except (OSError, ValueError):
        groups = []
    units, members = {}, {}
    for g in groups:
        live = [c for c in g["members"] if c in bugs]
        if len(live) < 2:
            continue
        first = bugs[live[0]]
        units[g["id"]] = {"cluster": g["id"], "title": f"{len(live)} bugs believed to share one root cause: "
                          + "; ".join(str(bugs[c].get("title", "")) for c in live)[:600],
                          "file": ", ".join(sorted({str(bugs[c].get("file", "")) for c in live}))[:200],
                          "text": first.get("text", "")}
        members[g["id"]] = live
    grouped = {c for live in members.values() for c in live}
    units.update({c: i for c, i in bugs.items() if c not in grouped})
    return units, members


def text(item):
    body = BUG_LINE.sub("", str(item.get("text") or ""))
    return f"{item.get('title', '')}. File: {item.get('file', '?')}. {body}"[:1400]


class Comparator:
    def __init__(self, items):
        self.items, self.cache, self.calls = items, {}, 0
        self.journal = OUT / "comparisons.jsonl"
        if self.journal.exists():
            for line in self.journal.read_text().splitlines():
                r = json.loads(line)
                self.cache[(r["a"], r["b"])] = r["p_a_harder"]

    def ask(self, a, b):
        body = {"model": "clef-flash", "state": {"bug_a": text(self.items[a]), "bug_b": text(self.items[b])},
                "questions": {"harder": {"type": "choice", "instructions": QUESTION,
                                         "criteria": {"A": "bug_a is harder to fix correctly",
                                                      "B": "bug_b is harder to fix correctly"}}}}
        req = urllib.request.Request(HOST, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    return json.load(r)["answers"]["harder"]["probabilities"]["A"]
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))

    def harder(self, a, b):
        """P(a is harder than b), averaged over both presentation orders."""
        if (a, b) in self.cache:
            return self.cache[(a, b)]
        if (b, a) in self.cache:
            return 1.0 - self.cache[(b, a)]
        # One call per pair, presented in a pair-stable pseudo-random order: position bias averages out over the sort
        # (half the cost of asking both orders, which the 476-unit feature sort could afford and 3,000+ bugs cannot).
        if int(hashlib.sha256(f"{a}|{b}".encode()).hexdigest(), 16) & 1:
            p = self.ask(a, b)
        else:
            p = 1.0 - self.ask(b, a)
        self.calls += 1
        self.cache[(a, b)] = p
        with self.journal.open("a") as f:
            f.write(json.dumps({"a": a, "b": b, "p_a_harder": p}) + "\n")
        if self.calls % 2000 == 0:
            log(f"{self.calls} Clef calls so far")
        return p


def merge_sort(xs, cmp):
    if len(xs) <= 1:
        return xs
    mid = len(xs) // 2
    left, right = merge_sort(xs[:mid], cmp), merge_sort(xs[mid:], cmp)
    out, i, j = [], 0, 0
    while i < len(left) and j < len(right):
        if cmp.harder(left[i], right[j]) <= 0.5:
            out.append(left[i]); i += 1
        else:
            out.append(right[j]); j += 1
    return out + left[i:] + right[j:]


def insert(order, x, cmp):
    lo, hi = 0, len(order)
    while lo < hi:
        mid = (lo + hi) // 2
        if cmp.harder(x, order[mid]) <= 0.5:
            hi = mid
        else:
            lo = mid + 1
    order.insert(lo, x)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    items, members = queue_items()
    ranked_path = OUT / "ranked.jsonl"
    order = [json.loads(l)["cluster"] for l in ranked_path.read_text().splitlines()] if ranked_path.exists() else []
    order = [c for c in order if c in items]
    new = [c for c in items if c not in set(order)]
    if a.limit:
        new = new[:a.limit]
    log(f"{len(items)} units ({len(members)} root-cause groups + single bugs); {len(order)} already ranked; {len(new)} to place "
        f"({'merge sort' if not order else 'binary insertion'})")
    if a.dry_run or not new:
        return
    cmp = Comparator(items)
    t0 = time.time()
    with T.GpuLease(QWEN):
        if not order:
            order = merge_sort(new, cmp)
        else:
            for n, c in enumerate(new, 1):
                insert(order, c, cmp)
                if n % 50 == 0:
                    log(f"{n}/{len(new)} placed")
    n = len(order)
    rows = [{"cluster": c, "rank": i, "pct": round(i / max(1, n - 1), 4),
             "class": "easy" if i < n / 3 else "hard" if i >= 2 * n / 3 else "moderate",
             "title": items[c].get("title"), "file": items[c].get("file")} for i, c in enumerate(order)]
    ranked_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    rank = {r["cluster"]: r["pct"] for r in rows}
    for gid, live in members.items():          # every member of a group takes its group's place
        for c in live:
            rank[c] = rank.get(gid, 0.5)
    (QWEN / "BUG-RANK.json").write_text(json.dumps(rank))
    summary = {"bugs": n, "new_clef_calls": cmp.calls, "seconds": round(time.time() - t0), "updated": T.now()}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    log(f"ranked {n} bugs ({cmp.calls} new Clef calls, {summary['seconds']} s)")


if __name__ == "__main__":
    main()
