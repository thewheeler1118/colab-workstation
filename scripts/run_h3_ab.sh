#!/usr/bin/env bash
# run_h3_ab.sh — legacy name for scripts/run_h3_task.sh (TASK-007.1).
# Thin compatibility wrapper: forwards every argument unchanged. New
# callers should use run_h3_task.sh directly.
set -euo pipefail
exec "$(dirname "$0")/run_h3_task.sh" "$@"
