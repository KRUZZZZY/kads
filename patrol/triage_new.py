#!/usr/bin/env python3
"""Triage each completed Luna patrol pass with local Clef and publish its NEW findings as a fix queue.

For every completed, untriaged pass (see pass_states for how completion is read from STATUS.md):
  1. Take the pass's findings that are not in the frozen 10-05 snapshot.
  2. Dedup them against everything known so far: the frozen 10-05 dedup clusters, the 10-06 inventory
     (incl. review-* items), clusters this tool made from earlier passes, bugs in the Qwen fixer's
     hands (its state/*.json + runs/<cluster>.r<n>/brief.md), and the patrol's own `duplicate` flag.
     The method is dedup.py's: nomic-embed-text cosine, Clef on the LO..HI band, average linkage at
     distance T, never joining two findings of one pass. Known clusters are held fixed; a new finding
     only joins one or starts its own.
  3. Ask Clef classify.py's realness questions (classify.QUESTIONS / classify.disposition) for each new
     distinct cluster, pinned to the revision the pass read. (Dedup runs first so no Clef call is spent
     on a repeat; the queue is the same as realness-then-dedup, because repeats are dropped either way.)
  4. Ask the v2 difficulty questions (difficulty_v2.QUESTIONS, difficulty.label) for new clusters whose
     realness is likely_open_bug or needs_context.
  5. Publish queue/{easy,needs_context,hard}.jsonl atomically, COUNTS.json, the append-only
     decisions.jsonl journal (dropped findings included, with the reason and the cluster a repeat joined)
     and TRIAGED-PASSES.

Every Clef/embedding call happens inside one GPU lease per run (GPU-REQUEST / GPU-FREE handshake with
the Qwen fixer). Model errors stop the run; they never count as verdicts. Suggestions only: reproduce a
defect before fixing it.

Usage: triage_new.py --once | --watch | --dry-run   [--interval SEC] [--repo DIR]
"""
import argparse
import atexit
import collections
import datetime
import fcntl
import hashlib
import heapq
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import classify  # noqa: E402  realness questions, disposition thresholds, contracts, Clef client
import dedup  # noqa: E402  embedding text, cache, same-defect question, LO/HI/T
import difficulty  # noqa: E402  label() thresholds, atomic write
import difficulty_v2  # noqa: E402  v2 conditional difficulty questions

PROG = HERE.parents[1]
QWEN = pathlib.Path.home() / ".local/share/zene-bunny-fix/qwen"

# difficulty_v2.main()'s state['scope'] string, verbatim (it is a literal inside main(), not importable).
# test_triage_new checks that difficulty_v2.py still contains it, so a drift is caught.
DIFFICULTY_SCOPE = ("Difficulty CONDITIONAL on the hypothesis being real. Preserving behavior means intended "
                    "contracts/unrelated behavior, NOT preserving erroneous behavior. Defect correction and "
                    "deterministic quick refutation are allowed. This call does not establish realness or "
                    "permission to weaken tests.")
# classify.main()'s prior-review matching (TF-IDF top 2 at score >= 0.20, chunks capped at 4500 chars).
REVIEW_TOP, REVIEW_MIN, REVIEW_CHUNK = 2, 0.20, 4500
OPEN_REALNESS = ("likely_open_bug", "needs_context")
QUEUES = ("easy", "needs_context", "hard")
SAME_QUESTION = {"same": {"type": "noul", "instructions": dedup.QUESTION}}

STARTED = re.compile(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d) pass (\d+) started: .*? at ([0-9a-f]+), angle '([^']*)'")
FINISHED = re.compile(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d) pass (\d+) finished: (\d+) new findings")
PASS_DIR = re.compile(r"^pass-(\d{4})-([0-9a-f]+)-(\w+)$")
# Luna's own fix-difficulty line (prompt.txt; patrol.py findings_of() stores it as `difficulty` from pass 9).
LUNA_DIFFICULTY = re.compile(r"difficulty:\s*(easy|hard|unsure)", re.I)
LUNA_DIFFICULTY_LINE = re.compile(r"^[ \t]*difficulty:[^\n]*\n?", re.I | re.M)


