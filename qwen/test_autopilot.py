"""Autopilot regressions: temporary repositories, fake systemctl, no messages sent."""
import datetime
import importlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import issue_ledger as L
import autopilot as A


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zene-autopilot-test-')
        self.root = Path(self.temp.name)
        self.oldroot = L.ROOT
        L.ROOT = self.root
        (self.root/'issues').mkdir()
        self.prog = self.root/'prog'
        self.repo = self.prog/'zene-cachyos'
        self.repo.mkdir(parents=True)
        self.git(self.repo, 'init', '-q', '-b', '050/m0-train-1')
        self.git(self.repo, 'config', 'user.email', 't@example.invalid')
        self.git(self.repo, 'config', 'user.name', 'T')
        (self.repo/'a.cpp').write_text('int a;\n')
        self.git(self.repo, 'add', '.'); self.git(self.repo, 'commit', '-qm', 'train 1')
        self.tree = self.prog/'zene-qwen-123'
        self.git(self.repo, 'worktree', 'add', '-q', '-b', 'bugs/qwen-123', str(self.tree), '050/m0-train-1')
        self.git(self.repo, 'checkout', '-q', '-b', '050/m0-train-2')
        (self.repo/'b.cpp').write_text('int b;\n')
        self.git(self.repo, 'add', '.'); self.git(self.repo, 'commit', '-qm', 'train 2')
        (self.root/'FIX-BASE').write_text('050/m0-train-2\n')
        self.item = {'cluster': '123', 'title': 'defect', 'worktree': str(self.repo)}

    def tearDown(self):
        L.ROOT = self.oldroot
        self.temp.cleanup()

    def git(self, wt, *args):
        r = subprocess.run(['git', '-C', str(wt), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def run_autopilot(self, show='', active='active'):
        def fake(*args):
            return show if args[0] == 'show' else active + '\n'
        with patch.object(A, 'systemctl', side_effect=fake):
            return A.run(prog=self.prog)

    def test_blocked_build_without_own_commits_moves_to_new_train_once_per_cap(self):
        L.block(self.item, 'blocked-build', 'cmake failed', self.tree)
        self.assertEqual(L.gate(self.item), 'blocked')
        done = self.run_autopilot()
        self.assertTrue(done and done[0].startswith('revived 123'))
        self.assertEqual(self.git(self.tree, 'rev-parse', 'HEAD'), self.git(self.repo, 'rev-parse', '050/m0-train-2'))
        self.assertNotEqual(L.gate(self.item), 'blocked')       # the stamp changed: the dispatcher retries it
        self.assertEqual(L.get(self.item)['auto_revives'], 1)
        for n in range(3, 5):                                    # it fails again on two more trains
            L.block(self.item, 'blocked-build', 'cmake failed', self.tree)
            self.git(self.repo, 'commit', '-q', '--allow-empty', '-m', f'train {n}')
            self.git(self.repo, 'branch', f'050/m0-train-{n}')
            (self.root/'FIX-BASE').write_text(f'050/m0-train-{n}\n')
            self.run_autopilot()
        self.assertEqual(L.get(self.item)['auto_revives'], 2)    # the cap: the second retry was the last
        self.assertEqual(L.gate(self.item), 'blocked')

    def test_tree_with_its_own_commits_is_left_alone(self):
        (self.tree/'fix.cpp').write_text('int fix;\n')
        self.git(self.tree, 'add', '.'); self.git(self.tree, 'commit', '-qm', 'work in progress')
        head = self.git(self.tree, 'rev-parse', 'HEAD')
        L.block(self.item, 'blocked-build', 'cmake failed', self.tree)
        done = self.run_autopilot()
        self.assertIn('commits of its own', done[0])
        self.assertEqual(self.git(self.tree, 'rev-parse', 'HEAD'), head)
        self.assertEqual(L.gate(self.item), 'blocked')

    def test_lane_tree_is_never_moved(self):
        self.git(self.repo, 'checkout', '-q', '050/m0-train-1')
        L.block(self.item, 'blocked-build', 'cmake failed', self.repo)
        self.assertEqual(self.run_autopilot(), [])
        self.assertEqual(self.git(self.repo, 'rev-parse', '--abbrev-ref', 'HEAD'), '050/m0-train-1')

    def test_failed_diagnostic_is_rerun_once_after_cooldown_but_a_verdict_is_kept(self):
        L.block(self.item, 'blocked-diagnostic', 'Diagnostic failed or gave no actionable plan (rc 1); dir', self.repo)
        self.assertEqual(self.run_autopilot(), [])               # cooldown not over
        with L.edit(self.item) as data:
            data['events'][-1]['at'] = (datetime.datetime.now() - datetime.timedelta(hours=7)).isoformat(timespec='seconds')
        self.assertTrue(self.run_autopilot()[0].startswith('diagnostic retry for 123'))
        self.assertEqual(L.get(self.item)['status'], 'ready')
        L.block(self.item, 'blocked-diagnostic', 'Diagnostic failed or gave no actionable plan (rc 1); dir', self.repo)
        with L.edit(self.item) as data:
            data['events'][-1]['at'] = '2000-01-01T00:00:00'
        self.assertEqual(self.run_autopilot(), [])               # only once per issue
        other = {**self.item, 'cluster': '456'}
        L.block(other, 'blocked-diagnostic', '{"decision": "blocked", "blockers": ["no ASIO on Linux"]}', self.repo)
        with L.edit(other) as data:
            data['events'][-1]['at'] = '2000-01-01T00:00:00'
        self.assertEqual(self.run_autopilot(), [])
        self.assertEqual(L.get(other)['status'], 'blocked-diagnostic')

    def test_train_oom_halves_ctest_jobs_once_per_kill_then_alerts_at_floor(self):
        show = 'Result=oom-kill\nExecMainExitTimestamp=Sat 2026-10-24 18:53:36 BST\n'
        self.run_autopilot(show); self.run_autopilot(show)
        self.assertEqual((self.root/'merge/BAR-JOBS').read_text().strip(), '4')
        for stamp in ('19:00', '20:00', '21:00'):
            self.run_autopilot(show.replace('18:53', stamp))
        self.assertEqual((self.root/'merge/BAR-JOBS').read_text().strip(), '2')
        log = (self.root/'logs/autopilot.log').read_text()
        self.assertEqual(log.count('alert:'), 2)                 # two kills at the floor, one alert each
        mt = importlib.import_module('merge_train')
        with patch.object(mt, 'M', self.root/'merge'):
            self.assertEqual(mt.bar_jobs(), 2)

    def test_service_down_alerts_only_after_two_runs_and_once(self):
        self.run_autopilot(active='failed')
        self.assertFalse((self.root/'logs/autopilot.log').exists())
        self.run_autopilot(active='failed'); self.run_autopilot(active='failed')
        self.assertEqual((self.root/'logs/autopilot.log').read_text().count('has been down'), 1)

    def test_off_switch(self):
        (self.root/'AUTOPILOT-OFF').write_text('')
        L.block(self.item, 'blocked-build', 'cmake failed', self.tree)
        self.assertEqual(self.run_autopilot(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
