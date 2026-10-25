#!/usr/bin/env python3
"""Refill each Bunny lane's NOT-YET-STARTED batches from the v2 dedup clusters.

A lane's universe is every finding in the Clef clusters it was given. Findings are mapped to v2
clusters; any v2 cluster touched by a batch that has started (state file present) or finished is
dropped; one representative per remaining v2 cluster is queued (a likely-open member from the latest
pass when there is one). Unstarted batch briefs are rewritten atomically; surplus tags get a skip
state file so the driver passes over them. Usage: rebatch.py [--dry-run]
"""
import json, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import make_batches as mb  # noqa: E402

PROG = mb.PROG
CLEF = f"{PROG}/research/reviews/luna-clef-2026-10-05"
V2 = f"{PROG}/research/reviews/luna-dedup-2026-10-05/run-v2/clusters.jsonl"
DRY = "--dry-run" in sys.argv

F = [json.loads(l) for l in open(f"{CLEF}/findings-snapshot.jsonl")]
DISP = {d["id"]: d for d in map(json.loads, open(f"{CLEF}/decisions.jsonl"))}
clef_members = {c["representative"]: c["members"] for c in map(json.loads, open(f"{CLEF}/clusters.jsonl"))}
v2_of = {}
v2_members = {}
for c in map(json.loads, open(V2)):
    v2_members[c["representative"]] = c["members"]
    for m in c["members"]:
        v2_of[m] = c["representative"]


def item_for(members):
    open_ = [m for m in members if DISP[m]["disposition"] == "likely_open_bug"]
    pool = open_ or members
    best = max(pool, key=lambda m: (F[m]["pass"], DISP[m]["answers"]["real"]["score"]))
    f = F[best]
    return dict(cluster=v2_of[best], members=members, id=best, file=f["file"], line=f["line"], title=f["title"],
                text=f["text"], score=round(DISP[best]["answers"]["real"]["score"], 2), rev=f["rev"])


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


summary = {}
for lane in sorted(os.listdir(os.path.join(ROOT, "lanes"))):
    d = os.path.join(ROOT, "lanes", lane)
    cfg = json.load(open(os.path.join(d, "lane.json")))
    batches = json.load(open(os.path.join(d, "batches.json")))
    state = os.path.join(d, "state")
    started = [b for b in batches if os.path.exists(os.path.join(state, f"{b['tag']}.json"))
               or os.path.exists(os.path.join(state, f"{b['tag']}-session.log"))]
    pending = [b for b in batches if b not in started and b["clusters"]]
    universe = sorted({m for b in batches for c in b["clusters"] for m in clef_members.get(c, [c])})
    taken = {v2_of[m] for b in started for c in b["clusters"] for m in clef_members.get(c, [c])}
    remaining = sorted({v2_of[m] for m in universe} - taken)
    items = [item_for(v2_members[r]) for r in remaining]
    items = [x for x in items if any(DISP[m]["disposition"] == "likely_open_bug" for m in x["members"])]
    items.sort(key=lambda x: (x["file"], x["line"]))
    chunks = [items[i:i + mb.BATCH] for i in range(0, len(items), mb.BATCH)]
    old = sum(len(b["clusters"]) for b in pending)
    summary[lane] = {"pending_batches": len(pending), "old_items": old, "new_items": len(items),
                     "new_batches": len(chunks)}
    if DRY:
        continue
    scope = mb.SCOPES.get(lane, "see the lane's takeover brief")
    for k, b in enumerate(pending):
        tag = b["tag"]
        if os.path.exists(os.path.join(state, f"{tag}-session.log")):
            continue  # started while we were working
        if k < len(chunks):
            chunk = chunks[k]
            txt = (mb.header(lane, cfg["worktree"], cfg["branch"], scope) + mb.RULES.format(wt=cfg["worktree"]) + "\n"
                   + mb.PROCEDURE + f"\n## The {len(chunk)} findings for this batch\n\n"
                   + "Each finding below is one defect after automated deduplication (v2); the repeat reports of it "
                     "were removed, so do not expect duplicates inside this batch.\n\n"
                   + "\n".join(mb.finding_block(x) for x in chunk) + "\n"
                   + mb.REPORT.format(date=mb.DATE, lane=lane, tag=tag))
            write_atomic(os.path.join(d, "batches", f"{tag}.md"), txt)
        else:
            write_atomic(os.path.join(state, f"{tag}.json"),
                         json.dumps({"tag": tag, "skipped": "no findings left after v2 deduplication"}))
    if len(chunks) > len(pending):
        summary[lane]["overflow_items_not_queued"] = sum(len(c) for c in chunks[len(pending):])
print(json.dumps(summary, indent=1))
