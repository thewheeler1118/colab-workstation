# Issue draft: P0: SaveVideo history schema and artifact recovery

STATUS: PROPOSED_ONLY.

TASK-002 NOTE (2026-11-12, offline-verified only): `find_rendered_video`
now recognises `images` when `animated` is true, `videos`, and `gifs`;
entries without a filename never succeed; RUN_ID+variant binding,
ffprobe gate (exactly 226 frames, 736x1312, ~24 fps), ambiguity refusal,
and a bounded `rescue_rendered_video` fallback exist
(`h3_remote_worker.py`, branch `fix/video-artifact-recovery`, commit
`fa11196`; tests `tests/test_savevideo_history.py`, 17 tests, real
ffmpeg fixtures). No live GPU run has exercised this path yet — a real
run id is still required before CONFIRMED_FIXED.

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
