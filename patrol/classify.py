#!/usr/bin/env python3
"""Resumable Clef classification and semantic repeat checks on a frozen patrol.

Unlike triage.py, preserves the old report, pins code to an explicit revision,
retains low-confidence/insufficient-context cases, and journals every decision.
No classification is a verified bug or grounds to discard a finding.
Requires the existing local Clef /v1/systemone endpoint and scikit-learn.
"""
import argparse
import collections
import hashlib
import json
import pathlib
import re
import subprocess
import time
import urllib.request
import urllib.error

from sklearn.feature_extraction.text import TfidfVectorizer

CONTRACTS = """Zene verified intended contracts: saves append a provenance journal;
undo coalesces commands and reports undone_commands; mastering.run/render ACK then
complete asynchronously; boolean parameters reject fractional values invalid_args;
first save to a new path is irreversible; empty-active tempo maps persist; effect
IDs renumber after unload/undo; malformed wire lines reply with id -1. CMake requires
C++20 in both src/ and plugins/ at all five sweep revisions and the current tip;
C++17 compatibility was never required. Mixer channel
count is an unbounded vector. ALSA and JACK capture exist; PortAudio/SDL capture do
not. Offline stem separation is intentional. A missing local code excerpt cannot
prove a false positive or an already-fixed bug. Do not assume a race is reachable
without checking callers/thread ownership. Do not assume a raw pointer is unsafe
without checking its owner's lifetime. This is triage, not runtime verification."""
CLASSES = {
    "likely_open_bug": "A plausible defect still visible in the supplied current code",
    "likely_false_positive": "The alleged ORIGINAL defect is contradicted by code or an explicit intended contract",
    "already_fixed": "The original defect was plausible but current code demonstrably repairs it",
    "needs_context": "Insufficient caller, lifetime, code, specification or reproduction evidence",
}
REAL = ["Almost certainly not a bug", "Unlikely to be a bug", "Plausible", "Likely a real bug", "Almost certainly a real bug"]
QUESTIONS = {
    "real": {"type": "score", "criteria": REAL, "instructions":
        "How likely is the ORIGINAL finding a real bug, given the original code and intended contracts? "
        "An intended behaviour or unsupported C++17 build assumption is not a bug. Later repairs do not make an originally real bug false."},
    "still_present": {"type": "noul", "instructions":
        "Does the current code still contain the defect this finding describes?"},
    "false_positive": {"type": "noul", "instructions":
        "Is the original finding a false positive, contradicted by the original code or an explicit intended contract? "
        "Insufficient context alone is not evidence that the report is false."},
    "already_fixed": {"type": "noul", "instructions":
        "Was the original defect real but demonstrably repaired in the current code? "
        "Require evidence of a repair; do not equate an original false positive with already fixed."},
}


def disposition(answers):
    real = answers.get("real", {}).get("score", 2)
    present = answers.get("still_present", {}).get("noul", 0.5)
    false = answers.get("false_positive", {}).get("noul", 0.5)
    fixed = answers.get("already_fixed", {}).get("noul", 0.5)
    if false >= 0.75 and real <= 1.5:
        return "likely_false_positive"
    if fixed >= 0.75 and present <= 0.25 and real >= 2:
        return "already_fixed"
    if present >= 0.75 and real >= 3 and false <= 0.25:
        return "likely_open_bug"
    return "needs_context"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, errors="replace")


def post(host, state, questions):
    req = urllib.request.Request(host + "/v1/systemone", data=json.dumps({
        "model": "clef-flash", "state": state, "questions": questions,
    }).encode(), headers={"Content-Type": "application/json"})
    # Retry connection/transient failures; never turn an error into a verdict.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                result = json.load(response)
            if set(result.get("answers", {})) != set(questions):
                raise ValueError("Clef response has missing or unexpected answer keys")
            return result["answers"]
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"Clef HTTP {error.code}: {detail}; request bytes {len(req.data)}") from error
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def append(path, rows):
    with path.open("a") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
        out.flush()


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def source_context(source, findings, limit=16000):
    if not source:
        return "SOURCE NOT RESOLVED: return needs_context, not a false-positive verdict."
    lines = source.splitlines()
    if len(source) < limit:
        return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))
    selected = set()
    for f in findings:
        # Sweep line numbers are historical; also locate exact quoted identifiers.
        centers = [max(0, int(f.get("line") or 1) - 1)]
        anchors = re.findall(r"`([^`\n]{3,100})`", f["text"])
        for anchor in anchors[:6]:
            needle = anchor.strip().removesuffix("()")
            hit = next((i for i, line in enumerate(lines) if needle in line), None)
            if hit is not None:
                centers.append(hit)
        for center in centers[:4]:
            selected.update(range(max(0, center - 18), min(len(lines), center + 24)))
    chunks = []
    previous = -2
    for i in sorted(selected):
        if i != previous + 1:
            chunks.append("... omitted lines ...")
        chunks.append(f"{i + 1}: {lines[i]}")
        previous = i
    text = "\n".join(chunks)
    return text[:limit] + ("\n... context truncated; use needs_context if needed ..." if len(text) > limit else "")


