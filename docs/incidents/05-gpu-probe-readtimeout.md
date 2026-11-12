# Incident: GPU probe ReadTimeout

STATUS: FIXED_BUT_NOT_LIVE_VERIFIED

## What happened

The remote GPU probe exec can hit a read timeout (timeout=10) on a cold-start kernel. probe_actual_gpu retries the same session with bounded retries and records a gpu_probe_timeout_retry event. No dedicated offline test for the retry path was found in the suite, and no run id on file isolates a ReadTimeout recovery.

## Evidence

run_id: none on file
  file_path: h3_controller.py
  line_or_function: h3_controller.probe_actual_gpu (bounded retries, exec_script timeout=180)
  test_name: none found for the retry path
  source_report: (code only — do not upgrade to a live-verified fix)
