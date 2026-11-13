"""TASK-005 §4.1 (Issue #26): durable run manifest.

Every production run owns a persistent record that survives a controller
restart: atomic writes, idempotent updates, append-only audit log, task
state and GPU session state modelled separately, no stage ever inferred
from log text. Terminal states never move except through an explicit
resume + reopen.

No GPU, no live CLI: the manifest lives in a temp dir.
"""
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
import h3_run_manifest as rm  # noqa: E402
from h3_session_watchdog import SessionState  # noqa: E402

RUN_ID = "h3-20261113T000000Z-test01"
TASK = "h3-ab-20261111"


def _create(root):
    return rm.create_run_manifest(
        root, TASK, RUN_ID, variant="A", code_commit="abc1234",
        workflow_sha256="w" * 64, model_manifest_sha256="m" * 64,
        budget={"project_budget_cu": 30.0, "spent_cu": 0.0,
                "hard_stop_balance_cu": 132.77})


class CreateTest(unittest.TestCase):
    def test_schema_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            m = _create(root)
            self.assertEqual(m["task_state"], "TASK_AUTHORIZED")
            self.assertEqual(m["session_state"], "UNKNOWN")
            self.assertEqual(m["recovery_generation"], 0)
            self.assertEqual(m["last_verified_stage"], "TASK_AUTHORIZED")
            self.assertIsNone(m["last_error"])
            self.assertEqual(m["artifacts"], {})
            path = root / "model_cache" / "run_manifests" / f"{RUN_ID}.json"
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text())["run_id"], RUN_ID)
            audit = rm.read_audit_log(root, RUN_ID)
            self.assertEqual([e["event"] for e in audit], ["RUN_CREATED"])

    def test_duplicate_create_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _create(root)
            with self.assertRaises(FileExistsError):
                _create(root)


