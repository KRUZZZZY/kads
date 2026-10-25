"""Scheduling and diagnostic checkpoints for the fix loop; no hidden model changes."""
import functools
import contextlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import issue_ledger as L


def members(item):
    out = {L.issue_id(item): item}
    for companion in item.get('batch') or []:
        member = {**companion, 'worktree':item.get('worktree')}
        if item.get('integration'):
            member['integration'] = item['integration']
        out[L.issue_id(member)] = member
    return [out[k] for k in sorted(out)]


def selector(fn, claude=False):
    @functools.wraps(fn)
    def pick(*args, **kwargs):
        if (L.ROOT/'WORKFLOW-MAINTENANCE').exists():
            return None, None
        index = 1 if claude else 0
        argv = list(args)
        original = argv[index] if len(argv) > index else kwargs.get('claimed', ())
        excluded = set(original)
        # Integration requests are selected separately from implementation queues.
        import integration_jobs
        lane = argv[0] if claude else None
        allow = claude or kwargs.get('bug_reworks_ok', True) is not False
        if allow and (not claude or not fn.__globals__['lane_holding'](lane['name'])):
            job, path = integration_jobs.next_job(excluded, lane)
            if job:
                return job, path
        for _ in range(5000):
            call_kw = dict(kwargs)
            if len(argv) > index:
                argv[index] = excluded
                call_kw.pop('claimed', None)
            else:
                call_kw['claimed'] = excluded
            job, path = fn(*argv, **call_kw)
            if job is None:
                return None, None
            item = job['item']
            if item.get('batch') and not item.get('integration'):
                item['batch'] = [b for b in item['batch'] if L.runnable(b,original)]
            if L.runnable(item, original):
                item.setdefault('issue_id', L.issue_id(item))
                if isinstance(original,set):
                    original.update(str(b['cluster']) for b in item.get('batch') or [])
                return job, path
            excluded.add(str(item['cluster']))
        raise RuntimeError('workflow selector could not make progress')
    return pick


def parse_plan(text):
    for body in reversed(re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.S)):
        try:
            value = json.loads(body)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    return {}


def diagnose(job, driver):
    item = job['item']
    identity = L.issue_id(item)
    data = L.get(item)
    folder = L.ROOT / 'diagnostics' / identity / str(time.time_ns())
    folder.mkdir(parents=True)
    wt = next((Path(e.get('run_dir', '')) for e in reversed(data['events']) if e.get('run_dir')), None)
    records = []
    for event in data['events'][-8:]:
        run = Path(event.get('run_dir', '.'))
        record = L.read(run / 'record.json')
        tree = record.get('worktree')
        if tree and Path(tree).is_dir():
            wt = Path(tree)
        review = L.read(L.ROOT / 'qa/done' / (run.name + '.json'))
        records.append({'kind': event['kind'], 'model': event.get('model'),
                        'files': record.get('files'), 'verdict': record.get('qwen_verdict'),
                        'failure': record.get('error'), 'review': review.get('qa'),
                        'record': str(run / 'record.json')})
    if wt is None or not (wt / '.git').exists():
        wt = Path(item.get('worktree', driver['PROG']))
    prompt = f'''Diagnose why this issue needs repeated attempts. Read-only: do not edit, commit, build, or run tests.
Issue {identity}: {item.get('title')}. Read only relevant functions, the existing tests and recorded evidence.
Companion issues in this shared task: {json.dumps(item.get('batch') or [])[:6000]}
Find the common cause of ALL evidenced blockers together. Check whether the necessary test target/dependency exists.
The next implementer needs one complete test matrix (ordinary, failure/exhaustion, realtime and portability paths),
not successive local patches. Unrelated defects become separate tasks. Preserve already-correct work.
Previous attempts: {json.dumps(records, default=str)[:18000]}
Latest feedback: {str(job.get('notes') or '')[:7000]}
Previous diagnostic plans: {json.dumps(data.get('checkpoints', []), default=str)[:5000]}
End with fenced JSON:
{{"decision":"retry|blocked|split", "root_cause":"file:line evidence and the actual cause",
"changed_plan":"concrete changed approach, not just try again", "why_different":"what changes compared with earlier attempts",
"tests":["specific target/scenario, expected failure before and success after"], "blockers":["capabilities unavailable"]}}
Use retry only for an actionable changed plan with an available test capability. A missing build dependency is blocked.
'''
    (folder / 'brief.md').write_text(prompt)
    out, log = folder / 'plan.md', folder / 'session.log'
    key = L.start(item, 'diagnostic', folder, driver['light_model']())
    cmd = driver['light_cmd'](['codex', 'exec', '-m', 'gpt-6-luna', '-c', 'model_reasoning_effort="max"',
                             '-c', 'approval_policy="never"', '-s', 'read-only', '-C', str(wt),
                             '--ephemeral', '-o', str(out), '-'])
    started = time.monotonic()
    try:
        with log.open('w') as stream:
            result = subprocess.run(cmd, input=prompt, text=True, stdout=stream, stderr=subprocess.STDOUT,
                                    env=driver['session_env'](), timeout=1200)
        rc = result.returncode
    except (OSError, subprocess.TimeoutExpired):
        rc = 124
    quota = rc != 0 and driver['note_luna_limit'](log)
    L.finish(item, key, {'minutes': round((time.monotonic()-started)/60, 2), 'rc': rc}, cancelled=quota)
    if quota:
        return
    plan = parse_plan(out.read_text() if out.exists() else '')
    if rc == 0 and L.approve(item, plan):
        for companion in members(item):
            if L.issue_id(companion) != identity:
                L.approve(companion,plan)
        job['notes'] = (str(job.get('notes') or '') + '\n\nDIAGNOSTIC CHECKPOINT — agreed scope and test matrix:\n' +
                        json.dumps(plan, indent=2))
        # Keep the original job, counter, lane and resume pointer; a new alias earns no new budget.
        target = Path(job.pop('_integration_path', '')) if job.get('_integration_path') else L.ROOT/'rework'/f"{item['cluster']}.json"
        L.write(target, job)
        driver['log']('fix', f'{identity}: diagnostic approved a changed plan; next attempt stays on this issue')
    else:
        L.block(item, 'blocked-diagnostic', json.dumps(plan) if plan else f'Diagnostic failed or gave no actionable plan (rc {rc}); {folder}', wt)
        driver['log']('fix', f'{identity}: held by diagnostic; {folder}')


