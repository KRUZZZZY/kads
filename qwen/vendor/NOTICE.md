# Sources of ideas ported into the Zene fix pipeline (research/code-factories, 2026-10-25)

No third-party source code is copied into this repository by these ports. Each module is a clean-room
implementation of an idea described in public documentation or papers; the origin is credited here and in the
header of the module. If code is ever vendored later, list it here with its licence and the commit it came from.

| Module | Idea | Where it comes from | Licence of the source |
|---|---|---|---|
| `stuck_detector.py` | detect an agent that repeats the same action/observation, repeats the same error, or ping-pongs between two actions | OpenHands "Stuck Detector" (software-agent-sdk, docs.openhands.dev/sdk/guides/agent-stuck-detector); thresholds 4 / 3 / 6 cycles are theirs, the log-line based algorithm is ours | MIT |
| `conflict_forecast.py` | rank queued changes by how likely they are to conflict with the moving head and with each other; promote a train early when a queued change goes stale | Uber SubmitQueue "Keeping Master Green at Scale" (EuroSys 2019, conflict analyzer); Zuul speculative gating; Shopify River "freshness-gated merge queue" (shopify.engineering, 2026-09-02) | paper / Apache-2.0 (Zuul) / blog |
| `review_diversity.py` | the reviewer must come from a different provider or model family than the author | "Cross-Provider Review as a Runtime Contract for Coding Agents" (arXiv 2610.10961); AscendyProject/redteam (independent reviewer harness); the existing `fixed_by_haiku` rule in `qwenfix.py` is the local precedent | Apache-2.0 (redteam) |
| `scope_guard.py` | mechanical scope-drift check on a patch before review: product files outside the neighbourhood of the finding, oversize patches | Google CodeMender (patches are validated for "no regressions and minimal change" before human review, deepmind.google/blog/introducing-codemender); SWE-agent / Agentless minimal-patch practice | MIT (SWE-agent, Agentless) |
