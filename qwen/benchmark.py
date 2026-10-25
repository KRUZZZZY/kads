#!/usr/bin/env python3
"""Per-model benchmark of the fix/feature loop (owner, 2026-10-15: "create a success rate and weight it by the
difficulty of tasks, and a speed rate, and have it track each model, just so we can build up a benchmark over time").

One row per ATTEMPT (a fixer session = runs/<slug>.r<round>/record.json). An attempt succeeds when its QA verdict is
ACCEPT / ACCEPT-NOTE / CONFIRM (features: both Luna passes). It fails on REWORK / REJECT / DISPUTE / QA-ERROR or when
the session committed nothing. Attempts still in QA count as pending and are left out of the rates. Sessions that never
reached a model (endpoint limits, archived under runs/_free-limit and runs/_usage-limit) are not attempts.

Difficulty weight (1 = easy .. 3 = hard):
- bugs: Clef tier: easy 1, unclassified 1.5, needs_context 2, hard 3;
- features: 1 + 2 * Clef difficulty percentile (the unit's rank in research/reviews/clef-difficulty-2026-10-13).
Weighted success = sum(weight * success) / sum(weight) over decided attempts.
Speed: median session minutes per attempt, median minutes per success, and successes per session-hour.
Reviewers: Luna review count, median minutes and verdict split.

Writes BENCHMARK.json and BENCHMARK.md next to this file; appends a dated snapshot to benchmark-history.jsonl.
Run by hand or from the QA loop after each verdict (cheap: reads a few thousand small JSON files).
"""
import json
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROG = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
RANKED = PROG / "research/reviews/clef-difficulty-2026-10-13/ranked.jsonl"
BUG_WEIGHT = {"easy": 1.0, "new:easy": 1.0, "unclassified": 1.5, "UNCLASSIFIED": 1.5,
              "needs_context": 2.0, "new:needs_context": 2.0, "hard": 3.0, "new:hard": 3.0}
SUCCESS = {"ACCEPT", "ACCEPT-NOTE", "ACCEPT-FIXED", "CONFIRM"}


TOKEN_CACHE = ROOT / "tokens-cache.json"
_token_cache = None
CODEX_TOTAL = re.compile(r"^tokens used\n([\d,]+)", re.M)


def run_tokens(run_dir):
    """Owner 2026-10-19 ("start tracking output tokens per task"): (output, total, exact) for one run.
    OpenCode: output + reasoning summed over the step_finish events of session.log (exact). Codex/Luna: its log
    prints only a session total ("tokens used"), so output stays None and total is that figure. Cached by mtime."""
    global _token_cache
    if _token_cache is None:
        _token_cache = load(TOKEN_CACHE) or {}
    log = Path(run_dir) / "session.log"
    try:
        st = log.stat()
    except OSError:
        return None, None
    key = f"{log}:{st.st_mtime_ns}:{st.st_size}"
    if key in _token_cache:
        return tuple(_token_cache[key])
    out = total = None
    text = log.read_text(errors="replace")
    if '"step_finish"' in text:
        out = total = 0
        for line in text.splitlines():
            if '"step_finish"' not in line:
                continue
            try:
                t = json.loads(line).get("part", {}).get("tokens") or {}
            except ValueError:
                continue
            out += (t.get("output") or 0) + (t.get("reasoning") or 0)
            total += t.get("total") or 0
    elif '"phase":"step_end"' in text:       # DeepSeek Harness (dsh headless --json, 2026-10-20): per-step usage, exact
        out = total = 0
        for line in text.splitlines():
            if '"step_end"' not in line:
                continue
            try:
                u = json.loads(line).get("usage") or {}
            except ValueError:
                continue
            out += u.get("outputTokens") or 0
            total += u.get("totalTokens") or 0
    elif '"type":"turn.completed"' in text:  # MiniMax Code (mcode exec stream-json, 2026-10-20): per-turn usage, exact
        out = total = 0
        for line in text.splitlines():
            if '"turn.completed"' not in line:
                continue
            try:
                u = json.loads(line).get("usage") or {}
            except ValueError:
                continue
            out += u.get("outputTokens") or 0
            total += u.get("totalTokens") or 0
    elif '"type":"result"' in text:          # Claude (haiku-exec stream-json): the result event's usage, exact
        for line in text.splitlines():
            if '"type":"result"' not in line:
                continue
            try:
                u = json.loads(line).get("usage") or {}
            except ValueError:
                continue
            out = (out or 0) + (u.get("output_tokens") or 0)
            total = (total or 0) + sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens",
                                                               "cache_creation_input_tokens", "cache_read_input_tokens"))
    else:
        m = CODEX_TOTAL.findall(text)
        if m:
            total = sum(int(x.replace(",", "")) for x in m)
    _token_cache[key] = [out, total]
    return out, total


