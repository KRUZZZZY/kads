#!/usr/bin/env python3
"""Freeze remaining measured findings, then journal resumable local Clef difficulty suggestions."""
import argparse, collections, hashlib, json, pathlib, re, subprocess, urllib.request
import classify

CURRENT = ['cmds1', 'cmds2', 'control', 'core', 'engine', 'gui', 'plugins', 'script']
CLOSED = {'FIXED', 'ALREADY-FIXED', 'DUPLICATE', 'FALSE-POSITIVE'}
QUESTIONS = {
 'easy': {'type': 'noul', 'instructions': 'Can this finding be repaired with a small local deterministic behaviour-preserving patch and a practical failing-then-passing regression in this Linux Qt6 tree? Answer yes ONLY if code supplied establishes a concrete defect and patch. Thread ownership, realtime concurrency, design/API contract changes, platform hardware, external dependencies, lifecycle architecture, or missing callers are NOT easy.'},
 'hard': {'type': 'noul', 'instructions': 'Does repairing this finding require concurrency/lifetime architecture, real-time coordination, design/API decisions, platform hardware or dependency changes, substantial cross-component work? Local input validation/arithmetic/serialization bounds with clear existing contracts are usually easy.'},
 'context': {'type': 'noul', 'instructions': 'Is supplied code and evidence enough to confidently decide difficulty and identify a concrete regression strategy? Missing relevant callers/contracts/lifetime evidence means no. Prior model reports are hypotheses, not proof.'},
}

def rows(path):
 return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
def write(path, value):
 path.parent.mkdir(parents=True, exist_ok=True)
 tmp = path.with_suffix(path.suffix + '.tmp')
 tmp.write_text(json.dumps(value, indent=2) + '\n'); tmp.replace(path)
def sections(text):
 matches = list(re.finditer(r'^#{1,6}\s+Cluster\s+(\d+)\b[^\n]*', text, re.M | re.I))
 for i, m in enumerate(matches):
  yield int(m[1]), text[m.start():matches[i+1].start() if i+1<len(matches) else len(text)]
def verdict(text):
 m = re.search(r'Verdict\s*:\s*(?:\*\*)?([A-Z]+(?:[- ][A-Z]+)?)', text, re.I)
 return m[1].upper().replace(' ', '-') if m else 'UNREPORTED'
def label(answers, resolved):
 def n(key): return answers.get(key, {}).get('noul', .5)
 if not resolved: return 'needs_context'
 if n('easy') >= .85 and n('hard') <= .25 and n('context') >= .75: return 'easy'
 if n('hard') >= .75: return 'hard'
 return 'needs_context'
