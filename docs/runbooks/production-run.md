# Production run runbook (live GPU, spends CU)

## Authorisation required

A production run needs the user's explicit go-ahead for THAT task (new
task = new 30 CU envelope, task_id + ledger). Never re-run on the basis of
an old authorisation.

## Steps

1. `./scripts/check_sessions.sh` — expect no active sessions and a known balance.
2. `./scripts/preflight.sh` — all checks green.
3. `./scripts/run_h3_ab.sh --confirm-live --variant A --run-id h3-<UTC>-<suffix>`
   (run-id format: h3-YYYYMMDDTHHMMSSZ-xxxxxx, see h3_controller.create_run_id).
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
5. After variant A succeeds, run B the same way (same task_id, same 30 CU
   envelope). Do not start B while A's session is still active.
6. `./scripts/check_outputs.sh <local mp4>` — ffprobe must read exactly
   274 frames at 24 fps, 1080x1920.
7. `./scripts/verify_shutdown.sh` — no active sessions; verify the CU
   balance moved only by the expected amount.

## If the VM restarts mid-run

See vm-restart-orphan-recovery.md FIRST: check/download the orphan session
BEFORE stopping it — a completed render can otherwise be destroyed.
