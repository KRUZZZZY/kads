#!/usr/bin/env python3
"""Scope guard: a mechanical scope-drift check on a bug-fix patch before a reviewer sees it (research/code-factories,
2026-10-25).

Why: reviewers send work back as `out-of-scope` when a fix changes behaviour outside the finding, and every
rejection costs a full rework round. Google CodeMender validates patches for "no regressions, minimal change"
before a human sees them (deepmind.google/blog/introducing-codemender), and SWE-agent/Agentless style pipelines
prefer the smallest patch. Whether a patch wandered is cheap to see from the file list, so a script checks it first.

A changed product file (src/, include/, plugins/) is IN SCOPE when it is
  - a file named by the finding or by a batch companion, or sits in the same directory as one,
  - the header/implementation sibling of one (same stem, e.g. include/Mixer.h for src/core/Mixer.cpp),
  - named in the fixer's report (docs/qwen-fixes-*/cluster-<id>.md), where the fixer justifies it,
  - a CMakeLists.txt.
Everything else is listed as outside the finding's neighbourhood. A patch over `max_product_lines` changed product
lines is flagged as large. Feature units are never checked (they legitimately span many files).

Knob file (absent = off): qwen/SCOPE-GUARD.json
    {"enabled": true, "fail": false, "max_product_lines": 200}
  "fail": false  the pre-submit check shows a WARN (the session is not bounced);
  "fail": true   it shows a FAIL, which sends the work back to the same session once, like the other mechanical checks.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOB = ROOT / "SCOPE-GUARD.json"
DEFAULTS = {"enabled": False, "fail": False, "max_product_lines": 200}
PRODUCT = ("src/", "include/", "plugins/")
PATH_IN_TEXT = re.compile(r"[\w./+-]+\.(?:cpp|h|hpp|cc|c|txt|cmake|py|lua)\b")


def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(KNOB.read_text()))
    except (OSError, ValueError):
        cfg["enabled"] = False
    return cfg


def finding_files(item):
    files = []
    for it in [item or {}] + list((item or {}).get("batch") or []):
        f = str(it.get("resolved_path") or it.get("file") or "").strip()
        if f:
            files.append(f)
    return files


def stem(path):
    return re.sub(r"(Test|Tests)$", "", Path(path).stem)


def out_of_scope(changed, finding, report_text=""):
    named = {Path(p).as_posix() for p in PATH_IN_TEXT.findall(report_text)}
    dirs = {str(Path(f).parent) for f in finding}
    stems = {stem(f) for f in finding}
    out = []
    for f in changed:
        if not f.startswith(PRODUCT):
            continue
        if f in finding or str(Path(f).parent) in dirs or stem(f) in stems or Path(f).name == "CMakeLists.txt":
            continue
        if f in named or any(f.endswith("/" + n) or n.endswith("/" + f) for n in named if "/" in n):
            continue
        out.append(f)
    return out


def product_lines(numstat):
    total = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith(PRODUCT) and parts[0].isdigit() and parts[1].isdigit():
            total += int(parts[0]) + int(parts[1])
    return total


def evaluate(item, changed, numstat, report_text, cfg=None):
    """None (nothing to say or not applicable) or (state, detail) with state WARN or FAIL."""
    cfg = cfg or config()
    finding = finding_files(item)
    if not cfg["enabled"] or not finding:
        return None
    bad = out_of_scope(changed, finding, report_text)
    lines = product_lines(numstat)
    notes = []
    if bad:
        notes.append(f"product files outside the finding's neighbourhood and not named in the report: "
                     f"{', '.join(bad[:5])}{' ...' if len(bad) > 5 else ''} - say in the report why each must change, "
                     "or revert it (reviewers reject unexplained scope growth)")
    if lines > cfg["max_product_lines"]:
        notes.append(f"{lines} product lines changed (limit {cfg['max_product_lines']}): is this one fix, or should part "
                     "of it be a separate issue?")
    if not notes:
        return None
    return ("FAIL" if cfg["fail"] else "WARN", "; ".join(notes))
