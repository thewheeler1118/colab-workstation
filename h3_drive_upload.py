"""Noninteractive Drive resumable upload for the H3 worker (TASK-006
P1-A, Issue #48). Stdlib-only (urllib), so the remote worker can import
it with no google libs — the same constraint as h3_drive_download.py.

Protocol: Drive v3 resumable upload (POST initiate -> Location session
URI; PUT chunks with Content-Range; 308 -> query offset; 200/201 ->
done). The session URI + byte offset persist in a 0600 sidecar on the
REMOTE disk, so a worker restart resumes the same session instead of
creating a duplicate file.

Identity: the uploaded file carries appProperties {run_id, variant}.
Before initiating, the uploader lists the target folder for a file
with the same name + run_id: found with matching SHA-256 -> reuse
(idempotent recovery); found with DIFFERENT SHA-256 -> error, never
overwrite (same rule as drive_depot.upload_one).

Rules:
- No token value ever appears in an exception message or return value.
  Only token_id (first 16 hex chars of SHA-256(token)) may appear in
  diagnostics.
- Upload API success alone is never treated as integrity: the caller
  must verify via verify_uploaded() (metadata + full read-back SHA-256).
- A 401 refreshes the token once per upload attempt; a second 401 (or
  a 403) fails closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from h3_drive_download import (
    TOKEN_REFRESH_MARGIN_SEC,
    DriveAuthError,
    DriveDownloadError,
    FileTokenProvider,
    TokenUnavailable,
    token_id_for,
)

urlopen = urllib.request.urlopen

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # multiple of 256 KiB (Drive requirement)
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE_SEC = 2.0
DEFAULT_TIMEOUT_SEC = 300
TRANSIENT_STATUSES = (429, 500, 502, 503, 504)


class DriveUploadError(RuntimeError):
    """Base upload failure."""


class DriveConflictError(DriveUploadError):
    """A same-name, same-run file exists with different content."""


class DriveUploadRetryExhaustedError(DriveUploadError):
    """Transient failures exhausted max_attempts; the sidecar is kept
    so a later run can resume the same session."""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json_0600(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = None, None
    import tempfile

    fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(doc, fp, sort_keys=True)
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


class DriveUploader:
    """Resumable Drive uploader over a short-lived token provider."""

    def __init__(self, token_provider, chunk_size: int = DEFAULT_CHUNK_SIZE,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 backoff_base_sec: float = DEFAULT_BACKOFF_BASE_SEC,
                 timeout_sec: int = DEFAULT_TIMEOUT_SEC):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if chunk_size % (256 * 1024) != 0:
            raise ValueError("chunk_size must be a multiple of 256 KiB")
        self.token_provider = token_provider
        self.chunk_size = chunk_size
        self.max_attempts = max_attempts
        self.backoff_base_sec = backoff_base_sec
        self.timeout_sec = timeout_sec

    # -- low-level ----------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        return self.backoff_base_sec * (2 ** (attempt - 1)) + random.uniform(0, 1)

    def _headers(self) -> dict:
        return {"Authorization": "Bearer " + self.token_provider.get_token()}

    def _do(self, request) -> object:
        return urlopen(request, timeout=self.timeout_sec)

    # -- reconcile ----------------------------------------------------

    def list_candidates(self, folder_id: str, name: str) -> list:
        """Files in the folder with this exact name (not trashed)."""
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = (f"'{folder_id}' in parents and name = '{safe}' "
             f"and trashed = false")
        url = (f"{DRIVE_API_BASE}/files?q={urllib.request.quote(q)}"
               f"&spaces=drive&pageSize=100&fields=files(id,name,size,"
               f"md5Checksum,sha256Checksum,appProperties)"
               f"&supportsAllDrives=true&includeItemsFromAllDrives=true")
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self._do(req) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise DriveAuthError("Drive list unauthorized") from e
            if e.code == 403:
                raise DriveAuthError("Drive list forbidden") from e
            raise DriveUploadError(f"Drive list failed: HTTP {e.code}") from e
        return body.get("files", [])

    def reconcile_existing(self, folder_id: str, name: str, run_id: str,
                           size: int, sha256: str) -> dict | None:
        """Return the existing file evidence when a same-name file for
        this run_id already holds the exact bytes; None when no such
        file exists. Raises DriveConflictError when the run's file
        exists with different content."""
        for f in self.list_candidates(folder_id, name):
            props = f.get("appProperties") or {}
            if props.get("run_id") != run_id:
                continue
            if int(f.get("size", -1)) == int(size) and \
                    str(f.get("sha256Checksum", "")).lower() == sha256.lower():
                return {"drive_file_id": f["id"], "size": int(f["size"]),
                        "sha256": sha256, "reused": True,
                        "reused_via": "reconcile_existing"}
            raise DriveConflictError(
                f"Drive file {name} for run {run_id} exists with different "
                f"content (id {f.get('id')}); refusing to overwrite")
        return None

    # -- resumable session ---------------------------------------------

    def initiate(self, folder_id: str, name: str, run_id: str,
                 variant: str, size: int, mime_type: str = "video/mp4") -> str:
        """Start a resumable session. Returns the session URI (a
        bearer-like capability: never logged)."""
        metadata = {"name": name, "parents": [folder_id],
                    "mimeType": mime_type,
                    "appProperties": {"run_id": run_id, "variant": variant}}
        body = json.dumps(metadata).encode("utf-8")
        headers = dict(self._headers())
        headers.update({
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(size),
        })
        req = urllib.request.Request(
            UPLOAD_BASE + "?uploadType=resumable", data=body,
            headers=headers, method="POST")
        try:
            with self._do(req) as resp:
                location = resp.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise DriveAuthError("Drive initiate unauthorized") from e
            if e.code == 403:
                raise DriveAuthError("Drive initiate forbidden") from e
            raise DriveUploadError(
                f"Drive initiate failed: HTTP {e.code}") from e
        if not location:
            raise DriveUploadError("Drive initiate returned no session URI")
        return location

    def query_offset(self, session_uri: str, total: int) -> int:
        """Ask the server how many bytes it holds (0 when nothing). A
        200/201 here means the upload already completed: the caller
        must reconcile instead of re-uploading."""
        headers = dict(self._headers())
        headers["Content-Length"] = "0"
        headers["Content-Range"] = f"bytes */{total}"
        req = urllib.request.Request(session_uri, data=b"", headers=headers,
                                     method="PUT")
        try:
            with self._do(req) as resp:
                if resp.status in (200, 201):
                    return total
                raise DriveUploadError(
                    f"Drive offset query returned HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            if e.code == 308:
                rng = e.headers.get("Range", "")
                m = re.fullmatch(r"bytes=0-(\d+)", rng or "")
                if not m:
                    return 0
                offset = int(m.group(1)) + 1
                if offset > total:
                    raise DriveUploadError("Drive offset beyond source length")
                return offset
            if e.code in (404, 410):
                raise DriveUploadError("Drive session expired (404/410)") from e
            if e.code == 401:
                raise DriveAuthError("Drive offset query unauthorized") from e
            if e.code == 403:
                raise DriveAuthError("Drive offset query forbidden") from e
            raise DriveUploadError(
                f"Drive offset query failed: HTTP {e.code}") from e

    def _put_chunk(self, session_uri: str, data: bytes, start: int,
                   total: int):
        """PUT one chunk. Returns (status, body_bytes). Raises HTTPError
        for the caller to classify (308 is success-with-more-to-send);
        a 404/410 is translated to the session-expired DriveUploadError
        the attempt loop reconciles, matching query_offset."""
        end = start + len(data) - 1
        headers = dict(self._headers())
        headers["Content-Length"] = str(len(data))
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        req = urllib.request.Request(session_uri, data=data, headers=headers,
                                     method="PUT")
        try:
            with self._do(req) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 308:
                raise
            if e.code in (404, 410):
                raise DriveUploadError(
                    "Drive session expired (404/410)") from e
            raise

    # -- high-level ---------------------------------------------------

    def upload_file(self, local_path, folder_id: str, name: str,
                    run_id: str, variant: str, expected_sha256: str,
                    sidecar_path, on_progress=None,
                    on_token_expiring=None) -> dict:
        """Upload one file resumably with identity reconcile + sidecar
        resume. Returns evidence {drive_file_id, size, sha256, reused,
        reused_via}. The sidecar is deleted on success and kept on
        failure so a later run resumes the same session."""
        local_path = Path(local_path)
        sidecar_path = Path(sidecar_path)
        if not local_path.is_file():
            raise DriveUploadError(f"missing local file: {local_path}")
        size = local_path.stat().st_size
        if size <= 0:
            raise DriveUploadError("empty local file")
        sha256 = _sha256_file(local_path)
        if sha256.lower() != expected_sha256.lower():
            raise DriveUploadError("local SHA-256 does not match expected")

        reused = self.reconcile_existing(folder_id, name, run_id, size, sha256)
        if reused is not None:
            return reused

        sidecar = self._load_sidecar(sidecar_path, name, folder_id,
                                     run_id, size, sha256)
        session_uri = (sidecar or {}).get("session_uri")
        uploaded = int((sidecar or {}).get("uploaded_bytes") or 0)
        refreshed_once = False

        with open(local_path, "rb") as fp:
            for attempt in range(1, self.max_attempts + 1):
                # Token-expiry signal: let the caller request a renewal
                # while the current token still has life left (same
                # proactive pattern as the downloader).
                if on_token_expiring is not None:
                    remaining = None
                    seconds_remaining = getattr(
                        self.token_provider, "seconds_remaining", None)
                    if callable(seconds_remaining):
                        remaining = seconds_remaining()
                    if remaining is not None and remaining < TOKEN_REFRESH_MARGIN_SEC:
                        token_id = getattr(self.token_provider, "token_id", None)
                        on_token_expiring(token_id, remaining)
                try:
                    if session_uri is None:
                        session_uri = self.initiate(
                            folder_id, name, run_id, variant, size)
                        uploaded = 0
                        self._save_sidecar(sidecar_path, {
                            "session_uri": session_uri, "name": name,
                            "parent_id": folder_id, "run_id": run_id,
                            "variant": variant, "size": size,
                            "sha256": sha256, "uploaded_bytes": 0})
                    if uploaded > 0:
                        # Confirm the server agrees with the sidecar
                        # offset before sending more bytes.
                        uploaded = self.query_offset(session_uri, size)
                    while uploaded < size:
                        fp.seek(uploaded)
                        chunk = fp.read(self.chunk_size)
                        if not chunk:
                            break
                        try:
                            status, body = self._put_chunk(
                                session_uri, chunk, uploaded, size)
                        except urllib.error.HTTPError as e:
                            if e.code == 308:
                                rng = e.headers.get("Range", "")
                                m = re.fullmatch(r"bytes=0-(\d+)", rng or "")
                                if not m:
                                    raise DriveUploadError(
                                        "Drive 308 without a Range header; "
                                        "cannot determine the resume offset") from e
                                uploaded = int(m.group(1)) + 1
                                self._save_sidecar(sidecar_path, {
                                    "session_uri": session_uri, "name": name,
                                    "parent_id": folder_id, "run_id": run_id,
                                    "variant": variant, "size": size,
                                    "sha256": sha256,
                                    "uploaded_bytes": uploaded})
                                if on_progress is not None:
                                    on_progress(uploaded, size)
                                continue
                            raise
                        if status in (200, 201):
                            meta = json.loads(body.decode("utf-8"))
                            file_id = meta.get("id")
                            if not file_id:
                                raise DriveUploadError(
                                    "Drive upload completed without a file id")
                            try:
                                sidecar_path.unlink()
                            except OSError:
                                pass
                            return {"drive_file_id": file_id, "size": size,
                                    "sha256": sha256, "reused": False,
                                    "reused_via": None}
                        raise DriveUploadError(
                            f"Drive chunk PUT returned HTTP {status}")
                    # Loop ended without a 200/201: reconcile before retry.
                    uploaded = self.query_offset(session_uri, size)
                    if uploaded >= size:
                        found = self.reconcile_existing(
                            folder_id, name, run_id, size, sha256)
                        if found is not None:
                            try:
                                sidecar_path.unlink()
                            except OSError:
                                pass
                            return found
                        raise DriveUploadError(
                            "Drive reports the upload complete but no "
                            "matching file reconciles")
                except DriveAuthError:
                    if refreshed_once:
                        raise
                    refreshed_once = True
                    try:
                        self.token_provider.refresh()
                    except Exception as e:  # noqa: BLE001 - refresh failure is auth failure
                        raise DriveAuthError(
                            "Drive token refresh failed") from e
                    continue
                except (DriveConflictError, DriveUploadError) as e:
                    if isinstance(e, DriveConflictError):
                        raise
                    if "expired" in str(e):
                        # Session URI dead: reconcile, then start fresh.
                        found = self.reconcile_existing(
                            folder_id, name, run_id, size, sha256)
                        if found is not None:
                            try:
                                sidecar_path.unlink()
                            except OSError:
                                pass
                            return found
                        session_uri = None
                        uploaded = 0
                        try:
                            sidecar_path.unlink()
                        except OSError:
                            pass
                        continue
                    if attempt >= self.max_attempts:
                        raise DriveUploadRetryExhaustedError(
                            f"Drive upload failed after {attempt} attempts: "
                            f"{type(e).__name__}") from e
                    _sleep(self._backoff_delay(attempt))
                    continue
                except urllib.error.HTTPError as e:
                    if e.code == 401 and not refreshed_once:
                        refreshed_once = True
                        try:
                            self.token_provider.refresh()
                        except Exception as exc:  # noqa: BLE001
                            raise DriveAuthError(
                                "Drive token refresh failed") from exc
                        continue
                    if e.code == 401:
                        raise DriveAuthError(
                            "Drive upload unauthorized after token refresh") from e
                    if e.code == 403:
                        raise DriveAuthError(
                            "Drive upload forbidden") from e
                    if e.code in TRANSIENT_STATUSES:
                        if attempt >= self.max_attempts:
                            raise DriveUploadRetryExhaustedError(
                                f"Drive upload failed after {attempt} "
                                f"attempts: HTTP {e.code}") from e
                        _sleep(self._backoff_delay(attempt))
                        continue
                    raise DriveUploadError(
                        f"Drive upload failed: HTTP {e.code}") from e
                except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                    if attempt >= self.max_attempts:
                        raise DriveUploadRetryExhaustedError(
                            f"Drive upload failed after {attempt} attempts: "
                            f"{type(e).__name__}") from e
                    _sleep(self._backoff_delay(attempt))
                    continue
        raise DriveUploadRetryExhaustedError(
            "Drive upload failed after "
            f"{self.max_attempts} attempts (no final response)")

    def verify_uploaded(self, file_id: str, expected_size: int,
                        expected_sha256: str, expected_md5: str | None = None,
                        parent_id: str | None = None) -> dict:
        """Independent verification: server metadata (size + MD5 + SHA
        when Drive reports it + parent + not trashed) AND a full
        alt=media read-back with SHA-256. Returns the evidence dict.
        Raises DriveUploadError on any mismatch."""
        url = (f"{DRIVE_API_BASE}/files/{file_id}"
               f"?fields=id,name,size,md5Checksum,sha256Checksum,trashed,"
               f"parents&supportsAllDrives=true")
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self._do(req) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise DriveAuthError(
                    f"Drive verify unauthorized: HTTP {e.code}") from e
            raise DriveUploadError(
                f"Drive verify failed: HTTP {e.code}") from e
        if meta.get("trashed"):
            raise DriveUploadError("Drive file is trashed")
        if int(meta.get("size", -1)) != int(expected_size):
            raise DriveUploadError("Drive file size mismatch")
        if expected_md5 and meta.get("md5Checksum", "").lower() != expected_md5.lower():
            raise DriveUploadError("Drive file MD5 mismatch")
        server_sha = str(meta.get("sha256Checksum", "")).lower()
        if server_sha and server_sha != expected_sha256.lower():
            raise DriveUploadError("Drive file SHA-256 mismatch")
        if parent_id is not None and parent_id not in (meta.get("parents") or []):
            raise DriveUploadError("Drive file parent mismatch")
        media_url = (f"{DRIVE_API_BASE}/files/{file_id}?alt=media"
                     f"&supportsAllDrives=true")
        req = urllib.request.Request(media_url, headers=self._headers())
        h = hashlib.sha256()
        read_bytes = 0
        try:
            with self._do(req) as resp:
                if resp.status != 200:
                    raise DriveUploadError(
                        f"Drive read-back returned HTTP {resp.status}")
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                    read_bytes += len(chunk)
        except urllib.error.HTTPError as e:
            raise DriveUploadError(
                f"Drive read-back failed: HTTP {e.code}") from e
        if read_bytes != int(expected_size):
            raise DriveUploadError("Drive read-back size mismatch")
        if h.hexdigest().lower() != expected_sha256.lower():
            raise DriveUploadError("Drive read-back SHA-256 mismatch")
        return {"drive_file_id": file_id, "size": int(expected_size),
                "sha256": expected_sha256, "read_back_bytes": read_bytes,
                "verified": True}

    # -- sidecar ------------------------------------------------------

    @staticmethod
    def _load_sidecar(path: Path, name: str, folder_id: str, run_id: str,
                      size: int, sha256: str) -> dict | None:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        if doc.get("name") != name or doc.get("parent_id") != folder_id:
            return None
        if doc.get("run_id") != run_id or int(doc.get("size", -1)) != size:
            return None
        if str(doc.get("sha256", "")).lower() != sha256.lower():
            return None
        if not doc.get("session_uri"):
            return None
        return doc

    @staticmethod
    def _save_sidecar(path: Path, doc: dict) -> None:
        _atomic_write_json_0600(path, doc)