def save_token_cache():
    if _token_cache is not None:
        TOKEN_CACHE.write_text(json.dumps(_token_cache))


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def feature_pct():
    out = {}
    try:
        for line in RANKED.read_text().splitlines():
            r = json.loads(line)
            out[r["queue_id"]] = float(r["pct"])
    except OSError:
        pass
    return out


SPACE_BUNNY = "space-bunny / MiniMax M3.1"
# owner 2026-10-19: "minimax m3.1 is space bunny btw, you can merge them in the benchmarks" - every route of it is one row
ALIASES = {"luna": "gpt-6-luna", "opencode/free": "opencode (model not recorded)",
           "opencode/space-bunny-free": SPACE_BUNNY, "space-bunny-free": SPACE_BUNNY, "opencode-go/space-bunny": SPACE_BUNNY,
           "opencode/minimax-m3.1-flash-free": SPACE_BUNNY, "opencode-go/minimax-m3.1-flash-free": SPACE_BUNNY,
           "minimax-m3.1-flash-free": SPACE_BUNNY}


# DeepSeek shipped V4.1 Flash 1017 in place under the same OpenCode ids (Go and Free, ~17:00 2026-10-19). The loop ran no
# DeepSeek between the Go hold and 2026-10-19T20:22, when 1017 went onto the feature tier, so a DeepSeek run started from then on
# is 1017 and gets its own row (owner: "make sure it shows up differently on benchmarks").
DEEPSEEK_1017_FROM = "2026-10-19T20:22:57"
HAIKU_BUG_LANE_FROM = "2026-10-20T00:00:00"
DEEPSEEK_IDS = {"opencode-go/deepseek-v4.1-flash", "opencode/deepseek-v4.1-flash", "opencode/deepseek-v4.1-flash-free"}


def model_of(rec):
    # records before 2026-10-08 carry no engine field; the fixer is not recoverable from them
    m = rec.get("model") or rec.get("engine") or "unrecorded (pre-2026-10-08)"
    if m == "claude-haiku-5-5" and str(rec.get("started") or "") >= HAIKU_BUG_LANE_FROM:
        return "claude-haiku-5-5 (bug lane)"     # 2026-10-20 trial on the easier bugs, apart from the 10-14/15 driver runs
    if m in DEEPSEEK_IDS:
        return m + (" (1017)" if str(rec.get("started") or "") >= DEEPSEEK_1017_FROM else " (pre-1017)")
    return ALIASES.get(m, m)


def never_reached_model(run_dir, rec):
    """A session that got no answer from the model is an infrastructure failure, not an attempt: no commits and either
    under a minute (usage-limit stops, failed starts) or OpenCode usage files with no completed step."""
    if rec.get("commits"):
        return False
    if (rec.get("minutes") or 0) < 1:
        return True
    usage = list(run_dir.glob("opencode-*-usage.json"))
    return bool(usage) and all(not (load(u) or {}).get("step_costs") for u in usage)


