#!/usr/bin/env python3
"""Usage monitor (owner 2026-10-18: "check on opencode usage every once in a while").

Every run (systemd user timer, 10 min):
- OpenCode Go plan: GET https://opencode.ai/zen/go/v1/usage with the opencode CLI's own key
  (~/.local/share/opencode/auth.json; never printed or copied) -> rolling / weekly / monthly percent + status.
  While a window is not "ok", every priced Go model in OPENCODE-FREE.json rests until that window resets
  (opencode_free.rest), so the fix loop stops spending attempts on a plan that refuses them. Zero-cost models
  (ids ending in -free) keep running: they do not count against the plan.
- Claude: the OAuth usage endpoint (same call /usage makes) -> five-hour and weekly percent. Report only. The account
  holds a banked "Full reset" (claude.ai Settings > Usage > Limit resets, "Reset for free", expires 2026-10-22) that this
  API does not expose (correction 2026-10-19, owner's screenshot); only the owner can press it, so 95/100 % tell them to.
- Hold (owner 2026-10-19: "hold off on using it until they rollout deepseek flash 1017"): while OPENCODE-GO-HOLD exists,
  every priced Go model rests (renewed each run; free -free ids keep running). Each run checks models.dev for DeepSeek
  V4.1 Flash 1017 on the opencode-go provider. Served in place under the current id -> the hold is lifted and the owner
  notified; served under a new id -> the owner is notified and the hold stays until the id is wired in (PAID_PLAN,
  OPENCODE-FREE.json, BUG-PREFER.json).
Writes USAGE.json, appends usage-history.jsonl, and sends notify-me when a status changes.
"""
import datetime
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import opencode_free  # noqa: E402

STATUS = ROOT / "USAGE.json"
HISTORY = ROOT / "usage-history.jsonl"
CLAUDE_STEPS = (80, 90, 95, 100)
LUNA_SWEEP_STOP = 50          # percent of the Codex weekly window
CLAUDE_FULL_RESET = " Banked Full reset (expires Oct 22): press 'Reset for free' in Claude Settings > Usage."
HOLD = ROOT / "OPENCODE-GO-HOLD"
DEEPSEEK_GO_ID = "deepseek-v4.1-flash"
DEEPSEEK_1017_DATE = "2026-10-17"


