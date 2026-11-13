"""TASK-005 §3 (Issue #25): the stop_session confirmation contract.

stop_session() returns a structured result:
{"session", "state", "stop_cli_ok", "attempts", "queries", "error"}

- Success is reported ONLY on explicit STOPPED (three-state query).
- UNKNOWN runs bounded re-confirmation, then escalates as UNKNOWN.
- stop_managed_session() refuses ids absent from the managed file, so a
  non-project session is never stopped.
- The managed record is cleared only on STOPPED.
- Watchdog independence is honestly marked INDEPENDENT_SHUTDOWN_NOT_GUARANTEED
  (docs/watchdog_independence.md); no remote kill -9 substitutes for a
  real GPU session stop.

No GPU, no live CLI: _run_colab is mocked.
"""
import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_session_watchdog as wd  # noqa: E402
from h3_session_watchdog import (  # noqa: E402
    SessionState,
    load_managed_sessions,
    record_session,
    stop_managed_session,
    stop_session,
    watch,
)

SID = "muse-h3-abc123"


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _base_dt():
    return datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.timezone.utc)


class StopResultTest(unittest.TestCase):
    def test_stop_cli_failed_but_session_gone_still_stopped(self):
        # The verdict comes from the re-query, not from the stop CLI's
        # exit code: the session is gone, so STOPPED (with stop_cli_ok
        # False recorded as evidence).
        with mock.patch.object(wd, "_run_colab",
                               return_value=_proc(returncode=1, stderr="stop failed")), \
                mock.patch.object(wd, "query_session_state",
                                  return_value=SessionState.STOPPED), \
                mock.patch("time.sleep"):
            result = stop_session(SID)
        self.assertEqual(result["state"], "STOPPED")
        self.assertFalse(result["stop_cli_ok"])
        self.assertEqual(result["attempts"], 1)

    def test_stop_cli_raises_but_session_gone_still_stopped(self):
        with mock.patch.object(wd, "_run_colab",
                               side_effect=subprocess.TimeoutExpired("colab", 65)), \
                mock.patch.object(wd, "query_session_state",
                                  return_value=SessionState.STOPPED), \
                mock.patch("time.sleep"):
            result = stop_session(SID)
        self.assertEqual(result["state"], "STOPPED")
        self.assertFalse(result["stop_cli_ok"])
        # The stop itself is verified gone, so there is no error to report;
        # the CLI failure is preserved as stop_cli_ok=False evidence.
        self.assertIsNone(result["error"])

    def test_stop_success_but_session_still_active_escalates(self):
        # "stop returned success but the session is still there" is the
        # most dangerous false-stop: the verdict must stay ACTIVE.
        with mock.patch.object(wd, "_run_colab",
                               return_value=_proc(returncode=0, stdout="ok")), \
                mock.patch.object(wd, "query_session_state",
                                  return_value=SessionState.ACTIVE), \
                mock.patch("time.sleep"):
            result = stop_session(SID)
        self.assertEqual(result["state"], "ACTIVE")
        self.assertTrue(result["stop_cli_ok"])
        self.assertEqual(result["attempts"], 3)
        self.assertIn("still ACTIVE", result["error"])

    def test_unknown_bounded_then_escalates(self):
        with mock.patch.object(wd, "_run_colab",
                               return_value=_proc(returncode=0, stdout="ok")) as run, \
                mock.patch.object(wd, "query_session_state",
                                  return_value=SessionState.UNKNOWN) as q, \
                mock.patch("time.sleep"):
            result = stop_session(SID)
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["queries"], 3)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(q.call_count, 3)