def bug_pct():
    """Owner 2026-10-19: bug difficulty on Clef's own curve, like features. Each classified bug's Clef difficulty
    answers give d = mean(1 - P(easy), P(hard), 1 - P(self-contained)); every bug is ranked by d across the whole
    Clef inventory (the 10-06 campaign queues + the patrol triage queues) and weighted 1 + 2 x its percentile."""
    try:          # 2026-10-19: Clef's pairwise "is this bug harder than that one" ranking, when it exists
        ranked = json.loads((ROOT / "BUG-RANK.json").read_text())
        if ranked:
            return ranked
    except (OSError, ValueError):
        pass
    prog = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program/research/reviews"
    score = {}
    for f in list((prog / "luna-easy-fix-2026-10-06/queue").glob("*.jsonl")) + list((prog / "luna-new-triage/queue").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            a = r.get("difficulty_answers") or r.get("answers") or {}
            if not all(k in a for k in ("easy", "hard", "context")):
                continue
            n = lambda k: a[k].get("noul", 0.5)
            score[str(r["cluster"])] = ((1 - n("easy")) + n("hard") + (1 - n("context"))) / 3
    ranked = sorted(score, key=score.get)
    return {c: i / max(1, len(ranked) - 1) for i, c in enumerate(ranked)}


def bug_key(cluster):
    c = str(cluster)
    while c.endswith("-port"):
        c = c[:-len("-port")]
    return c


def attempts():
    pct = feature_pct()
    bpct = bug_pct()
    done = {p.stem: load(p) for p in (ROOT / "qa/done").glob("*.json")}
    pending = {p.stem for p in (ROOT / "qa/pending").glob("*.json")}
    rows = []
    for rec_path in (ROOT / "runs").glob("*/record.json"):
        if rec_path.parent.name.startswith("_"):
            continue
        rec = load(rec_path)
        if not rec or "minutes" not in rec:
            continue
        key = rec_path.parent.name                       # <slug>.r<round>
        if never_reached_model(rec_path.parent, rec):
            continue
        feature = str(rec.get("tier", "")).startswith("feature:")
        if feature:
            weight = 1 + 2 * pct.get(rec["cluster"], 0.5)
        else:
            bp = bpct.get(bug_key(rec["cluster"]))
            weight = 1 + 2 * bp if bp is not None else BUG_WEIGHT.get(str(rec.get("tier", "unclassified")), 1.5)
        qa = done.get(key)
        if not rec.get("commits"):
            outcome = "no-result"
        elif key in pending:
            outcome = "pending"
        elif qa:
            outcome = qa.get("outcome") or (qa.get("qa") or {}).get("verdict") or "unknown"
        else:
            outcome = "unknown"
        review = (qa or {}).get("qa") or {}
        if outcome == "ACCEPT-FIXED" and (qa or {}).get("kind") == "claim":
            outcome = "DISPUTE"          # the fixer said "not a bug" and Luna fixed it: not the fixer's success
        cost = 0.0                # OpenCode-reported cost (Go plan quota at API prices; 0 for free models)
        for u in rec_path.parent.glob("opencode-*-usage.json"):
            cost += sum(c or 0 for c in (load(u) or {}).get("step_costs") or [])
        out_tokens, total_tokens = run_tokens(rec_path.parent)
        batch = (rec.get("item") or {}).get("batch") or []
        if batch:        # 2026-10-19 same-file batch: time and tokens are shared by its bugs
            share = 1 + len(batch)
            rec = {**rec, "minutes": round((rec.get("minutes") or 0.0) / share, 2)}
            out_tokens = round(out_tokens / share) if out_tokens else out_tokens
            total_tokens = round(total_tokens / share) if total_tokens else total_tokens
            for b in batch:
                bv = (rec.get("batch_verdicts") or {}).get(str(b["cluster"]))
                if bv not in ("FIXED", "ALREADY-FIXED", "FALSE-POSITIVE", "IMPLEMENTED"):
                    continue      # released back to the queue: not this attempt's result
                bp = pct.get(b["cluster"]) if feature else bpct.get(bug_key(b["cluster"]))
                rows.append({"key": f"{key}+{b['cluster']}", "cost": 0.0, "out_tokens": out_tokens, "total_tokens": total_tokens,
                             "started": rec.get("started"), "cluster": b["cluster"], "round": rec.get("round"), "kind": "feature" if feature else "bug",
                             "model": model_of(rec), "weight": round(1 + 2 * bp, 3) if bp is not None else (2.0 if feature else 1.5),
                             "batch_size": share,
                             "minutes": rec["minutes"], "outcome": outcome, "success": outcome in SUCCESS,
                             "finished": rec.get("finished"), "review_minutes": None, "reviewer": None,
                             "code_review_minutes": None, "batch_of": rec["cluster"]})
        rows.append({"key": key, "batch_size": (1 + len(batch)) if batch else 1,
                     "cost": round(cost, 5), "out_tokens": out_tokens, "total_tokens": total_tokens,
                     "started": rec.get("started"), "cluster": rec["cluster"], "round": rec.get("round"), "kind": "feature" if feature else "bug",
                     "model": model_of(rec) + harness_label(rec_path.parent),
                     "weight": round(weight, 3), "minutes": rec.get("minutes") or 0.0,
                     "outcome": outcome, "success": outcome in SUCCESS, "finished": rec.get("finished"),
                     "review_minutes": review.get("minutes"),
                     # QA was Luna-only until Haiku joined on 2026-10-15; older verdicts never recorded the reviewer
                     "reviewer": review.get("reviewer_model") or ("gpt-6-luna" if str(rec.get("finished") or "") < "2026-10-15" else None),
                     "code_review_minutes": ((qa or {}).get("qa_code") or {}).get("minutes"),
                     # owner 2026-10-20: accepted after Luna fixed a small problem itself (either verifier)
                     "luna_fixed": any(((x or {}).get("qa_fix") or {}).get("ok") for x in ((qa or {}).get("qa"), (qa or {}).get("qa_code")))})
    rows += claude_lanes(pct)
    return rows


def claude_lanes(pct):
    """Sonnet/Opus lane rounds run by the orchestrating Claude session (top feature tiers, CI lanes): one JSON object
    per round in CLAUDE-LANES.jsonl - {key, cluster, kind, model, round, started, finished, minutes, outcome}.
    outcome is the verdict pair's result (ACCEPT/ACCEPT-NOTE when both verifiers accept, else REWORK), or pending."""
    rows = []
    path = ROOT / "CLAUDE-LANES.jsonl"
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        kind = r.get("kind", "feature")
        weight = 1 + 2 * pct.get(r.get("cluster"), 0.5) if kind == "feature" else 1.5
        outcome = r.get("outcome") or "pending"
        rows.append({"key": r.get("key"), "cost": 0.0, "out_tokens": r.get("out_tokens"), "total_tokens": r.get("tokens"),
                     "started": r.get("started"), "cluster": r.get("cluster"),
                     "round": r.get("round"), "kind": kind, "model": r.get("model", "claude"), "weight": round(weight, 3),
                     "minutes": r.get("minutes") or 0.0, "outcome": outcome, "success": outcome in SUCCESS,
                     "finished": r.get("finished"), "review_minutes": None, "reviewer": "gpt-6-luna",
                     "code_review_minutes": None})
    return rows


def summarise(rows):
    def block(sel):
        decided = [r for r in sel if r["outcome"] not in ("pending", "unknown")]
        wins = [r for r in decided if r["success"]]
        w_all = sum(r["weight"] for r in decided)
        minutes = [r["minutes"] for r in decided if r["minutes"]]
        hours = sum(minutes) / 60
        return {
            "attempts": len(sel), "decided": len(decided), "successes": len(wins),
            "success_rate": round(len(wins) / len(decided), 3) if decided else None,
            "weighted_success_rate": round(sum(r["weight"] for r in wins) / w_all, 3) if w_all else None,
            "mean_difficulty": round(w_all / len(decided), 2) if decided else None,
            "median_minutes_per_attempt": round(statistics.median(minutes), 1) if minutes else None,
            "median_minutes_per_success": round(statistics.median([r["minutes"] for r in wins]), 1) if wins else None,
            "successes_per_session_hour": round(len(wins) / hours, 2) if hours else None,
            "no_result": sum(r["outcome"] == "no-result" for r in decided),
            "luna_fixed": sum(bool(r.get("luna_fixed")) for r in wins),
            "pending": sum(r["outcome"] == "pending" for r in sel),
            # owner 2026-10-16: first-pass consistency vs cost
            "first_pass_rate": (lambda f: round(sum(r["success"] for r in f) / len(f), 3) if f else None)(
                [r for r in decided if r.get("round") == 1]),
            "mean_cost_per_attempt": round(sum(r["cost"] for r in decided) / len(decided), 4) if decided else None,
            "cost_per_success": round(sum(r["cost"] for r in decided) / len(wins), 4) if wins else None,
            # owner 2026-10-19: output tokens per task (OpenCode exact incl. reasoning; Luna/Claude only log totals)
            "median_output_tokens_per_attempt": (lambda t: round(statistics.median(t)) if t else None)(
                [r["out_tokens"] for r in decided if r.get("out_tokens")]),
            "output_tokens_per_success": (lambda t: round(sum(t) / len(wins)) if t and wins else None)(
                [r["out_tokens"] for r in decided if r.get("out_tokens")]),
            # only rounds that recorded tokens (older hand-run Claude lane rounds did not): their tokens per their wins
            "total_tokens_per_success": (lambda t, w: round(sum(t) / w) if t and w else None)(
                [r["total_tokens"] for r in decided if r.get("total_tokens")],
                sum(1 for r in wins if r.get("total_tokens"))),
            "median_total_tokens_per_attempt": (lambda t: round(statistics.median(t)) if t else None)(
                [r["total_tokens"] for r in decided if r.get("total_tokens")]),
        }
    by_model = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)
    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "models": {}}
    for m, sel in sorted(by_model.items()):
        out["models"][m] = {"all": block(sel),
                            "bugs": block([r for r in sel if r["kind"] == "bug"]),
                            "features": block([r for r in sel if r["kind"] == "feature"]),
                            "ci": block([r for r in sel if r["kind"] == "ci"])}
    trial_path = ROOT / "BUG-TRIAL.json"
    if trial_path.exists():
        trial = load(trial_path) or {}
        tr = [r for r in rows if r["kind"] == "bug" and r["model"] in trial.get("models", [])
              and str(r.get("started") or "") >= trial.get("started", "")]
        out["bug_trial"] = {"started": trial.get("started"), "per_model": trial.get("per_model"),
                            "models": {m: block([r for r in tr if r["model"] == m]) for m in trial.get("models", [])}}
    eval_path = ROOT / "FEATURE-EVAL.json"   # owner 2026-10-19: bottom feature tier, Mimo V2.6 Flash vs GLM 5.3 Flash
    if eval_path.exists():
        ev = load(eval_path) or {}
        er = [r for r in rows if r["kind"] == "feature" and r["model"] in ev.get("models", [])
              and str(r.get("started") or "") >= ev.get("started", "")]
        out["feature_eval"] = {"started": ev.get("started"), "why": ev.get("why"),
                               "models": {m: block([r for r in er if r["model"] == m]) for m in ev.get("models", [])}}
    # owner 2026-10-19: same-file / root-cause / same-place batches vs single tasks
    out["batching"] = {kind: {"single": block([r for r in rows if r["kind"] == kind and r.get("batch_size", 1) == 1
                                               and str(r.get("started") or "") >= "2026-10-19T12"]),
                              "batched": block([r for r in rows if r["kind"] == kind and r.get("batch_size", 1) > 1])}
                       for kind in ("bug", "feature")}
    reviews = [r for r in rows if r["review_minutes"]]
    verdicts = defaultdict(int)
    for r in rows:
        if r["outcome"] not in ("pending", "unknown", "no-result"):
            verdicts[r["outcome"]] += 1
    out["reviewers"] = {"reviews": len(reviews),
                        "median_review_minutes": round(statistics.median([r["review_minutes"] for r in reviews]), 1) if reviews else None,
                        "verdicts": dict(verdicts),
                        "by_model": {m: sum(1 for r in reviews if r["reviewer"] == m) for m in {r["reviewer"] for r in reviews}}}
    return out


