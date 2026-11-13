# TASK-006 P1-B / P1-C design — restart-safe bootstrap, duplicate-allocation guard, deadline-bound E2E metric

Issue #50. Written BEFORE implementation. Status: design record; the
implementation must match it, and any deviation is recorded here as a
correction, not silently applied.

## 0. What P1-B and P1-C are

P1-B makes a run survivable across a Muse/VM restart at every phase:
allocation, bootstrap, inference, upload. P1-C measures whether a whole
production task actually completes end to end within its deadline, and
adds the full-chain fault injection and the production acceptance
runbook that prove the recovery paths.

Nothing in this PR spends CU, creates a GPU, touches the Drive depot,
deploys a cron, or claims any production reliability rate. The
production sample stays n=0 / UNKNOWN until real runs exist.

## 1. The four P1-B gaps (verified in the code before designing)

### 1.1 Allocation window — a live GPU no managed file knows about

`allocate_managed_session` (h3_controller.py) generates the session
name locally (`"muse-h3-" + uuid hex`) BEFORE the CLI `new` call, but
`record_session` and `_register_gpu_lease` run only AFTER the CLI
returns. A VM restart in that window (2026-11-12 had 7 restarts)
leaves a live, billing GPU that:

- `reconcile_on_startup` cannot see (it reads the managed file only);
- the backup shutdown executor cannot stop (it iterates the lease
  registry only);
- the next run sees as a *foreign* session and refuses.

The outer finally does stop the session when the controller survives
(the `allocation_attempted` flag + `stop_and_verify_session`), so the
hole is exactly the controller-death window, not the normal path.

### 1.2 Bootstrap has no durable state

Between `GPU_ALLOCATED` and `WORKER_STARTED` the controller runs a
long synchronous sequence: `h3_init_remote`, contract/workflow uploads,
Drive download probe, drive-upload block, worker file uploads, task
upload. The manifest records only the two endpoints. A restart
mid-bootstrap leaves:

- manifest at `GPU_ALLOCATED`, session ACTIVE, no record of how far
  bootstrap got;
- resume (`_resume_run`) polling a worker that was never launched:
  `poll_worker` raises `WorkerPollError` (no state file) and resume
  refuses, leaving the GPU running. The only path is a manual stop +
  a new run_id.

### 1.3 Two controllers can race one task

The run_id duplicate guard blocks a second `run` with the SAME run_id.
Nothing blocks a second controller running a DIFFERENT run_id for the
same task in parallel. `reconcile_on_startup` blocks only when the
first session is already in the managed file — there is a race window
between allocation and registration (see 1.1), and a second controller
that starts before the first registers sees an empty managed file.

### 1.4 A network timeout is treated as a task failure

`wait_for_worker` fails the run after 3 consecutive poll failures,
even when the worker is healthy and only the channel is down. A
transient `inspect()` / exec failure mid-run has the same effect: the
exception propagates, the outer finally stops the GPU, and the manifest
records FAILED. The failure reason written is the network error, not
the real state — and the run can never be resumed as "the worker was
fine, the channel was not".

## 2. P1-B design

### 2.1 Pre-registration of sessions and leases

- `record_session` gains a `status` argument. The controller records
  the session as `"allocating"` BEFORE the CLI `new` call, and
  confirms it to `"active"` after the CLI returns (new
  `confirm_session_active`, a locked load-modify-save transition
  `allocating -> active`; re-recording the same session keeps its
  version monotonic, as today).
- The lease is pre-registered the same way, BEFORE the CLI call, with
  `deadline_final=False`. The executor never stops a non-final lease,
  so a pre-registered lease is safe: it is visible but inert until the
  post-probe finalisation, which is unchanged.
- `reconcile_on_startup` resolves `allocating` records by live query:
  - live ACTIVE → the record is reported as `allocating_live` and
    BLOCKS a new allocation exactly like a managed active session
    (the operator stops it explicitly with the `stop` action, or
    resumes the run — see 2.2; the reconciler never adopts it
    silently and never stops it blindly);
  - live STOPPED / not found → the record is marked failed
    (`status="allocation_failed"`, audit event) and no longer blocks.
  - live UNKNOWN (query raises) → reported as unknown and blocks a
    new allocation (an unjudgeable state never permits a second GPU).
