#!/usr/bin/env python3
"""Root-cause families across the open bug queue (owner 2026-10-19: "check if there's common root causes for any
bug ... is it possible to be done algorithmically").

The dedup joins reports of the SAME defect; this looks for DIFFERENT bugs that share one cause, so one systemic fix
can close several. Candidate families come from three signals, combined into one graph:
  1. code: the function each finding sits in (code_locations.py: the enclosing function of the finding's line at the
     revision it was read from); same function, same function name elsewhere, or a shared rare callee pull bugs together;
  2. text: nomic-embed-text similarity across DIFFERENT files at a looser threshold than the dedup's;
  3. pattern: keyword classes (non-finite numbers, deferred deletion, undo before-state, audio-thread allocation, ...)
     - a pattern only strengthens a text link, it never links on its own.
Connected components of 3..MAX bugs are the candidate families. Step 4 (does a family really share one cause?) is
a separate, measured judge: see root_cause_eval.py and the `confirm` action.

Step 4 (measured 2026-10-19 on 90 hand-labelled pairs, see root_cause_eval.py): Clef's "one change fixes both?"
judgement, averaged over the plain question and the same question with each bug's current code, separates
same-cause pairs well (AUC 0.96-0.99; at 0.25: 80 % precision, 80 % recall, most false positives being same-pattern
pairs). `confirm` clusters every open bug (average linkage, cut CUT), scores every pair inside each cluster with that
judge, joins pairs at >= JUDGE_THRESHOLD and writes the connected groups (2+ bugs) to qwen/ROOT-CAUSE-GROUPS.json.
qwenfix turns a group into one job (a batch across files): one shared fix, a regression test per bug; a member that
turns out not to share the cause is reported NEEDS-CONTEXT and goes back to the queue.

Usage: root_causes.py candidates | confirm [--no-lease] | all
"""
import argparse
import collections
import hashlib
import importlib.util
import json
import math
import pathlib
import re
import subprocess
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PROG = HERE.parents[1]
QWEN = pathlib.Path.home() / ".local/share/zene-bunny-fix/qwen"
OUT = PROG / "research/reviews/root-causes"
TREE = PROG / "zene-cachyos"
EMBED = "http://127.0.0.1:11434/api/embed"
IDENT = re.compile(r"`([A-Za-z_][\w]*(?:::[A-Za-z_]\w*)*)(?:\([^`]*\))?`")
GENERIC = {"std", "QString", "QVariant", "QList", "QVector", "nullptr", "true", "false", "size_t", "std::size_t",
           "std::thread", "std::unique_ptr", "std::shared_ptr", "std::vector", "std::clamp", "std::min", "std::max",
           "std::terminate", "QObject", "QWidget", "deleteLater", "emit", "override", "const", "static"}
PATTERNS = {
    "non-finite-number": r"\bnan\b|non-?finite|infinit|\binf\b|toDouble|toFloat|floating.{0,20}(cast|conver)",
    "deferred-delete": r"deleteLater|WA_DeleteOnClose|deferred delet|queued for delet",
    "undo-before-state": r"before.?state|undo (record|step|snapshot)|inverse|transaction",
    "audio-thread-alloc": r"(allocat|lock|free|mutex|log)\w* (on|in|from) the (audio|render|realtime)|audio (thread|callback|path)",
    "limit-silently-changed": r"silently (clamp|chang|truncat|round|accept)|MaxSongLength|above the limit",
    "settings-parse": r"loadSettings|saveSettings|settings? (read|load|pars)",
    "dangling-lifetime": r"dangl|outliv|use.after|after (free|delet|remov)|stale (pointer|handle|reference)",
    "cross-thread-signal": r"signal|slot|queued connection|cross.thread|wrong thread",
    "error-message": r"(message|error text|reports?|says) .{0,40}(wrong|incorrect|mislead|stale)",
}
SIM_LINK = 0.80          # text-only link (different files)
SIM_PATTERN = 0.72       # text link when both bugs share a pattern class
MAX_IDENT_SPREAD = 25    # an identifier named by more bugs than this is too generic to link on
MAX_FAMILY = 25
CUT = 0.18               # average-linkage distance cut for the clusters the judge examines
JUDGE_THRESHOLD = 0.25   # mean of the plain and code-context judgements


def queue():
    sys.path.insert(0, str(QWEN))
    spec = importlib.util.spec_from_file_location("qwenfix_rc", QWEN / "qwenfix.py")
    q = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q)
    return [i for i in q.load_queue() if not (q.D["state"] / f"{q.slug(i['cluster'])}.json").exists()], q


def bug_text(i):
    return re.sub(r"^BUG \d+:\s*", "", f"{i.get('title', '')}. {i.get('text', '')}", flags=re.M)[:1500]


