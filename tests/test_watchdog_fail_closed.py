"""Tests for the fail-closed GPU watchdog (TASK-002 §6).

§6.1: the controller refuses to run inference unless the watchdog
proves it started — alive, holding the session record, bound to the
right production task, able to resolve the Colab CLI that executes the
stop. Any startup failure raises WATCHDOG_NOT_RUNNING.

§6.2: a managed session still active before allocation blocks a new
GPU with CRITICAL_ORPHAN_GPU_SESSION, carrying per-session evidence
(run/task/deadline/unbilled estimate). Never stop non-project sessions.

No GPU, no live CLI: every live call is mocked or stubbed.
"""
import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_controller as hc  # noqa: E402
import h3_session_watchdog as wd  # noqa: E402
from h3_session_watchdog import (  # noqa: E402
    load_managed_sessions,
    record_session,
    reconcile_on_startup,
    watch,
)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


BASE = datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.timezone.utc)


class WaitReadyTest(unittest.TestCase):
    def _sleeper(self):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_ready_file_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            ready = Path(d) / "ready.json"
            ready.write_text(json.dumps({
                "session_id": "muse-h3-abc", "production_task_id": "h3-ab-20261112",
                "run_id": "run-1", "deadline_utc": _iso(BASE)}))
            proc = self._sleeper()
            try:
                got = hc._wait_watchdog_ready(proc, ready, "muse-h3-abc", "h3-ab-20261112")
            finally:
                proc.terminate()
            self.assertEqual(got["run_id"], "run-1")

    def test_exited_process_raises(self):
        with tempfile.TemporaryDirectory() as d:
            ready = Path(d) / "ready.json"
            proc = subprocess.run([sys.executable, "-c", "raise SystemExit(3)"],
                                  capture_output=True)
            fake = mock.Mock()
            fake.poll.return_value = 3
            fake.returncode = 3
            with self.assertRaisesRegex(RuntimeError, r"WATCHDOG_NOT_RUNNING.*code 3"):
                hc._wait_watchdog_ready(fake, ready, "muse-h3-abc", "h3-ab-20261112")

    def test_readiness_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as d:
            ready = Path(d) / "ready.json"
            ready.write_text(json.dumps({
                "session_id": "muse-h3-abc", "production_task_id": "h3-ab-20261111"}))
            proc = self._sleeper()
            try:
                with self.assertRaisesRegex(RuntimeError, "WATCHDOG_NOT_RUNNING.*mismatch"):
                    hc._wait_watchdog_ready(proc, ready, "muse-h3-abc", "h3-ab-20261112")
            finally:
                proc.terminate()

    def test_timeout_raises(self):
        with tempfile.TemporaryDirectory() as d:
            ready = Path(d) / "ready.json"
            proc = self._sleeper()
            try:
                with self.assertRaisesRegex(RuntimeError, "WATCHDOG_NOT_RUNNING.*did not signal"):
                    hc._wait_watchdog_ready(proc, ready, "muse-h3-abc", "h3-ab-20261112",
                                            timeout_sec=0.5)
            finally:
                proc.terminate()