def active_models(kind=None):
    """Models still in use. Features: the FEATURE-TIERS.json tiers plus the Claude lanes. Bugs: the other OpenCode models
    (OPENCODE-FREE.json minus the feature tiers) plus Luna and Strata. No kind: either."""
    def norm(m):
        return m if "/" in m else f"opencode/{m}"
    try:
        ids = {norm(m) for m in json.loads((ROOT / "OPENCODE-FREE.json").read_text()).get("models", [])}
    except (OSError, ValueError):
        ids = set()
    try:
        tiers = {norm(m) for k, v in json.loads((ROOT / "FEATURE-TIERS.json").read_text()).items()
                 if not k.startswith("_") for m in v}
    except (OSError, ValueError):
        tiers = set()
    features = {ALIASES.get(m, m) for m in tiers - DEEPSEEK_IDS} | {"claude-sonnet-5-5", "claude-opus-5-5"}
    features |= {m + " (1017)" for m in tiers & DEEPSEEK_IDS}       # the 1017 row is the one in use, pre-1017 is retired
    bugs = {ALIASES.get(m, m) for m in ids - tiers - DEEPSEEK_IDS} | {"gpt-6-luna", "custom_provider:strata/qwen3.8-flash-next-coder"}
    bugs |= {m + " (1017)" for m in ids & DEEPSEEK_IDS}       # 2026-10-19: DeepSeek 1017 fixes bugs too
    bugs.add("claude-haiku-5-5 (bug lane)")
    # 2026-10-20: DeepSeek through DeepSeek Harness gets its own row beside the OpenCode-harness one
    features |= {m + " [DeepSeek Harness]" for m in features if "(1017)" in m}
    bugs |= {m + " [DeepSeek Harness]" for m in bugs if "(1017)" in m}
    features |= {m + " [DeepSeek Harness · OpenDesign]" for m in features if "(1017)" in m and "[" not in m}
    bugs |= {m + " [DeepSeek Harness · OpenDesign]" for m in bugs if "(1017)" in m and "[" not in m}
    bugs |= {m + " [MiniMax Code]" for m in bugs if m == SPACE_BUNNY}     # 2026-10-20: MiniMax M3.1 through mcode
    return features if kind == "features" else bugs if kind == "bugs" else features | bugs


