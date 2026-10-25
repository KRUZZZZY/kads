import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from tempfile import TemporaryDirectory
import opencode_free as free


def model(name='space-bunny-free'):
    return {'id': name, 'providerID': 'opencode', 'status': 'active',
            'name': name, 'cost': {'input': 0, 'output': 0, 'cache': {'read': 0, 'write': 0}},
            'capabilities': {'toolcall': True}}


class FreeModelTest(unittest.TestCase):
    def test_paid_or_non_coding_models_are_refused(self):
        self.assertTrue(free.eligible(model()))
        paid = model(); paid['cost']['output'] = 0.01
        self.assertFalse(free.eligible(paid))
        self.assertFalse(free.eligible(model('gpt-6-luna')))
        cached = model(); cached['cost']['cache']['write'] = 1
        self.assertFalse(free.eligible(cached))
        unknown = model(); unknown['cost']['input'] = False
        self.assertFalse(free.eligible(unknown))
        no_tools = model(); no_tools['capabilities']['toolcall'] = False
        self.assertFalse(free.eligible(no_tools))

    def test_every_auxiliary_model_is_the_selected_free_model(self):
        config = free.profile('space-bunny-free', Path('/tmp/probe'))
        expected = 'opencode/space-bunny-free'
        self.assertEqual(config['model'], expected)
        self.assertEqual(config['small_model'], expected)
        self.assertTrue(all(v['model'] == expected for v in config['agent'].values()))
        self.assertEqual(config['provider']['opencode']['whitelist'], ['space-bunny-free'])
        self.assertEqual(config['enabled_providers'], ['opencode'])
        self.assertEqual(config['permission']['task'], 'deny')
        self.assertEqual(config['mcp']['fff']['cwd'], '/tmp/probe')

    def test_catalog_filters_nonzero_cost(self):
        paid = model(); paid['id'] = 'mimo-v2.6-flash-free'; paid['cost']['input'] = 1
        text = 'opencode/space-bunny-free\n' + json.dumps(model(), indent=2)
        text += '\nopencode/mimo-v2.6-flash-free\n' + json.dumps(paid, indent=2)
        self.assertEqual(list(free.parse_catalog(text)), ['space-bunny-free'])

    def test_provider_errors_are_not_part_of_final_answer(self):
        events = [{'type': 'text', 'part': {'text': 'Done'}},
                  {'type': 'error', 'error': {'message': 'rate limit'}}]
        self.assertEqual(free.text_from_events(events), 'Done')

    def test_catalog_skips_unverifiable_records_and_checks_identity(self):
        text = 'opencode/paid-model\n{bad metadata}\n'
        text += 'opencode/space-bunny-free\n{bad metadata}\n'
        text += 'opencode/mimo-v2.6-flash-free\n' + json.dumps(model('mimo-v2.6-flash-free'))
        text += '\nopencode/ling-3.1-flash-free\n' + json.dumps(model())
        self.assertEqual(list(free.parse_catalog(text)), ['mimo-v2.6-flash-free'])

    def test_nonzero_cost_pauses_even_when_cli_already_exited(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            proc = MagicMock()
            proc.stdout = [json.dumps({'type': 'step_finish', 'part': {'cost': 0.01}}) + '\n']
            proc.wait.return_value = 0
            with patch.object(free, 'STATE', root / 'state.json'), \
                 patch.object(free.subprocess, 'Popen', return_value=proc), \
                 patch.object(free.os, 'killpg', side_effect=ProcessLookupError), \
                 patch.object(free.signal, 'signal'):
                with self.assertRaisesRegex(RuntimeError, 'nonzero cost'):
                    free.run_model('space-bunny-free', root, 'fix', root / 'final.md')
            self.assertGreater(free.load(root / 'state.json', {})['paused_until'], free.time.time())
            usage = free.load(root / 'opencode-space-bunny-free-usage.json', {})
            self.assertTrue(usage['stopped_for_nonzero_cost'])
            self.assertEqual(usage['step_costs'], [0.01])

    def test_rate_limit_falls_back_only_if_tree_has_not_changed(self):
        for changed in [False, True]:
            with self.subTest(changed=changed), TemporaryDirectory() as temp:
                root = Path(temp); cfg = root / 'config.json'; state = root / 'state.json'
                free.write(cfg, {'enabled': True, 'models': ['space-bunny-free', 'mimo-v2.6-flash-free']})
                models = {name: model(name) for name in ['space-bunny-free', 'mimo-v2.6-flash-free']}
                signatures = [('head', ''), ('head', 'M source.cpp' if changed else '')]
                with patch.multiple(free, CONFIG=cfg, STATE=state), \
                     patch.object(free, 'catalog', return_value=models), \
                     patch.object(free, 'tree_signature', side_effect=signatures), \
                     patch.object(free, 'run_model', side_effect=[(1, True), (0, False)]) as runner, \
                     patch.object(free.sys, 'stdin', __import__('io').StringIO('fix')):
                    free.run(root, root / 'final.md')
                self.assertEqual(runner.call_count, 1 if changed else 2)


if __name__ == '__main__':
    unittest.main()
