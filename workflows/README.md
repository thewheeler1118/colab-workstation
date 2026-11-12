# workflows/

## Canonical files

| File | What it is |
|---|---|
| `workflows/source/video_minimax_h3_r2v.json` | Source **UI-format** ComfyUI workflow (has `nodes`/`links`, 146 nodes). Exported from the official MiniMax-H3 GUI template. **Never submit directly to `/prompt`.** |
| `workflow_api.json` (repo root) | Canonical **API-format** workflow, produced by ComfyUI "Save (API Format)". This is what the controller submits. |
| `workflows/api/workflow_api.json` | Byte-identical copy of the root `workflow_api.json`, kept here so the workflow directory is self-contained. Provenance: copied verbatim from the repo-root file at migration time; the root file is the canonical one. |
| `workflows/schemas/api_workflow.schema.json` | JSON Schema for the API workflow (stdlib-checked by `scripts/check_workflow_schema.py`, no network). |

## How the API workflow becomes a submission

1. `build_api_candidate.py` converts a UI-format workflow to an API-format
   **candidate** (`convert`). The candidate is for validation only.
2. `h3_contract.patch_api_workflow(api, variant, poster_name, seed)` patches
   the known official GUI node IDs (136 MiniMaxH3ReferenceToVideo, 137/139
   LoadImage, 124 BasicScheduler, 129 RandomNoise, 92 SaveVideo, 127
   UNETLoader, 128 CLIPLoader, 146 PrimitiveBoolean; plus 141/143/145
   handling) and refuses on any node/type mismatch.
3. `h3_remote_worker.validate_api_against_object_info` checks the patched
   workflow against the live ComfyUI `/object_info` before submission.
4. `h3_remote_worker.execute_prompt` submits to `/prompt` and waits for the
   history result.

## Rules

- Node IDs are pinned by `patch_api_workflow`. If the official template
  drifts, map the new IDs explicitly — never silently accept a different
  graph.
- The Turbo LoRA branch (node 145, `LoraLoaderModelOnly`) is **removed**
  by the patch: the 1.82 GB Turbo LoRA is not in the 4-file depot, so a
  live `/prompt` rejects the whole graph with HTTP 400. Node 146 forces
  the switch off and node 141 is re-pointed at the plain model (node 127).
  Verified live in run `h3-20261112T062103Z-901bc6` (400) and the A run
  `h3-20261112T090027Z-8b07c6` (no longer 400).
- `USE_TURBO = False` in the job config. Re-enabling Turbo requires
  seeding the LoRA into the depot first — a separate, reviewed change.
