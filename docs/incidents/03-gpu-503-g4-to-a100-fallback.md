# Incident: GPU 503 allocation failure and G4 → A100 High-Mem fallback

STATUS: CONFIRMED_FIXED

## What happened

G4 allocation can return 503 (capacity). Policy: at most two G4 attempts, then one A100 High-Mem attempt; attempt >= 3 raises 'GPU allocation attempts exhausted'.

## Evidence

run_id: h3-20261112T093207Z-7ab515 (B: G4 twice unallocated, A100 High-Mem allocated); 2026-11-11 isolation: valid G4 token → 503 3/3
  file_path: h3_budget.py
  line_or_function: h3_budget.choose_gpu_candidate / GPU_ATTEMPT_PLAN = ('G4', 'G4', 'A100_HIGH_MEM')
  test_name: (allocation-plan unit tests in tests/test_h3_production.py cover choose_gpu_candidate bounds)
  source_report: docs/audits/v3_cli_proxy_patch.md; jobs/minimax_h3_ab/job.yaml status
