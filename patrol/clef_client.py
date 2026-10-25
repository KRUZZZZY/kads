#!/usr/bin/env python3
"""Shared Clef client: one keep-alive connection per thread, retries, and an optional on-disk answer cache.

Clef (`clef-flash` in ollama, custom endpoint /v1/systemone) answers `noul` (a probability), `score` (an index into
`criteria`) and `choice` (probabilities per criterion). All of the Clef-using tools post the same body shape, so this
is the one place that builds it. Nothing here talks to the GPU lease; callers hold a `triage_new.GpuLease`.

Measured 2026-10-25 (RTX 5070 Ti, ollama 0.35.1, -np 1): ~185 calls/min sequential for a ~600-token pairwise prompt,
GPU-bound (100 % util) so client concurrency does not raise throughput (2 clients: 184/min, 4: 181/min); it only
raises latency. See research/reviews/bug-urgency/REPORT.md.
"""
import hashlib
import http.client
import json
import pathlib
import threading
import time
import urllib.parse

HOST = "http://127.0.0.1:11434/v1/systemone"
MODEL = "clef-flash"
_local = threading.local()


def _conn(url):
    u = urllib.parse.urlparse(url)
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "key", None) != (u.hostname, u.port):
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=300)
        _local.conn, _local.key = c, (u.hostname, u.port)
    return c, u.path


def post(state, questions, url=HOST, model=MODEL, retries=4, post_fn=None):
    """POST one Clef request and return the `answers` dict. `post_fn(body) -> answers` replaces the network (tests)."""
    body = {"model": model, "state": state, "questions": questions}
    if post_fn is not None:
        return post_fn(body)
    data = json.dumps(body).encode()
    for attempt in range(retries):
        try:
            c, path = _conn(url)
            c.request("POST", path, data, {"Content-Type": "application/json"})
            r = c.getresponse()
            raw = r.read()
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}: {raw[:200]!r}")
            return json.loads(raw)["answers"]
        except Exception:
            _local.conn = None
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))


class Cache:
    """Append-only JSONL cache keyed by a string; safe to resume. Never rewrites existing lines."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.d = {}
        self.lock = threading.Lock()
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    self.d[r["k"]] = r["v"]
                except ValueError:
                    pass

    def get(self, k):
        return self.d.get(k)

    def put(self, k, v):
        with self.lock:
            self.d[k] = v
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps({"k": k, "v": v}) + "\n")


def key(*parts):
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:24]
