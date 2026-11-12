# Issue draft: P2: Evaluate MiniMax_H3_Clean against Muse baseline

STATUS: PROPOSED_ONLY — the candidate files have not been obtained; nothing from the audit is merged.

## Context

Third-party audit candidate files (NOT in this repo): config_contract.py, prompts_ab.py, workflow_builder.py, remote_worker.py, orchestrator.py, tests/test_pipeline.py. Baseline: this repo (h3_contract.py, h3_controller.py, h3_remote_worker.py).

## Reproduction

1. Obtain the actual MiniMax_H3_Clean source (it was never present in this VM — do not reconstruct it from the audit report).
2. Diff its prompt/workflow/worker against h3_contract.py / h3_remote_worker.py behaviour, test by test.
3. Only then propose a PR; never overwrite the verified controller.

## Acceptance

Acceptance: a written对照 (comparison) with per-file verdicts (adopt / adapt / reject) and the tests that would prove parity; no merge without it. Expected impact: any genuinely better idea from the audit is adopted with evidence; the verified baseline is never replaced on hearsay.

## Process

Issue → feature branch → tests → pull request → review → merge. Never commit directly to main.
