#!/bin/bash
# Owner 2026-10-19: "switch strata back on after the sort". Waits for Clef's pairwise difficulty sort to finish, then
# re-enables the local Strata fixer (bugs only, easier half; it still yields the GPU to Clef triage).
while kill -0 1972194 2>/dev/null; do sleep 60; done
[ -f /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/QWEN-PAUSED ] && mv /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/QWEN-PAUSED /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/QWEN-PAUSED.lifted-$(date +%Y%m%d-%H%M)
echo "owner 2026-10-19: Strata back on after the difficulty sort (bugs only, the easier half of BUG-RANK)" > /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/LOCAL-QWEN
if [ -f /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/BUG-RANK.json ]; then msg="Clef's difficulty sort finished ($(python3 -c "import json;print(len(json.load(open('/home/kruzzzzy/.local/share/zene-bunny-fix/qwen/BUG-RANK.json'))))") bugs ranked); Strata is back on, fixing the easier half of the bug queue."
else msg="Clef's difficulty sort ended without a ranking (see research/reviews/bug-difficulty/run.log); Strata is back on anyway, on the confidence/severity order."; fi
python3 "$(dirname "$0")/notify.py" "$msg"
echo "[$(date +%T)] $msg" >> /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/logs/restart.log
