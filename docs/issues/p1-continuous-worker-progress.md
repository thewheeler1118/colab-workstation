# Issue draft: P1: Continuous worker progress visibility

STATUS: PROPOSED_ONLY.

## Context

h3_remote_worker.py — set_stage / WORKER_STAGE markers; h3_controller.py — persist_exec_result / download_remote_comfyui_log. Historical runs: h3-20261111T080429Z-0c4fef (worker died ~146s in, stage had to be inferred).

## Reproduction

1. Run a mock worker that emits stage markers and confirm the controller surfaces the latest stage without waiting for completion.
2. Confirm the ComfyUI log is downloadable mid-run (download_remote_comfyui_log).

## Acceptance

Acceptance: a test proves the controller can report the worker's current stage (e.g. restore_weights / inference_submit) from persisted exec output while the session is still alive; no polling loop may create a GPU or spend CU. Expected impact: failures are diagnosed from the live stage instead of a post-mortem traceback.

## Process

Issue → feature branch → tests → pull request → review → merge. Never commit directly to main.
