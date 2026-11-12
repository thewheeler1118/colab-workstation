import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from h3_budget import (
    PROJECT_BUDGET_CU, PER_SESSION_BUDGET_CU, CLEANUP_RESERVE_CU,
    MAX_SESSION_SECONDS, initial_session_wall_seconds,
    choose_gpu_candidate, allocation_command, is_capacity_failure,
    is_transient_transport_error,
    load_or_create_budget_ledger, reconcile_budget, remaining_project_cu,
    allowed_runtime_seconds, parse_gpu_probe_output, parse_usage_text,
    effective_hard_stop_cu, ledger_path_for_task,
)
from h3_contract import (
    POSTER_SHA256, POSTER_DRIVE_ID, STORY_DRIVE_ID, MODEL_FILES,
    ANIMATION_FRAMES, POSTER_FRAMES, TOTAL_FRAMES, prompt_for, patch_api_workflow,
    validate_poster, validate_model_cache_manifest,
)
from h3_controller import check_archive_metadata, cli_post_body_patch_present, probe_actual_gpu, run_cli, check_worker_done_marker, check_remote_output, persist_exec_result, download_remote_comfyui_log
from h3_video_qa import QA_FRAME_INDICES, inspect_video, validate_final_video, compare_poster_tail
from h3_remote_worker import validate_api_against_object_info, worker_failure_info, run_cmd, run_cmd_with_retry, post_json, find_rendered_video
from h3_session_watchdog import should_stop, load_managed_sessions, save_managed_sessions
import datetime


def _good_manifest():
    files = [{"path": p, "drive_file_id": "abc", "size": 100, "sha256": "a" * 64,
              "server_verified": True, "hf_revision": "54472221d27c695fca9621b0c3ed52f06c4af707"} for p in MODEL_FILES]
    return {"status": "READY", "verification_method": "drive_v3_server_api",
            "account_verified": True, "verified_at_utc": "2026-11-10T01:00:00Z", "files": files,
            "depot_root_id": "1YmCfFOOsoMvREhdyHusBDy95C1eI4KE_", "hf_revision": "54472221d27c695fca9621b0c3ed52f06c4af707"}


def _api_stub():
    names = {"136": "MiniMaxH3ReferenceToVideo", "137": "LoadImage", "139": "LoadImage",
             "124": "BasicScheduler", "129": "RandomNoise", "92": "SaveVideo",
             "127": "UNETLoader", "128": "CLIPLoader", "146": "PrimitiveBoolean"}
    return {k: {"class_type": v, "inputs": {}} for k, v in names.items()}


