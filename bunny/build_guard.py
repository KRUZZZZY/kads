#!/usr/bin/env python3
"""Two root builds plus four shared extra make tokens: at most six make jobs.

Slot/lease descriptors remain in the child too: terminating a wrapper cannot
release admission while its compiler subtree still runs. Nested invocations
inherit admission and their parent's jobserver.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from jobserver import HERE, FIFO, PID, STATE, LEASE, config


def server_status():
    try:
        pid = int(PID.read_text())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False, False
    try:
        state = json.loads(STATE.read_text())
        ready = state.get('pid') == pid and state.get('ready') is True and FIFO.is_fifo()
    except (OSError, ValueError, AttributeError):
        ready = False
    return True, ready


def uses_make(cmd):
    if Path(cmd[0]).name in {'make', 'gmake'}:
        return True
    if Path(cmd[0]).name == 'cmake' and '--build' in cmd:
        i = cmd.index('--build')
        return i + 1 < len(cmd) and (Path(cmd[i + 1]) / 'Makefile').exists()
    return False


def main(argv=None):
    cmd = list(sys.argv[1:] if argv is None else argv)
    if cmd and cmd[0] == '--':
        cmd.pop(0)
    if not cmd:
        raise SystemExit('build_guard.py -- COMMAND [ARGS...]')
    if os.environ.get('ZENE_BUILD_GUARDED') == '1':
        os.execvp(cmd[0], cmd)
    cfg = config()
    locks = HERE / 'locks'
    locks.mkdir(exist_ok=True)
    slot = None
    while slot is None:
        for i in range(cfg['max_builds']):
            candidate = open(locks / f'root-build-{i}.lock', 'a')
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                slot = candidate
                break
            except BlockingIOError:
                candidate.close()
        if slot is None:
            time.sleep(0.25)
    with slot, open(LEASE, 'a') as lease:
        while True:
            # Other generators have no GNU make tokens. Run them exclusively,
            # so a three-job Ninja fallback cannot overlap a five-job make.
            mode = fcntl.LOCK_SH if uses_make(cmd) else fcntl.LOCK_EX
            fcntl.flock(lease, mode)
            running, ready = server_status()
            if ready or not running:
                break
            fcntl.flock(lease, fcntl.LOCK_UN)
            time.sleep(0.25)
        env = dict(os.environ)
        env.pop('CMAKE_BUILD_PARALLEL_LEVEL', None)
        env.pop('MFLAGS', None)
        env['ZENE_BUILD_GUARDED'] = '1'
        if ready and uses_make(cmd):
            env['MAKEFLAGS'] = f'-j --jobserver-auth=fifo:{FIFO}'
        else:
            jobs = max(1, cfg['max_jobs'] // cfg['max_builds'])
            env['MAKEFLAGS'] = f'-j{jobs}'
            env['CMAKE_BUILD_PARALLEL_LEVEL'] = str(jobs)
        priority = os.getpriority(os.PRIO_PROCESS, 0)
        os.setpriority(os.PRIO_PROCESS, 0, max(priority, cfg['nice']))
        child = subprocess.Popen(cmd, env=env, pass_fds=(slot.fileno(), lease.fileno()))
        def forward(signum, _frame):
            child.send_signal(signum)
        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        rc = child.wait()
        return rc if rc >= 0 else 128 - rc


if __name__ == '__main__':
    raise SystemExit(main())
