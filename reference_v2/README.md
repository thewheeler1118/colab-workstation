# reference_v2/ — vendored helper (single file)

`colab_manager.py` is imported by production code, together with its
sibling `generate_video.py`, which `colab_manager.py` imports at module
level (`from generate_video import MODEL_RESOURCE_PROFILES,
RESULT_JSON_PREFIX, SVD_INTEGRATION_STATUS, ParameterValidationError,
VideoGenerationParams, estimate_model_vram_requirement,
inspect_mp4_atoms, is_valid_mp4_file, validate_generation_params`).
Both files are migrated verbatim:

- `h3_controller.py` inserts `reference_v2/` into `sys.path` and imports
  `parse_usage_output`, `parse_sessions_output`, `redact_sensitive_text`
  from `colab_manager`.
- `tests/test_h3_production.py` does the same for its parser tests.

Correction note (2026-11-12, found by the test run, not by grep): an
early grep excluded `reference_v2/` lines from its own output and wrongly
concluded `generate_video.py` was unimported. The full test discovery
failed with `ModuleNotFoundError: No module named 'generate_video'`,
which proved the transitive import; `generate_video.py` was then
migrated verbatim (SHA-256 re-verified) and the suite re-run. The
migration log records this instead of hiding it.

`reference_v2/config.yaml` was NOT migrated: no production code path
loads it — `colab_manager.load_yaml_config` takes an explicit path and
is never called by the H3 controller or the tests — and the file holds a
hardcoded Mac user path (`/Users/skycjq/.local/bin/colab`) plus SVD
model settings unrelated to the MiniMax-H3 job. `reference_v2/AGENTS.md`
and its requirements files were also not migrated.

## Secret-scan note

`colab_manager.py` contains **redaction regular expressions** (patterns
that match tokens so they can be scrubbed from logs). Those patterns are
not credentials and not credential values — they are the scrubber itself.
The repo secret scan records them as expected hits; see
`docs/code_provenance.md` and the scan report in `CHANGELOG.md`.
