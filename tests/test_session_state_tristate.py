"""TASK-005 §3 (Issue #24): session state must be three-state.

The old boolean session_still_active() used substring matching and
never checked the CLI exit code, so a failed `colab sessions` call
(empty output) was misread as 'session gone'. query_session_state()
returns ACTIVE only on an exact match in a reliably parsed response,
STOPPED only on an explicit no-sessions response or a parsed list
without the target, and UNKNOWN on every unconfirmable outcome.
Callers fail closed on UNKNOWN.

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

import h3_controller as hc  # noqa: E402
import h3_session_watchdog as wd  # noqa: E402
from h3_session_watchdog import (  # noqa: E402
    SessionQueryError,
    SessionState,
    load_managed_sessions,
    query_session_state,
    record_session,
    reconcile_on_startup,
    stop_session,
    watch,
)

SID = "muse-h3-abc123"
LINE = "[muse-h3-abc123] gpu-g4-xyz | Hardware: G4 | Shape: High-RAM | Variant: GPU"
OTHER_LINE = "[muse-h3-xyz999] gpu-a100-abc | Hardware: A100 | Shape: High-RAM | Variant: GPU"
NOTICE = ("[colab] A new version of Colab CLI is available: 0.8.0 (current: 0.7.5)\n"
          "[colab] Run 'colab update' to update.\n"
          "[colab] To silence this check, set \"enable_update_check\": false in ~/.config/colab-cli/settings.json\n")


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _base_dt():
    return datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.timezone.utc)


class QueryMatrixTest(unittest.TestCase):
    def _query(self, proc, sid=SID):
        with mock.patch.object(wd, "_run_colab", return_value=proc):
            return query_session_state(sid)

    def test_active_exact_line(self):
        self.assertIs(self._query(_proc(stdout=LINE + "\n")), SessionState.ACTIVE)

    def test_active_with_update_notice(self):
        self.assertIs(self._query(_proc(stdout=NOTICE + LINE + "\n")), SessionState.ACTIVE)

    def test_active_json_list(self):
        self.assertIs(
            self._query(_proc(stdout='[{"name": "muse-h3-abc123", "gpu": "G4"}]')),
            SessionState.ACTIVE)

    def test_stopped_no_sessions_marker(self):
        self.assertIs(
            self._query(_proc(stdout="[colab] No active sessions found on server.\n")),
            SessionState.STOPPED)

    def test_stopped_other_sessions_only(self):
        self.assertIs(self._query(_proc(stdout=OTHER_LINE + "\n")), SessionState.STOPPED)

    def test_substring_must_not_match(self):
        # The target id is a prefix of a longer session id; exact match only.
        self.assertIs(
            self._query(_proc(stdout="[muse-h3-abc1234] gpu-g4-xyz | Hardware: G4\n")),
            SessionState.STOPPED)
        self.assertIs(self._query(_proc(stdout=LINE + "\n"), sid="muse-h3-abc1234"),
                      SessionState.STOPPED)

    def test_unknown_nonzero_exit_even_if_id_in_output(self):
        # Auth failure text that happens to mention the session id must
        # never yield ACTIVE: the exit code is checked first.
        proc = _proc(returncode=1, stdout="",
                     stderr="ERROR: Failed to authenticate; session muse-h3-abc123 unreachable")
        self.assertIs(self._query(proc), SessionState.UNKNOWN)

    def test_unknown_timeout(self):
        with mock.patch.object(wd, "_run_colab",
                               side_effect=subprocess.TimeoutExpired("colab", 65)):
            self.assertIs(query_session_state(SID), SessionState.UNKNOWN)

    def test_unknown_empty_output_exit_zero(self):
        # Exit 0 + empty output cannot confirm absence (the real
        # no-sessions output is the explicit marker, not silence).
        self.assertIs(self._query(_proc(stdout="", stderr="")), SessionState.UNKNOWN)

    def test_unknown_garbage_format(self):
        self.assertIs(self._query(_proc(stdout="something unexpected\nmore garbage\n")),
                      SessionState.UNKNOWN)

    def test_unknown_missing_cli(self):
        with mock.patch.object(wd, "_run_colab",
                               side_effect=FileNotFoundError("no colab")):
            self.assertIs(query_session_state(SID), SessionState.UNKNOWN)


class ReconcileUnknownTest(unittest.TestCase):
    def _state_file(self, d):
        state = Path(d) / "sessions.json"
        record_session(SID, "G4", "run-1", "2026-11-12T13:00:00+00:00",
                       path=state, production_task_id="h3-ab-20261112")
        return state

    def test_reconcile_raises_on_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            state = self._state_file(d)
            with mock.patch.object(wd, "query_session_state", return_value=SessionState.UNKNOWN):
                with self.assertRaises(SessionQueryError):
                    reconcile_on_startup("adc", path=state)

    def test_reconcile_stopped_does_not_block(self):
        with tempfile.TemporaryDirectory() as d:
            state = self._state_file(d)
            with mock.patch.object(wd, "query_session_state", return_value=SessionState.STOPPED):
                out = reconcile_on_startup("adc", path=state)
            self.assertEqual(out["managed_active_sessions"], [])


class WatchUnknownTest(unittest.TestCase):
    def _setup(self, d, deadline):
        state = Path(d) / "sessions.json"
        record_session(SID, "G4", "run-1", deadline, path=state,
                       production_task_id="h3-ab-20261112")
        return load_managed_sessions(path=state)

    def test_watch_unknown_then_stopped_marks_once(self):
        with tempfile.TemporaryDirectory() as d:
            # Deadline must be in the real future (not the fixed BASE date):
            # otherwise should_stop fires on the first poll.
            future = (datetime.datetime.now(datetime.timezone.utc)
                      + datetime.timedelta(hours=1)).isoformat()
            stored = self._setup(d, future)
            ledger = Path(d) / "ledger.json"
            states = [SessionState.UNKNOWN, SessionState.STOPPED]
            with mock.patch.object(wd, "load_managed_sessions", return_value=stored), \
                    mock.patch.object(wd, "mark_session_stopped") as marked, \
                    mock.patch.object(wd, "LEDGER_PATH", ledger), \
                    mock.patch.object(wd, "query_session_state", side_effect=states), \
                    mock.patch("h3_budget.read_live_usage",
                               return_value={"balance": 160.0, "rate": 0.0}), \
                    mock.patch("auth_support.resolve_colab_executable",
                               return_value="/usr/bin/colab"):
                rc = watch(SID, production_task_id="h3-ab-20261112", poll_sec=0.01)
            self.assertEqual(rc, 0)
            marked.assert_called_once_with(SID, SessionState.STOPPED)

    def test_watch_unknown_at_deadline_escalates_without_marking(self):
        # Deadline already past + state UNKNOWN: the watch must attempt
        # the stop, fail to confirm, and exit 1 WITHOUT marking stopped.
        with tempfile.TemporaryDirectory() as d:
            stored = self._setup(d, (_base_dt() - datetime.timedelta(minutes=1)).isoformat())
            ledger = Path(d) / "ledger.json"
            with mock.patch.object(wd, "load_managed_sessions", return_value=stored), \
                    mock.patch.object(wd, "mark_session_stopped") as marked, \
                    mock.patch.object(wd, "LEDGER_PATH", ledger), \
                    mock.patch.object(wd, "query_session_state",
                                      return_value=SessionState.UNKNOWN), \
                    mock.patch.object(wd, "stop_managed_session",
                                      return_value={"session": SID, "state": "UNKNOWN",
                                                    "stop_cli_ok": False, "attempts": 3,
                                                    "queries": 3,
                                                    "error": "session state UNKNOWN after stop"}), \
                    mock.patch("h3_budget.read_live_usage",
                               return_value={"balance": 160.0, "rate": 8.88}), \
                    mock.patch("auth_support.resolve_colab_executable",
                               return_value="/usr/bin/colab"):
                rc = watch(SID, production_task_id="h3-ab-20261112", poll_sec=0.01)
            self.assertEqual(rc, 1)
            marked.assert_not_called()


class StopSessionTristateTest(unittest.TestCase):
    def test_stop_confirmed_only_on_stopped(self):
        with mock.patch.object(wd, "_run_colab",
                               return_value=_proc(returncode=0, stdout="ok")) as run, \
                mock.patch.object(wd, "query_session_state",
                                  return_value=SessionState.STOPPED), \
                mock.patch("time.sleep"):
            result = stop_session(SID)
        self.assertEqual(result["state"], "STOPPED")
        self.assertTrue(result["stop_cli_ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertIsNone(result["error"])
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ["stop", "-s", SID])

    def test_stop_not_confirmed_when_still_active(self):
        with mock.patch.object(wd, "_run_colab",
                               return_value=_proc(returncode=0, stdout="ok")), \
                mock.patch.object(wd, "query_session_state",
                                  return_value=SessionState.ACTIVE), \
                mock.patch("time.sleep"):
            result = stop_session(SID)
        self.assertEqual(result["state"], "ACTIVE")
        self.assertEqual(result["attempts"], 3)
        self.assertIsNotNone(result["error"])

    def test_stop_not_confirmed_on_unknown(self):
        with mock.patch.object(wd, "_run_colab",
                               return_value=_proc(returncode=0, stdout="ok")) as run, \
                mock.patch.object(wd, "query_session_state",
                                  return_value=SessionState.UNKNOWN) as q, \
                mock.patch("time.sleep"):
            result = stop_session(SID)
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(q.call_count, 3)
        self.assertEqual(result["attempts"], 3)


class ControllerStopTest(unittest.TestCase):
    def test_already_stopped_no_stop_call(self):
        with mock.patch("h3_session_watchdog.query_session_state",
                        return_value=SessionState.STOPPED), \
                mock.patch.object(hc, "run_cli") as run_cli, \
                mock.patch.object(hc, "record"), \
                mock.patch.object(hc, "_confirm_gpu_lease_stopped") as lease_confirmed, \
                mock.patch("h3_session_watchdog.mark_session_stopped") as marked:
            out = hc.stop_and_verify_session(SID)
        self.assertTrue(out["stopped_confirmed"])
        self.assertEqual(out["state"], "STOPPED")
        run_cli.assert_not_called()
        marked.assert_called_once_with(SID, SessionState.STOPPED)
        lease_confirmed.assert_called_once_with(SID)

    def test_active_then_stopped(self):
        with mock.patch("h3_session_watchdog.query_session_state",
                        side_effect=[SessionState.ACTIVE, SessionState.STOPPED]), \
                mock.patch.object(hc, "run_cli",
                                  return_value={"returncode": 0}) as run_cli, \
                mock.patch.object(hc, "record") as rec, \
                mock.patch("h3_budget.read_live_usage",
                          return_value={"balance": 144.0, "rate": 0.0}), \
                mock.patch("h3_session_watchdog.mark_session_stopped") as marked, \
                mock.patch.object(hc, "_confirm_gpu_lease_stopped") as lease_confirmed, \
                mock.patch("time.sleep"):
            out = hc.stop_and_verify_session(SID)
        self.assertTrue(out["stopped_confirmed"])
        self.assertEqual(out["state"], "STOPPED")
        run_cli.assert_called_once_with(["stop", "-s", SID], timeout=65)
        events = [c.args[0]["event"] for c in rec.call_args_list]
        self.assertIn("stopped", events)
        marked.assert_called_once_with(SID, SessionState.STOPPED)
        lease_confirmed.assert_called_once_with(SID)

    def test_unknown_never_confirms(self):
        with mock.patch("h3_session_watchdog.query_session_state",
                        return_value=SessionState.UNKNOWN), \
                mock.patch.object(hc, "run_cli") as run_cli, \
                mock.patch.object(hc, "record") as rec, \
                mock.patch("h3_session_watchdog.mark_session_stopped") as marked, \
                mock.patch.object(hc, "_confirm_gpu_lease_stopped") as lease_confirmed, \
                mock.patch("time.sleep"):
            out = hc.stop_and_verify_session(SID)
        self.assertFalse(out["stopped_confirmed"])
        self.assertEqual(out["state"], "UNKNOWN")
        run_cli.assert_not_called()
        marked.assert_not_called()
        lease_confirmed.assert_not_called()
        events = [c.args[0]["event"] for c in rec.call_args_list]
        self.assertIn("CRITICAL_ORPHAN_RISK", events)
        self.assertEqual(events.count("stop_error"), 3)

    def test_still_active_after_stop_never_confirms(self):
        with mock.patch("h3_session_watchdog.query_session_state",
                        return_value=SessionState.ACTIVE), \
                mock.patch.object(hc, "run_cli",
                                  return_value={"returncode": 0}), \
                mock.patch.object(hc, "record") as rec, \
                mock.patch("h3_session_watchdog.mark_session_stopped") as marked, \
                mock.patch.object(hc, "_confirm_gpu_lease_stopped") as lease_confirmed, \
                mock.patch("time.sleep"):
            out = hc.stop_and_verify_session(SID)
        self.assertFalse(out["stopped_confirmed"])
        self.assertEqual(out["state"], "ACTIVE")
        marked.assert_not_called()
        lease_confirmed.assert_not_called()
        events = [c.args[0]["event"] for c in rec.call_args_list]
        self.assertIn("CRITICAL_ORPHAN_RISK", events)


if __name__ == "__main__":
    unittest.main()
