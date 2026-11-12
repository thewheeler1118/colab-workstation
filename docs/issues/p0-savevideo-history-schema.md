# Issue draft: P0: SaveVideo history schema and artifact recovery

STATUS: PROPOSED_ONLY — the RUN_ID-prefix fallback and the ffprobe gate do not exist in find_rendered_video today (see the no-ffprobe-gap test).

## Context

h3_remote_worker.py — find_rendered_video (line ~548). Historical run: h3-20261112T065629Z-061a20 (find failed after ~18 min inference; product lost); fix verified in h3-20261112T090027Z-8b07c6.

## Reproduction

1. Run the pinned ComfyUI 0.44.0 SaveVideo path and capture the real history JSON for a rendered video.
2. Confirm find_rendered_video locates it via the PreviewVideo {images, animated} shape (regression tests exist: tests/test_savevideo_history.py).
3. Attempt the gap cases: a video present under ComfyUI/output/ with a RUN_ID prefix but absent from history; a >10000-byte non-video blob with an .mp4 suffix.

## Acceptance

Acceptance: (a) all tests in tests/test_savevideo_history.py pass; (b) a new test proves a RUN_ID-prefixed file under ComfyUI/output/ is found by the fallback search (search limited to that directory and video suffixes — never another task's video); (c) a test proves a non-decodable .mp4 blob is rejected by an ffprobe gate before download. Expected impact: renders are never silently missed again; the 061a20 loss class is closed.

## Process

Issue → feature branch → tests → pull request → review → merge. Never commit directly to main.
