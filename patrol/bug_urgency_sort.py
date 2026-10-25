#!/usr/bin/env python3
"""Rank every queued bug by URGENCY with Clef, the way bug_difficulty_sort.py ranks it by difficulty.

Urgency = user impact x likelihood x reach for a DAW: crashes, loss/corruption of the user's project or files and
realtime audio-thread violations that normal use can hit rank above a broken, commonly used feature, above an edge case
(unusual input/state, malformed-file hardening), above cosmetic/test/vendored/internal issues.

Method (chosen by the held-out eval in research/reviews/bug-urgency/, see REPORT.md): DIRECT scoring, not pairwise.
One Clef request per bug carries NINE questions about the same text (Clef's per-call overhead is ~0.5 s and each extra
question costs ~0.08 s, so nine questions cost about 2x one) and a fixed linear model (research/reviews/bug-urgency/
weights.json, fitted on the TRAIN split of the gold set only) turns the nine answers + a path prior (tests/, vendored
code) into one score. Direct scoring beat Clef pairwise ("which is more urgent") on held-out pairs and costs n calls
instead of ~n log2 n. A "new bug" therefore needs ONE call and its percentile is its position among the stored scores,
which is what binary insertion gives the difficulty sort, without the ~12 comparisons.

Resumable: every bug's features are journalled (research/reviews/bug-urgency/run/scores.jsonl); a re-run asks only for
bugs not yet scored. Model-digest freeze: the Clef digest is pinned in run/meta.json; a different digest refuses to mix
old and new scores (use --rescore to start a new journal, the old one is kept as scores.<digest>.jsonl).

Writes run/{ranked.jsonl,summary.json} and qwen/BUG-URGENCY.json ({cluster: percentile}, 1 = most urgent). A root-cause
group takes the MAX urgency of its members (fixing the group fixes the most urgent one). All Clef calls run inside one
GpuLease (the GPU-REQUEST / GPU-FREE handshake).

Usage: bug_urgency_sort.py [--limit N] [--dry-run] [--rescore] [--no-lease]
(--no-lease skips the GPU-REQUEST/GPU-FREE handshake: only when the caller has checked nvidia-smi and nothing else uses the GPU.)
"""
import argparse
import bisect
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
import clef_client as C  # noqa: E402

PROG = HERE.parents[1]
QWEN = pathlib.Path.home() / ".local/share/zene-bunny-fix/qwen"
OUT = PROG / "research/reviews/bug-urgency"
RUN = OUT / "run"
WEIGHTS = OUT / "weights.json"

CTX = ("Zene Studio is a large C++20/Qt digital audio workstation (an LMMS fork with realtime audio threads, plug-in "
       "hosting, undo, project files and an agent control API). One bug report follows.")
RUBRIC = ("How urgent is it to fix this bug, judged as user impact x likelihood x reach for a DAW? MOST urgent: "
          "crashes, loss or corruption of the user's project or files, and realtime audio-thread violations (locks, "
          "allocation, data races, use-after-free in audio processing) that normal use can hit. Then a broken, commonly "
          "used feature. Then edge cases needing unusual input or state, and hardening against malformed files. LEAST "
          "urgent: cosmetic issues, tests, vendored third-party code, internal cleanups.")
# name -> (type, instructions/criteria). Order is part of the weights file's contract.
QUESTIONS = {
    "noul_top": ("noul", RUBRIC + " Is this among the MOST urgent bugs (a crash, data loss, or realtime audio "
                 "violation that normal use can hit)?"),
    "n_top2": ("noul", "A senior DAW engineer is deciding what to fix first. Is this bug in the top tier: a crash, data "
               "loss, or audio-thread problem that ordinary users can reach, as opposed to an edge case, hardening, "
               "cosmetic or test-only issue?"),
    "n_crash": ("noul", "Can this bug crash the program or read/write freed, uninitialised or out-of-range memory in "
                "normal use of the application?"),
    "n_rt": ("noul", "Does this bug put a lock, allocation, data race or blocking call on the realtime audio thread, or "
             "cause audio glitches, dropouts or wrong audio output?"),
    "n_loss": ("noul", "Can this bug lose, corrupt or silently overwrite the user's project, recordings or exported "
               "audio files?"),
    "n_common": ("noul", "Would a typical DAW user hit this bug in everyday work? Answer no if it needs a malformed "
                 "file, an extreme or non-finite value, API misuse, a rare platform, or a test or vendored library."),
    "noul_low": ("noul", "Is this bug minor: cosmetic, test-only, vendored third-party code, internal cleanup, or an "
                 "edge case that needs unusual input or state?"),
    "score5": ("score", RUBRIC),
    "choice4": ("choice", RUBRIC),
}
SCORE5 = ["cosmetic, test-only, vendored or internal", "edge case or rare trigger",
          "real defect in a moderately used feature", "crash, data race or data loss on an ordinary path",
          "realtime audio violation or common crash/data loss"]
