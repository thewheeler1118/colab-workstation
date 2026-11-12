# Preflight runbook (no GPU, no CU)

Run `./scripts/preflight.sh` (or `python3 h3_controller.py preflight`).
Preflight is read-only plus live `colab usage` / `colab sessions` reads; it
never allocates a session.

## Checks, in order (h3_controller.preflight)

1. Local sources present (`workflow_api.json`, `model_cache/verified_manifest.json`, `h3_budget.py`).
2. ComfyUI source cache present (`_comfyui_cache`).
3. Drive READY manifest valid (`_drive_ready` → drive_depot.verify_ready shape).
4. Workflow patches cleanly for A and B (`_workflow` → patch_api_workflow).
5. Official CLI present (`_cli` → `colab version`).
6. CLI post-body patch present (`_cli_post_patch` → cli_post_body_patch_present).
   If missing: reinstall patches/google-colab-cli-0.7.5/ into the venv and
   re-run; preflight fails closed otherwise.
7. CLI drivemount timeout patch present (`_cli_drivemount_patch`).
8. Interactive PTY available (`_pty` — the official drivemount needs a TTY;
   without one the run is blocked BEFORE any GPU is requested).
9. Budget ledger initialises within the 30 CU task envelope (`_budget`).
10. Restore capacity: 40GB fits on the remote disk within budget (`_restore_capacity`).
11. No active sessions (`_sessions` — a live session blocks a new run).
12. GPU policy is exactly G4_THEN_A100_HIGHMEM (`_policy`).

## If preflight fails

- Patch missing → apply the patch files, do not bypass the gate.
- Active sessions → run `./scripts/check_sessions.sh`, then
  `./scripts/verify_shutdown.sh` after stopping (see shutdown-verification.md).
- Budget refusal → check `./scripts/check_budget.sh`; never edit the ledger
  by hand to force a pass.
