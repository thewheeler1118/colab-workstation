#!/usr/bin/env bash
# run_h3_ab.sh — LIVE GPU RUN. Refuses to do anything unless the operator
# explicitly passes --confirm-live (plus --variant, --run-id and
# --production-task-id).
# Usage: ./scripts/run_h3_ab.sh --confirm-live --production-task-id h3-ab-20261112 --variant A --run-id h3-YYYYMMDDTHHMMSSZ-xxxxxx
#   New task (no ledger yet): add --authorize-new-task. Without it the
#   controller refuses to grant a fresh 30 CU budget (TASK-002 §5.2).
#   Continuing the historical task h3-ab-20261111 (e.g. the B re-run):
#   pass --production-task-id h3-ab-20261111 explicitly, no new flag.
# Forwards to: python3 h3_controller.py run --execute --confirm-gpu
#   --authorize-task-gpu --allow-g4 --allow-a100-highmem
#   --approve-interactive-drive-mount --fresh-inference
#   --variant A|B --run-id ID --production-task-id ID [--authorize-new-task]
# The controller itself enforces: 30 CU task budget, 25 CU per-session cap,
# G4_THEN_A100_HIGHMEM policy, pinned ComfyUI commit, interactive TTY for
# the official drivemount, and a Drive path under /content/drive.
# This script creates a PAID GPU session and spends CU. Do not run it from CI.
set -euo pipefail
cd "$(dirname "$0")/.."
CONFIRM_LIVE=0; VARIANT=""; RUN_ID=""; TASK_ID=""; AUTHORIZE_NEW_TASK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --confirm-live) CONFIRM_LIVE=1; shift ;;
    --variant) VARIANT="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --production-task-id) TASK_ID="$2"; shift 2 ;;
    --authorize-new-task) AUTHORIZE_NEW_TASK=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ "$CONFIRM_LIVE" -ne 1 ]; then
  echo "REFUSED: run_h3_ab.sh requires --confirm-live. This creates a paid GPU session." >&2
  echo "Usage: $0 --confirm-live --production-task-id TASK --variant A|B --run-id h3-..." >&2
  exit 2
fi
if [ "$VARIANT" != "A" ] && [ "$VARIANT" != "B" ]; then echo "REFUSED: --variant A|B is required" >&2; exit 2; fi
if [ -z "$RUN_ID" ]; then echo "REFUSED: --run-id is required" >&2; exit 2; fi
if [ -z "$TASK_ID" ]; then echo "REFUSED: --production-task-id is required (the historical default is never reused silently)" >&2; exit 2; fi
EXTRA=()
if [ "$AUTHORIZE_NEW_TASK" -eq 1 ]; then EXTRA+=(--authorize-new-task); fi
exec python3 h3_controller.py run --execute --confirm-gpu --authorize-task-gpu --allow-g4 --allow-a100-highmem --approve-interactive-drive-mount --fresh-inference --variant "$VARIANT" --run-id "$RUN_ID" --production-task-id "$TASK_ID" "${EXTRA[@]}"
