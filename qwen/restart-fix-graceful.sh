#!/bin/bash
# 2026-10-19: the fix loop runs as zene-fix.service (Restart=always). A graceful restart is: touch STOP-FIX - the loop lets
# its workers finish, exits, and systemd starts it again (ExecStartPre removes STOP-FIX).
touch /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/STOP-FIX
echo "graceful restart of zene-fix requested at $(date +%T)" >> /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/logs/restart.log
