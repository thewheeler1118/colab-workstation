# Issue draft: P0: Persist generated MP4 before GPU shutdown

STATUS: PROPOSED_ONLY.

TASK-002 NOTE (2026-11-12, offline-verified only): the delivery chain
is now wired — `run_variant` records INFERENCE_COMPLETED /
REMOTE_MP4_VALIDATED / LOCAL_MP4_DOWNLOADED / LOCAL_MP4_VALIDATED,
stops and verifies the GPU first, then `deliver_variant_to_drive`
re-proves the local file (path, SHA-256, 274f/1080x1920) and uploads
with bounded retries and server-side metadata verification
(`h3_controller.py`, branch `fix/drive-delivery-integration`, commit
`bfcfa71`; tests `tests/test_drive_delivery.py`, 8 tests). No live GPU
run has exercised this path yet — a real run id is still required
before CONFIRMED_FIXED.

## Context

h3_controller.py — download_and_validate_variant / stop_and_verify_session; h3_remote_worker.py — find_rendered_video. Historical run: h3-20261112T065629Z-061a20 (18 min inference product lost on shutdown).

## Reproduction

1. Reproduce the loss class offline: worker completes inference, find_rendered_video raises, session stops — assert today whether any artifact survives.
2. Design a bounded rescue: on find failure, list ComfyUI/output/ (RUN_ID prefix only), download any candidate > threshold, verify with ffprobe, and only then stop — inside the remaining session budget.

## Acceptance

Acceptance: a mock test proves that when inference succeeded but the history lookup failed, the MP4 is downloaded and ffprobe-verified before stop_and_verify_session runs; a budget test proves the rescue never exceeds the session's remaining CU (25 CU cap minus cleanup reserve 0.75). Expected impact: a GPU session can never again destroy a completed render.

## Process

Issue → feature branch → tests → pull request → review → merge. Never commit directly to main.
