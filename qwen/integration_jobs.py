"""Pinned, idempotent integration requests for accepted patches; never new -port jobs."""
from pathlib import Path
import datetime
import json
import subprocess
import time

import issue_ledger as L


def queue(candidate, why, item, target, regression=False):
    identity = L.issue_id(item)
    path = L.ROOT / 'integrations' / (identity + '.json')
    old = L.read(path)
    accepted = candidate['commit']
    # An accepted patch can cover a batch; retain those accepted companions for review.
    for done in (L.ROOT/'qa/done').glob(str(candidate['cluster'])+'.r*.json'):
        rec = L.read(done)
        if rec.get('branch') == candidate['branch'] and str(rec.get('head_after','')).startswith(candidate['sha']):
            if (rec.get('item') or {}).get('batch'):
                item = {**item, 'batch': rec['item']['batch']}
            break
    request = {'source_commit': accepted, 'source_branch': candidate['branch'],
               'target_base': target, 'reason': why, 'regression': regression,
               'queued_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    L.get(item)
    if old and L.busy(item):
        return path  # preserve a pinned request already being worked on
    full_target = L.git(item['worktree'], 'rev-parse', target)
    if not full_target:
        raise RuntimeError(f'Cannot resolve pinned integration base {target}')
    request['target_commit'] = full_target
    starts = []
    for record in (L.ROOT/'runs').glob(str(candidate['cluster'])+'.r*/record.json'):
        rec = L.read(record)
        if rec.get('head_before') and rec.get('branch') == candidate['branch']:
            starts.append((int(rec.get('round') or 0), rec['head_before']))
    request['source_base'] = candidate.get('source_base') or (min(starts)[1] if starts else
                                    L.git(item['worktree'],'merge-base',accepted,full_target))
    previous = old.get('item',{}).get('integration',{})
    if old and all(previous.get(k)==request.get(k) for k in ('source_commit','source_base','target_commit','regression')):
        return path
    highest = max([int(p.parent.name.rsplit('.r',1)[1]) for p in (L.ROOT/'runs').glob(str(item['cluster'])+'.r*/record.json')] or [0])
    notes = f'''INTEGRATION JOB for an already accepted patch, issue {identity}.
Source: {candidate['branch']} @ {accepted}; target pinned at {target} @ {full_target}.
Reason: {why}. This is integration, not a request to implement the original bug again.
The driver applies the accepted patch with a three-way merge in your own integration worktree.
Resolve only its conflicts and any demonstrated regression; retain the accepted design and regression tests.
Inspect the accepted diff and previous evidence. List all affected failure paths and supported-platform hazards together.
Build the affected targets, run their focused tests and gates, and verify any semantic difference from the accepted patch.
Do not change shared worktrees or rewrite the accepted branch. If a conflict requires a new design, report BLOCKED.
Commit the integrated patch and the existing issue report with an honest verdict and exact evidence.
'''
    job = {'item': {**item,
                    'issue_id': identity, 'integration': request},
           'round': highest + 1, 'notes': notes, 'integration_status': 'queued',
           'created_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    L.write(path, job)
    with L.edit(item) as data:
        data.update(status='ready')
        data.pop('blocked_stamp',None)
        data['integration'] = {**request, 'status': 'queued', 'request': str(path)}
        data['events'].append({'key': f'integration:{accepted}:{full_target}', 'kind': 'integration-request',
                               'at': job['created_at'], 'reason': why})
    return path


def defer(candidate, why, item, regression=False):
    """Wait for promotion so conflicts are resolved against the validated cumulative train."""
    path = L.ROOT/'integration-pending'/(L.issue_id(item)+'.json')
    old = L.read(path)
    request = {'candidate': candidate, 'why': why, 'item': item, 'regression': regression}
    if old != request:
        L.write(path, request)
    return path


def activate(target, errors=None):
    """Queue every pending request against the promoted train. One bad request stays pending (and is reported
    through errors) instead of stopping the rest: this runs after FIX-BASE has already moved."""
    activated = []
    for path in (L.ROOT/'integration-pending').glob('*.json'):
        try:
            request = L.read(path)
            queue(request['candidate'], request['why'], request['item'], target, request['regression'])
        except Exception as exc:
            if errors is not None:
                errors.append(f'{path.stem}: {exc!r}')
            continue
        path.unlink()
        activated.append(path.stem)
    return activated


def next_job(claimed, lane=None):
    folder = L.ROOT / 'integrations'
    for path in sorted(folder.glob('*.json'), key=lambda p:p.stat().st_mtime):
        job = L.read(path)
        item = job.get('item') or {}
        feature = str(item.get('cluster','')).startswith('Q050-')
        if feature != bool(lane):
            continue
        if lane:
            tiers = [t for t in lane.get('tiers',['sonnet','opus']) if t in ('sonnet','opus')]
            if not tiers:
                continue
            item['feature_tier'] = tiers[0]
            job['lane'] = lane['name']
        if not L.runnable(item, claimed):
            continue
        if any(not L.runnable({**b,'worktree':item.get('worktree'),'integration':item['integration']},claimed)
               for b in item.get('batch') or []):
            continue
        job['_integration_path'] = str(path)
        return job, path
    return None, None


def tree(item, program):
    request = item['integration']
    suffix = request['target_commit'][:10]
    identity = L.issue_id(item)
    return Path(program)/f'zene-integrate-{identity}-{suffix}', f'integration/{identity}-{suffix}'


def apply(item, wt, run_dir):
    """Apply only in an owned tree pinned to the target. Never mutate an accepted branch."""
    request = item['integration']
    target, source = request['target_commit'], request['source_commit']
    head = L.git(wt, 'rev-parse', 'HEAD')
    if head != target:
        return  # an integration correction continues the commits already on this owned branch
    if L.git(wt, 'status', '--porcelain'):
        raise RuntimeError('Integration tree must be clean before applying the accepted patch')
    ancestor = request.get('source_base') or L.git(wt, 'merge-base', source, target)
    if not ancestor or ancestor == source:
        raise RuntimeError('Accepted patch base unavailable; refusing to treat a previously reverted patch as an empty integration')
    patch = subprocess.run(['git','-C',str(wt),'diff','--binary',f'{ancestor}..{source}'],
                           capture_output=True, check=True).stdout
    patch_path = Path(run_dir)/'accepted.patch'
    patch_path.write_bytes(patch)
    allowed = L.git(wt,'diff','--name-only',f'{ancestor}..{source}').splitlines()
    L.write(Path(run_dir)/'integration-evidence.json', {'source_commit':source,'target_commit':target,
             'source_base':ancestor,'allowed_files':allowed,'patch':str(patch_path)})
    if not patch:
        return
    result = subprocess.run(['git','-C',str(wt),'apply','--3way','--index',str(patch_path)],capture_output=True,text=True)
    unmerged = L.git(wt,'diff','--name-only','--diff-filter=U').splitlines()
    (Path(run_dir)/'integration-apply.log').write_text(result.stdout+result.stderr)
    if result.returncode and not unmerged:
        raise RuntimeError('Accepted patch could not be applied: '+result.stderr[-500:])
    with L.edit(item) as data:
        data['integration'].update(status='resolving-conflicts' if unmerged else 'applied', tree=str(wt), conflicts=unmerged)


def feature_lane_open():
    """Feature integrations run only on an enabled Claude lane with a sonnet/opus tier, and only while FEATURES-ON."""
    if not (L.ROOT/'FEATURES-ON').exists():
        return False
    lanes = L.read(L.ROOT/'LANES.json').get('lanes', [])
    return any(l.get('enabled', True) and set(l.get('tiers') or ['sonnet', 'opus']) & {'sonnet', 'opus'} for l in lanes)


def queued_at(item):
    """When this issue's integration request was queued (the ledger's latest integration-request event)."""
    at = (item.get('integration') or {}).get('queued_at')
    if not at:
        at = max((e.get('at', '') for e in L.get(item).get('events', []) if e.get('kind') == 'integration-request'),
                 default='')
    try:
        return datetime.datetime.fromisoformat(at)
    except ValueError:
        return None


def unsettled(max_age_h=None, overdue=None):
    """Keep a validated base fixed while its accepted-patch integration cohort runs. A member that cannot run
    (feature with no open Claude lane) or has waited longer than max_age_h no longer holds the train; the
    overdue ones are appended to overdue when it is given."""
    identities=set()
    lane_open = feature_lane_open()
    now = datetime.datetime.now()
    for folder in ('integrations','rework','qa/pending'):
        for path in (L.ROOT/folder).glob('*.json'):
            rec=L.read(path);item=rec.get('item') or {}
            if not item.get('integration') or L.gate(item) in ('blocked','exhausted'):
                continue
            identity = L.issue_id(item)
            if identity.startswith('Q050-') and not lane_open and folder != 'qa/pending':
                if overdue is not None:
                    overdue.append(f'{identity} (no open Claude feature lane)')
                continue
            since = queued_at(item)
            if max_age_h is not None and since and (now - since).total_seconds() > max_age_h * 3600:
                if overdue is not None:
                    overdue.append(f'{identity} (waiting {(now - since).total_seconds() / 3600:.0f} h)')
                continue
            identities.add(identity)
    if overdue is not None:
        overdue[:] = sorted(set(overdue))
    return sorted(identities)


def outside_patch(item, wt, changed):
    """Files an integration changed that the accepted patch did not touch (registries excepted; the caller drops reports):
    each must be justified as a conflict resolution or a demonstrated regression fix, so the reviewer is told."""
    request = item.get('integration') or {}
    base = request.get('source_base') or L.git(wt, 'merge-base', request.get('source_commit', ''), request.get('target_commit', ''))
    if not base or not request.get('source_commit'):
        return []
    allowed = set(L.git(wt, 'diff', '--name-only', f"{base}..{request['source_commit']}").splitlines())
    registries = {'tests/all-sources.txt', 'tests/fork-sources.txt', 'tests/upstream-modifications.txt'}
    return sorted(f for f in changed if f not in allowed and f not in registries)
