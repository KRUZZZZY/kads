#!/usr/bin/env python3
"""Recheck review corrections with concrete reviewed hunks, preserving first-pass answers."""
import argparse, hashlib, json, pathlib, urllib.request
import classify
import difficulty


def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--out',required=True);ap.add_argument('--host',default='http://127.0.0.1:11434');args=ap.parse_args();out=pathlib.Path(args.out)
 if (out/'metadata-v2.json').exists():raise SystemExit('This campaign uses conditional difficulty v2; legacy rechecks cannot overwrite its journal')
 if not (out/'COMPLETE').exists():raise SystemExit('First-pass classification must finish before rechecking the shared journal')
 inventory=difficulty.load_inventory(out);decisions=classify.read_rows(out/'decisions.jsonl');model=json.loads((out/'model.json').read_text())
 with urllib.request.urlopen(args.host+'/api/tags',timeout=20) as r:models=json.load(r)['models']
 current=next(m for m in models if m['name'].split(':')[0]=='clef-flash')
 if current['digest']!=model['digest']:raise SystemExit('Refusing changed Clef digest')
 first=out/'review-first-pass.jsonl'
 first_ids={d['cluster'] for d in classify.read_rows(first)}
 classify.append(first,[d for d in decisions if isinstance(d['cluster'],str) and d['cluster'] not in first_ids])
 rechecks=classify.read_rows(out/'review-rechecks.jsonl');done={r['cluster'] for r in rechecks};index={r['cluster']:r for r in decisions}
 # A journal append can precede a crash during the atomic view replacement.
 # Replay durable raw responses before skipping previously completed calls.
 index.update({r['cluster']:r for r in rechecks})
 updated=[index[d['cluster']] for d in decisions]
 tmp=out/'decisions.jsonl.tmp';tmp.write_text(''.join(json.dumps(d)+'\n' for d in updated));tmp.replace(out/'decisions.jsonl');difficulty.queues(out,inventory,updated)
 for item in inventory:
  cid=item['cluster']
  if not isinstance(cid,str) or cid in done:continue
  repo,rev,path=item['worktree'],item['revision'],item['file']
  source=classify.git(repo,'show',f'{rev}:{path}')
  patch=classify.git(repo,'show','--format=',item['original_revision'],'--',path)
  # Diff added/context lines supply exact current identifiers and line hints when
  # an original prose review has no historical line number. Include the patch as
  # evidence while retaining the pinned current revision as the source of truth.
  anchors=[]
  for line in patch.splitlines():
   if line.startswith('@@'):
    anchors.append(line)
   elif line.startswith('+') and not line.startswith('+++'):
    value=line[1:].strip()
    if 4<=len(value)<=100:anchors.append('`'+value+'`')
  finding={**item,'text':item['text']+'\n'+'\n'.join(anchors[:16])}
  state={'contracts':classify.CONTRACTS,'revision':rev,'file':path,'current_code':item.get('source_context_override') or classify.source_context(source,[finding],20000),'reviewed_commit_hunks':patch[:16000],'finding':item['text'],'prior_report_evidence':item['report_evidence'],'task':'Difficulty recheck with additional exact reviewed hunk evidence. Diff may include removed code; current pinned source is authoritative. Do not dismiss cases merely because first-pass context was insufficient. Easy still requires local deterministic patch and meaningful runnable red/green regression; architecture, concurrency, RT coordination, design decisions remain hard.'}
  if item['lane']=='script':state['dependency_evidence']=json.loads((out/'review-amendments.json').read_text()).get('script_dependency_fact','')
  answers=classify.post(args.host,state,difficulty.QUESTIONS)
  decision={'cluster':cid,'difficulty':difficulty.label(answers,bool(source)),'context_required':answers.get('context',{}).get('noul',.5)<.75,'routing_version':2,'resolved_path':path,'answers':answers,'context_sha256':hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest(),'model':model['name'],'model_digest':model['digest'],'recheck':'reviewed_hunks_v1','previous_context_sha256':index[cid]['context_sha256']}
  classify.append(out/'review-rechecks.jsonl',[decision]);index[cid]=decision
  updated=[index[d['cluster']] for d in decisions];tmp=out/'decisions.jsonl.tmp';tmp.write_text(''.join(json.dumps(d)+'\n' for d in updated));tmp.replace(out/'decisions.jsonl');difficulty.queues(out,inventory,updated)
  print(json.dumps({'cluster':cid,'difficulty':decision['difficulty'],'previous_difficulty':next(d['difficulty'] for d in decisions if d['cluster']==cid)}),flush=True)
 (out/'REVIEW_RECHECK_COMPLETE').write_text('All frozen review corrections rechecked against reviewed hunks and pinned current source.\n')
if __name__=='__main__':main()