- `stop_managed_session` accepts an `allocating` record for stopping:
  stopping an allocating record is an explicit operator decision, not
  a silent ALREADY_STOPPED refusal. (PR-1 made it refuse non-`active`
  records; `allocating` is the one deliberate exception, and only
  through the explicit `stop` action.)

### 2.2 Bootstrap state machine

- New manifest field `bootstrap` (schema_version 3; v1/v2 manifests
  load with `bootstrap={}` and `last_transient_error=None`):
  `{"steps": {<step>: {"state": "done"|"skipped"|"failed", "at_utc": ...}},
  "current_step": <step>|null}`. Recorded through a new `bootstrap`
  parameter on `update_run` (same merge shape as `artifacts`).
  `current_step` is set BEFORE a step is attempted and cleared (with
  the step marked `done`) after it succeeds — so a restart leaves an
  exact pointer at the interrupted step.
- Bootstrap steps (download path, in order): `init_remote`,
  `upload_contract_files`, `drive_download_probe`,
  `drive_upload_block` (fail-soft: recorded `skipped`, never `failed`),
  `upload_worker_files`, `upload_task`, `launch_worker`.
  The mount path records the same shape with `mount_drive` /
  `mount_probe` in place of the download steps.
- The sequence is factored out of `run_variant` into
  `_run_bootstrap(args, session, run_dir, budget_sec, live_rate,
  bootstrap_state)` — one code path, used by both the fresh run and
  the resume path. Steps already marked `done` are skipped on re-entry
  (uploads overwrite the same remote paths, so re-running a step is
  idempotent); the drive download probe is always re-run before launch
  (cheap, and it re-proves the remote still holds the token files).
- Resume (`_resume_run`): when the session is ACTIVE and the worker
  state is unobservable, the manifest decides:
  - worker never launched (no `WORKER_STARTED` stage, no launch
    record in `bootstrap`): enter `_resume_bootstrap` — re-run the
    bootstrap from the recorded step, launch the worker, then continue
    into the normal poll loop (`wait_for_worker` + `_resume_finish`).
    No new GPU is allocated; no inference is re-run (none ever ran).
  - a launch record EXISTS but the worker state file is missing:
    REFUSE with "worker launched but unobservable — stop the session
    explicitly". Re-launching could create a second worker on the same
    GPU; the refusal is the fail-closed answer.
  - worker state file exists but is corrupt: REFUSE (unchanged).
- `_resume_bootstrap` recomputes `budget_sec` from the live rate +
  ledger exactly as `_resume_run` already does for the poll loop, and
  refuses when the remaining budget cannot cover a minimal resumed run
  + cleanup reserve (same 300 s floor).

### 2.3 Controller lock — one controller per task

- New stdlib-only module `h3_task_lock.py`: `TaskLock`
  (context manager) + `TaskLockBusy`. Lock file
  `model_cache/h3_task_<sanitised task id>.lock`; `fcntl.flock(fd,
  LOCK_EX | LOCK_NB)`. The lock file also records the holder pid +
  acquisition time (for the refusal message). `flock` is released
  automatically when the holder process dies — including a VM restart
  — so there are no stale locks to clean up.
- Acquired at the top of `run_variant`, `_resume_run`, `_stop_run`
  and `_reconcile_task`, BEFORE the run_id duplicate check (two
  controllers racing the same run_id must serialise before either
  reads the manifest), released in a `finally`. A second controller
  refuses immediately with the holder pid + task id; it never waits
  (a run takes ~30 min; waiting would hide a stuck controller).
- `status` / `recovery_view` stay lock-free (read-only).

### 2.4 Transient vs fatal

- New manifest field `last_transient_error`
  (`{"error": ..., "at_utc": ..., "count": n}`), set through a new
  `transient_error` parameter on `update_run`. It never moves
  `task_state` and never sets `last_error`: a transient is not a
  failure.
- `wait_for_worker`: the `max_consecutive_poll_failures=3` cap becomes
  a *degraded* threshold, not a failure. After 3 consecutive poll
  failures the loop records `poll_degraded` (audit event + manifest
  transient error) and KEEPS polling until `deadline_monotonic`. The
  run ends only on: the budget wall time while unobservable (failure
  reason: the deadline, not the network), a worker FAILED stage, or a
  confirmed stop. The watchdog still enforces the deadline on its own
  poll loop while the channel is down, so extending the retry window
  cannot burn past the deadline.
