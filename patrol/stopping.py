#!/usr/bin/env python3
"""stopping.py: should the bug-sweep loop stop? (owner, 2026-10-09)

The owner's rule: keep running sweep -> fix rounds until (a) the probability that one more sweep pass finds a NEW
MAJOR bug is below alpha, and (b) most of a round's findings are false positives.

A ROUND is T >= 2 patrol passes (different angles) over ONE frozen commit, deduplicated into clusters (dedup.py) and
triaged (each cluster: real or false positive, severity S1-S4). Only verified-real clusters at a major severity count.

Model (incidence data, each pass is one sampling unit; Chao 2014, "Rarefaction and extrapolation with Hill numbers"):
  Q1 = major clusters found in exactly one pass, Q2 = in exactly two, T = passes in the round.
  Expected NEW majors in one more pass     m1 = Q1 / T   (incidence Good-Turing)
  P(next pass finds >= 1 new major)        p  = 1 - exp(-m1)
  Undetected majors still in the tree      f0 = (T-1)/T * Q1^2 / (2 Q2)   (Chao2; Q1 (Q1-1) / 2 when Q2 = 0)
  Conservative upper bound                 p_up uses the Garwood (1-alpha) upper Poisson bound on Q1.
The owner asked for a less extreme bound ("this is just something simple", 2026-10-09): the decision uses p_up taken at
a modest CONFIDENCE (default 75%, one-sided) against a THRESHOLD (default 0.25): stop when we are 75% sure a new
major has at most a 1-in-4 chance per pass. A round whose majors all repeat across passes (Q1 = 0) stops after 5
passes (p_up = 1 - exp(-1.386/5) = 0.24). Also required: the LAST pass found no new major, no cluster is unverified,
and the false-positive share of the round is above 1/2 (the owner's earlier rule).

Inputs (one round):
  --findings  findings.jsonl  (patrol records; a finding has "title" and "pass"; other lines are events and skipped)
  --clusters  clusters.jsonl  ({"representative": i, "members": [i, ...]}; indices count findings in file order)
  --verdicts  verdicts.jsonl  ({"representative": i, "verdict": "real"|"false_positive"|"unverified", "severity": "S1"})
  --rev       only findings of this commit (a round is one frozen tree)
Exit code: 0 = STOP, 3 = CONTINUE, 2 = bad input.
"""
import argparse
import json
import math
import sys


def garwood_upper(k, alpha):
    """(1-alpha) one-sided upper confidence bound of a Poisson mean given k observed events (chi-square form)."""
    # chi2.ppf(1-alpha, 2(k+1)) / 2, via the regularised gamma inverse by bisection (no scipy needed).
    target = 1.0 - alpha
    def poisson_cdf(kk, mu):
        term, total = math.exp(-mu), math.exp(-mu)
        for i in range(1, kk + 1):
            term *= mu / i
            total += term
        return total
    lo, hi = 0.0, max(10.0, 10.0 * (k + 1))
    for _ in range(200):          # P(X <= k | mu) is decreasing in mu; find mu with P(X <= k) = alpha
        mid = (lo + hi) / 2
        if poisson_cdf(k, mid) > 1.0 - target:
            lo = mid
        else:
            hi = mid
    return hi


