#!/usr/bin/env python3
"""Qwen (local, via Strata) fixes Zene bugs one at a time; the light driver QA-checks each result.

The light driver is Claude Haiku 5.5 (tools/haiku/haiku-exec) since 2026-10-08; `echo luna > LIGHT-ENGINE`
switches it back to GPT-6 Luna (codex). The engine key "luna" in this file names the light-driver slot.

  qwenfix.py fix      fixer loop: next bug -> fresh worktree off its lane tip -> configure +
                      build zene -> one mcode session on Strata's Qwen -> queue for QA
  qwenfix.py qa       QA loop: one light-driver review per queued result ->
                      ACCEPT / REWORK (back to Qwen) / REJECT (parked)
  qwenfix.py status   counts, current work, recent log lines

Bug source: the Clef inventory of the 2026-10-06 campaign (624 clusters), ordered
EASY -> unclassified -> needs_context -> hard; clusters Luna-easy already attempted are skipped.
Stop: touch STOP (both loops), STOP-FIX or STOP-QA; each finishes its current item first.
Never pushes, never merges: accepted fixes stay on bugs/qwen-* branches for the merge train.
"""
import datetime
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import adaptive
import opencode_free
import autoscale
import issue_ledger
import workflow_runtime
import integration_jobs
import workflow_evidence

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
PROG = HOME / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
CAMPAIGN = PROG / "research/reviews/luna-easy-fix-2026-10-06"
NEW_TRIAGE = PROG / "research/reviews/luna-new-triage"     # tools/luna-patrol/triage_new.py, after each patrol pass
STRATA_DIR = HOME / "Documents/AI_KOS_PROJECT/projects/strata"
BUNNY = HOME / ".local/share/zene-bunny-fix/bunny"
GIT = "/usr/bin/git"
CMAKE = "/usr/bin/cmake"
STRATA = os.environ.get("STRATA_URL", "http://127.0.0.1:8081")   # 8080 is SearXNG on this machine
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
TRIM_PORT = int(os.environ.get("QWEN_TRIM_PORT", "8092"))   # trim_proxy.py: drops unused tools/text, ~13.5K -> ~6K tokens
TRIM_URL = f"http://127.0.0.1:{TRIM_PORT}"
QWEN_MODEL = "custom_provider:strata/qwen3.8-flash-next-coder"
REPORT_DIR = "docs/qwen-fixes-2026-10"

MIN_FREE_GB = int(os.environ.get("QWEN_MIN_FREE_GB", "40"))
# Engine schedule (owner, 2026-10-07): Qwen on Strata 00:00-06:00, the light driver every other hour.
QWEN_HOURS = (0, 6)
LUNA_WORKERS = int(os.environ.get("QWEN_LUNA_WORKERS", "16"))    # parallel Luna fixes; sessions mostly think remotely, so they can outnumber the build slots
QA_WORKERS = int(os.environ.get("QWEN_QA_WORKERS", "6"))          # parallel Luna QA reviews


def luna_cap(kind):
    """Live Luna session cap from LUNA-CAP ("fix=12 qa=5"), re-read every loop; the constants above are the fallback."""
    try:
        caps = dict(x.split("=") for x in (ROOT / "LUNA-CAP").read_text().split())
        ceiling = max(0, int(caps[kind]))
    except (OSError, ValueError, KeyError):
        ceiling = LUNA_WORKERS if kind == "fix" else QA_WORKERS
    try:     # 2026-10-25: the usage monitor's pace (luna_pace.py) can only lower the owner's ceiling
        import luna_pace
        return luna_pace.cap(kind, ceiling)
    except Exception:
        return ceiling
BUILD_JOBS = os.environ.get("QWEN_BUILD_JOBS", "6")             # initial zene build of a fresh tree
SESSION_JOBS = os.environ.get("QWEN_SESSION_JOBS", "4")         # builds inside a Qwen/Luna session
MAX_ROUNDS = int(os.environ.get("QWEN_MAX_ROUNDS", "2"))        # Qwen attempts per bug incl. reworks
FIX_TIMEOUT = os.environ.get("QWEN_FIX_TIMEOUT", "150m")
# At medium effort Qwen sometimes thought past the reply cap and ended a turn with no answer
# ("Runtime completed without a final assistant response"; 8192 tokens generated, measured 2026-10-06).
QWEN_EFFORT = os.environ.get("QWEN_EFFORT", "high")     # with the 24K reply cap and 128K context (owner, 2026-10-06)
QWEN_MAX_OUTPUT = int(os.environ.get("QWEN_MAX_OUTPUT", "24576"))
KEEP_ACCEPTED = os.environ.get("QWEN_KEEP_ACCEPTED_TREES") == "1"

D = {k: ROOT / k for k in ("state", "qa/pending", "qa/done", "qa/reviews", "rework", "parked", "logs", "runs")}
for p in D.values():
    p.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ helpers
# Light driver (owner, 2026-10-08): Claude Haiku 5.5 unless LIGHT-ENGINE says "luna". Read live, like LUNA-CAP
# (which caps whichever model is the light driver). Each model has its own usage-limit file, shared with the
# research driver: a Haiku limit does not pause Luna or the other way round.
HAIKU_EXEC = PROG / "tools/haiku/haiku-exec"
HAIKU_EFFORT = os.environ.get("HAIKU_EFFORT", "high")


def light_engine():
    # Owner, 2026-10-14: Haiku fixes, Luna reviews. FIX-ENGINE ("haiku"|"luna") overrides LIGHT-ENGINE in the
    # fix process only, so the QA loop (and patrol/research, which read LIGHT-ENGINE) keep Luna.
    name = "FIX-ENGINE" if len(sys.argv) > 1 and sys.argv[1] == "fix" and (ROOT / "FIX-ENGINE").exists() else "LIGHT-ENGINE"
    try:
        return "luna" if (ROOT / name).read_text().strip() == "luna" else "haiku"
    except OSError:
        return "haiku"


def light_name():
    return "Luna" if light_engine() == "luna" else "Haiku 5.5"


def light_model():
    return "gpt-6-luna" if light_engine() == "luna" else "claude-haiku-5-5"


def light_cmd(codex_cmd):
    """codex_cmd is the Luna command line; for Haiku the same arguments go to haiku-exec."""
    return codex_cmd if light_engine() == "luna" else [str(HAIKU_EXEC)] + codex_cmd[2:]


def limit_file():
    return ROOT / ("LUNA-LIMIT-UNTIL" if light_engine() == "luna" else "HAIKU-LIMIT-UNTIL")


MODEL_LABEL = {"gpt-6-luna": "GPT-6 Luna", "claude-haiku-5-5": "Claude Haiku 5.5",
               QWEN_MODEL: "Local Qwen coder"}


def fixer_label(rec):
    """Who wrote a fix: records from before 2026-10-08 have no model and were Luna's."""
    return MODEL_LABEL.get(rec.get("model", "gpt-6-luna"), rec.get("model"))
LIMIT_RE = re.compile(r"hit your usage limit", re.I)


class LunaLimit(Exception):
    """A Luna session was refused for the plan's usage limit: not an attempt, the job goes back unchanged."""


class FreeModelLimit(Exception):
    """Free endpoints unavailable without changes: retry later without spending an attempt."""


def note_luna_limit(log_path, target=None):
    """If a session log shows the usage limit, record when to try again (the time it names, +2 min) and say so.
    target: the limit file to write (default: the light driver's); the Haiku reviewer passes HAIKU-LIMIT-UNTIL."""
    try:
        text = Path(log_path).read_text(errors="replace")
    except OSError:
        return False
    if not LIMIT_RE.search(text):
        return False
    until = datetime.datetime.now() + datetime.timedelta(minutes=30)      # fallback if no time is given
    m = re.search(r"try again at (\d{1,2}):(\d{2})\s*([AP]M)?", text, re.I)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if m.group(3):
            h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
        t = datetime.datetime.now().replace(hour=h, minute=mi, second=0, microsecond=0)
        until = (t if t > datetime.datetime.now() else t + datetime.timedelta(days=1)) + datetime.timedelta(minutes=2)
    target = target or limit_file()
    current = paused_until_in(target)
    if not current or until > current:
        target.write_text(until.isoformat(timespec="minutes") + "\n")
    return True


def luna_paused_until():
    return paused_until_in(limit_file())


def paused_until_in(path):
    try:
        until = datetime.datetime.fromisoformat(path.read_text().strip())
    except (OSError, ValueError):
        return None
    if until <= datetime.datetime.now():
        path.unlink(missing_ok=True)
        return None
    return until


HAIKU_LIMIT = ROOT / "HAIKU-LIMIT-UNTIL"


def haiku_review_ok():
    """The second reviewer (owner, 2026-10-15): Haiku 5.5, unless HAIKU-QA-OFF is set or Haiku is at its limit."""
    return not (ROOT / "HAIKU-QA-OFF").exists() and not paused_until_in(HAIKU_LIMIT)


def fixed_by_haiku(rec):
    """A Haiku-made fix is never reviewed by Haiku: it waits for Luna."""
    return rec.get("model") == "claude-haiku-5-5" or "haiku" in str(rec.get("harness", ""))


def luna_available():
    """Luna can take sessions: not held for the research campaign and not inside a usage-limit pause."""
    return not (ROOT / "LUNA-HOLD").exists() and not luna_paused_until()


def engine_now():
    """'qwen' inside QWEN_HOURS, else 'luna' - and 'qwen' by day too whenever Luna is unavailable
    (held for research or paused at its usage limit; owner, 2026-10-07). QWEN_ENGINE=qwen|luna forces one."""
    if (ROOT / "QWEN-PAUSED").exists():
        return "luna"
    forced = os.environ.get("QWEN_ENGINE")
    if forced in ("qwen", "luna"):
        return forced
    if QWEN_HOURS[0] <= datetime.datetime.now().hour < QWEN_HOURS[1] or not luna_available():
        return "qwen"
    return "luna"


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def log(stream, msg):
    line = f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(D["logs"] / f"{stream}.log", "a") as f:
        f.write(line + "\n")


