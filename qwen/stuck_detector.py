#!/usr/bin/env python3
"""Stuck-session detector for fix/review sessions (research/code-factories, 2026-10-25).

Idea from the OpenHands "Stuck Detector" (MIT; docs.openhands.dev/sdk/guides/agent-stuck-detector), which flags an
agent that repeats the same action and observation, repeats the same error, or ping-pongs between two actions.
OpenHands reads structured events. Our harnesses (codex, OpenCode, Claude Code, MiniMax Code) each write their own
transcript to session.log, so this is a clean-room version that works on normalised log lines instead.

Why: run_session() only stops a session after 45 minutes of *silence*. A session that loops loudly (the same failing
command again and again) keeps the log changing, so it is never stopped and burns usage for hours.

Knob file (absent = off): qwen/STUCK-DETECTOR.json
    {"enabled": true, "action": "log"}          action "log" only writes a line; "stop" ends the session like the
                                                silence watchdog does (exit code 124, counts as a normal attempt)
  optional keys: window_lines, min_line_len, repeat_single, repeat_cycle, max_period, repeat_error, check_every_s

  python3 stuck_detector.py <session.log> [...]    prints the verdict for each log (read-only)
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOB = ROOT / "STUCK-DETECTOR.json"
DEFAULTS = {"enabled": False, "action": "log", "window_lines": 600, "min_line_len": 14, "repeat_single": 12,
            "repeat_cycle": 6, "max_period": 6, "repeat_error": 8, "check_every_s": 120, "tail_bytes": 262144}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STAMP = re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?Z?\b|\b\d\d:\d\d:\d\d(\.\d+)?\b|\[\s*\d+(\.\d+)?s\]")
HEX = re.compile(r"\b[0-9a-f]{7,40}\b")
# Tool output that reports a failure. Prose, markdown tables, diff lines and quoted commands also contain the word
# "error", and the finished report is echoed several times at the end of a log, so only tool-style lines count.
ERROR = re.compile(r"\b(?:fatal error|fatal|error|failed):|traceback \(most recent|segmentation fault|command not found|"
                   r"no such file or directory|permission denied|exit(?:ed)? (?:code|status) [1-9]|\brc[=:] ?[1-9]", re.I)
NOT_TOOL_OUTPUT = re.compile(r"^[+\-|>#*]|`")


def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(KNOB.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def normalise(line):
    line = STAMP.sub("", HEX.sub("#", ANSI.sub("", line)))
    return re.sub(r"\s+", " ", line).strip()[:240]


def meaningful(lines, min_len):
    return [n for n in (normalise(l) for l in lines) if len(n) >= min_len]


def detect(lines, cfg=None):
    """Return None or {"kind", "detail", "sample"} for a list of raw log lines (the tail of a session log)."""
    cfg = {**DEFAULTS, **(cfg or {})}
    seq = meaningful(lines[-cfg["window_lines"]:], cfg["min_line_len"])
    if len(seq) < cfg["repeat_cycle"]:
        return None
    # a block of 1..max_period lines that ends the window and repeats back to back
    for period in range(1, cfg["max_period"] + 1):
        need = cfg["repeat_single"] if period == 1 else cfg["repeat_cycle"]
        block = seq[-period:]
        if period > 1 and len(set(block)) < 2:
            continue                       # a one-line loop is the period-1 case
        reps, end = 1, len(seq) - period
        while end - period >= 0 and seq[end - period:end] == block:
            reps, end = reps + 1, end - period
        if reps >= need:
            kind = "repeat" if period == 1 else "cycle"
            return {"kind": kind, "detail": f"{reps} identical repeats of a {period}-line block", "sample": block[:3]}
    # the same error text again and again, with other lines in between (a retry loop)
    counts = {}
    for n in seq:
        if ERROR.search(n) and not NOT_TOOL_OUTPUT.search(n):
            counts[n] = counts.get(n, 0) + 1
    if counts:
        worst = max(counts, key=counts.get)
        if counts[worst] >= cfg["repeat_error"]:
            return {"kind": "error-loop", "detail": f"the same error line {counts[worst]} times in the last "
                    f"{len(seq)} lines", "sample": [worst]}
    return None


def tail_lines(path, nbytes):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    return lines[1:] if size > nbytes and lines else lines      # the first line may be cut in half


def check_log(path, cfg=None):
    cfg = {**DEFAULTS, **(cfg or {})}
    try:
        return detect(tail_lines(path, cfg["tail_bytes"]), cfg)
    except OSError:
        return None


class Watch:
    """Polled from a session's wait loop. Costs one config read per call and one log tail per check_every_s;
    does nothing at all while the knob file is absent or disabled."""

    def __init__(self, log_path, clock=None):
        import time
        self.path, self.clock = log_path, clock or time.time
        self.last, self.reported = 0.0, False
        self.action = "log"

    def poll(self):
        """None, or a verdict dict (once per session) when the session looks stuck."""
        cfg = config()
        if not cfg["enabled"] or self.reported:
            return None
        now = self.clock()
        if now - self.last < cfg["check_every_s"]:
            return None
        self.last = now
        verdict = check_log(self.path, cfg)
        if verdict:
            self.reported, self.action = True, cfg["action"]
        return verdict


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, json.dumps(check_log(p, {**DEFAULTS, **config()})))
