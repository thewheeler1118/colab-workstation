# Issue draft: P2: G4/A100 cost and latency optimization

STATUS: PROPOSED_ONLY.

## Context

h3_budget.py — GPU_CHOICES (max_rate_cu_h 14.0 for both shapes); h3_controller.probe_actual_gpu. Historical runs: A on G4 (h3-20261112T090027Z-8b07c6); B fell back to A100 High-Mem 80GB at 8.88 CU/h (h3-20261112T093207Z-7ab515).

## Reproduction

1. Collect wall-clock and CU-per-variant for G4 vs A100 High-Mem from recorded runs.
2. Test whether the 14.0 CU/h planning ceiling matches observed rates.

## Acceptance

Acceptance: a note in docs/research/ records measured G4 vs A100 cost/latency with run ids and states whether the attempt plan or the 14.0 ceiling should change. Expected impact: the GPU policy stops being a guess.

## Process

Issue → feature branch → tests → pull request → review → merge. Never commit directly to main.
