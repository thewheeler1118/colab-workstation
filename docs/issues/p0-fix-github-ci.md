# Issue draft: P0: Fix GitHub CI clean-checkout failures

STATUS: PROPOSED_ONLY.

TASK-002 NOTE (2026-11-12, offline-verified only): the workflow is
now five independent jobs (`unit-tests`, `workflow-validation`,
`patch-validation`, `secret-scan`, `docs-and-manifest`) with no `needs`
edges, so a test failure can no longer silently skip the security
checks; `unit-tests` installs ffmpeg and `requirements-ci.txt`
(requests==2.34.2, PyYAML==6.0.3); patch validation uses vendored
pristine CLI 0.7.5 fixtures instead of asserting about the installed
CLI; the secret scan runs pinned Gitleaks 8.27.2 (SHA-256 verified)
over worktree and history plus `scripts/secret_scan.py`.
(`.github/workflows/ci.yml`, branch `ci/clean-checkout-fix`, commit
`af4d1c6`.) Local CI entry: 222 ran, 0 failures, 0 errors, 2
asset-gated skips. A real GitHub Actions run on the pushed branches is
still pending — pushes are blocked while the repo is public (see the
TASK-002 report) — so this is NOT proven green on GitHub yet.

## Context

.github/workflows/ci.yml — single sequential job. Historical run:
https://github.com/thewheeler1118/colab-workstation/actions/runs/43209421525
(head 17075e6): 183 tests, 4 assertion failures, 12 execution errors;
workflow-schema, patch-verification, secret-scan, and docs steps were
all skipped. Causes: no `requests`/`PyYAML`/`ffmpeg` in the runner,
and patch-presence tests asserted about the installed CLI, which a
clean checkout cannot satisfy.

## Reproduction

1. From a clean checkout, run the CI entry point and confirm all five
   jobs execute even when `unit-tests` fails.
2. Confirm the patch tests pass against the pristine fixtures and fail
   against an unpatched CLI copy.
3. Confirm the secret scan runs on worktree AND history.
