#!/usr/bin/env python3
"""Trimming proxy between mcode and Strata: shrinks Qwen's starting prompt.

mcode sends 21 tools and its full harness text on every request, and `exec` has no setting to drop
any of it. This proxy removes what a single bug fix never uses, then forwards the request unchanged
otherwise (streaming included):
  - the 12 tools in DROP_TOOLS (subagent tasks, website deploy, goals, web search/fetch, skills, todos)
  - the <available_skills> catalog and the "## Media Output" section of the system text
Measured on a captured request: ~13.5K tokens -> ~5.7K.

  python3 trim_proxy.py [--port 8092] [--upstream http://127.0.0.1:8081]
One line per request goes to logs/trim.log. Stop it with SIGTERM.
"""
import argparse
import datetime
import http.client
import http.server
import json
import re
import sys
import urllib.parse
from pathlib import Path
import adaptive

DROP_TOOLS = {"task", "task_append", "task_output", "task_query", "task_stop", "website_deploy",
              "update_goal", "get_goal", "web_search", "web_fetch", "skill", "todowrite"}
SKILLS = re.compile(r"\n?<available_skills>.*?</available_skills>\n?", re.S)
MEDIA = re.compile(r"^## Media Output\n.*?(?=^#{1,2} |\Z)", re.S | re.M)
LOG = Path(__file__).resolve().parent / "logs" / "trim.log"


def tool_name(tool):
    return tool.get("name") or (tool.get("function") or {}).get("name")


def trim_text(text):
    return MEDIA.sub("", SKILLS.sub("\n", text))


def trim_message(msg):
    """Trim a system/developer message, whether its content is a string or a list of parts."""
    if msg.get("role") not in ("system", "developer"):
        return msg
    c = msg.get("content")
    if isinstance(c, str):
        return {**msg, "content": trim_text(c)}
    if isinstance(c, list):
        return {**msg, "content": [{**p, "text": trim_text(p["text"])} if isinstance(p, dict) and "text" in p else p
                                   for p in c]}
    return msg


def trim(body):
    """Return (new body, stats). Handles chat/completions ('messages') and Responses ('input')."""
    stats = {"tools_before": len(body.get("tools") or [])}
    if body.get("tools"):
        body["tools"] = [t for t in body["tools"] if tool_name(t) not in DROP_TOOLS]
        if not body["tools"]:
            body.pop("tools")
            body.pop("tool_choice", None)
    stats["tools_after"] = len(body.get("tools") or [])
    for key in ("messages", "input"):
        if isinstance(body.get(key), list):
            body[key] = [trim_message(m) if isinstance(m, dict) else m for m in body[key]]
    if isinstance(body.get("instructions"), str):
        body["instructions"] = trim_text(body["instructions"])
    return body, stats


class Handler(http.server.BaseHTTPRequestHandler):
    upstream = None  # urllib.parse result, set in main()

    def _forward(self, method, body=None):
        up = self.upstream
        conn = http.client.HTTPConnection(up.hostname, up.port, timeout=900)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection")}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn.request(method, self.path, body=body, headers=headers)
        resp = conn.getresponse()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("Connection", "close")  # close-delimited body: streams SSE as it arrives
        self.end_headers()
        while True:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
        conn.close()

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            body, stats = trim(adaptive.request_budget(json.loads(raw)))
            out = json.dumps(body).encode()
            with open(LOG, "a") as f:
                f.write(f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {self.path} tools {stats['tools_before']}->"
                        f"{stats['tools_after']} bytes {len(raw)}->{len(out)} budget={body.get('reasoning_budget_tokens')}\n")
        except (ValueError, TypeError):
            out = raw  # not JSON: pass through untouched
        self._forward("POST", out)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--upstream", default="http://127.0.0.1:8081")
    a = ap.parse_args()
    LOG.parent.mkdir(exist_ok=True)
    Handler.upstream = urllib.parse.urlparse(a.upstream)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"trim proxy on 127.0.0.1:{a.port} -> {a.upstream}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
