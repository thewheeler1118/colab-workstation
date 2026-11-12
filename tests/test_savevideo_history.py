"""Regression tests for SaveVideo history-shape handling.

Target: h3_remote_worker.find_rendered_video(comfy, outputs)
Real behaviour (read from h3_remote_worker.py, lines ~548-566):
- It reads output keys 'videos' and 'gifs' always, and 'images' ONLY when
  that output entry has a truthy 'animated' value (the pinned ComfyUI
  0.44.0 SaveVideo UI output is PreviewVideo: {"images": [...],
  "animated": (True,)} — NOT {"videos": [...]}).
- A candidate passes only if the file exists under <comfy>/output/,
  its suffix is .mp4/.webm/.mov, and its size is > 10000 bytes.
- Otherwise it raises RuntimeError ("did not contain verifiable saved
  video"). The suffix+size check is what proves the entry is a real video
  file, so a still image under 'images' can never pass.
- What it does NOT do (documented gap, see
  docs/issues/p0-savevideo-history-schema.md): no ffprobe decode check,
  no RUN_ID-prefix lookup under ComfyUI/output/. Those are PROPOSED_ONLY.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from h3_remote_worker import find_rendered_video  # noqa: E402

FAKE_VIDEO = b"\x00\x00\x00\x18ftypisom" + b"x" * 20000  # > 10000 bytes, .mp4 suffix


def _comfy_with(d, name="example.mp4", subfolder="", size_blob=FAKE_VIDEO):
    """Build a fake ComfyUI dir with output/<subfolder>/<name> present."""
    base = Path(d)
    out = base / "output" / subfolder
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_bytes(size_blob)
    return base


class SaveVideoHistoryTest(unittest.TestCase):
    def test_previewvideo_images_with_animated_list(self):
        # Pinned 0.44.0 shape: {"images": [...], "animated": [true]}
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d)
            outputs = {"92": {"images": [{"filename": "example.mp4", "subfolder": "", "type": "output"}],
                               "animated": [True]}}
            self.assertEqual(find_rendered_video(comfy, outputs), comfy / "output" / "example.mp4")

    def test_previewvideo_images_with_animated_bool(self):
        # Same shape with a plain boolean True (as the task lists it).
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d)
            outputs = {"92": {"images": [{"filename": "example.mp4", "subfolder": "", "type": "output"}],
                               "animated": True}}
            self.assertEqual(find_rendered_video(comfy, outputs), comfy / "output" / "example.mp4")

    def test_legacy_videos_key(self):
        # Older ComfyUI shape: {"videos": [{"filename": "example.mp4"}]}
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d)
            outputs = {"92": {"videos": [{"filename": "example.mp4", "subfolder": ""}]}}
            self.assertEqual(find_rendered_video(comfy, outputs), comfy / "output" / "example.mp4")

    def test_bare_animated_true_carries_no_filename(self):
        # {"animated": true} alone names no file, so nothing can be located.
        # This is the correct fail-closed behaviour for this shape.
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d)
            outputs = {"92": {"animated": True}}
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs)

    def test_images_without_animated_is_ignored(self):
        # A still image under 'images' with no 'animated' flag must not pass:
        # the 'images' key is only read when animated is truthy.
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name="still.jpg", size_blob=b"y" * 20000)
            outputs = {"92": {"images": [{"filename": "still.jpg", "subfolder": "", "type": "output"}]}}
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs)

    def test_wrong_suffix_or_tiny_file_rejected(self):
        # Suffix gate: .jpg never passes even when large and animated.
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, name="frame.jpg", size_blob=b"y" * 20000)
            outputs = {"92": {"images": [{"filename": "frame.jpg", "subfolder": "", "type": "output"}],
                               "animated": [True]}}
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs)
        # Size gate: a 10-byte .mp4 never passes.
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d, size_blob=b"tiny")
            outputs = {"92": {"images": [{"filename": "example.mp4", "subfolder": "", "type": "output"}],
                               "animated": [True]}}
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs)

    def test_missing_file_rejected(self):
        # History names a file that is not on disk: fail closed.
        with tempfile.TemporaryDirectory() as d:
            comfy = Path(d)
            (comfy / "output").mkdir()
            outputs = {"92": {"images": [{"filename": "example.mp4", "subfolder": "", "type": "output"}],
                               "animated": [True]}}
            with self.assertRaises(RuntimeError):
                find_rendered_video(comfy, outputs)

    def test_current_behaviour_has_no_ffprobe_gate(self):
        # DOCUMENTED GAP (not a bug in this test): a >10000-byte blob of
        # arbitrary bytes with an .mp4 suffix IS accepted — find_rendered_video
        # checks existence + suffix + size only, it never decodes the file.
        # The ffprobe decode gate lives downstream (h3_output_check on the
        # remote runtime, h3_video_qa locally). If this test ever starts
        # failing because a decode check was added, update this doc and
        # close docs/issues/p0-savevideo-history-schema.md accordingly.
        with tempfile.TemporaryDirectory() as d:
            comfy = _comfy_with(d)
            outputs = {"92": {"images": [{"filename": "example.mp4", "subfolder": "", "type": "output"}],
                               "animated": [True]}}
            self.assertEqual(find_rendered_video(comfy, outputs), comfy / "output" / "example.mp4")


if __name__ == "__main__":
    unittest.main()
