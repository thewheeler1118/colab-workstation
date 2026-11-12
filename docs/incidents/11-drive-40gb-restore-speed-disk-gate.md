# Incident: Drive 40GB restore speed and disk-capacity gate

STATUS: CONFIRMED_FIXED

## What happened

Restoring 40,073,842,159 bytes from DriveFS to NVMe is time- and CU-expensive. estimate_restore / check_restore_capacity enforce a disk-space gate and a cost gate (projected throughput and CU budget) before the copy starts; the A run restored 40GB at 51.26 MB/s.

## Evidence

run_id: h3-20261112T090027Z-8b07c6 (40GB restore 51.26 MB/s)
  file_path: h3_remote_worker.py
  line_or_function: h3_remote_worker.estimate_restore / check_restore_capacity / project_restore_throughput
  test_name: test_h3_mount_repair (test_estimate_restore_disk_ok; test_estimate_restore_disk_short; test_check_restore_capacity_disk_gate; test_project_restore_throughput_blocks_slow_copy)
  source_report: docs/v3_progress.md (mount acceptance 44.9 MB/s on first 256MB)
