"""Track distinct delivered issues separately from accepted attempts and claim closures."""
from collections import Counter, defaultdict
from pathlib import Path
import json
import subprocess
import time

import issue_ledger as L


def accepted():
    rows = []
    for name in ('ACCEPTED.tsv','FEATURES-ACCEPTED.tsv'):
        path = L.ROOT/name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            parts = line.split('\t')
            if len(parts) >= 4:
                rows.append({'id':L.issue_id(parts[0]),'alias':parts[0],'branch':parts[1],
                             'sha':parts[2], 'verdict':parts[3],'feature':name.startswith('FEATURES')})
    return rows


def refresh():
    root = L.ROOT
    rows = defaultdict(lambda: {'aliases':set(),'sessions':Counter(),'session_minutes':0.0,
                               'reported_total_tokens':0,'recorded_output_tokens':0,'token_logs':0,
                               'source_edits':False,'test_edits':False,'reviews':0,'review_minutes':0.0})
    cache = L.read(root/'tokens-cache.json')
    records = {}
    verdicts_by_head = {}
    for path in (root/'runs').glob('*/record.json'):
        rec = L.read(path)
        if not rec.get('cluster'):
            continue
        reviewed = L.read(root/'qa/done'/(path.parent.name+'.json'))
        for field in ('item','batch_verdicts'):
            if field not in rec and field in reviewed:
                rec[field] = reviewed[field]
        identity = L.issue_id(rec.get('item') or rec)
        row = rows[identity]
        row['aliases'].add(str(rec['cluster']))
        row['title'] = rec.get('title') or row.get('title','')
        files = rec.get('files') or []
        row['source_edits'] |= any(f.startswith(('src/','include/','plugins/')) for f in files)
        row['test_edits'] |= any(f.startswith('tests/') for f in files)
        meta = L.read(path.parent/'workflow-event.json')
        kind = meta.get('kind') or ('integration' if str(rec['cluster']).endswith('-port') else 'implementation')
        if rec.get('minutes') is None:
            kind = 'infrastructure'
        row['sessions'][kind] += 1
        row['session_minutes'] += float(rec.get('minutes') or 0)
        log = path.parent/'session.log'
        value = None
        if log.exists():
            st = log.stat(); value = cache.get(f'{log}:{st.st_mtime_ns}:{st.st_size}')
            if value and value[1] is not None:
                row['reported_total_tokens'] += value[1]
                row['recorded_output_tokens'] += value[0] or 0
                row['token_logs'] += 1
        companions = [b for b in (rec.get('item') or {}).get('batch',[])
                      if (rec.get('batch_verdicts') or {}).get(str(b['cluster'])) in
                      ('FIXED','ALREADY-FIXED','FALSE-POSITIVE','IMPLEMENTED')]
        if companions:
            share = len(companions)+1
            child_minutes = float(rec.get('minutes') or 0)/share
            row['session_minutes'] -= child_minutes*len(companions)
            child_tokens = (value[1] or 0)//share if value else 0
            child_output = (value[0] or 0)//share if value else 0
            row['reported_total_tokens'] -= child_tokens*len(companions)
            row['recorded_output_tokens'] -= child_output*len(companions)
            for companion in companions:
                child=rows[L.issue_id(companion)]
                child['aliases'].add(str(companion['cluster']))
                child['title']=companion.get('title','')
                child['sessions'][kind]+=1
                child['session_minutes']+=child_minutes
                child['reported_total_tokens']+=child_tokens
                child['recorded_output_tokens']+=child_output
                child['source_edits'] |= row['source_edits'] and not str(companion.get('file','')).startswith('tests/')
                child['test_edits'] |= row['test_edits']
                child['shared_run']=path.parent.name
                if rec.get('head_after'):
                    verdicts_by_head[(str(companion['cluster']),rec['head_after'][:12])]=(rec.get('batch_verdicts') or {}).get(str(companion['cluster']))
        records[path.parent.name] = rec
        if rec.get('head_after'):
            verdicts_by_head[(str(rec.get('cluster')),rec['head_after'][:12])] = rec.get('qwen_verdict')
    seen_reviews = set()
    for path in (root/'qa/done').glob('*.json'):
        rec = L.read(path)
        if not rec.get('cluster'):
            continue
        row = rows[L.issue_id(rec.get('item') or rec)]
        for qa in (rec.get('qa_code') or {}, rec.get('qa') or {}):
            key = qa.get('review')
            if key and key not in seen_reviews:
                row['reviews'] += 1
                row['review_minutes'] += float(qa.get('minutes') or 0)
                seen_reviews.add(key)
    for path in (root/'issues').glob('*.json'):
        ledger = L.read(path)
        if not ledger.get('id'):
            continue
        row = rows[ledger['id']]
        row['status'] = ledger.get('status','ready')
        row['reason'] = ledger.get('reason','')
        row['diagnostics'] = sum(e['kind']=='diagnostic' and not e.get('cancelled') for e in ledger.get('events',[]))
        for event in ledger.get('events',[]):
            if event['kind'] == 'diagnostic' and not event.get('cancelled'):
                row['session_minutes'] += float(event.get('minutes') or 0)
    base = (root/'FIX-BASE').read_text().strip() if (root/'FIX-BASE').exists() else ''
    repo = Path.home()/'Documents/AI_KOS_PROJECT/projects/zene-studio-program/zene-cachyos'
    result = subprocess.run(['git','-C',str(repo),'rev-list',base],capture_output=True,text=True) if base else None
    ancestors = {h[:12] for h in result.stdout.splitlines()} if result and result.returncode==0 else None
    excluded = set((root/'merge/MERGE-EXCLUDE.txt').read_text().split()) if (root/'merge/MERGE-EXCLUDE.txt').exists() else set()
    candidates = defaultdict(list)
    for entry in accepted():
        row = rows[entry['id']]
        row['aliases'].add(entry['alias'])
        disposition = verdicts_by_head.get((entry['alias'],entry['sha'][:12]))
        if entry['verdict'] == 'CONFIRM' or disposition in ('ALREADY-FIXED','FALSE-POSITIVE'):
            row['claim_closed'] = True
            continue
        if entry['branch'] not in excluded:
            candidates[entry['id']].append(entry)
    counts = Counter()
    for identity, row in rows.items():
        entries = candidates[identity]
        delivered = [e for e in entries if ancestors is not None and e['sha'][:12] in ancestors]
        feature = identity.startswith('Q050-')
        row['delivery'] = 'integrated' if delivered else 'accepted-awaiting-integration' if entries else 'claim-closed' if row.get('claim_closed') else 'not-delivered'
        row['integrated_heads'] = [e['sha'] for e in delivered]
        if delivered:
            group = 'features' if feature else 'source_fixes' if row['source_edits'] else 'test_improvements' if row['test_edits'] else 'documentation_or_metadata'
            counts[group] += 1
            row['delivery_kind'] = group
        if row.get('claim_closed'):
            counts['claims_closed'] += 1
        if row.get('status','').startswith('blocked') or row.get('status')=='budget-exhausted':
            counts['held_issues'] += 1
        row['aliases'] = sorted(row['aliases'])
        row['sessions'] = dict(row['sessions'])
        row['session_minutes'] = round(row['session_minutes'],2)
        row['review_minutes'] = round(row['review_minutes'],2)
    totals = {'issues':len(rows),'recorded_session_minutes':round(sum(r['session_minutes'] for r in rows.values()),2),
              'recorded_review_minutes':round(sum(r['review_minutes'] for r in rows.values()),2),
              'reported_total_tokens':sum(r['reported_total_tokens'] for r in rows.values()),
              'recorded_output_tokens':sum(r['recorded_output_tokens'] for r in rows.values()),
              'token_logs':sum(r['token_logs'] for r in rows.values())}
    improvements = counts['source_fixes']+counts['test_improvements']+counts['features']
    totals['integrated_improvements_per_million_reported_tokens'] = round(improvements*1e6/totals['reported_total_tokens'],6) if totals['reported_total_tokens'] else None
    data = {'generated':time.strftime('%Y-%m-%dT%H:%M:%S'),'base':base,'ancestry_verified':ancestors is not None,
            'delivered':dict(counts),'totals':totals,'issues':dict(rows),
            'limits':'Token coverage is partial and includes input/cache counts. Review token usage is not fully recorded. Ancestry plus semantic-exclusion filtering is not a fresh runtime verification. Distinct issue IDs may still describe duplicate reports.'}
    baseline = L.read(root/'WORKFLOW-BASELINE.json')
    delivered_ids = {i for i,r in rows.items() if r.get('delivery_kind') in ('source_fixes','test_improvements','features')}
    if baseline:
        new_ids = sorted(delivered_ids-set(baseline.get('delivered_ids',[])))
        new_events = [e for p in (root/'issues').glob('*.json') for e in L.read(p).get('events',[])
                      if not e.get('historical') and not e.get('cancelled') and e['kind'] in ('implementation','mechanical','integration','diagnostic','review')]
        groups = {e.get('session_group',e['key']):e for e in new_events}
        data['since_rollout'] = {'started_at':baseline.get('generated'),'newly_integrated_issues':len(new_ids),
            'newly_integrated_ids':new_ids,'model_sessions':len(groups),
            'work_sessions':sum(e['kind']!='review' for e in groups.values()),
            'review_sessions':sum(e['kind']=='review' for e in groups.values()),
            'recorded_session_minutes':round(sum(e.get('minutes') or 0 for e in new_events),2)}
    L.write(root/'WORKFLOW-STATUS.json',data)
    lines = [f'Workflow delivery report — {data["generated"]}',f'Validated base: {base}',
             'Distinct tracked issues integrated: '+', '.join(f'{k}={v}' for k,v in sorted(counts.items())),
             f'Recorded total tokens: {totals["reported_total_tokens"]:,}; output tokens recorded: {totals["recorded_output_tokens"]:,}; logs covered: {totals["token_logs"]}',
             f'Integrated improvements per million recorded total tokens: {totals["integrated_improvements_per_million_reported_tokens"]}',
             data['limits'], 'Since rollout: '+json.dumps(data.get('since_rollout',{})), '', '| Issue | Delivery | Work sessions by kind | Session min | Review min | Total tokens recorded |',
             '|---|---|---|---:|---:|---:|']
    for identity, row in sorted(rows.items(),key=lambda x:-x[1]['session_minutes'])[:100]:
        lines.append(f'| {identity} | {row["delivery"]} | {json.dumps(row["sessions"])} | {row["session_minutes"]} | {row["review_minutes"]} | {row["reported_total_tokens"]:,} |')
    (root/'WORKFLOW-STATUS.md').write_text('\n'.join(lines)+'\n')
    return data


if __name__ == '__main__':
    data=refresh()
    print(json.dumps({'delivered':data['delivered'],'totals':data['totals'],'ancestry_verified':data['ancestry_verified']},indent=2))
