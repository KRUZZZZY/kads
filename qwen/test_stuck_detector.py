"""stuck_detector regressions: fake log lines only, no sessions, no models."""
import tempfile
import unittest
from pathlib import Path

import stuck_detector as S


class StuckDetectorTests(unittest.TestCase):
    def test_progress_is_not_stuck(self):
        lines = [f"[{i:3d}%] Building CXX object src/core/File{i}.cpp.o" for i in range(100)]
        self.assertIsNone(S.detect(lines))

    def test_same_line_repeated_is_flagged(self):
        lines = ["thinking about the crossfader undo step"] + ["exec: cmake --build build --target zene -j2 (rc=2)"] * 15
        v = S.detect(lines)
        self.assertEqual(v["kind"], "repeat")

    def test_timestamps_and_hashes_do_not_hide_a_loop(self):
        lines = [f"2026-10-25T10:{i:02d}:07Z fetching deadbeef{i:02d}1234 failed to connect to host" for i in range(14)]
        self.assertEqual(S.detect(lines)["kind"], "repeat")

    def test_two_step_ping_pong(self):
        lines = ["exec: sed -n 1,80p src/core/Mixer.cpp", "output: the file is unchanged since the last read"] * 7
        v = S.detect(lines)
        self.assertEqual(v["kind"], "cycle")
        self.assertIn("2-line", v["detail"])

    def test_repeated_error_between_other_work(self):
        lines = []
        for i in range(10):
            lines += [f"editing file number {i} with a new approach", "error: No Q_OBJECT in the class passed to findChild"]
        self.assertEqual(S.detect(lines)["kind"], "error-loop")

    def test_short_lines_are_ignored(self):
        self.assertIsNone(S.detect(["}"] * 100 + ["ok"] * 100))

    def test_short_loop_below_threshold_is_fine(self):
        self.assertIsNone(S.detect(["ctest -R MixerTest passed with the new test"] * 5))

    def test_check_log_reads_only_the_tail(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "session.log"
            p.write_text("x" * 5000 + "\n" + "retrying the same failing command line now\n" * 30)
            self.assertEqual(S.check_log(p, {"tail_bytes": 2000})["kind"], "repeat")
            self.assertIsNone(S.check_log(Path(d) / "missing.log"))


class WatchAndHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.old = S.KNOB
        S.KNOB = self.dir / "STUCK-DETECTOR.json"

    def tearDown(self):
        S.KNOB = self.old
        self.tmp.cleanup()

    def test_off_without_knob(self):
        log = self.dir / "session.log"
        log.write_text("the same failing command line again\n" * 40)
        self.assertIsNone(S.Watch(log, clock=lambda: 1e9).poll())

    def test_watch_reports_once_and_respects_interval(self):
        S.KNOB.write_text('{"enabled": true, "action": "stop", "check_every_s": 60}')
        log = self.dir / "session.log"
        log.write_text("the same failing command line again\n" * 40)
        now = [1e9]
        w = S.Watch(log, clock=lambda: now[0])
        self.assertEqual(w.poll()["kind"], "repeat")
        self.assertEqual(w.action, "stop")
        now[0] += 120
        self.assertIsNone(w.poll())            # already reported for this session

    def test_run_session_stops_a_looping_session_only_when_action_is_stop(self):
        import sys
        import time
        from unittest import mock
        import qwenfix
        real_sleep = time.sleep
        run_dir = self.dir / "run"
        (run_dir / "logs").mkdir(parents=True)

        def go(action, linger):
            prog = ("import time\n[print('exec: cmake --build build --target zene (rc=2)', flush=True) "
                    f"for _ in range(40)]\ntime.sleep({linger})\n")
            S.KNOB.write_text('{"enabled": true, "action": "%s", "check_every_s": 0}' % action)
            lf = open(run_dir / "session.log", "w")
            with mock.patch.object(qwenfix.time, "sleep", lambda s: real_sleep(0.5)), \
                    mock.patch.object(qwenfix, "log", lambda *a: None):
                return qwenfix.run_session([sys.executable, "-c", prog], "", lf, None, run_dir, "t", "fake")
        self.assertEqual(go("stop", 60), 124)      # killed as soon as the loop is seen
        self.assertEqual(go("log", 2), 0)          # observe-only: the session is left to finish


if __name__ == "__main__":
    unittest.main()
