"""Tests for the per-task controller lock (TASK-006 P1-B, Issue #50).

The lock is a POSIX flock: two separate open file descriptions in the
same process DO conflict, so the contention test is a real test of the
mechanism, not a mock.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h3_task_lock as tl


class TaskLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_and_release(self):
        with tl.TaskLock(self.root, "h3-ab-20261111"):
            lock_path = tl.task_lock_path(self.root, "h3-ab-20261111")
            self.assertTrue(lock_path.is_file())
        # Released: a second lock for the same task succeeds.
        with tl.TaskLock(self.root, "h3-ab-20261111"):
            pass

    def test_second_lock_for_same_task_refuses(self):
        first = tl.TaskLock(self.root, "h3-ab-20261111").acquire()
        try:
            with self.assertRaises(tl.TaskLockBusy) as ctx:
                tl.TaskLock(self.root, "h3-ab-20261111").acquire()
            self.assertEqual(ctx.exception.production_task_id, "h3-ab-20261111")
            self.assertTrue(ctx.exception.holder.get("pid"))
        finally:
            first.release()

    def test_different_tasks_do_not_conflict(self):
        with tl.TaskLock(self.root, "task-one"):
            with tl.TaskLock(self.root, "task-two"):
                pass

    def test_release_on_exception(self):
        try:
            with tl.TaskLock(self.root, "h3-ab-20261111"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # The lock was released by the context manager despite the error.
        with tl.TaskLock(self.root, "h3-ab-20261111"):
            pass

    def test_task_id_is_sanitised(self):
        path = tl.task_lock_path(self.root, "../../etc/passwd")
        self.assertNotIn("..", path.name)
        self.assertEqual(path.parent, self.root / "model_cache")


if __name__ == "__main__":
    unittest.main()
