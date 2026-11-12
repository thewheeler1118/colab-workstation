# Incident: ComfyUI pinned-version install and source-cache scheme

STATUS: CONFIRMED_FIXED

## What happened

ComfyUI is pinned to commit 61e524bdba7a4e193b45c2697f1af83c1beb86d6. The worker prefers the Drive source cache (tarball 12,670,714 B, sha256 3dca94e6…, Drive folder 1MJXkdxXl4EMdKKMf2edpyRJLzQkYT86D) with SHA verification, falling back to git clone; verify_comfyui_commit enforces the pinned commit. The A run restored from cache and validated /object_info.

## Evidence

run_id: h3-20261112T090027Z-8b07c6 (source cache restore + 40GB restore OK; COMFY_OBJECT_INFO_VALIDATED)
  file_path: h3_comfyui_cache.py; h3_remote_worker.ensure_comfy / restore_comfyui_from_cache / verify_comfyui_commit
  line_or_function: h3_remote_worker.ensure_comfy
  test_name: tests/test_h3_comfyui_cache.py (21 tests)
  source_report: config/comfyui_source_manifest.example.json
