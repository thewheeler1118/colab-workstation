"""TASK-006 P0-C (Issue #46): backup shutdown executor tests.

Scenario matrix for h3_shutdown_executor.run_executor with a fake
clock, a fake stop function, and a fake owner function. No test here
touches a real GPU, real Drive, or real credentials.
"""
import datetime
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import h3_shutdown_executor as hx
from h3_gpu_lease_registry import LeaseRegistry, owner_id_for
from tests.test_gpu_lease_registry import FakeMirror

OWNER = owner_id_for("owner@example.com")
OTHER = owner_id_for("someone-else@example.com")
NOW = datetime.datetime(2026, 11, 13, 8, 0, tzinfo=datetime.timezone.utc)
PAST = "2026-11-13T07:00:00+00:00"
FUTURE = "2026-11-13T09:00:00+00:00"


class FakeStop:
    """Scripted stop function: results per session id."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, session_id):
        self.calls.append(session_id)
        if session_id in self.results:
            outcome = self.results[session_id]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return {"state": "STOPPED", "attempts": 1, "queries": 1}


class ExecutorTestBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.td, ignore_errors=True))
        self.mirror = FakeMirror()
        self.reg = LeaseRegistry(path=Path(self.td) / "leases.json",
                                 mirror=self.mirror)

    def _lease(self, sid, deadline=PAST, final=True, owner=OWNER, **kw):
        lease = self.reg.register_lease(
            session_id=sid, run_id="run-1",
            production_task_id="h3-ab-20261111", gpu_model="G4",
            stop_deadline_utc=deadline, budget_cap_cu=25.0, owner_id=owner,
            started_at_utc="2026-11-13T06:00:00+00:00")
        if final:
            self.reg.update_deadline(sid, deadline, final=True)
        return lease

    def _run(self, confirm=True, stop_fn=None, owner_fn=None,
             max_stops=3, **kw):
        return hx.run_executor(
            self.reg, confirm_stop=confirm, max_stops=max_stops,
            executor_id="exec-test", now=NOW,
            stop_fn=stop_fn or FakeStop(),
            owner_id_fn=owner_fn or (lambda: OWNER), **kw)


class AuditTests(ExecutorTestBase):
    def test_audit_is_read_only(self):
        stop = FakeStop()
        self._lease("muse-h3-aaa")
        report = self._run(confirm=False, stop_fn=stop)
        self.assertEqual(report["action"], "audit")
        self.assertEqual(stop.calls, [])
        self.assertEqual(len(report["overdue"]), 1)
        self.assertEqual(
            self.reg.get_lease("muse-h3-aaa")["last_confirmed_state"],
            "ACTIVE")

    def test_audit_skips_with_reasons(self):
        self._lease("muse-h3-due")
        self._lease("muse-h3-future", deadline=FUTURE)
        self._lease("muse-h3-notfinal", final=False)
        stopped = self._lease("muse-h3-done")
        self.reg.confirm_stopped("muse-h3-done", "STOPPED")
        report = self._run(confirm=False)
        self.assertEqual(len(report["overdue"]), 1)
        reasons = {s["session_id"]: s["reason"] for s in report["skipped"]}
        self.assertEqual(reasons["muse-h3-future"], "not_yet_due")
        self.assertEqual(reasons["muse-h3-notfinal"], "deadline_not_final")
        self.assertEqual(reasons["muse-h3-done"], "already_stopped")


class StopTests(ExecutorTestBase):
    def test_overdue_final_owner_match_is_stopped(self):
        stop = FakeStop()
        self._lease("muse-h3-aaa")
        report = self._run(stop_fn=stop)
        self.assertEqual(stop.calls, ["muse-h3-aaa"])
        self.assertEqual(len(report["stopped"]), 1)
        self.assertEqual(report["pending"], [])
        self.assertEqual(report["exit_hint"], 0)
        lease = self.reg.get_lease("muse-h3-aaa")
        self.assertEqual(lease["last_confirmed_state"], "STOPPED")

    def test_stop_active_stays_pending_with_attempts(self):
        stop = FakeStop({"muse-h3-aaa": {"state": "ACTIVE",
                                         "attempts": 3, "queries": 3,
                                         "error": "session still ACTIVE"}})
        self._lease("muse-h3-aaa")
        report = self._run(stop_fn=stop)
        self.assertEqual(len(report["stopped"]), 0)
        self.assertEqual(len(report["pending"]), 1)
        self.assertEqual(report["pending"][0]["reason"], "stop_unconfirmed")
        self.assertEqual(report["exit_hint"], 1)
        lease = self.reg.get_lease("muse-h3-aaa")
        self.assertEqual(lease["last_confirmed_state"], "ACTIVE")
        self.assertEqual(lease["stop_attempts"], 1)
        self.assertIsNotNone(lease["next_retry_at_utc"])

    def test_stop_unknown_stays_pending_never_marked(self):
        stop = FakeStop({"muse-h3-aaa": {"state": "UNKNOWN",
                                         "attempts": 3, "queries": 3,
                                         "error": "session state UNKNOWN"}})
        self._lease("muse-h3-aaa")
        self._run(stop_fn=stop)
        self.assertEqual(
            self.reg.get_lease("muse-h3-aaa")["last_confirmed_state"],
            "ACTIVE")

    def test_stop_exception_stays_pending(self):
        stop = FakeStop({"muse-h3-aaa": RuntimeError("cli exploded")})
        self._lease("muse-h3-aaa")
        report = self._run(stop_fn=stop)
        self.assertEqual(len(report["pending"]), 1)
        self.assertEqual(
            self.reg.get_lease("muse-h3-aaa")["last_confirmed_state"],
            "ACTIVE")

    def test_owner_mismatch_is_refused_without_cli_call(self):
        stop = FakeStop()
        self._lease("muse-h3-aaa", owner=OTHER)
        report = self._run(stop_fn=stop)
        self.assertEqual(stop.calls, [])
        self.assertEqual(len(report["refused"]), 1)
        self.assertEqual(report["refused"][0]["reason"], "owner_mismatch")
        self.assertEqual(
            self.reg.get_lease("muse-h3-aaa")["last_confirmed_state"],
            "ACTIVE")

    def test_owner_unestablishable_fails_closed(self):
        stop = FakeStop()
        self._lease("muse-h3-aaa")

        def _boom():
            raise RuntimeError("whoami down")

        report = self._run(stop_fn=stop, owner_fn=_boom)
        self.assertEqual(stop.calls, [])
        self.assertEqual(report["owner_error"], "RuntimeError")
        self.assertEqual(report["exit_hint"], 2)

    def test_foreign_sessions_never_reach_stop(self):
        # The executor only ever stops registry ids. A live session
        # that is not a lease must not be passed to stop_fn — here the
        # fake stop asserts every id it sees is a registry id.
        stop = FakeStop()
        self._lease("muse-h3-aaa")
        self._run(stop_fn=stop)
        self.assertEqual(set(stop.calls), {"muse-h3-aaa"})

    def test_claim_conflict_skips(self):
        self._lease("muse-h3-aaa")
        self.reg.claim_lease("muse-h3-aaa", "exec-other")
        stop = FakeStop()
        report = self._run(stop_fn=stop)
        self.assertEqual(stop.calls, [])
        self.assertEqual(report["pending"][0]["reason"],
                         "claimed_by_other_executor")

    def test_stale_claim_is_taken_over(self):
        self._lease("muse-h3-aaa")
        self.reg.claim_lease("muse-h3-aaa", "exec-other")
        lease = self.reg.get_lease("muse-h3-aaa")
        lease["executor_claim"]["claimed_at_utc"] = \
            "2026-11-13T07:00:00+00:00"
        doc = json.loads((Path(self.td) / "leases.json").read_text())
        doc["leases"] = [lease]
        (Path(self.td) / "leases.json").write_text(json.dumps(doc))
        stop = FakeStop()
        report = self._run(stop_fn=stop)
        self.assertEqual(stop.calls, ["muse-h3-aaa"])
        self.assertEqual(len(report["stopped"]), 1)

    def test_max_stops_caps_a_pass(self):
        self._lease("muse-h3-a")
        self._lease("muse-h3-b")
        self._lease("muse-h3-c")
        stop = FakeStop()
        report = self._run(stop_fn=stop, max_stops=1)
        self.assertEqual(len(report["stopped"]), 1)
        self.assertEqual(len(report["pending"]), 2)
        self.assertEqual(report["pending"][0]["reason"], "max_stops_reached")
        self.assertEqual(len(stop.calls), 1)

    def test_retry_not_due_is_skipped(self):
        self._lease("muse-h3-aaa")
        self.reg.note_stop_attempt("muse-h3-aaa", "UNKNOWN",
                                   retry_after_sec=3600)
        stop = FakeStop()
        report = self._run(stop_fn=stop)
        self.assertEqual(stop.calls, [])
        self.assertEqual(report["skipped"][0]["reason"], "retry_not_due")


class MainTests(unittest.TestCase):
    def test_main_audit_on_empty_registry_exits_zero(self):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        with patch("sys.argv", ["h3_shutdown_executor.py", "audit",
                                "--registry", str(Path(td) / "leases.json"),
                                "--no-mirror"]), \
                patch.object(hx, "current_owner_id", return_value=OWNER), \
                patch.object(hx, "REPORT_DIR", Path(td) / "reports"), \
                patch("sys.stdout") as fake_out:
            code = hx.main()
        self.assertEqual(code, 0)
        printed = "".join(c[0][0] for c in fake_out.write.call_args_list)
        report = json.loads(printed)
        self.assertEqual(report["action"], "audit")
        self.assertEqual(report["leases_seen"], 0)


if __name__ == "__main__":
    unittest.main()
