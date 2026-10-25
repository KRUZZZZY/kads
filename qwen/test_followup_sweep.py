import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
import followup_sweep as sweep
from followup_sweep import queue_status


class QueueGateTest(unittest.TestCase):
    def test_empty_queue_is_not_completion(self):
        self.assertFalse(queue_status([], {}, set(), 0, 0)['ready'])

    def test_parked_bug_and_qa_block_completion(self):
        q = [{'cluster': 'a'}]
        self.assertFalse(queue_status(q, {'a': {'outcome': 'parked-rework'}}, set(), 0, 0)['ready'])
        self.assertFalse(queue_status(q, {'a': {'outcome': 'accepted'}}, set(), 1, 0)['ready'])
        self.assertFalse(queue_status(q, {'a': {'outcome': 'accepted'}}, set(), 0, 1)['ready'])

    def test_skipped_attempt_is_not_a_resolved_bug(self):
        status = queue_status([{'cluster': 'old'}], {}, {'old'}, 0, 0)
        self.assertFalse(status['ready'])
        self.assertEqual(status['legacy_unreviewed'], ['old'])

    def test_all_accepted_and_no_pending_is_ready(self):
        self.assertTrue(queue_status([{'cluster': 'a'}], {'a': {'outcome': 'accepted'}}, set(), 0, 0)['ready'])

    def test_unfinished_state_outside_current_queue_still_blocks(self):
        self.assertFalse(queue_status([{'cluster': 'a'}], {
            'a': {'outcome': 'accepted'}, 'b': {'outcome': 'in-qa'},
        }, set(), 0, 0)['ready'])

    def test_unintegrated_fix_blocks_launch(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for folder in ['state', 'qa/pending', 'rework']:
                (root / folder).mkdir(parents=True)
            sweep.write(root / 'config.json', {'armed': True, 'repo': temp, 'branch': 'train'})
            sweep.write(root / 'state/a.json', {'outcome': 'accepted', 'kind': 'fix', 'head_after': 'fix'})
            (root / 'FIX-DRAINED').touch()
            fake = SimpleNamespace(load_queue=lambda: [{'cluster': 'a'}],
                                   luna_easy_attempted=lambda: set(), stopped=lambda _: False)
            with patch.multiple(sweep, ROOT=root, CONFIG=root / 'config.json', STATE=root / 'status.json'), \
                 patch.object(sweep, 'driver', return_value=fake), \
                 patch.object(sweep, 'git', side_effect=['train-tip', 'train-tip\nbase']), \
                 patch.object(sweep.subprocess, 'run') as run:
                result = sweep.check()
            self.assertEqual(result['phase'], 'waiting-for-integration')
            run.assert_not_called()

    def test_run_is_one_pinned_pass_then_stop_and_triage(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / 'out'; out.mkdir()
            (out / 'STOP').write_text('Stop after pass 12')
            sweep.write(root / 'config.json', {'armed': True, 'repo': temp})
            sweep.write(root / 'status.json', {'phase': 'starting', 'revision': 'pinned-sha', 'workers': 3})
            with patch.multiple(sweep, ROOT=root, OUT=out, CONFIG=root / 'config.json', STATE=root / 'status.json'), \
                 patch.object(sweep.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
                sweep.run_sweep()
            command = run.call_args_list[0].args[0]
            self.assertEqual(command[command.index('--passes') + 1], '1')
            self.assertEqual(command[command.index('--branch') + 1], 'pinned-sha')
            self.assertTrue((out / 'STOP').exists())
            self.assertIn('--once', run.call_args_list[1].args[0])
            self.assertFalse(__import__('json').loads((root / 'config.json').read_text())['armed'])


if __name__ == '__main__':
    unittest.main()
