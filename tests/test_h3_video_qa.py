"""Tests for h3_video_qa.build_qa_report (TASK-003 §3).

The report keeps technical acceptance and content (story) acceptance
separate: technical checks are decided by the real QA functions, story
checks always stay CONTENT_REVIEW_REQUIRED until a human reviews the
contact sheet. A technical pass is never presented as a content pass.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_video_qa as qa  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _make_tiny_video(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=24",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=120)


class BuildQaReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe required")
    def test_nonconforming_video_reports_technical_failure_not_crash(self):
        video = self.base / "tiny.mp4"
        _make_tiny_video(video)
        report = qa.build_qa_report(video, run_id="h3-test-run")
        self.assertEqual(report["technical_acceptance"]["status"], "FAILED")
        self.assertIn("expected", report["technical_acceptance"]["error"])
        # Content is never decided by the tools.
        self.assertEqual(report["content_acceptance"]["status"],
                         "CONTENT_REVIEW_REQUIRED")
        self.assertEqual(
            set(report["content_acceptance"]["checks"]),
            {"bull_charge_1_and_dodge", "bull_charge_2_and_dodge",
             "cheetah_appearance", "no_text_ghosting",
             "ending_basketball_raise"})
        self.assertTrue(all(v == "UNREVIEWED"
                            for v in report["content_acceptance"]["checks"].values()))

    def test_happy_path_keeps_technical_and_content_separate(self):
        info = {"frame_count": 274, "width": 1080, "height": 1920,
                "fps": 24.0, "duration": 11.4167, "size": 7453808,
                "sha256": "a" * 64, "technical_pass": True}
        frames = [self.base / f"frame_{i:03d}.png" for i in range(9)]
        sheet = self.base / "contact_sheet.png"
        with mock.patch.object(qa, "validate_final_video", return_value=info), \
                mock.patch.object(qa, "compare_poster_tail",
                                  return_value={"tail_frames": 48, "psnr_db": 31.38,
                                                "pass": True}), \
                mock.patch.object(qa, "extract_qa_frames", return_value=frames), \
                mock.patch.object(qa, "build_contact_sheet", return_value=sheet):
            report = qa.build_qa_report(self.base / "A.mp4",
                                        original_poster=self.base / "poster.jpg",
                                        run_id="h3-20261112T090027Z-8b07c6",
                                        qa_dir=self.base / "qa")
        tech = report["technical_acceptance"]
        self.assertEqual(tech["status"], "PASSED")
        self.assertEqual(tech["tail_psnr_db"], 31.38)
        self.assertTrue(tech["tail_pass"])
        self.assertEqual(tech["frames"], 274)
        self.assertEqual(report["artifacts"]["contact_sheet"], str(sheet))
        self.assertEqual(len(report["artifacts"]["qa_frames"]), 9)
        self.assertEqual(report["content_acceptance"]["status"],
                         "CONTENT_REVIEW_REQUIRED")

    def test_no_poster_leaves_tail_unchecked_but_technical_passes(self):
        info = {"frame_count": 274, "width": 1080, "height": 1920,
                "fps": 24.0, "duration": 11.4167, "size": 7453808,
                "sha256": "a" * 64, "technical_pass": True}
        with mock.patch.object(qa, "validate_final_video", return_value=info):
            report = qa.build_qa_report(self.base / "A.mp4", run_id="r1")
        tech = report["technical_acceptance"]
        self.assertEqual(tech["status"], "PASSED")
        self.assertIsNone(tech["tail_psnr_db"])
        self.assertIsNone(tech["tail_pass"])

    def test_tail_psnr_failure_fails_technical(self):
        info = {"frame_count": 274, "width": 1080, "height": 1920,
                "fps": 24.0, "duration": 11.4167, "size": 7453808,
                "sha256": "a" * 64, "technical_pass": True}
        with mock.patch.object(qa, "validate_final_video", return_value=info), \
                mock.patch.object(qa, "compare_poster_tail",
                                  return_value={"tail_frames": 48, "psnr_db": 12.5,
                                                "pass": False}):
            report = qa.build_qa_report(self.base / "A.mp4",
                                        original_poster=self.base / "poster.jpg",
                                        run_id="r1")
        self.assertEqual(report["technical_acceptance"]["status"], "FAILED")
        self.assertEqual(report["technical_acceptance"]["tail_psnr_db"], 12.5)


if __name__ == "__main__":
    unittest.main()