def harness_label(run_dir):
    """A row suffix for a model run outside OpenCode (2026-10-20: DeepSeek in DeepSeek Harness, MiniMax in MCode)."""
    usage = [load(u) or {} for u in run_dir.glob("opencode-*-usage.json")]
    harnesses = {u.get("harness") for u in usage}
    if "dsh" in harnesses:   # 2026-10-22 A/B: DeepSeek through OpenDesign gets its own row beside OpenCode Go's
        return " [DeepSeek Harness · OpenDesign]" if any(u.get("provider") == "opendesign" for u in usage) else " [DeepSeek Harness]"
    return " [MiniMax Code]" if "mcode" in harnesses else ""


def plan_of(model):
    """The subscription a model's tokens come out of (PLANS.json names), or None for a free model."""
    m = str(model)
    if m.startswith("claude-") or "claude" in m and "opencode" not in m:
        return "Claude Max"
    if "luna" in m:
        return "ChatGPT Plus (Luna)"
    if m.startswith("opencode-go/") and "-free" not in m:
        return "OpenCode Go"
    return None


def plan_cost_per_token(plan):
    """The plan's own price for one token, from plans.py's measured tokens per full window (owner 2026-10-19: "estimated
    plan costing based on [token output]")."""
    for p in (load(ROOT / "PLANS.json") or {}).get("plans", []):
        if plan and p["plan"].startswith(plan) and p.get("tokens_per_window"):
            return p["price_per_window"] / p["tokens_per_window"]
    return None


