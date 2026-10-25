#!/bin/bash
# One-off 2026-10-19: hand the hand-started processes over to their user services, each after a graceful drain.
log() { echo "[$(date +%T)] $*" >> /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/logs/restart.log; }
mig() { # name pid stopfile service
  [ -n "$2" ] && kill -0 "$2" 2>/dev/null && { [ -n "$3" ] && touch "$3"; while kill -0 "$2" 2>/dev/null; do sleep 20; done; }
  systemctl --user start "$4"; log "migrated $1 (old pid $2) -> $4"; }
mig fix "$(cat /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/fix.pid)" /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/STOP-FIX zene-fix &
mig qa "$(cat /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/qa.pid)" /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/STOP-QA zene-qa &
mig scope "240135" /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/STOP-SCOPE zene-scope &
mig patrol 1567639 /home/kruzzzzy/Documents/AI_KOS_PROJECT/projects/zene-studio-program/research/reviews/luna-patrol/STOP zene-patrol &
wait
