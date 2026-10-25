#!/usr/bin/env python3
"""Pre-submit completion check (owner 2026-10-19: "make sure they also have all the tools they need to easily check if
they 'completed' something ... before they submit it have an automated prompt feedback asking if they did a list").

  python3 ~/.local/share/zene-bunny-fix/qwen/submit_check.py <worktree> <queue id or cluster> [--json]

Mechanical items are CHECKED (PASS / FAIL / WARN); judgement items are ASKED (each must be answered, with evidence, in
the report's "Self-check" section). The fix loop runs the same check when a session ends: any FAIL sends the work
straight back to the same session with this list, before a reviewer sees it.

Built from what the Luna verifiers actually sent back (62 feature reworks, 2026-10-19): missing agent command /
stale snapshot (61 %), registration and gates (58 %), GUI path missing or untested (53 %), docs and manual (46 %),
realtime (37 %), undo (29 %), unmet criterion (22 %), tests that pass without the change (20 %).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import feature_check as FC  # noqa: E402

REPORT_DIR = "docs/qwen-fixes-2026-10"
COMMAND_ID = re.compile(r'^\+.*"([a-z][a-z0-9_]*\.[a-z0-9_.]+)"', re.M)
GUI_PATH = re.compile(r"^(src/gui/|include/gui/|plugins/[^/]+/[^/]*(View|Dialog|Widget|Editor|Gui)[^/]*\.(cpp|h)$)")
AUDIO_HINT = re.compile(r"processImpl|playImpl|play\(|renderPeriod|processAudioBuffer|RealtimeScope|NotePlayHandle|"
                        r"AudioEngine::|tap\(|pushPeriod", re.I)


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, errors="replace").stdout


def check(wt, unit, item=None):
    wt = Path(wt).resolve()
    fix_base = (ROOT / "FIX-BASE").read_text().strip()
    base = git(wt, "merge-base", "HEAD", fix_base).strip() or fix_base
    changed = [l for l in git(wt, "diff", "--name-only", f"{base}..HEAD").splitlines() if l]
    diff = git(wt, "diff", f"{base}..HEAD")
    status = git(wt, "status", "--porcelain").strip()
    feature = str(unit).startswith("Q050-")
    batch = [str(b["cluster"]) for b in (item or {}).get("batch") or []]
    units = [str(unit)] + batch
    acceptance = (item or {}).get("acceptance") or []
    res = []      # (state, label, detail)

    def add(state, label, detail=""):
        res.append((state, label, detail))

    add("PASS" if git(wt, "log", "--oneline", f"{base}..HEAD").strip() else "FAIL", "commits on the branch",
        "" if changed else "nothing committed since the base")
    add("PASS" if not status else "FAIL", "working tree clean", "" if not status else status[:200])
    rep = wt / REPORT_DIR / f"cluster-{unit}.md"
    rtext = rep.read_text() if rep.exists() else ""
    words = "IMPLEMENTED|BLOCKED" if feature else "FIXED|ALREADY-FIXED|FALSE-POSITIVE|NEEDS-CONTEXT|BLOCKED"
    add("PASS" if re.search(rf"^\W*(verdict\W*)?({words})\b", "\n".join(rtext.splitlines()[:6]), re.M | re.I) else "FAIL",
        f"report {REPORT_DIR}/cluster-{unit}.md",
        "" if re.search(rf"^\W*(verdict\W*)?({words})\b", "\n".join(rtext.splitlines()[:6]), re.M | re.I)
        else ("the verdict word must be in its first lines" if rtext else "missing"))
    if batch:
        missing = [u for u in units if not re.search(rf"^\W*{re.escape(u)}\W*[:|-]\s*\**({words})\b", rtext, re.M)]
        add("FAIL" if missing else "PASS", "one verdict line per batch unit", ", ".join(missing))
    if re.search(r"self-check", rtext, re.I) is None:
        add("FAIL", "report has a 'Self-check' section answering every ASK item below")
    if feature:
        for u in units:
            doc = f"docs/050-features/{u}.md"
            dtext = (wt / doc).read_text() if (wt / doc).exists() else ""
            add("PASS" if doc in changed and dtext else "FAIL", f"feature record {doc}", "" if dtext else "missing")
            if dtext and acceptance and u == str(unit):
                rows = len(re.findall(r"^\|(?!\s*-)(?!.*criterion)", dtext, re.M))
                add("PASS" if rows >= len(acceptance) else "WARN", "acceptance table covers every criterion",
                    f"{rows} table rows for {len(acceptance)} criteria")
        gui_changed = [f for f in changed if GUI_PATH.search(f)]
        gui_tests = [f for f in changed if f.startswith("tests/src/gui/")]
        add("PASS" if gui_changed else "WARN", "a GUI path for the user-facing action",
            ", ".join(gui_changed[:3]) or "no GUI file changed - every user-facing action needs a GUI entry point")
        add("PASS" if gui_tests else ("FAIL" if gui_changed else "WARN"), "a GUI test drives that GUI path",
            ", ".join(gui_tests[:3]) or "no tests/src/gui test changed")
        new_ids = sorted(set(COMMAND_ID.findall(diff)) - set(re.findall(r'^-.*"([a-z][a-z0-9_]*\.[a-z0-9_.]+)"', diff, re.M)))
        snap = "tools/mcp-zene-control/zene_control/commands_snapshot.json"
        cmd_files = [f for f in changed if re.search(r"ControlCommands|ControlReversibilityTable|ControlRegistry", f)]
        if cmd_files:
            add("PASS" if snap in changed else "FAIL", "commands snapshot regenerated live",
                f"command code changed ({', '.join(cmd_files[:2])}) but {snap} did not" if snap not in changed else "")
            add("PASS" if "README.md" in changed or not new_ids else "WARN", "canary figures (README) updated",
                "new command ids: " + ", ".join(new_ids[:5]) if new_ids else "")
        else:
            add("WARN", "agent command for the user-facing action", "no command file changed - is the action on the agent surface?")
        add("PASS" if "data/manual/index.html" in changed else "WARN", "user manual updated (data/manual/index.html)")
    tests = [f for f in changed if f.startswith("tests/") and re.search(r"\.(cpp|h|py|lua|sh)$", f)]
    claim = (not feature and re.search(r"\b(ALREADY-FIXED|FALSE-POSITIVE)\b", "\n".join(rtext.splitlines()[:6]))
             and not any(f.startswith(("src/", "include/", "plugins/")) for f in changed))
    add("PASS" if tests or claim else "FAIL", "tests added or changed",
        "claim-only closure: independent QA must verify its cited code/contract" if claim else ", ".join(tests[:3]))
    new_test_files = [f for f in git(wt, "diff", "--name-only", "--diff-filter=A", f"{base}..HEAD").splitlines()
                      if f.startswith("tests/src/") and f.endswith(".cpp")]
    if new_test_files:
        cm = (wt / "tests/CMakeLists.txt").read_text()
        unreg = [f for f in new_test_files if Path(f).stem not in cm and Path(f).name not in cm]
        add("WARN" if unreg else "PASS", "new test files registered in tests/CMakeLists.txt",
            ", ".join(unreg) + " (fine only if the folder glob picks them up)" if unreg else "")
    if not feature:         # research/code-factories: scope_guard.py, off unless qwen/SCOPE-GUARD.json enables it
        try:
            import scope_guard
            verdict = scope_guard.evaluate(item, changed, git(wt, "diff", "--numstat", f"{base}..HEAD"), rtext)
            if verdict:
                add(verdict[0], "patch stays inside the finding's scope", verdict[1])
        except Exception as e:      # noqa: BLE001
            add("WARN", "scope guard", repr(e)[:120])
    # gates in one go
    try:
        rc = subprocess.run([sys.executable, str(ROOT / "feature_check.py"), str(wt)], capture_output=True, text=True)
        add("PASS" if rc.returncode == 0 else "FAIL", "gates (feature_check.py)",
            "" if rc.returncode == 0 else " | ".join(l.strip() for l in rc.stdout.splitlines() if l.strip().startswith("FAIL")))
    except Exception as e:
        add("WARN", "gates (feature_check.py)", repr(e)[:120])
    asks = [
        "Each new test FAILS with your product change reverted (reverse the non-test hunks, rebuild, run: exit code?), then PASSES restored.",
        "Every acceptance criterion is proven by a test you ran yourself (name the test and its exit code per criterion).",
        "Each edit is ONE undo step, and every new or changed command has an honest reversibility class.",
        "Nothing you added runs on the audio thread and allocates, frees, locks, waits, does I/O or emits a signal"
        + (" (your diff touches audio-path code - name the functions and say why each is safe)" if AUDIO_HINT.search(diff) else "") + ".",
        "Old projects still load and render exactly as before; new data round-trips through save and load.",
        "Qt 5.15 / MSVC / macOS < 10.15 portability: no Qt-6-only API without a guard, no windows.h macro names, no std::filesystem outside _WIN32.",
    ] if feature else [
        "The regression test FAILS with your product change reverted and PASSES with it (exit codes).",
        "The test is deterministic: no wall-clock timing, sleeps or races (name the seam or injected values it uses).",
        "The fix is at the cause and complete: every caller and sibling of what you changed was checked and stays consistent.",
        "No collateral damage: old projects and saved data still load as before, and any limit or check you added does not "
        "hit other callers that legitimately exceed it.",
        "If your verdict is ALREADY-FIXED or FALSE-POSITIVE: the code (file:line) or the intended contract that shows it.",
    ]
    if not feature:
        # 2026-10-19: several fixes were sent back for changing one command but not its siblings (set_arm vs set_arm_many):
        # list the other commands registered in the command files the diff touched
        snap = wt / "tools/mcp-zene-control/zene_control/commands_snapshot.json"
        try:
            all_ids = [c["id"] for c in json.loads(snap.read_text()).get("commands", [])]
        except (OSError, ValueError, KeyError):
            all_ids = []
        by_group = {}
        for cid in all_ids:
            by_group.setdefault(cid.split(".")[0], []).append(cid)
        siblings = []
        for f in changed:
            if re.search(r"ControlCommands|ControlReversibilityTable", f) and (wt / f).exists():
                text = (wt / f).read_text(errors="replace")
                groups = {g for g in re.findall(r'"([a-z][a-z0-9_]*)\.(?:[a-z0-9_.]*)"', text) if g in by_group}
                for g in groups:
                    if g != "control" and len(by_group[g]) <= 30:     # the generic control.* group is not a sibling set
                        siblings += by_group[g]
        if siblings:
            asks.append("Sibling commands in the command files you changed - confirm each is consistent with your fix "
                        "(or say why it is not affected): " + ", ".join(sorted(set(siblings))[:15]))
    return res, asks


def render(res, asks, unit):
    lines = [f"PRE-SUBMIT CHECK for {unit}"]
    for state, label, detail in res:
        lines.append(f"  {state:4}  {label}" + (f" - {detail}" if detail else ""))
    lines.append("ASK (answer each in the report's 'Self-check' section, with the evidence):")
    lines += [f"  ?     {a}" for a in asks]
    fails = sum(1 for r in res if r[0] == "FAIL")
    lines.append("RESULT: " + ("PASS - submit" if not fails else f"FAIL - {fails} item(s) to fix before submitting"))
    return "\n".join(lines)


if __name__ == "__main__":
    wt, unit = sys.argv[1], sys.argv[2]
    res, asks = check(wt, unit)
    if "--json" in sys.argv:
        print(json.dumps({"checks": res, "asks": asks}))
    else:
        print(render(res, asks, unit))
    sys.exit(1 if any(r[0] == "FAIL" for r in res) else 0)
