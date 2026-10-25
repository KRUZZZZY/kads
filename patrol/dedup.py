#!/usr/bin/env python3
"""Group repeated Luna patrol findings into one cluster per defect.

Method (v2, measured against hand-labelled gold in research/reviews/luna-dedup-2026-10-05/):
  1. Embed each finding (title, what, when, fix) with a local Ollama embedding model.
  2. Compare findings only within the same source file. Cosine >= HI is the same defect,
     cosine < LO is a different defect, and pairs in between are judged by local Clef
     (the same-defect question classify.py uses), giving a probability.
  3. Agglomerate with average linkage at distance T, never joining two findings from the
     same patrol pass: one reviewer reads each slice once and numbers distinct bugs, so a
     pass never reports one defect twice (0 of 516 gold duplicate pairs share a pass).

Every judgement is journalled with its content hash, so a re-run resumes. Clusters are written
in the clusters.jsonl shape classify.py uses ({representative, members}); the representative is
the member from the latest pass, whose text matches the newest code. Clusters are suggestions:
reproduce a defect before fixing it.
"""
import argparse
import collections
import hashlib
import json
import pathlib
import re
import time
import urllib.request

import numpy as np

DEFAULTS = {"lo": 0.84, "hi": 0.94, "t": 0.40}
QUESTION = ("Do the two findings describe the SAME root cause in the SAME place? "
            "Shared file, theme, symptom or proposed fix alone is insufficient. Distinct operations "
            "or independent defects count as different. Judge original claims regardless of whether fixed.")


def field(text, key, following):
    m = re.search(key + r":(.*?)(?:\n\s*(?:" + following + r"):|$)", text, re.S)
    return " ".join(m.group(1).split()) if m else ""


