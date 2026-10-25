#!/usr/bin/env python3
"""One-shot graceful handover for the owner's OpenCode free-worker request."""
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parent
OLD_PID = 3058317
EXPECTED = 'Graceful restart to add one OpenCode free-model worker. Owner request 2026-10-12.\n'


def say(message):
    print(time.strftime('%Y-%m-%d %H:%M:%S ') + message, flush=True)


def authorized():
    return (not (ROOT / 'STOP').exists() and not (ROOT / 'DISABLED').exists()
            and (ROOT / 'STOP-FIX').exists()
            and (ROOT / 'STOP-FIX').read_text() == EXPECTED
            and json.loads((ROOT / 'OPENCODE-FREE.json').read_text()).get('enabled'))


say('Waiting for the old fixer to finish its current jobs.')
while Path(f'/proc/{OLD_PID}').exists():
    if not authorized():
        say('Owner control changed; cancelling handover.')
        raise SystemExit(0)
    command = Path(f'/proc/{OLD_PID}/cmdline').read_bytes()
    if b'qwenfix.py\x00fix' not in command:
        say('Old PID changed identity; cancelling handover.')
        raise SystemExit(0)
    time.sleep(15)
if not authorized():
    say('Owner control changed; cancelling handover.')
    raise SystemExit(0)
say('Starting the upgraded fixer.')
subprocess.run(['bash', str(ROOT / 'start.sh'), 'fix'], check=True)
say('Handover complete; QA remains independently active.')
