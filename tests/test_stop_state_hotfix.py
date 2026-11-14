"""TASK-006 P0-A (Issue #42): stop-state hotfix tests.

The bug: run_variant()'s outer finally (and the mount-acceptance
equivalent) called mark_session_stopped() unconditionally after
stop_and_verify_session(), even when the stop was NOT confirmed — so a
still-billing GPU was recorded as stopped, and stop_managed_session()
later refused it as ALREADY_STOPPED without ever calling the CLI. The
watchdog was also terminated before the stop was confirmed.

The fix under test:
- mark_session_stopped() requires a confirmed STOPPED state and refuses
  anything else (ValueError).
- stop_and_verify_session() is the single place that marks, only on a
  confirmed STOPPED verdict.
- The watchdog is terminated only after a confirmed stop; on an
  unconfirmed stop it is left running to enforce the deadline/hard-stop.
- Managed-session mutations hold an exclusive lock across the whole
  load-modify-save and bump a per-record version.
- reconcile_on_startup() checks every record against live state:
  stopped-record-but-ACTIVE is a contradiction (fail closed);
  active-record-but-STOPPED is repaired from the live query.

No GPU, no live CLI, no real Drive: the chain tests mock-execute the
REAL run_variant() with scripted session queries, exactly like
test_drive_delivery.py. A real process kill cannot be simulated
in-process; the watchdog-death test says so in its docstring.
"""
import datetime
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_controller as hc  # noqa: E402
import h3_session_watchdog as hwd  # noqa: E402
import h3_run_manifest as rm  # noqa: E402
import drive_depot  # noqa: E402
from h3_session_watchdog import (  # noqa: E402
    SessionQueryError,
    SessionState,
    load_managed_sessions,
    mark_session_stopped,
    record_session,
    reconcile_on_startup,
    update_session_deadline,
    watch,
)

SID = "muse-h3-abc123"
RUN_ID = "h3-20261112T000000Z-a1b2c3"
SESSION = "muse-h3-test-session"
TASK = "h3-ab-20261111"


def _deadline(hours=1):
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=hours)).isoformat()


def _video_info(sha, frames=274, width=1080, height=1920, fps=24.0):
    return {"frame_count": frames, "width": width, "height": height,
            "fps": fps, "sha256": sha}


def _args():
    return SimpleNamespace(
        variant="A", run_id=RUN_ID, production_task_id=TASK,
        execute=True, confirm_gpu=True, authorize_task_gpu=True,
        allow_g4=True, allow_a100_highmem=True,
        project_budget_cu=30.0, per_session_budget_cu=25.0,
        gpu_policy="G4_THEN_A100_HIGHMEM",
        comfy_ref="61e524bdba7a4e193b45c2697f1af83c1beb86d6",
        drive_cache_dir="/content/drive/MyDrive/Muse-H3-Model-Depot",
        approve_interactive_drive_mount=True, fresh_inference=True,
        workflow_api="/tmp/workflow_api.json",
        cache_manifest="/tmp/verified_manifest.json",
        max_rate_cu_h=14.0, max_wall_sec=3600,
        prior_unbilled_reserve_cu=0.14, authorize_new_task=False)


# TASK-006 P0-B: the chain harness stubs the download-path boundaries
# (preflight, block builder, token upload) the same way it already
# stubs mount_drive / verify_ready / validate_local. The real builder,
# preflight and token upload are covered by tests/test_drive_download.py.
_CANNED_DOWNLOAD_BLOCK = {
    "files": [{"file_id": "fid-1",
               "path": "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
               "size": 10, "sha256": "a" * 64, "md5": "b" * 32}],
    "comfyui": None,
    "dest_dir": "/content/muse_h3/drive_downloads",
    "token_file": "/content/muse_h3/drive_token.json",
    "probe": {"file_id": "fid-1"},
}


