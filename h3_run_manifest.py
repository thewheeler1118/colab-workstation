"""Durable run manifest (TASK-005 §4.1, Issue #26).

Every production run owns a persistent record that survives a controller
restart, so a new controller process can recover the task view instead of
guessing from log text. The manifest lives under
model_cache/run_manifests/ (gitignored local persistent store, never on
a Colab temp disk, never in git).

Schema (one JSON file per run_id, atomically written):

    production_task_id, run_id, variant, code_commit, workflow_sha256,
    model_manifest_sha256, session_id, session_state (ACTIVE/STOPPED/
    UNKNOWN), task_state, last_heartbeat_utc, last_verified_stage,
    last_error, artifacts, budget, recovery_generation, created_at_utc,
    updated_at_utc

Rules:
- Atomic writes (temp file + os.replace + fsync).
- Idempotent updates: re-applying the same state is a no-op; a backward
  stage move is recorded as an audit anomaly and ignored, never applied.
- Append-only audit log (<run_id>.audit.jsonl): one JSON object per line,
  never rewritten. Important events (creation, stage changes, errors,
  anomalies, resumes) are appended here.
- Task state and GPU session state are modelled separately.
- Terminal states: TASK_COMPLETED, FAILED, COMPLETED_AWAITING_ARCHIVE.
  A terminal run never moves again except through resume_run() + an
  explicit reopen (PR-4).
- No stage is ever inferred from log text: task_state only moves when
  the controller (or, later, the worker via PR-4) reports it with
  evidence; last_verified_stage tracks the highest stage with evidence.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path

TASK_STAGES = [
    "PREFLIGHT",
    "TASK_AUTHORIZED",
    "GPU_ALLOCATED",
    "WORKER_STARTED",
    "MODELS_READY",
    "INFERENCE_COMPLETED",
    "VIDEO_VALIDATED",
    # ARTIFACT_DURABLE means the MP4 is safe on Muse-controlled persistent
    # storage (the VM's output dir), which happens at VIDEO_VALIDATED —
    # before the GPU stop. The Drive upload (DRIVE_VERIFIED) happens only
    # after the stop is confirmed. This matches the §8 chain order and the
    # real run_variant flow (TASK-002 §4.5 / TASK-003 §1).
    "ARTIFACT_DURABLE",
    "GPU_STOP_CONFIRMED",
    "DRIVE_VERIFIED",
    "TASK_COMPLETED",
]
TERMINAL_STATES = {"TASK_COMPLETED", "FAILED", "COMPLETED_AWAITING_ARCHIVE"}
_STAGE_INDEX = {name: i for i, name in enumerate(TASK_STAGES)}
# FAILED and COMPLETED_AWAITING_ARCHIVE are terminal states outside the
# linear order; they sort after every linear stage for comparison.
_TERMINAL_INDEX = len(TASK_STAGES)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def manifest_dir(root: Path) -> Path:
    return Path(root) / "model_cache" / "run_manifests"


def _manifest_path(root: Path, run_id: str) -> Path:
    return manifest_dir(root) / f"{run_id}.json"


def _audit_path(root: Path, run_id: str) -> Path:
    return manifest_dir(root) / f"{run_id}.audit.jsonl"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".h3_manifest_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, sort_keys=True, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def record_audit_event(root: Path, run_id: str, event: str, detail: dict | None = None) -> None:
    """Append one event to the run's append-only audit log."""
    path = _audit_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts_utc": _utc_now(), "run_id": run_id, "event": event,
             "detail": detail or {}}
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        fp.flush()
        os.fsync(fp.fileno())


def read_audit_log(root: Path, run_id: str) -> list:
    path = _audit_path(root, run_id)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _stage_index(state: str) -> int:
    if state in TERMINAL_STATES:
        return _TERMINAL_INDEX
    return _STAGE_INDEX.get(state, -1)


def create_run_manifest(root: Path, production_task_id: str, run_id: str,
                       variant: str = "", code_commit: str = "",
                       workflow_sha256: str = "", model_manifest_sha256: str = "",
                       budget: dict | None = None) -> dict:
    """Create the durable record for a newly authorised task. Raises if a
    manifest already exists for this run_id (a run_id must never be
    silently re-created; resume is a separate, audited operation)."""
    path = _manifest_path(root, run_id)
    if path.exists():
        raise FileExistsError(f"run manifest already exists for {run_id}; use resume, not re-create")
    now = _utc_now()
    manifest = {
        "production_task_id": production_task_id,
        "run_id": run_id,
        "variant": variant,
        "code_commit": code_commit,
        "workflow_sha256": workflow_sha256,
        "model_manifest_sha256": model_manifest_sha256,
        "session_id": "",
        "session_state": "UNKNOWN",
        "task_state": "TASK_AUTHORIZED",
        "last_heartbeat_utc": now,
        "last_verified_stage": "TASK_AUTHORIZED",
        "last_error": None,
        "artifacts": {},
        "budget": budget or {},
        "recovery_generation": 0,
        "created_at_utc": now,
        "updated_at_utc": now,
    }
    _atomic_write_json(path, manifest)
    record_audit_event(root, run_id, "RUN_CREATED", {
        "production_task_id": production_task_id, "variant": variant,
        "code_commit": code_commit, "workflow_sha256": workflow_sha256[:16],
        "model_manifest_sha256": model_manifest_sha256[:16]})
    return manifest


