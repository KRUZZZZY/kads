#!/usr/bin/env python3
"""Train doctor (owner 2026-10-21): "if a train fails repeatedly can it go down and spawn the best agent available at
the time of its running, so like first it tries Opus 5.5 on medium (owner update 2026-10-24), then muse spark contributor on high, then sol 6.1
medium, then like a random free model, then finally strata".

merge_train.fail() calls note_failure(). After STREAK unpromoted trains in a row with the same unpinned failure
signature, a doctor session starts (as its own transient systemd unit, so the train exits):

  - it DIAGNOSES ONLY, from a self-contained brief (error excerpt, the train's merges, and the diffs of the merges that
    touch the files the error names) plus a code-only checkout of the failed train branch (zene-train-doctor). It never
    builds, never touches zene-cachyos, and its only output is a verdict: which merged branch(es) to take off the train.
  - the verdict is checked (each branch must be one the failed train merged; at most MAX_EXCLUDE), applied with the
    train's own port_job (exclusion + a port job, so a fixer re-applies the fix on the newest line), and the train is
    started again. Promotion still needs the full bar and gates - nothing here relaxes them.
  - if the next train fails the same way, the next doctor starts one rung down the ladder. Each rung is tried only when
    its model is available at that moment (usage limit, a resting model, Strata not running = skipped).

State: merge/DOCTOR.json. Log lines go to the merge-train log with a "doctor:" prefix.
  train_doctor.py status          what the doctor knows
  train_doctor.py run             one doctor session now (normally started by note_failure)
"""
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import merge_train as mt          # noqa: E402  (log, notify, git, port_job, M, PROG)
import opencode_free              # noqa: E402

STATE = mt.M / "DOCTOR.json"
STREAK = 2                        # unpromoted trains with the same signature before a doctor starts
MAX_EXCLUDE = 3
SESSION_TIMEOUT_S = 75 * 60
DOCTOR_WT = mt.PROG / "zene-train-doctor"
CLONE = mt.PROG / "zene-studio"
HAIKU_EXEC = mt.PROG / "tools/haiku/haiku-exec"


def git(*a):
    """Read-only git in the main clone (the doctor never runs anything in zene-cachyos)."""
    return mt.git(*a, cwd=CLONE)


FREE_MODELS = ["mimo-v2.6-flash-free", "opencode-go/minimax-m3.1-flash-free", "opencode-go/beam-free"]
LADDER = [
    {"name": "opus", "label": "Claude Opus 5.5 (medium)"},
    {"name": "muse", "label": "Muse Spark 1.3 contributor (high, OpenCode Go)"},
    {"name": "sol", "label": "GPT 6.1 Sol (medium, codex)"},
    {"name": "free", "label": "a free OpenCode model"},
    {"name": "strata", "label": "local Qwen on Strata"},
]


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save(st):
    STATE.write_text(json.dumps(st, indent=1) + "\n")


def log(msg):
    mt.log(f"doctor: {msg}")


