# Incident: Drive output not yet server-verified risk

STATUS: UNDER_INVESTIGATION

## What happened

Downloading the MP4 to the Muse VM is not the same as a verified Drive archive: archive_variant_to_drive + check_archive_metadata exist to confirm the Drive copy, but the A run's Drive archive verification status is not recorded in the evidence available to this migration (local A.mp4 verified: 7,453,808 B, sha256 907a23dc…; QA passed). Do not claim Drive delivery is verified until check_archive_metadata has passed on a recorded run.

## Evidence

run_id: h3-20261112T090027Z-8b07c6 (local download + QA verified; Drive archive verification unrecorded)
  file_path: h3_controller.py
  line_or_function: h3_controller.archive_variant_to_drive / check_archive_metadata
  test_name: (none on file)
  source_report: docs/issues/p0-persist-mp4-before-shutdown.md