def load_run_manifest(root: Path, run_id: str) -> dict | None:
    path = _manifest_path(root, run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_runs(root: Path) -> list:
    d = manifest_dir(root)
    if not d.exists():
        return []
    runs = []
    for path in sorted(d.glob("*.json")):
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    runs.sort(key=lambda m: m.get("created_at_utc", ""))
    return runs


def update_run(root: Path, run_id: str, task_state: str | None = None,
               session_state=None, session_id: str | None = None,
               last_verified_stage: str | None = None,
               last_error: str | None = None, artifacts: dict | None = None,
               budget: dict | None = None, heartbeat: bool = False,
               reopen: bool = False) -> tuple[dict, bool, str | None]:
    """Idempotent update. Returns (manifest, applied, anomaly).

    - task_state moves forward only (by stage index); a backward move is
      recorded as an audit anomaly and ignored.
    - TASK_COMPLETED requires the current state to be DRIVE_VERIFIED.
    - Terminal states never move again unless reopen=True (resume path).
    - last_verified_stage only moves forward.
    - session_state accepts a SessionState enum or its string value.
    - Re-applying the same values is a no-op (applied=False, no anomaly).
    """
    path = _manifest_path(root, run_id)
    if not path.exists():
        raise FileNotFoundError(f"no run manifest for {run_id}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    anomaly = None
    now = _utc_now()

    if task_state is not None:
        current = manifest.get("task_state", "PREFLIGHT")
        if current in TERMINAL_STATES and not reopen:
            anomaly = (f"refused move {current} -> {task_state}: run is terminal; "
                       "resume with reopen instead")
            record_audit_event(root, run_id, "ANOMALY", {"detail": anomaly})
        elif task_state == "TASK_COMPLETED" and current != "DRIVE_VERIFIED" and not reopen:
            anomaly = (f"refused move {current} -> TASK_COMPLETED: Drive must be "
                       "verified first")
            record_audit_event(root, run_id, "ANOMALY", {"detail": anomaly})
        elif _stage_index(task_state) < _stage_index(current) and not reopen:
            anomaly = f"refused backward move {current} -> {task_state}"
            record_audit_event(root, run_id, "ANOMALY", {"detail": anomaly})
        elif task_state != current:
            manifest["task_state"] = task_state
            changed = True
            record_audit_event(root, run_id, "STAGE", {"from": current, "to": task_state})

    if last_verified_stage is not None:
        current_verified = manifest.get("last_verified_stage", "PREFLIGHT")
        if _stage_index(last_verified_stage) > _stage_index(current_verified):
            manifest["last_verified_stage"] = last_verified_stage
            changed = True

    if session_state is not None:
        value = getattr(session_state, "value", session_state)
        if value in ("ACTIVE", "STOPPED", "UNKNOWN") and value != manifest.get("session_state"):
            manifest["session_state"] = value
            changed = True

    if session_id is not None and session_id != manifest.get("session_id"):
        manifest["session_id"] = session_id
        changed = True

    if last_error is not None and last_error != manifest.get("last_error"):
        manifest["last_error"] = last_error
        changed = True
        record_audit_event(root, run_id, "ERROR", {"error": str(last_error)[:500]})

    if artifacts:
        merged = {**manifest.get("artifacts", {}), **artifacts}
        if merged != manifest.get("artifacts", {}):
            manifest["artifacts"] = merged
            changed = True

    if budget:
        merged = {**manifest.get("budget", {}), **budget}
        if merged != manifest.get("budget", {}):
            manifest["budget"] = merged
            changed = True

    if heartbeat or changed:
        manifest["last_heartbeat_utc"] = now
    if changed:
        manifest["updated_at_utc"] = now
        _atomic_write_json(path, manifest)
    return manifest, changed, anomaly


def recovery_view(root: Path, run_id: str) -> dict:
    """Read-only recovery view: which stages are done, what is next, and
    whether the run is resumable. No writes (a `status` read must not
    bump recovery_generation)."""
    manifest = load_run_manifest(root, run_id)
    if manifest is None:
        raise FileNotFoundError(f"no run manifest for {run_id}")
    state = manifest.get("task_state", "PREFLIGHT")
    done = [s for s in TASK_STAGES if _stage_index(s) < _stage_index(state)]
    if state in TASK_STAGES:
        idx = _STAGE_INDEX[state]
        nxt = TASK_STAGES[idx + 1] if idx + 1 < len(TASK_STAGES) else None
    else:
        nxt = None
    resumable = state not in ("TASK_COMPLETED",)
    reason = None
    if state == "TASK_COMPLETED":
        reason = "run already completed; nothing to resume"
    elif state == "FAILED":
        reason = "run failed; resume needs an explicit operator decision, not an automatic restart"
    elif state == "COMPLETED_AWAITING_ARCHIVE":
        reason = "video is durable locally; resume means archive-only retry, never re-inference"
    return {"manifest": manifest, "resumable": resumable, "reason": reason,
            "completed_stages": done, "current_stage": state, "next_stage": nxt,
            "session_state": manifest.get("session_state"),
            "session_id": manifest.get("session_id"),
            "artifacts": manifest.get("artifacts", {})}


def resume_run(root: Path, run_id: str) -> dict:
    """Recovery view for a controller restart (PR-4 resume uses this).

    Bumps recovery_generation and records a RESUMED audit event, then
    returns the read-only recovery view.
    """
    manifest = load_run_manifest(root, run_id)
    if manifest is None:
        raise FileNotFoundError(f"no run manifest for {run_id}")
    manifest["recovery_generation"] = int(manifest.get("recovery_generation", 0)) + 1
    manifest["updated_at_utc"] = _utc_now()
    _atomic_write_json(_manifest_path(root, run_id), manifest)
    record_audit_event(root, run_id, "RESUMED",
                       {"recovery_generation": manifest["recovery_generation"]})
    return recovery_view(root, run_id)
