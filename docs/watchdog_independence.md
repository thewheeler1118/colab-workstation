# Watchdog independence — can anything stop a GPU if the Muse VM dies?

Status: **INDEPENDENT_SHUTDOWN_NOT_GUARANTEED**

Investigated 2026-11-13 (TASK-005 §3, Issue #25). No independent shutdown
executor is deployed or verified. Do not claim otherwise.

## The problem

The watchdog (`h3_session_watchdog.py`) is a local process on the Muse VM.
A VM restart kills it together with the controller. Observed 2026-11-12:
7 VM restarts; the B run's orphaned A100 session (`muse-h3-28874dbd25`)
kept billing until it was stopped manually. `start_new_session=True`
protects the remote worker process, not the local watchdog.

## What the Muse platform offers (investigated, not deployed)

| Mechanism | What it can do | Why it is not a guarantee |
|---|---|---|
| Cron (scheduled agent task) | Run `reconcile` + `stop` for overdue managed sessions every N minutes. Crons survive VM replacement (runtime-managed recovery). | Poll-based: checks can be missed between runs, schedules can run late. No continuous observation. A stop would arrive minutes after the controller dies, not immediately. |
| Hook (polling script waking an agent) | Detect an overdue managed session and wake an agent to stop it. | Same polling limits as crons. A detector runs on a cadence, not continuously. |
| Guest systemd service | Supervise a long-running watchdog process. | The guest boots a minimal `default.target`; a custom program's startup path after VM replacement is not established. A persistent unit file + `Restart` alone does not prove recovery. |

## Proposed backup stopper (design only — NOT deployed)

A cron, every 10 minutes, during an authorised production task only:

1. Run `python3 h3_session_watchdog.py reconcile --colab-auth adc`.
2. For any managed session past its deadline, run
   `python3 h3_session_watchdog.py stop --session <id>`.
3. Stop only sessions in the managed file; never create a GPU.
4. Report each action to the originating chat.

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
