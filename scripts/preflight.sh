#!/usr/bin/env bash
# preflight.sh — offline + live read-only preflight. NEVER creates a GPU.
# Usage: ./scripts/preflight.sh [--run-id ID] [--task-spec SPEC]
# Runs: <resolved python> h3_controller.py preflight
# Interpreter resolution (TASK-007.1, fix 1): prefer the production venv
# python (.venv-colab-cli, then .venv) so the Drive checks have their
# google libs; fall back to system python3 with a warning when no venv
# is present (offline checks still run, Drive checks will fail closed).
# Preflight checks (h3_controller.preflight): local sources, CLI post-body
# and drivemount patches, PTY availability, budget ledger, live `colab
# usage` / `colab sessions` (read-only), GPU policy, and restore capacity.
# It does NOT allocate a session, mount Drive, or spend CU.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
if [ -x "$REPO/.venv-colab-cli/bin/python" ]; then
  PY="$REPO/.venv-colab-cli/bin/python"
  export COLAB_CLI_PATH="$REPO/.venv-colab-cli/bin/colab"
elif [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
  export COLAB_CLI_PATH="$REPO/.venv/bin/colab"
else
  echo "WARNING: no production venv python found; falling back to system python3 (Drive checks will fail without google libs)" >&2
  PY="python3"
fi
exec "$PY" h3_controller.py preflight "$@"
