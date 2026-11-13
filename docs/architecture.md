# Architecture

## North Star (immutable, user correction 2026-11-08)

Muse is the only AI agent. Natural language → Muse Code → Muse-managed
Python controller + official Google Colab CLI → Colab GPU → PNG/MP4 to
Muse-accessible persistent storage → release GPU → return results.
Antigravity v2 is a technical reference and code source only. The Mac is
not a GPU scheduling relay. See `docs/decisions/ADR-005.md`.

## System overview

```
Operator (TTY, browser for Drive OAuth)
  │
  ▼
h3_controller.py  (CLI: plan | check | preflight | run)
  │  imports h3_budget, h3_contract, drive_depot, h3_session_watchdog,
  │  reference_v2/colab_manager (parse_* / redact_*)
  ├─► official `colab` CLI 0.7.5 (vendored patches, see patches/)
  │     ├─► Colab GPU session (G4, then A100 High-Mem on capacity failure)
  │     └─► Drive mount (official drivemount, timeout_ms=600000) — legacy
  │           mount path only; default is the noninteractive Drive download
  │           (TASK-006 P0-B): short-lived read-only token + Range download
  ├─► Drive API (drive_depot / drive_api / drive_resumable) — depot verify
  └─► local ledger model_cache/h3_production_budget_<task>.json (gitignored)
        │
        ▼ (remote, via `colab exec -f`)
  h3_remote_worker.py  (on the Colab runtime, stdlib only + ComfyUI)
        ├─► ensure_comfy (Drive source cache → git clone fallback)
        ├─► restore_weights (Drive download via h3_drive_download, or
        │     DriveFS → NVMe on the legacy mount path; SHA-256 per file)
        ├─► ComfyUI :8188 — /object_info validation, /prompt submit
        ├─► find_rendered_video (SaveVideo history shapes)
        └─► assemble.py (226 frames + 48 poster frames = 274)
```

## Module map (flat repo root — see ADR-003)

| Area | Modules |
|---|---|
| Controller / state machine | `h3_controller.py` |
| Budget ledger | `h3_budget.py` |
| Contract (video, prompts, workflow patch) | `h3_contract.py` |
| Remote worker | `h3_remote_worker.py` |
| Drive depot / API / resumable upload | `drive_depot.py`, `drive_api.py`, `drive_resumable.py` |
| Auth / CLI resolution | `auth_support.py` |
| Session watchdog | `h3_session_watchdog.py` |
| GPU / drive / mount / restore probes | `h3_gpu_probe.py`, `h3_drive_probe.py`, `h3_mount_signature_probe.py`, `h3_mount_timeline.py`, `h3_mount_acceptance.py`, `h3_restore_probe.py`, `h3_init_remote.py` |
| Video assembly + QA + remote output check | `assemble.py`, `h3_video_qa.py`, `h3_output_check.py` |
| UI→API workflow conversion | `build_api_candidate.py` |
| Vendored CLI output parsing | `reference_v2/colab_manager.py` (only file used from `reference_v2/`) |
| Config (checked against code) | `config/*.yaml`, `config/*.example.json` |
| Job definition | `jobs/minimax_h3_ab/` (facade + `job.yaml`) |

Full per-file inventory: `docs/module_inventory.md`.
Stage-by-stage flow: `docs/execution_flow.md`.

## Key invariants

1. **Fail-closed everywhere.** A bare `colab exec` returncode 0 is not
   success (`check_worker_done_marker` requires `MUSE_H3_DONE_JSON`).
   A mount exit code 0 is not proof of a mount (signature probe +
   timeline). A `READY` flag file is not proof of a depot
   (`verify_ready` re-checks the Drive API, same account).
2. **Per-task 30 CU.** Every user-initiated task gets its own ledger and
   its own 30.00 CU envelope (ADR-001). The controller refuses
   `--project-budget-cu` ≠ 30.0.
3. **Pinned everything.** MiniMax-H3 revision
   `54472221d27c695fca9621b0c3ed52f06c4af707`, ComfyUI commit
   `61e524bdba7a4e193b45c2697f1af83c1beb86d6`, 4 model files with SHA-256,
   25 steps / beta scheduler / 226+48 frames / seeds 42/43, Turbo off.
4. **No credential ever leaves the operator's machine in git.** ADC lives
   at `~/.config/gcloud/application_default_credentials.json` (0600),
   never committed, never printed. The repo holds only Drive file IDs
   (non-credential references) and hashes.
5. **CI is offline.** No GPU, no Drive, no credentials (see
   `.github/workflows/ci.yml`). Live acceptance is a separate manual task.
6. **Every exec result is persisted** to `logs/<run_id>/`
   (`persist_exec_result`) so a failure stage stays diagnosable after the
   session stops — including after a VM restart kills the controller.

## Known gaps (honest list)

- `archive_variant_to_drive` exists but is **not called by `run_variant`**
  (no call site in `h3_controller.py`); the DRIVE_OUTPUT_VERIFIED stage
  is therefore UNVERIFIED in the live flow — see `docs/execution_flow.md`
  and `docs/incidents/24-drive-output-not-server-verified.md`.
- `find_rendered_video` checks existence + suffix + size only; it has no
  ffprobe decode gate and no RUN_ID-prefix fallback (see
  `docs/issues/p0-savevideo-history-schema.md`).
- VM restarts orphan GPU sessions; recovery is manual (see
  `docs/runbooks/vm-restart-orphan-recovery.md`).
- The drivemount 600s fix is mock-verified; live mounts so far completed
  authorization inside the old 120s window (see `docs/incidents/07`).
