#!/usr/bin/env python3
"""Offline tests for triage_new.py (no Ollama, Clef or network; a throwaway git repo in a temp dir)."""
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

import triage_new as tn

HERE = pathlib.Path(__file__).resolve().parent


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class FakeModels:
    """Embeds by title (same title -> cosine 1, else 0); Clef answers chosen by title words."""

    def __init__(self, digest="d1", fail=False):
        self.digest, self.fail, self.calls, self.topics = digest, fail, [], {}

    def pins(self):
        return {"clef": {"name": "clef-flash:latest", "digest": self.digest},
                "embed": {"name": "nomic-embed-text:latest", "digest": "e1"}}

    def embed(self, items):
        self.calls.append(("embed", len(items)))
        X = np.zeros((len(items), 64))
        for row, f in enumerate(items):
            title = f["title"]
            if title.endswith(" v2"):  # cosine 0.9 to its base title: inside the Clef band
                X[row, self.topics.setdefault(title[:-3], len(self.topics))] = 0.9
                X[row, self.topics.setdefault(title, len(self.topics))] = (1 - 0.81) ** 0.5
            else:
                X[row, self.topics.setdefault(title, len(self.topics))] = 1.0
        return X

    def ask(self, state, questions):
        self.calls.append(("ask", tuple(sorted(questions))))
        if self.fail:
            raise RuntimeError("Clef HTTP 500")
        if "same" in questions:
            return {"same": {"type": "noul", "noul": 0.9}}
        if "real" in questions:
            fp = "fp" in state["finding"]
            return {"real": {"type": "score", "score": 0 if fp else 4},
                    "still_present": {"type": "noul", "noul": 0.1 if fp else 0.9},
                    "false_positive": {"type": "noul", "noul": 0.95 if fp else 0.05},
                    "already_fixed": {"type": "noul", "noul": 0.05}}
        return {"easy": {"type": "noul", "noul": 0.95}, "hard": {"type": "noul", "noul": 0.05},
                "context": {"type": "noul", "noul": 0.9}}


class StubLease:
    def __init__(self):
        self.entered = self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                                   text=True).strip()