- Post-allocation `inspect()` calls go through a bounded retry
  (`_inspect_live`, 3 attempts, short backoff, transient errors
  recorded). Pre-allocation `inspect()` still refuses on failure:
  the balance cannot be verified, so nothing is allocated.
- `poll_worker` itself stays strict: one poll failure is still a
  `WorkerPollError`. The retry policy lives in `wait_for_worker`,
  not in the probe.

### 2.5 UNKNOWN blocks new paid allocation (verify + test)

Already true in the code and now covered by tests, not changed:

- `_resume_run` refuses on `SessionQueryError` / `UNKNOWN` without
  allocating or stopping blindly.
- `run_variant` refuses while any managed session is active, and
  `ensure_no_foreign_sessions` refuses on a foreign live session.
- A FAILED run never auto-reallocates: FAILED is terminal, resume
  refuses it, and the duplicate run_id guard blocks re-running the
  same id. A NEW run_id for the same task is a new operator decision
  (that is how the B re-run works) and is allowed.
- A verified local MP4 is never re-inferred: resume reuses it
  (`_resume_stopped_session` / `_resume_finish` validate-then-reuse).

## 3. P1-C design — deadline-bound end-to-end success rate (metric F)

### 3.1 Unit and deadline

- The unit is the production TASK (`production_task_id`), not the
  run. A task is the thing the user authorises with a budget and a
  deadline; a run is one video inside it.
- The ledger gains `deadline_utc`, recorded at task authorisation
  (`load_or_create_budget_ledger(..., deadline_utc=None)`; CLI
  `--task-deadline-utc` on `run`). Rules:
  - a NEW ledger (created with `--authorize-new-task`) records the
    passed deadline; without one the ledger records
    `deadline_utc: None` and the task is NOT in the F denominator
    (it was accepted without a deadline — the metric requires
    authorisation + budget + deadline).
  - an EXISTING ledger with a recorded deadline refuses a different
    passed deadline (mismatch → RuntimeError, like the
    prior-reserve check): deadlines never move silently.
  - existing ledgers (e.g. `h3-ab-20261111`, created before this
    field) have no deadline and stay OUT of the denominator, listed
    separately as `deadline_unknown_tasks`. No retroactive deadline.
- Task completion time: the latest `TASK_COMPLETED` STAGE timestamp
  across the task's runs, read from the audit logs (no new field).

### 3.2 Denominator and numerator

Denominator: tasks whose ledger records `authorized == True`,
`project_budget_cu`, and `deadline_utc`, AND (the deadline has passed
OR every started run has reached a terminal state). A task still
in-flight before its deadline is excluded and listed separately as
`in_flight_tasks`. A task past its deadline with a non-terminal run is
stuck/over-deadline: it is IN the denominator as a failure — stuck,
over-deadline, lost-artifact, unconfirmed-shutdown and
budget-violating tasks are never silently removed.

Numerator: tasks where EVERY started run meets all seven conditions,
and task completion ≤ `deadline_utc`:

1. GPU allocation success: the run manifest has a `session_id` and
   its audit log contains a `GPU_ALLOCATED` STAGE.
2. Model restore+load success: `MODELS_READY` in the audit stages.
3. Inference produced a spec-conforming MP4: `VIDEO_VALIDATED` in
   the audit stages, `artifacts.local_sha256` present, and
   `artifacts.local_frames == 274`.
4. MP4 independently verified in persistent storage: `DRIVE_VERIFIED`
   in the audit stages, `delivery_state == "DELIVERED"`, and
   `drive_file_id` recorded.
5. GPU session confirmed stopped: `GPU_STOP_CONFIRMED` in the audit
   stages and `session_state == "STOPPED"`.
6. CU within the authorised budget: the task ledger reports
   `max_observed_spent_cu <= project_budget_cu` (reuse the metric-E
   ledger check).
7. Final manifest + audit log complete: `task_state ==
   TASK_COMPLETED` and the audit log contains a `TASK_COMPLETED`
   STAGE event.

### 3.3 Statuses and reporting

Same thresholds as the §12 rates: denominator 0 → `UNKNOWN`;
0 < n < 30 → `INSUFFICIENT_SAMPLE`; n ≥ 30 → `MEASURED`.
`target_verified` is True only when MEASURED and the 95% Wilson lower
bound is ≥ 0.95 (same `wilson_interval` as §12). With zero failures
that needs about 73 completed tasks (60/60 reaches only 0.9398) —
a statistical threshold, NOT a plan to buy 73 GPU runs.