def plan_cost(model, tokens):
    plan = plan_of(model)
    if plan is None:
        return 0.0 if tokens else None
    per = plan_cost_per_token(plan)
    return round(per * tokens, 4) if per and tokens else None


def rejections_section():
    """Owner 2026-10-20: rejections only for real failures, reasons transparent. REJECTIONS.jsonl (qwenfix.log_rejection)
    holds every rejection's checked reasons since the rule began; 'unfounded' = rejected twice with no evidenced failure,
    so the loop accepted it."""
    rows = []
    for line in (ROOT / "REJECTIONS.jsonl").read_text().splitlines() if (ROOT / "REJECTIONS.jsonl").exists() else []:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    if not rows:
        return ["\n## Why work was sent back\n", "No rejections recorded yet under the evidence rule (2026-10-20)."]
    cats = sorted({b["category"] for r in rows for b in r.get("blocking") or []})
    per = {}
    for r in rows:
        m = model_of(r)
        d = per.setdefault(m, {"n": 0, "unfounded": 0, "dropped": 0, **{c: 0 for c in cats}})
        d["n"] += 1
        d["unfounded"] += bool(r.get("unfounded"))
        d["dropped"] += len(r.get("dropped") or [])
        for b in r.get("blocking") or []:
            d[b["category"]] += 1
    out = ["\n## Why work was sent back (evidence rule, since 2026-10-20)\n",
           "Each rejection must name its failures with evidence; counts are failures per category. Unfounded = rejected twice "
           "without one, then accepted. Dropped = reasons the reviewer gave that were not evidenced failures (passed on as notes).\n",
           "| Fixer | Rejections | " + " | ".join(cats) + " | Unfounded | Dropped |",
           "|---|---|" + "---|" * len(cats) + "---|---|"]
    for m, d in sorted(per.items(), key=lambda x: -x[1]["n"]):
        out.append(f"| {m} | {d['n']} | " + " | ".join(str(d[c] or "-") for c in cats) + f" | {d['unfounded'] or '-'} | {d['dropped'] or '-'} |")
    return out


