#!/usr/bin/env python3
"""Print the TASK-005 §12 reliability report for a run-manifest root.

Reads only: run manifests, audit logs, and budget ledgers under --root.
Prints the JSON report from h3_reliability.compute_reliability.

Example:
    python3 scripts/reliability_report.py --root ~/workspace/Muse_Colab_H3_Drive_v3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from h3_reliability import compute_reliability


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True,
                        help="production root holding model_cache/run_manifests/")
    parser.add_argument("--task", default=None,
                        help="only runs for this production_task_id")
    parser.add_argument("--since", default=None,
                        help="only runs created at or after this UTC timestamp")
    parser.add_argument("--until", default=None,
                        help="only runs created at or before this UTC timestamp")
    parser.add_argument("--output-root", default=None,
                        help="directory holding <run_id>/<variant>.mp4 files "
                             "(metric C also verifies the local file)")
    args = parser.parse_args()
    report = compute_reliability(
        args.root, production_task_id=args.task,
        since=args.since, until=args.until,
        output_root=args.output_root)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
