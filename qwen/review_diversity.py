#!/usr/bin/env python3
"""Reviewer diversity: the reviewer should come from a different model family than the author.

Idea sources (see vendor/NOTICE.md): "Cross-Provider Review as a Runtime Contract for Coding Agents" (arXiv
2610.10961) and AscendyProject/redteam (Apache-2.0), plus our own `fixed_by_haiku` rule in qwenfix.py, which already
keeps Haiku from reviewing Haiku. Fixer and reviewer from one family share blind spots; the pipeline blueprint
(plans/PIPELINE-FIND-TO-MERGE.md section 12.3) lists this as its main risk. Measured 2026-10-25: about half of all
reviewed attempts were GPT-6 Luna reviewing Luna (see `python3 review_diversity.py audit`).

This module only decides; it never starts a review. qwenfix.qa_loop asks `reviewer_ok(rec, reviewer)` before it
hands a pending review to a reviewer, next to the `fixed_by_haiku` check.

Knob file (absent = off): qwen/REVIEW-DIVERSITY.json
    {"enabled": true, "mode": "prefer", "max_wait_h": 6}
  mode "require": a same-family pairing is never allowed (the review waits for another reviewer, however long);
  mode "prefer":  a same-family pairing is held back for max_wait_h hours, then allowed so nothing starves.
  optional "families": {"substring": "family"} overrides/extends the built-in table.

  python3 review_diversity.py audit [qwen-root]    share of same-family reviews, and how each pairing fared
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOB = ROOT / "REVIEW-DIVERSITY.json"
# first match wins; substring of the lower-cased model/harness string
FAMILIES = [("claude", "anthropic"), ("haiku", "anthropic"), ("sonnet", "anthropic"), ("opus", "anthropic"),
            ("gpt", "openai"), ("luna", "openai"), ("codex", "openai"), ("deepseek", "deepseek"),
            ("minimax", "minimax"), ("qwen", "alibaba"), ("strata", "alibaba"), ("glm", "zhipu"),
            ("mimo", "xiaomi"), ("muse", "meta"), ("hy3", "tencent"), ("kimi", "moonshot")]
UNKNOWN = "unknown"       # hosted free models whose maker we cannot tell (beam, space-bunny, cat-jam): never "same"


def config():
    try:
        cfg = json.loads(KNOB.read_text())
    except (OSError, ValueError):
        return {"enabled": False}
    return {"mode": "prefer", "max_wait_h": 6, **cfg}


def family(model, extra=None):
    m = str(model or "").lower()
    for sub, fam in list((extra or {}).items()) + FAMILIES:
        if sub.lower() in m:
            return fam
    return UNKNOWN


def same_family(author, reviewer, extra=None):
    a, r = family(author, extra), family(reviewer, extra)
    return a == r != UNKNOWN


def author_model(rec):
    """Records from before 2026-10-08 have no model and were Luna's (qwenfix.fixer_label does the same)."""
    return rec.get("model") or "gpt-6-luna"


REVIEWER_MODEL = {"luna": "gpt-6-luna", "haiku": "claude-haiku-5-5", "qwen": "qwen"}      # qa_one's reviewer names


def reviewer_ok(rec, reviewer, waited_h=None, cfg=None):
    """May this pending review go to this reviewer now? `reviewer` is qa_one's name ('luna', 'haiku', 'qwen')."""
    cfg = cfg if cfg is not None else config()
    if not cfg.get("enabled"):
        return True
    if not same_family(author_model(rec), REVIEWER_MODEL.get(reviewer, reviewer), cfg.get("families")):
        return True
    if cfg["mode"] == "require":
        return False
    return waited_h is not None and waited_h >= float(cfg["max_wait_h"])


def waited_hours(path, now=None):
    """Hours a pending review file has been waiting (its mtime; qa_loop touches it when a review is deferred)."""
    try:
        return ((now or time.time()) - Path(path).stat().st_mtime) / 3600
    except OSError:
        return None


def audit(qwen_root=ROOT):
    """Count reviewed attempts by (author family, reviewer family) and how each pairing ended."""
    rows, out = [], {}
    for p in sorted((Path(qwen_root) / "qa" / "done").glob("*.json")):
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        qa = rec.get("qa") or {}
        verdict = str(qa.get("verdict") or "")
        if not verdict:
            continue
        pair = (family(author_model(rec)), family(qa.get("reviewer_model") or qa.get("reviewer")))
        same = pair[0] == pair[1] != UNKNOWN
        row = out.setdefault("same" if same else "cross", {"n": 0, "accept": 0, "rework": 0})
        row["n"] += 1
        row["accept" if verdict.startswith(("ACCEPT", "CONFIRM")) else "rework"] += 1
        pairs = out.setdefault("pairs", {})
        pr = pairs.setdefault(f"{pair[0]} -> {pair[1]}", {"n": 0, "accept": 0})
        pr["n"] += 1
        pr["accept"] += verdict.startswith(("ACCEPT", "CONFIRM"))
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["audit"]:
        res = audit(sys.argv[2] if len(sys.argv) > 2 else ROOT)
        total = sum(res.get(k, {}).get("n", 0) for k in ("same", "cross")) or 1
        for k in ("same", "cross"):
            r = res.get(k, {"n": 0, "accept": 0})
            print(f"{k:5} n={r['n']:5} ({100 * r['n'] / total:.0f}%)  accepted {100 * r['accept'] / max(r['n'], 1):.0f}%")
        for pair, r in sorted(res.get("pairs", {}).items(), key=lambda kv: -kv[1]["n"]):
            print(f"  {pair:28} n={r['n']:5} accepted {100 * r['accept'] / r['n']:.0f}%")
    else:
        print(__doc__)
