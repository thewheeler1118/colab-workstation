"""Remote worker launcher (TASK-005 §4.2, Issue #27).

Executed by the controller via a SHORT `colab exec -f` call. It starts
the real worker detached (nohup semantics: start_new_session + output
redirected to a log file) and returns immediately with a launch record —
it never blocks on the worker.

nohup/detach is a candidate process starter ONLY. It is not a task
manager and not a shutdown guarantee: if the Colab VM is reclaimed the
worker dies with it, and the controller learns that from the tristate
session query + the durable job state file, never from assuming the
process is alive. The launcher verifies the state file exists before
it exits, so a launch that cannot be observed fails here, fast.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("/content/muse_h3")
sys.path.insert(0, str(ROOT))

from h3_job_state import JOB_STATE_FILENAME, read_job_state, write_job_state  # noqa: E402


def main() -> None:
    task = json.loads((ROOT / "task.json").read_text(encoding="utf-8"))
    run_id = task["run_id"]
    variant = task["variant"]
    worker_path = ROOT / "h3_remote_worker.py"
    if not worker_path.is_file():
        raise FileNotFoundError(f"worker not uploaded: {worker_path}")
    log_path = ROOT / "worker.log"
    # Initial state first: if the worker dies instantly, the probe still
    # finds a JOB_STARTED record instead of a missing file.
    write_job_state(ROOT, run_id, variant, "JOB_STARTED")
    with open(log_path, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(worker_path)],
            stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(ROOT),
        )
    state = write_job_state(ROOT, run_id, variant, "JOB_STARTED", pid=proc.pid)
    # Prove the state file is readable before reporting success.
    read_job_state(ROOT)
    print("MUSE_H3_LAUNCH_JSON:" + json.dumps({
        "run_id": run_id, "variant": variant, "pid": proc.pid,
        "state_file": str(ROOT / JOB_STATE_FILENAME),
        "log_file": str(log_path),
        "stage": state["stage"],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
