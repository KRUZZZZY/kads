#!/usr/bin/env python3
"""Retriage v2: turn Clef's `still_present` answer into an ordering/skip signal instead of a realness verdict.

Why (measured 2026-10-25, research/reviews/retriage-v2/REPORT-retriage.md): of 1,634 queued bugs the fixers touched,
1,614 were accepted fixes and only ~26 were ever recorded as not-real (ACCEPTED.tsv), so asking "is this a real bug?" has
almost nothing to find and Clef's conservative `real` score (median 2.85 of 4) fails classify.disposition's `real >= 3`
test for most rows, which is why most rows read `needs_context`. The informative answer was already in the journal:
`still_present` ("does the current code still contain the defect?"). Reading the code at the cited lines for the 22
lowest-ranked rows plus a sample showed the low tail is overwhelmingly already repaired (labels.json), while
classify.disposition only drops a row when ALSO `already_fixed >= 0.75` (7 of 1,630 rows).

What this does:
  default      read qwen/CLEF-RETRIAGE.jsonl (+ research/reviews/retriage-v2/rows.jsonl, newer wins) and write
               qwen/CLEF-OPEN-SCORE.json  {cluster: {"still_present": p, "action": ..., "base": ...}}. No GPU.
  --refresh N  re-ask the N lowest-scoring untouched bugs (plus bugs never retriaged) on the CURRENT FIX-BASE, because the
               old rows were read on 050/m0-train-6 and ~10 trains have landed since. Journal: research/reviews/
               retriage-v2/rows.jsonl (append-only; the old journal is never touched). One GpuLease for all calls.
Actions: already_fixed_likely (p <= 0.25), stale_check (p <= 0.50), open. The fixer should skip the first (or send it to a
cheap "is this still present?" check) and sort the second last; see REPORT.md for the proposed qwenfix.py diff.

Usage: retriage_v2.py [--refresh N] [--dry-run]
"""
import argparse
import datetime
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PROG = HERE.parents[1]
QWEN = pathlib.Path.home() / ".local/share/zene-bunny-fix/qwen"
OLD = QWEN / "CLEF-RETRIAGE.jsonl"
OUT_DIR = PROG / "research/reviews/retriage-v2"
ROWS = OUT_DIR / "rows.jsonl"
SCORES = QWEN / "CLEF-OPEN-SCORE.json"
LIKELY_FIXED_AT = 0.25
STALE_AT = 0.50


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def action(p):
    if p is None:
        return "open"
    return "already_fixed_likely" if p <= LIKELY_FIXED_AT else "stale_check" if p <= STALE_AT else "open"


def still_present(answers):
    try:
        return float(answers["still_present"]["noul"])
    except (KeyError, TypeError, ValueError):
        return None


def read_rows(path):
    p = pathlib.Path(path)
    out = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def build(old_rows, new_rows):
    """Latest row per cluster; v2 rows (re-read on a newer base) win over the old journal."""
    latest = {}
    for r in list(old_rows) + list(new_rows):
        p = still_present(r.get("answers") or {})
        if p is None:
            continue
        latest[str(r["cluster"])] = {"still_present": round(p, 4), "action": action(p), "base": r.get("base"),
                                     "revision": r.get("revision")}
    return latest


def refresh_order(scores, untouched, never_seen, n):
    """Which clusters to re-ask: bugs with no row first (unknown), then the lowest still_present, untouched only."""
    unseen = [c for c in never_seen if c in untouched]
    seen = sorted((c for c in scores if c in untouched), key=lambda c: scores[c]["still_present"])
    return (unseen + seen)[:n]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    scores = build(read_rows(OLD), read_rows(ROWS))
    if a.refresh:
        import hashlib

        import classify
        import retriage_open as RO
        import triage_new as T
        q = RO.qwenfix()
        repo = T.Repo(PROG / "zene-cachyos")
        base = (QWEN / "FIX-BASE").read_text().strip()
        rev = repo.full(base)
        skip = q.luna_easy_attempted()
        untouched = {}
        for i in q.load_queue():
            c = str(i["cluster"])
            if q.slug(i["cluster"]) not in skip and not (q.D["state"] / f"{q.slug(i['cluster'])}.json").exists():
                untouched[c] = i
        done = {(str(r["cluster"]), r.get("revision")) for r in read_rows(ROWS)}
        order = [c for c in refresh_order(scores, set(untouched), [c for c in untouched if c not in scores], a.refresh * 3)
                 if (c, rev) not in done][:a.refresh]
        print(f"{len(order)} bug(s) to re-ask on {base} ({rev[:9]})", flush=True)
        if a.dry_run or not order:
            return
        cfg = T.Config(repo=PROG / "zene-cachyos")
        pins = T.OllamaModels(cfg).pins()
        with T.GpuLease(QWEN):
            for n, c in enumerate(order, 1):
                item = untouched[c]
                name = item.get("resolved_path") or item.get("file") or ""
                path = repo.resolve(rev, name)
                source = repo.show(rev, path)
                orig_rev = item.get("revision") or item.get("original_revision") or rev
                original = repo.show(orig_rev, repo.resolve(orig_rev, name))
                finding = {"file": item.get("file", ""), "line": int(item.get("line") or 0),
                           "text": item.get("text") or item.get("title", "")}
                state = {"contracts": classify.CONTRACTS, "current_revision": rev, "file": path,
                         "current_code": classify.source_context(source, [finding], 8000),
                         "original_code": "IDENTICAL to current code" if original == source else
                         classify.source_context(original, [finding], 6000),
                         "prior_review_evidence": [], "finding": T.clef_text(finding["text"])[:3000]}
                answers = classify.post(cfg.host, state, classify.QUESTIONS)
                row = {"cluster": c, "revision": rev, "base": base, "path": path, "answers": answers,
                       "context_sha256": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
                       "model_digest": pins["clef"]["digest"], "decided": now()}
                with ROWS.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                if n % 25 == 0 or n == len(order):
                    print(f"{n}/{len(order)} re-asked", flush=True)
        scores = build(read_rows(OLD), read_rows(ROWS))
    counts = {}
    for v in scores.values():
        counts[v["action"]] = counts.get(v["action"], 0) + 1
    if a.dry_run:
        print(f"dry run, {len(scores)} bugs {counts}")
        return
    SCORES.write_text(json.dumps(scores))
    print(f"wrote {SCORES.name}: {len(scores)} bugs {counts}")


if __name__ == "__main__":
    main()