def signature(why, log_text):
    """What failed, without paths, numbers or hashes: the same breakage on two trains gives the same signature."""
    lines = [l for l in log_text.splitlines() if ERROR_RE.search(l)][-6:]
    text = why.split(" (see ")[0] + "\n" + "\n".join(lines)
    text = re.sub(r"/[\w./-]+/", "", text)
    text = re.sub(r"\b[0-9a-f]{7,40}\b|\d+", "N", text)
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def note_failure(new, base, why):
    """Called by merge_train.fail(). Counts the streak and starts a doctor when it is due. Never raises."""
    try:
        if "merge conflict" in why or "postponed" in why:
            return
        m = re.search(r"see (\S+?\.log)", why)
        log_path = Path(m.group(1)) if m else None
        text = log_path.read_text(errors="replace") if log_path and log_path.exists() else ""
        sig = signature(why, text)
        st = load()
        if st.get("signature") != sig:
            st = {"signature": sig, "streak": 0, "rung": 0, "history": []}
        st.update(streak=st.get("streak", 0) + 1, train=new, base=base, why=why, log=str(log_path or ""),
                  at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        save(st)
        if st["streak"] < STREAK or st.get("running"):
            return
        if st.get("rung", 0) >= len(LADDER):
            if not st.get("exhausted_told"):
                mt.notify(f"Merge train: the doctor ladder is exhausted for this failure ({why[:160]}). It needs you.")
                st["exhausted_told"] = True
                save(st)
            return
        subprocess.run(["systemd-run", "--user", f"--unit=zene-train-doctor-{int(time.time())}",
                        "--description=Zene train doctor (diagnoses a repeatedly failing merge train)",
                        sys.executable, str(Path(__file__).resolve()), "run"], capture_output=True)
        log(f"{new} failed {st['streak']} times the same way ({sig}) - doctor started")
    except Exception as e:          # the train's own failure handling must never break on this
        mt.log(f"doctor: could not start ({e!r})")


def note_promoted():
    """Called by merge_train.promote(): a promoted train ends any streak."""
    try:
        if STATE.exists():
            st = load()
            st.update(streak=0, rung=0, running=False, resolved_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            save(st)
    except Exception:
        pass


# ------------------------------------------------------------------ availability
def claude_ok():
    import qwenfix
    return not qwenfix.paused_until_in(qwenfix.CLAUDE_LIMIT)


def luna_ok():                    # Sol runs on the same OpenAI plan as Luna: it waits while Luna is paused
    import qwenfix
    return not qwenfix.luna_paused_until()


def go_ok():
    return not (ROOT / "OPENCODE-GO-HOLD").exists()


def strata_ok():
    import qwenfix
    return qwenfix.strata_health() is not None


def available(rung):
    n = rung["name"]
    if n == "opus":
        return claude_ok()
    if n == "muse":
        return go_ok() and not opencode_free.cooling("opencode-go/muse-spark-1.3-contributor")
    if n == "sol":
        return luna_ok()
    if n == "free":
        return any(not opencode_free.cooling(m) for m in FREE_MODELS)
    if n == "strata":
        return strata_ok()
    return False


# ------------------------------------------------------------------ the brief
def merges(base, new):
    out = git("log", "--first-parent", "--format=%H %s", f"{base}..{new}")
    rows = []
    for line in out.splitlines():
        h, _, subj = line.partition(" ")
        b = re.match(r"Merge (\S+) \(accepted", subj)
        if b:
            rows.append({"merge": h, "branch": b.group(1)})
    return rows


def named_files(text, branch=None):
    """Source files the ERROR lines name: compiler paths, and the objects a link error names (lmmsobjs.dir/core/X.cpp.o
    -> src/core/X.cpp, SomeTest.dir/src/core/X.cpp.o -> tests/src/core/X.cpp). Only files that exist on the branch."""
    files = set()
    for line in [l for l in text.splitlines() if ERROR_RE.search(l)][-12:]:     # the error lines themselves
        for m in re.finditer(r"CMakeFiles/(\w+)\.dir/([\w./-]+?\.(?:cpp|c|cc))\.o", line):
            target, path = m.groups()
            files.add("src/" + path if target == "lmmsobjs" else ("tests/" + path if path.startswith("src/") else path))
        for m in re.finditer(r"(?:zene-cachyos/|^|[\s(\'\"])((?:src|include|tests|plugins|cmake)/[\w./-]+\.(?:cpp|cc|c|h|hpp|py|cmake))", line):
            files.add(m.group(1))
    if branch:
        files = {f for f in files if subprocess.run(["git", "-C", str(CLONE), "cat-file", "-e", f"{branch}:{f}"],
                                                   capture_output=True).returncode == 0}
    return sorted(files)


# real failures only: a bare "error" also matches warnings, flags and file names
ERROR_RE = re.compile(r": (?:fatal )?error:|error adding symbols|undefined reference|multiple definition|TLS definition|"
                      r"ld returned|\*\*\*Failed|\*\*\* \[|FAILED:|FAIL!|The following tests FAILED")


def excerpt(text, n=80):
    lines = text.splitlines()
    keep = [i for i, l in enumerate(lines) if ERROR_RE.search(l)]
    picked = sorted({j for i in keep[-12:] for j in range(max(0, i - 4), min(len(lines), i + 3))})   # a build stops at the end
    return "\n".join(lines[j] for j in picked)[-12000:] or "\n".join(lines[-n:])


def brief(st):
    base, new = st["base"], st["train"]
    text = Path(st["log"]).read_text(errors="replace") if st.get("log") and Path(st["log"]).exists() else ""
    rows = merges(base, new)
    files = named_files(text, new)
    suspects = []
    for r in rows:
        touched = git("diff", "--name-only", f"{r['merge']}^1", r["merge"]).split()
        hit = [f for f in files if f in touched]
        if hit:
            diff = git("diff", f"{r['merge']}^1", r["merge"], "--", *hit)
            suspects.append(f"### {r['branch']} (merge {r['merge'][:10]}) touches {', '.join(hit)}\n```diff\n{diff[:6000]}\n```")
    tried = "\n".join(f"- {h['rung']}: excluded {', '.join(h.get('excluded') or []) or 'nothing'} - {h.get('result', '')}"
                      for h in st.get("history", [])) or "- (none)"
    return f"""You are the train doctor for Zene Studio's automatic merge train (an LMMS-derived C++/Qt DAW).

The train merges accepted fix branches onto the fix base {base} as branch {new}, then builds and tests it. It has now
failed {st['streak']} times in a row the same way, and its own culprit search could not pin a merge:

  {st['why']}

Your job is DIAGNOSIS ONLY. Name the merged branch (or branches, at most {MAX_EXCLUDE}) that must come off the train so it
builds and passes, with evidence. The loop then takes them off and queues a port job for each, so a fixer re-applies
that fix by hand on the newest line - nothing is lost. Typical cause: two branches that are each fine alone but break
together (one changes a declaration, signature or behaviour; the other was written against the old one). Prefer the
branch whose code is stale against the other (usually the one written against older code), and the fewest branches.

You have a code-only checkout of {new} at {DOCTOR_WT} (no build tree). You may read and search it; do not edit,
build or run anything, and do not touch any other directory.

## Error excerpt (from {st.get('log')})
```
{excerpt(text)}
```

## Files the error names
{chr(10).join('- ' + f for f in files) or '- (none found)'}

## Merges on {new} that touch those files
{chr(10).join(suspects) or '(none of the merges touches a named file - look at the full list and the checkout)'}

## Every merge on {new} (oldest first)
{chr(10).join('- ' + r['branch'] for r in reversed(rows))}

## Earlier doctor attempts on this failure
{tried}

## Answer
End your reply with exactly one fenced json block and nothing after it:
```json
{{"exclude": [{{"branch": "bugs/qwen-...", "why": "one sentence with file:line evidence"}}], "confident": true}}
```
Use an empty list only if you are sure no merged branch is the cause, and say what is instead.
"""


# ------------------------------------------------------------------ sessions
def session(rung, prompt, out):
    """Run one rung; returns its final text ('' on failure)."""
    n = rung["name"]
    env = dict(os.environ)
    if n == "opus":
        env.update(HAIKU_MODEL="claude-opus-5-5", HAIKU_EFFORT="medium", HAIKU_FALLBACK="")
        cmd = [str(HAIKU_EXEC), "-C", str(DOCTOR_WT), "-s", "read-only", "-o", str(out), "-"]
    elif n == "sol":
        cmd = ["codex", "exec", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="medium"', "-s", "read-only",
               "-C", str(DOCTOR_WT), "--ephemeral", "-o", str(out), "-"]
    elif n in ("muse", "free"):
        model = "opencode-go/muse-spark-1.3-contributor" if n == "muse" else \
            random.choice([m for m in FREE_MODELS if not opencode_free.cooling(m)])
        rung["model"] = model
        cmd = ["opencode", "run", "--pure", "--model", opencode_free.full_id(model), "--dir", str(DOCTOR_WT),
               "--title", "Zene train doctor"] + (["--variant", "high"] if n == "muse" else [])
    else:
        import qwenfix
        cmd = ["mcode", "exec", "--config", str(qwenfix.mcode_config()), "--model", qwenfix.QWEN_MODEL,
               "--permission", "smart", "--prompt-mode", "coding", "--cwd", str(DOCTOR_WT), "-o", str(out), "--input", "-"]
    logf = out.with_suffix(".log")
    try:
        with logf.open("w") as lf:
            r = subprocess.run(cmd, input=prompt, text=True, stdout=lf, stderr=subprocess.STDOUT, env=env,
                               timeout=SESSION_TIMEOUT_S, cwd=str(DOCTOR_WT))
        if n in ("muse", "free"):          # opencode prints the answer on stdout
            out.write_text(logf.read_text(errors="replace"))
        return out.read_text(errors="replace") if r.returncode == 0 and out.exists() else ""
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"{rung['label']}: session failed ({e!r})")
        return ""


def verdict(text):
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not blocks:
        return None
    try:
        v = json.loads(blocks[-1])
    except ValueError:
        return None
    return v if isinstance(v.get("exclude"), list) else None


def checkout(new):
    if not (DOCTOR_WT / ".git").exists():
        subprocess.run(["git", "-C", str(CLONE), "worktree", "add", "--detach", str(DOCTOR_WT), new],
                       capture_output=True, check=True)
    subprocess.run(["git", "-C", str(DOCTOR_WT), "checkout", "-q", "--detach", "--force", new], capture_output=True, check=True)


def run():
    st = load()
    if not st.get("train"):
        print("nothing to diagnose")
        return 0
    st["running"] = True
    save(st)
    try:
        checkout(st["train"])
        prompt = brief(st)
        runs = mt.M / "doctor"
        runs.mkdir(exist_ok=True)
        while st.get("rung", 0) < len(LADDER):
            rung = dict(LADDER[st["rung"]])
            if not available(rung):
                log(f"rung {st['rung'] + 1} ({rung['label']}) unavailable now - next rung")
                st["history"].append({"rung": rung["label"], "result": "unavailable", "at": time.strftime("%H:%M")})
                st["rung"] += 1
                save(st)
                continue
            log(f"rung {st['rung'] + 1}: {rung['label']} diagnosing {st['train']}")
            out = runs / f"{st['train'].split('/')[-1]}-{rung['name']}-{int(time.time())}.md"
            v = verdict(session(rung, prompt, out))
            merged = {r["branch"]: r for r in merges(st["base"], st["train"])}
            excl = [e for e in (v or {}).get("exclude", []) if isinstance(e, dict) and e.get("branch") in merged][:MAX_EXCLUDE]
            entry = {"rung": rung.get("model") and f"{rung['label']} [{rung['model']}]" or rung["label"],
                     "at": time.strftime("%Y-%m-%dT%H:%M"), "answer": str(out)}
            st["rung"] += 1                 # the next doctor (if this does not fix it) goes one rung down
            if not excl:
                entry["result"] = "no usable verdict" if v is None else "named no merged branch"
                st["history"].append(entry)
                save(st)
                log(f"{rung['label']}: {entry['result']} - next rung")
                continue
            for e in excl:
                b = e["branch"]
                cluster = re.sub(r"^(bugs/qwen-|feat/050-)", "", b).removesuffix("-oc")
                mt.port_job({"cluster": cluster, "branch": b, "commit": git("rev-parse", merged[b]["merge"] + "^2")},
                            f"train doctor ({entry['rung']}): {e.get('why', '')[:300]}", exclude=True)
            entry.update(excluded=[e["branch"] for e in excl], result="applied; train restarted")
            st["history"].append(entry)
            st["streak"] = STREAK - 1       # the next failure of the same kind starts the next rung at once
            save(st)
            log(f"{rung['label']} took {', '.join(entry['excluded'])} off the train - train restarted")
            mt.notify(f"Merge train doctor ({rung['label']}) took {', '.join(entry['excluded'])} off the train; "
                      f"a port job each; the train is running again.")
            subprocess.run(["systemctl", "--user", "start", "--no-block", "zene-merge-train.service"], capture_output=True)
            return 0
        mt.notify(f"Merge train doctor: every rung tried on {st['train']} without a fix. It needs you.")
        log("ladder exhausted")
        return 1
    finally:
        st["running"] = False
        save(st)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        sys.exit(run())
    print(json.dumps(load(), indent=1))
