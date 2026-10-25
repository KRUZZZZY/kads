#!/usr/bin/env python3
"""Measure how well Clef judges "do these two bugs share one root cause?" before it is trusted (owner 2026-10-19:
"check if step 4 is actually accurate before implementing, try multiple ways to try make it accurate").

Ground truth: research/reviews/root-causes/eval-labels.json - 56 bug pairs (34 from inside candidate families, 22
near neighbours from different families) labelled by the orchestrating Claude (Opus 5.5) as SAME (one code change
would fix both), PATTERN (same kind of defect, separate fixes) or DIFFERENT.

Methods (each returns P(same root cause) per pair):
  M1 noul      "share ONE underlying root cause that a single change fixes?"           one order
  M2 noul x2   M1 asked in both orders, averaged
  M3 choice    SAME / PATTERN / DIFFERENT with definitions; P(SAME)
  M4 score     0..4 likelihood that one change fixes both, as a fraction
  M5 noul+code M1 with the current source around each bug's location (zene-cachyos)
Baselines: B1 embedding similarity (1 - combined distance), B2 shared rare identifier.
Report: ROC AUC for SAME vs rest and for SAME+PATTERN vs DIFFERENT, plus the best-threshold accuracy.
"""
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import classify  # noqa: E402
import root_causes as R  # noqa: E402

HOST = "http://127.0.0.1:11434/v1/systemone"
CTX = ("Context: Zene Studio, a large C++20/Qt digital audio workstation (an LMMS fork with realtime audio threads, "
       "plug-in hosting, undo journal, project files and an agent control API). Two independent bug reports follow.")
Q_SAME = ("Do bug_a and bug_b share ONE underlying root cause, so that a single code change - in one shared function, "
          "helper, data structure, or one piece of duplicated logic - would fix both? Being in the same file, the same "
          "subsystem, or the same general category (e.g. both are races, both are overflows) is NOT enough: answer yes "
          "only if fixing one cause fixes both.")


def post(state, questions):
    req = urllib.request.Request(HOST, data=json.dumps({"model": "clef-flash", "state": state, "questions": questions}).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)["answers"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))


def m_noul(a, b, extra=None):
    st = {"context": CTX, "bug_a": a, "bug_b": b, **(extra or {})}
    return post(st, {"same": {"type": "noul", "instructions": Q_SAME}})["same"]["noul"]


def m3(a, b):
    ans = post({"context": CTX, "bug_a": a, "bug_b": b}, {"rel": {"type": "choice", "instructions":
        "How are these two bugs related?", "criteria": {
            "SAME": "one underlying root cause: a single code change (shared function, helper, data structure or duplicated logic) fixes both",
            "PATTERN": "the same kind of mistake made in different places: each needs its own separate fix",
            "DIFFERENT": "unrelated defects, even if they share a file or subsystem"}}})
    return ans["rel"]["probabilities"]["SAME"]


def m4(a, b):
    ans = post({"context": CTX, "bug_a": a, "bug_b": b}, {"one_fix": {"type": "score", "instructions": Q_SAME,
        "criteria": ["Certainly separate causes", "Probably separate causes", "Unclear",
                     "Probably one shared cause", "Certainly one shared cause"]}})
    return ans["one_fix"]["score"] / 4.0


def code_around(item):
    path = item.get("resolved_path") or item.get("file") or ""
    try:
        src = subprocess.run(["git", "-C", str(R.TREE), "show", f"HEAD:{path}"], capture_output=True, text=True,
                             errors="replace").stdout
    except OSError:
        src = ""
    return classify.source_context(src, [{"line": item.get("line") or 0, "text": str(item.get("text", ""))}], 2500)


def auc(scores, labels):
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def best_acc(scores, labels):
    best = (0, 0.5)
    for t in sorted(set(scores)):
        acc = sum((s >= t) == l for s, l in zip(scores, labels)) / len(labels)
        best = max(best, (acc, t))
    return best


def main():
    out = R.OUT
    pairs = json.loads((out / "eval-pairs.json").read_text())
    labels = json.loads((out / "eval-labels.json").read_text())
    items, _ = R.queue()
    by = {str(i["cluster"]): i for i in items}
    import numpy as np
    D = np.load(out / "dist.npy")
    order = json.loads((out / "emb-ids.json").read_text())
    pos = {c: n for n, c in enumerate(order)}
    known = R.tree_identifiers()
    res = {k: [] for k in ("M1", "M2", "M3", "M4", "M5", "B1", "B2")}
    t0 = time.time()
    for p in pairs:
        A, B = by[p["a"]], by[p["b"]]
        a, b = R.bug_text(A), R.bug_text(B)
        m1 = m_noul(a, b)
        res["M1"].append(m1)
        res["M2"].append((m1 + m_noul(b, a)) / 2)
        res["M3"].append(m3(a, b))
        res["M4"].append(m4(a, b))
        res["M5"].append(m_noul(a, b, {"code_a": code_around(A), "code_b": code_around(B)}))
        res["B1"].append(1 - float(D[pos[p["a"]], pos[p["b"]]]))
        res["B2"].append(1.0 if R.identifiers(A, known) & R.identifiers(B, known) else 0.0)
    lab = [labels[str(p["id"])] for p in pairs]
    same = [l == "SAME" for l in lab]
    related = [l in ("SAME", "PATTERN") for l in lab]
    report = {"pairs": len(pairs), "labels": {k: lab.count(k) for k in ("SAME", "PATTERN", "DIFFERENT")},
              "seconds": round(time.time() - t0), "methods": {}}
    for k, s in res.items():
        acc, thr = best_acc(s, same)
        report["methods"][k] = {"auc_same": round(auc(s, same), 3), "auc_related": round(auc(s, related), 3),
                                "best_acc_same": round(acc, 3), "threshold": round(thr, 3)}
    (out / "eval-scores.json").write_text(json.dumps({"scores": res, "labels": lab}, indent=1))
    (out / "eval-report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
