# Issue draft: P1: Account-safe 30 CU task ledger

STATUS: PROPOSED_ONLY.

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
