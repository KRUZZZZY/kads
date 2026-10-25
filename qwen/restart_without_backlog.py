#!/usr/bin/env python3
"""Owner-requested graceful reload and GPU release after the current Qwen fix."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import time
import qwenfix as driver

ROOT = Path(__file__).resolve().parent
EXPECTED = 'Graceful reload to remove the review backlog cap and honor the owner GPU pause, 2026-10-12.\n'
OLD = {'fix': 2045994, 'qa': 1910135}
QWEN_JOB = 3495951


def say(message):
    print(time.strftime('%Y-%m-%d %H:%M:%S ') + message, flush=True)


def running(pid, token):
    try:
        return token in Path(f'/proc/{pid}/cmdline').read_bytes()
    except FileNotFoundError:
        return False


def owns_reload(loop):
    try:
        return (not (ROOT / 'STOP').exists() and not (ROOT / 'DISABLED').exists()
                and (ROOT / ('STOP-' + loop.upper())).read_text() == EXPECTED)
    except FileNotFoundError:
        return False


def reload_loop(loop):
    say(f'{loop}: waiting for current jobs to finish.')
    while running(OLD[loop], b'qwenfix.py\x00' + loop.encode()):
        if not owns_reload(loop):
            say(f'{loop}: owner controls changed; cancelling reload.')
            return
        time.sleep(5)
    if not owns_reload(loop):
        say(f'{loop}: owner controls changed; cancelling reload.')
        return
    # Starting each loop separately preserves the other loop's stop flag.
    subprocess.run(['bash', str(ROOT / 'start.sh'), loop], check=True)
    say(f'{loop}: reload complete.')


def release_gpu():
    say('GPU: waiting for the current Qwen fix; no new local jobs allowed.')
    while running(QWEN_JOB, b'/qwen/shims/mcode'):
        if not (ROOT / 'QWEN-PAUSED').exists() or (ROOT / 'LOCAL-QWEN').exists():
            say('GPU: owner resumed local Qwen; cancelling release.')
            return
        time.sleep(5)
    if (ROOT / 'QWEN-PAUSED').exists() and not (ROOT / 'LOCAL-QWEN').exists():
        # The fixer records the result and QA proceeds on Luna; inference is over.
        if driver.strata_pids():
            driver.strata_stop()
        say('GPU: Qwen model stopped; GPU released.')
        (ROOT / 'QWEN-GPU-RELEASED').write_text(driver.now() + '\n')


if __name__ == '__main__':
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = [pool.submit(reload_loop, loop) for loop in ['fix', 'qa']]
        tasks.append(pool.submit(release_gpu))
        for task in tasks:
            task.result()
