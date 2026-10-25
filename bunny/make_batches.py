#!/usr/bin/env python3
"""Generate lane.json, batches.json and per-batch briefs for the Bunny lanes."""
import json, os, fnmatch

ROOT = os.path.dirname(os.path.abspath(__file__))
S = os.path.dirname(ROOT)
PROG = "/home/kruzzzzy/Documents/AI_KOS_PROJECT/projects/zene-studio-program"
BATCH = 6
DATE = "2026-10-05"

RULES = """\
## Binding rules (from the program's AGENTS.md and plans/DELEGATION-RULES.md — these override your defaults)
1. Work ONLY inside `{wt}`. Never modify anything outside it (reading the program docs one level up is fine). Never push, tag, fetch, merge, rebase,
   switch branch, stash or rewrite history (a git guard enforces this; do not try to get around it).
2. Build TARGETED only, from the worktree root: `cmake --build build --target zene <SomeTest> > build/bunny-b.log 2>&1; echo EXIT=$?`
   The build tree is already configured — never run cmake configure, never build without `--target`.
   Builds share 4 machine-wide slots; a "slots busy" wait is normal.
3. Tests: `cd build/tests && ctest -R '^SomeTest$' --output-on-failure > build/bunny-t.log 2>&1; echo EXIT=$?`.
   ctest must run from `build/tests`. **0 tests run is an ERROR, not a pass.** Rebuild the test target AND
   `zene` before believing any failure (stale binaries are the #1 false lead here). Use QT_QPA_PLATFORM=offscreen
   (already set). Never export LMMS_PLUGIN_DIR.
4. Exit codes unpiped: `cmd > log 2>&1; echo EXIT=$?` — never `cmd | tail; echo $?`.
5. NEVER weaken, delete, skip or loosen a test, gate, ratchet, tolerance or assertion to get green.
6. Realtime rule: no allocation, no locking, no unbounded growth on audio-thread paths (process/play/render
   callbacks). If a fix would need that, mark the finding SKIPPED-SCOPE instead.
7. Behaviour-preserving: change only what the defect needs. No refactors, no reformatting, no renames.
8. Do NOT add, remove or rename control commands, and do not change a command's params/result schema
   (that moves the ControlCommandsSnapshot canary — the parent's job). If a fix needs it, SKIPPED-SCOPE.
9. Known-by-design, NEVER "fix": the append-only `<z:provenance>` save journal; undo coalescing and
   `undone_commands`; `mastering.run`'s async ACK; boolean param_set refusing fractional values; the first save
   to a new path reporting `reversible:false`; empty-active tempo-map persistence; fx-id renumbering after
   unload/undo; `id:-1` replies to malformed wire lines; the project requires C++20 (C++17 complaints are false).
10. Known traps: `SampleBuffer` has no operator[] (use data()/size()); `AudioEngine::sampleRate` does not exist
   (baseSampleRate()); Qt6 has no QString::midRef; `Q_ASSERT` compiles to nothing (use QVERIFY in tests);
   unit tests that drive commands need `ControlRegistry::setReady(true)`; tests that play notes need
   `NotePlayHandleManager::init()`; header and source must change together.
11. Prefer adding regression cases to the EXISTING test file for the unit (grep tests/src and
   tests/CMakeLists.txt). Only if no suitable test exists, add a new file: register it in tests/CMakeLists.txt
   (LMMS_TESTS list) AND in tests/fork-sources.txt (one path per line, file must end with a newline).
12. Before each commit run BOTH and require EXIT=0:
   `bash tests/complexity-gate.sh --check > build/bunny-cx.log 2>&1; echo EXIT=$?` (CCN <= 10 per new/changed function —
   split helpers rather than growing a function) and `bash tests/file-length-gate.sh --check > build/bunny-fl.log 2>&1; echo EXIT=$?`.
   Never re-anchor a ratchet.
13. Commit with explicit paths only: `git add <path> <path>` then `git commit -m "..."`. One commit per fixed defect.
   Message: first line `fix(<area>): <what was wrong>`; body: `Luna cluster <N>.`, the regression test name,
   and the red/green evidence (the command + EXIT codes). End the body with the line
   `Co-Authored-By: Space Bunny (OpenCode Zen) <noreply@opencode.ai>`.
14. A finding is a HYPOTHESIS, not a fact. About half of these model-written reports were false when checked by
   hand. Read the current code (line numbers come from an older revision and may have moved) and decide with
   `file:line` evidence before touching anything. A well-argued FALSE-POSITIVE with the source line is a success.
15. Search with fff FIRST: `mcp__fff__grep` (one identifier or plain phrase), `mcp__fff__multi_grep` (several
   spellings at once), `mcp__fff__find_files` (file names). ALWAYS pass `path="{wt}"`, and narrow with
   `constraints` such as `"src/ tests/"` or `"*.cpp *.h"`; page with `cursor` instead of raising maxResults.
   Use it to find the callers, the validation, the header, the test and the registration a finding talks about.
   fff is ranked and fuzzy: "0 exact matches ... approximate" means the literal text is NOT there. To PROVE an
   absence ("nothing validates X", "never registered"), confirm with one bounded ripgrep:
   `rg -n -l 'X' src tests`. Never run unbounded searches: no `grep -r` over the tree, no rg without a path,
   never search build/ or src/3rdparty/. rg output is capped at 120 lines.
"""

