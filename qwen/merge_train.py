#!/usr/bin/env python3
"""Automatic merge trains (owner 2026-10-19: "lets do those auto merge trains").

Every run (zene-merge-train.timer, every 3 h) checks whether a train is due - at least MIN_CANDIDATES accepted
branches waiting, or any waiting for more than MAX_WAIT_H - and if so builds the next one in the zene-cachyos worktree:

  1. candidates: accepted fix and feature commits (ACCEPTED.tsv, FEATURES-ACCEPTED.tsv) not on the fix base, minus
     originals superseded by a -port job and merge/MERGE-EXCLUDE.txt (semantic exclusions from earlier trains);
  2. dry run (git merge-tree, merge/union.attributes + the add/add driver): clean ones merge as they are; a conflict
     only in generated docs (README, release notes, the commands snapshot, all-sources) merges keeping ours (they are
     regenerated below); any other conflict becomes a port job (re-apply by hand on the next train);
  3. merge onto 050/m0-train-<N+1>; build everything (+ the EXCLUDE_FROM_ALL NAM/RT probes); a build error is pinned
     on the merge that last touched the failing file, which is reverted and ported (up to MAX_REVERTS times);
  4. regenerate the commands snapshot from a live instance (wasm.* out), every canary figure, the A16 histogram and
     tests/all-sources.txt; commit;
  5. full ctest bar; a failure beyond merge/baseline-failures.txt is rerun alone (load flakes pass); a persistent one
     is pinned on the merge that touched its test source, else examined (flaky? a crash pinned from its gdb stack?
     a first-parent bisect?) - a pinned merge is reverted + ported once its revert is shown to fix the test; a flaky
     or unpinnable failure becomes a test-fix job and joins the baseline, an unpinnable crash blocks; every baseline
     test failing in the bar gets a test-fix job (see "failures the test source cannot pin" below);
  6. gates: every gate that passed on the previous train must pass; complexity / file-length items new to this train
     are queued as split jobs (the ratchets are red on purpose and are never re-anchored);
  7. promote: FIX-BASE, a STATUS.md, restart the patrol on the new base, notify the owner. Anything unresolved leaves
     the train unpromoted with the reason in logs/merge-train.log and a Telegram message.
Nothing is pushed. `merge_train.py --dry-run` only reports candidates; `--force` ignores the due check.
"""
import argparse
import datetime
import fcntl
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
M = ROOT / "merge"
PROG = Path.home() / "Documents/AI_KOS_PROJECT/projects/zene-studio-program"
WT = PROG / "zene-cachyos"
LOG = ROOT / "logs/merge-train.log"
MIN_CANDIDATES = 25
MAX_MERGES = 40            # 2026-10-25: oldest 40 per train; train 10 (143 merges) failed 9 times in 25 h, trains of <= 21 promoted first try
MAX_WAIT_H = 24
COHORT_MAX_WAIT_H = 24     # an integration that waits longer than this stops holding the next train (the owner is told)
MAX_REVERTS = 6
MIN_FREE_GB = 40
DOC_ONLY = re.compile(r"^(README\.md|docs/RELEASE-NOTES[^/]*\.md|docs/KNOWN-LIMITATIONS\.md|tests/all-sources\.txt|"
                      r"tools/mcp-zene-control/zene_control/commands_snapshot\.json)$")
CANARY = re.compile(r"<!-- canary -->(\d+) ids / (\d+) groups<!-- /canary -->")
DEPS = Path.home() / ".local/zene-deps"
ENV_SH = (f"unset LMMS_PLUGIN_DIR; export QT_QPA_PLATFORM=offscreen PKG_CONFIG_PATH={DEPS}/root/usr/lib/pkgconfig "
          f"LD_LIBRARY_PATH={DEPS}/root/usr/lib:{DEPS}/onnxruntime-linux-x64-1.28.0/lib:{WT}/third_party/wasmtime/lib "
          f"CCACHE_DIR=$HOME/.cache/ccache-bunny CCACHE_BASEDIR={PROG} CCACHE_NOHASHDIR=true; ")


def log(msg):
    line = f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def bar_jobs():
    """ctest parallelism for the full bar: merge/BAR-JOBS (autopilot.py halves it after an OOM kill), else 8."""
    try:
        return max(1, int((M / "BAR-JOBS").read_text().strip()))
    except (OSError, ValueError):
        return 8


def notify(text):
    subprocess.run(["notify-me", text], capture_output=True, timeout=60)


def git(*a, check=False, cwd=WT):
    r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def gitm(*a):
    """git with the train's merge attributes and add/add driver."""
    return ["git", "-C", str(WT), "-c", f"core.attributesFile={M / 'union.attributes'}",
            "-c", f"merge.addadd.driver={M / 'addadd-driver.py'} %O %A %B %P", "-c", "merge.addadd.name=addadd", *a]