CHOICE4 = {"A": "crash, data loss/corruption, or realtime audio-thread violation reachable in normal use",
           "B": "a broken commonly used feature",
           "C": "edge case, rare trigger, or hardening against malformed input",
           "D": "cosmetic, test-only, vendored or internal"}
NAMES = list(QUESTIONS)
PATH_LOW = re.compile(r"(^|/)tests?/|3rdparty|thirdparty|/asio/|ableton-link|Test\.cpp")
TEXT_CHARS = 900


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def log(msg):
    line = f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "run.log").open("a") as f:
        f.write(line + "\n")


def text(item):
    body = re.sub(r"\s+", " ", re.sub(r"^BUG \d+:\s*", "", str(item.get("text") or "")))
    return f"{item.get('title', '')}. File: {item.get('file', '?')}. {body}"[:TEXT_CHARS]


def build_questions():
    qs = {}
    for name, (kind, instr) in QUESTIONS.items():
        q = {"type": kind, "instructions": instr}
        if name == "score5":
            q["criteria"] = SCORE5
        elif name == "choice4":
            q["criteria"] = CHOICE4
        qs[name] = q
    return qs


def parse_answers(ans):
    """Nine answers -> {feature: float in 0..1, higher = more urgent}. Missing answers raise KeyError."""
    f = {n: float(ans[n]["noul"]) for n in NAMES if QUESTIONS[n][0] == "noul"}
    f["score5"] = ans["score5"]["score"] / 4.0
    p = ans["choice4"]["probabilities"]
    f["choice4"] = (3 * p["A"] + 2 * p["B"] + p["C"]) / 3.0
    return f


def features(item, post_fn=None):
    """One Clef request, nine questions."""
    ans = C.post({"context": CTX, "bug": text(item)}, build_questions(), post_fn=post_fn)
    f = parse_answers(ans)
    f["path_low"] = 1.0 if PATH_LOW.search(str(item.get("file") or "")) else 0.0
    return f


def load_weights(path=WEIGHTS):
    try:
        w = json.loads(pathlib.Path(path).read_text())
        return w["names"], w["w"], w["bias"]
    except (OSError, ValueError, KeyError):
        # Fallback: mean of the two best single questions, halved for tests/vendored paths.
        return ["noul_top", "n_top2", "path_low"], [0.5, 0.5, -0.5], 0.0


def score(f, weights=None):
    names, w, b = weights or load_weights()
    return b + sum(wi * f.get(n, 0.0) for n, wi in zip(names, w))


def percentile(sorted_scores, s):
    """Fraction of stored scores below s (ties split): the 'insertion position' of a new bug, 0..1, 1 = most urgent."""
    lo = bisect.bisect_left(sorted_scores, s)
    hi = bisect.bisect_right(sorted_scores, s)
    n = len(sorted_scores)
    return round(((lo + hi) / 2) / max(1, n), 4)


def clef_digest(host="http://127.0.0.1:11434"):
    with urllib.request.urlopen(host + "/api/tags", timeout=20) as r:
        for m in json.load(r)["models"]:
            if m["name"].startswith("clef-flash"):
                return m["digest"]
    raise RuntimeError("clef-flash not found in ollama")


