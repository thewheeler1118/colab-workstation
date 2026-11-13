"""TASK-005 §4.2/§4.3 (Issue #27): recoverable control plane.

No GPU, no live CLI, no real Drive: every external boundary is a mock,
but the functions under test are the real ones — launch/poll parsing,
the poll loop, the resume decision tree, stop, and reconcile.

Nothing here asserts stage order via inspect.getsource: the run_variant
wiring test in test_drive_delivery.py mock-executes the real function,
and the resume tests below mock-execute the real _resume_run.
"""
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_controller as hc  # noqa: E402
import h3_job_state as hjs  # noqa: E402
import h3_remote_worker as hrw  # noqa: E402
import h3_run_manifest as rm  # noqa: E402
from h3_session_watchdog import SessionQueryError, SessionState  # noqa: E402

RUN_ID = "h3-20261113T000000Z-resume01"
TASK = "h3-ab-20261111"
SESSION = "muse-h3-resume01"
DONE = {"file": "/content/muse_h3/output/shao_nian_h3_A.mp4", "frames": 274,
        "seconds": 274 / 24, "sha256": "a" * 64, "with_audio": True}


def _state(run_id=RUN_ID, variant="A", stage="VIDEO_READY", output=None, error=None):
    s = {"run_id": run_id, "variant": variant, "stage": stage,
         "updated_at_utc": "2026-11-13T00:00:00Z", "heartbeat_utc": "2026-11-13T00:00:00Z",
         "pid": 123}
    if output is not None:
        s["output"] = output
    if error is not None:
        s["error"] = error
    return s


def _video_info(sha):
    return {"sha256": sha, "frame_count": 274, "width": 1080,
            "height": 1920, "fps": 24, "path": "A.mp4", "size": 1234}


class JobStateTest(unittest.TestCase):
    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            written = hjs.write_job_state(root, RUN_ID, "A", "MODELS_READY", pid=42)
            read = hjs.read_job_state(root)
            self.assertEqual(read, written)
            self.assertEqual(read["stage"], "MODELS_READY")
            self.assertEqual(read["pid"], 42)
            self.assertEqual((root / "MUSE_H3_JOB_STATE.json").stat().st_mode & 0o777, 0o600)

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                hjs.read_job_state(Path(d))

    def test_corrupt_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "MUSE_H3_JOB_STATE.json").write_text("{not json")
            with self.assertRaises(ValueError):
                hjs.read_job_state(root)

    def test_missing_field_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "MUSE_H3_JOB_STATE.json").write_text(json.dumps({"run_id": RUN_ID}))
            with self.assertRaises(ValueError):
                hjs.read_job_state(root)

    def test_phase_mapping_covers_every_worker_phase(self):
        # Every set_stage() name in the worker maps onto a job stage; an
        # unmapped phase must not crash the worker's bookkeeping.
        for phase in ("ensure_comfy", "restore_weights", "comfyui_start",
                      "object_info_validation", "inference_submit",
                      "find_rendered_video", "rescue_rendered_video",
                      "assemble_mp4", "done"):
            self.assertIn(hjs.WORKER_PHASE_TO_JOB_STAGE[phase], hjs.JOB_STAGES)


class WorkerStateFileTest(unittest.TestCase):
    def test_set_stage_writes_durable_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(hrw, "ROOT", root):
                hrw._JOB_RUN_ID = RUN_ID
                hrw._JOB_VARIANT = "A"
                try:
                    hrw.set_stage("restore_weights")
                    self.assertEqual(hjs.read_job_state(root)["stage"], "RESTORING_MODELS")
                    hrw.set_stage("inference_submit")
                    self.assertEqual(hjs.read_job_state(root)["stage"], "INFERENCE_RUNNING")
                    hrw.set_stage("done")
                    self.assertEqual(hjs.read_job_state(root)["stage"], "VIDEO_READY")
                finally:
                    hrw._JOB_RUN_ID = ""
                    hrw._JOB_VARIANT = ""

    def test_set_stage_never_raises_when_state_unwritable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(hrw, "ROOT", root):
                hrw._JOB_RUN_ID = RUN_ID
                hrw._JOB_VARIANT = "A"
                try:
                    # Make the state file a directory: the write fails.
                    (root / "MUSE_H3_JOB_STATE.json").mkdir()
                    hrw.set_stage("restore_weights")  # must not raise
                finally:
                    hrw._JOB_RUN_ID = ""
                    hrw._JOB_VARIANT = ""


