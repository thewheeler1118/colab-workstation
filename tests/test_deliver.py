"""TASK-008 (Issue #57): task-level automatic recovery loop (`deliver`).

The loop reuses a delivered run, recovers an archive-only run,
resumes an in-flight run, and only behind the session / budget /
deadline gates starts a controlled new attempt. No GPU, no CU, no
Drive — the live layer (inspect, run_variant, resume, Drive verify)
is stubbed; manifests and the ledger are real files on a temp ROOT.
"""
import argparse
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import h3_controller as hc
import h3_run_manifest as rm
from h3_session_watchdog import SessionState

TASK = "h3-t008-deliver"
SHA = "ab" * 32


def _fmt(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spec_file(root, task=TASK):
    spec = {
        "task_id": task, "output_prefix": "A",
        "ref_image_1": {"sha256": SHA, "local_path": "assets/original_poster.jpg"},
        "prompt": "test prompt", "seed": 42,
        "width": 736, "height": 1312, "fps": 24,
        "animation_frames": 226, "tail_card_frames": 0, "steps": 25,
        "output_width": 1080, "output_height": 1920,
    }
    path = root / "spec.json"
    path.write_text(json.dumps(spec))
    return str(path)


def _ledger(root, task=TASK, spent=18.58, deadline=None):
    d = root / "model_cache"
    d.mkdir(parents=True, exist_ok=True)
    doc = {"production_task_id": task, "project_budget_cu": 30.0,
           "hard_stop_balance_cu": 132.77, "max_observed_spent_cu": spent,
           "prior_task_unbilled_reserve_cu": 0.14, "run_ids": [],
           "authorized": True}
    if deadline is not None:
        doc["deadline_utc"] = deadline
    (d / f"h3_production_budget_{task}.json").write_text(json.dumps(doc))


def _manifest(root, run_id, state, session_id=None, session_state=None,
              task=TASK):
    """Create a manifest and walk it to `state` through legal stages."""
    rm.create_run_manifest(root, task, run_id, variant="A", budget={})
    if state in ("GPU_ALLOCATED", "WORKER_STARTED", "MODELS_READY",
                 "VIDEO_VALIDATED", "GPU_STOP_CONFIRMED", "DRIVE_VERIFIED",
                 "TASK_COMPLETED", "FAILED", "COMPLETED_AWAITING_ARCHIVE"):
        rm.update_run(root, run_id, session_id=session_id or "muse-h3-test",
                      session_state=session_state or "ACTIVE",
                      task_state="GPU_ALLOCATED")
    chain = {"WORKER_STARTED": ["WORKER_STARTED"],
             "MODELS_READY": ["WORKER_STARTED", "MODELS_READY"],
             "VIDEO_VALIDATED": ["WORKER_STARTED", "MODELS_READY",
                                 "VIDEO_VALIDATED"],
             "GPU_STOP_CONFIRMED": ["WORKER_STARTED", "MODELS_READY",
                                    "VIDEO_VALIDATED", "GPU_STOP_CONFIRMED"],
             "DRIVE_VERIFIED": ["WORKER_STARTED", "MODELS_READY",
                                "VIDEO_VALIDATED", "GPU_STOP_CONFIRMED",
                                "DRIVE_VERIFIED"],
             "TASK_COMPLETED": ["WORKER_STARTED", "MODELS_READY",
                                "VIDEO_VALIDATED", "GPU_STOP_CONFIRMED",
                                "DRIVE_VERIFIED", "TASK_COMPLETED"],
             "FAILED": [],
             "COMPLETED_AWAITING_ARCHIVE": []}
    for stage in chain.get(state, []):
        kw = {}
        if stage == "GPU_STOP_CONFIRMED":
            kw["session_state"] = "STOPPED"
        rm.update_run(root, run_id, task_state=stage, **kw)
    if state == "FAILED":
        rm.update_run(root, run_id, task_state="FAILED",
                      session_state=session_state or "STOPPED",
                      last_error="RuntimeError: boom")
    if state == "COMPLETED_AWAITING_ARCHIVE":
        rm.update_run(root, run_id, task_state="COMPLETED_AWAITING_ARCHIVE")


class DeliverTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        for sub in ("runs", "output", "logs", "model_cache"):
            (self.root / sub).mkdir(parents=True)
        self.spec_path = _spec_file(self.root)
        self._patches = [
            mock.patch.object(hc, "ROOT", self.root),
            mock.patch.object(hc, "OUTPUT_ROOT", self.root / "output"),
            mock.patch.object(hc, "RUNS", self.root / "runs"),
            mock.patch.object(hc.time, "sleep", lambda s: None),
        ]
        for p in self._patches:
            p.start()
        self.inspect = mock.patch.object(
            hc, "inspect",
            return_value={"cu_balance": 144.19, "rate": 12.0,
                          "sessions": []}).start()
        self.run_variant = mock.patch.object(hc, "_run_variant_inner").start()
        self.resume_run = mock.patch.object(hc, "_resume_run_inner").start()
        self.verify = mock.patch.object(hc, "_verify_completed_delivery").start()
        self.wait_stopped = mock.patch.object(
            hc, "_wait_for_session_stopped",
            return_value=SessionState.STOPPED).start()
        # One cleanup for every patch started above (stopall also covers
        # the per-test patches started inside the test bodies).
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.td.cleanup)

    def _args(self, **over):
        args = argparse.Namespace(
            action="deliver", production_task_id=TASK,
            task_spec=self.spec_path, variant=None, run_id="",
            max_task_attempts=2, task_deadline_utc="",
            execute=True, confirm_gpu=True, authorize_task_gpu=True,
            allow_g4=True, allow_a100_highmem=True, fresh_inference=True,
            project_budget_cu=30.0, per_session_budget_cu=25.0,
            authorize_new_task=True, prior_unbilled_reserve_cu=0.14,
            max_wall_sec=7200, poll_interval_sec=60,
            gpu_policy="G4_THEN_A100_HIGHMEM", comfy_ref="c" * 40,
            workflow_api=str(self.root / "workflow_api.json"),
            cache_manifest=str(self.root / "model_cache" / "verified_manifest.json"),
            drive_source="download",
            drive_cache_dir="/content/drive/MyDrive/Muse-H3-Model-Depot",
            colab_auth="adc", budget_cu=0.0, max_rate_cu_h=14.0,
            approve_interactive_drive_mount=False, auto_recover=False)
        for k, v in over.items():
            setattr(args, k, v)
        return args

    def test_reuse_completed_returns_without_gpu(self):
        _manifest(self.root, "run-1", "TASK_COMPLETED")
        self.verify.return_value = (
            "verified",
            {"drive_file_id": "drive-file-1", "state": "DELIVERY_COMPLETED"})
        result = hc.deliver_task(self._args())
        self.assertTrue(result["delivered"])
        self.assertEqual(result["outcome"], "reused_completed")
        self.assertEqual(result["run_id"], "run-1")
        self.run_variant.assert_not_called()
        self.resume_run.assert_not_called()

    def test_delivery_evidence_lost_fails_closed(self):
        _manifest(self.root, "run-1", "TASK_COMPLETED")
        self.verify.return_value = (
            "lost", "Drive file drive-file-1 for run run-1 is gone (404)")
        result = hc.deliver_task(self._args())
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "delivery_evidence_lost")
        self.run_variant.assert_not_called()

    def test_archive_recovery_completes_without_new_attempt(self):
        _manifest(self.root, "run-1", "COMPLETED_AWAITING_ARCHIVE")

        def _resume(args):
            rm.update_run(self.root, "run-1", task_state="DRIVE_VERIFIED",
                          artifacts={"drive_file_id": "drive-file-1"},
                          session_state="STOPPED",
                          reopen=True)  # COMPLETED_AWAITING_ARCHIVE is terminal
            rm.update_run(self.root, "run-1", task_state="TASK_COMPLETED")
            return {"resumed": True, "run_id": "run-1",
                    "outcome": "archive recovered"}

        self.resume_run.side_effect = _resume
        self.verify.return_value = (
            "verified",
            {"drive_file_id": "drive-file-1", "state": "DELIVERY_COMPLETED"})
        result = hc.deliver_task(self._args())
        self.assertTrue(result["delivered"])
        self.assertEqual(result["outcome"], "reused_completed")
        self.run_variant.assert_not_called()

    def test_failed_run_stopped_session_new_attempt_succeeds(self):
        _manifest(self.root, "run-1", "FAILED")
        _ledger(self.root)
        self.run_variant.return_value = {
            "delivery": {"drive_file_id": "drive-file-2"}}
        result = hc.deliver_task(self._args())
        self.assertTrue(result["delivered"])
        self.assertEqual(result["outcome"], "new_attempt_succeeded")
        self.assertEqual(result["attempts_used"], 2)
        self.assertEqual(self.run_variant.call_count, 1)
        called_run_id = self.run_variant.call_args[0][0].run_id
        self.assertTrue(called_run_id.startswith("h3-"))

    def test_unknown_session_refuses_new_gpu(self):
        _manifest(self.root, "run-1", "FAILED")
        _ledger(self.root)
        self.wait_stopped.return_value = SessionState.UNKNOWN
        result = hc.deliver_task(self._args())
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "blocked_live_session")
        self.run_variant.assert_not_called()

    def test_active_session_refuses_new_gpu(self):
        _manifest(self.root, "run-1", "FAILED")
        _ledger(self.root)
        self.wait_stopped.return_value = SessionState.ACTIVE
        result = hc.deliver_task(self._args())
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "blocked_live_session")
        self.run_variant.assert_not_called()

    def test_deterministic_error_no_retry(self):
        _manifest(self.root, "run-1", "FAILED")
        _ledger(self.root)
        self.run_variant.side_effect = RuntimeError("ACCOUNT_MISMATCH")
        result = hc.deliver_task(self._args())
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "deterministic_error")
        self.assertEqual(self.run_variant.call_count, 1)

    def test_transient_error_retries_then_succeeds(self):
        _manifest(self.root, "run-1", "FAILED")
        _ledger(self.root)
        self.run_variant.side_effect = [
            RuntimeError("boom transient"),
            {"delivery": {"drive_file_id": "drive-file-2"}},
        ]
        result = hc.deliver_task(self._args())
        self.assertTrue(result["delivered"])
        self.assertEqual(result["outcome"], "new_attempt_succeeded")
        self.assertEqual(self.run_variant.call_count, 2)

    def test_attempts_exhausted(self):
        _manifest(self.root, "run-1", "FAILED")
        _manifest(self.root, "run-2", "FAILED")
        _ledger(self.root)
        result = hc.deliver_task(self._args())
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "attempts_exhausted")
        self.run_variant.assert_not_called()

    def test_budget_insufficient(self):
        _manifest(self.root, "run-1", "FAILED")
        _ledger(self.root, spent=29.0)
        result = hc.deliver_task(self._args())
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "budget_insufficient")
        self.run_variant.assert_not_called()

    def test_deadline_passed(self):
        _manifest(self.root, "run-1", "FAILED")
        _ledger(self.root)
        soon = _fmt(datetime.now(timezone.utc) + timedelta(minutes=10))
        result = hc.deliver_task(self._args(task_deadline_utc=soon))
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "deadline_passed")
        self.run_variant.assert_not_called()

    def test_inflight_resume_refused_live_session_blocks(self):
        _manifest(self.root, "run-1", "VIDEO_VALIDATED")
        self.resume_run.return_value = {
            "resumed": False, "run_id": "run-1",
            "reason": "session state UNKNOWN; refusing to allocate a "
                      "second GPU or stop blindly"}
        result = hc.deliver_task(self._args())
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "blocked_live_session")
        self.run_variant.assert_not_called()

    def test_inflight_resume_failed_then_new_attempt_succeeds(self):
        _manifest(self.root, "run-1", "VIDEO_VALIDATED")
        _ledger(self.root)

        def _resume(args):
            rm.update_run(self.root, "run-1", task_state="FAILED",
                          session_state="STOPPED",
                          last_error="Remote worker failed")
            return {"resumed": True, "run_id": "run-1",
                    "outcome": "worker failed; session stopped and run marked FAILED"}

        self.resume_run.side_effect = _resume
        self.run_variant.return_value = {
            "delivery": {"drive_file_id": "drive-file-2"}}
        result = hc.deliver_task(self._args())
        self.assertTrue(result["delivered"])
        self.assertEqual(result["outcome"], "new_attempt_succeeded")
        self.assertEqual(self.run_variant.call_count, 1)

    def test_missing_task_id_raises(self):
        with self.assertRaises(RuntimeError):
            hc.deliver_task(self._args(production_task_id=""))

    def test_max_attempts_validation(self):
        with self.assertRaises(RuntimeError):
            hc.deliver_task(self._args(max_task_attempts=0))
        with self.assertRaises(RuntimeError):
            hc.deliver_task(self._args(max_task_attempts=4))
        # 3 is legal: it proceeds to the spec gate (no runs exist, so
        # the attempt path is reached and the stubbed run succeeds).
        self.run_variant.return_value = {"delivery": {"drive_file_id": "x"}}
        result = hc.deliver_task(self._args(max_task_attempts=3))
        self.assertTrue(result["delivered"])
        self.assertEqual(result["max_task_attempts"], 3)


