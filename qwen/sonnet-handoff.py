#!/usr/bin/env python3
"""Owner 2026-10-21: "only 1 sonnet lane after one of these are done".

Both Sonnet lanes run with tiers ["drain"] (no tier has that name), so neither takes a new unit while each finishes the
unit it holds (reworks included: a lane's held unit comes back to it whatever its tiers). Run every 3 min by the
transient timer zene-sonnet-handoff: when one lane's watched unit is finished (a final outcome, or escalated away by
features_queue.py), that lane is disabled, the other gets its saved tiers back, the owner is told, and the timer stops.
"""
import json
import subprocess
import time
from pathlib import Path
import notify

ROOT = Path(__file__).resolve().parent
LANES = ROOT / "LANES.json"
FINAL = ("accept", "confirm", "parked", "no-result", "driver-error", "build-failed")   # qwenfix.FINAL_OUTCOMES


def finished(unit, since):
    st = ROOT / "state" / f"{unit}.json"
    if st.exists():
        try:
            return str(json.loads(st.read_text()).get("outcome", "")).lower().startswith(FINAL)
        except (OSError, ValueError):
            return False
    moved = list((ROOT / "parked").glob(f"_escalated/state-{unit}-*.json")) + list((ROOT / "parked").glob(f"_donation/*{unit}*"))
    return any(p.stat().st_mtime > since for p in moved)


def main():
    d = json.loads(LANES.read_text())
    watched = [l for l in d["lanes"] if l.get("handoff_unit") and l.get("enabled")]
    if len(watched) < 2:
        done(d, None, None)
        return
    for lane in watched:
        if finished(lane["handoff_unit"], float(lane.get("handoff_since", 0))):
            other = next(l for l in watched if l is not lane)
            lane["enabled"] = False
            for l in watched:                 # both get their own tiers back; only the finished one is switched off
                l["tiers"] = l.pop("tiers_saved", l["tiers"])
                for k in ("handoff_unit", "handoff_since"):
                    l.pop(k, None)
            d["_note"] += (f" {time.strftime('%Y-%m-%d %H:%M')} sonnet-handoff: {lane['name']} finished its unit and is "
                           f"disabled; {other['name']} continues as the one Sonnet lane (tiers {other['tiers']}).")
            LANES.write_text(json.dumps(d, indent=1) + "\n")
            done(d, lane, other)
            return


def done(d, lane, other):
    if lane:
        notify.send(f"Zene: {lane['name']} finished its unit and is off; {other['name']} is now the one "
                    f"Sonnet lane (as asked).")
    subprocess.run(["systemctl", "--user", "stop", "zene-sonnet-handoff.timer"], capture_output=True)


if __name__ == "__main__":
    main()
