"""TASK-008 (Issue #57) 改造 4: forced heterogeneous-spec end-to-end chain.

(i) A no-tail 226-frame TaskSpec must pass the WHOLE offline chain:
    TaskSpec -> check_job_done_info -> check_remote_output ->
    download_and_validate_variant -> _validate_delivery_inputs ->
    Drive archive -> metric F success.
(ii) The same chain with a non-default legal output size (720x1280).

No GPU, no CU, no Drive: run_cli is faked to write a local file and
inspect_video is stubbed to report that file's real SHA-256.
"""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import h3_controller as hc
import h3_reliability as rel
import h3_run_manifest as rm
import h3_video_qa
from h3_task_spec import RefImage, TaskSpec

TASK = "h3-t008-chain"
SHA = "ab" * 32
FAKE_MP4 = b"fake-mp4-bytes-for-chain-test"


def _spec(task_id=TASK, tail=0, frames=226, fps=24,
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


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _ledger(root, task, spent=2.5, budget=30.0, deadline=None):
    path = root / "model_cache" / f"h3_production_budget_{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"production_task_id": task, "project_budget_cu": budget,
           "hard_stop_balance_cu": 132.77, "max_observed_spent_cu": spent,
           "run_ids": [], "authorized": True}
    if deadline is not None:
        doc["deadline_utc"] = deadline
    path.write_text(json.dumps(doc))


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        for sub in ("runs", "output", "logs", "model_cache"):
            (self.root / sub).mkdir()
        self._patches = [
            mock.patch.object(hc, "ROOT", self.root),
            mock.patch.object(hc, "OUTPUT_ROOT", self.root / "output"),
            mock.patch.object(hc, "RUNS", self.root / "runs"),
            mock.patch.object(hc.time, "sleep", lambda s: None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.td.cleanup)
        self.inspect_calls = []

        def _fake_inspect(path, expected_frames=None, expected_fps=None,
                          expected_width=None, expected_height=None):
            self.inspect_calls.append({
                "expected_frames": expected_frames,
                "expected_fps": expected_fps,
                "expected_width": expected_width,
                "expected_height": expected_height})
            return {"technical_pass": True,
                    "frame_count": expected_frames,
                    "fps": expected_fps,
                    "width": expected_width, "height": expected_height,
                    "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}

        mock.patch.object(h3_video_qa, "inspect_video",
                          side_effect=_fake_inspect).start()

        def _fake_run_cli(args, timeout=None):
            target = Path(args[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"{}" if target.suffix == ".json" else FAKE_MP4)
            return {"ok": True}

        mock.patch.object(hc, "run_cli", side_effect=_fake_run_cli).start()

    def _walk_to_video_validated(self, spec, run_id="run-1"):
        """Seed the manifest up to MODELS_READY (the pre-download stages).
        The spec is recorded at authorisation, exactly as _manifest_create
        does in production (h3_controller.py:1539)."""
        rm.create_run_manifest(self.root, TASK, run_id, variant="A", budget={})
        rm.update_run(self.root, run_id,
                      artifacts={"task_spec": spec.to_dict()})
        rm.update_run(self.root, run_id, session_id="muse-h3-chain0001",
                      session_state="ACTIVE", task_state="GPU_ALLOCATED")
        rm.update_run(self.root, run_id, task_state="WORKER_STARTED")
        rm.update_run(self.root, run_id, task_state="MODELS_READY")

    def _finish_run(self, spec, local_frames, run_id="run-1"):
        """VIDEO_VALIDATED -> stopped -> DRIVE_VERIFIED -> TASK_COMPLETED."""
        video_artifacts = {"local_sha256": SHA, "local_frames": local_frames,
                           "task_spec": spec.to_dict()}
        rm.update_run(self.root, run_id, task_state="VIDEO_VALIDATED",
                      artifacts=video_artifacts)
        rm.update_run(self.root, run_id, task_state="GPU_STOP_CONFIRMED",
                      session_state="STOPPED")
        rm.update_run(self.root, run_id, task_state="DRIVE_VERIFIED",
                      artifacts={"delivery_state": "DELIVERED",
                                 "drive_file_id": "drive-file-1"})
        rm.update_run(self.root, run_id, task_state="TASK_COMPLETED")
        stages = rel._stage_times(self.root, run_id)
        return stages["TASK_COMPLETED"][-1]

    def _assert_f_success(self, completed):
        deadline = _fmt(_parse(completed) + timedelta(hours=1))
        now = _fmt(_parse(completed) + timedelta(hours=2))
        _ledger(self.root, TASK, deadline=deadline)
        report = rel.compute_reliability(self.root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (1, 1))
        self.assertEqual(f["verdicts"][0]["verdict"], "success")
        self.assertEqual(f["verdicts"][0]["failed_conditions"], [])

    def _run_chain(self, spec, frames, width, height, pass_spec_to_delivery):
        """Drive one spec through every gate; return the delivery evidence."""
        self._walk_to_video_validated(spec)
        done = hc.check_job_done_info(_done_info(frames), "A", spec)
        self.assertEqual(done["frames"], frames)
        remote = hc.check_remote_output(
            _output_check(frames, 24.0, width, height), "A", spec)
        self.assertEqual((remote["frames"], remote["width"], remote["height"]),
                         (frames, width, height))
        info = hc.download_and_validate_variant("sess-1", "A", "run-1", spec)
        self.assertEqual(len(self.inspect_calls), 1)
        self.assertEqual(self.inspect_calls[0], {
            "expected_frames": frames, "expected_fps": 24,
            "expected_width": width, "expected_height": height})
        self.assertEqual((info["frame_count"], info["width"], info["height"]),
                         (frames, width, height))
        local_path = self.root / "output" / "run-1" / "A.mp4"
        self.assertTrue(local_path.is_file())
        self.assertEqual(info["sha256"],
                         hashlib.sha256(FAKE_MP4).hexdigest())
        # Delivery pre-check: spec=None proves the manifest-rebuilt spec
        # path; passing the spec proves the explicit path.
        delivery_spec = spec if pass_spec_to_delivery else None
        checked = hc._validate_delivery_inputs("A", "run-1", info,
                                               delivery_spec)
        self.assertEqual(checked, local_path)

        def _archive(variant, run_id):
            return {"output_folder_id": "folder-1",
                    "files": {"A.mp4": {"drive_file_id": "drive-file-1"}}}

        evidence = hc.deliver_variant_to_drive_idempotent(
            "A", "run-1", info, archive_fn=_archive, spec=spec)
        self.assertEqual(evidence["state"], "DELIVERY_COMPLETED")
        self.assertEqual(evidence["drive_file_id"], "drive-file-1")
        self.assertEqual(evidence["frames"], frames)
        return evidence

    def test_no_tail_226_full_chain(self):
        spec = _spec()
        self.assertEqual(spec.total_frames, 226)
        self._run_chain(spec, 226, 1080, 1920, pass_spec_to_delivery=False)
        completed = self._finish_run(spec, local_frames=226)
        self._assert_f_success(completed)

    def test_landscape_720x1280_full_chain(self):
        spec = _spec(out_w=720, out_h=1280)
        self.assertEqual(spec.total_frames, 226)
        self._run_chain(spec, 226, 720, 1280, pass_spec_to_delivery=True)
        completed = self._finish_run(spec, local_frames=226)
        self._assert_f_success(completed)


if __name__ == "__main__":
    unittest.main()
