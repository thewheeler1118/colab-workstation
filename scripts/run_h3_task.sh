#!/usr/bin/env bash
# run_h3_task.sh — LIVE GPU RUN for a generic --task-spec task (TASK-007.1).
# The standard entry point for every real run. Refuses to do anything
# unless the operator explicitly passes --confirm-live (plus --run-id
# and --production-task-id and exactly one of --task-spec / --variant).
#
# Standard (generic) usage:
#   ./scripts/run_h3_task.sh --confirm-live --production-task-id <TASK_ID> \
#       --task-spec <spec.json> --run-id h3-YYYYMMDDTHHMMSSZ-xxxxxx
# Legacy A/B fixture usage:
#   ./scripts/run_h3_task.sh --confirm-live --production-task-id h3-ab-20261112 \
#       --variant A --run-id h3-YYYYMMDDTHHMMSSZ-xxxxxx
#   New task (no ledger yet): add --authorize-new-task. Without it the
#   controller refuses to grant a fresh 30 CU budget (TASK-002 §5.2).
# Task-level supervised delivery (TASK-008): --deliver (or run with
#   --auto-recover) runs the deliver loop instead of a single attempt:
#   it reuses a delivered run, recovers an archive-only run, resumes an
#   in-flight run, and only behind the budget/session/deadline gates
#   starts a controlled new attempt (default --max-task-attempts 2,
#   max 3). --run-id is optional there (a fresh id is derived per
#   attempt); --task-deadline-utc bounds new attempts.
#
# Interpreter resolution (TASK-007.1, fix 1): the script resolves the
# production interpreter itself — prefer $REPO/.venv-colab-cli/bin/python
# (the pinned production venv), then $REPO/.venv/bin/python — and exports
# the matching COLAB_CLI_PATH, so a single script command works with no
# hand-assembled environment variables. The old run_h3_ab.sh hardcoded
# `exec python3 ...`, which called the system python3 (no google libs,
# no COLAB_CLI_PATH) and crashed before doing anything.
#
# Forwards to: <resolved python> h3_controller.py (run | deliver) --execute
#   --confirm-gpu --authorize-task-gpu --allow-g4 --allow-a100-highmem
#   --fresh-inference
#   --task-spec SPEC | --variant A|B --run-id ID --production-task-id ID [--authorize-new-task]
#   [--task-deadline-utc UTC] [--auto-recover] [--max-task-attempts N]
# The controller itself enforces: 30 CU task budget, 25 CU per-session cap,
# G4_THEN_A100_HIGHMEM policy, pinned ComfyUI commit, and (TASK-006 P0-B)
# the noninteractive Drive download preflight before allocation. The
# legacy interactive mount path needs --drive-source mount plus
# --approve-interactive-drive-mount and a TTY; this script does not pass
# them, so it runs the download path by default.
# This script creates a PAID GPU session and spends CU. Do not run it from CI.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
CONFIRM_LIVE=0; VARIANT=""; TASK_SPEC=""; RUN_ID=""; TASK_ID=""; AUTHORIZE_NEW_TASK=0
DELIVER=0; AUTO_RECOVER=0; MAX_ATTEMPTS=""; TASK_DEADLINE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --confirm-live) CONFIRM_LIVE=1; shift ;;
    --variant) VARIANT="$2"; shift 2 ;;
    --task-spec) TASK_SPEC="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --production-task-id) TASK_ID="$2"; shift 2 ;;
    --authorize-new-task) AUTHORIZE_NEW_TASK=1; shift ;;
    --deliver) DELIVER=1; shift ;;
    --auto-recover) AUTO_RECOVER=1; shift ;;
    --max-task-attempts) MAX_ATTEMPTS="$2"; shift 2 ;;
    --task-deadline-utc) TASK_DEADLINE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ "$CONFIRM_LIVE" -ne 1 ]; then
  echo "REFUSED: run_h3_task.sh requires --confirm-live. This creates a paid GPU session." >&2
  echo "Usage: $0 --confirm-live --production-task-id TASK (--task-spec SPEC | --variant A|B) --run-id h3-... [--deliver | --auto-recover] [--max-task-attempts N] [--task-deadline-utc UTC]" >&2
  exit 2
