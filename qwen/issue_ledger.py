"""One issue identity and usage budget across aliases, models and integrations."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parent
LIVE_ROOT = ROOT
HELD = ('blocked', 'blocked-build', 'blocked-diagnostic', 'budget-exhausted')
DEFAULT = {'enabled': True, 'started_at': '9999', 'implementation_attempts_per_checkpoint': 2,
           'max_new_model_sessions': 8, 'max_diagnostics': 2, 'max_integration_sessions': 2}


def policy():
    try:
        d = json.loads((ROOT / 'WORKFLOW.json').read_text())
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d,dict) else {}
    cfg = {**DEFAULT, **d}
    for key, default in DEFAULT.items():
        if isinstance(default,int) and not isinstance(default,bool):
            value=cfg[key]
            cfg[key]=value if isinstance(value,int) and not isinstance(value,bool) and 0 < value <= 64 else default
    return cfg


def issue_id(item):
    value = item.get('issue_id') or item.get('cluster') or item.get('queue_id') if isinstance(item, dict) else item
    return re.sub(r'[^A-Za-z0-9_.-]', '-', re.sub(r'(?:-port(?:\d+)?)+$', '', str(value)))


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def historical(identity):
    events = []
    for p in (ROOT / 'runs').glob(identity + '*.r*/record.json'):
        r = read(p)
        if issue_id(r) != identity:
            continue
        kind = 'implementation'
        if r.get('minutes') is None:
            kind = 'infrastructure'
        elif '-port' in str(r.get('cluster')):
            kind = 'integration'
        events.append({'key': p.parent.name, 'kind': kind, 'historical': True,
                       'alias': str(r.get('cluster')), 'at': r.get('started', ''),
                       'model': r.get('model'), 'minutes': r.get('minutes'),
                       'head': r.get('head_after'), 'outcome': r.get('outcome'),
                       'run_dir': str(p.parent)})
    return sorted(events, key=lambda e: e['at'])


@contextlib.contextmanager
def edit(item):
    identity = issue_id(item)
    folder = ROOT / 'issues'
    folder.mkdir(exist_ok=True)
    path = folder / (identity + '.json')
    with open(folder / (identity + '.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(path)
        if not data:
            data = {'id': identity, 'status': 'ready', 'events': historical(identity), 'checkpoints': []}
        if isinstance(item, dict):
            data['aliases'] = sorted(set(data.get('aliases', [])) | {str(item.get('cluster', identity))})
            data['title'] = item.get('title') or data.get('title', '')
        yield data
        data['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        write(path, data)


def get(item):
    existing = read(ROOT / 'issues' / (issue_id(item) + '.json'))
    if existing:
        return existing
    with edit(item) as data:
        return json.loads(json.dumps(data))


@contextlib.contextmanager
def claim(item):
    folder = ROOT / 'issues'
    folder.mkdir(exist_ok=True)
    with open(folder / (issue_id(item) + '.active'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def busy(item):
    with claim(item) as owned:
        return not owned


def git(wt, *args):
    r = subprocess.run(['git', '-C', str(wt), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


def build_stamp(item, wt=None):
    """Only actual build inputs: a new train doesn't unstick an unchanged old checkout."""
    wt = Path(wt or item.get('worktree', '.'))
    pieces = [git(wt, 'rev-parse', 'HEAD')]
    if not pieces[0]:
        pieces.append((ROOT / 'FIX-BASE').read_text() if (ROOT / 'FIX-BASE').exists() else '')
    for p in [ROOT / 'FIX-CMAKE-ARGS', wt / 'build/CMakeCache.txt']:
        try:
            pieces.append(p.read_text())
        except OSError:
            pieces.append('missing:' + str(p))
    for p in [Path('/usr/bin/cmake'), Path('/usr/bin/c++'), wt / 'third_party/wasmtime/lib/libwasmtime.so',
              wt / 'build/clap', wt / 'build/vst3sdk', Path.home() / '.local/zene-deps/root/usr/lib']:
        try:
            st = p.stat()
            pieces.append(f'{p}:{st.st_mtime_ns}:{st.st_size}')
        except OSError:
            pieces.append('missing:' + str(p))
    return hashlib.sha256('\n'.join(pieces).encode()).hexdigest()


def held(identity, status, reason):
    """An issue just became held: logged in issues/held.log. The autopilot recovers what needs no judgement and the
    daily digest lists the rest, so nothing here messages the owner (owner 2026-10-24: mostly away from the PC)."""
    with open(ROOT / 'issues' / 'held.log', 'a') as stream:
        stream.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'id': identity, 'status': status,
                                 'reason': str(reason)[:300]}) + '\n')