PROCEDURE = """\
## Procedure for EACH finding
1. Locate the code in the current tree. Classify: REAL, FALSE-POSITIVE, ALREADY-FIXED or DUPLICATE (of another
   finding in this batch or of a commit already on this branch: `git log --oneline -30`). Cite `file:line`.
2. If REAL: write the regression test FIRST, build it, run it, and confirm it FAILS for the stated reason (RED).
   If you cannot make a deterministic failing test headlessly, do NOT change product code: mark it
   REAL-UNTESTED with your evidence and move on.
3. Make the smallest correct fix. Rebuild the test target and `zene`, rerun the regression (GREEN) and the
   rest of that test executable's cases, plus any other test that covers the changed file
   (`grep -rl <FileStem> tests/CMakeLists.txt tests/src | head`).
4. Run the two gates (rule 12), then commit (rule 13).
5. If the fix is out of scope (rules 6, 8, platform-only code, a design change, or more than ~150 changed
   lines), mark SKIPPED-SCOPE with one sentence of why and leave the code alone.
Spend at most ~15 minutes per finding; when stuck, record what you know and move on.
"""

REPORT = """\
## Report (required, even if nothing was fixed)
Write `docs/bunny-fixes-{date}/{lane}/{tag}.md` with one section per finding:
`### Cluster <N> — <title>` then `Verdict:` (FIXED | FALSE-POSITIVE | ALREADY-FIXED | DUPLICATE | REAL-UNTESTED |
SKIPPED-SCOPE), `Evidence:` (file:line and one or two sentences), `Commit:` (short sha or none),
`Test:` (test::case or none), `Red/green:` (the exact commands and EXIT codes, or none).
End with a `Leftovers:` line naming anything half-done. Then `git add` that one report file and commit it
with message `docs(bunny): {lane} {tag} report`. Leave the tree clean (`git status --short` prints nothing).
Your final message: at most 10 lines — the verdict counts and the commit shas.
"""


SCOPES = {
    "theory": "session tuning / Scala / MTS, chord track and progressions, grooves, scales, project markers.",
    "midi": "SMF interchange reader/writer, MIDI controllers, MIDI input transform, MIDI drivers and reconnect.",
    "notes": "note.* commands and their helpers, comp groups, range commands, stem render/support.",
    "docmodel": "DataFile, DocumentIndex, provenance section, automation clips/transforms/commands, track folders.",
    "presets": "chain presets (atomic chain replacement on apply/replace), export/render preset overwrite and delete file safety.",
    "clips": "clip.link_* anchor validation, gain-envelope point validation, truthful undo for clip edits.",
    "import": "DAWproject import: mixer snapshots in import undo, undo byte accounting, typed refusal preservation.",
    "audio": "the reproduced ArrangingCatchUpTest audio-worker crash (InstrumentPlayHandle lifetime / queued handle ownership).",
    "cmds1": "control commands ControlCommands[A-M]*.cpp.",
    "cmds2": "control commands ControlCommands[N-Z]*.cpp.",
    "control": "control-surface infrastructure (registry, schema, support, reversibility tables, server).",
    "core": "core model code not covered by another lane.",
    "engine": "the audio engine (Mixer, Song, Track, AudioEngine, drivers, play handles); realtime rule absolute.",
    "gui": "the Qt GUI (offscreen tests, tests/src/gui/GuiTestApplication.h).",
    "plugins": "plugin hosts and plugins (ClapEffect, Vst3Effect, others).",
    "script": "Lua scripting, WASM sandboxes, stem separation.",
}


def finding_block(x):
    return (f"### Cluster {x['cluster']} — {x['title']}\n"
            f"(file `{x['file']}` around line {x['line']} at revision {x['rev']}; "
            f"{len(x['members'])} repeat report(s); Clef realness score {x['score']}/4)\n\n"
            f"```\n{x['text'].strip()}\n```\n")


def header(lane, wt, branch, scope):
    return (f"# Space Bunny fixer lane `{lane}` — Zene Studio (LMMS-derived DAW, C++20 / Qt6)\n\n"
            f"Worktree `{wt}`, branch `{branch}`, build tree `{wt}/build`.\n"
            f"Lane scope: {scope}\n\n")