def get_json(url, headers):
    req = urllib.request.Request(url, headers={**headers, "User-Agent": "zene-usage-monitor/1.0"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def opencode_usage():
    key = json.loads((Path.home() / ".local/share/opencode/auth.json").read_text())["opencode"]["key"]
    return get_json("https://opencode.ai/zen/go/v1/usage", {"Authorization": "Bearer " + key})["usage"]


def claude_usage():
    tok = json.loads((Path.home() / ".claude/.credentials.json").read_text())["claudeAiOauth"]["accessToken"]
    d = get_json("https://api.anthropic.com/api/oauth/usage",
                 {"Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20"})
    return {k: {"percent": d[k]["utilization"], "resets_at": d[k]["resets_at"]} for k in ("five_hour", "seven_day")}


def seconds_until(iso):
    t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return max(0, (t - datetime.datetime.now(datetime.timezone.utc)).total_seconds())


def priced_go_models():
    cfg = json.loads((ROOT / "OPENCODE-FREE.json").read_text())
    return [m for m in cfg.get("models", []) if m.startswith("opencode-go/") and not m.endswith("-free")]


def deepseek_1017_on_go():
    """(id, how) of a DeepSeek 1017 entry on models.dev's opencode-go provider, else None."""
    models = (get_json("https://models.dev/api.json", {}).get("opencode-go") or {}).get("models") or {}
    for mid, m in models.items():
        if "deepseek" not in mid or "flash" not in mid:
            continue
        dated = max(m.get("release_date") or "", m.get("last_updated") or "") >= DEEPSEEK_1017_DATE
        if dated or "1017" in mid or "1017" in (m.get("name") or ""):
            return mid, ("in place" if mid == DEEPSEEK_GO_ID else "new id")
    return None


def notify(text):
    try:
        subprocess.run(["notify-me", text], timeout=60, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def main():
    prev = {}
    try:
        prev = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        pass
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        oc = opencode_usage()
        out["opencode_go"] = oc
        limited = {w: v for w, v in oc.items() if v.get("status") != "ok"}
        out["opencode_rested"] = []
        if limited:
            until = max(seconds_until(v["resetsAt"]) for v in limited.values()) + 60
            for m in priced_go_models():
                # 2026-10-22: a model DeepSeek Harness can also serve from another provider (OpenDesign) keeps running there;
                # only its OpenCode Go provider rests
                others = [p for p in opencode_free.dsh_provider_weights() if p != "opencode-go"] if opencode_free.uses_dsh(m) else []
                if others:
                    opencode_free.dsh_rest_provider_until("opencode-go", time.time() + until)
                    out["opencode_rested"].append(f"{m} (opencode-go provider only; {', '.join(others)} carries on)")
                    continue
                opencode_free.rest(m, until)
                out["opencode_rested"].append(m)
            out["opencode_rested_until"] = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + until))
        was = sorted(w for w, v in (prev.get("opencode_go") or {}).items() if v.get("status") != "ok")
        now_limited = sorted(limited)
        if was != now_limited:
            if now_limited:
                notify(f"OpenCode Go limited ({', '.join(f'{w} {oc[w]['percent']}%' for w in now_limited)}); "
                       f"paid Go models rest until {out['opencode_rested_until']}, free models carry on.")
            else:
                notify("OpenCode Go plan is back under its limits; paid Go models resume.")
    except Exception as e:      # the monitor never stops the loop
        out["opencode_error"] = repr(e)[:200]
    if HOLD.exists():
        try:
            found = deepseek_1017_on_go()
            out["deepseek_1017_on_go"] = found
            if found and not prev.get("deepseek_1017_on_go"):
                # owner 2026-10-19 ~12:40: all paid OpenCode is off ("stop all the opencode stuff bar the free stuff"),
                # so the release is only reported; the owner decides whether DeepSeek comes back
                notify(f"DeepSeek V4.1 Flash 1017 is live on OpenCode Go ({found[0]}). Paid OpenCode is off by your instruction; "
                       f"say if DeepSeek should be added back for bugs (the bug-queue hold is still on).")

        except Exception as e:
            out["deepseek_check_error"] = repr(e)[:200]
    if HOLD.exists():
        # owner 2026-10-19: the bottom-tier feature eval (FEATURE-EVAL.json, hold_exempt) runs through the hold
        try:
            ev = json.loads((ROOT / "FEATURE-EVAL.json").read_text())
            exempt = set(ev.get("models", [])) if ev.get("hold_exempt") else set()
        except (OSError, ValueError):
            exempt = set()
        out["opencode_hold_exempt"] = sorted(exempt)
        for m in priced_go_models():
            if m not in exempt:
                opencode_free.rest(m, 1800)
        out["opencode_hold"] = HOLD.read_text().strip() or True
    try:
        cl = claude_usage()
        out["claude"] = cl
        step = max([s for s in CLAUDE_STEPS if cl["seven_day"]["percent"] >= s], default=0)
        prev_step = (prev.get("claude_step") or 0)
        out["claude_step"] = step
        if step > prev_step:
            notify(f"Claude weekly usage at {cl['seven_day']['percent']:.0f}% (resets {cl['seven_day']['resets_at'][:16]} UTC)."
                   + (CLAUDE_FULL_RESET if step >= 95 else "")
                   + (" Claude lanes stop until the reset; the free/Luna loops continue." if step >= 100 else ""))
    except Exception as e:
        out["claude_error"] = repr(e)[:200]
    # owner 2026-10-19: "once we reach 50% of luna usage shut down the bug sweep" (the Luna patrol; reviews and
    # scoping keep running). Codex weekly usage comes from the banked-reset watcher's state (refreshed each minute).
    try:
        cx = json.loads((Path.home() / ".local/share/codex-banked-reset/state.json").read_text()).get("last_usage") or {}
        used = 100 - cx.get("weekly_remaining_percent", 100)
        out["luna_weekly_used"] = used
        stop = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program/research/reviews/luna-patrol/STOP"
        if used >= LUNA_SWEEP_STOP and not stop.exists() and not prev.get("luna_sweep_stopped"):
            stop.write_text(f"usage_monitor {out['at']}: Luna weekly usage {used}% >= {LUNA_SWEEP_STOP}% (owner 2026-10-19)\n")
            out["luna_sweep_stopped"] = True
            notify(f"Luna weekly usage reached {used}%: the Luna bug sweep is stopping (finishes its slices in flight). Reviews and scoping continue.")
        elif prev.get("luna_sweep_stopped") and used >= LUNA_SWEEP_STOP:
            out["luna_sweep_stopped"] = True
        # owner 2026-10-25: pace every Luna use so the week lasts to the reset (luna_pace.py)
        import luna_pace
        pace = luna_pace.step(used, cx.get("weekly_resets_at"), usage=out)
        if pace:
            out["luna_pace"] = {k: pace[k] for k in ("level", "why", "budget_pct_h", "burn_pct_h", "fix_factor", "scope")}
    except Exception as e:
        out["luna_error"] = repr(e)[:200]
    # 2026-10-19: place the bugs of each newly triaged patrol pass on Clef's pairwise difficulty curve
    try:
        prog = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
        passes = len((prog / "research/reviews/luna-new-triage/TRIAGED-PASSES").read_text().splitlines())
        out["triaged_passes"] = passes
        ranked = prog / "research/reviews/bug-difficulty/ranked.jsonl"
        running = subprocess.run(["pgrep", "-f", "bug_difficulty_sort.py|root_causes.py"], capture_output=True).returncode == 0
        if ranked.exists() and passes > (prev.get("triaged_passes") or passes) and not running:
            py = Path.home() / ".local/share/zene-bunny-fix/clef-venv/bin/python"
            # root-cause groups first, then difficulty placement of the resulting units (owner 2026-10-19); both journal, so reruns judge only new pairs
            subprocess.Popen(["bash", "-c", f"'{py}' tools/luna-patrol/root_causes.py confirm; '{py}' tools/luna-patrol/bug_difficulty_sort.py"],
                             cwd=prog, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            out["bug_sort_started"] = True
    except Exception as e:
        out["bug_sort_error"] = repr(e)[:200]
    # owner 2026-10-19 ("nothing should go into human review"): bugs deferred at the top of the ladder come back when
    # the fix base moves to a new train (their ladder restarts at level 1 there)
    try:
        base = (ROOT / "FIX-BASE").read_text().strip()
        revived = 0
        for f in (ROOT / "state").glob("*.json"):
            try:
                r = json.loads(f.read_text())
            except ValueError:
                continue
            if r.get("outcome") == "deferred" and (r.get("escalation") or {}).get("base") != base:
                f.unlink()
                revived += 1
        if revived:
            out["deferred_revived"] = revived
    except Exception as e:
        out["deferred_error"] = repr(e)[:200]
    STATUS.write_text(json.dumps(out, indent=1) + "\n")
    with open(HISTORY, "a") as f:
        f.write(json.dumps(out) + "\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
