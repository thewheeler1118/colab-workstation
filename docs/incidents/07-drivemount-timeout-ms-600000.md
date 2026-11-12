# Incident: Remote timeout_ms=600000 fix

STATUS: FIXED_BUT_NOT_LIVE_VERIFIED

## What happened

Fix: injected code now passes timeout_ms=600000 (10 min) and the local automation ceiling is 660s (60s margin for propagation + reply). Mounts succeeded live with the patched CLI in the 2026-11-12 runs — BUT authorization completed in 85.1s (A) and 56.3s (B), inside the old 120s window, so the 600s path itself was not exercised. The fix is present and mock-verified, not live stress-verified.

## Evidence

run_id: h3-20261112T090027Z-8b07c6 (mount 95.4s, auth 85.1s); h3-20261112T093207Z-7ab515 (mount 70.7s, auth 56.3s)
  file_path: patches/google-colab-cli-0.7.5/drivemount_timeout.patch
  line_or_function: h3_controller.cli_drivemount_timeout_patch_present; h3_mount_signature_probe.py
  test_name: test_h3_mount_repair (test_auth_at_300s_mounts_with_600s_patch; test_drivemount_timeout_patch_present; test_injected_code_contains_timeout)
  source_report: docs/audits/v3_drivemount_timeout_patch.md
