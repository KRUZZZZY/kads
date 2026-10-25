# zene-studio-fix-pipeline

**An unattended find → fix → review → merge pipeline for coding agents, built and run by one developer.**

This is the system that keeps [Zene Studio](https://github.com/KRUZZZZY/zene-studio) (a C++/Qt DAW) moving with one
person at the wheel. Agents find bugs, other agents fix them on their own branches, scripts check the work
mechanically, an independent reviewer re-proves it, and accepted branches land together on a merge train that has to
build and pass the tests before it becomes the new base. A human sees a job only when every model on the escalation
ladder has failed it.

It is published so other solo developers can take the parts that work for them. You do not need a DAW, a C++
codebase or the same models. The [blueprint](docs/BLUEPRINT.md) describes every stage in a vendor-neutral way, with
the exact algorithms, thresholds and prompt texts, plus a section on adapting it to a web/SaaS codebase and a
Claude-only setup.

## Why you might want it

Coding agents have become cheap enough to run all day. Getting work you can trust out of them is the hard part. This pipeline is
tuned around a few rules, most of which came from a failure:

- **Finders never fix, fixers never review their own work, reviewers are independent.** Each role is a fresh
  session with its own brief. A built-in rule keeps Haiku from reviewing Haiku, and an optional knob extends that
  to any same-family pair.
- **Mechanical checks before judgement.** Before any model reviews a fix, a script checks that the commits exist,
  that tests changed and that the gates pass. It then runs a **negative control**: it reverts the product change and
  requires the new test to *fail* without the fix and pass with it.
- **Rejections need evidence:** a `file:line` or a command with its exit code. "Might" and style comments are notes,
  not blockers.
- **Infrastructure failures are not attempts.** An outage or usage limit rests that model and requeues the job. It
  never burns a round.
- **Escalate instead of parking.** Failed work climbs a ladder of stronger models.
- **Never weaken a check.** Skipping, deleting or loosening a test, gate or baseline is a rejection category of its
  own.
- **Integration is its own test.** Changes reviewed alone still break each other. A batch becomes the base only
  after a merge train builds and tests all of it together.
- **Everything is journalled and live-configurable.** Decisions go to JSONL ledgers, and every routing knob is a
  small file that is read fresh on each loop.

## The stages

| Stage | What happens | Where |
|---|---|---|
| 1. Find | A read-only agent sweeps the code in slices, rotating "angles" (threads, lifetimes, edge cases, UI state, ...) | `patrol/patrol.py`, `patrol/prompt.txt` |
| 2. Triage | Dedup with embeddings plus a small classifier, realness (real / false positive / already fixed / needs context), difficulty and urgency ranking, root-cause families | `patrol/dedup.py`, `classify.py`, `bug_difficulty_*.py`, `bug_urgency_*.py`, `root_causes.py` |
| 3. Queue and route | Order the queue and pick a fixer per job by difficulty and measured per-model acceptance | `qwen/qwenfix.py`, `qwen/benchmark.py`, `qwen/adaptive.py` |
| 4. Fix | Reproduce, write a failing test, fix, prove, report, each job in its own git worktree | `qwen/qwenfix.py`, harness configs in `dsh/`, `mcode/` |
| 5. Mechanical checks | Pre-submit completion check, scope guard, negative control | `qwen/submit_check.py`, `qwen/scope_guard.py`, `qwen/negctl.py` |
| 6. Review | Independent reviewer, evidence-only rejections, optional reviewer-diversity rule | `qwen/qwenfix.py` (QA loop), `qwen/review_diversity.py` |
| 7. Rework and escalation | Same session up to N rounds, then the next model up the ladder | `qwen/qwenfix.py`, `qwen/stuck_detector.py` |
| 8. Integration | Merge trains with a known-failures baseline, confirm-by-reversal, conflict forecast, and a "train doctor" agent that diagnoses repeat failures | `qwen/merge_train.py`, `qwen/conflict_forecast.py`, `qwen/train_doctor.py` |

Supporting pieces: usage monitoring and pacing across subscriptions (`qwen/usage_monitor.py`, `usage_guard.py`,
`luna_pace.py`), autoscaling lanes (`qwen/autoscale.py`), a daily digest (`qwen/digest.py`), a build jobserver that
stops parallel agents from exhausting RAM (`bunny/`), and systemd units for all of it (`systemd/`).

## How to use it

Be clear about what this is: **a working system lifted from one machine, not a packaged tool.** The code has
Zene's paths, model lanes, build commands and gates wired in. There are three ways to take from it:

1. **Take the design.** Read [docs/BLUEPRINT.md](docs/BLUEPRINT.md). Section 19.1 is a minimum viable version you
   can build in a week or two on your own stack: a CSV queue, one fixer in a sandboxed worktree, the pre-submit check
   and negative control, one reviewer from a different model tier, a rework loop and your existing CI as the
   integration step. Appendix A has the prompts verbatim; Appendix B has every threshold.
2. **Lift single modules.** The smaller modules are close to standalone and short enough to adapt in an afternoon:
   `negctl.py` (~130 lines), `submit_check.py`, `scope_guard.py`, `stuck_detector.py`, `review_diversity.py`,
   `conflict_forecast.py`. Each says in its docstring what it does and what it assumes. Swap the build/test
   commands and path conventions for yours.
3. **Run the whole thing.** Possible, but you will be editing paths and lanes in `qwen/qwenfix.py`,
   `qwen/LANES.json` and the knob files, and replacing the C++ build and gate commands. `qwen/README.md` and
   `qwen/WORKFLOW.md` are the operating notes.

Section 12 of the blueprint maps every Zene-specific concept onto a web/SaaS equivalent. It covers what to check,
what to sandbox and what to use as the integration branch. **Never give these bots production access or production
data.** Each job needs its own sandbox, with push blocked and only scoped tokens.

## Layout

| Path | What |
| --- | --- |
| `docs/BLUEPRINT.md` | The full design: roles, stages, algorithms, thresholds, prompts, failure modes, build order |
| `patrol/` | Stages 1–2: the finder sweep and the triage tools (dedup, classify, difficulty, urgency, root causes) |
| `qwen/` | Stages 3–8: dispatcher and fix/QA loops (`qwenfix.py`), mechanical checks, merge train, train doctor, usage and autoscaling, tests (`test_*.py`), and the live knob files the loops read |
| `bunny/` | Build slot driver, jobserver and command shims that cap build parallelism |
| `dsh/`, `mcode/` | Harness configs for the DeepSeek and MiniMax Code agent runners |
| `status/` | Live status view (`zene_status.py --text`) |
| `systemd/` | The user units and timers that run the loops |

Tests: `cd qwen && for t in test_*.py; do python3 $t; done`. `test_opencode_free.py` and `test_parallel_workers.py`
read live state, so they pass only on a configured machine.

API keys and credentials are never in this repository. They come from the environment or from files outside it.

## Credits and licence

Several modules are clean-room ports of ideas from OpenHands, Uber SubmitQueue, Zuul, Shopify, Google CodeMender,
SWE-agent and others. See [qwen/vendor/NOTICE.md](qwen/vendor/NOTICE.md). No third-party code is copied in.

MIT licensed, see [LICENSE](LICENSE). Issues and write-ups of your own adaptations are welcome.
