#!/usr/bin/env python3
"""Operator notifications through a configurable messaging backend.

Every alert, digest and status message the loops send goes through send(text). The backend comes from the knob file
qwen/NOTIFY.json, read fresh on each call. With no knob file the pipeline behaves as it always has: it runs the
`notify-me` command (the reference deployment's Telegram bot).

  {"backend": "command", "command": ["notify-me"]}                       text appended as the last argument
  {"backend": "webhook", "url_env": "NOTIFY_WEBHOOK_URL", "field": "text"} JSON POST {field: text}; "text" suits Slack,
                                                                            use "content" for Discord
  {"backend": "ntfy", "topic_url_env": "NTFY_TOPIC_URL"}                   plain-text POST to an ntfy topic
  {"backend": "telegram", "token_env": "TELEGRAM_BOT_TOKEN", "chat_id_env": "TELEGRAM_CHAT_ID"}
  {"backend": "file", "path": "logs/notify.log"}                           append to a file (relative to qwen/)
  {"backend": "none"}                                                       drop messages

"backends" (a list of the objects above) sends to several at once. URLs and tokens are read from the named
environment variables, never from the knob file, so it can be committed. A failed send is logged to stderr and never
raises: a notification must not take a loop down.

  python3 notify.py "message"        send from a shell script
  python3 notify.py --check          print the resolved backends without sending
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOB = ROOT / "NOTIFY.json"
DEFAULT = {"backend": "command", "command": ["notify-me"]}


def backends():
    try:
        cfg = json.loads(KNOB.read_text())
    except FileNotFoundError:
        return [DEFAULT]
    except (OSError, ValueError) as e:
        print(f"notify: {KNOB.name} unreadable ({e}); using the default", file=sys.stderr)
        return [DEFAULT]
    return cfg.get("backends") or [cfg]


def _post(url, data, content_type, timeout):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": content_type}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def _env(b, key):
    name = b.get(key)
    value = os.environ.get(name, "") if name else ""
    if not value:
        raise RuntimeError(f"environment variable {name or '(' + key + ' not set)'} is empty")
    return value


def _send_one(b, text, timeout):
    kind = b.get("backend", "command")
    if kind == "none":
        return
    if kind == "command":
        subprocess.run(list(b.get("command") or DEFAULT["command"]) + [text], capture_output=True, timeout=timeout,
                       check=True)
    elif kind == "webhook":
        body = json.dumps({b.get("field", "text"): text}).encode()
        _post(_env(b, "url_env"), body, "application/json", timeout)
    elif kind == "ntfy":
        _post(_env(b, "topic_url_env"), text.encode(), "text/plain; charset=utf-8", timeout)
    elif kind == "telegram":
        url = f"https://api.telegram.org/bot{_env(b, 'token_env')}/sendMessage"
        body = json.dumps({"chat_id": _env(b, "chat_id_env"), "text": text}).encode()
        _post(url, body, "application/json", timeout)
    elif kind == "file":
        path = Path(b.get("path", "logs/notify.log"))
        path = path if path.is_absolute() else ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(text.rstrip("\n") + "\n")
    else:
        raise RuntimeError(f"unknown backend {kind!r}")


def send(text, timeout=60):
    """Send text to every configured backend. Returns True if at least one accepted it."""
    ok = False
    for b in backends():
        try:
            _send_one(b, text, timeout)
            ok = True
        except Exception as e:  # never let a notification failure stop a loop
            print(f"notify: {b.get('backend', 'command')} failed: {e}", file=sys.stderr)
    return ok


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        print(json.dumps(backends(), indent=1))
        sys.exit(0)
    if len(sys.argv) < 2:
        sys.exit("usage: notify.py MESSAGE | --check")
    sys.exit(0 if send(" ".join(sys.argv[1:])) else 1)
