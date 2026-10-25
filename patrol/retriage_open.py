#!/usr/bin/env python3
"""Re-ask Clef whether each untouched fix-queue bug is still open on the fix loop's current base.

Owner 2026-10-19: "get clef classifying everything it can" / "from each according to their means". Every queued bug
was classified once, on the revision its finding was read from (2026-10-03..10). Since then ~930 fixes and the M0
feature merges landed, so some queued findings are already repaired. Asking local Clef (free) first keeps the paid and
free fixers off those.

For each queued bug the fixer has not touched (no qwen/state/<slug>.json, not tried by Luna-easy), in the fixer's own
order: the same realness questions and state as triage_new.py (classify.QUESTIONS / classify.disposition), with
"current code" = the file at FIX-BASE (resolved in zene-cachyos) and "original code" = the file at the finding's
revision. One row per (cluster, revision) goes to qwen/CLEF-RETRIAGE.jsonl (resumable: done pairs are skipped).
qwenfix.load_queue drops a cluster whose latest row is already_fixed or likely_false_positive (Clef's own conservative
thresholds); deleting the file undoes that. Model errors stop the run; they never count as verdicts.

All Clef calls run inside one GpuLease (the GPU-REQUEST / GPU-FREE handshake with the fixer).

Usage: retriage_open.py [--limit N] [--dry-run]
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import classify  # noqa: E402
import triage_new as T  # noqa: E402

PROG = HERE.parents[1]
QWEN = pathlib.Path.home() / ".local/share/zene-bunny-fix/qwen"
OUT = QWEN / "CLEF-RETRIAGE.jsonl"
LOG = QWEN / "logs/clef-retriage.log"


def log(msg):
    line = f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def qwenfix():
    sys.path.insert(0, str(QWEN))   # qwenfix imports its siblings (adaptive, opencode_free)
    spec = importlib.util.spec_from_file_location("qwenfix", QWEN / "qwenfix.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    q = qwenfix()
    repo = T.Repo(PROG / "zene-cachyos")
    base = (QWEN / "FIX-BASE").read_text().strip()
    rev = repo.full(base)
    done = {(r["cluster"], r["revision"]) for r in T.rows(OUT)}
    skip = q.luna_easy_attempted()
    todo = [i for i in q.adaptive.order(q.load_queue())
            if q.slug(i["cluster"]) not in skip and not (q.D["state"] / f"{q.slug(i['cluster'])}.json").exists()
            and (str(i["cluster"]), rev) not in done]
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(todo)} untouched queued bug(s) to re-check on {base} ({rev[:9]}); {len(done)} row(s) already in {OUT.name}")
    if args.dry_run or not todo:
        return
    cfg = T.Config(repo=PROG / "zene-cachyos")
    pins = T.OllamaModels(cfg).pins()
    counts = {}
    with T.GpuLease(QWEN):
        for n, item in enumerate(todo, 1):
            name = item.get("resolved_path") or item.get("file") or ""
            path = repo.resolve(rev, name)
            source = repo.show(rev, path)
            orig_rev = item.get("revision") or item.get("original_revision") or rev
            original = repo.show(orig_rev, repo.resolve(orig_rev, name))
            finding = {"file": item.get("file", ""), "line": int(item.get("line") or 0), "text": item.get("text") or item.get("title", "")}
            evidence = item.get("prior_review_evidence") or [{"report": e.get("path"), "text": str(e.get("text", ""))[:600]}
                                                             for e in item.get("report_evidence") or [] if isinstance(e, dict)]
            state = {"contracts": classify.CONTRACTS, "current_revision": rev, "file": path,
                     "current_code": classify.source_context(source, [finding], 8000),
                     "original_code": "IDENTICAL to current code" if original == source else
                     classify.source_context(original, [finding], 6000),
                     "prior_review_evidence": evidence, "finding": T.clef_text(finding["text"])[:3000]}
            answers = classify.post(cfg.host, state, classify.QUESTIONS)
            disp = classify.disposition(answers) if path and original else "needs_context"
            counts[disp] = counts.get(disp, 0) + 1
            T.append_rows(OUT, [{"cluster": str(item["cluster"]), "revision": rev, "base": base, "path": path,
                                 "clef_tier": item.get("clef_tier"), "prior_disposition": item.get("realness") or item.get("prior_disposition"),
                                 "disposition": disp, "answers": answers,
                                 "context_sha256": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
                                 "model": pins["clef"]["name"], "model_digest": pins["clef"]["digest"],
                                 "decided": T.now()}])
            if n % 25 == 0 or n == len(todo):
                log(f"{n}/{len(todo)} re-checked: {counts}")


if __name__ == "__main__":
    main()
