# State-machine migration — remote durable artifact delivery (TASK-006 P1-A, Issue #48)

This is the migration design for PR-4, written before the implementation
and committed with it. Status: implemented (offline-verified only);
no live GPU run has exercised it yet.

## The problem with the linear model

`h3_run_manifest.py` models the task as one linear list:
PREFLIGHT → TASK_AUTHORIZED → GPU_ALLOCATED → WORKER_STARTED →
MODELS_READY → INFERENCE_COMPLETED → VIDEO_VALIDATED →
ARTIFACT_DURABLE → GPU_STOP_CONFIRMED → DRIVE_VERIFIED →
TASK_COMPLETED, with forward-only moves.

The brief requires DRIVE_VERIFIED to be able to occur BEFORE
GPU_STOP_CONFIRMED: the worker uploads the finished MP4 while the GPU
is still alive, so a Muse VM restart after inference cannot destroy
the artifact. In the linear list that is a forward jump that SKIPS
GPU_STOP_CONFIRMED: the manifest would then show DRIVE_VERIFIED with
no record of whether the GPU was ever confirmed stopped, and
TASK_COMPLETED (which only checked current == DRIVE_VERIFIED) could
hide a still-billing GPU. That is faking stage order.

## Migration: three separate state axes

`TASK_STAGES` and its forward-only rule stay for the TASK axis. The
artifact axis and the GPU session axis are modelled separately:

1. **Task axis** (`task_state`, unchanged linear list): the task's
   progress. DRIVE_VERIFIED on this axis keeps its current meaning
   (the controller has independently verified the Drive artifact) and
   still gates TASK_COMPLETED.
2. **Artifact axis** (`artifacts.delivery_state`, extended): the MP4's
   durability, independent of the task axis:
   TEMPORARY → COMPLETE_LOCAL → REMOTE_UPLOAD_STARTED →
   REMOTE_UPLOAD_VERIFIED → DURABLE_UPLOAD_STARTED →
   DURABLE_UPLOAD_VERIFIED → DELIVERED.
   The two new states are worker-side: REMOTE_UPLOAD_STARTED (the
   worker began the resumable upload) and REMOTE_UPLOAD_VERIFIED (the
   worker uploaded AND verified via metadata + read-back; evidence
   recorded, not just a claim). The controller-side states keep their
   meanings. `artifacts` also gains `remote_upload` evidence
   (drive_file_id, size, sha256, md5, uploaded_by "worker",
   verified_by "worker_readback", upload start/finish times, reused
   flag) and `drive_verified_before_stop`.
3. **GPU session axis** (`session_state`, unchanged): ACTIVE / STOPPED /
   UNKNOWN.

`update_run` changes:
- Moving to DRIVE_VERIFIED while `session_state` != STOPPED records a
  `DRIVE_VERIFIED_BEFORE_STOP` audit event and sets
  `artifacts.drive_verified_before_stop = True`. The stage order is
  never faked by marking GPU_STOP_CONFIRMED early.
- TASK_COMPLETED now requires BOTH current == DRIVE_VERIFIED AND
  `session_state == STOPPED`. (Previously only the first was checked;
  with out-of-order verification the second check is what stops a
  completed task from hiding a billing GPU.) A caller passing
  `session_state=STOPPED` in the same call satisfies the gate.
- `delivery_state` itself is forward-only in the artifacts merge:
  TEMPORARY → COMPLETE_LOCAL → REMOTE_UPLOAD_STARTED →
  REMOTE_UPLOAD_VERIFIED → DURABLE_UPLOAD_STARTED →
  DURABLE_UPLOAD_VERIFIED → DELIVERED. A backward move (e.g. a stale
  local-state update writing TEMPORARY after the worker already
  verified the remote upload) is refused and logged as an ANOMALY,
  never applied. This is what makes the call order in `run_variant`
  and `_resume_finish` (record the remote upload AFTER the local
  TEMPORARY/COMPLETE_LOCAL update) defense-in-depth rather than the
  only protection.
- `last_verified_stage` still only moves forward on the task axis.
- Old manifests load unchanged: missing `delivery_state` is treated
  as before; missing `drive_verified_before_stop` defaults False;
  missing `remote_upload` defaults absent. New manifests carry
  `"schema_version": 2` (missing means 1). `recovery_view` reports the
  artifact axis and `drive_verified_before_stop` explicitly.

No historical manifest is rewritten. No stage order is faked: a
manifest that reached DRIVE_VERIFIED before GPU_STOP_CONFIRMED says so
in `drive_verified_before_stop` and in the audit log.