class ProductionTest(unittest.TestCase):
    def test_same_account_required(self):
        # verify_ready in drive_depot refuses when account_ok is False; the
        # controller gates on account_matches before any GPU work.
        import drive_depot
        with self.assertRaises(RuntimeError):
            drive_depot.verify_ready(None, False, ROOT / "model_cache" / "verified_manifest.json")

    def test_drive_ready_required_before_gpu(self):
        # A manifest that is not server-verified READY must be rejected.
        bad = _good_manifest()
        bad["status"] = "CACHE_READY"
        with self.assertRaises(ValueError):
            validate_model_cache_manifest(bad)

    def test_exact_four_model_ids(self):
        m = _good_manifest()
        self.assertEqual(len(m["files"]), 4)
        self.assertEqual({x["path"] for x in m["files"]}, MODEL_FILES)
        expected = {
            "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors": 20970379616,
            "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": 15687142551,
            "vae/minimax_h3_audio_vae_fp32.safetensors": 605254808,
            "vae/minimax_h3_video_vae_int8_convrot.safetensors": 2811065184,
        }
        self.assertEqual(sum(expected.values()), 40073842159)

    def test_poster_sha256_required(self):
        validate_poster(ROOT / "assets" / "original_poster.jpg")
        self.assertEqual(POSTER_SHA256, "bd6d45312f340234d0fa06af0de5284949b2196f280677dd44468a300075c7bb")
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "x.jpg"
            bad.write_bytes(b"not a poster")
            with self.assertRaises(ValueError):
                validate_poster(bad)

    def test_storyboard_drive_id_locked(self):
        self.assertEqual(STORY_DRIVE_ID, "1TTv-xyLsSLSvTL1jy9ewOFM4qXUKBwpP")
        self.assertEqual(POSTER_DRIVE_ID, "1Punsy8qZKQktUcYyrNDpG46XMu_SWAJO")

    def test_project_budget_30cu(self):
        self.assertEqual(PROJECT_BUDGET_CU, 30.0)

    def test_budget_baseline_not_reset(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.json"
            first = load_or_create_budget_ledger(p, 165.27, "h3-run-1")
            self.assertEqual(first["initial_balance_cu"], 165.27)
            self.assertEqual(first["hard_stop_balance_cu"], 135.27)
            second = load_or_create_budget_ledger(p, 160.00, "h3-run-2")
            self.assertEqual(second["initial_balance_cu"], 165.27)
            self.assertEqual(second["hard_stop_balance_cu"], 135.27)
            self.assertIn("h3-run-1", second["run_ids"])
            self.assertIn("h3-run-2", second["run_ids"])

    def test_budget_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.json"
            load_or_create_budget_ledger(p, 165.27, "h3-run-1")
            loaded = json.loads(p.read_text())
            self.assertEqual(loaded["initial_balance_cu"], 165.27)
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")

    def test_per_session_budget_25cu(self):
        self.assertEqual(PER_SESSION_BUDGET_CU, 25.0)

    def test_budget_cleanup_reserve(self):
        self.assertEqual(CLEANUP_RESERVE_CU, 0.75)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.json"
            ledger = load_or_create_budget_ledger(p, 165.27, "r1")
            # 25 CU session, 24.5 CU already spent in-session -> usable < reserve
            with self.assertRaises(RuntimeError):
                allowed_runtime_seconds(10.0, 165.27, ledger, session_spent_cu=24.5)

    def test_session_wall_time_is_dynamic_not_fixed_3600(self):
        # At the max allowed rate (14 CU/h) the initial wall time is the
        # 25 CU session cap minus reserve, not a fixed 3600 s.
        self.assertEqual(initial_session_wall_seconds(14.0), int((25.0 - 0.75) / 14.0 * 3600))
        self.assertEqual(MAX_SESSION_SECONDS, 14400)
        # At the observed A100 rate the budget-derived wall time exceeds
        # the old fixed 3600 s cap: the session must not be cut off by it.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.json"
            ledger = load_or_create_budget_ledger(p, 165.27, "r1")
            self.assertGreater(allowed_runtime_seconds(8.88, 163.21, ledger), 3600)

    def test_worker_done_marker_required(self):
        # 2026-11-11 run A: `colab exec` returned 0 while the remote
        # worker never produced the MP4, and the download failed with no
        # diagnosable worker output. The marker is now the gate.
        done = check_worker_done_marker(
            {"stdout": "RESTORE_CAPACITY {...}\nMUSE_H3_DONE_JSON:{\"frames\": 274, \"sha256\": \"ab\"}\n", "stderr": ""}, "A")
        self.assertEqual(done["frames"], 274)
        with self.assertRaises(RuntimeError) as cm:
            check_worker_done_marker({"stdout": "Traceback (most recent call last):\nRuntimeError: boom\n", "stderr": ""}, "A")
        self.assertIn("did not print MUSE_H3_DONE_JSON", str(cm.exception))
        self.assertIn("boom", str(cm.exception))
        with tempfile.TemporaryDirectory() as d:
            p = persist_exec_result(Path(d), "worker_A", {"returncode": 0, "stdout": "x", "stderr": ""})
            self.assertTrue(p.is_file())
            self.assertEqual(json.loads(p.read_text())["stdout"], "x")

    def test_g4_selected_first(self):
        self.assertEqual(choose_gpu_candidate(0)["key"], "G4")
        self.assertEqual(choose_gpu_candidate(0)["min_vram_mib"], 92160)

    def test_g4_capacity_retry_bounded(self):
        self.assertEqual(choose_gpu_candidate(1)["key"], "G4")
        self.assertEqual(choose_gpu_candidate(2)["key"], "A100_HIGH_MEM")
        with self.assertRaises(RuntimeError):
            choose_gpu_candidate(3)

    def test_a100_fallback_only_after_capacity_failure(self):
        self.assertTrue(is_capacity_failure("HTTP 503 Service Unavailable"))
        self.assertTrue(is_capacity_failure("status 412 precondition failed"))
        self.assertFalse(is_capacity_failure("allocation succeeded"))

    def test_a100_40gb_rejected(self):
        with self.assertRaises(RuntimeError):
            parse_gpu_probe_output("NVIDIA A100 40GB PCIe, 40536", "A100_HIGH_MEM")
        ok = parse_gpu_probe_output("NVIDIA A100-SXM4-80GB, 81920", "A100_HIGH_MEM")
        self.assertEqual(ok["memory_total_mib"], 81920)

    def test_unapproved_gpu_rejected(self):
        with self.assertRaises(RuntimeError):
            parse_gpu_probe_output("NVIDIA H100 80GB HBM3, 81559", "G4")
        with self.assertRaises(RuntimeError):
            allocation_command("muse-h3-abc123", "H100", allow_g4=True, allow_a100_highmem=True)
        with self.assertRaises(RuntimeError):
            allocation_command("muse-h3-abc123", "A100_HIGH_MEM", allow_g4=True, allow_a100_highmem=False)

    def test_403_does_not_trigger_gpu_fallback(self):
        self.assertFalse(is_capacity_failure("403 Forbidden: permission denied"))
        self.assertFalse(is_capacity_failure("401 Unauthorized"))

    def test_unknown_gpu_rate_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.json"
            ledger = load_or_create_budget_ledger(p, 165.27, "r1")
            with self.assertRaises(RuntimeError):
                allowed_runtime_seconds(0, 165.27, ledger)
            with self.assertRaises(RuntimeError):
                allowed_runtime_seconds(-1, 165.27, ledger)

    def test_interactive_tty_required(self):
        import h3_controller
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(RuntimeError):
                h3_controller.mount_drive_interactive("muse-h3-test123")

    def test_comfy_api_schema_required(self):
        api = patch_api_workflow(_api_stub(), "A")
        info = {
            "MiniMaxH3ReferenceToVideo": {"input": {"required": {"prompt": ["STRING"], "width": ["INT"], "height": ["INT"], "length": ["INT"], "ref_image_size": ["STRING"]}, "optional": {}}},
            "LoadImage": {"input": {"required": {"image": ["STRING"]}, "optional": {}}},
            "BasicScheduler": {"input": {"required": {"steps": ["INT"], "scheduler": ["STRING"]}, "optional": {}}},
            "RandomNoise": {"input": {"required": {"noise_seed": ["INT"]}, "optional": {}}},
            "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING"]}, "optional": {}}},
            "UNETLoader": {"input": {"required": {"unet_name": ["STRING"]}, "optional": {}}},
            "CLIPLoader": {"input": {"required": {"clip_name": ["STRING"]}, "optional": {}}},
            "PrimitiveBoolean": {"input": {"required": {"value": ["BOOLEAN"]}, "optional": {}}},
        }
        # The stub workflow has linked inputs for some nodes; schema check
        # must at least reject an unknown class type.
        with self.assertRaises(RuntimeError):
            validate_api_against_object_info({"1": {"class_type": "NoSuchNode", "inputs": {}}}, info)

    def test_autogrow_required_group_satisfied_by_dotted_key(self):
        # 2026-11-12: the live /object_info lists the autogrow group
        # ("values") while the prompt addresses entries as "values.a".
        # The old literal check rejected the official template's node 131.
        info = {
            "ComfyMathExpression": {"input": {"required": {"expression": ["STRING"], "values": ["AUTOGROW"]}, "optional": {}}},
            "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING"]}, "optional": {}}},
        }
        api = {
            "131": {"class_type": "ComfyMathExpression",
                    "inputs": {"values.a": ["132", 0],
                               "expression": "max(5, round(a * 24))"}},
            "92": {"class_type": "SaveVideo",
                   "inputs": {"video": ["130", 0], "filename_prefix": "video/x"}},
        }
        validate_api_against_object_info(api, info)  # must not raise

    def test_autogrow_optional_group_dotted_keys_allowed(self):
        # MiniMaxH3ReferenceToVideo: ref_images is an optional autogrow
        # group (pinned ComfyUI nodes_minimax_h3.py); the prompt uses
        # ref_images.ref_image_0 / ref_images.ref_image_1.
        info = {
            "MiniMaxH3ReferenceToVideo": {"input": {"required": {"clip": ["CLIP"], "prompt": ["STRING"], "width": ["INT"], "height": ["INT"], "length": ["INT"], "ref_image_size": ["STRING"]}, "optional": {"vae": ["VAE"], "audio_vae": ["VAE"], "ref_images": ["AUTOGROW"], "ref_videos": ["AUTOGROW"], "ref_video_audios": ["AUTOGROW"], "ref_audios": ["AUTOGROW"]}}},
            "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING"]}, "optional": {}}},
        }
        api = {
            "136": {"class_type": "MiniMaxH3ReferenceToVideo",
                    "inputs": {"clip": ["128", 0], "vae": ["119", 0],
                               "ref_images.ref_image_0": ["137", 0],
                               "ref_images.ref_image_1": ["139", 0],
                               "prompt": ["138", 0], "width": ["115", 0],
                               "height": ["115", 1], "length": ["131", 1],
                               "ref_image_size": "match"}},
            "92": {"class_type": "SaveVideo",
                   "inputs": {"video": ["130", 0], "filename_prefix": "video/x"}},
        }
        validate_api_against_object_info(api, info)  # must not raise

    def test_real_workflow_autogrow_nodes_pass(self):
        # The exact nodes that failed the 2026-11-12 run, taken from the
        # real workflow_api.json, validated against fixtures modeled on
        # the pinned ComfyUI schemas (nodes_math.py, nodes_minimax_h3.py).
        api = json.loads((ROOT / "workflow_api.json").read_text())
        info = {
            "ComfyMathExpression": {"input": {"required": {"expression": ["STRING"], "values": ["AUTOGROW"]}, "optional": {}}},
            "MiniMaxH3ReferenceToVideo": {"input": {"required": {"clip": ["CLIP"], "prompt": ["STRING"], "width": ["INT"], "height": ["INT"], "length": ["INT"], "ref_image_size": ["STRING"]}, "optional": {"vae": ["VAE"], "audio_vae": ["VAE"], "ref_images": ["AUTOGROW"], "ref_videos": ["AUTOGROW"], "ref_video_audios": ["AUTOGROW"], "ref_audios": ["AUTOGROW"]}}},
            "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING"]}, "optional": {"format": ["STRING"], "codec": ["STRING"]}}},
        }
        subset = {k: api[k] for k in ("131", "136", "92")}
        validate_api_against_object_info(subset, info)  # must not raise

    def test_autogrow_missing_group_still_rejected(self):
        info = {
            "ComfyMathExpression": {"input": {"required": {"expression": ["STRING"], "values": ["AUTOGROW"]}, "optional": {}}},
            "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING"]}, "optional": {}}},
        }
        api = {
            "131": {"class_type": "ComfyMathExpression",
                    "inputs": {"expression": "a + 1"}},
            "92": {"class_type": "SaveVideo",
                   "inputs": {"video": ["130", 0], "filename_prefix": "video/x"}},
        }
        with self.assertRaisesRegex(RuntimeError, "missing required input 'values'"):
            validate_api_against_object_info(api, info)

    def test_dotted_key_with_unknown_root_still_rejected(self):
        info = {
            "ComfyMathExpression": {"input": {"required": {"expression": ["STRING"], "values": ["AUTOGROW"]}, "optional": {}}},
            "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING"]}, "optional": {}}},
        }
        api = {
            "131": {"class_type": "ComfyMathExpression",
                    "inputs": {"expression": "a + 1", "values.a": ["132", 0],
                               "bogus.x": ["1", 0]}},
            "92": {"class_type": "SaveVideo",
                   "inputs": {"video": ["130", 0], "filename_prefix": "video/x"}},
        }
        with self.assertRaisesRegex(RuntimeError, "unknown input 'bogus.x'"):
            validate_api_against_object_info(api, info)

    def test_control_after_generate_modes_tolerated(self):
        # 2026-11-12: the frontend serializes the control-after-generate
        # setting as an extra input key on primitive nodes
        # (PrimitiveInt {"value": 20, "fixed": "fixed"}); the live
        # /object_info does not list it and the server ignores unknown
        # prompt keys, so the validator must tolerate the four enum values.
        info = {
            "PrimitiveInt": {"input": {"required": {"value": ["INT"]}, "optional": {}}},
            "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING"]}, "optional": {}}},
        }
        for mode in ("fixed", "increment", "decrement", "randomize"):
            api = {
                "143": {"class_type": "PrimitiveInt",
                        "inputs": {"value": 20, mode: mode}},
                "92": {"class_type": "SaveVideo",
                       "inputs": {"video": ["130", 0], "filename_prefix": "video/x"}},
            }
            validate_api_against_object_info(api, info)  # must not raise

    def test_real_workflow_primitive_and_savevideo_nodes_pass(self):
        # The exact nodes that failed the 2026-11-12 run (143/144) plus
        # SaveVideo (92, whose hidden 'codec' input the live schema
        # exposes in required/optional — node 92 passed live validation).
        api = json.loads((ROOT / "workflow_api.json").read_text())
        info = {
            "PrimitiveInt": {"input": {"required": {"value": ["INT"]}, "optional": {}}},
            "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING"]}, "optional": {"format": ["STRING"], "codec": ["STRING"]}}},
        }
        subset = {k: api[k] for k in ("143", "144", "92")}
        validate_api_against_object_info(subset, info)  # must not raise

    def test_plain_unknown_input_still_rejected(self):
        info = {
            "PrimitiveInt": {"input": {"required": {"value": ["INT"]}, "optional": {}}},
            "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING"]}, "optional": {}}},
        }
        api = {
            "143": {"class_type": "PrimitiveInt",
                    "inputs": {"value": 20, "bogus": 1}},
            "92": {"class_type": "SaveVideo",
                   "inputs": {"video": ["130", 0], "filename_prefix": "video/x"}},
        }
        with self.assertRaisesRegex(RuntimeError, "unknown input 'bogus'"):
            validate_api_against_object_info(api, info)

    def test_post_json_surfaces_server_error_body(self):
        # 2026-11-12: a bare "HTTP Error 400: Bad Request" from /prompt
        # hid the server's validation reason and cost a GPU session to
        # chase. The body must be in the raised error.
        import io as _io
        import urllib.error
        body = b'{"error": {"message": "lora_name not in list"}, "node_errors": {}}'
        err = urllib.error.HTTPError("http://x/prompt", 400, "Bad Request", {}, _io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400.*lora_name not in list"):
                post_json("http://x/prompt", {"prompt": {}})

    def test_download_remote_comfyui_log_best_effort(self):
        # The log download must never raise: a missing log must not mask
        # the worker failure it was meant to diagnose.
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            with mock.patch("h3_controller.run_cli", side_effect=RuntimeError("no session")):
                self.assertIsNone(download_remote_comfyui_log("muse-h3-dead", run_dir))
            log = run_dir / "comfyui_remote.log"
            log.write_text("server log")
            with mock.patch("h3_controller.run_cli", return_value={"returncode": 0}):
                self.assertEqual(download_remote_comfyui_log("muse-h3-x", run_dir), str(log))

    def test_find_rendered_video_accepts_preview_video_format(self):
        # 2026-11-12: the pinned ComfyUI 0.44.0 SaveVideo returns its UI
        # output as PreviewVideo -> {"images": [...], "animated": (True,)},
        # not {"videos": [...]}. The worker only read 'videos'/'gifs' and
        # discarded a fully rendered 18-minute inference. A real video
        # under animated 'images' must be found; a still image must not.
        with tempfile.TemporaryDirectory() as d:
            comfy = Path(d)
            out_dir = comfy / "output"
            out_dir.mkdir()
            video = out_dir / "muse_h3_A_00001_.mp4"
            video.write_bytes(b"x" * 20000)
            outputs = {"92": {"images": [{"filename": "muse_h3_A_00001_.mp4", "subfolder": "", "type": "output"}], "animated": (True,)}}
            self.assertEqual(find_rendered_video(comfy, outputs), video)
            # Still image under 'images' without animated must not match.
            png = out_dir / "still.png"
            png.write_bytes(b"y" * 20000)
            outputs2 = {"92": {"images": [{"filename": "still.png", "subfolder": "", "type": "output"}]}}
            with self.assertRaisesRegex(RuntimeError, "did not contain verifiable saved video"):
                find_rendered_video(comfy, outputs2)
            # Legacy 'videos' format still works.
            outputs3 = {"92": {"videos": [{"filename": "muse_h3_A_00001_.mp4", "subfolder": "", "type": "output"}]}}
            self.assertEqual(find_rendered_video(comfy, outputs3), video)
            # Failure message must name the history output keys for diagnosis.
            with self.assertRaisesRegex(RuntimeError, "History output keys"):
                find_rendered_video(comfy, {"92": {"images": []}})

    def test_ui_workflow_json_rejected(self):
        with self.assertRaisesRegex(ValueError, "API format"):
            patch_api_workflow({"nodes": []}, "A")

    def test_variant_a_prompt_two_dodges(self):
        s = prompt_for("A")
        self.assertIn("SECOND TIME", s)
        self.assertIn("second distinct elegant dodge", s)

    def test_variant_b_prompt_awkward_then_confident(self):
        s = prompt_for("B")
        self.assertIn("awkwardly", s)
        self.assertIn("recovers confidence", s)
        self.assertNotEqual(prompt_for("A"), prompt_for("B"))

    def test_226_source_frames_required(self):
        self.assertEqual(ANIMATION_FRAMES, 226)
        self.assertEqual(ANIMATION_FRAMES % 17, 5)

    def test_274_final_frames_required(self):
        self.assertEqual(TOTAL_FRAMES, 274)
        self.assertEqual(ANIMATION_FRAMES + POSTER_FRAMES, 274)

    def test_poster_48_frames(self):
        self.assertEqual(POSTER_FRAMES, 48)

    def test_drive_output_parent_verified(self):
        self.assertTrue(check_archive_metadata({"parents": ["folder-1"], "trashed": False}, "folder-1"))
        self.assertFalse(check_archive_metadata({"parents": ["other"], "trashed": False}, "folder-1"))
        self.assertFalse(check_archive_metadata({"parents": ["folder-1"], "trashed": True}, "folder-1"))

    def test_failed_video_not_reported_as_success(self):
        # A 10-frame test video must fail final validation, never pass as 274.
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            mp4 = Path(d) / "short.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=24:duration=0.5",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mp4)], check=True, timeout=60)
            info = inspect_video(mp4)
            self.assertNotEqual(info["frame_count"], 274)
            self.assertFalse(info["technical_pass"])
            with self.assertRaises(RuntimeError):
                validate_final_video(mp4)

    def test_stop_attempted_after_worker_error(self):
        import h3_controller
        calls = []

        def fake_run_cli(cmd, timeout=90, check=True):
            calls.append(cmd)
            if cmd and cmd[0] == "sessions":
                return {"args": cmd, "returncode": 0, "stdout": "muse-h3-abc123\n", "stderr": ""}
            if cmd and cmd[0] == "stop":
                calls.append(["_stop_called"])
                return {"args": cmd, "returncode": 0, "stdout": "", "stderr": ""}
            return {"args": cmd, "returncode": 0, "stdout": "", "stderr": ""}

        # stop_and_verify_session consults inspect(), which calls run_cli.
        # Simulate: first sessions call lists the session, stop succeeds,
        # second sessions call is empty.
        states = {"n": 0}

        def fake_inspect():
            states["n"] += 1
            if states["n"] == 1:
                return {"cu_balance": 160.0, "rate": 0.0, "sessions": [{"name": "muse-h3-abc123", "gpu": "G4"}]}
            return {"cu_balance": 160.0, "rate": 0.0, "sessions": []}

        with mock.patch.object(h3_controller, "inspect", fake_inspect), \
             mock.patch.object(h3_controller, "run_cli", fake_run_cli), \
             mock.patch.object(h3_controller, "record", lambda data: None):
            result = h3_controller.stop_and_verify_session("muse-h3-abc123")
            self.assertTrue(result["stopped_confirmed"])
            self.assertIn(["stop", "-s", "muse-h3-abc123"], calls)

    def test_late_gpu_allocation_reconciled(self):
        # Watchdog reconcile must report a still-active managed session.
        import h3_session_watchdog as wd
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sessions.json"
            wd.save_managed_sessions([{"session_id": "muse-h3-abc123", "gpu": "G4", "run_id": "r1",
                                       "started_at_utc": "2026-11-11T00:00:00+00:00",
                                       "deadline_utc": "2026-11-11T02:00:00+00:00", "status": "active"}], path=p)
            with mock.patch.object(wd, "session_still_active", return_value=True):
                rec = wd.reconcile_on_startup(path=p)
                self.assertIn("muse-h3-abc123", rec["managed_active_sessions"])

    def test_ledger_rejects_60cu_cap(self):
        # The 30->60 CU amendment of 2026-11-11 08:25 was a misunderstanding
        # (user correction 09:12). A ledger carrying a 60 CU project cap
        # must fail closed, never be silently accepted.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.json"
            led = load_or_create_budget_ledger(p, 165.27, "run-1")
            led["project_budget_cu"] = 60.0
            led["hard_stop_balance_cu"] = 105.27
            p.write_text(json.dumps(led))
            with self.assertRaises(RuntimeError):
                load_or_create_budget_ledger(p, 165.27, "run-2")

    def test_prior_unbilled_reserve_sets_effective_hard_stop(self):
        # Old task's unbilled cost is reserved: the effective hard stop is
        # the task hard stop PLUS the reserve, so late postings of the
        # previous task cannot push the account below the floor.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.json"
            led = load_or_create_budget_ledger(p, 162.77, "run-1", prior_unbilled_reserve_cu=0.14)
            self.assertEqual(led["hard_stop_balance_cu"], 132.77)
            self.assertEqual(led["prior_task_unbilled_reserve_cu"], 0.14)
            self.assertEqual(led["effective_hard_stop_balance_cu"], 132.91)
            self.assertEqual(effective_hard_stop_cu(led), 132.91)
            # A balance between the plain hard stop and the effective one
            # must already raise: 132.85 < 132.91.
            with self.assertRaises(RuntimeError):
                reconcile_budget(led, 132.85, p)
            # Mismatched reserve on a later load also fails closed.
            with self.assertRaises(RuntimeError):
                load_or_create_budget_ledger(p, 162.77, "run-2", prior_unbilled_reserve_cu=0.50)

    def test_per_task_ledger_path(self):
        self.assertEqual(str(ledger_path_for_task(Path("/x"), "h3-ab-20261111")),
                         "/x/model_cache/h3_production_budget_h3-ab-20261111.json")
        with self.assertRaises(ValueError):
            ledger_path_for_task(Path("/x"), "../evil")

    def test_remote_output_check_gate(self):
        ok = {"stdout": "OUTPUT_CHECK_JSON:" + json.dumps(
            {"ok": True, "exists": True, "size_bytes": 123456, "frames": 274, "fps": 24,
             "width": 1080, "height": 1920, "codec": "h264"}) + "\n", "stderr": ""}
        self.assertEqual(check_remote_output(ok, "A")["frames"], 274)
        bad_frames = {"stdout": "OUTPUT_CHECK_JSON:" + json.dumps(
            {"ok": False, "exists": True, "frames": 100, "reason": "ffprobe_mismatch"}) + "\n", "stderr": ""}
        with self.assertRaises(RuntimeError) as cm:
            check_remote_output(bad_frames, "A")
        self.assertIn("Remote output check failed", str(cm.exception))
        missing = {"stdout": "WORKER_STAGE done\n", "stderr": ""}
        with self.assertRaises(RuntimeError) as cm:
            check_remote_output(missing, "A")
        self.assertIn("no OUTPUT_CHECK_JSON", str(cm.exception))

    def test_worker_failure_info(self):
        info = worker_failure_info("restore_weights", RuntimeError("disk full"))
        self.assertEqual(info["stage"], "restore_weights")
        self.assertEqual(info["error_type"], "RuntimeError")
        self.assertIn("disk full", info["error"])

    def test_run_cmd_surfaces_failure(self):
        # 2026-11-12: git clone failed with exit 128 and git's fatal text
        # was lost (inherited stdio was not captured). run_cmd must raise
        # CalledProcessError carrying the exit code.
        import subprocess
        import sys
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            run_cmd([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(cm.exception.returncode, 3)

    def test_run_cmd_with_retry_retries_once_then_raises(self):
        import subprocess
        import sys
        import time
        start = time.monotonic()
        with self.assertRaises(subprocess.CalledProcessError):
            run_cmd_with_retry([sys.executable, "-c", "raise SystemExit(7)"],
                               attempts=2, delay_sec=0)
        self.assertLess(time.monotonic() - start, 30)

    def test_run_cmd_with_retry_succeeds_on_second_attempt(self):
        import sys
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "n.txt"
            lines = [
                "import pathlib, sys",
                "p = pathlib.Path(sys.argv[1])",
                "n = int(p.read_text()) if p.exists() else 0",
                "p.write_text(str(n + 1))",
                "raise SystemExit(1 if n == 0 else 0)",
            ]
            script = "\n".join(lines) + "\n"
            run_cmd_with_retry([sys.executable, "-c", script, str(marker)],
                               attempts=2, delay_sec=0)
            self.assertEqual(marker.read_text(), "2")

    def test_ab_share_one_project_budget(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.json"
            a = load_or_create_budget_ledger(p, 165.27, "h3-run-A")
            reconcile_budget(a, 160.27, p)
            self.assertEqual(a["max_observed_spent_cu"], 5.0)
            b = load_or_create_budget_ledger(p, 160.27, "h3-run-B")
            self.assertEqual(b["initial_balance_cu"], 165.27)
            self.assertEqual(b["max_observed_spent_cu"], 5.0)
            self.assertEqual(remaining_project_cu(b), 25.0)

    def test_parse_usage_text(self):
        u = parse_usage_text("Current balance: 165.27 compute units\nUsage rate: 8.88/hr\nActive assignments: 0\n")
        self.assertEqual(u["balance"], 165.27)
        self.assertEqual(u["rate"], 8.88)

    def test_watchdog_should_stop_on_deadline(self):
        rec = {"deadline_utc": "2026-11-11T00:00:00+00:00"}
        now = datetime.datetime(2026, 11, 11, 1, 0, 0, tzinfo=datetime.timezone.utc)
        stop, _ = should_stop(rec, 160.0, None, now=now)
        self.assertTrue(stop)

    # 2026-11-11 proxy investigation: empty POSTs get 411, responses are
    # intermittently truncated at 1025 bytes. Transport errors retry the
    # SAME GPU only; they never justify an A100 fallback.
    def test_transient_transport_error_classification(self):
        for msg in (
            "ProxyError: HTTPSConnectionPool Max retries exceeded (Caused by ProxyError('Unable to connect to proxy', RemoteDisconnected('Remote end closed connection without response')))",
            "ChunkedEncodingError: Connection broken: IncompleteRead(1025 bytes read, 539 more expected)",
            "Failed to issue request POST /tun/m/assign: Length Required",
            "SSLEOFError: EOF occurred in violation of protocol",
        ):
            self.assertTrue(is_transient_transport_error(msg), msg)
        for msg in (
            "Failed to issue request POST /tun/m/assign: Service Unavailable",
            "401 Unauthorized",
            "403 Forbidden",
            "GPU allocation failed (G4): CLI failed",
        ):
            self.assertFalse(is_transient_transport_error(msg), msg)

    def test_capacity_failure_includes_too_many_assignments(self):
        self.assertTrue(is_capacity_failure("TooManyAssignmentsError: Failed to issue request POST /tun/m/assign: Service Unavailable"))
        self.assertTrue(is_capacity_failure("status 503"))
        self.assertFalse(is_capacity_failure("status 411 Length Required"))
        self.assertFalse(is_capacity_failure("403 Forbidden"))

    def test_cli_post_body_patch_present(self):
        # Fail-closed: if the venv is rebuilt without the 2026-11-11
        # POST-body workaround, empty POSTs return 411 again.
        # Verified offline against the vendored pristine 0.7.5 fixture:
        # pristine reports absent, patch-applied reports present.
        fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "pristine-colab-cli-0.7.5" / "colab_cli"
        patch_dir = Path(__file__).resolve().parents[1] / "patches" / "google-colab-cli-0.7.5"
        import shutil
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "site-packages"
            shutil.copytree(fixtures, tree / "colab_cli")
            self.assertFalse(cli_post_body_patch_present([str(tree)]))
            for name in ("proxy_post_body.patch", "drivemount_timeout.patch"):
                subprocess.run(["git", "apply", str(patch_dir / name)],
                               cwd=str(tree), check=True, capture_output=True, text=True)
            self.assertTrue(cli_post_body_patch_present([str(tree)]))

    # 2026-11-11: A100-HM allocated fine, but the first probe exec failed
    # with jupyter-kernel-client's 10s REQUEST_TIMEOUT. run_cli must raise
    # that timeout for every CLI subprocess.
    def test_run_cli_sets_request_timeout_env(self):
        import h3_controller as hc
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_run(*a, **k):
            captured["env"] = k.get("env")
            return FakeProc()

        with mock.patch.object(hc.subprocess, "run", fake_run), \
                mock.patch.object(hc, "resolve_colab_executable",
                                  return_value="/usr/bin/colab"):
            run_cli(["usage"], timeout=10, check=False)
        self.assertEqual(captured["env"]["REQUEST_TIMEOUT"], "120")
        self.assertEqual(captured["env"]["COLAB_CLI_REQUEST_TIMEOUT"], "180")

    def test_gpu_probe_retries_on_read_timeout(self):
        import h3_controller as hc
        good = {"stdout": 'H3_GPU_EVIDENCE: {"name": "NVIDIA A100 80GB PCIe", "memory_total_mib": 81920}', "stderr": ""}
        calls = {"n": 0}

        def fake_exec(sess, path, timeout, env=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("CLI failed: ReadTimeout: Read timed out. (read timeout=10)")
            return good

        with mock.patch.object(hc, "exec_script", fake_exec), mock.patch.object(hc.time, "sleep", lambda s: None), mock.patch.object(hc, "record", lambda data: None):
            info = probe_actual_gpu("muse-h3-test1234", "A100_HIGH_MEM")
        self.assertEqual(info["memory_total_mib"], 81920)
        self.assertEqual(calls["n"], 2)

    def test_inspect_ignores_cli_update_notice(self):
        # 2026-11-11: the CLI prints '[colab] A new version of Colab CLI
        # is available: 0.8.0' to stdout before the balance line. The loose
        # reference parser read 'available: 0.8.0' as balance 0.8 and the
        # hard-stop gate killed a healthy G4 run. inspect() must return
        # the real balance.
        import h3_controller as hc
        notice = (
            "[colab] A new version of Colab CLI is available: 0.8.0 (current: 0.7.5)\n"
            "[colab] Run 'colab update' to update.\n"
            "[colab] To silence this check, set \"enable_update_check\": false in ~/.config/colab-cli/settings.json\n"
        )
        usage_text = notice + "\nCurrent balance: 165.06 compute units\nUsage rate: 8.88/hr\nActive assignments: 1\n"
        sessions_text = notice + "\n[muse-h3-e76302ae4b] gpu-a100-hm-euw4a0-wnsvovi8whof | Hardware: A100 | Shape: High-RAM | Variant: GPU\n"

        def fake_run_cli(cmd, timeout=90, check=True):
            if cmd == ["usage"]:
                return {"args": cmd, "returncode": 0, "stdout": usage_text, "stderr": ""}
            return {"args": cmd, "returncode": 0, "stdout": sessions_text, "stderr": ""}

        with mock.patch.object(hc, "run_cli", fake_run_cli):
            info = hc.inspect()
        self.assertEqual(info["cu_balance"], 165.06)
        self.assertEqual(info["rate"], 8.88)

    def test_loose_parser_no_longer_reads_version_as_balance(self):
        import sys
        sys.path.insert(0, str(ROOT / "reference_v2"))
        from colab_manager import parse_usage_output
        notice = "[colab] A new version of Colab CLI is available: 0.8.0 (current: 0.7.5)\n"
        info = parse_usage_output(notice + "\nCurrent balance: 165.06 compute units\nUsage rate: 8.88/hr\nActive assignments: 1\n")
        self.assertEqual(info.cu_balance, 165.06)

    def test_mount_drive_timeout_is_900s(self):
        # 2026-11-11: 300s was too short for the user to see the Drive
        # authorization URL and click it; the mount timed out and the
        # allocated A100-HM session was released unused.
        import h3_controller as hc
        captured = {}

        def fake_mount(session, timeout, **kwargs):
            captured["timeout"] = timeout

        with mock.patch.object(hc, "mount_drive_interactive", fake_mount):
            hc.mount_drive("muse-h3-test1234", deadline_sec=3600)
        self.assertEqual(captured["timeout"], 900)

    def test_gpu_probe_does_not_retry_wrong_gpu(self):
        import h3_controller as hc
        bad = {"stdout": 'H3_GPU_EVIDENCE: {"name": "NVIDIA T4", "memory_total_mib": 15360}', "stderr": ""}
        calls = {"n": 0}

        def fake_exec(sess, path, timeout, env=None):
            calls["n"] += 1
            return bad

        with mock.patch.object(hc, "exec_script", fake_exec), mock.patch.object(hc.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError):
                probe_actual_gpu("muse-h3-test1234", "A100_HIGH_MEM")
        self.assertEqual(calls["n"], 1)

    def test_compare_poster_tail_parses_ffmpeg8_average_token(self):
        # 2026-11-12: compare_poster_tail looked for "psnr_avg:" but
        # ffmpeg 8.x logs the summary as "PSNR y:.. u:.. v:.. average:..",
        # so a real 31.38 dB tail was reported as psnr None / fail.
        import h3_video_qa as qa
        import subprocess

        def fake_run(args, **kwargs):
            if "psnr" in args:
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout="",
                    stderr="[Parsed_psnr_0 @ 0x1] PSNR y:29.493579 u:31.059007 v:35.570918 average:31.377571 min:31.377571 max:31.377571\n")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as d:
            mp4 = Path(d) / "A.mp4"
            mp4.write_bytes(b"fake")
            poster = Path(d) / "poster.jpg"
            poster.write_bytes(b"fake")
            with mock.patch.object(qa, "inspect_video", return_value={"frame_count": 274}), \
                 mock.patch.object(qa.subprocess, "run", side_effect=fake_run):
                result = compare_poster_tail(mp4, poster)
        self.assertEqual(result["psnr_db"], 31.377571)
        self.assertTrue(result["pass"])


if __name__ == "__main__":
    unittest.main()
