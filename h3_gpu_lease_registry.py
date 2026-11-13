"""Persistent GPU lease registry (TASK-006 P0-C, Issue #46).

A lease is the backup executor's authority to stop a GPU session. The
registry does NOT live only on the Muse VM filesystem: every lease is
written to a local file (the write-through cache, required) and mirrored
to a single JSON document on Drive (best-effort, retried on the next
mutation). A replacement host reads the Drive mirror first.

Privacy: the lease never stores an email address. `owner_id` is the
first 16 hex chars of SHA-256 of the casefolded Colab account email;
the executor recomputes it from its own ADC and compares hashes. The
raw email never leaves `current_owner_id()` and never appears in any
record, report, or log.

Failure posture (fail closed):
- Local registry write failure raises: an unregistered lease is
  invisible to the backup executor, so the run must not proceed.
- Mirror write failure never blocks: the lease is recorded
  `mirror_pending` and retried on the next mutation / `sync_mirror()`.
- A lease is only stoppable by the executor once its deadline is final
  (`deadline_final=True`, set by the post-probe deadline update). A
  lease whose deadline update never landed keeps the earlier initial
  deadline and is NEVER stopped by the executor — stopping on a stale
  early deadline could kill a healthy run.
- Marking stopped requires a confirmed STOPPED verdict, exactly like
  `h3_session_watchdog.mark_session_stopped` (TASK-006 P0-A).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from h3_session_watchdog import _atomic_write_json, _state_lock

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "model_cache" / "h3_gpu_leases.json"
REGISTRY_FOLDER_NAME = "Muse-H3-Lease-Registry"
REGISTRY_FILE_NAME = "h3_gpu_leases.json"
CLAIM_TTL_SEC = 15 * 60
MIRROR_MAX_ATTEMPTS = 2


def _utc_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def owner_id_for(email: str) -> str:
    """First 16 hex chars of SHA-256 of the casefolded email.

    The only owner-derived value allowed in a lease, a report, or a log.
    """
    return hashlib.sha256(email.strip().casefold().encode()).hexdigest()[:16]


def current_owner_id(colab_auth: str = "adc") -> str:
    """Hash of the Colab account email for this host's ADC.

    Runs the same hidden `colab whoami` command `account_matches`
    uses. Raises when the CLI is unavailable or the output lacks an
    email — the caller must fail closed (no stop without an owner).
    The email itself is never returned, logged, or stored.
    """
    from drive_depot import resolve_colab_executable

    exe = resolve_colab_executable()
    proc = subprocess.run([exe, "--auth=" + colab_auth, "whoami"],
                          input="", capture_output=True, text=True,
                          timeout=65, check=False)
    if proc.returncode:
        raise RuntimeError("CLI whoami unavailable or not authorized")
    mat = re.search(r"^Email:\s*([^\s]+@[^\s]+)\s*$", proc.stdout, re.I | re.M)
    if not mat:
        raise RuntimeError("CLI whoami output lacks email; refuse owner assertion")
    return owner_id_for(mat.group(1))


class MirrorConflictError(RuntimeError):
    """Mirror version moved under us; the write was refused."""


class MirrorUnavailableError(RuntimeError):
    """Mirror could not be read or written (transport / auth / Drive)."""


def _merge_docs(remote: dict, local: dict) -> dict:
    """Merge two registry docs after a mirror version conflict: per
    session id, the highest-version lease wins; the merged doc carries
    the remote's version as its mirror base so the retry's
    expected-version check passes. Local-only bookkeeping
    (mirror_pending / mirror_error) is preserved from the local doc."""
    by_id: dict[str, dict] = {}
    for lease in list(remote.get("leases") or []) + list(local.get("leases") or []):
        sid = lease.get("session_id")
        if sid is None:
            continue
        prev = by_id.get(sid)
        if prev is None or int(lease.get("version") or 0) > int(prev.get("version") or 0):
            by_id[sid] = lease
    merged = dict(local)
    merged["leases"] = [by_id[k] for k in sorted(by_id)]
    merged["mirror_version"] = remote.get("version")
    merged["mirror_file_id"] = remote.get("mirror_file_id")
    return merged


class LeaseRegistry:
    """Lease store: local file (required) + Drive mirror (best-effort).

    `mirror` is a backend with `read_mirror() -> dict | None` and
    `write_mirror(doc, expected_version) -> dict`. Pass None for
    local-only operation (tests, or hosts without Drive access).
    """

    def __init__(self, path: Path = REGISTRY_PATH, mirror=None):
        self.path = Path(path)
        self.mirror = mirror

    # -- local file ---------------------------------------------------

    def _load_local(self) -> dict:
        if not self.path.exists():
            return {"leases": [], "version": 0,
                    "updated_at_utc": _utc_now(),
                    "mirror_pending": False, "mirror_error": None,
                    "mirror_file_id": None, "mirror_version": None}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("leases"), list):
            raise RuntimeError(f"lease registry corrupt: {self.path}")
        return data

    def _save_local(self, doc: dict) -> None:
        # Every local save bumps the document version: the mirror's
        # optimistic-concurrency check keys off this number.
        doc["version"] = int(doc.get("version") or 0) + 1
        doc["updated_at_utc"] = _utc_now()
        _atomic_write_json(self.path, doc)

    def _mutate(self, fn):
        """Load-modify-save under the file lock. fn(doc, leases) mutates
        the lease list in place and returns the affected lease (or None).
        A mutation that changes nothing writes nothing: the file is not
        touched and the document version does not move. Returns
        (doc, lease)."""
        with _state_lock(self.path):
            doc = self._load_local()
            before = json.dumps(doc, sort_keys=True)
            leases = doc["leases"]
            lease = fn(doc, leases)
            if json.dumps(doc, sort_keys=True) != before:
                self._save_local(doc)
        return doc, lease

    # -- mirror -------------------------------------------------------

    def _mirror_write_best_effort(self, doc: dict) -> dict:
        """Attempt the mirror write. On failure the doc is marked
        mirror_pending (local file already saved) and the failure is
        returned, never raised. On a version conflict the mirror is
        re-read, merged per-lease (highest version wins), and the write
        is retried once — a concurrent writer's leases are never
        silently overwritten."""
        if self.mirror is None:
            return doc
        for attempt in range(MIRROR_MAX_ATTEMPTS):
            try:
                written = self.mirror.write_mirror(
                    doc, doc.get("mirror_version"))
                doc["mirror_pending"] = False
                doc["mirror_error"] = None
                doc["mirror_file_id"] = written.get("mirror_file_id")
                doc["mirror_version"] = written.get("version")
                with _state_lock(self.path):
                    saved = self._load_local()
                    # Merge, never replace: a concurrent local writer
                    # (or the conflict merge above) may hold a newer
                    # record for a session this doc does not know.
                    saved["leases"] = _merge_docs(
                        {"leases": saved["leases"]},
                        {"leases": doc["leases"]})["leases"]
                    saved["mirror_pending"] = False
                    saved["mirror_error"] = None
                    saved["mirror_file_id"] = doc["mirror_file_id"]
                    saved["mirror_version"] = doc["mirror_version"]
                    self._save_local(saved)
                return doc
            except MirrorConflictError:
                try:
                    current = self.mirror.read_mirror()
                except Exception:  # noqa: BLE001 - fall through to pending
                    current = None
                if not isinstance(current, dict) or attempt + 1 >= MIRROR_MAX_ATTEMPTS:
                    break
                doc = _merge_docs(current, doc)
                continue
            except Exception as e:  # noqa: BLE001 - mirror is best-effort
                doc["mirror_pending"] = True
                doc["mirror_error"] = type(e).__name__
                with _state_lock(self.path):
                    saved = self._load_local()
                    saved["mirror_pending"] = True
                    saved["mirror_error"] = doc["mirror_error"]
                    self._save_local(saved)
                return doc
        doc["mirror_pending"] = True
        doc["mirror_error"] = "MirrorConflictError"
        with _state_lock(self.path):
            saved = self._load_local()
            saved["mirror_pending"] = True
            saved["mirror_error"] = doc["mirror_error"]
            self._save_local(saved)
        return doc

    def sync_mirror(self) -> dict:
        """Retry a pending mirror write. Returns the local doc."""
        with _state_lock(self.path):
            doc = self._load_local()
        if not doc.get("mirror_pending") and self.mirror is None:
            return doc
        return self._mirror_write_best_effort(doc)

    # -- leases -------------------------------------------------------

    def register_lease(self, session_id: str, run_id: str,
                       production_task_id: str, gpu_model: str,
                       stop_deadline_utc: str, budget_cap_cu: float,
                       owner_id: str, started_at_utc: str | None = None) -> dict:
        """Create (or re-create) a lease. The local write is REQUIRED
        and raises on failure; the mirror write is best-effort. The
        initial deadline is NOT final: the executor never stops a lease
        until the post-probe deadline update marks it final."""
        def _reg(doc, leases):
            old = next((x for x in leases
                        if x.get("session_id") == session_id), None)
            leases[:] = [x for x in leases
                         if x.get("session_id") != session_id]
            lease = {
                "session_id": session_id,
                "run_id": run_id,
                "production_task_id": production_task_id,
                "owner_id": owner_id,
                "gpu_model": gpu_model,
                "started_at_utc": started_at_utc or _utc_now(),
                "stop_deadline_utc": stop_deadline_utc,
                "deadline_final": False,
                "budget_cap_cu": float(budget_cap_cu),
                "last_confirmed_state": "ACTIVE",
                "last_audit_at_utc": _utc_now(),
                "version": (int(old.get("version") or 0) + 1) if old else 1,
                "executor_claim": None,
                "stop_attempts": 0,
                "last_stop_error": None,
                "next_retry_at_utc": None,
            }
            leases.append(lease)
            return lease

        doc, lease = self._mutate(_reg)
        self._mirror_write_best_effort(doc)
        return lease

    def update_deadline(self, session_id: str, stop_deadline_utc: str,
                        final: bool = True) -> dict | None:
        """Replace a lease's stop deadline and mark it final (default).
        Only an ACTIVE lease is updated. Returns the lease, or None when
        not found / no longer active. Raises on local-write failure."""
        def _upd(doc, leases):
            lease = next((x for x in leases
                          if x.get("session_id") == session_id), None)
            if lease is None or lease.get("last_confirmed_state") != "ACTIVE":
                return None
            lease["stop_deadline_utc"] = stop_deadline_utc
            lease["deadline_final"] = bool(final)
            lease["last_audit_at_utc"] = _utc_now()
            lease["version"] = int(lease.get("version") or 0) + 1
            return lease

        doc, lease = self._mutate(_upd)
        if lease is not None:
            self._mirror_write_best_effort(doc)
        return lease

    def confirm_stopped(self, session_id: str, confirmed_state) -> bool:
        """Mark a lease stopped — ONLY on a confirmed STOPPED verdict.
        `confirmed_state` must be the string "STOPPED" (or an enum with
        that value); anything else raises ValueError. Returns True when
        the lease was found (and is now stopped), False when absent.
        Marking an already-stopped lease is a no-op True."""
        from h3_session_watchdog import SessionState

        value = (confirmed_state.value
                 if isinstance(confirmed_state, SessionState)
                 else str(confirmed_state))
        if value != SessionState.STOPPED.value:
            raise ValueError(
                "confirm_stopped requires a confirmed STOPPED state, "
                f"got {value!r}; refusing to mark the lease stopped")

        def _mark(doc, leases):
            lease = next((x for x in leases
                          if x.get("session_id") == session_id), None)
            if lease is None:
                return None
            if lease.get("last_confirmed_state") != "STOPPED":
                lease["last_confirmed_state"] = "STOPPED"
                lease["stopped_at_utc"] = _utc_now()
                lease["executor_claim"] = None
                lease["version"] = int(lease.get("version") or 0) + 1
            lease["last_audit_at_utc"] = _utc_now()
            return lease

        doc, lease = self._mutate(_mark)
        if lease is not None:
            self._mirror_write_best_effort(doc)
        return lease is not None

    def note_stop_attempt(self, session_id: str, error_type: str | None,
                          retry_after_sec: int = 300) -> dict | None:
        """Record a failed / unconfirmed stop attempt: the lease stays
        pending, attempts +1, next retry 5 min out. Returns the lease
        or None when absent."""
        import datetime

        def _note(doc, leases):
            lease = next((x for x in leases
                          if x.get("session_id") == session_id), None)
            if lease is None:
                return None
            lease["stop_attempts"] = int(lease.get("stop_attempts") or 0) + 1
            lease["last_stop_error"] = error_type
            lease["next_retry_at_utc"] = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=retry_after_sec)).isoformat()
            lease["last_audit_at_utc"] = _utc_now()
            lease["version"] = int(lease.get("version") or 0) + 1
            return lease

        doc, lease = self._mutate(_note)
        if lease is not None:
            self._mirror_write_best_effort(doc)
        return lease

    def claim_lease(self, session_id: str, executor_id: str) -> dict | None:
        """Claim a lease for one executor. A fresh claim (younger than
        CLAIM_TTL_SEC) held by another executor refuses the claim
        (returns None); a stale claim may be taken over. Returns the
        claimed lease or None."""
        import datetime

        def _claim(doc, leases):
            lease = next((x for x in leases
                          if x.get("session_id") == session_id), None)
            if lease is None:
                return None
            claim = lease.get("executor_claim") or {}
            holder = claim.get("executor_id")
            if holder and holder != executor_id:
                try:
                    claimed_at = datetime.datetime.fromisoformat(
                        str(claim.get("claimed_at_utc", "")).replace("Z", "+00:00"))
                except ValueError:
                    claimed_at = None
                now = datetime.datetime.now(datetime.timezone.utc)
                if claimed_at is not None and (
                        now - claimed_at).total_seconds() < CLAIM_TTL_SEC:
                    return None
            lease["executor_claim"] = {
                "executor_id": executor_id, "claimed_at_utc": _utc_now()}
            lease["version"] = int(lease.get("version") or 0) + 1
            return lease

        _doc, lease = self._mutate(_claim)
        return lease

    def release_claim(self, session_id: str, executor_id: str) -> None:
        """Best-effort claim release (never raises; a stale claim ages
        out via CLAIM_TTL_SEC anyway)."""
        def _rel(doc, leases):
            lease = next((x for x in leases
                          if x.get("session_id") == session_id), None)
            if lease is None:
                return None
            claim = lease.get("executor_claim") or {}
            if claim.get("executor_id") == executor_id:
                lease["executor_claim"] = None
                lease["version"] = int(lease.get("version") or 0) + 1
            return lease

        try:
            self._mutate(_rel)
        except Exception:  # noqa: BLE001 - best-effort only
            pass

    # -- reads --------------------------------------------------------

    def list_leases(self) -> list:
        with _state_lock(self.path):
            return list(self._load_local()["leases"])

    def get_lease(self, session_id: str) -> dict | None:
        return next((x for x in self.list_leases()
                     if x.get("session_id") == session_id), None)

    def load_for_executor(self) -> tuple[list, str, bool]:
        """Mirror-first read for the backup executor. Returns
        (leases, source, degraded): source is "mirror" or "local";
        degraded is True when the mirror could not be read and the
        local cache was used instead. When the mirror is newer than
        local, local is refreshed from it."""
        if self.mirror is not None:
            try:
                remote = self.mirror.read_mirror()
            except Exception:  # noqa: BLE001 - degraded fallback below
                remote = None
            if isinstance(remote, dict) and isinstance(
                    remote.get("leases"), list):
                with _state_lock(self.path):
                    local = self._load_local()
                    if int(remote.get("version") or 0) > int(
                            local.get("mirror_version") or 0):
                        local["leases"] = remote["leases"]
                        local["mirror_version"] = remote.get("version")
                        local["mirror_file_id"] = remote.get("mirror_file_id")
                        local["mirror_pending"] = False
                        self._save_local(local)
                return list(remote["leases"]), "mirror", False
        return self.list_leases(), "local", self.mirror is not None


def import_managed_sessions(path=None) -> list:
    """Migration path: read the watchdog's managed-session file and
    return lease-shaped dicts (read-only — nothing is written). Fields
    the managed file lacks (owner, budget cap, final deadline) are left
    absent so callers can see the gap instead of inventing values."""
    from h3_session_watchdog import STATE_PATH, load_managed_sessions

    sessions = load_managed_sessions(path or STATE_PATH)
    out = []
    for s in sessions:
        out.append({
            "session_id": s.get("session_id"),
            "run_id": s.get("run_id"),
            "production_task_id": s.get("production_task_id", ""),
            "owner_id": None,
            "gpu_model": s.get("gpu"),
            "started_at_utc": s.get("started_at_utc"),
            "stop_deadline_utc": s.get("deadline_utc"),
            "deadline_final": False,
            "budget_cap_cu": None,
            "last_confirmed_state": (
                "STOPPED" if s.get("status") == "stopped" else "ACTIVE"),
            "version": int(s.get("version") or 0),
        })
    return out


class DriveMirrorBackend:
    """Drive mirror for the lease registry: one JSON document in the
    dedicated `Muse-H3-Lease-Registry` folder (never the model depot
    root). google libs are imported lazily inside the methods, so this
    module imports on hosts without them; the methods raise
    MirrorUnavailableError there instead."""

    def __init__(self, service=None):
        self._service = service
        self._folder_id: str | None = None

    def _service_or_raise(self):
        if self._service is not None:
            return self._service
        from drive_depot import drive_service

        self._service = drive_service()
        return self._service

    def _folder(self, service) -> str:
        if self._folder_id:
            return self._folder_id
        from drive_depot import ensure_folder

        self._folder_id = ensure_folder(
            service, None, REGISTRY_FOLDER_NAME, allow_write=True)
        return self._folder_id

    def _find_file(self, service, folder_id: str) -> dict | None:
        from drive_depot import list_named

        found = list_named(service, folder_id, REGISTRY_FILE_NAME)
        if len(found) > 1:
            raise MirrorUnavailableError(
                "duplicate lease registry files on Drive; refuse ambiguity")
        return found[0] if found else None

    def read_mirror(self) -> dict | None:
        try:
            service = self._service_or_raise()
            folder_id = self._folder(service)
            found = self._find_file(service, folder_id)
            if found is None:
                return None
            raw = service.files().get_media(
                fileId=found["id"]).execute()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            doc = json.loads(raw)
            if not isinstance(doc, dict) or not isinstance(
                    doc.get("leases"), list):
                raise MirrorUnavailableError(
                    "lease registry mirror is not a lease document")
            doc["mirror_file_id"] = found["id"]
            return doc
        except MirrorUnavailableError:
            raise
        except Exception as e:  # noqa: BLE001 - wrapped for callers
            raise MirrorUnavailableError(f"mirror read failed: {type(e).__name__}") from e

    def write_mirror(self, doc: dict, expected_version) -> dict:
        """Write the doc, conditional on the mirror's current version.
        Raises MirrorConflictError when the mirror moved under us.
        Returns {"version", "mirror_file_id"} of the written doc."""
        try:
            from googleapiclient.http import MediaFileUpload

            service = self._service_or_raise()
            folder_id = self._folder(service)
            found = self._find_file(service, folder_id)
            current_version = None
            file_id = None
            if found is not None:
                raw = service.files().get_media(
                    fileId=found["id"]).execute()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                current = json.loads(raw)
                current_version = current.get("version")
                file_id = found["id"]
            if expected_version is not None and current_version is not None \
                    and int(current_version) != int(expected_version):
                raise MirrorConflictError(
                    f"mirror version {current_version} != expected "
                    f"{expected_version}")
            new_version = max(int(doc.get("version") or 0),
                              int(current_version or 0)) + 1
            payload = dict(doc)
            payload["version"] = new_version
            payload["mirror_written_at_utc"] = _utc_now()
            fd, tmp_name = tempfile.mkstemp(
                prefix=".h3_lease_mirror_", suffix=".json",
                dir=str(REGISTRY_PATH.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(payload, fp, sort_keys=True, indent=2)
                media = MediaFileUpload(tmp_name, mimetype="application/json",
                                        resumable=False)
                if file_id is not None:
                    out = service.files().update(
                        fileId=file_id, media_body=media,
                        fields="id", supportsAllDrives=True).execute()
                else:
                    out = service.files().create(
                        body={"name": REGISTRY_FILE_NAME,
                              "parents": [folder_id]},
                        media_body=media, fields="id",
                        supportsAllDrives=True).execute()
                    file_id = out["id"]
            finally:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            # Verify what the server now holds is what we wrote.
            verify = self.read_mirror()
            if verify is None or int(verify.get("version") or 0) != new_version:
                raise MirrorUnavailableError(
                    "mirror verify after write failed")
            return {"version": new_version, "mirror_file_id": file_id}
        except (MirrorConflictError, MirrorUnavailableError):
            raise
        except Exception as e:  # noqa: BLE001 - wrapped for callers
            raise MirrorUnavailableError(
                f"mirror write failed: {type(e).__name__}") from e
