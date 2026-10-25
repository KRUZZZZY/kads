#!/usr/bin/env python3
"""Plan economics (owner 2026-10-19: "track token output in the benchmarks, and estimated plan costing based on that,
just so that i could potentially see how many tokens the plans actually give me").

For each subscription it measures, over the last SPAN_DAYS, the tokens actually used and how far the plan's own meter
moved, and from that the tokens one percent of the plan buys, a full window's worth, and what that full window would
cost at API prices compared with what the plan costs for the same window.

  Claude (Max, $200/month)   tokens: every Claude Code transcript (~/.claude/projects, this orchestrator and the loop's
                             Claude lanes alike, deduplicated by message id); meter: the 7-day percent the usage
                             monitor records in usage-history.jsonl. Each account change starts a new meter period.
  Luna (ChatGPT Plus, $20)   tokens and meter from the same Codex session logs (~/.codex/sessions): every token_count
                             event carries its last call's tokens and the 5-hour / weekly percent.
  OpenCode Go ($10 or $40)   tokens and cost: OpenCode's message store (opencode.db, provider opencode-go); meter: the
                             weekly percent in usage-history.jsonl.

A meter that rose less than MIN_POINTS in the span is reported as too little data. Writes PLANS.json; benchmark.py adds
the section and a plan-cost-per-accepted column from it. Parsing is incremental (plans-cache.json keeps each log's
offset and the last 10 days of events). Run: `plans.py` (prints the table); the usage monitor runs it every 30 min.
"""
import datetime
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
CACHE = ROOT / "plans-cache.json"
CONFIG = ROOT / "PLANS-CONFIG.json"
OUT = ROOT / "PLANS.json"
SPAN_DAYS = 7
KEEP_DAYS = 10
MIN_POINTS = 3
DEFAULT_CONFIG = {
    "_note": "Monthly subscription prices (USD). go_plan: 'go' ($10) or 'go_plus' ($40) - set the one you pay for.",
    "claude_monthly": 200, "luna_monthly": 20, "go_plan": "go", "go_monthly": {"go": 10, "go_plus": 40},
}
WEEKS_PER_MONTH = 52 / 12


