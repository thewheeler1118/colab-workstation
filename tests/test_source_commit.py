"""TASK-007.1, fix 3: code_commit traceability in a non-git production
dir. _manifest_create() and _resume_run() resolve the commit through
_current_source_commit() (git HEAD, else the sync's RELEASE_COMMIT),
so a production run manifest records a real commit and resume refuses
a code mismatch instead of silently skipping the check.

No GPU, no Drive, no live CLI: the git case runs a read-only rev-parse
against the real repo checkout; the non-git cases use temp dirs.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_controller as hc  # noqa: E402
import h3_run_manifest as hrm  # noqa: E402


class ManifestCreateTest(unittest.TestCase):
    def _args(self, root):
        wf = root / "workflow_api.json"
        wf.write_text("{}", encoding="utf-8")
        cm = root / "verified_manifest.json"
        cm.write_text("{}", encoding="utf-8")
        return SimpleNamespace(production_task_id="task-0071",
                               run_id="run-0071",
                               workflow_api=str(wf),
                               cache_manifest=str(cm),
                               variant="A")

    def test_manifest_create_records_release_commit_in_non_git_dir(self):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        root = Path(td)
        (root / "RELEASE_COMMIT").write_text("cafe" * 10 + "\n",
                                             encoding="utf-8")
        old_root = hc.ROOT
        hc.ROOT = root
        try:
            hc._manifest_create(self._args(root), ledger=None)
        finally:
            hc.ROOT = old_root
        manifest = hrm.load_run_manifest(root, "run-0071")
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["code_commit"], "cafe" * 10)

    @unittest.skipUnless((ROOT / ".git").exists(),
                          "not a git checkout (production has no .git; "
                          "the RELEASE_COMMIT fallback is covered by "
                          "test_manifest_create_records_release_commit_in_non_git_dir)")
    def test_current_source_commit_is_git_head_in_checkout(self):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True,
                              timeout=10).stdout.strip()
        self.assertTrue(head)
        self.assertEqual(hc._current_source_commit(), head)


class ResumeCodeGateTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.td, ignore_errors=True))
        self.root = Path(self.td)
        (self.root / "RELEASE_COMMIT").write_text("b" * 40 + "\n",
                                                  encoding="utf-8")
        self.old_root = hc.ROOT
        hc.ROOT = self.root
        self.addCleanup(lambda: setattr(hc, "ROOT", self.old_root))
        self.verify_patch = mock.patch.object(
            hc, "verify_environment", return_value={"ok": True})
        self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def _view(self, code_commit):
        return {"manifest": {"task_state": "RUNNING",
                             "code_commit": code_commit,
                             "production_task_id": "task-0071",
                             "run_id": "run-0071"}}

    def test_resume_refuses_code_mismatch_from_release_commit(self):
        with mock.patch.object(hrm, "resume_run",
                               return_value=self._view("a" * 40)):
            result = hc._resume_run(SimpleNamespace(run_id="run-0071"))
        self.assertFalse(result["resumed"])
        self.assertIn("code mismatch", result["reason"])
        self.assertIn("a" * 40, result["reason"])
        self.assertIn("b" * 40, result["reason"])

    def test_resume_passes_code_gate_when_commits_match(self):
        with mock.patch.object(hrm, "resume_run",
                               return_value=self._view("b" * 40)):
            result = hc._resume_run(SimpleNamespace(run_id="run-0071"))
        # The code gate passed; the next check (ledger must exist)
        # refuses — resume never creates a 30 CU ledger.
        self.assertFalse(result["resumed"])
        self.assertIn("no budget ledger", result["reason"])


if __name__ == "__main__":
    unittest.main()
