# Issue draft: P1: Account-safe 30 CU task ledger

STATUS: PROPOSED_ONLY.

TASK-002 NOTE (2026-11-12, offline-verified only): a missing ledger
now refuses without explicit authorisation
(`NEW_TASK_REQUIRES_AUTHORIZATION`, `--authorize-new-task`);
`--production-task-id` is required at the controller and in
`scripts/run_h3_ab.sh`; the historical default `h3-ab-20261111` is
never reused silently; A/B and retries share one task's 30 CU
(`h3_budget.py`, `h3_controller.py`, branch `fix/task-budget-isolation`,
commit `ccf405b`; tests `tests/test_task_budget.py`, 9 tests). No live
GPU run has exercised this path yet — a real run id is still required
before CONFIRMED_FIXED.

## Context

h3_budget.py — initialize/ledger functions; h3_controller.py — initialize_project_budget. Historical context: user correction 2026-11-11 09:12 (60 CU cumulative reading voided; per-task 30 CU).

## Reproduction

1. Create two ledgers for two task ids and assert neither shares state and neither exceeds 30.0 CU.
2. Attempt to initialise a ledger with project_budget_cu != 30.0 and assert the controller refuses.
3. Assert no ledger file, account id, or personal identifier is ever written outside model_cache/ (gitignored).

## Acceptance

Acceptance: a test proves two tasks' ledgers are independent and capped at 30.0 CU each; a scan test proves no ledger content is committable (git check-ignore). Expected impact: the 60 CU misreading class cannot recur, and no personal billing data can leak into git.

## Process

Issue → feature branch → tests → pull request → review → merge. Never commit directly to main.
