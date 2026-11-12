# colab-workstation

Versioned Colab AI production workstation with audited workflows, code,
tests and runbooks. The production baseline is the Muse
Colab H3 controller/worker that renders MiniMax-H3 A/B video on Google
Colab GPUs from a Drive-hosted model depot.

> Visibility (2026-11-12, verified live): this repo is currently
> **PUBLIC**. The change to private is blocked — the stored GitHub
> token lacks the Administration scope (see
> `TASK-002_SECURITY_CI_PRODUCTION_REMEDIATION.md`). Do not treat the
> repo as private until the visibility is re-verified.

## Status (2026-11-12, v0.1.0 — initial migration + TASK-002 local integration, not yet pushed)

- **Variant A: SUCCEEDED** — run `h3-20261112T090027Z-8b07c6` (G4,
  274 frames, A.mp4 7,453,808 B, SHA-256
  `907a23dcf7fba719a3fd7d622c3477280698c2e1dd6a795c0f0ffd6593a63cbb`,
  poster-tail PSNR 31.38 dB). Archived to Drive
  `Muse-H3-Video-Outputs/h3-20261112T090027Z-8b07c6/` (folder
  `1t4PXRCytvIvQAYzhZkXfxPI3sR63cYo8`, 6 files; A.mp4 Drive ID
  `17tMh9D_a6a1tz0bPgPHZoaAI6YflyS_S`, local MD5 = server MD5
  `77f132fdf32c4a1a3ca1246b54371303`) — verified 2026-11-12 in the H3
  production session, not re-checked inside TASK-002.
- **Variant B: PENDING RE-RUN** — run `h3-20261112T093207Z-7ab515` was
  interrupted by a VM restart ~09:48 UTC; the orphan A100 session was
  stopped and no MP4 was produced.
- **Budget (task `h3-ab-20261111`)**: baseline 162.77 CU, max observed
  spent 18.58 CU (2026-11-12T09:49 UTC). Per-task independent 30 CU —
  never cumulative (see `docs/decisions/ADR-001.md`).
- **Tests in this repo**: 224 total.
  **222 pass, 2 fail** in a clean checkout. The 2 failures are the
  asset-gated poster tests below — expected, not a regression.
- **No Colab GPU was created, no CU was spent, and no Drive content was
  touched by this migration.**

## The two expected test failures (read this before running the suite)

`tests/test_contract.py::ContractTest.test_poster_hash` and
`tests/test_h3_production.py::ProductionTest.test_poster_sha256_required`
call `validate_poster` on `assets/original_poster.jpg`. The original
poster binary is intentionally **excluded** from this repo (no poster
binaries in git), so both fail in a clean checkout with:

```
ValueError: Original poster missing or SHA256 mismatch: <repo>/assets/original_poster.jpg
```

In the source project both pass because the poster file is present on
disk (source baseline: 173/173 OK, 2026-11-12). CI runs
`tests/run_ci_tests.py`, which runs the same discovery but skips exactly
those two tests with a printed reason. Run the plain discovery locally
only if the poster asset is present:

```bash
python -m unittest discover -s tests -q   # 185 tests, 2 poster-gated errors in a clean checkout
python tests/run_ci_tests.py               # 183 tests, 0 failures, 2 asset-gated skips
```

## Layout — flat repo root is deliberate (ADR-003)

Production modules import each other **top-level**
(`from h3_budget import ...`) and the tests do
`sys.path.insert(0, str(ROOT))`; `h3_controller.py` also inserts
`reference_v2/` into `sys.path` to import `colab_manager`. So v0.1.0
keeps **all verbatim production `.py` files flat at the repo root**.
Do not "tidy" them into packages in a drive-by commit — the package
refactor is a separate future PR, and `workstation/` currently holds
only a PLANNED layout document with no implementation.

