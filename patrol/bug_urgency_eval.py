#!/usr/bin/env python3
"""Held-out evaluation of urgency rankers against the hand-graded gold set (research/reviews/bug-urgency/gold.json).

Gold: 260 queue items graded 1-5 for urgency by a single rater (see gold.json "rubric"), split 50/50 into train/test
(130 each; 200 uniform-random items + 60 enriched with crash/race/audio-thread wording). Prompts and weights were tuned
on TRAIN only; this script fits the hybrid's ridge weights on train, writes weights.json, then scores TEST once.

Methods: keyword (today's qwenfix severity classes), keyword + difficulty tie-break (today's smart_order), each single
Clef question (noul_top, n_top2, score5, choice4), the mean of the two best, the fitted nine-question hybrid, and Clef
pairwise ("which is more urgent") on a sample of test pairs. Metrics: pairwise accuracy over gold-non-tied pairs (a model
tie counts 0.5), Kendall tau-b, and AUC for "grade >= 4" (the tier that must go first).

Usage: bug_urgency_eval.py [--pairs N] [--no-pairwise]
"""
import argparse
import hashlib
import importlib.util
import itertools
import json
import pathlib
import random
import sys
import time

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bug_urgency_sort as U  # noqa: E402
import clef_client as C  # noqa: E402

OUT = U.OUT
QWEN = U.QWEN
PQ = ("Two bug reports for a large C++20/Qt digital audio workstation follow. Which bug is MORE URGENT to fix, judged as "
      "user impact x likelihood x reach? Crashes, loss or corruption of the user's project or files, and realtime "
      "audio-thread violations (locks, allocation, data races, use-after-free in audio processing) that normal use can "
      "hit are the most urgent; then a broken commonly used feature; then edge cases needing unusual input or state and "
      "hardening against malformed files; cosmetic issues, tests, vendored third-party code and internal cleanups are "
      "the least urgent.")


def kendall_tau_b(x, y):
    c = d = tx = ty = 0
    for a, b in itertools.combinations(range(len(x)), 2):
        dx, dy = x[a] - x[b], y[a] - y[b]
        if dx == 0 and dy == 0:
            continue
        if dx == 0:
            tx += 1
        elif dy == 0:
            ty += 1
        elif dx * dy > 0:
            c += 1
        else:
            d += 1
    den = ((c + d + tx) * (c + d + ty)) ** 0.5
    return (c - d) / den if den else 0.0


def pair_acc(s, g, pairs=None):
    w = n = 0
    for a, b in (pairs if pairs is not None else itertools.combinations(range(len(g)), 2)):
        if g[a] == g[b]:
            continue
        n += 1
        ds = s[a] - s[b]
        w += 0.5 if ds == 0 else (ds > 0) == (g[a] > g[b])
    return (w / n if n else 0.0), n


def auc(s, pos):
    P = [x for x, p in zip(s, pos) if p]
    N = [x for x, p in zip(s, pos) if not p]
    if not P or not N:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in P for n in N) / (len(P) * len(N))


def ridge_fit(X, y, lam):
    A = np.c_[X, np.ones(len(X))]
    w = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ y)
    return w[:-1], w[-1]