def tree_identifiers():
    """Every identifier-like token in the C++ sources of the current train (one pass with git grep -o)."""
    out = subprocess.run(["git", "-C", str(TREE), "grep", "-ohE", r"\b[A-Za-z_][A-Za-z0-9_]{5,}\b", "--",
                          "*.cpp", "*.h", "*.hpp", "*.c"], capture_output=True, text=True, errors="replace").stdout
    return set(out.split())


def identifiers(i, known):
    found = set()
    for m in IDENT.findall(bug_text(i)):
        last = m.split("::")[-1]
        if m in GENERIC or last in GENERIC or len(last) < 6 or last not in known:
            continue
        found.add(last)
    return found


def embed(texts):
    vecs = []
    for k in range(0, len(texts), 64):
        req = urllib.request.Request(EMBED, data=json.dumps({"model": "nomic-embed-text",
                                                             "input": [f"clustering: {t}" for t in texts[k:k + 64]]}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            vecs += json.load(r)["embeddings"]
    norm = [math.sqrt(sum(x * x for x in v)) or 1.0 for v in vecs]
    return [[x / n for x in v] for v, n in zip(vecs, norm)]


def candidates():
    OUT.mkdir(parents=True, exist_ok=True)
    items, q = queue()
    known = tree_identifiers()
    ids = [str(i["cluster"]) for i in items]
    texts = [bug_text(i) for i in items]
    files = [str(i.get("resolved_path") or i.get("file") or "") for i in items]
    pats = [{k for k, rx in PATTERNS.items() if re.search(rx, t, re.I)} for t in texts]
    idents = [identifiers(i, known) for i in items]
    spread = collections.Counter(x for s in idents for x in s)
    edges = collections.defaultdict(set)      # (a, b) -> reasons
    by_ident = collections.defaultdict(list)
    for n, s in enumerate(idents):
        for x in s:
            if 2 <= spread[x] <= MAX_IDENT_SPREAD:
                by_ident[x].append(n)
    for x, members in by_ident.items():
        for a in members:
            for b in members:
                if a < b:
                    edges[(a, b)].add(f"code:{x}")
    import numpy as np
    vec = np.array(embed(texts), dtype=np.float32)
    sim = vec @ vec.T
    for a in range(len(items)):
        row = sim[a]
        for b in np.nonzero(row >= SIM_PATTERN)[0]:
            b = int(b)
            if b <= a or files[a] == files[b]:
                continue
            shared = pats[a] & pats[b]
            if row[b] >= SIM_LINK:
                edges[(a, b)].add(f"text:{row[b]:.2f}")
            elif shared:
                edges[(a, b)].add(f"text+pattern:{row[b]:.2f}:{sorted(shared)[0]}")
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in edges:
        parent[find(a)] = find(b)
    groups = collections.defaultdict(list)
    for n in range(len(items)):
        groups[find(n)].append(n)
    fams = []
    for members in groups.values():
        if not 3 <= len(members) <= MAX_FAMILY:
            continue
        reasons = collections.Counter(r.split(":")[0] + ":" + r.split(":")[1] if r.startswith("code") else r.split(":")[0]
                                      for a in members for b in members if a < b for r in edges.get((a, b), ()))
        fams.append({"members": [ids[n] for n in members], "size": len(members),
                     "files": sorted({files[n] for n in members}), "titles": [items[n].get("title") for n in members],
                     "links": dict(reasons.most_common(6)),
                     "patterns": dict(collections.Counter(p for n in members for p in pats[n]).most_common(3))})
    fams.sort(key=lambda f: -f["size"])
    big = sum(1 for m in groups.values() if len(m) > MAX_FAMILY)
    (OUT / "candidates.json").write_text(json.dumps({"bugs": len(items), "edges": len(edges), "families": fams,
                                                     "oversized_components": big}, indent=1))
    print(f"{len(items)} open bugs, {len(edges)} links, {len(fams)} candidate families "
          f"({sum(f['size'] for f in fams)} bugs), {big} components over {MAX_FAMILY} left out")
    return fams


def distances(items):
    """Combined distance (measured on the 90 labelled pairs, 2026-10-19): 1 - text similarity, x0.5 when both bugs sit
    in the same function of the same file (code_locations.locate: the function enclosing the finding's line at the
    revision it was read from), x0.8 for the same function name in different files, x0.85 when their functions share a
    rarely-called callee. AUC for same-cause pairs: text alone 0.76, + backticked identifiers 0.68 (worse, dropped),
    + located functions 0.80. Pattern classes are kept for labelling only (they lowered same-cause AUC)."""
    import numpy as np
    import code_locations as C
    loc = [C.locate(i) for i in items]
    spread = collections.Counter(x for g in loc if g for x in set(g["callees"]))
    rare = [{x for x in g["callees"] if 2 <= spread[x] <= 15} if g else set() for g in loc]
    vec = np.array(embed([bug_text(i) for i in items]), dtype=np.float32)
    dist = np.clip(1 - vec @ vec.T, 0, None)
    np.fill_diagonal(dist, 0)
    by_func = collections.defaultdict(list)
    by_name = collections.defaultdict(list)
    for n, g in enumerate(loc):
        if g:
            by_func[(g["file"], g["function"])].append(n)
            by_name[g["function"].split("::")[-1]].append(n)
    factor = {}
    for members in by_name.values():
        for x in members:
            for y in members:
                if x < y:
                    factor[(x, y)] = 0.8
    for members in by_func.values():
        for x in members:
            for y in members:
                if x < y:
                    factor[(x, y)] = 0.5
    by_callee = collections.defaultdict(list)
    for n, r in enumerate(rare):
        for c in r:
            by_callee[c].append(n)
    for members in by_callee.values():
        for x in members:
            for y in members:
                if x < y:
                    factor[(x, y)] = factor.get((x, y), 1.0) * 0.85 if factor.get((x, y), 1.0) >= 0.8 else factor[(x, y)]
    for (x, y), f in factor.items():
        dist[x, y] = dist[y, x] = dist[x, y] * f
    return dist


def confirm(lease=True):
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform
    import root_cause_eval as E
    OUT.mkdir(parents=True, exist_ok=True)
    items, q = queue()
    ids = [str(i["cluster"]) for i in items]
    dist = distances(items)
    lab = fcluster(linkage(squareform(dist, checks=False), "average"), CUT, "distance")
    clusters = collections.defaultdict(list)
    for n, l in enumerate(lab):
        clusters[l].append(n)
    clusters = [m for m in clusters.values() if 2 <= len(m) <= MAX_FAMILY]
    journal = OUT / "judgements.jsonl"
    seen = {}
    if journal.exists():
        for line in journal.read_text().splitlines():
            r = json.loads(line)
            seen[(r["a"], r["b"])] = r["score"]
    import contextlib
    import triage_new as T
    ctx = T.GpuLease(QWEN) if lease else contextlib.nullcontext()
    edges = []
    with ctx, journal.open("a") as jf:
        for members in clusters:
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    a, b = members[x], members[y]
                    key = (ids[a], ids[b])
                    if key not in seen:
                        A, B = items[a], items[b]
                        ta, tb = bug_text(A), bug_text(B)
                        plain = E.m_noul(ta, tb)
                        coded = E.m_noul(ta, tb, {"code_a": E.code_around(A), "code_b": E.code_around(B)})
                        seen[key] = (plain + coded) / 2
                        jf.write(json.dumps({"a": key[0], "b": key[1], "plain": plain, "code": coded, "score": seen[key]}) + "\n")
                        jf.flush()
                    if seen[key] >= JUDGE_THRESHOLD:
                        edges.append((a, b, seen[key]))
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b, _ in edges:
        parent[find(a)] = find(b)
    groups = collections.defaultdict(set)
    for a, b, _ in edges:
        groups[find(a)].update((a, b))
    out = []
    for n, members in enumerate(sorted(groups.values(), key=len, reverse=True)):
        m = sorted(members)
        scores = [sc for a, b, sc in edges if a in members and b in members]
        gid = "rc-" + hashlib.sha1("|".join(sorted(ids[i] for i in m)).encode()).hexdigest()[:8]   # stable across runs
        out.append({"id": gid, "members": [ids[i] for i in m], "size": len(m),
                    "score_mean": round(sum(scores) / len(scores), 3),
                    "files": sorted({str(items[i].get("resolved_path") or items[i].get("file") or "") for i in m}),
                    "titles": [items[i].get("title") for i in m]})
    payload = {"generated": T.now(), "bugs": len(items), "clusters_examined": len(clusters),
               "pairs_judged": sum(len(m) * (len(m) - 1) // 2 for m in clusters), "threshold": JUDGE_THRESHOLD,
               "cut": CUT, "groups": out}
    (OUT / "groups.json").write_text(json.dumps(payload, indent=1))
    (QWEN / "ROOT-CAUSE-GROUPS.json").write_text(json.dumps(payload, indent=1))
    print(f"{len(out)} root-cause groups covering {sum(g['size'] for g in out)} bugs "
          f"(from {len(clusters)} clusters, {payload['pairs_judged']} pairs judged)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["candidates", "confirm", "all"])
    ap.add_argument("--no-lease", action="store_true")
    a = ap.parse_args()
    if a.action == "candidates":
        candidates()
    else:
        confirm(lease=not a.no_lease)
