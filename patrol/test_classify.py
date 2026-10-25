"""Exercise resumable classification against a fake local decision endpoint.

These tests validate transport and bookkeeping, not Clef's judgement accuracy.
"""
import http.server
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest


class ClassifierTest(unittest.TestCase):
    def test_individual_decisions_resume_and_frozen_inputs(self):
        calls = []

        class Endpoint(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, value):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_GET(self):
                self.reply(200, {"models": [{"name": "clef-flash:latest", "digest": "fixture"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                questions = body["questions"]
                calls.append(list(questions))
                answers = {}
                for key in questions:
                    if key == "same":
                        answers[key] = {"type": "noul", "noul": 0.96}
                    elif key == "real":
                        answers[key] = {"type": "score", "score": 0.5}
                    else:
                        answers[key] = {"type": "noul", "noul": 0.96 if key == "false_positive" else 0.04}
                self.reply(200, {"answers": answers})

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "fixture.cpp").write_text("bool accepts(int x) { return x == 0 || x == 1; }\n")
            subprocess.run(["git", "-C", str(repo), "add", "fixture.cpp"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                            "user.email=fixture@example.invalid", "commit", "-qm", "Fixture"], check=True)
            revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            findings = root / "findings.jsonl"
            row = {"title": "Fractional boolean values are rejected", "file": "fixture.cpp", "line": 1,
                   "text": "Fractional boolean values are rejected by the validation function.",
                   "key": "fixture|boolean", "rev": revision}
            findings.write_text(json.dumps(row) + "\n" + json.dumps(dict(row, duplicate=True)) + "\n"
                                + json.dumps(dict(row, duplicate=True)) + "\n")
            out = root / "out"
            with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                command = [sys.executable, str(pathlib.Path(__file__).with_name("classify.py")),
                           "--repo", str(repo), "--revision", revision, "--findings", str(findings),
                           "--out", str(out), "--host", f"http://127.0.0.1:{server.server_port}"]
                run = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                decisions = (out / "decisions.jsonl").read_bytes()
                pairs = (out / "repeat-decisions.jsonl").read_bytes()
                self.assertEqual(len(decisions.splitlines()), 3)
                self.assertEqual(len(pairs.splitlines()), 2)
                for keys in calls:
                    self.assertIn(set(keys), [{"real", "still_present", "false_positive", "already_fixed"}, {"same"}])
                self.assertTrue(all(json.loads(row)["disposition"] == "likely_false_positive"
                                    for row in decisions.splitlines()))
                self.assertIn("repeat findings beyond representatives: 2", (out / "REPORT.md").read_text())
                calls_before_resume = len(calls)
                resumed = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(len(calls), calls_before_resume)
                self.assertEqual((out / "decisions.jsonl").read_bytes(), decisions)
                self.assertEqual((out / "repeat-decisions.jsonl").read_bytes(), pairs)
                findings.write_text(findings.read_text() + json.dumps(row) + "\n")
                changed = subprocess.run(command, capture_output=True, text=True)
                self.assertNotEqual(changed.returncode, 0)
                self.assertIn("Refusing to mix", changed.stderr)
                self.assertEqual((out / "decisions.jsonl").read_bytes(), decisions)
                server.shutdown()


if __name__ == "__main__":
    unittest.main()
