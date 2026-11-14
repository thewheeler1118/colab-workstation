# Generic task spec runbook (TASK-007)

## The rule

A new video task is a **JSON file**, not a code change. The core code is
frozen: `h3_contract.py` / `h3_controller.py` / `h3_remote_worker.py`
never change when the reference image, the storyboard, the resolution,
the frame count or the seed change. If a new task seems to need a `.py`
edit, stop — the spec schema is missing a field, and that is a design
change with its own PR, not a drive-by edit.

## The spec file

`config/task_spec.example.json` is the canonical example: a generic
226-frame task with `tail_card_frames: 0` (no poster tail). The old A
fixture form — 48-frame tail, 274 frames total — lives in
`config/task_spec.with_tail.example.json`, which is byte-equal to the
A adapter's spec. A spec has:

| Field | Required | Meaning |
|---|---|---|
| `task_id` | yes | Production / budget task id. Must equal `--production-task-id` on `run`. A new task id is a new 30 CU envelope — never reuse one. |
| `output_prefix` | yes | Naming token replacing the old "variant": remote `shao_nian_h3_<prefix>.mp4`, local `<prefix>.mp4`, Drive `<prefix>.mp4`, SaveVideo prefix `muse_h3_<prefix>_<run_id>`. |
| `ref_image_1` | yes | ComfyUI node 137 `<Picture 1>`: the main subject/character reference. Exactly one of `local_path` / `drive_file_id`, plus a required `sha256` (64 hex). |
| `ref_image_2` | no | ComfyUI node 139 `<Picture 2>`: scene/style auxiliary reference. Omitted → the worker reuses `ref_image_1` for node 139. Provided → independently SHA-256-verified and uploaded. |
| `prompt` | yes, xor `shots` | Full structured storyboard prompt (non-empty, ≤ 20000 chars). |
| `shots` | yes, xor `prompt` | Timeline list (≤ 32 entries): each `{text, label?, start_s?, end_s?}`. All-or-none timing, strictly increasing, non-overlapping. Rendered deterministically to the prompt string. |
| `seed` | yes | Integer, 0..2^63-1. Fixed per task for reproducibility. |
| `width` / `height` | no (736 / 1312) | Model resolution. Multiples of 16, 64..2048. |
| `fps` | no (24) | 1..60. |
| `animation_frames` | no (226) | 17..2048, **must satisfy `% 17 == 5`** (MiniMax H3 frame rule). |
| `tail_card_frames` | no (0) | Static tail hold of `ref_image_1`. 0 = no tail assembly at all. The old A/B fixture uses 48. |
| `steps` | no (25) | 1..100. |
| `output_width` / `output_height` | no (1080 / 1920) | Delivered video size. Even, 64..4096. |

Unknown keys are rejected at every level; booleans are rejected as
integers. The schema is fail-closed: a bad spec is a hard error at
`plan` / `preflight`, never a silent default.

## Drive-sourced reference images

Drive metadata exposes MD5 only, never SHA-256, so a Drive ref image's
`sha256` **cannot** be proven from metadata. Preflight proves the Drive
file exists and is not trashed; `run` downloads it and verifies the
SHA-256 **before** any GPU is allocated. A hash mismatch fails the run
at 0 CU.

## Standard commands (one spec, no code edits)

Controller commands run under the production venv python (the system
`python3` has no google libs — TASK-007.1). `scripts/run_h3_task.sh`
resolves the interpreter and `COLAB_CLI_PATH` itself; for direct
controller calls use `.venv-colab-cli/bin/python` with `COLAB_CLI_PATH`
set (or `./scripts/preflight.sh`, which resolves the same way).

```bash
# 0 CU: plan + preflight (schema, refs, workflow patch, dual-token
# mint, Drive metadata, live CU balance, active-session check)
COLAB_CLI_PATH=$PWD/.venv-colab-cli/bin/colab .venv-colab-cli/bin/python h3_controller.py plan --task-spec config/my-task.json
COLAB_CLI_PATH=$PWD/.venv-colab-cli/bin/colab .venv-colab-cli/bin/python h3_controller.py preflight --task-spec config/my-task.json

# Live GPU (needs the user's explicit go-ahead for THAT task):
./scripts/run_h3_task.sh --confirm-live --task-spec config/my-task.json \
    --production-task-id <task_id> --run-id h3-<UTC>-<suffix>

# Archive a finished local mp4 under the same spec:
COLAB_CLI_PATH=$PWD/.venv-colab-cli/bin/colab .venv-colab-cli/bin/python h3_controller.py archive --task-spec config/my-task.json --run-id <run_id>
```

`--production-task-id` is still required on `run` and must equal the
spec's `task_id`. For `preflight` / `plan` it may be omitted (the spec's
`task_id` is used).

## The old `--variant A|B` (test fixture adapter only)

`--variant A|B` still works, but only as a built-in test-fixture
adapter: it maps to the equivalent TaskSpec (A: seed 42, B: seed 43,
tail 48, the pinned poster + prompt). All regression tests keep
passing through it, and `config/task_spec.with_tail.example.json` is
byte-equal to the A adapter's spec. New tasks, new docs and the standard
production entry always use `--task-spec`. Do not add a third variant.

## Spec-driven acceptance (TASK-008)

Every acceptance gate reads the spec — there is no 274 / 1080x1920
hardcode left in the chain: remote output check, local download
validation, delivery pre-check and metric F all compare against
`spec.total_frames`, `spec.fps`, `spec.output_width`,
`spec.output_height`. A 226-frame no-tail task and a 720x1280 task
pass the same gates the A fixture does. The only 274 fallback is the
spec-less legacy path (old manifests, `--variant` fixture).

The worker also debounces its ComfyUI polling: a single `/history`
query may time out (30 s now, was 10 s); transient failures
(timeout, 502/503/504, connection errors) are retried with backoff
up to 8 consecutive times while the ComfyUI process is alive, and a
`/prompt` submit that times out is first checked against `/queue`
and `/history` for the same `client_id` before any resubmit — never
a blind second submit, never a killed inference.

## What preflight proves at 0 CU (Task 3)

1. The spec schema, ref-image existence / SHA-256 (local) or metadata
   (Drive), and the ComfyUI workflow patch with the spec's values.
2. Both short-lived tokens are really mintable from the user's ADC
   refresh token: `drive.readonly` (proven by reading a locked model
   file's Drive metadata over the `requests` + `certifi` transport)
   and `drive.file` (proven by a zero-write metadata probe of the
   archive folder). An `invalid_scope` surfaces here, never after
   allocation.
3. The live CU balance, the ledger reconciliation, and that zero GPU
   sessions are active.

## Shutdown scope (Task 4)

The independent shutdown executor is task-scoped: `audit` and
`stop --confirm-stop` take `--production-task-id` and never touch
another task's sessions under the same owner. Every lease also
carries an absolute `emergency_deadline_utc`
(`started_at_utc + max_wall_sec + 900s`); a lease whose deadline was
never finalised is still reclaimed after that hard cap. See
`docs/runbooks/independent-shutdown-cron.md`.