def freeze(args):
 prog, out, runtime = pathlib.Path(args.program), pathlib.Path(args.out), pathlib.Path(args.runtime)
 snapshot = prog/'research/reviews/luna-clef-2026-10-05/findings-snapshot.jsonl'
 findings = rows(snapshot); clusters = rows(prog/'research/reviews/luna-dedup-2026-10-05/run-v2/clusters.jsonl')
 prior = {d['id']:d for d in rows(prog/'research/reviews/luna-clef-2026-10-05/decisions.jsonl')}
 mapping = {m:c['representative'] for c in clusters for m in c['members']}
 members = {c['representative']:c['members'] for c in clusters}
 assigned = collections.defaultdict(list); reports=collections.defaultdict(list); tips={}
 for lane_dir in sorted((runtime/'lanes').iterdir()):
  cfg = json.loads((lane_dir/'lane.json').read_text()); lane=lane_dir.name; wt=pathlib.Path(cfg['worktree'])
  rev=classify.git(wt,'rev-parse','HEAD').strip(); tips[lane]={'worktree':str(wt),'revision':rev}
  for brief in sorted((lane_dir/'batches').glob('batch-*.md')):
   for cid, text in sections(brief.read_text()):
    cid=mapping.get(cid,cid); assigned[cid].append({'lane':lane,'batch':brief.stem,'brief':str(brief),'sha256':hashlib.sha256(brief.read_bytes()).hexdigest()})
  for report in sorted((wt/'docs/bunny-fixes-2026-10-05'/lane).glob('batch-*.md')):
   for cid,text in sections(report.read_text()):
    cid=mapping.get(cid,cid); reports[cid].append({'lane':lane,'path':str(report),'sha256':hashlib.sha256(report.read_bytes()).hexdigest(),'verdict':verdict(text),'text':text})
 baseline=pathlib.Path(args.baseline); base_rev=classify.git(baseline,'rev-parse','HEAD').strip()
 inventory=[]; dispositions=[]
 for cid, ids in members.items():
  evidence=reports[cid]; known=[e['verdict'] for e in evidence]
  # Terminal claims are preserved in evidence; any contradictory open report keeps the item queued.
  closed=bool(known) and all(v in CLOSED for v in known)
  plausible=any(prior.get(i,{}).get('disposition')=='likely_open_bug' for i in ids)
  assignments=assigned[cid]
  disposition={'cluster':cid,'members':ids,'assignments':assignments,'report_evidence':evidence,'terminal_report_claim':closed,'queued':not closed and (bool(assignments) or plausible)}
  dispositions.append(disposition)
  if not disposition['queued']: continue
  pool=[i for i in ids if prior.get(i,{}).get('disposition')=='likely_open_bug'] or ids
  best=max(pool,key=lambda i:(findings[i].get('pass',0),prior.get(i,{}).get('answers',{}).get('real',{}).get('score',0)))
  f=findings[best]; lane=next((a['lane'] for a in assignments if a['lane'] in CURRENT), assignments[0]['lane'] if assignments else 'unassigned')
  pin=tips.get(lane,{'worktree':str(baseline),'revision':base_rev})
  inventory.append({'cluster':cid,'members':ids,'finding_id':best,'lane':lane,**pin,'title':f['title'],'file':f['file'],'line':f.get('line',0),'text':f['text'],'original_revision':f.get('rev'),'assignments':assignments,'report_evidence':evidence,'prior_disposition':prior.get(best,{}).get('disposition')})
 inventory.sort(key=lambda i:(i['lane'] not in CURRENT, CURRENT.index(i['lane']) if i['lane'] in CURRENT else 99,i['file'],i['cluster']))
 for name,data in [('inventory.jsonl',inventory),('dispositions.jsonl',dispositions)]:
  # Shard inventory/evidence to remain below the evidence gate's 1MiB per-file cap.
  target=out/name.removesuffix('.jsonl'); target.mkdir(parents=True,exist_ok=True)
  for k in range(0,len(data),50):
   (target/f'part-{k//50:03d}.jsonl').write_text(''.join(json.dumps(d)+'\n' for d in data[k:k+50]))
 write(out/'metadata.json',{'snapshot_sha256':hashlib.sha256(snapshot.read_bytes()).hexdigest(),'tips':tips,'baseline':{'worktree':str(baseline),'revision':base_rev},'items':len(inventory),'assigned_clusters':len(assigned),'questions':QUESTIONS})
 return inventory

def load_inventory(out):
 return [item for p in sorted((out/'inventory').glob('*.jsonl')) for item in rows(p)]