## Worker-side upload (`h3_drive_upload.py`, stdlib-only)

Mirrors `h3_drive_download.py`: urllib only, module-level `urlopen`
for test patching, no token value in any message.

- `DriveUploader`: resumable upload protocol (POST initiate →
  Location session URI; PUT chunks with Content-Range; 308 → query
  offset; 200/201 → done). Session URI + byte offset persist in a
  0600 sidecar `<mp4>.upload_state.json` on the REMOTE disk, so a
  worker restart resumes the same session (no duplicate file). 404/410
  on the session URI means expired: reconcile first (list the folder
  for the run-id file) before starting a new session.
- Identity: the uploaded file carries `appProperties: {run_id,
  variant}`. Before initiating, the uploader lists the target folder
  for a file with the same name + run_id: found with matching SHA-256
  → reuse (idempotent recovery); found with DIFFERENT SHA-256 → error,
  never overwrite (same rule as `drive_depot.upload_one`).
- Validation before upload (in the worker, stdlib + ffprobe):
  `ffprobe` on the assembled MP4: exactly 274 frames, 24 fps,
  1080x1920, one decoded frame, then full SHA-256. A failed validation
  is a worker failure, never an upload of a bad file.
- Verification after upload (worker): `files.get` metadata (size +
  MD5 + SHA-256 when Drive reports it + parent + not trashed) AND an
  independent full read-back + SHA-256 (outputs are ~7–10 MB; if a
  future output exceeds 100 MB the read-back switches to sampled
  Range chunks, but that switch is a separate decision, not silent).
  Upload API success alone is never treated as integrity.
- Token: a SECOND short-lived token with `drive.file` scope (least
  privilege — the worker only ever touches files the app created),
  minted by `h3_drive_token.mint_file_token()` and delivered as a
  separate 0600 file `drive_upload_token.json`. Renewal reuses the
  PR-2 channel: job-state `token_refresh_requested` with a `scope:
  "drive.file"` marker, served by the controller's poll loop (the
  renewal handler mints the matching scope and replaces the matching
  remote file). `drive.file` scope + the controller pre-creating the
  run folder with the full-scope service is TESTABLE_HYPOTHESIS until
  a live run proves the worker can write into that folder (same OAuth
  client created it, so it should be accessible — if live verification
  refutes this, the fallback is a short-lived full-`drive` token for
  the single run, recorded as a decision, not silently substituted).
- GPU cost: upload happens on the GPU runtime and burns CU. The worker
  records `upload_started_at_utc` / `upload_finished_at_utc` in the
  job state; the controller records the derived `upload_wall_seconds`
  in the manifest artifacts. The CU cost is wall_seconds x live
  session rate / 3600; the rate lives in the ledger/live inspect, so
  the CU attribution is derived by the operator from those two
  numbers. The budget check still happens at the controller (the
  worker never decides budget).
- Failure modes (worker): upload failure → job state stage
  `REMOTE_UPLOAD_FAILED` with error type; the worker still completes
  with VIDEO_READY (the MP4 is valid locally) and the controller falls
  back to its own download + `deliver_variant_to_drive_idempotent`
  path. Never re-run inference because an upload failed.

## Controller changes (`h3_controller.py`)

- Pre-worker (after the download probe, before the worker starts):
  `_build_drive_upload_block` creates the run folder
  `Muse-H3-Video-Outputs/<RUN_ID>/` with the full-scope service and
  returns `{folder_id, file_name, run_id, variant}`; the controller
  mints + uploads the `drive.file` token as `drive_upload_token.json`
  and uploads `h3_drive_upload.py`. Any failure here is fail-soft:
  the worker gets no `drive_upload` block and the run falls back to
  controller-side delivery (no GPU time is wasted on a doomed upload
  setup, and the run is not aborted for a Drive-side hiccup).
- `task.json` gains `"drive_upload"`. The worker reads it in `main()`
  exactly like `drive_download`.
- After the worker reports VIDEO_READY: `_record_remote_upload`
  records the worker's `remote_upload` evidence from the final job
  state into the manifest (delivery_state REMOTE_UPLOAD_VERIFIED,
  remote_upload evidence, drive_file_id,
  drive_verified_before_stop=True — the session is still ACTIVE at
  that point) and a `remote_upload_verified` audit event. The tail's
  `deliver_variant_to_drive_idempotent` then reuses the recorded id
  only after its own server-side verify against the locally validated
  MP4 — the worker upload never skips the controller's independent
  verification. The controller's own download + local validation still
  run (the local MP4 is the second copy and the QA input).