def git(tree, *args, check=False):
    r = subprocess.run([GIT, "-C", str(tree), *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {tree}: {r.stderr.strip()}")
    return r.stdout.strip()


def free_gb():
    return shutil.disk_usage(PROG).free / 1e9


def stopped(which):
    return (ROOT / "STOP").exists() or (ROOT / f"STOP-{which}").exists()


def slug(cluster):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(cluster))


def write_json(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(path)


def jsonl(path):
    out = []
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def session_env():
    e = dict(os.environ)
    e.pop("LMMS_PLUGIN_DIR", None)  # trap 2: leaks into PluginScanCacheTest
    deps = HOME / ".local/zene-deps/root/usr/lib"
    e.update({
        "PATH": f"{ROOT / 'shims'}:{BUNNY / 'shims'}:{e['PATH']}",   # qwen/shims/mcode: the lean mcode data dir
        "BUNNY_LOCKDIR": str(BUNNY / "locks"),
        "BUNNY_JOBS": str(min(adaptive.settings()["session_jobs"], 2)
                          if (ROOT / "LOCAL-QWEN").exists() else adaptive.settings()["session_jobs"]),
        "CCACHE_DIR": str(HOME / ".cache/ccache-bunny"),
        "CCACHE_MAXSIZE": "25G",
        "CCACHE_BASEDIR": str(PROG),
        "CCACHE_NOHASHDIR": "true",
        "QT_QPA_PLATFORM": "offscreen",
        "TMPDIR": str(HOME / ".cache/zene-tmp"),   # 2026-10-22: /tmp is RAM (tmpfs); sessions filled it to 15/16 GB
        "PKG_CONFIG_PATH": f"{deps}/pkgconfig",   # CachyOS: FFmpeg comes from pacman; the Ubuntu .pc dir stays off
        "LD_LIBRARY_PATH": f"{deps}:{HOME / '.local/zene-deps/onnxruntime-linux-x64-1.28.0/lib'}"
                           + (":" + e["LD_LIBRARY_PATH"] if e.get("LD_LIBRARY_PATH") else ""),
    })
    return e


def build_env():
    """The driver's own configure/build: the session env minus the guard shims (CMake must init submodules)."""
    e = session_env()
    shims = {str(ROOT / "shims"), str(BUNNY / "shims")}
    e["PATH"] = ":".join(p for p in e["PATH"].split(":") if p not in shims)
    return e


def strata_health():
    try:
        with urllib.request.urlopen(f"{STRATA}/health", timeout=5) as r:
            h = json.loads(r.read())
        return h if h.get("loaded") else None
    except Exception:
        return None


def trim_proxy_up():
    """Start trim_proxy.py if nothing listens on its port; it forwards to Strata, so it can outlive restarts."""
    out = subprocess.run(["ss", "-ltnH", f"sport = :{TRIM_PORT}"], capture_output=True, text=True).stdout
    if out.strip():
        return True
    subprocess.Popen([sys.executable, str(ROOT / "trim_proxy.py"), "--port", str(TRIM_PORT), "--upstream", STRATA],
                     stdout=open(D["logs"] / "trim-proxy.out", "a"), stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    log("fix", f"trim proxy started on :{TRIM_PORT} -> {STRATA}")
    return True


def ollama_unload_all():
    """Evict every model Ollama holds (Clef, embeddings): it keeps them in VRAM ~5 min after use by default."""
    def call(path, body=None):
        req = urllib.request.Request(OLLAMA + path, json.dumps(body).encode() if body else None,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    try:
        loaded = [m["name"] for m in call("/api/ps").get("models", [])]
    except Exception:
        return  # Ollama not running: nothing to unload
    for name in loaded:
        try:
            subprocess.run(["ollama", "stop", name], capture_output=True, timeout=60)  # works for every model; Clef refuses /api/generate
        except Exception:
            pass
    for _ in range(30):
        try:
            if not call("/api/ps").get("models"):
                break
        except Exception:
            break
        time.sleep(2)
    if loaded:
        log("fix", f"unloaded from Ollama: {', '.join(loaded)}")


def gpu_free_mib():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else 0


def strata_start():
    if (ROOT / "QWEN-PAUSED").exists():
        return False
    if strata_health():
        return True
    ollama_unload_all()
    for _ in range(60):   # Strata needs nearly the whole card; wait for other GPU users to let go
        if gpu_free_mib() >= 14000:
            break
        time.sleep(5)
    else:
        log("fix", f"GPU still busy ({gpu_free_mib()} MiB free) - not starting Strata yet")
        return False
    runs = sorted(STRATA_DIR.glob("run-coder*.sh")) or sorted(STRATA_DIR.glob("run-*.sh"))
    if not runs:
        return False
    out = open(STRATA_DIR / "strata-server.out", "a")
    proc = subprocess.Popen([str(runs[0])], cwd=STRATA_DIR, stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    (ROOT / "strata.pgid").write_text(str(proc.pid))
    log("fix", f"starting Strata ({runs[0].name}, pgid {proc.pid}) - loading takes 1-3 min")
    for _ in range(90):
        if strata_health():
            log("fix", "Strata is up")
            return True
        if proc.poll() is not None:
            log("fix", f"Strata exited (rc={proc.returncode}) - see {STRATA_DIR}/strata-server.out")
            return False
        time.sleep(10)
    return bool(strata_health())


def strata_pids():
    """Processes listening on Strata's port (it may have been started outside this script)."""
    port = STRATA.rsplit(":", 1)[-1].split("/")[0]
    out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True).stdout
    return {int(p) for p in re.findall(r"pid=(\d+)", out)}


def strata_stop():
    import signal
    groups = set()
    pg = ROOT / "strata.pgid"
    if pg.exists():
        groups.add(int(pg.read_text()))
    for pid in strata_pids():
        try:
            groups.add(os.getpgid(pid))
        except ProcessLookupError:
            pass
    for g in groups:
        try:
            os.killpg(g, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for _ in range(60):
        if not strata_pids():
            break
        time.sleep(2)
    pg.unlink(missing_ok=True)
    log("fix", "Strata stopped (GPU released)")


def yield_gpu_if_requested():
    """Clef (triage_new.py) asks for the GPU between bugs: stop Strata, hand over, restart after."""
    req = ROOT / "GPU-REQUEST"
    if not req.exists():
        return
    log("fix", f"GPU requested by Clef triage ({req.read_text().strip()}) - pausing between bugs")
    strata_stop()
    (ROOT / "GPU-FREE").write_text(now() + "\n")
    while req.exists() and not stopped("FIX"):
        holder = req.read_text().split()[0] if req.read_text().split() else ""
        if holder.isdigit() and not Path(f"/proc/{holder}").exists():
            log("fix", "GPU requester is gone - taking the GPU back")
            req.unlink(missing_ok=True)
            break
        time.sleep(30)
    (ROOT / "GPU-FREE").unlink(missing_ok=True)
    log("fix", "GPU handed back")


# ------------------------------------------------------------------ the bug queue
def norm_item(item):
    """Some inventory rows name their lane tree relatively ('zene-ui'): resolve against the program root."""
    wt = item.get("worktree")
    if wt and not str(wt).startswith("/"):
        item = {**item, "worktree": str(PROG / wt)}
    return item


def clef_retriage_dropped():
    """Owner 2026-10-19 ("get clef classifying everything it can"): tools/luna-patrol/retriage_open.py re-asks Clef
    whether each untouched queued bug is still open on FIX-BASE. A cluster whose latest row says already_fixed or
    likely_false_positive is skipped; deleting CLEF-RETRIAGE.jsonl undoes it."""
    latest = {}
    for r in jsonl(ROOT / "CLEF-RETRIAGE.jsonl"):
        latest[str(r["cluster"])] = r.get("disposition")
    return {k for k, d in latest.items() if d in ("already_fixed", "likely_false_positive")}


# Owner 2026-10-19 ("find a way to fix smarter"): the untouched queue is ordered by confidence, then severity, then
# Clef's pairwise difficulty (BUG-RANK.json from tools/luna-patrol/bug_difficulty_sort.py: 0 = easiest), and routed by
# difficulty: local Strata takes only the easier half.
SEVERITY = [("realtime", re.compile(r"audio thread|realtime|real-time|render thread|allocat\w* (on|in) the|lock\w* (on|in) the "
                                    r"(audio|render)|frees? .* on the audio|xrun|dropout", re.I)),
            ("data-loss", re.compile(r"\bundo|\bredo|save|load|serializ|project file|journal|snapshot|round-?trip|lost|loses|"
                                     r"discard|overwrit|corrupt", re.I)),
            ("crash", re.compile(r"crash|dangl|use-after|after free|null|dereferenc|outliv|double free|segfault|abort", re.I))]
STRATA_MAX_PCT = 0.5
# Owner 2026-10-19 ("build the same-file batching"): a new bug job takes up to BATCH_MAX untouched bugs of the same
# file (one tree, one build, one session, one review); BATCH-MAX overrides it live (1 = off). Strata gets at most 2.
BATCH_MAX = 3
BATCH_VERDICT = re.compile(r"^\W*([\w.-]+)\W*[:|-]\s*\**(FIXED|ALREADY-FIXED|FALSE-POSITIVE|NEEDS-CONTEXT|BLOCKED|IMPLEMENTED)\b", re.M)


ROOT_CAUSE_MAX = 6      # members per root-cause job (owner 2026-10-19: common root causes, judged by Clef)


def root_cause_groups():
    """{cluster: group} from ROOT-CAUSE-GROUPS.json (tools/luna-patrol/root_causes.py confirm)."""
    try:
        groups = json.loads((ROOT / "ROOT-CAUSE-GROUPS.json").read_text()).get("groups", [])
    except (OSError, ValueError):
        return {}
    return {c: g for g in groups for c in g["members"]}


# Feature batches (owner 2026-10-19: "don't set a hard cap ... it makes it easier to build features, and debloat in the
# future, and less spaghetti code if the agent is aware of future things that have to go in the same place"): a fresh
# unit takes with it every other fresh queued unit of the same tier and area whose scoping brief names a real source
# file shared with the batch (transitively). One branch and tree for all; designed together; reviewed together.
BRIEFS = PROG / "research/reviews/feature-briefs"
GENERIC_FILES = re.compile(r"ControlRegistry\.(cpp|h)$|CMakeLists\.txt$|fork-sources\.txt$|all-sources\.txt$|"
                           r"upstream-modifications\.txt$|commands_snapshot\.json$|index\.html$|^docs/|README")
BRIEF_FILE = re.compile(r"zene-scope/((?:src|include|plugins|tests)/[\w/.+-]+\.(?:cpp|h|hpp|c|py))")


def brief_files(unit):
    f = BRIEFS / f"{unit}.md"
    if not f.exists():
        return None
    return {x for x in BRIEF_FILE.findall(f.read_text()) if not GENERIC_FILES.search(x)}


def feature_batch(item, candidates, claimed):
    """Fresh units of the same tier and area that share a scoped source file with the batch (no size cap)."""
    if not FEATURE_BATCHING or (D["state"] / f"{slug(item['cluster'])}.json").exists():
        return []
    files = brief_files(item["cluster"])
    if not files:
        return []
    pool = []
    for c in candidates:
        q = str(c.get("cluster") or c.get("queue_id"))
        if (q == str(item["cluster"]) or c.get("feature_tier") != item.get("feature_tier") or c.get("area") != item.get("area")
                or slug(q) in claimed or (D["state"] / f"{slug(q)}.json").exists() or int(c.get("round_base") or 0)
                or not c.get("acceptance")):
            continue
        cf = brief_files(q)
        if cf:
            pool.append((c, cf))
    mates, grew = [], True
    while grew:
        grew = False
        for entry in list(pool):
            if entry[1] & files:
                mates.append(entry[0])
                files |= entry[1]
                pool.remove(entry)
                grew = True
    return [{**m, "cluster": str(m.get("cluster") or m.get("queue_id"))} for m in mates]


FEATURE_BATCHING = not (ROOT / "FEATURE-BATCH-OFF").exists()


def batch_max(engine):
    try:
        n = int((ROOT / "BATCH-MAX").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        n = BATCH_MAX
    return max(1, min(n, 2) if engine == "qwen" else n)


def bug_file(item):
    return str(item.get("resolved_path") or item.get("file") or "").strip()


def batch_verdicts(report_text, item):
    """{cluster: verdict} from a batch report's per-bug lines (`<cluster>: FIXED` ...)."""
    wanted = {str(item["cluster"])} | {str(b["cluster"]) for b in item.get("batch") or []}
    return {m.group(1): m.group(2) for m in BATCH_VERDICT.finditer(report_text) if m.group(1) in wanted}


def bug_rank():
    try:
        return json.loads((ROOT / "BUG-RANK.json").read_text())
    except (OSError, ValueError):
        return {}


def bug_key(cluster):
    c = str(cluster)
    while c.endswith("-port"):
        c = c[:-len("-port")]
    return c


def severity(item):
    t = f"{item.get('title', '')} {str(item.get('text', ''))[:600]}"
    return next((i for i, (_, rx) in enumerate(SEVERITY) if rx.search(t)), len(SEVERITY))


def smart_order(items):
    rank = bug_rank()
    for i in items:
        i["bug_pct"] = rank.get(bug_key(i["cluster"]))
    return sorted(items, key=lambda i: (i.get("realness") not in (None, "likely_open_bug"), severity(i),
                                        i["bug_pct"] if i.get("bug_pct") is not None else 0.5))


def load_queue():
    """Every remaining cluster, best-first. Items from Clef's queues carry its classification."""
    inv = {}
    for f in sorted(glob.glob(str(CAMPAIGN / "inventory/*.jsonl"))):
        for row in jsonl(f):
            inv[str(row["cluster"])] = row
    tiers = []
    seen = set()
    stale = clef_retriage_dropped()
    for name in ("easy", "new:easy", "UNCLASSIFIED", "needs_context", "new:needs_context", "hard", "new:hard"):
        if name.startswith("new:"):
            rows = [{**r, "clef_tier": name} for r in jsonl(NEW_TRIAGE / f"queue/{name[4:]}.jsonl")]
        elif name == "UNCLASSIFIED":
            classified = {str(r["cluster"]) for q in ("easy", "needs_context", "hard")
                          for r in jsonl(CAMPAIGN / f"queue/{q}.jsonl")}
            rows = [r for k, r in inv.items() if k not in classified]
        else:
            rows = [{**inv.get(str(r["cluster"]), {}), **r, "clef_tier": name}
                    for r in jsonl(CAMPAIGN / f"queue/{name}.jsonl")]
        for r in rows:
            k = str(r["cluster"])
            if k in stale:
                continue
            if k not in seen and r.get("worktree"):
                seen.add(k)
                tiers.append(norm_item(r))
    return tiers


def luna_easy_attempted():
    return {p.name for p in (CAMPAIGN / "runtime").iterdir() if p.is_dir()} if (CAMPAIGN / "runtime").exists() else set()


FEATURE_BRIEF = PROG / "research/reviews/train5-2026-10-15/features/FEATURE-BRIEF.md"   # v1, kept for reference
FEATURE_BRIEF_V2 = PROG / "research/reviews/train7-2026-10-19/features/FEATURE-BRIEF-V2.md"
# Clef delegation policy (research/reviews/clef-difficulty-2026-10-13/delegation.py): a unit moves up one tier after a
# failed two-round attempt; features_queue.py re-queues a parked unit on the next tier.
FEATURE_MAX_ROUNDS = 2   # owner 2026-10-18: fail -> back to the implementing session -> fail again -> up a tier
SMALL_CONTEXT_MODELS = "deepseek-v4.1-flash-free"   # never on M/L feature units


def feature_tiers():
    """Owner 2026-10-16: features run only on the FEATURE-TIERS.json models of the unit's tier (bottom: GLM 5.3 Flash +
    Mimo; middle: paid Muse Spark contributor); those models never take bugs, every other model takes bugs only."""
    try:
        return {k: v for k, v in json.loads((ROOT / "FEATURE-TIERS.json").read_text()).items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {}


def eval_order(models):
    """FEATURE-EVAL.json (owner 2026-10-19: Mimo V2.6 Flash vs GLM 5.3 Flash for the bottom tier): when a tier's
    models are the eval's, the one with the fewest eval attempts so far (BENCHMARK.json feature_eval) goes first."""
    try:
        ev = json.loads((ROOT / "FEATURE-EVAL.json").read_text())
        seen = json.loads((ROOT / "BENCHMARK.json").read_text()).get("feature_eval", {}).get("models", {})
    except (OSError, ValueError):
        return models
    if not models or not set(models) <= set(ev.get("models", [])):
        return models
    return sorted(models, key=lambda m: (seen.get(m, {}).get("attempts", 0), models.index(m)))


def trial_pick():
    """BUG-TRIAL.json (owner 2026-10-16): the idle trial model with the fewest bug attempts since the trial started
    (under its per-model target), or None. Balances the sample so every model sees a similar mix of bugs."""
    try:
        cfg = json.loads((ROOT / "BUG-TRIAL.json").read_text())
    except (OSError, ValueError):
        return None
    if not cfg.get("active"):
        return None
    counts = {m: 0 for m in cfg.get("models", [])}
    for rec_path in D["runs"].glob("*/record.json"):
        try:
            r = json.loads(rec_path.read_text())
        except (OSError, ValueError):
            continue
        if r.get("model") in counts and str(r.get("started", "")) >= cfg.get("started", "") \
                and not str(r.get("tier", "")).startswith("feature:"):
            counts[r["model"]] += 1
    target = int(cfg.get("per_model", 25))
    for m in sorted(counts, key=counts.get):
        if counts[m] < target and opencode_free.model_free(m, "feature"):
            return m
    return None


def feature_models():
    return sorted({m for models in feature_tiers().values() for m in models})


def tier_model_free(tier):
    models = feature_tiers().get(tier)
    return models is None or any(opencode_free.model_free(m, "feature") for m in models)


def feature_model_free():
    return not feature_tiers() or any(tier_model_free(t) for t in feature_tiers())


def load_features():
    """0.5.0 feature units for the OpenCode fixers (owner, 2026-10-15), from FEATURE-QUEUE.jsonl (features_queue.py),
    only while FEATURES-ON exists. They go ahead of untouched bugs; reworks still come first."""
    if not (ROOT / "FEATURES-ON").exists():
        return []
    return [norm_item(r) for r in jsonl(ROOT / "FEATURE-QUEUE.jsonl")]


def is_feature(item):
    return (item or {}).get("kind") == "feature"


# ------------------------------------------------------------------ Claude lanes (owner 2026-10-19)
# "build the claude subagents into the feature loop, and have a simple system where you can just add whatever lane and
# remove whatever lane you want". LANES.json (read live; edit it or use lanes.py) lists the lanes: each runs one unit
# at a time on its model/effort, taking Sonnet/Opus-tier units from CLAUDE-QUEUE.jsonl (features_queue.py) in queue
# order. A lane stays OPEN while its unit is in QA or rework (owner: "the one that is in review counts as an open
# lane"); a rework goes back to the same lane and continues the same Claude session; a second failed round parks the
# unit and features_queue.py moves it up a tier (sonnet -> opus). Sessions run through tools/haiku/haiku-exec (headless
# Claude Code in the Landlock write-jail). A usage limit pauses every lane until CLAUDE-LIMIT-UNTIL.
CLAUDE_TIERS = ("sonnet", "opus")
CLAUDE_LIMIT = ROOT / "CLAUDE-LIMIT-UNTIL"
CLAUDE_TIMEOUT_MIN = 180
FINAL_OUTCOMES = ("accept", "confirm", "parked", "no-result", "driver-error", "build-failed")   # compared lower-case


# Owner 2026-10-19: "nothing should go into human review". A bug the loop would have parked climbs a ladder instead:
#   level 1: a fresh session on a strong OpenCode fixer (no Strata), with every earlier review as notes
#   level 2: Claude lane tier "bug" (Sonnet), level 3: tier "bug-hard" (Opus)
#   level 4: deferred - out of the queue until the next FIX-BASE, when it starts again at level 1
# A build failure (tree could not be prepared) is retried once at the same level first. Features already climb
# free -> Muse -> Sonnet -> Opus in features_queue.py.
BUG_LADDER = {1: None, 2: "bug", 3: "bug-hard"}
BUG_ESC = ROOT / "BUG-ESCALATIONS.json"


def bug_escalate(item, rnd, reason, notes=""):
    """Requeue a would-be-parked bug one rung up the ladder; returns the new level."""
    try:
        esc = json.loads(BUG_ESC.read_text())
    except (OSError, ValueError):
        esc = {}
    c = str(item["cluster"])
    base = (ROOT / "FIX-BASE").read_text().strip()
    cur = esc.get(c, {})
    level = (cur.get("level", 0) if cur.get("base") == base else 0) + 1
    history = cur.get("history", []) + [{"at": now(), "reason": reason, "round": rnd, "to_level": level}]
    esc[c] = {"level": level, "base": base, "history": history}
    BUG_ESC.write_text(json.dumps(esc, indent=1))
    s = slug(c)
    batch = item.get("batch") or []
    for b in batch:                     # companions go back to the queue on their own
        (D["state"] / f"{slug(b['cluster'])}.json").unlink(missing_ok=True)
    clean = {k: v for k, v in item.items() if k not in ("batch", "root_cause")}
    if level >= 4:
        clean["deferred"] = True
        write_json(D["state"] / f"{s}.json", {"cluster": c, "outcome": "deferred", "escalation": esc[c], "item": clean})
        log("fix", f"{s}: {reason} - deferred until the next train (ladder exhausted on {base})")
        return level
    clean["escalation_level"] = level
    tier = BUG_LADDER.get(level)
    if tier:
        clean["claude_tier"] = tier
    else:
        clean.pop("claude_tier", None)    # 2026-10-20: a Haiku bug-lane unit moves on to the OpenCode fixers, not back
    job = {"item": clean, "round": rnd + 1,
           "notes": (f"ESCALATED (level {level}) after: {reason}. Earlier attempts by other fixers did not succeed; "
                     f"start fresh from the current code and read their reviews below.\n\n{notes}")}
    write_json(D["rework"] / f"{s}.json", job)
    write_json(D["state"] / f"{s}.json", {"cluster": c, "outcome": "escalated", "escalation": esc[c]})
    log("fix", f"{s}: {reason} - escalated to level {level}" + (f" (Claude lane tier {tier})" if tier else " (strong OpenCode fixer)"))
    return level


def is_claude_item(item):
    return (is_feature(item) and (item or {}).get("feature_tier") in CLAUDE_TIERS) or bool((item or {}).get("claude_tier"))


def claude_lanes():
    try:
        cfg = json.loads((ROOT / "LANES.json").read_text())
    except (OSError, ValueError):
        return []
    return [l for l in cfg.get("lanes", []) if l.get("enabled", True) and l.get("name") and l.get("model")]


def lane_holding(name):
    """The units a lane still holds: state records of its own that are not final (in QA, rework, retry)."""
    held = set()
    try:
        bug_slugs = [slug(c) for c, e in json.loads(BUG_ESC.read_text()).items() if e.get("level", 0) >= 2]
    except (OSError, ValueError):
        bug_slugs = []
    for f in list(D["state"].glob("Q050-*.json")) + [D["state"] / f"{b}.json" for b in bug_slugs]:
        if not f.exists():
            continue
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if rec.get("lane") == name and not str(rec.get("outcome", "")).lower().startswith(FINAL_OUTCOMES):
            if issue_ledger.gate(rec.get("item") or rec) in ("blocked", "exhausted"):
                continue
            held.add(rec.get("cluster"))
    return held


def next_claude_item(lane, claimed):
    """A lane's next job: a rework of the unit it holds, else (when it holds nothing) the next queued unit of its tiers."""
    held = lane_holding(lane["name"])
    tiers_ = lane.get("tiers") or list(CLAUDE_TIERS)
    for r in sorted(D["rework"].glob("*.json"), key=os.path.getmtime):
        if r.stem in claimed:
            continue
        job = json.loads(r.read_text())
        ct = (job.get("item") or {}).get("claude_tier")
        if ct and ct in tiers_ and not held and not job.get("lane"):
            job["item"] = norm_item(job["item"])
            job["lane"] = lane["name"]
            return job, r
        mine = job["item"]["cluster"] in held if held else job.get("lane") == lane["name"]
        if is_claude_item(job.get("item")) and mine:
            job["item"] = norm_item(job["item"])
            job["lane"] = lane["name"]
            return job, r
    if held:
        return None, None                     # its unit is in review: the lane stays open
    if lane.get("bug_queue"):
        # 2026-10-20 owner: "do the haiku thing, but only with bugs" - a lane that takes untouched bugs from the easier
        # half of Clef's ranking (BUG-RANK percentile <= max_pct, as Strata did); its reworks come back here (claude_tier)
        cfg = lane["bug_queue"]
        max_pct = float(cfg.get("max_pct", 0.5))
        skip = luna_easy_attempted()
        for item in smart_order(load_queue()):
            s = slug(item["cluster"])
            if is_feature(item) or s in skip or s in claimed or (D["state"] / f"{s}.json").exists():
                continue
            if (item.get("bug_pct") if item.get("bug_pct") is not None else 0.5) > max_pct:
                continue
            item = {**norm_item(item), "claude_tier": cfg.get("tier", "bug-easy")}
            return {"item": item, "round": 1, "notes": None, "lane": lane["name"]}, None
        return None, None
    taken = set().union(*[lane_holding(l["name"]) for l in claude_lanes()]) | set(
        (ROOT / "LANE-CLAIMED").read_text().split() if (ROOT / "LANE-CLAIMED").exists() else [])
    tiers = lane.get("tiers") or list(CLAUDE_TIERS)
    for item in jsonl(ROOT / "CLAUDE-QUEUE.jsonl"):
        q = item.get("cluster") or item.get("queue_id")
        if (item.get("feature_tier") not in tiers or q in claimed or q in taken
                or (D["state"] / f"{slug(q)}.json").exists() or not item.get("acceptance")):
            continue
        if not int(item.get("round_base") or 0):
            queue_rows = [r for r in jsonl(ROOT / "CLAUDE-QUEUE.jsonl") if r.get("feature_tier") in tiers
                          and str(r.get("cluster") or r.get("queue_id")) not in taken]
            mates = feature_batch({**item, "cluster": q}, queue_rows, claimed)
            if mates:
                item = {**item, "batch": mates}
                claimed.update(slug(m["cluster"]) for m in mates)
        return {"item": item, "round": int(item.get("round_base") or 0) + 1,
                "notes": item.get("escalation_notes"), "lane": lane["name"]}, None
    return None, None


def next_item(claimed=frozenset(), allow_rework=True, features_ok=True, bug_reworks_ok=None, engine=None):
    """Feature reworks, new features, bug reworks/ports, then the next untouched cluster; never one in flight.
    features_ok=False (Luna fixers, owner 2026-10-16: Luna back on BUGS only) skips feature units and their reworks;
    bug_reworks_ok (default: allow_rework) lets the OpenCode engine leave bug reworks to the Luna fixers."""
    if bug_reworks_ok is None:
        bug_reworks_ok = allow_rework
    reworks = [r for r in sorted(D["rework"].glob("*.json"), key=os.path.getmtime) if r.stem not in claimed]
    jobs = [(r, json.loads(r.read_text())) for r in reworks]
    features = load_features() if features_ok else []
    if features_ok:
        for r, job in jobs:
            if is_feature(job.get("item")) and not is_claude_item(job.get("item")):
                if features_ok == "gated" and not tier_model_free(job["item"].get("feature_tier")):
                    continue     # its tier's models are busy
                job["item"] = norm_item(job["item"])
                return job, r
    for item in features:
        s = slug(item["cluster"])
        if features_ok == "gated" and not tier_model_free(item.get("feature_tier")):
            continue             # its tier's models are busy: leave it for later, don't block the slot
        if s not in claimed and not (D["state"] / f"{s}.json").exists():
            # an escalated unit continues its round count on its branch, with the last review as notes
            if not int(item.get("round_base") or 0):
                mates = feature_batch(item, features, claimed)
                if mates:
                    item = {**item, "batch": mates}
                    if isinstance(claimed, set):
                        claimed.update(slug(m["cluster"]) for m in mates)
            return {"item": item, "round": int(item.get("round_base") or 0) + 1,
                    "notes": item.get("escalation_notes")}, None
    if (ROOT / "BUGS-HOLD").exists() and engine != "qwen":
        # owner 2026-10-19: "leave the bug queue on hold" until DeepSeek V4.1 Flash 1017 lands; features continue.
        # Local Strata (free, own GPU) is exempt: owner "start the strata session on fixes, it seemed to work quite well"
        return None, None
    if bug_reworks_ok:
        for r, job in jobs:
            it = job.get("item") or {}
            if it.get("claude_tier") or (engine == "qwen" and it.get("escalation_level")):
                continue
            if not is_feature(job.get("item")):
                job["item"] = norm_item(job["item"])
                return job, r
    skip = luna_easy_attempted()
    for item in smart_order(load_queue()):
        s = slug(item["cluster"])
        if s in skip or s in claimed or (D["state"] / f"{s}.json").exists():
            continue
        if engine == "qwen" and (item.get("bug_pct") if item.get("bug_pct") is not None else 0.5) > STRATA_MAX_PCT:
            continue          # the harder half waits for the stronger fixers
        group = root_cause_groups().get(str(item["cluster"])) if engine != "qwen" else None
        if group and not is_feature(item):
            # a judged root-cause group: one job, one shared fix (Strata keeps to same-file batches)
            by_cluster = {str(i["cluster"]): i for i in load_queue()}
            mates = []
            for c in group["members"]:
                o = slug(c)
                if c == str(item["cluster"]) or c not in by_cluster or o in skip or o in claimed or (D["state"] / f"{o}.json").exists():
                    continue
                mates.append(compact(by_cluster[c]))
                if len(mates) >= ROOT_CAUSE_MAX - 1:
                    break
            if mates:
                item = {**item, "batch": mates, "root_cause": group["id"]}
                if isinstance(claimed, set):
                    claimed.update(slug(m["cluster"]) for m in mates)
                return {"item": item, "round": 1, "notes": None}, None
        if batch_max(engine) > 1 and bug_file(item) and not is_feature(item):
            mates = []
            for other in smart_order(load_queue()):
                o = slug(other["cluster"])
                if (other is item or o == s or bug_file(other) != bug_file(item) or o in skip or o in claimed
                        or (D["state"] / f"{o}.json").exists()):
                    continue
                if engine == "qwen" and (other.get("bug_pct") if other.get("bug_pct") is not None else 0.5) > STRATA_MAX_PCT:
                    continue
                mates.append(compact(other))
                if len(mates) >= batch_max(engine) - 1:
                    break
            if mates:
                item = {**item, "batch": mates}
                if isinstance(claimed, set):
                    claimed.update(slug(m["cluster"]) for m in mates)
        return {"item": item, "round": 1, "notes": None}, None
    return None, None


# ------------------------------------------------------------------ worktree + build
CACHE_KEYS = re.compile(r"^((?:CMAKE_BUILD_TYPE|CMAKE_EXE_LINKER_FLAGS|TARGET_UARCH|USE_[A-Z0-9_]+|WANT_[A-Z0-9_]+)):[A-Z]+=(.*)$")
FALLBACK = ["-DCMAKE_BUILD_TYPE=RelWithDebInfo", "-DUSE_WERROR=ON", "-DTARGET_UARCH=official",
            "-DUSE_COMPILE_CACHE=ON", "-DWANT_DEBUG_CPACK=ON", "-DWANT_VST3=ON", "-DWANT_CLAP=ON",
            "-DWANT_VST3_TEST_INSTRUMENT=ON", "-DWANT_QT6=ON", "-DCMAKE_EXE_LINKER_FLAGS=-Wl,--strip-debug"]


def configure_args(lane_tree):
    fixed = ROOT / "FIX-CMAKE-ARGS"   # CachyOS (2026-10-14): one argument per line, '#' comments
    if fixed.exists():
        return [l.strip() for l in fixed.read_text().splitlines() if l.strip() and not l.startswith("#")]
    cache = Path(lane_tree) / "build/CMakeCache.txt"
    if not cache.exists():
        return FALLBACK
    args = []
    for line in cache.read_text(errors="replace").splitlines():
        m = CACHE_KEYS.match(line)
        if m and m.group(1) != "WANT_WASM":  # WASM needs a per-tree Wasmtime bootstrap
            args.append(f"-D{m.group(1)}={m.group(2)}")
    return args or FALLBACK


def run_logged(cmd, logfile, cwd=None, env=None):
    with open(logfile, "a") as lf:
        lf.write(f"\n$ {' '.join(map(str, cmd))}\n")
        lf.flush()
        return subprocess.run(cmd, cwd=cwd, env=env, stdout=lf, stderr=subprocess.STDOUT).returncode


def slot_build(build_dir, target, logfile):
    """Share root-build admission and compiler tokens with the agent shims."""
    return run_logged([sys.executable, str(BUNNY / "build_guard.py"), "--",
                       CMAKE, "--build", str(build_dir), "--target", target],
                      logfile, env=build_env())


def prepare_tree(item, run_dir):
    """Fresh worktree on bugs/qwen-<cluster> off the lane tip, configured and with zene built."""
    s = slug(item["cluster"])
    wt = PROG / (f"zene-feat-{s.lower()}" if is_feature(item) else f"zene-qwen-{s}")
    branch = f"feat/050-{s}-oc" if is_feature(item) else f"bugs/qwen-{s}"
    if item.get("integration"):
        wt, branch = integration_jobs.tree(item, PROG)
    blog = run_dir / "build.log"
    if not wt.exists():
        lane = Path(item["worktree"])
        git(lane, "worktree", "prune")
        if git(lane, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            git(lane, "worktree", "add", str(wt), branch, check=True)   # keep the earlier run's commits
        else:
            fix_base = (ROOT / "FIX-BASE").read_text().strip() if (ROOT / "FIX-BASE").exists() else ""
            pinned = (item.get("integration") or {}).get("target_commit")
            base = git(lane, "rev-parse", "--verify", pinned or fix_base or "HEAD", check=True)   # FIX-BASE: newest line
            git(lane, "worktree", "add", "-b", branch, str(wt), base, check=True)
    freshen(item, wt, branch, run_dir)
    configured = (wt / "build/CMakeCache.txt").exists() and \
        ((wt / "build/Makefile").exists() or (wt / "build/build.ninja").exists())
    if not configured:
        (wt / "build/CMakeCache.txt").unlink(missing_ok=True)   # a half-written cache from a failed configure
        # A build tree must own its CLAP headers and VST3 SDK (CLAUDE-READ-FIRST §1): copy, never point.
        donors = [Path(item["worktree"]) / "build"] + sorted(PROG.glob("zene-*/build"))
        for sub in ("clap", "vst3sdk"):
            if not (wt / "build" / sub).exists():
                src = next((d / sub for d in donors if (d / sub).is_dir()), None)
                if src is None:
                    return wt, branch, f"no {sub} checkout in any build tree to copy"
                shutil.copytree(src, wt / "build" / sub, symlinks=True)
        rc = run_logged([CMAKE, "-S", str(wt), "-B", str(wt / "build"), *configure_args(item["worktree"])],
                        blog, env=build_env())
        if rc:
            (wt / "build/CMakeCache.txt").unlink(missing_ok=True)
            rc = run_logged([CMAKE, "-S", str(wt), "-B", str(wt / "build"), *FALLBACK], blog, env=build_env())
        if rc:
            (wt / "build/CMakeCache.txt").unlink(missing_ok=True)   # so the next attempt configures again
            return wt, branch, f"configure failed (EXIT={rc}, {blog})"
    rc = slot_build(wt / "build", "zene", blog)
    return wt, branch, (f"zene build failed (EXIT={rc}, {blog})" if rc else None)


def freshen(item, wt, branch, run_dir):
    """2026-10-25: a reused branch (rework, retry, precheck bounce) is brought up to the newest train before the
    session, so the head QA accepts already sits on FIX-BASE and the merge train has nothing to re-integrate (651 of
    3,260 work sessions were integration of stale accepted patches). A conflict is left for the same warm session
    to resolve: freshen.txt in the run dir becomes a note at the top of its brief. Skipped for integration jobs
    (they pin their own base), dirty trees, and branches the train ever merged (merging a base that holds a revert
    of the branch would silently undo the fix)."""
    fx = ROOT / "FIX-BASE"
    if item.get("integration") or not fx.exists() or (ROOT / "FRESHEN-OFF").exists():
        return
    base = fx.read_text().strip()
    tip = git(wt, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if not tip or subprocess.run([GIT, "-C", str(wt), "merge-base", "--is-ancestor", tip, "HEAD"]).returncode == 0:
        return
    if git(wt, "status", "--porcelain", "--untracked-files=no"):
        return
    if git(wt, "log", "--oneline", "-1", "-F", f"--grep=Merge {branch} ", tip):
        return
    s = slug(item["cluster"])
    r = subprocess.run([GIT, "-C", str(wt), "merge", "--no-edit", "-m",
                        f"Merge {base} into {branch} (newest train, before the next round)\n\n"
                        "Co-Authored-By: Zene fix-loop agent <noreply@zene.invalid>", tip], capture_output=True, text=True)
    if r.returncode == 0:
        log("fix", f"{s}: branch brought up to {base}")
        return
    files = [l[3:] for l in git(wt, "status", "--porcelain").splitlines() if l[:2] in ("UU", "AA", "DU", "UD", "AU", "UA")]
    subprocess.run([GIT, "-C", str(wt), "merge", "--abort"], capture_output=True)
    log("fix", f"{s}: branch conflicts with {base} in {', '.join(files[:6]) or '?'} - left for the session")
    (run_dir / "freshen.txt").write_text(
        f"FIRST: your branch is behind the newest train `{base}` and merging it conflicts in: {', '.join(files) or 'see git'}.\n"
        f"Before anything else run `git merge {base}` in your worktree, resolve the conflicts keeping both your fix and the "
        f"train's changes, rebuild, and commit the merge. Then continue with the rest of this brief.\n")


def save_leftovers(wt, run_dir):
    status = git(wt, "status", "--porcelain")
    if not status:
        return None
    out = run_dir / "leftover"
    out.mkdir(parents=True, exist_ok=True)
    (out / "tracked.patch").write_text(git(wt, "diff", "HEAD"))
    for line in status.splitlines():
        if line.startswith("?? "):
            src = wt / line[3:]
            dst = out / "untracked" / line[3:]
            dst.parent.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
    subprocess.run([GIT, "-C", str(wt), "restore", "--source=HEAD", "--staged", "--worktree", "--", "."], capture_output=True)
    subprocess.run([GIT, "-C", str(wt), "clean", "-fdq", "--", ".", ":!build"], capture_output=True)
    return status


# ------------------------------------------------------------------ the Qwen fixer
def tests_covering(wt, rel):
    """Test files that include the source's header or are named after it, with their ctest names."""
    stem = Path(rel).stem
    if not stem:
        return []
    hits = []
    for t in sorted((Path(wt) / "tests").rglob("*.cpp")):
        try:
            text = t.read_text(errors="replace")
        except OSError:
            continue
        score = 2 if re.search(rf'#include\s+[<"]([^>"]*/)?{re.escape(stem)}\.h[>"]', text) else 0
        score += 1 if stem.lower() in t.stem.lower() else 0
        if score:
            hits.append((score, -len(text), t))
    hits.sort(reverse=True)
    return [(str(t.relative_to(wt)), t.stem) for _, _, t in hits[:3]]


def where_to_start(item, wt):
    """The code at the finding (as it is in this tree) and where its tests live: saves Qwen's first read turns."""
    rel = item.get("file") or ""
    path = Path(wt) / rel
    out = []
    if rel and path.is_file():
        lines = path.read_text(errors="replace").splitlines()
        line = int(item.get("line") or 0)
        if not 0 < line <= len(lines):   # no usable line: take it from the finding's own "where:" text
            m = re.search(rf"{re.escape(Path(rel).name)}:(\d+)", item.get("text", ""))
            line = int(m.group(1)) if m else 0
        if not 0 < line <= len(lines):   # else the first line naming a `code` term from the title or text
            terms = [t for t in re.findall(r"`([A-Za-z_][\w:.]{3,40})`", item.get("title", "") + item.get("text", ""))
                     if "/" not in t]
            line = next((i + 1 for i, l in enumerate(lines) if any(t.split("::")[-1] in l for t in terms)), 0)
        if not 0 < line <= len(lines):
            line = None                  # don't show the licence header: no excerpt is better than a wrong one
    if rel and path.is_file() and line:
        lo, hi = max(1, line - 30), min(len(lines), line + 30)
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(lo, hi + 1))[:4000]
        out.append(f"### {rel} lines {lo}-{hi} (current tree; the finding's line {item.get('line')} may have moved)\n"
                   f"```cpp\n{body}\n```")
    tests = tests_covering(wt, rel) if rel else []
    if tests:
        out.append("### Tests that already cover this file (put your regression in one of these if it fits)\n"
                   + "\n".join(f"- {f}  (ctest -R '^{name}$')" for f, name in tests))
    elif rel:
        out.append("### No existing test includes this file's header: add a new test next to the closest area's tests.")
    return "\n\n".join(out)


def compact(item):
    """Only what Qwen needs to act: the finding, where it points, and at most two short prior verdicts."""
    keep = {k: item[k] for k in ("cluster", "title", "file", "line", "text", "clef_tier", "difficulty") if k in item}
    if len(item.get("members") or []) > 1:
        keep["reported_times"] = len(item["members"])
    prior = [{"verdict": ev.get("verdict"), "note": (ev.get("text") or "")[:600]}
             for ev in (item.get("report_evidence") or [])[:2] if isinstance(ev, dict)]
    if prior:
        keep["earlier_attempts"] = prior
    return keep


# Condensed on purpose: a small local model loses the plot in long briefs ("context rot"). Every line was
# checked against AGENTS.md (rules 2-12, API traps, contracts) and CLAUDE-READ-FIRST §3-§4 on 2026-10-06.
RULES = """\
GIT: never push/fetch/pull/merge/rebase/stash/switch/reset --hard or rewrite history. `git add <explicit paths>` only.
TESTS: never weaken a test, assertion, gate or ratchet. Your regression must FAIL before the fix and PASS after.
  Change only the buggy behaviour (behaviour-preserving otherwise).
REALTIME: on audio-thread paths (process/render, play handles, mixer channels, anything per period):
  no allocation, no mutex, no logging, no allocating Qt calls.
NEW FILES: register product-new files in tests/fork-sources.txt; inherited files you change in
  tests/upstream-modifications.txt with a reason (the file must end with a newline). Same commit.
HEADERS: change declaration (.h) and definition (.cpp) together; rebuild every target that includes it.
BUILD/TEST: send ALL build and test output to a new file in your LOG FOLDER (given above), e.g.
  `cmake --build build --target zene <Test> > LOGS/build-1.log 2>&1; echo EXIT=$?`, and search logs with the
  fff grep tool (path = LOG FOLDER, one term: error, FAILED, Assertion), never by reading whole logs.
  Run ctest ONLY from build/tests (`ctest -R <Test> --output-on-failure > LOGS/test-1.log 2>&1; echo EXIT=$?`);
  0 tests run = error.
  Rebuild zene before believing a failure (stale binaries cause fake regressions).
  Env: QT_QPA_PLATFORM=offscreen is set; never set LMMS_PLUGIN_DIR. Never pkill/pgrep -f.
GATES before commit: `python3 ~/.local/share/zene-bunny-fix/qwen/feature_check.py <worktree>` (all gates; new
  functions CCN <= 10, no new file over 500 lines). Never re-anchor a gate.
COMMANDS: do not add new control commands for a bug fix (it moves the ControlCommandsSnapshot canary).
API TRAPS: SampleBuffer has no operator[] (use data()/size()); AudioEngine::baseSampleRate() not sampleRate;
  Qt6 has no QString::midRef (use mid); channel types are ch_cnt_t; Q_ASSERT compiles out - use QVERIFY in tests.
TEST HELPERS: tests that play notes call NotePlayHandleManager::init(); tests driving commands call
  ControlRegistry::setReady(true).
FACTS: C++20. Command args are schema-validated before the handler (ControlRegistry.cpp:251).
  Mixer channels are an unbounded vector (no MAX_MIXER_CHANNELS).
BY DESIGN - never "fix": append-only <z:provenance> save journal; undo coalescing + undone_commands;
  mastering.run async ack; boolean params refuse fractions; first save reports reversible:false;
  empty tempo maps persist; fx-id renumbering after removal; id:-1 replies to malformed wire lines.
SCRATCH: /tmp is RAM - never put scratch trees, extracted bases, build dirs or copies there. Use $TMPDIR (on disk) or
  your LOG FOLDER, and delete your scratch when done.
SEARCH: use the fff tools for code (path = your worktree) and for logs (path = LOG FOLDER).
CONTEXT: read only the functions you need (line ranges, not whole files); keep command output short."""


def feature_brief(item, wt, rnd, notes, logs):
    s = slug(item["cluster"])
    rework = ""
    if notes:
        rework = f"""
## FIRST: the review of your previous attempt (round {rnd - 1}) asked for changes
Your earlier commits are already on this branch. Address every point below, on top of them.
{notes}
"""
    unit = {k: item.get(k) for k in ("cluster", "title", "goal", "acceptance", "type", "size", "priority", "area",
                                      "why", "member_ids")}
    # Owner 2026-10-19: a scoping brief (feature_scope.py: read-only Luna, written before the unit is handed out)
    batch = item.get("batch") or []
    batch_text = ""
    if batch:
        ids = [str(item["cluster"])] + [str(b["cluster"]) for b in batch]
        batch_text = (f"""
## THIS JOB IS A FEATURE BATCH: {len(ids)} units that go in the same place ({', '.join(ids)})
Their scoping briefs name the same source files, so build them TOGETHER on this one branch: read every unit and brief
first, then design the shared pieces once (one model, one command family, one widget, one helper) so the code stays
coherent - no copy-paste per unit, no structure the next unit has to undo. Each unit still needs its own acceptance
tests, its own docs/050-features/<id>.md and its own commits (name the unit in each commit message). In
{REPORT_DIR}/cluster-{s}.md put ONE LINE PER UNIT, exactly `<id>: IMPLEMENTED` or `<id>: BLOCKED` (with the reason),
then each unit's acceptance table. A unit you cannot finish: report it BLOCKED and leave none of its half-done code on
the branch (it goes back to the queue on its own).
""" + "".join(f"\n### Unit {b['cluster']}\n```json\n{json.dumps({k: b.get(k) for k in ('cluster', 'title', 'goal', 'acceptance', 'type', 'size', 'priority', 'area', 'why', 'member_ids')}, ensure_ascii=False, indent=1)}\n```\n"
              + ((f"Scoping brief:\n{(BRIEFS / (str(b['cluster']) + '.md')).read_text()}\n") if (BRIEFS / (str(b['cluster']) + '.md')).exists() else "")
              for b in batch))
    scoped = PROG / "research/reviews/feature-briefs" / f"{s}.md"
    stage_note = ""
    if not scoped.exists() and re.match(r"Q050-\d{3}\.\d+$", s):
        # 2026-10-19: a stage of a split unit uses its parent's brief, which planned the split
        parent = s.rsplit(".", 1)[0]
        scoped = PROG / "research/reviews/feature-briefs" / f"{parent}.md"
        stage_note = (f"\nTHIS IS STAGE {item.get('stage')} of {item.get('stages')} of {parent} (the scoping brief below planned the "
                      f"split). Build ONLY this stage's acceptance criteria above, so that it is useful and mergeable on its own; "
                      f"leave the other stages for later units, but do not design anything they would have to undo.\n")
    scoping = stage_note + (f"\n## Scoping brief for this unit (a reviewer read the code and the specs for you; start from it, but "
               f"verify each pointer: the line numbers are from the base and may have moved)\n{scoped.read_text()}\n"
               if scoped.exists() else "")
    # 2026-10-19 brief v2 (owner: "optimise it"): the unit and its scoping brief first, then one definition of done
    # built from the reviewers' actual rework reasons, the completion check, the report, and reference material last.
    v2 = FEATURE_BRIEF_V2.read_text().replace("{PROG}", str(PROG)).replace("{WT}", str(wt)).replace("{ID}", str(item["cluster"])) \
        .replace("{LOGS}", str(logs)).replace("{TRAILER}", "Zene fix-loop agent <noreply@zene.invalid>")
    return f"""You implement {'ONE queue unit' if not batch else f'{1 + len(batch)} queue units together'} of Zene Studio, an LMMS-derived DAW (C++20, Qt) with an agent
control surface (JSON-RPC commands, each exposed to agents through MCP). You work alone; nobody can answer questions.
Worktree: {wt} (branch feat/050-{s}-oc, cut from the current train {(ROOT / "FIX-BASE").read_text().strip()}). Work ONLY in it.
Its build tree is configured and `zene` is built. Logs and scratch: {logs} (a NEW numbered file per build/test log).
{rework}
## Your queue unit
```json
{json.dumps(unit, ensure_ascii=False, indent=1)}
```
{scoping}{batch_text}
{v2}"""


def fix_brief(item, wt, rnd, notes, logs="LOGS"):
    if is_feature(item):
        return feature_brief(item, wt, rnd, notes, logs)
    s = slug(item["cluster"])
    rework = ""
    if notes and str(notes).startswith(("PORT JOB", "TEST FIX JOB")):
        # 2026-10-19: jobs the merge train files (a fix to re-apply, a broken test to fix) are not review rounds
        rework = f"""
## FIRST: what this job is (filed by the merge train - read it before the finding below)
{notes}
"""
    elif notes:
        rework = f"""
## FIRST: Luna's QA review of your previous attempt (round {rnd - 1}) asked for changes
Your earlier commits are already on this branch. Address every point below, on top of them.
{notes}
"""
    # Owner 2026-10-19 ("make sure the bug fixes are scoped well"): feature_scope.py's read-only Luna bug brief
    bug_brief = PROG / "research/reviews/bug-briefs" / f"{s}.md"
    scoping = (f"\n## Scoping brief (a reviewer located this bug in the current code for you; verify before relying on it)\n"
               f"{bug_brief.read_text()}\n" if bug_brief.exists() else "")
    batch = item.get("batch") or []
    batch_text = ""
    if batch:
        ids = [str(item["cluster"])] + [str(b["cluster"]) for b in batch]
        head = (f"""
## THIS JOB IS A ROOT-CAUSE GROUP ({item['root_cause']}): {len(ids)} bugs that likely share ONE underlying cause
An independent judge (Clef, measured ~80 % precise) found these reports probably come from one shared defect - one
function, helper, data structure or duplicated piece of logic. FIRST find that shared cause and fix it ONCE, in the
place all of them go through (a shared helper is better than {len(ids)} local patches). A member that turns out NOT to
share it: fix it separately here if it is small, otherwise report it NEEDS-CONTEXT (it goes back to the queue).
""" if item.get("root_cause") else f"""
## THIS JOB IS A SAME-FILE BATCH: {len(ids)} bugs in {bug_file(item)}
""")
        batch_text = (head + f"""Fix every bug below in this one tree and build (the first is "The finding" further down). Treat each on its own:
its own decision (REAL / ALREADY-FIXED / FALSE-POSITIVE), its own regression test that FAILS without its fix, its own
RED/GREEN proof (step 4b, per bug). One commit per bug is best; name the cluster in each commit message.
In {REPORT_DIR}/cluster-{s}.md put ONE LINE PER BUG, exactly `<cluster>: <VERDICT>` (VERDICT = FIXED / ALREADY-FIXED /
FALSE-POSITIVE / NEEDS-CONTEXT / BLOCKED) for {', '.join(ids)}, each followed by its evidence and commands. A bug
you cannot finish is NEEDS-CONTEXT or BLOCKED - it goes back to the queue without holding up the others.
""" + "".join(f"\n### Also in this batch: {b['cluster']}\n```json\n{json.dumps(b, ensure_ascii=False, indent=1)}\n```\n" for b in batch))
    # 2026-10-19 bug brief v2 (owner: "build it"): the finding first, then a definition of done built from the reviewers'
    # actual rejections (incomplete fix / siblings missed, collateral damage, non-deterministic tests, unproven
    # already-fixed claims, registration), the procedure, and the rules last.
    return f"""You are fixing {'ONE bug' if not batch else f'{1 + len(batch)} bugs'} in Zene Studio (C++20 / Qt DAW, LMMS fork). You work alone.
Worktree: {wt}  (branch bugs/qwen-{s}). Work ONLY inside it. Its build tree {wt}/build is configured and `zene` is built.
LOG FOLDER: {logs}   (write every build/test log here as a NEW numbered file; search it with fff grep)
{rework}
## The finding (from Luna's patrol; historical line numbers)
```json
{json.dumps(compact(item), ensure_ascii=False, indent=1)}
```
{batch_text}{scoping}
## Where to start
{where_to_start(item, wt)}

## Definition of done (reviewers send fixes back for exactly these)
1. A decision with evidence: REAL, ALREADY-FIXED or FALSE-POSITIVE. A not-real verdict cites the code (file:line) or the
   intended contract that shows it - an unproven "already fixed" is sent back.
2. A deterministic regression test that FAILS without your fix and PASSES with it (step 4b proves it). No wall-clock
   timing, sleeps or races in the test: inject timestamps, use a test seam or a synchronisation hook instead.
3. The fix is at the cause and complete: check every caller and every sibling of what you change (the matching command
   for many items, the other path into the same function, the wider type the value flows into) and keep them consistent.
4. No collateral damage: old projects and saved data load exactly as before; a limit or check you add must not hit other
   callers that legitimately exceed it; behaviour outside the bug does not change.
5. Realtime, undo and commands: nothing new on the audio thread allocates, frees, locks, waits or logs; undo stays one
   honest step; do not add new control commands for a bug fix.
6. Registration and gates pass (step 5).

## Procedure
1. Read the finding and the code it points at (line numbers may have moved). Search with the fff tools (path={wt}) or
   `rg -n` (output is capped).
2. Decide: is it REAL, ALREADY-FIXED on this branch, or a FALSE-POSITIVE? If not real, skip to step 6 with the evidence.
3. Before editing, confirm the regression target/dependency exists and write one complete test matrix: normal path,
   failure/exhaustion/reconfiguration paths, audio-thread constraints, and supported-platform build requirements.
   If a prerequisite is absent, report the concrete missing capability instead of repeatedly attempting a fix.
   Write the regression test first (an existing test file for that area if one exists). Build it and run it from
   build/tests, logging both to the LOG FOLDER - it must FAIL (RED).
4. Make the smallest correct fix at the cause. Rebuild and rerun: it must PASS (GREEN), and the other tests in that
   ctest -R family must still pass. If you changed a header, rebuild every target that includes it.
4b. PROVE THE TEST CATCHES THE BUG: `git diff -- <the product files you changed, not tests> > {logs}/fix.patch && git apply -R {logs}/fix.patch`,
   rebuild, run your test: it must FAIL. Then `git apply {logs}/fix.patch`, rebuild, run: it must PASS. Record both
   commands and EXIT codes. If it passes without the fix, the test is wrong: fix it.
5. Gates: `python3 {ROOT}/feature_check.py {wt}` - one PASS/FAIL table (complexity and file-length only fail on items new
   in your files; they are red on the train on purpose).
6. Write {REPORT_DIR}/cluster-{s}.md: first line the verdict FIXED / ALREADY-FIXED / FALSE-POSITIVE / NEEDS-CONTEXT /
   BLOCKED, then the file:line evidence and the exact commands you ran with their EXIT codes (copy them; never claim a
   result you did not see). Commit it with the fix, message naming cluster {item['cluster']}.
7. Leave `git status` clean. If you are stuck (platform, design or concurrency question), report NEEDS-CONTEXT or
   BLOCKED and explain - never guess a fix or fake a result.
8. Run the completion check: `python3 {ROOT}/submit_check.py {wt} {item['cluster']}`. Fix every FAIL and answer every "?"
   item in a `## Self-check` section of the report with the evidence. The loop runs the same check when your session
   ends and sends unfinished work straight back to you.

## Rules and reference (condensed from AGENTS.md + plans/CLAUDE-READ-FIRST.md; open those only to look something up)
{RULES}
{adaptive.feedback()}"""


def mcode_config():
    h = strata_health() or {}
    ctx = int(h.get("max_context") or 65536)
    cfg = ROOT / "qwen-mcode.yaml"
    cfg.write_text(f"""# generated by qwenfix.py from {STRATA}/health - edits are overwritten
agents:
  default:
    modelConfigId: {QWEN_MODEL}
    skills: []        # drops the user's own skills from the catalog (~600 tokens); built-in ones stay
defaultModelContextWindow: {ctx}
permissionMode: bypassPermissions
logLevel: info
custom_provider:
  strata:
    name: strata
    kind: custom
    api: openai-completions
    enabled: true
    options:
      baseURL: {TRIM_URL}/v1
      apiKey: local
      authMode: api-key
    models:
      qwen3.8-flash-next-coder:
        attachment: false
        limit: {{context: {ctx}, output: {QWEN_MAX_OUTPUT}}}
        modalities: {{input: [text], output: [text]}}
        reasoning: true
        thinking: {{defaultEffort: {QWEN_EFFORT}, effortOptions: [none, low, medium, high]}}   # Strata's reasoning_effort values
        thinking_config: {{default_value: "true", mode: switchable}}
        tool_call: true
""")
    return cfg


def session_command(engine, wt, run_dir):
    if engine == "claude":
        return [str(HAIKU_EXEC), "-C", str(wt), "-o", str(run_dir / "final.md"), "-"]
    if engine == "luna":
        resume = issue_ledger.read(run_dir / "luna-resume.json") if light_engine() == "luna" else {}
        cmd = ["codex", "exec", "-c", 'model_reasoning_effort="max"', "-c", 'approval_policy="never"',
               "-s", "danger-full-access", "-C", str(wt)]
        if resume.get("session"):
            cmd += ["resume", resume["session"]]
        return light_cmd(cmd + ["-m", "gpt-6-luna", "-o", str(run_dir / "final.md"), "-"])
    if engine == "opencode":
        cmd = [sys.executable, str(ROOT / "opencode_free.py"), "run",
               "--cwd", str(wt), "--out", str(run_dir / "final.md")]
        prefer = run_dir / "prefer-models.txt"
        exclude = run_dir / "exclude-models.txt"
        kind = run_dir / "job-kind.txt"
        return cmd + (["--prefer", prefer.read_text().strip()] if prefer.exists() else []) + (
            ["--exclude", exclude.read_text().strip()] if exclude.exists() else []) + (
            ["--kind", kind.read_text().strip()] if kind.exists() else [])
    return ["mcode", "exec", "--config", str(mcode_config()), "--model", QWEN_MODEL,
            "--permission", "full", "--prompt-mode", "coding", "--effort", QWEN_EFFORT, "--cwd", str(wt),
            "--timeout", FIX_TIMEOUT, "--max-steps", "250", "-o", str(run_dir / "final.md"), "--input", "-"]


def haiku_effort(rnd):
    """Owner, 2026-10-14: Haiku fixes at high; a rework round gets xhigh. HAIKU_EFFORT forces one level."""
    return os.environ.get("HAIKU_EFFORT") or ("high" if rnd <= 1 else "xhigh")


# Owner 2026-10-21: "allow the agents to add more time if they need it ... no reason to hard cap it". A fix session has
# no wall-clock limit: it runs while it shows activity (its session log or its run folder's logs change) and is stopped
# only after SESSION_QUIET_MIN without any. A session still going after SESSION_LONG_H is reported to the owner, not
# stopped (the OpenCode / DeepSeek Harness / MCode runners keep their own 25-minute silence watchdog as well).
SESSION_QUIET_MIN = 45
SESSION_LONG_H = 6


def run_session(cmd, brief, lf, env, run_dir, s, name):
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=lf, stderr=subprocess.STDOUT, text=True, env=env,
                            start_new_session=True)
    try:
        proc.stdin.write(brief)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    t0, told = time.time(), False

    def last_activity():
        paths = [run_dir / "session.log"] + list((run_dir / "logs").glob("**/*"))
        return max((p.stat().st_mtime for p in paths if p.exists()), default=t0)

    while proc.poll() is None:
        time.sleep(30)
        quiet = (time.time() - max(last_activity(), t0)) / 60
        if quiet > SESSION_QUIET_MIN:
            log("fix", f"{s}: {name} session silent for {quiet:.0f} min - stopped")
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=60)
                    break
                except subprocess.TimeoutExpired:
                    continue
            return 124
        if not told and time.time() - t0 > SESSION_LONG_H * 3600:
            told = True
            log("fix", f"{s}: {name} session still active after {SESSION_LONG_H} h - left running")
            subprocess.run(["notify-me", f"Zene: {s} ({name}) has been working for {SESSION_LONG_H} h and is still "
                                         "active - left running (no time cap)."], capture_output=True)
    return proc.returncode


def fix_one(job, rework_file, engine="qwen"):
    item, rnd = job["item"], job["round"]
    s = slug(item["cluster"])
    run_dir = D["runs"] / f"{s}.r{rnd}"
    run_dir.mkdir(exist_ok=True)
    rec = {"cluster": item["cluster"], "round": rnd, "title": item.get("title"),
           "tier": item.get("clef_tier", "unclassified"), "lane_tree": item["worktree"], "started": now()}
    log("fix", f"{s} r{rnd} [{rec['tier']}] {item.get('title', '')[:90]}"
        + (f" (+{len(item['batch'])} same-file: {', '.join(str(b['cluster']) for b in item['batch'])})" if item.get("batch") else ""))
    for b in item.get("batch") or []:     # reserve the companions so no other worker takes them
        write_json(D["state"] / f"{slug(b['cluster'])}.json", {"cluster": b["cluster"], "outcome": "batched", "batch_of": item["cluster"]})
    wt, branch, err = prepare_tree(item, run_dir)
    rec.update(worktree=str(wt), branch=branch)
    if err:
        rec.update(outcome="build-failed", error=err, finished=now(), issue_id=issue_ledger.issue_id(item))
        write_json(run_dir / "record.json", rec)
        workflow_runtime.build_failed(job, wt, err, globals())
        return
    if save_leftovers(wt, run_dir / "pre"):
        log("fix", f"{s}: tree was dirty before the session; saved to {run_dir}/pre")
    head0 = git(wt, "rev-parse", "HEAD")
    if item.get("integration"):
        integration_jobs.apply(item, wt, run_dir)
    if (run_dir / "freshen.txt").exists():     # freshen() left a train conflict for this session
        job = {**job, "notes": (run_dir / "freshen.txt").read_text() + "\n" + (job.get("notes") or "")}
    (run_dir / "logs").mkdir(exist_ok=True)   # outside any git repo, so fff indexes it (worktree build/ is gitignored)
    brief = fix_brief(item, wt, rnd, job.get("notes"), logs=run_dir / "logs")
    (run_dir / "brief.md").write_text(brief)
    # owner 2026-10-20: a model's sessions can be split between features and bugs (MULTI-SESSION.json kind_sessions)
    (run_dir / "job-kind.txt").write_text("feature" if is_feature(item) else "bug")
    fm = feature_models()
    if fm:
        configured = json.loads((ROOT / "OPENCODE-FREE.json").read_text()).get("models", [])
        if is_feature(item):     # features: only the models of the unit's tier
            mine = eval_order(feature_tiers().get(item.get("feature_tier"), fm))
            (run_dir / "prefer-models.txt").write_text(",".join(mine))
            (run_dir / "exclude-models.txt").write_text(",".join(m for m in configured if m not in mine))
        else:                    # bugs: every other model; the bug trial's least-sampled idle model first
            # owner 2026-10-18: BUG-PREFER.json models (beam, DeepSeek V4.1 Flash 1017) run constantly on bugs;
            # 2026-10-19 owner "deepseek shouldn't be excluded from bug fixing": a BUG-PREFER model also on a feature
            # tier builds both, so only the feature models NOT in BUG-PREFER are kept off bugs
            try:
                bug_prefer = json.loads((ROOT / "BUG-PREFER.json").read_text()).get("models", [])
            except (OSError, ValueError):
                bug_prefer = []
            (run_dir / "exclude-models.txt").write_text(",".join(m for m in fm if m not in bug_prefer))
            pick = trial_pick()
            prefer = [pick] if pick else []
            prefer += [m for m in bug_prefer if m not in prefer]
            if prefer:
                (run_dir / "prefer-models.txt").write_text(",".join(prefer))
    else:
        if item.get("prefer_models"):
            (run_dir / "prefer-models.txt").write_text(item["prefer_models"])
        elif not is_feature(item):
            # 2026-10-19: with no feature tiers left, bugs still take BUG-PREFER.json first (MiniMax M3.1 free, beam)
            try:
                prefer = json.loads((ROOT / "BUG-PREFER.json").read_text()).get("models", [])
            except (OSError, ValueError):
                prefer = []
            if prefer:
                (run_dir / "prefer-models.txt").write_text(",".join(prefer))
        if is_feature(item) and item.get("size") != "S":
            # owner 2026-10-16: DeepSeek Free has a 200k context - small jobs only (bugs and size-S features)
            (run_dir / "exclude-models.txt").write_text(SMALL_CONTEXT_MODELS)
    if engine == "opencode" and job.get("resume"):
        # continue the implementing session: it already holds the brief and its own work, so send only the review
        short = (f"## The review of your work (round {rnd - 1}) asked for changes\n"
                 f"Your commits are on the branch. Address every point below on top of them, then rebuild, rerun the "
                 f"targeted tests and the gates, commit, and finish with the same final report as before (the verdict "
                 f"word on the last line).\n\n{job.get('notes') or ''}\n")
        write_json(run_dir / "resume.json", {**job["resume"], "brief": short})
        (run_dir / "prefer-models.txt").write_text(job["resume"]["model"])
        rec["resumed_session"] = job["resume"]["session"]
    lane = next((l for l in claude_lanes() if l["name"] == job.get("lane")), None) if engine == "claude" else None
    if engine == "claude":
        if lane is None:
            raise RuntimeError(f"lane {job.get('lane')!r} is not in LANES.json")
        rec.update(lane=lane["name"], effort=lane.get("effort", "high"))
        trailer = {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5"}.get(lane["model"], lane["model"])
        brief = brief.replace("Co-Authored-By: Zene fix-loop agent <noreply@zene.invalid>", f"Co-Authored-By: {trailer} <noreply@anthropic.com>")
        brief += ("\n## This run\nYou are a Claude lane of the feature loop. Work at -j4 at most (other lanes, Luna and the fix "
                  "loop share the 30 GB machine). Delete nothing outside your worktree. Commit on the branch; never push.\n")
        res = job.get("resume") or {}
        if res.get("engine") == "claude" and res.get("session") and res.get("model") == lane["model"]:
            brief = (f"## The review of your work (round {rnd - 1}) asked for changes\nYour commits are on the branch. "
                     f"Address every point below on top of them, then rebuild, rerun the targeted tests and the gates, "
                     f"commit, update the report and finish with the same final report as before.\n\n{job.get('notes') or ''}\n")
            rec["resumed_session"] = res["session"]
        (run_dir / "brief.md").write_text(brief)
    if engine == "luna" and light_engine() == "luna" and (job.get("resume") or {}).get("engine") == "luna":
        res = job["resume"]
        if res.get("session") and res.get("model") == light_model():
            write_json(run_dir / "luna-resume.json", res)
            rec["resumed_session"] = res["session"]
            brief = (f"Continue issue {issue_ledger.issue_id(item)} on the existing branch; preserve correct work. "
                     f"Address all evidenced blockers together, rebuild the focused targets, and update the same report.\n"
                     + str(job.get("notes") or ""))
            (run_dir / "brief.md").write_text(brief)
    name = (f"Claude lane {lane['name']}" if engine == "claude" else light_name() if engine == "luna"
            else "OpenCode Free" if engine == "opencode" else "Qwen")
    rec["engine"] = engine
    if engine == "luna":
        rec["model"] = light_model()
        rec["harness"] = "codex gpt-6-luna max" if light_engine() == "luna" else f"haiku-exec claude-haiku-5-5 {haiku_effort(rnd)}"
    elif engine == "opencode":
        rec["model"] = "opencode/free"
        rec["harness"] = "opencode run (verified free models only)"
    elif engine == "claude":
        rec["model"] = lane["model"]
        rec["harness"] = f"haiku-exec {lane['model']} {lane.get('effort', 'high')} (Claude lane {lane['name']})"
    else:
        rec["model"] = QWEN_MODEL
        rec["harness"] = "mcode qwen/strata"
    log("fix", f"{s}: tree ready ({wt.name}), {name} session start")
    workflow_runtime.session_started(job, run_dir, rec.get("model"))
    t0 = time.time()
    with open(run_dir / "session.log", "w") as lf:
        try:
            env = session_env()
            if engine == "luna" and light_engine() == "haiku":
                env["HAIKU_EFFORT"] = haiku_effort(rnd)
            if engine == "claude":
                env.update(HAIKU_MODEL=lane["model"], HAIKU_EFFORT=lane.get("effort", "high"), HAIKU_FALLBACK="",
                           HAIKU_PERSIST="1", HAIKU_SESSION_OUT=str(run_dir / "claude-session.json"),
                           HAIKU_RESUME=rec.get("resumed_session", ""))
            rc = run_session(session_command(engine, wt, run_dir), brief, lf, env, run_dir, s, name)
        except OSError:
            rc = 124
    if engine == "luna" and light_engine() == "luna":
        workflow_runtime.capture_luna(run_dir, light_model())
    head1 = git(wt, "rev-parse", "HEAD")
    if engine == "opencode":
        model_record = run_dir / "opencode-model.json"
        if model_record.exists():
            selected = json.loads(model_record.read_text())
            rec.update(model=selected["model"], model_name=selected["name"], free_cost_verified=selected["verified_cost"])
        if rc == 75 and head1 == head0 and not git(wt, "status", "--porcelain"):
            arch = D["runs"] / "_free-limit"
            arch.mkdir(exist_ok=True)
            shutil.move(str(run_dir), str(arch / f"{run_dir.name}-{int(time.time())}"))
            raise FreeModelLimit(f"{s}: free endpoints unavailable; unchanged job remains queued")
    if engine == "claude" and rc != 0 and head1 == head0 and note_luna_limit(run_dir / "session.log", CLAUDE_LIMIT):
        save_leftovers(wt, run_dir)
        arch = D["runs"] / "_usage-limit"
        arch.mkdir(exist_ok=True)
        shutil.move(str(run_dir), str(arch / f"{run_dir.name}-{int(time.time())}"))
        if not rework_file:           # a new unit: keep it with its lane, unchanged, for after the limit
            write_json(D["rework"] / f"{s}.json", {k: v for k, v in job.items()})
        raise LunaLimit(f"{s}: Claude usage limit - not an attempt; lane {lane['name']} waits until {paused_until_in(CLAUDE_LIMIT):%H:%M}")
    if engine == "luna" and rc != 0 and head1 == head0 and note_luna_limit(run_dir / "session.log"):
        save_leftovers(wt, run_dir)
        arch = D["runs"] / "_usage-limit"
        arch.mkdir(exist_ok=True)
        shutil.move(str(run_dir), str(arch / f"{run_dir.name}-{int(time.time())}"))
        raise LunaLimit(f"{s}: {light_name()} usage limit - not an attempt; back in the queue")
    if head1 != head0:
        hbase = git(wt, "merge-base", "HEAD", (ROOT / "FIX-BASE").read_text().strip()) or head0
        try:
            rec["housekeeping"] = workflow_evidence.housekeeping(wt, hbase, item)
        except Exception as e:   # registry upkeep is a convenience: its failure must not park the session's work
            rec["housekeeping_error"] = repr(e)[:500]
            log("fix", f"{s}: registry housekeeping failed ({e!r}); the work goes on to the checks without it")
        head1 = git(wt, "rev-parse", "HEAD")
    commits = git(wt, "log", "--format=%h %s", f"{head0}..{head1}").splitlines() if head1 != head0 else []
    leftovers = save_leftovers(wt, run_dir)
    report = wt / REPORT_DIR / f"cluster-{s}.md"
    verdict = None
    if report.exists():
        m = re.search(r"\b(IMPLEMENTED|FIXED|ALREADY-FIXED|FALSE-POSITIVE|NEEDS-CONTEXT|BLOCKED)\b", report.read_text())
        verdict = m.group(1) if m else None
    changed = git(wt, "diff", "--name-only", f"{head0}..{head1}").splitlines() if commits else []
    code_changed = [f for f in changed if not f.startswith(REPORT_DIR)]
    if item.get("integration") and code_changed:
        rec["integration_outside_patch"] = integration_jobs.outside_patch(item, wt, code_changed)
    rec.update(rc=rc, minutes=round((time.time() - t0) / 60, 1), head_before=head0, head_after=head1,
               commits=commits, files=changed, leftovers=bool(leftovers), qwen_verdict=verdict, finished=now())
    if item.get("batch"):
        bv = batch_verdicts(report.read_text(), item) if report.exists() else {}
        rec["batch_verdicts"] = bv
        rec["batch_size"] = 1 + len(item["batch"])
        if commits:      # companions not claimed as handled go back to the queue now
            keep = [b for b in item["batch"] if bv.get(str(b["cluster"])) in ("FIXED", "ALREADY-FIXED", "FALSE-POSITIVE", "IMPLEMENTED")]
            for b in item["batch"]:
                if b not in keep:
                    (D["state"] / f"{slug(b['cluster'])}.json").unlink(missing_ok=True)
            item = {**item, "batch": keep}
            if not keep:
                item.pop("batch")
    write_json(run_dir / "record.json", rec)
    if rework_file:
        rework_file.unlink()
    if commits and not job.get("precheck_bounced"):
        # owner 2026-10-19: "before they submit it have an automated prompt feedback asking if they did a list of stuff".
        # The same completion check the brief tells the builder to run; a mechanical FAIL goes straight back to the same
        # session (once per submission, not counted as a review round) before a reviewer sees it.
        try:
            import submit_check
            res, asks = submit_check.check(wt, item["cluster"], item)
            fails = [r for r in res if r[0] == "FAIL"]
        except Exception as e:
            fails, asks = [], []
            log("fix", f"{s}: pre-submit check could not run ({e!r}) - sent to QA unchecked")
        if fails:
            notes = ("AUTOMATED PRE-REVIEW CHECK: your work is not complete yet, so it has NOT gone to the reviewers. "
                     "Fix every FAIL below, answer every ? item in the report's `## Self-check` section, commit, then finish "
                     "with the same final report.\n\n" + submit_check.render(res, asks, item["cluster"]))
            bounce = {"item": {**item, "round_base": int(item.get("round_base") or 0) + 1}, "round": rnd + 1,
                      "notes": notes, "precheck_bounced": True}
            if engine == "claude":
                bounce["lane"] = lane["name"]
            for fname, extra in (("opencode-session.json", {}), ("claude-session.json", {"engine": "claude"}),
                                 ("luna-session.json", {"engine": "luna"})):
                try:
                    sess = json.loads((run_dir / fname).read_text())
                    if sess.get("session"):
                        bounce["resume"] = {"session": sess["session"], "model": sess.get("model"), **extra}
                except (OSError, ValueError):
                    pass
            write_json(D["rework"] / f"{s}.json", bounce)
            write_json(D["state"] / f"{s}.json", {**rec, "outcome": "precheck", "lane": rec.get("lane"),
                                                  "precheck_fails": [f"{r[1]}: {r[2]}" for r in fails]})
            if rework_file and rework_file.exists() and rework_file != D["rework"] / f"{s}.json":
                rework_file.unlink()
            log("fix", f"{s}: pre-submit check FAIL ({len(fails)}: {'; '.join(r[1] for r in fails)[:160]}) - back to the same session")
            return
    if commits and code_changed and not job.get("negctl_bounced"):
        # owner 2026-10-19: prove mechanically that the new tests fail without the product change, before review
        try:
            import negctl
            nbase = subprocess.run(["git", "-C", str(wt), "merge-base", "HEAD", (ROOT / "FIX-BASE").read_text().strip()],
                                   capture_output=True, text=True).stdout.strip() or head0
            nc = workflow_evidence.prove(wt, nbase, run_dir, report.read_text() if report.exists() else "", negctl.prove)
        except Exception as e:
            nc = {"ok": None, "detail": f"negative control could not run: {e!r}"}
        rec["negctl"] = nc
        log("fix", f"{s}: negative control {'PASS' if nc.get('ok') else 'FAIL' if nc.get('ok') is False else 'not judged'} - {nc.get('detail', '')[:140]}")
        if nc.get("ok") is False:
            bounce = {"item": {**item, "round_base": int(item.get("round_base") or 0) + 1}, "round": rnd + 1,
                      "negctl_bounced": True, "precheck_bounced": True,
                      "notes": ("AUTOMATED PROOF CHECK: the loop reverted your product changes (keeping your tests), rebuilt "
                                f"and ran the tests - {nc['detail']}. A test that passes without the fix proves nothing and "
                                "would be rejected. Make the tests exercise the changed behaviour so they FAIL without your "
                                f"change and PASS with it. Logs: {run_dir / 'negctl'}. Then commit and finish with the same report.")}
            if engine == "claude":
                bounce["lane"] = lane["name"]
            for fname, extra in (("opencode-session.json", {}), ("claude-session.json", {"engine": "claude"}),
                                 ("luna-session.json", {"engine": "luna"})):
                try:
                    sess = json.loads((run_dir / fname).read_text())
                    if sess.get("session"):
                        bounce["resume"] = {"session": sess["session"], "model": sess.get("model"), **extra}
                except (OSError, ValueError):
                    pass
            write_json(D["rework"] / f"{s}.json", bounce)
            write_json(D["state"] / f"{s}.json", {**rec, "outcome": "precheck", "lane": rec.get("lane")})
            write_json(run_dir / "record.json", rec)
            return
    if commits:
        rec["kind"] = "feature" if is_feature(item) else "fix" if code_changed else "claim"
        if engine == "claude":
            rec["lane"] = lane["name"]
        rec["item"] = item
        write_json(run_dir / "record.json", rec)
        write_json(D["qa/pending"] / f"{s}.r{rnd}.json", rec)
        write_json(D["state"] / f"{s}.json", {**rec, "outcome": "in-qa"})
        log("fix", f"{s}: {len(commits)} commit(s), {name} says {verdict}, {rec['minutes']} min -> QA queue ({rec['kind']})")
    elif rnd < adaptive.settings()["max_rounds"]:
        retry = {"item": item, "round": rnd + 1,
                 "notes": f"Your previous session (rc={rc}) ended without committing anything"
                          + (" (uncommitted edits were saved and reverted)" if leftovers else "")
                          + ". Finish the procedure this time, including the committed report."}
        if engine == "claude":
            retry["lane"] = lane["name"]
        write_json(D["rework"] / f"{s}.json", retry)
        write_json(D["state"] / f"{s}.json", {**rec, "outcome": "retry"})
        log("fix", f"{s}: no commits (rc={rc}, {rec['minutes']} min) - one retry queued")
    else:
        rec["outcome"] = "no-result"
        if is_feature(item):
            write_json(D["parked"] / f"{s}.json", rec)      # features_queue.py moves it up a tier
            write_json(D["state"] / f"{s}.json", rec)
            for b in item.get("batch") or []:
                (D["state"] / f"{slug(b['cluster'])}.json").unlink(missing_ok=True)
            log("fix", f"{s}: no commits after {rnd} rounds - feature escalates a tier")
        else:
            bug_escalate(item, rnd, f"no commits after {rnd} rounds")


def run_job(job, rework_file, engine):
    try:
        workflow_runtime.run(job, rework_file, engine, fix_one, globals())
    except LunaLimit as e:
        log("fix", f"{e} (paused until {luna_paused_until():%H:%M})" if luna_paused_until() else str(e))
    except FreeModelLimit as e:
        log("fix", str(e))
    except Exception as e:  # one bad item must not stop the loop
        s = slug(job["item"]["cluster"])
        write_json(D["parked"] / f"{s}.json", {"cluster": job["item"]["cluster"], "outcome": "driver-error",
                                              "error": repr(e), "engine": engine, "finished": now()})
        write_json(D["state"] / f"{s}.json", {"cluster": job["item"]["cluster"], "outcome": "driver-error"})
        if rework_file and rework_file.exists():
            rework_file.unlink()
        log("fix", f"{s}: driver error {e!r} - PARKED")


def answer_gpu_request_now():
    """Light-driver hours: Strata is off, so a Clef GPU request is granted at once (no blocking)."""
    req, free = ROOT / "GPU-REQUEST", ROOT / "GPU-FREE"
    if req.exists() and not (free.exists() and free.stat().st_mtime >= req.stat().st_mtime):
        free.write_text(now() + "\n")
        log("fix", "GPU requested by Clef: Strata is off in light-driver hours - granted")
    elif not req.exists():
        free.unlink(missing_ok=True)


def fix_loop():
    import concurrent.futures
    (ROOT / "fix.pid").write_text(str(os.getpid()))
    log("fix", f"fixer start ({LUNA_WORKERS} cloud workers, one local and up to {opencode_free.MAX_WORKERS} free-model workers, one per model, when enabled; "
               f"no QA backlog cap, min free {MIN_FREE_GB} GB)")
    # One coordinator owns all claims: four Luna, one local, one free session.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(LUNA_WORKERS, 1) + 1 + opencode_free.MAX_WORKERS + 6)
    active = {}          # future -> (slug, engine)
    lane_of = {}         # future -> Claude lane name
    waiting = None
    current = None
    stopping = False
    while True:
        for f in [f for f in active if f.done()]:
            active.pop(f)
        if stopped("FIX") or free_gb() < MIN_FREE_GB:
            if not stopping:
                log("fix", "stop requested" if stopped("FIX") else f"free space {free_gb():.0f} GB < {MIN_FREE_GB} GB"
                    + f" - finishing {len(active)} running bug(s), starting no more")
            stopping = True
            if not active:
                break
            time.sleep(15)
            continue
        engine = engine_now()
        parallel_local = engine == "luna" and (ROOT / "LOCAL-QWEN").exists() and not (ROOT / "QWEN-PAUSED").exists()
        mode = "luna + local qwen" if parallel_local else engine
        if opencode_free.ready():
            mode += " + opencode free"
        if mode != current:
            log("fix", f"engine: {mode}")
            current = mode
        busy = {e for _, e in active.values()}
        capacities = {}
        reason = None
        if parallel_local:
            capacities["luna"] = min(luna_cap("fix"), LUNA_WORKERS)
            if luna_paused_until() or (ROOT / "LUNA-HOLD").exists():
                capacities["luna"] = 0
            qwen_busy = "qwen" in busy
            request = ROOT / "GPU-REQUEST"
            if request.exists():
                holder = request.read_text().split()[0] if request.read_text().split() else ""
                if holder.isdigit() and not Path(f"/proc/{holder}").exists():
                    request.unlink(missing_ok=True)
                    (ROOT / "GPU-FREE").unlink(missing_ok=True)
                    log("fix", "GPU requester exited; local Qwen can resume")
            if (ROOT / "GPU-REQUEST").exists():
                # Cloud jobs can continue while Clef owns the GPU.
                if not qwen_busy:
                    if strata_pids():
                        strata_stop()
                    answer_gpu_request_now()
                capacities["qwen"] = 0
            else:
                (ROOT / "GPU-FREE").unlink(missing_ok=True)
                if not strata_health():
                    strata_start()
                trim_proxy_up()
                capacities["qwen"] = 1 if strata_health() else 0
        elif engine == "luna":
            if "qwen" in busy:
                time.sleep(15)          # finish the Qwen bug before taking Strata down
                continue
            if strata_pids():
                log("fix", f"{light_name()} hours: shutting Strata down")
                strata_stop()
            answer_gpu_request_now()
            capacities["luna"] = min(luna_cap("fix"), LUNA_WORKERS)
            if luna_paused_until():
                capacities["luna"] = 0
                if not opencode_free.ready():
                    reason = f"{light_name()} usage limit - paused until {luna_paused_until():%H:%M}"
            elif (ROOT / "LUNA-HOLD").exists():
                capacities["luna"] = 0
                if not opencode_free.ready():
                    reason = f"{light_name()} held for the research campaign (LUNA-HOLD; removed when the research is done)"
        else:
            if "luna" in busy:
                time.sleep(15)          # let the Luna bugs finish before Strata takes the RAM and GPU
                continue
            if not active:
                yield_gpu_if_requested()
                if not strata_health() and not (ROOT / "GPU-REQUEST").exists():
                    strata_start()
                trim_proxy_up()
            capacities["qwen"] = 1
            reason = None if strata_health() else "Strata is not up at " + STRATA
        capacities["opencode"] = opencode_free.workers()   # owner 2026-10-15: free models side by side, one each
        # Autoscaling (owner, 2026-10-15): a long review queue slows, then holds, new fixer sessions; free
        # memory caps how many more can start (a Haiku session brings a 5-6 GB clangd).
        k = autoscale.knobs()
        backlog = len(list(D["qa/pending"].glob("*.json")))
        factor = autoscale.fixer_factor(backlog, k)
        spare = autoscale.available_ram_gb() - float(k["ram_floor_gb"])
        need = {"opencode": float(k["ram_free_model_gb"]),
                "luna": float(k["ram_haiku_gb"] if light_engine() == "haiku" else k["ram_luna_gb"])}
        running = [e for _, e in active.values()]
        for e in ("opencode", "luna"):
            if e not in capacities:
                continue
            capacities[e] = autoscale.scale(capacities[e], factor) if factor < 1 else capacities[e]
            extra = max(0, capacities[e] - running.count(e))
            fits = max(0, int(spare // need[e])) if k["enabled"] else extra
            capacities[e] = running.count(e) + min(extra, fits)
            spare -= min(extra, fits) * need[e]
        decision = {"backlog": backlog, "factor": factor, "caps": dict(capacities),
                    "running": {e: running.count(e) for e in set(running)}}
        autoscale.report("fix", decision)
        if (backlog, factor, tuple(sorted(capacities.items()))) != getattr(fix_loop, "_last", None):
            log("fix", f"autoscale: review backlog {backlog} (x{factor}), caps {dict(capacities)}")
            fix_loop._last = (backlog, factor, tuple(sorted(capacities.items())))
        if reason is None and available_ram_gb() < 3:
            reason = "less than 3 GB available RAM - waiting before starting another session"
        if reason:
            if reason != waiting:
                log("fix", f"waiting: {reason}")
            waiting = reason
            time.sleep(30)
            continue
        waiting = None
        claimed = {slug_ for slug_, _ in active.values()}
        started = 0
        # 2026-10-19: at most as many OpenCode feature jobs as the feature tiers' models have sessions (MULTI-SESSION.json).
        # The gate only asked whether a tier model was idle, and a tree takes 10-20 min to prepare, so DeepSeek 1017's
        # first evening dispatched 7+ feature jobs that all built trees for 3 sessions and starved the bug builds.
        try:
            multi = json.loads((ROOT / "MULTI-SESSION.json").read_text())
        except (OSError, ValueError):
            multi = {}
        # 2026-10-20: a model whose sessions are split (kind_sessions) counts only its feature slots
        feat_cap = sum(len(opencode_free.slot_range(m, "feature")) for m in feature_models())
        feat_active = sum(1 for slug_, e in active.values() if e == "opencode" and str(slug_).startswith("Q050-"))
        for job_engine in ("opencode", "qwen", "luna"):
            slots = open_session_slots(capacities, [e for _, e in active.values()], job_engine)
            for _ in range(slots):
                # Owner 2026-10-15 20:3x: no Haiku fixers (LUNA-CAP fix=0), so reworks and port jobs go to the
                # OpenCode models (free + Go-plan DeepSeek) whenever the Claude/Luna fixer cap is 0.
                # Luna fixers (owner 2026-10-16) take bugs only; the OpenCode models take features first, and bug
                # reworks only while no Luna fixer slot exists.
                job, rework_file = next_item(
                    claimed, allow_rework=True,
                    # features go out only while a FEATURE-MODELS model is idle, so the other slots keep taking bugs
                    # 2026-10-19: Strata (qwen) takes bugs only; features stay with the OpenCode tiers and Claude lanes
                    features_ok="gated" if job_engine == "opencode" and feat_active < feat_cap else False,
                    engine=job_engine,
                    bug_reworks_ok=job_engine == "luna" or (job_engine == "opencode" and capacities.get("luna", 0) == 0)
                    or (job_engine == "qwen" and not parallel_local))
                if job is None:
                    break
                s_ = slug(job["item"]["cluster"])
                claimed.add(s_)
                if job_engine == "opencode" and is_feature(job["item"]):
                    feat_active += 1
                active[pool.submit(run_job, job, rework_file, job_engine)] = (s_, job_engine)
                started += 1
        # Claude lanes (LANES.json): each enabled lane with nothing running and no unit in review takes its next job
        if (ROOT / "FEATURES-ON").exists() and not paused_until_in(CLAUDE_LIMIT):
            running_lanes = {lane_of.get(f) for f in active}
            # owner 2026-10-19: LANES.json "max_concurrent" caps open lanes; a lane is open while a session runs or
            # one of its units is in review/rework (owner 2026-10-19: "the one that is in review counts as an open lane")
            try:
                cap = int(json.loads((ROOT / "LANES.json").read_text()).get("max_concurrent", 99))
            except (OSError, ValueError):
                cap = 99
            open_lanes = {l["name"] for l in claude_lanes() if l["name"] in running_lanes or lane_holding(l["name"])}
            lane_names = {l["name"] for l in claude_lanes()}
            for lane in claude_lanes():
                if lane["name"] in running_lanes:
                    continue
                # 2026-10-19 owner "allow singular claude lanes": the cap also bounds running sessions, so a lane
                # resuming a unit it holds waits while another lane's session runs (two held units ran at once)
                if len(running_lanes & lane_names) >= cap:
                    break
                if lane["name"] not in open_lanes and len(open_lanes) >= cap:
                    continue
                job, rework_file = next_claude_item(lane, claimed)
                if job is None:
                    continue
                s_ = slug(job["item"]["cluster"])
                claimed.add(s_)
                fut = pool.submit(run_job, job, rework_file, "claude")
                active[fut] = (s_, "claude")
                lane_of[fut] = lane["name"]
                open_lanes.add(lane["name"])
                running_lanes.add(lane["name"])
                started += 1
        if not active and not started:
            # A quota pause or cap=0 is not a drained queue.
            if next_item() is not None:
                time.sleep(30)
                continue
            if not (ROOT / "BUGS-HOLD").exists() and not list(D["qa/pending"].glob("*.json")) and not list(D["rework"].glob("*.json")):
                log("fix", "queue drained and nothing in QA - fixer done")
                (ROOT / "FIX-DRAINED").write_text(now() + "\n")
                break
            time.sleep(120)  # QA may still send reworks back
            continue
        time.sleep(15)
    pool.shutdown(wait=True)
    (ROOT / "fix.pid").unlink(missing_ok=True)
    log("fix", "fixer stopped")


def available_ram_gb():
    try:
        match = re.search(r"^MemAvailable:\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M)
        return int(match.group(1)) / 1_000_000 if match else float("inf")
    except OSError:
        return float("inf")


def open_session_slots(capacities, active_engines, engine):
    return max(0, capacities.get(engine, 0) - active_engines.count(engine))


# ------------------------------------------------------------------ Luna QA
CHECKLIST = (PROG / "plans/HANDOVER-2026-10-05.md")


def feature_qa_brief(rec, reviewer="luna"):
    s = slug(rec["cluster"])
    wt = rec["worktree"]
    stage = rec.get("qa_stage", "code")
    unit = {k: rec["item"].get(k) for k in ("cluster", "title", "goal", "acceptance", "type", "size", "priority")}
    batch = rec["item"].get("batch") or []
    batch_units = ("\nFEATURE BATCH: this branch also implements these units, designed together with the main one; verify EVERY "
                   "unit against its own acceptance criteria and check the shared design is coherent (no per-unit copy-paste). "
                   "ACCEPT only if all of them hold; if one does not, REWORK and name it.\n"
                   + "".join(f"```json\n{json.dumps({k: b.get(k) for k in ('cluster', 'title', 'goal', 'acceptance', 'size')}, ensure_ascii=False, indent=1)}\n```\n"
                             for b in batch)) if batch else ""
    base, rtext = range_text(rec)
    focus = ("CODE verifier: realtime rule (nothing on the audio thread allocates, locks, waits, does I/O or emits "
             "signals), portability (Qt 5.15 + MSVC + macOS 10.15 + clang windows-arm64: no Qt-6-only API without a "
             "guard, no std::filesystem outside _WIN32, windows.h macro names), tests that would FAIL without the "
             "change (prove one yourself by reversing the product hunks, rebuilding and running), no weakened test, "
             "gate or ratchet, registrations (fork-sources, all-sources, upstream-modifications, CMakeLists), code "
             "smells, licensing of anything new.") if stage == "code" else (
             "FEATURE verifier (the code verifier already accepted it): is EVERY acceptance criterion really met for a "
             "musician, proven by a test that exercises it; does the action exist in BOTH the GUI and the agent "
             "surface through the same registered command, with one undo step and an honest reversibility class; "
             "old projects still load; docs/manual updated; snapshot/canary regenerated if commands changed; no bloat "
             "or scope creep beyond the unit. Run the new tests yourself.")
    return f"""You are the independent {stage.upper()} verifier for ONE 0.5.0 feature unit built by another agent
({fixer_label(rec)}). You did not write it. Review it on its own merits.
Worktree: {wt}  branch {rec['branch']}; {rtext}
Its report: {REPORT_DIR}/cluster-{s}.md and docs/050-features/{s}.md in that tree. The builder's brief:
{D['runs']}/{s}.r{rec['round']}/brief.md. Binding rules: {PROG}/AGENTS.md.

Your focus as the {focus}

Verify, do not trust: read `git -C {wt} diff {base}..{rec['head_after']}`; build the touched targets
(`cmake --build build --target zene <Tests> -- -j4`) and run the tests from build/tests (`ctest -R <Test>`),
unpiped exit codes. Run `python3 {ROOT}/feature_check.py {wt}`: complexity and file-length are red on the train on
purpose and block only when it lists an item NEW in a file of this unit; a red direct `--check` alone never blocks.
{REVIEW_LIMITS}{qa_commit_rule(rec, reviewer)} Never push.
Merge rule (owner): ACCEPT when the unit works at a fundamental level and breaks nothing core; minor issues are
ACCEPT-NOTE; REWORK for an unmet criterion, a missing GUI or agent path, a realtime violation, a crash, a weakened test
or gate, a compile error on any CI platform; REJECT only if the approach is wrong beyond rework.

The unit:
```json
{json.dumps(unit, ensure_ascii=False, indent=1)}
```
{batch_units}{goalposts(rec)}{negctl_note(rec)}{integration_note(rec)}{rereview_note(rec)}{blocking_rule(rec)}Finish with a short review (what you checked, file:line, commands + EXIT codes), then a final fenced block:
```json
{{"verdict": "<ACCEPT | ACCEPT-NOTE | {qa_fix_verdict(reviewer)}REWORK | REJECT>", "summary": "<one paragraph>", {BLOCKING_FIELDS}"rework_instructions": "<concrete steps, if REWORK>"{qa_fix_fields(reviewer)}}}
```
"""


# 2026-10-20 failure review: verified fixes bounced only for their report (new-p10-414-port, new-p9-390-port) or for a
# native macOS run this Linux box cannot do (new-p12-162-port).
REVIEW_LIMITS = """NEVER BLOCKING: (a) problems only in the report - a stale log path, an abbreviated or placeholder command, format
or wording - never block a fix whose code and tests you verified: correct the report yourself if small fixes are allowed
below, otherwise list them under ACCEPT-NOTE. (b) This machine is Linux only: never require a run on macOS, Windows or a
Qt 5 build. Check portability by reading the code - a compile error you can point to (file:line, API) still blocks;
hosted CI runs the other platforms.
"""


# Owner 2026-10-20: "We want rejections to only happen when it actually fails, and we need the reasons for failure to be
# transparent and actually matter." A rejection names each failure in one of these categories with evidence; the loop
# checks it (founded_rejection), passes only those to the builder, and logs every reason to REJECTIONS.jsonl.
BLOCKING = {
    "not-fixed": "the bug, or an acceptance criterion, still fails",
    "regression": "something that worked before now fails",
    "crash-or-data-loss": "a crash, hang, corruption, or saved data lost or changed",
    "realtime": "the audio thread now allocates, frees, locks, waits, does I/O or logs",
    "ci-compile": "does not compile on a CI platform (Qt 5.15, MSVC, macOS 10.15, clang windows-arm64) - point at the API",
    "test-proves-nothing": "the regression passes with the product change reversed, or is not deterministic",
    "weakened-check": "a test, assertion, gate, ratchet or baseline is weakened, skipped or re-anchored",
    "new-gate-item": "feature_check.py lists an item NEW in a file this change touches",
    "missing-path": "an acceptance criterion has no working GUI or agent path",
    "out-of-scope": "behaviour outside the finding or unit changes",
    "bug-exists": "(claims) the fixer says it is not a bug, but it is",
    "wrong-approach": "(REJECT) the approach cannot work; rework would not save it",
}
EVIDENCE_RE = re.compile(r"[\w./+-]+\.\w+:\d+|\b(?:EXIT|exit|rc|ctest)\b\D{0,4}-?\d+")
BLOCKING_FIELDS = ('"blocking": [{"category": "<category>", "what": "<the failure>", "evidence": "<command + exit code, or file:line>"}], '
                   '"notes": ["<non-blocking observation>"], ')


def blocking_rule(rec):
    cats = "".join(f"  - {k}: {v}\n" for k, v in BLOCKING.items())
    again = ""
    prev = rec.get("qa_unfounded")
    if prev:
        again = (f"YOUR PREVIOUS REVIEW OF THIS SAME ATTEMPT answered {prev.get('verdict')} without one evidenced failure in these "
                 f"categories (it said: {str(prev.get('summary', ''))[:400]}). Name the failure with evidence, or accept. "
                 f"If you again name none, the work is accepted with your notes.\n")
    return f"""REJECTIONS (owner rule: work goes back only when it actually fails; every reason must be visible and matter).
REWORK, REJECT or DISPUTE only for a concrete failure in one of these categories, each shown by evidence - a command with
its exit code and the failing output, or file:line where the failure is:
{cats}Anything else - style, naming, report wording, a concern you could not demonstrate, a "might" with no reachable case -
is not a failure: put it under "notes" and ACCEPT-NOTE. Put each blocking failure in "blocking" (one entry each); the
loop checks every entry and passes only those to the builder, so rework_instructions must address exactly them. A
rejection with no valid entry is sent back to you once; if it still names none, the work is accepted with your notes.
{again}"""


def founded_rejection(v):
    """(valid, invalid) blocking entries of a REWORK/REJECT/DISPUTE: a known category, a stated failure, and evidence that
    is a file:line or a command with an exit code."""
    good, bad = [], []
    for b in v.get("blocking") if isinstance(v.get("blocking"), list) else []:
        b = b if isinstance(b, dict) else {"what": str(b)}
        ok = (b.get("category") in BLOCKING and str(b.get("what") or "").strip()
              and EVIDENCE_RE.search(str(b.get("evidence") or "")))
        (good if ok else bad).append({k: str(b.get(k) or "") for k in ("category", "what", "evidence")})
    return good, bad


def log_rejection(rec, qa, verdict):
    with open(ROOT / "REJECTIONS.jsonl", "a") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "cluster": rec["cluster"], "round": rec["round"],
                            "kind": rec.get("kind"), "stage": rec.get("qa_stage") if rec.get("kind") == "feature" else None,
                            "model": rec.get("model") or rec.get("engine"), "started": rec.get("started"),
                            "reviewer": qa.get("reviewer_model"), "verdict": verdict,
                            "blocking": qa.get("blocking") or [], "dropped": qa.get("dropped_reasons") or [],
                            "unfounded": bool(qa.get("unfounded_rejection"))}, ensure_ascii=False) + "\n")


def blocking_text(qa):
    out = "".join(f"- [{b['category']}] {b['what']} (evidence: {b['evidence']})\n" for b in qa.get("blocking") or [])
    notes = [str(n) for n in (qa.get("notes") or []) if str(n).strip()] + [
        f"[{b['category'] or 'uncategorised'}] {b['what']}" for b in qa.get("dropped_reasons") or [] if b.get("what")]
    if notes:
        out += "\nNon-blocking notes (not why it was sent back; act on them only if it is free):\n" + "".join(f"- {n}\n" for n in notes)
    return out


def branch_base(rec):
    """Where the branch left the fix line (merge-base with FIX-BASE): the whole fix is branch_base..head_after. A later
    round's own range (head_before..head_after) can hold only report edits, so reversing it proves nothing - reviews
    judged that range until 2026-10-20 (new-p9-458-port: the fix sat in an earlier round's commit)."""
    try:
        mb = git(rec["worktree"], "merge-base", rec["head_after"], (ROOT / "FIX-BASE").read_text().strip())
    except OSError:
        mb = ""
    return mb or rec["head_before"]


def range_text(rec):
    base = branch_base(rec)
    whole = git(rec["worktree"], "log", "--reverse", "--format=%h %s", f"{base}..{rec['head_after']}").splitlines() or rec["commits"]
    out = f"the whole change is {base[:12]}..{rec['head_after'][:12]} (branch base to tip):\n" + "\n".join("  " + c for c in whole)
    if base != rec["head_before"]:
        out += (f"\nThis round added {rec['head_before'][:12]}..{rec['head_after'][:12]}: " + "; ".join(rec["commits"])
                + "\nJudge, build and prove (reverse the product hunks) against the WHOLE change, not only this round.")
    return base, out


def rereview_note(rec):
    return f"\nRE-REVIEW (loop note): {rec['rereview']}\n" if rec.get("rereview") else ""


def goalposts(rec):
    """Owner 2026-10-19: goalposts are set up front - round one lists every blocking issue, the next review may add one
    more round of them, after that reviews only verify (new non-blocking findings become ACCEPT-NOTE)."""
    s, rnd = slug(rec["cluster"]), int(rec["round"])
    stage = rec.get("qa_stage", "code") if rec.get("kind") == "feature" else None
    prior = []
    for k in range(1, rnd):
        for f in sorted(D["qa/reviews"].glob(f"{s}.r{k}.{stage}.md" if stage else f"{s}.r{k}.md")):
            m = re.findall(r"```json\s*(\{.*?\})\s*```", f.read_text(errors="replace"), re.S)
            try:
                v = json.loads(m[-1]) if m else {}
            except ValueError:
                v = {}
            prior.append((k, f, v))
    if not prior:
        return ("\nGOALPOSTS: this is the first review of this work. List EVERY blocking issue now, against the unit's "
                "acceptance criteria and the merge rule; later reviews will only check that these are resolved.\n")
    listing = "".join(f"- round {k} ({f}): {v.get('verdict', '?')} - {str(v.get('rework_instructions') or v.get('summary', ''))[:500]}\n"
                      for k, f, v in prior)
    if len(prior) == 1:
        rule = ("First check that every point of the earlier review is resolved. You may still raise NEW blocking issues "
                "this round - it is the last round that may; anything non-blocking goes under ACCEPT-NOTE.")
    else:
        rule = ("VERIFY ONLY: check that the earlier reviews' points are resolved. A NEW issue may block only if the latest "
                "changes introduced it, or it is a crash, data loss, a realtime violation or a CI compile error; every other "
                "new finding goes under ACCEPT-NOTE, never REWORK.")
    return f"\nGOALPOSTS - earlier reviews of this work:\n{listing}{rule}\n"


def integration_note(rec):
    extra = rec.get("integration_outside_patch") or []
    if not extra:
        return ""
    return (f"\nINTEGRATION SCOPE: this integration changed {len(extra)} file(s) the accepted patch did not touch: "
            f"{', '.join(extra[:20])}{' ...' if len(extra) > 20 else ''}. Each must be a conflict resolution or the fix "
            f"of a demonstrated regression; anything else is REWORK.\n")


def negctl_note(rec):
    nc = rec.get("negctl") or {}
    if nc.get("proof_base") and workflow_evidence.valid_proof(rec["worktree"], nc["proof_base"], nc):
        return (f"\nMECHANICAL PROOF VALID FOR THIS SOURCE/TEST/BUILD CONTENT: {nc.get('detail')}. "
                f"Artifact: {nc['proof_dir']}. Inspect the tests and evidence independently; the same reversal need not "
                f"be repeated unless code/build inputs changed or you find a specific flaw in that proof. "
                f"Check semantics, failure paths and compatibility together.\n")
    return ""


QA_FIX = ROOT / "QA-FIX.json"
TEST_ASSERT = re.compile(r"\b(QVERIFY\w*|QCOMPARE\w*|QTRY_\w+|QFAIL|assert\w*|ASSERT_\w+|EXPECT_\w+|CHECK\w*|REQUIRE\w*)\b")
TEST_SKIP = re.compile(r"\b(QSKIP|QEXPECT_FAIL|DISABLED\w*|WILL_FAIL|skipTest|pytest\.(skip|xfail|mark\.skip\w*)|unittest\.skip\w*)\b")
PROTECTED = re.compile(r"(^|/)[^/]*(gate|ratchet|baseline)[^/]*$|^\.github/")


def qa_fix_cfg():
    """Owner 2026-10-20: "allow the luna reviewers to fix small problems, iff the focused tests pass". QA-FIX.json, read
    live; absent or "enabled": false = reviewers never commit (the old rule). Luna only, never the Haiku/Qwen reviewers."""
    try:
        cfg = json.loads(QA_FIX.read_text())
    except (OSError, ValueError):
        return None
    return cfg if cfg.get("enabled") else None


def qa_fix_verdict(reviewer):
    return "ACCEPT-FIXED | " if reviewer == "luna" and qa_fix_cfg() else ""


def qa_fix_fields(reviewer):
    if reviewer != "luna" or not qa_fix_cfg():
        return ""
    return (', "fix_commit": "<sha, if ACCEPT-FIXED>", "focused_tests": ["<exact ctest name>", "..."], '
            '"fix_summary": "<what you changed and why, if ACCEPT-FIXED>"')


def qa_commit_rule(rec, reviewer):
    cfg = qa_fix_cfg() if reviewer == "luna" else None
    if not cfg:
        return "You must NOT commit, amend, or leave any change: `git status` must be clean when you finish."
    kind = rec.get("kind")
    when = {
        # owner 2026-10-20 (2nd): bug fixes may be repaired even when they would otherwise be rejected
        "fix": ("whenever what stands between this attempt and ACCEPT is small - even if the focused tests fail as\n"
                "submitted - fix it yourself instead of REWORK/REJECT. After your fix the focused tests must PASS and the\n"
                "regression must FAIL with the product change reversed (and PASS restored)."),
        "claim": ("the fixer says the bug is not real. If you find it IS real (you would DISPUTE) and the fix is small, fix it\n"
                  "yourself instead: write a deterministic regression test first and see it FAIL, make the fix at the cause,\n"
                  "see it PASS, and prove it FAILS again with only the product change reversed. Register a new test file."),
    }.get(kind, "only if the focused tests (the tests this work adds or changes, plus those its report names) PASS on the\n"
                "branch as submitted - run them first; if they fail, REWORK.")
    return f"""SMALL FIXES (owner rule): you may fix a SMALL blocking problem yourself instead of sending the work back: {when}
Small means: at most {cfg.get('max_lines', 40)} changed product lines in at most {cfg.get('max_files', 3)} product files, plus at most
{cfg.get('max_test_lines', 120)} test lines in {cfg.get('max_test_files', 4)} test/registration files (reports excluded), within the same design, nothing new to
invent - e.g. an overflow or null guard, a missed count or assertion update, a Qt 5 / MSVC guard, a wrong doc sentence, a
duplicated report row, a missing registration, a one-function bug fix. NOT small (send those back): a missing GUI or agent
path, a test that must be redesigned around a new seam, a race or threading change, realtime-path logic beyond a one-line
guard, or anything you are unsure of. Never weaken, skip, disable or delete a test, assertion, gate, ratchet or baseline.
How: make the change, rebuild, rerun the focused tests (all must PASS). Commit once on top of the branch (`git add` new
files; `git commit -m "qa-fix(<id>): <what>"`); never amend or rebase the builder's commits; leave `git status` clean. Then
answer ACCEPT-FIXED with fix_commit, focused_tests and fix_summary, and still fill blocking and rework_instructions as if
you had not fixed it. The loop checks your fix itself (size, protected files, no fewer assertions, no new skips, then it rebuilds and
reruns your focused tests{', and for a bug the regression must fail with the product change reversed' if kind != 'feature' else ''}). If any check fails your
commit is dropped and your rework_instructions go back to the builder. If you fix nothing, you must NOT commit or leave any
change: `git status` clean."""


def verify_qa_fix(rec, v, logs):
    """The loop's own check of a reviewer's ACCEPT-FIXED commit(s) on top of head_after: (ok, detail). Never trusts the review."""
    import negctl
    cfg = qa_fix_cfg() or {}
    wt, base = Path(rec["worktree"]), rec["head_after"]
    head = git(wt, "rev-parse", "HEAD")
    if head == base:
        return False, "ACCEPT-FIXED but no commit was added"
    if subprocess.run([GIT, "-C", str(wt), "merge-base", "--is-ancestor", base, head]).returncode:
        return False, "the fix rewrote the builder's commits instead of adding one on top"
    commits = git(wt, "log", "--format=%h %s", f"{base}..{head}").splitlines()
    if len(commits) > int(cfg.get("max_commits", 2)):
        return False, f"{len(commits)} commits (limit {cfg.get('max_commits', 2)})"
    if git(wt, "diff", "--name-only", "--diff-filter=D", f"{base}..{head}"):
        return False, "the fix deletes a file"
    size = {"product": [0, 0], "test": [0, 0]}           # [lines, files]
    for row in git(wt, "diff", "--numstat", f"{base}..{head}").splitlines():
        a, d, f = row.split("\t", 2)
        if f.startswith((REPORT_DIR, "docs/050-features/")):
            continue
        if PROTECTED.search(f):
            return False, f"the fix touches a gate, ratchet or baseline file ({f})"
        k = size["test" if f.startswith("tests/") else "product"]
        k[1] += 1
        k[0] += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    (lines, files), (tlines, tfiles) = size["product"], size["test"]
    if (lines > int(cfg.get("max_lines", 40)) or files > int(cfg.get("max_files", 3))
            or tlines > int(cfg.get("max_test_lines", 120)) or tfiles > int(cfg.get("max_test_files", 4))):
        return False, (f"too big for a reviewer fix: {lines} product lines in {files} files, {tlines} test lines in {tfiles} files "
                       f"(limits {cfg.get('max_lines', 40)}/{cfg.get('max_files', 3)}, {cfg.get('max_test_lines', 120)}/{cfg.get('max_test_files', 4)})")
    for f in git(wt, "diff", "--name-only", f"{base}..{head}", "--", "tests").splitlines():
        hunk = git(wt, "diff", "-U0", f"{base}..{head}", "--", f).splitlines()
        gone = [l[1:] for l in hunk if l.startswith("-") and not l.startswith("---")]
        new = [l[1:] for l in hunk if l.startswith("+") and not l.startswith("+++")]
        if sum(len(TEST_ASSERT.findall(l)) for l in gone) > sum(len(TEST_ASSERT.findall(l)) for l in new):
            return False, f"the fix leaves fewer assertions in {f}"
        if any(TEST_SKIP.search(l) for l in new):
            return False, f"the fix adds a skip or expected failure in {f}"
    build = wt / "build"
    if not (build / "tests").is_dir():
        return False, "no build tree to rerun the focused tests in"
    known = set(re.findall(r"Test #\d+: (\S+)", subprocess.run(["ctest", "-N"], cwd=build / "tests", capture_output=True,
                                                                text=True).stdout))
    named = {str(t).strip("^$") for t in (v.get("focused_tests") or [])} | set((rec.get("negctl") or {}).get("tests") or [])
    tests = sorted(named & known)
    if not tests:
        return False, f"no registered ctest test among the focused tests named ({', '.join(sorted(named))[:200] or 'none'})"
    out = logs / "qa-fix"
    out.mkdir(parents=True, exist_ok=True)
    e = negctl.env(wt)
    targets = set(re.findall(r"^\.\.\. (\S+)", subprocess.run(["cmake", "--build", "build", "--target", "help"], cwd=wt,
                                                               capture_output=True, text=True, env=e).stdout, re.M))
    rb = negctl.sh(["cmake", "--build", "build", "--target", "zene", *[t for t in tests if t in targets], "--", "-j4"],
                   wt, out / "build.log", e)
    if rb:
        return False, f"build failed with the fix (rc {rb}; {out / 'build.log'})"
    regex = "^(" + "|".join(re.escape(t) for t in tests) + ")$"
    rt = negctl.sh(["ctest", "-R", regex, "--output-on-failure", "--timeout", "600"], build / "tests", out / "test.log", e)
    if rt:
        return False, f"focused tests fail with the fix (rc {rt}; {out / 'test.log'})"
    red = ""
    if rec.get("kind") != "feature":         # a bug is only fixed with a regression that fails without the fix
        nc = negctl.prove(wt, branch_base(rec), out)
        if not nc.get("ok"):
            return False, f"red proof lost: {nc.get('detail')}"
        red = "; red proof holds"
    if git(wt, "status", "--porcelain"):
        return False, "tree not clean after the check"
    return True, f"{len(commits)} commit(s), {lines}+{tlines} product+test lines; {', '.join(tests)} pass{red}"


def drop_qa_fix(rec, logs):
    """Put the branch back on the builder's head_after, keeping the reviewer's attempt as a patch."""
    wt = Path(rec["worktree"])
    out = logs / "qa-fix"
    out.mkdir(parents=True, exist_ok=True)
    (out / "dropped.patch").write_text(git(wt, "diff", rec["head_after"], "HEAD"))
    subprocess.run([GIT, "-C", str(wt), "reset", "-q", "--hard", rec["head_after"]], capture_output=True)


def batch_qa_note(rec):
    batch = (rec.get("item") or {}).get("batch") or []
    if not batch:
        return ""
    bv = rec.get("batch_verdicts") or {}
    lines = "\n".join(f"  - {b['cluster']}: {b.get('title', '')} (fixer: {bv.get(str(b['cluster']), '?')})" for b in batch)
    kind = (f"ROOT-CAUSE GROUP {rec['item']['root_cause']} (bugs judged to share one cause; check the fix really addresses "
            f"the shared cause and that each bug's own test fails without it)") if rec["item"].get("root_cause") else "SAME-FILE BATCH"
    return (f"{kind}: besides the main finding ({rec['cluster']}, fixer: {bv.get(str(rec['cluster']), rec.get('qwen_verdict'))}) "
            f"this branch also handles these bugs (their findings are in the brief):\n{lines}\n"
            f"Review EVERY bug in the batch with the same rigour (own test, RED without its own fix). ACCEPT only if all of them "
            f"hold; if one does not, REWORK and name it - the others stay on the branch.\n")


def qa_brief(rec, reviewer="luna"):
    if rec.get("kind") == "feature":
        return feature_qa_brief(rec, reviewer)
    s = slug(rec["cluster"])
    wt = rec["worktree"]
    kind = rec["kind"]
    verdicts = f"ACCEPT | ACCEPT-NOTE | {qa_fix_verdict(reviewer)}REWORK | REJECT" if kind == "fix" else f"CONFIRM | {qa_fix_verdict(reviewer)}DISPUTE"
    fields = qa_fix_fields(reviewer)
    base, rtext = range_text(rec)
    author = f"another {fixer_label(rec)} session"
    who = f"{fixer_label(rec)} (fixer)"
    return f"""You are the QA reviewer for ONE bug-fix attempt made by {author}. Review it independently.
Worktree: {wt}  branch {rec['branch']}. In this attempt {rtext}
{who}'s own verdict: {rec.get('qwen_verdict')}; its report is {REPORT_DIR}/cluster-{s}.md in that tree.
This is a {'code fix' if kind == 'fix' else 'claim with no code change (the fixer says the bug is not real / already fixed)'}.
{batch_qa_note(rec)}{goalposts(rec)}{negctl_note(rec)}{integration_note(rec)}{rereview_note(rec)}
Read {PROG}/AGENTS.md, then section 5 "Reviewing lane commits" of {CHECKLIST} - apply every check there.
The finding the fixer was given is in {D['runs']}/{s}.r{rec['round']}/brief.md.

Verify, do not trust: read the diff (`git -C {wt} diff {base}..{rec['head_after']}`) against the
code and the finding's real semantics. For a fix: rebuild the touched targets
(`cmake --build build --target zene <Tests>`), run the regression from build/tests (`ctest -R <Test>`), and show
it is RED without the product change: reverse only the non-test hunks in the working tree, rebuild, run, then
restore them with `git checkout -- <paths>` and rebuild. Unpiped exit codes.
Gates: run `python3 {ROOT}/feature_check.py {wt}` (unpiped EXIT). The complexity and file-length gates are red
on the train on purpose: they block ONLY when that table lists an item NEW in a file this change touches. A red
direct `--check` run is never by itself a reason for REWORK or DISPUTE, and never ask for a "parent disposition".
{REVIEW_LIMITS}For a claim: check the code yourself and decide whether the bug really is absent.

{qa_commit_rule(rec, reviewer)}
Do not run the full suite or run-all-gates.sh. Never push.

{blocking_rule(rec)}Finish with a short review (what you checked, file:line, commands + EXIT codes), then a final fenced block:
```json
{{"verdict": "<{verdicts}>", "summary": "<one paragraph>", {BLOCKING_FIELDS}"rework_instructions": "<concrete steps for Qwen, if REWORK/DISPUTE>"{fields}}}
```
"""


# Condensed from plans/HANDOVER-2026-10-05.md section 5 (each check caught a real problem in earlier reviews).
QA_CHECKLIST = """\
1. SEMANTICS: does the change fix the behaviour the finding describes, at the right place, for every case it
   names? Check boundaries (the END of a range, not just its start), and the type callers narrow to.
2. SIDE EFFECTS: anything holding an index that the change renumbers; data saved in projects (a stricter check
   that also runs on project LOAD must not drop saved data - clamp on load instead); undo stays one honest step.
3. KNOWN FALSE ALARMS: command args are schema-validated before the handler (ControlRegistry.cpp:251), so
   "malformed argument reaches the handler" is usually false; handlers without a transaction still get an
   honest registry record. Don't accept or reject on those grounds alone.
4. REALTIME RULE: no allocation, lock or logging added on an audio-thread path.
5. HEADERS: a changed header means every module including it was rebuilt (stale plug-ins corrupt the heap).
6. THE TEST: deterministic, registered (tests/all-sources.txt / CMakeLists.txt), and it must FAIL without the
   product change - prove it yourself (procedure step 3). A test that passes without the fix is REWORK.
7. Never accept a fix that weakens a test, gate or ratchet, or changes a known-by-design contract."""


def qa_brief_qwen(rec, logs):
    """Lean review brief for a Qwen reviewer: checklist inline and the material handed over up front."""
    s, wt, kind = slug(rec["cluster"]), Path(rec["worktree"]), rec["kind"]
    a, b = branch_base(rec), rec["head_after"]
    files = git(wt, "diff", "--name-only", f"{a}..{b}").splitlines()
    product = [f for f in files if not f.startswith(("tests/", REPORT_DIR, "docs/"))]
    ordered = product + [f for f in files if f not in product and not f.startswith(REPORT_DIR)]
    diff = git(wt, "diff", f"{a}..{b}", "--", *ordered) if ordered else ""
    if len(diff) > 14000:
        diff = diff[:14000] + f"\n...[diff truncated; read the rest with: git diff {a[:12]}..{b[:12]} -- <path>]"
    report = wt / REPORT_DIR / f"cluster-{s}.md"
    report_text = report.read_text(errors="replace")[:2500] if report.exists() else "(no report found)"
    tests = sorted({t for f in product for t, _ in tests_covering(wt, f)} | {f for f in files if f.startswith("tests/src/")})
    item = compact(rec.get("item") or {"cluster": rec["cluster"], "title": rec.get("title")})
    verdicts = "ACCEPT | ACCEPT-NOTE | REWORK | REJECT" if kind == "fix" else "CONFIRM | DISPUTE"
    if kind == "fix":
        steps = f"""1. Read the diff below against the finding. Read only the surrounding functions you need (fff, path={wt}).
2. Build the touched targets and the tests, logging to the LOG FOLDER:
   `cmake --build build --target zene <Tests> > {logs}/build-1.log 2>&1; echo EXIT=$?`, then from build/tests
   `ctest -R '^<Test>$' --output-on-failure > {logs}/test-green.log 2>&1; echo EXIT=$?` - must PASS.
3. RED PROOF: `git -C {wt} diff {a[:12]}..{b[:12]} -- {' '.join(product) or '<product files>'} > {logs}/fix.patch && git -C {wt} apply -R {logs}/fix.patch`,
   rebuild, run the test into {logs}/test-red.log - it must FAIL. Then `git -C {wt} apply {logs}/fix.patch`,
   rebuild, run again - PASS. Search the logs with the fff grep tool (path = LOG FOLDER).
4. `python3 {ROOT}/feature_check.py {wt}` (unpiped EXIT). Complexity and file-length are red on the train on purpose;
   they block only when the table lists an item NEW in a file this change touches.
5. Go through the checklist, then give the verdict."""
        proof = rec.get("negctl") or {}
        if proof.get("proof_base") and workflow_evidence.valid_proof(wt, proof["proof_base"], proof):
            steps = re.sub(r"3\. RED PROOF:.*?\n4\.",
                           "3. Inspect the content-bound negative-control evidence in " + proof["proof_dir"] +
                           "; do not repeat that reversal unless you identify a specific problem with its validity.\n4.",
                           steps, flags=re.S)

    else:
        steps = """1. The fixer says this bug is not real or already fixed. Find the code path the finding describes
   (fff, path=the worktree) and decide yourself whether the bug exists today. Read the fixer's evidence below.
2. If you can show the bug with a small test or a command, do it (logs in the LOG FOLDER); never commit it.
3. CONFIRM if the bug really is absent, DISPUTE (with the exact code and reason) if it is present."""
    return f"""You are the QA reviewer for ONE bug-fix attempt made by another session. Review it independently;
you did not write it. Verify, do not trust.
Worktree: {wt}  branch {rec['branch']}  attempt {a[:12]}..{b[:12]}: {'; '.join(rec['commits'])}
This is a {'code fix' if kind == 'fix' else 'claim with no code change'}. The fixer's verdict: {rec.get('qwen_verdict')}.
LOG FOLDER: {logs}   (every build/test log goes here as a new file; search it with fff grep)

## Rules
- Do NOT commit, amend, stash, switch branches, push or fetch. Leave `git status` clean when you finish
  (re-apply the patch if you reversed it). Don't run the full suite or run-all-gates.sh.
- Builds are targeted and already configured; rebuild before believing any failure. Env is set; never set
  LMMS_PLUGIN_DIR. Guard shims cap parallelism - never work around them.

## Review checklist
{QA_CHECKLIST}

## Procedure
{steps}

## The finding
```json
{json.dumps(item, ensure_ascii=False, indent=1)}
```

## The fixer's report ({REPORT_DIR}/cluster-{s}.md, first part)
{report_text}

## Files changed
{chr(10).join('- ' + f for f in files) or '(none)'}
## Tests that cover the changed code
{chr(10).join('- ' + t for t in tests) or '(none found: the fix must add one)'}

## The diff (product files first)
```diff
{diff}
```

{blocking_rule(rec)}Finish with a short review (what you checked, file:line, commands + EXIT codes), then a final fenced block:
```json
{{"verdict": "<{verdicts}>", "summary": "<one paragraph>", {BLOCKING_FIELDS}"rework_instructions": "<concrete steps, if REWORK/DISPUTE>"}}
```
"""


def parse_verdict(text):
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    for b in reversed(blocks):
        try:
            v = json.loads(b)
            if v.get("verdict"):
                v["verdict"] = v["verdict"].strip().upper()
                return v
        except json.JSONDecodeError:
            pass
    return None


def retire_tree(rec, keep_tree):
    wt = Path(rec["worktree"])
    if not wt.exists():
        return
    if keep_tree:
        shutil.rmtree(wt / "build", ignore_errors=True)  # keep the source tree, free the build
    else:
        subprocess.run([GIT, "-C", rec["lane_tree"], "worktree", "remove", "--force", str(wt)], capture_output=True)


def qa_one(path, reviewer=None):
    rec = json.loads(path.read_text())
    s, rnd = slug(rec["cluster"]), rec["round"]
    wt = rec["worktree"]
    if reviewer is None:
        reviewer = "luna" if luna_available() or (ROOT / "LOCAL-QWEN").exists() else "qwen"
    haiku = reviewer == "haiku"          # the second reviewer: Luna's brief, Haiku 5.5 through haiku-exec
    rname = "Haiku 5.5 (second reviewer)" if haiku else light_name()
    qa_logs = D["runs"] / f"{s}.r{rnd}" / "qa-logs"      # outside git, so fff can index it
    qa_logs.mkdir(parents=True, exist_ok=True)
    brief = qa_brief(rec, reviewer) if reviewer in ("luna", "haiku") else qa_brief_qwen(rec, qa_logs)
    out = D["qa/reviews"] / (f"{s}.r{rnd}.{rec.get('qa_stage', 'code')}.md" if rec.get("kind") == "feature" else f"{s}.r{rnd}.md")
    if out.exists():
        # 2026-10-25: a re-run of the same review (re-ask, retry) whose reviewer crashed before writing would otherwise
        # have the PREVIOUS attempt's verdict parsed as its own. Keep the old text, out of the round's glob.
        out.rename(out.with_name(f"{out.stem}.prev-{int(time.time())}{out.suffix}"))
    (D["runs"] / f"{s}.r{rnd}" / "qa-brief.md").write_text(brief)
    log("qa", f"{s} r{rnd} ({rec['kind']}, {rec.get('engine', 'qwen')}: {rec.get('qwen_verdict')}) "
              f"{rname if reviewer in ('luna', 'haiku') else 'Qwen (separate session)'} review start")
    t0 = time.time()
    env = session_env()
    if reviewer in ("luna", "haiku"):
        codex = ["codex", "exec", "-m", "gpt-6-luna", "-c", 'model_reasoning_effort="max"',
                 "-c", 'approval_policy="never"', "-s", "danger-full-access", "-C", wt,
                 "--ephemeral", "-o", str(out), "-"]
        cmd = [str(HAIKU_EXEC)] + codex[2:] if haiku else light_cmd(codex)
        if haiku:
            env["HAIKU_EFFORT"] = os.environ.get("HAIKU_QA_EFFORT", "high")
    else:
        cmd = ["mcode", "exec", "--config", str(mcode_config()), "--model", QWEN_MODEL,
               "--permission", "full", "--prompt-mode", "coding", "--effort", QWEN_EFFORT, "--cwd", str(wt),
               "--timeout", FIX_TIMEOUT, "--max-steps", "250", "-o", str(out), "--input", "-"]
    review_item = rec.get("item") or {"cluster": rec["cluster"], "title": rec.get("title")}
    review_key = issue_ledger.start(review_item, "review", qa_logs, "claude-haiku-5-5" if haiku else light_model())
    review_attempt = len([e for e in issue_ledger.get(review_item)["events"] if e["kind"] == "review"])
    review_log = D["runs"] / f"{s}.r{rnd}" / f"qa-session-{review_attempt}.log"
    with open(review_log, "w") as lf:
        rc = subprocess.run(cmd, input=brief, text=True, stdout=lf, stderr=subprocess.STDOUT,
                            env=env).returncode
    # Keep the current conventional log for callers while retaining every review attempt separately.
    shutil.copy2(review_log, D["runs"] / f"{s}.r{rnd}" / "qa-session.log")
    dirty = save_leftovers(Path(wt), D["runs"] / f"{s}.r{rnd}" / "qa-leftover")
    limited = rc != 0 and note_luna_limit(D["runs"] / f"{s}.r{rnd}" / "qa-session.log", HAIKU_LIMIT if haiku else None)
    # a usage-limited review is not a review: cancelled, so the report does not count it
    issue_ledger.finish(review_item, review_key, {"rc": rc, "minutes": round((time.time()-t0)/60, 2)},
                        cancelled=bool(limited))
    if limited:
        until = paused_until_in(HAIKU_LIMIT) if haiku else luna_paused_until()
        log("qa", f"{s}: {rname} usage limit - review not counted, stays pending (paused until {until:%H:%M})")
        os.utime(path)
        return
    moved = git(wt, "rev-parse", "HEAD") != rec["head_after"]
    v = parse_verdict(out.read_text()) if out.exists() else None
    qa = {"rc": rc, "minutes": round((time.time() - t0) / 60, 1), "review": str(out), "reviewer": reviewer,
          "reviewer_model": "claude-haiku-5-5" if haiku else light_model() if reviewer == "luna" else "qwen",
          "left_tree_dirty": bool(dirty), "moved_head": moved, **(v or {})}
    rec["qa"] = qa
    if v and v.get("verdict") == "ACCEPT-FIXED" and not dirty:
        # owner 2026-10-20: Luna may fix small problems itself; the loop re-checks the fix before it counts
        ok, detail = verify_qa_fix(rec, v, qa_logs) if reviewer == "luna" and qa_fix_cfg() else (False, "reviewer fixes are off")
        qa["qa_fix"] = {"ok": ok, "detail": detail}
        if ok:
            new = git(wt, "rev-parse", "HEAD")
            rec["commits"] = rec["commits"] + git(wt, "log", "--reverse", "--format=%h %s", f"{rec['head_after']}..{new}").splitlines()
            rec["qa_fixed_from"], rec["head_after"] = rec["head_after"], new
            log("qa", f"{s}: {rname} fixed it itself - check passed ({detail})")
        else:
            drop_qa_fix(rec, qa_logs)
            v = {**v, "verdict": "DISPUTE" if rec.get("kind") == "claim" else "REWORK",
                 "summary": f"(the reviewer's own fix was dropped: {detail}) {v.get('summary', '')}"}
            qa.update(v)
            log("qa", f"{s}: {rname} fix dropped ({detail}) - {v['verdict']}")
        moved = False
    elif moved and reviewer == "luna" and qa_fix_cfg():
        drop_qa_fix(rec, qa_logs)            # a stray reviewer commit without ACCEPT-FIXED: undone, review retried
    allowed_verdicts = {"ACCEPT", "ACCEPT-NOTE", "ACCEPT-FIXED", "CONFIRM", "REWORK", "DISPUTE", "REJECT"}
    if v is None or moved or dirty or v.get("verdict") not in allowed_verdicts:
        tries = rec.get("qa_failures", 0) + 1
        rec["qa_failures"] = tries
        if tries < 2:
            write_json(path, rec)  # stays pending; retried after the others
            os.utime(path)
            log("qa", f"{s}: {rname} gave no usable verdict (rc={rc}{', HEAD moved' if moved else ''}{', dirty tree' if dirty else ''}) - will retry")
            return
        verdict = "QA-ERROR"
    else:
        verdict = v["verdict"]
        if verdict in ("REWORK", "REJECT", "DISPUTE"):
            good, bad = founded_rejection(v)
            qa["blocking"], qa["dropped_reasons"] = good, bad
            if not good and not rec.get("qa_unfounded"):
                rec["qa_unfounded"] = {"verdict": verdict, "summary": v.get("summary", ""), "review": str(out)}
                write_json(path, rec)
                os.utime(path)
                log("qa", f"{s}: {rname} {verdict} named no evidenced failure - review asked again")
                return
            if not good:
                qa["unfounded_rejection"] = True
                log_rejection(rec, qa, verdict)
                verdict = qa["verdict"] = "ACCEPT-NOTE"
                qa["notes"] = [f"(a {rec['qa_unfounded']['verdict']} and then a {v['verdict']} named no evidenced failure; accepted "
                               f"under the owner's rule) {v.get('summary', '')}"] + list(qa.get("notes") or [])
                log("qa", f"{s}: {rname} rejected twice without an evidenced failure - accepted with notes")
            else:
                log_rejection(rec, qa, verdict)
    if rec.get("kind") == "feature" and verdict in ("ACCEPT", "ACCEPT-NOTE", "ACCEPT-FIXED") and rec.get("qa_stage", "code") == "code":
        # SPEC-050 7.1: a second, independent Luna pass (feature verifier) before the unit counts as accepted.
        rec["qa_code"] = qa
        rec["qa_stage"] = "feature"
        rec.pop("qa_failures", None)
        rec.pop("qa_unfounded", None)
        write_json(path, rec)
        log("qa", f"{s}: code verifier {verdict} ({qa['minutes']} min) - feature verifier next")
        return
    rec["outcome"] = verdict
    path.replace(D["qa/done"] / path.name)
    write_json(D["qa/done"] / path.name, rec)
    if verdict in ("ACCEPT", "ACCEPT-NOTE", "ACCEPT-FIXED", "CONFIRM"):
        write_json(D["state"] / f"{s}.json", {**rec, "outcome": "accepted"})
        with open(ROOT / ("FEATURES-ACCEPTED.tsv" if rec.get("kind") == "feature" else "ACCEPTED.tsv"), "a") as f:
            f.write(f"{rec['cluster']}\t{rec['branch']}\t{rec['head_after'][:12]}\t{verdict}\t{rec.get('title', '')}"
                    f"\treviewed-by:{qa.get('reviewer', 'luna')}\n")
        for b in (rec.get("item") or {}).get("batch") or []:     # same-file batch: its handled companions are accepted too
            bverdict = (rec.get("batch_verdicts") or {}).get(str(b["cluster"]))
            write_json(D["state"] / f"{slug(b['cluster'])}.json", {"cluster": b["cluster"], "outcome": "accepted",
                                                                  "batch_of": rec["cluster"], "batch_verdict": bverdict})
            with open(ROOT / ("FEATURES-ACCEPTED.tsv" if rec.get("kind") == "feature" else "ACCEPTED.tsv"), "a") as f:
                f.write(f"{b['cluster']}\t{rec['branch']}\t{rec['head_after'][:12]}\t{verdict}\t{b.get('title', '')}"
                        f"\treviewed-by:{qa.get('reviewer', 'luna')} (batch with {rec['cluster']}: {bverdict})\n")
        retire_tree(rec, KEEP_ACCEPTED)
        log("qa", f"{s}: {verdict} ({qa['minutes']} min) - branch {rec['branch']} kept for the merge train"
            + (f" (+{len(rec['item']['batch'])} batched bug(s))" if (rec.get("item") or {}).get("batch") else ""))
    elif verdict in ("REWORK", "DISPUTE") and rnd < ((int((rec.get("item") or {}).get("round_base") or 0) + FEATURE_MAX_ROUNDS) if rec.get("kind") == "feature" else adaptive.settings()["max_rounds"]):
        item = rec.get("item") or {"cluster": rec["cluster"], "worktree": rec["lane_tree"], "title": rec.get("title")}
        notes = (f"{rname} verdict: {verdict}\n{v.get('summary', '')}\n\nBlocking failures - the only reasons this came back; fix exactly "
                 f"these:\n{blocking_text(qa)}\nWhat to do:\n{v.get('rework_instructions', '')}\n\nFull review: {out}")
        job = {"item": item, "round": rnd + 1, "notes": notes}
        try:      # owner 2026-10-18: the rework continues the session that implemented it (same model, context kept)
            sess = json.loads((D["runs"] / f"{s}.r{rnd}" / "opencode-session.json").read_text())
            if sess.get("session") and sess.get("model"):
                job["resume"] = {"session": sess["session"], "model": sess["model"]}
        except (OSError, ValueError):
            pass
        if rec.get("engine") == "luna":
            sess = issue_ledger.read(D["runs"] / f"{s}.r{rnd}" / "luna-session.json")
            if sess.get("session"):
                job["resume"] = sess
        if rec.get("lane"):       # a Claude lane's unit: same lane, same Claude session
            job["lane"] = rec["lane"]
            try:
                sess = json.loads((D["runs"] / f"{s}.r{rnd}" / "claude-session.json").read_text())
                if sess.get("session"):
                    job["resume"] = {"session": sess["session"], "model": sess.get("model"), "engine": "claude"}
            except (OSError, ValueError):
                pass
        write_json(D["rework"] / f"{s}.json", job)
        write_json(D["state"] / f"{s}.json", {**rec, "outcome": "rework"})
        log("qa", f"{s}: {verdict} - sent back (round {rnd + 1}): " + "; ".join(f"[{b['category']}] {b['what'][:90]}" for b in qa.get("blocking") or []))
    else:
        retire_tree(rec, True)
        if rec.get("kind") == "feature":
            write_json(D["parked"] / f"{s}.json", rec)          # features_queue.py moves it up a tier
            write_json(D["state"] / f"{s}.json", {**rec, "outcome": f"parked-{verdict.lower()}"})
            for b in (rec.get("item") or {}).get("batch") or []:
                (D["state"] / f"{slug(b['cluster'])}.json").unlink(missing_ok=True)
            log("qa", f"{s}: {verdict} - feature escalates a tier")
        else:
            item = rec.get("item") or {"cluster": rec["cluster"], "worktree": rec["lane_tree"], "title": rec.get("title")}
            notes = (f"{rname} verdict: {verdict}\n{(v or {}).get('summary', '')}\n\nBlocking failures:\n{blocking_text(qa)}\n"
                     f"{(v or {}).get('rework_instructions', '')}\n\nFull review: {out}")
            bug_escalate(item, rnd, f"QA {verdict} after {rnd} round(s)", notes)


def qa_guarded(path, reviewer=None):
    try:
        qa_one(path, reviewer)
        try:
            # owner 2026-10-15: per-model, difficulty-weighted success + speed. A subprocess, so edits to
            # benchmark.py apply at once (an in-process import kept the 10-16 09:10 code until restart).
            subprocess.run([sys.executable, str(ROOT / "benchmark.py")], capture_output=True, timeout=300, check=True)
        except Exception as e:        # the benchmark never stops a review
            log("qa", f"benchmark update failed: {e!r}")
        try:                          # escalate parked feature units one tier (free -> Muse -> Sonnet list)
            subprocess.run([sys.executable, str(ROOT / "features_queue.py")], capture_output=True, timeout=120)
        except Exception as e:
            log("qa", f"feature queue refresh failed: {e!r}")
    except Exception as e:
        log("qa", f"{path.name}: driver error {e!r} - moved to parked")
        if path.exists():
            path.replace(D["parked"] / path.name)
        s = path.name.split(".r")[0]
        write_json(D["state"] / f"{s}.json", {"cluster": s, "outcome": "qa-driver-error", "error": repr(e)})


def qa_loop():
    import concurrent.futures
    (ROOT / "qa.pid").write_text(str(os.getpid()))
    log("qa", f"QA start ({QA_WORKERS} Luna reviews, plus autoscaled Haiku 5.5 second reviewers)")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(QA_WORKERS, 1) + 8)
    active = {}          # future -> (pending file name, reviewer)
    idle_logged = False
    last_decision = None
    while True:
        for f in [f for f in active if f.done()]:
            active.pop(f)
        if stopped("QA"):
            if not active:
                break
            time.sleep(15)
            continue
        if (ROOT / "WORKFLOW-MAINTENANCE").exists():
            time.sleep(5)
            continue
        qwen_reviews = not luna_available()
        haiku_ok = haiku_review_ok()
        if qwen_reviews and not haiku_ok and ((ROOT / "LOCAL-QWEN").exists() or (ROOT / "QWEN-PAUSED").exists()):
            if not idle_logged:
                log("qa", "Luna and Haiku unavailable - reviews stay pending; no local Qwen review fallback")
            idle_logged = True
            time.sleep(30)
            continue
        if qwen_reviews and not haiku_ok and not strata_health():
            if not idle_logged:
                log("qa", f"{light_name()} unavailable and Strata not up yet - waiting to review with Qwen")
            idle_logged = True
            time.sleep(60)
            continue
        names = {n for n, _ in active.values()}
        pending = [p for p in sorted(D["qa/pending"].glob("*.json"), key=os.path.getmtime)
                   if p.name not in names]
        if not pending and not active:
            if (ROOT / "FIX-DRAINED").exists():
                log("qa", "fixer drained and QA queue empty - QA done")
                break
            if not idle_logged:
                log("qa", "waiting for fixes")
            idle_logged = True
            time.sleep(60)
            continue
        idle_logged = False
        if qwen_reviews and not haiku_ok:
            for p in pending[:max(0, 1 - len(active))]:
                active[pool.submit(qa_guarded, p, None)] = (p.name, "qwen")
            time.sleep(15)
            continue
        # Luna takes its LUNA-CAP slots; Haiku reviewers scale with the backlog and free memory, and never
        # review a Haiku-made fix (those wait for Luna).
        k = autoscale.knobs()
        busy = [r for _, r in active.values()]
        # owner 2026-10-20 "let luna autoscale to meet the review requirements": Luna's review sessions follow the
        # backlog (AUTOSCALE.json luna_review_*), never above LUNA-CAP qa / QA_WORKERS
        luna_slots = 0 if qwen_reviews else autoscale.luna_reviewers(len(pending) + len(active),
                                                                      min(luna_cap("qa"), QA_WORKERS), k)
        backlog = len(pending) + len(active)
        want_haiku = autoscale.haiku_reviewers(backlog, luna_slots, k) if haiku_ok else 0
        room_luna = max(0, luna_slots - busy.count("luna"))
        room_haiku = max(0, min(want_haiku - busy.count("haiku"), autoscale.ram_starts(float(k["ram_haiku_gb"]), k)))
        for p in pending:
            if room_luna <= 0 and room_haiku <= 0:
                break
            try:
                needs_luna = fixed_by_haiku(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
            if room_luna > 0:
                active[pool.submit(qa_guarded, p, "luna")] = (p.name, "luna")
                room_luna -= 1
            elif room_haiku > 0 and not needs_luna:
                active[pool.submit(qa_guarded, p, "haiku")] = (p.name, "haiku")
                room_haiku -= 1
        busy = [r for _, r in active.values()]
        decision = {"backlog": backlog, "luna_slots": luna_slots, "luna_busy": busy.count("luna"),
                    "haiku_target": want_haiku, "haiku_busy": busy.count("haiku"), "haiku_ok": haiku_ok}
        autoscale.report("qa", decision)
        key = (luna_slots, want_haiku, busy.count("haiku"))
        if key != last_decision:
            log("qa", f"autoscale: backlog {backlog}, Luna {busy.count('luna')}/{luna_slots}, "
                      f"Haiku {busy.count('haiku')}/{want_haiku}")
            last_decision = key
        time.sleep(15)
    pool.shutdown(wait=True)
    (ROOT / "qa.pid").unlink(missing_ok=True)
    log("qa", "QA stopped")


# ------------------------------------------------------------------ status
def status():
    q = load_queue()
    st = {p.stem: json.loads(p.read_text()) for p in D["state"].glob("*.json")}
    skip = luna_easy_attempted()
    remaining = [i for i in q if slug(i["cluster"]) not in st and slug(i["cluster"]) not in skip]
    from collections import Counter
    c = Counter(r.get("outcome", "?") for r in st.values())
    h = strata_health()
    print(f"Strata: {'UP, context ' + str(h.get('max_context')) if h else 'DOWN'}   free disk: {free_gb():.0f} GB")
    print(f"bugs: {len(q)} in queue, {len(remaining)} untouched, {len(skip)} already tried by Luna-easy")
    print(f"outcomes: {dict(c)}")
    print(f"QA pending: {len(list(D['qa/pending'].glob('*.json')))}   reworks queued: {len(list(D['rework'].glob('*.json')))}"
          f"   parked: {len(list(D['parked'].glob('*.json')))}")
    for stream in ("fix", "qa"):
        p = D["logs"] / f"{stream}.log"
        if p.exists():
            print(f"--- {stream}.log")
            print("\n".join(p.read_text().splitlines()[-5:]))
    for flag in ("STOP", "STOP-FIX", "STOP-QA", "FIX-DRAINED"):
        if (ROOT / flag).exists():
            print(f"[{flag} is set]")


# All routes (including legacy ports and Claude lanes) obey one issue policy.
next_item = workflow_runtime.selector(next_item)
next_claude_item = workflow_runtime.selector(next_claude_item, claude=True)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd in ("fix", "qa"):
        if (ROOT / "DISABLED").exists():
            sys.exit("Qwen pipeline DISABLED by the owner (2026-10-08): see " + str(ROOT / "DISABLED"))
        import fcntl
        with (ROOT / f"{cmd}.loop.lock").open("a") as loop_lock:
            try:
                fcntl.flock(loop_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                sys.exit(f"{cmd} loop already owns its lock")
            {"fix": fix_loop, "qa": qa_loop}[cmd]()
    else:
        {"status": status}.get(cmd, lambda: sys.exit(__doc__))()