class Env:
    """A miniature program workspace: patrol output, the three frozen inputs, a git repo, a fixer dir."""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.patrol = self.root / "research/reviews/luna-patrol"
        self.patrol.mkdir(parents=True)
        repo = self.root / "zene-ui"
        (repo / "src/core").mkdir(parents=True)
        subprocess.check_call(["git", "init", "-q", str(repo)])
        for name in ("A", "B", "C"):
            (repo / f"src/core/{name}.cpp").write_text("".join(f"int {name.lower()}{i} = {i};\n" for i in range(40)))
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        self.sha = git(repo, "rev-parse", "HEAD")
        self.qwen = self.root / "qwen"
        (self.qwen / "state").mkdir(parents=True)
        (self.qwen / "runs").mkdir()
        self.findings, self.status = [], []
        self.cfg = tn.Config(program=self.root, qwen=self.qwen, repo=repo, poll=0.01,
                             reviews=self.root / "no-reviews")

    def f(self, pass_no, file, title, duplicate=False, key=None, luna_field=None, luna_line=None):
        line = f"  difficulty: {luna_line} - how hard the FIX is\n" if luna_line else ""
        row = {"title": title, "file": f"src/core/{file}", "line": 3, "confidence": "high",
               "text": f"BUG 1: {title}\n  where: src/core/{file}:3\n  what: {title}\n  when: w\n"
                       f"  fix: x\n{line}  confidence: high",
               "pass": pass_no, "rev": self.sha[:9], "angle": "undo", "slice": 1,
               "duplicate": duplicate, "key": key or f"{file}|{title}"}
        if luna_field is not None:
            row["difficulty"] = luna_field  # pass 9+: patrol.py findings_of() stores the field
        self.findings.append(row)
        return len(self.findings) - 1

    def start(self, n, finish=True):
        (self.patrol / f"pass-{n:04d}-{self.sha[:9]}-undo").mkdir(exist_ok=True)
        self.status.append(f"- 2026-10-06 10:0{n} pass {n} started: HEAD at {self.sha[:9]}, angle 'undo', 2 slices")
        if finish:
            self.status.append(f"- 2026-10-06 11:0{n} pass {n} finished: 2 new findings")

    def write(self, snapshot_n=None):
        (self.patrol / "STATUS.md").write_text("\n".join(self.status) + "\n")
        (self.patrol / "findings.jsonl").write_text("".join(json.dumps(f) + "\n" for f in self.findings))
        if snapshot_n is not None:
            clef = self.root / "research/reviews/luna-clef-2026-10-05"
            clef.mkdir(parents=True)
            (clef / "findings-snapshot.jsonl").write_text(
                "".join(json.dumps(f) + "\n" for f in self.findings[:snapshot_n]))

    def frozen(self, clusters, inventory):
        d = self.root / "research/reviews/luna-dedup-2026-10-05/run-v2"
        d.mkdir(parents=True)
        (d / "clusters.jsonl").write_text("".join(json.dumps(c) + "\n" for c in clusters))
        inv = self.root / "research/reviews/luna-easy-fix-2026-10-06/inventory"
        inv.mkdir(parents=True)
        (inv / "part-000.jsonl").write_text("".join(json.dumps(r) + "\n" for r in inventory))

    def qwen_bug(self, cluster, file, title):
        slug = str(cluster)
        (self.qwen / "state" / f"{slug}.json").write_text(json.dumps({"cluster": cluster, "outcome": "in-qa"}))
        run = self.qwen / "runs" / f"{slug}.r1"
        run.mkdir()
        item = {"cluster": cluster, "title": title, "file": f"src/core/{file}", "line": 3,
                "text": f"BUG 1: {title}\n  what: {title}\n  when: w\n  fix: x"}
        (run / "brief.md").write_text("You are fixing ONE bug\n\n## The finding (from Luna's patrol; historical "
                                      "line numbers)\n```json\n" + json.dumps(item, indent=1) + "\n```\n")

    def decisions(self):
        return tn.rows(self.cfg.out / "decisions.jsonl")

    def queue(self, kind="easy"):
        return tn.rows(self.cfg.out / "queue" / f"{kind}.jsonl")


def build_standard(env):
    """Pass 1 is the frozen snapshot; pass 2 holds one finding per dedup source, two new and one false positive."""
    env.start(1)
    a0 = env.f(1, "A.cpp", "alpha bug")
    env.f(1, "A.cpp", "beta bug")
    env.write(snapshot_n=2)
    env.frozen(clusters=[{"representative": 0, "members": [0]}, {"representative": 1, "members": [1]}],
               inventory=[{"cluster": 1, "members": [1], "lane": "core", "file": "src/core/A.cpp"},
                          {"cluster": "review-abc", "members": [], "lane": "engine", "file": "src/core/B.cpp",
                           "title": "gamma bug", "text": "gamma bug"}])
    env.qwen_bug(777, "C.cpp", "qwen bug")
    env.start(2)
    ids = {"repeat_a": env.f(2, "A.cpp", "alpha bug"),            # (a) the frozen 10-05 cluster 0
           "repeat_b": env.f(2, "B.cpp", "gamma bug"),            # (b) the 10-06 review-abc item
           "new1": env.f(2, "A.cpp", "delta bug"),
           "new2": env.f(2, "A.cpp", "delta bug", key="other"),   # same pass, same text: never merged
           "fp": env.f(2, "A.cpp", "epsilon fp bug"),
           "flag": env.f(2, "A.cpp", "zeta bug", duplicate=True, key=env.findings[a0]["key"]),  # (e)
           "repeat_d": env.f(2, "C.cpp", "qwen bug")}             # (d) the fixer's cluster 777
    env.write()
    return ids


class PassCompletionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finished_superseded_running_and_abandoned(self):
        e = self.env
        e.start(1)                 # finished
        e.start(2, finish=False)   # a later pass started: superseded
        e.start(3, finish=False)   # the last pass, no 'finished'
        e.status.append("- 2026-10-06 12:00 pass 3 slice 1/2: 4 new findings")
        e.write(snapshot_n=0)
        states = tn.pass_states(e.cfg, alive=True)
        self.assertEqual([states[n]["state"] for n in (1, 2, 3)], ["finished", "superseded", "running"])
        self.assertEqual([states[n]["complete"] for n in (1, 2, 3)], [True, True, False])
        self.assertEqual(states[3]["rev"], e.sha[:9])
        self.assertEqual(tn.pass_states(e.cfg, alive=False)[3]["state"], "abandoned")

    def test_patrol_alive_reads_pid_and_command_line(self):
        e = self.env
        e.write(snapshot_n=0)
        (e.patrol / "patrol.pid").write_text(str(dead_pid()))
        self.assertFalse(tn.patrol_alive(e.cfg))
        (e.patrol / "patrol.pid").write_text(str(os.getpid()))  # alive, but not a patrol.py process
        self.assertFalse(tn.patrol_alive(e.cfg))


class GpuLeaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        # Never touch the real machine: no ss/nvidia-smi/Ollama calls, no stopping a real Strata.
        self.strata = set()
        self.calls = []
        fakes = {
            "strata_pids": lambda: set(self.strata),
            "stop_strata": lambda: (self.calls.append("stop"), self.strata.clear(), True)[2],
            "start_strata": lambda: self.calls.append("start"),
            "gpu_free_mib": lambda: 16000,
            "ollama_unload_all": lambda: self.calls.append("unload"),
        }
        for name, fake in fakes.items():
            patcher = mock.patch.object(tn, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def lease(self, needle=""):
        return tn.GpuLease(self.dir, poll=0.01, fixer_needle=needle, log_every=60)

    def test_no_live_fixer_proceeds_and_withdraws_request(self):
        (self.dir / "fix.pid").write_text(str(dead_pid()))
        with self.lease() as lease:
            text = (self.dir / "GPU-REQUEST").read_text().split()
            self.assertEqual(text[0], str(os.getpid()))
            self.assertTrue(lease.held)
        self.assertFalse((self.dir / "GPU-REQUEST").exists())

    def test_missing_fix_pid_means_free(self):
        with self.lease():
            self.assertTrue((self.dir / "GPU-REQUEST").exists())
        self.assertFalse((self.dir / "GPU-REQUEST").exists())

    def test_live_fixer_waits_for_fresh_gpu_free(self):
        (self.dir / "fix.pid").write_text(str(os.getpid()))  # a live process; needle "" matches it
        stale = self.dir / "GPU-FREE"
        stale.write_text("old\n")
        os.utime(stale, (time.time() - 3600, time.time() - 3600))  # left over: must not count
        seen = {}

        def fixer():
            for _ in range(500):
                if (self.dir / "GPU-REQUEST").exists():
                    seen["request"] = (self.dir / "GPU-REQUEST").read_text()
                    break
                time.sleep(0.01)
            time.sleep(0.3)
            stale.write_text("now\n")

        t = threading.Thread(target=fixer)
        t0 = time.time()
        t.start()
        with self.lease():
            waited = time.time() - t0
        t.join()
        self.assertGreaterEqual(waited, 0.25)
        self.assertTrue(seen["request"].startswith(str(os.getpid()) + " "))
        self.assertFalse((self.dir / "GPU-REQUEST").exists())

    def test_released_on_exception_and_on_sigterm_handler(self):
        with self.assertRaises(RuntimeError):
            with self.lease():
                raise RuntimeError("Clef HTTP 500")
        self.assertFalse((self.dir / "GPU-REQUEST").exists())
        with self.assertRaises(SystemExit):
            with self.lease():
                tn.on_signal(15, None)
        self.assertFalse((self.dir / "GPU-REQUEST").exists())

    def test_standalone_strata_is_stopped_then_restarted_and_clef_unloaded(self):
        self.strata = {4242}  # Strata running with no fixer (started by hand)
        with self.lease() as lease:
            self.assertEqual(self.calls, ["stop"])
            self.assertTrue(lease.stopped_strata)
        self.assertEqual(self.calls, ["stop", "unload", "start"])
        self.assertFalse((self.dir / "GPU-REQUEST").exists())

    def test_with_a_live_fixer_the_lease_leaves_strata_to_the_fixer(self):
        (self.dir / "fix.pid").write_text(str(os.getpid()))
        self.strata = {4242}
        free = self.dir / "GPU-FREE"

        def fixer():
            for _ in range(500):
                if (self.dir / "GPU-REQUEST").exists():
                    break
                time.sleep(0.01)
            free.write_text("now\n")

        t = threading.Thread(target=fixer)
        t.start()
        with self.lease():
            pass
        t.join()
        self.assertEqual(self.calls, ["unload"])  # no stop/start: the fixer owns Strata's lifecycle

    def test_never_deletes_someone_elses_request(self):
        lease = self.lease()
        lease.held = True
        (self.dir / "GPU-REQUEST").write_text("1 2026-10-06T10:00:00 other\n")
        lease.release()
        self.assertTrue((self.dir / "GPU-REQUEST").exists())


class IdsAndWritesTest(unittest.TestCase):
    def test_new_ids_cannot_collide_with_integer_or_review_ids(self):
        ids = tn.new_cluster_ids(8, 3, {0, 5426, "review-faa175bfa", "new-p7-1"})
        self.assertEqual(ids, ["new-p8-1", "new-p8-2", "new-p8-3"])
        for i in ids:
            self.assertIsInstance(i, str)
            self.assertFalse(i.startswith("review-"))
            self.assertRaises(ValueError, int, i)
        with self.assertRaises(RuntimeError):
            tn.new_cluster_ids(8, 3, {"new-p8-2"})

    def test_atomic_write_keeps_old_content_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "queue" / "easy.jsonl"
            tn.atomic_write(target, "old\n")
            with mock.patch.object(tn.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    tn.atomic_write(target, "new\n")
            self.assertEqual(target.read_text(), "old\n")
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["easy.jsonl"])
            tn.atomic_write(target, "new\n")
            self.assertEqual(target.read_text(), "new\n")

    def test_link_scores_unjudged_band_pairs_with_band_default(self):
        corpus = {0: {"file": "a.cpp", "pass": 1}, 1: {"file": "a.cpp", "pass": 2}}
        vec = {0: np.array([1.0, 0.0]), 1: np.array([0.9, (1 - 0.81) ** 0.5])}  # cosine 0.9: in band
        groups = {0: {"members": [0]}}
        self.assertEqual(tn.link([1], groups, corpus, vec, {}, 0.84, 0.94, 0.4), {})
        self.assertEqual(tn.link([1], groups, corpus, vec, {}, 0.84, 0.94, 0.4, band_default=1.0), {1: (0, 1.0)})
        self.assertEqual(tn.link([1], groups, corpus, vec, {(1, 0): 0.5}, 0.84, 0.94, 0.4), {})  # 0.5 < 1 - t

    def test_luna_difficulty_field_text_fallback_and_absent(self):
        self.assertEqual(tn.luna_difficulty({"difficulty": "Hard", "text": "difficulty: easy"}), "hard")  # field wins
        self.assertEqual(tn.luna_difficulty({"text": "BUG 1: t\n  fix: x\n  DIFFICULTY:  Unsure - why\n"}), "unsure")
        self.assertEqual(tn.luna_difficulty({"difficulty": "", "text": "  difficulty: easy - small"}), "easy")
        self.assertEqual(tn.luna_difficulty({"text": "BUG 1: t\n  fix: x\n  confidence: high"}), "")
        self.assertEqual(tn.luna_difficulty({"difficulty": "medium", "text": "difficulty: moderate"}), "")

    def test_difficulty_agree_only_on_decided_ratings(self):
        self.assertIs(tn.difficulty_agree("easy", "easy"), True)
        self.assertIs(tn.difficulty_agree("hard", "easy"), False)
        for luna, clef in (("", "easy"), ("unsure", "needs_context"), ("unsure", "hard"), ("easy", "needs_context"),
                           ("easy", None)):
            self.assertIsNone(tn.difficulty_agree(luna, clef))

    def test_clef_text_drops_only_lunas_difficulty_line(self):
        text = "BUG 1: t\n  what: w\n  fix: x\n  difficulty: hard - architecture\n  confidence: high"
        self.assertEqual(tn.clef_text(text), "BUG 1: t\n  what: w\n  fix: x\n  confidence: high")

    def test_scope_string_still_matches_difficulty_v2(self):
        self.assertIn(repr(tn.DIFFICULTY_SCOPE)[1:-1], (HERE / "difficulty_v2.py").read_text())


class TriageRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(self.tmp.name)
        self.ids = build_standard(self.env)
        self.models, self.lease = FakeModels(), StubLease()

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, models=None):
        return tn.run_once(self.env.cfg, models=models or self.models, lease=self.lease,
                           states=tn.pass_states(self.env.cfg, alive=False))

    def by_finding(self):
        return {d["finding_id"]: d for d in self.env.decisions() if "finding_id" in d}

    def test_repeats_of_every_known_source_are_dropped_not_queued(self):
        self.assertEqual(self.run_once(), 2)
        d, ids = self.by_finding(), self.ids
        self.assertEqual((d[ids["repeat_a"]]["reason"], d[ids["repeat_a"]]["joined_cluster"]), ("repeat", 0))
        self.assertIn("dedup-2026-10-05", d[ids["repeat_a"]]["join_sources"])
        self.assertEqual(d[ids["repeat_b"]]["joined_cluster"], "review-abc")
        self.assertEqual(d[ids["repeat_b"]]["join_sources"], ["inventory-2026-10-06"])
        self.assertEqual(d[ids["repeat_d"]]["joined_cluster"], "qwen:777")
        self.assertEqual(d[ids["repeat_d"]]["join_sources"], ["qwen-fixer"])
        self.assertEqual(d[ids["flag"]]["joined_cluster"], 0)
        self.assertTrue(d[ids["flag"]]["join_via"].startswith("patrol duplicate flag"))
        self.assertEqual((d[ids["fp"]]["outcome"], d[ids["fp"]]["reason"]), ("dropped", "likely_false_positive"))
        queued = self.env.queue("easy")
        self.assertEqual(sorted(q["finding_id"] for q in queued), [ids["new1"], ids["new2"]])  # same pass: 2 clusters
        self.assertEqual(sorted(q["cluster"] for q in queued), ["new-p2-1", "new-p2-2"])
        self.assertEqual(self.env.queue("hard") + self.env.queue("needs_context"), [])
        self.assertEqual((self.lease.entered, self.lease.exited), (1, 1))  # one GPU window for the run

    def test_queue_items_carry_the_inventory_fields(self):
        self.run_once()
        item = self.env.queue("easy")[0]
        for field in ("cluster", "members", "finding_id", "lane", "worktree", "revision", "title", "file", "line",
                      "text", "report_evidence", "realness", "difficulty", "realness_answers",
                      "difficulty_answers", "model_digest", "realness_context_sha256",
                      "difficulty_context_sha256"):
            self.assertIn(field, item)
        self.assertEqual(item["revision"], self.env.sha)
        self.assertEqual(item["worktree"], str(self.env.cfg.repo))
        self.assertEqual(item["lane"], "core")
        self.assertEqual(item["realness"], "likely_open_bug")
        counts = json.loads((self.env.cfg.out / "COUNTS.json").read_text())
        self.assertEqual(counts["queue"], {"easy": 2, "needs_context": 0, "hard": 0})
        self.assertEqual(counts["repeats"], 4)

    def test_rerun_resumes_and_skips_done_work(self):
        self.run_once()
        journal = (self.env.cfg.out / "decisions.jsonl").read_text()
        rerun = FakeModels()
        self.assertEqual(self.run_once(rerun), 0)
        self.assertEqual(rerun.calls, [])
        self.assertEqual((self.env.cfg.out / "decisions.jsonl").read_text(), journal)
        self.assertEqual(sorted(r["pass"] for r in tn.rows(self.env.cfg.out / "TRIAGED-PASSES")), [1, 2])

    def test_unfinished_run_is_ignored_and_redone_without_new_clef_calls(self):
        self.run_once()
        out = self.env.cfg.out
        for name in ("decisions.jsonl", "TRIAGED-PASSES", "queue/easy.jsonl"):
            (out / name).write_text("")
        tn.append_rows(out / "decisions.jsonl", [{"run_id": "dead", "pass": 2, "finding_id": 99,
                                                  "new_cluster": True, "cluster": "new-p2-1"}])
        self.assertEqual(tn.load_ledger(self.env.cfg)["clusters"], {})
        again = FakeModels()
        self.assertEqual(self.run_once(again), 2)
        self.assertEqual([c for c in again.calls if c[0] == "ask"], [])  # realness/difficulty/pairs journalled
        self.assertEqual(len(self.env.queue("easy")), 2)

    def test_luna_difficulty_is_carried_but_clef_routes(self):
        self.run_once()
        self.env.start(3)
        field = self.env.f(3, "A.cpp", "eta bug", luna_field="hard")         # pass 9+: field (Clef says easy)
        text = self.env.f(3, "A.cpp", "theta bug", luna_line="easy")         # pass 8: only in text
        absent = self.env.f(3, "A.cpp", "iota bug")                          # no rating at all
        unsure = self.env.f(3, "A.cpp", "kappa bug", luna_line="unsure")
        rep = self.env.f(3, "A.cpp", "delta bug", key="r3", luna_field="easy")  # a repeat still records it
        self.env.write()
        models = FakeModels()
        seen = []
        orig = models.ask
        models.ask = lambda state, q: (seen.append(state.get("finding", "")), orig(state, q))[1]
        self.run_once(models)
        self.assertFalse(any("difficulty:" in s.lower() for s in seen))  # Clef never sees Luna's rating
        d = self.by_finding()
        self.assertEqual({k: d[k]["luna_difficulty"] for k in (field, text, absent, unsure, rep)},
                         {field: "hard", text: "easy", absent: "", unsure: "unsure", rep: "easy"})
        self.assertEqual({k: d[k]["difficulty_agree"] for k in (field, text, absent, unsure, rep)},
                         {field: False, text: True, absent: None, unsure: None, rep: None})
        self.assertTrue(all("luna_difficulty" in r and "difficulty_agree" in r for r in d.values()))
        queued = {q["finding_id"]: q for q in self.env.queue("easy")}
        self.assertEqual(queued[field]["difficulty"], "easy")  # Clef routes; Luna's 'hard' does not move it
        self.assertEqual(queued[field]["luna_difficulty"], "hard")
        self.assertEqual(queued[absent]["luna_difficulty"], "")
        self.assertEqual(self.env.queue("hard"), [])
        tally = json.loads((self.env.cfg.out / "COUNTS.json").read_text())["luna_vs_clef_difficulty"]
        self.assertEqual((tally["compared"], tally["agree"], tally["disagree"], tally["agreement_rate"]),
                         (2, 1, 1, 0.5))
        self.assertEqual(tally["luna_absent"], 3)  # the two pass-2 clusters and 'iota'
        self.assertEqual(tally["pairs"]["luna:hard clef:easy"], 1)

    def test_c_repeat_of_a_cluster_from_an_earlier_new_pass(self):
        self.run_once()
        self.env.start(3)
        later = self.env.f(3, "A.cpp", "delta bug", key="later")
        self.env.write()
        self.assertEqual(self.run_once(), 1)
        d = self.by_finding()[later]
        self.assertEqual((d["reason"], d["joined_cluster"]), ("repeat", "new-p2-1"))
        self.assertEqual(d["join_sources"], ["new-triage-pass-2"])
        queued = {q["cluster"]: q for q in self.env.queue("easy")}
        self.assertEqual(sorted(queued), ["new-p2-1", "new-p2-2"])  # nothing new queued
        self.assertEqual(queued["new-p2-1"]["members"], [self.ids["new1"], later])

    def test_band_pair_is_judged_by_clef_and_journalled(self):
        self.run_once()
        self.env.start(3)
        later = self.env.f(3, "A.cpp", "delta bug v2", key="v2")
        self.env.write()
        models = FakeModels()
        self.run_once(models)
        self.assertIn(("ask", ("same",)), models.calls)
        d = self.by_finding()[later]
        self.assertEqual((d["joined_cluster"], d["avg_similarity"]), ("new-p2-1", 0.9))
        pairs = tn.rows(self.env.cfg.out / "pair-judgements.jsonl")
        # Both pass-2 'delta' clusters are in band; the tie goes to the lower id, deterministically.
        self.assertEqual(sorted((r["a"], r["b"], r["p"]) for r in pairs),
                         [(later, self.ids["new1"], 0.9), (later, self.ids["new2"], 0.9)])

    def test_d_fixer_cluster_mapped_to_a_known_id_marks_that_cluster(self):
        self.env.qwen_bug(1, "A.cpp", "beta bug")  # the fixer holds frozen cluster 1
        self.env.start(3)
        later = self.env.f(3, "A.cpp", "beta bug", key="b2")
        self.env.write()
        self.run_once()
        d = self.by_finding()[later]
        self.assertEqual(d["joined_cluster"], 1)
        self.assertEqual(d["join_sources"], ["dedup-2026-10-05", "inventory-2026-10-06", "qwen-fixer"])

    def test_clef_error_stops_the_run_and_records_nothing(self):
        with self.assertRaises(RuntimeError):
            self.run_once(FakeModels(fail=True))
        self.assertEqual(sorted(r["pass"] for r in tn.rows(self.env.cfg.out / "TRIAGED-PASSES")), [1])
        self.assertEqual(self.env.queue("easy"), [])
        self.assertEqual((self.lease.entered, self.lease.exited), (1, 1))

    def test_changed_model_digest_is_refused(self):
        self.run_once()
        self.env.start(3)
        self.env.f(3, "A.cpp", "omega bug")
        self.env.write()
        with self.assertRaises(SystemExit):
            self.run_once(FakeModels(digest="d2"))

    def test_running_pass_is_not_triaged(self):
        self.env.start(3, finish=False)
        self.env.f(3, "A.cpp", "omega bug")
        self.env.write()
        states = tn.pass_states(self.env.cfg, alive=True)
        tn.run_once(self.env.cfg, models=self.models, lease=self.lease, states=states)
        self.assertNotIn(3, {r["pass"] for r in tn.rows(self.env.cfg.out / "TRIAGED-PASSES")})

    def test_dry_run_writes_nothing(self):
        out = self.env.cfg.out
        with mock.patch("builtins.print"):
            tn.dry_run(self.env.cfg)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
