#!/usr/bin/env python3
"""Space Bunny fixer-lane driver.

One mcode session (model space-bunny-free) per batch of Luna findings, run
sequentially inside one lane worktree. Resumable: a batch whose state file
exists is skipped. Stop: touch <root>/STOP (finishes the running batch).

usage: driver.py <lane-name>
"""
import json, os, shutil, subprocess, sys, time, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PROG = "/home/kruzzzzy/Documents/AI_KOS_PROJECT/projects/zene-studio-program"
GIT = "/usr/bin/git"
MODEL = os.environ.get("BUNNY_MODEL", "custom_provider:opencode-zen/space-bunny-free")
SESSION_TIMEOUT = "100m"
MIN_FREE_GB = 25

LANE = sys.argv[1]
CFG = json.load(open(os.path.join(ROOT, "lanes", LANE, "lane.json")))
WT = CFG["worktree"]
STATE = os.path.join(ROOT, "lanes", LANE, "state")
os.makedirs(STATE, exist_ok=True)


def log(msg):
    line = f"[{datetime.datetime.now():%H:%M:%S}] {LANE}: {msg}"
    print(line, flush=True)
    with open(os.path.join(ROOT, "lanes", LANE, "driver.log"), "a") as f:
        f.write(line + "\n")


def git(*args):
    return subprocess.run([GIT, "-C", WT, *args], capture_output=True, text=True).stdout


def free_gb():
    st = os.statvfs(PROG)
    return st.f_bavail * st.f_frsize / 1e9


def env():
    e = dict(os.environ)
    e.pop("LMMS_PLUGIN_DIR", None)
    home = os.path.expanduser("~")
    e.update({
        "PATH": os.path.join(ROOT, "shims") + ":" + e["PATH"],
        "BUNNY_LOCKDIR": os.path.join(ROOT, "locks"),
        "BUNNY_JOBS": "5",
        "CCACHE_DIR": f"{home}/.cache/ccache-bunny",
        "CCACHE_MAXSIZE": "25G",
        "CCACHE_BASEDIR": PROG,
        "CCACHE_NOHASHDIR": "true",
        "QT_QPA_PLATFORM": "offscreen",
        "PKG_CONFIG_PATH": f"{home}/.local/zene-deps/root/usr/lib/pkgconfig:{home}/.local/zene-deps/root/usr/lib/x86_64-linux-gnu/pkgconfig",
        "LD_LIBRARY_PATH": f"{home}/.local/zene-deps/root/usr/lib" + (":" + e["LD_LIBRARY_PATH"] if e.get("LD_LIBRARY_PATH") else ""),
    })
    return e


def save_leftovers(tag, untracked_before):
    """Uncommitted session output is saved as a patch, then the tree is restored to HEAD."""
    status = git("status", "--porcelain")
    if not status.strip():
        return None
    out = os.path.join(STATE, f"{tag}-leftover")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "tracked.patch"), "w") as f:
        f.write(git("diff", "HEAD"))
    new_untracked = [l[3:] for l in status.splitlines() if l.startswith("?? ") and l[3:] not in untracked_before]
    for rel in new_untracked:
        src = os.path.join(WT, rel)
        dst = os.path.join(out, "untracked", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
    subprocess.run([GIT, "-C", WT, "checkout", "--", "."], capture_output=True)
    for rel in new_untracked:
        p = os.path.join(WT, rel)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    return {"saved_to": out, "status": status}


def run_batch(batch):
    tag = batch["tag"]
    state_file = os.path.join(STATE, f"{tag}.json")
    if os.path.exists(state_file):
        return "skip"
    if os.path.exists(os.path.join(ROOT, "STOP")):
        return "stop"
    if free_gb() < MIN_FREE_GB:
        log(f"free space {free_gb():.1f} GB < {MIN_FREE_GB} GB: stopping")
        return "stop"
    prompt = open(os.path.join(ROOT, "lanes", LANE, "batches", f"{tag}.md")).read()
    head0 = git("rev-parse", "HEAD").strip()
    untracked_before = [l[3:] for l in git("status", "--porcelain").splitlines() if l.startswith("?? ")]
    out_md = os.path.join(STATE, f"{tag}-final.md")
    run_log = os.path.join(STATE, f"{tag}-session.log")
    log(f"batch {tag} start at {head0[:9]}")
    t0 = time.time()
    attempts = 0
    while True:
        attempts += 1
        with open(run_log, "a") as lf:
            rc = subprocess.run(
                ["mcode", "exec", "--config", os.path.join(ROOT, "..", "sb-config.yaml"),
                 "--model", MODEL, "--permission", "full", "--prompt-mode", "coding",
                 "--cwd", WT, "--timeout", SESSION_TIMEOUT, "--max-steps", "600",
                 "-o", out_md, "--input", "-"],
                input=prompt, text=True, stdout=lf, stderr=subprocess.STDOUT, env=env(),
            ).returncode
        head1 = git("rev-parse", "HEAD").strip()
        quick_fail = rc != 0 and head1 == head0 and time.time() - t0 < 300
        if quick_fail and attempts < 4:
            log(f"batch {tag}: quick failure rc={rc}, retry {attempts} after backoff")
            time.sleep(120 * attempts)
            continue
        break
    commits = git("log", "--format=%h %s", f"{head0}..{head1}").strip().splitlines()
    leftovers = save_leftovers(tag, untracked_before)
    rec = {"tag": tag, "rc": rc, "attempts": attempts, "minutes": round((time.time() - t0) / 60, 1),
           "head_before": head0, "head_after": head1, "commits": commits, "leftovers": leftovers,
           "clusters": batch.get("clusters", [])}
    if rc != 0 and not commits and not leftovers:
        # Nothing happened: keep the batch pending so a relaunch tries it again.
        state_file = os.path.join(STATE, f"{tag}.failed.json")
    json.dump(rec, open(state_file, "w"), indent=1)
    log(f"batch {tag} done rc={rc} {rec['minutes']} min, {len(commits)} commits"
        + (", LEFTOVERS saved" if leftovers else ""))
    return "ok"


def main():
    batches = json.load(open(os.path.join(ROOT, "lanes", LANE, "batches.json")))
    log(f"driver start, {len(batches)} batches, worktree {WT}")
    for b in batches:
        r = run_batch(b)
        if r == "stop":
            log("STOP")
            break
    log("driver end")


if __name__ == "__main__":
    main()