class MarkRequiresConfirmedStateTest(unittest.TestCase):
    """mark_session_stopped() refuses anything but a confirmed STOPPED."""

    def test_mark_with_confirmed_stopped_marks_and_bumps_version(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            rec = record_session(SID, "G4", "run-1", _deadline(), path=state,
                                 production_task_id=TASK)
            self.assertEqual(rec["version"], 1)
            self.assertTrue(mark_session_stopped(SID, SessionState.STOPPED, path=state))
            stored = load_managed_sessions(path=state)[0]
            self.assertEqual(stored["status"], "stopped")
            self.assertEqual(stored["version"], 2)
            self.assertIn("stopped_at_utc", stored)
            self.assertEqual(stored["run_id"], "run-1")

    def test_mark_with_string_stopped_works(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", _deadline(), path=state,
                           production_task_id=TASK)
            self.assertTrue(mark_session_stopped(SID, "STOPPED", path=state))
            self.assertEqual(load_managed_sessions(path=state)[0]["status"], "stopped")

    def test_mark_with_active_raises_and_leaves_record_active(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", _deadline(), path=state,
                           production_task_id=TASK)
            with self.assertRaisesRegex(ValueError, "confirmed STOPPED"):
                mark_session_stopped(SID, SessionState.ACTIVE, path=state)
            stored = load_managed_sessions(path=state)[0]
            self.assertEqual(stored["status"], "active")
            self.assertEqual(stored["version"], 1)

    def test_mark_with_unknown_raises(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", _deadline(), path=state,
                           production_task_id=TASK)
            with self.assertRaisesRegex(ValueError, "confirmed STOPPED"):
                mark_session_stopped(SID, SessionState.UNKNOWN, path=state)
            self.assertEqual(load_managed_sessions(path=state)[0]["status"], "active")

    def test_mark_missing_record_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            self.assertFalse(mark_session_stopped("muse-h3-nope", SessionState.STOPPED,
                                                  path=state))

    def test_mark_already_stopped_is_idempotent_no_version_bump(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", _deadline(), path=state,
                           production_task_id=TASK)
            self.assertTrue(mark_session_stopped(SID, SessionState.STOPPED, path=state))
            self.assertTrue(mark_session_stopped(SID, SessionState.STOPPED, path=state))
            self.assertEqual(load_managed_sessions(path=state)[0]["version"], 2)


class StopAndVerifyMarksOnlyOnConfirmedTest(unittest.TestCase):
    """stop_and_verify_session() marks only on a confirmed STOPPED verdict."""

    def _run_stop(self, query_states, run_cli_side_effect=None):
        """Run the real stop_and_verify_session against a temp state file
        with scripted session queries. Returns (result, stored_record,
        run_cli_mock, events, lease_confirmed_mock). The lease confirm is
        mocked (TASK-006 P0-C) so the test never touches the real
        registry file."""
        tmp = tempfile.TemporaryDirectory()
        state = Path(tmp.name) / "sessions.json"
        record_session(SID, "G4", "run-1", _deadline(), path=state,
                       production_task_id=TASK)
        real_mark = hwd.mark_session_stopped
        events = []
        lease_confirmed = mock.Mock()
        patches = [
            mock.patch.object(hwd, "mark_session_stopped",
                              side_effect=lambda sid, cs, path=None: real_mark(sid, cs, path=state)),
            mock.patch.object(hwd, "query_session_state", side_effect=list(query_states)),
            mock.patch.object(hc, "run_cli",
                              side_effect=run_cli_side_effect) if run_cli_side_effect
            else mock.patch.object(hc, "run_cli", return_value={"returncode": 0}),
            mock.patch.object(hc, "read_live_usage",
                              return_value={"balance": 150.0, "rate": 5.0}),
            mock.patch.object(hc, "record", side_effect=lambda data: events.append(data)),
            mock.patch.object(hc, "_confirm_gpu_lease_stopped", lease_confirmed),
            mock.patch.object(hc.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
        try:
            result = hc.stop_and_verify_session(SID)
            stored = load_managed_sessions(path=state)[0]
            return result, stored, events, lease_confirmed
        finally:
            for p in reversed(patches):
                p.stop()
            tmp.cleanup()

    def test_stop_success_query_stopped_marks_record(self):
        result, stored, _events, lease_confirmed = self._run_stop(
            [SessionState.ACTIVE, SessionState.STOPPED])
        self.assertTrue(result["stopped_confirmed"])
        self.assertEqual(result["state"], "STOPPED")
        self.assertTrue(result["record_marked"])
        self.assertEqual(stored["status"], "stopped")
        self.assertEqual(stored["version"], 2)
        lease_confirmed.assert_called_once_with(SID)

    def test_stop_cli_zero_but_query_still_active_leaves_record(self):
        # The exact P0-A scenario: the stop CLI exits 0, but the session
        # is still there. The record must stay active and the run must
        # report the orphan risk — never a confirmed stop.
        result, stored, events, lease_confirmed = self._run_stop(
            [SessionState.ACTIVE] * 6)
        self.assertFalse(result["stopped_confirmed"])
        self.assertEqual(result["state"], "ACTIVE")
        self.assertFalse(result["record_marked"])
        self.assertEqual(stored["status"], "active")
        self.assertEqual(stored["version"], 1)
        self.assertTrue(any(e.get("event") == "CRITICAL_ORPHAN_RISK" for e in events))
        lease_confirmed.assert_not_called()

    def test_stop_timeout_query_unknown_leaves_record(self):
        result, stored, events, lease_confirmed = self._run_stop(
            [SessionState.UNKNOWN] * 3)
        self.assertFalse(result["stopped_confirmed"])
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertFalse(result["record_marked"])
        self.assertEqual(stored["status"], "active")
        self.assertTrue(any(e.get("event") == "CRITICAL_ORPHAN_RISK" for e in events))
        lease_confirmed.assert_not_called()

    def test_stop_cli_fails_but_later_query_stopped_confirms_and_marks(self):
        # stop_session contract (TASK-005 §3): the CLI exit code is only
        # evidence; the query decides. A failed CLI followed by a
        # confirmed STOPPED is still a confirmed stop.
        def failing_cli(*args, **kwargs):
            raise RuntimeError("CLI failed: exit 1")

        result, stored, _events, lease_confirmed = self._run_stop(
            [SessionState.ACTIVE, SessionState.STOPPED],
            run_cli_side_effect=failing_cli)
        self.assertTrue(result["stopped_confirmed"])
        self.assertEqual(result["state"], "STOPPED")
        self.assertTrue(result["record_marked"])
        self.assertEqual(stored["status"], "stopped")
        lease_confirmed.assert_called_once_with(SID)


class ConcurrencyTest(unittest.TestCase):
    """Controller + watchdog mutating the same file concurrently must not
    lose updates: every mutation bumps the version by exactly one."""

    def test_concurrent_updates_no_lost_version(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", _deadline(), path=state,
                           production_task_id=TASK)
            errors = []

            def updater(n):
                try:
                    for i in range(25):
                        update_session_deadline(SID, _deadline(hours=2 + i), path=state)
                except Exception as e:  # noqa: BLE001 - collected, asserted below
                    errors.append(e)

            def rerecorder():
                try:
                    for _ in range(25):
                        record_session(SID, "G4", "run-1", _deadline(), path=state,
                                       production_task_id=TASK)
                except Exception as e:  # noqa: BLE001 - collected, asserted below
                    errors.append(e)

            t1 = threading.Thread(target=updater, args=(1,))
            t2 = threading.Thread(target=rerecorder)
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)
            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertEqual(errors, [])
            stored = load_managed_sessions(path=state)[0]
            # Initial version 1 + 50 mutations, none lost.
            self.assertEqual(stored["version"], 51)
            self.assertEqual(stored["status"], "active")
            self.assertEqual(stored["run_id"], "run-1")


class _ChainHarness(unittest.TestCase):
    """Mock-execute the REAL run_variant() with the REAL stop path.

    Differences from test_drive_delivery's harness: stop_and_verify_session
    is REAL (scripted only at the query/CLI boundary), and the managed
    session file is REAL (temp file), so the tests assert the actual
    record/watchdog behaviour of the P0-A fix.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.out_root = base / "output"
        self.runs_root = base / "runs"
        self.out_root.mkdir()
        self.runs_root.mkdir()
        self.state_path = base / "sessions.json"
        self.mp4 = self.out_root / RUN_ID / "A.mp4"
        self.mp4.parent.mkdir(parents=True)
        self.mp4.write_bytes(b"fake-validated-mp4-bytes" * 100)
        self.sha = hashlib.sha256(self.mp4.read_bytes()).hexdigest()
        self.video_info = _video_info(self.sha)
        self.events = []
        self.patches = []
        self.query_states = [SessionState.ACTIVE, SessionState.STOPPED]
        self._install()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _po(self, target, attr, new=mock.DEFAULT, **kwargs):
        if new is mock.DEFAULT:
            p = mock.patch.object(target, attr, **kwargs)
        else:
            p = mock.patch.object(target, attr, new, **kwargs)
        m = p.start()
        self.patches.append(p)
        return m

    def _install(self):
        self._po(hc, "OUTPUT_ROOT", self.out_root)
        self._po(hc, "RUNS", self.runs_root)
        self._po(hc, "ROOT", Path(self.tmp.name))
        self._po(hc, "record", lambda data: self.events.append(data))
        self._po(hc.time, "sleep", lambda s: None)
        self._po(hc, "validate_local")
        self._po(hc, "validate_comfy_workflow", return_value={})
        # TASK-007: reference-image materialisation is stubbed here the
        # same way validate_local is — the real function (local SHA-256
        # check + Drive download) is covered by tests/test_task_spec.py,
        # and the temp ROOT has no poster file.
        self._po(hc, "_materialize_ref_images",
                 return_value={"ref_image_1": Path("/tmp/ref_image_1.jpg"),
                               "ref_image_2": Path("/tmp/ref_image_2.jpg")})
        self._po(hc, "verify_environment", return_value={"ok": True, "checked": []})
        self._po(hc, "verify_drive_download_preflight",
                 return_value={"preflight": "ok"})
        self._po(hc, "_build_drive_download_block",
                 return_value=_CANNED_DOWNLOAD_BLOCK)
        self._po(hc, "_mint_and_upload_drive_token",
                 return_value={"token_id": "test-token-id",
                               "remote_path": "/content/muse_h3/drive_token.json"})
        self._po(hc, "_register_gpu_lease",
                 return_value={"session_id": "muse-h3-test",
                               "version": 1, "deadline_final": False})
        self._po(hc, "_update_gpu_lease_deadline", return_value=None)
        self._po(hc, "_confirm_gpu_lease_stopped", return_value=None)
        self._po(hc, "initialize_project_budget",
                 return_value={"max_observed_spent_cu": 0.0, "production_task_id": TASK})
        self._po(hc, "inspect",
                 return_value={"cu_balance": 150.0, "rate": 5.0, "sessions": []})
        self._po(hc, "ensure_no_foreign_sessions")
        self._po(hc, "run_cli", return_value={"returncode": 0})
        self._po(hc, "choose_gpu_candidate", return_value={"key": "G4"})
        self._po(hc, "allocate_managed_session", return_value=SESSION)
        self._po(hc, "probe_actual_gpu", return_value={"gpu": "G4"})
        self._po(hc, "reconcile_budget", side_effect=lambda ledger, bal, path: ledger)
        self._po(hc, "estimate_unbilled_cu", return_value=0.1)
        self._po(hc, "allowed_runtime_seconds", return_value=3600)
        self._po(hc, "initial_session_wall_seconds", return_value=3600)
        self._po(hc, "exec_script",
                 return_value={"stdout": "MUSE_DRIVE_MOUNT_MODELS_VISIBLE 4\n"
                               "MUSE_DRIVE_DOWNLOAD_PROBE_OK fid-1 10 10", "returncode": 0})
        self._po(hc, "check_mount_signature", return_value=True)
        self._po(hc, "mount_drive", return_value={})
        self._po(hc, "launch_worker",
                 return_value={"run_id": RUN_ID, "variant": "A", "pid": 123,
                               "state_file": "/content/muse_h3/MUSE_H3_JOB_STATE.json",
                               "log_file": "/content/muse_h3/worker.log"})
        self._po(hc, "wait_for_worker",
                 return_value={"run_id": RUN_ID, "variant": "A", "stage": "VIDEO_READY",
                               "output": {"file": "/content/muse_h3/output/shao_nian_h3_A.mp4",
                                          "frames": 274, "sha256": self.sha,
                                          "seconds": 274 / 24, "with_audio": True}})
        self._po(hc, "check_remote_output", return_value={"ok": True})
        self._po(hc, "download_and_validate_variant", return_value=self.video_info)
        self._po(hc, "download_remote_comfyui_log")
        self._po(hc, "_comfyui_cache_task", return_value={})
        self._po(hc, "_wait_watchdog_ready",
                 return_value={"session_id": SESSION, "production_task_id": TASK})
        self._po(hc, "ledger_path_for_task",
                 return_value=Path(self.tmp.name) / "ledger.json")
        self._po(hc, "read_live_usage", return_value={"balance": 150.0, "rate": 5.0})
        # Real watchdog functions, redirected onto the temp state file.
        self._real_record = hwd.record_session
        self._real_confirm = hwd.confirm_session_active
        self._real_mark = hwd.mark_session_stopped
        self._real_update = hwd.update_session_deadline
        self._real_reconcile = hwd.reconcile_on_startup
        self._po(hwd, "record_session",
                 side_effect=lambda sid, gpu, rid, dl, production_task_id="", status="active":
                     self._real_record(sid, gpu, rid, dl, path=self.state_path,
                                       production_task_id=production_task_id,
                                       status=status))
        self._po(hwd, "confirm_session_active",
                 side_effect=lambda sid, path=None: self._real_confirm(sid, path=self.state_path))
        self._po(hwd, "mark_session_stopped",
                 side_effect=lambda sid, cs, path=None: self._real_mark(sid, cs, path=self.state_path))
        self._po(hwd, "update_session_deadline",
                 side_effect=lambda sid, dl, path=None: self._real_update(sid, dl, path=self.state_path))
        self._po(hwd, "reconcile_on_startup",
                 side_effect=lambda auth="adc", path=None: self._real_reconcile(auth, path=self.state_path))
        self._po(hwd, "query_session_state",
                 side_effect=lambda sid, colab_auth=None: self.query_states.pop(0)
                 if self.query_states else SessionState.ACTIVE)
        import h3_budget
        self._po(h3_budget, "read_live_usage",
                 return_value={"balance": 150.0, "rate": 5.0})
        self._po(drive_depot, "drive_service", return_value=mock.Mock())
        self._po(drive_depot, "account_matches", return_value=True)
        self._po(drive_depot, "verify_ready", return_value={})
        self.proc = mock.Mock()
        self.proc.poll.return_value = None
        popen_patch = mock.patch("subprocess.Popen", return_value=self.proc)
        popen_patch.start()
        self.patches.append(popen_patch)
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        self._po(sys, "stdin", stdin)
        self.mocks_deliver = self._po(hc, "deliver_variant_to_drive_idempotent",
                                      return_value={"state": "DELIVERY_COMPLETED",
                                                    "drive_file_id": "drive-file-1"})

    def _record(self):
        rows = load_managed_sessions(path=self.state_path)
        return rows[0] if rows else None

    def _manifest(self):
        return rm.load_run_manifest(Path(self.tmp.name), RUN_ID)

    def _events_named(self, name):
        return [e for e in self.events if e.get("event") == name]


class RunVariantChainStopTest(_ChainHarness):
    """Full-chain P0-A scenarios through the real run_variant()."""

    def test_stop_persists_active_record_stays_watchdog_lives_no_delivery(self):
        # Stop CLI exits 0 every time, but the session never goes away.
        self.query_states = [SessionState.ACTIVE] * 10
        with self.assertRaisesRegex(RuntimeError, "Stop not verified"):
            hc.run_variant(_args())
        rec = self._record()
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "active")
        # The confirm (+1) and deadline update (+1) happened; the failed
        # stop marked nothing.
        self.assertEqual(rec["version"], 3)
        self.assertEqual(rec["run_id"], RUN_ID)
        # The watchdog was NOT terminated: it keeps deadline protection.
        self.proc.terminate.assert_not_called()
        self.assertEqual(len(self._events_named("watchdog_left_running")), 1)
        finished = self._events_named("finished")
        self.assertEqual(len(finished), 1)
        self.assertFalse(finished[0]["stop_verified"])
        self.assertEqual(finished[0]["stop_state"], "ACTIVE")
        # No Drive delivery was attempted on an unconfirmed stop.
        self.mocks_deliver.assert_not_called()
        manifest = self._manifest()
        self.assertEqual(manifest["task_state"], "FAILED")
        self.assertEqual(manifest["session_state"], "UNKNOWN")

    def test_stop_success_marks_record_and_terminates_watchdog(self):
        self.query_states = [SessionState.ACTIVE, SessionState.STOPPED]
        result = hc.run_variant(_args())
        rec = self._record()
        self.assertEqual(rec["status"], "stopped")
        self.assertEqual(rec["version"], 4)  # record + confirm + deadline update + mark
        self.proc.terminate.assert_called_once()
        self.assertEqual(self._events_named("watchdog_left_running"), [])
        self.assertEqual(result["delivery"]["state"], "DELIVERY_COMPLETED")
        manifest = self._manifest()
        self.assertEqual(manifest["task_state"], "TASK_COMPLETED")
        self.assertEqual(manifest["session_state"], "STOPPED")

    def test_controller_crash_during_stop_leaves_watchdog_and_record(self):
        # Simulated crash: the stop call itself dies (KeyboardInterrupt
        # stands in for a process kill — a real SIGKILL cannot be raised
        # in-process). The watchdog must stay alive and the record must
        # stay active; nothing may claim the GPU is stopped.
        self._po(hc, "stop_and_verify_session",
                 side_effect=KeyboardInterrupt("simulated crash during stop"))
        with self.assertRaises(KeyboardInterrupt):
            hc.run_variant(_args())
        rec = self._record()
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "active")
        self.proc.terminate.assert_not_called()
        self.assertEqual(self._events_named("finished"), [])
        manifest = self._manifest()
        self.assertNotEqual(manifest["task_state"], "GPU_STOP_CONFIRMED")
        self.assertNotEqual(manifest.get("session_state"), "STOPPED")

    def test_new_task_blocked_while_old_gpu_state_unknown(self):
        # An old managed record whose live state cannot be confirmed must
        # block a new allocation — before any GPU is minted.
        record_session("muse-h3-old", "G4", "run-old", _deadline(),
                       path=self.state_path, production_task_id=TASK)
        self.query_states = [SessionState.UNKNOWN]
        allocate = mock.Mock(return_value=SESSION)
        self._po(hc, "allocate_managed_session", allocate)
        with self.assertRaises(SessionQueryError):
            hc.run_variant(_args())
        allocate.assert_not_called()


class WatchdogKilledAndRecoveredTest(unittest.TestCase):
    """A watchdog that dies unexpectedly must not corrupt the record, and
    a new watchdog must pick the same session up and finish the stop.

    Simulation honesty: a real SIGKILL cannot be raised in-process, so
    the 'kill' here is a KeyboardInterrupt injected into the watch loop
    of a thread — the observable behaviour under test (record intact
    after the death, new watch completes the stop) is the same.
    """

    def test_killed_watchdog_record_intact_new_watch_completes_stop(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", _deadline(hours=1), path=state,
                           production_task_id=TASK)
            before = load_managed_sessions(path=state)[0]
            real_load = hwd.load_managed_sessions
            real_mark = hwd.mark_session_stopped
            ledger = Path(d) / "ledger.json"
            states = [SessionState.ACTIVE, KeyboardInterrupt("simulated SIGKILL")]
            errors = []

            def _query(sid, colab_auth=None):
                nxt = states.pop(0) if states else SessionState.ACTIVE
                if isinstance(nxt, BaseException):
                    raise nxt
                return nxt

            def run_watch():
                try:
                    watch(SID, production_task_id=TASK, poll_sec=0.05)
                except KeyboardInterrupt:
                    pass
                except Exception as e:  # noqa: BLE001 - collected, asserted below
                    errors.append(e)

            with mock.patch.object(hwd, "load_managed_sessions",
                                   side_effect=lambda path=None: real_load(path=state)), \
                    mock.patch.object(hwd, "mark_session_stopped",
                                      side_effect=lambda sid, cs, path=None: real_mark(sid, cs, path=state)), \
                    mock.patch.object(hwd, "LEDGER_PATH", ledger), \
                    mock.patch.object(hwd, "query_session_state",
                                      side_effect=_query), \
                    mock.patch("h3_budget.read_live_usage",
                               return_value={"balance": 160.0, "rate": 8.88}), \
                    mock.patch("auth_support.resolve_colab_executable",
                               return_value="/usr/bin/colab"):
                t = threading.Thread(target=run_watch)
                t.start()
                t.join(timeout=10)
                self.assertFalse(t.is_alive())
                self.assertEqual(errors, [])
                # The death changed nothing in the record.
                after = load_managed_sessions(path=state)[0]
                self.assertEqual(after, before)
                # A new watchdog recovers: deadline now past, stop confirms.
                update_session_deadline(SID, _deadline(hours=-1), path=state)
                with mock.patch.object(hwd, "query_session_state",
                                       return_value=SessionState.ACTIVE), \
                        mock.patch.object(hwd, "stop_managed_session",
                                          return_value={"session": SID, "state": "STOPPED",
                                                        "stop_cli_ok": True, "attempts": 1,
                                                        "queries": 1, "error": None}):
                    rc = watch(SID, production_task_id=TASK, poll_sec=0.05)
                self.assertEqual(rc, 0)
                final = load_managed_sessions(path=state)[0]
                self.assertEqual(final["status"], "stopped")
                self.assertGreater(final["version"], before["version"])


class ContradictionTest(unittest.TestCase):
    """Managed-record vs live-state contradictions fail closed; stale
    active records are repaired from the live query."""

    def test_stopped_record_but_live_active_raises_and_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", _deadline(), path=state,
                           production_task_id=TASK)
            mark_session_stopped(SID, SessionState.STOPPED, path=state)
            with mock.patch.object(hwd, "query_session_state",
                                   return_value=SessionState.ACTIVE), \
                    mock.patch("h3_budget.read_live_usage",
                               return_value={"balance": 160.0, "rate": 8.88}):
                with self.assertRaisesRegex(SessionQueryError, "still ACTIVE"):
                    reconcile_on_startup("adc", path=state)

    def test_active_record_but_live_stopped_is_repaired_not_blocking(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            record_session(SID, "G4", "run-1", _deadline(), path=state,
                           production_task_id=TASK)
            with mock.patch.object(hwd, "query_session_state",
                                   return_value=SessionState.STOPPED), \
                    mock.patch("h3_budget.read_live_usage",
                               return_value={"balance": 160.0, "rate": 8.88}):
                out = reconcile_on_startup("adc", path=state)
            self.assertEqual(out["managed_active_sessions"], [])
            self.assertEqual(out["repaired_stale_active"], [SID])
            stored = load_managed_sessions(path=state)[0]
            self.assertEqual(stored["status"], "stopped")
            self.assertEqual(stored["version"], 2)


class OldFormatCompatTest(unittest.TestCase):
    """Old state files (no version field) and old manifests stay readable.

    PR-1 adds no manifest fields, so 'old' here is the pre-PR-1 writer
    output; the test guards against an accidental schema break.
    """

    def test_old_state_file_without_version_loads_and_upgrades(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            state.write_text(json.dumps({"sessions": [{
                "session_id": SID, "gpu": "G4", "run_id": "run-1",
                "production_task_id": TASK,
                "started_at_utc": "2026-11-12T00:00:00+00:00",
                "deadline_utc": _deadline(), "status": "active"}]}))
            rec = update_session_deadline(SID, _deadline(hours=2), path=state)
            self.assertEqual(rec["version"], 1)
            self.assertEqual(rec["gpu"], "G4")
            self.assertTrue(mark_session_stopped(SID, SessionState.STOPPED, path=state))
            stored = load_managed_sessions(path=state)[0]
            self.assertEqual(stored["version"], 2)
            self.assertEqual(stored["status"], "stopped")
            self.assertEqual(stored["run_id"], "run-1")

    def test_old_manifest_reads_and_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rm.create_run_manifest(root, TASK, RUN_ID, variant="A")
            rm.update_run(root, RUN_ID, task_state="GPU_ALLOCATED",
                          session_id=SESSION, session_state="ACTIVE")
            view = rm.recovery_view(root, RUN_ID)
            self.assertEqual(view["manifest"]["task_state"], "GPU_ALLOCATED")
            self.assertEqual(view["manifest"]["session_id"], SESSION)
            resumed = rm.resume_run(root, RUN_ID)
            self.assertEqual(resumed["manifest"]["recovery_generation"], 1)
            self.assertEqual(resumed["manifest"]["task_state"], "GPU_ALLOCATED")


if __name__ == "__main__":
    unittest.main()