```
colab-workstation/
├── h3_controller.py  h3_budget.py  h3_contract.py  h3_remote_worker.py ...
├── reference_v2/            # colab_manager.py + generate_video.py (vendored helpers)
├── model_cache/verified_manifest.example.json   # sanitized example only
├── config/                  # video_profiles.yaml, gpu_policy.yaml, budget_policy.yaml, *.example.json
├── jobs/minimax_h3_ab/      # job.yaml, video_contract.py (facade), README.md
├── workflows/               # source UI workflow, api copy, JSON schema
├── patches/google-colab-cli-0.7.5/   # real diffs vs pristine 0.7.5 wheel
├── scripts/                 # preflight / sessions / budget / drive / run / outputs / shutdown
├── tests/                   # 8 migrated suites + test_savevideo_history.py + test_config_contract.py
├── docs/                    # architecture, execution_flow, module_inventory, code_provenance,
│                            # runbooks, incidents (24), audits, research, decisions (5), issues (9)
├── examples/  workstation/ (PLANNED)  .github/workflows/ci.yml
├── SOURCE_MANIFEST.json     # every file with size + SHA-256 (verify: scripts/verify_manifest.py)
└── README.md  AGENTS.md  CHANGELOG.md  pyproject.toml  requirements.lock
```

## Pinned values (single source of truth: the code)

| Item | Value |
|---|---|
| MiniMax-H3 HF revision | `54472221d27c695fca9621b0c3ed52f06c4af707` (4 files, 40,073,842,159 B) |
| ComfyUI commit | `61e524bdba7a4e193b45c2697f1af83c1beb86d6` |
| Video | 736×1312, 24 fps, 226 animation + 48 poster = 274 frames, 25 steps, beta, seeds 42/43, Turbo off |
| Final | 1080×1920, 274 frames, ~11.42 s |
| GPU plan | G4, G4 retry, then A100 High-Mem once (A100 40GB never allowed) |
| Budget | 30.00 CU per task, 25.00 CU per session, 0.75 CU cleanup reserve |
| READY manifest (Drive) | `1jLYefARIDs8VLtsCXJPuTU7zHXw0coly`, depot root `1YmCfFOOsoMvREhdyHusBDy95C1eI4KE_` |

Config YAMLs mirror these constants and are asserted by
`tests/test_config_contract.py`.

## How to use

```bash
# Offline checks (no GPU, no CU, no credentials)
python -m py_compile *.py
python tests/run_ci_tests.py
python scripts/check_workflow_schema.py
python scripts/verify_patch.py
python scripts/verify_manifest.py

# Read-only live checks (colab usage/sessions only, no GPU created)
./scripts/preflight.sh
./scripts/check_sessions.sh
./scripts/check_budget.sh
./scripts/verify_drive.sh
./scripts/verify_shutdown.sh

# LIVE GPU RUN — spends CU, needs explicit operator confirmation + TTY
./scripts/run_h3_ab.sh --confirm-live --variant A --run-id h3-<UTC>-<suffix>
```

## CI is offline only

`.github/workflows/ci.yml` runs syntax checks, the test suite, the
workflow schema check, patch verification, a grep-based secret scan,
and the docs-index check. It never creates a GPU, never touches Drive,
and never uses a credential. Real GPU acceptance is a separate,
explicitly authorised manual task.

## Safety

- No credential, token, refresh token, cookie, password, or proxy auth
  material is in this repo. Secret scan result (2026-11-12): clean,
  except the redaction regexes in `reference_v2/colab_manager.py`
  (the scrubber itself, allow-listed) and two tuned-out false positives
  (the CI file's own search patterns; a dotted model filename). See
  `CHANGELOG.md` and `docs/code_provenance.md`.
- No model weights, poster binary, or MP4 in git (`.gitignore` + CI scan).
- No `colab-dfs-ext`, no Mac GPU relay, no third-party model substitution
  (ADRs 004–005).

## Known gaps (not hidden)

- `archive_variant_to_drive` exists but is **not called by `run_variant`**
  — Drive delivery is therefore UNVERIFIED in the live flow
  (`docs/execution_flow.md`, incident 24).
- `find_rendered_video` has no ffprobe decode gate and no RUN_ID-prefix
  fallback (P0 issue; regression tests added).
- VM restarts orphan GPU sessions; recovery is manual
  (`docs/runbooks/vm-restart-orphan-recovery.md`).
- The drivemount 600s fix is mock-verified; live mounts so far finished
  authorization inside the old 120s window (incident 07).
- Third-party `MiniMax_H3_Clean` files were never obtained; nothing from
  that audit is merged (P2 evaluation issue).

## Development flow

Issue → feature branch → tests → pull request → review → merge. Never
commit directly to `main`. See `AGENTS.md`.
