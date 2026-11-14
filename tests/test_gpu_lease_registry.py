"""TASK-006 P0-C (Issue #46): GPU lease registry tests.

The registry is the backup executor's authority to stop a session: a
local file (required write) + a Drive mirror (best-effort, versioned,
merged on conflict). No test here touches a real GPU, real Drive, or
real credentials. The fake mirror is in-memory; the fake owner is a
hash, never an email address.
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import h3_gpu_lease_registry as lr
from h3_gpu_lease_registry import (
    DriveMirrorBackend,
    LeaseRegistry,
    MirrorConflictError,
    MirrorUnavailableError,
    owner_id_for,
)

OWNER = owner_id_for("owner@example.com")
OTHER_OWNER = owner_id_for("someone-else@example.com")
NOW = "2026-11-13T08:00:00+00:00"
DEADLINE = "2026-11-13T09:00:00+00:00"


class FakeMirror:
    """In-memory Drive mirror. `fail_writes` counts down failures;
    `concurrent_write` simulates another host writing between our read
    and our write (raises MirrorConflictError once, after applying the
    foreign doc)."""

    def __init__(self):
        self.doc = None
        self.fail_writes = 0
        self.concurrent_doc = None
        self.writes = 0

    def read_mirror(self):
        return json.loads(json.dumps(self.doc)) if self.doc else None

    def write_mirror(self, doc, expected_version):
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise MirrorUnavailableError("fake transport down")
        current_version = (self.doc or {}).get("version")
        if expected_version is not None and current_version is not None \
                and int(current_version) != int(expected_version):
            raise MirrorConflictError("version moved")
        if self.concurrent_doc is not None:
            foreign = self.concurrent_doc
            self.concurrent_doc = None
            self.doc = json.loads(json.dumps(foreign))
            raise MirrorConflictError("concurrent writer won")
        self.writes += 1
        written = json.loads(json.dumps(doc))
        written["version"] = max(int(doc.get("version") or 0),
                                 int(current_version or 0)) + 1
        written["mirror_file_id"] = "mirror-file-1"
        self.doc = written
        return {"version": written["version"],
                "mirror_file_id": "mirror-file-1"}


class RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.td, ignore_errors=True))
        self.mirror = FakeMirror()
        self.reg = LeaseRegistry(path=Path(self.td) / "leases.json",
                                 mirror=self.mirror)

    def _lease(self, sid="muse-h3-aaa", **kw):
        args = {"session_id": sid, "run_id": "run-1",
                "production_task_id": "h3-ab-20261111", "gpu_model": "G4",
                "stop_deadline_utc": DEADLINE, "budget_cap_cu": 25.0,
                "owner_id": OWNER, "started_at_utc": NOW}
        args.update(kw)
        return self.reg.register_lease(**args)


class RegisterTests(RegistryTestBase):
    def test_register_writes_local_and_mirror(self):
        lease = self._lease()
        self.assertEqual(lease["version"], 1)
        self.assertFalse(lease["deadline_final"])
        self.assertEqual(lease["last_confirmed_state"], "ACTIVE")
        self.assertEqual(lease["owner_id"], OWNER)
        local = json.loads((Path(self.td) / "leases.json").read_text())
        self.assertEqual(len(local["leases"]), 1)
        self.assertFalse(local["mirror_pending"])
        self.assertIsNotNone(self.mirror.doc)
        self.assertEqual(self.mirror.doc["leases"][0]["session_id"], "muse-h3-aaa")

    def test_no_email_anywhere_in_registry(self):
        self._lease()
        blob = (Path(self.td) / "leases.json").read_text()
        self.assertNotIn("owner@example.com", blob)
        self.assertNotIn("@", blob)
        mirror_blob = json.dumps(self.mirror.doc)
        self.assertNotIn("@", mirror_blob)

    def test_mirror_failure_marks_pending_local_still_written(self):
        self.mirror.fail_writes = 99
        lease = self._lease()
        self.assertEqual(lease["session_id"], "muse-h3-aaa")
        local = json.loads((Path(self.td) / "leases.json").read_text())
        self.assertTrue(local["mirror_pending"])
        self.assertEqual(local["mirror_error"], "MirrorUnavailableError")
        self.assertIsNone(self.mirror.doc)

    def test_sync_mirror_retries_pending(self):
        self.mirror.fail_writes = 99
        self._lease()
        self.mirror.fail_writes = 0
        doc = self.reg.sync_mirror()
        self.assertFalse(doc["mirror_pending"])
        self.assertIsNotNone(self.mirror.doc)

    def test_local_write_failure_raises(self):
        with patch.object(lr, "_atomic_write_json",
                          side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._lease()
        self.assertIsNone(self.mirror.doc)

    def test_reregister_carries_version_forward(self):
        self._lease()
        again = self._lease()
        self.assertEqual(again["version"], 2)
        self.assertEqual(len(self.reg.list_leases()), 1)


class DeadlineTests(RegistryTestBase):
    def test_update_deadline_finalises(self):
        self._lease()
        updated = self.reg.update_deadline("muse-h3-aaa",
                                           "2026-11-13T10:00:00+00:00")
        self.assertTrue(updated["deadline_final"])
        self.assertEqual(updated["stop_deadline_utc"],
                         "2026-11-13T10:00:00+00:00")
        self.assertEqual(updated["version"], 2)

    def test_update_deadline_on_stopped_lease_is_none(self):
        self._lease()
        self.reg.confirm_stopped("muse-h3-aaa", "STOPPED")
        self.assertIsNone(self.reg.update_deadline(
            "muse-h3-aaa", "2026-11-13T10:00:00+00:00"))

    def test_update_deadline_unknown_session_is_none(self):
        self.assertIsNone(self.reg.update_deadline(
            "muse-h3-nope", "2026-11-13T10:00:00+00:00"))


class ConfirmTests(RegistryTestBase):
    def test_confirm_requires_stopped(self):
        self._lease()
        for bad in ("ACTIVE", "UNKNOWN", "", None):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.reg.confirm_stopped("muse-h3-aaa", bad)
        self.assertEqual(
            self.reg.get_lease("muse-h3-aaa")["last_confirmed_state"],
            "ACTIVE")

    def test_confirm_stopped_marks_and_clears_claim(self):
        self._lease()
        self.reg.claim_lease("muse-h3-aaa", "exec-1")
        self.assertTrue(self.reg.confirm_stopped("muse-h3-aaa", "STOPPED"))
        lease = self.reg.get_lease("muse-h3-aaa")
        self.assertEqual(lease["last_confirmed_state"], "STOPPED")
        self.assertIsNone(lease["executor_claim"])
        # Idempotent re-mark.
        self.assertTrue(self.reg.confirm_stopped("muse-h3-aaa", "STOPPED"))

    def test_confirm_unknown_session_is_false(self):
        self.assertFalse(self.reg.confirm_stopped("muse-h3-nope", "STOPPED"))


class ClaimTests(RegistryTestBase):
    def test_claim_conflict_then_stale_takeover(self):
        self._lease()
        self.assertIsNotNone(self.reg.claim_lease("muse-h3-aaa", "exec-1"))
        # Fresh claim held by another executor: refused.
        self.assertIsNone(self.reg.claim_lease("muse-h3-aaa", "exec-2"))
        # Age the claim past the TTL: takeover allowed.
        lease = self.reg.get_lease("muse-h3-aaa")
        lease["executor_claim"]["claimed_at_utc"] = "2026-11-13T07:00:00+00:00"
        doc = json.loads((Path(self.td) / "leases.json").read_text())
        doc["leases"] = [lease]
        (Path(self.td) / "leases.json").write_text(json.dumps(doc))
        self.assertIsNotNone(self.reg.claim_lease("muse-h3-aaa", "exec-2"))

    def test_release_claim_is_best_effort(self):
        self._lease()
        self.reg.claim_lease("muse-h3-aaa", "exec-1")
        self.reg.release_claim("muse-h3-aaa", "exec-1")
        self.assertIsNone(
            self.reg.get_lease("muse-h3-aaa")["executor_claim"])
        # Releasing a claim we do not hold is a silent no-op.
        self.reg.release_claim("muse-h3-aaa", "exec-2")


class MirrorConflictTests(RegistryTestBase):
    def test_concurrent_writer_merges_both_leases(self):
        self._lease(sid="muse-h3-aaa")
        foreign = {"leases": [{
            "session_id": "muse-h3-bbb", "run_id": "run-2",
            "production_task_id": "h3-ab-20261111", "owner_id": OWNER,
            "gpu_model": "A100_HIGH_MEM", "started_at_utc": NOW,
            "stop_deadline_utc": DEADLINE, "deadline_final": True,
            "budget_cap_cu": 25.0, "last_confirmed_state": "ACTIVE",
            "version": 1, "executor_claim": None, "stop_attempts": 0}],
            "version": 5, "mirror_file_id": "mirror-file-1"}
        self.mirror.concurrent_doc = foreign
        self._lease(sid="muse-h3-ccc")
        ids = {x["session_id"] for x in self.mirror.doc["leases"]}
        self.assertEqual(ids, {"muse-h3-aaa", "muse-h3-bbb", "muse-h3-ccc"})
        local = json.loads((Path(self.td) / "leases.json").read_text())
        self.assertFalse(local["mirror_pending"])
        # The merge also lands in the LOCAL file: a later local-only
        # read must see the foreign lease too.
        self.assertEqual({x["session_id"] for x in local["leases"]},
                         {"muse-h3-aaa", "muse-h3-bbb", "muse-h3-ccc"})


class ExecutorReadTests(RegistryTestBase):
    def test_load_for_executor_prefers_mirror_and_refreshes_local(self):
        self._lease(sid="muse-h3-aaa")
        # A newer mirror (written by another host) carries a lease the
        # local file never saw.
        self.mirror.doc["leases"].append({
            "session_id": "muse-h3-bbb", "run_id": "run-2",
            "production_task_id": "h3-ab-20261111", "owner_id": OWNER,
            "gpu_model": "G4", "started_at_utc": NOW,
            "stop_deadline_utc": DEADLINE, "deadline_final": True,
            "budget_cap_cu": 25.0, "last_confirmed_state": "ACTIVE",
            "version": 1, "executor_claim": None, "stop_attempts": 0})
        self.mirror.doc["version"] = 99
        leases, source, degraded = self.reg.load_for_executor()
        self.assertEqual(source, "mirror")
        self.assertFalse(degraded)
        self.assertEqual({x["session_id"] for x in leases},
                         {"muse-h3-aaa", "muse-h3-bbb"})
        # Local was refreshed from the mirror.
        self.assertEqual({x["session_id"] for x in self.reg.list_leases()},
                         {"muse-h3-aaa", "muse-h3-bbb"})

    def test_load_for_executor_degraded_when_mirror_down(self):
        self._lease()
        with patch.object(self.mirror, "read_mirror",
                          side_effect=MirrorUnavailableError("down")):
            leases, source, degraded = self.reg.load_for_executor()
        self.assertEqual(source, "local")
        self.assertTrue(degraded)
        self.assertEqual(len(leases), 1)

    def test_note_stop_attempt_stays_pending(self):
        self._lease()
        noted = self.reg.note_stop_attempt("muse-h3-aaa", "UNKNOWN")
        self.assertEqual(noted["stop_attempts"], 1)
        self.assertEqual(noted["last_stop_error"], "UNKNOWN")
        self.assertIsNotNone(noted["next_retry_at_utc"])
        self.assertEqual(noted["last_confirmed_state"], "ACTIVE")


class OwnerTests(unittest.TestCase):
    def test_owner_id_is_hash_not_email(self):
        self.assertEqual(owner_id_for("Owner@Example.COM"),
                         hashlib.sha256(
                             b"owner@example.com").hexdigest()[:16])
        self.assertNotIn("@", owner_id_for("a@b.c"))

    def test_current_owner_id_parses_whoami(self):
        import subprocess

        fake = type("P", (), {"returncode": 0,
                             "stdout": "Email: Owner@Example.COM\n"})()
        with patch("drive_depot.resolve_colab_executable",
                   return_value="/usr/bin/colab"), \
                patch.object(subprocess, "run", return_value=fake):
            self.assertEqual(lr.current_owner_id(), OWNER)

    def test_current_owner_id_fails_closed(self):
        import subprocess

        fake = type("P", (), {"returncode": 0, "stdout": "no email here\n"})()
        with patch("drive_depot.resolve_colab_executable",
                   return_value="/usr/bin/colab"), \
                patch.object(subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError):
                lr.current_owner_id()


class ImportTests(unittest.TestCase):
    def test_import_managed_sessions_is_read_only(self):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        managed = Path(td) / "h3_managed_sessions.json"
        managed.write_text(json.dumps({"sessions": [{
            "session_id": "muse-h3-aaa", "gpu": "G4", "run_id": "run-1",
            "production_task_id": "h3-ab-20261111",
            "started_at_utc": NOW, "deadline_utc": DEADLINE,
            "status": "active", "version": 3}]}))
        imported = lr.import_managed_sessions(managed)
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["session_id"], "muse-h3-aaa")
        self.assertIsNone(imported[0]["owner_id"])
        self.assertFalse(imported[0]["deadline_final"])
        self.assertIsNone(imported[0]["budget_cap_cu"])
        # Nothing was written next to the managed file.
        self.assertEqual(sorted(p.name for p in Path(td).iterdir()),
                         ["h3_managed_sessions.json"])


class EmergencyCapTests(RegistryTestBase):
    """TASK-007: register_lease writes the absolute physical cap
    emergency_deadline_utc = started + max_wall_sec + 900 s."""

    def test_register_writes_emergency_deadline(self):
        lease = self._lease(started_at_utc="2026-11-13T06:00:00+00:00",
                            max_wall_sec=3600)
        # 06:00 + 3600 s + 900 s grace = 07:15.
        self.assertEqual(lease["emergency_deadline_utc"],
                         "2026-11-13T07:15:00+00:00")

    def test_register_without_max_wall_sec_has_no_emergency_cap(self):
        lease = self._lease()
        self.assertIsNone(lease["emergency_deadline_utc"])

    def test_update_deadline_preserves_emergency_cap(self):
        lease = self._lease(started_at_utc="2026-11-13T06:00:00+00:00",
                            max_wall_sec=3600)
        self.assertEqual(lease["emergency_deadline_utc"],
                         "2026-11-13T07:15:00+00:00")
        updated = self.reg.update_deadline("muse-h3-aaa", DEADLINE, final=True)
        self.assertTrue(updated["deadline_final"])
        self.assertEqual(updated["emergency_deadline_utc"],
                         "2026-11-13T07:15:00+00:00")

    def test_reregister_recomputes_emergency_cap(self):
        self._lease(started_at_utc="2026-11-13T06:00:00+00:00",
                    max_wall_sec=3600)
        again = self._lease(started_at_utc="2026-11-13T06:00:00+00:00",
                            max_wall_sec=7200)
        self.assertEqual(again["emergency_deadline_utc"],
                         "2026-11-13T08:15:00+00:00")
        self.assertEqual(again["version"], 2)

    def test_mirror_pending_accessor(self):
        self.mirror.fail_writes = 99
        self._lease()
        self.assertTrue(self.reg.mirror_pending())
        self.mirror.fail_writes = 0
        self.reg.sync_mirror()
        self.assertFalse(self.reg.mirror_pending())


class UpdateDeadlineFailClosedTests(unittest.TestCase):
    """TASK-007: h3_controller._update_gpu_lease_deadline is fail-closed
    on a local registry write failure (raises into the outer finally,
    which stops the session); only a Drive mirror failure degrades to
    a warning event."""

    def setUp(self):
        import h3_controller as hc

        self.hc = hc
        self.records = []
        self.sleep_patch = patch.object(hc.time, "sleep", lambda s: None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.record_patch = patch.object(
            hc, "record", lambda data: self.records.append(data))
        self.record_patch.start()
        self.addCleanup(self.record_patch.stop)

    def _hook(self, registry):
        with patch.object(self.hc, "_lease_registry", return_value=registry):
            return self.hc._update_gpu_lease_deadline(
                "muse-h3-aaa", "2026-11-13T09:00:00+00:00")

    def test_local_write_failure_raises_after_retries(self):
        class FailingRegistry:
            def __init__(self):
                self.calls = 0

            def update_deadline(self, *a, **k):
                self.calls += 1
                raise OSError("disk full")

            def mirror_pending(self):
                return False

        reg = FailingRegistry()
        with self.assertRaises(OSError):
            self._hook(reg)
        self.assertEqual(reg.calls, 3)
        events = [r["event"] for r in self.records]
        self.assertIn("lease_deadline_sync_failed", events)

    def test_mirror_pending_is_a_warning_not_a_raise(self):
        class PendingMirrorRegistry:
            def update_deadline(self, *a, **k):
                return {"session_id": "muse-h3-aaa"}

            def mirror_pending(self):
                return True

        self._hook(PendingMirrorRegistry())  # must not raise
        events = [r["event"] for r in self.records]
        self.assertIn("lease_mirror_pending", events)
        self.assertNotIn("lease_deadline_sync_failed", events)

    def test_missing_lease_is_a_warning_not_a_raise(self):
        class MissingLeaseRegistry:
            def update_deadline(self, *a, **k):
                return None

            def mirror_pending(self):
                return False

        self._hook(MissingLeaseRegistry())  # must not raise
        events = [r["event"] for r in self.records]
        self.assertIn("lease_deadline_missing", events)


if __name__ == "__main__":
    unittest.main()
