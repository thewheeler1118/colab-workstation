#!/usr/bin/env bash
# check_budget.sh — READ-ONLY. Shows the task budget policy and live balance.
# Usage: ./scripts/check_budget.sh
# Prints the per-task 30 CU constants from h3_budget.py, then the live
# balance via `python3 h3_controller.py check` (read-only `colab usage`).
# The live ledger file lives under model_cache/ (gitignored, never committed).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -c "from h3_budget import PROJECT_BUDGET_CU, PER_SESSION_BUDGET_CU, CLEANUP_RESERVE_CU, MAX_SESSION_SECONDS, DEFAULT_PRODUCTION_TASK_ID, GPU_ATTEMPT_PLAN; print(f'task_budget_cu={PROJECT_BUDGET_CU} per_session_cu={PER_SESSION_BUDGET_CU} cleanup_reserve_cu={CLEANUP_RESERVE_CU} max_session_seconds={MAX_SESSION_SECONDS} default_task={DEFAULT_PRODUCTION_TASK_ID} gpu_plan={list(GPU_ATTEMPT_PLAN)}')"
python3 h3_controller.py check
