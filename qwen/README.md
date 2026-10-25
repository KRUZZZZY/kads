# Qwen fixer + Luna QA (Zene bug queue)

## Current state (2026-10-25) - read this first

The sections below are history from 2026-10-12; much of it no longer holds (local Qwen/Strata is paused, Haiku
reviews are off, models and lanes changed almost daily). What is current:

- **Version control:** this whole tree (`~/.local/share/zene-bunny-fix`) is a git repo: code, docs and the live knob
  files. `.gitignore` keeps runs, logs, state, caches, backups and secrets (`sb-config.yaml`) out. Commit knob and
  code changes; stop making `*.bak-*` copies.
- **Services** (user systemd; unit copies in `../systemd/`): `zene-fix`, `zene-qa` (graceful restart = touch
  `STOP-FIX` / `STOP-QA`), `zene-scope`, `zene-patrol` + `zene-triage` (held by `PATROL-HOLD`), timers
  `zene-usage-monitor` (10 min), `zene-merge-train` (3 h), `zene-autopilot` (hourly), `zene-digest` (09:00).
- **Workflow rules** (one issue ledger across models and ports, budgets, integration requests): `WORKFLOW.md`,
  policy `WORKFLOW.json`. Methodology audit: `audits/methodology-2026-10-24/findings.md`.
- **Who does what** is in the live knobs: `LUNA-CAP` (Luna ceilings), `LANES.json` (Claude lanes),
  `MULTI-SESSION.json` / `OPENCODE-FREE.json` / `BUG-PREFER.json` / `FEATURE-TIERS.json` (OpenCode/DeepSeek/MiniMax
  routing), `DSH-HARNESS.json` / `MCODE-HARNESS.json` (harness per model).
- **Luna pacing** (`luna_pace.py`, 2026-10-25): the usage monitor writes `LUNA-PACE.json`; it lowers `LUNA-CAP` so the
  Codex week lasts to its reset (scoping shed first, then Luna fixers, reviews last, never below 1) and keeps Luna
  off fixing while DeepSeek has quota. Off: `LUNA-PACE-OFF`.
- **Fresh branches** (`qwenfix.freshen`, 2026-10-25): a reused fix branch is merged up to `FIX-BASE` before each round;
  conflicts go to the same session. Off: `FRESHEN-OFF`.
- **Merge train** (`merge_train.py`): due at 25 waiting or after 24 h, takes the oldest 40, skips when nothing is
  waiting, recovers a tree left on a killed attempt. Train doctor: `train_doctor.py`.
- **Full design** of the find-to-merge pipeline: `plans/PIPELINE-FIND-TO-MERGE.md` in the program repo.

## History (2026-10-12 onwards)
### Operation as of 2026-10-12

Four Luna fix sessions, one local Qwen coder and one OpenCode free worker share one coordinator and one
set of claimed bugs. `LOCAL-QWEN` enables the extra local session; remove it
to stop assigning new local work after its current bug finishes. Luna remains
the default engine in `start.sh`, with two independent Luna reviewers.

Qwen continues during a Luna usage-limit pause. There is no pending-review cap;
coding does not pause to let QA catch up. Reviews wait for Luna rather than sharing Qwen's model.
The local worker yields its GPU to Clef between bugs when triage requests it.
Builds share two slots (`BUNNY_SLOTS=2`) and two compiler jobs per build while
the local worker is enabled; fewer than 3 GB available RAM pauses
new fix sessions, and the existing 40 GB free-disk floor remains enforced.
Every result still needs regression evidence and independent QA acceptance.
The model is the already-installed coder IQ1_M on localhost port 8081;
enabling it does not download a second model.

`OPENCODE-FREE.json` enables one additional cloud worker rotating
`opencode/mimo-v2.6-flash-free` and `opencode/space-bunny-free`.
Ling Flash is excluded at the owner's request. It takes fresh bugs from the same claimed queue;
Luna handles rework and independent QA. Set `enabled` to `false` to stop assigning
new free-model jobs after the current one finishes. No separate coordinator is needed.
The wrapper pins all auxiliary agents to the selected free model, restricts the
provider/model list, and verifies active tool support and zero input, output and
cache prices against refreshed CLI metadata and the live OpenCode model list.
It checks hourly and fails closed if verification is unavailable. It never falls
back to paid models. Provider limits pause the worker without spending a bug
attempt; another verified free endpoint is tried only while the tree is unchanged.
`runs/<cluster>.r<n>/opencode-model.json` and `opencode-*-usage.json` record the
selected model and reported step costs; stdout tool events stay in `session.log`.
Free coding continues during Luna quota pauses with no QA backlog limit.
Its only MCP server is the existing local fff search proxy, scoped to its worktree.
Luna reviews still consume the Codex allowance. Regression and integration gates
are unchanged. Connection probes passed for both models and a native tool
probe exercised file creation and Python execution with zero reported cost.

The older sections below describe the original standalone Qwen setup.

Owner update, 2026-10-12: local Qwen is paused after its current fix to release
the GPU. `QWEN-PAUSED` blocks local fixing, review fallback and model startup;
`LOCAL-QWEN` is saved as `LOCAL-QWEN.paused`. Resume only on the owner's request
by removing `QWEN-PAUSED` and restoring `LOCAL-QWEN`.

The local Qwen model (Strata, `http://127.0.0.1:8080`) fixes Zene bugs one at a time. Each result
then waits in a QA queue, where GPT 6.0 Luna (codex, max effort) reviews and retests it.

