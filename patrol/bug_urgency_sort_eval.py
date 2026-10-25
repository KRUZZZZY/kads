#!/usr/bin/env python3
"""Does SORTING by Clef's direct a-vs-b comparison ("which bug is more urgent?") beat scoring each bug directly?

Owner question 2026-10-25. Runs on the held-out TEST split of research/reviews/bug-urgency/gold.json (the prompts were not
tuned on it). Orderings built from the pairwise comparator:
  merge-1    merge sort, each comparison asked once, in a pair-stable pseudo-random presentation order (what
             bug_difficulty_sort.py does)
  merge-2    merge sort, each comparison asked in BOTH orders and averaged (2x the calls)
  borda-K    every item compared with K random opponents (both-orders averaged not used: one call each), score = mean
             P(win); the round-robin limit as K grows
Each is scored like the direct methods (pair accuracy over gold-non-tied pairs, Kendall tau-b, AUC for grade >= 4), next
to the direct hybrid / single-question scores, the keyword baseline, and the number of Clef calls spent.

Usage: bug_urgency_sort_eval.py [--borda-k 30]
"""
import argparse
import hashlib
import json
import random
import sys
import time

HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bug_urgency_eval as E  # noqa: E402
import bug_urgency_sort as U  # noqa: E402
import clef_client as C  # noqa: E402


class Pairwise:
    def __init__(self, cache):
        self.cache, self.asked = cache, 0

    def p_first(self, i, j):
        """P(first argument is more urgent) as presented (i first)."""
        k = C.key("pairU", i["cluster"], j["cluster"])
        v = self.cache.get(k)
        if v is None:
            v = C.post({"bug_a": U.text(i), "bug_b": U.text(j)}, {"u": {"type": "choice", "instructions": E.PQ,
                       "criteria": {"A": "bug_a is more urgent to fix", "B": "bug_b is more urgent to fix"}}})["u"]["probabilities"]["A"]
            self.cache.put(k, v)
            self.asked += 1
        return v

    def stable(self, i, j):
        if int(hashlib.sha256(f"{i['cluster']}|{j['cluster']}".encode()).hexdigest(), 16) & 1:
            return self.p_first(i, j)
        return 1 - self.p_first(j, i)

    def both(self, i, j):
        return (self.p_first(i, j) + 1 - self.p_first(j, i)) / 2


def merge_sort(xs, more_urgent):
    """Ascending urgency (least urgent first). more_urgent(a, b) -> P(a more urgent than b)."""
    if len(xs) <= 1:
        return xs
    mid = len(xs) // 2
    left, right = merge_sort(xs[:mid], more_urgent), merge_sort(xs[mid:], more_urgent)
    out, i, j = [], 0, 0
    while i < len(left) and j < len(right):
        if more_urgent(left[i], right[j]) <= 0.5:
            out.append(left[i]); i += 1
        else:
            out.append(right[j]); j += 1
    return out + left[i:] + right[j:]


def metrics(S, score):
    g = [i["grade"] for i in S]
    s = [score[i["cluster"]] for i in S]
    pa, n = E.pair_acc(s, g)
    return pa, E.kendall_tau_b(s, g), E.auc(s, [x >= 4 for x in g])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--borda-k", type=int, default=30)
    a = ap.parse_args()
    G = json.loads((U.OUT / "gold.json").read_text())["items"]
    S = [i for i in G if i["split"] == "test"]
    cache = C.Cache(U.OUT / "eval-answers.jsonl")
    pw = Pairwise(cache)
    feats = {i["cluster"]: cache.get(C.key("feat9", i["cluster"], U.TEXT_CHARS)) for i in S}
    W = U.load_weights()
    kw, _ = E.load_keyword()
    rows = {}
    rows["keyword (today)"] = ({i["cluster"]: kw(i) for i in S}, 0)
    rows["direct: noul_top (1 call/bug)"] = ({c: f["noul_top"] for c, f in feats.items()}, len(S))
    rows["direct: hybrid 9q (1 call/bug)"] = ({c: U.score(f, W) for c, f in feats.items()}, len(S))
    t = time.time()
    for name, cmpf in (("pairwise merge sort, 1 order/pair", pw.stable), ("pairwise merge sort, both orders/pair", pw.both)):
        n0 = pw.asked
        order = merge_sort(list(S), cmpf)
        rows[name] = ({i["cluster"]: r for r, i in enumerate(order)}, None)
        rows[name] = (rows[name][0], pw.asked - n0)
        print(f"{name}: {pw.asked - n0} new calls ({time.time() - t:.0f}s)", flush=True)
    rng = random.Random(8)
    n0 = pw.asked
    wins = {i["cluster"]: [] for i in S}
    for i in S:
        for j in rng.sample([x for x in S if x is not i], a.borda_k):
            p = pw.stable(i, j)
            wins[i["cluster"]].append(p)
            wins[j["cluster"]].append(1 - p)
    rows[f"pairwise Borda ({a.borda_k} random opponents/bug)"] = ({c: sum(v) / len(v) for c, v in wins.items()}, pw.asked - n0)
    print(f"borda: {pw.asked - n0} new calls", flush=True)
    out = {}
    print(f"\nTEST n={len(S)}; pair accuracy / Kendall tau-b / AUC(grade>=4) / Clef calls (new calls only for pairwise rows; cached pairs are free)")
    for name, (score, calls) in rows.items():
        pa, tau, au = metrics(S, score)
        out[name] = {"pair_acc": round(pa, 4), "tau": round(tau, 4), "auc_ge4": round(au, 4), "new_calls": calls}
        print(f"{name:52s} {pa:.3f}  {tau:.3f}  {au:.3f}  calls={calls}")
    (U.OUT / "sort-eval-report.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
