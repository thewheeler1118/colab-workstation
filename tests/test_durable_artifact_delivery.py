"""TASK-005 §5 (Issue #28): durable artifact delivery.

Delivery states (TEMPORARY -> COMPLETE_LOCAL -> DURABLE_UPLOAD_STARTED
-> DURABLE_UPLOAD_VERIFIED -> DELIVERED) are recorded in the run
manifest; delivery is idempotent — a manifest-recorded Drive file id is
server-verified and reused instead of re-uploaded, and a recorded id
that does not verify is an explicit error, never a silent re-upload.

No GPU, no live Drive: the Drive service and the verify helper are
mocked; the functions under test are the real ones.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_api  # noqa: E402
import drive_depot  # noqa: E402
import h3_controller as hc  # noqa: E402
import h3_run_manifest as rm  # noqa: E402
import h3_video_qa as hvq  # noqa: E402

RUN_ID = "h3-20261113T000000Z-deliver01"
TASK = "h3-ab-20261111"
SESSION = "muse-h3-deliver01"


def _video_info(sha, frames=274):
    return {"sha256": sha, "frame_count": frames, "width": 1080,
            "height": 1920, "fps": 24}


def _archive_ok(drive_file_id="drive-file-1", folder="folder-1"):
    def fn(variant, run_id):
        return {"output_folder_id": folder,
                "files": {f"{variant}.mp4": {"drive_file_id": drive_file_id,
                                             "size": 1234, "server_md5": "md5",
                                             "sha256": "x" * 64, "parent": folder}}}
    return fn


class _DeliverBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "model_cache").mkdir()
        self.out_root = self.root / "output"
        self.mp4 = self.out_root / RUN_ID / "A.mp4"
        self.mp4.parent.mkdir(parents=True)
        self.mp4.write_bytes(b"fake-validated-mp4-bytes" * 100)
        self.sha = hashlib.sha256(self.mp4.read_bytes()).hexdigest()
        self.patches = [
            mock.patch.object(hc, "ROOT", self.root),
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

    def _manifest(self, artifacts=None):
        m = rm.create_run_manifest(self.root, TASK, RUN_ID, variant="A",
                                   code_commit="abc1234", budget={})
        if artifacts:
            rm.update_run(self.root, RUN_ID, artifacts=artifacts)
        return rm.load_run_manifest(self.root, RUN_ID)

    def _artifacts(self):
        return rm.load_run_manifest(self.root, RUN_ID).get("artifacts") or {}


class DeliveryStateTest(_DeliverBase):
    def test_success_records_verified_state(self):
        self._manifest()
        ev = hc.deliver_variant_to_drive("A", RUN_ID, _video_info(self.sha),
                                         archive_fn=_archive_ok())
        self.assertEqual(ev["state"], "DELIVERY_COMPLETED")
        self.assertEqual(self._artifacts()["delivery_state"],
                         "DURABLE_UPLOAD_VERIFIED")

    def test_failure_leaves_upload_started_state(self):
        self._manifest()
        with self.assertRaisesRegex(RuntimeError, "Drive delivery failed after 3 attempts"):
            hc.deliver_variant_to_drive(
                "A", RUN_ID, _video_info(self.sha),
                archive_fn=mock.Mock(side_effect=RuntimeError("Drive 500")))
        self.assertEqual(self._artifacts()["delivery_state"],
                         "DURABLE_UPLOAD_STARTED")

    def test_no_manifest_still_delivers(self):
        # Best-effort manifest updates never break a delivery for a run
        # whose manifest does not exist (e.g. a pre-TASK-005 run).
        ev = hc.deliver_variant_to_drive("A", RUN_ID, _video_info(self.sha),
                                         archive_fn=_archive_ok())
        self.assertEqual(ev["drive_file_id"], "drive-file-1")


class IdempotentDeliverTest(_DeliverBase):
    def test_recorded_id_verified_is_reused_without_upload(self):
        self._manifest(artifacts={"drive_file_id": "drive-file-9",
                                  "drive_output_folder_id": "folder-9"})
        archive = mock.Mock(side_effect=AssertionError("upload must not run"))
        service = mock.Mock()
        with mock.patch.object(drive_depot, "drive_service", return_value=service):
            with mock.patch.object(drive_api, "verify_uploaded_file",
                                   return_value=None) as verify:
                ev = hc.deliver_variant_to_drive_idempotent(
                    "A", RUN_ID, _video_info(self.sha), archive_fn=archive)
        verify.assert_called_once()
        args = verify.call_args[0]
        self.assertIs(args[0], service)
        self.assertEqual(args[1], "drive-file-9")
        self.assertEqual(str(args[2]), str(self.mp4))
        archive.assert_not_called()
        self.assertTrue(ev["reused_existing"])
        self.assertEqual(ev["reused_via"], "recorded_id")
        self.assertEqual(ev["drive_file_id"], "drive-file-9")
        self.assertEqual(ev["drive_output_folder_id"], "folder-9")
        self.assertEqual(ev["state"], "DELIVERY_COMPLETED")

    def test_recorded_id_that_does_not_verify_is_an_explicit_error(self):
        self._manifest(artifacts={"drive_file_id": "drive-file-9",
                                  "drive_output_folder_id": "folder-9"})
        archive = mock.Mock(side_effect=AssertionError("upload must not run"))
        with mock.patch.object(drive_depot, "drive_service", return_value=mock.Mock()):
            with mock.patch.object(
                    drive_api, "verify_uploaded_file",
                    side_effect=RuntimeError("Drive server MD5 missing/mismatch")):
                with self.assertRaisesRegex(RuntimeError, "does not verify"):
                    hc.deliver_variant_to_drive_idempotent(
                        "A", RUN_ID, _video_info(self.sha), archive_fn=archive)
        archive.assert_not_called()

    def test_no_recorded_id_falls_through_to_upload(self):
        self._manifest()
        ev = hc.deliver_variant_to_drive_idempotent(
            "A", RUN_ID, _video_info(self.sha), archive_fn=_archive_ok())
        self.assertEqual(ev["drive_file_id"], "drive-file-1")
        self.assertNotIn("reused_existing", ev)

    def test_validation_runs_before_any_drive_call(self):
        self._manifest()
        self.mp4.unlink()
        with mock.patch.object(drive_depot, "drive_service") as svc:
            with self.assertRaisesRegex(RuntimeError, "Delivery refused"):
                hc.deliver_variant_to_drive_idempotent(
                    "A", RUN_ID, _video_info(self.sha))
        svc.assert_not_called()


class ResumeUploadStartedTest(_DeliverBase):
    """A run stuck in DURABLE_UPLOAD_STARTED (upload attempted, never
    verified) resumes into the idempotent path and does NOT re-infer or
    re-upload."""

    def test_resume_reuses_recorded_file_and_completes(self):
        self._manifest(artifacts={"delivery_state": "DURABLE_UPLOAD_STARTED",
                                  "drive_file_id": "drive-file-9",
                                  "drive_output_folder_id": "folder-9"})
        rm.update_run(self.root, RUN_ID, task_state="COMPLETED_AWAITING_ARCHIVE")
        args = SimpleNamespace(action="resume", run_id=RUN_ID,
                               production_task_id=TASK, max_wall_sec=14400,
                               poll_interval_sec=60)
        with mock.patch.object(hvq, "validate_final_video",
                               return_value=_video_info(self.sha)):
            with mock.patch.object(drive_depot, "drive_service",
                                   return_value=mock.Mock()):
                with mock.patch.object(drive_api, "verify_uploaded_file",
                                       return_value=None):
                    with mock.patch.object(hc, "archive_variant_to_drive") as arch:
                        result = hc._resume_run(args)
        arch.assert_not_called()
        self.assertTrue(result["resumed"])
        self.assertEqual(result["delivery"]["drive_file_id"], "drive-file-9")
        self.assertTrue(result["delivery"]["reused_existing"])
        manifest = rm.load_run_manifest(self.root, RUN_ID)
        self.assertEqual(manifest["task_state"], "TASK_COMPLETED")
        self.assertEqual(manifest["artifacts"]["delivery_state"], "DELIVERED")
        self.assertEqual(manifest["artifacts"]["drive_file_id"], "drive-file-9")


if __name__ == "__main__":
    unittest.main()
