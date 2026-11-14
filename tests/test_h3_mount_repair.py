"""Mock tests for the 2026-11-11 H3 mount repair (items 3-5, 7-8).

Covers, with no GPU, no network and no Drive writes:
  * authorization inside 120s vs outside 120s (runs 6-7 scenario) under the
    old 120s remote timeout and the patched 600s timeout;
  * the WebSocket credential-hook blocking interval;
  * mount failure after authorization (and the CLI exit-code gap);
  * structured mount timeline phases, durations, and URI redaction;
  * the vendored CLI drivemount patch presence (fail-closed);
  * the remote drive.mount signature probe;
  * CU monitoring with the unbilled-session estimate (baseline 165.27 and
    hard stop 135.27 are never rewritten);
  * VM restart (ledger survives, baseline preserved) and session cleanup;
  * the 40GB Drive->Colab restore capacity detection (disk, throughput, cost).

Run: .venv/bin/python -m unittest discover -s tests -v
"""
import datetime
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from h3_budget import (
    allowed_runtime_seconds,
    effective_balance_cu,
    estimate_unbilled_cu,
    load_or_create_budget_ledger,
    reconcile_budget,
)
from h3_controller import (
    check_mount_signature,
    cli_drivemount_timeout_patch_present,
    cli_post_body_patch_present,
    mount_drive_interactive,
    stop_and_verify_session,
)
from h3_mount_timeline import (
    build_mount_timeline,
    parse_stdout_markers,
    summarize_timeline,
)
from h3_remote_worker import (
    _download_progress,
    check_restore_capacity,
    estimate_restore,
    project_restore_throughput,
)
from h3_session_watchdog import (
    record_session,
    reconcile_on_startup,
    should_stop,
)

BASE = datetime.datetime(2026, 11, 11, 3, 0, 0, tzinfo=datetime.timezone.utc)
TOTAL_BYTES = 40073842159


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _history_events(propagated_sec=None, fail_sec=None):
    """Synthetic CLI history events. propagated_sec: seconds after BASE when
    drive_auth_success happened. fail_sec: seconds after BASE when the mount
    cell errored (automation_result with an error output)."""
    events = [
        {"timestamp": _iso(BASE), "event_type": "automation", "op": "drivemount",
         "path": "/content/drive", "code": "drive.mount('/content/drive', timeout_ms=600000)"},
        {"timestamp": _iso(BASE + datetime.timedelta(seconds=2)), "event_type": "colab_request",
         "type": "dfs_ephemeral", "colab_msg_id": "msg-1"},
        {"timestamp": _iso(BASE + datetime.timedelta(seconds=5)), "event_type": "drive_auth_needed",
         "uri": "https://example.invalid/auth"},
    ]
    if propagated_sec is not None:
        events.append({"timestamp": _iso(BASE + datetime.timedelta(seconds=propagated_sec)),
                       "event_type": "drive_auth_success"})
    if fail_sec is not None:
        events.append({"timestamp": _iso(BASE + datetime.timedelta(seconds=fail_sec)),
                       "event_type": "automation_result", "op": "drivemount",
                       "outputs": [{"output_type": "error", "ename": "ValueError",
                                    "evalue": "mount failed", "traceback": ["ValueError: mount failed"]}]})
    else:
        fin = (propagated_sec if propagated_sec is not None else 10) + 5
        events.append({"timestamp": _iso(BASE + datetime.timedelta(seconds=fin)),
                       "event_type": "automation_result", "op": "drivemount",
                       "outputs": [{"text": "Mounted at /content/drive\n"}]})
    return events


def _stdout_lines(propagated_sec=None, fail=False):
    """Synthetic timestamped drivemount stdout lines (iso_ts, text)."""
    lines = [
        (_iso(BASE + datetime.timedelta(seconds=1)), "[colab] Mounting Google Drive to '/content/drive' on muse-h3-x..."),
        (_iso(BASE + datetime.timedelta(seconds=5)), "[colab] REQUIRED: Google Drive Authorization needed."),
        (_iso(BASE + datetime.timedelta(seconds=6)), "https://example.invalid/auth"),
    ]
    if propagated_sec is not None:
        lines.append((_iso(BASE + datetime.timedelta(seconds=propagated_sec - 5)), "[colab] Authorizing VM..."))
        lines.append((_iso(BASE + datetime.timedelta(seconds=propagated_sec)), "[colab] Credentials propagated. Resuming mount..."))
    if fail:
        lines.append((_iso(BASE + datetime.timedelta(seconds=120)), "ValueError: mount failed"))
    return lines


