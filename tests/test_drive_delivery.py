"""Tests for Drive delivery integration (TASK-002 §4.5, TASK-003 §1-2).

Target: h3_controller.deliver_variant_to_drive + its wiring in run_variant.

The delivery rule: a locally validated MP4 is uploaded to Drive ONLY
after the GPU is stopped and verified, with bounded retries, and the
returned evidence is only complete when the server-side metadata check
has passed. MUSE_H3_DONE_JSON alone is never delivery success.

TASK-003 (Issue #18): the success path used to overwrite the production
result with the stop result in the outer finally
(`result = stop_and_verify_session(session)`), so the delivery call
`deliver_variant_to_drive(variant, result["run_id"], result["video"])`
raised KeyError on every successful run. The regression tests below
mock-execute the REAL run_variant() (no GPU, no ADC, no real Drive) and
assert the wiring — the old inspect.getsource() order-check is gone.

The archive-only retry path (TASK-003 §1) is the `archive` action in
test_or_run: it re-validates the local MP4 and calls the deliver helper
with no GPU involved.
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_controller as hc  # noqa: E402
import h3_session_watchdog as hwd  # noqa: E402
import drive_depot  # noqa: E402
import h3_video_qa  # noqa: E402
from h3_controller import deliver_variant_to_drive  # noqa: E402

RUN_ID = "h3-20261112T000000Z-a1b2c3"
SESSION = "muse-h3-test-session"


def _video_info(sha, frames=274, width=1080, height=1920, fps=24.0):
    return {"frame_count": frames, "width": width, "height": height,
            "fps": fps, "sha256": sha}


def _args():
    return SimpleNamespace(
        variant="A", run_id=RUN_ID, production_task_id="h3-ab-20261111",
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


class DeliverVariantTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self.tmp.name) / "output"
        self.mp4 = self.out_root / RUN_ID / "A.mp4"
        self.mp4.parent.mkdir(parents=True)
        self.mp4.write_bytes(b"fake-validated-mp4-bytes" * 100)
        self.sha = hashlib.sha256(self.mp4.read_bytes()).hexdigest()
        self.patches = [
            mock.patch.object(hc, "OUTPUT_ROOT", self.out_root),
            mock.patch.object(hc, "record", lambda data: None),
            mock.patch.object(hc.time, "sleep", lambda s: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _archive_ok(self):
        def fn(variant, run_id):
            return {"output_folder_id": "folder-1",
                    "files": {"A.mp4": {"drive_file_id": "drive-file-1",
                                        "size": self.mp4.stat().st_size,
                                        "server_md5": "md5", "sha256": self.sha,
                                        "parent": "folder-1"}}}
        return fn

    def test_success_returns_full_delivery_evidence(self):
        ev = deliver_variant_to_drive("A", RUN_ID, _video_info(self.sha),
                                      archive_fn=self._archive_ok())
        self.assertEqual(ev["state"], "DELIVERY_COMPLETED")
        self.assertTrue(ev["local_video_validated"])
        self.assertTrue(ev["drive_upload_verified"])
        self.assertEqual(ev["drive_file_id"], "drive-file-1")
        self.assertEqual((ev["frames"], ev["width"], ev["height"], ev["fps"]),
                         (274, 1080, 1920, 24.0))
        self.assertEqual(ev["sha256"], self.sha)
        self.assertEqual(ev["variant"], "A")

    def test_retry_then_success(self):
        calls = {"n": 0}

        def flaky(variant, run_id):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient Drive 500")
            return self._archive_ok()(variant, run_id)

        ev = deliver_variant_to_drive("A", RUN_ID, _video_info(self.sha),
                                      archive_fn=flaky, max_attempts=3)
        self.assertEqual(ev["state"], "DELIVERY_COMPLETED")
        self.assertEqual(calls["n"], 3)

    def test_permanent_failure_preserves_local_file_and_says_archive_only(self):
        calls = {"n": 0}

        def failing(variant, run_id):
            calls["n"] += 1
            raise RuntimeError("Drive 403")

        with self.assertRaisesRegex(RuntimeError, "Re-run archive only, do NOT re-run inference"):
            deliver_variant_to_drive("A", RUN_ID, _video_info(self.sha),
                                     archive_fn=failing, max_attempts=3)
        self.assertEqual(calls["n"], 3)
        self.assertTrue(self.mp4.is_file())  # local file preserved

    def test_missing_local_file_refuses_without_uploading(self):
        self.mp4.unlink()
        calls = {"n": 0}

        def counting(variant, run_id):
            calls["n"] += 1
            return self._archive_ok()(variant, run_id)

        with self.assertRaisesRegex(RuntimeError, "local MP4 missing"):
            deliver_variant_to_drive("A", RUN_ID, _video_info(self.sha), archive_fn=counting)
        self.assertEqual(calls["n"], 0)

    def test_sha_mismatch_refuses_without_uploading(self):
        with self.assertRaisesRegex(RuntimeError, "SHA-256 does not match"):
            deliver_variant_to_drive("A", RUN_ID, _video_info("0" * 64),
                                     archive_fn=self._archive_ok())

    def test_wrong_frames_refuses_without_uploading(self):
        with self.assertRaisesRegex(RuntimeError, "failed final dimensions/frames"):
            deliver_variant_to_drive("A", RUN_ID, _video_info(self.sha, frames=100),
                                     archive_fn=self._archive_ok())

    def test_archive_without_file_id_is_a_failure(self):
        def no_id(variant, run_id):
            return {"output_folder_id": "folder-1", "files": {}}

        with self.assertRaisesRegex(RuntimeError, "Drive delivery failed after 3 attempts"):
            deliver_variant_to_drive("A", RUN_ID, _video_info(self.sha),
                                     archive_fn=no_id, max_attempts=3)


class RunVariantDeliveryTest(unittest.TestCase):
    """Mock-execute the real run_variant() and assert the delivery wiring.

    No GPU, no ADC, no real Drive: every external boundary is a mock, but
    the function under test is the real one, including its try/finally
    structure. On the pre-fix code the success test fails with
    KeyError: 'run_id' — that is the TASK-003 reproduction.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.out_root = base / "output"
        self.runs_root = base / "runs"
        self.out_root.mkdir()
        self.runs_root.mkdir()
        self.mp4 = self.out_root / RUN_ID / "A.mp4"
        self.mp4.parent.mkdir(parents=True)
        self.mp4.write_bytes(b"fake-validated-mp4-bytes" * 100)
        self.sha = hashlib.sha256(self.mp4.read_bytes()).hexdigest()
        self.video_info = _video_info(self.sha)
        self.calls = []   # ordered ("stop", session) / ("deliver", variant, run_id, video_info)
        self.events = []
        self.stop_ok = True
        self.patches = []
        self.mocks = {}
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
        self.patchers[attr] = p
        return m

    def _install(self):
        self.patchers = {}
        self._po(hc, "OUTPUT_ROOT", self.out_root)
        self._po(hc, "RUNS", self.runs_root)
        self._po(hc, "record", lambda data: self.events.append(data))
        self._po(hc.time, "sleep", lambda s: None)
        self._po(hc, "validate_local")
        self._po(hc, "validate_comfy_workflow", return_value={})
        self._po(hc, "initialize_project_budget",
                 return_value={"max_observed_spent_cu": 0.0,
                               "production_task_id": "h3-ab-20261111"})
        self._po(hc, "inspect",
                 return_value={"cu_balance": 150.0, "rate": 5.0, "sessions": []})
        self._po(hc, "ensure_no_foreign_sessions")
        self._po(hc, "run_cli", return_value={"returncode": 0})
        self._po(hc, "choose_gpu_candidate", return_value={"key": "G4"})
        self._po(hc, "allocate_managed_session", return_value=SESSION)
        self._po(hc, "probe_actual_gpu", return_value={"gpu": "G4"})
        self._po(hc, "reconcile_budget",
                 side_effect=lambda ledger, bal, path: ledger)
        self._po(hc, "estimate_unbilled_cu", return_value=0.1)
        self._po(hc, "allowed_runtime_seconds", return_value=3600)
        self._po(hc, "initial_session_wall_seconds", return_value=3600)
        self._po(hc, "exec_script",
                 return_value={"stdout": "MUSE_DRIVE_MOUNT_MODELS_VISIBLE 4",
                               "returncode": 0})
        self._po(hc, "check_mount_signature", return_value=True)
        self._po(hc, "mount_drive", return_value={})
        self.mocks["worker_marker"] = self._po(
            hc, "check_worker_done_marker",
            return_value={"frames": 274, "file": "x.mp4"})
        self._po(hc, "check_remote_output", return_value={"ok": True})
        self._po(hc, "download_and_validate_variant",
                 return_value=self.video_info)
        self._po(hc, "download_remote_comfyui_log")
        self._po(hc, "_comfyui_cache_task", return_value={})
        self.mocks["watchdog"] = self._po(
            hc, "_wait_watchdog_ready",
            return_value={"session_id": SESSION,
                          "production_task_id": "h3-ab-20261111"})
        self._po(hc, "ledger_path_for_task",
                 return_value=str(Path(self.tmp.name) / "ledger.json"))
        self._po(hwd, "record_session",
                 return_value={"started_at_utc": "2026-11-12T00:00:00+00:00",
                               "session_id": SESSION})
        self._po(hwd, "reconcile_on_startup",
                 return_value={"managed_active_sessions": []})
        self._po(hwd, "mark_session_stopped")
        self._po(hwd, "update_session_deadline")
        self._po(drive_depot, "drive_service", return_value=mock.Mock())
        self._po(drive_depot, "account_matches", return_value=True)
        self._po(drive_depot, "verify_ready", return_value={})
        proc = mock.Mock()
        proc.poll.return_value = None
        popen_patch = mock.patch("subprocess.Popen", return_value=proc)
        popen_patch.start()
        self.patches.append(popen_patch)
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        self._po(sys, "stdin", stdin)
        # Stop + deliver mocks with an ordered call log.
        self.mocks["stop"] = self._po(
            hc, "stop_and_verify_session", side_effect=self._fake_stop)
        self.mocks["deliver"] = self._po(
            hc, "deliver_variant_to_drive", side_effect=self._fake_deliver)

    def _fake_stop(self, session):
        self.calls.append(("stop", session))
        return {"session": session, "stopped_confirmed": self.stop_ok}

    def _fake_deliver(self, variant, run_id, video_info, **kwargs):
        self.calls.append(("deliver", variant, run_id, dict(video_info)))
        return {"state": "DELIVERY_COMPLETED", "variant": variant, "run_id": run_id,
                "drive_file_id": "drive-file-1", "drive_upload_verified": True,
                "local_video_validated": True, "sha256": video_info["sha256"]}

    def _stop_calls(self):
        return [c for c in self.calls if c[0] == "stop"]

    def _deliver_calls(self):
        return [c for c in self.calls if c[0] == "deliver"]

    def test_success_delivers_original_run_id_and_video_after_stop(self):
        result = hc.run_variant(_args())
        # 1. stop_and_verify_session() was called, once, for our session.
        self.assertEqual(self._stop_calls(), [("stop", SESSION)])
        # 2. deliver received the ORIGINAL run_id and video_info — not the
        # stop result dict (pre-fix: KeyError 'run_id' here).
        self.assertEqual(len(self._deliver_calls()), 1)
        _, variant, run_id, video_info = self._deliver_calls()[0]
        self.assertEqual((variant, run_id), ("A", RUN_ID))
        self.assertEqual(video_info["sha256"], self.sha)
        self.assertEqual(video_info["frame_count"], 274)
        # 3. Archive happened after the stop confirmation.
        kinds = [c[0] for c in self.calls]
        self.assertLess(kinds.index("stop"), kinds.index("deliver"))
        # 4. The returned result carries both the original video info and
        # the final delivery evidence.
        self.assertEqual(result["run_id"], RUN_ID)
        self.assertEqual(result["video"]["sha256"], self.sha)
        self.assertEqual(result["delivery"]["state"], "DELIVERY_COMPLETED")
        self.assertEqual(result["delivery"]["drive_file_id"], "drive-file-1")

    def test_stop_not_confirmed_skips_delivery_and_fails(self):
        self.stop_ok = False
        with self.assertRaisesRegex(RuntimeError, "Stop not verified"):
            hc.run_variant(_args())
        self.assertEqual(self._stop_calls(), [("stop", SESSION)])
        self.assertEqual(self._deliver_calls(), [])

    def test_drive_failure_preserves_local_mp4(self):
        # Use the REAL deliver helper with a failing archive function:
        # the run must surface the archive-only error and keep the MP4.
        # Stop only the deliver patch (leave every other mock in place).
        deliver_patch = self.patchers["deliver_variant_to_drive"]
        deliver_patch.stop()
        self.patches.remove(deliver_patch)
        archive = self._po(
            hc, "archive_variant_to_drive",
            side_effect=RuntimeError("Drive 403"))
        with self.assertRaisesRegex(RuntimeError, "Re-run archive only, do NOT re-run inference"):
            hc.run_variant(_args())
        self.assertEqual(archive.call_count, 3)  # bounded retries, then give up
        self.assertEqual(self._stop_calls(), [("stop", SESSION)])
        self.assertTrue(self.mp4.is_file())  # local MP4 preserved

    def test_watchdog_failure_still_stops_managed_gpu(self):
        self.mocks["watchdog"].side_effect = RuntimeError(
            "WATCHDOG_NOT_RUNNING: watchdog exited")
        with self.assertRaisesRegex(RuntimeError, "WATCHDOG_NOT_RUNNING"):
            hc.run_variant(_args())
        self.assertEqual(self._stop_calls(), [("stop", SESSION)])
        self.assertEqual(self._deliver_calls(), [])

    def test_worker_failure_propagates_original_error_and_stops_gpu(self):
        self.mocks["worker_marker"].side_effect = RuntimeError(
            "worker marker missing: no MUSE_H3_DONE_JSON")
        with self.assertRaisesRegex(RuntimeError, "worker marker missing"):
            hc.run_variant(_args())
        self.assertEqual(self._stop_calls(), [("stop", SESSION)])
        self.assertEqual(self._deliver_calls(), [])