def embed_text(f):
    t = f["text"]
    return (f"{f['title']}. {field(t, 'what', 'when')} {field(t, 'when', 'fix')} "
            f"Fix: {field(t, 'fix', 'confidence')}")


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def post(url, body, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def embed_all(findings, host, model, cache):
    """Unit-normalised embeddings, cached by model and text hash."""
    keys = [sha(model + "\0" + embed_text(f)) for f in findings]
    store = {}
    if cache.exists():
        z = np.load(cache, allow_pickle=False)
        store = dict(zip(z["keys"].tolist(), z["vecs"]))
    todo = [i for i, k in enumerate(keys) if k not in store]
    for s in range(0, len(todo), 48):
        chunk = todo[s:s + 48]
        out = post(host + "/api/embed", {"model": model, "input": ["clustering: " + embed_text(findings[i]) for i in chunk]})
        for i, v in zip(chunk, out["embeddings"]):
            store[keys[i]] = np.asarray(v, dtype=np.float32)
    if todo:
        np.savez(cache, keys=np.array(list(store)), vecs=np.stack(list(store.values())))
    X = np.stack([store[k] for k in keys])
    return X / np.linalg.norm(X, axis=1, keepdims=True)


def band_pairs(findings, X, lo, hi):
    byfile = collections.defaultdict(list)
    for i, f in enumerate(findings):
        byfile[f["file"]].append(i)
    for ids in byfile.values():
        for x, a in enumerate(ids):
            for b in ids[x + 1:]:
                c = float(X[a] @ X[b])
                if lo <= c < hi and findings[a]["pass"] != findings[b]["pass"]:
                    yield a, b


def judge_band(findings, X, args, journal):
    """Clef same-defect probability for every in-band pair, journalled and resumable."""
    known = {}
    if journal.exists():
        for row in map(json.loads, journal.read_text().splitlines()):
            known[(row["a"], row["b"], row["h"])] = row["p"]
    pairs = list(band_pairs(findings, X, args.lo, args.hi))
    out = {}
    with journal.open("a") as j:
        for n, (a, b) in enumerate(pairs, 1):
            h = sha(findings[a]["text"] + "\0" + findings[b]["text"])
            if (a, b, h) not in known:
                state = {"finding_a": findings[a]["text"][:2500], "finding_b": findings[b]["text"][:2500]}
                ans = post(args.clef + "/v1/systemone", {"model": "clef-flash", "state": state,
                                                          "questions": {"same": {"type": "noul", "instructions": QUESTION}}})
                p = ans["answers"]["same"].get("noul")
                if p is None:
                    raise RuntimeError(f"Clef gave no probability for pair {a},{b}")
                known[(a, b, h)] = p
                j.write(json.dumps({"a": a, "b": b, "h": h, "p": p}) + "\n")
                j.flush()
            out[(a, b)] = known[(a, b, h)]
            if n % 500 == 0:
                print(f"judged {n}/{len(pairs)} band pairs", flush=True)
    return out


def constrained_average(ids, sim, t, cannot):
    """Average-linkage agglomeration; never merges clusters containing a cannot-link pair."""
    clusters = [[k] for k in range(len(ids))]
    while True:
        best = None
        for x in range(len(clusters)):
            for y in range(x + 1, len(clusters)):
                if any(cannot(ids[p], ids[q]) for p in clusters[x] for q in clusters[y]):
                    continue
                avg = float(np.mean([sim[p, q] for p in clusters[x] for q in clusters[y]]))
                if avg >= 1 - t and (best is None or avg > best[0]):
                    best = (avg, x, y)
        if best is None:
            return clusters
        _, x, y = best
        clusters[x] += clusters.pop(y)


def cluster(findings, X, judged, lo, hi, t):
    byfile = collections.defaultdict(list)
    for i, f in enumerate(findings):
        byfile[f["file"]].append(i)
    same_pass = lambda a, b: findings[a]["pass"] == findings[b]["pass"]
    result = []
    for ids in byfile.values():
        n = len(ids)
        sim = np.zeros((n, n))
        for x in range(n):
            for y in range(x + 1, n):
                a, b = ids[x], ids[y]
                c = float(X[a] @ X[b])
                v = 1.0 if c >= hi else 0.0 if c < lo else judged.get((a, b), 0.0)
                sim[x, y] = sim[y, x] = v
        for members in constrained_average(ids, sim, t, same_pass):
            m = sorted(ids[k] for k in members)
            rep = max(m, key=lambda i: (findings[i]["pass"], -i))
            result.append({"representative": rep, "members": m})
    return sorted(result, key=lambda c: c["members"][0])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--findings", required=True, help="findings snapshot (jsonl)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--clef", default="http://127.0.0.1:11434")
    ap.add_argument("--embed-model", default="nomic-embed-text")
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k, type=float, default=v)
    ap.add_argument("--gold", nargs="*", default=[], help="gold files to score the result against")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    findings = [json.loads(l) for l in open(args.findings)]
    X = embed_all(findings, args.ollama, args.embed_model, out / "embeddings.npz")
    judged = judge_band(findings, X, args, out / "pair-judgements.jsonl")
    clusters = cluster(findings, X, judged, args.lo, args.hi, args.t)
    with (out / "clusters.jsonl").open("w") as f:
        for c in clusters:
            f.write(json.dumps(c) + "\n")
    summary = {"findings": len(findings), "clusters": len(clusters),
               "repeat_reports": len(findings) - len(clusters), "band_pairs_judged": len(judged),
               "params": {"lo": args.lo, "hi": args.hi, "t": args.t, "embed_model": args.embed_model,
                          "judge": "clef-flash same-defect", "cannot_link": "same patrol pass"},
               "findings_sha256": hashlib.sha256(open(args.findings, "rb").read()).hexdigest()}
    if args.gold:
        from dedup_eval import score
        pred = {m: ("c", c["representative"]) for c in clusters for m in c["members"]}
        summary["gold"] = {}
        for g in args.gold:
            s = score(pred, g)
            s.pop("errors")
            summary["gold"][pathlib.Path(g).name] = s
    (out / "SUMMARY.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "gold"}))


if __name__ == "__main__":
    main()
