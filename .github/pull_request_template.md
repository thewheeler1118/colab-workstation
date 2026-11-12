## What does this PR change?

## Why (link the issue)

## Tests run (offline only — CI runs no GPU, no Drive, no credentials)

- [ ] `python tests/run_ci_tests.py`
- [ ] `python scripts/check_workflow_schema.py`
- [ ] `python scripts/verify_patch.py`
- [ ] `python scripts/check_docs_index.py`
- [ ] Full local discover (only if the poster asset is present locally):
      `python -m unittest discover -s tests -q`

## Safety checklist

- [ ] No credential, token, refresh token, cookie, password, or proxy
      auth material added (see `docs/code_provenance.md`).
- [ ] No model weights, poster binary, or MP4 added.
- [ ] No production behaviour changed unless the linked issue asked for it.
- [ ] Budget rule untouched: per-task 30 CU, never cumulative
      (see `docs/decisions/ADR-001.md`).
- [ ] If this touches the controller, GPU policy, or Drive paths, the
      affected stages in `docs/execution_flow.md` are updated.
