# Incident: MP4 generated but not downloaded, lost on shutdown

STATUS: UNDER_INVESTIGATION

## What happened

In run h3-20261112T065629Z-061a20 the worker ran ~18 minutes of inference and the product was lost when the session stopped, because no MP4 had been downloaded before shutdown. The controller flow downloads before stopping (download_and_validate_variant, then stop_and_verify_session), but there is still no rescue path when the render is found only after the worker failed, and no RUN_ID-prefix fallback search in ComfyUI/output/. Tracked as P0 — see docs/issues/p0-persist-mp4-before-shutdown.md.

## Evidence

run_id: h3-20261112T065629Z-061a20 (18 min inference product lost)
  file_path: h3_controller.py
  line_or_function: h3_controller.download_and_validate_variant / stop_and_verify_session
  test_name: (none — acceptance is the P0 issue's rescue test)
  source_report: docs/issues/p0-persist-mp4-before-shutdown.md