```
bash ~/.local/share/zene-bunny-fix/qwen/start.sh        # both loops, detached (the fixer starts Strata)
python3 ~/.local/share/zene-bunny-fix/qwen/qwenfix.py status
touch ~/.local/share/zene-bunny-fix/qwen/STOP           # both loops finish their current item, then exit
                                         # (STOP-FIX / STOP-QA stop one loop only)
```

## What happens to one bug
1. **Pick:** the next cluster. Reworks go first. Then the queues in this order: the 10-06 Clef
   EASY queue, Clef's EASY bugs from new patrol passes (`research/reviews/luna-new-triage/`, written
   by `tools/luna-patrol/triage_new.py`), not-yet-classified bugs, then needs_context, then hard.
   Clusters Luna-easy already attempted are skipped.
2. **Worktree:** `zene-qwen-<cluster>` on a new branch `bugs/qwen-<cluster>`, created from the tip of
   the bug's lane worktree. It is configured with that lane's own CMake options (WASM excluded),
   and `zene` is built through ccache, in one of the 4 shared build slots.
3. **Fix:** one `mcode exec` session on Strata's Qwen (at most 250 steps), with the bunny shims on
   PATH and fff as its only MCP server. The brief is kept short (~1,200 tokens) to avoid context
   rot. It contains a condensed, hand-checked digest of the rules in AGENTS.md and
   CLAUDE-READ-FIRST (`RULES` in `qwenfix.py`; update it when those files change), and a trimmed
   copy of the finding. The brief asks it to reproduce the bug (RED), fix it (GREEN), pass the two
   ratchet gates, and commit a report to `docs/qwen-fixes-2026-10/cluster-<id>.md`.
   - A session that commits nothing gets one retry, then the bug is parked.
4. **QA queue:** the result goes to `qa/pending/` without a backlog cap on coding.
   Luna reviews it against section 5 of `plans/HANDOVER-2026-10-05.md`, reruns the targeted
   build, the test and the gates, and checks that the test is RED without the fix.
   Luna is not allowed to commit.
   - **ACCEPT** / **CONFIRM:** the branch is listed in `ACCEPTED.tsv` and the worktree is removed;
     the branch stays for the merge train.
   - **REWORK** / **DISPUTE:** sent back to Qwen with Luna's notes, up to 2 rounds in total.
   - **REJECT**, out of rounds, or broken: moved to `parked/`. The worktree is kept for a human;
     its build is deleted.

Nothing is pushed and nothing is merged. Landing accepted branches is the normal merge-train job
(AGENTS.md).

## Files
`logs/{fix,qa}.log` (one line per event) · `runs/<cluster>.r<n>/` (brief, build.log, session.log,
final.md, QA brief and session) · `qa/reviews/` (Luna's reviews) · `state/` (one record per
cluster) · `ACCEPTED.tsv` · `parked/` · `qwen-mcode.yaml` (generated from Strata's `/health`).

## Knobs (environment variables)
`QWEN_MAX_ROUNDS` (2) · `QWEN_BUILD_JOBS` (6, the first zene
build) · `QWEN_SESSION_JOBS` (4, builds inside sessions) · `QWEN_FIX_TIMEOUT` (150m) ·
`QWEN_MIN_FREE_GB` (40) · `QWEN_KEEP_ACCEPTED_TREES=1` (keep accepted worktrees, without their build).

## Keeping Qwen's prompt small
Qwen runs mcode with its own lean data folder (`mcode-data/`: fff is the only MCP server, no skills,
plugins, agents or integrations; `skills: []` in its config). Requests go through `trim_proxy.py` on
:8092, which drops the 12 tools a bug fix never uses (subagent tasks, website deploy, goals, web
search/fetch, skill, todos), the skills catalog and the media-output section. Each line of
`logs/trim.log` records what was removed. Measured starting prompt: 16.1K tokens with the default
setup, 13.5K with the lean folder, and **~6K through the proxy**. With 8K reserved per reply, that
leaves about 51K of Strata's 64K for each bug. The fixer starts the proxy if it isn't running.

## Sharing the GPU with Clef
Clef (Ollama) and Strata can't share the 16 GB card: Strata alone uses ~15.4 GB. `triage_new.py`
writes `GPU-REQUEST`. The fixer then finishes its current bug, stops Strata, and writes `GPU-FREE`. If
no fixer is running but Strata is, the triage stops Strata itself and restarts it afterwards. When the
triage is done it unloads Clef with `ollama stop`, since Ollama would otherwise keep it in VRAM for
5 minutes. The fixer also unloads Ollama models and waits for free VRAM before starting Strata. If the
requester dies, the fixer takes the GPU back. Strata listens on :8081, because SearXNG owns :8080.
Measured swap: Strata stopped in 4 s, Clef loaded and answered at 16 s, and Strata was serving again 35 s
after Clef was unloaded.

## Caveats
- **RAM:** Strata keeps about 13 GB of the model in RAM on this 31 GB machine. Builds share what's
  left, which is why build parallelism is capped. If Strata reports "engine stopped unexpectedly",
  lower `QWEN_SESSION_JOBS`.
- **Context:** 64K is tight for an agent working in this codebase. Raise it with
  `./setup.sh --setup --context 131072` in the Strata folder (this re-downloads nothing).
- **Luna-easy driver:** don't run `tools/bunny-fix/luna_easy_driver.py` at the same time. Both
  read the same queue.