fi
if [ -n "$VARIANT" ] && [ -n "$TASK_SPEC" ]; then echo "REFUSED: --variant and --task-spec conflict; pass exactly one" >&2; exit 2; fi
if [ -z "$VARIANT" ] && [ -z "$TASK_SPEC" ]; then echo "REFUSED: --task-spec <spec.json> or --variant A|B is required" >&2; exit 2; fi
if [ -n "$VARIANT" ] && [ "$VARIANT" != "A" ] && [ "$VARIANT" != "B" ]; then echo "REFUSED: --variant must be A or B" >&2; exit 2; fi
if [ "$DELIVER" -eq 1 ] && [ "$AUTO_RECOVER" -eq 1 ]; then echo "REFUSED: --deliver and --auto-recover conflict; pass at most one" >&2; exit 2; fi
if [ -n "$MAX_ATTEMPTS" ] && [ "$DELIVER" -ne 1 ] && [ "$AUTO_RECOVER" -ne 1 ]; then echo "REFUSED: --max-task-attempts needs --deliver or --auto-recover" >&2; exit 2; fi
if [ -n "$MAX_ATTEMPTS" ]; then
  case "$MAX_ATTEMPTS" in
    1|2|3) ;;
    *) echo "REFUSED: --max-task-attempts must be 1, 2 or 3" >&2; exit 2 ;;
  esac
fi
# --run-id is required for a single run; the deliver loop derives a
# fresh id per attempt when none is given.
if [ "$DELIVER" -ne 1 ] && [ "$AUTO_RECOVER" -ne 1 ] && [ -z "$RUN_ID" ]; then echo "REFUSED: --run-id is required" >&2; exit 2; fi
if [ -z "$TASK_ID" ]; then echo "REFUSED: --production-task-id is required (the historical default is never reused silently)" >&2; exit 2; fi
# Resolve the production interpreter and its matching CLI. Gates above
# run first, so the refusal messages work with no venv present.
if [ -x "$REPO/.venv-colab-cli/bin/python" ]; then
  PY="$REPO/.venv-colab-cli/bin/python"
  export COLAB_CLI_PATH="$REPO/.venv-colab-cli/bin/colab"
elif [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
  export COLAB_CLI_PATH="$REPO/.venv/bin/colab"
else
  echo "REFUSED: no production venv python found (looked for $REPO/.venv-colab-cli/bin/python, then $REPO/.venv/bin/python). Install one with ./scripts/install_colab_cli.sh" >&2
  exit 2
fi
# Fast self-diagnosis: the controller needs all five control-plane
# modules in THIS interpreter (see h3_environment.CONTROL_PLANE_MODULES).
# verify_environment() gates on the same set before allocation; failing
# here names the missing module before any live call.
MISSING="$("$PY" -c "
import importlib.util
mods = ['google.auth', 'googleapiclient', 'requests', 'certifi', 'yaml']
missing = []
for m in mods:
    try:
        ok = importlib.util.find_spec(m) is not None
    except Exception:
        ok = False
    if not ok:
        missing.append(m)
print(' '.join(missing))
" 2>/dev/null || echo IMPORT-CHECK-FAILED)"
if [ -n "$MISSING" ]; then
  if [ "$MISSING" = "IMPORT-CHECK-FAILED" ]; then
    echo "REFUSED: control-plane import check could not run under $PY" >&2
  else
    echo "REFUSED: control-plane modules missing in $PY: $MISSING. Reinstall with ./scripts/install_colab_cli.sh" >&2
  fi
  exit 2
fi
EXTRA=()
if [ "$AUTHORIZE_NEW_TASK" -eq 1 ]; then EXTRA+=(--authorize-new-task); fi
if [ -n "$TASK_SPEC" ]; then EXTRA+=(--task-spec "$TASK_SPEC"); else EXTRA+=(--variant "$VARIANT"); fi
if [ -n "$RUN_ID" ]; then EXTRA+=(--run-id "$RUN_ID"); fi
if [ -n "$TASK_DEADLINE" ]; then EXTRA+=(--task-deadline-utc "$TASK_DEADLINE"); fi
if [ -n "$MAX_ATTEMPTS" ]; then EXTRA+=(--max-task-attempts "$MAX_ATTEMPTS"); fi
if [ "$DELIVER" -eq 1 ]; then
  exec "$PY" h3_controller.py deliver --execute --confirm-gpu --authorize-task-gpu --allow-g4 --allow-a100-highmem --fresh-inference --production-task-id "$TASK_ID" "${EXTRA[@]}"
fi
if [ "$AUTO_RECOVER" -eq 1 ]; then EXTRA+=(--auto-recover); fi
exec "$PY" h3_controller.py run --execute --confirm-gpu --authorize-task-gpu --allow-g4 --allow-a100-highmem --fresh-inference --production-task-id "$TASK_ID" "${EXTRA[@]}"
