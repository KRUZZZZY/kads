#!/usr/bin/env python3
"""git merge driver: %O %A %B %P. Three-way merge; a conflict hunk is resolved only when the base side is empty
(both branches purely ADDED lines at the same spot) - then ours followed by theirs. Any other conflict stays and
the driver exits 1, so git reports the file as conflicted.
2026-10-25: a hunk whose sides differ only in numbers on lines the merge train regenerates afterwards (canary
figures, the A16 `measured:` line, `rows=` figures) is resolved to ours: the train rewrites them from the merged tree.
Those figure-only hunks were most of the train's feature conflicts."""
import re, subprocess, sys
base, ours, theirs = sys.argv[1:4]
REGEN = re.compile(r"<!-- canary -->|measured: |\brows=\d")


def figures_only(a, c):
    norm = lambda ls: [re.sub(r"\d+", "#", l) for l in ls]
    return a and len(a) == len(c) and norm(a) == norm(c) and any(REGEN.search(l) for l in a + c)
import os
if os.path.getsize(base) == 0 and os.path.getsize(ours) > 0 and os.path.getsize(theirs) > 0:
    sys.exit(1)   # both branches created this file: never glue two whole files together
r = subprocess.run(["git", "merge-file", "-p", "--diff3", "-L", "ours", "-L", "base", "-L", "theirs", ours, base, theirs],
                   capture_output=True)
if r.returncode < 0 or r.returncode > 127:
    sys.exit(2)
lines = r.stdout.decode("utf-8", "surrogateescape").splitlines(keepends=True)
out, i, unresolved = [], 0, False
while i < len(lines):
    l = lines[i]
    if l.startswith("<<<<<<< ours"):
        a, b, c = [], [], []
        i += 1
        while not lines[i].startswith("||||||| base"): a.append(lines[i]); i += 1
        i += 1
        while not lines[i].startswith("======="): b.append(lines[i]); i += 1
        i += 1
        while not lines[i].startswith(">>>>>>> theirs"): c.append(lines[i]); i += 1
        i += 1
        if not b:
            out += a + c
        elif figures_only(a, c):
            out += a
        else:
            unresolved = True
            out += ["<<<<<<< ours\n", *a, "||||||| base\n", *b, "=======\n", *c, ">>>>>>> theirs\n"]
        continue
    out.append(l); i += 1
open(ours, "w", encoding="utf-8", errors="surrogateescape").write("".join(out))
sys.exit(1 if unresolved else 0)