class ArchiveActionTest(unittest.TestCase):
    """The archive-only retry path: re-validate local MP4, deliver, no GPU."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self.tmp.name) / "output"
        self.mp4 = self.out_root / RUN_ID / "A.mp4"
        self.mp4.parent.mkdir(parents=True)
        self.mp4.write_bytes(b"fake-validated-mp4-bytes" * 100)
        self.sha = hashlib.sha256(self.mp4.read_bytes()).hexdigest()
        self.patches = [
            mock.patch.object(hc, "OUTPUT_ROOT", self.out_root),
            mock.patch.object(hc, "record", lambda data: None),
            mock.patch.object(hc.time, "sleep", lambda s: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_archive_delivers_validated_info_without_gpu(self):
        vp = mock.patch.object(
            h3_video_qa, "validate_final_video",
            return_value=_video_info(self.sha))
        validate = vp.start()
        self.patches.append(vp)
        dp = mock.patch.object(
            hc, "deliver_variant_to_drive",
            return_value={"state": "DELIVERY_COMPLETED",
                          "drive_file_id": "drive-file-1"})
        deliver = dp.start()
        self.patches.append(dp)
        ap = mock.patch.object(hc, "allocate_managed_session")
        allocate = ap.start()
        self.patches.append(ap)
        args = SimpleNamespace(action="archive", variant="A", run_id=RUN_ID,
                               production_task_id="h3-ab-20261111")
        result = hc.test_or_run(args)
        validate.assert_called_once_with(self.mp4)
        deliver.assert_called_once_with("A", RUN_ID, _video_info(self.sha))
        allocate.assert_not_called()
        self.assertEqual(result["state"], "DELIVERY_COMPLETED")

    def test_archive_refuses_missing_local_mp4(self):
        self.mp4.unlink()
        args = SimpleNamespace(action="archive", variant="A", run_id=RUN_ID,
                               production_task_id="h3-ab-20261111")
        with self.assertRaisesRegex(RuntimeError, "Video missing or empty"):
            hc.test_or_run(args)


if __name__ == "__main__":
    unittest.main()
