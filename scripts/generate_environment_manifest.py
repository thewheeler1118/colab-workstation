#!/usr/bin/env python3
"""Generate environment_manifest.json from the live environment
(TASK-005 §6, Issue #29).

Usage:
    python3 scripts/generate_environment_manifest.py [--cli-exe PATH] [--out PATH]

Records the pinned CLI (version + installed client hash + both patch
markers), ffmpeg/ffprobe, the Python version, and the remote
expectations (ComfyUI commit, model HF revision, workflow SHA-256).
Never invents a value: a check that cannot run records None/False and
the preflight gate fails on it.

The checked-in manifest describes the environment the controller
expects. Regenerate it deliberately in a PR when the pinned
environment changes — never hand-edit a hash.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from h3_environment import ENVIRONMENT_MANIFEST_PATH, generate_environment_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli-exe", default=None,
                        help="Colab CLI executable to record (default: resolver)")
    parser.add_argument("--out", default=str(ENVIRONMENT_MANIFEST_PATH),
                        help="Output path (default: environment_manifest.json)")
    args = parser.parse_args()
    manifest = generate_environment_manifest(cli_exe=args.cli_exe)
    out = Path(args.out)
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