class StopManagedGuardTest(unittest.TestCase):
    def test_refuses_unmanaged_session_without_calling_cli(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session("muse-h3-other", "G4", "run-9", "2026-11-12T13:00:00+00:00",
                           path=state, production_task_id="h3-ab-20261112")
            with mock.patch.object(wd, "_run_colab") as run:
                result = stop_managed_session(SID, path=state)
            run.assert_not_called()
        self.assertEqual(result["state"], "REFUSED_NOT_MANAGED")
        self.assertEqual(result["attempts"], 0)

    def test_refuses_already_stopped_record(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", "2026-11-12T13:00:00+00:00",
                           path=state, production_task_id="h3-ab-20261112")
            from h3_session_watchdog import mark_session_stopped
            mark_session_stopped(SID, SessionState.STOPPED, path=state)
            with mock.patch.object(wd, "_run_colab") as run:
                result = stop_managed_session(SID, path=state)
            run.assert_not_called()
        self.assertEqual(result["state"], "ALREADY_STOPPED")

    def test_managed_active_delegates_to_stop(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", "2026-11-12T13:00:00+00:00",
                           path=state, production_task_id="h3-ab-20261112")
            with mock.patch.object(wd, "_run_colab",
                                   return_value=_proc(returncode=0, stdout="ok")), \
                    mock.patch.object(wd, "query_session_state",
                                      return_value=SessionState.STOPPED), \
                    mock.patch("time.sleep"):
                result = stop_managed_session(SID, path=state)
        self.assertEqual(result["state"], "STOPPED")


class WatchStopResultTest(unittest.TestCase):
    def _setup(self, d):
        state = Path(d) / "sessions.json"
        record_session(SID, "G4", "run-1",
                       (_base_dt() - datetime.timedelta(minutes=1)).isoformat(),
                       path=state, production_task_id="h3-ab-20261112")
        return load_managed_sessions(path=state)

    def test_watch_unconfirmed_stop_exits_1_without_marking(self):
        with tempfile.TemporaryDirectory() as d:
            stored = self._setup(d)
            ledger = Path(d) / "ledger.json"
            active_result = {"session": SID, "state": "ACTIVE", "stop_cli_ok": True,
                             "attempts": 3, "queries": 3, "error": "session still ACTIVE after stop"}
            with mock.patch.object(wd, "load_managed_sessions", return_value=stored), \
                    mock.patch.object(wd, "mark_session_stopped") as marked, \
                    mock.patch.object(wd, "LEDGER_PATH", ledger), \
                    mock.patch.object(wd, "query_session_state",
                                      return_value=SessionState.ACTIVE), \
                    mock.patch.object(wd, "stop_managed_session",
                                      return_value=active_result), \
                    mock.patch("h3_budget.read_live_usage",
                               return_value={"balance": 160.0, "rate": 8.88}), \
                    mock.patch("auth_support.resolve_colab_executable",
                               return_value="/usr/bin/colab"):
                rc = watch(SID, production_task_id="h3-ab-20261112", poll_sec=0.01)
            self.assertEqual(rc, 1)
            marked.assert_not_called()


class CliStopSubcommandTest(unittest.TestCase):
    def _run_main(self, argv):
        with mock.patch.object(sys, "argv", argv):
            return wd.main()

    def test_cli_stop_confirmed_exits_0_and_marks(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", "2026-11-12T13:00:00+00:00",
                           path=state, production_task_id="h3-ab-20261112")
            stopped = {"session": SID, "state": "STOPPED", "stop_cli_ok": True,
                       "attempts": 1, "queries": 1, "error": None}
            with mock.patch.object(wd, "STATE_PATH", state), \
                    mock.patch.object(wd, "stop_managed_session",
                                      return_value=stopped) as stop_fn, \
                    mock.patch.object(wd, "mark_session_stopped") as marked:
                rc = self._run_main(["h3_session_watchdog.py", "stop",
                                     "--session", SID])
            self.assertEqual(rc, 0)
            stop_fn.assert_called_once()
            marked.assert_called_once_with(SID, SessionState.STOPPED)

    def test_cli_stop_unknown_exits_1_without_marking(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", "2026-11-12T13:00:00+00:00",
                           path=state, production_task_id="h3-ab-20261112")
            unknown = {"session": SID, "state": "UNKNOWN", "stop_cli_ok": False,
                       "attempts": 3, "queries": 3, "error": "session state UNKNOWN after stop"}
            with mock.patch.object(wd, "STATE_PATH", state), \
                    mock.patch.object(wd, "stop_managed_session",
                                      return_value=unknown), \
                    mock.patch.object(wd, "mark_session_stopped") as marked:
                rc = self._run_main(["h3_session_watchdog.py", "stop",
                                     "--session", SID])
            self.assertEqual(rc, 1)
            marked.assert_not_called()


class IndependenceDocTest(unittest.TestCase):
    def test_independence_marking_is_honest(self):
        doc = (Path(__file__).resolve().parents[1] / "docs" / "watchdog_independence.md").read_text()
        self.assertIn("INDEPENDENT_SHUTDOWN_NOT_GUARANTEED", doc)
        self.assertIn("kill -9", doc)
        src = Path(wd.__file__).read_text()
        self.assertIn("INDEPENDENT_SHUTDOWN_NOT_GUARANTEED", src)


if __name__ == "__main__":
    unittest.main()
