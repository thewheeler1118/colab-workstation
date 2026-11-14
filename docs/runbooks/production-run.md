# Production run runbook (live GPU, spends CU)

## Authorisation required

A production run needs the user's explicit go-ahead for THAT task (new
task = new 30 CU envelope, task_id + ledger). Never re-run on the basis of
an old authorisation.

## Steps

1. `./scripts/check_sessions.sh` — expect no active sessions and a known balance.
2. `./scripts/preflight.sh` — all checks green.
3. `./scripts/run_h3_task.sh --confirm-live --task-spec config/<task>.json --production-task-id <task_id> --run-id h3-<UTC>-<suffix>`
   (run-id format: h3-YYYYMMDDTHHMMSSZ-xxxxxx, see h3_controller.create_run_id).
   The task is declared in the spec file — see
   `docs/runbooks/generic-task-spec.md` and `config/task_spec.example.json`.
   The old `--variant A|B` form still works as a built-in test-fixture
   adapter only; new tasks always use `--task-spec`.
   The script resolves the production venv python and exports the
   matching `COLAB_CLI_PATH` itself (TASK-007.1); the legacy
   `run_h3_ab.sh` name is a thin wrapper that forwards to it.
   The script forwards --execute --confirm-gpu --authorize-task-gpu --allow-g4
   --allow-a100-highmem --fresh-inference. The run uses the noninteractive
   Drive download path by default (TASK-006 P0-B): the controller mints a
   short-lived read-only Drive token, proves the remote transport with a
   1 MiB Range probe, and the worker downloads the models — no TTY and no
   browser authorization step. The legacy interactive mount path needs
   --drive-source mount plus --approve-interactive-drive-mount and a TTY;
   this script does not pass them.
4. Watch the run dir logs/<run_id>/ (exec results are persisted per exec).
   Stage markers: WORKER_STAGE lines; completion requires MUSE_H3_DONE_JSON.
   A bare exec returncode 0 is NOT success (see incidents/17).
5. After one task's video succeeds, a new task is a new spec file (same
   procedure, new task_id, new 30 CU envelope). Do not start a second
   task while the first task's session is still active.
6. `./scripts/check_outputs.sh <local mp4>` — ffprobe must read exactly
   the spec's `total_frames` frames at the spec's `fps`, at the spec's
   `output_width`x`output_height` (A fixture: 274 frames at 24 fps,
   1080x1920).
7. `./scripts/verify_shutdown.sh` — no active sessions; verify the CU
   balance moved only by the expected amount.

## Supervised delivery (TASK-008, recommended entry point)

For a task that must finish unattended, run the task-level loop
instead of a single `run`:

`./scripts/run_h3_task.sh --confirm-live --deliver --task-spec config/<task>.json --production-task-id <task_id> [--task-deadline-utc <UTC>] [--max-task-attempts 2]`

or equivalently `run --auto-recover` with a `--run-id` for attempt 1
(default `--max-task-attempts 2`, hard cap 3). The loop, inside the
same TaskLock and 30 CU envelope, in order:

1. Reuses a `TASK_COMPLETED` run (0 CU) after re-verifying its Drive
   delivery evidence; if the evidence is gone it fails closed rather
   than re-inferring or blindly re-uploading.
2. Recovers a `COMPLETED_AWAITING_ARCHIVE` run by archiving only
   (0 GPU / 0 CU).
3. Resumes a non-terminal in-flight run (`_resume_run` takeover).
4. Only then, and only if the previous run is `FAILED` with its GPU
   session confirmed `STOPPED` by the three-state query, the remaining
   budget covers a full minimal run + cleanup reserve, and time to the
   deadline suffices, starts a controlled new attempt with a derived
   run_id. An `ACTIVE` or `UNKNOWN` old session strictly blocks a
   second GPU. Deterministic errors (illegal spec, SHA-256 mismatch,
   403, ACCOUNT_MISMATCH, budget exhausted) fail closed immediately —
   never retried.

## If the VM restarts mid-run

See vm-restart-orphan-recovery.md FIRST: check/download the orphan session
BEFORE stopping it — a completed render can otherwise be destroyed.
