# Execution flow — 19-stage state machine

Each stage maps to the real file:function that implements it, verified by
grep against this repo. Stages marked UNVERIFIED have no call site or no
live evidence in the migrated code — they are not guessed.

Log markers are the grep-able strings an operator (or another AI) can
search for in `logs/<run_id>/` and remote stdout.

## Stage index

| # | Stage | File:function | Success condition | Failure / error | Retry / timeout / budget | Test | Log marker |
|---|---|---|---|---|---|---|---|
| 1 | PREFLIGHT | `h3_controller.preflight` | All 12 checks pass (sources, CLI patches, PTY, budget, sessions, policy) | Any check raises; CLI patch missing → fail-closed before GPU | No retries; `drivemount --help` probe 20s | `tests/test_h3_production.py` (patch presence), `tests/test_h3_mount_repair.py` | preflight JSON result |
| 2 | ACCOUNT_VERIFIED | `h3_controller.run_variant` → `drive_depot.drive_service` + `drive_depot.account_matches` (+ `auth_support.audit_user_adc_file`) | Same Google account for Colab ADC and Drive | `ACCOUNT_MISMATCH` → abort before GPU | None; no GPU yet | `tests/test_h3_production.py::test_same_account_required` | `same_google_account: true` |
| 3 | DRIVE_READY_VERIFIED | `drive_depot.verify_ready` | Manifest status READY, method drive_v3_server_api, exactly the 4 pinned files, same depot root, remote READY doc re-fetched (size + MD5 match) | Any mismatch raises RuntimeError | None; no GPU yet | `tests/test_drive_depot.py`, `tests/test_h3_production.py::test_drive_ready_required_before_gpu` | `{"ready": true, "files": 4, "bytes": 40073842159}` |
| 4 | BUDGET_INITIALIZED | `h3_controller.initialize_project_budget` → `h3_budget.load_or_create_budget_ledger` | Ledger created/baselined on live `colab usage` balance; hard_stop = balance − 30.00 CU (+ 0.14 prior unbilled reserve) | Ledger cap ≠ 30.0 raises; balance unparsable raises | None | `tests/test_h3_mount_repair.py::test_ledger_survives_restart_baseline_unchanged` | ledger JSON in `model_cache/` |
| 5 | GPU_ALLOCATING | `h3_controller.run_variant` allocation loop → `h3_budget.choose_gpu_candidate` + `h3_budget.allocation_command` + `h3_controller.run_cli` (`colab new`, timeout 420s) | `colab new` exits 0; session recorded (`allocated` event); lease registered (`h3_gpu_lease_registry.register_lease`, TASK-006 P0-C) before watchdog start — a local registry failure aborts the run and the outer finally stops the session | 503 capacity → next attempt (max 3: G4, G4, A100_HIGH_MEM); transport error → same G4 once only; late allocation detected via `inspect()` → abort | 3 attempts; CLI-internal G4 503 retries (up to 3, 30s each + 10/20/40s backoff); 420s per `new` | allocation-plan tests in `tests/test_h3_production.py`; lease registration in `tests/test_gpu_lease_registry.py` | `allocation_attempt`, `allocated` |
| 6 | GPU_VERIFIED | `h3_controller.probe_actual_gpu` → remote `h3_gpu_probe.py` (`nvidia-smi`, `H3_GPU_CHOICE` env) | Name tokens match (G4: RTX PRO 6000 + BLACKWELL; A100: A100) and VRAM ≥ min (G4 92160 MiB, A100_HIGH_MEM 76800 MiB); live rate ≤ 14.0 CU/h | Hardware mismatch / VRAM short / rate unknown or > 14.0 → abort | Bounded retries on read timeout (timeout=10); probe exec timeout 180s | (live-only; policy constants in `tests/test_config_contract.py`) | `H3_GPU_EVIDENCE:`, `GPU_H3_G4_OK`, `gpu_probed` |
| 7 | DRIVE_MOUNTING / DOWNLOAD_PROBE | Default (TASK-006 P0-B, Issue #44): `h3_controller` mints a short-lived read-only Drive token, uploads it (0600) + `h3_drive_download.py`, runs remote `h3_drive_download_probe.py` (1 MiB Range probe, timeout 180s). Legacy mount path only: `h3_controller.mount_drive` / `mount_drive_interactive` (official `colab drivemount`, patched remote `timeout_ms=600000`, local ceiling 660s) after the remote signature probe (`h3_mount_signature_probe.py`, timeout 120s) accepts `timeout_ms` | Default: probe stdout contains `MUSE_DRIVE_DOWNLOAD_PROBE_OK`. Mount: mount completes within `budget_sec` | Default: probe failure raises, GPU released. Mount: signature missing → abort before mount; mount failure raises with timeline | Mount deadline = budget-derived `budget_sec`; local subprocess floor 720s | `tests/test_drive_download.py` (offline simulator); `tests/test_h3_mount_repair.py` (mount: 300s-auth cases, timeline) | `drive_download_probe_ok`, mount timeline JSON |
| 8 | DRIVE_MOUNTED / DOWNLOAD_READY | Default: token delivered + probe passed. Mount: `h3_controller.check_mount_signature` + `h3_mount_timeline.build_mount_timeline` | Default: probe marker present. Mount: signature line present; timeline shows `drive_auth_success` | Missing probe marker / signature / auth event → fail-closed | — | `tests/test_h3_mount_repair.py::test_mount_fails_after_authorization_despite_exit_zero` (mount) | `MUSE_DRIVE_DOWNLOAD_PROBE_OK` (default), `MUSE_DRIVE_MOUNT_MODELS_VISIBLE` (mount) |
| 9 | MODELS_RESTORING | `h3_remote_worker.restore_weights` (+ `check_restore_capacity`, `estimate_restore`, `project_restore_throughput`) | All 4 files on NVMe with per-file SHA-256 match; disk + cost gates pass first. Default: downloaded via `h3_drive_download` (Range resume, token renewal via job state). Mount: copied DriveFS → NVMe | Disk short / projected cost over budget → refuse before copying; download failure raises (fail closed) | Copy/download resumable per file; gates enforced pre-copy | `tests/test_drive_download.py` (download simulator); `tests/test_h3_mount_repair.py` (restore capacity/throughput tests) | `WORKER_STAGE: restore_weights` |
| 10 | MODELS_VERIFIED | Default: the worker's per-file SHA-256 verify inside `h3_drive_download` (no separate probe script). Mount: remote `h3_drive_probe.py` (exec, timeout 180s) | Default: all 4 downloads verified. Mount: stdout contains `MUSE_DRIVE_MOUNT_MODELS_VISIBLE 4` | Default: download error → `WORKER_FAILED_JSON`. Mount: anything else → `Mounted Drive model cache not visible; release GPU` | No retry (fail-closed) | `tests/test_drive_download.py`; `tests/test_h3_production.py` (drive-ready gates) | `CACHE_VERIFIED_NVME` (default), `MUSE_DRIVE_MOUNT_MODELS_VISIBLE 4` (mount) |
| 11 | COMFY_INSTALLING | `h3_remote_worker.ensure_comfy` → `restore_comfyui_from_cache` (Drive tarball, SHA verified) or `clone_comfyui_atomic` (git clone, retry) | Pinned commit `61e524bd…` verified (`verify_comfyui_commit`) | Clone exit 128 / commit mismatch → `WORKER_FAILED_JSON` (whole body in try) | `run_cmd_with_retry` (2 attempts, 20s delay) | `tests/test_h3_comfyui_cache.py` (21 tests) | `WORKER_STAGE: ensure_comfy` |
| 12 | COMFY_READY | `h3_remote_worker.wait_health` (deadline +180s) | `/object_info` responds; `COMFYUI_HEALTH_OK` | Timeout / proc death → failure | 180s health deadline | — (live-only) | `COMFYUI_HEALTH_OK`, `COMFY_OBJECT_INFO_VALIDATED` |
| 13 | WORKFLOW_VALIDATED | `h3_contract.patch_api_workflow` + `h3_remote_worker.validate_api_against_object_info` | Pinned node ids/types match (136/137/139/124/129/92/127/128/146); Turbo node 145 removed; `object_info` covers every used class | Node/type drift or missing class → ValueError; `/prompt` would 400 | No retry; drift must be mapped explicitly | `tests/test_contract.py`; `scripts/check_workflow_schema.py` | `COMFY_OBJECT_INFO_VALIDATED` |
| 14 | INFERENCE_SUBMITTED | `h3_remote_worker.execute_prompt` (`/prompt`, `timeout_sec` = min(1800, max(100, budget_sec − elapsed − 120))) | Prompt accepted; history polling starts | HTTP 400 (e.g. dead LoRA branch) → `WORKER_FAILED_JSON` | Single submit; timeout is budget-derived | `tests/test_h3_production.py` (workflow contract) | `WORKER_STAGE: inference_submit` |
| 15 | INFERENCE_COMPLETED | `h3_remote_worker.execute_prompt` returns history outputs | History contains a completed prompt entry | Timeout / error entry → failure | Bounded by `inference_timeout_sec` | — (live-only) | `WORKER_STAGE: find_rendered_video` |
| 16 | VIDEO_LOCATED | `h3_remote_worker.find_rendered_video` | File exists under `ComfyUI/output/`, suffix `.mp4`/`.webm`/`.mov`, size > 10000 (reads `videos`/`gifs` always, `images` only when `animated` is truthy) | `RuntimeError: did not contain verifiable saved video` (history keys included in the message) | No retry; no RUN_ID-prefix fallback (see P0 issue) | `tests/test_savevideo_history.py` (8 tests) | `WORKER_STAGE: assemble_mp4` |
| 17 | VIDEO_DOWNLOADED | `h3_controller.download_and_validate_variant` → `run_cli` (`colab download`, timeout 600s) + `h3_video_qa.validate_final_video` | Local MP4 ffprobe-reads exactly 274 frames, 24 fps, 1080x1920 | Any mismatch → failure (no success report) | Download 600s; metadata JSON download best-effort (120s) | `tests/test_h3_production.py` (download/validate) | `version_downloaded`, `MUSE_H3_DONE_JSON` |
| 18 | VIDEO_QA_PASSED | Remote `h3_output_check.py` → `OUTPUT_CHECK_JSON` + `h3_controller.check_remote_output` (before download); local `h3_video_qa.validate_final_video` + `compare_poster_tail` (poster-tail PSNR ≥ 30 dB) | Remote: `ok=true`, 274 frames, 24 fps, 1080x1920. Local: same + poster-tail PSNR | `OUTPUT_CHECK_JSON ok=false` → fail-closed, no download | Output check exec 180s; ffprobe 120s | `tests/test_h3_production.py` | `OUTPUT_CHECK_JSON:{"ok": true, ...}`, `MP4_WRITTEN` |
| 19 | DRIVE_OUTPUT_VERIFIED | `h3_controller.archive_variant_to_drive` + `check_archive_metadata` | MP4 uploaded to `Muse-H3-Video-Outputs/<RUN_ID>/`, server metadata re-queried, parent == run folder, not trashed | Parent/trash check fails → RuntimeError | — | (none on file) | **UNVERIFIED — see note below** |
| — | GPU_STOP_VERIFIED | `h3_controller.stop_and_verify_session` (+ `h3_session_watchdog.stop_session`, 3 attempts) in `run_variant`'s `finally` | Session confirmed gone; `mark_session_stopped`; `sessions == []` and rate 0 on re-check | `Stop not verified; run FAILED regardless of generated files` | Stop attempts: 3 × (`stop` 65s + 3s re-check) | `tests/test_h3_mount_repair.py::test_session_cleanup_verified` | `finished` event with `stop_verified: true` |

## UNVERIFIED stage note

**DRIVE_OUTPUT_VERIFIED (stage 19): `archive_variant_to_drive` is defined
in `h3_controller.py` but has no call site in `run_variant`** (verified by
grep: only the definition exists). The function itself is sound — it
uploads to `Muse-H3-Video-Outputs/<RUN_ID>/` and re-queries server
metadata via `check_archive_metadata` — but the live flow never invokes
it, so Drive delivery is NOT verified by the current production run.
Do not claim otherwise. See `docs/incidents/24-drive-output-not-server-verified.md`
and `docs/issues/p0-persist-mp4-before-shutdown.md`.

## Pre-stage gates (before any GPU)

`run_variant` refuses to proceed unless ALL of these hold:
`--execute --confirm-gpu --authorize-task-gpu --allow-g4 --allow-a100-highmem`,
`--fresh-inference`,
`--project-budget-cu 30.0`, `--per-session-budget-cu 25.0`,
`--gpu-policy G4_THEN_A100_HIGHMEM`, a pinned `--comfy-ref`, and no
foreign or managed-active sessions (`ensure_no_foreign_sessions`,
`reconcile_on_startup`). Default download path (TASK-006 P0-B): the
download preflight (read-only token mint + Drive metadata read) runs
before allocation; no TTY and no `--drive-cache-dir` under
`/content/drive/` are required. Legacy mount path only:
`--approve-interactive-drive-mount`, `--drive-cache-dir` under
`/content/drive/`, and an interactive TTY. The watchdog background process
(`h3_session_watchdog.py watch --session …`) enforces the
budget-derived deadline and stops the session at the hard stop.