class TimelineTest(unittest.TestCase):
    def test_auth_within_120s_mounts(self):
        tl = build_mount_timeline("muse-h3-x", "run-1", _stdout_lines(62), _history_events(62), 0,
                                  _iso(BASE), _iso(BASE + datetime.timedelta(seconds=67)))
        self.assertEqual(tl["outcome"], "mounted")
        self.assertAlmostEqual(tl["phase_durations_sec"]["auth_wait_total"], 62.0, places=0)
        self.assertTrue(tl["auth_wait_within_remote_timeout"])
        self.assertFalse(tl["would_have_failed_at_120s"])
        self.assertIsNone(tl["error"])

    def test_auth_at_300s_mounts_with_600s_patch(self):
        # Runs 6-7 scenario under the fix: auth completes ~5 min in.
        tl = build_mount_timeline("muse-h3-x", "run-1", _stdout_lines(300), _history_events(300), 0,
                                  _iso(BASE), _iso(BASE + datetime.timedelta(seconds=305)),
                                  remote_timeout_ms=600000)
        self.assertEqual(tl["outcome"], "mounted")
        self.assertAlmostEqual(tl["phase_durations_sec"]["auth_wait_total"], 300.0, places=0)
        self.assertTrue(tl["auth_wait_within_remote_timeout"])
        self.assertTrue(tl["would_have_failed_at_120s"])
        self.assertAlmostEqual(tl["phase_durations_sec"]["ws_blocked_sec"], 298.0, places=0)

    def test_auth_at_300s_fails_under_old_120s(self):
        tl = build_mount_timeline("muse-h3-x", "run-1", _stdout_lines(None, fail=True),
                                  _history_events(None, fail_sec=120), 1,
                                  _iso(BASE), _iso(BASE + datetime.timedelta(seconds=120)),
                                  remote_timeout_ms=120000)
        self.assertEqual(tl["outcome"], "mount_failed")
        self.assertIn("mount failed", tl["error"])
        self.assertFalse(tl["auth_wait_within_remote_timeout"])
        self.assertIsNone(tl["credentials_propagated_utc"])

    def test_websocket_block_interval_recorded(self):
        tl = build_mount_timeline("muse-h3-x", "run-1", _stdout_lines(302), _history_events(302), 0,
                                  _iso(BASE), _iso(BASE + datetime.timedelta(seconds=307)))
        # Kernel asked for auth at +2s, credentials propagated at +302s:
        # the hook spent ~300s blocked on the WebSocket dispatch path.
        self.assertAlmostEqual(tl["phase_durations_sec"]["ws_blocked_sec"], 300.0, places=0)
        self.assertTrue(tl["would_have_failed_at_120s"])

    def test_mount_fails_after_authorization_despite_exit_zero(self):
        # The CLI exits 0 even when the remote mount cell raises (run_automation
        # prints error outputs but never exits nonzero); the timeline error
        # marker, not the returncode, is what must catch this.
        tl = build_mount_timeline("muse-h3-x", "run-1", _stdout_lines(60, fail=True),
                                  _history_events(60, fail_sec=65), 0,
                                  _iso(BASE), _iso(BASE + datetime.timedelta(seconds=65)))
        self.assertEqual(tl["outcome"], "mount_failed")
        self.assertIn("mount failed", tl["error"])
        self.assertIsNotNone(tl["credentials_propagated_utc"])

    def test_stdout_markers_never_store_uris(self):
        marks = parse_stdout_markers([
            (_iso(BASE), "[colab] REQUIRED: Google Drive Authorization needed."),
            (_iso(BASE), "https://colab.research.google.com/tun/m/authorize-for-drive-credentials-ephem?state=SECRET123"),
            (_iso(BASE), "ValueError: mount failed, see https://example.invalid/faq for details"),
        ])
        blob = json.dumps(marks, ensure_ascii=False, default=str)
        self.assertNotIn("SECRET123", blob)
        self.assertNotIn("example.invalid", blob)
        self.assertIn("mount failed", marks.get("mount_error_text", ""))

    def test_timeline_summary_line(self):
        tl = build_mount_timeline("muse-h3-x", "run-1", _stdout_lines(300), _history_events(300), 0,
                                  _iso(BASE), _iso(BASE + datetime.timedelta(seconds=305)))
        summary = summarize_timeline(tl)
        self.assertIn("outcome=mounted", summary)
        self.assertIn("would_have_failed_at_120s", summary)


