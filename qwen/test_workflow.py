"""Workflow regressions: temporary repositories and fake model calls only."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import issue_ledger as L
import workflow_runtime as R
import workflow_evidence as E
import integration_jobs as I
import workflow_report as WR


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zene-workflow-test-')
        self.root = Path(self.temp.name)
        self.oldroot = L.ROOT
        L.ROOT = self.root
        for name in ['runs','state','rework','qa/done','merge']:
            (self.root/name).mkdir(parents=True)
        L.write(self.root/'WORKFLOW.json',{'started_at':'2026-10-24','enabled':True,
            'implementation_attempts_per_checkpoint':2,'max_new_model_sessions':8,
            'max_diagnostics':2,'max_integration_sessions':2})
        (self.root/'FIX-BASE').write_text('HEAD\n')
        (self.root/'negctl.py').write_text('# proof version 1\n')
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.git('init','-q')
        self.git('config','user.email','test@example.invalid')
        self.git('config','user.name','Test')
        (self.repo/'.gitignore').write_text('build/\n')
        (self.repo/'src').mkdir(); (self.repo/'src/a.cpp').write_text('int a = 1;\n')
        (self.repo/'tests').mkdir(); (self.repo/'tests/test.cpp').write_text('int test = 1;\n')
        (self.repo/'tests/fork-sources.txt').write_text('# fork\n')
        (self.repo/'tests/upstream-modifications.txt').write_text('# modified\n')
        (self.repo/'tests/all-sources.txt').write_text("# Regenerate: git ls-files '*.cpp' | grep -vE '^(vendor/)'\nold.cpp\n")
        self.commit('base')
        self.base = self.git('rev-parse','HEAD')
        self.item = {'cluster':'123','title':'Test defect','worktree':str(self.repo)}

    def tearDown(self):
        L.ROOT = self.oldroot
        self.temp.cleanup()

    def git(self,*args,repo=None):
        result=subprocess.run(['git','-C',str(repo or self.repo),*args],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout.strip()

    def commit(self,message):
        self.git('add','.')
        self.git('commit','-qm',message)
        return self.git('rev-parse','HEAD')

    def event(self,item=None,kind='implementation',name=None):
        item=item or self.item
        key=L.start(item,kind,self.root/'runs'/(name or ('r'+str(len(L.get(item)['events'])))),'model')
        L.finish(item,key,{'minutes':1,'rc':0})
        return key

    def test_ports_share_identity_and_checkpoint_budget(self):
        self.assertEqual(L.issue_id('new-p12-34-port-port'),'new-p12-34')
        self.assertEqual(L.issue_id('Q050-003.2'),'Q050-003.2')
        self.assertEqual(L.issue_id('Q050-114-port10'),'Q050-114')
        self.event(); self.event({**self.item,'cluster':'123-port'})
        self.assertEqual(L.gate({**self.item,'cluster':'123-port-port'}),'diagnostic')
        plan={'decision':'retry','root_cause':'src/a.cpp:1','changed_plan':'use a checked operation',
              'why_different':'test the previously missed overflow path','tests':['OverflowTest: fail before, pass after']}
        self.assertTrue(L.approve(self.item,plan))
        self.assertEqual(L.gate(self.item),'run')
        self.assertFalse(L.approve(self.item,plan))

    def test_global_budget_cannot_reset_by_port_model_or_checkpoint(self):
        for i in range(8):
            self.event({**self.item,'cluster':'123'+'-port'*i},kind='mechanical',name=str(i))
        self.assertEqual(L.gate(self.item),'exhausted')
        self.assertEqual(L.get(self.item)['status'],'budget-exhausted')

    def test_quota_cancelled_attempts_do_not_consume_budget(self):
        for i in range(12):
            key=L.start(self.item,'implementation',self.root/str(i),'model')
            L.finish(self.item,key,cancelled=True)
        self.assertEqual(L.gate(self.item),'run')

    def test_build_failure_waits_for_actual_changed_inputs(self):
        L.block(self.item,'blocked-build','missing target',self.repo)
        self.assertEqual(L.gate(self.item),'blocked')
        (self.root/'FIX-BASE').write_text('unrelated-new-train\n')
        self.assertEqual(L.gate(self.item),'blocked')
        (self.repo/'build').mkdir(); (self.repo/'build/CMakeCache.txt').write_text('WANT_WASM=ON\n')
        self.assertEqual(L.gate(self.item),'run')

    def test_source_change_unblocks_while_no_change_does_not(self):
        L.block(self.item,'blocked-diagnostic','needs dependency',self.repo)
        self.assertFalse(L.runnable(self.item))
        (self.repo/'src/a.cpp').write_text('int a = 2;\n'); self.commit('new implementation')
        self.assertTrue(L.runnable(self.item))

    def test_canonical_claim_prevents_two_aliases_running(self):
        with L.claim(self.item) as owned:
            self.assertTrue(owned)
            self.assertFalse(L.runnable({**self.item,'cluster':'123-port'}))
        self.assertTrue(L.runnable(self.item))

    def test_selector_skips_blocked_job_and_does_other_work(self):
        second={**self.item,'cluster':'456'}
        L.block(self.item,'blocked-build','broken base',self.repo)
        def select(claimed=(),**kwargs):
            for item in [self.item,second]:
                if item['cluster'] not in claimed:
                    return {'item':dict(item),'round':1},None
            return None,None
        pick=R.selector(select)
        job,_=pick()
        self.assertEqual(job['item']['cluster'],'456')

    def test_build_block_preserves_job_and_round(self):
        job={'item':self.item,'round':5}
        driver={'slug':str,'D':{'state':self.root/'state'},'log':lambda *a:None}
        R.build_failed(job,self.repo,'bad compiler config',driver)
        self.assertEqual(L.read(self.root/'rework/123.json')['round'],5)
        self.assertEqual(L.read(self.root/'state/123.json')['outcome'],'blocked-build')
        self.assertEqual(len([e for e in L.get(self.item)['events'] if e['kind']=='implementation']),0)

    def test_diagnostic_must_supply_changed_plan_and_tests(self):
        self.event();self.event()
        self.assertFalse(L.approve(self.item,{'decision':'retry','changed_plan':'try again'}))
        self.assertEqual(L.gate(self.item),'diagnostic')

    def test_diagnostic_uses_read_only_and_preserves_job(self):
        self.event();self.event()
        plan={'decision':'retry','root_cause':'src/a.cpp:1 overflow','changed_plan':'validate before arithmetic',
              'why_different':'uses the missed boundary case','tests':['BoundaryTest: INT_MAX case']}
        calls=[]
        def fake(cmd,**kwargs):
            calls.append(cmd)
            Path(cmd[cmd.index('-o')+1]).write_text('```json\n'+json.dumps(plan)+'\n```')
            return subprocess.CompletedProcess(cmd,0)
        driver={'PROG':self.repo,'light_model':lambda:'gpt-6-luna','light_cmd':lambda x:x,
                'session_env':lambda:{},'note_luna_limit':lambda x:False,'log':lambda *a:None}
        job={'item':self.item,'round':3,'notes':'old feedback'}
        with patch.object(R.subprocess,'run',side_effect=fake):
            R.diagnose(job,driver)
        self.assertIn('read-only',calls[0])
        saved=L.read(self.root/'rework/123.json')
        self.assertEqual(saved['round'],3)
        self.assertIn('DIAGNOSTIC CHECKPOINT',saved['notes'])
        self.assertEqual(L.gate(self.item),'run')

    def accepted_branch(self):
        (self.repo/'src/a.cpp').write_text('int a = 10;\n')
        sha=self.commit('accepted fix')
        self.git('branch','accepted',sha)
        return {'cluster':'123','branch':'accepted','commit':sha,'sha':sha[:12]}

    def test_integration_request_is_idempotent_and_pinned(self):
        candidate=self.accepted_branch()
        p=I.queue(candidate,'conflict',self.item,self.base)
        first=p.read_bytes()
        q=I.queue(candidate,'conflict',self.item,self.base)
        self.assertEqual(p,q);self.assertEqual(first,q.read_bytes())
        job=L.read(p)
        self.assertEqual(job['item']['cluster'],'123')
        self.assertEqual(job['item']['integration']['target_commit'],self.base)
        self.assertNotIn('-port',p.name)

    def test_integration_waits_until_validated_target_activated(self):
        candidate=self.accepted_branch()
        I.defer(candidate,'conflict',self.item)
        self.assertIsNone(I.next_job(set())[0])
        self.assertEqual(I.activate(self.base),['123'])
        job,_=I.next_job(set())
        self.assertEqual(job['item']['integration']['target_commit'],self.base)

    def test_real_three_way_conflict_only_touches_owned_tree(self):
        candidate=self.accepted_branch()
        self.git('checkout','-q',self.base)
        (self.repo/'src/a.cpp').write_text('int a = 20;\n')
        target=self.commit('new train changes same code')
        p=I.queue(candidate,'conflict',self.item,target)
        job=L.read(p);item=job['item']
        wt=self.root/'integration-tree'
        self.git('worktree','add','--detach',str(wt),target)
        run=self.root/'integration-run';run.mkdir()
        I.apply(item,wt,run)
        self.assertEqual(self.git('rev-parse','accepted'),candidate['commit'])
        self.assertEqual(self.git('diff','--name-only','--diff-filter=U',repo=wt),'src/a.cpp')
        self.assertEqual(L.read(run/'integration-evidence.json')['allowed_files'],['src/a.cpp'])

    def test_accepted_integrations_do_not_repeat_implementation_diagnosis(self):
        self.event();self.event()
        item={**self.item,'integration':{'source_commit':'accepted'}}
        self.assertEqual(L.gate(item),'run')
        self.event(item,'integration');self.event(item,'integration')
        self.assertEqual(L.gate(item),'exhausted')

    def test_manifest_updates_are_deterministic_and_scoped(self):
        (self.repo/'src/new.cpp').write_text('int b = 1;\n')
        (self.repo/'src/a.cpp').write_text('int a = 2;\n')
        self.commit('fix with missing registries')
        paths=E.housekeeping(self.repo,self.base,self.item)
        self.assertIn('tests/fork-sources.txt',paths)
        self.assertIn('src/new.cpp',(self.repo/'tests/fork-sources.txt').read_text())
        self.assertIn('src/a.cpp',(self.repo/'tests/upstream-modifications.txt').read_text())
        self.assertNotIn('old.cpp',(self.repo/'tests/all-sources.txt').read_text())
        self.assertEqual(self.git('status','--porcelain'),'')
        self.assertEqual(E.housekeeping(self.repo,self.base,self.item),[])

    def test_dirty_work_is_not_committed_by_housekeeping(self):
        (self.repo/'src/a.cpp').write_text('uncommitted work\n')
        head=self.git('rev-parse','HEAD')
        self.assertEqual(E.housekeeping(self.repo,self.base,self.item),[])
        self.assertEqual(self.git('rev-parse','HEAD'),head)
        self.assertEqual((self.repo/'src/a.cpp').read_text(),'uncommitted work\n')

    def test_proof_reuse_survives_docs_only_commit_but_not_code_changes(self):
        (self.repo/'src/a.cpp').write_text('int a = 2;\n');self.commit('fix')
        calls=[]
        def prover(wt,base,run,report):
            calls.append(run)
            folder=Path(run)/'negctl';folder.mkdir(parents=True)
            for name in ['test-restored.log','test-reverted.log']:(folder/name).write_text('observed test result\n')
            return {'ok':True,'detail':'red then green','tests':['ATest']}
        first=E.prove(self.repo,self.base,self.root/'run1','',prover)
        (self.repo/'docs').mkdir();(self.repo/'docs/report.md').write_text('facts\n');self.commit('report only')
        second=E.prove(self.repo,self.base,self.root/'run2','',prover)
        self.assertTrue(second['reused']);self.assertEqual(len(calls),1)
        self.assertTrue(E.valid_proof(self.repo,self.base,first))
        (self.repo/'src/a.cpp').write_text('int a = 3;\n');self.commit('different fix')
        self.assertFalse(E.valid_proof(self.repo,self.base,first))
        E.prove(self.repo,self.base,self.root/'run3','',prover)
        self.assertEqual(len(calls),2)

    def test_build_inputs_and_missing_artifacts_invalidate_proof(self):
        (self.repo/'build').mkdir();cache=self.repo/'build/CMakeCache.txt';cache.write_text('CONFIG=1\n')
        folder=self.root/'proof';folder.mkdir()
        for name in ['test-restored.log','test-reverted.log']:(folder/name).write_text('observed\n')
        proof={'ok':True,'proof_key':E.proof_key(self.repo,self.base),'proof_dir':str(folder),
               'artifact_hashes':E.artifact_hashes(folder)}
        self.assertTrue(E.valid_proof(self.repo,self.base,proof))
        (folder/'test-reverted.log').write_text('changed evidence\n')
        self.assertFalse(E.valid_proof(self.repo,self.base,proof))
        (folder/'test-reverted.log').write_text('observed\n')
        cache.write_text('CONFIG=2\n')
        self.assertFalse(E.valid_proof(self.repo,self.base,proof))
        cache.write_text('CONFIG=1\n');(folder/'test-reverted.log').unlink()
        self.assertFalse(E.valid_proof(self.repo,self.base,proof))

    def test_luna_context_capture_is_per_session(self):
        run=self.root/'run';run.mkdir()
        sid='12345678-1234-1234-1234-123456789abc'
        (run/'session.log').write_text('session id: '+sid+'\n')
        R.capture_luna(run,'gpt-6-luna')
        self.assertEqual(L.read(run/'luna-session.json')['session'],sid)

    def test_report_deduplicates_ports_and_separates_claims(self):
        sha=self.base
        for alias,files in [('123',['src/a.cpp']),('123-port',['src/a.cpp']),('456',[])]:
            folder=self.root/'runs'/(alias+'.r1');folder.mkdir()
            L.write(folder/'record.json',{'cluster':alias,'minutes':2,'files':files})
        (self.root/'ACCEPTED.tsv').write_text(f'123\tfirst\t{sha[:12]}\tACCEPT\n123-port\tsecond\t{sha[:12]}\tACCEPT\n456\tclaim\t{sha[:12]}\tCONFIRM\n')
        with patch.object(WR.subprocess,'run',return_value=subprocess.CompletedProcess([],0,sha+'\n','')):
            result=WR.refresh()
        self.assertEqual(result['delivered']['source_fixes'],1)
        self.assertEqual(result['delivered']['claims_closed'],1)
        self.assertNotEqual(result['issues']['456']['delivery'],'integrated')


    def test_controller_setup_failure_does_not_escalate_models(self):
        q=importlib.import_module('qwenfix')
        dirs={name:self.root/name for name in ['state','runs','rework','parked','logs','qa/pending','qa/done','qa/reviews']}
        for folder in dirs.values():folder.mkdir(parents=True,exist_ok=True)
        with patch.object(q,'ROOT',self.root), patch.object(q,'D',dirs), patch.object(q,'prepare_tree',return_value=(self.repo,'owned','missing toolchain')), patch.object(q,'log'), patch.object(q,'bug_escalate') as escalate:
            q.fix_one({'item':self.item,'round':4},None,'luna')
        escalate.assert_not_called()
        self.assertEqual(L.read(self.root/'state/123.json')['outcome'],'blocked-build')
        self.assertEqual(L.read(self.root/'rework/123.json')['round'],4)

    def test_controller_luna_resume_command_is_persistent(self):
        q=importlib.import_module('qwenfix')
        run=self.root/'resume-run';run.mkdir()
        sid='12345678-1234-1234-1234-123456789abc'
        L.write(run/'luna-resume.json',{'session':sid})
        with patch.object(q,'light_engine',return_value='luna'), patch.object(q,'light_cmd',side_effect=lambda x:x):
            cmd=q.session_command('luna',self.repo,run)
        self.assertEqual(cmd[cmd.index('resume')+1],sid)
        self.assertNotIn('--ephemeral',cmd)
        self.assertLess(cmd.index('-C'),cmd.index('resume'))

    def test_admission_requires_diagnosis_before_third_implementation(self):
        self.event();self.event()
        fix=unittest.mock.Mock()
        with patch.object(R,'diagnose') as diagnosis:
            R.run({'item':self.item,'round':3},None,'luna',fix,{'log':lambda *a:None})
        diagnosis.assert_called_once();fix.assert_not_called()

    def test_actual_session_gets_one_finished_ledger_event(self):
        job={'item':self.item,'round':1}
        folder=self.root/'runs/123.r1';folder.mkdir()
        driver={'log':lambda *a:None,'D':{'runs':self.root/'runs'},'slug':str}
        def fix(job,path,engine):
            R.session_started(job,folder,'model')
            L.write(folder/'record.json',{'cluster':'123','round':1,'minutes':2,'rc':0})
        R.run(job,None,'luna',fix,driver)
        events=[e for e in L.get(self.item)['events'] if e['kind']=='implementation']
        self.assertEqual(len(events),1);self.assertEqual(events[0]['state'],'finished')
        self.assertEqual(events[0]['minutes'],2)

    def test_merge_train_defers_integration_without_new_port_identity(self):
        mt=importlib.import_module('merge_train')
        candidate=self.accepted_branch()
        L.write(self.root/'state/123.json',{'item':self.item})
        with patch.object(mt,'ROOT',self.root),patch.object(mt,'M',self.root/'merge'),patch.object(mt,'log'):
            mt.port_job(candidate,'conflict in src/a.cpp')
        self.assertTrue((self.root/'integration-pending/123.json').exists())
        self.assertFalse(list((self.root/'rework').glob('*-port.json')))
        self.assertEqual((self.root/'merge/MERGE-EXCLUDE.txt').read_text().strip(),'accepted')

    def test_claim_only_completion_does_not_demand_new_regression(self):
        sc=importlib.import_module('submit_check')
        report=self.repo/'docs/qwen-fixes-2026-10/cluster-123.md'
        report.parent.mkdir(parents=True)
        report.write_text('FALSE-POSITIVE\nEvidence: src/a.cpp:1 checks this already.\n## Self-check\nVerified the cited contract.\n')
        def git(wt,*args):
            if args[:2]==('diff','--name-only'):return 'docs/qwen-fixes-2026-10/cluster-123.md\n'
            if args[0]=='merge-base':return self.base
            if args[0]=='log':return 'committed report\n'
            return ''
        with patch.object(sc,'ROOT',self.root),patch.object(sc,'git',side_effect=git),patch.object(sc.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'PASS\n','')):
            checks,_=sc.check(self.repo,'123')
        self.assertEqual(next(c[0] for c in checks if c[1]=='tests added or changed'),'PASS')

    def test_vendor_sources_are_not_registered_as_product_code(self):
        (self.repo/'vendor').mkdir();(self.repo/'vendor/dependency.cpp').write_text('int vendor;\n')
        self.commit('vendor addition')
        E.housekeeping(self.repo,self.base,self.item)
        self.assertNotIn('vendor/dependency.cpp',(self.repo/'tests/fork-sources.txt').read_text())
        self.assertNotIn('vendor/dependency.cpp',(self.repo/'tests/all-sources.txt').read_text())

    def test_maintenance_gate_dispatches_no_new_work(self):
        (self.root/'WORKFLOW-MAINTENANCE').touch()
        fn=unittest.mock.Mock(return_value=({'item':self.item},None))
        self.assertEqual(R.selector(fn)(),(None,None))
        fn.assert_not_called()


    def test_reverted_accepted_patch_is_reapplied_from_original_patch_base(self):
        candidate=self.accepted_branch()
        run=self.root/'runs/123.r1';run.mkdir()
        L.write(run/'record.json',{'cluster':'123','round':1,'branch':'accepted','head_before':self.base,'head_after':candidate['commit']})
        self.git('revert','--no-edit',candidate['commit'])
        target=self.git('rev-parse','HEAD')
        request=L.read(I.queue(candidate,'regression',self.item,target,True))
        self.assertEqual(request['item']['integration']['source_base'],self.base)
        wt=self.root/'reverted-integration';self.git('worktree','add','--detach',str(wt),target)
        output=self.root/'reverted-output';output.mkdir()
        I.apply(request['item'],wt,output)
        self.assertEqual((wt/'src/a.cpp').read_text(),'int a = 10;\n')

    def test_pre_model_preparation_exception_blocks_instead_of_spinning(self):
        driver={'D':{'runs':self.root/'runs'},'PROG':self.root,'slug':str,'log':lambda *a:None}
        job={'item':self.item,'round':1}
        with self.assertRaises(RuntimeError):
            R.run(job,None,'luna',lambda *args:(_ for _ in ()).throw(RuntimeError('bad integration inputs')),driver)
        self.assertEqual(L.gate(self.item),'blocked')
        self.assertEqual(L.get(self.item)['status'],'blocked-build')

    def test_migration_preserves_active_and_committed_port_work(self):
        migration=importlib.import_module('migrate_workflow')
        candidate=self.accepted_branch()
        (self.root/'FIX-BASE').write_text(self.base+'\n')
        (self.root/'ACCEPTED.tsv').write_text(f'123\taccepted\t{candidate["sha"]}\tACCEPT\n')
        job={'item':{**self.item,'cluster':'123-port'},'round':1,'notes':'PORT JOB was accepted as accepted'}
        path=self.root/'rework/123-port.json';L.write(path,job)
        self.assertEqual(migration.migrate(['123'],True),[])
        self.assertTrue(path.exists())
        run=self.root/'runs/123-port.r1';run.mkdir()
        L.write(run/'record.json',{'cluster':'123-port','commits':['preserved correction']})
        self.assertEqual(migration.migrate([],True),[])
        (run/'record.json').unlink()
        results=migration.migrate([],True)
        self.assertEqual(len(results),1)
        self.assertFalse(path.exists())
        self.assertTrue((self.root/'integrations/123.json').exists())


    def test_batch_reservations_reach_dispatcher_claim_set(self):
        claimed=set()
        def select(claimed=(),**kwargs):
            claimed.add('456')
            return {'item':{**self.item,'batch':[{'cluster':'456','title':'other defect'}]},'round':1},None
        job,_=R.selector(select)(claimed)
        self.assertIn('456',claimed)
        self.assertEqual(job['item']['batch'][0]['cluster'],'456')

    def test_batch_usage_is_shared_and_each_issue_keeps_its_budget(self):
        item={**self.item,'batch':[{'cluster':'456','title':'second defect'}]}
        job={'item':item,'round':1}
        folder=self.root/'runs/123.r1';folder.mkdir()
        driver={'log':lambda *a:None,'D':{'runs':self.root/'runs'},'slug':str}
        def fix(job,path,engine):
            R.session_started(job,folder,'model')
            L.write(folder/'record.json',{'cluster':'123','minutes':4,'rc':0})
        R.run(job,None,'luna',fix,driver)
        parent=L.get(item)['events'][-1]
        child=L.get('456')['events'][-1]
        self.assertEqual(parent['minutes'],2);self.assertEqual(child['minutes'],2)
        self.assertEqual(parent['session_group'],child['session_group'])
        self.assertEqual(child['kind'],'implementation')

    def test_report_recovers_historical_accepted_batch_members(self):
        folder=self.root/'runs/123.r1';folder.mkdir()
        rec={'cluster':'123','minutes':4,'head_after':self.base,'qwen_verdict':'FIXED','files':['src/a.cpp','tests/test.cpp']}
        L.write(folder/'record.json',rec)
        reviewed={**rec,'item':{'cluster':'123','batch':[{'cluster':'456','file':'src/a.cpp'}]},'batch_verdicts':{'456':'FIXED'}}
        L.write(self.root/'qa/done/123.r1.json',reviewed)
        (self.root/'ACCEPTED.tsv').write_text(f'123\taccepted\t{self.base[:12]}\tACCEPT\n456\taccepted\t{self.base[:12]}\tACCEPT\n')
        with patch.object(WR.subprocess,'run',return_value=subprocess.CompletedProcess([],0,self.base+'\n','')):
            report=WR.refresh()
        self.assertEqual(report['delivered']['source_fixes'],2)
        self.assertEqual(report['issues']['123']['session_minutes'],2)
        self.assertEqual(report['issues']['456']['session_minutes'],2)


    def test_train_waits_for_integration_cohort_before_building(self):
        mt=importlib.import_module('merge_train')
        (self.root/'FIX-BASE').write_text('050/m0-train-1\n')
        with patch.object(mt,'ROOT',self.root),patch.object(mt,'log'),patch.object(mt,'candidates') as candidates,patch.object(I,'unsettled',return_value=['123']):
            self.assertEqual(mt.run(),0)
        candidates.assert_not_called()

    def test_accepted_lower_tier_feature_can_integrate_on_enabled_claude_lane(self):
        item={**self.item,'cluster':'Q050-123','feature_tier':'muse'}
        candidate=self.accepted_branch();candidate['cluster']='Q050-123'
        I.queue(candidate,'conflict',item,self.base)
        job,_=I.next_job(set(),{'name':'enabled','tiers':['sonnet']})
        self.assertIsNotNone(job)
        self.assertEqual(job['item']['feature_tier'],'sonnet')

    def test_manual_numbered_feature_port_recovers_its_specification(self):
        mt=importlib.import_module('merge_train')
        program=self.root/'program';(program/'plans').mkdir(parents=True)
        (program/'plans/queue-050.jsonl').write_text(json.dumps({'queue_id':'Q050-114','title':'feature','acceptance':['works']})+'\n')
        candidate=self.accepted_branch();candidate['cluster']='Q050-114-port10'
        with patch.object(mt,'ROOT',self.root),patch.object(mt,'M',self.root/'merge'),patch.object(mt,'PROG',program),patch.object(mt,'WT',self.repo),patch.object(mt,'log'):
            mt.port_job(candidate,'conflict')
        self.assertTrue((self.root/'integration-pending/Q050-114.json').exists())


    # ---- review fixes 2026-10-24
    def test_usage_limit_pause_does_not_block_the_issue(self):
        class LunaLimit(Exception): pass
        runs=self.root/'runs'
        driver={'D':{'runs':runs,'state':self.root/'state'},'PROG':self.root,'slug':str,'log':lambda *a:None,'LunaLimit':LunaLimit}
        def session_hits_limit(job,rework_file,engine):
            run=runs/'123.r1';run.mkdir()
            R.session_started(job,run,'gpt-6-luna')
            (runs/'_usage-limit').mkdir()
            run.rename(runs/'_usage-limit'/'123.r1-1')
            raise LunaLimit('usage limit - not an attempt')
        with self.assertRaises(LunaLimit):
            R.run({'item':dict(self.item),'round':1},None,'luna',session_hits_limit,driver)
        self.assertEqual(L.gate(self.item),'run')
        self.assertFalse(L.get(self.item)['status'].startswith('blocked'))
        self.assertTrue(all(e.get('cancelled') for e in L.get(self.item)['events'] if e['kind']=='implementation'))

    def test_integration_build_failure_requeues_in_place(self):
        candidate=self.accepted_branch()
        p=I.queue(candidate,'conflict',self.item,self.base)
        job,path=I.next_job(set())
        driver={'D':{'state':self.root/'state'},'slug':str,'log':lambda *a:None}
        R.build_failed(job,self.repo,'cmake failed',driver)
        self.assertFalse((self.root/'rework/123.json').exists())
        self.assertEqual(path,p)
        self.assertNotIn('_integration_path',L.read(p))
        self.assertEqual(L.gate(self.item),'blocked')

    def test_mechanical_bounce_is_not_the_one_correction(self):
        self.event();self.event(kind='mechanical')
        self.assertEqual(L.gate(self.item),'run')
        self.event()
        self.assertEqual(L.gate(self.item),'diagnostic')

    def test_activation_failure_keeps_request_pending_and_continues(self):
        candidate=self.accepted_branch()
        I.defer(candidate,'conflict',self.item)
        broken={**self.item,'cluster':'456','worktree':str(self.root/'missing-tree')}
        I.defer({**candidate,'cluster':'456'},'conflict',broken)
        errors=[]
        self.assertEqual(I.activate(self.base,errors),['123'])
        self.assertEqual(len(errors),1)
        self.assertTrue((self.root/'integration-pending/456.json').exists())

    def test_integration_cohort_has_a_cap_and_needs_a_runnable_lane(self):
        candidate=self.accepted_branch()
        I.queue(candidate,'conflict',self.item,self.base)
        feature={**self.item,'cluster':'Q050-9'}
        I.queue({**candidate,'cluster':'Q050-9'},'conflict',feature,self.base)
        overdue=[]
        self.assertEqual(I.unsettled(24,overdue),['123'])        # no FEATURES-ON: the feature cannot run
        self.assertIn('Q050-9 (no open Claude feature lane)',overdue)
        (self.root/'FEATURES-ON').write_text('')
        L.write(self.root/'LANES.json',{'lanes':[{'name':'s','tiers':['sonnet']}]})
        self.assertEqual(I.unsettled(24),['123','Q050-9'])
        job=L.read(self.root/'integrations/123.json')
        job['item']['integration']['queued_at']='2000-01-01T00:00:00'
        L.write(self.root/'integrations/123.json',job)
        overdue=[]
        self.assertEqual(I.unsettled(24,overdue),['Q050-9'])
        self.assertTrue(overdue[0].startswith('123 (waiting'))

    def test_held_transition_is_logged_once(self):
        L.block(self.item,'blocked-build','missing target',self.repo)
        L.block(self.item,'blocked-build','missing target',self.repo)
        L.gate(self.item);L.gate(self.item)
        rows=[json.loads(l) for l in (self.root/'issues/held.log').read_text().splitlines()]
        self.assertEqual([r['status'] for r in rows],['blocked-build'])
        for _ in range(8):
            self.event()           # a new session resets the status, as the dispatcher's start does
        L.gate(self.item);L.gate(self.item)
        rows=[json.loads(l) for l in (self.root/'issues/held.log').read_text().splitlines()]
        self.assertEqual([r['status'] for r in rows],['blocked-build','budget-exhausted'])

    def test_integration_outside_patch_files_are_named(self):
        candidate=self.accepted_branch()
        p=I.queue(candidate,'conflict',self.item,self.base)
        item=L.read(p)['item']
        changed=['src/a.cpp','src/other.cpp','tests/fork-sources.txt']
        self.assertEqual(I.outside_patch(item,self.repo,changed),['src/other.cpp'])

    def test_manifest_recipe_uses_the_header_block_only(self):
        mt=importlib.import_module('merge_train')
        header=['# The list is the output of the command under "Regenerate with", and so on.',
                '# Regenerate with (the whole command):',
                "#   { git ls-files '*.cpp' \\",
                "#       | grep -vE '^(vendor/|sfizz/)' ; } | LC_ALL=C sort -u",
                '# Verify it - this must print REPRODUCES:',
                '#   diff ...']
        self.assertEqual(mt.manifest_recipe(header),
                         "{ git ls-files '*.cpp' \\\n      | grep -vE '^(vendor/|sfizz/)' ; } | LC_ALL=C sort -u")
        self.assertEqual(mt.manifest_recipe(['# no recipe here']),'')

    def test_feature_spec_recovery_skips_blank_queue_lines(self):
        mt=importlib.import_module('merge_train')
        program=self.root/'program';(program/'plans').mkdir(parents=True)
        (program/'plans/queue-050.jsonl').write_text('\n'+json.dumps({'queue_id':'Q050-114','title':'feature'})+'\n\n')
        candidate=self.accepted_branch();candidate['cluster']='Q050-114-port2'
        with patch.object(mt,'ROOT',self.root),patch.object(mt,'M',self.root/'merge'),patch.object(mt,'PROG',program),patch.object(mt,'WT',self.repo),patch.object(mt,'log'):
            mt.port_job(candidate,'conflict')
        self.assertTrue((self.root/'integration-pending/Q050-114.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
