"""Tests for the pinned ComfyUI source cache and the atomic clone path.

Covers (user instruction 2026-11-12): Git failure recording, residual
directories, the bounded two-attempt retry, cache corruption, commit
mismatch, and a normal cache restore — all against local git repos and
locally built archives, no network, no GPU.
"""
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

import h3_remote_worker
from h3_remote_worker import (
    _LAST_CMD_FAILURE,
    _sanitize_text,
    clone_comfyui_atomic,
    comfyui_commit,
    ensure_comfy,
    log_requirements_status,
    pip_install_needed,
    restore_comfyui_from_cache,
    run_cmd,
    verify_comfyui_commit,
    worker_failure_info,
)
from h3_comfyui_cache import build_archive, verify_local_archive


def _make_repo(base: Path, files=None):
    """Create a small local git repo with one commit. Returns (repo, sha)."""
    repo = base / "repo"
    repo.mkdir(parents=True)
    body = files or {"main.py": "print('comfy')\n",
                     "requirements.txt": "pip>=1\n"}
    for name, content in body.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(base)}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "test"],
                   check=True, env=env)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return repo, sha


class TestGitFailureRecord(unittest.TestCase):
    def test_run_cmd_records_sanitized_failure(self):
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            run_cmd([sys.executable, "-c",
                     "import sys; sys.stderr.write('fatal: boom\\n'); "
                     "raise SystemExit(3)"])
        self.assertEqual(cm.exception.returncode, 3)
        detail = h3_remote_worker._LAST_CMD_FAILURE
        self.assertIsNotNone(detail)
        self.assertEqual(detail["returncode"], 3)
        self.assertIn("fatal: boom", detail["stderr"])
        info = worker_failure_info("ensure_comfy", cm.exception)
        self.assertEqual(info["stage"], "ensure_comfy")
        self.assertEqual(info["error_type"], "CalledProcessError")
        self.assertIn("exit status 3", info["error"])
        self.assertEqual(info["cmd_failure"]["returncode"], 3)

    def test_sanitizer_redacts_credentials(self):
        self.assertEqual(
            _sanitize_text("https://user:s3cret@github.com/x.git"),
            "https://user:***@github.com/x.git")
        self.assertEqual(
            _sanitize_text("https://github.com/Comfy-Org/ComfyUI.git"),
            "https://github.com/Comfy-Org/ComfyUI.git")

    def test_last_cmd_failure_clears_on_success(self):
        with self.assertRaises(subprocess.CalledProcessError):
            run_cmd([sys.executable, "-c", "raise SystemExit(1)"])
        self.assertIsNotNone(h3_remote_worker._LAST_CMD_FAILURE)
        run_cmd([sys.executable, "-c", "pass"])
        self.assertIsNone(h3_remote_worker._LAST_CMD_FAILURE)


class TestAtomicClone(unittest.TestCase):
    def test_clone_success_and_commit_verified(self):
        with tempfile.TemporaryDirectory() as d:
            repo, sha = _make_repo(Path(d))
            target = Path(d) / "ComfyUI"
            result = clone_comfyui_atomic(target, sha, repo_url=str(repo),
                                          attempts=2, delay_sec=0)
            self.assertEqual(result, "cloned")
            self.assertEqual((target / "main.py").read_text(), "print('comfy')\n")
            self.assertTrue(verify_comfyui_commit(target, sha))

    def test_clone_replaces_partial_residual_directory(self):
        # 2026-11-12 defect: a failed clone leaves a partial target dir,
        # and a plain `git clone` into a non-empty dir fails at once, so
        # the old retry could never work. The atomic path must clear it.
        with tempfile.TemporaryDirectory() as d:
            repo, sha = _make_repo(Path(d))
            target = Path(d) / "ComfyUI"
            target.mkdir()
            (target / "junk.txt").write_text("partial clone leftovers")
            (target / ".git").mkdir()
            result = clone_comfyui_atomic(target, sha, repo_url=str(repo),
                                          attempts=2, delay_sec=0)
            self.assertEqual(result, "cloned")
            self.assertFalse((target / "junk.txt").exists())
            self.assertTrue(verify_comfyui_commit(target, sha))

    def test_clone_present_reuses_pinned_commit(self):
        with tempfile.TemporaryDirectory() as d:
            repo, sha = _make_repo(Path(d))
            target = Path(d) / "ComfyUI"
            clone_comfyui_atomic(target, sha, repo_url=str(repo),
                                 attempts=2, delay_sec=0)
            self.assertEqual(clone_comfyui_atomic(target, sha, repo_url=str(repo),
                                                 attempts=2, delay_sec=0),
                             "present")

    def test_clone_retries_once_then_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            repo, sha = _make_repo(Path(d))
            target = Path(d) / "ComfyUI"
            real_run_cmd = h3_remote_worker.run_cmd
            calls = {"clone": 0}

            def flaky(args, timeout=1200, cwd=None):
                if args[0] == "git" and args[1] == "clone":
                    calls["clone"] += 1
                    if calls["clone"] == 1:
                        raise subprocess.CalledProcessError(
                            128, args, output="", stderr="fatal: flaky\n")
                return real_run_cmd(args, timeout=timeout, cwd=cwd)

            with mock.patch.object(h3_remote_worker, "run_cmd", flaky):
                result = clone_comfyui_atomic(target, sha, repo_url=str(repo),
                                              attempts=2, delay_sec=0)
            self.assertEqual(result, "cloned")
            self.assertEqual(calls["clone"], 2)
            self.assertTrue(verify_comfyui_commit(target, sha))

    def test_clone_two_failures_raise_and_leave_no_partial_dir(self):
        with tempfile.TemporaryDirectory() as d:
            repo, sha = _make_repo(Path(d))
            target = Path(d) / "ComfyUI"
            calls = {"n": 0}

            def always_fail(args, timeout=1200, cwd=None):
                calls["n"] += 1
                raise subprocess.CalledProcessError(
                    128, args, output="", stderr="fatal: no network\n")

            with mock.patch.object(h3_remote_worker, "run_cmd", always_fail):
                with self.assertRaises(subprocess.CalledProcessError):
                    clone_comfyui_atomic(target, sha, repo_url=str(repo),
                                         attempts=2, delay_sec=0)
            self.assertEqual(calls["n"], 2)
            self.assertFalse(target.exists())
            leftovers = [p for p in Path(d).iterdir()
                         if p.name.startswith(".comfyui-install-")]
            self.assertEqual(leftovers, [])


