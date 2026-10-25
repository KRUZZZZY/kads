#!/usr/bin/env python3
"""Fixed GNU make token pool; refill only while every build is idle.

Recursive makes share their parent's implicit job. Compiler/process counts cannot
reconstruct token ownership. After restarting, drain old tokens until old builds
finish, then seed a known pool. Admission locks prevent racing new builds.
"""
import fcntl
import json
import math
import os
import signal
import struct
import termios
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIFO = HERE / 'make-jobserver.fifo'
CONFIG = HERE / 'JOBSERVER.json'
STATE = HERE / 'jobserver-state.json'
PID = HERE / 'jobserver.pid'
LEASE = HERE / 'build-pool.lock'
TICK = 1
DEFAULT = {'tokens': 4, 'max_builds': 2, 'max_jobs': 6,
           'min_free_gb': 4.0, 'reserve_gb_per_token': 1.0, 'nice': 10}


def config():
    try:
        value = json.loads(CONFIG.read_text())
        if not isinstance(value, dict):
            value = {}
    except (OSError, ValueError):
        value = {}
    cfg = dict(DEFAULT)
    for key, default in DEFAULT.items():
        n = value.get(key)
        if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n):
            cfg[key] = type(default)(n)
    cfg['max_jobs'] = max(1, min(6, cfg['max_jobs']))
    cfg['max_builds'] = max(1, min(2, cfg['max_jobs'], cfg['max_builds']))
    cfg['tokens'] = max(0, min(cfg['tokens'], cfg['max_jobs'] - cfg['max_builds']))
    cfg['nice'] = max(0, min(19, cfg['nice']))
    cfg['reserve_gb_per_token'] = max(0.1, cfg['reserve_gb_per_token'])
    return cfg


def mem_available_gb():
    with open('/proc/meminfo') as source:
        for line in source:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1048576
    return 0.0


def pool_makes(proc=Path('/proc')):
    """Detect old/unregistered builds; never use their count to create tokens."""
    needle = f'fifo:{FIFO}'.encode()
    makes = 0
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            if (p / 'comm').read_text().strip() in {'make', 'gmake'}:
                makes += needle in (p / 'environ').read_bytes()
        except OSError:
            continue
    return makes


def available(fd):
    buf = fcntl.ioctl(fd, termios.FIONREAD, struct.pack('i', 0))
    return struct.unpack('i', buf)[0]


def drain(fd, count):
    try:
        return len(os.read(fd, count)) if count > 0 else 0
    except BlockingIOError:
        return 0


class TokenPool:
    def __init__(self, fd):
        self.fd = fd
        self.ready = False
        self.budget = 0

    def update(self, target, idle):
        if idle:
            drain(self.fd, available(self.fd))
            if target:
                os.write(self.fd, b'+' * target)
            self.budget = target
            self.ready = True
        elif not self.ready:
            # Existing builds may hold arbitrary excess tokens after restart.
            drain(self.fd, available(self.fd))
        elif target < self.budget:
            self.budget -= drain(self.fd, min(available(self.fd), self.budget - target))


def main():
    with open(HERE / 'jobserver.lock', 'a') as singleton, open(LEASE, 'a') as lease:
        fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not FIFO.exists():
            os.mkfifo(FIFO, 0o600)
        fd = os.open(FIFO, os.O_RDWR | os.O_NONBLOCK)
        PID.write_text(str(os.getpid()))
        pool = TokenPool(fd)
        stop = []
        signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
        signal.signal(signal.SIGINT, lambda *_: stop.append(True))
        try:
            while not stop:
                cfg = config()
                free = mem_available_gb()
                target = max(0, cfg['tokens'] - math.ceil(
                    max(0.0, cfg['min_free_gb'] - free) / cfg['reserve_gb_per_token']))
                makes = pool_makes()
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    pool.update(target, makes == 0)
                except BlockingIOError:
                    pool.update(target, False)
                finally:
                    fcntl.flock(lease, fcntl.LOCK_UN)
                state = {'at': time.strftime('%H:%M:%S'), 'pid': os.getpid(),
                         'ready': pool.ready, 'recovering': not pool.ready,
                         'tokens_target': target, 'token_budget': pool.budget,
                         'in_pool': available(fd), 'make_processes': makes,
                         'max_builds': cfg['max_builds'], 'max_jobs': cfg['max_jobs'],
                         'mem_available_gb': round(free, 1)}
                tmp = STATE.with_suffix('.tmp')
                tmp.write_text(json.dumps(state) + '\n')
                tmp.replace(STATE)
                time.sleep(TICK)
        finally:
            PID.unlink(missing_ok=True)
            os.close(fd)


if __name__ == '__main__':
    main()
