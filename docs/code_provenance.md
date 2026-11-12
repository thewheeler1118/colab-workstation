# Code provenance

Three levels. Level 1 is the production baseline. Levels 2 and 3 are
explicitly NOT production code in this repo.

## Level 1 — Muse production code (this repo's `main` baseline)

Verbatim migration of the live production project
`~/workspace/Muse_Colab_H3_Drive_v3/` (source of truth), as of 2026-11-12:
all flat-root `.py` modules, `workflow_api.json`, `requirements-local.txt`,
`reference_v2/colab_manager.py` (only), the 8 `test_*.py` files plus the
live-only `tests/live_drive_resume_check.py`, and the project docs
(`docs/v3_readme.md`, `docs/v3_progress.md`, `docs/deployment_status.json`,
`docs/muse_code_level_prompt.md`, `docs/muse_execute_auth_depot_video.md`,
`docs/storyboard*.md`, `docs/research/drive_v3_remote_download_design.md`,
`docs/audits/archive_p0_*.md`).

Every migrated file's SHA-256 is recorded in `SOURCE_MANIFEST.json` and
re-checked by `scripts/verify_manifest.py`.

Deliberately NOT migrated (live state, binaries, or unimported files):

- `model_cache/` live files: ledger JSONs, `h3_managed_sessions.json`,
  `depot_state.json`, `verified_manifest.json` (live state, never commit).
  Only the sanitized `verified_manifest.example.json` is kept (both under
  `model_cache/` for the tests and as `config/model_manifest.example.json`).
- `assets/original_poster.jpg` (binary poster; SHA-256 pinned in
  `h3_contract.py` instead), `Video_reference.zip`, `output/` MP4s,
  `logs/` run dirs, `comfyui-source/` checkout and tarball (only the
  manifest is kept as `config/comfyui_source_manifest.example.json`),
  `.venv/`, `__pycache__/`.
- `reference_v2/generate_video.py` and `reference_v2/colab_manager.py`:
  `colab_manager` is imported by `h3_controller.py` and the tests, and
  `colab_manager` itself imports 10 names from `generate_video` at module
  level — so both are migrated verbatim. (Correction: an early grep that
  excluded `reference_v2/` lines wrongly concluded `generate_video.py`
  was unimported; the test run proved the transitive import with
  `ModuleNotFoundError`, and the file was then migrated. See
  `reference_v2/README.md` and `CHANGELOG.md`.)
- `reference_v2/config.yaml`: NOT migrated — no production code path
  loads it (`load_yaml_config` takes an explicit path and is never called
  by the H3 controller or tests), and it holds a hardcoded Mac user path
  plus SVD settings unrelated to this job.
- `archive/` prompt docs were migrated as historical records under
  `docs/audits/` (they contain no secrets — verified by the secret scan).

## Level 2 — MiniMax_H3_Clean (third-party audit candidate, NOT present)

Files named by a third-party audit: `config_contract.py`,
`prompts_ab.py`, `workflow_builder.py`, `remote_worker.py`,
`orchestrator.py`, `tests/test_pipeline.py`. **None of these files were
ever present in the Muse VM, and none are in this repo.** Nothing was
reconstructed from the audit report. The prompts live in
`h3_contract.py` (`prompt_for`) and the workflow patch in
`h3_contract.patch_api_workflow`. Evaluation is tracked as a P2 issue
(`docs/issues/p2-evaluate-minimax-h3-clean.md`): only obtainable source,
compared test by test, may enter via an independent PR. Never overwrite
the verified controller on the basis of an audit claim.

## Level 3 — Reference projects and third-party suggestions (design only)

- https://github.com/thewheeler1118/colab-ultra-workstation
- https://github.com/yuanyang749/colab-ultra-workstation
- https://github.com/googlecolab/google-colab-cli

Design reference only (modular structure, run SOPs, hardware scheduling,
lifecycle ideas). No code was forked, vendored, or run from these repos,
except the official `google-colab-cli==0.7.5` itself, which is a
declared dependency plus two documented vendored patches
(`patches/google-colab-cli-0.7.5/`, real diffs against the pristine wheel).

## Secret-scan note

A repo-wide scan finds no credential values. The only pattern hits are the
redaction regular expressions inside `reference_v2/colab_manager.py`
(patterns that MATCH tokens so log output can be scrubbed — the scrubber
itself, not a secret). They are allow-listed by path in CI
(`.github/workflows/ci.yml`) and documented here. If a future scan flags
anything else, it must be verified by a human and redacted before merge.
See `CHANGELOG.md` for the recorded scan result of this migration.
