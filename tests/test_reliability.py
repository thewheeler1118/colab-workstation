"""TASK-006 P1-C (Issue #50): deadline-bound end-to-end success rate (metric F).

Fixtures are built through the real h3_run_manifest module on temp
roots — no GPU, no CU, no Drive. The audit logs are real, so metric F
reads the same evidence it would read in production. Deadlines and the
"now" clock are computed relative to the actual completion timestamps
the fixtures produce, so the tests do not depend on the wall-clock
time they happen to run at.
"""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h3_reliability as rel
import h3_run_manifest as rm

TASK = "h3-f-task-1"
OTHER_TASK = "h3-f-task-2"
SHA = hashlib.sha256(b"fake-mp4").hexdigest()


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _ledger(root, task, spent=2.5, budget=30.0, deadline=None,
            authorized=True):
    path = root / "model_cache" / f"h3_production_budget_{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"production_task_id": task, "project_budget_cu": budget,
           "hard_stop_balance_cu": 132.77, "max_observed_spent_cu": spent,
           "run_ids": [], "authorized": authorized}
    if deadline is not None:
        doc["deadline_utc"] = deadline
    path.write_text(json.dumps(doc))


def _good_run(root, run_id, task=TASK, variant="A", local_frames=274,
              task_spec=None):
    """A run meeting all seven F conditions. Returns its completion ts.
    `local_frames` / `task_spec` let a spec-driven run (e.g. 226 frames,
    tail 0) prove the spec total — not the frozen 274 — is the gate."""
    rm.create_run_manifest(root, task, run_id, variant=variant, budget={})
    rm.update_run(root, run_id, session_id="muse-h3-aaaa0001",
                  session_state="ACTIVE", task_state="GPU_ALLOCATED")
    rm.update_run(root, run_id, task_state="WORKER_STARTED")
    rm.update_run(root, run_id, task_state="MODELS_READY")
    video_artifacts = {"local_sha256": SHA, "local_frames": local_frames}
    if task_spec is not None:
        video_artifacts["task_spec"] = task_spec
    rm.update_run(root, run_id, task_state="VIDEO_VALIDATED",
                  artifacts=video_artifacts)
    rm.update_run(root, run_id, task_state="GPU_STOP_CONFIRMED",
                  session_state="STOPPED")
    rm.update_run(root, run_id, task_state="DRIVE_VERIFIED",
                  artifacts={"delivery_state": "DELIVERED",
                             "drive_file_id": "drive-file-1"})
    rm.update_run(root, run_id, task_state="TASK_COMPLETED")
    stages = rel._stage_times(root, run_id)
    return stages["TASK_COMPLETED"][-1]


def _stuck_run(root, run_id, task=TASK):
    rm.create_run_manifest(root, task, run_id, variant="A", budget={})
    rm.update_run(root, run_id, session_id="muse-h3-aaaa0002",
                  session_state="ACTIVE", task_state="GPU_ALLOCATED")


