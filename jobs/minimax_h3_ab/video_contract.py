"""Thin video-contract facade for the MiniMax-H3 A/B job.

Re-exports the production contract from the repo-root ``h3_contract`` module
so the job directory can refer to one stable place for the pinned numbers.
No values are duplicated here: every constant comes from ``h3_contract`` and
``tests/test_config_contract.py`` asserts the YAML configs agree with it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from h3_contract import (  # noqa: E402,F401
    ANIMATION_FRAMES,
    ASSETS,
    BASE,
    FPS,
    HEIGHT,
    MODEL_FILES,
    POSTER_DRIVE_ID,
    POSTER_FRAMES,
    POSTER_SHA256,
    SCENES,
    STEPS,
    STORY_DRIVE_ID,
    TOTAL_FRAMES,
    WIDTH,
    patch_api_workflow,
    prompt_for,
    sha256_file,
    validate_model_cache_manifest,
    validate_poster,
)

# Job-level aliases (same values, job vocabulary).
VIDEO_WIDTH = WIDTH
VIDEO_HEIGHT = HEIGHT
VIDEO_FPS = FPS
ANIMATION_FRAMES_COUNT = ANIMATION_FRAMES
POSTER_FRAMES_COUNT = POSTER_FRAMES
FINAL_FRAMES = TOTAL_FRAMES
FINAL_WIDTH = 1080
FINAL_HEIGHT = 1920
SEED_A = 42
SEED_B = 43
USE_TURBO = False