def load_keyword():
    sys.path.insert(0, str(QWEN))
    spec = importlib.util.spec_from_file_location("qwenfix_eval", QWEN / "qwenfix.py")
    q = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q)
    rank = q.bug_rank()
    kw = lambda i: len(q.SEVERITY) - q.severity({"title": i["title"], "text": i["text"]})  # noqa: E731
    kwd = lambda i: kw(i) + (1 - rank.get(q.bug_key(i["cluster"]), 0.5)) * 0.5  # noqa: E731  severity, then easier first
    return kw, kwd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=600)
    ap.add_argument("--no-pairwise", action="store_true")
    a = ap.parse_args()
    G = json.loads((OUT / "gold.json").read_text())["items"]
    cache = C.Cache(OUT / "eval-answers.jsonl")
    feats = {}
    t0 = time.time()
    for i in G:
        k = C.key("feat9", i["cluster"], U.TEXT_CHARS)
        v = cache.get(k)
        if v is None:
            v = U.features(i)
            cache.put(k, v)
        feats[i["cluster"]] = v
    print(f"features for {len(G)} items in {time.time() - t0:.0f}s", flush=True)
    tr = [i for i in G if i["split"] == "train"]
    te = [i for i in G if i["split"] == "test"]
    names = U.NAMES + ["path_low"]
    Xtr = np.array([[feats[i["cluster"]][n] for n in names] for i in tr])
    ytr = np.array([i["grade"] for i in tr], float)
    # lambda by 5-fold CV on train (pairwise accuracy)
    best = None
    idx = np.arange(len(tr))
    np.random.RandomState(0).shuffle(idx)
    for lam in (0.3, 1, 3, 10):
        pred = np.zeros(len(tr))
        for f in range(5):
            hold = idx[f::5]
            fit = np.setdiff1d(idx, hold)
            w, b = ridge_fit(Xtr[fit], ytr[fit], lam)
            pred[hold] = Xtr[hold] @ w + b
        acc = pair_acc(list(pred), list(ytr))[0]
        print(f"  ridge lambda={lam}: train 5-fold pair accuracy {acc:.3f}")
        if best is None or acc > best[0]:
            best = (acc, lam)
    w, b = ridge_fit(Xtr, ytr, best[1])
    weights = {"names": names, "w": [round(float(x), 5) for x in w], "bias": round(float(b), 5), "lambda": best[1],
               "fit_on": "train split of gold.json only", "train_cv_pair_acc": round(best[0], 4)}
    (OUT / "weights.json").write_text(json.dumps(weights, indent=1))
    print("weights:", dict(zip(names, weights["w"])), "bias", weights["bias"])
    kw, kwd = load_keyword()
    W = U.load_weights()
    methods = {
        "keyword (today)": lambda i: kw(i),
        "keyword+difficulty tie-break (smart_order)": lambda i: kwd(i),
        "Clef noul_top (1 question)": lambda i: feats[i["cluster"]]["noul_top"],
        "Clef n_top2 (1 question)": lambda i: feats[i["cluster"]]["n_top2"],
        "Clef score5 (1-5 score)": lambda i: feats[i["cluster"]]["score5"],
        "Clef choice4 (expected class)": lambda i: feats[i["cluster"]]["choice4"],
        "mean(noul_top, n_top2)": lambda i: (feats[i["cluster"]]["noul_top"] + feats[i["cluster"]]["n_top2"]) / 2,
        "noul_top x path penalty": lambda i: feats[i["cluster"]]["noul_top"] * (1 - 0.5 * feats[i["cluster"]]["path_low"]),
        "hybrid (9 questions + path, ridge on train)": lambda i: U.score(feats[i["cluster"]], W),
    }
    report = {"n_train": len(tr), "n_test": len(te), "weights": weights, "results": {}}
    sets = {"test (all 130)": te, "test random-only (100)": [i for i in te if i["set"] == "random"],
            "test enriched-only (30)": [i for i in te if i["set"] == "enriched"]}
    for sname, S in sets.items():
        g = [i["grade"] for i in S]
        print(f"\n== {sname}: n={len(S)}, {pair_acc([0] * len(g), g)[1]} non-tied gold pairs")
        print(f"{'method':48s} pairacc   tau   AUC(grade>=4)")
        for mname, f in methods.items():
            s = [f(i) for i in S]
            pa, n = pair_acc(s, g)
            t = kendall_tau_b(s, g)
            au = auc(s, [x >= 4 for x in g])
            report["results"].setdefault(sname, {})[mname] = {"pair_acc": round(pa, 4), "tau": round(t, 4),
                                                              "auc_ge4": None if au is None else round(au, 4), "n": len(S)}
            print(f"{mname:48s} {pa:.3f}  {t:.3f}  {'' if au is None else f'{au:.3f}'}")
    if not a.no_pairwise:
        S = te
        pairs = [(x, y) for x, y in itertools.combinations(range(len(S)), 2) if S[x]["grade"] != S[y]["grade"]]
        random.Random(3).shuffle(pairs)
        pairs = pairs[:a.pairs]

        def pair_p(i, j):
            k = C.key("pairU", i["cluster"], j["cluster"])
            v = cache.get(k)
            if v is None:
                v = C.post({"bug_a": U.text(i), "bug_b": U.text(j)}, {"u": {"type": "choice", "instructions": PQ,
                           "criteria": {"A": "bug_a is more urgent to fix", "B": "bug_b is more urgent to fix"}}})["u"]["probabilities"]["A"]
                cache.put(k, v)
            return v

        def p_first(i, j):  # pair-stable random order, like bug_difficulty_sort
            if int(hashlib.sha256(f"{i['cluster']}|{j['cluster']}".encode()).hexdigest(), 16) & 1:
                return pair_p(i, j)
            return 1 - pair_p(j, i)

        t0 = time.time()
        pw = [p_first(S[x], S[y]) for x, y in pairs]
        gold = [S[x]["grade"] > S[y]["grade"] for x, y in pairs]
        pacc = sum((p > .5) == gd for p, gd in zip(pw, gold)) / len(pairs)
        print(f"\n== pairwise sample on TEST: {len(pairs)} gold-non-tied pairs ({time.time() - t0:.0f}s)")
        print(f"{'Clef pairwise (which is more urgent)':48s} acc {pacc:.3f}")
        report["pairwise"] = {"n_pairs": len(pairs), "clef_pairwise_acc": round(pacc, 4), "methods_on_same_pairs": {}}
        for mname, f in methods.items():
            s = [f(i) for i in S]
            acc = pair_acc(s, [i["grade"] for i in S], pairs)[0]
            report["pairwise"]["methods_on_same_pairs"][mname] = round(acc, 4)
            print(f"{mname:48s} acc {acc:.3f}")
    (OUT / "eval-report.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
