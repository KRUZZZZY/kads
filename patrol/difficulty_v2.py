#!/usr/bin/env python3
"""Actual Clef difficulty calls conditional on realness; preserve complete v1 journals."""
import argparse, collections, hashlib, json, pathlib, urllib.request
import classify
import difficulty

QUESTIONS = {
 'easy': {'type':'noul','instructions':'CONDITIONAL ON THIS FINDING BEING REAL: could its repair be a small local patch preserving intended contracts and unrelated behavior, with a practical deterministic regression or quick source/runtime refutation strategy? Correcting erroneous behavior is explicitly allowed. You are estimating repair scope, NOT verifying realness. An easy rating does NOT require this triage to already prove the defect. Input validation, checked arithmetic, state/result cleanup and test/gate bookkeeping with a clear local strategy are usually easy. Thread ownership architecture, RT concurrency, design/API decisions, hardware/platform dependencies, or substantial cross-component redesign are not easy.'},
 'hard': {'type':'noul','instructions':'CONDITIONAL ON THIS FINDING BEING REAL: would repairing it need concurrency/lifetime architecture, RT coordination, design/API decisions, platform/hardware/dependency changes, or substantial cross-component redesign? Do not confuse uncertain realness with repair difficulty. Local bounded validation, state cleanup and deterministic test preservation can be easy even before the hypothesis has been proven.'},
 'context': {'type':'noul','instructions':'Is supplied context sufficient to estimate repair scope and a regression/quick-refutation strategy? It need NOT be enough to verify that the finding is real. Uncertain realness alone is NOT missing difficulty context. Missing architecture/caller evidence matters only if it prevents estimating the repair scope or testing strategy.'},
}

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--out',required=True);ap.add_argument('--host',default='http://127.0.0.1:11434');args=ap.parse_args();out=pathlib.Path(args.out)
 with urllib.request.urlopen(args.host+'/api/tags',timeout=20) as r:models=json.load(r)['models']
 current=next(m for m in models if m['name'].split(':')[0]=='clef-flash');model=json.loads((out/'model.json').read_text())
 if model['digest']!=current['digest']:raise SystemExit('Refusing changed model digest')
 archive=out/'v1';archive.mkdir(exist_ok=True)
 if not (archive/'decisions.jsonl').exists():
  for name in ['metadata.json','decisions.jsonl','COUNTS.json','verification.json','SUMMARY.md','review-first-pass.jsonl','review-rechecks.jsonl','COMPLETE','REVIEW_RECHECK_COMPLETE']:
   if (out/name).exists():(archive/name).write_bytes((out/name).read_bytes())
 legacy=classify.read_rows(archive/'decisions.jsonl');new=classify.read_rows(out/'decisions-v2.jsonl');done={d['cluster'] for d in new}
 metadata={'question_version':2,'questions':QUESTIONS,'model':model,'source_pins':'metadata.json','inventory':'inventory/part-*.jsonl','v1_decisions_sha256':hashlib.sha256((archive/'decisions.jsonl').read_bytes()).hexdigest()}
 meta_path=out/'metadata-v2.json'
 if meta_path.exists() and json.loads(meta_path.read_text())!=metadata:raise SystemExit('Refusing changed v2 questions/model/legacy journal')
 difficulty.write(meta_path,metadata);(out/'COMPLETE').unlink(missing_ok=True)
 def update(inventory):
  # Pending v1 easy suggestions stay visible until v2 answers the same stable ID;
  # executing jobs are owned by the parent scheduler and are never cancelled here.
  pending=[d for d in legacy if d['cluster'] not in done and d['difficulty']=='easy']
  operational=[{**d,'legacy_pending_v2':True} for d in pending]+new
  difficulty.queues(out,inventory,operational)
  difficulty.write(out/'COUNTS.json',{'total':len(inventory),'classified':len(new),'question_version':2,'difficulty':dict(collections.Counter(d['difficulty'] for d in new)),'pending_legacy_easy':len(pending)})
  tmp=out/'decisions.jsonl.tmp';tmp.write_text(''.join(json.dumps(d)+'\n' for d in operational));tmp.replace(out/'decisions.jsonl')
 inventory=difficulty.load_inventory(out);update(inventory);sources={};trees={}
 while True:
  inventory=difficulty.load_inventory(out);pending=[i for i in inventory if i['cluster'] not in done]
  # Review corrections first; then current8; then historical/unassigned items.
  pending.sort(key=lambda i:(not isinstance(i['cluster'],str),i['lane'] not in difficulty.CURRENT,i['lane'],str(i['cluster'])))
  if not pending:break
  item=pending[0];wt,rev=item['worktree'],item['revision'];key=(wt,rev)
  if key not in trees:trees[key]=classify.git(wt,'ls-tree','-r','--name-only',rev).splitlines()
  paths=trees[key];candidates=[p for p in paths if pathlib.Path(p).name==pathlib.Path(item['file']).name];path=item['file'] if item['file'] in paths else candidates[0] if len(candidates)==1 else ''
  codekey=(wt,rev,path)
  if codekey not in sources:sources[codekey]=classify.git(wt,'show',f'{rev}:{path}') if path else ''
  state={'contracts':classify.CONTRACTS,'revision':rev,'file':path,'current_code':item.get('source_context_override') or classify.source_context(sources[codekey],[item],18000),'finding':item['text'],'prior_report_evidence':item['report_evidence'],'scope':'Difficulty CONDITIONAL on the hypothesis being real. Preserving behavior means intended contracts/unrelated behavior, NOT preserving erroneous behavior. Defect correction and deterministic quick refutation are allowed. This call does not establish realness or permission to weaken tests.'}
  if isinstance(item['cluster'],str):state['reviewed_commit_hunks']=classify.git(wt,'show','--format=',item['original_revision'],'--',path)[:16000] if path else ''
  if item['lane']=='script':state['dependency_evidence']=json.loads((out/'review-amendments.json').read_text()).get('script_dependency_fact','')
  answers=classify.post(args.host,state,QUESTIONS)
  decision={'cluster':item['cluster'],'difficulty':difficulty.label(answers,bool(path)),'context_required':answers.get('context',{}).get('noul',.5)<.75,'question_version':2,'conditional_on_realness':True,'resolved_path':path,'answers':answers,'context_sha256':hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest(),'model':model['name'],'model_digest':model['digest']}
  classify.append(out/'decisions-v2.jsonl',[decision]);new.append(decision);done.add(item['cluster']);update(inventory)
  print(json.dumps({'classified':len(new),'total':len(inventory),'cluster':item['cluster'],'difficulty':decision['difficulty'],'question_version':2}),flush=True)
 (out/'COMPLETE').write_text('Every retained finding has an actual conditional-difficulty v2 Clef call; realness and fixes remain unverified.\n')
if __name__=='__main__':main()
