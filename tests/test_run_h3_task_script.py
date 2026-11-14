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

    def test_full_args_without_venv_refuses_on_interpreter(self):
        # The repo checkout has no .venv-colab-cli/.venv: the gates pass,
        # then the interpreter resolution refuses — before any live call.
        r = self._run("--confirm-live", "--production-task-id", "t-1",
                      "--task-spec", "config/task_spec.example.json",
                      "--run-id", "h3-20261114T000000Z-abcdef")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no production venv python found", r.stderr)


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


class WrapperTest(unittest.TestCase):
    def test_wrapper_forwards_arguments(self):
        r = subprocess.run(["bash", str(WRAPPER)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("requires --confirm-live", r.stderr)


if __name__ == "__main__":
    unittest.main()
