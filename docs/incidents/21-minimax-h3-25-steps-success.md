# Incident: MiniMax-H3 25/25-step inference success

STATUS: CONFIRMED_FIXED

## What happened

Non-Turbo production: 25 steps, beta scheduler, 226 frames at 736x1312, seeds 42/43. The A run completed inference end to end and assembled 274 frames.

## Evidence

run_id: h3-20261112T090027Z-8b07c6 (274 frames, A.mp4 7,453,808 B, sha256 907a23dcf7fba719a3fd7d622c3477280698c2e1dd6a795c0f0ffd6593a63cbb)
  file_path: h3_contract.py (STEPS=25, scheduler beta); h3_remote_worker.execute_prompt
  line_or_function: h3_remote_worker.execute_prompt / main
  test_name: tests/test_h3_production.py (workflow contract)
  source_report: jobs/minimax_h3_ab/job.yaml status
