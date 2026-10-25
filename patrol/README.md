# luna-patrol

GPT 6.0 Luna (codex, max effort) reads the Zene Studio code base for bugs, pass after pass, until stopped. Started 2026-10-03 at the owner's request ("Luna models perma scrolling through the repo, finding bugs").

**What it reads**
- Each pass snapshots the product worktree's committed tip (`../../zene-survey`, `git show`), so uncommitted edits are never read.
- It packs three kinds of file into slices of about 230 KB:
  - the fork's own files, whole and with line numbers;
  - the inherited files the fork changed, as diffs against upstream `4e677cb6c6ab`;
  - since 2026-10-07, the upstream LMMS files the fork never changed, whole and with line numbers (601 files, about 17 extra slices). Zene ships that code too.
- Pass size is now about 93 slices.
- `--no-upstream` restores the fork-only sweep (about 76 slices).
- Bundled third-party libraries (Eigen, rtneural, rnnoise, calf/caps/cmt/swh/tap, resid, adplug, exprtk and the like) are left out unless you pass `--with-vendored`. Single source files over 300 KB (weights and lookup tables) are always left out.
- Three slices run at a time. A pass takes about 4-6 hours.
- Slice boundaries shift every pass, and the angle rotates: general, threads and realtime, undo, lifetimes, edge cases, UI state.

**What it writes:** findings only, never fixes, under `research/reviews/luna-patrol/`.
- `pass-NNNN-<sha>-<angle>/slice-NNN.md`: raw reports.
- `findings.jsonl`: every finding, with a `duplicate` flag keyed on file and title words.
- `STATUS.md`: progress, one line per slice.

Every finding still has to be checked against the code before it's fixed. Earlier rounds found many false positives.

**Controlling it**
- Stop: `touch research/reviews/luna-patrol/STOP`. It finishes the slices in flight, then exits.
- Restart: delete `STOP`, then `setsid nohup python3 tools/luna-patrol/patrol.py --workers 3 > research/reviews/luna-patrol/patrol.log 2>&1 < /dev/null &`.
- Running? Check the pid in `research/reviews/luna-patrol/patrol.pid`, or `pgrep -f patrol.py`.
- Options: `--branch NAME` (default `HEAD`), `--workers N`, `--slice-kb N`, `--passes N` (0 = until stopped).

**Classifying repeats and false positives with local Clef**

`classify.py` calls the existing local Clef-Flash instance at
`http://127.0.0.1:11434/v1/systemone`. It snapshots all findings, including those
flagged as duplicates, and pins the product code to the supplied commit. It
compares historical sweep code with that commit, supplies matched earlier fix
report evidence, and asks Clef to distinguish likely open defects, original
false positives, already-fixed defects, and cases needing more context.

```bash
python3 tools/luna-patrol/classify.py \
  --repo zene-luna-r7 --revision b81a13ec8 \
  --findings research/reviews/luna-patrol/findings.jsonl \
  --reviews research/reviews/catchup-2026-10-04/fix-reports \
  --out research/reviews/luna-clef-2026-10-05
```

Re-running the same command resumes its append-only decision files. Mixing
different findings snapshots, code revisions, model versions or review inputs
is refused. Each request judges one finding or one candidate repeat pair.
The finding uses the existing classifier's descriptive question names (`real`,
`still_present`) plus `false_positive` and `already_fixed`; pairs use `same`.
Trial requests batching unrelated findings under numbered question keys failed
known-case calibration, so their results are excluded.
Model/context errors stop the process; they never count as verdicts.

TF-IDF proposes up to three earlier neighbors in the same resolved file; Clef
then decides whether each pair describes the same root cause. A repeat requires
probability >= 0.85. An open suggestion requires realness >= 3/4, present >= 0.75
and false-positive <= 0.25. A false-positive suggestion requires realness <= 1.5
and false-positive >= 0.75. Already-fixed requires realness >= 2, present <= 0.25
and already-fixed >= 0.75. Everything else stays `needs_context`; unresolved
source files also stay there. The report includes raw probabilities
and candidate clusters. These thresholds are conservative filters, not
calibrated guarantees. Cross-file repeats and candidates below the similarity
threshold may remain, so cluster counts are not verified unique-bug counts.