def round_stats(findings, clusters, verdicts, rev, majors, alpha, confidence=0.75):
    idx = [f for f in findings if "title" in f]
    if rev:
        keep = {i for i, f in enumerate(idx) if f.get("rev", "").startswith(rev)}
    else:
        keep = set(range(len(idx)))
    passes = sorted({idx[i].get("pass") for i in keep})
    T = len(passes)
    real_major, fp, real, unverified = [], 0, 0, 0
    for c in clusters:
        members = [m for m in c["members"] if m in keep]
        if not members:
            continue
        v = verdicts.get(c["representative"], {})
        verdict = v.get("verdict", "unverified")
        if verdict == "false_positive":
            fp += 1
        elif verdict == "real":
            real += 1
            if v.get("severity") in majors:
                real_major.append({idx[m].get("pass") for m in members})
        else:
            unverified += 1
    incid = [len(s) for s in real_major]
    Q1, Q2 = incid.count(1), incid.count(2)
    last = passes[-1] if passes else None
    new_in_last = sum(1 for s in real_major if s == {last})
    m1 = Q1 / T if T else float("inf")
    p = 1.0 - math.exp(-m1) if T else 1.0
    q1_up = garwood_upper(Q1, 1.0 - confidence)
    p_up = 1.0 - math.exp(-q1_up / T) if T else 1.0
    f0 = ((T - 1) / T) * (Q1 * Q1 / (2 * Q2) if Q2 > 0 else Q1 * (Q1 - 1) / 2) if T > 1 else float("nan")
    judged = fp + real
    fp_share = fp / judged if judged else 0.0
    stop = T >= 2 and p_up < alpha and new_in_last == 0 and fp_share > 0.5 and unverified == 0
    return {"passes": T, "real_major_clusters": len(real_major), "Q1": Q1, "Q2": Q2,
            "new_majors_in_last_pass": new_in_last, "p_new_major_next_pass": round(p, 4),
            "p_upper_bound": round(p_up, 4), "undetected_majors_chao2": round(f0, 2) if f0 == f0 else None,
            "false_positive_share": round(fp_share, 3), "unverified_clusters": unverified,
            "threshold": alpha, "confidence": confidence, "decision": "STOP" if stop else "CONTINUE"}


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def selftest():
    # Round A: 3 passes, many singleton majors -> CONTINUE.  Round B: every major seen in all 3 passes, mostly FPs -> STOP.
    def build(groups, fp_count):
        findings, clusters, verdicts, i = [], [], {}, 0
        for passes in groups:
            members = []
            for p in passes:
                findings.append({"title": "b", "pass": p, "rev": "r"}); members.append(i); i += 1
            clusters.append({"representative": members[0], "members": members})
            verdicts[members[0]] = {"verdict": "real", "severity": "S1"}
        for _ in range(fp_count):
            findings.append({"title": "fp", "pass": 1, "rev": "r"})
            clusters.append({"representative": i, "members": [i]}); verdicts[i] = {"verdict": "false_positive"}; i += 1
        return findings, clusters, verdicts
    a = round_stats(*build([{1}, {2}, {3}, {1}, {1, 2}], 1), "r", {"S1", "S2"}, 0.25)
    b = round_stats(*build([{1, 2, 3, 4, 5}, {1, 2, 3, 4, 5}], 9), "r", {"S1", "S2"}, 0.25)
    b3 = round_stats(*build([{1, 2, 3}, {1, 2, 3}], 9), "r", {"S1", "S2"}, 0.25)   # same, only 3 passes: not enough yet
    ok = (a["decision"] == "CONTINUE" and b["decision"] == "STOP" and b3["decision"] == "CONTINUE"
          and abs(garwood_upper(0, 0.05) - 2.9957) < 1e-3 and abs(garwood_upper(0, 0.25) - 1.3863) < 1e-3)
    print(json.dumps({"round_A": a, "round_B_5_passes": b, "round_B_3_passes": b3}, indent=1))
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--findings"); ap.add_argument("--clusters"); ap.add_argument("--verdicts")
    ap.add_argument("--rev", default=None)
    ap.add_argument("--alpha", type=float, default=0.25, help="stop when P(new major next pass) is below this")
    ap.add_argument("--confidence", type=float, default=0.75, help="one-sided confidence of the upper bound")
    ap.add_argument("--majors", default="S1,S2"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.findings and a.clusters and a.verdicts):
        ap.error("--findings, --clusters and --verdicts are required (or --selftest)")
    verdicts = {v["representative"]: v for v in load_jsonl(a.verdicts)}
    s = round_stats(load_jsonl(a.findings), load_jsonl(a.clusters), verdicts, a.rev, set(a.majors.split(",")), a.alpha,
                    a.confidence)
    print(json.dumps(s, indent=1))
    return 0 if s["decision"] == "STOP" else 3


if __name__ == "__main__":
    sys.exit(main())
