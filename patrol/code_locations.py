#!/usr/bin/env python3
"""Step 1 of the root-cause search, done properly (owner 2026-10-19): map each bug to the function it is in.

Every finding records the file, line and revision it was read from. The file is read at THAT revision (git show), so
the line is exact, and a small C/C++ scanner finds the innermost function definition enclosing it: comments, strings
and character literals are blanked, braces are matched, and a `{` whose preceding header looks like `name(...)` (not
if/for/while/switch/catch, not class/struct/namespace/enum/lambda) opens a function. Class and namespace bodies are
tracked so inline methods get `Class::method`. When the finding has no usable line, the `where:` text is parsed for
`file:line`, and as a last resort a backticked identifier that names a function of that file is used.

For each located function the scanner also records its callees (identifiers followed by `(`), so two bugs whose
functions call the same rarely-called helper can be linked.

API: locate(item) -> {"function": "Class::method", "file": path, "start": n, "end": m, "how": "line|where|name",
                      "callees": [...] } or None
"""
import functools
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1] / "zene-cachyos"
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "sizeof", "alignof", "decltype", "static_assert",
            "do", "else", "new", "delete", "throw", "co_await", "co_return", "noexcept", "requires", "assert",
            "Q_ASSERT", "defined", "emit", "Q_EMIT", "typeid"}
SCOPE_KW = re.compile(r"\b(class|struct|union|namespace|enum)\b\s*([A-Za-z_][\w:]*)?[^;(){}]*$")
FUNC_HDR = re.compile(r"([A-Za-z_~][\w:~<>,\s*&]*?)\s*\(([^()]|\([^()]*\))*\)\s*"
                      r"(const|noexcept(\([^)]*\))?|override|final|volatile|&|&&|->\s*[\w:<>,\s*&]+|\s)*"
                      r"(:\s*[^{;]*)?$", re.S)
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
LAMBDA_HDR = re.compile(r"\[[^\[\]]*\]\s*(\(([^()]|\([^()]*\))*\))?\s*(mutable|constexpr|noexcept|->\s*[\w:<>,\s*&]+|\s)*$")
COMMAND_ID = re.compile(r'"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)"')
LINE_IN_WHERE = re.compile(r"([\w./-]+\.(?:cpp|cc|c|h|hpp|hh))[^\d\n]{0,3}:?~?(\d+)")


@functools.lru_cache(maxsize=4096)
def source(rev, path):
    if not rev or not path:
        return ""
    r = subprocess.run(["git", "-C", str(TREE), "show", f"{rev}:{path}"], capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else ""


def blank(src):
    """Comments, string and char literals replaced by spaces (newlines kept, so line numbers stay)."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", src[i:j])); i = j
        elif c == 'R' and src.startswith('R"', i):
            m = re.match(r'R"([^(\s]{0,16})\(', src[i:])
            if m:
                end = src.find(")" + m.group(1) + '"', i)
                end = n if end < 0 else end + len(m.group(1)) + 2
                out.append(re.sub(r"[^\n]", " ", src[i:end])); i = end
            else:
                out.append(c); i += 1
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(c + " " * (j - i - 2) + (c if j - i >= 2 else "")); i = j
        else:
            out.append(c); i += 1
    return "".join(out)


@functools.lru_cache(maxsize=2048)
def functions(rev, path):
    """[(qualified name, start line, end line, body text)] for every function definition in the file, lambdas included
    (a lambda registered as a control command is named `cmd:<command id>` from the nearest command-id string before it)."""
    src = source(rev, path)
    if not src:
        return []
    text = blank(src)
    found, stack = [], []          # stack entries: (kind, name, start offset)
    last = 0                       # start of the current header (after the last ; { or })
    line_at = [0] * (len(text) + 1)
    ln = 1
    for k, ch in enumerate(text):
        line_at[k] = ln
        if ch == "\n":
            ln += 1
    for k, ch in enumerate(text):
        if ch == "{":
            header = text[last:k].strip()
            kind, name = "block", None
            outer = next((s[1] for s in reversed(stack) if s[0] == "func"), None)
            lam = LAMBDA_HDR.search(header)
            m = SCOPE_KW.search(header)
            if lam:
                ids = COMMAND_ID.findall(src[max(0, k - 600):k])
                kind = "func"
                name = f"cmd:{ids[-1]}" if ids else f"{outer or Path(path).stem}::lambda@{line_at[k]}"
            elif outer is None and m and "(" not in header.split(m.group(1))[-1]:
                kind = "ns" if m.group(1) == "namespace" else "scope"
                name = (m.group(2) or "").split("::")[-1] or None
            elif outer is None:
                fm = FUNC_HDR.search(header)
                if fm and "=" not in header.split("(")[0]:
                    head = fm.group(1).split()
                    fname = head[-1].lstrip("*&") if head else ""
                    if fname and fname.split("::")[-1] not in KEYWORDS and re.match(r"[A-Za-z_~]", fname):
                        scopes = [s[1] for s in stack if s[0] == "scope" and s[1]]
                        if "::" not in fname and scopes:
                            fname = f"{scopes[-1]}::{fname}"
                        kind, name = "func", fname
            stack.append((kind, name, k))
            last = k + 1
        elif ch == "}":
            if stack:
                kind, name, start = stack.pop()
                if kind == "func":
                    found.append((name, line_at[start], line_at[k], text[start:k]))
            last = k + 1
        elif ch == ";":
            last = k + 1
    return found


def enclosing(rev, path, line):
    best = None
    for name, a, b, body in functions(rev, path):
        if a <= line <= b and (best is None or b - a < best[2] - best[1]):
            best = (name, a, b, body)
    return best


def callees(body):
    return sorted({c for c in CALL.findall(body) if c not in KEYWORDS and len(c) > 3})


def locate(item):
    path = str(item.get("resolved_path") or item.get("file") or "")
    rev = item.get("revision") or item.get("original_revision") or "HEAD"
    hit, how = None, None
    try:
        line = int(str(item.get("line") or 0).lstrip("~") or 0)
    except ValueError:
        line = 0
    if line > 0:
        hit, how = enclosing(rev, path, line), "line"
    if hit is None:
        for f, l in LINE_IN_WHERE.findall(str(item.get("text", ""))[:600]):
            if path.endswith(f) or f.endswith(Path(path).name):
                hit, how = enclosing(rev, path, int(l)), "where"
                if hit:
                    break
    if hit is None:
        names = {n.split("::")[-1] for n in re.findall(r"`([A-Za-z_~][\w:~]*)\(?\)?`", str(item.get("text", "")))}
        for name, a, b, body in functions(rev, path):
            if name.split("::")[-1] in names:
                hit, how = (name, a, b, body), "name"
                break
    if hit is None:
        return None
    name, a, b, body = hit
    return {"function": name, "file": path, "start": a, "end": b, "how": how, "callees": callees(body)}


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, str(HERE))
    import root_causes as R
    items, _ = R.queue()
    got = [locate(i) for i in items]
    ok = [g for g in got if g]
    from collections import Counter
    print(f"{len(ok)}/{len(items)} bugs located; by method {dict(Counter(g['how'] for g in ok))}")
    for i, g in list(zip(items, got))[:8]:
        print(i["cluster"], "->", g and g["function"], g and g["how"])
