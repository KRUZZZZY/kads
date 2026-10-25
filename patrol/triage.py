#!/usr/bin/env python3
"""triage.py: rank and de-duplicate luna-patrol's findings with Clef-Flash (local, through Ollama).

For every distinct finding in <out>/findings.jsonl:
  1. Reads the current code at the finding's file and line from the product worktree's committed tip.
  2. Asks Clef-Flash in one call: is the defect still in that code? how likely is it real (a 5-level score)?
     how severe? which area?
  3. Embeds each finding (nomic-embed-text). Findings in the same file whose embeddings are close get one more
     Clef-Flash question, "same defect?", and those that are join one cluster.
Writes <out>/triage.jsonl (one record per finding with its answers and cluster) and <out>/TRIAGE.md (the clusters
still present, ranked). Nothing here verifies a bug: it orders the queue a reviewer works through.

Usage: triage.py [--out DIR] [--repo DIR] [--limit N] [--host URL]
"""
import argparse
import json
import math
import os
import re
import subprocess
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = ["Almost certainly not a bug", "Unlikely to be a bug", "Plausible", "Likely a real bug", "Almost certainly a real bug"]
SEVERITY = {"crash": "Crash, hang, memory corruption or data loss", "audio": "Wrong or glitching audio, or the realtime rule broken",
            "behaviour": "A feature does the wrong thing or undo is wrong", "ui": "Cosmetic or wording only", "perf": "Slowness only"}
AREA = {"engine": "Audio engine, mixer, devices, plug-in hosting", "editing": "Arranger, clips, notes, automation",
        "commands": "The control commands, undo journal, scripting", "interface": "Widgets, menus, dialogs, panels",
        "files": "Saving, loading, import, export, rendering"}


def post(host, path, body):
    req = urllib.request.Request(host + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as reply:
        return json.load(reply)


def resolve(repo, name, cache={}):
    if not name:
        return ""
    if not cache:
        files = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True).stdout.split()
        for path in files:
            cache.setdefault(os.path.basename(path), path)
            cache[path] = path
    return cache.get(name) or cache.get(os.path.basename(name), "")


def code_at(repo, path, line, around=40):
    if not path:
        return ""
    lines = subprocess.run(["git", "-C", repo, "show", f"HEAD:{path}"], capture_output=True, text=True, errors="replace").stdout.split("\n")
    lo = max(0, (line or 1) - around)
    return "\n".join(f"{i + 1:5d}  {text}" for i, text in enumerate(lines[lo:lo + 2 * around], lo))


def judge(host, finding, code):
    questions = {
        "still_present": {"type": "noul", "instructions": "Does the current code still contain the defect the finding describes?"},
        "real": {"type": "score", "instructions": "How likely is the finding a real bug, given the code?", "criteria": REAL},
        "severity": {"type": "choice", "instructions": "If real, how bad is it?", "criteria": SEVERITY},
        "area": {"type": "choice", "instructions": "Which part of the program is it in?", "criteria": AREA},
    }
    state = {"finding": finding["text"][:4000], "current_code": code or "(the file is not in the tree any more)"}
    return post(host, "/v1/systemone", {"model": "clef-flash", "state": state, "questions": questions}).get("answers", {})


def same_defect(host, a, b):
    state = {"finding_a": a["text"][:2500], "finding_b": b["text"][:2500]}
    q = {"same": {"type": "noul", "instructions": "Do the two findings describe the same defect in the same place?"}}
    return post(host, "/v1/systemone", {"model": "clef-flash", "state": state, "questions": q}).get("answers", {}).get("same", {})


def embed(host, text):
    return post(host, "/api/embeddings", {"model": "nomic-embed-text", "prompt": text[:2000]}).get("embedding", [])


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def prob(answer, key="noul"):
    value = answer.get(key) if isinstance(answer, dict) else answer
    return float(value) if isinstance(value, (int, float)) else 0.0


def clusters_of(host, records):
    parent = list(range(len(records)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    by_file = defaultdict(list)
    for i, r in enumerate(records):
        by_file[r["path"]].append(i)
    for indices in by_file.values():
        for x in range(len(indices)):
            for y in range(x + 1, len(indices)):
                i, j = indices[x], indices[y]
                if find(i) == find(j) or cosine(records[i]["vec"], records[j]["vec"]) < 0.86:
                    continue
                if prob(same_defect(host, records[i], records[j])) >= 0.5:
                    parent[find(j)] = find(i)
    return [find(i) for i in range(len(records))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "..", "research", "reviews", "luna-patrol"))
    ap.add_argument("--repo", default=os.path.join(HERE, "..", "..", "zene-survey"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    args = ap.parse_args()
    findings = [json.loads(line) for line in open(os.path.join(args.out, "findings.jsonl"))]
    findings = [f for f in findings if not f.get("duplicate")]
    if args.limit:
        findings = findings[:args.limit]
    records = []
    for n, f in enumerate(findings, 1):
        path = resolve(args.repo, f.get("file", ""))
        answers = judge(args.host, f, code_at(args.repo, path, f.get("line", 0)))
        f.update(path=path, answers=answers, vec=embed(args.host, f["title"] + "\n" + f["text"][:1500]))
        records.append(f)
        if n % 100 == 0:
            print(f"judged {n}/{len(findings)}", flush=True)
    roots = clusters_of(args.host, records)
    with open(os.path.join(args.out, "triage.jsonl"), "w") as out:
        for r, root in zip(records, roots):
            r["cluster"] = root
            out.write(json.dumps({k: v for k, v in r.items() if k != "vec"}) + "\n")
    write_report(args.out, records, roots)


def rank_of(r):
    a = r["answers"]
    present = prob(a.get("still_present", {}))
    real = a.get("real", {}).get("score", 0.0) if isinstance(a.get("real"), dict) else 0.0
    severe = {"crash": 4, "audio": 3, "behaviour": 2, "perf": 1, "ui": 0}.get(a.get("severity", {}).get("choice", ""), 0)
    return present * (real + 1) * (severe + 1)


def write_report(out, records, roots):
    groups = defaultdict(list)
    for r, root in zip(records, roots):
        groups[root].append(r)
    rows = []
    for members in groups.values():
        best = max(members, key=rank_of)
        if prob(best["answers"].get("still_present", {})) < 0.5:
            continue
        rows.append((rank_of(best), best, len(members)))
    rows.sort(key=lambda row: -row[0])
    with open(os.path.join(out, "TRIAGE.md"), "w") as f:
        f.write(f"# luna-patrol triage (Clef-Flash)\n\n{len(records)} distinct findings, {len(groups)} clusters, {len(rows)} still "
                "present in the code. Ranked by present x likely-real x severity. Leads, not verified bugs.\n\n")
        for score, r, size in rows:
            a = r["answers"]
            f.write(f"- **{r['title']}** ({r['path']}:{r.get('line', 0)}) - real {a.get('real', {}).get('score', '?')}/4, "
                    f"{a.get('severity', {}).get('choice', '?')}, {a.get('area', {}).get('choice', '?')}, x{size} "
                    f"[pass {r.get('pass')} slice {r.get('slice')}]\n")


if __name__ == "__main__":
    main()
