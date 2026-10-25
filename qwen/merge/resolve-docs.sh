#!/bin/bash
# Resolve conflicted generated/doc files in the current merge (orchestrator feature ports, 2026-10-19):
# README / release notes / commands snapshot / all-sources: conflicting hunks take ours (the train regenerates them);
# Everything else, the manual and docs/*.md included, stays conflicted for a hand resolution.
tmp=$(mktemp -d)
for f in $(git diff --name-only --diff-filter=U); do
  case "$f" in
    README.md|docs/RELEASE-NOTES*.md|tools/mcp-zene-control/zene_control/commands_snapshot.json|tests/all-sources.txt) mode=--ours ;;
    *) echo "left conflicted: $f"; continue ;;
  esac
  git show ":1:$f" > "$tmp/base" 2>/dev/null || : > "$tmp/base"
  git show ":2:$f" > "$tmp/ours"
  git show ":3:$f" > "$tmp/theirs"
  git merge-file "$mode" "$tmp/ours" "$tmp/base" "$tmp/theirs"
  cp "$tmp/ours" "$f"
  git add "$f"
  echo "resolved $mode: $f"
done
rm -rf "$tmp"
