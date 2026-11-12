# Vendored Colab CLI drivemount timeout patch — remote mount 120s → 600s (2026-11-11)

## Files patched
`.venv/lib/python3.12/site-packages/colab_cli/commands/automation.py`,
in the official `google-colab-cli==0.7.5`:
1. `drivemount()` — the injected remote cell code.
2. `INTERACTIVE_AUTOMATION_TIMEOUT_SEC` — the local execute ceiling.

## Exact change
```python
# 1. drivemount(): was
code = f"from google.colab import drive\ndrive.mount('{path}')"
# now
code = f"from google.colab import drive\ndrive.mount('{path}', timeout_ms=600000)"

# 2. module constant: was 600, now 660
INTERACTIVE_AUTOMATION_TIMEOUT_SEC = 660
```

## Why it exists (live evidence, 2026-11-11)
- Verified in the vendored source: `drivemount()` injects exactly
  `from google.colab import drive` + `drive.mount('<path>')` with **no
  `timeout_ms` argument**, so the remote Colab runtime applies its default
  `timeout_ms=120000`, measured from cell start **including** the wait for
  browser authorization. `colab drivemount` exposes no timeout flag, and no
  other place in the CLI passes one — the injected code string is the only
  place the value can be set without leaving the official CLI.
- Runs 6 (`h3-20261111T015803Z-a0d192`) and 7 (`h3-20261111T021132Z-22c8ee`)
  both failed exactly here: authorization completed (~9 min and ~7 min after
  the prompt), `Credentials propagated` printed, then the remote kernel
  raised `ValueError: mount failed` (drive-timeout FAQ). The 120s kernel
  timer had expired long before the authorization finished.
- 600000ms (10 min) covers a realistic interactive ceremony, including the
  browser-login round trip. The 660s local ceiling exists because
  `jupyter-kernel-client` sets a hard wall-clock deadline of
  `monotonic() + timeout` for the whole execute (`wsclient.py`: deadline is
  fixed at start; arriving messages do not extend it). At 600s local the
  local side would kill the mount at the same instant the remote timer
  allows it to succeed; 660s leaves a 60s margin for propagation + reply.

## WebSocket credential handling — blocking evaluation
- `runtime.py` `ColabRuntime._apply_ws_hook()` monkey-patches
  `wsclient.kernel_socket.on_message`. Every incoming WebSocket message is
  deserialized; `msg_type == "colab_request"` is handed to
  `drivefs_hook` in `automation.py`, and a `True` return intercepts the
  message (it is NOT passed to the original handler).
- `drivefs_hook` handles `authType == "dfs_ephemeral"` only. It runs
  **synchronously on the WebSocket dispatch path**: it GETs then POSTs
  `{colab_domain}/tun/m/credentials-propagation/{endpoint}` (dryrun=true,
  then dryrun=false), prints the `unauthorized_redirect_uri` when
  `success` is false, and blocks on `/dev/tty` readline ("Press Enter after
  you have granted access...") until the operator confirms. On HTTP 200 it
  prints "Credentials propagated. Resuming mount...", logs
  `drive_auth_success`, and sends an `input_reply`
  (`{"type": "colab_reply", "colab_msg_id": msg_id}`) back through
  `wsclient.stdin_channel`.
- Verdict: the hook **does block the WebSocket message loop while it waits**,
  but that block is benign for the mount flow itself — the kernel is idle
  waiting for the `colab_request` reply, so no other kernel message is
  expected in the meantime. The block is harmful only indirectly: every
  second the hook spends waiting (Enter + propagation round trips) is a
  second off the remote `drive.mount()` timer. That is exactly what killed
  runs 6–7 at 120s, and exactly what the 600s remote timeout fixes. The
  hook itself needs no change.
- Two residual risks, recorded not fixed here:
  1. The propagation GET/POST use `google.auth.transport.requests`
     (`auth.py`), not the patched `client.py._issue_request`. The GET has no
     body and the POST carries a multipart `files=` body, so the empty-POST
     411 strip does not apply to them (verified by reading `auth.py`).
  2. `hooked_on_message` swallows hook exceptions at debug level and falls
     through to the original handler. If propagation throws mid-flow, the
     mount then fails opaquely. The mount timeline (see `h3_mount_timeline.py`)
     flags a missing `drive_auth_success` event so this is diagnosable.

## Scope and limits
- Only the injected mount cell and the local automation ceiling change.
  The `colab` command, the `drivemount` subcommand, the auth provider
  (ADC), the interactive authorization ceremony, and the credential
  propagation path are all untouched. No third-party DFS, no Mac relay,
  no other cloud, no custom exec cell replacing `drivemount`.
- This patches the venv copy only. If the venv is rebuilt or the CLI is
  upgraded, the patch is lost — `h3_controller.preflight` checks for it
  (`cli_drivemount_timeout_patch`) and fails closed before any GPU is
  requested, and `tests/test_h3_mount_repair.py` asserts its presence.
- The remote `drive.mount(timeout_ms=...)` signature has NOT yet been
  verified live on a Colab runtime (no GPU may be created this round).
  The controller runs a remote signature probe (`h3_mount_signature_probe.py`)
  before the mount on the next run and aborts fail-closed if `timeout_ms`
  is not accepted. Until a patched mount succeeds on a real GPU, the fix
  is verified by Mock tests + static checks only.
- The old 900s local wait is NOT this fix and is not claimed to be one:
  900s only caps the local `drivemount` subprocess. The fix is the remote
  `timeout_ms=600000` in the injected code. The local subprocess ceiling
  stays 900s and its floor is raised to 720s (600 remote + 120 margin) so
  the controller never kills a mount that still has remote time left.
