> **Superseded 2026-10-07 17:00 by `zene-studio-program/plans/PLAN-050-MASTER.md`** (rolling merges instead of waiting for the queue to drain; Luna capped at 20 sessions: fix 12, review 5, other 3 in `qwen/LUNA-CAP`).

# What happens next (owner's plan, 2026-10-07)

1. **Drain the bug queue.** Luna fixes 06:00-24:00 (4 parallel), Qwen on Strata 00:00-06:00. Luna QA reviews
   every fix (3 parallel). Done when `qwen/FIX-DRAINED` exists (queue empty, QA caught up).
   At 2026-10-07 09:20: 938 queued, 886 untouched, 40 accepted, 2 parked. Estimate: 4-5 days.
2. **Feature lanes, one at a time** (to leave RAM and build slots for the bug fixes): plugin-hosting is running;
   then resume launcher-rec, audio-clips, arranger-gaps in that order (see `feature-lanes/PAUSED.md`). Each lane
   is verified by the parent (commits, rebuild, its tests, both gates) before the next starts.
3. **Merge**, once the queue has drained and the paused lanes are done:
   - an integration branch off `0.5.0/ui-train` (or `050/features-2026-10-06`);
   - merge every verified feature lane (2026-10-06: ui-gaps, f2-ui, mc-wav, wf033, meters, editor-lanes,
     inst-pdc; 2026-10-07: plugin-hosting and the three paused lanes) and every branch in
     `qwen/ACCEPTED.tsv` with an ACCEPT / ACCEPT-NOTE verdict (CONFIRM branches carry only a report);
   - regenerate `ControlCommandsSnapshot` once from a live instance of the merged build;
   - the full `ctest` from `build/tests` and `env -u LMMS_PLUGIN_DIR bash tests/run-all-gates.sh`, alone on the
     machine (never two suites at once; never hard-stop gate 5). Never push.
4. **Restart the Luna sweep on the merged tip**: `rm research/reviews/luna-patrol/STOP`, then
   `setsid nohup python3 tools/luna-patrol/patrol.py --repo <merged worktree> --workers 3 >> research/reviews/luna-patrol/patrol.log 2>&1 < /dev/null &`
   and the Clef triage watcher
   `setsid nohup python3 tools/luna-patrol/triage_new.py --watch >> research/reviews/luna-new-triage/watch.log 2>&1 < /dev/null &`.
   The prompt already has the C++20 fix, the schema note and the per-bug difficulty rating.
   Each pass also reads the unchanged upstream LMMS code: about 93 slices instead of 76. Add `--no-upstream` for the old fork-only sweep.