def report(out, findings, decisions, pairs, meta):
    parents = list(range(len(findings)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    confirmed = uncertain = 0
    for pair in pairs:
        value = pair["answer"].get("noul")
        if isinstance(value, (int, float)) and value >= 0.85:
            parents[root(pair["b"])] = root(pair["a"])
            confirmed += 1
        elif isinstance(value, (int, float)) and value >= 0.5:
            uncertain += 1
    groups = collections.defaultdict(list)
    for i in range(len(findings)):
        groups[root(i)].append(i)
    counts = collections.Counter()
    for d in decisions:
        counts[d["disposition"]] += 1
    text = ["# Luna classification by local Clef", "",
            f"Code revision: `{meta['revision']}`. Findings snapshot SHA256: `{meta['findings_sha256']}`.", "",
            f"Classified {len(decisions)}/{len(findings)} findings. Same-defect comparisons: {len(pairs)}; "
            f"{confirmed} positive at probability >= 0.85; {uncertain} uncertain at 0.5–0.85.", "",
            "Model suggestions only. Uncertain cases are retained. Already-fixed and false-positive claims "
            "are separate; neither is discarded automatically. Dispositions require agreement of the realness "
            "score and factual probabilities (see classify.py). Repeat matching is a conservative candidate search, not a proof "
            "that the remaining clusters are unique.", "",
            "| Suggested disposition | Findings |", "| --- | ---: |"]
    text += [f"| {label} | {counts[label]} |" for label in CLASSES]
    text += ["", f"Candidate clusters: {len(groups)}; repeat findings beyond representatives: "
             f"{len(findings) - len(groups)}.", "", "## Repeat clusters", ""]
    for members in sorted(groups.values(), key=lambda m: (-len(m), m[0])):
        if len(members) < 2:
            continue
        f = findings[members[0]]
        text.append(f"- **{f['title']}** (`{f['file']}`): IDs {', '.join(map(str, members))}")
    (out / "REPORT.md").write_text("\n".join(text) + "\n")
    with (out / "clusters.jsonl").open("w") as f:
        for members in groups.values():
            f.write(json.dumps({"representative": members[0], "members": members}) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--revision", required=True)
    ap.add_argument("--findings", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--reviews", help="Directory of prior fix reports; supplies matched review evidence, not automatic verdicts")
    args = ap.parse_args()
    repo, out = pathlib.Path(args.repo), pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    blob = pathlib.Path(args.findings).read_bytes()
    findings = [json.loads(line) for line in blob.splitlines()]
    revision = git(repo, "rev-parse", args.revision).strip()
    with urllib.request.urlopen(args.host + "/api/tags", timeout=20) as response:
        models = json.load(response)["models"]
    model = next(m for m in models if m["name"].split(":")[0] == "clef-flash")
    references = []
    review_hash = hashlib.sha256()
    if args.reviews:
        for path in sorted(pathlib.Path(args.reviews).rglob("*.md")):
            blob_review = path.read_bytes()
            review_hash.update(str(path).encode() + b"\0" + blob_review)
            text = blob_review.decode(errors="replace")
            # Reports separate BUG entries with blank lines; keep the heading,
            # verdict and evidence together, including multi-paragraph entries.
            for chunk in re.split(r"\n(?=[^\n]*\bBUG\s+\d+[^\n]*:)", text):
                if "verdict:" in chunk and "evidence:" in chunk:
                    references.append({"report": str(path), "text": chunk[:4500]})
    meta = {"revision": revision, "findings_sha256": hashlib.sha256(blob).hexdigest(),
            "findings_count": len(findings), "model": model["name"], "model_digest": model["digest"],
            "source_context_version": 4, "review_sha256": review_hash.hexdigest(),
            "contracts_sha256": hashlib.sha256(CONTRACTS.encode()).hexdigest(), "repeat_threshold": 0.85}
    if (out / "metadata.json").exists() and json.loads((out / "metadata.json").read_text()) != meta:
        raise SystemExit("Refusing to mix revisions, snapshots or model versions; choose a new output directory")
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    (out / "findings-snapshot.jsonl").write_bytes(blob)
    matched = collections.defaultdict(list)
    if references:
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
        review_vectors = vectorizer.fit_transform(r["text"] for r in references)
        finding_vectors = vectorizer.transform(f["title"] + " " + f["text"] for f in findings)
        matches = (finding_vectors @ review_vectors.T).toarray()
        for i, scores in enumerate(matches):
            for j in sorted(range(len(scores)), key=lambda j: -scores[j])[:2]:
                if scores[j] >= 0.20:
                    matched[i].append(references[j])
    paths = git(repo, "ls-tree", "-r", "--name-only", revision).splitlines()
    basenames = collections.defaultdict(list)
    for path in paths:
        basenames[pathlib.Path(path).name].append(path)
    by_file = collections.defaultdict(list)
    for i, finding in enumerate(findings):
        name = finding.get("file", "")
        candidates = basenames[pathlib.Path(name).name]
        path = name if name in paths else candidates[0] if len(candidates) == 1 else ""
        by_file[path].append(i)
    decisions = read_rows(out / "decisions.jsonl")
    judged = {d["id"] for d in decisions}
    pairs = read_rows(out / "repeat-decisions.jsonl")
    compared = {(p["a"], p["b"]) for p in pairs}
    parents = list(range(len(findings)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for pair in pairs:
        if pair["answer"].get("noul", 0) >= 0.85:
            parents[root(pair["b"])] = root(pair["a"])
    for path, ids in by_file.items():
        source = git(repo, "show", f"{revision}:{path}") if path else ""
        original_sources = {}
        for i in ids:
            rev = findings[i].get("rev", "")
            if rev and rev not in original_sources:
                try:
                    original_sources[rev] = git(repo, "show", f"{rev}:{path}") if path else ""
                except subprocess.CalledProcessError:
                    original_sources[rev] = ""
        # TF-IDF proposes pairs; Clef makes each same-defect decision. Compare up
        # to three earlier neighbors, plus title-key repeats regardless of score.
        candidates = set()
        if path and len(ids) > 1:
            vectors = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True).fit_transform([
                findings[i]["title"] + " " + findings[i]["text"] for i in ids])
            similarities = (vectors @ vectors.T).toarray()
            for local, i in enumerate(ids):
                previous = sorted(range(local), key=lambda j: -similarities[local, j])[:3]
                for j in previous:
                    if similarities[local, j] >= 0.28 or findings[i]["key"] == findings[ids[j]]["key"]:
                        candidates.add((ids[j], i))
        todo = [i for i in ids if i not in judged]
        for i in todo:
            finding = findings[i]
            original = original_sources.get(finding.get("rev", ""), "")
            state = {"contracts": CONTRACTS, "current_revision": revision, "file": path,
                     "current_code": source_context(source, [finding], 8000),
                     "original_code": "IDENTICAL to current code" if original == source else source_context(original, [finding], 6000),
                     "prior_review_evidence": matched[i], "finding": finding["text"][:3000]}
            answers = post(args.host, state, QUESTIONS)
            label = disposition(answers) if path and original else "needs_context"
            rows = [{"id": i, "path": path, "answers": answers, "disposition": label,
                     "context_sha256": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()}]
            append(out / "decisions.jsonl", rows)
            decisions.extend(rows)
            judged.add(i)
            if len(decisions) % 50 == 0:
                report(out, findings, decisions, pairs, meta)
                print(f"classified {len(decisions)}/{len(findings)}; repeat comparisons {len(pairs)}", flush=True)
        todo_pairs = sorted(candidates - compared)
        for a, b in todo_pairs:
            if root(a) == root(b):
                continue  # Already connected by stronger same-defect decisions; as in the original triage tool.
            state = {"finding_a": findings[a]["text"][:2500], "finding_b": findings[b]["text"][:2500]}
            questions = {"same": {"type": "noul", "instructions":
                "Do the two findings describe the SAME root cause in the SAME place? "
                "Shared file, theme, symptom or proposed fix alone is insufficient. Distinct operations "
                "or independent defects count as different. Judge original claims regardless of whether fixed."}}
            answers = post(args.host, state, questions)
            rows = [{"a": a, "b": b, "answer": answers["same"]}]
            append(out / "repeat-decisions.jsonl", rows)
            pairs.extend(rows)
            compared.add((a, b))
            if answers["same"].get("noul", 0) >= 0.85:
                parents[root(b)] = root(a)
        report(out, findings, decisions, pairs, meta)
        print(f"classified {len(decisions)}/{len(findings)}; repeat comparisons {len(pairs)}; {path or 'unresolved'}", flush=True)
    (out / "COMPLETE").write_text("All frozen findings classified; candidate repeat comparisons complete.\n")


if __name__ == "__main__":
    main()
