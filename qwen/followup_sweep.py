#!/usr/bin/env python3
"""Run one post-fix Luna sweep after every queued report is resolved and integrated."""
import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PROG = Path.home() / 'Documents/AI_KOS_PROJECT/projects/zene-studio-program'
OUT = PROG / 'research/reviews/luna-patrol'
CONFIG = ROOT / 'FOLLOWUP-SWEEP.json'
STATE = ROOT / 'followup-sweep-state.json'


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def write(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def queue_status(items, states, skipped, pending, reworks):
    keys = {str(i['cluster']).replace('/', '-') for i in items}
    unresolved = sorted(k for k in keys if states.get(k, {}).get('outcome') != 'accepted')
    legacy = [k for k in unresolved if k in skipped and k not in states]
    parked = [k for k, d in states.items() if d.get('outcome') != 'accepted'
              and d.get('outcome') != 'in-qa']
    return {'queued': len(keys), 'unresolved': len(unresolved),
            'unresolved_sample': unresolved[:12], 'legacy_unreviewed': legacy,
            'other_unresolved': len(unresolved) - len(legacy),
            'qa_pending': pending, 'reworks_queued': reworks,
            'other_unfinished_states': len(parked),
            'ready': bool(keys) and not unresolved and not pending and not reworks
                     and not parked and not any(d.get('outcome') == 'in-qa' for d in states.values())}


def driver():
    spec = importlib.util.spec_from_file_location('qwenfix', ROOT / 'qwenfix.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check():
    if (ROOT / 'PATROL-DISABLED').exists():
        return {'phase': 'disabled-by-owner'}
    cfg = json.loads(CONFIG.read_text())
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    if not cfg.get('armed') or old.get('phase') in ('running', 'complete', 'sweep-failed', 'triage-failed'):
        return old
    d = driver()
    items = d.load_queue()
    states = {p.stem: json.loads(p.read_text()) for p in (ROOT / 'state').glob('*.json')}
    status = queue_status(items, states, d.luna_easy_attempted(),
                          len(list((ROOT / 'qa/pending').glob('*.json'))),
                          len(list((ROOT / 'rework').glob('*.json'))))
    result = {'updated': now(), 'phase': 'waiting-for-fixes', 'queue': status}
    if d.stopped('FIX') or d.stopped('QA'):
        result['phase'] = 'held-by-stop-flag'
        write(STATE, result)
        return result
    # The old fixer skips ten historical Luna attempts, including interrupted ones.
    # At the end, put any still-unreviewed reports through the normal fix/QA path.
    if (status['legacy_unreviewed'] and not status['other_unresolved']
            and not status['qa_pending'] and not status['reworks_queued']
            and not status['other_unfinished_states']):
        indexed = {d.slug(i['cluster']): i for i in items}
        for key in status['legacy_unreviewed']:
            write(ROOT / 'rework' / f'{key}.json', {
                'item': indexed[key], 'round': 1,
                'notes': 'Historical Luna-easy attempt was never independently accepted. Recheck the current '
                         'code and supply normal regression evidence; if already fixed, prove that claim for QA.',
            })
        result['phase'] = 'rechecking-legacy-attempts'
        write(STATE, result)
        subprocess.run(['bash', str(ROOT / 'start.sh'), 'both'], check=True)
        return result
    if not status['ready'] or not (ROOT / 'FIX-DRAINED').exists():
        write(STATE, result)
        return result
    repo = Path(cfg['repo'])
    revision = git(repo, 'rev-parse', cfg['branch'])
    ancestors = set(git(repo, 'rev-list', revision).splitlines())
    missing = [k for k, r in states.items() if r.get('outcome') == 'accepted'
               and r.get('kind') == 'fix' and r.get('head_after') not in ancestors]
    result.update(revision=revision, unintegrated_fixes=len(missing), unintegrated_sample=missing[:12])
    if missing:
        result['phase'] = 'waiting-for-integration'
        write(STATE, result)
        return result
    token = hashlib.sha256(json.dumps([revision, sorted(states)]).encode()).hexdigest()
    if old.get('ready_token') != token:
        result.update(phase='confirming-stable-queue', ready_token=token, ready_since=now())
        write(STATE, result)
        return result
    since = datetime.datetime.fromisoformat(old['ready_since'])
    if (datetime.datetime.now(datetime.timezone.utc) - since).total_seconds() < 120:
        return old
    result.update(phase='starting', ready_token=token, ready_since=old['ready_since'],
                  requested_passes=1, workers=cfg.get('workers', 3))
    write(STATE, result)
    subprocess.run(['systemctl', '--user', 'start', 'zene-followup-sweep.service'], check=True)
    return result


def run_sweep():
    if (ROOT / 'PATROL-DISABLED').exists():
        raise RuntimeError('Bug sweeps permanently disabled by owner')
    state = json.loads(STATE.read_text())
    cfg = json.loads(CONFIG.read_text())
    if state.get('phase') != 'starting':
        raise RuntimeError('Follow-up sweep has no ready request')
    pidfile = OUT / 'patrol.pid'
    if pidfile.exists():
        pid = pidfile.read_text().strip()
        p = Path(f'/proc/{pid}/cmdline')
        if p.exists() and b'patrol.py' in p.read_bytes():
            raise RuntimeError('Another patrol is already running')
    stop = OUT / 'STOP'
    state['previous_stop'] = stop.read_text() if stop.exists() else None
    state.update(phase='running', started=now())
    write(STATE, state)
    # Only this explicitly authorized new pass clears the historical pass-12 stop.
    stop.unlink(missing_ok=True)
    cmd = [sys.executable, str(PROG / 'tools/luna-patrol/patrol.py'),
           '--repo', cfg['repo'], '--branch', state['revision'], '--out', str(OUT),
           '--workers', str(state['workers']), '--passes', '1']
    with (OUT / 'followup-sweep.log').open('a') as log:
        rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
    stop.write_text('Authorized one post-fix sweep finished; continuous sweeping remains stopped.\n')
    state.update(sweep_exit=rc, finished=now())
    if rc:
        state['phase'] = 'sweep-failed'
        write(STATE, state)
        return
    with (OUT / 'followup-triage.log').open('a') as log:
        rc = subprocess.run([sys.executable, str(PROG / 'tools/luna-patrol/triage_new.py'),
                             '--once', '--repo', cfg['repo']], stdout=log,
                            stderr=subprocess.STDOUT).returncode
    state.update(triage_exit=rc, phase='complete' if rc == 0 else 'triage-failed')
    write(STATE, state)
    if rc == 0:
        cfg['armed'] = False
        write(CONFIG, cfg)
        # A later owner stop overrides the earlier request to continue fixing.
        if not any((ROOT / flag).exists() for flag in ['STOP', 'STOP-FIX', 'STOP-QA']):
            subprocess.run(['bash', str(ROOT / 'start.sh'), 'both'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'run', 'status'])
    action = parser.parse_args().action
    if action == 'run':
        # Separate lock: the timer may read/check state while the sweep runs.
        with (ROOT / 'followup-sweep-run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run_sweep()
        return
    with (ROOT / 'followup-sweep-check.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = check() if action == 'check' else json.loads(STATE.read_text())
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