class CliPatchTest(unittest.TestCase):
    """Patch-presence checks against isolated fixture trees (TASK-002 §3.4).

    The checks take an explicit search root, so these tests never depend
    on the machine's installed CLI: pristine 0.7.5 fixture files must
    report "absent", and the same files with the vendored patches applied
    in a temp dir must report "present" — with the real marker strings.
    No GPU, no network, no installed CLI is touched. The production
    preflight still calls the checks with no argument (real site-packages).
    """

    FIXTURES = ROOT / "tests" / "fixtures" / "pristine-colab-cli-0.7.5" / "colab_cli"
    PATCH_DIR = ROOT / "patches" / "google-colab-cli-0.7.5"

    def _pristine_tree(self, tmp):
        import shutil

        tree = Path(tmp) / "site-packages"
        shutil.copytree(self.FIXTURES, tree / "colab_cli")
        return tree

    def _apply_vendored_patches(self, tree):
        for name in ("proxy_post_body.patch", "drivemount_timeout.patch"):
            subprocess.run(
                ["git", "apply", str(self.PATCH_DIR / name)],
                cwd=str(tree), check=True, capture_output=True, text=True,
            )

    def test_pristine_cli_reports_patch_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._pristine_tree(tmp)
            self.assertFalse(cli_drivemount_timeout_patch_present([str(tree)]))
            self.assertFalse(cli_post_body_patch_present([str(tree)]))

    def test_patched_cli_reports_patch_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._pristine_tree(tmp)
            self._apply_vendored_patches(tree)
            self.assertTrue(cli_drivemount_timeout_patch_present([str(tree)]))
            self.assertTrue(cli_post_body_patch_present([str(tree)]))

    def test_injected_code_contains_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._pristine_tree(tmp)
            self._apply_vendored_patches(tree)
            found = (tree / "colab_cli" / "commands" / "automation.py").read_text(
                encoding="utf-8", errors="ignore")
            self.assertIn("drive.mount('{path}', timeout_ms=600000)", found)
            self.assertIn("INTERACTIVE_AUTOMATION_TIMEOUT_SEC = 660", found)

    def test_post_body_patch_still_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._pristine_tree(tmp)
            self._apply_vendored_patches(tree)
            found = (tree / "colab_cli" / "client.py").read_text(
                encoding="utf-8", errors="ignore")
            self.assertIn('kwargs["data"] = b"{}"', found)
            self.assertIn('if method == "POST"', found)

    def test_default_check_returns_bool_without_installed_cli(self):
        # In a clean checkout there may be no installed colab_cli at all;
        # the no-argument check must simply report False, never raise.
        self.assertIsInstance(cli_post_body_patch_present(), bool)
        self.assertIsInstance(cli_drivemount_timeout_patch_present(), bool)


class SignatureProbeTest(unittest.TestCase):
    def test_signature_with_timeout_ms(self):
        self.assertTrue(check_mount_signature(
            {"stdout": "MOUNT_SIGNATURE: (mount_path, force_remount=False, timeout_ms=120000, use_metadata_server=False)\n",
             "stderr": ""}))

    def test_signature_without_timeout_ms(self):
        self.assertFalse(check_mount_signature(
            {"stdout": "MOUNT_SIGNATURE: (mount_path, force_remount=False)\n", "stderr": ""}))

    def test_signature_missing_line_raises(self):
        with self.assertRaises(RuntimeError):
            check_mount_signature({"stdout": "something else\n", "stderr": ""})


