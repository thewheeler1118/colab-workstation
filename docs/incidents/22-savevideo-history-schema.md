# Incident: SaveVideo history images/videos/animated structure differences

STATUS: CONFIRMED_FIXED

## What happened

Pinned ComfyUI 0.44.0 SaveVideo UI output is PreviewVideo {"images": [...], "animated": (True,)} — not {"videos": [...]}. The old finder only read 'videos'/'gifs', so a rendered video was missed. Fix: find_rendered_video also reads 'images' when 'animated' is truthy, and still requires the file to exist with a video suffix and size > 10000. Verified live in the A run; regression tests added in this repo.

## Evidence

run_id: h3-20261112T065629Z-061a20 (incident — inference product lost after find failure); h3-20261112T090027Z-8b07c6 (fix verified live)
  file_path: h3_remote_worker.py
  line_or_function: h3_remote_worker.find_rendered_video
  test_name: tests/test_savevideo_history.py (8 tests: PreviewVideo list/bool, legacy videos key, bare animated, images-without-animated, suffix/size gates, missing file, no-ffprobe-gap doc)
  source_report: h3_remote_worker.find_rendered_video comment
