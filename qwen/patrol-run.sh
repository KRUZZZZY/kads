#!/bin/bash
# Owner 2026-10-24: permanently disabled, regardless of model or usage resets.
if [ -e /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/PATROL-DISABLED ]; then
  echo "Bug sweep permanently disabled by owner (PATROL-DISABLED)."
  exit 0
fi
# zene-patrol.service: the Luna bug sweep on the current fix base. Owner 2026-10-19: stop at 50 % of Luna's week
# (usage_monitor writes the patrol STOP file); a start while Luna is at or above that leaves it stopped.
cd /home/kruzzzzy/Documents/AI_KOS_PROJECT/projects/zene-studio-program || exit 1
# Owner 2026-10-20: "luna is not allowed a bug fix, a bug finder, and reviews all at once, only 2 of the 3" - while
# qwen/PATROL-HOLD exists Luna reviews and fixes, and the patrol (also restarted by the merge train) stays stopped.
# Owner 2026-10-24: Beam does the finding (qwen/PATROL-ENGINE = beam); the hold and the Luna budget are about Luna only.
Q=/home/kruzzzzy/.local/share/zene-bunny-fix/qwen
if [ "$(cat $Q/PATROL-ENGINE 2>/dev/null)" = beam ]; then
  rm -f research/reviews/luna-patrol/STOP
  exec python3 tools/luna-patrol/patrol.py --repo zene-cachyos --branch "$(cat $Q/FIX-BASE)" --workers 3
fi
if [ -e /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/PATROL-HOLD ]; then echo "PATROL-HOLD: $(cat /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/PATROL-HOLD)"; exit 0; fi
used=$(python3 -c "import json;print(json.load(open('/home/kruzzzzy/.local/share/zene-bunny-fix/qwen/USAGE.json')).get('luna_weekly_used',0))" 2>/dev/null || echo 0)
if [ "${used:-0}" -ge 50 ]; then echo "Luna weekly usage $used% >= 50% - patrol stays stopped"; exit 0; fi
rm -f research/reviews/luna-patrol/STOP
exec python3 tools/luna-patrol/patrol.py --repo zene-cachyos --branch "$(cat /home/kruzzzzy/.local/share/zene-bunny-fix/qwen/FIX-BASE)" --workers 3
