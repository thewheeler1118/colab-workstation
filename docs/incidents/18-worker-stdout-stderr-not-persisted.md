# Incident: Worker stdout/stderr not durably persisted

STATUS: CONFIRMED_FIXED

## What happened

2026-11-11 lesson: run A failed at the final download and the worker's own progress output had been discarded, so the failure stage could not be identified after the session stopped. Fix: persist_exec_result writes every remote exec result (stdout/stderr/returncode) to the run dir, with _2/_3 suffixes instead of overwriting on repeats.

## Evidence

run_id: h3-20261111T080429Z-0c4fef (incident); h3-20261112T090027Z-8b07c6 (exec results persisted)
  file_path: h3_controller.py
  line_or_function: h3_controller.persist_exec_result
  test_name: test_h3_production (persist_exec_result coverage)
  source_report: h3_controller.persist_exec_result docstring