def run(job, rework_file, engine, fix_one, driver):
    if (L.ROOT/'WORKFLOW-MAINTENANCE').exists():
        return
    item = job['item']
    item.setdefault('issue_id', L.issue_id(item))
    with contextlib.ExitStack() as leases:
        all_members = members(item)
        if not all(leases.enter_context(L.claim(member)) for member in all_members):
            return
        gates = [L.gate(member) for member in all_members]
        if any(g in ('blocked', 'exhausted') for g in gates):
            driver['log']('fix', f"{L.issue_id(item)}: a member is held; existing work preserved")
            return
        if 'diagnostic' in gates:
            diagnose(job, driver)
            return
        plan = L.get(item).get('diagnostic_plan')
        if plan and 'DIAGNOSTIC CHECKPOINT' not in str(job.get('notes') or ''):
            job['notes'] = str(job.get('notes') or '') + '\n\nDIAGNOSTIC CHECKPOINT — retained across model switches:\n' + json.dumps(plan,indent=2)
        try:
            fix_one(job, rework_file, engine)
        except Exception as exc:
            folder = driver['D']['runs'] / f"{driver['slug'](item['cluster'])}.r{job['round']}"
            # A usage-limit pause is not a preparation failure: fix_one has already archived the run folder and
            # requeued the job; blocking here would hold the issue until its build inputs changed.
            quota = tuple(driver[n] for n in ('LunaLimit', 'FreeModelLimit') if isinstance(driver.get(n), type))
            if not isinstance(exc, quota) and not (folder/'workflow-event.json').exists():
                wt = integration_jobs_tree(item, driver)
                L.block(item, 'blocked-build', f'Preparation failed before a model session: {exc}', wt)
            raise
        finally:
            folder = driver['D']['runs'] / f"{driver['slug'](item['cluster'])}.r{job['round']}"
            cancelled = False
            if not folder.exists():
                archived = [p for name in ('_usage-limit','_free-limit')
                            for p in (driver['D']['runs']/name).glob(folder.name+'-*')]
                if archived:
                    folder = max(archived, key=lambda p:p.stat().st_mtime)
                    cancelled = True
            event = L.read(folder / 'workflow-event.json')
            if event.get('key'):
                record = L.read(folder / 'record.json')
                if record.get('minutes') is not None:
                    record['minutes'] /= max(1,len(event.get('companions',[]))+1)
                L.finish(item, event['key'], record, cancelled=cancelled)
                for companion in event.get('companions',[]):
                    L.finish(companion['item'],companion['key'],record,cancelled=cancelled)


def integration_jobs_tree(item, driver):
    if item.get('integration'):
        import integration_jobs
        return integration_jobs.tree(item, driver['PROG'])[0]
    return item.get('worktree', driver.get('PROG','.'))


def session_started(job, folder, model):
    item = job['item']
    kind = 'integration' if item.get('integration') else 'mechanical' if (
        job.get('precheck_bounced') or job.get('negctl_bounced')) else 'implementation'
    key = L.start(item, kind, folder, model)
    companions=[]
    for companion in members(item):
        if L.issue_id(companion) != L.issue_id(item):
            companions.append({'item':companion,'key':L.start(companion,kind,folder,model,session_group=key)})
    L.write(Path(folder)/'workflow-event.json', {'key': key, 'issue_id': L.issue_id(item), 'kind': kind,'companions':companions})


def build_failed(job, wt, reason, driver):
    item = job['item']
    L.block(item, 'blocked-build', reason, wt)
    # An integration job goes back to its own request file; a rework/ copy would be a second, duplicate job.
    job = dict(job)
    origin = job.pop('_integration_path', None)
    path = Path(origin) if origin else L.ROOT/'rework'/f"{driver['slug'](item['cluster'])}.json"
    L.write(path, job)
    L.write(driver['D']['state']/f"{driver['slug'](item['cluster'])}.json",
            {'cluster': item['cluster'], 'issue_id': L.issue_id(item), 'outcome': 'blocked-build',
             'reason': reason, 'worktree': str(wt), 'lane': job.get('lane')})
    driver['log']('fix', f"{L.issue_id(item)}: build inputs blocked; no more attempts until inputs change ({reason})")


def capture_luna(folder, model):
    path = Path(folder)/'session.log'
    try:
        with path.open(errors='replace') as stream:
            header = stream.read(16384)
    except OSError:
        return
    match = re.search(r'session id:\s*([0-9a-f-]{36})', header, re.I)
    if match:
        L.write(Path(folder)/'luna-session.json', {'session': match.group(1), 'model': model, 'engine': 'luna'})