def sh(cmd, logf, cwd=WT, timeout=6 * 3600):
    with open(logf, "w") as f:
        try:
            return subprocess.run(["bash", "-c", ENV_SH + cmd], cwd=cwd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            return 124


def free_gb():
    import shutil
    return shutil.disk_usage(PROG).free / 1e9


# ------------------------------------------------------------------ candidates
def accepted_rows():
    rows = []
    for f in ("FEATURES-ACCEPTED.tsv", "ACCEPTED.tsv"):
        for line in (ROOT / f).read_text().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                rows.append({"cluster": parts[0], "branch": parts[1], "sha": parts[2], "file": f})
    return rows


def candidates(base):
    exclude = set((M / "MERGE-EXCLUDE.txt").read_text().split()) if (M / "MERGE-EXCLUDE.txt").exists() else set()
    # an original whose port job exists (queued in rework/ or already run) is superseded by it
    ported = {p.stem[:-len("-port")] for d in ("state", "rework") for p in (ROOT / d).glob("*-port.json")} | \
             {r["cluster"][:-len("-port")] for r in accepted_rows() if r["cluster"].endswith("-port")}
    out, seen = [], set()
    for r in accepted_rows():
        b = r["branch"]
        if b in seen or b in exclude or r["cluster"] in ported:
            continue
        seen.add(b)
        sha = git("rev-parse", "-q", "--verify", f"{r['sha']}^{{commit}}") or git("rev-parse", "-q", "--verify", f"{b}^{{commit}}")
        if not sha or subprocess.run(["git", "-C", str(WT), "merge-base", "--is-ancestor", sha, base]).returncode == 0:
            continue
        out.append({**r, "commit": sha})
    return out


def dry_run(base, cands):
    tip = git("rev-parse", base)
    clean, docs, conflict = [], [], []
    for c in cands:
        r = subprocess.run(gitm("merge-tree", "--write-tree", "--name-only", "--no-messages", tip, c["commit"]),
                           capture_output=True, text=True)
        lines = r.stdout.split("\n")
        if r.returncode == 0:
            tip = git("commit-tree", lines[0].strip(), "-p", tip, "-p", c["commit"], "-m", f"dry {c['branch']}")
            clean.append(c)
            continue
        files = [l for l in lines[1:] if l.strip()]
        (docs if files and all(DOC_ONLY.match(f) for f in files) else conflict).append({**c, "files": files})
    return clean, docs, conflict


# ------------------------------------------------------------------ port jobs
def port_job(c, why, exclude=False):
    """Keep one integration request per issue; activate it after the cumulative train is validated."""
    import integration_jobs
    orig = c["cluster"]
    item = None
    for src in (ROOT / "state" / f"{orig}.json", *sorted((ROOT / "runs").glob(f"{orig}.r*/record.json"))):
        try:
            item = json.loads(src.read_text()).get("item") or item
        except (OSError, ValueError):
            pass
    if not item and orig.startswith("Q050-"):
        import issue_ledger
        identity = issue_ledger.issue_id(orig)
        for row in (PROG / "plans/queue-050.jsonl").read_text().splitlines():
            try:
                unit = json.loads(row)
            except ValueError:
                continue
            if unit.get("queue_id") == identity:
                item = {**unit, "cluster": orig, "issue_id": identity, "worktree": str(WT), "feature_tier": "sonnet"}
                break
    if not item:
        log(f"{orig}: cannot queue integration without its accepted item ({why})")
        return
    request = integration_jobs.defer(c, why, item, regression=exclude)
    path = M / "MERGE-EXCLUDE.txt"
    existing = set(path.read_text().split()) if path.exists() else set()
    if c["branch"] not in existing:
        with path.open("a") as stream:
            stream.write(c["branch"] + "\n")
    log(f"{orig}: accepted patch awaiting focused integration ({request}); no new port identity")


# ------------------------------------------------------------------ figures
def snapshot_and_figures(train):
    """Live commands snapshot (wasm.* out), canary figures, A16 histogram, all-sources; one commit."""
    out = M / f"{train}-snapshot.json"
    code = f'''
import os, subprocess, sys, json
WT = {str(WT)!r}; sys.path.insert(0, WT + "/tests"); sys.path.insert(0, WT + "/tools/mcp-zene-control")
import control_socket_harness as H
inst = H.start_instance(WT + "/build/zene")
try:
    H.wait_for_socket(inst)
    rc = subprocess.run([sys.executable, WT + "/tools/mcp-zene-control/snapshot_commands.py", "--socket", inst.socket_path,
                         "--out", {str(out)!r}], capture_output=True).returncode
finally:
    for n in ("stop", "close", "terminate", "kill"):
        if hasattr(inst, n):
            getattr(inst, n)(); break
from zene_control import registry as R
b = json.load(open({str(out)!r}))
cmds = [c for c in b["commands"] if not c["id"].startswith("wasm.")]
bundle = R.bundle_from_result({{"commands": cmds, "count": len(cmds), "proto": b.get("proto")}}, b["instance"])
bundle["captured_at"] = b["captured_at"]
R.save_bundle(WT + "/tools/mcp-zene-control/zene_control/commands_snapshot.json", bundle)
print(bundle["surface"]["id_count"], bundle["surface"]["group_count"])
'''
    r = subprocess.run(["bash", "-c", ENV_SH + f"python3 - <<'PYEOF'\n{code}\nPYEOF"], cwd=WT, capture_output=True, text=True)
    nums = r.stdout.split()
    if r.returncode or len(nums) < 2:
        raise RuntimeError(f"live snapshot failed: {(r.stderr or r.stdout)[-400:]}")
    ids, groups = nums[-2], nums[-1]
    for f in git("grep", "-l", "<!-- canary -->").splitlines():
        p = WT / f
        p.write_text(CANARY.sub(f"<!-- canary -->{ids} ids / {groups} groups<!-- /canary -->", p.read_text()))
    # A16 histogram: ReversibilityContractTest names what the table measures when the published block is stale
    sh("cmake --build build --target ReversibilityContractTest -- -j8", M / f"{train}-rev-build.log")
    sh("cd build/tests && ctest -R '^ReversibilityContractTest$' --output-on-failure", M / f"{train}-rev.log")
    m = re.search(r"the table measures (\d+) rows = (\d+) true_inverse \+ (\d+) snapshot \+ (\d+) irreversible \+ (\d+) not_mutating",
                  (M / f"{train}-rev.log").read_text())
    if m:
        rows, ti, sn, ir, nm = m.groups()
        notes = WT / "docs/RELEASE-NOTES-v0.3.0-alpha.md"
        t = notes.read_text()
        measured = f"rows={rows} true_inverse={ti} snapshot={sn} irreversible={ir} not_mutating={nm}"
        t = re.sub(r"(<!-- A16-HISTOGRAM-BEGIN\n\s*measured: )[^\n]+", lambda mm: mm.group(1) + measured, t, count=1)
        t = t.replace("\n<!-- A16-HISTOGRAM-BEGIN", f"\n**M0 merge train {train.rsplit('-', 1)[-1]}** (`050/m0-{train}`, "
                      f"{datetime.date.today()}, automatic): `ReversibilityContractTest` on the merged tree measures `{measured}`.\n"
                      f"\n<!-- A16-HISTOGRAM-BEGIN", 1)
        notes.write_text(t)
    # all-sources: the byte-exact output of the recipe in its own header
    hdr = []
    for line in (WT / "tests/all-sources.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            break
        hdr.append(line)
    # 2026-10-24: run the header's own "Regenerate with" command. A hard-coded copy of it lacked
    # plugins/SfzPlayer/sfizz/, so trains committed 1,148 vendored paths and tests/all-sources-reproduce.sh failed.
    recipe = manifest_recipe(hdr)
    done = subprocess.run(["bash", "-c", recipe], cwd=WT, capture_output=True, text=True) if recipe else None
    if done and done.returncode == 0 and done.stdout.strip():
        (WT / "tests/all-sources.txt").write_text("\n".join(hdr) + "\n" + done.stdout)
    else:
        log("all-sources NOT re-derived: no runnable 'Regenerate with' recipe in its header" +
            (f" (rc {done.returncode}: {done.stderr[-200:]})" if done else ""))
    git("add", "-A", "--", "README.md", "docs", "tools/mcp-zene-control/zene_control/commands_snapshot.json", "tests/all-sources.txt",
        *[f for f in git("grep", "-l", "<!-- canary -->").splitlines()])
    if git("diff", "--cached", "--name-only"):
        git("commit", "-q", "-m", f"chore({train}): snapshot regenerated live ({ids} ids / {groups} groups, wasm.* out), canary, "
            f"A16 histogram, all-sources re-derived (automatic merge train)\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", check=True)
    return ids, groups


# ------------------------------------------------------------------ culprits
def norm(path):
    path = re.sub(r"^(\.\./)+", "", path)
    return path[len(str(WT)) + 1:] if path.startswith(str(WT) + "/") else path


def pin_build_error(base, text):
    """The merge to revert for a compile error: merges touching the failing file or a file the compiler's notes name
    (a changed header), preferring a bug fix (cheap to re-port) over a feature (2026-10-19, train 8: Q050-090 called a
    TaskCenter API another merge had changed)."""
    files = []
    for m in re.finditer(r"^\s*([^\s:]+\.(?:cpp|cc|c|h|hpp)):\d+:\d+:(?: (error|note)|\s*$)", text, re.M):
        f = norm(m.group(1))
        if f not in files and not f.startswith("/") and (WT / f).exists():
            files.append(f)
    hits = []
    for f in files:
        for h, subj in merges_touching(base, f):
            if (h, subj) not in hits:
                hits.append((h, subj))
    if not hits:
        return None
    fixes = [x for x in hits if "Merge feat/" not in x[1]]
    return (fixes or hits)[0]



def merges_touching(base, path):
    out = []
    for line in git("log", "--merges", "--format=%H %s", f"{base}..HEAD").splitlines():
        h, subj = line.split(" ", 1)
        if path in git("diff", "--name-only", f"{h}^1", h).splitlines():
            out.append((h, subj))
    return out


# ------------------------------------------------------------------ failures the test source cannot pin
# 2026-10-19 (owner: "build the gdb crash pinning, and fix other broken tests that pop up"). Train 8's agent_surface gate
# died mid-sweep: heap corruption from a strtok race that a DrumSynth fix exposed - no merge touched the gate, so the
# source pin found nothing. Each persistent failure the source pin misses is now examined in order:
#   1. flaky?  four more solo runs; any pass -> a test-fix job, onto the baseline (the test stays red, never weakened);
#   2. crash?  rerun under gdb with glibc's malloc checking (catches the first bad free, not a later victim); the
#      stack's app frames are matched to the merges that changed the functions in them;
#   3. bisect  over the train's first-parent history (targeted build + the test at each step);
# a pinned merge is reverted and ported only if its revert makes the test pass (confirm by reversal: bisect misled on
# train 4). An unpinned crash blocks the train; an unpinned plain failure gets a test-fix job and joins the baseline
# (owner: features merge when fundamentally working, minor test failures go to the bug queue).
# Every test that fails in the bar while on the baseline also gets a test-fix job, once; a baseline entry leaves the
# baseline when it passes in a bar after its test-fix job was accepted.
CRASH_RE = re.compile(r"received signal (SIG(?:SEGV|ABRT|BUS|FPE|ILL|TRAP))")
FRAME_RE = re.compile(r"^#\d+\s+(?:0x[0-9a-f]+ in )?(.+?)(?: from (\S+))?$")
SKIP_FRAME = re.compile(r"^(std::|__|Q[A-Z]\w*::|QtPrivate|QMetaObject|QObject|QCoreApplication|QThread|QRunnable|main$|start_thread|clone)")
BISECT_PER_RUN = 2
BASELINE_LOG = M / "baseline-added.jsonl"


def test_slug(t):
    return "test-" + re.sub(r"[^A-Za-z0-9_.-]", "-", t)


def test_source(t):
    """The test's own source: tests/src/<dir>/<t>.cpp, else the script its ctest command runs."""
    src = next(iter(git("ls-files", f"tests/src/*/{t}.cpp", f"tests/{t}.py", f"tests/src/{t}.cpp").splitlines()), "")
    if not src:
        argv = ctest_command(t)[0] or []
        src = next((norm(a) for a in argv if a.startswith(str(WT) + "/tests/") and re.search(r"\.(py|sh|lua)$", a)), "")
    return src


def solo(t, logf, timeout=900):
    return sh(f"cd build/tests && ctest -R '^{t}$' --output-on-failure --timeout {timeout}", logf)


def excerpt(text, limit=2500):
    keep = [l for l in text.splitlines() if re.search(r"FAIL|Loc: |Error|error:|Traceback|assert|Exception|Expected|Actual|"
                                                      r"timed? ?out|Timeout| dead ", l)]
    body = "\n".join(keep[:40]) if keep else "\n".join(text.splitlines()[-40:])
    return body[-limit:]


def ctest_command(t):
    """argv, environment and working directory of one registered test, as `ctest -N -V` prints them."""
    import shlex
    r = subprocess.run(["bash", "-c", ENV_SH + f"cd build/tests && ctest -N -V -R '^{t}$'"], cwd=WT, capture_output=True,
                       text=True, errors="replace")
    argv, env, cwd = None, {}, WT / "build/tests"
    lines = r.stdout.splitlines()
    for i, line in enumerate(lines):
        if m := re.match(r"^\d+: Test command: (.*)$", line):
            argv = shlex.split(m.group(1))
        elif m := re.match(r"^\d+: Working Directory: (.*)$", line):
            cwd = Path(m.group(1).strip())
        elif re.match(r"^\d+: Environment variables:\s*$", line):
            for nxt in lines[i + 1:]:
                if not (mm := re.match(r"^\d+:\s+(\w+)=(.*)$", nxt)):
                    break
                env[mm.group(1)] = mm.group(2)
    return argv, env, cwd


def gdb_run(train, t):
    """Run test t with the app (the test binary itself, or the `zene` it launches) under gdb + glibc malloc checking.
    Returns (signal, frames, backtrace text) of the first crash, or (None, [], "")."""
    import shlex
    import shutil
    argv, env, cwd = ctest_command(t)
    if not argv or not shutil.which("gdb"):
        return None, [], ""
    crash_dir = M / f"{train}-crash-{t}"
    shutil.rmtree(crash_dir, ignore_errors=True)
    crash_dir.mkdir()

    def wrapper(real):
        # gdb's own output (the signal, the stacks) goes to a file; the app's stdout/stderr stay where the test reads them
        w = crash_dir / f"gdb-{Path(real).name}"
        w.write_text("#!/bin/sh\n" + f'out="{crash_dir}/gdb-$$.txt"\n' + "exec gdb -q -batch -nx -iex 'set debuginfod enabled off' "
                     "-ex 'set confirm off' -ex 'set pagination off' -ex \"set logging file $out\" -ex 'set logging redirect on' "
                     "-ex 'set logging enabled on' -ex 'handle SIGPIPE nostop noprint pass' "
                     "-ex 'handle SIG32 SIG33 SIG34 SIG35 nostop noprint pass' "
                     "-ex 'set environment LD_PRELOAD=/usr/lib/libc_malloc_debug.so.0' "
                     "-ex 'set environment GLIBC_TUNABLES=glibc.malloc.check=3' "
                     f"-ex run -ex 'bt 40' -ex 'thread apply all bt 20' --args {shlex.quote(str(real))} \"$@\"\n")
        w.chmod(0o755)
        return str(w)

    apps = {str(WT / "build/zene"), str(WT / "build/lmms")}
    argv = [wrapper(a) if a in apps else a for a in argv]
    first = Path(argv[0])
    try:
        if str(first).startswith(str(WT)) and first.is_file() and first.read_bytes()[:4] == b"\x7fELF":
            argv[0] = wrapper(first)
    except OSError:
        pass
    envs = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    sh(f"cd {shlex.quote(str(cwd))} && env {envs} {' '.join(shlex.quote(a) for a in argv)}", M / f"{train}-gdb-{t}.log",
       timeout=2400)
    for f in sorted(crash_dir.glob("gdb-*.txt")):
        text = f.read_text(errors="replace")
        if m := CRASH_RE.search(text):
            return m.group(1), crash_frames(text[m.end():]), text[m.start():][:6000]
    return None, [], ""


def crash_frames(text):
    """The crashing thread's own frames (the `bt` right after the signal), app code only: [{name, method, file}]."""
    frames, started = [], False
    for line in text.splitlines():
        if not line.startswith("#"):
            if started:
                break
            continue
        started = True
        m = FRAME_RE.match(line.strip())
        if not m or (m.group(2) and not m.group(2).startswith(str(WT))):
            continue                        # a system library (libc, Qt, ...)
        s = m.group(1).replace("(anonymous namespace)::", "")
        at = re.search(r" at (\S+):\d+$", s)
        name = re.sub(r"^lmms::", "", re.split(r"[ (]", s, maxsplit=1)[0])
        if "<" in name or "lambda" in s.split("(")[0] or SKIP_FRAME.match(name) or name == "??":
            continue
        frames.append({"name": name, "method": name.rsplit("::", 1)[-1], "file": norm(at.group(1)) if at else ""})
    return frames


def pin_crash(base, frames):
    """The merge that changed the code a crash runs through: the topmost app frame whose function a merge's diff
    names, else the topmost frame whose file a merge changed; bug fixes before features. Returns (merge, frame)."""
    pick = lambda group: next((x for x in group if "Merge feat/" not in x[1]), group[0])
    loose = (None, None)
    for fr in frames[:12]:
        files = [fr["file"]] if fr["file"] else []
        if not files:
            pat = rf"(^|[^A-Za-z0-9_]){re.escape(fr['name'] if '::' in fr['name'] else fr['method'])}\s*\("
            files = git("grep", "-l", "-E", pat, "--", "*.cpp", "*.cc", "*.h", "*.hpp").splitlines()[:6]
        hits = list(dict.fromkeys(x for f in files for x in merges_touching(base, f)))
        if not hits:
            continue
        strong = [(h, s) for h, s in hits
                  if re.search(rf"\b{re.escape(fr['method'])}\b", git("diff", "-U3", f"{h}^1", h, "--", *files))]
        if strong:
            return pick(strong), fr
        if loose[0] is None:
            loose = (pick(hits), fr)
    return loose


def bisect_test(base, train, t):
    """First-parent bisect of a deterministic failure: the first train commit at which test t fails (a merge), or None.
    A step whose targeted build fails is skipped forward. The worktree is back on the train tip, rebuilt, after."""
    branch = git("branch", "--show-current")
    commits = git("rev-list", "--first-parent", "--reverse", f"{base}..HEAD").splitlines()
    has_target = bool(git("ls-files", f"tests/src/*/{t}.cpp", f"tests/src/{t}.cpp"))

    def fails(rev):
        git("checkout", "-q", "--detach", rev, check=True)
        if sh(f"cmake --build build --target zene {t if has_target else ''} -j10", M / f"{train}-bisect-build.log") != 0:
            return None
        rc = solo(t, M / f"{train}-bisect-{t}.log")
        return rc != 0 and "No tests were found" not in (M / f"{train}-bisect-{t}.log").read_text(errors="replace")

    try:
        if fails(base) is not False:
            log(f"bisect {t}: fails or does not build on {base} too - not this train's")
            return None
        lo, hi = -1, len(commits) - 1               # lo passes (the base), hi fails (the tip)
        while hi - lo > 1:
            probe, r = (lo + hi) // 2, None
            while probe < hi and (r := fails(commits[probe])) is None:
                probe += 1
            if r is None:
                log(f"bisect {t}: no buildable step before {commits[hi][:9]}")
                return None
            if r:
                hi = probe
            else:
                lo = probe
        subj = git("log", "-1", "--format=%s", commits[hi])
        log(f"bisect {t}: first failing commit {commits[hi][:9]} {subj[:90]}")
        return (commits[hi], subj) if subj.startswith("Merge ") else None
    finally:
        git("checkout", "-q", branch)
        sh("cmake --build build -j10", M / f"{train}-bisect-rebuild.log")


def examine(base, train, t, budget):
    """('culprit', (h, subj), why) | ('flaky', None, why) | ('crash', None, why) | ('unpinned', None, why)."""
    passes = sum(solo(t, M / f"{train}-repeat-{t}-{i}.log") == 0 for i in range(4))
    if passes:
        return "flaky", None, f"failed in the bar and alone, then passed {passes} of 4 more solo runs"
    sig, frames, bt = gdb_run(train, t)
    if sig:
        culprit, fr = pin_crash(base, frames)
        chain = " <- ".join(f["name"] for f in frames[:6]) or "no app frames"
        if culprit:
            return "culprit", culprit, f"crashes ({sig}) in {chain}; the merge changed {fr['name']}\n{bt[:3000]}"
        if budget["bisect"] > 0:
            budget["bisect"] -= 1
            if c := bisect_test(base, train, t):
                return "culprit", c, f"crashes ({sig}) in {chain}; bisect\n{bt[:3000]}"
        return "crash", None, f"crashes ({sig}) in {chain}\n{bt[:3000]}"
    if budget["bisect"] > 0:
        budget["bisect"] -= 1
        if c := bisect_test(base, train, t):
            return "culprit", c, "bisect"
    return "unpinned", None, "fails on every run; no merge could be pinned (test source, crash stack, bisect)"


def revert_confirmed(culprit, why, t, train, cands):
    """Revert + port the culprit only if the revert makes t pass; otherwise undo the revert and its port job."""
    exclude = M / "MERGE-EXCLUDE.txt"
    before_ex = exclude.read_text() if exclude.exists() else ""
    before_jobs = set((ROOT / "rework").glob("*.json"))
    revert_merge(*culprit, why, cands)
    sh("cmake --build build -j10", M / f"{train}-build-revert.log")
    if solo(t, M / f"{train}-confirm-{t}.log") == 0:
        return True
    git("reset", "-q", "--hard", "HEAD~1")
    for j in set((ROOT / "rework").glob("*.json")) - before_jobs:
        j.unlink()
    exclude.write_text(before_ex)
    sh("cmake --build build -j10", M / f"{train}-build-revert.log")
    log(f"reverting {culprit[1][:80]} did not make {t} pass - revert and port job undone")
    return False


def job_exists(t):
    s = test_slug(t)
    return any((ROOT / d / f"{s}.json").exists() for d in ("rework", "state")) or s in {r["cluster"] for r in accepted_rows()}


def test_job(t, kind, why, evidence, train):
    """A test-fix job for the fix loop (once per test: skipped while one is queued, in flight or accepted)."""
    s = test_slug(t)
    if job_exists(t):
        return None
    src = test_source(t)
    text = (f"BROKEN TEST {t} ({kind}) on merge train {train}: {why}.\n"
            f"Test: ctest -R '^{t}$' from build/tests" + (f"; source {src}" if src else "") + ".\n"
            f"Failure output:\n{evidence}\n\n"
            "Find out whether the PRODUCT or the TEST is wrong and fix the cause. A product bug is fixed in the product "
            "(the test then proves it). A test bug (a race, a timing assumption, a stale expectation, a fixture "
            "problem) is fixed in the test - deterministically: latches, injected clocks or seams, never sleeps or "
            "retries. Never weaken, skip, delete or loosen an assertion, never raise a tolerance or a timeout to make "
            "it pass, never add it to a skip list. If it only fails because of this machine's environment (a missing "
            "device, network, package), report BLOCKED with the evidence instead of changing anything.")
    item = {"pass": train, "finding_id": s, "cluster": s, "kind": "test-fix",
            "title": f"Broken test {t} ({kind}) on {train}", "file": src or f"tests (ctest {t})", "line": "",
            "text": text, "lane": "core", "angle": "tests", "realness": "likely_open_bug", "clef_tier": "new:needs_context",
            "difficulty": "needs_context", "members": "[]"}
    (ROOT / "rework" / f"{s}.json").write_text(json.dumps({"item": item, "round": 1, "notes": "TEST FIX JOB: " + text}, indent=1))
    log(f"{t}: test-fix job {s} ({kind})")
    return s


def add_to_baseline(t, train, kind, job):
    b = M / "baseline-failures.txt"
    names = b.read_text().split()
    if t not in names:
        b.write_text("\n".join(names + [t]) + "\n")
    with BASELINE_LOG.open("a") as f:
        f.write(json.dumps({"test": t, "train": train, "kind": kind, "job": job or test_slug(t),
                            "added": datetime.datetime.now().isoformat(timespec="seconds")}) + "\n")
    log(f"{t}: on the baseline ({kind}); stays red until its test-fix job lands")


def baseline_upkeep(failed, baseline, train):
    """Test-fix jobs for baseline tests failing in this bar; drop entries that pass after their job was accepted."""
    accepted = {r["cluster"] for r in accepted_rows()}
    for t in sorted(failed & baseline):
        if not job_exists(t):
            logf = M / f"{train}-solo-{t}.log"      # the -j8 bar interleaves output; a solo run gives clean evidence
            solo(t, logf)
            ev = excerpt(logf.read_text(errors="replace"))
            if re.search(r"SEGFAULT|Subprocess aborted|Exception:|Child aborted|signal", logf.read_text(errors="replace")):
                sig, frames, bt = gdb_run(train, t)       # a crash: the job gets the stack, not just ctest's verdict
                if sig:
                    ev = f"crashes ({sig}) in " + " <- ".join(f["name"] for f in frames[:6]) + f"\n{bt[:2500]}\n\n{ev}"
            test_job(t, "on the baseline", "it fails on every train and sits in the train's known-failure list", ev, train)
    healed = sorted(t for t in baseline - failed if test_slug(t) in accepted)
    if healed:
        b = M / "baseline-failures.txt"
        b.write_text("\n".join(n for n in b.read_text().split() if n not in healed) + "\n")
        baseline -= set(healed)
        log(f"off the baseline (pass again after their test-fix job landed): {', '.join(healed)}")


class RebuildWithout(Exception):
    """A merge that must leave the train could not be reverted (later commits touch the same lines): the train is
    rebuilt from the base without it (2026-10-20: train 9 aborted on 'could not revert' and blocked every later train)."""


def revert_merge(h, subj, why, cands):
    m = re.search(r"Merge (\S+)", subj)
    c = next((x for x in cands if m and x["branch"] == m.group(1)), None)
    git("revert", "--no-edit", "-m", "1", h)
    if git("status", "--porcelain", "--untracked-files=no"):
        git("revert", "--abort")
        if c:
            port_job(c, why, exclude=True)
        raise RebuildWithout(subj)
    if c:
        port_job(c, why, exclude=True)
    log(f"reverted {subj[:100]} ({why})")


def manifest_recipe(header):
    """The shell command under "Regenerate with" in tests/all-sources.txt's header - the same extraction as
    tests/all-sources-reproduce.sh's recipe_of awk: from "# Regenerate with" to "# Verify it", "# ?" stripped."""
    lines, on = [], False
    for line in header:
        if line.startswith("# Regenerate with"):
            on = True
            continue
        if on and (line.startswith("# Verify it") or not line.startswith("#")):
            break
        if on:
            lines.append(re.sub(r"^# ?", "", line))
    recipe = "\n".join(lines).strip()
    return recipe if "git ls-files" in recipe else ""


# ------------------------------------------------------------------ freshness gate (research/code-factories)
def freshness_gate(base, cands):
    """(config or None, early-train reason or None). Off unless qwen/FRESHNESS-GATE.json enables it; never fatal."""
    try:
        import conflict_forecast
        cfg = conflict_forecast.config()
        if not cfg["enabled"] or not cands:
            return None, None
        reason = conflict_forecast.early_due(conflict_forecast.forecast(WT, base, cands), cfg) \
            if cfg["early_train"] else None
        return cfg, reason
    except Exception as e:      # noqa: BLE001
        log(f"freshness gate ignored: {e!r}")
        return None, None


def freshness_order(base, cands):
    try:
        import conflict_forecast
        ordered = conflict_forecast.order(conflict_forecast.forecast(WT, base, cands), cands)
        if [c["commit"] for c in ordered] != [c["commit"] for c in cands]:
            log("freshness gate: most exposed branches merge first")
        return ordered
    except Exception as e:      # noqa: BLE001
        log(f"freshness order ignored: {e!r}")
        return cands


# ------------------------------------------------------------------ the train
def run(force=False, dry=False):
    base = (ROOT / "FIX-BASE").read_text().strip()
    n = int(re.search(r"train-(\d+)$", base).group(1)) + 1
    train = f"train-{n}"
    new = f"050/m0-{train}"
    import integration_jobs
    overdue = []
    cohort = integration_jobs.unsettled(COHORT_MAX_WAIT_H, overdue)
    if overdue and not dry:      # the daily digest reports these lines; they stay queued
        log(f"not waiting for {len(overdue)} integration(s): {', '.join(overdue)}")
    if cohort and not dry:
        log(f"train postponed: keep {base} pinned while {len(cohort)} accepted patches finish focused integration "
            f"({', '.join(cohort)}; each holds the train for at most {COHORT_MAX_WAIT_H} h)")
        return 0
    cands = candidates(base)
    age_h = (time.time() - (ROOT / "FIX-BASE").stat().st_mtime) / 3600
    log(f"{len(cands)} accepted commits waiting for {base} (base is {age_h:.0f} h old)")
    if dry:
        for c in cands:
            print(c["cluster"], c["branch"])
        return 0
    pending_integrations = list((ROOT / "integration-pending").glob("*.json"))
    if not cands and not force:      # 2026-10-25: train 14 ran a whole build + bar with 0 merges
        log("no train due (nothing to merge)")
        return 0
    gate, early = freshness_gate(base, cands)
    if early:
        log(f"freshness gate: train due early - {early}")
    if not force and not pending_integrations and not early and len(cands) < MIN_CANDIDATES and \
            not (cands and age_h > MAX_WAIT_H):
        log("no train due")
        return 0
    if len(cands) > MAX_MERGES:
        log(f"taking the oldest {MAX_MERGES} of {len(cands)}; the rest wait for the next train")
        cands = cands[:MAX_MERGES]
    if gate and gate.get("order"):
        cands = freshness_order(base, cands)
    if free_gb() < MIN_FREE_GB:
        log(f"only {free_gb():.0f} GB free - train postponed")
        notify(f"Merge train postponed: only {free_gb():.0f} GB disk free.")
        return 1
    cur = git("branch", "--show-current")
    if cur != base and not git("status", "--porcelain", "--untracked-files=no") and \
            re.fullmatch(r"050/m0-train-\d+(-attempt-\d+)?", cur or ""):
        # 2026-10-25: an OOM-killed train never reaches fail(), so the tree stayed on train-16 and every run
        # postponed for 25 h. A clean tree on an unpromoted train branch is a dead attempt: go back to the base
        # (the branch is kept; the next attempt renames it <train>-attempt-<ts>).
        log(f"zene-cachyos was left on unpromoted {cur} (killed run?) - switching back to {base}")
        git("switch", base, check=True)
    if git("status", "--porcelain", "--untracked-files=no") or git("branch", "--show-current") != base:
        log(f"zene-cachyos is not a clean checkout of {base} - train postponed")
        return 1
    clean, docs, conflict = dry_run(base, cands)
    log(f"dry run: {len(clean)} clean, {len(docs)} docs-only conflicts, {len(conflict)} code conflicts")
    for c in conflict:
        port_job(c, f"merge conflict with the newer line in: {' '.join(c['files'][:6])}")
    if git("rev-parse", "-q", "--verify", new):      # an earlier unpromoted attempt: keep it under another name
        git("branch", "-m", new, f"{new}-attempt-{int(time.time())}", check=True)
    git("switch", "-c", new, base, check=True)
    merged = []
    for c in clean + docs:
        args = ["merge", "--no-ff", "--no-edit"] + (["-X", "ours"] if c in docs else []) + [
            "-m", f"Merge {c['branch']} (accepted; {new}, automatic train)\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", c["commit"]]
        if subprocess.run(gitm(*args), capture_output=True).returncode == 0:
            merged.append(c)
        else:
            git("merge", "--abort")
            port_job(c, "conflict while merging onto the train")
    log(f"merged {len(merged)} onto {new} ({git('rev-parse', '--short', 'HEAD')})")
    # build, pinning errors on merges
    for attempt in range(MAX_REVERTS + 1):
        rc = sh("cmake --build build -j10 && cmake --build build --target nam_harness nam_profile rt_alloc_probe -j6",
                M / f"{train}-build.log")
        if rc == 0:
            break
        text = (M / f"{train}-build.log").read_text(errors="replace")
        culprit = pin_build_error(base, text)
        if attempt == MAX_REVERTS or not culprit:
            return fail(new, base, f"build fails and no merge could be pinned (see {M / (train + '-build.log')})")
        revert_merge(*culprit, f"breaks the build on {new}", merged)
    ids, groups = snapshot_and_figures(train)
    # bar
    baseline = set((M / "baseline-failures.txt").read_text().split())
    budget = {"bisect": BISECT_PER_RUN}
    for attempt in range(MAX_REVERTS + 1):
        sh("cmake --build build -j10", M / f"{train}-build-bar.log")
        sh(f"cd build/tests && ctest -j{bar_jobs()} --timeout 900 --output-on-failure", M / f"{train}-bar.log")
        text = (M / f"{train}-bar.log").read_text(errors="replace")
        failed = set(re.findall(r"\d+ - (\S+) \(", text.split("The following tests FAILED")[-1])) if "The following tests FAILED" in text else set()
        baseline_upkeep(failed, baseline, train)
        new_fail = sorted(failed - baseline)
        persistent = []
        for t in new_fail:
            if sh(f"cd build/tests && ctest -R '^{t}$' --output-on-failure --timeout 900", M / f"{train}-solo-{t}.log") != 0:
                persistent.append(t)
        summary = re.search(r"(\d+)% tests passed, (\d+) tests failed out of (\d+)", text)
        log(f"bar: {summary.group(0) if summary else '?'}; beyond baseline {new_fail}; persistent {persistent}")
        if not persistent:
            break
        culprit = None
        for t in persistent:
            src = test_source(t)
            mt = merges_touching(base, src) if src else []
            if mt:
                culprit = mt[0]
                break
        if culprit:
            if attempt == MAX_REVERTS:
                return fail(new, base, f"tests still fail after {MAX_REVERTS} reverts: {', '.join(persistent)}")
            revert_merge(*culprit, f"fails {', '.join(persistent)} on {new}", merged)
            sh("cmake --build build -j10", M / f"{train}-build-revert.log")
            continue
        crashes, reverted = [], False
        for t in persistent:
            kind, c, why = examine(base, train, t, budget)
            log(f"{t}: {kind} - {why.splitlines()[0][:200]}")
            if kind == "culprit":
                if attempt < MAX_REVERTS and revert_confirmed(
                        c, f"{'crashes' if 'crashes' in why else 'fails'} {t} on {new} ({why.splitlines()[0][:300]})", t, train, merged):
                    reverted = True
                    break                   # the bar reruns with the revert in
                kind = "crash" if why.startswith("crashes") else "unpinned"     # an unpinned crash still blocks
                head, _, rest = why.partition("\n")
                why = head + "; reverting the pinned merge did not make it pass" + ("\n" + rest if rest else "")
            ev = excerpt((M / f"{train}-solo-{t}.log").read_text(errors="replace"))
            if kind == "crash":
                test_job(t, "crash", why.splitlines()[0], why.split("\n", 1)[1] if "\n" in why else ev, train)
                crashes.append(t)
            else:
                add_to_baseline(t, train, kind, test_job(t, kind, why, ev, train))
                baseline.add(t)
        if crashes:
            return fail(new, base, f"the app crashes in {', '.join(crashes)} and no merge could be pinned; "
                                   f"test-fix jobs queued (stacks in {M}/{train}-crash-*)")
        if not reverted:
            break
    # gates vs the previous train
    sh("bash tests/run-all-gates.sh", M / f"{train}-gates.log")
    gtext = (M / f"{train}-gates.log").read_text(errors="replace")
    now = dict(re.findall(r"^\s*(\d+)\s+\S+\s+(PASS|FAIL|SKIP)\s*$", gtext, re.M))
    prev = json.loads((M / "gates-last.json").read_text()) if (M / "gates-last.json").exists() else {}
    broke = [g for g, st in now.items() if st == "FAIL" and prev.get(g) == "PASS" and g not in ("1",)]
    if broke:
        return fail(new, base, f"gates that passed on {base} now fail: {', '.join(broke)} (see {M / (train + '-gates.log')})")
    (M / "gates-last.json").write_text(json.dumps(now))
    promote(new, base, merged, ids, groups)
    return 0


def fail(new, base, why):
    log(f"NOT PROMOTED {new}: {why}")
    notify(f"Merge train {new} not promoted: {why}. The branch is kept; the fix base stays {base}.")
    git("switch", base)
    # owner 2026-10-21: a train that keeps failing the same way gets a doctor (train_doctor.py, a ladder of models)
    import train_doctor
    train_doctor.note_failure(new, base, why)
    return 1


def promote(new, base, merged, ids, groups):
    import train_doctor
    train_doctor.note_promoted()        # a promoted train ends any doctor streak
    head = git("rev-parse", "--short", "HEAD")
    (ROOT / "FIX-BASE").write_text(new + "\n")
    import integration_jobs
    errors = []
    activated = integration_jobs.activate(new, errors)
    if activated:
        log(f"pinned {len(activated)} integration requests to validated {new}")
    if errors:
        log(f"{len(errors)} integration request(s) could not be pinned and stay pending: {'; '.join(errors)}")
        notify(f"Merge train {new}: {len(errors)} integration request(s) could not be pinned (see merge log).")
    d = PROG / f"research/reviews/{new.split('/')[-1]}-{datetime.date.today()}"
    d.mkdir(parents=True, exist_ok=True)
    feats = [c["cluster"] for c in merged if c["cluster"].startswith("Q050-")]
    (d / "STATUS.md").write_text(
        f"# Status {datetime.date.today()}: {new} promoted (automatic merge train)\n\n"
        f"**`{new}` @ `{head}`** on `{base}`. Fix base since {datetime.datetime.now():%H:%M}. Not pushed.\n\n"
        f"- Merged {len(merged)} accepted commits ({len(feats)} features: {', '.join(feats) or '-'}).\n"
        f"- Snapshot {ids} ids / {groups} groups (wasm.* out), canary, A16 histogram and all-sources regenerated.\n"
        f"- Bar and gates: logs in `{M}` ({new.split('/')[-1]}-*.log); see `{LOG}` for reverts and port jobs.\n")
    patrol_note = "bug sweep remains disabled."
    if not (ROOT / "PATROL-DISABLED").exists():
        restarted = subprocess.run(["systemctl", "--user", "restart", "zene-patrol"], capture_output=True)
        patrol_note = "patrol restarted on it." if restarted.returncode == 0 else "patrol restart failed."
    log(f"promoted {new} @ {head}: {len(merged)} merges, {len(feats)} features")
    notify(f"Merge train {new} promoted @ {head}: {len(merged)} accepted commits ({len(feats)} features). "
           f"Fix base updated; {patrol_note}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    M.mkdir(exist_ok=True)
    with open(M / "train.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("a train is already running")
            sys.exit(0)
        base = (ROOT / "FIX-BASE").read_text().strip()
        for rebuilds in range(2):
            try:
                sys.exit(run(a.force or rebuilds > 0, a.dry_run))
            except RebuildWithout as e:
                log(f"could not revert {e} - the train is rebuilt from {base} without it")
                git("switch", base)
            except Exception as e:
                log(f"train aborted: {e!r}")
                notify(f"Merge train aborted: {e!r}"[:300])
                break
        # never leave zene-cachyos off the base: the next train refuses to start until it is a clean checkout of it
        for op in ("merge", "revert", "cherry-pick"):
            subprocess.run(gitm(op, "--abort"), capture_output=True)
        git("switch", base)
        sys.exit(1)
