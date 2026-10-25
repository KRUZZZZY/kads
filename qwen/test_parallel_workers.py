import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path
import qwenfix as driver


class ParallelWorkerTest(unittest.TestCase):
    def test_cloud_and_local_caps_are_independent(self):
        caps = {'luna': 4, 'qwen': 1}
        self.assertEqual(driver.open_session_slots(caps, ['luna'] * 4, 'qwen'), 1)
        self.assertEqual(driver.open_session_slots(caps, ['qwen'], 'luna'), 4)
        self.assertEqual(driver.open_session_slots(caps, ['qwen'], 'qwen'), 0)

    def test_cloud_quota_pause_does_not_block_local_worker(self):
        caps = {'luna': 0, 'qwen': 1, 'opencode': 1}
        self.assertEqual(driver.open_session_slots(caps, [], 'luna'), 0)
        self.assertEqual(driver.open_session_slots(caps, [], 'qwen'), 1)
        self.assertEqual(driver.open_session_slots(caps, ['qwen'], 'opencode'), 1)
        self.assertEqual(driver.open_session_slots(caps, ['opencode'], 'opencode'), 0)

    def test_reduced_cap_does_not_cancel_existing_sessions(self):
        self.assertEqual(driver.open_session_slots({'luna': 2}, ['luna'] * 4, 'luna'), 0)

    def test_claimed_bug_is_not_assigned_twice(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ['state', 'rework']:
                (root / name).mkdir()
            queue = [{'cluster': 'first'}, {'cluster': 'second'}]
            with patch.dict(driver.D, {'state': root / 'state', 'rework': root / 'rework'}), \
                 patch.object(driver, 'load_queue', return_value=queue), \
                 patch.object(driver, 'luna_easy_attempted', return_value=set()):
                first, _ = driver.next_item()
                second, _ = driver.next_item({driver.slug(first['item']['cluster'])})
                self.assertEqual(second['item']['cluster'], 'second')

    def test_local_worker_can_leave_reworks_for_cloud_workers(self):
        import json
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ['state', 'rework']:
                (root / name).mkdir()
            rework = {'item': {'cluster': 'retry'}, 'round': 2}
            (root / 'rework/retry.json').write_text(json.dumps(rework))
            (root / 'state/retry.json').write_text('{}')
            with patch.dict(driver.D, {'state': root / 'state', 'rework': root / 'rework'}), \
                 patch.object(driver, 'load_queue', return_value=[{'cluster': 'retry'}, {'cluster': 'fresh'}]), \
                 patch.object(driver, 'luna_easy_attempted', return_value=set()):
                local, path = driver.next_item(allow_rework=False)
                cloud, retry_path = driver.next_item({local['item']['cluster']})
                self.assertEqual(local['item']['cluster'], 'fresh')
                self.assertIsNone(path)
                self.assertEqual(cloud['item']['cluster'], 'retry')
                self.assertIsNotNone(retry_path)


if __name__ == '__main__':
    unittest.main()
