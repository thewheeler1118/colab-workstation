"""Regression tests for SaveVideo history-shape handling (TASK-002 §4.2-4.4).

Target: h3_remote_worker.find_rendered_video / rescue_rendered_video /
probe_animation, plus h3_contract.patch_api_workflow filename_prefix.

Current behaviour:
- History keys 'videos' and 'gifs' are always read, and 'images' ONLY when
  that output entry has a truthy 'animated' value (pinned ComfyUI 0.44.0
  SaveVideo UI output is PreviewVideo: {"images": [...], "animated": [true]}
  or {"animated": true} — NOT {"videos": [...]}).
- A candidate passes only if the file exists under <comfy>/output/, its
  suffix is .mp4/.webm/.mov, its size is > 10000 bytes, its name contains
  this run's RUN_ID (and variant) when given, AND the ffprobe decode gate
  passes: full decode, video stream, exactly 226 frames at 24 fps,
  736x1312 (the pinned production constants).
- Exactly one valid candidate wins; several valid candidates raise
  AMBIGUITY instead of silently taking the first.
- History failure does not end the run: rescue_rendered_video() scans
  ComfyUI/output/ for this RUN_ID under a time budget, with the same
  gates, and returns SHA-256 evidence for the single valid file.

Success-path tests use real ffmpeg-encoded videos (the gate is a real
decode check, so a stub probe would prove nothing). The 226-frame fixture
is encoded once per class and copied into each test's temp dir.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from h3_contract import patch_api_workflow  # noqa: E402
from h3_remote_worker import (  # noqa: E402
    find_rendered_video,
    probe_animation,
    rescue_rendered_video,
)

RUN_ID = "h3-20261112T000000Z-a1b2c3"
OTHER_RUN_ID = "h3-20261112T000000Z-d4e5f6"
FAKE_VIDEO = b"\x00\x00\x00\x18ftypisom" + b"x" * 20000  # > 10000 bytes, .mp4 suffix


def _encode_video(dest, frames=226, size="736x1312", rate=24):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={frames / rate}",
         "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(dest)],
        check=True, timeout=120,
    )


def _comfy_with(d, name, blob):
    """Build a fake ComfyUI dir with output/<name> present (blob bytes)."""
    base = Path(d)
    out = base / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_bytes(blob)
    return base


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg/ffprobe required for the real decode-gate tests")
class SaveVideoHistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        valid = base / "valid_226f.mp4"
        _encode_video(valid, frames=226)
        cls.valid_blob = valid.read_bytes()
        short = base / "short_100f.mp4"
        _encode_video(short, frames=100)
        cls.short_blob = short.read_bytes()
        wrong_dims = base / "wrong_dims.mp4"
        _encode_video(wrong_dims, frames=226, size="640x480")
        cls.wrong_dims_blob = wrong_dims.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _history(self, name):
        return {"92": {"images": [{"filename": name, "subfolder": "", "type": "output"}],
                       "animated": [True]}}

    def test_previewvideo_images_with_animated_list(self):
        # Pinned 0.44.0 shape: {"images": [...], "animated": [true]}
        name = f"muse_h3_A_{RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, self.valid_blob)
            self.assertEqual(
                find_rendered_video(comfy, self._history(name), RUN_ID, "A"),
                comfy / "output" / name)

    def test_previewvideo_images_with_animated_bool(self):
        # Same shape with a plain boolean True (as the task lists it).
        name = f"muse_h3_A_{RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, self.valid_blob)
            outputs = {"92": {"images": [{"filename": name, "subfolder": "", "type": "output"}],
                              "animated": True}}
            self.assertEqual(
                find_rendered_video(comfy, outputs, RUN_ID, "A"),
                comfy / "output" / name)

    def test_legacy_videos_key(self):
        # Older ComfyUI shape: {"videos": [{"filename": ...}]}
        name = f"muse_h3_A_{RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, self.valid_blob)
            outputs = {"92": {"videos": [{"filename": name, "subfolder": ""}]}}
            self.assertEqual(
                find_rendered_video(comfy, outputs, RUN_ID, "A"),
                comfy / "output" / name)

    def test_bare_animated_true_carries_no_filename(self):
        # {"animated": true} alone names no file, so nothing can be located.
        # This is the correct fail-closed behaviour for this shape.
        with tempfile.TemporaryDirectory() as d:
            comfy = Path(d)
            (comfy / "output").mkdir()
            outputs = {"92": {"animated": True}}
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs, RUN_ID, "A")

    def test_images_without_animated_is_ignored(self):
        # A still image under 'images' with no 'animated' flag must not pass:
        # the 'images' key is only read when animated is truthy.
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, "still.jpg", b"y" * 20000)
            outputs = {"92": {"images": [{"filename": "still.jpg", "subfolder": "", "type": "output"}]}}
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs, RUN_ID, "A")

    def test_wrong_suffix_or_tiny_file_rejected(self):
        # Suffix gate: .jpg never passes even when large and animated.
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, "frame.jpg", b"y" * 20000)
            outputs = {"92": {"images": [{"filename": "frame.jpg", "subfolder": "", "type": "output"}],
                              "animated": [True]}}
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs, RUN_ID, "A")
        # Size gate: a 10-byte .mp4 never passes.
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, "example.mp4", b"tiny")
            outputs = {"92": {"images": [{"filename": "example.mp4", "subfolder": "", "type": "output"}],
                              "animated": [True]}}
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs, RUN_ID, "A")

    def test_missing_file_rejected(self):
        # History names a file that is not on disk: fail closed.
        with tempfile.TemporaryDirectory() as d:
            comfy = Path(d)
            (comfy / "output").mkdir()
            outputs = self._history(f"muse_h3_A_{RUN_ID}_00001_.mp4")
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs, RUN_ID, "A")

    def test_fake_mp4_blob_rejected_by_ffprobe_gate(self):
        # TASK-002 §4.3: the old test recorded that a >10000-byte blob of
        # arbitrary bytes with an .mp4 suffix WAS accepted (suffix+size
        # only). That is now a failure: the blob cannot be decoded, so the
        # ffprobe gate rejects it.
        name = f"muse_h3_A_{RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, FAKE_VIDEO)
            with self.assertRaisesRegex(RuntimeError, "ffprobe failed|No video stream"):
                find_rendered_video(comfy, self._history(name), RUN_ID, "A")

    def test_wrong_frame_count_rejected(self):
        # A decodable video with 100 frames is not the 226-frame animation.
        name = f"muse_h3_A_{RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, self.short_blob)
            with self.assertRaisesRegex(RuntimeError, "frames=100, expected 226"):
                find_rendered_video(comfy, self._history(name), RUN_ID, "A")

    def test_wrong_dimensions_rejected(self):
        # A decodable 226-frame video at the wrong resolution is not ours.
        name = f"muse_h3_A_{RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, self.wrong_dims_blob)
            with self.assertRaisesRegex(RuntimeError, "expected 736x1312"):
                find_rendered_video(comfy, self._history(name), RUN_ID, "A")

    def test_history_candidate_from_other_run_rejected(self):
        # History names a valid video, but for a different RUN_ID: it must
        # not be used, even though it decodes fine.
        name = f"muse_h3_A_{OTHER_RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, self.valid_blob)
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, self._history(name), RUN_ID, "A")

    def test_ambiguous_candidates_rejected(self):
        # Two different valid videos both matching this RUN_ID: ambiguity
        # is an error, never "take the first".
        name1 = f"muse_h3_A_{RUN_ID}_00001_.mp4"
        name2 = f"muse_h3_A_{RUN_ID}_00002_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name1, self.valid_blob)
            (comfy / "output" / name2).write_bytes(self.valid_blob)
            outputs = {"92": {"images": [{"filename": name1, "subfolder": "", "type": "output"},
                                          {"filename": name2, "subfolder": "", "type": "output"}],
                              "animated": [True]}}
            with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
                find_rendered_video(comfy, outputs, RUN_ID, "A")

    def test_probe_animation_accepts_real_video(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v.mp4"
            p.write_bytes(self.valid_blob)
            info = probe_animation(p)
            self.assertEqual((info["frames"], info["fps"], info["width"], info["height"]),
                             (226, 24.0, 736, 1312))

    def test_rescue_finds_run_id_file_when_history_fails(self):
        # History names nothing usable, but this run's file is on disk.
        name = f"muse_h3_A_{RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, self.valid_blob)
            found = rescue_rendered_video(comfy, RUN_ID, "A", remaining_budget_seconds=60)
            self.assertEqual(found["path"], str(comfy / "output" / name))
            self.assertEqual((found["frames"], found["width"], found["height"]), (226, 736, 1312))
            self.assertEqual(found["size_bytes"], len(self.valid_blob))
            self.assertEqual(len(found["sha256"]), 64)

    def test_rescue_rejects_other_run_file(self):
        # Only another run's file is on disk: rescue must not take it.
        name = f"muse_h3_A_{OTHER_RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, self.valid_blob)
            with self.assertRaisesRegex(RuntimeError, "no verifiable video"):
                rescue_rendered_video(comfy, RUN_ID, "A", remaining_budget_seconds=60)

    def test_rescue_respects_zero_time_budget(self):
        # No time left: rescue refuses immediately instead of scanning.
        name = f"muse_h3_A_{RUN_ID}_00001_.mp4"
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name, self.valid_blob)
            with self.assertRaisesRegex(RuntimeError, "time budget exhausted"):
                rescue_rendered_video(comfy, RUN_ID, "A", remaining_budget_seconds=0)

    def test_filename_prefix_includes_run_id(self):
        # patch_api_workflow binds the SaveVideo filename to the run.
        with open(ROOT / "workflow_api.json") as f:
            import json
            api = json.load(f)
        patched = patch_api_workflow(api, "A", run_id=RUN_ID)
        self.assertEqual(patched["92"]["inputs"]["filename_prefix"], f"muse_h3_A_{RUN_ID}")
        legacy = patch_api_workflow(api, "A")
        self.assertEqual(legacy["92"]["inputs"]["filename_prefix"], "muse_h3_A")


if __name__ == "__main__":
    unittest.main()
