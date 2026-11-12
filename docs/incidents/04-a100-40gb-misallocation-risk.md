# Incident: A100 40GB VRAM mis-allocation risk

STATUS: CONFIRMED_FIXED

## What happened

A100 40GB must never be accepted: the GPU probe requires >= 76800 MiB for A100_HIGH_MEM (>= 92160 MiB for G4) and the choice requires the '--high-mem' flag. The 80GB High-Mem shape was allocated and probed in the B run.

## Evidence

run_id: h3-20261112T093207Z-7ab515 (A100 High-Mem 80GB, 8.88 CU/h; probe completed)
  file_path: h3_budget.py (GPU_CHOICES A100_HIGH_MEM min_vram_mib 76800); h3_gpu_probe.py
  line_or_function: h3_controller.probe_actual_gpu; h3_gpu_probe.CHOICES
  test_name: (probe tests are live-only; offline suite covers the policy constants via tests/test_config_contract.py)
  source_report: config/gpu_policy.yaml
