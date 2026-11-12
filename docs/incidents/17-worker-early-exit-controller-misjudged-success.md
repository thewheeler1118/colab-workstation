# Incident: Worker exited early but controller misjudged success

STATUS: CONFIRMED_FIXED

## What happened

The controller used to treat a colab exec return code of 0 as success even when the remote worker had died early (2026-11-11 A run: worker exited ~146s in, no MP4). Fix (fail-closed): the controller requires the MUSE_H3_DONE_JSON marker printed only after assemble_video writes the final MP4; every exec result is persisted to the run dir. Verified live in the A run.

## Evidence

run_id: h3-20261111T080429Z-0c4fef (incident, ~146s early exit); h3-20261112T090027Z-8b07c6 (fix verified live)
  file_path: h3_controller.py
  line_or_function: h3_controller.check_worker_done_marker / persist_exec_result
  test_name: test_h3_production.ProductionTest.test_worker_done_marker_required; test_stop_attempted_after_worker_error
  source_report: h3_controller.check_worker_done_marker docstring
