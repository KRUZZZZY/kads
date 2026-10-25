# Build limits — 2026-10-24

The Luna fix, QA and scope services are currently frozen with systemd's cgroup
freezer. Existing agent sessions and build processes are preserved. Beam patrol
is stopped. The replacement jobserver is running.

To resume the preserved Luna work:

    systemctl --user thaw zene-fix.service zene-qa.service zene-scope.service

STOP-FIX and STOP-QA were queued so the old Python controllers finish their
current items and then exit. Their existing Restart=always configuration and
ExecStartPre hooks clear those flags and load the updated controller on restart.
Do not restart the frozen fix/QA services directly: that would discard sessions.

## Limits

- JOBSERVER.json: two admitted root builds, four shared GNU make tokens, six
  make recipe jobs maximum. Recursive makes inherit admission and tokens.
- Idle-only token refill: never infer ownership from compiler or nested make
  process counts. Tokens returned under memory pressure are retired. Lost tokens
  are recovered only while idle. Four GiB of available RAM is the reserve.
- Restart recovery drains tokens from the old pool until old builds finish.
  The current state may show ready=false/recovering=true while preserved old
  builds are frozen. This is expected. New guarded builds wait for recovery.
- Builds run with nice >= 10. Non-Make generators run exclusively with three
  jobs rather than independently consuming another shared pool.
- No-server fallback: two root builds, three jobs each.
- The guard removes inherited CMAKE_BUILD_PARALLEL_LEVEL and replaces MAKEFLAGS;
  cmake/make shims remove explicit parallelism before invoking the guard.
- systemd persistent service properties also protect the already-running old
  controllers: zene-fix CPUQuota=400%, zene-qa CPUQuota=200%, both CPUWeight=20.
  Together those two services get at most six logical CPUs even during migration.
  CPUWeight lowers their share when the desktop competes for CPU.

An inherited root-build lock stays open in subprocesses, so killing the Python
wrapper does not release admission before its remaining build subtree exits.

## Validation

    python3 ~/.local/share/zene-bunny-fix/bunny/test_build_limits.py

Seven tests cover actual concurrent recursive GNU make builds, no-server
fallback, CMake overrides, restart recovery, borrowed tokens and memory pressure.
The integration runs completed 48 recipes each with peak_jobs=6,
peak_root_builds=2 and nice=10, without jobserver warnings.
Python compilation and shell syntax checks passed as well.

Pre-change copies (including qwenfix.py) are in:

    backup-20261024-120024/

The persistent systemd CPUQuota/CPUWeight properties were newly added separately
from that backup. Inspect their generated overrides with:

    systemctl --user cat zene-fix.service zene-qa.service

The controller entry point, bunny/shims/cmake and bunny/shims/make all invoke
bunny/build_guard.py; bunny/jobserver.py owns the fixed FIFO token pool.
