# workstation/ — FUTURE package layout (PLANNED, no implementation)

This directory is the **planned** home for the packaged form of the
workstation. It intentionally contains **no implementation code** in
v0.1.0.

## Why the code is not here yet

The production modules (`h3_controller.py`, `h3_budget.py`,
`h3_remote_worker.py`, …) import each other **top-level**
(`from h3_budget import ...`) and the tests do
`sys.path.insert(0, str(ROOT))` where `ROOT` is the repo root.
`h3_controller.py` also inserts `reference_v2/` into `sys.path` to
import `colab_manager`. Moving those modules into `workstation/`
packages in the first migration would break every import and every
test, for zero production benefit.

So v0.1.0 keeps **all verbatim production `.py` files flat at the repo
root** — that flat root is the compatibility baseline. The package
refactor is a separate, future PR (see `docs/issues/` — package-layout
refactor; not yet filed as a numbered issue because it must not change
runtime behaviour).

## Planned subpackages

- `controller/` — run orchestration (`h3_controller.py`)
- `budget/` — per-task CU ledger (`h3_budget.py`)
- `sessions/` — session registry + watchdog (`h3_session_watchdog.py`)
- `auth/` — ADC / CLI resolution (`auth_support.py`)
- `drive/` — Drive depot, resumable upload, API helpers
  (`drive_depot.py`, `drive_api.py`, `drive_resumable.py`)
- `gpu/` — GPU probing + allocation policy (`h3_gpu_probe.py`)
- `comfyui/` — ComfyUI cache, worker, workflow contract
  (`h3_comfyui_cache.py`, `h3_remote_worker.py`, `h3_contract.py`)
- `video/` — assembly + QA (`assemble.py`, `h3_video_qa.py`,
  `h3_output_check.py`)
- `observability/` — mount timeline, exec-result persistence,
  log markers (`h3_mount_timeline.py`, `h3_controller.py`
  `persist_exec_result` / `check_worker_done_marker`)

Every subpackage above is currently **PLANNED**. Do not create empty
`.py` files pretending to be implementations, and do not import from
`workstation.*` — nothing there exists yet.