class LaunchPollTest(unittest.TestCase):
    def test_launch_parses_launch_json(self):
        with mock.patch.object(hc, "exec_script", return_value={
                "stdout": "MUSE_H3_LAUNCH_JSON:" + json.dumps(
                    {"run_id": RUN_ID, "variant": "A", "pid": 7,
                     "state_file": "/content/muse_h3/MUSE_H3_JOB_STATE.json",
                     "log_file": "/content/muse_h3/worker.log"}),
                "stderr": "", "returncode": 0}):
            launch = hc.launch_worker(SESSION)
        self.assertEqual(launch["pid"], 7)
        self.assertEqual(launch["run_id"], RUN_ID)

    def test_launch_without_marker_raises(self):
        with mock.patch.object(hc, "exec_script", return_value={
                "stdout": "something else\n", "stderr": "", "returncode": 0}):
            with self.assertRaisesRegex(RuntimeError, "MUSE_H3_LAUNCH_JSON"):
                hc.launch_worker(SESSION)

    def test_poll_parses_state_json(self):
        with mock.patch.object(hc, "exec_script", return_value={
                "stdout": "MUSE_H3_JOB_STATE_JSON:" + json.dumps(_state(stage="MODELS_READY")),
                "stderr": "", "returncode": 0}):
            self.assertEqual(hc.poll_worker(SESSION)["stage"], "MODELS_READY")

    def test_poll_missing_is_worker_poll_error(self):
        with mock.patch.object(hc, "exec_script", return_value={
                "stdout": "MUSE_H3_JOB_STATE_MISSING\n", "stderr": "", "returncode": 2}):
            with self.assertRaises(hc.WorkerPollError):
                hc.poll_worker(SESSION)

    def test_poll_corrupt_is_worker_poll_error(self):
        with mock.patch.object(hc, "exec_script", return_value={
                "stdout": "MUSE_H3_JOB_STATE_CORRUPT: boom\n", "stderr": "", "returncode": 3}):
            with self.assertRaises(hc.WorkerPollError):
                hc.poll_worker(SESSION)

    def test_poll_exec_failure_is_worker_poll_error(self):
        with mock.patch.object(hc, "exec_script", side_effect=RuntimeError("ReadTimeout")):
            with self.assertRaises(hc.WorkerPollError):
                hc.poll_worker(SESSION)

    def test_poll_incomplete_record_is_worker_poll_error(self):
        with mock.patch.object(hc, "exec_script", return_value={
                "stdout": "MUSE_H3_JOB_STATE_JSON:" + json.dumps({"run_id": RUN_ID}),
                "stderr": "", "returncode": 0}):
            with self.assertRaises(hc.WorkerPollError):
                hc.poll_worker(SESSION)


