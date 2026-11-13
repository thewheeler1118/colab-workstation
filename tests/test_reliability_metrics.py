"""TASK-005 §12 (Issue #40): reliability metrics from durable evidence.

Constructed manifests via the real h3_run_manifest module on temp
roots — no GPU, no CU, no Drive. The production sample for the new
architecture is currently n = 0, so the empty-root case (all UNKNOWN)
is the honest baseline, and the 95% target must stay unverified until
a real sample exists.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import h3_reliability as rel
import h3_run_manifest as rm

TASK = "h3-ab-20261111"
OTHER_TASK = "h3-ab-20261112"


def _ledger(root, task, spent, budget=30.0):
    path = root / "model_cache" / f"h3_production_budget_{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "production_task_id": task, "project_budget_cu": budget,
        "hard_stop_balance_cu": 132.77, "max_observed_spent_cu": spent,
        "run_ids": []}))


def _run(root, run_id, task=TASK, variant="A", session=""):
    rm.create_run_manifest(root, task, run_id, variant=variant, budget={})
    if session:
        rm.update_run(root, run_id, session_id=session,
                      session_state="ACTIVE")
    return run_id


def _complete(root, run_id, resumed=False, stop_verified=True,
              mp4=False, sha=None):
    if resumed:
        rm.resume_run(root, run_id)
    if stop_verified:
        rm.update_run(root, run_id, task_state="GPU_STOP_CONFIRMED",
                      session_state="STOPPED")
    artifacts = {}
    if mp4:
        artifacts = {"local_sha256": sha,
                     "local_frames": 274,
                     "delivery_state": "COMPLETE_LOCAL"}
    rm.update_run(root, run_id, task_state="DRIVE_VERIFIED",
                  last_verified_stage="DRIVE_VERIFIED",
                  artifacts=artifacts or None)
    rm.update_run(root, run_id, task_state="TASK_COMPLETED")


def _fail(root, run_id):
    rm.update_run(root, run_id, task_state="FAILED",
                  last_error="RuntimeError: boom")


class ReliabilityMetricsTest(unittest.TestCase):
    def test_empty_root_all_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            report = rel.compute_reliability(d)
        self.assertEqual(report["sample_size"], 0)
        self.assertEqual(report["runs_sampled"], 0)
        self.assertIsNone(report["observation_dates"])
        for metric in report["metrics"].values():
            self.assertEqual(metric["status"], "UNKNOWN")
            self.assertIsNone(metric["rate"])
            self.assertIsNone(metric["confidence_interval_95"])
        self.assertFalse(report["target_95_verified"])
        self.assertIn("unverified", report["target_95_evidence"])

    def test_mixed_sample(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ledger(root, TASK, 18.58)
            sha = hashlib.sha256(b"fake-mp4").hexdigest()
            # First-attempt success, stop verified, MP4 produced.
            _run(root, "run-1", session="muse-h3-aaaa0001")
            _complete(root, "run-1", mp4=True, sha=sha)
            # Eventual success via resume, stop verified, no MP4 recorded.
            _run(root, "run-2", session="muse-h3-aaaa0002")
            _complete(root, "run-2", resumed=True)
            # Failure, stop never verified, MP4 produced.
            _run(root, "run-3", session="muse-h3-aaaa0003")
            rm.update_run(root, "run-3", artifacts={
                "local_sha256": sha, "local_frames": 274,
                "delivery_state": "COMPLETE_LOCAL"})
            _fail(root, "run-3")
            # In-flight: excluded from every denominator.
            _run(root, "run-4", session="muse-h3-aaaa0004")
            report = rel.compute_reliability(root)
        self.assertEqual(report["sample_size"], 3)
        self.assertEqual(report["runs_sampled"], 4)
        self.assertEqual(report["breakdown"],
                         {"completed": 2, "failed": 1,
                          "awaiting_archive": 0, "in_flight": 1})
        self.assertEqual(report["in_flight_run_ids"], ["run-4"])
        a = report["metrics"]["first_attempt_success_rate"]
        self.assertEqual((a["numerator"], a["denominator"]), (1, 3))
        self.assertEqual(a["status"], "INSUFFICIENT_SAMPLE")
        b = report["metrics"]["eventual_task_success_rate"]
        self.assertEqual((b["numerator"], b["denominator"]), (2, 3))
        c = report["metrics"]["artifact_preservation_rate"]
        self.assertEqual((c["numerator"], c["denominator"]), (2, 2))
        d = report["metrics"]["verified_shutdown_rate"]
        self.assertEqual((d["numerator"], d["denominator"]), (2, 3))
        e = report["metrics"]["budget_compliance_rate"]
        self.assertEqual((e["numerator"], e["denominator"]), (3, 3))
        self.assertFalse(report["target_95_verified"])

    def test_artifact_preservation_checks_local_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            out = root / "output"
            sha = hashlib.sha256(b"fake-mp4").hexdigest()
            # run-1: file present with matching SHA -> preserved.
            _run(root, "run-1", session="muse-h3-aaaa0001")
            mp4 = out / "run-1" / "A.mp4"
            mp4.parent.mkdir(parents=True, exist_ok=True)
            mp4.write_bytes(b"fake-mp4")
            _complete(root, "run-1", mp4=True, sha=sha)
            # run-2: MP4 recorded but the file is gone -> not preserved.
            _run(root, "run-2", session="muse-h3-aaaa0002")
            _complete(root, "run-2", mp4=True, sha=sha)
            # run-3: never produced an MP4 -> excluded from C.
            _run(root, "run-3", session="muse-h3-aaaa0003")
            _complete(root, "run-3")
            report = rel.compute_reliability(root, output_root=out)
        c = report["metrics"]["artifact_preservation_rate"]
        self.assertEqual((c["numerator"], c["denominator"]), (1, 2))

    def test_budget_ledger_verdicts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _ledger(root, TASK, 18.58)          # compliant
            _ledger(root, OTHER_TASK, 31.0)     # over budget
            _run(root, "run-1", task=TASK)
            _complete(root, "run-1")
            _run(root, "run-2", task=OTHER_TASK)
            _complete(root, "run-2")
            _run(root, "run-3", task="h3-ab-20261113")  # no ledger
            _complete(root, "run-3")
            report = rel.compute_reliability(root)
        e = report["metrics"]["budget_compliance_rate"]
        self.assertEqual((e["numerator"], e["denominator"]), (1, 2))
        self.assertEqual(report["e_unknown_ledger_runs"], ["run-3"])

    def test_filters(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _run(root, "run-1", task=TASK)
            _complete(root, "run-1")
            _run(root, "run-2", task=OTHER_TASK)
            _complete(root, "run-2")
            by_task = rel.compute_reliability(root, production_task_id=TASK)
            self.assertEqual(by_task["sample_size"], 1)
            self.assertEqual(by_task["runs_sampled"], 1)
            created = rm.load_run_manifest(root, "run-1")["created_at_utc"]
            after = rel.compute_reliability(root, since="2999-01-01T00:00:00+00:00")
            self.assertEqual(after["sample_size"], 0)
            until2 = rm.load_run_manifest(root, "run-2")["created_at_utc"]
            before = rel.compute_reliability(root, until=until2)
            self.assertEqual(before["sample_size"], 2)

    def test_wilson_interval(self):
        self.assertIsNone(rel.wilson_interval(0, 0))
        lo, hi = rel.wilson_interval(2, 3)
        self.assertLess(lo, 2 / 3)
        self.assertGreater(hi, 2 / 3)
        # 60/60 only reaches a 0.9398 lower bound — not enough for 0.95.
        lo60, _ = rel.wilson_interval(60, 60)
        self.assertAlmostEqual(lo60, 0.9398, places=4)
        self.assertLess(lo60, 0.95)
        # 75/75 clears it.
        lo75, _ = rel.wilson_interval(75, 75)
        self.assertGreaterEqual(lo75, 0.95)
        # 29/30 does not clear it.
        lo29, _ = rel.wilson_interval(29, 30)
        self.assertLess(lo29, 0.95)

    def test_target_95_verified_only_with_a_real_sample(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(75):
                run_id = f"run-{i:03d}"
                _run(root, run_id, session=f"muse-h3-{i:010d}")
                _complete(root, run_id)
            report = rel.compute_reliability(root)
        b = report["metrics"]["eventual_task_success_rate"]
        self.assertEqual(b["status"], "MEASURED")
        self.assertEqual(b["rate"], 1.0)
        self.assertTrue(report["target_95_verified"])
        # One failure in the same sample breaks the verification.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(75):
                run_id = f"run-{i:03d}"
                _run(root, run_id, session=f"muse-h3-{i:010d}")
                if i == 0:
                    _fail(root, run_id)
                else:
                    _complete(root, run_id)
            report = rel.compute_reliability(root)
        self.assertFalse(report["target_95_verified"])

    def test_awaiting_archive_counts_as_not_completed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _run(root, "run-1", session="muse-h3-aaaa0001")
            rm.update_run(root, "run-1",
                          task_state="COMPLETED_AWAITING_ARCHIVE")
            report = rel.compute_reliability(root)
        self.assertEqual(report["breakdown"]["awaiting_archive"], 1)
        b = report["metrics"]["eventual_task_success_rate"]
        self.assertEqual((b["numerator"], b["denominator"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