def log(msg):
    print(f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def sha256(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def rows(path):
    path = pathlib.Path(path)
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def append_rows(path, items):
    """Append-only journal write, flushed and synced before returning."""
    with open(path, "a") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def atomic_write(path, text):
    """Write a sibling temp file, fsync it, then rename over the target: readers see old or new, never half."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def proc_alive(pid_text, needle):
    """True if pid_text names a live, non-zombie process whose command line contains needle (no signals sent)."""
    pid = (pid_text or "").split()[0] if (pid_text or "").split() else ""
    if not pid.isdigit():
        return False
    proc = pathlib.Path(f"/proc/{pid}")
    try:
        state = (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, IndexError):
        return False
    return state != "Z" and needle in cmdline


def luna_difficulty(finding):
    """Luna's rating: the `difficulty` field, else the line inside `text` (pass 8), else ''."""
    value = str(finding.get("difficulty") or "").strip().lower()
    if value in ("easy", "hard", "unsure"):
        return value
    m = LUNA_DIFFICULTY.search(finding.get("text") or "")
    return m[1].lower() if m else ""


def difficulty_agree(luna, clef):
    """True/False only when both gave a decided easy/hard; None if either is missing, 'unsure' or needs_context."""
    if luna not in ("easy", "hard") or clef not in ("easy", "hard"):
        return None
    return luna == clef


def clef_text(text):
    """The finding as Clef sees it: Luna's difficulty line removed, so Clef's own rating stays independent."""
    return LUNA_DIFFICULTY_LINE.sub("", text)


# --------------------------------------------------------------------------- configuration
class Config:
    def __init__(self, program=PROG, qwen=QWEN, repo=None, out=None, host="http://127.0.0.1:11434",
                 embed_host=None, embed_model="nomic-embed-text", poll=15.0, reviews=None):
        program = pathlib.Path(program)
        self.patrol = program / "research/reviews/luna-patrol"
        self.frozen_clef = program / "research/reviews/luna-clef-2026-10-05"
        self.frozen_dedup = program / "research/reviews/luna-dedup-2026-10-05/run-v2"
        self.frozen_inv = program / "research/reviews/luna-easy-fix-2026-10-06"
        self.out = pathlib.Path(out) if out else program / "research/reviews/luna-new-triage"
        self.repo = pathlib.Path(repo).resolve() if repo else program / "zene-ui"
        self.reviews = pathlib.Path(reviews) if reviews else program / "research/reviews/catchup-2026-10-04/fix-reports"
        self.qwen = pathlib.Path(qwen)
        self.host, self.embed_host, self.embed_model = host, embed_host or host, embed_model
        self.poll = poll


# --------------------------------------------------------------------------- pass completion
def patrol_alive(cfg):
    pid_file = cfg.patrol / "patrol.pid"
    return pid_file.exists() and proc_alive(pid_file.read_text(), "patrol.py")


def pass_states(cfg, alive=None):
    """Read STATUS.md. patrol.py writes 'pass N started' (patrol.py:160) and, after every slice of the pass
    has been recorded to findings.jsonl, 'pass N finished' (patrol.py:177). Passes run one at a time and a
    restart numbers a fresh pass (patrol.py:193-197), so a pass without 'finished' is also over once a later
    pass has started ('superseded') or once the patrol process is gone ('abandoned'): no writer is left.
    """
    alive = patrol_alive(cfg) if alive is None else alive
    states = {}
    status = cfg.patrol / "STATUS.md"
    lines = status.read_text(errors="replace").splitlines() if status.exists() else []
    for line in lines:
        m = STARTED.match(line)
        if m:
            states[int(m[2])] = {"pass": int(m[2]), "started": m[1], "status_rev": m[3], "angle": m[4],
                                 "finished": None}
        m = FINISHED.match(line)
        if m and int(m[2]) in states:
            states[int(m[2])]["finished"] = m[1]
    numbers = sorted(states)
    for n in numbers:
        s = states[n]
        if s["finished"]:
            s["state"] = "finished"
        elif any(k > n for k in numbers):
            s["state"] = "superseded"
        elif not alive:
            s["state"] = "abandoned"
        else:
            s["state"] = "running"
        s["complete"] = s["state"] != "running"
        dirs = sorted(cfg.patrol.glob(f"pass-{n:04d}-*"))
        m = PASS_DIR.match(dirs[0].name) if dirs else None
        s["dir"] = str(dirs[0]) if dirs else ""
        s["rev"] = m[2] if m else s["status_rev"]
    return states


# --------------------------------------------------------------------------- GPU lease
STRATA_PORT = os.environ.get("STRATA_URL", "http://127.0.0.1:8081").rsplit(":", 1)[-1].split("/")[0]
STRATA_DIR = pathlib.Path.home() / "Documents/AI_KOS_PROJECT/projects/strata"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


def strata_pids():
    """Processes listening on Strata's port (8081 here; 8080 is SearXNG)."""
    out = subprocess.run(["ss", "-ltnpH", f"sport = :{STRATA_PORT}"], capture_output=True, text=True).stdout
    return {int(p) for p in re.findall(r"pid=(\d+)", out)}


def stop_strata():
    groups = set()
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
            return bool(groups)
        time.sleep(2)
    return bool(groups)


def start_strata():
    runs = sorted(STRATA_DIR.glob("run-coder*.sh")) or sorted(STRATA_DIR.glob("run-*.sh"))
    if not runs or strata_pids():
        return
    out = open(STRATA_DIR / "strata-server.out", "a")
    subprocess.Popen([str(runs[0])], cwd=STRATA_DIR, stdout=out, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    log(f"GPU: restarted Strata ({runs[0].name}) that this run had stopped")


def gpu_free_mib():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else 0


def ollama_unload_all():
    """Ollama keeps a model in VRAM ~5 min after use; evict Clef and the embedder so Strata can load."""
    def call(path, body=None):
        req = urllib.request.Request(OLLAMA_URL + path, json.dumps(body).encode() if body else None,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    try:
        names = [m["name"] for m in call("/api/ps").get("models", [])]
        for name in names:
            subprocess.run(["ollama", "stop", name], capture_output=True, timeout=60)  # works for every model; Clef refuses /api/generate
        if names:
            log(f"GPU: unloaded from Ollama: {', '.join(names)}")
    except Exception as e:
        log(f"GPU: could not unload Ollama models ({e!r})")


class GpuLease:
    """GPU-REQUEST / GPU-FREE handshake with the Qwen fixer (qwenfix.py yield_gpu_if_requested).

    acquire: wait out another live requester, write GPU-REQUEST ('<pid> <time>'), then wait for a GPU-FREE
    written after our request, or for fix.pid not to name a live qwenfix process. release (also on
    exceptions, SIGTERM via the installed handler, and atexit): delete GPU-REQUEST if it is still ours.
    """

    def __init__(self, qwen_dir, poll=15.0, fixer_needle="qwenfix", log_every=300.0):
        self.dir = pathlib.Path(qwen_dir)
        self.request, self.free, self.fix_pid = self.dir / "GPU-REQUEST", self.dir / "GPU-FREE", self.dir / "fix.pid"
        self.poll, self.needle, self.log_every = poll, fixer_needle, log_every
        self.held = False
        self.stopped_strata = False
        atexit.register(self.release)

    def _wait_vram(self, need_mib=11000, limit_s=300):
        t0 = time.time()
        while gpu_free_mib() < need_mib and time.time() - t0 < limit_s:
            time.sleep(5)

    def fixer_alive(self):
        return self.fix_pid.exists() and proc_alive(self.fix_pid.read_text(), self.needle)

    def _foreign_request(self):
        if not self.request.exists():
            return False
        holder = (self.request.read_text().split() or [""])[0]
        return holder != str(os.getpid()) and proc_alive(holder, "")

    def acquire(self):
        last = 0.0
        while self._foreign_request():
            if time.time() - last > self.log_every:
                log(f"GPU: another requester holds {self.request}; waiting")
                last = time.time()
            time.sleep(self.poll)
        self.dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        self.request.write_text(f"{os.getpid()} {now()} triage_new.py\n")
        self.held = True
        last = 0.0
        while True:
            if self.free.exists() and self.free.stat().st_mtime >= t0 - 1:
                self._wait_vram()
                log("GPU: the fixer released it (GPU-FREE)")
                return
            if not self.fixer_alive():
                if strata_pids():
                    log("GPU: no live fixer, but Strata is running on its own - stopping it")
                    self.stopped_strata = stop_strata()
                self._wait_vram()
                log("GPU: no live fixer (fix.pid) - proceeding")
                return
            if time.time() - last > self.log_every:
                log("GPU: requested; waiting for the fixer to finish its current bug and stop Strata")
                last = time.time()
            time.sleep(self.poll)

    def release(self):
        if self.held:
            ollama_unload_all()          # hand the VRAM back, not just the token
            if self.stopped_strata and not self.fixer_alive():
                start_strata()
            self.stopped_strata = False
        if self.held and self.request.exists():
            if (self.request.read_text().split() or [""])[0] == str(os.getpid()):
                self.request.unlink(missing_ok=True)
                log("GPU: request withdrawn")
        self.held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def on_signal(signum, frame):
    """SIGTERM/SIGHUP become SystemExit, so every `with GpuLease` and finally block runs."""
    raise SystemExit(128 + signum)


# --------------------------------------------------------------------------- models
class OllamaModels:
    """Real calls. Embeddings via dedup.embed_all (content-hash cache), Clef via classify.post."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cache = cfg.out / "embeddings.npz"
        self.calls = collections.Counter()

    def pins(self):
        with urllib.request.urlopen(self.cfg.host + "/api/tags", timeout=20) as r:
            models = json.load(r)["models"]
        if self.cfg.embed_host != self.cfg.host:
            with urllib.request.urlopen(self.cfg.embed_host + "/api/tags", timeout=20) as r:
                embed_models = json.load(r)["models"]
        else:
            embed_models = models
        clef = next((m for m in models if m["name"].split(":")[0] == "clef-flash"), None)
        emb = next((m for m in embed_models if m["name"].split(":")[0] == self.cfg.embed_model.split(":")[0]), None)
        if clef is None or emb is None:
            raise SystemExit("clef-flash or the embedding model is not installed in Ollama")
        return {"clef": {"name": clef["name"], "digest": clef["digest"]},
                "embed": {"name": emb["name"], "digest": emb["digest"]}}

    def embed(self, items):
        frozen = self.cfg.frozen_dedup / "embeddings.npz"
        if not self.cache.exists() and frozen.exists():
            shutil.copyfile(frozen, self.cache)  # read-only reuse of the 10-05 vectors (same model, same keys)
        self.calls["embed_batches"] += 1
        return dedup.embed_all(items, self.cfg.embed_host, self.cfg.embed_model, self.cache)

    def ask(self, state, questions):
        self.calls["clef"] += 1
        return classify.post(self.cfg.host, state, questions)


def cached_vectors(cfg, model):
    """Embedding vectors already on disk (ours, else the frozen 10-05 cache), keyed like dedup.embed_all."""
    import numpy as np
    store = {}
    for path in (cfg.frozen_dedup / "embeddings.npz", cfg.out / "embeddings.npz"):
        if path.exists():
            z = np.load(path, allow_pickle=False)
            store.update(zip(z["keys"].tolist(), z["vecs"]))
    return store


def embed_key(model, f):
    return dedup.sha(model + "\0" + dedup.embed_text(f))


# --------------------------------------------------------------------------- inputs
def load_findings(cfg):
    """findings.jsonl as written so far, and the frozen snapshot length (verified to be its prefix)."""
    blob = (cfg.patrol / "findings.jsonl").read_bytes()
    snapshot = (cfg.frozen_clef / "findings-snapshot.jsonl").read_bytes()
    if not blob.startswith(snapshot):
        raise SystemExit("findings.jsonl no longer starts with the frozen 10-05 snapshot; refusing")
    complete = blob[:blob.rfind(b"\n") + 1]  # a line the patrol is still writing is ignored
    findings = [json.loads(x) for x in complete.splitlines()]
    return findings, snapshot.count(b"\n"), sha256(snapshot)


def review_references(cfg):
    references, h = [], hashlib.sha256()
    if cfg.reviews and cfg.reviews.exists():
        for path in sorted(cfg.reviews.rglob("*.md")):
            blob = path.read_bytes()
            h.update(str(path).encode() + b"\0" + blob)
            for chunk in re.split(r"\n(?=[^\n]*\bBUG\s+\d+[^\n]*:)", blob.decode(errors="replace")):
                if "verdict:" in chunk and "evidence:" in chunk:
                    references.append({"report": str(path), "text": chunk[:REVIEW_CHUNK]})
    return references, h.hexdigest()


def match_reviews(references, findings):
    if not references or not findings:
        return [[] for _ in findings]
    vectorizer = classify.TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
    review_vectors = vectorizer.fit_transform(r["text"] for r in references)
    scores = (vectorizer.transform(f["title"] + " " + f["text"] for f in findings) @ review_vectors.T).toarray()
    return [[references[j] for j in sorted(range(len(s)), key=lambda j: -s[j])[:REVIEW_TOP] if s[j] >= REVIEW_MIN]
            for s in scores]


def brief_finding(brief_text):
    """The finding JSON qwenfix.fix_brief embeds after '## The finding'."""
    m = re.search(r"## The finding[^\n]*\n```json\n(.*?)\n```", brief_text, re.S)
    if not m:
        return None
    try:
        return json.loads(m[1])
    except ValueError:
        return None


class Known:
    """Every cluster a new finding may repeat. groups: gid -> {members, sources}; corpus: uid -> finding."""

    def __init__(self):
        self.groups, self.corpus, self.member_of, self.warnings = {}, {}, {}, []

    def add(self, gid, members, source):
        g = self.groups.setdefault(gid, {"members": [], "sources": set()})
        for m in members:
            if m not in self.member_of:
                g["members"].append(m)
                self.member_of[m] = gid
        g["sources"].add(source)

    def lookup(self, key):
        for gid in self.groups:
            if str(gid) == str(key):
                return gid
        return None


def known_clusters(cfg, findings, ledger):
    k = Known()
    for i, f in enumerate(findings):
        k.corpus[i] = f
    # (a) frozen 10-05 dedup clusters
    for c in rows(cfg.frozen_dedup / "clusters.jsonl"):
        k.add(c["representative"], c["members"], "dedup-2026-10-05")
    # (b) 10-06 inventory, incl. review-* items (text only, no members: a pseudo-finding of their own)
    for p in sorted((cfg.frozen_inv / "inventory").glob("*.jsonl")):
        for r in rows(p):
            cid = r["cluster"]
            if isinstance(cid, int):
                k.add(cid, r.get("members") or [], "inventory-2026-10-06")
            else:
                uid = f"inv:{cid}"
                k.corpus[uid] = {"title": r.get("title", ""), "file": r.get("file", ""), "line": r.get("line", 0),
                                 "text": "what: " + r.get("text", ""), "pass": uid}
                k.add(cid, [uid], "inventory-2026-10-06")
    # (c) clusters this tool made from earlier passes (with every finding that joined them since)
    for gid, members in ledger["clusters"].items():
        k.add(gid, members, f"new-triage-pass-{ledger['cluster_pass'][gid]}")
    # (d) bugs in the Qwen fixer's hands
    state_dir = cfg.qwen / "state"
    for p in sorted(state_dir.glob("*.json")) if state_dir.exists() else []:
        try:
            rec = json.loads(p.read_text())
        except ValueError:
            k.warnings.append(f"unreadable fixer state {p}")
            continue
        cid = rec.get("cluster", p.stem)
        existing = k.lookup(cid)
        if existing is not None:
            k.groups[existing]["sources"].add("qwen-fixer")
            continue
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", str(cid))
        briefs = sorted((cfg.qwen / "runs").glob(f"{slug}.r*/brief.md"),
                        key=lambda b: int(re.search(r"\.r(\d+)$", b.parent.name)[1]))
        item = brief_finding(briefs[-1].read_text(errors="replace")) if briefs else None
        if not item or not item.get("file"):
            k.warnings.append(f"fixer cluster {cid}: no brief finding with a file; cannot be compared")
            continue
        uid = f"qwen:{cid}"
        k.corpus[uid] = {"title": item.get("title") or rec.get("title", ""), "file": item["file"],
                         "line": item.get("line", 0), "text": item.get("text", ""), "pass": uid}
        k.add(uid, [uid], "qwen-fixer")
    # Findings of earlier triaged passes that joined a known (non-new) cluster count as its members too.
    for gid, members in ledger.get("joined", {}).items():
        if gid in k.groups and gid not in ledger["clusters"]:
            g = k.groups[gid]
            for m in members:
                if m not in k.member_of:
                    g["members"].append(m)
                    k.member_of[m] = gid
    return k


# --------------------------------------------------------------------------- the ledger (our own journals)
def load_ledger(cfg):
    """Rebuild state from decisions.jsonl. Only rows of a run that wrote its pass_complete marker count."""
    decisions = rows(cfg.out / "decisions.jsonl")
    done = {(d["pass"], d["run_id"]) for d in decisions if d.get("event") == "pass_complete"}
    valid = [d for d in decisions if d.get("event") != "pass_complete" and (d.get("pass"), d.get("run_id")) in done]
    clusters, cluster_pass, created = collections.OrderedDict(), {}, {}
    for d in valid:
        if d.get("new_cluster"):
            clusters[d["cluster"]] = [d["finding_id"]]
            cluster_pass[d["cluster"]] = d["pass"]
            created[d["cluster"]] = d
    joined = collections.defaultdict(list)
    for d in valid:
        if d.get("reason") == "repeat" and d.get("joined_cluster") is not None:
            joined[d["joined_cluster"]].append(d["finding_id"])
            if d["joined_cluster"] in clusters:
                clusters[d["joined_cluster"]].append(d["finding_id"])
    triaged = {p for p, _ in done} | {r["pass"] for r in rows(cfg.out / "TRIAGED-PASSES")}
    return {"decisions": valid, "clusters": clusters, "cluster_pass": cluster_pass, "created": created,
            "joined": joined, "triaged": triaged, "done_runs": done}


def new_cluster_ids(pass_no, count, known_ids):
    """'new-p<pass>-<n>': a string (never equal to an integer id) without the 'review-' prefix."""
    ids = [f"new-p{pass_no}-{n}" for n in range(1, count + 1)]
    taken = {str(x) for x in known_ids}
    for i in ids:
        if i in taken or i.startswith("review-") or i.lstrip("-").isdigit():
            raise RuntimeError(f"cluster id collision: {i}")
    return ids


# --------------------------------------------------------------------------- dedup (dedup.py's method)
def band_pairs(new_uids, candidates, corpus, vec, lo, hi):
    """(new, member) pairs needing a Clef same-defect judgement: same file, different pass, LO <= cos < HI."""
    out = []
    for n in new_uids:
        for gid, members in candidates[n]:
            for m in members:
                if corpus[m]["file"] == corpus[n]["file"] and corpus[m]["pass"] != corpus[n]["pass"]:
                    c = float(vec[n] @ vec[m])
                    if lo <= c < hi:
                        out.append((n, m))
    return sorted(set(out), key=str)


def link(new_uids, groups, corpus, vec, judged, lo, hi, t, band_default=0.0):
    """Average linkage at distance t with the known clusters held fixed (dedup.constrained_average's rule):
    repeatedly join the (new finding, cluster) pair with the highest mean similarity >= 1 - t, where
    similarity is 1 at cos >= hi, 0 below lo or across files, the Clef probability in the band, and a
    cluster that already holds a finding of the same pass can never be joined. Returns uid -> (gid, avg).
    band_default scores an unjudged band pair (0.0 in real runs; the dry-run estimate brackets 0..1)."""
    members = {gid: list(g["members"]) for gid, g in groups.items()}
    by_file = collections.defaultdict(set)
    for gid, ms in members.items():
        for m in ms:
            by_file[corpus[m]["file"]].add(gid)

    def sim(n, m):
        if corpus[n]["file"] != corpus[m]["file"]:
            return 0.0
        c = float(vec[n] @ vec[m])
        return 1.0 if c >= hi else 0.0 if c < lo else float(judged.get((n, m), band_default))

    def score(n, gid):
        ms = members[gid]
        if not ms or any(corpus[m]["pass"] == corpus[n]["pass"] for m in ms):
            return None
        return sum(sim(n, m) for m in ms) / len(ms)

    version = collections.Counter()
    heap = []
    for n in new_uids:
        for gid in by_file[corpus[n]["file"]]:
            s = score(n, gid)
            if s is not None and s >= 1 - t:
                heapq.heappush(heap, (-s, str(n), str(gid), n, gid, 0))
    assigned = {}
    while heap:
        neg, _, _, n, gid, ver = heapq.heappop(heap)
        if n in assigned or ver != version[gid]:
            continue
        assigned[n] = (gid, -neg)
        members[gid].append(n)
        version[gid] += 1
        for other in new_uids:  # the joined cluster changed: rescore it for the still-unassigned findings
            if other not in assigned and corpus[other]["file"] in {corpus[m]["file"] for m in members[gid]}:
                s = score(other, gid)
                if s is not None and s >= 1 - t:
                    heapq.heappush(heap, (-s, str(other), str(gid), other, gid, version[gid]))
    return assigned


# --------------------------------------------------------------------------- per-pass triage
class Repo:
    def __init__(self, path):
        self.path, self.trees, self.sources = path, {}, {}

    def full(self, rev):
        return classify.git(self.path, "rev-parse", f"{rev}^{{commit}}").strip()

    def resolve(self, rev, name):
        if rev not in self.trees:
            paths = classify.git(self.path, "ls-tree", "-r", "--name-only", rev).splitlines()
            base = collections.defaultdict(list)
            for p in paths:
                base[pathlib.PurePath(p).name].append(p)
            self.trees[rev] = (set(paths), base)
        paths, base = self.trees[rev]
        cands = base[pathlib.PurePath(name or "").name]
        return name if name in paths else cands[0] if len(cands) == 1 else ""

    def show(self, rev, path):
        if not path:
            return ""
        if (rev, path) not in self.sources:
            try:
                self.sources[(rev, path)] = classify.git(self.path, "show", f"{rev}:{path}")
            except subprocess.CalledProcessError:
                self.sources[(rev, path)] = ""
        return self.sources[(rev, path)]


def inventory_lanes(cfg):
    votes = collections.defaultdict(collections.Counter)
    for p in sorted((cfg.frozen_inv / "inventory").glob("*.jsonl")):
        for r in rows(p):
            if r.get("lane") and r["lane"] != "unassigned":
                votes[r.get("file", "")][r["lane"]] += 1
    return {f: c.most_common(1)[0][0] for f, c in votes.items()}


def lane_for(path, lanes):
    if path in lanes:
        return lanes[path]
    name = pathlib.PurePath(path).name
    if path.startswith("plugins/"):
        return "plugins"
    if "/gui/" in path or path.startswith(("src/gui", "include/gui")):
        return "gui"
    if name.startswith("ControlCommands"):
        return "cmds"
    if path.startswith(("src/core/audio", "include/Audio")) or name.startswith("Audio"):
        return "engine"
    if path.startswith(("src/core", "include/", "src/tracks")):
        return "core"
    return "unassigned"


def plan_pass(cfg, info, findings, snap_n, ledger):
    """CPU-only: which findings are new, which the patrol already flagged, which need the model."""
    new = [i for i in range(snap_n, len(findings)) if findings[i].get("pass") == info["pass"]]
    flagged = [i for i in new if findings[i].get("duplicate")]
    candidates = [i for i in new if not findings[i].get("duplicate")]
    first_by_key = {}
    for i, f in enumerate(findings):
        first_by_key.setdefault(f.get("key"), i)
    return {"new": new, "flagged": flagged, "candidates": candidates, "first_by_key": first_by_key}


def triage_pass(cfg, info, findings, snap_n, ledger, models, pins, repo, references, lanes, run_id):
    """All model work for one pass; returns the decision rows (written by the caller in one append)."""
    p = info["pass"]
    plan = plan_pass(cfg, info, findings, snap_n, ledger)
    revision = repo.full(info["rev"]) if plan["new"] else ""
    mismatched = {findings[i].get("rev") for i in plan["new"]} - {info["rev"], None}
    if mismatched:
        raise SystemExit(f"pass {p}: findings carry revisions {sorted(mismatched)}, the pass dir says {info['rev']}")
    known = known_clusters(cfg, findings, ledger)
    for w in known.warnings:
        log(f"warning: {w}")
    corpus = known.corpus
    lo, hi, t = dedup.DEFAULTS["lo"], dedup.DEFAULTS["hi"], dedup.DEFAULTS["t"]

    # Dedup: embeddings for the candidates and every known member in their files, Clef on the band.
    files = {corpus[i]["file"] for i in plan["candidates"]}
    by_file = collections.defaultdict(set)
    for gid, g in known.groups.items():
        for m in g["members"]:
            if corpus[m]["file"] in files:
                by_file[corpus[m]["file"]].add(gid)
    neighbours = sorted({m for gids in by_file.values() for gid in gids for m in known.groups[gid]["members"]},
                        key=str)
    uids = list(plan["candidates"]) + [m for m in neighbours if m not in set(plan["candidates"])]
    vec = {}
    if plan["candidates"]:
        X = models.embed([corpus[u] for u in uids])
        vec = dict(zip(uids, X))
    candidates = {n: [(gid, known.groups[gid]["members"]) for gid in by_file[corpus[n]["file"]]]
                  for n in plan["candidates"]}
    judged_rows = {(r["a"], r["b"], r["h"]): r["p"] for r in rows(cfg.out / "pair-judgements.jsonl")}
    judged = {}
    pairs = band_pairs(plan["candidates"], candidates, corpus, vec, lo, hi)
    for n, m in pairs:
        h = dedup.sha(corpus[n]["text"] + "\0" + corpus[m]["text"])
        key = (n, m, h)
        if key not in judged_rows:
            state = {"finding_a": corpus[n]["text"][:2500], "finding_b": corpus[m]["text"][:2500]}
            value = models.ask(state, SAME_QUESTION)["same"].get("noul")
            if not isinstance(value, (int, float)):
                raise RuntimeError(f"Clef gave no same-defect probability for {n},{m}")
            append_rows(cfg.out / "pair-judgements.jsonl", [{"a": n, "b": m, "h": h, "p": value,
                                                            "model_digest": pins["clef"]["digest"]}])
            judged_rows[key] = value
        judged[(n, m)] = judged_rows[key]
    assigned = link(plan["candidates"], known.groups, corpus, vec, judged, lo, hi, t)

    decisions = []
    fresh = [i for i in plan["candidates"] if i not in assigned]
    ids = dict(zip(fresh, new_cluster_ids(p, len(fresh), known.groups)))
    cluster_of = {}
    for i in plan["candidates"]:
        f = findings[i]
        base = {"run_id": run_id, "pass": p, "finding_id": i, "title": f["title"], "file": f["file"],
                "line": f.get("line", 0), "luna_difficulty": luna_difficulty(f), "difficulty_agree": None,
                "decided": now()}
        if i in assigned:
            gid, avg = assigned[i]
            cluster_of[i] = gid
            decisions.append({**base, "outcome": "dropped", "reason": "repeat", "joined_cluster": gid,
                              "join_sources": sorted(known.groups[gid]["sources"]),
                              "join_via": "embedding+clef average linkage (dedup.py v2)",
                              "avg_similarity": round(avg, 4)})
        else:
            cluster_of[i] = ids[i]
    # (e) the patrol's own duplicate flag: the repeat joins whatever cluster the key's first report is in.
    for i in plan["flagged"]:
        f = findings[i]
        j = plan["first_by_key"].get(f.get("key"), i)
        gid = cluster_of.get(j, known.member_of.get(j))
        decisions.append({"run_id": run_id, "pass": p, "finding_id": i, "title": f["title"], "file": f["file"],
                          "line": f.get("line", 0), "luna_difficulty": luna_difficulty(f), "difficulty_agree": None,
                          "decided": now(), "outcome": "dropped", "reason": "repeat",
                          "joined_cluster": gid, "first_report": j,
                          "join_sources": sorted(known.groups[gid]["sources"]) if gid in known.groups else
                          [f"new-triage-pass-{p}"] if gid is not None else [],
                          "join_via": f"patrol duplicate flag (key {f.get('key')!r})"})

    # Realness (classify.py) for each new distinct cluster, pinned to the pass revision.
    matched = match_reviews(references, [findings[i] for i in fresh])
    real_rows = {(r["finding_id"], r["context_sha256"]): r for r in rows(cfg.out / "realness.jsonl")}
    diff_rows = {(r["cluster"], r["context_sha256"]): r for r in rows(cfg.out / "difficulty.jsonl")}
    amendments = cfg.frozen_inv / "review-amendments.json"
    for i, refs in zip(fresh, matched):
        f, cid = findings[i], ids[i]
        path = repo.resolve(revision, f["file"])
        source = repo.show(revision, path)
        original = repo.show(f.get("rev") or revision, path)
        state = {"contracts": classify.CONTRACTS, "current_revision": revision, "file": path,
                 "current_code": classify.source_context(source, [f], 8000),
                 "original_code": "IDENTICAL to current code" if original == source else
                 classify.source_context(original, [f], 6000),
                 "prior_review_evidence": refs, "finding": clef_text(f["text"])[:3000]}
        ctx = sha256(json.dumps(state, sort_keys=True))
        real = real_rows.get((i, ctx))
        if real is None:
            answers = models.ask(state, classify.QUESTIONS)
            real = {"finding_id": i, "pass": p, "revision": revision, "path": path, "answers": answers,
                    "disposition": classify.disposition(answers) if path and original else "needs_context",
                    "context_sha256": ctx, "model": pins["clef"]["name"], "model_digest": pins["clef"]["digest"]}
            append_rows(cfg.out / "realness.jsonl", [real])
        lane = lane_for(path or f["file"], lanes)
        row = {"run_id": run_id, "pass": p, "finding_id": i, "new_cluster": True, "cluster": cid,
               "title": f["title"], "file": f["file"], "line": f.get("line", 0), "text": f["text"],
               "lane": lane, "worktree": str(cfg.repo), "revision": revision, "original_revision": f.get("rev"),
               "angle": f.get("angle"), "report_evidence": [], "assignments": [],
               "prior_review_evidence": [{"report": r["report"], "text": r["text"][:600]} for r in refs],
               "realness": real["disposition"], "realness_answers": real["answers"],
               "realness_context_sha256": real["context_sha256"], "resolved_path": path,
               "luna_difficulty": luna_difficulty(f), "difficulty_agree": None,
               "model": pins["clef"]["name"], "model_digest": pins["clef"]["digest"],
               "embed_model": pins["embed"]["name"], "embed_model_digest": pins["embed"]["digest"],
               "decided": now()}
        if real["disposition"] not in OPEN_REALNESS:
            decisions.append({**row, "outcome": "dropped", "reason": real["disposition"], "difficulty": None})
            continue
        dstate = {"contracts": classify.CONTRACTS, "revision": revision, "file": path,
                  "current_code": classify.source_context(source, [f], 18000), "finding": clef_text(f["text"]),
                  "prior_report_evidence": [], "scope": DIFFICULTY_SCOPE}
        if lane == "script" and amendments.exists():
            dstate["dependency_evidence"] = json.loads(amendments.read_text()).get("script_dependency_fact", "")
        dctx = sha256(json.dumps(dstate, sort_keys=True))
        diff = diff_rows.get((cid, dctx))
        if diff is None:
            answers = models.ask(dstate, difficulty_v2.QUESTIONS)
            diff = {"cluster": cid, "finding_id": i, "pass": p, "answers": answers,
                    "difficulty": difficulty.label(answers, bool(path)),
                    "context_required": answers.get("context", {}).get("noul", .5) < .75, "question_version": 2,
                    "conditional_on_realness": True, "resolved_path": path, "context_sha256": dctx,
                    "model": pins["clef"]["name"], "model_digest": pins["clef"]["digest"]}
            append_rows(cfg.out / "difficulty.jsonl", [diff])
        decisions.append({**row, "outcome": "queued", "reason": None, "difficulty": diff["difficulty"],
                          "difficulty_agree": difficulty_agree(row["luna_difficulty"], diff["difficulty"]),
                          "difficulty_answers": diff["answers"], "context_required": diff["context_required"],
                          "difficulty_context_sha256": diff["context_sha256"], "question_version": 2,
                          "conditional_on_realness": True, "prior_disposition": real["disposition"]})
    return decisions


# --------------------------------------------------------------------------- publishing
QUEUE_FIELDS_DROP = ("run_id", "new_cluster", "outcome", "reason", "decided")


def publish(cfg, ledger):
    """queue/*.jsonl and COUNTS.json from the journal alone, each file replaced atomically."""
    queued = [d for d in ledger["decisions"] if d.get("new_cluster") and d.get("outcome") == "queued"]
    out = {k: [] for k in QUEUES}
    for d in queued:
        item = {k: v for k, v in d.items() if k not in QUEUE_FIELDS_DROP}
        item["members"] = list(ledger["clusters"].get(d["cluster"], [d["finding_id"]]))
        out[d["difficulty"]].append(item)
    for kind in QUEUES:
        atomic_write(cfg.out / "queue" / f"{kind}.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n"
                                                                 for x in out[kind]))
    findings = [d for d in ledger["decisions"] if "finding_id" in d]
    repeats = [d for d in findings if d.get("reason") == "repeat"]
    sources = collections.Counter()
    for d in repeats:
        for s in d.get("join_sources") or ["unknown"]:
            sources[re.sub(r"-pass-\d+$", "", s)] += 1
    counts = {"updated": now(), "passes_triaged": sorted(ledger["triaged"]),
              "new_findings": len(findings), "repeats": len(repeats),
              "repeat_by_source": dict(sources),
              "repeat_via": dict(collections.Counter("patrol duplicate flag" if d["join_via"].startswith("patrol")
                                                     else "embedding+clef" for d in repeats)),
              "new_clusters": len(ledger["created"]),
              "realness": dict(collections.Counter(d["realness"] for d in ledger["created"].values())),
              "dropped": dict(collections.Counter(d["reason"] for d in findings if d.get("outcome") == "dropped")),
              "queue": {k: len(v) for k, v in out.items()},
              "luna_vs_clef_difficulty": agreement_tally(queued)}
    atomic_write(cfg.out / "COUNTS.json", json.dumps(counts, indent=2) + "\n")
    return counts


def agreement_tally(queued):
    """Luna's own rating against Clef's v2 call, over queued clusters (the only ones with both)."""
    agree = sum(1 for d in queued if d.get("difficulty_agree") is True)
    disagree = sum(1 for d in queued if d.get("difficulty_agree") is False)
    return {"compared": agree + disagree, "agree": agree, "disagree": disagree,
            "agreement_rate": round(agree / (agree + disagree), 4) if agree + disagree else None,
            "not_compared": len(queued) - agree - disagree,
            "luna_absent": sum(1 for d in queued if not d.get("luna_difficulty")),
            "pairs": dict(collections.Counter(f"luna:{d.get('luna_difficulty') or 'absent'} clef:{d['difficulty']}"
                                              for d in queued))}


# --------------------------------------------------------------------------- campaign pins
def campaign_metadata(cfg, snapshot_sha, review_sha):
    return {"campaign": "luna-new-triage", "snapshot_sha256": snapshot_sha,
            "dedup": {**dedup.DEFAULTS, "embed_model": cfg.embed_model, "same_question_sha256": sha256(dedup.QUESTION),
                      "cannot_link": "same patrol pass", "known_clusters_held_fixed": True},
            "realness_questions_sha256": sha256(json.dumps(classify.QUESTIONS, sort_keys=True)),
            "contracts_sha256": sha256(classify.CONTRACTS), "source_context_version": 4,
            "review_sha256": review_sha,
            "difficulty_questions_sha256": sha256(json.dumps(difficulty_v2.QUESTIONS, sort_keys=True)),
            "difficulty_scope_sha256": sha256(DIFFICULTY_SCOPE), "worktree": str(cfg.repo)}


def pin_campaign(cfg, meta, pins):
    """Refuse changed questions/thresholds/inputs (as classify.py does) or model digests (as difficulty_v2 does)."""
    cfg.out.mkdir(parents=True, exist_ok=True)
    meta_path, model_path = cfg.out / "metadata.json", cfg.out / "model.json"
    if meta_path.exists() and json.loads(meta_path.read_text()) != meta:
        raise SystemExit("Refusing changed questions/thresholds/snapshot/reviews; choose a new output directory")
    if pins is not None and model_path.exists() and json.loads(model_path.read_text()) != pins:
        raise SystemExit("Refusing changed model digest (clef-flash or the embedding model) mid-campaign")
    if not meta_path.exists():
        difficulty.write(meta_path, meta)
    if pins is not None and not model_path.exists():
        difficulty.write(model_path, pins)


# --------------------------------------------------------------------------- runs
def pending_passes(cfg, states, ledger):
    return [s for n, s in sorted(states.items()) if s["complete"] and n not in ledger["triaged"]]


def run_once(cfg, models=None, lease=None, states=None):
    """Triage every completed, untriaged pass. Returns the number of passes triaged."""
    cfg.out.mkdir(parents=True, exist_ok=True)
    lock = open(cfg.out / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(f"another triage_new.py holds {cfg.out / '.lock'}")
    try:
        ledger = load_ledger(cfg)
        recorded = {r["pass"] for r in rows(cfg.out / "TRIAGED-PASSES")}
        for p, run_id in sorted(ledger["done_runs"]):
            if p not in recorded:
                append_rows(cfg.out / "TRIAGED-PASSES", [{"pass": p, "run_id": run_id, "backfilled": True,
                                                         "triaged": now()}])
        states = pass_states(cfg) if states is None else states
        todo = pending_passes(cfg, states, ledger)
        if not todo:
            return 0
        findings, snap_n, snap_sha = load_findings(cfg)
        references, review_sha = review_references(cfg)
        models = models or OllamaModels(cfg)
        repo, lanes = Repo(cfg.repo), inventory_lanes(cfg)
        plans = {s["pass"]: plan_pass(cfg, s, findings, snap_n, ledger) for s in todo}
        for info in todo:
            if plans[info["pass"]]["new"]:
                repo.full(info["rev"])  # refuse before touching the GPU if the pass revision does not resolve
        needs_model = any(plan["candidates"] for plan in plans.values())
        # Model pins come from /api/tags (no GPU); a changed digest is refused before the GPU is requested.
        pins = models.pins() if needs_model else None
        pin_campaign(cfg, campaign_metadata(cfg, snap_sha, review_sha), pins)
        lease = lease if lease is not None else GpuLease(cfg.qwen, poll=cfg.poll)
        with (lease if needs_model else _NoLease()):
            for info in todo:
                run_id = uuid.uuid4().hex[:12]
                decisions = triage_pass(cfg, info, findings, snap_n, ledger, models, pins, repo, references,
                                        lanes, run_id)
                append_rows(cfg.out / "decisions.jsonl",
                            decisions + [{"event": "pass_complete", "pass": info["pass"], "run_id": run_id,
                                          "at": now()}])
                ledger = load_ledger(cfg)
                counts = publish(cfg, ledger)
                queued = sum(1 for d in decisions if d.get("outcome") == "queued")
                append_rows(cfg.out / "TRIAGED-PASSES", [{
                    "pass": info["pass"], "revision": info["rev"], "pass_state": info["state"], "run_id": run_id,
                    "new_findings": sum(1 for d in decisions if "finding_id" in d),
                    "repeats": sum(1 for d in decisions if d.get("reason") == "repeat"),
                    "new_clusters": sum(1 for d in decisions if d.get("new_cluster")), "queued": queued,
                    "triaged": now()}])
                ledger = load_ledger(cfg)
                log(f"pass {info['pass']} ({info['state']}) triaged: {queued} queued; queue now {counts['queue']}")
        publish(cfg, ledger)
        return len(todo)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


class _NoLease:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def dry_run(cfg):
    """Print what would be triaged; no model calls, no GPU handshake, nothing written."""
    ledger = load_ledger(cfg)  # reads only; a missing output directory is an empty ledger
    alive = patrol_alive(cfg)
    states = pass_states(cfg, alive)
    findings, snap_n, _ = load_findings(cfg)
    print(f"patrol process alive: {alive}; STOP present: {(cfg.patrol / 'STOP').exists()}; "
          f"findings.jsonl: {len(findings)} complete lines, frozen snapshot {snap_n}")
    print(f"already triaged passes: {sorted(ledger['triaged']) or 'none'}")
    store = cached_vectors(cfg, cfg.embed_model)
    known = known_clusters(cfg, findings, ledger)
    counts = collections.Counter(s for g in known.groups.values() for s in g["sources"])
    print(f"known clusters: {len(known.groups)} (by source: {dict(counts)})")
    for w in known.warnings:
        print(f"  warning: {w}")
    for n, s in sorted(states.items()):
        plan = plan_pass(cfg, s, findings, snap_n, ledger)
        try:
            full = Repo(cfg.repo).full(s["rev"])[:12]
        except subprocess.CalledProcessError:
            full = "UNRESOLVED in " + str(cfg.repo)
        verdict = ("already triaged" if n in ledger["triaged"] else
                   "WOULD TRIAGE" if s["complete"] else "waiting (pass still running)")
        luna = collections.Counter(luna_difficulty(findings[i]) or "absent" for i in plan["new"])
        print(f"pass {n}: {s['state']:<10} rev {s['rev']} -> {full}  angle {s['angle']:<8} "
              f"new findings {len(plan['new'])} (patrol-duplicate-flagged {len(plan['flagged'])}, "
              f"to embed+judge {len(plan['candidates'])})  -> {verdict}")
        if plan["new"]:
            print(f"  Luna difficulty ratings: {dict(luna)}")
        if plan["candidates"] and n not in ledger["triaged"]:
            print("  " + embedding_estimate(cfg, known, plan, store))


def embedding_estimate(cfg, known, plan, store):
    """Embedding-only dedup estimate from cached vectors: band pairs counted as 0 (lower) or 1 (upper)."""
    corpus = known.corpus
    have = [i for i in plan["candidates"] if embed_key(cfg.embed_model, corpus[i]) in store]
    if not have:
        return (f"dedup estimate: not computable without embedding calls ({len(plan['candidates'])} "
                f"candidates, none cached)")
    import numpy as np
    vec = {}
    for uid, f in corpus.items():
        k = embed_key(cfg.embed_model, f)
        if k in store:
            vec[uid] = store[k] / np.linalg.norm(store[k])
    groups = {gid: {"members": [m for m in g["members"] if m in vec]} for gid, g in known.groups.items()}
    lo, hi, t = dedup.DEFAULTS["lo"], dedup.DEFAULTS["hi"], dedup.DEFAULTS["t"]
    low = link(have, groups, corpus, vec, {}, lo, hi, t)
    high = link(have, groups, corpus, vec, {}, lo, hi, t, band_default=1.0)
    flagged, total = len(plan["flagged"]), len(plan["new"])
    return (f"EMBEDDING-ONLY dedup estimate (no Clef; {len(have)}/{len(plan['candidates'])} candidates cached): "
            f"repeats {flagged + len(low)}..{flagged + len(high)} of {total} new findings "
            f"({(flagged + len(low)) / total:.0%}..{(flagged + len(high)) / total:.0%}), "
            f"band pairs scored 0..1")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="triage every completed, untriaged pass, then exit")
    mode.add_argument("--watch", action="store_true", help="poll; exit when the patrol's STOP exists and nothing is pending")
    mode.add_argument("--dry-run", action="store_true", help="print what would be triaged; no Clef, no GPU handshake")
    ap.add_argument("--interval", type=float, default=600, help="watch poll interval in seconds (default 600)")
    ap.add_argument("--repo", help="git repo holding the pass revisions (default <program>/zene-ui)")
    ap.add_argument("--out", help="output directory (default research/reviews/luna-new-triage)")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    args = ap.parse_args(argv)
    cfg = Config(repo=args.repo, out=args.out, host=args.host)
    if args.dry_run:
        dry_run(cfg)
        return 0
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    if args.once:
        log(f"triaged {run_once(cfg)} pass(es)")
        return 0
    while True:
        n = run_once(cfg)
        if n:
            log(f"triaged {n} pass(es)")
        states = pass_states(cfg)
        waiting = [s for s in states.values() if not s["complete"]]
        if (cfg.patrol / "STOP").exists() and not waiting and not pending_passes(cfg, states, load_ledger(cfg)):
            log("patrol STOP present and no pass pending - exiting")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
