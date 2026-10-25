The fix workflow now follows one stable issue ID across model changes and repeated ports, including numbered manual port aliases. The implementation was authorized on 2026-10-24 after the methodology audit in audits/methodology-2026-10-24/findings.md.

Current policy is in WORKFLOW.json. Defaults are:

- One implementation and one correction before a read-only diagnostic checkpoint. Mechanical repairs (submit-check / negative-control bounces of the same submission) spend the shared session budget but are not the correction. The checkpoint must supply a changed cause/approach and concrete test scenarios; repeating the same plan cannot reset it.
- At most eight new work sessions per issue across implementations, mechanical repairs, integrations and diagnostics. At most two diagnostic sessions and two focused integration sessions. Historical work is retained; sessions already in flight during deployment are grandfathered. Reviews are recorded separately and do not prevent an already-produced patch from receiving its independent review.
- Unchanged preparation failures are blocked on their actual checkout, configuration and dependency stamp. A newer train alone does not retry an unchanged old checkout.
- An accepted patch that conflicts with the train becomes one integration request, not another -port issue. New conflicts wait for promotion and are pinned to the validated cumulative train. Subsequent trains wait while a runnable integration cohort is pending, correcting or in review — each member for at most 24 h (COHORT_MAX_WAIT_H), and a feature integration only while an enabled Claude sonnet/opus lane and FEATURES-ON exist; members that stop holding the train stay queued and the owner is told once; a train already in progress during rollout finishes its existing validation normally. The accepted patch is applied with a three-way merge in a separate owned integration worktree. Previously reverted patches use their original patch base. Accepted batches and their companions are retained.
- Current integration corrections with commits are preserved. The one manual feature port lacking a recorded item was recovered from its original queue specification. Four untouched legacy port requests were migrated; originals are backed up under workflow-migration/.
- Every member of a batch gets its own issue ledger, shared session identity and apportioned session time. Canonical issue locks prevent aliases or batch members from being worked concurrently.
- Luna implementation sessions persist and resume on rework when a session ID is available. Independent QA sessions remain independent.
- Factual source registries are generated from the tree's own tracked-file recipe, with vendor exclusions preserved. The driver commits only those mechanical changes. Reports, test evidence and judgement answers are never fabricated.
- Claims of ALREADY-FIXED/FALSE-POSITIVE with no product changes do not require a gratuitous new regression merely to submit the claim. Independent QA still verifies the cited code/contract.
- Negative-control reuse requires matching base tree, source/test/build content, compiler/package inputs, prover version and intact hashed test logs. Documentation/manifest-only corrections can reuse valid proof. Semantic QA and affected tests remain required; invalidated proof is rerun.

The source modules are issue_ledger.py, workflow_runtime.py, integration_jobs.py, workflow_evidence.py, workflow_report.py and migrate_workflow.py. Their entry points are connected to qwenfix.py, merge_train.py, benchmark.py and the status monitor. The Claude wrapper additionally permits only the existing build-pool coordination files needed by its sandboxed compiler launcher.

The dashboard and WORKFLOW-STATUS.json / WORKFLOW-STATUS.md separate integrated source fixes, test improvements, features and claim closures. WORKFLOW-BASELINE.json records the rollout baseline; the report also shows newly integrated issue IDs, work/review session counts and recorded minutes since rollout. Token coverage is partial and includes input/cache counts. Ancestry and semantic-exclusion checks establish integration history; they are not fresh runtime verification or proof of an overall crash/dropout improvement.

Validation completed before releasing the deployment hold:

    python3 test_workflow.py                  # 37 checks
    python3 test_model_switch.py              # 10 checks
    python3 ../bunny/test_build_limits.py     # 8 checks

All 55 checks passed. They use fake model calls and temporary repositories, including real three-way conflicts, a reverted accepted patch, canonical/batch locks, failed build gating, exact-content proof reuse, manifest generation, model routing, recursive Make and a real shared-pool build inside the Claude write jail. Python compilation checks also passed. No claim of a particular usage saving is made before the new workflow accumulates measured results.

The controllers were reloaded after preserving and draining their existing sessions. The merge-train timer is restored. The bug sweep and follow-up triggers remain masked; PATROL-DISABLED remains in force.

Pre-change source files are preserved under workflow-backup-20261024-173106/. Queue migration backups are separate. New state is under issues/, diagnostics/, integrations/, integration-pending/ and evidence/. No accepted branch or user worktree was deleted or rewritten during deployment.

Review fixes (2026-10-24, evening; backups `*.bak-pre-reviewfix-2026-10-24`):

- A usage-limit pause (LunaLimit / FreeModelLimit, including the Claude limit) no longer marks the issue `blocked-build`; it was blocking issues until their build inputs changed (1117 and new-p23-186 were released).
- A failed build on an integration job requeues it in its own `integrations/` file instead of adding a duplicate `rework/` copy.
- An issue that becomes held (blocked or budget-exhausted) is logged in `issues/held.log`; the daily digest lists new ones (no per-issue Telegram messages).
- Registry housekeeping that fails is recorded (`housekeeping_error`) instead of parking the session's work.
- An integration's files outside the accepted patch are recorded (`integration_outside_patch`) and named in the QA brief.
- A usage-limited review is recorded as cancelled, so it is not counted.
- `merge_train.py` re-derives `tests/all-sources.txt` from the header's own "Regenerate with" block (the hard-coded copy lacked `plugins/SfzPlayer/sfizz/`, so train-16's manifest failed `tests/all-sources-reproduce.sh`); a request that cannot be pinned on promotion stays pending and is reported instead of aborting the promotion; blank lines in `plans/queue-050.jsonl` are skipped.

Validation: `test_workflow.py` 46 checks (9 new), `test_model_switch.py` 10, `../bunny/test_build_limits.py` 8 — all pass.

Autopilot (2026-10-24; owner is mostly away from the PC): `autopilot.py`, hourly via `zene-autopilot.timer`, log
`logs/autopilot.log`, off switch `touch AUTOPILOT-OFF`, settings `WORKFLOW.json` "autopilot". It never deletes a
worktree/branch, rewrites commits or runs a model itself; recovered issues go back through the normal budgets.

- blocked-build on an owned fix tree (`zene-qwen-*`, `zene-feat-*`) with no commits of its own: fast-forwarded to the
  newest train so the dispatcher retries it; at most 2 times per issue. Trees with their own commits are left alone.
- blocked-diagnostic because the diagnostic failed or gave no plan: re-run once after 6 h. A real "blocked"/"split"
  verdict is kept.
- merge train OOM-killed: `merge/BAR-JOBS` halved (8 → 4 → 2; `merge_train.py` reads it for the full ctest bar).
- Telegram only for pipeline-level problems: fix/QA loop down on two hourly runs, no train for 72 h (once a day),
  OOM again at -j2. Everything else is in the 09:00 digest; on Mondays it adds a scorecard (week and since rollout:
  issues integrated, work sessions per issue, trains, held).

Tests: `test_autopilot.py` (7 checks).
