# KADS blueprint: an agent delegation surface for find → fix → review → merge

_A reproducible blueprint for delegating a codebase's bug and feature work to coding-agent harnesses._

Written 2026-10-21 from the code that runs the Zene Studio loops today. It is meant to be rebuilt elsewhere. Every
stage gives its purpose, inputs and outputs, the exact algorithm and thresholds, the prompt text (verbatim, in the
appendix), how it fails, and how it is verified.

The pipeline is a control plane, not an agent. It defines **roles** (finder, triager, router, fixer, checker,
reviewer, integrator), hands each role to whichever **harness and model** you configure (Claude Code, Codex,
OpenCode, a local model, ...), and owns everything between them: queue, routing, budgets, escalation, mechanical
checks, review rules and integration.

The numbers and examples come from the reference deployment, Zene Studio: a C++/Qt desktop app, one operator, a mix
of model vendors. Section 12 says what to change for a different product and for a Claude-only setup with your own
small classifier model.

**Names from the reference deployment.** *Zene* / *LMMS* = the target repository and its upstream. *Luna* = GPT 6.0
Luna through the Codex CLI (the strong lane). *Clef* = a small local classifier model. *Strata*, *Qwen* = local
fallback models. *the owner* = the human operator. `qwen/` = the dispatcher's directory (a historical name).

In this repository, paths starting `qwen/` are the loop code (`qwen/` here) and `tools/luna-patrol/` is `patrol/`.
Other paths are relative to the Zene program repository, which is not part of this release.

---

## Contents

