#!/usr/bin/env bash
# preflight.sh — offline + live read-only preflight. NEVER creates a GPU.
# Usage: ./scripts/preflight.sh [--run-id ID]
# Runs: python3 h3_controller.py preflight
# Preflight checks (h3_controller.preflight): local sources, CLI post-body
# and drivemount patches, PTY availability, budget ledger, live `colab
# usage` / `colab sessions` (read-only), GPU policy, and restore capacity.
# It does NOT allocate a session, mount Drive, or spend CU.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 h3_controller.py preflight "$@"