class WaitForWorkerTest(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(hc.time, "sleep", lambda s: None)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_poll_sequence_reaches_video_ready(self):
        seen = []
        states = [_state(stage=s) for s in
                  ("RESTORING_MODELS", "MODELS_READY", "INFERENCE_RUNNING",
                   "INFERENCE_COMPLETED", "VIDEO_READY")]
        states[-1]["output"] = DONE
        with mock.patch.object(hc, "poll_worker", side_effect=states):
            final = hc.wait_for_worker(SESSION, RUN_ID, "A",
                                       deadline_monotonic=1e9,
                                       on_state=lambda s: seen.append(s["stage"]))
        self.assertEqual(final["stage"], "VIDEO_READY")
        self.assertEqual(final["output"], DONE)
        self.assertEqual(seen, ["RESTORING_MODELS", "MODELS_READY",
                                "INFERENCE_RUNNING", "INFERENCE_COMPLETED",
                                "VIDEO_READY"])

    def test_worker_failed_raises_with_stage_and_error(self):
        failed = _state(stage="FAILED", error={"stage": "inference_submit",
                                               "error_type": "RuntimeError",
                                               "error": "/prompt rejected"})
        with mock.patch.object(hc, "poll_worker", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "inference_submit.*prompt rejected"):
                hc.wait_for_worker(SESSION, RUN_ID, "A", deadline_monotonic=1e9)

    def test_three_consecutive_poll_failures_raise(self):
        with mock.patch.object(hc, "poll_worker",
                               side_effect=hc.WorkerPollError("probe exec failed")):
            with self.assertRaisesRegex(RuntimeError, "unobservable after 3 consecutive"):
                hc.wait_for_worker(SESSION, RUN_ID, "A", deadline_monotonic=1e9)

    def test_transient_poll_failure_recovers(self):
        states = [_state(stage="VIDEO_READY", output=DONE)]
        side = [hc.WorkerPollError("timeout"), hc.WorkerPollError("timeout")] + states
        with mock.patch.object(hc, "poll_worker", side_effect=side):
            final = hc.wait_for_worker(SESSION, RUN_ID, "A", deadline_monotonic=1e9)
        self.assertEqual(final["stage"], "VIDEO_READY")

    def test_state_for_another_run_raises(self):
        other = _state(run_id="h3-other", stage="VIDEO_READY", output=DONE)
        with mock.patch.object(hc, "poll_worker", return_value=other):
            with self.assertRaisesRegex(RuntimeError, "belongs to another run"):
                hc.wait_for_worker(SESSION, RUN_ID, "A", deadline_monotonic=1e9)

    def test_unknown_stage_raises(self):
        weird = _state(stage="SOME_NEW_STAGE")
        with mock.patch.object(hc, "poll_worker", return_value=weird):
            with self.assertRaisesRegex(RuntimeError, "Unknown worker job stage"):
                hc.wait_for_worker(SESSION, RUN_ID, "A", deadline_monotonic=1e9)

    def test_deadline_exceeded_raises_timeout(self):
        with mock.patch.object(hc.time, "monotonic", return_value=1e9 + 1):
            with mock.patch.object(hc, "poll_worker") as poll:
                with self.assertRaises(TimeoutError):
                    hc.wait_for_worker(SESSION, RUN_ID, "A", deadline_monotonic=1e9)
                poll.assert_not_called()


class CheckJobDoneInfoTest(unittest.TestCase):
    def test_valid_done_info_passes(self):
        self.assertEqual(hc.check_job_done_info(DONE, "A"), DONE)

    def test_wrong_frames_raises(self):
        bad = dict(DONE, frames=100)
        with self.assertRaisesRegex(RuntimeError, "frames=100"):
            hc.check_job_done_info(bad, "A")

    def test_missing_file_or_sha_raises(self):
        with self.assertRaisesRegex(RuntimeError, "missing file/sha256"):
            hc.check_job_done_info({"frames": 274}, "A")

    def test_non_dict_raises(self):
        with self.assertRaisesRegex(RuntimeError, "not an object"):
            hc.check_job_done_info("done", "A")


class _ResumeBase(unittest.TestCase):
    """Shared harness: temp ROOT with a manifest + ledger, all live
    boundaries mocked. Tests call hc._resume_run / _stop_run /
    _reconcile_task directly, or hc.test_or_run for dispatch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "model_cache").mkdir()
        self.out_root = self.root / "output"
        self.out_root.mkdir()
        self.sha = hashlib.sha256(b"fake-mp4").hexdigest()
        self.patches = []
        self._po(hc, "ROOT", self.root)
        self._po(hc, "OUTPUT_ROOT", self.out_root)
        self._po(hc, "RUNS", self.root / "runs")
        self._po(hc, "record", lambda data: None)
        self._po(hc.time, "sleep", lambda s: None)
        # TASK-005 §6: resume drives the same CLI, so the environment
        # gate runs first; the mock lets the decision-tree tests proceed.
        self._po(hc, "verify_environment", return_value={"ok": True, "checked": []})
        # git HEAD matches the manifest's code_commit by default.
        self._git("abc1234")

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

    def _git(self, sha):
        import subprocess

        proc = mock.Mock()
        proc.stdout = sha + "\n" if sha else ""
        self._po(subprocess, "run", return_value=proc)

    def _manifest(self, task_state="GPU_ALLOCATED", session_id=SESSION,
                  session_state="ACTIVE", variant="A", code_commit="abc1234",
                  artifacts=None):
        m = rm.create_run_manifest(self.root, TASK, RUN_ID, variant=variant,
                                   code_commit=code_commit, budget={})
        if task_state != "TASK_AUTHORIZED":
            rm.update_run(self.root, RUN_ID, task_state=task_state,
                          session_id=session_id, session_state=session_state,
                          artifacts=artifacts or {})
        elif session_id:
            rm.update_run(self.root, RUN_ID, session_id=session_id,
                          session_state=session_state,
                          artifacts=artifacts or {})
        return rm.load_run_manifest(self.root, RUN_ID)

    def _ledger(self):
        from h3_budget import ledger_path_for_task

        path = ledger_path_for_task(self.root, TASK)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "production_task_id": TASK, "project_budget_cu": 30.0,
            "hard_stop_balance_cu": 132.77, "max_observed_spent_cu": 0.0}))
        return path

    def _args(self, **over):
        kw = {"action": "resume", "run_id": RUN_ID, "production_task_id": TASK,
              "max_wall_sec": 14400, "poll_interval_sec": 60}
        kw.update(over)
        return SimpleNamespace(**kw)

    def _local_mp4(self):
        mp4 = self.out_root / RUN_ID / "A.mp4"
        mp4.parent.mkdir(parents=True, exist_ok=True)
        # Bytes must hash to self.sha: the idempotent deliver re-proves
        # the local file against video_info (TASK-005 §5).
        mp4.write_bytes(b"fake-mp4")
        return mp4


class ResumeTerminalTest(_ResumeBase):
    def test_completed_is_a_noop(self):
        # TASK-006 P1-A: a completed run has a confirmed STOPPED session;
        # the fixture must model that, or the TASK_COMPLETED move itself
        # is refused by the new gate.
        self._manifest(task_state="DRIVE_VERIFIED", session_state="STOPPED")
        rm.update_run(self.root, RUN_ID, task_state="TASK_COMPLETED")
        result = hc._resume_run(self._args())
        self.assertFalse(result["resumed"])
        self.assertEqual(result["reason"], "already completed")

    def test_failed_is_a_noop(self):
        self._manifest(task_state="FAILED", session_state="UNKNOWN")
        result = hc._resume_run(self._args())
        self.assertFalse(result["resumed"])
        self.assertIn("explicit operator decision", result["reason"])

    def test_awaiting_archive_archives_only(self):
        # COMPLETED_AWAITING_ARCHIVE is only entered after the GPU stop
        # was confirmed, so the fixture's session is STOPPED.
        self._manifest(task_state="COMPLETED_AWAITING_ARCHIVE",
                       session_state="STOPPED")
        self._ledger()
        delivery = {"state": "DELIVERY_COMPLETED", "drive_file_id": "drive-file-9"}
        with mock.patch.object(hc, "_archive_local_mp4", return_value=delivery):
            result = hc._resume_run(self._args())
        self.assertTrue(result["resumed"])
        self.assertEqual(result["delivery"], delivery)
        self.assertEqual(rm.load_run_manifest(self.root, RUN_ID)["task_state"],
                         "TASK_COMPLETED")


class ResumeRefusalTest(_ResumeBase):
    def test_code_mismatch_refuses(self):
        self._manifest(task_state="GPU_ALLOCATED")
        self._ledger()
        self._git("different-sha")
        result = hc._resume_run(self._args())
        self.assertFalse(result["resumed"])
        self.assertIn("code mismatch", result["reason"])
        self.assertIn("abc1234", result["reason"])
        self.assertIn("different-sha", result["reason"])

    def test_missing_ledger_refuses_without_creating_one(self):
        self._manifest(task_state="GPU_ALLOCATED")
        result = hc._resume_run(self._args())
        self.assertFalse(result["resumed"])
        self.assertIn("no budget ledger", result["reason"])
        from h3_budget import ledger_path_for_task

        self.assertFalse(ledger_path_for_task(self.root, TASK).is_file())

    def test_no_session_id_refuses(self):
        self._manifest(task_state="GPU_ALLOCATED", session_id=None)
        self._ledger()
        result = hc._resume_run(self._args())
        self.assertFalse(result["resumed"])
        self.assertIn("no session id", result["reason"])

    def test_unknown_session_state_refuses(self):
        self._manifest(task_state="GPU_ALLOCATED")
        self._ledger()
        import h3_session_watchdog as hwd

        with mock.patch.object(hwd, "query_session_state",
                               side_effect=SessionQueryError("CLI exit 1")):
            result = hc._resume_run(self._args())
        self.assertFalse(result["resumed"])
        self.assertIn("UNKNOWN", result["reason"])

    def test_unobservable_worker_leaves_session_running(self):
        self._manifest(task_state="WORKER_STARTED")
        self._ledger()
        import h3_session_watchdog as hwd

        with mock.patch.object(hwd, "query_session_state",
                               return_value=SessionState.ACTIVE):
            with mock.patch.object(hc, "poll_worker",
                                   side_effect=hc.WorkerPollError("timeout")):
                with mock.patch.object(hc, "stop_and_verify_session") as stop:
                    result = hc._resume_run(self._args())
        self.assertFalse(result["resumed"])
        self.assertIn("unobservable", result["reason"])
        stop.assert_not_called()


class ResumeStoppedSessionTest(_ResumeBase):
    def _active_stopped(self):
        import h3_session_watchdog as hwd

        return mock.patch.object(hwd, "query_session_state",
                                 return_value=SessionState.STOPPED)

    def test_stopped_with_local_mp4_archives_it(self):
        self._manifest(task_state="VIDEO_VALIDATED")
        self._ledger()
        self._local_mp4()
        delivery = {"state": "DELIVERY_COMPLETED", "drive_file_id": "drive-file-2"}
        with self._active_stopped():
            with mock.patch.object(hc, "_archive_local_mp4", return_value=delivery):
                result = hc._resume_run(self._args())
        self.assertTrue(result["resumed"])
        self.assertEqual(result["delivery"], delivery)
        self.assertEqual(rm.load_run_manifest(self.root, RUN_ID)["task_state"],
                         "TASK_COMPLETED")

    def test_stopped_with_nothing_durable_fails(self):
        self._manifest(task_state="WORKER_STARTED")
        self._ledger()
        with self._active_stopped():
            result = hc._resume_run(self._args())
        self.assertFalse(result["resumed"])
        self.assertIn("nothing recoverable", result["reason"])
        self.assertEqual(rm.load_run_manifest(self.root, RUN_ID)["task_state"],
                         "FAILED")

    def test_stopped_after_remote_upload_started_marks_artifact_lost(self):
        # TASK-006 P1-A: the runtime is gone, so the remote temp disk is
        # gone with it. A worker upload was in flight and no durable
        # copy exists anywhere: the run FAILS with artifact_lost set,
        # and nothing is silently re-inferred.
        self._manifest(task_state="VIDEO_VALIDATED",
                       artifacts={"delivery_state": "REMOTE_UPLOAD_STARTED"})
        self._ledger()
        with self._active_stopped():
            result = hc._resume_run(self._args())
        self.assertFalse(result["resumed"])
        m = rm.load_run_manifest(self.root, RUN_ID)
        self.assertEqual(m["task_state"], "FAILED")
        self.assertTrue(m["artifacts"].get("artifact_lost"))


class ResumeLiveSessionTest(_ResumeBase):
    def _active(self, worker_state):
        import h3_session_watchdog as hwd

        patches = [
            mock.patch.object(hwd, "query_session_state",
                              return_value=SessionState.ACTIVE),
            mock.patch.object(hc, "poll_worker", return_value=worker_state),
        ]
        for p in patches:
            p.start()
            self.patches.append(p)

    def _finish_mocks(self):
        self._po(hc, "exec_script",
                 return_value={"stdout": "OUTPUT_CHECK_JSON:" + json.dumps(
                     {"ok": True, "exists": True, "frames": 274, "fps": 24,
                      "width": 1080, "height": 1920}), "stderr": "", "returncode": 0})
        self._po(hc, "check_remote_output", return_value={"ok": True})
        self._po(hc, "inspect",
                 return_value={"cu_balance": 145.0, "rate": 5.0, "sessions": []})
        self.stop_mock = self._po(hc, "stop_and_verify_session",
                                  return_value={"session": SESSION, "stopped_confirmed": True})
        # The mocked download writes the file it claims to have fetched:
        # the idempotent deliver re-proves the local file (TASK-005 §5).
        def _fake_download(session, variant, run_id):
            mp4 = self.out_root / run_id / f"{variant}.mp4"
            mp4.parent.mkdir(parents=True, exist_ok=True)
            mp4.write_bytes(b"fake-mp4")
            return _video_info(self.sha)

        self._po(hc, "download_and_validate_variant", side_effect=_fake_download)
        self._po(hc, "deliver_variant_to_drive",
                 return_value={"state": "DELIVERY_COMPLETED", "variant": "A",
                               "run_id": RUN_ID, "drive_file_id": "drive-file-3"})
        import h3_video_qa as hvq

        self._po(hvq, "validate_final_video",
                 return_value=_video_info(self.sha))

    def test_video_ready_finishes_the_run(self):
        self._manifest(task_state="WORKER_STARTED")
        self._ledger()
        self._active(_state(stage="VIDEO_READY", output=DONE))
        self._finish_mocks()
        result = hc._resume_run(self._args())
        self.assertEqual(result["delivery"]["drive_file_id"], "drive-file-3")
        self.assertTrue(result["resumed"])
        manifest = rm.load_run_manifest(self.root, RUN_ID)
        self.assertEqual(manifest["task_state"], "TASK_COMPLETED")
        self.assertEqual(manifest["session_state"], "STOPPED")

    def test_video_ready_reuses_validated_local_mp4(self):
        self._manifest(task_state="ARTIFACT_DURABLE")
        self._ledger()
        self._local_mp4()
        self._active(_state(stage="VIDEO_READY", output=DONE))
        self._finish_mocks()
        with mock.patch.object(hc, "download_and_validate_variant") as dl:
            result = hc._resume_run(self._args())
        dl.assert_not_called()
        self.assertEqual(result["delivery"]["drive_file_id"], "drive-file-3")

    def test_worker_failed_stops_session_and_marks_failed(self):
        self._manifest(task_state="WORKER_STARTED")
        self._ledger()
        failed = _state(stage="FAILED", error={"stage": "restore_weights",
                                               "error_type": "RuntimeError",
                                               "error": "disk full"})
        self._active(failed)
        stop = self._po(hc, "stop_and_verify_session",
                        return_value={"session": SESSION, "stopped_confirmed": True})
        result = hc._resume_run(self._args())
        self.assertTrue(result["resumed"])
        self.assertIn("worker failed", result["outcome"])
        stop.assert_called_once_with(SESSION)
        manifest = rm.load_run_manifest(self.root, RUN_ID)
        self.assertEqual(manifest["task_state"], "FAILED")
        self.assertEqual(manifest["session_state"], "STOPPED")

    def test_still_running_reenters_poll_loop_then_finishes(self):
        self._manifest(task_state="WORKER_STARTED")
        self._ledger()
        self._active(_state(stage="INFERENCE_RUNNING"))
        self._po(hc, "allowed_runtime_seconds", return_value=3600)
        self._po(hc, "wait_for_worker",
                 return_value=_state(stage="VIDEO_READY", output=DONE))
        self._finish_mocks()
        result = hc._resume_run(self._args())
        self.assertEqual(result["delivery"]["drive_file_id"], "drive-file-3")
        self.assertEqual(rm.load_run_manifest(self.root, RUN_ID)["task_state"],
                         "TASK_COMPLETED")

    def test_stop_not_verified_in_finish_fails(self):
        self._manifest(task_state="WORKER_STARTED")
        self._ledger()
        self._active(_state(stage="VIDEO_READY", output=DONE))
        self._finish_mocks()
        # Override the stop mock: not verified.
        self.stop_mock.return_value = {"session": SESSION, "stopped_confirmed": False}
        with self.assertRaisesRegex(RuntimeError, "Stop not verified"):
            hc._resume_run(self._args())
        self.assertEqual(rm.load_run_manifest(self.root, RUN_ID)["task_state"],
                         "FAILED")


class StopActionTest(_ResumeBase):
    def test_stop_managed_session_updates_manifest(self):
        self._manifest(task_state="GPU_ALLOCATED")
        import h3_session_watchdog as hwd

        with mock.patch.object(hwd, "stop_managed_session",
                               return_value={"session": SESSION, "state": "STOPPED",
                                             "stop_cli_ok": True, "attempts": 1,
                                             "queries": 1, "error": None}) as stop:
            result = hc._stop_run(self._args(action="stop"))
        stop.assert_called_once_with(SESSION, colab_auth="adc")
        self.assertEqual(result["state"], "STOPPED")
        self.assertEqual(rm.load_run_manifest(self.root, RUN_ID)["session_state"],
                         "STOPPED")

    def test_stop_refused_for_non_managed_leaves_manifest(self):
        self._manifest(task_state="GPU_ALLOCATED")
        import h3_session_watchdog as hwd

        with mock.patch.object(hwd, "stop_managed_session",
                               return_value={"session": SESSION,
                                             "state": "REFUSED_NOT_MANAGED",
                                             "stop_cli_ok": None, "attempts": 0,
                                             "queries": 0,
                                             "error": "not managed"}):
            result = hc._stop_run(self._args(action="stop"))
        self.assertEqual(result["state"], "REFUSED_NOT_MANAGED")
        self.assertEqual(rm.load_run_manifest(self.root, RUN_ID)["session_state"],
                         "ACTIVE")

    def test_stop_without_session_raises(self):
        self._manifest(task_state="TASK_AUTHORIZED", session_id=None)
        with self.assertRaisesRegex(ValueError, "no session id"):
            hc._stop_run(self._args(action="stop"))


class ReconcileActionTest(_ResumeBase):
    def test_active_manifest_corrected_to_stopped(self):
        self._manifest(task_state="GPU_ALLOCATED")
        other = "h3-20261113T000000Z-resume02"
        rm.create_run_manifest(self.root, TASK, other, variant="B",
                               code_commit="abc1234", budget={})
        rm.update_run(self.root, other, task_state="GPU_ALLOCATED",
                      session_id="muse-h3-other", session_state="ACTIVE")
        import h3_session_watchdog as hwd

        def query(session_id, colab_auth="adc"):
            if session_id == SESSION:
                return SessionState.STOPPED
            return SessionState.ACTIVE

        with mock.patch.object(hwd, "query_session_state", side_effect=query):
            report = hc._reconcile_task(self._args(action="reconcile"))
        self.assertEqual(report["corrections"], 1)
        self.assertEqual(report["unknown"], [])
        self.assertEqual(rm.load_run_manifest(self.root, RUN_ID)["session_state"],
                         "STOPPED")
        self.assertEqual(rm.load_run_manifest(self.root, other)["session_state"],
                         "ACTIVE")

    def test_unknown_session_left_alone_and_reported(self):
        self._manifest(task_state="GPU_ALLOCATED")
        import h3_session_watchdog as hwd

        with mock.patch.object(hwd, "query_session_state",
                               side_effect=SessionQueryError("CLI exit 1")):
            report = hc._reconcile_task(self._args(action="reconcile"))
        self.assertEqual(report["corrections"], 0)
        self.assertEqual(report["unknown"], [RUN_ID])
        self.assertEqual(rm.load_run_manifest(self.root, RUN_ID)["session_state"],
                         "ACTIVE")

    def test_reconcile_ignores_other_tasks(self):
        self._manifest(task_state="GPU_ALLOCATED")
        other = "h3-20261113T000000Z-resume02"
        rm.create_run_manifest(self.root, "h3-other-task", other, variant="B",
                               code_commit="abc1234", budget={})
        import h3_session_watchdog as hwd

        with mock.patch.object(hwd, "query_session_state",
                               return_value=SessionState.STOPPED):
            report = hc._reconcile_task(self._args(action="reconcile"))
        self.assertEqual(len(report["runs"]), 1)
        self.assertEqual(report["runs"][0]["run_id"], RUN_ID)


class DispatchTest(_ResumeBase):
    def test_status_resume_stop_reconcile_dispatch(self):
        self._manifest(task_state="TASK_AUTHORIZED")
        buf = io.StringIO()
        with redirect_stdout(buf):
            hc.test_or_run(self._args(action="status"))
        view = json.loads(buf.getvalue())
        self.assertEqual(view["current_stage"], "TASK_AUTHORIZED")
        self.assertTrue(view["resumable"])

        import h3_session_watchdog as hwd

        with mock.patch.object(hwd, "stop_managed_session",
                               return_value={"session": SESSION, "state": "ALREADY_STOPPED",
                                             "stop_cli_ok": None, "attempts": 0,
                                             "queries": 0, "error": None}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                hc.test_or_run(self._args(action="stop"))
        self.assertEqual(json.loads(buf.getvalue())["state"], "ALREADY_STOPPED")


if __name__ == "__main__":
    unittest.main()
