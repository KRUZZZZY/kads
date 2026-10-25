#!/bin/bash
# Start the bug fixer and QA loop, detached. Luna handles fixes by default (owner, 2026-10-10).
# Usage: bash start.sh            both loops
#        bash start.sh fix|qa     only one
set -u
Q=$HOME/.local/share/zene-bunny-fix/qwen
# Strata listens on 8081: SearXNG already owns 127.0.0.1:8080 on this machine.
export STRATA_URL=${STRATA_URL:-http://127.0.0.1:8081}
export QWEN_ENGINE=${QWEN_ENGINE:-luna}
# Four fixers feed two reviewers; both loops also honor the live LUNA-CAP file.
export QWEN_LUNA_WORKERS=${QWEN_LUNA_WORKERS:-4}
export QWEN_QA_WORKERS=${QWEN_QA_WORKERS:-2}
# Share two build slots while the local model also occupies system RAM.
export BUNNY_SLOTS=${BUNNY_SLOTS:-2}
if [ -f "$Q/DISABLED" ]; then echo "Qwen pipeline DISABLED by the owner (2026-10-08): $(cat "$Q/DISABLED")"; exit 1; fi
if [ -f "$Q/ALL-PAUSED" ]; then echo "Bug pipeline paused by the owner; resume explicitly before starting."; exit 1; fi
which=${1:-both}
# clear only the flags for the loop(s) being started, so starting one never un-stops the other
rm -f "$Q/STOP"
[ "$which" = qa ] || rm -f "$Q/STOP-FIX"
[ "$which" = fix ] || rm -f "$Q/STOP-QA"

for loop in fix qa; do
  [ "$which" = both ] || [ "$which" = "$loop" ] || continue
  if [ -f "$Q/$loop.pid" ] && kill -0 "$(cat "$Q/$loop.pid")" 2>/dev/null; then
    echo "$loop loop already running (pid $(cat "$Q/$loop.pid"))"; continue
  fi
  [ "$loop" = fix ] && rm -f "$Q/FIX-DRAINED"
  setsid nohup python3 "$Q/qwenfix.py" "$loop" >> "$Q/logs/$loop.out" 2>&1 < /dev/null &
  echo $! > "$Q/$loop.pid"
  echo "$loop loop started (pid $!)"
done
echo "watch: python3 $Q/qwenfix.py status    stop: touch $Q/STOP"