class Journal:
    """Append-only features journal with a pinned model digest."""

    def __init__(self, run=RUN, digest=None, rescore=False):
        self.run = pathlib.Path(run)
        self.run.mkdir(parents=True, exist_ok=True)
        self.path, self.meta = self.run / "scores.jsonl", self.run / "meta.json"
        pinned = json.loads(self.meta.read_text())["digest"] if self.meta.exists() else None
        if digest and pinned and digest != pinned:
            if not rescore:
                raise SystemExit(f"Clef digest changed ({pinned[:12]} -> {digest[:12]}): old scores are not comparable; "
                                 f"re-run with --rescore to start a new journal (the old one is kept)")
            if self.path.exists():
                self.path.rename(self.run / f"scores.{pinned[:12]}.jsonl")
            pinned = None
        if digest and not pinned:
            self.meta.write_text(json.dumps({"digest": digest, "pinned": now()}))
        self.rows = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                r = json.loads(line)
                self.rows[r["cluster"]] = r

    def add(self, cluster, f, s, digest):
        r = {"cluster": cluster, "features": f, "score": s, "digest": digest, "at": now()}
        self.rows[cluster] = r
        with self.path.open("a") as fh:
            fh.write(json.dumps(r) + "\n")


def queue_units():
    """(bugs, members): every queued bug by cluster id, and the root-cause groups {group id: [live member ids]}."""
    sys.path.insert(0, str(QWEN))
    spec = importlib.util.spec_from_file_location("qwenfix_urgency", QWEN / "qwenfix.py")
    q = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q)
    bugs = {str(i["cluster"]): i for i in q.load_queue()}
    try:
        groups = json.loads((QWEN / "ROOT-CAUSE-GROUPS.json").read_text()).get("groups", [])
    except (OSError, ValueError):
        groups = []
    members = {}
    for g in groups:
        live = [c for c in g["members"] if c in bugs]
        if len(live) >= 2:
            members[g["id"]] = live
    return bugs, members


def rank_units(bugs, members, scores):
    """Units = groups (max member score) + ungrouped bugs. Returns rows sorted most urgent first and {cluster: pct}."""
    grouped = {c for live in members.values() for c in live}
    unit = {g: max(scores[c] for c in live if c in scores) for g, live in members.items()
            if any(c in scores for c in live)}
    unit.update({c: s for c, s in scores.items() if c in bugs and c not in grouped})
    order = sorted(unit, key=lambda c: -unit[c])
    asc = sorted(unit.values())
    def label(c, key):
        live = members.get(c)
        b = bugs.get(live[0] if live else c) or {}
        v = b.get(key)
        return f"[group of {len(live)}] {v}" if live and key == "title" else v

    rows = [{"cluster": c, "rank": i, "score": round(unit[c], 4), "pct": percentile(asc, unit[c]),
             "title": label(c, "title"), "file": label(c, "file")}
            for i, c in enumerate(order)]
    pct = {r["cluster"]: r["pct"] for r in rows}
    for gid, live in members.items():
        for c in live:
            pct[c] = pct.get(gid, 0.5)
    return rows, pct


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--no-lease", action="store_true")
    a = ap.parse_args()
    bugs, members = queue_units()
    digest = clef_digest()
    J = Journal(digest=digest, rescore=a.rescore)
    new = [c for c in bugs if c not in J.rows]
    if a.limit:
        new = new[:a.limit]
    log(f"{len(bugs)} bugs ({len(members)} root-cause groups); {len(J.rows)} already scored; {len(new)} to score")
    if a.dry_run or (not new and not J.rows):
        return
    t0 = time.time()
    if new:
        import contextlib
        if a.no_lease:
            lease = contextlib.nullcontext()
        else:
            import triage_new as T  # GpuLease; heavy imports only when the GPU is needed
            lease = T.GpuLease(QWEN)
        with lease:
            for n, c in enumerate(new, 1):
                f = features(bugs[c])
                J.add(c, f, score(f), digest)
                if n % 200 == 0:
                    log(f"{n}/{len(new)} scored ({(time.time() - t0) / n:.2f} s/bug)")
    weights = load_weights()
    scores = {c: score(r["features"], weights) for c, r in J.rows.items()}   # re-score from features if weights changed
    rows, pct = rank_units(bugs, members, scores)
    (RUN / "ranked.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (QWEN / "BUG-URGENCY.json").write_text(json.dumps(pct))
    summary = {"bugs": len(bugs), "units": len(rows), "new_clef_calls": len(new), "seconds": round(time.time() - t0),
               "digest": digest, "updated": now()}
    (RUN / "summary.json").write_text(json.dumps(summary, indent=1))
    log(f"ranked {len(rows)} units ({len(new)} new Clef calls, {summary['seconds']} s)")


if __name__ == "__main__":
    main()
