"""TASK-007.1, fix 1: scripts/run_h3_task.sh resolves its own
production interpreter and COLAB_CLI_PATH; run_h3_ab.sh forwards to it.

No GPU, no live CLI: the gates are exercised against the real repo
(no venv present there), and the happy path against a fake venv layout
in a temp dir whose python is a shell script that echoes its arguments.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_h3_task.sh"
WRAPPER = ROOT / "scripts" / "run_h3_ab.sh"

FAKE_PY = """#!/bin/sh
if [ "$1" = "-c" ]; then exit 0; fi
echo "FAKE_PY ARGS: $@"
echo "COLAB_CLI_PATH=$COLAB_CLI_PATH"
"""


class ScriptGateTest(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args],
                              capture_output=True, text=True, timeout=30)

    def test_no_args_refuses_without_confirm_live(self):
        r = self._run()
        self.assertEqual(r.returncode, 2)
        self.assertIn("requires --confirm-live", r.stderr)

    def test_confirm_live_without_run_id_refuses(self):
        r = self._run("--confirm-live", "--production-task-id", "t-1",
                      "--task-spec", "config/task_spec.example.json")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--run-id is required", r.stderr)

    def test_variant_and_task_spec_conflict_refuses(self):
        r = self._run("--confirm-live", "--production-task-id", "t-1",
                      "--variant", "A", "--task-spec", "x.json",
                      "--run-id", "h3-20261114T000000Z-abcdef")
        self.assertEqual(r.returncode, 2)
        self.assertIn("conflict", r.stderr)

    def test_max_attempts_without_deliver_or_auto_recover_refuses(self):
        r = self._run("--confirm-live", "--production-task-id", "t-1",
                      "--task-spec", "config/task_spec.example.json",
                      "--run-id", "h3-20261114T000000Z-abcdef",
                      "--max-task-attempts", "2")
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs --deliver or --auto-recover", r.stderr)

    def test_max_attempts_out_of_range_refuses(self):
        r = self._run("--confirm-live", "--production-task-id", "t-1",
                      "--task-spec", "config/task_spec.example.json",
                      "--run-id", "h3-20261114T000000Z-abcdef",
                      "--deliver", "--max-task-attempts", "4")
        self.assertEqual(r.returncode, 2)
        self.assertIn("must be 1, 2 or 3", r.stderr)

    def test_deliver_and_auto_recover_conflict_refuses(self):
        r = self._run("--confirm-live", "--production-task-id", "t-1",
                      "--task-spec", "config/task_spec.example.json",
                      "--deliver", "--auto-recover")
        self.assertEqual(r.returncode, 2)
        self.assertIn("conflict", r.stderr)


class ScriptResolutionTest(unittest.TestCase):
    def _layout(self, venvs):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        root = Path(td)
        (root / "scripts").mkdir()
        shutil.copy(SCRIPT, root / "scripts" / "run_h3_task.sh")
        for venv in venvs:
            bin_dir = root / venv / "bin"
            bin_dir.mkdir(parents=True)
            py = bin_dir / "python"
            py.write_text(FAKE_PY, encoding="utf-8")
            py.chmod(0o755)
            colab = bin_dir / "colab"
            colab.write_text("#!/bin/sh\necho Version: 0.7.5\n",
                             encoding="utf-8")
            colab.chmod(0o755)
        return root

    def _run(self, root, *args):
        return subprocess.run(
            ["bash", str(root / "scripts" / "run_h3_task.sh"), *args],
            capture_output=True, text=True, timeout=30, cwd=str(root))

    def _full_args(self):
        return ["--confirm-live", "--production-task-id", "task-0071",
                "--task-spec", "config/task_spec.example.json",
                "--run-id", "h3-20261114T000000Z-abcdef"]

    def test_no_venv_refuses_on_interpreter(self):
        # Full args in a layout with NO venv at all: the gates pass,
        # then the interpreter resolution refuses — before any live
        # call. Uses a temp layout (not the repo) because the production
        # dir legitimately HAS a venv, where the same command would
        # proceed to the controller instead.
        root = self._layout([])
        r = self._run(root, *self._full_args())
        self.assertEqual(r.returncode, 2)
        self.assertIn("no production venv python found", r.stderr)

    def test_prefers_venv_colab_cli(self):
        root = self._layout([".venv-colab-cli", ".venv"])
        r = self._run(root, *self._full_args())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("COLAB_CLI_PATH="
                      + str(root / ".venv-colab-cli/bin/colab"), r.stdout)
        self.assertIn("h3_controller.py run --execute", r.stdout)
        self.assertIn("--task-spec config/task_spec.example.json", r.stdout)

    def test_falls_back_to_venv(self):
        root = self._layout([".venv"])
        r = self._run(root, *self._full_args())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("COLAB_CLI_PATH=" + str(root / ".venv/bin/colab"),
                      r.stdout)

    def test_deliver_forwards_without_run_id(self):
        # Deliver derives its run ids; --run-id must NOT be required
        # and --task-deadline-utc / --max-task-attempts pass through.
        root = self._layout([".venv-colab-cli"])
        r = self._run(root, "--confirm-live", "--production-task-id",
                      "task-0071", "--task-spec",
                      "config/task_spec.example.json", "--deliver",
                      "--task-deadline-utc", "2026-11-20T00:00:00Z",
                      "--max-task-attempts", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("h3_controller.py deliver --execute", r.stdout)
        self.assertIn("--task-deadline-utc 2026-11-20T00:00:00Z", r.stdout)
        self.assertIn("--max-task-attempts 3", r.stdout)
        self.assertNotIn("--run-id", r.stdout)

    def test_auto_recover_run_forwards_flag(self):
        root = self._layout([".venv-colab-cli"])
        r = self._run(root, *self._full_args(), "--auto-recover")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("h3_controller.py run --execute", r.stdout)
        self.assertIn("--auto-recover", r.stdout)


class WrapperTest(unittest.TestCase):
    def test_wrapper_forwards_arguments(self):
        r = subprocess.run(["bash", str(WRAPPER)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("requires --confirm-live", r.stderr)


if __name__ == "__main__":
    unittest.main()
