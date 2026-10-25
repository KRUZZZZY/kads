# zene-studio-fix-pipeline

**An automated delegation surface for coding-agent harnesses.** You define the roles (finder, triager, fixer,
reviewer, integrator), point each role at whichever agent harness and model you want, and the pipeline runs the
work between them unattended: queue, routing, budgets, escalation, mechanical checks, independent review and merge
trains. A human is needed only when every agent on the escalation ladder has failed a job.

It does not contain a model or an agent of its own. It is the control plane *around* agents. Today it drives Claude
Code, OpenAI Codex, OpenCode, MiniMax Code, a DeepSeek harness and local Ollama models side by side, each as one
or more "lanes" with its own quota, pacing and failure handling.

> **Status: reference implementation, not yet repo-agnostic.** It was built for one repository,
> [Zene Studio](https://github.com/KRUZZZZY/zene-studio) (a C++/Qt DAW). Paths, build and test commands, gates and
> lane definitions are still wired to that repo and one workstation. The goal is to change that: open the pipeline,
> pick an agent per role, point it at a repo, and get a bug tracker and queue for that repo. The work towards that is tracked in
> [issue #1](https://github.com/KRUZZZZY/zene-studio-fix-pipeline/issues/1).
> Until it lands, read this as a working design with working code you can lift from.

## What the surface does

```
            ┌──────────── roles (each delegated to any harness + model) ────────────┐
 repo  ──►  │ Finder ──► Triage ──► Queue/Router ──► Fixer ──► Checks ──► Reviewer   │ ──► Merge train ──► new base
            │  read-only   dedup,     difficulty,     own      submit,    evidence-  │     build + test all
            │  sweeps      realness,  urgency,        worktree negative   only       │     together; doctor
            │              ranking    per-model stats          control    verdicts   │     agent on repeats
            └───────────────── rework ◄── escalation ladder ◄── rejection ───────────┘
```

For each role it supplies:

- **A brief.** Each role starts in a fresh session with its own prompt (see Appendix A of the blueprint) and a
  clear definition of done.
- **A harness adapter.** It starts the session, sandboxes it in its own git worktree, watches it (silence timeout,
  stuck detection), and reads back a structured report.
- **Routing.** The job goes to the cheapest lane whose measured acceptance rate at that difficulty is good enough.
  Measurements are kept continuously per model (`benchmark.py`).
- **Budget handling.** Usage limits, provider outages and rate limits rest the lane and requeue the job. They never
  count as a failed attempt.
- **Escalation.** Failed work climbs a ladder of stronger models instead of being parked.
- **Mechanical checks before any judgement.** Commits exist, tests changed, gates pass, the patch stays in scope,
  and a negative control requires the new test to *fail* with the fix reverted.
- **Independent review.** A reviewer from a different session, and optionally a different model family, re-proves
  the fix. Rejections count only with evidence (`file:line` or a command and its exit code).
- **Integration.** Accepted branches merge in trains that must build and pass the full suite against a
  known-failures baseline before becoming the new base. A "train doctor" agent diagnoses trains that keep failing.
- **Operator messaging on any platform.** Alerts, the daily digest and escalations go through one adapter
  (`qwen/notify.py`). Pick the backend in `qwen/NOTIFY.json`: any command, a Slack/Discord/generic webhook, ntfy,
  Telegram, a log file, or several at once. Tokens come from environment variables, never from the file.
- **A journal of everything.** Decisions go to JSONL ledgers. Every routing knob is a small file read fresh each
  loop, so policy changes need no restart.

## Design rules

Most of these came from a failure in production:

1. Finders never fix, fixers never review their own work, reviewers are independent.
2. Anything a script can decide is decided before a model sees it.
3. Rejections need evidence. "Might" and style comments are notes, not blockers.
4. A classifier's "unsure" becomes "needs context", never "not a bug".
5. Infrastructure failures are not attempts.
6. Escalate instead of parking.
7. Never weaken a check: skipping or loosening a test, gate or baseline is its own rejection category.
8. Integration is a separate, mandatory test.
9. Everything is journalled, resumable and live-configurable.
10. Cheap models first, measured continuously; routing follows the numbers.

## Using it today

Three ways in, from least to most effort:

1. **Take the design.** [docs/BLUEPRINT.md](docs/BLUEPRINT.md) describes every stage independently of any model:
   algorithms, thresholds, prompts, failure modes and a build order. Section 19.1 is a minimum viable version you
   can build on your own stack in a week or two. Section 12 maps it onto a web/SaaS codebase and a Claude-only
   setup.
2. **Lift single modules.** These are short and close to standalone: `negctl.py` (negative control),
   `submit_check.py` (pre-submit checklist), `scope_guard.py`, `stuck_detector.py`, `review_diversity.py`,
   `conflict_forecast.py`. Swap in your build and test commands.
3. **Run the whole thing.** Possible, but expect to edit paths, lanes and build/gate commands by hand (see
   `qwen/README.md` and `qwen/WORKFLOW.md`). This is the part [issue #1](https://github.com/KRUZZZZY/zene-studio-fix-pipeline/issues/1) sets out to remove.

### Choosing where notifications go

With no `qwen/NOTIFY.json`, messages go to a `notify-me` command, as in the reference deployment. For anything else:

```json
{"backends": [
  {"backend": "webhook", "url_env": "SLACK_WEBHOOK_URL", "field": "text"},
  {"backend": "file", "path": "logs/notify.log"}
]}
```

Other backends: `{"backend": "webhook", "url_env": "DISCORD_WEBHOOK_URL", "field": "content"}`,
`{"backend": "ntfy", "topic_url_env": "NTFY_TOPIC_URL"}`,
`{"backend": "telegram", "token_env": "TELEGRAM_BOT_TOKEN", "chat_id_env": "TELEGRAM_CHAT_ID"}`,
`{"backend": "command", "command": ["your-pager", "--to", "ops"]}`, and `{"backend": "none"}`. The file is read on every
message, so a change applies at once. Check what is configured with `python3 qwen/notify.py --check`.

**Never give the agents production access or production data.** Each job needs its own sandbox, with push blocked
and only scoped tokens.

## Layout

| Path | Role in the pipeline |
| --- | --- |
| `docs/BLUEPRINT.md` | The full design |
| `patrol/` | **Finder and triage**: read-only sweep, dedup, realness classification, difficulty and urgency ranking, root-cause families |
| `qwen/` | **Dispatcher** (the name is historical; the first fixer was a local Qwen model): routing, harness lanes, fix and review loops (`qwenfix.py`), mechanical checks, merge train, train doctor, usage pacing, autoscaling, benchmark, tests, and the live knob files |
| `bunny/` | Build-slot jobserver and command shims, so parallel agents can't exhaust RAM or CPU |
| `dsh/`, `mcode/` | Harness configs for the DeepSeek and MiniMax Code runners |
| `status/` | Live status view |
| `systemd/` | The units and timers that keep the loops running |

## Names you will see in the code

The code uses names from the reference deployment. They are all roles or lanes, not dependencies.

| Name | What it is | Generic role |
|---|---|---|
| Zene, LMMS | the target repository and its upstream | *your repo* |
| Luna | GPT 6.0 Luna through the Codex CLI | strong model lane (finder, reviewer) |
| Clef | a small local model served by Ollama | classifier |
| Strata, Qwen | local models on a home server | last-resort / overnight fixer |
| OpenCode, DeepSeek, MiniMax | cheap hosted coding models in their own harnesses | default fixer lanes |
| Haiku, Sonnet, Opus | Claude Code lanes | easy / normal / hard-tail fixer lanes |
| owner | the human operator | *you* |

Tests: `cd qwen && for t in test_*.py; do python3 $t; done`. `test_opencode_free.py` and `test_parallel_workers.py`
read live state, so they pass only on a configured machine. API keys are never in this repository.

## Credits and licence

Several modules are clean-room ports of ideas from OpenHands, Uber SubmitQueue, Zuul, Shopify, Google CodeMender,
SWE-agent and others; see [qwen/vendor/NOTICE.md](qwen/vendor/NOTICE.md). No third-party code is copied in.

MIT licensed, see [LICENSE](LICENSE). Issues, ideas and write-ups of your own adaptations are welcome.
