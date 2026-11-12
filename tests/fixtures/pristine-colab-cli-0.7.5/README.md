# Pristine google-colab-cli 0.7.5 fixture files

These two files are the **unpatched** official `google-colab-cli==0.7.5`
sources, extracted verbatim from the PyPI wheel. They exist so the CLI
patch tests can prove, fully offline and without any installed CLI:

1. on pristine 0.7.5 the patch-presence checks report **absent**;
2. after applying the vendored patches in an isolated temp dir with
   `git apply`, the same checks report **present**;
3. the `drivemount` injection really contains `timeout_ms=600000` and
   the POST-body fix really exists.

Source:

- Wheel: `google_colab_cli-0.7.5-py3-none-any.whl`
- Wheel SHA-256 (PyPI JSON API, 2026-11-12):
  `2b303ed21ebd6da829f80d506224a6cd4f0e62d506fb373166d06a0532e516a9`
- Files: `colab_cli/client.py` (11,772 bytes),
  `colab_cli/commands/automation.py` (9,805 bytes)

Only these two files are vendored (the patch targets), not the whole
wheel. Do not edit them: any change breaks the "pristine" premise of
`tests/test_h3_mount_repair.py::CliPatchTest` and
`tests/test_h3_production.py::ProductionTest.test_cli_post_body_patch_present`.
The vendored patches live in `patches/google-colab-cli-0.7.5/` and are
verified independently by `scripts/verify_patch.py`.
