# Production acceptance runbook (TASK-006 P1-C, Issue #50)

**PROPOSAL ONLY — never auto-executed.** Every step below needs a
separate, explicit user authorisation before it runs. This runbook is
the live production test plan for the first real GPU run after the
TASK-005 / TASK-006 changes. It is the acceptance test for Issues #1,
#2, #5, #6 (record the real RUN_ID there before marking
CONFIRMED_FIXED) and the first post-TASK-005 production sample (n=1)
for the reliability metrics, including metric F.

## 0. What "acceptance" means

Production reliability is not "the code ran". A run is accepted only
when, within the authorised budget and deadline, the video is truly
durable AND the GPU is confirmed stopped:

1. GPU allocation succeeds (session_id + `GPU_ALLOCATED` stage).
2. Model restore + load succeeds (`MODELS_READY` stage).
3. Inference produces a spec-conforming MP4 (`VIDEO_VALIDATED` stage,
   local SHA-256, 274 frames).
4. The MP4 is independently verified in persistent storage
   (`DRIVE_VERIFIED` stage, delivery_state `DELIVERED`, drive_file_id).
5. The GPU session is confirmed stopped (`GPU_STOP_CONFIRMED` stage,
   session_state `STOPPED`).
6. CU is within the authorised budget (ledger
   `max_observed_spent_cu <= project_budget_cu`).
7. The final manifest + audit log are complete
   (`task_state == TASK_COMPLETED` + `TASK_COMPLETED` stage).

All seven are the numerator conditions of metric F
(`deadline_bound_e2e_success_rate` in `h3_reliability.py`). A run
that meets only some of them is a failure, not a partial pass.

## 1. Authorisations needed (each one separately, from the user)

1. **Sync the production directory** `~/workspace/Muse_Colab_H3_Drive_v3/`
   to the merged `main` (backup first, re-verify hashes, re-run the
   test suite there). Without this, the live run uses pre-TASK-002 code.
2. **Install the production PATH CLI** (`scripts/install_colab_cli.sh`
   into the repo `.venv`): the patched 0.7.5 CLI the environment
   contract requires. Without it, preflight fails closed.
3. **Start one GPU run** (the B re-run, task `h3-ab-20261111`,
   RUN_ID to be assigned by the controller). This is the only GPU
   authorisation in this plan. It does NOT authorise a second run,
   a retry, or any other task.
4. **(Optional, separate) Deploy the independent-shutdown cron**
   (PR-3 runbook `docs/runbooks/independent-shutdown-cron.md`):
   four approvals there. The cron is NOT required for this run — the
   in-process watchdog + controller stop are the primary shutdown path.
   Do not deploy it as part of this run unless the user approves it
   separately.

Never: buy CU, enable Cloud Billing, create a second GPU because the
first is unobservable, rewrite git history, touch the model depot,
or reuse the B re-run authorisation for another variant.

## 2. GPU whitelist and CU cap

- **GPU whitelist:** G4 (RTX PRO 6000 Blackwell) first, one fallback
  to A100 High-Mem 80 GB on capacity failure only
  (`--gpu-policy G4_THEN_A100_HIGHMEM`). No other GPU type.
- **CU cap:** the task budget is 30.00 CU total for `h3-ab-20261111`;
  the remaining balance for the B re-run is **11.42 CU** (spent 18.58
  CU, effective hard stop 132.91). One full B run is estimated at
  2.8–4.3 CU. Per-session cap 25.00 CU, cleanup reserve 0.75 CU.
  The controller enforces the hard stop; the operator re-checks the
  live balance and the live session list before starting.
- **Max duration:** one session is bounded by the budget wall time
  the controller computes from the live rate (about 22 min of
  inference wall time for the A run; the B run is the same shape).
  If the wall time is reached without a finished video, the run fails
  with the deadline reason — that is a failure to report, not a
  reason to extend the session.

## 3. Pre-flight checklist (before any GPU exists)

- [ ] Production dir synced to merged `main`, hashes re-verified,
      test suite green there (408+ tests at TASK-005 close; 569 at
      PR-5 close — re-check the count at run time).
- [ ] Production PATH CLI installed: `colab --version` reports 0.7.5
      and the environment contract passes (patched client SHA-256,
      both patch markers, ffmpeg/ffprobe 8.1.2).
- [ ] Live Colab state read-only checked: no active sessions, rate 0,
      balance ≥ the effective hard stop + the 30 CU task budget.
- [ ] Drive depot still CACHE_READY (READY manifest
      `READY_54472221d27c695fca9621b0c3ed52f06c4af707.json` verifies).
- [ ] ADC token fresh (lifetime ~60 min; the run restores ~40 GB
      through the noninteractive download path with bounded token
      renewal — the renewal channel is unit-tested, NOT live-verified,
      so watch the first renewal).