class TestCacheRestore(unittest.TestCase):
    def _archive(self, base: Path, repo: Path, sha: str):
        out = base / "cache.tar.gz"
        digest = build_archive(repo, sha, out)
        return out, digest

    def test_cache_restore_success(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo, sha = _make_repo(base)
            archive, digest = self._archive(base, repo, sha)
            cache_dir = base / "cache"
            cache_dir.mkdir()
            (cache_dir / archive.name).write_bytes(archive.read_bytes())
            target = base / "ComfyUI"
            result = restore_comfyui_from_cache(
                cache_dir, target, digest, sha, archive.name)
            self.assertEqual(result, "restored")
            self.assertEqual((target / "main.py").read_text(), "print('comfy')\n")
            self.assertEqual(comfyui_commit(target), sha)
            self.assertEqual((target / "COMFYUI_COMMIT.txt").read_text().strip(), sha)

    def test_cache_restore_sha_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo, sha = _make_repo(base)
            archive, digest = self._archive(base, repo, sha)
            cache_dir = base / "cache"
            cache_dir.mkdir()
            (cache_dir / archive.name).write_bytes(archive.read_bytes())
            target = base / "ComfyUI"
            with self.assertRaises(RuntimeError) as cm:
                restore_comfyui_from_cache(cache_dir, target, "0" * 64, sha,
                                           archive.name)
            self.assertIn("SHA-256 mismatch", str(cm.exception))
            self.assertFalse(target.exists())

    def test_cache_restore_tampered_archive(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo, sha = _make_repo(base)
            archive, digest = self._archive(base, repo, sha)
            cache_dir = base / "cache"
            cache_dir.mkdir()
            data = bytearray(archive.read_bytes())
            data[len(data) // 2] ^= 0xFF
            (cache_dir / archive.name).write_bytes(bytes(data))
            target = base / "ComfyUI"
            with self.assertRaises(RuntimeError):
                restore_comfyui_from_cache(cache_dir, target, digest, sha,
                                           archive.name)
            self.assertFalse(target.exists())

    def test_cache_restore_commit_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo, sha = _make_repo(base)
            archive, digest = self._archive(base, repo, sha)
            cache_dir = base / "cache"
            cache_dir.mkdir()
            (cache_dir / archive.name).write_bytes(archive.read_bytes())
            target = base / "ComfyUI"
            with self.assertRaises(RuntimeError) as cm:
                restore_comfyui_from_cache(cache_dir, target, digest,
                                           "f" * 40, archive.name)
            self.assertIn("commit mismatch", str(cm.exception))
            self.assertFalse(target.exists())

    def test_cache_restore_missing_archive(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            with self.assertRaises(RuntimeError) as cm:
                restore_comfyui_from_cache(base, base / "ComfyUI",
                                           "0" * 64, "a" * 40)
            self.assertIn("missing", str(cm.exception))

    def test_build_archive_deterministic_and_verified(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo, sha = _make_repo(base)
            a1 = base / "a1.tar.gz"
            a2 = base / "a2.tar.gz"
            s1 = build_archive(repo, sha, a1)
            s2 = build_archive(repo, sha, a2)
            self.assertEqual(s1, s2)
            checked = verify_local_archive(repo, sha, a1, s1)
            self.assertEqual(checked["files_checked"], 2)


class TestEnsureComfy(unittest.TestCase):
    def test_ensure_comfy_prefers_cache_over_clone(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo, sha = _make_repo(base)
            archive = base / "c.tar.gz"
            digest = build_archive(repo, sha, archive)
            cache_dir = base / "cache"
            cache_dir.mkdir()
            (cache_dir / archive.name).write_bytes(
                (base / "c.tar.gz").read_bytes())
            target = base / "ComfyUI"
            cache = {"dir": str(cache_dir), "archive": archive.name,
                     "sha256": digest, "commit": sha}
            with mock.patch.object(h3_remote_worker, "clone_comfyui_atomic") as clone:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ensure_comfy(target, sha, cache)
                clone.assert_not_called()
            self.assertIn("COMFYUI_CACHE_RESTORED", buf.getvalue())
            self.assertEqual(comfyui_commit(target), sha)

    def test_ensure_comfy_falls_back_to_clone_on_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo, sha = _make_repo(base)
            cache_dir = base / "cache"
            cache_dir.mkdir()
            target = base / "ComfyUI"
            cache = {"dir": str(cache_dir),
                     "archive": "comfyui-" + sha + ".tar.gz",
                     "sha256": "0" * 64, "commit": sha}
            calls = {"clone": 0}
            real_clone = h3_remote_worker.clone_comfyui_atomic

            def stub_clone(comfy, git_ref, **kw):
                calls["clone"] += 1
                return real_clone(comfy, git_ref, repo_url=str(repo),
                                  attempts=2, delay_sec=0)

            buf = io.StringIO()
            with mock.patch.object(h3_remote_worker, "clone_comfyui_atomic",
                                   stub_clone):
                with redirect_stdout(buf):
                    ensure_comfy(target, sha, cache)
            self.assertIn("COMFYUI_CACHE_FAILED", buf.getvalue())
            self.assertEqual(calls["clone"], 1)
            self.assertTrue(verify_comfyui_commit(target, sha))

    def test_ensure_comfy_rejects_wrong_commit_after_install(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo, sha = _make_repo(base)
            target = base / "ComfyUI"
            clone_comfyui_atomic(target, sha, repo_url=str(repo),
                                 attempts=2, delay_sec=0)
            # Simulate an install whose commit does not match the pinned
            # one (e.g. a stale tree): the final gate must refuse it.
            with mock.patch.object(h3_remote_worker, "comfyui_commit",
                                   return_value="e" * 40):
                with self.assertRaises(RuntimeError) as cm:
                    ensure_comfy(target, sha, None)
            self.assertIn("!= pinned", str(cm.exception))


class TestRequirementsCheck(unittest.TestCase):
    def test_status_classifies_satisfied_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            req = Path(d) / "requirements.txt"
            req.write_text("pip>=1\ndefinitely-not-installed-xyz>=1\n"
                           "# comment\n\n")
            status = log_requirements_status(req)
            self.assertEqual(status["satisfied"], ["pip>=1"])
            self.assertEqual(status["missing"],
                             ["definitely-not-installed-xyz>=1"])
            self.assertEqual(status["unknown"], [])

    def test_spec_comparison(self):
        with tempfile.TemporaryDirectory() as d:
            req = Path(d) / "requirements.txt"
            req.write_text("pip==0.0.0\n")
            status = log_requirements_status(req)
            self.assertEqual(status["satisfied"], [])
            self.assertEqual(len(status["missing"]), 1)

    def test_spec_compatible_release(self):
        # ~=2.0 means >=2.0 and ==2.* — 2.14.3 satisfies it, 3.0 does not.
        from h3_remote_worker import _spec_satisfied
        self.assertTrue(_spec_satisfied("2.14.3", "~=2.0"))
        self.assertTrue(_spec_satisfied("2.0", "~=2.0"))
        self.assertFalse(_spec_satisfied("3.0", "~=2.0"))
        self.assertFalse(_spec_satisfied("1.9", "~=2.0"))
        self.assertTrue(_spec_satisfied("1.58.5", "==1.58.5"))
        self.assertTrue(_spec_satisfied("1.26.4", ">=1.25.0"))
        self.assertFalse(_spec_satisfied("1.24.0", ">=1.25.0"))

    def test_pip_install_needed_decision(self):
        with tempfile.TemporaryDirectory() as d:
            ok = Path(d) / "ok.txt"
            ok.write_text("pip>=1\n")
            self.assertFalse(pip_install_needed(ok))
            bad = Path(d) / "bad.txt"
            bad.write_text("definitely-not-installed-xyz>=1\n")
            self.assertTrue(pip_install_needed(bad))


if __name__ == "__main__":
    unittest.main()
