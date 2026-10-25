# zene-studio-fix-pipeline

The unattended bug-fix and feature pipeline behind [Zene Studio](https://github.com/KRUZZZZY/zene-studio). It takes
bug findings and feature units from a queue and turns them into reviewed branches on an integration line:

1. **Queue and order:** dedup against open bugs, then difficulty and urgency ranking (local Clef model).
2. **Fix:** coding agents (OpenCode/DeepSeek, Claude, GPT Luna via Codex) each work on their own git worktree and
   branch, with targeted builds behind a shared jobserver.
3. **Mechanical checks:** a pre-submit completion check, then a negative control. The loop reverses the product
   change, rebuilds, and requires the new test to fail without the fix and pass with it.
4. **Review:** an independent reviewer session reads the diff, rebuilds, and re-proves the fix. A rejection counts
   only if it is backed by evidence (a command with its exit code, or a file:line). Reviewers may make small fixes,
   which the loop re-checks.
5. **Merge train:** accepted branches are merged in batches onto a train branch, the test suite and gates run, and
   the train is promoted to the new fix base. Conflicts go back to integration jobs.

It is written for one machine and one repository: paths, models, quotas and limits are those of the Zene Studio
workstation. Read it as a working example, not a turnkey tool. API keys are never in this repo; they come from the
environment or from files outside it.

## Layout

| Path | What |
| --- | --- |
| `qwen/` | The dispatcher (`qwenfix.py`: fix and QA loops), merge train (`merge_train.py`), usage monitor and Luna pacing, negative control (`negctl.py`), status digest, tests (`test_*.py`) and the live knob files the loops read (`LUNA-CAP`, `LANES.json`, `*.json`) |
| `qwen/README.md` | Operating notes; start at "Current state" |
| `qwen/WORKFLOW.md` | Workflow rules: one issue ledger across models, budgets, integration requests |
| `bunny/` | Build slot driver, jobserver and the command shims that cap build parallelism |
| `dsh/`, `mcode/` | Harness configs for the DeepSeek and MiniMax Code agent runners |
| `status/` | Live status view (`zene_status.py --text`) |
| `systemd/` | Copies of the user units (`zene-fix`, `zene-qa`, `zene-merge-train`, timers) |

Tests: `cd qwen && for t in test_*.py; do python3 $t; done`. `test_opencode_free.py` and `test_parallel_workers.py`
read live state, so they pass only on a clean checkout.
