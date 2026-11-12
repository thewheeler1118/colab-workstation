"""Tests for Drive delivery integration (TASK-002 §4.5).

Target: h3_controller.deliver_variant_to_drive + its wiring in run_variant.

The delivery rule: a locally validated MP4 is uploaded to Drive ONLY
after the GPU is stopped and verified, with bounded retries, and the
returned evidence is only complete when the server-side metadata check
has passed. MUSE_H3_DONE_JSON alone is never delivery success.

These tests use a fake archive function and a fake local MP4 in a temp
OUTPUT_ROOT — no GPU, no Drive, no credentials. The wiring test checks
the actual run_variant source: the archive call must sit after the GPU
stop (outer finally), and run_variant must not return before the stop.
"""
import hashlib
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_controller as hc  # noqa: E402
from h3_controller import deliver_variant_to_drive  # noqa: E402

RUN_ID = "h3-20261112T000000Z-a1b2c3"


def _video_info(sha, frames=274, width=1080, height=1920, fps=24.0):
    return {"frame_count": frames, "width": width, "height": height,
            "fps": fps, "sha256": sha}


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


class RunVariantWiringTest(unittest.TestCase):
    def test_delivery_happens_after_gpu_stop_in_run_variant(self):
        # Structural check on the real function (no GPU run): the Drive
        # delivery call must come after the stop-and-verify finally block,
        # and run_variant must not return a result before that stop.
        src = inspect.getsource(hc.run_variant)
        stop_pos = src.index("stop_and_verify_session(session)")
        deliver_pos = src.index("deliver_variant_to_drive(")
        self.assertGreater(deliver_pos, stop_pos,
                           "Drive delivery must happen after the GPU stop")
        # The old early `return {` inside the success path is gone: the
        # result is stored, then delivered after the stop, then returned.
        self.assertIn("result = {", src)
        self.assertNotIn("return {\n", src)
        # archive_variant_to_drive is only reached via the deliver helper.
        self.assertNotIn("archive_variant_to_drive(variant", src)


if __name__ == "__main__":
    unittest.main()
