# Incident: Git clone exit 128 with stderr lost

STATUS: FIXED_BUT_NOT_LIVE_VERIFIED

## What happened

On 2026-11-12 the remote worker died in ensure_comfy (git clone exit 128) BEFORE the old try block, so no WORKER_FAILED_JSON was printed and the stage had to be inferred from the persisted traceback. Fix: the whole worker body is now inside the try (set_stage + WORKER_FAILED_JSON on any failure), clone uses run_cmd_with_retry, and clone_comfyui_atomic exists. The clone-retry path itself has not been live-exercised since the fix; the A run took the cache-restore path instead.

## Evidence

run_id: (2026-11-12 ensure_comfy failure; run id not recorded in the code comment)
  file_path: h3_remote_worker.py
  line_or_function: h3_remote_worker.main (try covers the whole body); clone_comfyui_atomic / run_cmd_with_retry
  test_name: test_h3_production.ProductionTest.test_worker_failure_info
  source_report: h3_remote_worker.main comment (2026-11-12)
