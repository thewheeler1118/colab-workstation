"""Remote job-state probe (TASK-005 §4.2, Issue #27).

Executed by the controller via a SHORT `colab exec -f` call. Reads the
worker's durable job state file and prints exactly one JSON line. A
missing or corrupt file prints a MISSING/CORRUPT marker and exits
non-zero — the controller treats that as a poll failure, never as
"the worker is done" or "the worker never started".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("/content/muse_h3")
sys.path.insert(0, str(ROOT))

from h3_job_state import read_job_state  # noqa: E402


def main() -> None:
    try:
        state = read_job_state(ROOT)
    except FileNotFoundError:
        print("MUSE_H3_JOB_STATE_MISSING", flush=True)
        raise SystemExit(2)
    except ValueError as e:
        print(f"MUSE_H3_JOB_STATE_CORRUPT: {e}", flush=True)
        raise SystemExit(3)
    print("MUSE_H3_JOB_STATE_JSON:" + json.dumps(state, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