The old `triage.jsonl` and `TRIAGE.md` are preserved. No finding is automatically
deleted, suppressed or declared verified; reproduce a candidate before fixing
it and review false-positive suggestions against the actual code.

**Fixing in batches**

Group related defects into one lane. Each defect needs a regression that fails
on the baseline; during coding, rebuild and run only the affected suites from
`<build>/tests`. Shared tests can cover several related fixes in one run.
Run the full suite and all gates once on the finished lane and once at its
merge point, as required by AGENTS.md rule 2a. Documentation-only updates do
not justify another full run. Repeat a check when a change, failure or unresolved
concern makes it necessary.

Before the independent build, compare optional features with the source lane's
configuration. This machine's AAF and FFmpeg development libraries are under
`~/.local/zene-deps/root/usr/lib/pkgconfig` and
`~/.local/zene-deps/root/usr/lib/x86_64-linux-gnu/pkgconfig`; include both in
`PKG_CONFIG_PATH` for CMake. Missing these caused eight checked-command gaps in
the first 2026-10-05 build despite all 400 tests passing. Preserve the coverage
ratchet and correct the configuration before the batch gate run.

**Deduplicating findings (measured)**

`dedup.py` groups repeat reports into one cluster per defect: it compares findings within a file by local
embedding (`nomic-embed-text`), asks Clef only about the ambiguous band (cosine 0.84–0.94), never merges
two findings from the same patrol pass, and agglomerates with average linkage. On a held-out hand-labelled
set it scores pairwise P 0.988 / R 0.992; the old Clef clusters scored P 1.000 / R 0.633. Method, gold and
error analysis: `research/reviews/luna-dedup-2026-10-05/REPORT.md`.

```bash
python3 tools/luna-patrol/dedup.py \
  --findings research/reviews/luna-clef-2026-10-05/findings-snapshot.jsonl \
  --out research/reviews/luna-dedup-2026-10-05/run-v2 \
  --gold research/reviews/luna-dedup-2026-10-05/gold/test2-gold.json
python3 tools/luna-patrol/dedup_eval.py --gold <gold.json> --clusters <clusters.jsonl> [--multilabel] [--errors 20]
python3 -m unittest discover -s tools/luna-patrol -p 'test_dedup.py'
```

Re-runs resume from `embeddings.npz` and `pair-judgements.jsonl` (both keyed by content hash). Before
changing a parameter, score it on `gold/dev2-gold.json`; keep `test2` for a single final check, and label a
fresh held-out set if test2 has informed a change.

**Triaging each new patrol pass (`triage_new.py`)**

After a patrol pass completes, `triage_new.py` has local Clef dedup, check and grade that pass's new
findings (those not in the frozen 10-05 snapshot). It then publishes a fix queue under
`research/reviews/luna-new-triage/`. The Qwen fixer reads `queue/*.jsonl` as its `new:*` tiers. The tool
imports `classify.py`, `dedup.py`, `difficulty.py` and `difficulty_v2.py`; it neither modifies them nor
writes to their output directories.

```bash
python3 tools/luna-patrol/triage_new.py --dry-run   # what would be triaged; no Clef, no GPU handshake, writes nothing
python3 tools/luna-patrol/triage_new.py --once      # every completed, untriaged pass, then exit
python3 tools/luna-patrol/triage_new.py --watch     # poll every 10 min; exits once the patrol's STOP exists and nothing is pending
python3 -m unittest discover -s tools/luna-patrol -p 'test_triage_new.py'
```

- **Pass complete:** STATUS.md has `pass N finished` (patrol.py:177, written after every slice is recorded).
  A pass with no `finished` line also counts as complete, because nothing will write to it again, once:
  - a later pass has started (`superseded`, like pass 7), or
  - `patrol.pid` no longer names a live `patrol.py` (`abandoned`).
