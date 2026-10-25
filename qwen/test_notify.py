"""notify regressions: backend selection from NOTIFY.json, each backend's request, and failures never raising."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import notify as N


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.knob = Path(self.tmp.name) / "NOTIFY.json"
        patcher = mock.patch.multiple(N, KNOB=self.knob, ROOT=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def knob_says(self, cfg):
        self.knob.write_text(json.dumps(cfg))

    def test_no_knob_keeps_notify_me(self):
        with mock.patch.object(N.subprocess, "run") as run:
            self.assertTrue(N.send("hi"))
        self.assertEqual(run.call_args.args[0], ["notify-me", "hi"])

    def test_unreadable_knob_falls_back_to_default(self):
        self.knob.write_text("{not json")
        self.assertEqual(N.backends(), [N.DEFAULT])

    def test_custom_command(self):
        self.knob_says({"backend": "command", "command": ["my-pager", "--to", "ops"]})
        with mock.patch.object(N.subprocess, "run") as run:
            N.send("hi")
        self.assertEqual(run.call_args.args[0], ["my-pager", "--to", "ops", "hi"])

    def test_webhook_posts_json_with_the_configured_field(self):
        self.knob_says({"backend": "webhook", "url_env": "T_HOOK", "field": "content"})
        with mock.patch.dict(os.environ, {"T_HOOK": "https://example.invalid/h"}), \
                mock.patch.object(N, "_post") as post:
            self.assertTrue(N.send("hi"))
        url, body, ctype, _ = post.call_args.args
        self.assertEqual((url, json.loads(body), ctype), ("https://example.invalid/h", {"content": "hi"},
                                                          "application/json"))

    def test_telegram_reads_token_and_chat_from_env(self):
        self.knob_says({"backend": "telegram", "token_env": "T_TOK", "chat_id_env": "T_CHAT"})
        with mock.patch.dict(os.environ, {"T_TOK": "abc", "T_CHAT": "42"}), mock.patch.object(N, "_post") as post:
            N.send("hi")
        url, body, _, _ = post.call_args.args
        self.assertEqual(url, "https://api.telegram.org/botabc/sendMessage")
        self.assertEqual(json.loads(body), {"chat_id": "42", "text": "hi"})

    def test_missing_env_fails_quietly(self):
        self.knob_says({"backend": "ntfy", "topic_url_env": "T_UNSET_FOR_TEST"})
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(N, "_post") as post:
            os.environ.pop("T_UNSET_FOR_TEST", None)
            self.assertFalse(N.send("hi"))
        post.assert_not_called()

    def test_several_backends_and_one_failing(self):
        self.knob_says({"backends": [{"backend": "bogus"}, {"backend": "file", "path": "logs/n.log"}]})
        self.assertTrue(N.send("first"))
        self.assertTrue(N.send("second\n"))
        self.assertEqual((Path(self.tmp.name) / "logs/n.log").read_text(), "first\nsecond\n")

    def test_none_backend_drops(self):
        self.knob_says({"backend": "none"})
        with mock.patch.object(N.subprocess, "run") as run:
            self.assertTrue(N.send("hi"))
        run.assert_not_called()

    def test_command_failure_does_not_raise(self):
        self.knob_says({"backend": "command", "command": ["false"]})
        self.assertFalse(N.send("hi"))


if __name__ == "__main__":
    unittest.main()
