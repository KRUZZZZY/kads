#!/usr/bin/env bash
# Owner 2026-10-20: "after the weekly reset at 1am tomorrow pause all new claude lanes". Run once by the transient
# systemd timer zene-pause-claude-lanes (2026-10-21 01:00): disables every enabled Claude lane so none starts a new
# session; sessions already running finish, and units in review keep their lane for reworks once it is enabled again.
set -euo pipefail
cd "$(dirname "$0")"
cp LANES.json "LANES.json.bak-pre-pause-$(date +%Y-%m-%d-%H%M)"
for lane in $(python3 -c 'import json;print(" ".join(l["name"] for l in json.load(open("LANES.json"))["lanes"] if l.get("enabled")))'); do
  python3 lanes.py disable "$lane"
done
python3 - <<'PY'
import json, time
d = json.load(open("LANES.json"))
d["_note"] += f" {time.strftime('%Y-%m-%d %H:%M')} (scheduled, owner 2026-10-20): all Claude lanes disabled after the weekly reset."
json.dump(d, open("LANES.json", "w"), indent=1); open("LANES.json", "a").write("\n")
PY
notify-me "Zene: Claude lanes paused after the weekly reset (as asked). Running sessions finish; nothing new starts." || true
