"""Tests for per-task 30 CU budget isolation (TASK-002 §5).

Rule (user correction 2026-11-11 09:12, unchangeable): every newly
created video production task has its own independent 30.00 CU budget.
A and B variants plus all retries of one task share that task's 30 CU.
A new task id alone never grants a fresh budget — creation needs
explicit user authorization (--authorize-new-task) — and the historical
default task id h3-ab-20261111 is never reused silently for a new run.

No GPU, no Drive, no live balance: ledgers live in temp dirs.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from h3_budget import (  # noqa: E402
    allowed_runtime_seconds,
    effective_hard_stop_cu,
    ledger_path_for_task,
    load_or_create_budget_ledger,
    parse_task_deadline,
    reconcile_budget,
    remaining_project_cu,
)
import h3_controller as hc  # noqa: E402
import h3_session_watchdog as wd  # noqa: E402


def _ledger(root, task_id, balance, run_id, **kw):
    path = ledger_path_for_task(Path(root), task_id)
    return load_or_create_budget_ledger(path, balance, run_id, **kw)


class TaskBudgetTest(unittest.TestCase):
    def test_new_task_requires_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as d:
            path = ledger_path_for_task(Path(d), "h3-ab-20261112")
            with self.assertRaisesRegex(RuntimeError, "NEW_TASK_REQUIRES_AUTHORIZATION"):
                load_or_create_budget_ledger(path, 144.19, "run-1")
            self.assertFalse(path.exists())
            led = load_or_create_budget_ledger(path, 144.19, "run-1", authorize_new_task=True)
            self.assertTrue(led["authorized"])
            self.assertEqual(led["authorization"], "user-explicit --authorize-new-task")
            self.assertEqual(led["project_budget_cu"], 30.0)
            # Loading the now-existing ledger needs no new authorization.
            again = load_or_create_budget_ledger(path, 144.19, "run-2")
            self.assertEqual(again["initial_balance_cu"], 144.19)

    def test_new_task_uses_independent_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            old = _ledger(d, "h3-ab-20261111", 162.77, "run-old", authorize_new_task=True)
            new = _ledger(d, "h3-ab-20261112", 144.19, "run-new", authorize_new_task=True)
            self.assertEqual(old["initial_balance_cu"], 162.77)
            self.assertEqual(new["initial_balance_cu"], 144.19)
            self.assertEqual(new["hard_stop_balance_cu"], 114.19)
            # Spending on the old task does not touch the new ledger.
            reconcile_budget(old, 160.00, ledger_path_for_task(Path(d), "h3-ab-20261111"))
            fresh_new = load_or_create_budget_ledger(
                ledger_path_for_task(Path(d), "h3-ab-20261112"), 144.19, "run-new-2")
            self.assertEqual(fresh_new["max_observed_spent_cu"], 0.0)
            self.assertEqual(remaining_project_cu(fresh_new), 30.0)

    def test_same_task_A_B_share_budget(self):
        with tempfile.TemporaryDirectory() as d:
            path = ledger_path_for_task(Path(d), "h3-ab-20261112")
            a = load_or_create_budget_ledger(path, 165.27, "h3-run-A", authorize_new_task=True)
            reconcile_budget(a, 160.00, path)  # A spent 5.27 CU
            b = load_or_create_budget_ledger(path, 160.00, "h3-run-B")
            self.assertEqual(b["initial_balance_cu"], 165.27)
            self.assertEqual(b["hard_stop_balance_cu"], 135.27)
            self.assertAlmostEqual(b["max_observed_spent_cu"], 5.27)
            self.assertAlmostEqual(remaining_project_cu(b), 24.73)
            self.assertIn("h3-run-A", b["run_ids"])
            self.assertIn("h3-run-B", b["run_ids"])

    def test_retry_does_not_reset_budget(self):
        with tempfile.TemporaryDirectory() as d:
            path = ledger_path_for_task(Path(d), "h3-ab-20261112")
            first = load_or_create_budget_ledger(path, 165.27, "run-1", authorize_new_task=True)
            reconcile_budget(first, 160.00, path)
            retry = load_or_create_budget_ledger(path, 160.00, "run-2")
            self.assertEqual(retry["initial_balance_cu"], 165.27)
            self.assertEqual(retry["hard_stop_balance_cu"], 135.27)
            self.assertAlmostEqual(retry["max_observed_spent_cu"], 5.27)

    def test_old_task_budget_not_added(self):
        # A new task baselines on the LIVE balance, never on the old
        # task's baseline plus another 30 CU.
        with tempfile.TemporaryDirectory() as d:
            old = _ledger(d, "h3-ab-20261111", 162.77, "run-old", authorize_new_task=True)
            self.assertEqual(old["hard_stop_balance_cu"], 132.77)
            new = _ledger(d, "h3-ab-20261112", 144.19, "run-new", authorize_new_task=True)
            self.assertEqual(new["initial_balance_cu"], 144.19)
            self.assertEqual(new["hard_stop_balance_cu"], 114.19)
            self.assertEqual(new["project_budget_cu"], 30.0)

    def test_prior_unbilled_cost_reserved(self):
        with tempfile.TemporaryDirectory() as d:
            path = ledger_path_for_task(Path(d), "h3-ab-20261112")
            led = load_or_create_budget_ledger(
                path, 162.77, "run-1", prior_unbilled_reserve_cu=0.14,
                authorize_new_task=True)
            self.assertEqual(led["hard_stop_balance_cu"], 132.77)
            self.assertEqual(effective_hard_stop_cu(led), 132.91)
            with self.assertRaises(RuntimeError):
                reconcile_budget(led, 132.85, path)

    def test_default_historical_task_id_not_used_for_new_task(self):
        # (a) The shell entry point refuses without --production-task-id.
        r = subprocess.run(
            ["bash", str(ROOT / "scripts" / "run_h3_ab.sh"),
             "--confirm-live", "--variant", "A", "--run-id", "h3-20261112T000000Z-a1b2c3"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--production-task-id is required", r.stderr)
        # (b) The controller's run parser refuses without the flag
        # (before any live work: parser.error exits 2).
        r = subprocess.run(
            [sys.executable, str(ROOT / "h3_controller.py"), "run",
             "--variant", "A", "--run-id", "h3-20261112T000000Z-a1b2c3"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--production-task-id is required", r.stderr)
        # (c) run_variant itself refuses a namespace without a task id,
        # even before the GPU flags are checked.
        args = SimpleNamespace(variant="A", run_id="h3-x", production_task_id="")
        with self.assertRaisesRegex(RuntimeError, "--production-task-id is required"):
            hc.run_variant(args)

    def test_session_budget_limit_enforced(self):
        with tempfile.TemporaryDirectory() as d:
            path = ledger_path_for_task(Path(d), "h3-ab-20261112")
            led = load_or_create_budget_ledger(path, 165.27, "run-1", authorize_new_task=True)
            # A session that already spent its full 25 CU gets no time,
            # even though the project still has 30 CU.
            with self.assertRaises(RuntimeError):
                allowed_runtime_seconds(8.88, 160.00, led, session_spent_cu=25.0)
            # 20 CU spent still leaves bounded time under the session cap.
            seconds = allowed_runtime_seconds(8.88, 160.00, led, session_spent_cu=20.0)
            self.assertGreater(seconds, 300)
            self.assertLessEqual(seconds, 14400)

    def test_watchdog_respects_task_id(self):
        # (a) The session record carries the task it belongs to.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sessions.json"
            rec_a = wd.record_session("muse-h3-aaa", "G4", "run-a", "2026-11-12T02:00:00+00:00",
                                      path=p, production_task_id="h3-ab-20261111")
            rec_b = wd.record_session("muse-h3-bbb", "G4", "run-b", "2026-11-12T02:00:00+00:00",
                                      path=p, production_task_id="h3-ab-20261112")
            self.assertEqual(rec_a["production_task_id"], "h3-ab-20261111")
            self.assertEqual(rec_b["production_task_id"], "h3-ab-20261112")
            stored = wd.load_managed_sessions(path=p)
            by_id = {s["session_id"]: s for s in stored}
            self.assertEqual(by_id["muse-h3-aaa"]["production_task_id"], "h3-ab-20261111")
            self.assertEqual(by_id["muse-h3-bbb"]["production_task_id"], "h3-ab-20261112")
        # (b) The watch CLI binds its ledger to the task id it is given.
        argv = ["h3_session_watchdog.py", "watch", "--session", "muse-h3-aaa",
                "--production-task-id", "h3-ab-20261112"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(wd, "watch", return_value=0) as fake_watch:
            self.assertEqual(wd.main(), 0)
            fake_watch.assert_called_once()
            self.assertIn("h3-ab-20261112", str(wd.LEDGER_PATH))


class TaskDeadlineTest(unittest.TestCase):
    """TASK-006 P1-C (Issue #50): the task deadline on the ledger."""

    def test_new_ledger_without_deadline_records_none(self):
        with tempfile.TemporaryDirectory() as d:
            data = _ledger(d, "h3-ab-20261113", 150.0, "run-1",
                           authorize_new_task=True)
            self.assertIsNone(data["deadline_utc"])

    def test_new_ledger_records_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            data = _ledger(d, "h3-ab-20261113", 150.0, "run-1",
                           authorize_new_task=True,
                           deadline_utc="2026-11-13T20:00:00Z")
            self.assertEqual(data["deadline_utc"], "2026-11-13T20:00:00Z")

    def test_existing_ledger_adopts_first_deadline_then_refuses_a_move(self):
        with tempfile.TemporaryDirectory() as d:
            _ledger(d, "h3-ab-20261113", 150.0, "run-1",
                    authorize_new_task=True)
            data = _ledger(d, "h3-ab-20261113", 150.0, "run-2",
                           deadline_utc="2026-11-13T20:00:00Z")
            self.assertEqual(data["deadline_utc"], "2026-11-13T20:00:00Z")
            with self.assertRaisesRegex(RuntimeError, "deadline mismatch"):
                _ledger(d, "h3-ab-20261113", 150.0, "run-3",
                        deadline_utc="2026-11-14T20:00:00Z")

    def test_same_deadline_replayed_is_fine(self):
        with tempfile.TemporaryDirectory() as d:
            _ledger(d, "h3-ab-20261113", 150.0, "run-1",
                    authorize_new_task=True,
                    deadline_utc="2026-11-13T20:00:00Z")
            data = _ledger(d, "h3-ab-20261113", 150.0, "run-2",
                           deadline_utc="2026-11-13T20:00:00Z")
            self.assertEqual(data["deadline_utc"], "2026-11-13T20:00:00Z")

    def test_parse_task_deadline(self):
        self.assertIsNone(parse_task_deadline(None))
        self.assertIsNone(parse_task_deadline(""))
        self.assertEqual(parse_task_deadline("2026-11-13T20:00:00Z"),
                         "2026-11-13T20:00:00Z")
        with self.assertRaises(ValueError):
            parse_task_deadline("2026-11-13 20:00")
        with self.assertRaises(ValueError):
            parse_task_deadline("tomorrow")


if __name__ == "__main__":
    unittest.main()
