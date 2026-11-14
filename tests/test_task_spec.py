"""Tests for the generic TaskSpec engine (TASK-007, architecture correction).

Covers: spec schema validation (fail-closed), the shots[] renderer,
the A/B fixture adapter and its patch equivalence with the old
hardcoded path, reference-image materialisation, controller spec
resolution, worker spec resolution, and the spec-driven assembly /
video-QA / remote-output-check plumbing (with real ffmpeg fixtures).
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from h3_contract import (  # noqa: E402
    POSTER_SHA256,
    patch_api_workflow,
    patch_api_workflow_for_spec,
    prompt_for,
)
from h3_task_spec import (  # noqa: E402
    RefImage,
    TaskSpec,
    load_task_spec,
    task_spec_for_variant,
    validate_ref_image_file,
)
import h3_controller as hc  # noqa: E402
from h3_remote_worker import _spec_from_task  # noqa: E402

SHA_A = "a" * 64
SHA_B = "b" * 64


def _valid_spec_dict(**over):
    d = {
        "task_id": "h3-demo-task-1",
        "output_prefix": "demo",
        "ref_image_1": {"local_path": "/tmp/ref1.jpg", "sha256": SHA_A},
        "prompt": "A test storyboard prompt.",
        "seed": 7,
    }
    d.update(over)
    return d


def _api_fixture():
    names = {'136': 'MiniMaxH3ReferenceToVideo', '137': 'LoadImage',
             '139': 'LoadImage', '124': 'BasicScheduler', '129': 'RandomNoise',
             '92': 'SaveVideo', '127': 'UNETLoader', '128': 'CLIPLoader',
             '146': 'PrimitiveBoolean'}
    return {k: {'class_type': v, 'inputs': {}} for k, v in names.items()}


class SpecValidationTest(unittest.TestCase):
    def test_minimal_prompt_spec_gets_defaults(self):
        spec = TaskSpec.from_dict(_valid_spec_dict())
        self.assertEqual(spec.width, 736)
        self.assertEqual(spec.height, 1312)
        self.assertEqual(spec.fps, 24)
        self.assertEqual(spec.animation_frames, 226)
        self.assertEqual(spec.tail_card_frames, 0)
        self.assertEqual(spec.steps, 25)
        self.assertEqual(spec.output_width, 1080)
        self.assertEqual(spec.output_height, 1920)
        self.assertIsNone(spec.ref_image_2)
        self.assertIs(spec.effective_ref_image_2, spec.ref_image_1)
        self.assertEqual(spec.total_frames, 226)
        self.assertAlmostEqual(spec.duration_sec, 226 / 24)
        self.assertEqual(spec.origin, "task-spec")

    def test_dual_ref_images(self):
        spec = TaskSpec.from_dict(_valid_spec_dict(
            ref_image_2={"drive_file_id": "fid-2", "sha256": SHA_B}))
        self.assertEqual(spec.effective_ref_image_2.drive_file_id, "fid-2")
        self.assertEqual(spec.ref_image_2.sha256, SHA_B)

    def test_round_trip(self):
        spec = TaskSpec.from_dict(_valid_spec_dict(
            ref_image_2={"local_path": "/tmp/ref2.jpg", "sha256": SHA_B},
            tail_card_frames=48, animation_frames=260, width=768, height=1344))
        self.assertEqual(TaskSpec.from_dict(spec.to_dict()), spec)

    def test_shots_render_deterministically(self):
        spec = TaskSpec.from_dict(_valid_spec_dict(shots=[
            {"label": "FIRST", "text": "calm hero stands.", "start_s": 0, "end_s": 2},
            {"label": "FINAL", "text": "hero raises the ball.", "start_s": 7.5, "end_s": 9.4},
        ], prompt=None))
        self.assertEqual(
            spec.prompt,
            "[0-2s] FIRST: calm hero stands.\n[7.5-9.4s] FINAL: hero raises the ball.")

    def test_shots_without_times_join_plain(self):
        spec = TaskSpec.from_dict(_valid_spec_dict(shots=[
            {"text": "one."}, {"text": "two."}], prompt=None))
        self.assertEqual(spec.prompt, "one.\ntwo.")

    def test_rejects(self):
        bad = [
            ("unknown key", {"surprise": 1}),
            ("missing task_id", {"task_id": None}),
            ("bad task_id chars", {"task_id": "a/b"}),
            ("empty output_prefix", {"output_prefix": ""}),
            ("missing seed", {"seed": None}),
            ("bool seed", {"seed": True}),
            ("width not 16-multiple", {"width": 735}),
            ("height not 16-multiple", {"height": 100}),
            ("animation frames violate H3 rule", {"animation_frames": 227}),
            ("negative tail", {"tail_card_frames": -1}),
            ("steps zero", {"steps": 0}),
            ("fps zero", {"fps": 0}),
            ("empty prompt", {"prompt": "   "}),
            ("prompt and shots", {"shots": [{"text": "x"}]}),
            ("neither prompt nor shots", {"prompt": None}),
            ("empty shots", {"shots": []}),
            ("shots overlapping", {"prompt": None, "shots": [
                {"text": "a", "start_s": 0, "end_s": 3},
                {"text": "b", "start_s": 2, "end_s": 4}]}),
            ("shots mixed timed", {"prompt": None, "shots": [
                {"text": "a", "start_s": 0, "end_s": 1}, {"text": "b"}]}),
            ("ref missing sha256", {"ref_image_1": {"local_path": "/tmp/x.jpg"}}),
            ("ref both sources", {"ref_image_1": {
                "local_path": "/tmp/x.jpg", "drive_file_id": "f", "sha256": SHA_A}}),
            ("ref neither source", {"ref_image_1": {"sha256": SHA_A}}),
            ("ref bad sha256", {"ref_image_1": {"local_path": "/tmp/x.jpg", "sha256": "zz"}}),
            ("ref unknown key", {"ref_image_1": {
                "local_path": "/tmp/x.jpg", "sha256": SHA_A, "extra": 1}}),
            ("odd output width", {"output_width": 1081}),
        ]
        for label, over in bad:
            with self.subTest(label):
                d = _valid_spec_dict(**over)
                # "prompt and shots" / "neither" cases need the other key removed/kept explicitly.
                if label == "prompt and shots":
                    pass  # d already has prompt + shots
                if label == "neither prompt nor shots":
                    d.pop("prompt", None)
                with self.assertRaises(ValueError, msg=label):
                    TaskSpec.from_dict(d)

    def test_load_task_spec_file_errors(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "spec.json"
            p.write_text(json.dumps(_valid_spec_dict()))
            self.assertEqual(load_task_spec(p).task_id, "h3-demo-task-1")
            p.write_text("{not json")
            with self.assertRaises(ValueError):
                load_task_spec(p)
            with self.assertRaises(ValueError):
                load_task_spec(Path(td) / "missing.json")


class VariantAdapterTest(unittest.TestCase):
    def test_example_spec_is_generic_default(self):
        """config/task_spec.example.json is the generic default (TASK-008):
        the A-fixture task id / prefix / poster / seed, but tail 0 —
        a 226-frame pure generated video, decoupled from the historical
        274-frame A/B shape. The 48-frame tail lives in the with_tail
        example instead."""
        loaded = load_task_spec(ROOT / "config" / "task_spec.example.json")
        adapter = task_spec_for_variant("A", "h3-ab-20261111",
                                        "assets/original_poster.jpg")
        self.assertEqual(loaded.task_id, adapter.task_id)
        self.assertEqual(loaded.output_prefix, adapter.output_prefix)
        self.assertEqual(loaded.ref_image_1.sha256, adapter.ref_image_1.sha256)
        self.assertEqual(loaded.seed, adapter.seed)
        self.assertEqual(loaded.tail_card_frames, 0)
        self.assertEqual(loaded.total_frames, 226)
        self.assertEqual(loaded.origin, "task-spec")

    def test_with_tail_example_matches_a_fixture(self):
        """config/task_spec.with_tail.example.json is the A fixture,
        written as a plain task-spec file: same task id, prefix, poster,
        seed 42 and 48 tail frames (274 total). Only the origin differs
        (task-spec vs the in-code variant adapter)."""
        loaded = load_task_spec(ROOT / "config" / "task_spec.with_tail.example.json")
        adapter = task_spec_for_variant("A", "h3-ab-20261111",
                                        "assets/original_poster.jpg")
        self.assertEqual(loaded.to_dict(), adapter.to_dict())
        self.assertEqual(loaded.origin, "task-spec")
    def test_adapter_a_reproduces_frozen_values(self):
        spec = task_spec_for_variant("A", "h3-ab-20261111", "/x/poster.jpg")
        self.assertEqual(spec.task_id, "h3-ab-20261111")
        self.assertEqual(spec.output_prefix, "A")
        self.assertEqual(spec.prompt, prompt_for("A"))
        self.assertEqual(spec.seed, 42)
        self.assertEqual(spec.tail_card_frames, 48)
        self.assertEqual(spec.animation_frames, 226)
        self.assertEqual(spec.ref_image_1.local_path, "/x/poster.jpg")
        self.assertEqual(spec.ref_image_1.sha256, POSTER_SHA256)
        self.assertIsNone(spec.ref_image_2)
        self.assertEqual(spec.origin, "variant-adapter")

    def test_adapter_b_seed_43_and_explicit_seed_wins(self):
        self.assertEqual(task_spec_for_variant("B", "t", "/x/p.jpg").seed, 43)
        self.assertEqual(task_spec_for_variant("A", "t", "/x/p.jpg", seed=99).seed, 99)

    def test_adapter_rejects_bad_variant(self):
        with self.assertRaises(ValueError):
            task_spec_for_variant("C", "t", "/x/p.jpg")

    def test_patch_equivalence_with_legacy_path(self):
        # The adapter must produce byte-identical patched workflows to
        # the old hardcoded patch_api_workflow, for both variants, on
        # the fixture graph and on the real workflow_api.json.
        for variant in ("A", "B"):
            with self.subTest(variant):
                # The legacy entry point defaults seed=42 for both
                # variants (the controller passed 43 for B explicitly);
                # compare with that same seed.
                spec = task_spec_for_variant(variant, "t", "poster.jpg", seed=42)
                self.assertEqual(
                    patch_api_workflow(_api_fixture(), variant),
                    patch_api_workflow_for_spec(_api_fixture(), spec, "poster.jpg", "poster.jpg"))
                self.assertEqual(
                    patch_api_workflow(_api_fixture(), variant, run_id="r1"),
                    patch_api_workflow_for_spec(_api_fixture(), spec, "poster.jpg", "poster.jpg", run_id="r1"))
        real = json.loads((ROOT / "workflow_api.json").read_text())
        for variant in ("A", "B"):
            with self.subTest("real-" + variant):
                spec = task_spec_for_variant(variant, "t", "poster.jpg", seed=42)
                self.assertEqual(
                    patch_api_workflow(real, variant),
                    patch_api_workflow_for_spec(real, spec, "poster.jpg", "poster.jpg"))

    def test_spec_patch_applies_custom_values(self):
        spec = TaskSpec.from_dict(_valid_spec_dict(
            width=768, height=1344, animation_frames=260, tail_card_frames=0,
            steps=20, seed=7, fps=24, prompt="custom prompt",
            ref_image_2={"local_path": "/tmp/ref2.jpg", "sha256": SHA_B}))
        api = _api_fixture()
        api["138"] = {"class_type": "PrimitiveStringMultiline", "inputs": {}}
        api["143"] = {"class_type": "PrimitiveInt", "inputs": {}}
        api["132"] = {"class_type": "PrimitiveFloat", "inputs": {}}
        p = patch_api_workflow_for_spec(api, spec, "r1.jpg", "r2.jpg", run_id="run9")
        node = p["136"]["inputs"]
        self.assertEqual((node["width"], node["height"], node["length"]), (768, 1344, 260))
        self.assertEqual(node["prompt"], "custom prompt")
        self.assertEqual(p["124"]["inputs"]["steps"], 20)
        self.assertEqual(p["129"]["inputs"]["noise_seed"], 7)
        self.assertEqual(p["137"]["inputs"]["image"], "r1.jpg")
        self.assertEqual(p["139"]["inputs"]["image"], "r2.jpg")
        self.assertEqual(p["92"]["inputs"]["filename_prefix"], "muse_h3_demo_run9")
        self.assertEqual(p["132"]["inputs"]["value"], 260 / 24)
        self.assertEqual(p["138"]["inputs"]["value"], "custom prompt")
        self.assertEqual(p["143"]["inputs"]["value"], 20)

    def test_spec_patch_rejects_bad_frame_rule(self):
        spec = TaskSpec.from_dict(_valid_spec_dict())
        broken = replace(spec, animation_frames=227)  # bypasses from_dict validation
        with self.assertRaises(ValueError):
            patch_api_workflow_for_spec(_api_fixture(), broken)


class RefImageFileTest(unittest.TestCase):
    def test_validate_ref_image_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ref.jpg"
            p.write_bytes(b"ref-bytes")
            digest = hashlib.sha256(b"ref-bytes").hexdigest()
            self.assertEqual(validate_ref_image_file(p, digest), digest)
            with self.assertRaises(ValueError):
                validate_ref_image_file(p, "0" * 64)
            with self.assertRaises(ValueError):
                validate_ref_image_file(Path(td) / "missing.jpg", digest)


class FakeDrive:
    """Minimal Drive service stub: get_media returns bytes, get returns metadata."""

    def __init__(self, media=b"", meta=None):
        self._media = media
        self._meta = meta or {"id": "fid-1", "name": "ref.jpg", "trashed": False, "size": "4"}

    def files(self):
        return self

    def get_media(self, fileId=None):
        return _Executable(self._media)

    def get(self, fileId=None, fields=None):
        return _Executable(dict(self._meta))


class _Executable:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class MaterialiseTest(unittest.TestCase):
    def test_local_refs_returned_as_is(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ref1.jpg"
            p.write_bytes(b"one")
            digest = hashlib.sha256(b"one").hexdigest()
            spec = TaskSpec.from_dict(_valid_spec_dict(
                ref_image_1={"local_path": str(p), "sha256": digest}))
            out = hc._materialize_ref_images(spec, Path(td))
            self.assertEqual(out["ref_image_1"], p)
            self.assertEqual(out["ref_image_2"], p)  # reuse

    def test_drive_ref_downloaded_and_verified(self):
        spec = TaskSpec.from_dict(_valid_spec_dict(
            ref_image_1={"drive_file_id": "fid-1", "sha256": hashlib.sha256(b"dl").hexdigest()}))
        with tempfile.TemporaryDirectory() as td:
            out = hc._materialize_ref_images(spec, Path(td), drive=FakeDrive(media=b"dl"))
            data = out["ref_image_1"].read_bytes()
            self.assertEqual(data, b"dl")

    def test_drive_ref_bad_bytes_fail_closed(self):
        spec = TaskSpec.from_dict(_valid_spec_dict(
            ref_image_1={"drive_file_id": "fid-1", "sha256": "0" * 64}))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                hc._materialize_ref_images(spec, Path(td), drive=FakeDrive(media=b"dl"))

    def test_check_refs_local_and_drive(self):
        spec = TaskSpec.from_dict(_valid_spec_dict(
            ref_image_1={"drive_file_id": "fid-1", "sha256": SHA_A}))
        summary = hc._check_task_spec_refs(spec, drive=FakeDrive())
        self.assertIn("ref.jpg", summary)
        with self.assertRaises(RuntimeError):
            hc._check_task_spec_refs(
                spec, drive=FakeDrive(meta={"id": "fid-1", "trashed": True}))


class ResolveSpecTest(unittest.TestCase):
    def test_task_spec_file_loads_and_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "spec.json"
            p.write_text(json.dumps(_valid_spec_dict()))
            args = SimpleNamespace(task_spec=str(p), variant=None, production_task_id="h3-demo-task-1")
            self.assertEqual(hc.resolve_run_spec(args).task_id, "h3-demo-task-1")
            args2 = SimpleNamespace(task_spec=str(p), variant=None, production_task_id="other-task")
            with self.assertRaises(ValueError):
                hc.resolve_run_spec(args2)

    def test_task_spec_and_variant_conflict(self):
        args = SimpleNamespace(task_spec="/tmp/x.json", variant="A", production_task_id="t")
        with self.assertRaises(ValueError):
            hc.resolve_run_spec(args)

    def test_variant_adapter_and_neither(self):
        args = SimpleNamespace(task_spec=None, variant="B", production_task_id="h3-ab-20261111")
        spec = hc.resolve_run_spec(args)
        self.assertEqual(spec.output_prefix, "B")
        self.assertEqual(spec.origin, "variant-adapter")
        with self.assertRaises(ValueError):
            hc.resolve_run_spec(SimpleNamespace(task_spec=None, variant=None,
                                                production_task_id="t"))

    def test_spec_from_manifest(self):
        spec = TaskSpec.from_dict(_valid_spec_dict())
        m = {"variant": "demo", "production_task_id": "h3-demo-task-1",
             "artifacts": {"task_spec": spec.to_dict()}}
        self.assertEqual(hc._spec_from_manifest(m), spec)
        old = {"variant": "A", "production_task_id": "h3-ab-20261111", "artifacts": {}}
        self.assertEqual(hc._spec_from_manifest(old).output_prefix, "A")
        self.assertIsNone(hc._spec_from_manifest(
            {"variant": "Z", "production_task_id": "t", "artifacts": {}}))
        corrupt = {"variant": "demo", "production_task_id": "t",
                   "artifacts": {"task_spec": {"task_id": "bad id!"}}}
        self.assertIsNone(hc._spec_from_manifest(corrupt))


class WorkerSpecTest(unittest.TestCase):
    def test_spec_from_task(self):
        spec = TaskSpec.from_dict(_valid_spec_dict())
        got = _spec_from_task({"task_spec": spec.to_dict(), "variant": "demo"})
        self.assertEqual(got, spec)

    def test_legacy_variant_falls_back_to_adapter(self):
        got = _spec_from_task({"variant": "A"})
        self.assertEqual(got.output_prefix, "A")
        self.assertEqual(got.seed, 42)

    def test_neither_raises(self):
        with self.assertRaises(ValueError):
            _spec_from_task({"variant": "Z"})


class ValidateWorkflowSpecTest(unittest.TestCase):
    def test_validate_comfy_workflow_with_spec(self):
        spec = TaskSpec.from_dict(_valid_spec_dict())
        api = hc.validate_comfy_workflow(ROOT / "workflow_api.json", spec)
        node = api["136"]["inputs"]
        self.assertEqual((node["width"], node["height"], node["length"]), (736, 1312, 226))
        self.assertEqual(node["prompt"], spec.prompt)

    def test_validate_comfy_workflow_legacy_string(self):
        api = hc.validate_comfy_workflow(ROOT / "workflow_api.json", "A")
        self.assertEqual(api["136"]["inputs"]["length"], 226)

    def test_validate_local_with_spec(self):
        from h3_contract import MODEL_FILES

        spec = TaskSpec.from_dict(_valid_spec_dict())
        with tempfile.TemporaryDirectory() as td:
            manifest = {
                "status": "READY", "verification_method": "drive_v3_server_api",
                "account_verified": True, "verified_at_utc": "2026-11-10T01:00:00Z",
                "files": [{"path": p, "drive_file_id": "abc", "size": 100,
                           "sha256": "a" * 64, "server_verified": True,
                           "hf_revision": "fixed"} for p in MODEL_FILES]}
            mp = Path(td) / "manifest.json"
            mp.write_text(json.dumps(manifest))
            out = hc.validate_local(ROOT / "workflow_api.json", mp, spec)
            self.assertEqual(out["status"], "READY")


class PlanActionTest(unittest.TestCase):
    def test_plan_with_spec(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "spec.json"
            p.write_text(json.dumps(_valid_spec_dict()))
            args = SimpleNamespace(action="plan", task_spec=str(p), variant=None,
                                   production_task_id="h3-demo-task-1")
            buf = io.StringIO()
            with redirect_stdout(buf):
                hc.test_or_run(args)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["task_id"], "h3-demo-task-1")
            self.assertEqual(out["total_frames"], 226)
            self.assertTrue(out["no_gpu_created"])


def _fake_ready_manifest():
    from h3_contract import MODEL_FILES

    return {
        "status": "READY", "verification_method": "drive_v3_server_api",
        "account_verified": True, "verified_at_utc": "2026-11-10T01:00:00Z",
        "files": [{"path": p, "drive_file_id": "abc", "size": 100,
                   "sha256": "a" * 64, "server_verified": True,
                   "hf_revision": "fixed"} for p in MODEL_FILES]}


class PreflightWithSpecTest(unittest.TestCase):
    """TASK-007 Task 3: the spec gates are wired into the real
    preflight() entry point — schema, ref images and the workflow
    patch are all proven at 0 CU, and a bad ref fails preflight
    instead of reaching a GPU."""

    def _args(self, td, spec_path):
        return SimpleNamespace(
            run_id="test-run", task_spec=str(spec_path), variant=None,
            production_task_id="h3-preflight-test", drive_source="download",
            cache_manifest=str(Path(td) / "manifest.json"),
            workflow_api=str(ROOT / "workflow_api.json"),
            comfy_ref=hc.COMFY_REF_DEFAULT,
            gpu_policy="G4_THEN_A100_HIGHMEM",
            project_budget_cu=30.0, per_session_budget_cu=25.0)

    def _run_preflight(self, args, root_dir):
        import subprocess

        patches = [
            mock.patch.object(hc, "ROOT", root_dir),
            mock.patch.object(hc, "audit_user_adc_file", return_value="adc ok"),
            mock.patch("drive_depot.drive_service", return_value=mock.MagicMock()),
            mock.patch("drive_depot.account_matches", return_value=True),
            mock.patch("drive_depot.verify_ready", return_value={"status": "READY"}),
            mock.patch.object(hc, "verify_drive_download_preflight",
                              return_value={"token_id": "tok-1234", "probe_file_id": "fid-0"}),
            mock.patch.object(hc, "verify_drive_file_token_archive",
                              return_value={"archive_folder": "Muse-H3-Video-Outputs",
                                            "token_id": "tok-5678"}),
            mock.patch.object(hc, "run_cli",
                              return_value={"returncode": 0, "stdout": "0.7.5", "stderr": ""}),
            mock.patch.object(hc, "cli_post_body_patch_present", return_value=True),
            mock.patch.object(hc, "cli_drivemount_timeout_patch_present", return_value=True),
            mock.patch("h3_environment.verify_environment",
                       return_value={"ok": True, "cli_version": "0.7.5"}),
            mock.patch.object(subprocess, "run",
                              return_value=SimpleNamespace(stdout="TTY_OK", stderr="")),
            mock.patch.object(hc, "read_live_usage", return_value={"balance": 144.19}),
            mock.patch.object(hc, "inspect",
                              return_value={"sessions": [], "cu_balance": 144.19, "rate": 10.05}),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        return hc.preflight(args)

    def _scaffold_root(self, td):
        """A ROOT with every file preflight's _sources requires: the
        repo checkout lacks model_cache/verified_manifest.json and
        V3_DRIVEMOUNT_TIMEOUT_PATCH.md, so they are created empty
        (existence is all that check asserts)."""
        root_dir = Path(td) / "root"
        required = ["h3_controller.py", "h3_contract.py", "h3_remote_worker.py",
                    "h3_gpu_probe.py", "h3_drive_probe.py", "h3_init_remote.py",
                    "assemble.py", "build_api_candidate.py", "drive_depot.py",
                    "drive_api.py", "drive_resumable.py", "auth_support.py",
                    "workflow_api.json", "model_cache/verified_manifest.json",
                    "h3_budget.py", "h3_session_watchdog.py", "h3_video_qa.py",
                    "h3_mount_timeline.py", "h3_mount_signature_probe.py",
                    "h3_comfyui_cache.py", "V3_DRIVEMOUNT_TIMEOUT_PATCH.md"]
        for rel in required:
            target = root_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = ROOT / rel
            if src.exists():
                target.symlink_to(src)
            else:
                target.write_text("")
        return root_dir

    def test_preflight_with_spec_passes_all_gates(self):
        with tempfile.TemporaryDirectory() as td:
            ref1 = Path(td) / "ref1.jpg"
            ref1.write_bytes(b"ref-one")
            ref2 = Path(td) / "ref2.jpg"
            ref2.write_bytes(b"ref-two")
            spec = _valid_spec_dict(
                task_id="h3-preflight-test",
                ref_image_1={"local_path": str(ref1),
                             "sha256": hashlib.sha256(b"ref-one").hexdigest()},
                ref_image_2={"local_path": str(ref2),
                             "sha256": hashlib.sha256(b"ref-two").hexdigest()})
            spec_path = Path(td) / "spec.json"
            spec_path.write_text(json.dumps(spec))
            (Path(td) / "manifest.json").write_text(json.dumps(_fake_ready_manifest()))
            result = self._run_preflight(self._args(td, spec_path),
                                         self._scaffold_root(td))
            self.assertTrue(result["passed"], result.get("failed"))
            self.assertTrue(result["checks"]["task_spec_ref_images"]["pass"])
            self.assertTrue(result["checks"]["comfy_api_candidate_static"]["pass"])
            self.assertIn("task-spec patch ok",
                          result["checks"]["comfy_api_candidate_static"]["detail"])
            self.assertTrue(result["checks"]["drive_download_preflight"]["pass"])
            self.assertTrue(result["checks"]["drive_file_token_archive"]["pass"])
            self.assertNotIn("poster_sha256", result["checks"])
            self.assertEqual(result["task_spec"]["task_id"], "h3-preflight-test")
            self.assertEqual(result["task_spec"]["total_frames"], 226)

    def test_preflight_with_bad_ref_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            ref1 = Path(td) / "ref1.jpg"
            ref1.write_bytes(b"ref-one")
            spec = _valid_spec_dict(
                task_id="h3-preflight-test",
                ref_image_1={"local_path": str(ref1), "sha256": "0" * 64})
            spec_path = Path(td) / "spec.json"
            spec_path.write_text(json.dumps(spec))
            (Path(td) / "manifest.json").write_text(json.dumps(_fake_ready_manifest()))
            with self.assertRaises(RuntimeError) as ctx:
                self._run_preflight(self._args(td, spec_path),
                                    self._scaffold_root(td))
            self.assertIn("task_spec_ref_images", str(ctx.exception))


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, timeout=120)


class AssembleSpecTest(unittest.TestCase):
    """Real ffmpeg fixtures: a 39-frame raw (39 % 17 == 5) + a poster still."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        td = Path(cls.tmp.name)
        cls.raw = td / "raw.mp4"
        _ffmpeg("-f", "lavfi", "-i", "testsrc=size=736x1312:rate=24",
                "-frames:v", "39", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.raw))
        cls.poster = td / "poster.jpg"
        _ffmpeg("-f", "lavfi", "-i", "color=c=red:size=736x1312",
                "-frames:v", "1", str(cls.poster))
        cls.poster_sha = hashlib.sha256(cls.poster.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_no_tail_delivers_animation_only(self):
        from assemble import run as assemble_video

        dest = Path(self.tmp.name) / "out39.mp4"
        info = assemble_video(self.raw, None, dest, animation_frames=39, tail_card_frames=0)
        self.assertEqual(info["frames"], 39)
        from h3_video_qa import validate_final_video

        got = validate_final_video(dest, expected_frames=39)
        self.assertEqual(got["frame_count"], 39)
        with self.assertRaises(RuntimeError):
            validate_final_video(dest)  # default expects 274

    def test_tail_appends_static_hold(self):
        from assemble import run as assemble_video

        dest = Path(self.tmp.name) / "out51.mp4"
        info = assemble_video(self.raw, self.poster, dest, animation_frames=39,
                              tail_card_frames=12, tail_sha256=self.poster_sha)
        self.assertEqual(info["frames"], 51)

    def test_assemble_fail_closed(self):
        from assemble import run as assemble_video

        dest = Path(self.tmp.name) / "outx.mp4"
        with self.assertRaises(ValueError):
            assemble_video(self.raw, None, dest, animation_frames=38, tail_card_frames=0)
        with self.assertRaises(ValueError):
            assemble_video(self.raw, None, dest, animation_frames=39, tail_card_frames=12)
        with self.assertRaises(ValueError):
            assemble_video(self.raw, self.poster, dest, animation_frames=39,
                           tail_card_frames=12, tail_sha256="0" * 64)

    def test_output_check_env_driven(self):
        from assemble import run as assemble_video

        dest = Path(self.tmp.name) / "out39b.mp4"
        assemble_video(self.raw, None, dest, animation_frames=39, tail_card_frames=0)
        env = dict(os.environ, H3_CHECK_PATH=str(dest),
                   H3_EXPECTED_FRAMES="39", H3_EXPECTED_FPS="24",
                   H3_OUTPUT_WIDTH="1080", H3_OUTPUT_HEIGHT="1920")
        out = subprocess.run([sys.executable, str(ROOT / "h3_output_check.py")],
                             capture_output=True, text=True, env=env, timeout=120)
        line = next(x for x in out.stdout.splitlines() if "OUTPUT_CHECK_JSON:" in x)
        self.assertTrue(json.loads(line.split("OUTPUT_CHECK_JSON:", 1)[1])["ok"])
        env2 = dict(env, H3_EXPECTED_FRAMES="274")
        out2 = subprocess.run([sys.executable, str(ROOT / "h3_output_check.py")],
                              capture_output=True, text=True, env=env2, timeout=120)
        line2 = next(x for x in out2.stdout.splitlines() if "OUTPUT_CHECK_JSON:" in x)
        self.assertFalse(json.loads(line2.split("OUTPUT_CHECK_JSON:", 1)[1])["ok"])


if __name__ == "__main__":
    unittest.main()