class WatchReadyFileTest(unittest.TestCase):
    def _setup_state(self, d, task="h3-ab-20261112"):
        state = Path(d) / "sessions.json"
        record_session("muse-h3-abc", "G4", "run-1", _iso(BASE + datetime.timedelta(hours=1)),
                       path=state, production_task_id=task)
        return load_managed_sessions(path=state)

    def test_watch_writes_ready_file(self):
        with tempfile.TemporaryDirectory() as d:
            stored = self._setup_state(d)
            ready = Path(d) / "ready.json"
            ledger = Path(d) / "ledger.json"
            with mock.patch.object(wd, "load_managed_sessions", return_value=stored), \
                    mock.patch.object(wd, "mark_session_stopped"), \
                    mock.patch.object(wd, "LEDGER_PATH", ledger), \
                    mock.patch.object(wd, "session_still_active", return_value=False), \
                    mock.patch("auth_support.resolve_colab_executable", return_value="/usr/bin/colab"):
                rc = watch("muse-h3-abc", production_task_id="h3-ab-20261112", ready_path=ready)
            self.assertEqual(rc, 0)
            got = json.loads(ready.read_text())
            self.assertEqual(got["session_id"], "muse-h3-abc")
            self.assertEqual(got["production_task_id"], "h3-ab-20261112")
            self.assertEqual(got["run_id"], "run-1")
            self.assertEqual(got["deadline_utc"], _iso(BASE + datetime.timedelta(hours=1)))

    def test_watch_refuses_task_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            stored = self._setup_state(d)
            ready = Path(d) / "ready.json"
            with mock.patch.object(wd, "load_managed_sessions", return_value=stored), \
                    mock.patch("auth_support.resolve_colab_executable", return_value="/usr/bin/colab"):
                rc = watch("muse-h3-abc", production_task_id="h3-ab-20261111", ready_path=ready)
            self.assertEqual(rc, 1)
            self.assertFalse(ready.exists())

    def test_watch_refuses_without_cli(self):
        with tempfile.TemporaryDirectory() as d:
            stored = self._setup_state(d)
            ready = Path(d) / "ready.json"
            with mock.patch.object(wd, "load_managed_sessions", return_value=stored), \
                    mock.patch("auth_support.resolve_colab_executable",
                               side_effect=FileNotFoundError("no colab")):
                rc = watch("muse-h3-abc", production_task_id="h3-ab-20261112", ready_path=ready)
            self.assertEqual(rc, 1)
            self.assertFalse(ready.exists())

    def test_watch_refuses_missing_record(self):
        with tempfile.TemporaryDirectory() as d:
            ready = Path(d) / "ready.json"
            with mock.patch.object(wd, "load_managed_sessions", return_value=[]):
                rc = watch("muse-h3-nope", production_task_id="h3-ab-20261112", ready_path=ready)
            self.assertEqual(rc, 1)
            self.assertFalse(ready.exists())


class ReconcileDetailTest(unittest.TestCase):
    def test_reconcile_reports_detail_and_unbilled(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            started = BASE - datetime.timedelta(minutes=60)
            rec = record_session("muse-h3-abc", "G4", "run-1", _iso(BASE + datetime.timedelta(hours=1)),
                                 path=state, production_task_id="h3-ab-20261112")
            # Force a known started_at so the estimate is deterministic.
            stored = load_managed_sessions(path=state)
            stored[0]["started_at_utc"] = _iso(started)
            from h3_session_watchdog import save_managed_sessions
            save_managed_sessions(stored, path=state)
            with mock.patch.object(wd, "session_still_active", return_value=True), \
                    mock.patch("h3_budget.read_live_usage",
                               return_value={"balance": 160.0, "rate": 8.88}):
                out = reconcile_on_startup("adc", path=state)
            self.assertEqual(out["managed_active_sessions"], ["muse-h3-abc"])
            (detail,) = out["managed_active_detail"]
            self.assertEqual(detail["run_id"], "run-1")
            self.assertEqual(detail["production_task_id"], "h3-ab-20261112")
            # 60 min at 8.88 CU/h, capped by the estimator's own rules.
            self.assertIsNotNone(detail["unbilled_estimate_cu"])
            self.assertGreater(detail["unbilled_estimate_cu"], 0)

    def test_reconcile_query_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session("muse-h3-abc", "G4", "run-1", _iso(BASE + datetime.timedelta(hours=1)),
                           path=state, production_task_id="h3-ab-20261112")
            with mock.patch.object(wd, "session_still_active",
                                   side_effect=RuntimeError("colab CLI down")):
                with self.assertRaises(RuntimeError):
                    reconcile_on_startup("adc", path=state)


class ControllerWiringTest(unittest.TestCase):
    def test_run_variant_wiring(self):
        src = Path(hc.__file__).read_text()
        self.assertIn("WATCHDOG_NOT_RUNNING", src)
        self.assertIn("CRITICAL_ORPHAN_GPU_SESSION", src)
        self.assertIn("_wait_watchdog_ready(", src)
        self.assertIn('"--ready-file"', src)
        # The old fail-open swallow is gone: the start-failure record is
        # still written, but the exception now propagates.
        self.assertIn('"watchdog_start_failed"', src)
        self.assertIn("stop_and_verify_session(session)", src)


if __name__ == "__main__":
    unittest.main()
