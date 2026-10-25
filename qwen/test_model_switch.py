"""Verify model routing and fallback effort without making model/API calls."""
import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
WRAPPER = Path.home() / 'Documents/AI_KOS_PROJECT/projects/zene-studio-program/tools/haiku/haiku-exec'
SUCCESS = {'type':'result', 'subtype':'success', 'result':'done', 'session_id':'test', 'num_turns':1}
OVERLOAD = {'type':'result', 'subtype':'error_during_execution', 'is_error':True, 'result':'API Error: 529 overloaded', 'num_turns':0}


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ModelSwitchTests(unittest.TestCase):
    def invoke(self, extra=None, responses=None):
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            env = {k:v for k,v in os.environ.items() if not k.startswith('HAIKU_')}
            env.update(HAIKU_MODEL='claude-haiku-5-5', HAIKU_EFFORT='high', HAIKU_LEDGER=str(temp/'cost.jsonl'), HAIKU_SESSION_OUT=str(temp/'session.json'))
            env.update(extra or {})
            with patch.dict(os.environ, env, clear=True):
                module = load(WRAPPER, 'haiku_exec_test')
            calls = []
            answers = iter(responses or [(0,SUCCESS,[])])
            def stream(cmd, cwd, env, prompt):
                calls.append(list(cmd))
                return next(answers)
            with patch.object(module, 'jail_paths', return_value=[]), patch.object(module, 'run_stream', side_effect=stream), patch.object(sys, 'argv', [str(WRAPPER), '-C', str(temp), '-o', str(temp/'out.md'), '-']), patch.object(sys, 'stdin', io.StringIO('do the job')), contextlib.redirect_stdout(io.StringIO()):
                rc = module.main()
            ledger = json.loads((temp/'cost.jsonl').read_text())
            session = json.loads((temp/'session.json').read_text()) if (temp/'session.json').exists() else None
            return rc, calls, ledger, session

    def choices(self, calls):
        return [(cmd[cmd.index('--model')+1],cmd[cmd.index('--effort')+1]) for cmd in calls]

    def test_direct_sonnet_selects_opus_medium(self):
        rc,calls,ledger,session = self.invoke({'HAIKU_MODEL':'claude-sonnet-5-5'})
        self.assertEqual(rc,0)
        self.assertEqual(self.choices(calls),[('claude-opus-5-5','medium')])
        self.assertEqual((ledger['model'],ledger['effort']),('claude-opus-5-5','medium'))
        self.assertEqual(session['model'],'claude-opus-5-5')

    def test_other_primary_models_keep_their_effort(self):
        for model, effort in [('claude-haiku-5-5','high'),('claude-opus-5-5','high')]:
            rc,calls,_,_ = self.invoke({'HAIKU_MODEL':model,'HAIKU_EFFORT':effort})
            self.assertEqual(rc,0)
            self.assertEqual(self.choices(calls),[(model,effort)])

    def test_overload_fallback_uses_medium_and_records_actual_model(self):
        rc,calls,ledger,session = self.invoke(responses=[(1,OVERLOAD,[json.dumps(OVERLOAD)]),(0,SUCCESS,[])])
        self.assertEqual(rc,0)
        self.assertEqual(self.choices(calls),[('claude-haiku-5-5','high'),('claude-opus-5-5','medium')])
        self.assertTrue(all('--fallback-model' not in cmd for cmd in calls))
        self.assertEqual((ledger['model'],ledger['effort']),('claude-opus-5-5','medium'))
        self.assertEqual(session['model'],'claude-opus-5-5')

    def test_explicit_sonnet_fallback_also_maps_to_medium(self):
        rc,calls,_,_ = self.invoke({'HAIKU_FALLBACK':'claude-sonnet-5-5','HAIKU_FALLBACK_EFFORT':'high'},[(1,OVERLOAD,[json.dumps(OVERLOAD)]),(0,SUCCESS,[])])
        self.assertEqual(rc,0)
        self.assertEqual(self.choices(calls)[-1],('claude-opus-5-5','medium'))

    def test_usage_limits_do_not_start_a_fallback(self):
        limit={**OVERLOAD,'result':'usage_limit_reached: weekly limit'}
        rc,calls,_,_ = self.invoke(responses=[(1,limit,[json.dumps(limit)])])
        self.assertEqual(rc,2)
        self.assertEqual(len(calls),1)

    def test_partial_tool_session_is_not_replayed(self):
        tool={'type':'assistant','message':{'content':[{'type':'tool_use','name':'Edit'}]}}
        rc,calls,_,_ = self.invoke(responses=[(1,OVERLOAD,[json.dumps(tool),json.dumps(OVERLOAD)])])
        self.assertEqual(rc,1)
        self.assertEqual(len(calls),1)

    def test_disabled_fallback_stays_disabled(self):
        rc,calls,_,_ = self.invoke({'HAIKU_FALLBACK':''},[(1,OVERLOAD,[json.dumps(OVERLOAD)])])
        self.assertEqual(rc,1)
        self.assertEqual(len(calls),1)

    def test_live_lane_config_has_no_sonnet_model(self):
        lanes=json.loads((ROOT/'LANES.json').read_text())['lanes']
        self.assertFalse(any(l['model']=='claude-sonnet-5-5' for l in lanes))
        for lane in lanes:
            if lane['name'] in ['sonnet-1','sonnet-2']:
                self.assertEqual((lane['model'],lane['effort']),('claude-opus-5-5','medium'))

    def test_lane_creation_maps_old_model_to_new_choice(self):
        with tempfile.TemporaryDirectory() as td:
            lanes=load(ROOT/'lanes.py','lanes_test')
            lanes.FILE=Path(td)/'LANES.json'
            lanes.ROOT=Path(td)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lanes.main(['add','sample','claude-sonnet-5-5','--effort','high']),0)
            lane=json.loads(lanes.FILE.read_text())['lanes'][0]
            self.assertEqual((lane['model'],lane['effort']),('claude-opus-5-5','medium'))

    def test_train_doctor_launches_opus_medium(self):
        tree=ast.parse((ROOT/'train_doctor.py').read_text())
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ['session','available']]
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'answer.md'
            captured=[]
            def run(cmd,**kwargs):
                captured.append((cmd,kwargs['env']))
                out.write_text('diagnosis')
                return subprocess.CompletedProcess(cmd,0)
            ns={'os':os,'subprocess':subprocess,'HAIKU_EXEC':WRAPPER,'DOCTOR_WT':Path(td),'SESSION_TIMEOUT_S':5,'claude_ok':lambda:True,'log':lambda _:None}
            exec(compile(ast.Module(body=nodes,type_ignores=[]),'train_doctor.py','exec'),ns)
            with patch.object(subprocess,'run',side_effect=run):
                self.assertTrue(ns['available']({'name':'opus'}))
                self.assertEqual(ns['session']({'name':'opus'},'diagnose',out),'diagnosis')
            self.assertEqual((captured[0][1]['HAIKU_MODEL'],captured[0][1]['HAIKU_EFFORT']),('claude-opus-5-5','medium'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
