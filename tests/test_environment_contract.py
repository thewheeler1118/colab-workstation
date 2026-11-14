"""TASK-005 §6 (Issue #29): environment contract.

The checked-in environment_manifest.json pins the CLI (0.7.5 + both
production patches), ffmpeg/ffprobe and the allowed Python versions;
verify_environment() fails closed on any gated mismatch before a GPU
is allocated. The watchdog and the controller resolve the CLI through
the same auth_support function.

No GPU, no live CLI: the fake CLI is a shell script, the fake
site-packages are temp files, and the gate tests mock the live-env
reader.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_support  # noqa: E402
import h3_controller as hc  # noqa: E402
import h3_environment as he  # noqa: E402
import h3_session_watchdog as hwd  # noqa: E402

CLIENT_PATCHED = '''"""Fake patched client.py."""
import logging
def _issue_request(self, method, endpoint, **kwargs):
    if method == "POST" and "data" not in kwargs and "json" not in kwargs:
        kwargs["data"] = b"{}"
    return None
'''
CLIENT_UNPATCHED = '''"""Fake pristine client.py."""
def _issue_request(self, method, endpoint, **kwargs):
    return None
'''
AUTOMATION_PATCHED = '''INTERACTIVE_AUTOMATION_TIMEOUT_SEC = 660
def drivemount(path):
    drive.mount(path, timeout_ms=600000)
'''
AUTOMATION_UNPATCHED = '''INTERACTIVE_AUTOMATION_TIMEOUT_SEC = 600
def drivemount(path):
    drive.mount(path)
'''


def _fake_env(tmp: Path, patched: bool = True):
    """A fake CLI install: bin/colab (prints Version: 0.7.5) + a fake
    site-packages with colab_cli/client.py and automation.py."""
    site = tmp / "site"
    (site / "colab_cli" / "commands").mkdir(parents=True)
    (site / "colab_cli" / "client.py").write_text(
        CLIENT_PATCHED if patched else CLIENT_UNPATCHED)
    (site / "colab_cli" / "commands" / "automation.py").write_text(
        AUTOMATION_PATCHED if patched else AUTOMATION_UNPATCHED)
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    colab = bin_dir / "colab"
    colab.write_text('#!/bin/sh\nif [ "$1" = "version" ]; then echo "Version: 0.7.5"; fi\n')
    colab.chmod(0o755)
    return colab, [str(site)]


class GenerateTest(unittest.TestCase):
    def test_patched_env_records_all_fields(self):
        with tempfile.TemporaryDirectory() as d:
            colab, roots = _fake_env(Path(d), patched=True)
            m = he.generate_environment_manifest(cli_exe=str(colab), search_roots=roots)
        self.assertEqual(m["cli_version"], "0.7.5")
        self.assertTrue(m["cli_post_body_patch"])
        self.assertTrue(m["cli_drivemount_timeout_patch"])
        expected_hash = hashlib.sha256(CLIENT_PATCHED.encode()).hexdigest()
        self.assertEqual(m["cli_client_sha256"], expected_hash)
        pristine = ROOT / "tests/fixtures/pristine-colab-cli-0.7.5/colab_cli/client.py"
        self.assertEqual(m["cli_pristine_client_sha256"],
                         hashlib.sha256(pristine.read_bytes()).hexdigest())
        self.assertNotEqual(m["cli_client_sha256"], m["cli_pristine_client_sha256"])
        self.assertEqual(m["python_major_minor_allowed"], ["3.11", "3.12"])
        self.assertEqual(m["comfyui_commit"], "61e524bdba7a4e193b45c2697f1af83c1beb86d6")
        self.assertEqual(m["model_hf_revision"],
                         "54472221d27c695fca9621b0c3ed52f06c4af707")
        workflow = ROOT / "workflow_api.json"
        self.assertEqual(m["workflow_sha256"],
                         hashlib.sha256(workflow.read_bytes()).hexdigest())
        self.assertEqual(m["drive_transport"]["transport"], "requests")
        self.assertIn("PR-7", m["drive_transport"]["status"])
        # ffmpeg/ffprobe fields are consistent with the real tools.
        self.assertEqual(m["ffmpeg_present"], m["ffmpeg_version"] is not None)
        self.assertEqual(m["ffprobe_present"], m["ffprobe_version"] is not None)

    def test_unpatched_env_records_false_patches(self):
        with tempfile.TemporaryDirectory() as d:
            colab, roots = _fake_env(Path(d), patched=False)
            m = he.generate_environment_manifest(cli_exe=str(colab), search_roots=roots)
        self.assertEqual(m["cli_version"], "0.7.5")
        self.assertFalse(m["cli_post_body_patch"])
        self.assertFalse(m["cli_drivemount_timeout_patch"])

    def test_unresolvable_cli_records_nulls_and_false(self):
        # A CLI path that cannot run: version/hash stay None, patch
        # markers stay False — never invented, and the gate fails on them.
        m = he.generate_environment_manifest(cli_exe="/nonexistent/colab",
                                             search_roots=[])
        self.assertIsNone(m["cli_version"])
        self.assertIsNone(m["cli_client_sha256"])
        self.assertFalse(m["cli_post_body_patch"])
        self.assertFalse(m["cli_drivemount_timeout_patch"])

    def test_generate_records_control_plane_modules(self):
        # TASK-007.1: the manifest records one bool per control-plane
        # module, probed in the CLI's interpreter (here: the current
        # one, since the fake CLI has no sibling python). Values are
        # environment-dependent; keys and bool type are the contract.
        with tempfile.TemporaryDirectory() as d:
            colab, roots = _fake_env(Path(d), patched=True)
            m = he.generate_environment_manifest(cli_exe=str(colab),
                                                 search_roots=roots)
        self.assertEqual(set(m["control_plane_modules"]),
                         set(he.CONTROL_PLANE_MODULES))
        self.assertTrue(all(isinstance(v, bool)
                            for v in m["control_plane_modules"].values()))

    def test_generate_records_source_commit_from_release_commit(self):
        # TASK-007.1: in a synced dir the generator reads the sync's
        # RELEASE_COMMIT file (he.ROOT is patched to the fake dir, so
        # no other repo file is consulted).
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "RELEASE_COMMIT").write_text(
                "deadbeef" * 5 + "\n", encoding="utf-8")
            with mock.patch.object(he, "ROOT", root):
                m = he.generate_environment_manifest(cli_exe="/nonexistent/colab",
                                                     search_roots=[])
        self.assertEqual(m["source_commit"], "deadbeef" * 5)

    def test_generate_explicit_source_commit_wins(self):
        m = he.generate_environment_manifest(cli_exe="/nonexistent/colab",
                                             search_roots=[],
                                             source_commit="feedface" * 5)
        self.assertEqual(m["source_commit"], "feedface" * 5)

    def test_resolve_source_commit_git_repo(self):
        # The repo checkout itself: the live HEAD, nothing invented.
        import subprocess as sp

        head = sp.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                      capture_output=True, text=True,
                      timeout=10).stdout.strip()
        self.assertTrue(head)
        self.assertEqual(he.resolve_source_commit(), head)

    def test_resolve_source_commit_release_commit_fallback(self):
        # No .git: the sync's RELEASE_COMMIT file is the truth.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "RELEASE_COMMIT").write_text("cafe" * 10 + "\n",
                                                 encoding="utf-8")
            self.assertEqual(he.resolve_source_commit(root), "cafe" * 10)

    def test_resolve_source_commit_unknown_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(he.resolve_source_commit(Path(d)), "")


class VerifyTest(unittest.TestCase):
    def _manifest(self, **over):
        base = {
            "cli_version": "0.7.5",
            "cli_client_sha256": "a" * 64,
            "cli_post_body_patch": True,
            "cli_drivemount_timeout_patch": True,
            "python_major_minor_allowed": ["3.11", "3.12"],
            "ffmpeg_present": True,
            "ffprobe_present": True,
            "drive_transport": {"transport": "requests"},
            "control_plane_modules": {m: True
                                      for m in he.CONTROL_PLANE_MODULES},
            "source_commit": None,
        }
        base.update(over)
        return base

    def _live(self, **over):
        base = {
            "cli_version": "0.7.5",
            "cli_client_sha256": "a" * 64,
            "cli_post_body_patch": True,
            "cli_drivemount_timeout_patch": True,
            "python_major_minor": "3.12",
            "ffmpeg_present": True,
            "ffprobe_present": True,
            "cli_executable": "/fake/bin/colab",
            "drive_transport": {"transport": "requests"},
            "control_plane_modules": {m: True
                                      for m in he.CONTROL_PLANE_MODULES},
        }
        base.update(over)
        return base

    def test_matching_env_passes(self):
        with mock.patch.object(he, "_live_environment", return_value=self._live()):
            report = he.verify_environment(manifest=self._manifest())
        self.assertTrue(report["ok"])
        # 6 gated fields + python + transport + 5 control-plane modules
        # + source_commit = 14 checked entries.
        self.assertEqual(len(report["checked"]), 14)

    def test_missing_module_names_field(self):
        live = self._live(control_plane_modules={
            m: True for m in he.CONTROL_PLANE_MODULES})
        live["control_plane_modules"]["yaml"] = False
        with mock.patch.object(he, "_live_environment", return_value=live):
            with self.assertRaisesRegex(he.EnvironmentMismatch,
                                        "control_plane_module\\.yaml"):
                he.verify_environment(manifest=self._manifest())

    def test_source_commit_mismatch_names_field(self):
        manifest = self._manifest(source_commit="a" * 40)
        with mock.patch.object(he, "_live_environment",
                               return_value=self._live()), \
                mock.patch.object(he, "resolve_source_commit",
                                  return_value="b" * 40):
            with self.assertRaisesRegex(he.EnvironmentMismatch,
                                        "source_commit.*a{40}.*b{40}"):
                he.verify_environment(manifest=manifest)

    def test_source_commit_absent_in_manifest_passes(self):
        manifest = self._manifest()
        del manifest["source_commit"]
        with mock.patch.object(he, "_live_environment",
                               return_value=self._live()), \
                mock.patch.object(he, "resolve_source_commit",
                                  return_value="c" * 40):
            report = he.verify_environment(manifest=manifest)
        self.assertTrue(report["ok"])

    def test_transport_mismatch_names_field(self):
        with mock.patch.object(he, "_live_environment",
                               return_value=self._live(
                                   drive_transport={"transport": "httplib2"})):
            with self.assertRaisesRegex(he.EnvironmentMismatch,
                                        "drive_transport.transport.*requests.*httplib2"):
                he.verify_environment(manifest=self._manifest())

    def test_version_mismatch_names_field(self):
        with mock.patch.object(he, "_live_environment",
                               return_value=self._live(cli_version="0.7.4")):
            with self.assertRaisesRegex(he.EnvironmentMismatch,
                                        "cli_version.*0.7.5.*0.7.4"):
                he.verify_environment(manifest=self._manifest())

    def test_missing_patch_names_field(self):
        with mock.patch.object(he, "_live_environment",
                               return_value=self._live(cli_post_body_patch=False)):
            with self.assertRaisesRegex(he.EnvironmentMismatch, "cli_post_body_patch"):
                he.verify_environment(manifest=self._manifest())

    def test_client_hash_mismatch_names_field(self):
        with mock.patch.object(he, "_live_environment",
                               return_value=self._live(cli_client_sha256="b" * 64)):
            with self.assertRaisesRegex(he.EnvironmentMismatch, "cli_client_sha256"):
                he.verify_environment(manifest=self._manifest())

    def test_python_outside_allowed_list_names_field(self):
        with mock.patch.object(he, "_live_environment",
                               return_value=self._live(python_major_minor="3.10")):
            with self.assertRaisesRegex(he.EnvironmentMismatch, "python_major_minor"):
                he.verify_environment(manifest=self._manifest())

    def test_missing_ffprobe_names_field(self):
        with mock.patch.object(he, "_live_environment",
                               return_value=self._live(ffprobe_present=False)):
            with self.assertRaisesRegex(he.EnvironmentMismatch, "ffprobe_present"):
                he.verify_environment(manifest=self._manifest())

    def test_missing_manifest_file_raises(self):
        with self.assertRaisesRegex(he.EnvironmentMismatch, "manifest missing"):
            he.verify_environment(manifest_path=Path("/nonexistent/env.json"))

    def test_checked_in_manifest_matches_patched_scratch_venv(self):
        """The real manifest (generated from the proven patched venv)
        verifies against that same venv. This is the only test that
        touches a real CLI — read-only, no GPU, no Drive."""
        venv = Path.home() / "workspace/task005-work/.venv-colab-cli"
        cli = venv / "bin" / "colab"
        if not cli.is_file():
            self.skipTest("patched scratch venv not present")
        report = he.verify_environment(cli_exe=str(cli))
        self.assertTrue(report["ok"])


class ResolverSharingTest(unittest.TestCase):
    def test_controller_uses_auth_support_resolver(self):
        self.assertIs(hc.resolve_colab_executable,
                      auth_support.resolve_colab_executable)

    def test_watchdog_resolves_through_auth_support(self):
        fake_exe = "/fake/venv/bin/colab"
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        with mock.patch.object(auth_support, "resolve_colab_executable",
                               return_value=fake_exe):
            with mock.patch.object(hwd.subprocess, "run",
                                   return_value=proc) as run:
                hwd._run_colab(["sessions"])
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0][0], fake_exe)
        self.assertEqual(run.call_args[0][0][1], "--auth=adc")


if __name__ == "__main__":
    unittest.main()