class WaitForSessionStoppedTest(unittest.TestCase):
    def test_retries_until_stopped(self):
        with mock.patch("h3_session_watchdog.query_session_state") as q, \
                mock.patch("time.sleep") as slp:
            q.side_effect = [SessionState.UNKNOWN, SessionState.ACTIVE,
                             SessionState.STOPPED]
            state = hc._wait_for_session_stopped("muse-h3-x")
        self.assertEqual(state, SessionState.STOPPED)
        self.assertEqual(q.call_count, 3)
        self.assertEqual(slp.call_count, 2)

    def test_all_unknown_returns_unknown(self):
        with mock.patch("h3_session_watchdog.query_session_state",
                        return_value=SessionState.UNKNOWN), \
                mock.patch("time.sleep"):
            state = hc._wait_for_session_stopped("muse-h3-x")
        self.assertEqual(state, SessionState.UNKNOWN)


class DeterministicErrorTest(unittest.TestCase):
    def test_classifier(self):
        for text in ("ACCOUNT_MISMATCH",
                     "Colab account != Drive account",
                     "NEW_TASK_REQUIRES_AUTHORIZATION",
                     "mint failed: invalid_scope",
                     "POST https://x failed HTTP 403: Forbidden",
                     "BLOCKED: INSUFFICIENT_CU",
                     "Not enough paid time for safe GPU run",
                     "Reference image SHA-256 mismatch for /x: expected",
                     "Reference image SHA256 mismatch: /x",
                     "Delivery refused: local MP4 SHA-256 does not match",
                     "Reference image missing: /x",
                     "Wrong depot folder",
                     "Four model files required, no duplicates",
                     "No server-verified READY document on Drive",
                     "Missing/invalid verified manifest",
                     "READY document size mismatch"):
            self.assertTrue(hc._is_deterministic_error(RuntimeError(text)),
                            text)
        self.assertTrue(hc._is_deterministic_error(
            ValueError("spec field 'prompt' must be non-empty")))
        for text in ("boom transient", "Connection reset by peer",
                     "colab exec timeout", "WorkerPollError: no marker"):
            self.assertFalse(hc._is_deterministic_error(RuntimeError(text)),
                             text)

    def test_resume_refusal_markers(self):
        self.assertTrue(hc._resume_refusal_blocks_new_attempt(
            "session state UNKNOWN; refusing to allocate a second GPU"))
        self.assertTrue(hc._resume_refusal_blocks_new_attempt(
            "worker launched but state unobservable (x); session left running"))
        self.assertTrue(hc._resume_refusal_blocks_new_attempt(
            "worker state belongs to another run: 'run-9'/'A'"))
        self.assertFalse(hc._resume_refusal_blocks_new_attempt(
            "code mismatch: run started at abc, current HEAD is def"))
        self.assertFalse(hc._resume_refusal_blocks_new_attempt(
            "run FAILED; start a new run id"))


class DeliverDispatchTest(unittest.TestCase):
    def test_deliver_action_dispatches(self):
        args = argparse.Namespace(action="deliver", production_task_id=TASK)
        with mock.patch.object(hc, "deliver_task",
                               return_value={"delivered": True}) as d:
            result = hc.test_or_run(args)
        d.assert_called_once_with(args)
        self.assertEqual(result, {"delivered": True})

    def test_run_auto_recover_dispatches(self):
        args = argparse.Namespace(action="run", production_task_id=TASK,
                                  auto_recover=True)
        with mock.patch.object(hc, "deliver_task",
                               return_value={"delivered": True}) as d:
            result = hc.test_or_run(args)
        d.assert_called_once_with(args)
        self.assertEqual(result, {"delivered": True})

    def test_run_without_auto_recover_calls_run_variant(self):
        args = argparse.Namespace(action="run", production_task_id=TASK,
                                  auto_recover=False)
        with mock.patch.object(hc, "run_variant",
                               return_value={"ok": True}) as r:
            result = hc.test_or_run(args)
        r.assert_called_once_with(args)
        # The plain run path prints the result and returns None
        # (pre-existing test_or_run behaviour); what matters here is
        # that run_variant, not deliver_task, is dispatched.
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