`compute_reliability` gains the sixth metric
`deadline_bound_e2e_success_rate` (same `_metric` shape, plus a
per-task verdict list: each qualified task with its verdict and the
conditions it failed, so a failure is diagnosable, not just counted).
The existing `target_95_verified` top-level field keeps its metric-B
meaning; metric F carries its own `target_verified` inside its dict.
`scripts/reliability_report.py` needs no change (it prints the whole
`compute_reliability` dict).

### 3.4 Full-chain fault injection (tests)

Extend `tests/test_fault_injection.py` (ChainHarness) and add
`tests/test_task_lock.py` / metric-F tests in
`tests/test_reliability.py`:

- restart mid-bootstrap (controller restart after `init_remote`,
  before worker launch → resume re-runs bootstrap idempotently, no
  second GPU, no re-inference);
- restart mid-inference, restart after MP4 durable (already exist —
  extend to assert no duplicate allocation);
- restart during remote upload (worker uploaded, controller restarted
  before recording → resume finds the evidence in the worker state);
- Colab reclaimed after generation before upload (session STOPPED, no
  local MP4, worker upload evidence in the manifest → resume verifies
  the Drive id server-side and completes without re-inference);
- partial upload success (upload sidecar present, session stopped,
  no local MP4, no verified Drive id → FAILED with
  `artifact_lost=True`, and the task counts in the F denominator as
  a failure);
- token expired before upload (worker refresh request → controller
  mints → upload proceeds);
- upload succeeded but response lost (uploader sidecar complete, no
  evidence recorded → resume reconciles via the Drive file query by
  name + run_id; `upload_one` reuses the matching file);
- duplicate recovery of the same run_id (resume twice → second resume
  returns already-completed, no second GPU);
- same-name-different-SHA in Drive (controller deliver refuses;
  worker side raises `DriveConflictError`);
- GPU stopped but Drive unverified (COMPLETED_AWAITING_ARCHIVE →
  archive-only resume);
- Drive verified but GPU state UNKNOWN (resume refuses, no second
  GPU);
- duplicate controller (task lock held → second run refuses without
  allocating);
- network timeout during poll (transient poll failures → run
  continues; unobservable at the wall time → fails with the deadline
  reason);
- unobservable worker → UNKNOWN blocks new allocation.

### 3.5 Production acceptance runbook

New `docs/runbooks/production-acceptance.md` (proposal only, never
auto-executed): the live production test plan — exact authorisations
needed, GPU whitelist, CU cap, max duration, shutdown checks,
artifact persistence checks, token-expiry handling, VM-restart
simulation at each phase, pass/fail criteria, rollback. CU / failure /
success reporting is the `reliability_report.py` JSON output (metric
F section + per-task verdicts).

## 4. What stays unchanged

- Story / prompt / image / resolution / steps / quality config
  (Issues #20/#21/#23 stay independent).
- CLI baseline 0.7.5, both patches, environment contract.
- The TASK_COMPLETED gate (DRIVE_VERIFIED + session STOPPED),
  the forward-only delivery axis, the append-only audit log.
- The watchdog / lease-registry / executor design (PR-3); the cron
  itself is still NOT deployed in this PR.
- Issue #4: no history change, no visibility change (separate report).

## 5. Files

- NEW `h3_task_lock.py`, `docs/p1b-p1c-design.md` (this file),
  `docs/runbooks/production-acceptance.md`,
  `tests/test_task_lock.py`.
- CHANGED `h3_budget.py` (ledger `deadline_utc`),
  `h3_run_manifest.py` (schema_version 3: `bootstrap`,
  `last_transient_error`; `update_run` gains both parameters),
  `h3_controller.py` (pre-registration, `_run_bootstrap`,
  `_resume_bootstrap`, task lock, `_inspect_live`,
  `wait_for_worker` degraded polling),
  `h3_session_watchdog.py` (`allocating` status,
  `confirm_session_active`, reconcile handling, stop path),
  `h3_gpu_lease_registry.py` (pre-registration),
  `h3_reliability.py` (metric F),
  `tests/test_fault_injection.py`, `tests/test_reliability.py`,
  `tests/test_budget.py`, `tests/test_run_manifest.py`,
  `tests/test_stop_state_hotfix.py` (allocating records),
  `docs/execution_flow.md`, `docs/module_inventory.md`,
  `SOURCE_MANIFEST.json`.
