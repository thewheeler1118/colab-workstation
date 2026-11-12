"""Config-contract tests: YAML configs must equal the production constants.

The single source of truth is the code (h3_contract.py, h3_budget.py).
config/*.yaml are checked against it so a config edit that drifts from the
code fails loudly instead of silently changing a production run.
"""
import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from h3_budget import (  # noqa: E402
    CLEANUP_RESERVE_CU,
    DEFAULT_PRODUCTION_TASK_ID,
    GPU_ATTEMPT_PLAN,
    GPU_CHOICES,
    MAX_SESSION_SECONDS,
    PER_SESSION_BUDGET_CU,
    PROJECT_BUDGET_CU,
)
from h3_contract import (  # noqa: E402
    ANIMATION_FRAMES,
    FPS,
    HEIGHT,
    MODEL_FILES,
    POSTER_DRIVE_ID,
    POSTER_FRAMES,
    POSTER_SHA256,
    STEPS,
    TOTAL_FRAMES,
    WIDTH,
    validate_model_cache_manifest,
)


def _yaml(name):
    return yaml.safe_load((ROOT / "config" / name).read_text())


class ConfigContractTest(unittest.TestCase):
    def test_video_profile_matches_contract(self):
        v = _yaml("video_profiles.yaml")
        self.assertEqual((v["width"], v["height"], v["fps"]), (WIDTH, HEIGHT, FPS))
        self.assertEqual(v["animation_frames"], ANIMATION_FRAMES)
        self.assertEqual(v["poster_frames"], POSTER_FRAMES)
        self.assertEqual(v["total_frames"], TOTAL_FRAMES)
        self.assertEqual(v["steps"], STEPS)
        self.assertEqual((v["seed_a"], v["seed_b"]), (42, 43))
        self.assertIs(v["use_turbo"], False)
        self.assertEqual(v["poster_sha256"], POSTER_SHA256)
        self.assertEqual(v["poster_drive_id"], POSTER_DRIVE_ID)

    def test_gpu_policy_matches_budget_module(self):
        g = _yaml("gpu_policy.yaml")
        self.assertEqual(g["attempt_plan"], list(GPU_ATTEMPT_PLAN))
        self.assertEqual(g["max_attempts"], len(GPU_ATTEMPT_PLAN))
        for key, choice in GPU_CHOICES.items():
            cfg = g["choices"][key]
            self.assertEqual(cfg["min_vram_mib"], choice["min_vram_mib"])
            self.assertEqual(cfg["max_rate_cu_h"], choice["max_rate_cu_h"])
            self.assertEqual(list(cfg["name_tokens"]), list(choice["name_tokens"]))

    def test_budget_policy_matches_budget_module(self):
        b = _yaml("budget_policy.yaml")
        self.assertEqual(b["project_budget_cu"], PROJECT_BUDGET_CU)
        self.assertEqual(b["per_session_budget_cu"], PER_SESSION_BUDGET_CU)
        self.assertEqual(b["cleanup_reserve_cu"], CLEANUP_RESERVE_CU)
        self.assertEqual(b["max_session_seconds"], MAX_SESSION_SECONDS)
        self.assertEqual(b["default_production_task_id"], DEFAULT_PRODUCTION_TASK_ID)
        # The user-corrected rule: exactly 30.0 per task, never cumulative.
        self.assertEqual(PROJECT_BUDGET_CU, 30.0)

    def test_model_manifest_example_validates(self):
        m = json.loads((ROOT / "config" / "model_manifest.example.json").read_text())
        validate_model_cache_manifest(m)
        self.assertEqual({f["path"] for f in m["files"]}, MODEL_FILES)
        self.assertEqual(sum(f["size"] for f in m["files"]), 40073842159)
        self.assertEqual(m["hf_revision"], "54472221d27c695fca9621b0c3ed52f06c4af707")


if __name__ == "__main__":
    unittest.main()
