# Incident: CU billing delay and unbilled-cost estimation

STATUS: FIXED_BUT_NOT_LIVE_VERIFIED

## What happened

Colab CU postings lag real usage, so a ledger baselined on the live balance can understate spend. The ledger carries a prior_task_unbilled_reserve_cu (0.14 CU) and estimate_unbilled_cu projects in-flight cost from start time × rate; effective_hard_stop_cu adds the reserve to the hard stop. The mechanism is implemented and unit-tested; whether late postings behaved as modelled has not been separately verified live.

## Evidence

run_id: h3-ab-20261111 ledger (baseline 162.77, max observed spent 18.58 at 2026-11-12T09:49 UTC)
  file_path: h3_budget.py
  line_or_function: h3_budget.estimate_unbilled_cu / effective_hard_stop_cu; h3_controller.initialize_project_budget
  test_name: test_h3_mount_repair (test_estimate_unbilled_cu; test_effective_balance; test_ledger_survives_restart_baseline_unchanged)
  source_report: (code + ledger math only)
