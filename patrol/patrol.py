#!/usr/bin/env python3
"""luna-patrol: GPT 6.0 Luna reading the Zene Studio code base for bugs, pass after pass, until stopped.

Each pass snapshots the product branch's tip (git, so a half-edited working tree is never read), packs the
fork's own files (whole) and the inherited files it changed (as diffs against the upstream base) into
~230 KB slices, and has `codex exec` (gpt-6-luna, max effort, read-only, the code on stdin - codex's own
sandbox cannot run shell here) report bugs for each slice, several slices at a time. Slice boundaries
move and the review angle rotates every pass, so a later pass is not a rerun of an earlier one. Nothing
is fixed: findings land in <out>/pass-*/ and are de-duplicated into <out>/findings.jsonl; <out>/STATUS.md
says where it is. Stop it with `touch <out>/STOP` (it finishes the slices in flight).

Usage: patrol.py [--repo DIR] [--branch NAME] [--out DIR] [--workers N] [--slice-kb N] [--passes N]
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM_BASE = "4e677cb6c6ab"
CODE = re.compile(r"\.(cpp|h|c|hpp)$")
ANGLES = [
    ("general", "Any kind of bug."),
    ("threads", "Concurrency and the realtime rule: data races between the audio thread and the interface, "
                "locks, allocation, logging or Qt calls on audio paths, unsafe statics, signal/slot thread affinity."),
    ("undo", "Undo, redo and the command layer: steps that are not one undo step, inverses that cannot be "
             "replayed, journal checkpoints that miss state, transaction records that lie, the reversibility class."),
    ("lifetime", "Object lifetimes and memory: dangling pointers, use after free, deleteLater timing, widgets "
                 "parented to menus or dialogs that die first, raw pointers kept across an undo, leaks."),
    ("edges", "Edge cases and input validation: empty, zero, negative, huge and NaN values, off-by-one, "
              "integer overflow, sentinels, refusal paths that change state, malformed project files."),
    ("ui", "User-visible state: stale widgets, wrong labels or counts, actions that report success but do "
           "nothing, errors the user never sees, settings that do not persist or do not apply."),
]
LOCK = threading.Lock()


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace").stdout


def listed(repo, rev, path, column0=False):
    names = []
    for line in git(repo, "show", f"{rev}:{path}").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        names.append(line.split("\t")[0].strip() if column0 else line.strip())
    return [n for n in names if CODE.search(n)]


# Untouched upstream code: everything else Zene ships, minus bundled third-party libraries and data tables.
SKIP_PREFIXES = ("tests/", "data/", "cmake/", "tools/", "doc/", "docs/", "buildtools/")
VENDORED = re.compile(r"(^|/)(3rdparty|thirdparty|third_party|vendor)/|^plugins/LadspaEffect/(calf|caps|cmt|swh|tap)/"
                      r"|/(resid|adplug|exprtk|portsmf|libsamplerate|hiir|weakjack|ringbuffer|qt5-x11embed"
                      r"|mingw-std-threads|rtneural|rnnoise|Eigen)/")
DATA_TABLE_BYTES = 300_000   # a single source file this big is a weights/lookup table, not code to review


def upstream_files(repo, rev, known, with_vendored):
    """(path, kind) for code files the fork neither wrote nor changed."""
    out = []
    for line in git(repo, "ls-tree", "-r", "-l", rev).splitlines():
        meta, path = line.split("\t", 1)
        size = meta.split()[3]
        if size == "-" or path in known or not CODE.search(path) or path.startswith(SKIP_PREFIXES):
            continue          # '-' is a submodule entry
        if int(size) > DATA_TABLE_BYTES:
            continue
        vendored = bool(VENDORED.search(path))
        if vendored and not with_vendored:
            continue
        out.append((path, "vendored" if vendored else "upstream"))
    return out


def chunk_of(repo, rev, path, kind):
    inherited = kind == "inherited"
    if inherited:
        diff = git(repo, "diff", "-U30", UPSTREAM_BASE, rev, "--", path)
        return f"=== DIFF (inherited file, changed by the fork) {path} ===\n{diff}\n" if diff.strip() else ""
    text = git(repo, "show", f"{rev}:{path}")
    if not text:
        return ""
    numbered = "\n".join(f"{i:5d}  {line}" for i, line in enumerate(text.split("\n"), 1))
    label = {"fork": "FILE", "upstream": "UPSTREAM FILE (unchanged LMMS code)",
             "vendored": "VENDORED FILE (bundled third-party library)"}[kind]
    return f"=== {label} {path} ===\n{numbered}\n"


def slices_for(repo, rev, pass_no, slice_bytes, with_upstream=True, with_vendored=False):
    fork = listed(repo, rev, "tests/fork-sources.txt")
    inherited = [p for p in listed(repo, rev, "tests/upstream-modifications.txt", column0=True) if p not in set(fork)]
    entries = {(p, "fork") for p in fork} | {(p, "inherited") for p in inherited}
    if with_upstream:
        entries |= set(upstream_files(repo, rev, set(fork) | set(inherited), with_vendored))
    files = sorted(entries,
                   key=lambda f: (os.path.splitext(os.path.basename(f[0]))[0].lower(), f[0].endswith(".cpp")))
    shift = (pass_no * 37) % max(1, len(files))  # move the boundaries every pass
    files = files[shift:] + files[:shift]
    out, current, size = [], [], 0
    for path, kind in files:
        text = chunk_of(repo, rev, path, kind)
        if not text:
            continue
        if size + len(text) > slice_bytes and current:
            out.append("".join(current))
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        out.append("".join(current))
    return out


def prompt_for(angle):
    base = open(os.path.join(HERE, "prompt.txt")).read()
    return base.replace("{ANGLE}", angle[1])


# Light driver (owner, 2026-10-08): Claude Haiku 5.5 through tools/haiku/haiku-exec, unless
# ~/.local/share/zene-bunny-fix/qwen/LIGHT-ENGINE says "luna" (GPT-6 Luna through codex). Read live.
LIGHT_DIR = Path.home() / ".local/share/zene-bunny-fix/qwen"
HAIKU_EXEC = Path(__file__).resolve().parents[1] / "haiku" / "haiku-exec"


def light_engine():
    # Bug FINDING is always GPT 6.0 Luna (owner, 2026-10-09: "make sure the bug finding is still done with
    # luna no matter what"). The shared LIGHT-ENGINE switch still picks the light driver for fixes,
    # research and reviews elsewhere; it no longer applies to the patrol.
    return "luna"


def light_name():
    return "Luna" if light_engine() == "luna" else "Haiku 5.5"


def light_cmd(codex_cmd):
    """codex_cmd is the Luna command line; for Haiku the same arguments go to haiku-exec."""
    return codex_cmd if light_engine() == "luna" else [str(HAIKU_EXEC)] + codex_cmd[2:]


# Owner 2026-10-24: "swap [beam] to some more work that doesn't really need to be fact checked, like finding bugs"
# (supersedes the 2026-10-09 Luna-only rule while it is set). qwen/PATROL-ENGINE = "beam" sends every slice to
# OpenCode Go's free Beam instead of Luna; absent or anything else = Luna. Read live, per slice.
PATROL_ENGINE = LIGHT_DIR / "PATROL-ENGINE"
BEAM_MODEL = "opencode-go/beam-free"
BEAM_DIR = Path.home() / ".cache/zene-tmp/patrol-beam"   # empty: Beam reads the code on stdin, never a checkout


def patrol_engine():
    try:
        return "beam" if PATROL_ENGINE.read_text().strip() == "beam" else "luna"
    except OSError:
        return "luna"


def run_beam(prompt, text, out_md, log_path):
    BEAM_DIR.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        with open(log_path, "w") as log:
            done = subprocess.run(["opencode", "run", "--pure", "--model", BEAM_MODEL, "--variant", "max",
                                   "--format", "json", "--title", "Zene patrol slice", "--dir", str(BEAM_DIR)],
                                  input=prompt + "\n\n" + text, text=True, capture_output=True)
            log.write(done.stdout + done.stderr)
        parts = []
        for line in done.stdout.splitlines():
            try:
                part = json.loads(line).get("part") or {}
            except ValueError:
                continue
            if part.get("type") == "text" and part.get("text"):
                parts.append(part["text"])
        if done.returncode == 0 and parts:
            with open(out_md, "w") as f:
                f.write("\n".join(parts) + "\n")
            return True
        time.sleep(300 * (attempt + 1))
    return False


def run_codex(prompt, text, out_md, log_path):
    if patrol_engine() == "beam":
        return run_beam(prompt, text, out_md, log_path)
    for attempt in range(4):
        with open(log_path, "w") as log:
            done = subprocess.run(light_cmd(["codex", "exec", "-m", "gpt-6-luna", "-c", 'model_reasoning_effort="max"',
                                             "-s", "read-only", "--skip-git-repo-check", "--ephemeral", "-o", out_md,
                                             prompt]),
                                  input=text, text=True, stdout=log, stderr=subprocess.STDOUT)
        if done.returncode == 0 and os.path.exists(out_md) and os.path.getsize(out_md) > 0:
            return True
        time.sleep(300 * (attempt + 1))  # a rate limit or an outage: back off, then try again
    return False


def findings_of(md_path):
    text = open(md_path, errors="replace").read()
    for block in re.split(r"\n(?=BUG \d+:)", text):
        if not block.startswith("BUG"):
            continue
        title = block.split("\n")[0].split(":", 1)[-1].strip()
        where = re.search(r"where:\s*`?([\w./-]+\.(?:cpp|h|c|hpp))(?::(\d+))?", block)
        conf = re.search(r"confidence:\s*(\w+)", block)
        diff = re.search(r"difficulty:\s*(easy|hard|unsure)", block, re.I)
        yield {"title": title, "file": where.group(1) if where else "", "line": int(where.group(2)) if where and where.group(2) else 0,
               "confidence": conf.group(1).lower() if conf else "", "difficulty": diff.group(1).lower() if diff else "",
               "text": block.strip()}


def key_of(finding):
    words = sorted(set(re.findall(r"[a-z]{4,}", finding["title"].lower())))[:8]
    return os.path.basename(finding["file"]) + "|" + " ".join(words)


def record(out, meta, md_path, seen):
    new = 0
    with LOCK, open(os.path.join(out, "findings.jsonl"), "a") as index:
        for finding in findings_of(md_path):
            key = key_of(finding)
            finding.update(meta, duplicate=key in seen, key=key)
            new += 0 if key in seen else 1
            seen.add(key)
            index.write(json.dumps(finding) + "\n")
    return new


def status(out, line):
    with LOCK:
        with open(os.path.join(out, "STATUS.md"), "a") as f:
            f.write(f"- {datetime.datetime.now():%Y-%m-%d %H:%M} {line}\n")


def load_seen(out):
    seen = set()
    path = os.path.join(out, "findings.jsonl")
    if os.path.exists(path):
        for line in open(path):
            try:
                seen.add(json.loads(line)["key"])
            except (ValueError, KeyError):
                pass
    return seen


def one_pass(args, pass_no, seen):
    rev = git(args.repo, "rev-parse", args.branch).strip()
    angle = ANGLES[pass_no % len(ANGLES)]
    slices = slices_for(args.repo, rev, pass_no, args.slice_kb * 1024, not args.no_upstream, args.with_vendored)
    folder = os.path.join(args.out, f"pass-{pass_no:04d}-{rev[:9]}-{angle[0]}")
    os.makedirs(folder, exist_ok=True)
    status(args.out, f"pass {pass_no} started: {args.branch} at {rev[:9]}, angle '{angle[0]}', {len(slices)} slices, engine {patrol_engine()}")
    prompt = prompt_for(angle)

    def work(index_text):
        index, text = index_text
        if os.path.exists(os.path.join(args.out, "STOP")):
            return 0
        md = os.path.join(folder, f"slice-{index:03d}.md")
        if not run_codex(prompt, text, md, md[:-3] + ".log"):
            status(args.out, f"pass {pass_no} slice {index}: {patrol_engine()} failed 4 times, skipped")
            return 0
        new = record(args.out, {"pass": pass_no, "rev": rev[:9], "angle": angle[0], "slice": index,
                                "engine": patrol_engine()}, md, seen)
        status(args.out, f"pass {pass_no} slice {index}/{len(slices)}: {new} new findings")
        return new

    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        total = sum(pool.map(work, enumerate(slices, 1)))
    status(args.out, f"pass {pass_no} finished: {total} new findings")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.path.join(HERE, "..", "..", "zene-survey"))
    parser.add_argument("--branch", default="HEAD")
    parser.add_argument("--out", default=os.path.join(HERE, "..", "..", "research", "reviews", "luna-patrol"))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--slice-kb", type=int, default=230)
    parser.add_argument("--passes", type=int, default=0, help="0 = until STOP")
    parser.add_argument("--no-upstream", action="store_true", help="skip unchanged upstream LMMS files (fork + diffs only)")
    parser.add_argument("--with-vendored", action="store_true", help="also read bundled third-party libraries")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "patrol.pid"), "w") as f:
        f.write(str(os.getpid()))
    seen = load_seen(args.out)
    done = [d for d in os.listdir(args.out) if d.startswith("pass-")]
    pass_no = len(done) + 1
    while not os.path.exists(os.path.join(args.out, "STOP")) and (args.passes == 0 or pass_no <= len(done) + args.passes):
        one_pass(args, pass_no, seen)
        pass_no += 1
    status(args.out, "stopped")


if __name__ == "__main__":
    sys.exit(main())
