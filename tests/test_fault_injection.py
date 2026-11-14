"""TASK-005 PR-8 (Issue #31): full-chain fault injection.

A simulated executor over the whole production chain:

    PREFLIGHT -> TASK_AUTHORIZED -> GPU_ALLOCATED -> WORKER_STARTED
    -> MODELS_READY -> INFERENCE_COMPLETED -> VIDEO_VALIDATED
    -> ARTIFACT_DURABLE -> GPU_STOP_CONFIRMED -> DRIVE_VERIFIED
    -> TASK_COMPLETED

Every external boundary is a scripted fake, but the code under test is
the REAL controller: run_variant, _resume_run, _stop_run,
_reconcile_task, stop_and_verify_session, deliver_variant_to_drive_
idempotent, launch_worker / poll_worker / wait_for_worker,
probe_actual_gpu, archive_variant_to_drive, upload_one. No GPU, no CU,
no real Drive, no ADC.

Two honest limitations, stated here and in the tests that rely on them:

1. A real process kill cannot be simulated inside the test process.
   The controller-restart scenarios therefore CONSTRUCT the exact
   on-disk state a dead process leaves behind (manifest + ledger +
   local MP4 + managed-session record + scripted worker state) and
   execute the REAL resume path from it. The construction is explicit;
   the recovery execution is real.
2. run_cli is mocked in the chain harness, so the REAL run_cli
   redaction is not covered there. It is covered by dedicated tests
   (CredentialLeakTest) that drive the real run_cli against a fake
   `colab` executable. A canary injected into a mocked exception
   would test the mock, not the code: in production, exception text
   reaching record() has already passed through the real run_cli
   redaction (or comes from library/Drive errors that carry no
   tokens). The chain leak test therefore scans the real persisted
   artifacts for token-SHAPED strings under realistic scripted
   outputs.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive_api
import drive_depot
import h3_budget
import h3_controller as hc
import h3_run_manifest as rm
import h3_session_watchdog as hwd
import h3_video_qa
from h3_environment import EnvironmentMismatch
from h3_session_watchdog import SessionQueryError, SessionState

RUN_ID = "h3-20261113T000000Z-test01"
TASK = "h3-ab-20261111"
FOREIGN_SESSION = "muse-h3-foreign999"


def _video_info(sha, frames=274, width=1080, height=1920, fps=24.0):
    return {"frame_count": frames, "width": width, "height": height,
            "fps": fps, "sha256": sha}


def _args(action="run", **over):
    kw = {"action": action, "variant": "A", "run_id": RUN_ID,
          "production_task_id": TASK,
          "execute": True, "confirm_gpu": True, "authorize_task_gpu": True,
          "allow_g4": True, "allow_a100_highmem": True,
          "project_budget_cu": 30.0, "per_session_budget_cu": 25.0,
          "gpu_policy": "G4_THEN_A100_HIGHMEM",
          "comfy_ref": "61e524bdba7a4e193b45c2697f1af83c1beb86d6",
          "drive_cache_dir": "/content/drive/MyDrive/Muse-H3-Model-Depot",
          "approve_interactive_drive_mount": True, "fresh_inference": True,
          "workflow_api": "/tmp/workflow_api.json",
          "cache_manifest": "/tmp/verified_manifest.json",
          "max_rate_cu_h": 14.0, "max_wall_sec": 3600,
          "prior_unbilled_reserve_cu": 0.14, "authorize_new_task": False,
          "poll_interval_sec": 60}
    kw.update(over)
    return SimpleNamespace(**kw)


# TASK-006 P0-B: the chain harness stubs the download-path boundaries
# (preflight, block builder, token upload) the same way it already
# stubs mount_drive / verify_ready / validate_local. The real builder,
# preflight and token upload are covered by tests/test_drive_download.py.
_CANNED_DOWNLOAD_BLOCK = {
    "files": [{"file_id": "fid-1",
               "path": "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
               "size": 10, "sha256": "a" * 64, "md5": "b" * 32}],
    "comfyui": None,
    "dest_dir": "/content/muse_h3/drive_downloads",
    "token_file": "/content/muse_h3/drive_token.json",
    "probe": {"file_id": "fid-1"},
}


def _job_state(stage, output=None, error=None):
    state = {"run_id": RUN_ID, "variant": "A", "stage": stage}
    if output is not None:
        state["output"] = output
    if error is not None:
        state["error"] = error
    return state


class ChainHarness:
    """One simulated production run: temp ROOT, scripted boundaries.

    `root=None` creates a fresh temp dir with fixtures (local MP4 +
    ledger). `root=<existing>` reuses a previous harness's dir WITHOUT
    rewriting its fixtures — that is how a controller restart is
    simulated: a new harness (fresh mocks, fresh in-memory state)
    over the same on-disk state.
    """

    def __init__(self, test, root=None, tmp=None):
        self.t = test
        if root is None:
            self.tmp = tempfile.TemporaryDirectory()
            self.root = Path(self.tmp.name)
            fresh = True
        else:
            self.tmp = tmp
            self.root = Path(root)
            fresh = False
        self.out_root = self.root / "output"
        self.runs_root = self.root / "runs"
        self.out_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        (self.root / "model_cache").mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "managed_sessions.json"
        self.ledger_path = (self.root / "model_cache"
                            / f"h3_production_budget_{TASK}.json")
        self.mp4 = self.out_root / RUN_ID / "A.mp4"
        self.sha = hashlib.sha256(b"fake-mp4").hexdigest()
        self.video_info = _video_info(self.sha)
        if fresh:
            self.mp4.parent.mkdir(parents=True, exist_ok=True)
            self.mp4.write_bytes(b"fake-mp4")
            self.ledger_path.write_text(json.dumps({
                "production_task_id": TASK, "project_budget_cu": 30.0,
                "hard_stop_balance_cu": 132.77, "max_observed_spent_cu": 0.0,
                "run_ids": [RUN_ID]}))
        self.ledger_bytes = self.ledger_path.read_bytes()
        # Real watchdog functions, captured BEFORE _install patches them.
        self._real_record = hwd.record_session
        self._real_confirm_active = hwd.confirm_session_active
        self._real_mark_stopped = hwd.mark_session_stopped
        self._real_update_deadline = hwd.update_session_deadline
        self._real_reconcile = hwd.reconcile_on_startup
        self._real_stop_managed = hwd.stop_managed_session
        self._real_query = hwd.query_session_state
        self._real_allocate = hc.allocate_managed_session
        # Call logs.
        self.run_cli_calls = []
        self.exec_calls = []
        self.allocate_ids = []
        self.stop_cli_sessions = []
        self.archive_calls = []
        self.download_calls = []
        self.log_download_calls = []
        # Scripted behaviour (scenarios override before driving).
        self.inspect_plan = [{"cu_balance": 150.0, "rate": 5.0, "sessions": []}]
        self.inspect_idx = 0
        self.query_plan = [SessionState.ACTIVE, SessionState.STOPPED]
        self.query_idx = 0
        self.new_failures = []
        self.stop_failures = []
        self.probe_output = "RTX PRO 6000 Blackwell, 97808"
        self.job_probe_states = []
        self.download_exc = None
        self.archive_plan = []
        self.env_exc = None
        self.patches = []
        self._install()

    # -- patching ------------------------------------------------------
    def _po(self, target, attr, new=mock.DEFAULT, **kwargs):
        if new is mock.DEFAULT:
            p = mock.patch.object(target, attr, **kwargs)
        else:
            p = mock.patch.object(target, attr, new, **kwargs)
        m = p.start()
        self.patches.append(p)
        return m

    def _install(self):
        self._po(hc, "ROOT", self.root)
        self._po(hc, "OUTPUT_ROOT", self.out_root)
        self._po(hc, "RUNS", self.runs_root)
        self._po(hc.time, "sleep", lambda s: None)
        self._po(hc, "validate_local")
        self._po(hc, "validate_comfy_workflow", return_value={})
        # TASK-007: reference-image materialisation is stubbed here the
        # same way validate_local is — the real function (local SHA-256
        # check + Drive download) is covered by tests/test_task_spec.py,
        # and the temp ROOT has no poster file.
        self._po(hc, "_materialize_ref_images",
                 return_value={"ref_image_1": Path("/tmp/ref_image_1.jpg"),
                               "ref_image_2": Path("/tmp/ref_image_2.jpg")})
        self._po(hc, "verify_environment", side_effect=self._fake_env)
        self._po(hc, "verify_drive_download_preflight",
                 return_value={"preflight": "ok"})
        self._po(hc, "_build_drive_download_block",
                 return_value=_CANNED_DOWNLOAD_BLOCK)
        self._po(hc, "_mint_and_upload_drive_token",
                 return_value={"token_id": "test-token-id",
                               "remote_path": "/content/muse_h3/drive_token.json"})
        self._po(hc, "_register_gpu_lease",
                 return_value={"session_id": "muse-h3-test",
                               "version": 1, "deadline_final": False})
        self._po(hc, "_update_gpu_lease_deadline", return_value=None)
        self._po(hc, "_confirm_gpu_lease_stopped", return_value=None)
        self._po(hc, "initialize_project_budget",
                 return_value={"max_observed_spent_cu": 0.0,
                               "production_task_id": TASK,
                               "project_budget_cu": 30.0,
                               "hard_stop_balance_cu": 132.77})
        self._po(hc, "inspect", side_effect=self._fake_inspect)
        self._po(hc, "allocate_managed_session",
                 side_effect=self._fake_allocate)
        self._po(hc, "run_cli", side_effect=self._fake_run_cli)
        self._po(hc, "exec_script", side_effect=self._fake_exec)
        self._po(hc, "check_mount_signature", return_value=True)
        self._po(hc, "mount_drive", return_value={})
        self._po(hc, "check_remote_output", return_value={"ok": True})
        self._po(hc, "download_and_validate_variant",
                 side_effect=self._fake_download)
        self._po(hc, "download_remote_comfyui_log",
                 side_effect=self._fake_log_download)
        self._po(hc, "_comfyui_cache_task", return_value={})
        self._po(hc, "read_live_usage",
                 return_value={"balance": 150.0, "rate": 5.0})
        self._po(h3_budget, "read_live_usage",
                 return_value={"balance": 150.0, "rate": 5.0})
        self._po(h3_budget, "estimate_unbilled_cu", return_value=0.1)
        self._po(hc, "estimate_unbilled_cu", return_value=0.1)
        self._po(hc, "allowed_runtime_seconds", return_value=3600)
        self._po(hc, "initial_session_wall_seconds", return_value=3600)
        self._po(hc, "reconcile_budget",
                 side_effect=lambda ledger, bal, path: ledger)
        self._po(hc, "archive_variant_to_drive",
                 side_effect=self._fake_archive)
        self._po(hc, "_wait_watchdog_ready",
                 return_value={"session_id": "muse-h3-test-session",
                               "production_task_id": TASK})
        # The real watchdog functions, but on the temp managed file.
        # (Their path= defaults bind the real STATE_PATH at def time,
        # so they are wrapped, not merely re-pointed.)
        self._po(hwd, "record_session", side_effect=self._wrap_record_session)
        self._po(hwd, "confirm_session_active",
                 side_effect=self._wrap_confirm_active)
        self._po(hwd, "mark_session_stopped",
                 side_effect=self._wrap_mark_stopped)
        self._po(hwd, "update_session_deadline",
                 side_effect=self._wrap_update_deadline)
        self._po(hwd, "reconcile_on_startup",
                 side_effect=self._wrap_reconcile)
        self._po(hwd, "stop_managed_session",
                 side_effect=self._wrap_stop_managed)
        self._po(hwd, "query_session_state", side_effect=self._fake_query)
        self._po(drive_depot, "drive_service", return_value=mock.Mock())
        self._po(drive_depot, "account_matches", return_value=True)
        self._po(drive_depot, "verify_ready", return_value={})
        self._po(drive_api, "verify_uploaded_file",
                 return_value={"drive_file_id": "drive-file-9",
                               "server_verified": True})
        self._po(h3_video_qa, "validate_final_video",
                 return_value=self.video_info)
        proc = mock.Mock()
        proc.poll.return_value = None
        popen_patch = mock.patch("subprocess.Popen", return_value=proc)
        popen_patch.start()
        self.patches.append(popen_patch)
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        self._po(sys, "stdin", stdin)

    def close(self):
        for p in reversed(self.patches):
            p.stop()
        self.patches = []

    def unpatch(self, target, attr):
        """Stop one patch installed by _install (e.g. to run the real
        archive path in a single test)."""
        for p in list(self.patches):
            if getattr(p, "attribute", None) == attr and p.getter() is target:
                p.stop()
                self.patches.remove(p)
                return
        raise AssertionError(f"no patch for {target}.{attr}")

    def restart(self):
        """A new controller process over the same on-disk state."""
        self.close()
        return ChainHarness(self.t, root=self.root, tmp=self.tmp)

    # -- fakes ---------------------------------------------------------
    def _fake_env(self):
        if self.env_exc is not None:
            raise self.env_exc
        return {"ok": True, "checked": []}

    def _fake_inspect(self):
        entry = self.inspect_plan[min(self.inspect_idx,
                                      len(self.inspect_plan) - 1)]
        self.inspect_idx += 1
        if isinstance(entry, BaseException):
            raise entry
        if callable(entry):
            return entry(self)
        return dict(entry)

    def _fake_allocate(self, choice, run_id):
        sid = self._real_allocate(choice, run_id)
        self.allocate_ids.append(sid)
        return sid

    def _fake_query(self, session_id, colab_auth=None):
        entry = self.query_plan[min(self.query_idx, len(self.query_plan) - 1)]
        self.query_idx += 1
        if isinstance(entry, BaseException):
            raise entry
        return entry

    def _fake_run_cli(self, cmd, timeout=90, check=True):
        self.run_cli_calls.append(list(cmd))
        if cmd and cmd[0] == "new":
            if self.new_failures:
                exc = self.new_failures.pop(0)
                if exc is not None:
                    raise exc
            return {"returncode": 0, "stdout": "", "stderr": ""}
        if cmd and cmd[0] == "stop":
            sid = cmd[cmd.index("-s") + 1] if "-s" in cmd else None
            self.stop_cli_sessions.append(sid)
            if self.stop_failures:
                exc = self.stop_failures.pop(0)
                if exc is not None:
                    raise exc
            return {"returncode": 0, "stdout": "", "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    def _default_job_state(self):
        return _job_state("VIDEO_READY", output={
            "file": "/content/muse_h3/output/shao_nian_h3_A.mp4",
            "frames": 274, "sha256": self.sha,
            "seconds": 274 / 24, "with_audio": True})

    def _fake_exec(self, sess, path, timeout=None, env=None):
        name = Path(path).name
        self.exec_calls.append((sess, name))
        if name == "h3_gpu_probe.py":
            return {"returncode": 0, "stdout": self.probe_output, "stderr": ""}
        if name == "h3_worker_launcher.py":
            return {"returncode": 0, "stdout": "MUSE_H3_LAUNCH_JSON:" + json.dumps(
                {"run_id": RUN_ID, "variant": "A", "pid": 123,
                 "state_file": "/content/muse_h3/MUSE_H3_JOB_STATE.json",
                 "log_file": "/content/muse_h3/worker.log"}), "stderr": ""}
        if name == "h3_job_probe.py":
            if self.job_probe_states:
                state = self.job_probe_states.pop(0)
            else:
                state = self._default_job_state()
            if isinstance(state, str):
                return {"returncode": 0, "stdout": state, "stderr": ""}
            return {"returncode": 0, "stdout":
                    "MUSE_H3_JOB_STATE_JSON:" + json.dumps(state), "stderr": ""}
        if name == "h3_drive_probe.py":
            return {"returncode": 0, "stdout":
                    "MUSE_DRIVE_MOUNT_MODELS_VISIBLE 4", "stderr": ""}
        if name == "h3_drive_download_probe.py":
            return {"returncode": 0, "stdout":
                    "MUSE_DRIVE_DOWNLOAD_PROBE_OK fid-1 10 10", "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    def _fake_download(self, session, variant, run_id, spec=None):
        self.download_calls.append((session, variant, run_id))
        if self.download_exc is not None:
            raise self.download_exc
        return dict(self.video_info)

    def _fake_log_download(self, session, run_dir):
        self.log_download_calls.append((session, str(run_dir)))
        return None

    def _fake_archive(self, variant, run_id, qa_files=None):
        self.archive_calls.append((variant, run_id))
        if self.archive_plan:
            entry = self.archive_plan.pop(0)
            if isinstance(entry, BaseException):
                raise entry
            return entry
        return {"output_folder_id": "folder-1",
                "files": {f"{variant}.mp4": {
                    "drive_file_id": "drive-file-1", "size": 8,
                    "server_md5": "x" * 32, "sha256": self.sha,
                    "parent": "folder-1"}}}

    # -- real watchdog functions on the temp managed file --------------
    def _wrap_record_session(self, session_id, gpu, run_id, deadline_utc,
                             production_task_id="", status="active"):
        return self._real_record(session_id, gpu, run_id, deadline_utc,
                                 path=self.state_path,
                                 production_task_id=production_task_id,
                                 status=status)

    def _wrap_confirm_active(self, session_id, path=None):
        return self._real_confirm_active(session_id, path=self.state_path)

    def _wrap_mark_stopped(self, session_id, confirmed_state=None, path=None):
        return self._real_mark_stopped(session_id, confirmed_state, path=self.state_path)

    def _wrap_update_deadline(self, session_id, deadline_utc, path=None):
        return self._real_update_deadline(session_id, deadline_utc,
                                          path=self.state_path)

    def _wrap_reconcile(self, colab_auth="adc", path=None):
        return self._real_reconcile(colab_auth, path=self.state_path)

    def _wrap_stop_managed(self, session_id, colab_auth="adc", path=None):
        return self._real_stop_managed(session_id, colab_auth=colab_auth,
                                       path=self.state_path)

    # -- driving -------------------------------------------------------
    def run(self, **over):
        return hc.run_variant(_args(**over))

    def resume(self, **over):
        return hc._resume_run(_args(action="resume", **over))

    def stop(self, **over):
        return hc._stop_run(_args(action="stop", **over))

    def reconcile(self, **over):
        return hc._reconcile_task(_args(action="reconcile", **over))

    def archive(self, **over):
        return hc._archive_local_mp4("A", RUN_ID)

    # -- state readers -------------------------------------------------
    def manifest(self):
        return rm.load_run_manifest(self.root, RUN_ID)

    def manifest_state(self):
        m = self.manifest()
        return (m.get("task_state"), m.get("last_verified_stage"),
                m.get("last_error"), m.get("artifacts") or {})

    def audit_events(self):
        return rm.read_audit_log(self.root, RUN_ID)

    def audit_jsonl_text(self):
        p = self.runs_root / "h3_audit.jsonl"
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def managed_sessions(self):
        return hwd.load_managed_sessions(path=self.state_path)

    # -- invariant assertions ------------------------------------------
    def assert_no_false_success(self):
        state, _, _, _ = self.manifest_state()
        self.t.assertNotEqual(state, "TASK_COMPLETED")

    def assert_mp4_unchanged(self):
        self.t.assertEqual(self.mp4.read_bytes(), b"fake-mp4")
        self.t.assertEqual(hashlib.sha256(self.mp4.read_bytes()).hexdigest(),
                           self.sha)

    def assert_ledger_unchanged(self):
        self.t.assertEqual(self.ledger_path.read_bytes(), self.ledger_bytes)

    def assert_allocations(self, n):
        self.t.assertEqual(len(self.allocate_ids), n)

    def construct_manifest(self, task_state, session_id=None,
                           session_state="ACTIVE", artifacts=None,
                           code_commit=""):
        """The exact on-disk state a dead controller leaves behind."""
        rm.create_run_manifest(self.root, TASK, RUN_ID, variant="A",
                               code_commit=code_commit, budget={})
        if task_state != "TASK_AUTHORIZED":
            rm.update_run(self.root, RUN_ID, task_state=task_state,
                          session_id=session_id, session_state=session_state,
                          last_verified_stage=task_state,
                          artifacts=artifacts or {})
        elif session_id:
            rm.update_run(self.root, RUN_ID, session_id=session_id,
                          session_state=session_state,
                          artifacts=artifacts or {})
        return rm.load_run_manifest(self.root, RUN_ID)

# ----------------------------------------------------------------------
# Baseline: the whole chain, all 11 stages, exactly one GPU, one stop,
# one upload. Every fault test below is a deviation from this.
# ----------------------------------------------------------------------
class BaselineChainTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)

    def tearDown(self):
        self.h.close()

    def test_full_chain_succeeds(self):
        h = self.h
        result = h.run()
        self.assertEqual(result["delivery"]["drive_file_id"], "drive-file-1")
        self.assertTrue(result["delivery"]["drive_upload_verified"])
        state, verified, err, artifacts = h.manifest_state()
        self.assertEqual(state, "TASK_COMPLETED")
        self.assertEqual(verified, "DRIVE_VERIFIED")
        self.assertIsNone(err)
        self.assertEqual(artifacts.get("drive_file_id"), "drive-file-1")
        self.assertEqual(artifacts.get("delivery_state"), "DELIVERED")
        # The first poll already returns VIDEO_READY, so MODELS_READY is
        # never *observed* as a worker state here — the manifest records
        # observed stages, not assumed ones. The explicit poll sequence
        # in test_savevideo_alternate_shape proves MODELS_READY is
        # recorded when a poll does observe it.
        stages = [e.get("detail", {}).get("to") for e in h.audit_events()
                  if e.get("event") == "STAGE"]
        self.assertEqual(stages, ["GPU_ALLOCATED", "WORKER_STARTED",
                                  "INFERENCE_COMPLETED",
                                  "VIDEO_VALIDATED", "ARTIFACT_DURABLE",
                                  "GPU_STOP_CONFIRMED", "DRIVE_VERIFIED",
                                  "TASK_COMPLETED"])
        self.assertEqual(len(h.allocate_ids), 1)
        self.assertEqual(h.stop_cli_sessions, h.allocate_ids)
        self.assertEqual(len(h.archive_calls), 1)
        self.assertEqual(h.manifest().get("session_id"), h.allocate_ids[0])
        self.assertEqual(h.manifest().get("session_state"), "STOPPED")
        self.assertEqual(h.managed_sessions()[0]["status"], "stopped")
        h.assert_mp4_unchanged()
        h.assert_ledger_unchanged()


class ManifestExistsTest(unittest.TestCase):
    def test_manifest_exists(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertFalse(rm.manifest_exists(root, "nope"))
            rm.create_run_manifest(root, TASK, RUN_ID, variant="A", budget={})
            self.assertTrue(rm.manifest_exists(root, RUN_ID))
            self.assertFalse(rm.manifest_exists(root, "other-run"))


# ----------------------------------------------------------------------
# Controller faults (TASK-005 §8).
# ----------------------------------------------------------------------
class ControllerFaultTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)

    def tearDown(self):
        self.h.close()

    def test_duplicate_run_id_refused(self):
        h = self.h
        h.run()
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn(RUN_ID, str(ctx.exception))
        self.assertIn("TASK_COMPLETED", str(ctx.exception))
        # The second call allocated nothing and changed nothing.
        self.assertEqual(len(h.allocate_ids), 1)
        self.assertEqual(len(h.archive_calls), 1)
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        h.assert_mp4_unchanged()
        h.assert_ledger_unchanged()

    def test_stdout_lost_run_still_completes_from_durable_state(self):
        # stdout/stderr are logs, not the record: the run must complete
        # and the durable state must be intact even with both discarded.
        h = self.h
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = h.run()
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(result["delivery"]["drive_file_id"], "drive-file-1")
        self.assertTrue(h.audit_jsonl_text())

    def _construct_active_run(self, task_state):
        """Pre-state a dead controller leaves behind: manifest + ledger
        (harness fixture) + local MP4 (harness fixture) + managed record."""
        h = self.h
        session = "muse-h3-test-session"
        h.construct_manifest(task_state, session_id=session,
                             session_state="ACTIVE")
        h._real_record(session, "G4", RUN_ID, "2026-11-13T06:00:00+00:00",
                       path=h.state_path, production_task_id=TASK)
        return session

    def test_restart_mid_inference(self):
        h = self.h
        session = self._construct_active_run("WORKER_STARTED")
        h2 = h.restart()
        self.h = h2  # tearDown closes the live harness, not the dead one
        h2.query_plan = [SessionState.ACTIVE, SessionState.ACTIVE,
                         SessionState.STOPPED]
        result = h2.resume()
        self.assertTrue(result.get("resumed"))
        self.assertEqual(h2.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(h2.manifest()["recovery_generation"], 1)
        # No second GPU, no re-launch, no re-download: the local MP4
        # was reused and the one session was stopped.
        self.assertEqual(h2.allocate_ids, [])
        self.assertEqual(h2.download_calls, [])
        self.assertEqual(h2.stop_cli_sessions, [session])
        self.assertEqual(len(h2.archive_calls), 1)
        h2.assert_mp4_unchanged()
        h2.assert_ledger_unchanged()

    def test_restart_after_mp4_durable(self):
        h = self.h
        session = self._construct_active_run("ARTIFACT_DURABLE")
        # The durable artifact is recorded on the manifest.
        rm.update_run(h.root, RUN_ID, artifacts={
            "local_sha256": h.sha, "local_frames": 274,
            "delivery_state": "COMPLETE_LOCAL"})
        h2 = h.restart()
        self.h = h2  # tearDown closes the live harness, not the dead one
        h2.query_plan = [SessionState.STOPPED]
        result = h2.resume()
        self.assertTrue(result.get("resumed"))
        self.assertEqual(h2.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(h2.manifest()["recovery_generation"], 1)
        self.assertEqual(h2.allocate_ids, [])
        self.assertEqual(h2.download_calls, [])
        self.assertEqual(h2.stop_cli_sessions, [])
        self.assertEqual(len(h2.archive_calls), 1)
        h2.assert_mp4_unchanged()
        h2.assert_ledger_unchanged()

    def test_resume_finds_worker_still_running(self):
        h = self.h
        session = self._construct_active_run("INFERENCE_RUNNING")
        h2 = h.restart()
        self.h = h2  # tearDown closes the live harness, not the dead one
        h2.query_plan = [SessionState.ACTIVE, SessionState.ACTIVE,
                         SessionState.STOPPED]
        # First poll: still running. Second: VIDEO_READY.
        h2.job_probe_states = [
            _job_state("INFERENCE_RUNNING"),
            _job_state("VIDEO_READY", output={
                "file": "/content/muse_h3/output/shao_nian_h3_A.mp4",
                "frames": 274, "sha256": h2.sha,
                "seconds": 274 / 24, "with_audio": True}),
        ]
        result = h2.resume()
        self.assertTrue(result.get("resumed"))
        self.assertEqual(h2.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(h2.allocate_ids, [])
        self.assertEqual(h2.stop_cli_sessions, [session])
        self.assertEqual(len(h2.archive_calls), 1)

    # -- TASK-006 P1-B: restart-safe bootstrap, duplicate-allocation guard --
    def test_restart_mid_bootstrap(self):
        # The controller died after init_remote, before the worker was
        # launched. Resume re-runs the bootstrap idempotently
        # (init_remote skipped, the rest done once), launches the worker,
        # and finishes — no second GPU, no re-inference.
        h = self.h
        session = self._construct_active_run("GPU_ALLOCATED")
        rm.update_run(h.root, RUN_ID, bootstrap={
            "current_step": "init_remote",
            "steps": {"init_remote": {"state": "done",
                                      "at_utc": "2026-11-13T09:00:00Z"}}})
        h2 = h.restart()
        self.h = h2  # tearDown closes the live harness, not the dead one
        h2.query_plan = [SessionState.ACTIVE, SessionState.ACTIVE,
                         SessionState.STOPPED]
        # First probe: no worker state yet. Second (after the resumed
        # bootstrap launches the worker): VIDEO_READY.
        h2.job_probe_states = [
            "MUSE_H3_JOB_STATE_MISSING",
            _job_state("VIDEO_READY", output={
                "file": "/content/muse_h3/output/shao_nian_h3_A.mp4",
                "frames": 274, "sha256": h2.sha,
                "seconds": 274 / 24, "with_audio": True}),
        ]
        result = h2.resume()
        self.assertTrue(result.get("resumed"))
        self.assertEqual(h2.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(h2.manifest()["recovery_generation"], 1)
        # Exactly one allocation ever (the original); the resume launched
        # the worker once and stopped the same session.
        self.assertEqual(h2.allocate_ids, [])
        self.assertEqual(h2.stop_cli_sessions, [session])
        self.assertEqual(len(h2.archive_calls), 1)
        # The bootstrap step record survived and gained launch_worker.
        steps = h2.manifest()["bootstrap"]["steps"]
        self.assertEqual(steps["init_remote"]["state"], "done")
        self.assertEqual(steps["launch_worker"]["state"], "done")
        h2.assert_mp4_unchanged()
        h2.assert_ledger_unchanged()

    def test_duplicate_controller_refused_without_allocating(self):
        # A second controller for the same task while the first holds the
        # task lock must refuse before allocating anything.
        h = self.h
        from h3_task_lock import TaskLock
        with TaskLock(h.root, TASK):
            with self.assertRaisesRegex(RuntimeError, "locked by another"):
                h.run()
        self.assertEqual(h.allocate_ids, [])
        # Nothing was created: the lock is taken before the manifest.
        self.assertFalse((h.root / "model_cache" / "run_manifests"
                          / f"{RUN_ID}.json").exists())

    def test_transient_poll_failures_recover(self):
        # Three dead probes (the degrade threshold) then a live one: the
        # run records the degradation as a transient and still completes.
        h = self.h
        h.job_probe_states = ["MUSE_H3_JOB_STATE_MISSING"] * 3
        result = h.run()
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(result["delivery"]["drive_file_id"], "drive-file-1")
        manifest = h.manifest()
        self.assertIsNotNone(manifest.get("last_transient_error"))
        self.assertIn("poll degraded",
                      manifest["last_transient_error"]["error"])
        self.assertIn("poll_degraded", h.audit_jsonl_text())
        self.assertEqual(len(h.allocate_ids), 1)
        self.assertEqual(len(h.stop_cli_sessions), 1)

    def test_duplicate_recovery_returns_already_completed(self):
        # Resume twice: the second resume returns already-completed
        # without allocating, stopping, or re-archiving.
        h = self.h
        h.run()
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        h2 = h.restart()
        self.h = h2  # tearDown closes the live harness, not the dead one
        result = h2.resume()
        self.assertFalse(result.get("resumed"))
        self.assertIn("already completed", result.get("reason", ""))
        self.assertEqual(h2.allocate_ids, [])
        self.assertEqual(h2.archive_calls, [])
        self.assertEqual(h2.stop_cli_sessions, [])

    def test_drive_verified_but_gpu_unknown_refuses(self):
        # The video is durable but the GPU state cannot be read — resume
        # must refuse, never allocate a second GPU, never stop blindly.
        h = self.h
        self._construct_active_run("DRIVE_VERIFIED")
        h2 = h.restart()
        self.h = h2  # tearDown closes the live harness, not the dead one
        h2.query_plan = [SessionQueryError("sessions list timed out")]
        result = h2.resume()
        self.assertFalse(result.get("resumed"))
        self.assertIn("UNKNOWN", result.get("reason", ""))
        self.assertEqual(h2.allocate_ids, [])
        self.assertEqual(h2.stop_cli_sessions, [])
        self.assertEqual(h2.archive_calls, [])


# ----------------------------------------------------------------------
# GPU faults (TASK-005 §8).
# ----------------------------------------------------------------------
class GpuFaultTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)

    def tearDown(self):
        self.h.close()

    def test_capacity_503_retries_once(self):
        h = self.h
        h.new_failures = [RuntimeError("HTTP 503 Service Unavailable")]
        result = h.run()
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        # Two minted ids (one per attempt), one successful `new`, and the
        # run owns the second id.
        self.assertEqual(len(h.allocate_ids), 2)
        self.assertEqual(len([c for c in h.run_cli_calls if c[0] == "new"]), 2)
        self.assertEqual(h.manifest().get("session_id"), h.allocate_ids[1])
        self.assertEqual(h.stop_cli_sessions, [h.allocate_ids[1]])

    def test_late_allocation_is_stopped_not_leaked(self):
        h = self.h
        h.new_failures = [RuntimeError("connection reset by peer")]
        h.inspect_plan = [
            {"cu_balance": 150.0, "rate": 5.0, "sessions": []},
            lambda hh: {"cu_balance": 150.0, "rate": 5.0, "sessions": [
                {"name": hh.allocate_ids[-1]}]},
        ]
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("Late allocation appeared", str(ctx.exception))
        self.assertIn(h.allocate_ids[0], str(ctx.exception))
        # The late session was stopped by the outer finally.
        self.assertEqual(h.stop_cli_sessions, h.allocate_ids)
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertIn("Late allocation", h.manifest_state()[2] or "")
        self.assertEqual(h.archive_calls, [])
        h.assert_mp4_unchanged()

    def test_gpu_type_mismatch(self):
        h = self.h
        h.probe_output = "Tesla T4, 15360"
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("GPU hardware mismatch", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertEqual(len(h.stop_cli_sessions), 1)
        self.assertEqual(h.archive_calls, [])

    def test_gpu_rate_unknown(self):
        h = self.h
        h.inspect_plan = [
            {"cu_balance": 150.0, "rate": 5.0, "sessions": []},
            {"cu_balance": 150.0, "rate": None, "sessions": []},
        ]
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("Unknown/excessive live CU/hr rate", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertEqual(len(h.stop_cli_sessions), 1)
        self.assertEqual(h.archive_calls, [])

    def test_stop_timeout(self):
        h = self.h
        h.query_plan = [SessionQueryError("sessions list timed out")]
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("Stop not verified", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertIn("Stop not verified", h.manifest_state()[2] or "")
        self.assertEqual(h.query_idx, 3)  # three stop attempts
        self.assertEqual(h.stop_cli_sessions, [])  # never got far enough
        # The video is durable locally but was NOT delivered: a failed
        # stop is a failed run, not a success.
        self.assertEqual(h.manifest_state()[3].get("delivery_state"),
                         "COMPLETE_LOCAL")
        self.assertEqual(h.archive_calls, [])
        h.assert_mp4_unchanged()

    def test_stop_nonzero(self):
        h = self.h
        h.query_plan = [SessionState.ACTIVE]
        h.stop_failures = [RuntimeError("stop failed"),
                           RuntimeError("stop failed"),
                           RuntimeError("stop failed")]
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("Stop not verified", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertEqual(len(h.stop_cli_sessions), 3)
        self.assertEqual(h.archive_calls, [])

    def test_stop_success_but_session_persists(self):
        h = self.h
        h.query_plan = [SessionState.ACTIVE]  # every query says ACTIVE
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("Stop not verified", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertEqual(len(h.stop_cli_sessions), 3)
        self.assertEqual(h.query_idx, 6)  # pre-stop + post-stop per attempt
        self.assertEqual(h.archive_calls, [])

    def test_sessions_query_failure_preflight(self):
        h = self.h
        h._real_record("muse-h3-prior001", "G4", "h3-prior-run",
                       "2026-11-13T06:00:00+00:00", path=h.state_path,
                       production_task_id=TASK)
        h.query_plan = [SessionQueryError("cannot list sessions")]
        with self.assertRaises(SessionQueryError):
            h.run()
        self.assertEqual(h.allocate_ids, [])
        self.assertEqual(h.stop_cli_sessions, [])
        self.assertEqual(h.manifest_state()[0], "TASK_AUTHORIZED")

    def test_foreign_session_preflight(self):
        h = self.h
        h.inspect_plan = [{"cu_balance": 150.0, "rate": 5.0, "sessions": [
            {"name": FOREIGN_SESSION}]}]
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("Existing Colab sessions present", str(ctx.exception))
        self.assertEqual(h.allocate_ids, [])
        self.assertEqual(h.stop_cli_sessions, [])
        self.assertEqual(h.manifest_state()[0], "TASK_AUTHORIZED")

# ----------------------------------------------------------------------
# Drive faults (TASK-005 §8).
# ----------------------------------------------------------------------
def _success_archive(file_id):
    return {"output_folder_id": "folder-1",
            "files": {"A.mp4": {"drive_file_id": file_id, "size": 8,
                                "server_md5": "x" * 32,
                                "sha256": hashlib.sha256(b"fake-mp4").hexdigest(),
                                "parent": "folder-1"}}}


class DriveFaultTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)

    def tearDown(self):
        self.h.close()

    def _run_delivery_failure(self, error_msg):
        """Full chain whose Drive upload fails on every attempt, then an
        archive-only resume from a fresh controller process."""
        h = self.h
        h.archive_plan = [RuntimeError(error_msg)] * 3
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("Drive delivery failed after 3 attempts",
                      str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "COMPLETED_AWAITING_ARCHIVE")
        self.assertEqual(h.manifest_state()[3].get("delivery_state"),
                         "DURABLE_UPLOAD_STARTED")
        self.assertEqual(len(h.archive_calls), 3)
        self.assertEqual(len(h.allocate_ids), 1)
        self.assertEqual(len(h.stop_cli_sessions), 1)
        h.assert_mp4_unchanged()
        h.assert_ledger_unchanged()
        h2 = h.restart()
        self.h = h2
        result = h2.resume()
        self.assertTrue(result.get("resumed"))
        self.assertEqual(h2.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(h2.allocate_ids, [])
        self.assertEqual(len(h2.archive_calls), 1)
        h2.assert_mp4_unchanged()
        return h2

    def test_oauth_expired(self):
        self._run_delivery_failure(
            "401 Unauthorized: access token expired")

    def test_forbidden_403(self):
        h2 = self._run_delivery_failure(
            "403 Forbidden: insufficient Drive permissions")
        self.assertIn("403", self.h.manifest_state()[2] or "")

    def test_tls_proxy_failure(self):
        self._run_delivery_failure(
            "SSLError: [SSL: WRONG_VERSION_NUMBER] wrong version number")

    def test_rate_limit_then_success(self):
        h = self.h
        h.archive_plan = [RuntimeError("HTTP 429 Too Many Requests"),
                          RuntimeError("HTTP 429 Too Many Requests"),
                          _success_archive("drive-file-1")]
        result = h.run()
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(len(h.archive_calls), 3)

    def test_upload_succeeded_but_no_receipt(self):
        h = self.h
        h.archive_plan = [RuntimeError("receipt lost: connection dropped"),
                          _success_archive("drive-file-7")]
        result = h.run()
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(h.manifest_state()[3].get("drive_file_id"),
                         "drive-file-7")
        # A second delivery reuses the recorded id; it does not upload
        # again and does not create a second Drive file.
        reused = hc.deliver_variant_to_drive_idempotent(
            "A", RUN_ID, h.video_info)
        self.assertTrue(reused.get("reused_existing"))
        self.assertEqual(reused.get("reused_via"), "recorded_id")
        self.assertEqual(reused.get("drive_file_id"), "drive-file-7")
        self.assertEqual(len(h.archive_calls), 2)

    def test_same_file_submitted_twice(self):
        h = self.h
        h.run()
        self.assertEqual(len(h.archive_calls), 1)
        reused = hc.deliver_variant_to_drive_idempotent(
            "A", RUN_ID, h.video_info)
        self.assertTrue(reused.get("reused_existing"))
        self.assertEqual(len(h.archive_calls), 1)

    def test_server_metadata_delay(self):
        # The REAL archive path: upload succeeds, but the server metadata
        # re-query returns the wrong parent (metadata not yet visible),
        # so the parent/trash check refuses the archive — three times.
        h = self.h
        h.construct_manifest("ARTIFACT_DURABLE", artifacts={
            "local_sha256": h.sha, "local_frames": 274,
            "delivery_state": "COMPLETE_LOCAL"})
        h.unpatch(hc, "archive_variant_to_drive")
        upload_calls = []
        h._po(drive_depot, "ensure_folder", side_effect=(
            lambda service, parent, name, allow_write=True:
            "root-folder" if name == "Muse-H3-Video-Outputs"
            else "run-folder-1"))

        def _upload(service, source, folder_id, name, rel, sha, hf_rev):
            upload_calls.append((folder_id, name))
            return {"drive_file_id": "drive-file-9", "size": 8,
                    "server_md5": "x" * 32, "sha256": h.sha,
                    "parent": "run-folder-1"}

        h._po(drive_depot, "upload_one", side_effect=_upload)
        h._po(drive_depot, "get_file_evidence", return_value={
            "id": "drive-file-9", "name": "A.mp4", "size": 8,
            "md5Checksum": "x" * 32, "parents": ["some-other-folder"],
            "trashed": False, "mimeType": "video/mp4"})
        with self.assertRaises(RuntimeError) as ctx:
            hc.deliver_variant_to_drive("A", RUN_ID, h.video_info)
        self.assertIn("Drive delivery failed after 3 attempts",
                      str(ctx.exception))
        self.assertEqual(len(upload_calls), 3)
        self.assertEqual(h.manifest_state()[3].get("delivery_state"),
                         "DURABLE_UPLOAD_STARTED")
        h.assert_mp4_unchanged()

    def test_download_interrupted(self):
        h = self.h
        h.download_exc = RuntimeError("download interrupted mid-file")
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("download interrupted", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertEqual(h.archive_calls, [])
        h.assert_mp4_unchanged()


# ----------------------------------------------------------------------
# Worker faults (TASK-005 §8).
# ----------------------------------------------------------------------
class WorkerFaultTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)

    def tearDown(self):
        self.h.close()

    def test_comfyui_install_failure(self):
        h = self.h
        h.job_probe_states = [_job_state("FAILED", error={
            "stage": "ensure_comfy", "error_type": "RuntimeError",
            "error": "git clone failed"})]
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("ensure_comfy", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertIn("ensure_comfy", h.manifest_state()[2] or "")
        self.assertEqual(len(h.log_download_calls), 1)
        self.assertEqual(len(h.stop_cli_sessions), 1)
        self.assertEqual(h.archive_calls, [])

    def test_prompt_400(self):
        h = self.h
        h.job_probe_states = [_job_state("FAILED", error={
            "stage": "inference_submit", "error_type": "RuntimeError",
            "error": "POST /prompt returned 400: node type missing"})]
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("400", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertEqual(len(h.stop_cli_sessions), 1)
        self.assertEqual(h.archive_calls, [])

    def test_control_channel_disconnect(self):
        # TASK-006 P1-B: a dead control channel is transient, not a task
        # failure. The poll loop degrades (audit event + manifest
        # transient error) and keeps polling; only the budget wall time
        # ends the run. A fake clock advances on every sleep so the wall
        # time (3600 s, mocked allowed_runtime_seconds) arrives fast.
        h = self.h
        h.job_probe_states = ["MUSE_H3_JOB_STATE_MISSING"] * 200
        now = [time.monotonic()]

        def _sleep(s):
            now[0] += s

        with mock.patch.object(hc.time, "sleep", side_effect=_sleep), \
                mock.patch.object(hc.time, "monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(TimeoutError) as ctx:
                h.run()
        self.assertIn("did not finish before the budget wall time",
                      str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        # The run recorded the degradation as a transient, never as the
        # failure reason (the failure reason is the wall time).
        manifest = h.manifest()
        self.assertIsNotNone(manifest.get("last_transient_error"))
        self.assertIn("poll degraded",
                      manifest["last_transient_error"]["error"])
        self.assertIn("poll_degraded", h.audit_jsonl_text())
        # The session was stopped, not left running.
        self.assertEqual(len(h.stop_cli_sessions), 1)
        self.assertEqual(h.archive_calls, [])

    def test_history_unreadable(self):
        h = self.h
        h.job_probe_states = [_job_state("VIDEO_READY", output={
            "file": "/content/muse_h3/output/shao_nian_h3_A.mp4",
            "frames": 274})]  # no sha256: the done record is unusable
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("sha256", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertEqual(h.archive_calls, [])

    def test_savevideo_alternate_shape(self):
        # The worker's poll sequence walks every stage; the run completes
        # and the manifest records MODELS_READY + INFERENCE_COMPLETED.
        h = self.h
        h.job_probe_states = [
            _job_state("JOB_STARTED"),
            _job_state("RESTORING_MODELS"),
            _job_state("MODELS_READY"),
            _job_state("INFERENCE_RUNNING"),
            _job_state("INFERENCE_COMPLETED"),
            _job_state("VIDEO_READY", output={
                "file": "/content/muse_h3/output/shao_nian_h3_A.mp4",
                "frames": 274, "sha256": h.sha,
                "seconds": 274 / 24, "with_audio": True}),
        ]
        result = h.run()
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        stages = [e.get("detail", {}).get("to") for e in h.audit_events()
                  if e.get("event") == "STAGE"]
        self.assertIn("MODELS_READY", stages)
        self.assertIn("INFERENCE_COMPLETED", stages)

    def test_mp4_undecodable(self):
        h = self.h
        h.download_exc = RuntimeError(
            "ffprobe failed: Invalid data found when processing input")
        with self.assertRaises(RuntimeError) as ctx:
            h.run()
        self.assertIn("ffprobe", str(ctx.exception))
        self.assertEqual(h.manifest_state()[0], "FAILED")
        self.assertIn("ffprobe", h.manifest_state()[2] or "")
        # The undecodable file is preserved, never delivered.
        self.assertEqual(h.archive_calls, [])
        h.assert_mp4_unchanged()


# ----------------------------------------------------------------------
# Delivery layer: the real upload_one reuse path (TASK-005 §5.2).
# ----------------------------------------------------------------------
class DeliverLayerTest(unittest.TestCase):
    def test_upload_one_reuses_matching_file(self):
        import drive_resumable

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            local = root / "A.mp4"
            local.write_bytes(b"fake-mp4")
            md5 = hashlib.md5(b"fake-mp4").hexdigest()
            sha = hashlib.sha256(b"fake-mp4").hexdigest()
            service = mock.Mock()
            with mock.patch.object(drive_depot, "list_named", return_value=[
                    {"id": "drive-file-5", "name": "A.mp4"}]), \
                    mock.patch.object(drive_depot, "get_file_evidence",
                                      return_value={
                                          "id": "drive-file-5", "size": 8,
                                          "md5Checksum": md5,
                                          "trashed": False,
                                          "parents": ["folder-1"],
                                          "mimeType": "video/mp4"}), \
                    mock.patch.object(drive_depot, "verify_uploaded_file",
                                      return_value={
                                          "drive_file_id": "drive-file-5",
                                          "server_md5": md5,
                                          "server_verified": True}), \
                    mock.patch.object(
                        drive_resumable, "upload_with_persistent_session",
                        side_effect=AssertionError("must not upload")):
                entry = drive_depot.upload_one(
                    service, local, "folder-1", "A.mp4", "A.mp4", sha,
                    "54472221d27c695fca9621b0c3ed52f06c4af707")
            self.assertEqual(entry["drive_file_id"], "drive-file-5")
            self.assertTrue(entry["server_verified"])


# ----------------------------------------------------------------------
# Credential leakage: the redactor and the persisted artifacts.
# ----------------------------------------------------------------------
_TOKEN_SHAPES = [re.compile(r"ya29\.[A-Za-z0-9_.\-]{10,}"),
                 re.compile(r"Bearer\s+[A-Za-z0-9_.\-~+/=]{10,}"),
                 re.compile(r"1//[A-Za-z0-9_.\-]{10,}")]


class CredentialLeakTest(unittest.TestCase):
    def test_redact_sensitive_text(self):
        from reference_v2.colab_manager import redact_sensitive_text

        # Built by concatenation so the source file contains no literal
        # token-shaped string (the secret scan flags those); the value
        # fed to the redactor at runtime is a full-shaped token.
        token = "ya29." + "CANARY_ACCESS_abcdefghij"
        text = ("token " + token + " and "
                "Authorization: Bearer CANARY_BEARER_1234567890 and "
                "refresh 1//CANARY_REFRESH_1234567890")
        self.assertNotIn("CANARY", redact_sensitive_text(text))

    def test_real_run_cli_redacts(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "colab"
            # The token-shaped line is built by concatenation so the
            # source file contains no literal token-shaped string.
            script.write_text(
                "#!/bin/sh\n"
                "echo 'ya29." + "CANARY_ACCESS_abcdefghij'\n"
                "echo 'Authorization: Bearer CANARY_BEARER_1234567890' >&2\n"
                "exit 1\n")
            script.chmod(0o755)
            with mock.patch.object(hc, "resolve_colab_executable",
                                  return_value=str(script)):
                with self.assertRaises(RuntimeError) as ctx:
                    hc.run_cli(["usage"], timeout=10)
                self.assertNotIn("CANARY", str(ctx.exception))
            script.write_text(
                "#!/bin/sh\n"
                "echo 'ya29." + "CANARY_ACCESS_abcdefghij'\n"
                "exit 0\n")
            script.chmod(0o755)
            with mock.patch.object(hc, "resolve_colab_executable",
                                  return_value=str(script)):
                result = hc.run_cli(["usage"], timeout=10)
            self.assertNotIn("CANARY", result["stdout"])

    def test_chain_artifacts_contain_no_token_shapes(self):
        h = ChainHarness(self)
        try:
            h.query_plan = [SessionQueryError("sessions list timed out")]
            with self.assertRaises(RuntimeError):
                h.run()
            for path in h.root.rglob("*"):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for pattern in _TOKEN_SHAPES:
                    self.assertIsNone(
                        pattern.search(text),
                        f"token-shaped string in {path}: "
                        f"{pattern.search(text)}")
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