def block(item, status, reason, wt=None):
    with edit(item) as data:
        was = data.get('status')
        data.update(status=status, reason=reason, blocked_stamp=build_stamp(item, wt),
                    blocked_tree=str(wt or item.get('worktree', '.')))
        data['events'].append({'key': uuid.uuid4().hex, 'kind': 'infrastructure' if status == 'blocked-build' else 'blocked',
                               'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'reason': reason})
    if was != status:
        held(issue_id(item), status, reason)


def exhaust(item, reason):
    with edit(item) as live:
        was = live.get('status')
        live.update(status='budget-exhausted', reason=reason)
    if was != 'budget-exhausted':
        held(issue_id(item), 'budget-exhausted', reason)


def gate(item):
    """run, diagnostic, blocked, or exhausted; no counter resets on tier/alias changes."""
    cfg = policy()
    if not cfg['enabled']:
        return 'run'
    data = get(item)
    if data.get('status', '').startswith('blocked'):
        if data.get('blocked_stamp') == build_stamp(item, data.get('blocked_tree')):
            return 'blocked'
        with edit(item) as live:
            live.update(status='ready', reason='Build/code inputs changed; re-evaluate once.')
            live.pop('blocked_stamp', None)
        data['status'] = 'ready'
    events = [e for e in data['events'] if not e.get('cancelled') and e['kind'] in
              ('implementation', 'mechanical', 'integration', 'diagnostic')]
    new = [e for e in events if not e.get('historical')]
    if len(new) >= cfg['max_new_model_sessions']:
        exhaust(item, 'Shared issue budget exhausted; aliases and model switches do not reset it.')
        return 'exhausted'
    if item.get('integration'):
        if sum(e['kind'] == 'integration' for e in new) >= cfg['max_integration_sessions']:
            exhaust(item, 'Two focused integration sessions exhausted; no new port identity will be created.')
            return 'exhausted'
        return 'run'  # accepted work needs focused integration, not another implementation diagnosis
    checkpoint = data.get('checkpoints', [])
    last_keys = set(checkpoint[-1].get('covered', [])) if checkpoint else set()
    # Mechanical repairs (submit-check / negative-control bounces of the same submission) spend the shared budget
    # above but are not the 'one correction' a checkpoint allows: only implementation sessions count here.
    since = [e for e in events if e['kind'] == 'implementation' and e['key'] not in last_keys]
    # Imported prior attempts trigger a diagnosis, but do not consume the new rollout budget.
    if len(since) >= cfg['implementation_attempts_per_checkpoint']:
        if sum(e['kind'] == 'diagnostic' for e in new) >= cfg['max_diagnostics']:
            exhaust(item, 'Diagnostic budget exhausted; repeated plans cannot reset it.')
            return 'exhausted'
        return 'diagnostic'
    return 'run'


def runnable(item, claimed=()):
    if issue_id(item) in {issue_id(c) for c in claimed} or busy(item):
        return False
    return gate(item) not in ('blocked', 'exhausted')


def start(item, kind, run_dir, model, session_group=None):
    key = str(run_dir)
    with edit(item) as data:
        if any(e['key'] == key for e in data['events']):
            key += ':' + uuid.uuid4().hex
        data['events'].append({'key': key, 'kind': kind, 'alias': str(item.get('cluster')),
                               'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'model': model,
                               'run_dir': str(run_dir), 'state': 'started', 'session_group':session_group or key})
        if kind != 'review':
            data['status'] = kind
    return key


def finish(item, key, record=None, cancelled=False):
    with edit(item) as data:
        event = next(e for e in data['events'] if e['key'] == key)
        event.update(state='finished', cancelled=cancelled)
        record = record or {}
        event.update({k: record.get(k) for k in ('minutes', 'head_after', 'rc', 'qwen_verdict')})
        if data.get('status') in ('implementation', 'mechanical', 'integration', 'diagnostic'):
            data['status'] = 'ready'


def approve(item, result):
    """A checkpoint must identify a changed hypothesis and a concrete test plan."""
    required = ('root_cause', 'changed_plan', 'why_different', 'tests')
    if result.get('decision') != 'retry' or not all(result.get(k) for k in required):
        return False
    if any(not isinstance(result[k],str) or len(result[k].strip()) < minimum
           for k,minimum in (('root_cause',8),('changed_plan',20),('why_different',12))):
        return False
    if not isinstance(result['tests'],list) or any(not isinstance(t,str) or len(t.strip()) < 8 for t in result['tests']):
        return False
    plan = json.dumps({k: result[k] for k in required}, sort_keys=True)
    with edit(item) as data:
        if any(c.get('plan') == plan for c in data.get('checkpoints', [])):
            return False
        data.setdefault('checkpoints', []).append({'plan': plan, 'result': result,
                                                   'covered': [e['key'] for e in data['events']]})
        data.update(status='ready', diagnostic_plan=result)
    return True
