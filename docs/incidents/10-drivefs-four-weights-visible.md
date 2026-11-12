# Incident: DriveFS four-weight visibility verification

STATUS: CONFIRMED_FIXED

## What happened

After mount, before any pip/model load, the remote visibility probe confirms all four weights are visible under /content/drive/MyDrive/Muse-H3-Model-Depot with exact byte sizes; full SHA-256 is rechecked when copying each weight to NVMe.

## Evidence

run_id: h3-20261112T090027Z-8b07c6 (4 models visible); h3-20261112T093207Z-7ab515
  file_path: h3_drive_probe.py
  line_or_function: (module main) prints MUSE_DRIVE_MOUNT_MODELS_VISIBLE; h3_controller preflight _drive_ready
  test_name: tests/test_h3_production.py (manifest / drive-ready gates)
  source_report: docs/v3_readme.md
