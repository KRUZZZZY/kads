The workflow delivers real fixes, but its retry and integration process wastes substantial work. A higher-priced model alone will not fix that process.

This is a read-only audit of local records and controller code on 2026-10-24. No retry policies, queues, active jobs, or acceptance rules were changed. The accompanying evidence.json contains the repeat-lineage snapshot and source run directories.

The sample contains 2,850 run records, 2,792 with recorded session duration. Stripping repeated trailing `-port` suffixes groups them into 1,261 issue lineages: 157 have at least five recorded sessions and 10 have at least ten. This grouping does not deduplicate different reports of the same defect, and does not include every historical manually run lane.

| Underlying issue | Recorded sessions including ports | Recorded session minutes | Provider-reported total tokens available in cache |
|---|---:|---:|---:|
| new-p12-34: template replacement undo | 15 | 548.0 | 137,419,047 across 15 logs |
| new-p8-179: pending port-map cancellation | 14 | 202.2 | 17,526,795 across 9 logs |
| 5039: alternate signal stacks per thread | 13 | 386.0 | 85,246,554 across 13 logs |
| new-p12-221: sample-clip callback allocations | 12 | 818.4 | 161,935,257 across 10 logs |

Times include building, waiting and agent activity; these are summed session durations, not CPU hours. Token totals combine input, output and cache-related counts according to each harness's logging conventions. Coverage is incomplete for some lineages. These are not uncached-token totals, dollar charges, or measured wasted-token totals. For example, the template lineage has 584,798 recorded output/reasoning tokens within its much larger reported total.

The clearest causes are:

1. **Accepted fixes repeatedly re-enter implementation after integration conflicts.** The template fix was accepted at its original round 2. It then went through six successive `-port` generations, reaching 15 sessions total. Several intermediate ports were also accepted. The briefs repeatedly cite a merge conflict with the newer train. This is demonstrable integration churn, although some ports also uncovered genuine rollback/backup defects.
2. **Infrastructure failures are allowed to look like new rounds.** `5440` and `new-p10-5` each reached r11, but each has only four records with actual session duration. Their r5-r11 records are build failures. `3410` reached r10 with only three recorded sessions. The latest build logs show the same Qt `No Q_OBJECT in the class passed to QObject::findChild` errors in accessibility code, unrelated to the reported Wasm/tick/knob bugs. Retrying that base does not improve those fixes.
3. **Missing test infrastructure is discovered too late.** The Wasm reviewer already identified the unavailable Wasmtime regression target in earlier rounds. The job continued through later failed preparations instead of becoming blocked on a specific environment capability.
4. **The same requirements emerge serially.** For sample-clip allocation, reviews successively caught heap fallback under exhaustion, changed-period handling, and exception-runtime allocations. These are meaningful defects, but an agreed saturation/reconfiguration test plan before implementation could have exposed them together. Knob-index reviews likewise progressed from a regression that never reached the bug to platform-specific test-build problems.
5. **No single budget covers the full issue lineage.** Feature policy allows two review rounds per tier, while escalation preserves counters. Mechanical pre-submit/proof bounces increment the counter and feature round base, and port jobs get new identities and fresh budgets. There is no unified progress or usage budget spanning these paths.
6. **Some repair rounds are mostly administrative.** Across the sampled completed records, 81 later sessions committed only documentation or registration manifests, consuming 854.5 recorded minutes. Some registration changes are necessary for correctness; this does not mean every minute is dispensable. It does show an opportunity to prepare manifest/report updates deterministically before invoking another full implementation session.
7. **Repeated verification is expensive.** The fixer is asked to prove RED/GREEN, the automated negative-control stage can repeat it, and a fresh reviewer is asked to reverse/rebuild/test again. Independent review is useful, but exact-commit evidence and results could be reused for unchanged checks. Luna fix and QA invocations are ephemeral; Claude/OpenCode rework sometimes resumes prior sessions. Ephemeral sessions do not prove that provider prompt caches are cold, but they do force the workflow to reconstruct working context and often repeat tools/builds.

