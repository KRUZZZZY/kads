#!/usr/bin/env python3
"""Finish the active fix batch and its reviews, then honor the owner's full pause."""
import datetime
import fcntl
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'PAUSE-AFTER-CURRENT.json'


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def say(message):
    print(now() + ' ' + message, flush=True)


def running(pid, loop):
    try:
        return b'qwenfix.py\x00' + loop.encode() in Path(f'/proc/{pid}/cmdline').read_bytes()
    except FileNotFoundError:
        return False


def save(state, phase):
    state.update(phase=phase, updated=now())
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2) + '\n')
    tmp.replace(STATE)


def main():
    state = json.loads(STATE.read_text())
    expected = state['stop_text']
    def authorized():
        try:
            return ((ROOT / 'STOP-FIX').read_text() == expected
                    and json.loads(STATE.read_text())['requested'] == state['requested']
                    and not (ROOT / 'STOP').exists())
        except FileNotFoundError:
            return False
    def wait_while(test):
        while test():
            if not authorized():
                say('Owner controls changed; cancelling deferred pause.')
                return False
            time.sleep(5)
        return authorized()

    say('Waiting for active fixes to finish; new assignments are stopped.')
    if not wait_while(lambda: running(state['fix_pid'], 'fix')):
        return
    save(state, 'reviewing-current-results')
    say('Fixer stopped; finishing all pending independent reviews.')
    if not wait_while(lambda: any((ROOT / 'qa/pending').glob('*.json'))):
        return
    # No fixer remains to enqueue more results. The QA loop finishes its active
    # futures before observing this stop flag and exiting.
    qa_stop = ROOT / 'STOP-QA'
    if qa_stop.exists() and qa_stop.read_text() != expected:
        say('QA stop control changed; leaving it untouched.')
        return
    qa_stop.write_text(expected)
    save(state, 'finishing-review-workers')
    if not wait_while(lambda: running(state['qa_pid'], 'qa')):
        return
    (ROOT / 'ALL-PAUSED').write_text(expected)
    (ROOT / 'STOP').write_text(expected)
    subprocess.run(['systemctl', '--user', 'disable', '--now',
                    'zene-followup-sweep-check.timer', 'codex-banked-reset.timer'], check=True)
    # Let a rate-limit check already in flight finish instead of interrupting
    # a reset redemption. Disabling the timer prevents further checks.
    while subprocess.run(['systemctl', '--user', 'is-active', '--quiet',
                          'codex-banked-reset.service']).returncode == 0:
        time.sleep(5)
    save(state, 'paused')
    say('All fixes and reviews finished. Bug automation and reset watcher paused.')


if __name__ == '__main__':
    with (ROOT / 'pause-after-current.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
