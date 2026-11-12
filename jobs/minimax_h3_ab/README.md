# jobs/minimax_h3_ab — MiniMax-H3 A/B production job

Task `h3-ab-20261111`, independent budget 30.00 CU (see `job.yaml` and
`docs/decisions/ADR-001.md`).

## Where the real logic lives

This job directory holds **configuration and a thin facade only**. The
production implementation stays flat at the repo root so existing imports
keep working unchanged (see `README.md` — flat compat layout):

| Job concern | Real implementation |
|---|---|
| Video contract (sizes, frames, seeds, prompts) | `h3_contract.py`: `prompt_for`, `patch_api_workflow`, `validate_poster`, `validate_model_cache_manifest` |
| Job facade (re-exports) | `jobs/minimax_h3_ab/video_contract.py` |
| API workflow (canonical) | `workflow_api.json` at repo root; copy at `workflows/api/workflow_api.json` |
| Source UI workflow | `workflows/source/video_minimax_h3_r2v.json` (UI format; never submit directly) |
| UI → API conversion | `build_api_candidate.py` (`convert`) — produces a candidate for validation only |
| Controller / state machine | `h3_controller.py` (`preflight`, `run_variant`) |
| Remote worker | `h3_remote_worker.py` |
| Budget ledger | `h3_budget.py` |
| Story / storyboard | `docs/storyboard.md`, `docs/storyboard_full_drive.md` |

## What is NOT here

`prompts_ab.py` and `workflow_builder.py` from the third-party
`MiniMax_H3_Clean` audit candidate are **not in this repo**: those files
were never obtained in this VM, and nothing is imported or reconstructed
from the audit report. The prompts live in `h3_contract.py`
(`BASE` + `SCENES`, via `prompt_for`) and the workflow patch lives in
`h3_contract.patch_api_workflow`. Evaluation of the third-party candidate
against this baseline is tracked as a P2 issue
(`docs/issues/p2-evaluate-minimax-h3-clean.md`) — it may only enter via
an independent PR with its own tests, never by overwriting this baseline.

## Status (2026-11-12)

- Variant A: **SUCCEEDED** in run `h3-20261112T090027Z-8b07c6`
  (G4, 274 frames, poster-tail PSNR 31.38 dB).
- Variant B: **PENDING RE-RUN** — run `h3-20261112T093207Z-7ab515` was
  interrupted by a VM restart ~09:48 UTC; the orphan A100 session was
  stopped and no MP4 was produced. Budget remaining is within the same
  30 CU task envelope (see `CHANGELOG.md`).
