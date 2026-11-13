# Watchdog independence — can anything stop a GPU if the Muse VM dies?

Status: **INDEPENDENT_SHUTDOWN_NOT_GUARANTEED**

Investigated 2026-11-13 (TASK-005 §3, Issue #25); registry + backup
executor designed and implemented offline 2026-11-13 (TASK-006 P0-C,
Issue #46, PR pending). No independent shutdown executor is deployed
or verified. Do not claim otherwise.

## The problem

The watchdog (`h3_session_watchdog.py`) is a local process on the Muse VM.
A VM restart kills it together with the controller. Observed 2026-11-12:
7 VM restarts; the B run's orphaned A100 session (`muse-h3-28874dbd25`)
kept billing until it was stopped manually. `start_new_session=True`
protects the remote worker process, not the local watchdog.

## What exists now (TASK-006 P0-C, offline only)

- **Lease registry** (`h3_gpu_lease_registry.py`): every allocated
  session gets a lease (session id, run id, task id, owner hash, GPU
  model, start time, latest stop deadline, budget cap, last confirmed
  state, last audit time, version, executor claim). The local file
  `model_cache/h3_gpu_leases.json` is the write-through cache; a Drive
  mirror (`Muse-H3-Lease-Registry/h3_gpu_leases.json`) is written
  best-effort with optimistic-concurrency merge. A lease is only
  stoppable once its deadline is final (set by the post-probe deadline
  update). The controller registers the lease right after allocation
  and fails closed if the local write fails (the outer finally stops
  the just-created session).
- **Backup executor** (`h3_shutdown_executor.py`): audit-only by
  default; `stop --confirm-stop` stops registry leases past their
  final deadline, owner-matched against this host's ADC, one claim at
  a time, confirmed ONLY by the three-state query returning STOPPED.
  Failed stops stay pending with attempts + retry time. It never
  touches a session that is not in the registry.

## What the Muse platform offers (investigated, not deployed)

| Mechanism | What it can do | Why it is not a guarantee |
|---|---|---|
| Cron (scheduled agent task) | Run `audit` + `stop --confirm-stop` for overdue leases every 5 minutes. Crons survive VM replacement (runtime-managed recovery, per `~/docs/chat/scheduling-and-watching.md`). | Poll-based: checks can be missed between runs, schedules can run late. No continuous observation. A stop would arrive minutes after the controller dies, not immediately. |
| Hook (polling script waking an agent) | Detect an overdue managed session and wake an agent to stop it. | Same polling limits as crons, and hooks have no connector credentials — a detector that must read the Drive mirror and run the Colab CLI has no supported credential path in a hook. |
| Guest systemd service | Supervise a long-running watchdog process. | The guest boots a minimal `default.target`; a custom program's startup path after VM replacement is not established. A persistent unit file + `Restart` alone does not prove recovery. |

## Proposed backup stopper (design + code ready — NOT deployed)

A cron, every 5 minutes, during an authorised production task only
(see `docs/runbooks/independent-shutdown-cron.md` for the sample job
body, the deployment steps, and the approvals needed):

1. Run `python3 h3_shutdown_executor.py audit --registry <path>`.
2. If overdue leases exist, run `python3 h3_shutdown_executor.py stop --confirm-stop --max-stops 3`.
3. Stop only leases in the registry, owner-matched, past their final deadline; never create a GPU.
4. Report each action to the originating chat; exit 1 when overdue leases remain pending.

Deploying it needs the user's approval: it is a new background job that
stops GPU sessions autonomously. After deployment it needs a real
orphan-stop observed end-to-end (with a RUN_ID) before the status above
can change.

## Hard rule

Never substitute a remote `kill -9` (or killing the remote Python
process) for a real GPU session stop. Only the official stop API/CLI
releases the Colab session and stops billing. Killing the process leaves
the session allocated.

## What would change the marking

- The cron (or hook) backup stopper deployed with user approval, AND
- a real orphan session stopped by it end-to-end with a RUN_ID, AND
- the recovery verified after a VM replacement.

Until then: **INDEPENDENT_SHUTDOWN_NOT_GUARANTEED**.
