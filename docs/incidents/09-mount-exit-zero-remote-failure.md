# Incident: Drive mount CLI exit 0 but remote mount actually failed

STATUS: FIXED_BUT_NOT_LIVE_VERIFIED

## What happened

A zero exit code from the mount subprocess is not proof the remote mount succeeded. The controller now requires a mount signature (h3_mount_signature_probe), checks the mount signature of exec results, and builds a mount timeline that flags a missing drive_auth_success event. Mock tests cover the failure shape; no live run on file has yet caught a false-zero mount with this detector.

## Evidence

run_id: none on file for a caught false-zero
  file_path: h3_mount_signature_probe.py; h3_mount_timeline.py; h3_mount_acceptance.py
  line_or_function: h3_controller.check_mount_signature; h3_mount_timeline.build_mount_timeline
  test_name: test_h3_mount_repair (test_mount_fails_after_authorization_despite_exit_zero; test_mount_failure_after_auth_raises_with_timeline)
  source_report: docs/audits/v3_drivemount_timeout_patch.md (residual risk 2)
