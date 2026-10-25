#!/usr/bin/env python3
"""git merge driver: %O %A %B %P. Three-way merge; a conflict hunk is resolved only when the base side is empty
(both branches purely ADDED lines at the same spot) - then ours followed by theirs. Any other conflict stays and
the driver exits 1, so git reports the file as conflicted."""
import subprocess, sys
base, ours, theirs = sys.argv[1:4]
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
        else:
            unresolved = True
            out += ["<<<<<<< ours\n", *a, "||||||| base\n", *b, "=======\n", *c, ">>>>>>> theirs\n"]
        continue
    out.append(l); i += 1
open(ours, "w", encoding="utf-8", errors="surrogateescape").write("".join(out))
sys.exit(1 if unresolved else 0)
