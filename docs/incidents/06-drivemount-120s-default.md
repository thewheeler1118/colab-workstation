# Incident: Official drivemount remote default 120s timeout

STATUS: FIXED_BUT_NOT_LIVE_VERIFIED

## What happened

The vendored CLI's drivemount() injects 'drive.mount(path)' with no timeout_ms, so the remote runtime applies its 120s default measured from cell start INCLUDING the browser-authorization wait. Runs 6–7 failed exactly here: authorization finished at ~7–9 min, after the kernel timer raised ValueError: mount failed.

## Evidence

run_id: h3-20261111T015803Z-a0d192; h3-20261111T021132Z-22c8ee
  file_path: patches/google-colab-cli-0.7.5/drivemount_timeout.patch (colab_cli/commands/automation.py, drivemount)
  line_or_function: h3_controller.cli_drivemount_timeout_patch_present
  test_name: test_h3_mount_repair (test_auth_at_300s_fails_under_old_120s; test_auth_within_120s_mounts)
  source_report: docs/audits/v3_drivemount_timeout_patch.md