- **Dedup first:** a new finding is queued only if it repeats none of the following:
  - (a) the 10-05 clusters
  - (b) the 10-06 inventory, including `review-*` items
  - (c) clusters this tool made from earlier passes, whether queued or dropped
  - (d) bugs in the Qwen fixer's hands (`state/*.json`, with the finding text from the latest `runs/<cluster>.r<n>/brief.md`)
  - (e) the patrol's own `duplicate` flag

  The method is `dedup.py`'s: nomic-embed-text cosine, Clef on the 0.84-0.94 band, and average linkage at
  T=0.40. It never joins two findings of one pass. Known clusters stay fixed, and a new finding either joins
  one or starts `new-p<pass>-<n>`. Each repeat's journal row names the cluster it joined and that cluster's sources.
- **Then Clef:** new clusters get `classify.py`'s realness questions and thresholds, pinned to the revision
  the pass read (resolved in `zene-ui`). Those that come out `likely_open_bug` or `needs_context` also get
  the v2 difficulty questions (`difficulty_v2.QUESTIONS`, `difficulty.label`).
- **Luna's own rating:** a finding's `difficulty: easy/hard/unsure` (patrol.py stores it as a field from pass 9;
  for older findings it is parsed from `text`) is carried as `luna_difficulty` on every journal row and queue
  item, and is empty when absent. Clef never sees that line, and Clef's own v2 call (`difficulty`) still routes
  the queue. `difficulty_agree` is true or false only when both sides said easy or hard, and null otherwise.
  COUNTS.json tallies it under `luna_vs_clef_difficulty`.
- **GPU:** a run that needs a model first writes `~/.local/share/zene-bunny-fix/qwen/GPU-REQUEST`
  (`<pid> <time>`). It then waits for a fresh `GPU-FREE`, or for `fix.pid` to name no live qwenfix. It
  makes all its calls in that one window, then deletes its request, including on an exception or SIGTERM.
- **Outputs:**
  - `queue/{easy,needs_context,hard}.jsonl`: atomic, one item per new cluster. Each item has the 10-06
    inventory fields (`cluster`, `members`, `finding_id`, `lane`, `worktree`, `revision`, `title`, `file`,
    `line`, `text`, `report_evidence`), plus `realness`, `difficulty`, the raw Clef answers, the model
    digests and both context SHA256s.
  - `decisions.jsonl`: the append-only journal of every new finding's outcome; dropped findings carry `reason`.
  - `realness.jsonl`, `difficulty.jsonl`, `pair-judgements.jsonl`: Clef answers, used to resume.
  - `TRIAGED-PASSES`, `COUNTS.json`, `metadata.json`, `model.json`.
- **Resume and refusals:** a rerun skips triaged passes, and a pass interrupted mid-run is redone from the
  journalled Clef answers. A model error stops the run and never counts as a verdict. A changed Clef or
  embedding digest, or changed questions or thresholds, is refused.

## Direct scorers and retriage v2 (2026-10-25)

Measured in `research/reviews/bug-urgency/REPORT.md`. Clef's per-call overhead (~0.3-0.5 s) dwarfs its per-question cost
(~0.08 s) and the server runs one slot, so the fast pattern is ONE request per bug with SEVERAL questions
(`clef_client.post`), not pairwise sorts.

- `bug_urgency_sort.py`: urgency (impact x likelihood x reach) of every queued bug, nine questions + a path prior and
  weights fitted on the train half of `research/reviews/bug-urgency/gold.json`. Writes `qwen/BUG-URGENCY.json`
  (`{cluster: pct}`, 1 = most urgent). Eval: `bug_urgency_eval.py`.
- `bug_difficulty_direct.py`: direct difficulty (five questions, one call per bug); the mean tracks fix outcomes at least
  as well as the pairwise sort at ~10x fewer calls. Writes `qwen/BUG-RANK-DIRECT.json` (0 = easiest); never overwrites
  `BUG-RANK.json`.
- `retriage_v2.py`: turns the journalled `still_present` answer into `qwen/CLEF-OPEN-SCORE.json` (an ordering/skip
  signal) and can re-ask the suspicious tail on the current fix base (`--refresh N`).
- Tests: `test_bug_urgency.py`, `test_bug_difficulty_direct.py`, `test_retriage_v2.py` (fake model calls).
