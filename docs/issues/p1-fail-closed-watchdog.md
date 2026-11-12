# Issue draft: P1: Fail-closed GPU watchdog

STATUS: PROPOSED_ONLY.

TASK-002 NOTE (2026-11-12, offline-verified only): the watchdog start
is now fail-closed — `run_variant` waits up to 15 s for the watchdog's
readiness proof (alive, record read, deadline parses, bound to the
run's production task id, Colab CLI resolvable) and raises
`WATCHDOG_NOT_RUNNING` otherwise; the outer finally then stops and
verifies the just-created GPU session. The pre-allocation preflight
raises `CRITICAL_ORPHAN_GPU_SESSION` with per-session evidence
(run/task/deadline/unbilled estimate) and refuses a parallel GPU; it
never stops non-project sessions. (`h3_controller.py`
`_wait_watchdog_ready`, `h3_session_watchdog.py` `watch`/`reconcile_on_startup`,
branch `fix/watchdog-fail-closed`, commit `84b66c3`; tests
`tests/test_watchdog_fail_closed.py`, 11 tests.) No live GPU run has
exercised this path yet — a real run id is still required before
CONFIRMED_FIXED.

## Context

h3_controller.py — watchdog Popen in run_variant (used to record
`watchdog_start_failed` and continue without a watcher);
h3_session_watchdog.py — watch / reconcile_on_startup. Historical
context: VM restarts on 2026-11-12 killed the local controller and its
watchdog while a worker was still running, leaving an orphan A100
session that had to be stopped by hand.

## Reproduction

1. Start a watchdog subprocess that exits immediately (or writes a
   readiness file for the wrong task id) and assert run_variant raises
   WATCHDOG_NOT_RUNNING and the session is stopped — never proceeds to
   inference.
2. Leave a managed session active in the session ledger and assert a
   new run refuses with CRITICAL_ORPHAN_GPU_SESSION instead of
   allocating a second GPU.
3. Confirm the watchdog never stops a session that is not in its own
   managed-session file.