The round number therefore mixes implementation attempts, administrative repairs, preparation failures, model changes and repeated integration. It is an unreliable progress metric.

There are significant improvements in the current train. Read-only `git merge-base --is-ancestor` checks confirmed these accepted heads are present in `050/m0-train-15`:

- `new-p12-34-port-port-port-port-port-port`, `0ef7c1f85e242ac7e7f361d72de072bb546e1b28`: template replacement undo/redo and rollback handling; QA records a failing-before/passing-after regression and related project tests.
- `new-p9-149-port`, `db76a22ae3051a4dcb4ce066562e90f758e12c58`: GIG callback locking, pool-capacity and normalization handling; QA records focused regressions.
- `new-p17-202-port`, `7e13e9907e57492ebe58486c02b070952dc9e97e`: Watsyn oscillator parameter handoff; QA records reversal proof and focused checks.

Other accepted heads checked, including the original sample-clip allocation fix, the later LFO lifetime fix, and consent-dialog reporting, are not ancestors of that train head. Accepted does not imply integrated. This audit checked history inclusion and reviewed recorded evidence; it did not independently rebuild/reproduce every fix, measure dropout rates, or establish an overall stability/performance improvement.

The dashboard also counts `CONFIRM` as success, alongside code fixes and `ACCEPT-NOTE`. Its overall acceptance numbers include false-positive/already-fixed closures and multiple ports of one issue. They should not be read as counts of distinct product defects fixed. Current model rates are observational, with different task mixes; the small Opus sample does not establish that switching models alone reduces rounds.

The highest-value workflow changes would be:

1. Keep one stable issue ID and one ledger across model switches and ports. Display implementation attempts, mechanical repairs, infrastructure failures, reviews and integration work separately. Track total lineage usage and merged status.
2. Permit one implementation and one focused correction before a required diagnostic checkpoint. A third attempt must have a changed hypothesis, scoped plan, model responsibility or test capability. Do not merely reset a counter or feed back the same rejection.
3. Fingerprint setup failures. Mark the job blocked on its base/build capability and retry only when that input changes. Do not consume another model round or advance through model tiers for an unchanged broken base.
4. Give accepted fixes an integration owner and a pinned base. Resolve/rebase/cherry-pick conflicts with focused verification where appropriate rather than routinely commissioning a fresh manual implementation and full review. Verify the changed integration diff against the accepted patch and test artifact.
5. Agree on a concrete regression/test matrix before a difficult fix: reachable reproduction, saturation/failure paths, realtime constraints and supported-platform compilation. Ask reviewers for all evidenced blockers together. Additional unrelated issues become separate tasks rather than silently widening the original one.
6. Reuse exact-commit build/test evidence and persistent implementation sessions where supported. Automate report/manifest housekeeping. Retain independent semantic review and rerun checks when code, build inputs or the base invalidate the evidence.
7. Measure distinct integrated issues with demonstrated regressions per unit of recorded usage, plus user-impact measures such as crash reproductions, callback allocations/locks, undo/data-loss scenarios and feature acceptance scenarios. Compare this workflow against the current baseline before claiming a specific percentage saving.

Evidence locations:

- `/home/kruzzzzy/.local/share/zene-bunny-fix/qwen/runs/<issue>.r<N>/record.json`, `brief.md`, `session.log`, and `build.log`.
- `/home/kruzzzzy/.local/share/zene-bunny-fix/qwen/qa/done/*.json` and `qa/reviews/*.md`.
- `/home/kruzzzzy/.local/share/zene-bunny-fix/qwen/qwenfix.py`: preparation failure handling, pre-submit/proof bounces, session commands and QA rework decisions.
- `/home/kruzzzzy/.local/share/zene-bunny-fix/qwen/merge_train.py`: `port_job` and conflict handling.
- `/home/kruzzzzy/.local/share/zene-bunny-fix/qwen/features_queue.py`: per-tier escalation and round base.
- `/home/kruzzzzy/.local/share/zene-bunny-fix/qwen/benchmark.py`: success definitions and token aggregation.