def markdown(s):
    """Owner 2026-10-19: bugs and features in separate sections, models still in use first, retired ones folded away."""
    def num(x):
        return "-" if x is None else f"{x:,}"

    def pctf(x):
        return "-" if x is None else f"{x * 100:.0f} %"

    head = ("| Model | Decided | Accepted | First pass | Difficulty-weighted | Mean difficulty | Min/attempt | Accepted/hour "
            "| Output tok/attempt | Output tok/accepted | $/accepted | Plan $/accepted |", "|---|---|---|---|---|---|---|---|---|---|---|---|")

    def lfixed(b):
        return f" ({b['luna_fixed']} Luna-fixed)" if b.get("luna_fixed") else ""

    def row(m, b):
        return (f"| {m} | {b['decided']} | {pctf(b['success_rate'])}{lfixed(b)} | {pctf(b['first_pass_rate'])} | "
                f"{pctf(b['weighted_success_rate'])} | {b['mean_difficulty'] or '-'} | {b['median_minutes_per_attempt'] or '-'} | "
                f"{b['successes_per_session_hour'] or '-'} | {num(b['median_output_tokens_per_attempt'] or b['median_total_tokens_per_attempt'])}"
                f"{'' if b['median_output_tokens_per_attempt'] or not b['median_total_tokens_per_attempt'] else ' (total)'} | "
                f"{num(b['output_tokens_per_success'])} | {b['cost_per_success'] if b['cost_per_success'] is not None else '-'} | "
                f"{(lambda c: '-' if c is None else f'{c:.3f}')(plan_cost(m, b.get('total_tokens_per_success')))} |")

    def table(kind):
        rows = [(m, k[kind]) for m, k in s["models"].items() if k[kind]["attempts"]]
        rows.sort(key=lambda r: (-(r[1]["weighted_success_rate"] or 0), -r[1]["decided"]))
        active = active_models(kind)
        cur = [r for r in rows if r[0] in active]
        old = [r for r in rows if r[0] not in active]
        out = ["**In use**\n", *head, *[row(m, b) for m, b in cur]]
        if old:
            out += ["", "<details><summary>Retired or no longer used (" + str(len(old)) + ")</summary>\n", *head,
                    *[row(m, b) for m, b in old], "\n</details>"]
        return out

    def batching(kind):
        b = (s.get("batching") or {}).get(kind)
        if not b:
            return []
        out = [f"\n### Batched vs single {kind}s (since 2026-10-19 12:00)\n",
               "| Mode | Decided | Accepted | First pass | Difficulty-weighted | Min/attempt (per task) | Output tok/accepted |",
               "|---|---|---|---|---|---|---|"]
        for mode, x in b.items():
            out.append(f"| {mode} | {x['decided']} | {pctf(x['success_rate'])} | {pctf(x['first_pass_rate'])} | "
                       f"{pctf(x['weighted_success_rate'])} | {x['median_minutes_per_attempt'] or '-'} | {num(x['output_tokens_per_success'])} |")
        return out

    def plans_section():
        pl = load(ROOT / "PLANS.json") or {}
        if not pl.get("plans"):
            return []
        out = [f"\n## Plans: tokens a subscription actually gives (measured over the last {pl.get('span_days')} days)\n",
               "| Plan | 1 % of the plan | Output per 1 % | Full week | Output per week | Week at API prices | Plan price per week | Value | Meter rise measured |",
               "|---|---|---|---|---|---|---|---|---|"]

        def mt(n):
            return "-" if n is None else f"{n / 1e6:.1f} M" if n >= 1e6 else f"{n / 1e3:.0f} k"
        for p in pl["plans"]:
            if p.get("status") != "ok":
                out.append(f"| {p['plan']} | {p.get('status')} | | | | | ${p['price_per_window']} | | {p['meter_rise_points']} pts |")
                continue
            out.append(f"| {p['plan']} | {mt(p['tokens_per_percent'])} | {mt(p['output_per_percent'])} | {mt(p['tokens_per_window'])} | "
                       f"{mt(p['output_per_window'])} | ${p['api_value_per_window']} | ${p['price_per_window']} | "
                       f"x{p['value_multiple']} | {p['meter_rise_points']} pts |")
        out.append("\nTokens include cache reads, which are most of them (they are cheap at API prices). 'Plan $/accepted' in the "
                   "tables above = the plan's price per week divided by its measured tokens per week, times the tokens an "
                   "accepted task used. Free models cost 0; the OpenCode Go price follows go_plan in PLANS-CONFIG.json.")
        return out

    lines = [f"# Model benchmark\n", f"Generated {s['generated']} by `benchmark.py`. Sorted by difficulty-weighted acceptance. "
             "Notes on weights, costs and tokens are at the bottom.\n", "## Bugs\n", *table("bugs"), *batching("bug")]
    t = s.get("bug_trial")
    if t:
        lines += [f"\n<details><summary>Ended: bug-fix trial of {t['started'][:10]}</summary>\n",
                  "| Model | Decided | First pass | Accepted | Difficulty-weighted | $/attempt | $/accepted | Min/attempt |",
                  "|---|---|---|---|---|---|---|---|"]
        for m, b in sorted(t["models"].items(), key=lambda kv: -(kv[1]["first_pass_rate"] or -1)):
            lines.append(f"| {m} | {b['decided']} | {pctf(b['first_pass_rate'])} | {pctf(b['success_rate'])} | "
                         f"{pctf(b['weighted_success_rate'])} | {b['mean_cost_per_attempt'] if b['mean_cost_per_attempt'] is not None else '-'} | "
                         f"{b['cost_per_success'] if b['cost_per_success'] is not None else '-'} | {b['median_minutes_per_attempt'] or '-'} |")
        lines.append("\n</details>")
    lines += ["\n## Features\n", *table("features"), *batching("feature")]
    e = s.get("feature_eval")
    if e:
        lines += [f"\n<details><summary>Bottom-tier eval since {e['started'][:16]} ({e.get('why', '')[:90]})</summary>\n",
                  "| Model | Decided | First pass | Accepted | Difficulty-weighted | Min/attempt | Output tok/accepted |",
                  "|---|---|---|---|---|---|---|"]
        for m, b in e["models"].items():
            lines.append(f"| {m} | {b['decided']} | {pctf(b['first_pass_rate'])} | {pctf(b['success_rate'])} | "
                         f"{pctf(b['weighted_success_rate'])} | {b['median_minutes_per_attempt'] or '-'} | {num(b['output_tokens_per_success'])} |")
        lines.append("\n</details>")
    ci = [(m, k["ci"]) for m, k in s["models"].items() if k["ci"]["attempts"]]
    if ci:
        lines += ["\n## CI lanes\n", *head, *[row(m, b) for m, b in ci]]
    lines += plans_section()
    lines += rejections_section()
    r = s["reviewers"]
    lines += [f"\n## Reviews\n\n{r['reviews']} reviews (median {r['median_review_minutes']} min); by reviewer {r['by_model']}; "
              f"verdicts {r['verdicts']}.",
              "\n## Notes\n",
              "- Difficulty weight: 1 + 2 x the task's Clef difficulty percentile (bugs: Clef's pairwise ranking when it exists, "
              "else its easy/hard/self-contained answers; features: Clef's pairwise sort of the 0.5.0 queue); a bug Clef never "
              "scored uses tier weights 1 / 1.5 / 2 / 3. Mean difficulty is on that 1-3 scale.",
              "- $/accepted is OpenCode-metered only; Claude, Luna and Strata run on subscriptions or locally and show 0.",
              "- Output tokens are exact for OpenCode runs (incl. reasoning) and loop-run Claude lanes; Luna and hand-run Claude "
              "lanes log only a session total, marked (total).",
              "- Batches: each handled task is a row of its own, with the job's time and tokens split between its tasks."]
    return "\n".join(lines) + "\n"


def update():
    try:                    # 2026-10-19: refresh the plan measurements first (incremental, a few seconds)
        import plans
        plans.main()
    except Exception:
        pass
    s = summarise(attempts())
    try:
        import workflow_report
        delivery = workflow_report.refresh()
        s["delivery"] = {k: delivery[k] for k in ("generated", "base", "delivered", "totals", "limits")}
    except Exception as exc:
        s["delivery_error"] = str(exc)
    save_token_cache()
    (ROOT / "BENCHMARK.json").write_text(json.dumps(s, indent=1) + "\n")
    (ROOT / "BENCHMARK.md").write_text(markdown(s))
    with open(ROOT / "benchmark-history.jsonl", "a") as f:
        f.write(json.dumps({"at": s["generated"], "models": {m: k["all"] for m, k in s["models"].items()}}) + "\n")
    return s


if __name__ == "__main__":
    update()
    print((ROOT / "BENCHMARK.md").read_text())