class DeadlineBoundMetricTest(unittest.TestCase):
    def test_empty_root_f_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            report = rel.compute_reliability(d)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual(f["status"], "UNKNOWN")
        self.assertIsNone(f["rate"])
        self.assertEqual((f["numerator"], f["denominator"]), (0, 0))
        self.assertFalse(f["target_verified"])
        self.assertEqual(f["verdicts"], [])
        self.assertEqual(report["f_in_flight_tasks"], [])
        self.assertEqual(report["f_deadline_unknown_tasks"], [])
        self.assertEqual(report["f_time_window_excluded_tasks"], [])

    def test_success_task_counts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            deadline = _fmt(_parse(completed) + timedelta(hours=1))
            now = _fmt(_parse(completed) + timedelta(hours=2))
            _ledger(root, TASK, deadline=deadline)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (1, 1))
        self.assertEqual(f["rate"], 1.0)
        self.assertEqual(f["status"], "INSUFFICIENT_SAMPLE")
        # One sample can never verify a 95% target.
        self.assertFalse(f["target_verified"])
        self.assertEqual(len(f["verdicts"]), 1)
        self.assertEqual(f["verdicts"][0]["verdict"], "success")
        self.assertEqual(f["verdicts"][0]["failed_conditions"], [])
        self.assertEqual(f["verdicts"][0]["completed_at_utc"], completed)

    def test_completed_after_deadline_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            # The deadline passed one hour BEFORE the run completed.
            deadline = _fmt(_parse(completed) - timedelta(hours=1))
            now = _fmt(_parse(completed) + timedelta(hours=1))
            _ledger(root, TASK, deadline=deadline)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        self.assertEqual(f["verdicts"][0]["verdict"], "failure")
        self.assertTrue(any("after deadline" in c
                            for c in f["verdicts"][0]["failed_conditions"]))

    def test_in_flight_before_deadline_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _stuck_run(root, "run-1")
            now = datetime.now(timezone.utc)
            deadline = _fmt(now + timedelta(hours=2))
            _ledger(root, TASK, deadline=deadline)
            report = rel.compute_reliability(
                root, now_utc=_fmt(now + timedelta(hours=1)))
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 0))
        self.assertEqual(f["status"], "UNKNOWN")
        self.assertEqual(report["f_in_flight_tasks"], [TASK])

    def test_past_deadline_stuck_run_counts_as_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _stuck_run(root, "run-1")
            now = datetime.now(timezone.utc)
            deadline = _fmt(now - timedelta(hours=1))
            _ledger(root, TASK, deadline=deadline)
            report = rel.compute_reliability(root, now_utc=_fmt(now))
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        self.assertEqual(f["verdicts"][0]["verdict"], "failure")
        # TASK-008: past the deadline with nothing completed, the task
        # fails on (a) — no run fully meets the F conditions — not on
        # a blanket "not terminal" verdict.
        self.assertTrue(any("no run fully meets" in c
                            for c in f["verdicts"][0]["failed_conditions"]))

    def test_no_deadline_task_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _good_run(root, "run-1")
            # Authorised, but no deadline was ever recorded: not in F.
            _ledger(root, TASK, deadline=None)
            # A second task whose ledger was never authorised at all.
            _good_run(root, "run-2", task=OTHER_TASK)
            _ledger(root, OTHER_TASK, deadline="2026-11-13T10:00:00Z",
                    authorized=False)
            report = rel.compute_reliability(root)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 0))
        self.assertEqual(report["f_deadline_unknown_tasks"],
                         [TASK, OTHER_TASK])

    def test_budget_violation_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            deadline = _fmt(_parse(completed) + timedelta(hours=1))
            now = _fmt(_parse(completed) + timedelta(hours=2))
            _ledger(root, TASK, spent=31.0, deadline=deadline)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        self.assertTrue(any("budget" in c
                            for c in f["verdicts"][0]["failed_conditions"]))

    def test_failed_run_without_confirmed_stop_fails_the_task(self):
        # TASK-008 (b): a failed run whose session was never confirmed
        # STOPPED blocks the task — the failure is the unconfirmed
        # shutdown (a possible orphan billable GPU), not the failure
        # itself. A failed run WITH a confirmed stop does not block
        # (see test_retry_success_with_confirmed_stop_counts).
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            rm.create_run_manifest(root, TASK, "run-2", variant="B",
                                   budget={})
            rm.update_run(root, "run-2", session_id="muse-h3-aaaa0003",
                          session_state="ACTIVE", task_state="GPU_ALLOCATED")
            rm.update_run(root, "run-2", task_state="FAILED",
                          last_error="RuntimeError: boom")
            deadline = _fmt(_parse(completed) + timedelta(hours=1))
            now = _fmt(_parse(completed) + timedelta(hours=2))
            _ledger(root, TASK, deadline=deadline)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        conds = f["verdicts"][0]["failed_conditions"]
        self.assertTrue(any("run-2" in c and "shutdown" in c for c in conds))

    def test_retry_success_with_confirmed_stop_counts(self):
        # TASK-008 (a)+(b): the first run failed but its session is
        # confirmed STOPPED (GPU_STOP_CONFIRMED stage + session_state
        # STOPPED); the retry run fully succeeds before the deadline.
        # The task counts as a success — Metric A (not F) is where the
        # first-attempt failure is recorded.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rm.create_run_manifest(root, TASK, "run-1", variant="A",
                                   budget={})
            rm.update_run(root, "run-1", session_id="muse-h3-aaaa0003",
                          session_state="ACTIVE", task_state="GPU_ALLOCATED")
            rm.update_run(root, "run-1", task_state="GPU_STOP_CONFIRMED",
                          session_state="STOPPED")
            rm.update_run(root, "run-1", task_state="FAILED",
                          last_error="RuntimeError: boom")
            completed = _good_run(root, "run-2")
            deadline = _fmt(_parse(completed) + timedelta(hours=1))
            now = _fmt(_parse(completed) + timedelta(hours=2))
            _ledger(root, TASK, deadline=deadline)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (1, 1))
        self.assertEqual(f["verdicts"][0]["verdict"], "success")
        self.assertEqual(f["verdicts"][0]["failed_conditions"], [])
        self.assertEqual(f["verdicts"][0]["successful_run_ids"], ["run-2"])

    def test_spec_frames_drive_f(self):
        # TASK-008: a 226-frame no-tail run (task_spec recorded on the
        # manifest) counts with local_frames == 226 — the spec total,
        # not the frozen 274, is the F gate.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spec = {"animation_frames": 226, "tail_card_frames": 0}
            completed = _good_run(root, "run-1", local_frames=226,
                                  task_spec=spec)
            deadline = _fmt(_parse(completed) + timedelta(hours=1))
            now = _fmt(_parse(completed) + timedelta(hours=2))
            _ledger(root, TASK, deadline=deadline)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (1, 1))
        self.assertEqual(f["verdicts"][0]["verdict"], "success")

    def test_spec_frames_mismatch_fails_f(self):
        # The same 226-frame spec, but the run recorded 274 local
        # frames: the spec gate fails the run, and the task fails (a).
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spec = {"animation_frames": 226, "tail_card_frames": 0}
            completed = _good_run(root, "run-1", local_frames=274,
                                  task_spec=spec)
            deadline = _fmt(_parse(completed) + timedelta(hours=1))
            now = _fmt(_parse(completed) + timedelta(hours=2))
            _ledger(root, TASK, deadline=deadline)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        self.assertTrue(any("no run fully meets" in c
                            for c in f["verdicts"][0]["failed_conditions"]))

    def test_manifest_claim_without_audit_evidence_fails(self):
        # A manifest claiming TASK_COMPLETED whose audit log lacks the
        # GPU_STOP_CONFIRMED stage must NOT count: F reads the audit
        # evidence, not the manifest claim.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rm.create_run_manifest(root, TASK, "run-1", variant="A",
                                   budget={})
            rm.update_run(root, "run-1", session_id="muse-h3-aaaa0001",
                          session_state="ACTIVE", task_state="GPU_ALLOCATED")
            rm.update_run(root, "run-1", task_state="MODELS_READY")
            rm.update_run(root, "run-1", task_state="VIDEO_VALIDATED",
                          artifacts={"local_sha256": SHA,
                                     "local_frames": 274})
            # Hand-edit the manifest to claim completion without the
            # stop stage (the real update_run gate would refuse this).
            path = rm.manifest_dir(root) / "run-1.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["task_state"] = "TASK_COMPLETED"
            doc["session_state"] = "STOPPED"
            doc["artifacts"] = {"local_sha256": SHA, "local_frames": 274,
                                "delivery_state": "DELIVERED",
                                "drive_file_id": "drive-file-1"}
            path.write_text(json.dumps(doc))
            rm.record_audit_event(root, "run-1", "STAGE",
                                  {"from": "VIDEO_VALIDATED",
                                   "to": "DRIVE_VERIFIED"})
            rm.record_audit_event(root, "run-1", "STAGE",
                                  {"from": "DRIVE_VERIFIED",
                                   "to": "TASK_COMPLETED"})
            completed = rel._stage_times(root, "run-1")["TASK_COMPLETED"][-1]
            deadline = _fmt(_parse(completed) + timedelta(hours=1))
            now = _fmt(_parse(completed) + timedelta(hours=2))
            _ledger(root, TASK, deadline=deadline)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        self.assertTrue(any("shutdown" in c
                            for c in f["verdicts"][0]["failed_conditions"]))

    def test_no_runs_after_deadline_fails(self):
        # Authorised with a deadline, deadline passed, nothing started.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            now = datetime.now(timezone.utc)
            _ledger(root, TASK, deadline=_fmt(now - timedelta(hours=1)))
            report = rel.compute_reliability(root, now_utc=_fmt(now))
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        self.assertTrue(any("no runs started" in c
                            for c in f["verdicts"][0]["failed_conditions"]))

    def test_task_filter_scopes_f(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1", task=TASK)
            _good_run(root, "run-2", task=OTHER_TASK)
            deadline = _fmt(_parse(completed) + timedelta(hours=1))
            now = _fmt(_parse(completed) + timedelta(hours=2))
            _ledger(root, TASK, deadline=deadline)
            _ledger(root, OTHER_TASK, deadline=deadline)
            report = rel.compute_reliability(root, production_task_id=TASK,
                                             now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (1, 1))
        self.assertEqual([v["production_task_id"] for v in f["verdicts"]],
                         [TASK])

    def test_wilson_threshold_for_95(self):
        # The statistical threshold the design cites: 60/60 reaches
        # only a 0.9398 lower bound; 73/73 clears 0.95. This is a
        # threshold, not a plan to buy 73 GPU runs.
        self.assertLess(rel.wilson_interval(60, 60)[0], 0.95)
        self.assertAlmostEqual(rel.wilson_interval(60, 60)[0], 0.9398,
                               places=4)
        self.assertGreaterEqual(rel.wilson_interval(73, 73)[0], 0.95)


if __name__ == "__main__":
    unittest.main()