def load(p, d=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return d


def ts(s):
    return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


# ------------------------------------------------------------------ token events (incremental)
def read_new(path, cache, parse):
    """Parse lines appended to path since the cached offset; returns new events."""
    key = str(path)
    st = path.stat()
    off = cache["offsets"].get(key, 0)
    if off > st.st_size:
        off = 0                                    # truncated or replaced
    if off == st.st_size:
        return []
    out = []
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read()
    end = data.rfind(b"\n") + 1                    # only whole lines; a half-written last line waits for next time
    for line in data[:end].splitlines():
        ev = parse(line)
        if ev:
            out.append(ev)
    cache["offsets"][key] = off + end
    return out


def parse_claude(line):
    if b'"usage"' not in line or b'"assistant"' not in line:
        return None
    try:
        r = json.loads(line)
        m = r.get("message") or {}
        u = m.get("usage") or {}
        if r.get("type") != "assistant" or not u:
            return None
        return {"id": m.get("id") or r.get("requestId") or r.get("uuid"), "t": ts(r["timestamp"]), "model": m.get("model"),
                "in": u.get("input_tokens") or 0, "out": u.get("output_tokens") or 0,
                "cr": u.get("cache_read_input_tokens") or 0, "cw": u.get("cache_creation_input_tokens") or 0}
    except (ValueError, KeyError, TypeError):
        return None


def parse_codex(line):
    if b'"token_count"' not in line:
        return None
    try:
        r = json.loads(line)
        p = r.get("payload") or {}
        info = p.get("info") or {}
        last = info.get("last_token_usage") or {}
        rl = p.get("rate_limits") or {}
        sec, pri = rl.get("secondary") or {}, rl.get("primary") or {}
        if not last and not sec:
            return None
        cached = last.get("cached_input_tokens") or 0
        total = (info.get("total_token_usage") or {}).get("total_tokens")
        return {"t": ts(r["timestamp"]), "in": (last.get("input_tokens") or 0) - cached, "cr": cached, "cw": 0, "tot": total,
                "out": last.get("output_tokens") or 0,
                "week_pct": sec.get("used_percent"), "week_reset": sec.get("resets_at") or sec.get("reset_at"),
                "h5_pct": pri.get("used_percent")}
    except (ValueError, KeyError, TypeError):
        return None


def refresh_events():
    cache = load(CACHE, None) or {"offsets": {}, "claude": {}, "codex": []}
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in (HOME / ".claude/projects").rglob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            continue
        for ev in read_new(f, cache, parse_claude):
            if ev["t"] >= cutoff:
                cache["claude"][ev["id"]] = ev       # streamed duplicates of one message keep its final usage
    for f in (HOME / ".codex/sessions").rglob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            continue
        # 2026-10-24: Codex repeats a token_count event (same last_token_usage) when only the rate limits change -
        # ~6 % of Luna's tokens were counted twice. A repeat leaves the session's running total unchanged, so keep its
        # meter reading but zero its tokens.
        last_tot = cache.setdefault("codex_tot", {})
        for e in read_new(f, cache, parse_codex):
            tot = e.pop("tot", None)
            if tot is not None and tot == last_tot.get(str(f)):
                e.update({"in": 0, "out": 0, "cr": 0})
            elif tot is not None:
                last_tot[str(f)] = tot
            if e["t"] >= cutoff:
                cache["codex"].append(e)
    cache["claude"] = {k: v for k, v in cache["claude"].items() if v["t"] >= cutoff}
    cache["codex"] = [e for e in cache["codex"] if e["t"] >= cutoff]
    cache["offsets"] = {k: v for k, v in cache["offsets"].items() if Path(k).exists()}
    cache["codex_tot"] = {k: v for k, v in cache.get("codex_tot", {}).items() if k in cache["offsets"]}
    CACHE.write_text(json.dumps(cache))
    return list(cache["claude"].values()), sorted(cache["codex"], key=lambda e: e["t"])


def go_events(since):
    db = HOME / ".local/share/opencode/opencode.db"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        rows = con.execute("select time_created, data from message where time_created >= ? and data like '%\"providerID\":\"opencode-go\"%'",
                           (int(since * 1000),)).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    out = []
    for t, data in rows:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        if d.get("role") != "assistant":
            continue
        if str(d.get("modelID", "")).endswith("-free"):
            continue   # 2026-10-24: zero-cost models on the Go route (beam-free, minimax-free) do not move the Go meter
        tk = d.get("tokens") or {}
        c = tk.get("cache") or {}
        out.append({"t": t / 1000, "model": d.get("modelID"), "in": tk.get("input") or 0,
                    "out": (tk.get("output") or 0) + (tk.get("reasoning") or 0), "cr": c.get("read") or 0,
                    "cw": c.get("write") or 0, "cost": d.get("cost") or 0})
    # 2026-10-20: DeepSeek runs through DeepSeek Harness on the same Go key; opencode_free.run_model_dsh logs each step
    try:
        for line in (ROOT / "DSH-USAGE.jsonl").read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            # 2026-10-22: only OpenCode Go traffic counts against the Go plan (OpenDesign is a separate subscription)
            if e.get("t", 0) >= since and e.get("provider", "opencode-go") == "opencode-go":
                out.append(e)
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ prices
def prices():
    cached = load(ROOT / "api-prices.json", None)
    if cached and time.time() - cached.get("at", 0) < 86400:
        return cached["models"]
    try:
        req = urllib.request.Request("https://models.dev/api.json", headers={"User-Agent": "zene-plans/1.0 (curl compatible)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        models = {f"{p}/{k}": v.get("cost") for p in ("anthropic", "openai") for k, v in (d.get(p, {}).get("models") or {}).items()
                  if v.get("cost")}
        (ROOT / "api-prices.json").write_text(json.dumps({"at": time.time(), "models": models}))
        return models
    except (OSError, ValueError):
        return (cached or {}).get("models", {})


def api_cost(ev, price):
    if not price:
        return 0.0
    return (ev["in"] * price.get("input", 0) + ev["out"] * price.get("output", 0) + ev["cr"] * price.get("cache_read", 0)
            + ev["cw"] * price.get("cache_write", 0)) / 1e6


# ------------------------------------------------------------------ meters
def period_key(reset):
    """A meter's reset time, rounded to 10 minutes: the API reports it with sub-second jitter (02:59:59.77 / 03:00:00.44)."""
    if reset in (None, ""):
        return None
    try:
        t = float(reset) if isinstance(reset, (int, float)) or str(reset).replace(".", "", 1).isdigit() else ts(reset)
    except ValueError:
        return str(reset)
    return round(t / 600)


def meter_periods(points):
    """[(t, pct, reset)] -> list of (t0, t1, rise) per meter period (a reset or account change starts a new one)."""
    periods, cur = [], None
    for t, p, per in points:
        if p is None:
            continue
        per = period_key(per)
        if cur is None or per != cur["per"] or p < cur["p1"] - 0.5:
            if cur:
                periods.append(cur)
            cur = {"per": per, "t0": t, "p0": p, "t1": t, "p1": p}
        else:
            cur["t1"], cur["p1"] = t, p
    if cur:
        periods.append(cur)
    return [(c["t0"], c["t1"], c["p1"] - c["p0"]) for c in periods]


def estimate(name, events, periods, window_label, windows_per_month, monthly_price, cost_of):
    tokens = {"in": 0, "out": 0, "cr": 0, "cw": 0}
    api = 0.0
    rise = 0.0
    for t0, t1, r in periods:
        if r <= 0:
            continue
        rise += r
        for e in events:
            if t0 < e["t"] <= t1:
                for k in tokens:
                    tokens[k] += e[k]
                api += cost_of(e)
    total = sum(tokens.values())
    res = {"plan": name, "window": window_label, "meter_rise_points": round(rise, 1), "tokens_measured": total,
           "output_measured": tokens["out"], "api_value_measured": round(api, 2),
           "price_per_window": round(monthly_price / windows_per_month, 2), "monthly_price": monthly_price}
    if rise < MIN_POINTS or total == 0:
        res["status"] = f"too little data (meter rose {rise:.0f} points in {SPAN_DAYS} days)"
        return res
    per_pt = total / rise
    res.update(status="ok", tokens_per_percent=round(per_pt), output_per_percent=round(tokens["out"] / rise),
               tokens_per_window=round(per_pt * 100), output_per_window=round(tokens["out"] / rise * 100),
               api_value_per_window=round(api / rise * 100, 2),
               value_multiple=round(api / rise * 100 / (monthly_price / windows_per_month), 1) if monthly_price else None,
               mix={k: round(v / total, 3) for k, v in tokens.items()})
    return res


def main():
    cfg = {**DEFAULT_CONFIG, **(load(CONFIG, {}) or {})}
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, indent=1) + "\n")
    since = time.time() - SPAN_DAYS * 86400
    claude, codex = refresh_events()
    history = [h for h in (json.loads(l) for l in (ROOT / "usage-history.jsonl").read_text().splitlines() if l.strip())
               if ts(h["at"]) >= since] if (ROOT / "usage-history.jsonl").exists() else []
    pr = prices()
    plans = []
    # Claude: 7-day meter, period = its reset time (an account change moves it)
    pts = [(ts(h["at"]), ((h.get("claude") or {}).get("seven_day") or {}).get("percent"),
            ((h.get("claude") or {}).get("seven_day") or {}).get("resets_at")) for h in history]
    plans.append(estimate("Claude Max", [e for e in claude if e["t"] >= since], meter_periods(pts), "week",
                          WEEKS_PER_MONTH, cfg["claude_monthly"],
                          lambda e: api_cost(e, pr.get(f"anthropic/{e.get('model')}"))))
    # Luna: weekly meter from the Codex events themselves
    cx = [e for e in codex if e["t"] >= since]
    pts = [(e["t"], e.get("week_pct"), e.get("week_reset")) for e in cx if e.get("week_pct") is not None]
    plans.append(estimate("ChatGPT Plus (Luna)", cx, meter_periods(pts), "week", WEEKS_PER_MONTH, cfg["luna_monthly"],
                          lambda e: api_cost(e, pr.get("openai/gpt-6-luna"))))
    # OpenCode Go: weekly meter (50 % of the monthly allowance)
    go = go_events(since)
    pts = [(ts(h["at"]), ((h.get("opencode_go") or {}).get("weekly") or {}).get("percent"),
            ((h.get("opencode_go") or {}).get("weekly") or {}).get("resetsAt")) for h in history]
    go_month = cfg["go_monthly"].get(cfg["go_plan"], 10)
    plans.append(estimate(f"OpenCode Go ({cfg['go_plan']})", go, meter_periods(pts), "week", WEEKS_PER_MONTH, go_month,
                          lambda e: e.get("cost") or 0))
    out = {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "span_days": SPAN_DAYS, "plans": plans}
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    return out


def fmt(n):
    return "-" if n is None else f"{n / 1e6:.1f} M" if n >= 1e6 else f"{n / 1e3:.0f} k" if n >= 1e3 else str(n)


if __name__ == "__main__":
    o = main()
    for p in o["plans"]:
        if p["status"] != "ok":
            print(f"{p['plan']:24} {p['status']}")
            continue
        print(f"{p['plan']:24} 1% = {fmt(p['tokens_per_percent'])} tokens ({fmt(p['output_per_percent'])} output); "
              f"full {p['window']} = {fmt(p['tokens_per_window'])} ({fmt(p['output_per_window'])} output), "
              f"${p['api_value_per_window']} at API prices vs ${p['price_per_window']} for the plan "
              f"(x{p['value_multiple']}); mix {p['mix']}")
    sys.exit(0)