def write_lane(lane, wt, branch, scope, items, preamble_batches=()):
    d = os.path.join(ROOT, "lanes", lane)
    os.makedirs(os.path.join(d, "batches"), exist_ok=True)
    json.dump({"worktree": wt, "branch": branch}, open(os.path.join(d, "lane.json"), "w"), indent=1)
    batches = []
    for tag, body in preamble_batches:
        txt = header(lane, wt, branch, scope) + RULES.format(wt=wt) + "\n" + body + "\n" + \
            REPORT.format(date=DATE, lane=lane, tag=tag)
        open(os.path.join(d, "batches", f"{tag}.md"), "w").write(txt)
        batches.append({"tag": tag, "clusters": []})
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        tag = f"batch-{i // BATCH + 1:02d}"
        txt = header(lane, wt, branch, scope) + RULES.format(wt=wt) + "\n" + PROCEDURE + \
            f"\n## The {len(chunk)} findings for this batch\n\n" + "\n".join(finding_block(x) for x in chunk) + \
            "\n" + REPORT.format(date=DATE, lane=lane, tag=tag)
        open(os.path.join(d, "batches", f"{tag}.md"), "w").write(txt)
        batches.append({"tag": tag, "clusters": [x["cluster"] for x in chunk]})
    json.dump(batches, open(os.path.join(d, "batches.json"), "w"), indent=1)
    return len(batches)


def main():
    q = json.load(open(os.path.join(S, "queue.json")))
    lanes = json.load(open(os.path.join(S, "lanes.json")))
    scopes = {
        "theory": "session tuning / Scala / MTS, chord track and progressions, grooves, scales, project markers.",
        "midi": "SMF interchange reader/writer, MIDI controllers, MIDI input transform, MIDI drivers and reconnect.",
        "notes": "note.* commands and their helpers, comp groups, range commands, stem render/support.",
        "docmodel": "DataFile, DocumentIndex, provenance section, automation clips/transforms/commands, track folders.",
    }
    for lane, items in lanes.items():
        n = write_lane(lane, f"{PROG}/zene-bunny-{lane}", f"bugs/bunny-{lane}-{DATE}", scopes[lane], items)
        print(lane, len(items), "findings,", n, "batches")

    take = {
        "presets": (["*ControlChainPreset*", "*ControlCommandsChain*", "*ControlCommandsExportPresets*", "*ChainPreset*"],
                    "chain presets (atomic chain replacement on apply/replace), export/render preset overwrite and delete file safety.",
                    "docs/luna-presets-2026-10-05-logs/HANDOFF.md"),
        "clips": (["*ClipLink*", "*ClipEnvelope*", "*ClipGainEnvelope*", "*ControlCommandsClipTranspose*"],
                  "clip.link_* anchor validation, gain-envelope point validation, truthful undo for clip edits.", None),
        "import": (["*DawProject*"],
                   "DAWproject import: mixer snapshots in import undo, undo byte accounting, typed refusal preservation.", None),
        "audio": (["*InstrumentPlayHandle*", "*ArrangingCatchUp*", "*PlayHandle*"],
                  "the reproduced ArrangingCatchUpTest audio-worker crash (InstrumentPlayHandle lifetime / queued handle ownership).",
                  "docs/luna-audio-2026-10-05-logs/HANDOFF.md"),
    }
    for lane, (pats, scope, handoff) in take.items():
        wt = f"{PROG}/zene-fix-{lane}"
        branch = f"bugs/luna-{lane}-{DATE}"
        items = [x for x in q if any(fnmatch.fnmatch(x["file"], p) for p in pats)]
        items.sort(key=lambda x: (x["file"], x["line"]))
        hand = f"`{handoff}` (read it first)" if handoff else "the lane's committed WIP (`git log -3 --stat`, `git show HEAD`)"
        body = (
            "## Takeover task (this session)\n"
            f"This lane was started by a GPT 6.0 Luna fixer and is now yours. Its brief: {scope}\n"
            f"Start from {hand} and the campaign table in "
            f"`{PROG}/research/reviews/luna-fix-campaign-2026-10-05/STATUS.md` (read-only for you).\n"
            "The last commit is a WIP that preserved the predecessor's UNVERIFIED in-progress edits (tests and maybe a "
            "partial fix). Treat every part of it as a hypothesis:\n"
            "1. Build `zene` and the lane's test targets; run them; record which new cases fail and WHY.\n"
            "2. For each intended defect: confirm the red test fails for the stated reason; if a test is wrong or "
            "asserts something the product never promised, correct the TEST's premise only with a cited source line "
            "(never to make a real failure disappear). Then finish the minimal fix and get it green.\n"
            "3. Gates (rule 12), then commit each completed repair separately (rule 13); a commit may also replace or "
            "complete parts of the WIP. If something cannot be finished, leave it described in the report.\n"
        )
        if lane == "audio":
            body += ("\nAudio-specific: this is engine/audio-thread code. The realtime rule (6) is absolute. Reproduce first: "
                     "`cd build/tests && ctest -R '^ArrangingCatchUpTest$' --output-on-failure` (the parent measured a crash, "
                     "exit 8, in ~7 s; the trace pointed into InstrumentPlayHandle::play). The WIP regression reportedly failed "
                     "to COMPILE with a const-pointer error — fix the test's compile error first. Find the root cause "
                     "(who frees what, on which thread) before changing code; a precise, evidenced diagnosis with no code "
                     "change is an acceptable outcome. Run `ArrangingCatchUpTest` at least 5 times after any fix.\n")
        n = write_lane(lane, wt, branch, scope, items, preamble_batches=[("takeover", body)])
        print(lane, len(items), "findings,", n, "batches (incl. takeover)")


if __name__ == "__main__":
    main()