def queues(out, inventory, decisions):
 index={x['cluster']:x for x in inventory}
 for kind in ['easy','hard','needs_context']:
  target=out/'queue'/f'{kind}.jsonl'; target.parent.mkdir(exist_ok=True)
  items=[{**index[d['cluster']],**d} for d in decisions if d['difficulty']==kind]
  tmp=target.with_suffix('.tmp'); tmp.write_text(''.join(json.dumps(item)+'\n' for item in items)); tmp.replace(target)
  shard_dir=out/'queue'/kind; shard_dir.mkdir(exist_ok=True)
  for start in range(0,len(items),50):
   shard=shard_dir/f'part-{start//50:03d}.jsonl'; temp=shard.with_suffix('.tmp')
   temp.write_text(''.join(json.dumps(item)+'\n' for item in items[start:start+50])); temp.replace(shard)
  count=(len(items)+49)//50
  for stale in shard_dir.glob('part-*.jsonl'):
   if int(stale.stem.split('-')[1])>=count:stale.unlink()
 write(out/'COUNTS.json',{'total':len(inventory),'classified':len(decisions),'difficulty':dict(collections.Counter(d['difficulty'] for d in decisions))})
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--program',default=str(pathlib.Path(__file__).resolve().parents[2])); ap.add_argument('--runtime',default=str(pathlib.Path.home()/'.local/share/zene-bunny-fix/bunny'))
 ap.add_argument('--out',required=True); ap.add_argument('--baseline',required=True); ap.add_argument('--host',default='http://127.0.0.1:11434'); ap.add_argument('--limit',type=int); ap.add_argument('--freeze-only',action='store_true')
 args=ap.parse_args(); out=pathlib.Path(args.out); out.mkdir(parents=True,exist_ok=True)
 if (out/'metadata-v2.json').exists() and not args.freeze_only:
  raise SystemExit('This campaign uses conditional difficulty v2; resume difficulty_v2.py, not the legacy v1 runner')
 inventory=load_inventory(out) if (out/'metadata.json').exists() else freeze(args)
 decisions=rows(out/'decisions.jsonl'); queues(out,inventory,decisions)
 if args.freeze_only:return
 with urllib.request.urlopen(args.host+'/api/tags',timeout=20) as r: models=json.load(r)['models']
 model=next(m for m in models if m['name'].split(':')[0]=='clef-flash')
 model_pin={'name':model['name'],'digest':model['digest']}
 if (out/'model.json').exists() and json.loads((out/'model.json').read_text()) != model_pin:
  raise SystemExit('Refusing to mix model digests in an existing difficulty journal')
 write(out/'model.json',model_pin)
 done={d['cluster'] for d in decisions}; trees={}; source_cache={}; processed=0
 while True:
  inventory=load_inventory(out)  # Explicit supplemental review shards may arrive during the run.
  pending=[item for item in inventory if item['cluster'] not in done]
  if not pending: break
  item=pending[0]
  wt,rev=item['worktree'],item['revision']; key=(wt,rev)
  if key not in trees: trees[key]=classify.git(wt,'ls-tree','-r','--name-only',rev).splitlines()
  paths=trees[key]; candidates=[p for p in paths if pathlib.Path(p).name==pathlib.Path(item['file']).name]
  path=item['file'] if item['file'] in paths else candidates[0] if len(candidates)==1 else ''
  codekey=(wt,rev,path)
  if codekey not in source_cache: source_cache[codekey]=classify.git(wt,'show',f'{rev}:{path}') if path else ''
  state={'contracts':classify.CONTRACTS,'realtime_rule':'No allocation, locking, unbounded growth on audio-thread paths; bounded nonblocking atomic/snapshot designs are allowed.','revision':rev,'file':path,'current_code':item.get('source_context_override') or classify.source_context(source_cache[codekey],[item],14000),'finding':item['text'],'prior_report_evidence':item['report_evidence'],'task':'Difficulty only. Do not dismiss open items based on a false-positive suggestion. Easy requires local code evidence and practical deterministic regression.'}
  if item['lane']=='script':
   facts=json.loads((out/'review-amendments.json').read_text()) if (out/'review-amendments.json').exists() else {}
   state['dependency_evidence']=facts.get('script_dependency_fact','No additional verified dependency evidence supplied.')
  answers=classify.post(args.host,state,QUESTIONS)
  decision={'cluster':item['cluster'],'difficulty':label(answers,bool(path)),'context_required':answers.get('context',{}).get('noul',.5)<.75,'routing_version':2,'resolved_path':path,'answers':answers,'context_sha256':hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest(),'model':model['name'],'model_digest':model['digest']}
  classify.append(out/'decisions.jsonl',[decision]); decisions.append(decision); done.add(item['cluster']); queues(out,inventory,decisions); processed+=1
  print(json.dumps({'classified':len(decisions),'total':len(inventory),'cluster':item['cluster'],'lane':item['lane'],'difficulty':decision['difficulty']}),flush=True)
  if args.limit and processed>=args.limit:break
 if len(decisions)==len(inventory):(out/'COMPLETE').write_text('Frozen inventory classified; model suggestions require review and regression evidence.\n')
if __name__=='__main__': main()