1. [Design principles](#1-design-principles)
2. [Roles, independent of model](#2-roles-independent-of-model)
3. [Architecture, processes and files](#3-architecture-processes-and-files)
4. [Stage 1: finding bugs](#4-stage-1-finding-bugs)
5. [Stage 2: triage (dedup, realness, difficulty, root causes)](#5-stage-2-triage)
6. [Stage 3: queue and routing](#6-stage-3-queue-and-routing)
7. [Stage 4: the fix session](#7-stage-4-the-fix-session)
8. [Stage 5: mechanical checks before review](#8-stage-5-mechanical-checks-before-review)
9. [Stage 6: review](#9-stage-6-review)
10. [Stage 7: rework and escalation](#10-stage-7-rework-and-escalation)
11. [Stage 8: integration (merge trains) and the train doctor](#11-stage-8-integration)
12. [Adapting it: a SaaS codebase, Claude models, your classifier](#12-adapting-it)
13. [Unit state machine](#13-unit-state-machine)
14. [Data formats](#14-data-formats)
15. [Verification: what each layer catches and misses](#15-verification)
16. [Metrics and observability](#16-metrics-and-observability)
17. [Operations](#17-operations)
18. [Failure modes seen in production, and their fixes](#18-failure-modes-seen-in-production)
19. [Build order and a minimum viable version](#19-build-order)
- [Appendix A: prompt templates (verbatim)](#appendix-a-prompt-templates)
- [Appendix B: every threshold and constant](#appendix-b-thresholds-and-constants)

---

## 1. Design principles

These are the rules the system was tuned towards. Most of them came from a failure.

1. **Finders never fix; fixers never review their own work; reviewers are independent.** Each role runs in a fresh
   context with its own brief. A reviewer is told it did not write the code and must "verify, do not trust".
2. **Mechanical checks before judgement.** Anything a script can decide is decided before a model reviewer sees the
   work: commits exist, tests changed, gates pass, and the new test fails without the fix. This saves reviewer
   time and removes a whole class of argument.
3. **Rejections need evidence.** Work goes back only for a concrete failure in a fixed list of categories, shown by
   a `file:line` or a command with its exit code. "Might", style and report wording are notes, never blockers. A
   rejection without evidence is asked once more, then becomes an accept with notes.
4. **Uncertainty never discards.** A classifier's "unsure" becomes "needs context", never "not a bug". Every
   classifier output is a suggestion; the fixer reproduces before fixing.
5. **Infrastructure failures are not attempts.** A provider outage, usage limit or network fault rests that model
   and puts the job back unchanged. Rounds and escalation are only spent on real attempts. (This system burned
   rounds twice before that was enforced everywhere: see section 18.)
6. **Escalate instead of parking.** Failed work climbs a ladder of stronger, more expensive models. A human sees it
   only when the ladder is exhausted.
7. **Never weaken a check.** No fixer or reviewer may skip, delete or loosen a test, assertion, gate or ratchet,
   or re-anchor a baseline. A reviewer category exists just to catch it ("weakened-check").
8. **Integration is a separate, mandatory test.** Every change is reviewed alone, but changes break each other.
   Accepted work only becomes the base after a train has built and tested all of it together.
9. **Everything is journalled, resumable and live-configurable.** Each decision is appended to a JSONL ledger.
   Every routing knob is a small file, read fresh each loop, so policy changes need no restart.
10. **Cheap models first, measured continuously.** Per-model acceptance, first-pass rate, time and cost are
    benchmarked against task difficulty. Routing follows the numbers.

## 2. Roles, independent of model

| Role | What it does | Reference deployment (Zene) | Suggested Claude + classifier setup |
|---|---|---|---|
| **Finder** | reads code slices and reports suspected bugs; never edits | GPT 6.0 Luna (codex, read-only, max effort) | Claude Opus or Sonnet, read-only, high effort |
| **Deduplicator** | groups reports of the same defect | `nomic-embed-text` embeddings, plus Clef for the uncertain band | an embedding model, plus your classifier for the uncertain band |
| **Realness classifier** | likely real / false positive / already fixed / needs context | Clef (small local model) | a small classifier model |
| **Difficulty ranker** | puts every task on one difficulty curve by pairwise comparison | Clef | the same classifier (or Claude Haiku if your classifier is weak at it) |
| **Root-cause grouper** | finds different bugs with one shared cause | code, text and pattern signals, judged by Clef | the same signals, judged by your classifier |
| **Router** | orders the queue and picks a fixer per job | code (`qwenfix.py`) | code |
| **Fixer** | reproduces, writes a failing test, fixes, proves, reports | DeepSeek V4.1 Flash, MiniMax M3.1, Claude Haiku/Sonnet lanes; Luna and local Qwen at times | Claude Haiku for easy work, Sonnet for normal work, Opus for the hard tail |
| **Mechanical checker** | completeness check and negative control | code (`submit_check.py`, `negctl.py`) | code |
| **Reviewer** | independent verification with evidence-only rejections; may make small fixes | Luna (1–6 sessions, autoscaled) | Claude Opus, or Sonnet on a different model or tier than the fixer (section 12.3) |
| **Small-fix verifier** | re-proves a reviewer's own fix before accepting | code (`verify_qa_fix`) | code |
| **Escalator** | moves failed work up the model ladder | code | code |
| **Integrator** | builds and tests all accepted work together, pins and removes culprits | code (`merge_train.py`) | your CI and merge queue, plus this logic |
| **Train doctor** | diagnoses an integration failure the integrator cannot pin | model ladder: Sonnet → Muse Spark → Sol → free model → local | Sonnet → Opus |

The rest of this document describes the roles. Section 12 maps them onto Claude models and a classifier of your choice.

## 3. Architecture, processes and files

```
                    ┌───────────────────── FIX-BASE (newest promoted integration branch) ─────────────────────┐
                    │                                                                                          │
  [Finder] patrol ──► findings.jsonl ──► [Triage] dedup → realness → difficulty → root causes ──► fix queue     │
                                                                                                    │          │
                                             [Router] order, batch, pick a model ◄───────────────────┘          │
                                                   │                                                            │
                                     [Fixer] own worktree + branch, brief, red/green proof, report              │
                                                   │                                                            │
                                   [Mechanical] pre-submit check ──fail──► back to the same session (once)      │
                                                negative control ──fail──► back to the same session (once)      │
                                                   │                                                            │
                                     [Reviewer] evidence-only verdict (+ small fixes, re-proven)                │
                                         │                    │                                                 │
                                       ACCEPT              REWORK ──► same fixer session, up to N rounds        │
                                         │                    └──► escalation ladder ──► deferred / donation    │
                                         ▼                                                                      │
                      [Integrator] merge train: merge all accepted → build → full tests vs baseline → gates     │
                                         │  └─ culprits pinned → reverted → port jobs   └─ doctor if unpinnable │
                                         └──────────────────────────── promote ─────────────────────────────────┘
```

### Long-running processes (systemd user units on the Zene machine)

| Unit | Runs | Graceful stop |
|---|---|---|
| `zene-patrol` | the finder, pass after pass (`qwen/patrol-run.sh` → `tools/luna-patrol/patrol.py`) | `touch research/reviews/luna-patrol/STOP`; held by `qwen/PATROL-HOLD` |
| `zene-triage` | triage of each completed patrol pass (`tools/luna-patrol/triage_new.py --watch`) | — |
| `zene-fix` | the fix loop (`qwen/qwenfix.py fix`) | `touch qwen/STOP-FIX`: finishes running jobs, starts none, exits; systemd restarts it |
| `zene-qa` | the review loop (`qwen/qwenfix.py qa`) | `touch qwen/STOP-QA` |
| `zene-merge-train` (timer, every 3 h) | the integrator (`qwen/merge_train.py`) | one train at a time (file lock) |
| `zene-usage-monitor` (timer) | rests paid models while a plan is limited | — |

### Directory layout (the loop's working state)

```
qwen/
  state/<unit>.json            current state of each unit (section 13)
  runs/<unit>.r<N>/            one folder per attempt: brief.md, session.log, record.json, logs/, negctl/, leftover/
  rework/<unit>.json           a job waiting for its next round (notes = the review, or the escalation)
  qa/pending/<unit>.r<N>.json  waiting for review      qa/done/<unit>.r<N>.json  reviewed (verdict + reasons)
  parked/                      out of the loop (features waiting to move up a tier, driver errors)
  REJECTIONS.jsonl             every evidenced rejection     BUG-ESCALATIONS.json / ESCALATIONS.json  ladders
  FEATURE-QUEUE.jsonl, CLAUDE-QUEUE.jsonl, FEATURES-ACCEPTED.tsv, BUG-RANK.json, ROOT-CAUSE-GROUPS.json
  merge/                       train logs, MERGE-EXCLUDE.txt, baseline-failures.txt, gates-last.json, DOCTOR.json
  live config: OPENCODE-FREE.json, MULTI-SESSION.json, BUG-PREFER.json, LANES.json, LUNA-CAP, AUTOSCALE.json,
               FEATURE-TIERS.json, FEATURE-TIER-CUTS.json, MODEL-SCHEDULE.json, DSH-HARNESS.json, MCODE-HARNESS.json,
               QA-FIX.json, holds and stops: STOP*, *-HOLD, QWEN-PAUSED
  logs/fix.log, qa.log, merge-train.log
```

## 4. Stage 1: finding bugs

**Code:** `tools/luna-patrol/patrol.py` (about 280 lines). **Prompt:** Appendix A.1.

### Inputs
- the tip of the current base branch, read from git;
- the upstream base commit (Zene forks LMMS, so code inherited from upstream is handled differently).

### Algorithm, per pass
1. **Snapshot from git**, never from a working tree, so a half-edited file is never read.
2. **Pack the code into slices** of about 230 KB (`--slice-kb 230`):
   - files the project wrote: whole, with line numbers (`FILE`);
   - upstream files the project changed: as a unified diff against the upstream base (`DIFF`);
   - upstream files kept unchanged (optional): whole (`UPSTREAM FILE`).

   Files over 300 KB (`DATA_TABLE_BYTES`) are treated as data tables and skipped. Vendored code is optional.
3. **Rotate the view every pass.** Slice boundaries shift each pass, so no bug always sits on a boundary. A
   different review *angle* goes into the prompt each time:

   | Angle | Looks for |
   |---|---|
   | `general` | any kind of bug |
   | `threads` | concurrency and the realtime rule |
   | `undo` | undo/redo and the command layer |
   | `lifetime` | object lifetimes and memory |
   | `edges` | edge cases and input validation |
   | `ui` | user-visible state |
4. **Read each slice.** It goes on stdin to the finder, read-only, with no tools: everything it needs is in the
   slice. Three slices run in parallel (`--workers 3`).
5. **Parse** the reply's `BUG <n>:` blocks into findings, and de-duplicate them into
   `research/reviews/luna-patrol/findings.jsonl`. Write `STATUS.md` with progress.

### Why it is built this way
- **Whole files beat search** for finding bugs: the model sees invariants across a file.
- **Diffs for inherited code** spend tokens on what the project changed.
- **Rotating angles and boundaries** make repeated passes find new things instead of the same top-20.
- **The prompt asks it to "keep going"** and report low-confidence suspicions marked `low`. Recall matters more
  here, because triage removes the noise.

### Output per finding
Title, `where` (file:line), `what`, `when` (a concrete trigger and its effect), `fix` (a suggestion), `difficulty`
(easy/hard/unsure), `confidence` (high/medium/low), plus the pass number, slice and revision.

### Stop conditions
- the `STOP` file;
- a usage ceiling: `patrol-run.sh` will not start once the finder's weekly usage reaches 50 %;
- a hold file: the operator limits the finder model to two of the three roles (finder, fixer, reviewer).

The integrator restarts the patrol on every new base.

## 5. Stage 2: triage

**The classifier** (Clef in Zene) is a small local model, served by Ollama at `/v1/systemone`. It answers structured questions
(`noul` = a probability, `score` = 1–5, `choice`). It is free per call, which is why triage can be exhaustive.
Every call is journalled with the content hash of its input, so runs resume. A model error stops the run; it never
counts as an answer. **Your classifier** takes this role in a Claude setup (section 12.2).

### 5.1 Repeats: dedup

**Code:** `tools/luna-patrol/dedup.py`, called by `triage_new.py`.

1. Embed each finding (title + what + when + fix) with `nomic-embed-text`.
2. Compare only findings in the **same source file**.
3. Classify each pair by cosine similarity:
   - **at or above `HI` = 0.94:** the same defect;
   - **below `LO` = 0.84:** a different defect;
   - **in between:** the classifier is asked the same-defect question (Appendix A.2) and returns a probability.
4. Join findings with **average-linkage agglomeration** at distance **`T` = 0.40**, with one hard rule: two findings
   from the **same pass** are never joined. One reader numbers each bug once per slice; in the gold set, 0 of 516
   duplicate pairs shared a pass.
5. Hold known clusters fixed. A new finding joins one or starts its own. "Known" includes everything earlier, *and
   bugs already in a fixer's hands*.
6. Each cluster's representative is the member from the latest pass, whose text matches the newest code.

**Measured** against hand-labelled gold: pairwise precision 0.988, recall 0.992. An earlier classifier-only
clustering scored P 1.000 / R 0.633. Recall is what makes the embedding stage worth it.

### 5.2 Is it real? (`classify.py` questions, used by `triage_new.py`)
For each *new distinct* cluster (dedup runs first, so no call is spent on a repeat), the classifier sees the
original code at the revision the finder read, the current code, and the finding. It answers four questions
(Appendix A.3):

| Key | Type | Question |
|---|---|---|
| `real` | score 1–5 | how likely the original finding is a real bug |
| `still_present` | probability | does the current code still contain it |
| `false_positive` | probability | is it contradicted by the code or an intended contract |
| `already_fixed` | probability | was it real but demonstrably repaired since |

The answers become one **disposition**. The rules are conservative on purpose, and anything else is "needs context":
```
likely_false_positive : false_positive >= 0.75 and real <= 1.5
already_fixed         : already_fixed  >= 0.75 and still_present <= 0.25 and real >= 2
likely_open_bug       : still_present  >= 0.75 and real >= 3 and false_positive <= 0.25
needs_context         : otherwise
```

### 5.3 How hard? Two methods
- **Per-bug questions (v2).** Three questions (Appendix A.4), asked only for `likely_open_bug` or `needs_context`
  clusters:
  - `easy` if easy ≥ 0.85, hard ≤ 0.25 and context ≥ 0.75;
  - `hard` if hard ≥ 0.75;
  - otherwise `needs_context`.

  This labelled about 92 % of the queue `needs_context`, which is not useful for routing. Hence the second method.
- **Pairwise ranking** (`bug_difficulty_sort.py`).
  - The question is "which of these two is harder to fix correctly?" (Appendix A.5).
  - A merge sort over every queued bug, asking each comparison in **both orders**, in a pair-stable random
    order, to cancel position bias.
  - New bugs are placed later by binary insertion (about 12 comparisons each).
  - Output: a **percentile per bug** (0 = easiest) in `qwen/BUG-RANK.json`.
  - Features are ranked the same way.

### 5.4 Still open on today's code? (`retriage_open.py`)
Bugs can wait weeks. The realness questions are asked again with "current code" = the base *now*, and the results
are appended to `qwen/CLEF-RETRIAGE.jsonl`. The queue drops a bug whose latest answer is `already_fixed` or
`likely_false_positive`. Deleting the file undoes it. Run it before spending fixer time on old bugs.

### 5.5 Shared root causes (`root_causes.py`)
This looks for **different** bugs with one cause, so one systemic fix closes several. Candidate families are
connected components of a graph built from three signals:
1. **code:** the function enclosing each finding (at its revision); the same function, the same function name
   elsewhere, or a shared rare callee. An identifier named by more than 25 bugs is too generic to link on.
2. **text:** embedding similarity across *different* files: at least 0.80, or 0.72 when both bugs match the same
   pattern class.
3. **pattern:** keyword classes such as non-finite numbers, deferred deletion, undo before-state, or
   audio-thread allocation. A pattern only strengthens a text link; it never links on its own.

Families of 3–25 are cut with average linkage at 0.18. A judge then confirms that a family really shares one
cause (the mean of a plain and a code-context judgement must be at least 0.25). Confirmed families go to
`qwen/ROOT-CAUSE-GROUPS.json` and become one job of at most 6 bugs.

### Outputs of triage
- the queue files `queue/{easy,needs_context,hard}.jsonl`;
- `COUNTS.json`;
- an append-only `decisions.jsonl` that includes dropped findings, with the reason and the cluster a repeat joined.

## 6. Stage 3: queue and routing

**Code:** `qwen/qwenfix.py` (`load_queue`, `smart_order`, `next_item`, the main loop) and `qwen/opencode_free.py`
(model selection and session slots).

### 6.1 Order (`smart_order`)
Untouched bugs are sorted by three keys, in this order:
1. **The classifier said `likely_open_bug`** (those first).
2. **Severity from the text** (regexes, in this order):
   - realtime (audio thread, allocation or lock on a render path, xrun);
   - data loss (undo, save, load, serialise, corrupt);
   - crash (dangling, use after free, null, segfault).
3. **Difficulty percentile**, easiest first.

Before that, `next_item` serves work in this order: feature reworks, new feature units, bug reworks and port
jobs, then untouched bugs. A unit already in flight is never served twice.

### 6.2 Batching
A new bug job takes up to **3 untouched bugs in the same file** (`BATCH_MAX`, overridable live). That means one
tree, one build, one session and one review. The fixer reports a verdict per bug. Companions it did not claim go
back to the queue.

### 6.3 Who gets the job
Routing is a set of live files. Routing is by **kind** (bug or feature) and by **difficulty**:

| Lever | File | Effect |
|---|---|---|
| models allowed for the cheap fixer pool | `OPENCODE-FREE.json` | the list of models, and total workers |
| sessions per model | `MULTI-SESSION.json` | `{model: n}`; `kind_sessions` reserves slots per kind, e.g. 2 feature + 2 bug |
| bug preference | `BUG-PREFER.json` | models tried first for bugs |
| feature tiers | `FEATURE-TIERS.json` | which models build the cheap tiers |
| starting tier of a feature | `FEATURE-TIER-CUTS.json` | percentile below 0.65 → middle tier, below 0.9 → Sonnet, else Opus |
| Claude lanes | `LANES.json` | named lanes: model, effort, tiers, enabled; `max_concurrent` open lanes |
| an easy-bug lane | `LANES.json` `bug_queue.max_pct` | e.g. Haiku takes only bugs at or below percentile 0.5 |
| local model | `STRATA_MAX_PCT` | the local model takes only the easier half |
| schedules | `MODEL-SCHEDULE.json` | blocked windows, e.g. a provider's peak-price hours |
| reviewer and fixer caps for the reviewer model | `LUNA-CAP` | `fix=N qa=M` |
| which harness runs a model | `DSH-HARNESS.json`, `MCODE-HARNESS.json` | e.g. DeepSeek in DeepSeek Harness, MiniMax in MiniMax Code |

**Session slots** are non-blocking file locks: one lock file per slot per model. A job takes a free slot or moves on.

**A Claude lane** holds its unit through review and rework. Its reworks come back to the same lane and continue the
same Claude session. The lane is "open" while it holds a unit, and `max_concurrent` caps open lanes.

**Resting.** A model that failed transiently (rate limit, 5xx, quota, timeout, connection error) rests for 15
minutes (`COOLDOWN`). A session that went quiet is treated as rate-limited and rests longer. The job goes back
unchanged, so no round is spent.

### 6.4 Autoscaling (`qwen/autoscale.py`, knobs in `AUTOSCALE.json`)
- **Fixers back off when review falls behind.** With 12 or more reviews pending, new fixer sessions are halved. At
  24, none start.
- **Memory.** Sessions start only while free RAM stays above a floor (4 GB). Each kind of session has its own cost
  (Haiku 2.5 GB, cheap models 2.5 GB, reviewer 1.5 GB).
- **Reviewers scale with the queue.** One per review waiting or running, at least 1, at most `LUNA-CAP qa`
  (`luna_reviewers`).

## 7. Stage 4: the fix session

**Code:** `qwen/qwenfix.py` `fix_one`, `fix_brief` and `run_session`. **Prompt:** Appendix A.6.

1. **Workspace.** A dedicated git worktree on a dedicated branch (`bugs/qwen-<id>`, `feat/050-<id>-oc`), with a
   configured build tree. Run folder: `qwen/runs/<unit>.r<round>/`.
2. **The brief** has these sections:
   - **the finding** (or the feature's acceptance criteria), as JSON, with historical line numbers;
   - **where to start** (likely files);
   - **the definition of done**, the six things reviewers send work back for:
     1. a decision with evidence (REAL / ALREADY-FIXED / FALSE-POSITIVE);
     2. a deterministic regression test that fails without the fix;
     3. a fix at the cause, with every caller and sibling checked;
     4. no collateral damage;
     5. realtime, undo and command rules kept;
     6. registration and gates passing.
   - **the procedure:**
     1. read;
     2. decide;
     3. write the test first and see it fail (RED);
     4. make the smallest fix at the cause and see it pass (GREEN);

        4b. prove it: reverse the product hunks, rebuild, see RED; restore, see GREEN; record the commands and exit codes;
     5. run the gates script;
     6. write the report, whose first line is the verdict, then evidence and the exact commands;
     7. leave `git status` clean;
     8. run the completion check.
   - **the condensed project rules**;
   - **lessons from recent rejections** (`adaptive.feedback()`);
   - **on a rework:** every earlier review, and an instruction to address exactly the evidenced failures.
3. **The session** runs in a harness. Which one depends on the model: OpenCode, DeepSeek Harness, MiniMax Code,
   Claude Code in a Landlock write-jail, or codex. The harness gets:
   - write access only to the worktree, its git data, the run folder, `/tmp` and the build caches;
   - pushes made impossible: a git config that rewrites every push URL to an invalid one;
   - the read-only `fff` search tools, and an `rg` capped at 120 lines of output.
4. **No wall-clock limit.**
   - A session runs while it shows activity: its session log or run logs change.
   - It is stopped after **45 minutes of silence**. The harness wrappers also have a 25-minute silence watchdog,
     and 4 minutes to produce a first event.
   - After **6 hours** the operator is notified, and nothing is killed.
5. **Afterwards** the loop records: head before and after, commits, files, the report's verdict, minutes, the exit
   code, and leftovers (uncommitted edits are saved as a patch and reverted).
   - A session with commits goes on to the mechanical checks.
   - A session without commits gets **one retry**; after that it escalates.

## 8. Stage 5: mechanical checks before review

Both checks run when a session ends with commits. A failure goes **straight back to the same session**, resumed
with the failure list. It happens once per submission and does not count as a review round.

### 8.1 Pre-submit completion check (`qwen/submit_check.py`)
- **CHECKED** (pass, fail or warn):
  - the branch has commits;
  - the working tree is clean;
  - the report exists;
  - tests were added or changed;
  - the gates script passes on the touched files;
  - for features, also: agent command and snapshot regenerated, registrations, GUI path tested, docs and manual,
    realtime and undo.
- **ASKED:** each item must be answered, with evidence, in the report's `## Self-check` section:
  - Does the regression test fail without the change and pass with it (exit codes)?
  - Is it deterministic (no wall-clock timing, sleeps or races)?
  - Is the fix at the cause, with every caller and sibling consistent?
  - Is there collateral damage: old data loading, limits hitting legitimate callers?
  - If the verdict is not-real: what code or contract shows it?

**Where the list came from.** It was built from what the reviewers actually sent back across 62 feature reworks:

| Problem | Share of reworks |
|---|---|
| missing agent command or stale snapshot | 61 % |
| registration and gates | 58 % |
| GUI path missing or untested | 53 % |
| docs and manual | 46 % |
| realtime | 37 % |
| undo | 29 % |
| unmet criterion | 22 % |
| tests that pass without the change | 20 % |

### 8.2 Negative control: the "red proof" (`qwen/negctl.py`)
1. In the worktree, revert the branch's **product** changes, keeping tests, docs and manifests.
2. Build the affected test targets. Run the tests the branch changed or the report names.
3. Restore HEAD, rebuild, and run again.

Result: `{ok: True/False/None, tests, reverted_rc, restored_rc, detail}`.
- **`ok: False`:** the tests pass without the fix, so they prove nothing. It goes back to the session.
- **A build failure with the product reverted** counts as the test failing.
- **`ok: None` ("not judged"):** no product change, no identifiable test, build trouble, or a test that also fails
  with the fix in. It is recorded and passed on to the reviewer.

## 9. Stage 6: review

**Code:** `qwen/qwenfix.py qa` (`qa_one`, the brief builders, `blocking_rule`, `founded_rejection`,
`verify_qa_fix`). **Prompts:** Appendix A.7–A.10.

### 9.1 The review brief
- **What is under review:** the worktree, branch and the range under review. The range is **the whole branch from
  where it left the base** (`branch_base`), not just the latest round, because a round can hold only report edits.
- **The fixer's verdict and report.** For bugs, also the negative-control result, and any re-review context.
- **"Verify, do not trust":**
  - read the diff against the finding's real semantics;
  - rebuild the touched targets and run the regression;
  - **independently show it is RED** without the product change (reverse the non-test hunks, rebuild, run,
    restore);
  - run the gates script;
  - use unpiped exit codes.
- **The review limits** (never-blocking rules): report-only problems never block, and the reviewer never requires
  runs on platforms this machine does not have.
- **The commit rule:** commit nothing, or the small-fix rule (9.4).
- **The evidence rule** (9.2).
- **The answer:** a short review, then a fenced JSON verdict block (section 14).

### 9.2 The evidence rule: rejections only for evidenced failures
A REWORK, REJECT or DISPUTE must list `blocking` entries, each with a category, what fails, and evidence:

| Category | Means |
|---|---|
| `not-fixed` | the bug, or an acceptance criterion, still fails |
| `regression` | something that worked before now fails |
| `crash-or-data-loss` | a crash, hang, corruption, or saved data lost or changed |
| `realtime` | the audio thread now allocates, frees, locks, waits, does I/O or logs |
| `ci-compile` | does not compile on a CI platform; must point at the API |
| `test-proves-nothing` | the regression passes with the change reversed, or is not deterministic |
| `weakened-check` | a test, assertion, gate, ratchet or baseline is weakened, skipped or re-anchored |
| `new-gate-item` | the gates script lists an item new in a touched file |
| `missing-path` | an acceptance criterion has no working GUI or agent path |
| `out-of-scope` | behaviour outside the finding or unit changes |

**How the loop checks each entry** (`founded_rejection`): the category must be in the list, the `what` must not be
empty, and the evidence must match:
```
EVIDENCE_RE = [\w./+-]+\.\w+:\d+   |   \b(?:EXIT|exit|rc|ctest)\b\D{0,4}-?\d+
```
That is, a `file:line`, or a command result with an exit code.
- **Valid entries** go to the fixer as the rework instructions. **Invalid ones** are dropped and passed on as
  notes.
- **A rejection with no valid entry** is sent back to the reviewer once, with its own earlier answer quoted. If it
  still names none, the work is **accepted with the reviewer's notes**.
- Every evidenced rejection is appended to `qwen/REJECTIONS.jsonl`.

### 9.3 Verdicts

| Verdict | For | Effect |
|---|---|---|
| `ACCEPT` / `ACCEPT-NOTE` | fixes and features | accepted, with or without notes |
| `ACCEPT-FIXED` | fixes and features | the reviewer fixed a small problem itself (9.4); accepted once re-proven |
| `REWORK` | fixes and features | back to the fixer with the valid blocking entries |
| `REJECT` | fixes and features | rejected outright |
| `CONFIRM` / `DISPUTE` | claims ("not a bug" / "already fixed") | the reviewer checks the code itself; DISPUTE needs evidence the bug is present |

### 9.4 Reviewer small fixes (`ACCEPT-FIXED`)
The reviewer may fix a **small** blocking problem itself instead of sending the work back.
- **Size limits** (`QA-FIX.json`): at most 40 product lines in 3 product files, plus 120 test lines in 4 test or
  registration files, and at most 2 commits.
- **Allowed:** within the same design, nothing new invented. Typical cases are a missing guard, a missed count or
  assertion update, a portability guard, a doc sentence, a registration, or a one-function bug.
- **Never allowed:** a missing GUI or agent path, a test redesigned around a new seam, a race or threading change,
  realtime logic beyond a one-line guard, or anything the reviewer is unsure of.
- **For a claim** the reviewer finds is real: test first, see it fail, fix, prove it.

**`verify_qa_fix` re-proves it before accepting.** It checks:
- the size limits;
- no protected gate, ratchet or baseline file touched;
- no assertion removed;
- no skip added;
- a rebuild with the focused tests rerun;
- for bugs, the red proof again from the branch base.

If anything fails, the reviewer's fix is dropped and the review is asked again.

### 9.5 Features get two independent reviews
1. **Code verifier:** correctness, regressions, the realtime rule, undo, gates and registrations, code smells,
   licensing.
2. **Feature verifier**, run only after the code verifier accepts. It checks that every acceptance criterion is
   really met for a user and proven by a test, that the action exists in both GUI and agent surfaces through one
   registered command with one undo step, that old projects load, that docs are updated, and that there is no
   scope creep. It runs the new tests itself. (Prompt: Appendix A.10.)

## 10. Stage 7: rework and escalation

### 10.1 Bugs
1. **REWORK** returns the job to the **same model, continuing its own session** (session id kept per harness),
   with the valid blocking entries as notes. This repeats up to **4 rounds** (`adaptive.max_rounds`).
2. **A session ending without commits** gets one retry with a note.
3. **After that, the bug escalates instead of parking** (`bug_escalate`, `BUG-ESCALATIONS.json`):

   | Level | Who | How |
   |---|---|---|
   | 1 | a strong cheap-pool model | fresh session, every earlier review as notes |
   | 2 | Claude Sonnet lane (tier `bug`) | lane session |
   | 3 | Claude Opus lane (tier `bug-hard`) | lane session |
   | 4 | deferred | out of the queue until the next base, then starts again at level 1 |

   A failed tree preparation is retried once at the same level first.

### 10.2 Features
A unit that fails its attempt is parked, and `features_queue.py` moves it one tier up:
**free → middle (cheap strong model) → Sonnet → Opus → human** (in Zene, a donation goal: the operator funds it or drops it). The unit's
commits stay on its branch, and the next tier gets the last review as notes. Moves are recorded in `ESCALATIONS.json`.

### 10.3 What never escalates
- **Provider outages, usage limits and connection failures:** the model rests and the job is re-queued unchanged.
- **Driver errors:** the unit is parked with the error for a human to look at.
- **Work lost to an outage:** the rounds and escalations it caused are given back (section 18).

## 11. Stage 8: integration

**Code:** `qwen/merge_train.py` (about 800 lines), run every 3 hours. It is due when enough accepted branches are
waiting, or any has waited 24 hours. It works in one dedicated worktree (`zene-cachyos`), and nothing else runs in
it during a train.

### 11.1 Algorithm
1. **Candidates:** accepted fix and feature commits not yet on the base, minus `MERGE-EXCLUDE.txt` and minus
   originals superseded by a port job.
2. **Dry run** (`git merge-tree`, with a union merge driver for append-only lists). Each candidate gets one of:
   - **clean:** merge as is;
   - **conflict only in generated docs** (README, release notes, the commands snapshot, all-sources): merge
     keeping the train's side, because those files are regenerated in step 4;
   - **any other conflict, for a bug:** a **port job**. The original is excluded, and a fixer re-applies the fix by
     hand on the newest line: read each commit, do not merge or cherry-pick, keep the red/green proof;
   - **any other conflict, for a feature:** left for the orchestrator to port.
3. **Merge** onto a new branch `050/m0-train-<N+1>`, then **build everything**. A build error is pinned on the merge
   that last touched the failing file, which is reverted and ported, up to 6 reverts. If a revert is impossible, the
   train is rebuilt from the base without that merge.
4. **Regenerate** derived artefacts from a live instance (the commands snapshot, doc figures) and commit them.
5. **The bar:** the full test suite, compared with `baseline-failures.txt` (known failures).
   - **Each new failure is rerun alone.** One that passes alone is load-flaky.
   - **A persistent failure is pinned** on the merge that touched its test source. If none did, it is *examined*:
     flaky? a crash pinned from its stack? a first-parent bisect?
   - **A pinned merge is reverted and ported only once its revert is shown to fix the test** ("confirm by
     reversal"; bisect alone misled on scripted tests).
   - **A flaky or unpinnable failure** becomes a test-fix job and joins the baseline. An unpinnable **crash**
     blocks promotion.
6. **Gates:** every gate that passed on the previous train must still pass. Ratchet items new to this train are
   queued as split jobs. Ratchets are never re-anchored.
7. **Promote:** move `FIX-BASE`, write a STATUS note, restart the finder on the new base, notify. If anything is
   unresolved, the train is not promoted, with the reason logged and a notification sent.

### 11.2 The train doctor (`qwen/train_doctor.py`)
For a train that fails **twice with the same failure signature** that the integrator could not pin. The signature
is the error lines with paths, numbers and hashes stripped.
- **What it does.** It diagnoses only. It gets a self-contained brief (Appendix A.11): the error excerpt, the
  train's merges, and the diffs of the merges that touch the files the error names. It also gets a read-only
  checkout of the failed train. It must name the branch or branches to take off (at most 3), with `file:line`
  evidence, as a JSON block.
- **What the loop checks.** Every named branch must be one that train actually merged.
- **Applying the answer.** The loop applies it with the integrator's own port-job function and restarts the train.
  The train's full bar decides whether the doctor was right.
- **The ladder.** A repeat failure goes one rung down a configurable list of lanes, strongest first. Zene's is
  Sonnet 5.5 high → Muse Spark contributor high → GPT 6.1 Sol medium → a random free model → local Strata. A rung whose model is unavailable at that moment is skipped. When all
  rungs fail, a human is told.

### 11.3 What review cannot see, and the train can
Real examples from the week this was written. Every change involved passed its own review and tests.
- **A cross-branch link error.** Branch A made some globals `thread_local`; the base's existing test declared them
  plain `extern`. The train's pinning blamed a third branch at first. The fix was to read the linker message and
  check which branch made which change.
- **A startup crash from a build-configuration mismatch.** A test seam added *data members* under
  `#ifdef LMMS_TESTING`. The core library is compiled with that macro when tests are built; the app's `main.cpp` is
  not. The class layout differed, and the app segfaulted at startup, failing about 83 tests. The train blamed merges
  by test source and reverted innocent work six times. The fix was to read the core dump (`coredumpctl info`) and
  the disassembly at the faulting address. **Lesson:** a mass failure where every instance exits with a crash code
  is one crash: read the stack first.

## 12. Adapting it

### 12.1 To a SaaS / web codebase
| Zene concept | SaaS equivalent |
|---|---|
| upstream base and diffs | vendored or forked libraries; your monorepo's generated code (skip it) |
| slices of about 230 KB | per service or package, sized to the model's context; keep whole files |
| angles: threads, undo, lifetime, edges, ui | **authz and tenant isolation, injection and validation, data migrations and backfills, idempotency and retries, concurrency and races, caching and consistency, PII and secrets in logs, error handling and user-visible state, API contract drift, performance cliffs** |
| the realtime rule | latency budgets and hot paths: no N+1 queries, no sync I/O in request handlers |
| one honest undo step | transactional integrity: migrations reversible, writes atomic, events exactly-once or idempotent |
| `feature_check.py` gates | lint, type check, dependency and licence scan, security scan, coverage ratchets |
| the live-instance snapshot | the OpenAPI or GraphQL schema snapshot, regenerated and diffed |
| the merge train | a merge queue or "train" branch in CI: build all accepted PRs together, full test suite vs a known-failures baseline, confirm-by-reversal on failures |
| `FIX-BASE` | the integration branch your bots work from (not `main` until promoted) |
| `ci-compile` (other platforms) | other runtimes, browsers or regions your CI covers but bots cannot run locally |

**Never give bots production access or production data.** They need a sandbox per job: a container or devcontainer
with test databases seeded from fixtures, no outbound network except the model API and the package mirror, push
blocked, and only a scoped CI token.

### 12.2 Your own classifier
Your classifier replaces Clef everywhere in section 5. The pipeline needs four operations from it. Each must return a
calibrated number, not prose:

| Operation | Input | Output | Used by |
|---|---|---|---|
| `same_defect(a, b)` | two findings (text, at most about 2,500 characters each) | probability | dedup, uncertain band only (5.1) |
| `realness(finding, original_code, current_code)` | finding plus the two code snapshots | `real` 1–5; `still_present`, `false_positive`, `already_fixed` as probabilities | 5.2, 5.4 |
| `harder(a, b)` | two task descriptions | P(a is harder) | 5.3 pairwise ranking |
| `same_cause(family)` | 3–25 bug reports, with and without code context | probability | 5.5 |

**Calibrate before trusting any threshold.** The numbers in Appendix B were measured for Clef on Zene's data.
1. **Label a gold set:** about 300 finding pairs for same/different, and about 100 findings for real/false/fixed,
   all from your own codebase.
2. **Grid-search the thresholds** (`LO`, `HI`, `T`, and the disposition cut-offs) for precision and recall.
3. **Keep the asymmetry:** a false "not a bug" loses a real bug, so keep the drop thresholds strict (≥ 0.75 and
   more).

Freeze the classifier model version per run. Zene's tools refuse to run if the model digest changed mid-campaign.

If your classifier cannot do structured probabilities well, Claude Haiku with a forced-JSON answer is a usable substitute. It
costs more per call, so exhaustive triage gets expensive.

### 12.3 Claude-only fixers and reviewers
- **Fixers by difficulty:**
  - Haiku for the easy half (percentile ≤ 0.5);
  - Sonnet for most work;
  - Opus for escalation level 3 and the hardest features.

  This mirrors Zene's ladder, and Haiku did well on easy bugs (69 % accepted at mean difficulty 1.26).
- **Reviewer independence is the main risk when one vendor writes and reviews.** Their blind spots are correlated.
  Mitigations, in order of value:
  1. The mechanical checks (section 8) do not depend on the model at all.
  2. **The reviewer runs on a different model tier than the fixer**, in a fresh context, with the adversarial
     brief ("you did not write it; verify, do not trust") and the evidence rule.
  3. The reviewer re-runs the red proof itself.
  4. For high-risk areas (auth, billing, migrations, tenant isolation), add a **second reviewer** with a different
     focus angle, and require both to accept. This is the same pattern as Zene's two feature verifiers.
  5. The integration train, plus staged rollout, is the final net.
- **Budget control:**
  - per-session cost caps (the harness wrapper kills a session past a cap);
  - a usage monitor that rests models when a plan is near its limit;
  - autoscaling that halts fixers when review falls behind;
  - lane caps (`max_concurrent`).

  Log the tokens and cost of every attempt. The benchmark (section 16) needs them.

### 12.4 Human gates a company will probably want
Zene runs with no human review by the operator's choice. In a company:
- **merging to `main`:** a human approves the promoted train, not each PR;
- **escalation exhausted, driver errors, and train-doctor failures:** they go to a human queue;
- **security-sensitive paths:** always need a human, or a second reviewer plus a human;
- **an audit trail:** the JSONL ledgers (decisions, rejections, escalations, train log) already provide it.

## 13. Unit state machine

```
 queued ──► fixing ──► (no commits) ──► retry ──► fixing ──► (still none) ──► escalated(level+1)
              │
              └──► precheck fail ──► fixing (same session, once)
              └──► negctl fail ───► fixing (same session, once)
              └──► in-qa ──► ACCEPT / ACCEPT-NOTE / ACCEPT-FIXED / CONFIRM ──► accepted ──► merged onto train N
                       │                                               └──► conflict ──► port job (new unit "<id>-port")
                       ├──► REWORK / DISPUTE ──► rework (round+1, same session) ──► fixing
                       │         └── round > max_rounds ──► escalated(level+1) ──► ... level 4 ──► deferred
                       └──► (feature) code verifier ACCEPT ──► feature verifier ──► accepted | rework
 features: parked ──► next tier (free → middle → sonnet → opus) ──► donation goal
 infrastructure failure at any point ──► job re-queued unchanged (no round, no escalation)
```

`state/<unit>.json` `outcome` values include: `in-qa`, `rework`, `retry`, `precheck`, `escalated`, `deferred`,
`accepted`, `parked`, `no-result`, `driver-error`, `build-failed`. Final outcomes (`FINAL_OUTCOMES`) are those
starting with `accept`, `confirm`, `parked`, `no-result`, `driver-error` or `build-failed`.

## 14. Data formats

**Finding** (`findings.jsonl`; `text` is the finder's whole `BUG n:` block, and `where`, `what`, `when`, `fix`,
`difficulty` and `confidence` are parsed from it):
```json
{"key": "<stable id>", "pass": 20, "slice": 7, "angle": "undo", "rev": "<sha read>", "file": "tests/src/core/RackZonesTest.cpp",
 "line": 248, "title": "Zone inverse evidence contradicts the asserted inverse", "confidence": "medium",
 "duplicate": false, "text": "BUG 5: ...\n  where: ...\n  what: ...\n  when: ...\n  fix: ...\n  difficulty: easy\n  confidence: medium"}
```

**Triage decision** (`research/reviews/luna-new-triage/decisions.jsonl`, append-only; a published queue item is the
same record plus `members`). This is a real record, abridged:
```json
{"run_id": "729b4348fa88", "pass": 20, "finding_id": 17641, "new_cluster": true, "cluster": "new-p20-108",
 "title": "...", "file": "...", "line": 248, "text": "BUG 5: ...", "revision": "<sha>", "original_revision": "<sha>",
 "angle": "undo", "resolved_path": "...", "lane": "unassigned",
 "realness": "needs_context", "realness_answers": {"real": {"type": "score", "score": 2.46}, "...": "..."},
 "realness_context_sha256": "<hash of exactly what the classifier saw>",
 "difficulty": "needs_context", "difficulty_answers": {"easy": {"type": "noul", "noul": 0.80}, "...": "..."},
 "question_version": 2, "conditional_on_realness": true, "luna_difficulty": "easy",
 "model": "clef-flash:latest", "model_digest": "<sha256>", "embed_model": "nomic-embed-text:latest",
 "embed_model_digest": "<sha256>", "decided": "2026-10-20T20:24:20", "outcome": "queued", "reason": null,
 "prior_disposition": "needs_context", "report_evidence": [], "prior_review_evidence": [], "assignments": []}
```
Note the provenance: model digests and a hash of the exact context. Any decision can be reproduced, or invalidated
when the model changes.

**Run record** (`runs/<unit>.r<N>/record.json`):
```json
{"cluster": "new-p10-257-port-port", "round": 1, "title": "...", "tier": "new:needs_context",
 "worktree": "...", "branch": "bugs/qwen-new-p10-257-port-port", "engine": "opencode",
 "model": "opencode-go/deepseek-v4.1-flash", "harness": "...", "started": "...", "finished": "...", "minutes": 63.4,
 "rc": 0, "head_before": "<sha>", "head_after": "<sha>", "commits": ["<h> <subject>"], "files": ["..."],
 "leftovers": false, "qwen_verdict": "FIXED", "kind": "fix|claim|feature",
 "negctl": {"ok": true, "tests": ["..."], "reverted_rc": 8, "restored_rc": 0, "detail": "..."}}
```

**Reviewer answer** (the fenced JSON block at the end of a review):
```json
{"verdict": "ACCEPT|ACCEPT-NOTE|ACCEPT-FIXED|REWORK|REJECT|CONFIRM|DISPUTE", "summary": "...",
 "blocking": [{"category": "not-fixed", "what": "...", "evidence": "src/core/Mixer.cpp:2842"}],
 "notes": ["..."], "rework_instructions": "..."}
```

**Rejection ledger** (`REJECTIONS.jsonl`): at, cluster, round, kind, stage, model, started, reviewer, verdict,
blocking (valid entries), dropped (invalid entries), unfounded (bool).

**Bug escalation** (`BUG-ESCALATIONS.json`):
`{"<cluster>": {"level": 1, "base": "050/m0-train-9", "history": [{"at", "reason", "round", "to_level"}]}}`

**Accepted features** (`FEATURES-ACCEPTED.tsv`): `id \t branch \t sha12 \t verdict \t title \t provenance`.

**Usage per attempt** (`runs/.../opencode-<model>-usage.json`): model, harness, exit, stalled, per-step token
counts and costs, had_error.

## 15. Verification

| Layer | Catches | Cannot catch |
|---|---|---|
| Finder prompt discipline | `where`, `when` and `confidence` force concrete, checkable claims | anything outside the slice; invents some bugs (that is triage's job) |
| Dedup (embeddings + classifier) | repeats, so no double work | merges two different bugs in the same function rarely (P 0.988) |
| Realness classifier | obvious false positives; already-fixed bugs | a small model's judgement. Nothing is dropped on "unsure", and the fixer must reproduce |
| Fixer's own red/green proof | a test that fails before and passes after | a test that misses the real failure mode |
| Pre-submit check | incomplete work: no tests, no report, gates red, self-check unanswered | correctness |
| Negative control | tests that pass without the fix | tests that fail for an unrelated reason ("not judged") |
| Reviewer, with the evidence rule | wrong or incomplete fixes, regressions, scope creep, weakened checks; each rejection carries proof | correlated blind spots with the fixer; platforms it cannot run (it reads for portability) |
| Reviewer small-fix verification | a reviewer breaking things while "helping" | the same limits as the review |
| Second (feature) verifier | criteria not met; missing GUI or agent path | only what the criteria say |
| Integration train | combinations of individually-correct changes; build, full suite and gates on the real combined tree | platforms CI does not run locally (Zene: hosted CI is Qt 5 / MSVC, triggered by pushes to `ci/*` branches) |
| Train doctor | names an unpinnable culprit with evidence | can be wrong; the next train is the proof |

## 16. Metrics and observability

### 16.1 Per-model benchmark (`qwen/benchmark.py`, which writes `BENCHMARK.md`)
For each model and harness, separately for bugs and features, it reports:
- reviewed attempts, accepted %, first-pass %;
- mean difficulty;
- minutes per attempt, accepted per hour;
- output tokens per attempt and per accept;
- cost per accept, at list price and at the plan's effective price.

**Use difficulty-adjusted acceptance, not difficulty-weighted.** A weighted rate,
`Σ w·success / Σ w` over a model's own tasks, gives no credit for taking harder tasks.
1. Fit one logistic curve of acceptance against difficulty over all models.
2. For each model, compare its actual acceptance with the curve's prediction for *its own* task mix.

Zene, 2026-10-21, bugs:

| Model | Difference from expected | Reviewed | Mean difficulty |
|---|---|---|---|
| DeepSeek in DeepSeek Harness | +11 | 24 | 2.50 |
| Luna | +10 | 895 | 1.74 |
| Haiku | −14 | 78 | 1.26 |
| Beam | −28 | 110 | 2.05 |

Always show the sample size: ±10 points at n = 24.

### 16.2 Other views
- **Why work was sent back:** counts per blocking category per model, from `REJECTIONS.jsonl`. It feeds
  `adaptive.py` lessons back into fixer briefs.
- **Status window:** live sessions per model and role, queues, usage per plan. Also an "All bugs & features" view:
  every unit with status, rounds, models, rejections, escalation and a per-unit timeline, with a "repeated or
  troubled" filter (more than 2 rounds, more than 1 rejection, port jobs or escalations).
- **Notifications** (through `qwen/notify.py` to the backend set in `NOTIFY.json`; Telegram in Zene) for promotions, failed trains, the doctor, sessions running past 6 hours, exhausted
  ladders and scheduled lane changes. Not routine progress.

## 17. Operations

- **Live config.** Change routing by editing the small JSON or text files in section 6.3; they are read fresh each
  loop. Code changes need a graceful restart (`STOP-FIX` / `STOP-QA`), and running jobs finish first.
- **Holds:**
  - `PATROL-HOLD`: the finder off;
  - `QWEN-PAUSED`: the local model off;
  - `BUGS-HOLD`: features only;
  - `LUNA-HOLD`: the reviewer model reserved for something else;
  - a `CLAUDE-LIMIT-UNTIL` file pauses Claude lanes until a usage reset.
- **Scheduled policy:** one-shot systemd timers, for example "pause all Claude lanes at 01:00 after the weekly
  reset", or "keep one Sonnet lane once either current unit finishes".
- **Credentials:** read inside wrapper scripts from the provider's own login file, never printed, never written into
  model configs. Configs use `${ENV_VAR}` expansion.
- **Safety:** pushes blocked by git config, write-jails for agent processes, and a separate worktree per job. The
  integration worktree is touched only by the integrator.
- **Disk:** a full C++ build is 45–50 GB on Zene. Use targeted builds, a shared compiler cache, and debug info
  stripped from test binaries.
- **Sleep, outages and network:** see section 18; every harness needs network settings tolerant of slow
  connections.

## 18. Failure modes seen in production

| Symptom | Cause | Fix now in the system |
|---|---|---|
| Fixes bounced for "gates red" | the gates are red on the train on purpose (ratchets); reviewers took that as a fix's fault | gates are run through a script that fails only on items new in touched files; REVIEW_LIMITS; the evidence rule |
| Reviewers rejected for report wording or a "might" | no bar for a rejection | the evidence rule: category plus `file:line` or exit code; unfounded → re-ask → accept with notes |
| A harness could not commit | its sandbox could write only the worktree, but a linked worktree's git data lives in the main clone | run the harness inside a Landlock jail that includes the git common dir, with its own sandbox off |
| Every session of one harness failed in seconds ("TRANSPORT: Connection error") | IPv6 to the provider broke and IPv4 connects took 0.6–0.9 s, beyond Node's 250 ms per-address happy-eyeballs timeout | `NODE_OPTIONS=--dns-result-order=ipv4first --network-family-autoselection-attempt-timeout=3000` |
| That outage burned 4 rounds per job and escalated work in minutes | connection errors were not classed as transient | the transient regex includes connection errors; the rounds and escalations were given back by hand |
| A free-tier model silent all day | the provider's free tier stopped serving third-party callers | the model rests itself; take it out of rotation |
| A harness's personal config cost 148k input tokens per turn | the user's own MCP servers and skills were loaded | a dedicated data dir with one MCP server (about 12k) |
| Feature work starved bug work on a shared model | one session pool for both kinds | `kind_sessions`: reserved slots per kind |
| Long sessions killed mid-progress | a wall-clock cap | an activity watchdog (45 min of silence), plus a 6-hour notification |
| A train blocked for 15 hours | cross-branch breakages its heuristics could not pin (section 11.3) | the train doctor; diagnosis from linker messages and core dumps |
| A train reverted innocent merges six times | test-source pinning applied to a startup crash | (proposed) detect a mass failure with a crash exit code → one crash → read the stack → doctor |
| `pgrep -f "<script>"` killed the calling shell | the shell's own command line contained the pattern | match `/proc/<pid>/cmdline` exactly; print the process before killing it |

## 19. Build order

### 19.1 A minimum viable version (about 1–2 weeks of work)
1. **A queue:** start from a CSV of known bugs from your tracker. You don't need a finder on day one.
2. **One fixer:** Sonnet in a sandboxed worktree, with the brief in Appendix A.6, adapted.
3. **The pre-submit check and the negative control.** These are cheap and the most valuable step.
4. **One reviewer:** Opus, with the evidence rule (Appendix A.7–A.9), on a different tier than the fixer.
5. **Rework:** the same session, up to 3 rounds, then a human.
6. **Your existing CI** as the integration step, with a known-failures baseline.
7. **Ledgers:** runs, reviews and rejections as JSONL. Add a benchmark script later.

### 19.2 Then add, in this order
1. the escalation ladder;
2. the finder;
3. dedup with embeddings plus your classifier;
4. realness triage;
5. pairwise difficulty ranking;
6. routing by difficulty (Haiku for the easy half);
7. reviewer small fixes with verification;
8. two-stage review for features;
9. merge trains with confirm-by-reversal;
10. the train doctor;
11. root-cause families;
12. autoscaling;
13. scheduled policy.

---

## Appendix A: prompt templates

These are verbatim from the reference deployment, so they name Zene, LMMS, Luna and the "owner". Replace the
product description, build and test commands, and rule sources with your repo's; keep the structure, the
definition of done and the evidence rule.

The project-specific text is kept so you can see how much context each prompt carries. Replace it with your own.
`{...}` marks a value filled in at run time.

### A.1 Finder (patrol), `tools/luna-patrol/prompt.txt`
```
You are hunting bugs in Zene Studio 0.5.0, a music workstation (DAW) derived from LMMS: C++20, Qt 6, GPLv2. Do not run
any commands or tools - everything you need is in the stdin block, which is one slice of the code base. It holds three
kinds of file: files the fork wrote (given whole with line numbers, marked FILE), upstream LMMS files the fork changed
(given as unified diffs, marked DIFF), and upstream LMMS files the fork kept unchanged (given whole with line numbers,
marked UPSTREAM FILE). Zene ships all three, so a bug in unchanged LMMS code is a real Zene bug - report it the same
way. Other slices are being read by other reviewers, so focus on what is in front of you, but say so if something here
clearly breaks against code you can infer exists elsewhere.

This pass looks especially for: {ANGLE} Report other bugs you notice too.

Find as many real bugs as you can. Read it however you like. Every time you find one, write it down straight away and
keep going - do not stop at the first, the fifth or the twentieth, and do not polish earlier entries before moving on.

Useful background (not a checklist):
- <how commands are registered and validated, so the finder does not report what a schema already prevents>
- Realtime rule: nothing on the audio thread (process/render paths, play handles, mixer channel processing, anything
  called per period) may allocate, lock a mutex, log, or touch Qt objects that allocate.
- <sentinel and type facts: ids start at 0, unsigned tick types, Qt 6 JSON/DOM quirks, ...>
- Known by design, do not report: <the project's list of intended behaviours>

For each bug write:
BUG <n>: <one-line title>
  where: <file>:<line> (the line numbers given for new files; for diffs, the + line's position in the new file as best
         you can tell)
  what: what is wrong
  when: a concrete input, state or sequence that makes it go wrong, and what happens (crash, wrong value, lost undo,
        data loss, UI wrong...)
  fix: a short suggestion
  difficulty: easy / hard / unsure - how hard the FIX is, assuming the bug is real. easy = a small local patch that
        keeps intended contracts, with a practical deterministic regression test (input validation, checked arithmetic,
        state/result cleanup, bookkeeping). hard = needs concurrency or lifetime architecture, realtime coordination, a
        design/API decision, platform/hardware/dependency work, or changes across components.
  confidence: high / medium / low

Report low-confidence suspicions too, marked low - they will be checked. At the end, one line: "TOTAL: <n> bugs". No
preamble, no summary beyond that.
```
**What makes it work:** the "known by design" list (it removes the most common false positives), a concrete `when`,
and permission to report `low` confidence findings.

**Angle texts** (`ANGLES` in `patrol.py`):
- **general:** "Any kind of bug."
- **threads:** "Concurrency and the realtime rule: data races between the audio thread and the interface, locks,
  allocation, logging or Qt calls on audio paths, unsafe statics, signal/slot thread affinity."
- **undo:** "Undo, redo and the command layer: steps that are not one undo step, inverses that cannot be replayed,
  journal checkpoints that miss state, transaction records that lie, the reversibility class."
- **lifetime:** "Object lifetimes and memory: dangling pointers, use after free, deleteLater timing, widgets parented
  to menus or dialogs that die first, raw pointers kept across an undo, leaks."
- **edges:** "Edge cases and input validation: empty, zero, negative, huge and NaN values, off-by-one, integer
  overflow, sentinels, refusal paths that change state, malformed project files."
- **ui:** "User-visible state: stale widgets, wrong labels or counts, actions that report success but do nothing,
  errors the user never sees, settings that do not persist or do not apply."

### A.2 Same-defect question (dedup band, classifier)
```
Do the two findings describe the SAME root cause in the SAME place? Shared file, theme, symptom or proposed fix alone is
insufficient. Distinct operations or independent defects count as different. Judge original claims regardless of
whether fixed.
```
Each finding's text is capped at 2,500 characters. A classifier-only repeat check joins two findings at a
probability of at least 0.85.

### A.3 Realness questions (classifier)
```
real (score):            How likely is the ORIGINAL finding a real bug, given the original code and intended contracts?
                         An intended behaviour or unsupported C++17 build assumption is not a bug. Later repairs do not
                         make an originally real bug false.
still_present (prob):    Does the current code still contain the defect this finding describes?
false_positive (prob):   Is the original finding a false positive, contradicted by the original code or an explicit
                         intended contract? Insufficient context alone is not evidence that the report is false.
already_fixed (prob):    Was the original defect real but demonstrably repaired in the current code? Require evidence of
                         a repair; do not equate an original false positive with already fixed.
```

### A.4 Difficulty questions, v2 (classifier)
```
easy (prob):    CONDITIONAL ON THIS FINDING BEING REAL: could its repair be a small local patch preserving intended
                contracts and unrelated behavior, with a practical deterministic regression or quick source/runtime
                refutation strategy? Correcting erroneous behavior is explicitly allowed. You are estimating repair scope,
                NOT verifying realness. ... Input validation, checked arithmetic, state/result cleanup and test/gate
                bookkeeping with a clear local strategy are usually easy. Thread ownership architecture, RT concurrency,
                design/API decisions, hardware/platform dependencies, or substantial cross-component redesign are not easy.
hard (prob):    CONDITIONAL ON THIS FINDING BEING REAL: would repairing it need concurrency/lifetime architecture, RT
                coordination, design/API decisions, platform/hardware/dependency changes, or substantial cross-component
                redesign? Do not confuse uncertain realness with repair difficulty. ...
context (prob): Is supplied context sufficient to estimate repair scope and a regression/quick-refutation strategy? It
                need NOT be enough to verify that the finding is real. ...
```

### A.5 Pairwise difficulty (classifier, `choice` A/B, asked in both orders)
```
These are two bug reports for a large C++20/Qt digital audio workstation (an LMMS fork with realtime audio threads,
plug-in hosting, undo, project files and an agent control API). Which bug is MORE DIFFICULT to fix correctly: it needs
deeper reasoning, realtime/concurrency or audio-DSP correctness, changes across several subsystems, or subtle edge
cases, so that a weaker coding agent would likely fail? Judge the difficulty of a correct fix with a regression test,
not the bug's importance.
  A: bug_a is harder to fix correctly      B: bug_b is harder to fix correctly
```

### A.6 Fixer brief (`fix_brief`), body
```
You are fixing ONE bug in Zene Studio (C++20 / Qt DAW, LMMS fork). You work alone.
Worktree: {wt}  (branch bugs/qwen-{id}). Work ONLY inside it. Its build tree {wt}/build is configured and `zene` is built.
LOG FOLDER: {logs}   (write every build/test log here as a NEW numbered file; search it with fff grep)
{rework: earlier reviews, if any}
## The finding (from Luna's patrol; historical line numbers)
{finding as JSON}
## Where to start
{likely files}

## Definition of done (reviewers send fixes back for exactly these)
1. A decision with evidence: REAL, ALREADY-FIXED or FALSE-POSITIVE. A not-real verdict cites the code (file:line) or the
   intended contract that shows it - an unproven "already fixed" is sent back.
2. A deterministic regression test that FAILS without your fix and PASSES with it (step 4b proves it). No wall-clock
   timing, sleeps or races in the test: inject timestamps, use a test seam or a synchronisation hook instead.
3. The fix is at the cause and complete: check every caller and every sibling of what you change (the matching command
   for many items, the other path into the same function, the wider type the value flows into) and keep them consistent.
4. No collateral damage: old projects and saved data load exactly as before; a limit or check you add must not hit other
   callers that legitimately exceed it; behaviour outside the bug does not change.
5. Realtime, undo and commands: nothing new on the audio thread allocates, frees, locks, waits or logs; undo stays one
   honest step; do not add new control commands for a bug fix.
6. Registration and gates pass (step 5).

## Procedure
1. Read the finding and the code it points at (line numbers may have moved). Search with the fff tools (path={wt}) or
   `rg -n` (output is capped).
2. Decide: is it REAL, ALREADY-FIXED on this branch, or a FALSE-POSITIVE? If not real, skip to step 6 with the evidence.
3. Write the regression test first (an existing test file for that area if one exists). Build it and run it from
   build/tests, logging both to the LOG FOLDER - it must FAIL (RED).
4. Make the smallest correct fix at the cause. Rebuild and rerun: it must PASS (GREEN), and the other tests in that
   ctest -R family must still pass. If you changed a header, rebuild every target that includes it.
4b. PROVE THE TEST CATCHES THE BUG: `git diff -- <the product files you changed, not tests> > {logs}/fix.patch && git
   apply -R {logs}/fix.patch`, rebuild, run your test: it must FAIL. Then `git apply {logs}/fix.patch`, rebuild, run: it
   must PASS. Record both commands and EXIT codes. If it passes without the fix, the test is wrong: fix it.
5. Gates: `python3 {ROOT}/feature_check.py {wt}` - one PASS/FAIL table (complexity and file-length only fail on items new
   in your files; they are red on the train on purpose).
6. Write {REPORT_DIR}/cluster-{id}.md: first line the verdict FIXED / ALREADY-FIXED / FALSE-POSITIVE / NEEDS-CONTEXT /
   BLOCKED, then the file:line evidence and the exact commands you ran with their EXIT codes (copy them; never claim a
   result you did not see). Commit it with the fix, message naming cluster {id}.
7. Leave `git status` clean. If you are stuck (platform, design or concurrency question), report NEEDS-CONTEXT or
   BLOCKED and explain - never guess a fix or fake a result.
8. Run the completion check: `python3 {ROOT}/submit_check.py {wt} {id}`. Fix every FAIL and answer every "?" item in a
   `## Self-check` section of the report with the evidence. The loop runs the same check when your session ends and
   sends unfinished work straight back to you.

## Rules and reference (condensed from AGENTS.md + plans/CLAUDE-READ-FIRST.md; open those only to look something up)
{RULES}
{lessons from recent rejections}
```

### A.7 Reviewer brief for a bug fix (`qa_one`)
```
You are the QA reviewer for ONE bug-fix attempt made by {author}. Review it independently.
Worktree: {wt}  branch {branch}. In this attempt {range text: whole branch from its base}
{fixer}'s own verdict: {verdict}; its report is {REPORT_DIR}/cluster-{id}.md in that tree.
This is a {code fix | claim with no code change (the fixer says the bug is not real / already fixed)}.
{batch note}{goalposts}{negative-control result}{re-review note}
Read {PROG}/AGENTS.md, then section 5 "Reviewing lane commits" of {CHECKLIST} - apply every check there.
The finding the fixer was given is in {runs}/{id}.r{round}/brief.md.

Verify, do not trust: read the diff (`git -C {wt} diff {base}..{head}`) against the code and the finding's real semantics.
For a fix: rebuild the touched targets (`cmake --build build --target zene <Tests>`), run the regression from build/tests
(`ctest -R <Test>`), and show it is RED without the product change: reverse only the non-test hunks in the working tree,
rebuild, run, then restore them with `git checkout -- <paths>` and rebuild. Unpiped exit codes.
Gates: run `python3 {ROOT}/feature_check.py {wt}` (unpiped EXIT). The complexity and file-length gates are red on the
train on purpose: they block ONLY when that table lists an item NEW in a file this change touches. A red direct
`--check` run is never by itself a reason for REWORK or DISPUTE, and never ask for a "parent disposition".
{REVIEW_LIMITS}For a claim: check the code yourself and decide whether the bug really is absent.

{commit rule: either "You must NOT commit ... git status must be clean", or the SMALL FIXES rule (A.9)}
Do not run the full suite or run-all-gates.sh. Never push.

{evidence rule (A.8)}Finish with a short review (what you checked, file:line, commands + EXIT codes), then a final fenced
block:
{"verdict": "<allowed verdicts>", "summary": "<one paragraph>", "blocking": [{"category": "<category>", "what": "<the
failure>", "evidence": "<command + exit code, or file:line>"}], "notes": ["<non-blocking observation>"],
"rework_instructions": "<concrete steps, if REWORK/DISPUTE>"}
```
**REVIEW_LIMITS:**
```
NEVER BLOCKING: (a) problems only in the report - a stale log path, an abbreviated or placeholder command, format or
wording - never block a fix whose code and tests you verified: correct the report yourself if small fixes are allowed
below, otherwise list them under ACCEPT-NOTE. (b) This machine is Linux only: never require a run on macOS, Windows or a
Qt 5 build. Check portability by reading the code - a compile error you can point to (file:line, API) still blocks;
hosted CI runs the other platforms.
```
**For a claim**, the reviewer's instructions end:
```
3. CONFIRM if the bug really is absent, DISPUTE (with the exact code and reason) if it is present.
```

### A.8 Evidence rule (`blocking_rule`)
```
REJECTIONS (owner rule: work goes back only when it actually fails; every reason must be visible and matter).
REWORK, REJECT or DISPUTE only for a concrete failure in one of these categories, each shown by evidence - a command with
its exit code and the failing output, or file:line where the failure is:
  - not-fixed: the bug, or an acceptance criterion, still fails
  - regression: something that worked before now fails
  - crash-or-data-loss: a crash, hang, corruption, or saved data lost or changed
  - realtime: the audio thread now allocates, frees, locks, waits, does I/O or logs
  - ci-compile: does not compile on a CI platform (Qt 5.15, MSVC, macOS 10.15, clang windows-arm64) - point at the API
  - test-proves-nothing: the regression passes with the product change reversed, or is not deterministic
  - weakened-check: a test, assertion, gate, ratchet or baseline is weakened, skipped or re-anchored
  - new-gate-item: feature_check.py lists an item NEW in a file this change touches
  - missing-path: an acceptance criterion has no working GUI or agent path
  - out-of-scope: behaviour outside the finding or unit changes
Anything else - style, naming, report wording, a concern you could not demonstrate, a "might" with no reachable case - is
not a failure: put it under "notes" and ACCEPT-NOTE. Put each blocking failure in "blocking" (one entry each); the loop
checks every entry and passes only those to the builder, so rework_instructions must address exactly them. A rejection
with no valid entry is sent back to you once; if it still names none, the work is accepted with your notes.
```
**On the re-ask**, this line is added:
```
YOUR PREVIOUS REVIEW OF THIS SAME ATTEMPT answered {verdict} without one evidenced failure in these categories (it said:
{summary}). Name the failure with evidence, or accept. If you again name none, the work is accepted with your notes.
```

### A.9 Reviewer small-fix rule (`qa_commit_rule`)
```
SMALL FIXES (owner rule): you may fix a SMALL blocking problem yourself instead of sending the work back: {when}
Small means: at most 40 changed product lines in at most 3 product files, plus at most 120 test lines in 4
test/registration files (reports excluded), within the same design, nothing new to invent - e.g. an overflow or null
guard, a missed count or assertion update, a Qt 5 / MSVC guard, a wrong doc sentence, a duplicated report row, a missing
registration, a one-function bug fix. NOT small (send those back): a missing GUI or agent path, a test that must be
redesigned around a new seam, a race or threading change, realtime-path logic beyond a one-line guard, or anything you
are unsure of. Never weaken, skip, disable or delete a test, assertion, gate, ratchet or baseline.
```
**`{when}` depends on the kind of work:**
- **A bug fix:** "whenever what stands between this attempt and ACCEPT is small - even if the focused tests fail as
  submitted - fix it yourself instead of REWORK/REJECT. After your fix the focused tests must PASS and the regression
  must FAIL with the product change reversed (and PASS restored)."
- **A claim:** "If you find it IS real (you would DISPUTE) and the fix is small, fix it yourself instead: write a
  deterministic regression test first and see it FAIL, make the fix at the cause, see it PASS, and prove it FAILS again
  with only the product change reversed."
- **A feature:** "only if the focused tests PASS on the branch as submitted - run them first; if they fail, REWORK."

### A.10 Feature verifiers (two stages)
```
You are the independent {CODE|FEATURE} verifier for ONE 0.5.0 feature unit built by another agent ({builder}). You did not
write it. Review it on its own merits.
CODE stage: correctness, regressions, realtime, undo, gate or ratchet, registrations (fork-sources, all-sources,
            upstream-modifications, CMakeLists), code smells, licensing of anything new.
FEATURE stage (the code verifier already accepted it): is EVERY acceptance criterion really met for a musician, proven by
            a test that exercises it; does the action exist in BOTH the GUI and the agent surface through the same
            registered command, with one undo step and an honest reversibility class; old projects still load;
            docs/manual updated; snapshot/canary regenerated if commands changed; no bloat or scope creep beyond the
            unit. Run the new tests yourself.
{+ the evidence rule, the small-fix rule and the same JSON verdict block}
```

### A.11 Train doctor brief (`train_doctor.brief`)
```
You are the train doctor for <product>'s automatic merge train.
The train merges accepted fix branches onto the fix base {base} as branch {train}, then builds and tests it. It has now
failed {n} times in a row the same way, and its own culprit search could not pin a merge:
  {why}
Your job is DIAGNOSIS ONLY. Name the merged branch (or branches, at most 3) that must come off the train so it builds and
passes, with evidence. The loop then takes them off and queues a port job for each, so a fixer re-applies that fix by
hand on the newest line - nothing is lost. Typical cause: two branches that are each fine alone but break together (one
changes a declaration, signature or behaviour; the other was written against the old one). Prefer the branch whose code
is stale against the other (usually the one written against older code), and the fewest branches.
You have a code-only checkout of {train} at {path} (no build tree). You may read and search it; do not edit, build or run
anything, and do not touch any other directory.
## Error excerpt   ## Files the error names   ## Merges that touch those files (with diffs)   ## Every merge
## Earlier doctor attempts on this failure
## Answer: end with exactly one fenced json block:
{"exclude": [{"branch": "...", "why": "one sentence with file:line evidence"}], "confident": true}
```

## Appendix B: thresholds and constants

| Constant | Value | Where | Meaning |
|---|---|---|---|
| slice size | 230 KB | `patrol.py --slice-kb` | code per finder call |
| `DATA_TABLE_BYTES` | 300,000 | `patrol.py` | larger files are data tables, skipped |
| finder workers | 3 | `patrol-run.sh` | slices read in parallel |
| finder usage ceiling | 50 % of the week | `patrol-run.sh` | the patrol stays stopped above it |
| dedup `LO` / `HI` / `T` | 0.84 / 0.94 / 0.40 | `dedup.py DEFAULTS` | cosine band; linkage cut |
| classifier repeat join | ≥ 0.85 | `classify.py` | same-defect probability |
| disposition cut-offs | 0.75 / 0.25; real 1.5 / 2 / 3 | `classify.disposition` | section 5.2 |
| difficulty v2 | easy ≥ 0.85 & hard ≤ 0.25 & context ≥ 0.75; hard ≥ 0.75 | `difficulty.label` | section 5.3 |
| root causes | `SIM_LINK` 0.80, `SIM_PATTERN` 0.72, `MAX_IDENT_SPREAD` 25, `MAX_FAMILY` 25, `CUT` 0.18, `JUDGE_THRESHOLD` 0.25 | `root_causes.py` | section 5.5 |
| root-cause job size | 6 | `ROOT_CAUSE_MAX` | bugs per systemic job |
| same-file batch | 3 | `BATCH_MAX` | bugs per job |
| local-model share | percentile ≤ 0.5 | `STRATA_MAX_PCT` | the easier half |
| feature tier cuts | 0.65 / 0.9 | `FEATURE-TIER-CUTS.json` | middle / Sonnet / Opus |
| rework rounds | 4 | `adaptive.max_rounds` | before escalation |
| model rest after a transient failure | 900 s | `COOLDOWN` | per model |
| first event / quiet | 240 s / 1500 s | `FIRST_EVENT_S` / `QUIET_S` | harness watchdog |
| session silence / long-run notice | 45 min / 6 h | `SESSION_QUIET_MIN` / `SESSION_LONG_H` | driver watchdog; no wall-clock cap |
| review backlog slow / hold | 12 / 24 | `AUTOSCALE.json` | fixers halved / stopped |
| RAM floor | 4 GB | `AUTOSCALE.json` | no new sessions below |
| reviewer sessions | 1 per pending review, 1–6 | `luna_review_*`, `LUNA-CAP` | autoscaled |
| reviewer small fix | 40 product lines / 3 files; 120 test lines / 4 files; 2 commits | `QA-FIX.json` | `ACCEPT-FIXED` limits |
| evidence regex | `[\w./+-]+\.\w+:\d+` or `(EXIT\|exit\|rc\|ctest)\D{0,4}-?\d+` | `EVIDENCE_RE` | a valid rejection entry |
| train cadence / max wait | 3 h / 24 h | timer, `MAX_WAIT_H` | when a train is due |
| train reverts | 6 | `MAX_REVERTS` | per train run |
| doctor streak / exclusions | 2 / 3 | `train_doctor.py` | when it starts / how much it may remove |
| paid-session cap | per model | `PAID_SESSION_CAP_USD` | the harness wrapper stops a session past it |
