# Independent shutdown cron — deployment runbook (NOT deployed)

Status: **SAMPLE ONLY — do not enable without the user's approval.**
TASK-006 P0-C, Issue #46. The registry (`h3_gpu_lease_registry.py`)
and the executor (`h3_shutdown_executor.py`) are implemented and
offline-tested. No cron exists yet, no live Drive mirror has been
written, no GPU has been stopped by the executor.

## Why a cron

Per `~/docs/chat/scheduling-and-watching.md`: "The runtime manages
recovery for saved jobs, subagents, crons, artifacts and resumable
agent work", so a cron survives VM replacement by runtime design. A
hook cannot do this job (no connector credentials for the Drive mirror
read + Colab CLI), and a guest systemd service has no established
startup path after replacement (minimal `default.target`). Polling
limits apply: "Both mechanisms poll. Changes can be missed between
checks, and schedules can run late."

## Check period: 5 minutes

Worst-case lag past a lease's stop deadline is one period plus the
stop itself (~1–2 min for 3 CLI attempts + re-query). At the maximum
allowed rate (14 CU/h) one 5-minute period is ~1.2 CU of extra burn.
The period is a bound on lag, not a reliability proof: a missed or
late cron run extends the lag, and nothing here provides continuous
observation.

## Sample job body (one cron per production task)

Scope: exactly one `production_task_id` (the task the user authorised
a GPU for). The executor enforces the scope itself (TASK-007): a stop
pass without `--production-task-id` is refused outright (exit 2, no
stop attempted), and an audit pass with one reports only that task's
leases. The operator removes the cron when the task ends.

```
Every 5 minutes, during production task <TASK_ID> only:
1. cd ~/workspace/colab-workstation && git log --oneline -1 (confirm
   the deployed code is the merged main, not an old checkout).
2. python3 h3_shutdown_executor.py audit --production-task-id <TASK_ID>
   --registry model_cache/h3_gpu_leases.json
   (read-only; report overdue leases, skipped leases, registry source
   and degraded-read flag).
3. If the audit lists overdue leases for <TASK_ID>, run:
   python3 h3_shutdown_executor.py stop --confirm-stop --max-stops 3
   --production-task-id <TASK_ID>
   --registry model_cache/h3_gpu_leases.json
4. Report to the originating chat: stopped session ids (RUN_IDs),
   pending leases with reasons, refused leases, and the executor's
   report file under model_cache/h3_shutdown_executor/.
5. If the stop pass exits 1 (overdue leases remain pending), keep the
   cron and report it as an open incident — do NOT widen the scope,
   raise --max-stops, or stop anything by hand outside the registry.
Stop condition (remove the cron when any of these holds):
- the task's run completed (TASK_COMPLETED manifest) and no active
  leases remain, or
- the user cancels the task, or
- the user revokes this approval.
The cron never creates a GPU, never runs inference, never allocates.
```

## Deployment steps (each needs the approvals below)

1. Sync the production checkout to the merged PR-3 main (the executor
   must be the reviewed code, not a branch tip).
2. Run the executor once in audit mode against the live registry path
   (`python3 h3_shutdown_executor.py audit --production-task-id
   <TASK_ID> --registry model_cache/h3_gpu_leases.json`) and confirm it
   reads the Drive mirror (registry_source: mirror) for a lease created
   by a real allocation. (Needs a real allocation to have happened
   first; the first live mirror write happens at that allocation,
   inside `run_variant`.)
3. Create the cron with the job body above, scoped to the task id.
4. Kill-test (authorized separately): during a real production run,
   stop the controller + watchdog locally (or wait for a VM restart)
   and confirm the cron stops the orphaned session and reports the
   RUN_ID. Record the RUN_ID in Issue #46.
5. Replacement test (authorized separately): after a VM replacement,
   confirm the cron still runs and reads the old registry from the
   Drive mirror (registry_source: mirror, leases present).

## Approvals needed (none granted yet)

1. User approves creating the cron (a background job that stops cloud
   GPUs autonomously).
2. User approves the executor's stop authority: registry leases only,
   scoped to the approved production_task_id, owner-matched to this
   host's ADC, past their final deadline (or past the emergency cap
   when unfinalised, TASK-007), max 3 stops per pass.
3. User approves the live kill-test (step 4) and the replacement test
   (step 5) as separate live-GPU authorizations, each with its own
   task id and CU budget.
4. The Drive mirror folder `Muse-H3-Lease-Registry` is created on the
   user's Drive on first mirror write — no separate approval is needed
   for the folder itself beyond the task authorization, but the first
   live mirror write is reported in the task's audit events.

## What would change the status

`docs/watchdog_independence.md` stays
`INDEPENDENT_SHUTDOWN_NOT_GUARANTEED` until: the cron is deployed with
approval, AND a real orphan is stopped by it end-to-end with a RUN_ID,
AND recovery is verified after a VM replacement.
