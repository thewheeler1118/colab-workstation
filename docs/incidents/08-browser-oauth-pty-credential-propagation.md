# Incident: Browser OAuth authorization, PTY, credential propagation

STATUS: CONFIRMED_FIXED

## What happened

Mount requires an interactive TTY (preflight checks it), a browser authorization ceremony, and WebSocket credential propagation (drivefs_hook: dryrun then real POST to /tun/m/credentials-propagation, blocking on /dev/tty until the operator confirms). The 2026-11-12 runs completed this end to end with browser-approved authorization (no password).

## Evidence

run_id: h3-20261112T090027Z-8b07c6; h3-20261112T093207Z-7ab515
  file_path: h3_controller.py (preflight _pty check; mount_drive_interactive)
  line_or_function: h3_controller.mount_drive / mount_drive_interactive; h3_mount_timeline.parse_stdout_markers
  test_name: test_h3_mount_repair (test_websocket_block_interval_recorded; test_stdout_markers_never_store_uris)
  source_report: docs/audits/v3_drivemount_timeout_patch.md ('WebSocket credential handling' section)