class CuMonitoringTest(unittest.TestCase):
    LEDGER = {"initial_balance_cu": 165.27, "hard_stop_balance_cu": 135.27,
              "max_observed_spent_cu": 2.06, "project_budget_cu": 30.0,
              "per_session_budget_cu": 25.0}

    def test_estimate_unbilled_cu(self):
        started = "2026-11-11T02:00:00+00:00"
        now = datetime.datetime(2026, 11, 11, 2, 30, tzinfo=datetime.timezone.utc)
        self.assertAlmostEqual(estimate_unbilled_cu(started, 10.05, now=now), 5.025, places=3)
        self.assertEqual(estimate_unbilled_cu(None, 10.05, now=now), 0.0)
        self.assertEqual(estimate_unbilled_cu(started, None, now=now), 0.0)
        self.assertEqual(estimate_unbilled_cu("not-a-date", 10.05, now=now), 0.0)
        future = datetime.datetime(2026, 11, 11, 3, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(estimate_unbilled_cu(started, 10.05, now=future.replace(hour=1)), 0.0)

    def test_effective_balance(self):
        self.assertAlmostEqual(effective_balance_cu(163.21, 1.5), 161.71, places=2)
        self.assertAlmostEqual(effective_balance_cu(163.21, 0.0), 163.21, places=2)

    def test_allowed_runtime_seconds_uses_unbilled(self):
        plain = allowed_runtime_seconds(10.05, 163.21, dict(self.LEDGER))
        self.assertEqual(plain, 8686)
        with_unbilled = allowed_runtime_seconds(10.05, 163.21, dict(self.LEDGER), session_spent_cu=5.0, unbilled_cu=5.0)
        self.assertEqual(with_unbilled, 6895)
        self.assertLess(with_unbilled, plain)

    def test_allowed_runtime_seconds_blocked_at_hard_stop(self):
        # Unbilled spend brings the effective balance exactly to the hard stop.
        with self.assertRaises(RuntimeError):
            allowed_runtime_seconds(10.05, 163.21, dict(self.LEDGER),
                                    session_spent_cu=27.94, unbilled_cu=27.94)

    def test_watchdog_should_stop_with_unbilled(self):
        now = datetime.datetime(2026, 11, 11, 3, 0, tzinfo=datetime.timezone.utc)
        rec = {"deadline_utc": _iso(now + datetime.timedelta(hours=2)),
               "started_at_utc": _iso(now - datetime.timedelta(hours=1))}
        ledger = {"hard_stop_balance_cu": 135.27}
        # Posted 140 looks safe, but 1h x 10 CU/h unbilled = 130 effective.
        stop, reason = should_stop(rec, 140.0, ledger, now=now, rate_cu_h=10.0)
        self.assertTrue(stop)
        self.assertIn("hard-stop", reason)
        # Without a rate the old posted-balance-only behaviour applies.
        stop, _ = should_stop(rec, 140.0, ledger, now=now)
        self.assertFalse(stop)
        # Deadline reached stops regardless.
        rec["deadline_utc"] = _iso(now - datetime.timedelta(minutes=1))
        stop, reason = should_stop(rec, 115.0, ledger, now=now)
        self.assertTrue(stop)
        self.assertIn("deadline", reason)


class VmRestartAndCleanupTest(unittest.TestCase):
    def test_ledger_survives_restart_baseline_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ledger.json"
            ledger = load_or_create_budget_ledger(path, 165.27, "run-1", authorize_new_task=True)
            self.assertEqual(ledger["initial_balance_cu"], 165.27)
            self.assertEqual(ledger["hard_stop_balance_cu"], 135.27)
            reconcile_budget(ledger, 163.21, path)
            # Simulated VM restart: a fresh process reloads the same file.
            ledger2 = load_or_create_budget_ledger(path, 163.21, "run-2")
            self.assertEqual(ledger2["initial_balance_cu"], 165.27)
            self.assertEqual(ledger2["hard_stop_balance_cu"], 135.27)
            self.assertAlmostEqual(ledger2["max_observed_spent_cu"], 2.06, places=2)
            self.assertIn("run-1", ledger2["run_ids"])
            self.assertIn("run-2", ledger2["run_ids"])

    def test_reconcile_finds_active_managed_session(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "sessions.json"
            deadline = _iso(BASE + datetime.timedelta(hours=1))
            record_session("muse-h3-abc1234", "G4", "run-x", deadline, path=state)
            # Mock at the _run_colab level so the real strict parser runs:
            # the session line must be the canonical 0.7.5 format.
            fake = mock.Mock(returncode=0,
                             stdout="[muse-h3-abc1234] gpu-g4-xyz | Hardware: G4\n", stderr="")
            with mock.patch("h3_session_watchdog._run_colab", return_value=fake):
                rec = reconcile_on_startup("adc", path=state)
            self.assertEqual(rec["managed_active_sessions"], ["muse-h3-abc1234"])
            gone = mock.Mock(returncode=0, stdout="No active sessions\n", stderr="")
            with mock.patch("h3_session_watchdog._run_colab", return_value=gone):
                rec = reconcile_on_startup("adc", path=state)
            self.assertEqual(rec["managed_active_sessions"], [])

    def test_session_cleanup_verified(self):
        from h3_session_watchdog import SessionState
        with mock.patch("h3_session_watchdog.query_session_state",
                        side_effect=[SessionState.ACTIVE, SessionState.STOPPED]), \
             mock.patch("h3_controller.run_cli", return_value={}), \
             mock.patch("h3_controller.record"), \
             mock.patch("h3_session_watchdog.mark_session_stopped") as marked, \
             mock.patch("h3_controller._confirm_gpu_lease_stopped") as lease_confirmed, \
             mock.patch("time.sleep"):
            result = stop_and_verify_session("muse-h3-abc1234")
        self.assertTrue(result["stopped_confirmed"])
        marked.assert_called_once_with("muse-h3-abc1234", SessionState.STOPPED)
        lease_confirmed.assert_called_once_with("muse-h3-abc1234")

    def test_session_cleanup_orphan_risk_recorded(self):
        from h3_session_watchdog import SessionState
        with mock.patch("h3_session_watchdog.query_session_state",
                        return_value=SessionState.ACTIVE), \
             mock.patch("h3_controller.run_cli", return_value={}), \
             mock.patch("h3_controller.record") as record_mock, \
             mock.patch("h3_session_watchdog.mark_session_stopped") as marked, \
             mock.patch("h3_controller._confirm_gpu_lease_stopped") as lease_confirmed, \
             mock.patch("time.sleep"):
            result = stop_and_verify_session("muse-h3-abc1234")
        self.assertFalse(result["stopped_confirmed"])
        marked.assert_not_called()
        lease_confirmed.assert_not_called()
        events = [c.args[0].get("event") for c in record_mock.call_args_list if c.args]
        self.assertIn("CRITICAL_ORPHAN_RISK", events)


class RestoreCapacityTest(unittest.TestCase):
    def test_estimate_restore_disk_ok(self):
        est = estimate_restore(TOTAL_BYTES, 60 * 1024 ** 3)
        self.assertTrue(est["ok"])
        self.assertEqual(est["required_bytes"], TOTAL_BYTES + 5 * 1024 ** 3)

    def test_estimate_restore_disk_short(self):
        est = estimate_restore(TOTAL_BYTES, 40 * 1024 ** 3)
        self.assertFalse(est["ok"])
        self.assertIn("shortfall", est["reason"])

    def test_estimate_restore_cost_ok(self):
        est = estimate_restore(TOTAL_BYTES, None, measured_mbps=100.0, rate_cu_h=10.05, budget_cu=2.0)
        self.assertTrue(est["ok"])
        self.assertAlmostEqual(est["eta_sec"], 400.7, places=0)
        self.assertAlmostEqual(est["estimated_cost_cu"], 1.12, places=2)

    def test_estimate_restore_cost_exceeds_budget(self):
        est = estimate_restore(TOTAL_BYTES, None, measured_mbps=10.0, rate_cu_h=10.05, budget_cu=2.0)
        self.assertFalse(est["ok"])
        self.assertIn("exceeds session budget", est["reason"])

    def test_check_restore_capacity_disk_gate(self):
        with tempfile.TemporaryDirectory() as d:
            comfy = Path(d) / "ComfyUI"
            comfy.mkdir()
            usage = mock.Mock(total=100 * 1024 ** 3, used=40 * 1024 ** 3, free=60 * 1024 ** 3)
            with mock.patch("shutil.disk_usage", return_value=usage):
                est = check_restore_capacity(comfy, TOTAL_BYTES)
            self.assertTrue(est["ok"])
            usage.free = 30 * 1024 ** 3
            with mock.patch("shutil.disk_usage", return_value=usage):
                with self.assertRaises(RuntimeError):
                    check_restore_capacity(comfy, TOTAL_BYTES)

    def test_project_restore_throughput_blocks_slow_copy(self):
        est = project_restore_throughput(TOTAL_BYTES, 1_000_000, 0.01, rate_cu_h=10.05, budget_cu=2.0)
        self.assertTrue(est["ok"])
        with self.assertRaises(RuntimeError):
            project_restore_throughput(TOTAL_BYTES, 1_000_000, 0.1, rate_cu_h=10.05, budget_cu=2.0)


class DownloadProgressMilestoneTest(unittest.TestCase):
    """TASK-007: _download_progress fires on non-multiple ``done``
    values and advances its milestone; the throughput projection
    still runs exactly once per restore."""

    def test_fires_on_non_multiple_done_and_advances(self):
        milestone = [256 * 1024 * 1024]
        projected = [False]
        calls = []
        with mock.patch("h3_remote_worker.project_restore_throughput",
                        side_effect=lambda *a, **k: calls.append(a)):
            # Below the first milestone: silent.
            _download_progress("rel", 100 * 1024 * 1024, TOTAL_BYTES, 0.0,
                               projected, None, None, milestone)
            self.assertEqual(calls, [])
            # 300 MiB is NOT a multiple of 256 MiB: the old modulo
            # test would have skipped the projection entirely.
            _download_progress("rel", 300 * 1024 * 1024, TOTAL_BYTES, 0.0,
                               projected, None, None, milestone)
            self.assertEqual(len(calls), 1)
            self.assertEqual(milestone[0], 512 * 1024 * 1024)
            # Between milestones: quiet, no second projection.
            _download_progress("rel", 400 * 1024 * 1024, TOTAL_BYTES, 0.0,
                               projected, None, None, milestone)
            self.assertEqual(len(calls), 1)
            # Second boundary crossed: prints, but the projection
            # stays once-only.
            _download_progress("rel", 600 * 1024 * 1024, TOTAL_BYTES, 0.0,
                               projected, None, None, milestone)
            self.assertEqual(len(calls), 1)
            self.assertEqual(milestone[0], 768 * 1024 * 1024)
            self.assertTrue(projected[0])


def _fake_popen_factory(chunks, returncode, timeout_first=False):
    """Build a fake subprocess.Popen whose stdout yields `chunks` via os.read."""
    remaining = list(chunks)

    def _fake_read(fd, n):
        if remaining:
            return remaining.pop(0)
        return b""

    class FakeStdout:
        def fileno(self):
            return 99

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.stdout = FakeStdout()
            self._wait_calls = 0

        def wait(self, timeout=None):
            self._wait_calls += 1
            if timeout_first and self._wait_calls == 1:
                raise subprocess.TimeoutExpired(["colab"], timeout)
            return returncode

        def kill(self):
            pass

    return FakePopen, _fake_read


class MountDriveInteractiveTest(unittest.TestCase):
    def _run_mount(self, chunks, returncode, history, timeout_first=False):
        fake_popen, fake_read = _fake_popen_factory(chunks, returncode, timeout_first)
        with tempfile.TemporaryDirectory() as d:
            timeline_path = Path(d) / "mount_timeline.json"
            captured = io.StringIO()
            with mock.patch("sys.stdin"), \
                 mock.patch("h3_controller.resolve_colab_executable", return_value="/usr/bin/true"), \
                 mock.patch("subprocess.Popen", fake_popen), \
                 mock.patch("os.read", side_effect=fake_read), \
                 mock.patch("h3_controller.read_history_events", return_value=history), \
                 mock.patch("h3_controller.record"), \
                 redirect_stdout(captured):
                try:
                    tl = mount_drive_interactive("muse-h3-x", timeout=300, run_id="run-1",
                                                 timeline_path=timeline_path)
                    raised = None
                except RuntimeError as e:
                    tl = json.loads(timeline_path.read_text())
                    raised = e
            return tl, raised, captured.getvalue()

    def test_mount_success_forwards_output_and_writes_timeline(self):
        chunks = [("[colab] Mounting Google Drive to '/content/drive' on muse-h3-x...\n"
                   "[colab] REQUIRED: Google Drive Authorization needed.\n"
                   "https://example.invalid/auth\n"
                   "[colab] Authorizing VM...\n"
                   "[colab] Credentials propagated. Resuming mount...\n"
                   "Mounted at /content/drive\n").encode()]
        tl, raised, out = self._run_mount(chunks, 0, _history_events(62))
        self.assertIsNone(raised)
        self.assertEqual(tl["outcome"], "mounted")
        self.assertIn("Credentials propagated", out)
        self.assertNotIn("example.invalid", json.dumps(tl))

    def test_mount_failure_after_auth_raises_with_timeline(self):
        chunks = [("[colab] Mounting Google Drive to '/content/drive' on muse-h3-x...\n"
                   "[colab] Authorizing VM...\n"
                   "[colab] Credentials propagated. Resuming mount...\n"
                   "ValueError: mount failed\n").encode()]
        tl, raised, _ = self._run_mount(chunks, 1, _history_events(60, fail_sec=65))
        self.assertIsNotNone(raised)
        self.assertEqual(tl["outcome"], "mount_failed")
        self.assertIn("mount failed", tl["error"])

    def test_mount_subprocess_timeout_reports_timeout(self):
        tl, raised, _ = self._run_mount([], 1, _history_events(None), timeout_first=True)
        self.assertIsNotNone(raised)
        self.assertEqual(tl["outcome"], "timeout")
        self.assertIn("timeout", str(raised).lower())


if __name__ == "__main__":
    unittest.main()
