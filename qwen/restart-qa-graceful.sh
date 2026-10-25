#!/bin/bash
# 2026-10-19: the qa loop runs as zene-qa.service (Restart=always). A graceful restart is: touch STOP-QA - the loop lets
# its workers finish, exits, and systemd starts it again (ExecStartPre removes STOP-QA).
touch /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/STOP-QA
echo "graceful restart of zene-qa requested at $(date +%T)" >> /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/logs/restart.log