- [ ] No other controller holds the task lock for `h3-ab-20261111`.
- [ ] The B re-run uses the same task id, budget and deadline rules:
      `--production-task-id h3-ab-20261111`, and a
      `--task-deadline-utc` the user states for this run (the
      historical ledger has no deadline; metric F needs one recorded
      at authorisation — the deadline is the user's call, not the
      assistant's).

## 4. Shutdown checks

- [ ] During the run: the watchdog is alive (heartbeat in the manifest)
      and the lease is registered with a final deadline.
- [ ] After inference: `stop_and_verify_session` confirms STOPPED by a
      live query, the managed record is marked STOPPED, the watchdog
      is terminated, and the lease is confirmed stopped.
- [ ] After the run: an independent read-only check shows no active
      Colab sessions and rate 0. A session that is still ACTIVE after
      the controller claims a stop is a FAILED run, even if the video
      is durable.
- [ ] If the VM restarts mid-run: do NOT stop the orphan session
      blindly. First inspect/download from it (inference may have
      completed), then stop it and verify. A restart is itself a test
      of the resume path — record the RUN_ID and what the resume did.

## 5. Artifact persistence checks

- [ ] The local MP4 is validated (ffprobe: 1080×1920, 24 fps,
      274 frames, 11.417 s, video + audio) and its SHA-256 matches
      the manifest's `local_sha256`.
- [ ] The worker-side remote upload (if it ran) is verified by
      server-side metadata + an independent read-back, not by the
      upload API response alone. If the worker upload did not run,
      the controller's idempotent delivery must still verify the
      Drive file server-side before recording DELIVERED.
- [ ] The Drive file id recorded in the manifest resolves to the same
      size + MD5 as the local file. A same-name file with a different
      SHA-256 is a conflict, not a delivery.
- [ ] The run is archived under `Muse-H3-Video-Outputs/<RUN_ID>/`
      (A.mp4, qa_A.json / qa_B.json, workflow JSON, RUN_REPORT.md)
      and the archive is re-read from Drive, not just from the local
      upload log.

## 6. Token-expiry handling

- The ADC access token lives ~60 min; a 40 GB restore at measured
  Drive API rates takes far longer than one token lifetime, so the
  run depends on the bounded token-renewal channel (PR-2: at most 6
  renewals, 600 s margin, token_id only in the logs — never a token
  value). Watch for:
  - the first `token_refresh_requested` job-state signal and the
    controller's `token_renewed` audit event;
  - a `token_renewal_refused` event (renewal cap reached): the run
    must fail closed, not continue with an expired token;
  - a 401 during a chunked download: the downloader refreshes once,
    then fails with `DriveAuthError` — a second 401 is not retried.
- If the token expires mid-restore and renewal fails, stop the
  session, record the evidence, and report. Do not mint a long-lived
  token, do not pass a token through chat, and do not retry the
  restore with a fresh manual token unless the user authorises it.

## 7. VM-restart simulation (one phase per run, only if the user asks)

A restart is simulated by killing the local controller (not the
remote session) at exactly one phase, then resuming with the same
RUN_ID. Never simulate two phases in one run — the result would not
attribute to either.

| Phase | What to observe on resume |
|---|---|
| Mid-bootstrap (after `init_remote`, before worker launch) | Bootstrap re-runs idempotently; no second GPU; worker launches once. |
| Mid-inference | Worker state is re-polled; no re-inference; no second GPU. |
| After MP4 written, before upload | Local MP4 reused; upload proceeds; no re-inference. |
| During remote upload | Upload sidecar resumes the same session; no duplicate file. |
| After upload, before recording | Worker state evidence is found; no duplicate upload. |
| After Drive verified, GPU UNKNOWN | Resume refuses; no second GPU; operator inspects. |

Each simulation needs its own user authorisation (it spends CU while
the GPU runs) and its own RUN_ID. An unplanned real VM restart during
the run is recorded the same way — it is data, not a reason to hide
the interruption.

## 8. Pass / fail criteria

**PASS** (all of them): the seven conditions in §0 hold; the CU
spent for the run is within the remaining 11.42 CU; the session is
confirmed stopped by a live query after the run; the manifest audit
log contains the full stage sequence with no ANOMALY that changes a
verdict; the Drive archive re-reads correctly.

**FAIL** (any of them): any of the seven conditions unmet; CU over
the remaining budget; a session left ACTIVE or UNKNOWN after the
controller finished; a duplicate GPU allocated for the same task; a
re-inference of an already-verified MP4; a delivery recorded without
server-side verification; a token value appearing in any log,
manifest, or report.

A FAIL is reported with the RUN_ID, the failed condition, the CU
spent, and the session state — and the task counts in metric F's
denominator as a failure. It is never re-run silently: a retry is a
new authorisation.

## 9. Rollback

- **Stop the GPU first:** `stop` action on the RUN_ID, then a live
  read-only check that no session remains and the rate is 0.
- **Do not delete:** the manifest, the audit log, the local MP4, or
  the Drive archive. A failed run's evidence is what makes the next
  run safer; deleting it hides the failure.
- **Do not roll back the code:** the production directory stays on
  the merged `main` that was verified before the run. A code change
  after a failed run is a new PR, not a hot edit.
- **Report:** `scripts/reliability_report.py --root <production root>`
  — the metric F section (per-task verdicts + failed conditions) is
  the CU / failure / success report for the run. File it with the
  RUN_ID in Issues #1, #2, #5, #6 before marking anything
  CONFIRMED_FIXED.

## 10. What stays out of scope

- No second GPU run, no A/B re-run, no new task, no CU purchase.
- No cron deployment (separate approvals in
  `docs/runbooks/independent-shutdown-cron.md`).
- No history rewrite, no visibility change (Issue #4, separate).
- No story / prompt / image / resolution / steps / quality change
  (Issues #20 / #21 / #23 stay independent).
