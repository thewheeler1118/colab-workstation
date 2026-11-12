# VM restart / orphan GPU recovery runbook

## What happens

The Muse VM restarts irregularly (2026-11-12: ~05:58, ~06:55, ~07:54,
~09:48 UTC; intervals ~57, ~59, ~114 min — no hourly rhythm, so no safe
window can be scheduled). A restart kills the local controller and
watchdog; the remote GPU session becomes an orphan and keeps billing.

## Recovery order — download BEFORE stop

1. `./scripts/check_sessions.sh` — list live sessions (read-only).
2. For each orphan session, check whether the remote worker finished:
   the run's exec results live under logs/<run_id>/; the worker prints
   MUSE_H3_DONE_JSON only after the final MP4 is written, and
   h3_output_check.py (OUTPUT_CHECK_JSON) verifies the remote MP4.
3. If a render exists remotely, download it FIRST
   (h3_controller.download_and_validate_variant path) — stopping first
   destroys it. This is the lesson of run h3-20261112T065629Z-061a20,
   where ~18 minutes of inference product was lost on shutdown.
4. Stop the session (h3_session_watchdog.stop_session, 3 attempts),
   then `./scripts/verify_shutdown.sh` to prove sessions == [] and rate 0.
5. Record: run id, orphan session id, what was recovered (if anything),
   CU spent, and the restart time. If nothing could be recovered, say so —
   do not claim the render survived.

## Prevention (partial)

- h3_session_watchdog.reconcile_on_startup blocks a NEW allocation while a
  managed session is still active, so a restart cannot silently double-book.
- It does NOT auto-stop orphans and does NOT recover downloads by itself.
  Automatic orphan recovery is an open item (see incidents/12).
