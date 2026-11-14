"""TASK-008 (Issue #57): purge of the 274 / 1080x1920 hardcoded gates.

The spec must drive every acceptance gate; only the spec-less legacy
path falls back to the frozen A/B shape (274 frames, 24 fps,
1080x1920). No GPU, no CU, no Drive — all fixtures are local.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import h3_controller as hc
import h3_run_manifest as rm
from h3_task_spec import RefImage, TaskSpec

SHA = "ab" * 32


def _spec(task_id="h3-t008-purge", tail=0, frames=226, fps=24,
          out_w=1080, out_h=1920, prefix="A"):
    return TaskSpec(
        task_id=task_id, output_prefix=prefix,
        ref_image_1=RefImage(sha256=SHA, local_path="assets/original_poster.jpg"),
        prompt="test prompt", seed=42,
        animation_frames=frames, tail_card_frames=tail, fps=fps,
        output_width=out_w, output_height=out_h)


def _done_info(frames, sha="cd" * 32):
    return {"frames": frames, "file": "A.mp4", "sha256": sha}


def _output_check(frames=226, fps=24.0, width=1080, height=1920):
    body = {"ok": True, "exists": True, "size_bytes": 1234,
            "frames": frames, "fps": fps, "width": width, "height": height,
            "codec": "h264"}
    return {"stdout": "OUTPUT_CHECK_JSON:" + json.dumps(body) + "\n",
            "stderr": ""}


class CheckJobDoneInfoTest(unittest.TestCase):
    def test_spec_226_passes(self):
        self.assertEqual(
            hc.check_job_done_info(_done_info(226), "A", _spec()),
            _done_info(226))

    def test_spec_226_rejects_274(self):
        with self.assertRaises(RuntimeError) as ctx:
            hc.check_job_done_info(_done_info(274), "A", _spec())
        self.assertIn("expected 226", str(ctx.exception))

    def test_spec_with_tail_274_passes(self):
        spec = _spec(tail=48)
        self.assertEqual(spec.total_frames, 274)
        hc.check_job_done_info(_done_info(274), "A", spec)

    def test_legacy_no_spec_still_expects_274(self):
        hc.check_job_done_info(_done_info(274), "A")
        with self.assertRaises(RuntimeError):
            hc.check_job_done_info(_done_info(226), "A")


class CheckRemoteOutputTest(unittest.TestCase):
    def test_spec_226_passes(self):
        info = hc.check_remote_output(_output_check(), "A", _spec())
        self.assertEqual(info["frames"], 226)

    def test_spec_226_rejects_274(self):
        with self.assertRaises(RuntimeError) as ctx:
            hc.check_remote_output(_output_check(frames=274), "A", _spec())
        self.assertIn("expected 226 frames", str(ctx.exception))

    def test_fps_tolerance(self):
        # Container fps 23.98 is within the 0.5 tolerance of spec 24.
        hc.check_remote_output(_output_check(fps=23.98), "A", _spec())
        with self.assertRaises(RuntimeError):
            hc.check_remote_output(_output_check(fps=30.0), "A", _spec())

    def test_non_default_resolution_passes(self):
        spec = _spec(out_w=720, out_h=1280)
        info = hc.check_remote_output(
            _output_check(width=720, height=1280), "A", spec)
        self.assertEqual((info["width"], info["height"]), (720, 1280))

    def test_non_default_resolution_rejects_ab_shape(self):
        spec = _spec(out_w=720, out_h=1280)
        with self.assertRaises(RuntimeError):
            hc.check_remote_output(_output_check(), "A", spec)

    def test_legacy_no_spec_still_expects_ab_shape(self):
        hc.check_remote_output(_output_check(274, 24.0, 1080, 1920), "A")
        with self.assertRaises(RuntimeError):
            hc.check_remote_output(_output_check(), "A")


class ValidateDeliveryInputsTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.out_root = self.root / "output"
        self.runs_root = self.root / "runs"
        self.out_root.mkdir(parents=True)
        self.runs_root.mkdir(parents=True)
        self._patches = [
            mock.patch.object(hc, "ROOT", self.root),
            mock.patch.object(hc, "OUTPUT_ROOT", self.out_root),
            mock.patch.object(hc, "RUNS", self.runs_root),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self.td.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)

    def _write_mp4(self, run_id, variant="A", content=b"fake-mp4-226"):
        d = self.out_root / run_id
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{variant}.mp4"
        path.write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    def _video_info(self, sha, frames=226, width=1080, height=1920, fps=24.0):
        return {"frame_count": frames, "width": width, "height": height,
                "fps": fps, "sha256": sha}

    def _manifest_with_spec(self, run_id, spec):
        rm.create_run_manifest(self.root, spec.task_id, run_id,
                               variant=spec.output_prefix, budget={})
        rm.update_run(self.root, run_id,
                      artifacts={"task_spec": spec.to_dict()})

    def test_spec_arg_226_passes(self):
        run_id = "run-226"
        _, sha = self._write_mp4(run_id)
        self._manifest_with_spec(run_id, _spec())
        path = hc._validate_delivery_inputs(
            "A", run_id, self._video_info(sha), _spec())
        self.assertTrue(path.is_file())

    def test_manifest_spec_rebuilt_when_no_spec_arg(self):
        # No spec passed: the run's durable manifest supplies it.
        run_id = "run-226b"
        _, sha = self._write_mp4(run_id)
        self._manifest_with_spec(run_id, _spec())
        path = hc._validate_delivery_inputs(
            "A", run_id, self._video_info(sha))
        self.assertTrue(path.is_file())

    def test_legacy_manifest_falls_back_to_274(self):
        # A pre-TaskSpec manifest (variant A, no task_spec): the A/B
        # fixture adapter rebuilds the frozen 274-frame shape.
        run_id = "run-legacy"
        _, sha = self._write_mp4(run_id)
        rm.create_run_manifest(self.root, "h3-ab-20261111", run_id,
                               variant="A", budget={})
        with self.assertRaises(RuntimeError) as ctx:
            hc._validate_delivery_inputs(
                "A", run_id, self._video_info(sha, frames=226))
        self.assertIn("expected 274 frames", str(ctx.exception))
        # The same file with 274-frame info passes the legacy gate.
        hc._validate_delivery_inputs(
            "A", run_id, self._video_info(sha, frames=274))

    def test_non_default_resolution_passes(self):
        run_id = "run-720p"
        _, sha = self._write_mp4(run_id)
        spec = _spec(out_w=720, out_h=1280)
        self._manifest_with_spec(run_id, spec)
        hc._validate_delivery_inputs(
            "A", run_id, self._video_info(sha, width=720, height=1280),
            spec)

    def test_fps_mismatch_refused(self):
        run_id = "run-fps"
        _, sha = self._write_mp4(run_id)
        self._manifest_with_spec(run_id, _spec())
        with self.assertRaises(RuntimeError) as ctx:
            hc._validate_delivery_inputs(
                "A", run_id, self._video_info(sha, fps=30.0), _spec())
        self.assertIn("fps", str(ctx.exception))

    def test_sha_mismatch_still_refused(self):
        run_id = "run-sha"
        _, sha = self._write_mp4(run_id)
        self._manifest_with_spec(run_id, _spec())
        with self.assertRaises(RuntimeError):
            hc._validate_delivery_inputs(
                "A", run_id, self._video_info("00" * 32), _spec())


class MainExceptionTailTest(unittest.TestCase):
    """main()'s except handler must not rewrite an already-terminal
    manifest (COMPLETED_AWAITING_ARCHIVE / TASK_COMPLETED) to FAILED."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(parents=True)
        (self.root / "logs").mkdir(parents=True)
        self._patches = [
            mock.patch.object(hc, "ROOT", self.root),
            mock.patch.object(hc, "RUNS", self.runs_root),
            mock.patch.object(hc, "OUTPUT_ROOT", self.root / "output"),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self.td.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)

    def _seed(self, run_id, state):
        rm.create_run_manifest(self.root, "h3-t008-main-tail", run_id,
                               variant="A", budget={})
        if state == "TASK_COMPLETED":
            # TASK_COMPLETED is only reachable through the legal stage
            # chain (update_run silently ignores an illegal jump from
            # TASK_AUTHORIZED).
            rm.update_run(self.root, run_id, session_id="muse-h3-aaaa0001",
                          session_state="ACTIVE", task_state="GPU_ALLOCATED")
            rm.update_run(self.root, run_id, task_state="MODELS_READY")
            rm.update_run(self.root, run_id, task_state="VIDEO_VALIDATED")
            rm.update_run(self.root, run_id, task_state="GPU_STOP_CONFIRMED",
                          session_state="STOPPED")
            rm.update_run(self.root, run_id, task_state="DRIVE_VERIFIED")
            rm.update_run(self.root, run_id, task_state="TASK_COMPLETED")
        else:
            rm.update_run(self.root, run_id, task_state=state)

    def _run_main_expect_exit_1(self, run_id):
        # `run` with task id + variant passes main()'s argparse gates,
        # then run_variant refuses (no --execute/--confirm-gpu live
        # flags) before any GPU exists; main() must exit 1 via its
        # except handler.
        argv = ["h3_controller.py", "run", "--run-id", run_id,
                "--production-task-id", "h3-t008-main-tail",
                "--variant", "A"]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                hc.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_completed_awaiting_archive_not_rewritten(self):
        run_id = "run-awaiting"
        self._seed(run_id, "COMPLETED_AWAITING_ARCHIVE")
        self._run_main_expect_exit_1(run_id)
        state = rm.load_run_manifest(self.root, run_id)["task_state"]
        self.assertEqual(state, "COMPLETED_AWAITING_ARCHIVE")

    def test_task_completed_not_rewritten(self):
        run_id = "run-done"
        self._seed(run_id, "TASK_COMPLETED")
        self._run_main_expect_exit_1(run_id)
        state = rm.load_run_manifest(self.root, run_id)["task_state"]
        self.assertEqual(state, "TASK_COMPLETED")

    def test_non_terminal_still_marked_failed(self):
        run_id = "run-open"
        self._seed(run_id, "VIDEO_VALIDATED")
        self._run_main_expect_exit_1(run_id)
        state = rm.load_run_manifest(self.root, run_id)["task_state"]
        self.assertEqual(state, "FAILED")


if __name__ == "__main__":
    unittest.main()