class UpdateTest(unittest.TestCase):
    def test_forward_moves_apply_and_audit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _create(root)
            m, applied, anomaly = rm.update_run(
                root, RUN_ID, task_state="GPU_ALLOCATED",
                session_id="muse-h3-x", session_state=SessionState.ACTIVE)
            self.assertTrue(applied)
            self.assertIsNone(anomaly)
            self.assertEqual(m["task_state"], "GPU_ALLOCATED")
            self.assertEqual(m["session_id"], "muse-h3-x")
            self.assertEqual(m["session_state"], "ACTIVE")
            events = [e["event"] for e in rm.read_audit_log(root, RUN_ID)]
            self.assertIn("STAGE", events)

    def test_backward_move_refused_and_logged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _create(root)
            rm.update_run(root, RUN_ID, task_state="GPU_ALLOCATED")
            m, applied, anomaly = rm.update_run(root, RUN_ID, task_state="PREFLIGHT")
            self.assertFalse(applied)
            self.assertIn("backward", anomaly)
            self.assertEqual(m["task_state"], "GPU_ALLOCATED")
            anomalies = [e for e in rm.read_audit_log(root, RUN_ID)
                         if e["event"] == "ANOMALY"]
            self.assertEqual(len(anomalies), 1)

    def test_completed_requires_drive_verified(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _create(root)
            rm.update_run(root, RUN_ID, task_state="GPU_ALLOCATED")
            m, applied, anomaly = rm.update_run(root, RUN_ID, task_state="TASK_COMPLETED")
            self.assertFalse(applied)
            self.assertIn("Drive must be verified", anomaly)
            self.assertEqual(m["task_state"], "GPU_ALLOCATED")

    def test_terminal_never_moves_without_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _create(root)
            rm.update_run(root, RUN_ID, task_state="FAILED", last_error="boom")
            m, applied, anomaly = rm.update_run(root, RUN_ID, task_state="GPU_ALLOCATED")
            self.assertFalse(applied)
            self.assertIn("terminal", anomaly)
            self.assertEqual(m["task_state"], "FAILED")

    def test_idempotent_reapply_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _create(root)
            rm.update_run(root, RUN_ID, task_state="GPU_ALLOCATED",
                          session_id="muse-h3-x")
            before = rm.load_run_manifest(root, RUN_ID)
            m, applied, anomaly = rm.update_run(root, RUN_ID, task_state="GPU_ALLOCATED",
                                                session_id="muse-h3-x")
            self.assertFalse(applied)
            self.assertIsNone(anomaly)
            self.assertEqual(m["updated_at_utc"], before["updated_at_utc"])

    def test_last_verified_stage_only_moves_forward(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _create(root)
            rm.update_run(root, RUN_ID, last_verified_stage="INFERENCE_COMPLETED")
            m, _, _ = rm.update_run(root, RUN_ID, last_verified_stage="GPU_ALLOCATED")
            self.assertEqual(m["last_verified_stage"], "INFERENCE_COMPLETED")

    def test_artifacts_and_budget_merge(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _create(root)
            rm.update_run(root, RUN_ID, artifacts={"local_sha256": "aa"})
            m, _, _ = rm.update_run(root, RUN_ID, artifacts={"drive_file_id": "f1"},
                                    budget={"spent_cu": 2.5})
            self.assertEqual(m["artifacts"]["local_sha256"], "aa")
            self.assertEqual(m["artifacts"]["drive_file_id"], "f1")
            self.assertEqual(m["budget"]["spent_cu"], 2.5)
            self.assertEqual(m["budget"]["project_budget_cu"], 30.0)

    def test_missing_manifest_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                rm.update_run(Path(d), "nope", task_state="GPU_ALLOCATED")


class ResumeTest(unittest.TestCase):
    def _to_state(self, root, state):
        _create(root)
        if state != "TASK_AUTHORIZED":
            rm.update_run(root, RUN_ID, task_state=state)

    def test_resume_bumps_generation_and_reports_view(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._to_state(root, "VIDEO_VALIDATED")
            view = rm.resume_run(root, RUN_ID)
            self.assertEqual(view["manifest"]["recovery_generation"], 1)
            self.assertTrue(view["resumable"])
            self.assertEqual(view["current_stage"], "VIDEO_VALIDATED")
            self.assertEqual(view["next_stage"], "ARTIFACT_DURABLE")
            self.assertIn("GPU_ALLOCATED", view["completed_stages"])
            self.assertNotIn("VIDEO_VALIDATED", view["completed_stages"])
            events = [e["event"] for e in rm.read_audit_log(root, RUN_ID)]
            self.assertIn("RESUMED", events)

    def test_completed_is_not_resumable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._to_state(root, "DRIVE_VERIFIED")
            rm.update_run(root, RUN_ID, task_state="TASK_COMPLETED")
            view = rm.resume_run(root, RUN_ID)
            self.assertFalse(view["resumable"])
            self.assertIn("already completed", view["reason"])

    def test_awaiting_archive_reason_names_archive_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._to_state(root, "VIDEO_VALIDATED")
            rm.update_run(root, RUN_ID, task_state="COMPLETED_AWAITING_ARCHIVE")
            view = rm.resume_run(root, RUN_ID)
            self.assertIn("archive-only", view["reason"])

    def test_recovery_view_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._to_state(root, "GPU_ALLOCATED")
            before = rm.load_run_manifest(root, RUN_ID)
            rm.recovery_view(root, RUN_ID)
            after = rm.load_run_manifest(root, RUN_ID)
            self.assertEqual(before, after)


class ListTest(unittest.TestCase):
    def test_list_runs_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rm.create_run_manifest(root, TASK, "run-b", variant="B")
            rm.create_run_manifest(root, TASK, "run-a", variant="A")
            runs = rm.list_runs(root)
            self.assertEqual([r["run_id"] for r in runs], ["run-b", "run-a"])


class StatusActionTest(unittest.TestCase):
    def test_status_prints_recovery_view_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _create(root)
            rm.update_run(root, RUN_ID, task_state="GPU_ALLOCATED",
                          session_id="muse-h3-x", session_state="ACTIVE")
            args = SimpleNamespace(action="status", run_id=RUN_ID)
            buf = io.StringIO()
            with mock.patch.object(hc, "ROOT", root), redirect_stdout(buf):
                hc.test_or_run(args)
            view = json.loads(buf.getvalue())
            self.assertEqual(view["current_stage"], "GPU_ALLOCATED")
            self.assertTrue(view["resumable"])
            # Read-only: recovery_generation untouched.
            self.assertEqual(rm.load_run_manifest(root, RUN_ID)["recovery_generation"], 0)

    def test_status_unknown_run_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            args = SimpleNamespace(action="status", run_id="nope")
            with mock.patch.object(hc, "ROOT", root):
                with self.assertRaises(FileNotFoundError):
                    hc.test_or_run(args)


if __name__ == "__main__":
    unittest.main()
