"""Per-task controller lock (TASK-006 P1-B, Issue #50).

Two controllers must never compete for the same production task: one
allocating a GPU while another resumes the same task, or two fresh runs
with different run_ids racing the run_id duplicate guard. The lock is a
POSIX advisory lock (fcntl.flock) on a per-task lock file under the
model cache dir.

Rules:
- `TaskLock` is a context manager. It takes the lock non-blocking;
  when another controller holds it, it raises `TaskLockBusy` (with the
  holder's pid and acquisition time, read from the lock file) instead
  of waiting — a run takes ~30 minutes, so waiting would hide a stuck
  controller.
- flock is released automatically when the holder process dies,
  including a VM restart, so there are no stale locks to clean up.
  The lock FILE remains on disk (harmless); only the live flock
  matters.
- The lock is per production TASK, not per run: a resume, a fresh run
  and a stop for the same task serialise. Read-only `status` stays
  lock-free.
"""
from __future__ import annotations

import datetime
import fcntl
import os
import re
from pathlib import Path

__all__ = ["TaskLock", "TaskLockBusy", "task_lock_path"]


class TaskLockBusy(RuntimeError):
    """Another controller holds the lock for this production task."""

    def __init__(self, production_task_id: str, holder: dict | None = None):
        self.production_task_id = production_task_id
        self.holder = holder or {}
        detail = ""
        if self.holder.get("pid"):
            detail = (f" (held by pid {self.holder['pid']} since "
                      f"{self.holder.get('acquired_at_utc', 'unknown')})")
        super().__init__(
            f"production task {production_task_id!r} is locked by another "
            f"controller{detail}; refusing to run in parallel — finish or "
            "stop the other controller first")


def _sanitise_task_id(production_task_id: str) -> str:
    """A lock-file-safe form of the task id (no path separators, no dots)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(production_task_id or "")) or "unknown"


def task_lock_path(root: Path, production_task_id: str) -> Path:
    """Lock file for a task under <root>/model_cache/."""
    return Path(root) / "model_cache" / f"h3_task_{_sanitise_task_id(production_task_id)}.lock"


def _utc_now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


class TaskLock:
    """Non-blocking exclusive lock for one production task.

    Usage: `with TaskLock(ROOT, production_task_id): ...`
    Raises TaskLockBusy when another controller holds the lock.
    """

    def __init__(self, root: Path, production_task_id: str):
        self.root = Path(root)
        self.production_task_id = str(production_task_id)
        self.path = task_lock_path(self.root, self.production_task_id)
        self._fd = None

    def acquire(self) -> "TaskLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = {}
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 4096).decode("utf-8", "replace")
                if raw.strip():
                    import json

                    holder = json.loads(raw)
            except Exception:
                holder = {}
            os.close(fd)
            raise TaskLockBusy(self.production_task_id, holder) from None
        # Record the holder for the refusal message (best-effort: the
        # flock is the lock, this file content is only a label).
        try:
            import json

            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(
                {"pid": os.getpid(),
                 "production_task_id": self.production_task_id,
                 "acquired_at_utc": _utc_now()}).encode("utf-8"))
        except Exception:
            pass
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None

    def __enter__(self) -> "TaskLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