- If the worker upload is absent/failed: the existing fallback runs —
  controller downloads the MP4 and calls
  `deliver_variant_to_drive_idempotent` (archive-only retry, never
  re-inference). The tail's delivery evidence records which path
  delivered the file: `reused_existing: True, reused_via:
  "recorded_id"` when the worker's upload was reused, a fresh upload
  otherwise.
- `_resume_finish` records the same evidence from a resumed worker
  state before its tail.
- `_resume_stopped_session` recovery ladder, in order:
  1. Validated local MP4 → archive it (as before).
  2. Manifest-recorded Drive file id → server-side verify, now also
     comparing size + MD5 against the worker's `remote_upload`
     evidence when present (an id alone is not proof the right bytes
     are there).
  3. No local MP4 and no verifiable Drive file → FAILED. When a worker
     upload was in flight (or verified), `artifacts.artifact_lost`
     is set: the runtime is gone, the remote temp disk is gone with
     it, and the task counts as a failure (P1-C denominator rule).
     Never re-infer silently; a new inference needs a new explicit
     authorization.
- `stop_and_verify_session` unchanged (it already marks only on
  confirmed STOPPED). TASK_COMPLETED is only recorded after both
  DRIVE_VERIFIED and GPU_STOP_CONFIRMED; the resume paths that
  complete a run from a known-STOPPED session now record
  `session_state="STOPPED"` with the DRIVE_VERIFIED move so the new
  gate can pass.

## Simulator scenarios (tests, all offline with fakes)

`tests/test_drive_upload.py` (FakeUploadDrive scripting the resumable
protocol: initiate / 308 / offset query / 200 / 401 / 403 / 429 / 5xx /
mid-chunk drop / session expired 404 / changed metadata) and
`tests/test_run_manifest.py` (MigrationTest) plus the resume-ladder
test in `tests/test_recoverable_controller.py`:

- Muse restart mid-inference: worker job state INFERENCE_*; controller
  resume re-attaches, no duplicate GPU, no re-inference (existing
  resume tests; unchanged by this PR).
- Muse restart after MP4 written, before upload: worker job state
  VIDEO_READY, no remote_upload; controller downloads + delivers
  (existing `_resume_finish` path; the fallback is the absence of
  evidence).
- Muse restart during remote upload: sidecar session URI resumes;
  exactly one Drive file, correct SHA
  (`test_sidecar_resume_across_uploader_instances`).
- Colab reclaimed after generation before upload: remote disk gone;
  manifest FAILED + artifact_lost, no silent re-inference
  (`test_stopped_after_remote_upload_started_marks_artifact_lost`).
- Partial upload success: 308 offset honored, resume completes
  (`test_mid_chunk_drop_resumes_from_offset`).
- Token expired before upload: 401 → refresh once via the renewal
  channel → success; second 401 → DriveAuthError
  (`test_401_refreshes_once_then_succeeds`,
  `test_second_401_fails_closed`).
- Upload succeeded but response lost: the retry's offset query finds
  the upload complete, reconcile finds the run-id file with matching
  SHA → reused, no duplicate
  (`test_response_lost_after_final_chunk_recovers_via_reconcile`).
- Duplicate recovery of the same run_id: second delivery attempt
  reuses the recorded id after server verify
  (`test_reconcile_reuses_matching_file_without_initiate` + the
  existing idempotent-deliver tests).
- Same-name different-SHA file in Drive: error, never overwrite
  (`test_same_name_different_sha_is_conflict`).
- GPU stopped but Drive unverified: task stays
  COMPLETED_AWAITING_ARCHIVE; archive-only retry (existing tests).
- Drive verified but GPU state UNKNOWN: TASK_COMPLETED refused
  (session_state != STOPPED)
  (`test_completed_refused_while_session_active`).

## Deliberate non-changes

- No change to story/prompt/image/resolution/steps/quality; no change
  to the inference path itself.
- No live GPU, no live Drive write, no live cron in this PR.
- `deliver_variant_to_drive_idempotent` and `upload_one` semantics
  unchanged (the controller fallback reuses them verbatim).
- `h3_drive_download.py` / token-renewal channel unchanged except the
  `scope` marker on the renewal request (additive).
- The `drive.file` write-into-pre-created-folder assumption is
  TESTABLE_HYPOTHESIS (see above), not a verified fact.
