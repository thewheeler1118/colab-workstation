# Incident: VM restart and orphan GPU session risk

STATUS: UNDER_INVESTIGATION

## What happened

The Muse VM restarted ~05:58, ~06:55, ~07:54 and ~09:48 UTC on 2026-11-12 (intervals ~57, ~59, ~114 min — no hourly rhythm). Each restart kills the local controller and watchdog, leaving the remote GPU an orphan that keeps billing until manually stopped. Mitigation exists (watchdog reconcile_on_startup blocks a new allocation while a managed session is active; orphans were stopped manually and verified with zero active sessions), but there is no automatic orphan recovery and no way to schedule around irregular restarts. Lesson recorded 2026-11-12: if a restart lands after inference but before download, check/download the orphan session BEFORE stopping it.

## Evidence

run_id: h3-20261112T065629Z-061a20; h3-20261112T073838Z-6d89ae; h3-20261112T093207Z-7ab515 (all orphaned by restarts; all manually stopped, 0 active sessions verified)
  file_path: h3_session_watchdog.py
  line_or_function: h3_session_watchdog.reconcile_on_startup / session_still_active / stop_session; h3_controller.reconcile_run_state
  test_name: test_h3_mount_repair (test_reconcile_finds_active_managed_session; test_session_cleanup_orphan_risk_recorded)
  source_report: docs/runbooks/vm-restart-orphan-recovery.md
