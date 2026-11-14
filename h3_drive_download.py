"""Noninteractive Google Drive download for the H3 production path.

TASK-006 P0-B (Issue #44): the paid path must not depend on interactive
``colab drivemount``. This module downloads pinned model files (and the
ComfyUI source archive) from Drive with a short-lived, read-only access
token, using only the standard library so the remote Colab worker can
import it with no extra dependency.

Security rules (hard):
- This module never holds, mints, or stores a refresh token, ADC, or
  any long-lived credential. It only ever uses an access token handed
  to it through a token provider.
- A token value is NEVER written to a log, manifest, audit record,
  exception message, or return value. Only ``token_id`` (the first 16
  hex chars of SHA-256(token)) may appear in diagnostics; it is a
  non-sensitive identifier for correlating mint/renewal events.
- A token file is read and then deleted immediately by the reader
  (``read_token_file``), except for the explicit ``delete=False`` probe
  path, which is documented at its call site.
- Tokens are never passed on the command line (``/proc/<pid>/cmdline``
  is world-readable on the VM).

Download rules:
- Pinned identity: Drive file id + expected byte count + expected
  SHA-256 (and MD5 when the server reports one). The server metadata
  is re-checked before AND after the transfer; a file that changed
  mid-transfer fails instead of being silently accepted.
- HTTP Range resume with a ``.part`` file, atomic replace on success,
  full SHA-256 verification before the file is ever handed to a model
  loader. A ``.part`` file is never a verified model.
- Error classification: 429/5xx are transient (bounded backoff);
  401 triggers exactly one token refresh, then fails closed;
  403 fails closed immediately; other 4xx fail immediately.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Rebindable for tests: the fake transport patches h3_drive_download.urlopen.
urlopen = urllib.request.urlopen

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Token lifetime margin: ask for a renewal while this much life remains.
TOKEN_REFRESH_MARGIN_SEC = 600
# Hard cap on renewals per run; the controller enforces the same cap.
MAX_TOKEN_RENEWALS = 6

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB streaming writes (TASK-007)
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE_SEC = 2.0
DEFAULT_TIMEOUT_SEC = 300
# Files at or above this size get a sharded concurrent download when
# starting fresh (TASK-007): a 40 GiB restore over one TCP stream is
# the single biggest cold-start risk against a ~60-minute session
# budget. Below it the single stream is simpler and fast enough.
CONCURRENT_MIN_SIZE = 256 * 1024 * 1024
DEFAULT_CONCURRENT_WORKERS = 4
# A slow-but-alive download is not killed; the read timeout only bounds
# a single stalled read inside urlopen.
TRANSIENT_STATUSES = (429, 500, 502, 503, 504)


class DriveDownloadError(RuntimeError):
    """Base failure for a Drive download. Never carries a token value."""


class DriveAuthError(DriveDownloadError):
    """401 after a refresh, or 403: fail closed, never retry the token."""


class DriveChangedError(DriveDownloadError):
    """Server metadata changed between the pre/post checks: the remote
    file is not the pinned file any more. Never accept the bytes."""


class DriveRetryExhaustedError(DriveDownloadError):
    """Transient failures outlived the bounded retry budget."""


class _NoRangeSupport(Exception):
    """Internal: a ranged request was answered 200, so the server does
    not honour Range for this file. Signals the caller to fall back
    to the single-stream path (never an error by itself)."""


class _ProgressAborted(Exception):
    """Internal: a shard stopped early because the progress callback
    raised (e.g. the worker's budget projection). The recorded
    callback error is what surfaces, never this marker."""


class TokenUnavailable(RuntimeError):
    """The token provider has no usable access token right now."""


def token_id_for(token: str) -> str:
    """Non-sensitive identifier for a token value (audit correlation
    only). The id cannot be used to reconstruct or use the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _parse_utc(value: str) -> float:
    """Parse an ISO-8601 UTC timestamp to epoch seconds."""
    from datetime import datetime, timezone

    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()


def validate_token_doc(doc: dict) -> dict:
    """Shape-check a token document. Returns the doc unchanged.
    Raises ValueError on a bad shape. Never logs the token value."""
    if not isinstance(doc, dict):
        raise ValueError("token document is not an object")
    token = doc.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("token document has no access_token")
    expires = doc.get("expires_at_utc")
    if not isinstance(expires, str) or not expires:
        raise ValueError("token document has no expires_at_utc")
    _parse_utc(expires)  # raises ValueError on a bad timestamp
    token_id = doc.get("token_id")
    if not isinstance(token_id, str) or not token_id:
        raise ValueError("token document has no token_id")
    if token_id != token_id_for(token):
        raise ValueError("token document token_id does not match its token")
    return doc


def read_token_file(path, delete: bool = True) -> dict:
    """Read a 0600 token file and delete it immediately after a
    successful read (default). The caller owns the lifetime decision:
    the download probe passes delete=False so the worker's own token
    file survives the probe; the worker always reads with delete=True.
    Raises FileNotFoundError when no token file is present (the
    controller has not delivered one yet) and ValueError on a bad
    shape. Never logs the token value."""
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    validate_token_doc(doc)
    if delete:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return doc


class FileTokenProvider:
    """Thread-safe access-token holder for the remote worker.

    The token is loaded from a 0600 token file delivered by the
    controller (``colab upload``). ``get_token()`` returns the current
    token while it has more than ``margin_sec`` of life left; when it
    is about to expire it tries to pick up a freshly delivered token
    file at the same path (the controller uploads one after the worker
    requests renewal through its job state). ``refresh()`` forces a
    re-read of the token file (used exactly once after a 401).
    The provider never logs a token value.
    """

    def __init__(self, token_path, margin_sec: int = TOKEN_REFRESH_MARGIN_SEC):
        self.token_path = Path(token_path)
        self.margin_sec = margin_sec
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float | None = None
        self._token_id: str | None = None

    @property
    def token_id(self) -> str | None:
        with self._lock:
            return self._token_id

    def _load_file_locked(self) -> bool:
        try:
            doc = read_token_file(self.token_path, delete=True)
        except FileNotFoundError:
            return False
        self._token = doc["access_token"]
        self._expires_at = _parse_utc(doc["expires_at_utc"])
        self._token_id = doc["token_id"]
        return True

    def load_initial(self) -> dict:
        """Load the first token file. Raises TokenUnavailable when no
        token file has been delivered yet."""
        with self._lock:
            if not self._load_file_locked():
                raise TokenUnavailable(
                    "no Drive token file delivered at " + str(self.token_path))
            return {"token_id": self._token_id, "expires_at": self._expires_at}

    def get_token(self) -> str:
        """Current usable token, or raise TokenUnavailable. Picks up a
        freshly delivered token file when the current one is inside the
        renewal margin."""
        with self._lock:
            now = time.time()
            if self._token is not None and self._expires_at is not None:
                if self._expires_at - now > self.margin_sec:
                    return self._token
            # Inside the margin (or no token yet): try a fresh file.
            if self._load_file_locked():
                return self._token
            if self._token is not None and self._expires_at is not None \
                    and self._expires_at > now:
                # No fresh file, but the old token is still alive: use it
                # once more rather than failing the in-flight chunk.
                return self._token
            raise TokenUnavailable("no usable Drive access token")

    def refresh(self) -> str:
        """Force a re-read of the token file (after a 401). Raises
        TokenUnavailable when no fresh token file is present."""
        with self._lock:
            if self._load_file_locked():
                return self._token
            raise TokenUnavailable("no fresh Drive token file for refresh")

    def seconds_remaining(self) -> float | None:
        with self._lock:
            if self._expires_at is None:
                return None
            return self._expires_at - time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _backoff_delay(attempt: int, base: float) -> float:
    # Exponential backoff with jitter; the attempt number is 1-based.
    return base * (2 ** (attempt - 1)) + random.uniform(0, 1.0)


class DriveDownloader:
    """Range-resuming Drive file downloader over a token provider.

    ``token_provider`` must expose ``get_token() -> str`` and
    ``refresh() -> str`` (see FileTokenProvider). ``on_progress`` is
    called as ``on_progress(bytes_done, total)`` after each chunk; the
    worker uses it to request a token renewal while life remains.
    ``on_token_expiring`` is called as ``on_token_expiring(token_id,
    seconds_remaining)`` when the provider reports less than the
    renewal margin during a download.
    ``concurrent_workers`` bounds the sharded download of large fresh
    files (``>= CONCURRENT_MIN_SIZE``); it is separate from
    ``max_workers``, which bounds multi-file ``download_all``.
    """

    def __init__(self, token_provider, chunk_size: int = DEFAULT_CHUNK_SIZE,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 backoff_base_sec: float = DEFAULT_BACKOFF_BASE_SEC,
                 max_workers: int = 1,
                 concurrent_workers: int = DEFAULT_CONCURRENT_WORKERS,
                 timeout_sec: int = DEFAULT_TIMEOUT_SEC):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if concurrent_workers < 1:
            raise ValueError("concurrent_workers must be >= 1")
        self.token_provider = token_provider
        self.chunk_size = chunk_size
        self.max_attempts = max_attempts
        self.backoff_base_sec = backoff_base_sec
        self.max_workers = max_workers
        self.concurrent_workers = concurrent_workers
        self.timeout_sec = timeout_sec

    # -- HTTP plumbing (stdlib only; urlopen is module-level for tests) --

    def _request(self, url: str, headers: dict | None = None,
                 method: str = "GET"):
        token = self.token_provider.get_token()
        req_headers = {"Authorization": "Bearer " + token}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers, method=method)
        return urlopen(req, timeout=self.timeout_sec)

    def get_metadata(self, file_id: str) -> dict:
        """Server-side metadata for one Drive file. Raises
        DriveDownloadError on any non-200 answer; a 404 may mean
        permissions rather than absence."""
        url = (f"{DRIVE_API_BASE}/files/{file_id}"
               "?fields=id,name,size,md5Checksum,sha256Checksum,trashed"
               "&supportsAllDrives=true")
        try:
            with self._request(url) as resp:
                status = getattr(resp, "status", 200)
                if status != 200:
                    raise DriveDownloadError(
                        f"Drive metadata HTTP {status} for file {file_id}")
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise DriveDownloadError(
                f"Drive metadata HTTP {e.code} for file {file_id}") from e
        except urllib.error.URLError as e:
            raise DriveDownloadError(
                f"Drive metadata transport error for file {file_id}: "
                f"{type(e).__name__}") from e

    def _check_metadata(self, meta: dict, file_id: str, expected_size: int,
                        expected_sha256: str, expected_md5: str | None) -> None:
        if meta.get("trashed"):
            raise DriveChangedError(f"Drive file is trashed: {file_id}")
        size = meta.get("size")
        if size is None or int(size) != int(expected_size):
            raise DriveChangedError(
                f"Drive file size mismatch for {file_id}: server "
                f"{size!r} != pinned {expected_size}")
        server_sha = (meta.get("sha256Checksum") or "").lower()
        if server_sha and server_sha != expected_sha256.lower():
            raise DriveChangedError(
                f"Drive server SHA-256 mismatch for {file_id}")
        if expected_md5:
            server_md5 = (meta.get("md5Checksum") or "").lower()
            if server_md5 and server_md5 != expected_md5.lower():
                raise DriveChangedError(
                    f"Drive server MD5 mismatch for {file_id}")

    # -- single file --

    def download_file(self, file_id: str, expected_size: int,
                      expected_sha256: str, dest,
                      expected_md5: str | None = None,
                      on_progress=None, on_token_expiring=None) -> dict:
        """Download one pinned Drive file to ``dest``.

        Returns ``{"drive_file_id", "size", "sha256", "path"}``. The
        destination is atomically replaced only after the full SHA-256
        verifies; a ``.part`` file is left behind on failure for resume.
        Raises DriveDownloadError (or a subclass) on any failure; the
        exception message never contains a token value.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "", re.I):
            raise ValueError("Pinned SHA-256 required for " + str(file_id))
        if not expected_size or int(expected_size) <= 0:
            raise ValueError("Pinned byte count required for " + str(file_id))
        expected_size = int(expected_size)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(dest) + ".part")

        # Pre-check: the server must agree this is the pinned file.
        meta = self.get_metadata(file_id)
        self._check_metadata(meta, file_id, expected_size, expected_sha256,
                             expected_md5)

        # Reuse a complete, already-verified local copy.
        if dest.is_file() and dest.stat().st_size == expected_size:
            from h3_contract import sha256_file

            if sha256_file(dest) == expected_sha256.lower():
                return {"drive_file_id": file_id, "size": expected_size,
                        "sha256": expected_sha256.lower(), "path": str(dest)}
            dest.unlink()  # stale or corrupt local copy; never trust it

        start = part.stat().st_size if part.is_file() else 0
        if start > expected_size:
            part.unlink()
            start = 0
        if start == expected_size:
            from h3_contract import sha256_file

            if sha256_file(part) == expected_sha256.lower():
                os.replace(part, dest)
                return {"drive_file_id": file_id, "size": expected_size,
                        "sha256": expected_sha256.lower(), "path": str(dest)}
            part.unlink()
            start = 0

        last_error: Exception | None = None
        refreshed_once = False
        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        # TASK-007: large fresh downloads go sharded first. A merged
        # .part still passes through the standard final verification
        # below (size + full SHA-256 + post-metadata + atomic replace);
        # False means "fall back to the single stream".
        if start == 0 and expected_size >= CONCURRENT_MIN_SIZE \
                and self.concurrent_workers > 1:
            if self._download_concurrent(
                    file_id, expected_size, part, on_progress,
                    on_token_expiring):
                start = expected_size
        for attempt in range(1, self.max_attempts + 1):
            if start >= expected_size:
                # Concurrent path already merged the whole file into
                # .part; skip straight to final verification.
                break
            # Token-expiry signal: let the caller request a renewal
            # while the current token still has life left.
            if on_token_expiring is not None:
                remaining = None
                seconds_remaining = getattr(
                    self.token_provider, "seconds_remaining", None)
                if callable(seconds_remaining):
                    remaining = seconds_remaining()
                if remaining is not None and remaining < TOKEN_REFRESH_MARGIN_SEC:
                    token_id = getattr(self.token_provider, "token_id", None)
                    on_token_expiring(token_id, remaining)
            headers = {}
            if start > 0:
                headers["Range"] = f"bytes={start}-"
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                # _request adds Authorization; build the authorised
                # request directly so Range headers are preserved.
                token = self.token_provider.get_token()
                req.add_header("Authorization", "Bearer " + token)
                with urlopen(req, timeout=self.timeout_sec) as resp:
                    status = getattr(resp, "status", 200)
                    if status == 401:
                        if not refreshed_once:
                            refreshed_once = True
                            try:
                                self.token_provider.refresh()
                            except TokenUnavailable as e:
                                raise DriveAuthError(
                                    "Drive download 401 and no fresh "
                                    f"token available for {file_id}") from e
                            continue
                        raise DriveAuthError(
                            "Drive download 401 after token refresh for "
                            + str(file_id))
                    if status == 403:
                        raise DriveAuthError(
                            "Drive download 403 (revoked or insufficient "
                            f"permission) for {file_id}")
                    if status == 416:
                        # Range unsatisfiable: the part may already be
                        # complete; verify before deciding.
                        from h3_contract import sha256_file

                        if part.is_file() and part.stat().st_size == expected_size \
                                and sha256_file(part) == expected_sha256.lower():
                            os.replace(part, dest)
                            return {"drive_file_id": file_id,
                                    "size": expected_size,
                                    "sha256": expected_sha256.lower(),
                                    "path": str(dest)}
                        part.unlink(missing_ok=True)
                        start = 0
                        last_error = DriveDownloadError(
                            "HTTP 416 with incomplete part for " + str(file_id))
                        continue
                    if status == 200 and start > 0:
                        # Server ignored Range; restart from zero, do
                        # not append to a stale prefix.
                        part.unlink(missing_ok=True)
                        start = 0
                        last_error = DriveDownloadError(
                            "Server ignored Range for " + str(file_id))
                        continue
                    if status not in (200, 206):
                        if status in TRANSIENT_STATUSES:
                            last_error = DriveDownloadError(
                                f"Drive download HTTP {status} for {file_id}")
                            if attempt < self.max_attempts:
                                _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                                start = part.stat().st_size if part.is_file() else 0
                                continue
                            raise DriveRetryExhaustedError(
                                "Drive download retries exhausted for "
                                f"{file_id}: {last_error}") from last_error
                        raise DriveDownloadError(
                            f"Drive download HTTP {status} for {file_id}")
                    if status == 206:
                        cr = resp.headers.get("Content-Range", "")
                        m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr or "")
                        if not m or int(m.group(1)) != start \
                                or int(m.group(3)) != expected_size:
                            raise DriveChangedError(
                                "Unexpected Content-Range for " + str(file_id))
                    mode = "ab" if start > 0 else "wb"
                    written = start
                    with part.open(mode) as f:
                        while True:
                            chunk = resp.read(self.chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            written += len(chunk)
                            if written > expected_size:
                                raise DriveDownloadError(
                                    "Drive download overran pinned size for "
                                    + str(file_id))
                            if on_progress is not None:
                                on_progress(written, expected_size)
                    start = written
                    if written == expected_size:
                        break
                    last_error = DriveDownloadError(
                        "Drive download short read for " + str(file_id))
            except (DriveDownloadError, DriveAuthError, DriveChangedError,
                    ValueError):
                raise
            except urllib.error.HTTPError as e:
                # Raised by urlopen itself (not by our status branch)
                # when the server answers an error status.
                if e.code == 401 and not refreshed_once:
                    refreshed_once = True
                    try:
                        self.token_provider.refresh()
                    except TokenUnavailable as exc:
                        raise DriveAuthError(
                            "Drive download 401 and no fresh token "
                            f"available for {file_id}") from exc
                    continue
                if e.code == 401:
                    raise DriveAuthError(
                        "Drive download 401 after token refresh for "
                        + str(file_id)) from e
                if e.code == 403:
                    raise DriveAuthError(
                        "Drive download 403 (revoked or insufficient "
                        f"permission) for {file_id}") from e
                if e.code in TRANSIENT_STATUSES:
                    last_error = DriveDownloadError(
                        f"Drive download HTTP {e.code} for {file_id}")
                    if attempt < self.max_attempts:
                        _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                        start = part.stat().st_size if part.is_file() else 0
                        continue
                    raise DriveRetryExhaustedError(
                        "Drive download retries exhausted for "
                        f"{file_id}: {last_error}") from e
                raise DriveDownloadError(
                    f"Drive download HTTP {e.code} for {file_id}") from e
            except Exception as e:  # transport errors are transient
                last_error = e
            if attempt < self.max_attempts:
                _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                start = part.stat().st_size if part.is_file() else 0

        # Final verification: size + full SHA-256, then atomic replace.
        # The part is only promoted when it proves to be the pinned file.
        from h3_contract import sha256_file

        if part.is_file() and part.stat().st_size == expected_size \
                and sha256_file(part) == expected_sha256.lower():
            # Post-check: the server file must still be the pinned one;
            # a file that changed mid-transfer is refused even if the
            # downloaded bytes happen to hash correctly.
            post = self.get_metadata(file_id)
            self._check_metadata(post, file_id, expected_size,
                                 expected_sha256, expected_md5)
            os.replace(part, dest)
            return {"drive_file_id": file_id, "size": expected_size,
                    "sha256": expected_sha256.lower(), "path": str(dest)}
        if part.is_file():
            part.unlink(missing_ok=True)
        detail = (str(last_error)[:120] if last_error else "unknown")
        raise DriveRetryExhaustedError(
            "Drive download failed checksum/size for " + str(file_id)
            + ": " + detail)

    # -- sharded concurrent download (TASK-007) --

    def _download_shard(self, url: str, file_id: str, start: int, end: int,
                        expected_size: int, shard_path: Path, report,
                        abort: threading.Event) -> None:
        """Download one byte range ``[start, end]`` into ``shard_path``.

        Each attempt rewrites the shard from scratch. Raises
        ``_NoRangeSupport`` when the server answers 200 (Range
        ignored), ``DriveAuthError`` on 403 (fail closed, never falls
        back), ``DriveRetryExhaustedError`` when retries run out, and
        ``_ProgressAborted`` when the shared abort flag is set (the
        progress callback raised; the recorded callback error is what
        surfaces). Token refresh stays inside the thread-safe provider.
        """
        length = end - start + 1
        last_error: Exception | None = None
        refreshed_once = False
        for attempt in range(1, self.max_attempts + 1):
            if abort.is_set():
                raise _ProgressAborted()
            try:
                token = self.token_provider.get_token()
                req = urllib.request.Request(
                    url, headers={"Authorization": "Bearer " + token,
                                  "Range": f"bytes={start}-{end}"},
                    method="GET")
                with urlopen(req, timeout=self.timeout_sec) as resp:
                    status = getattr(resp, "status", 200)
                    if status == 200:
                        raise _NoRangeSupport(
                            "server answered 200 to a Range request for "
                            + str(file_id))
                    if status == 401:
                        if not refreshed_once:
                            refreshed_once = True
                            try:
                                self.token_provider.refresh()
                            except TokenUnavailable as e:
                                raise DriveAuthError(
                                    "Drive download 401 and no fresh "
                                    f"token available for {file_id}") from e
                            continue
                        raise DriveAuthError(
                            "Drive download 401 after token refresh for "
                            + str(file_id))
                    if status == 403:
                        raise DriveAuthError(
                            "Drive download 403 (revoked or insufficient "
                            f"permission) for {file_id}")
                    if status != 206:
                        if status in TRANSIENT_STATUSES:
                            last_error = DriveDownloadError(
                                f"Drive download HTTP {status} for {file_id}")
                            if attempt < self.max_attempts:
                                _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                                continue
                            raise DriveRetryExhaustedError(
                                "Drive download retries exhausted for "
                                f"{file_id}: {last_error}") from last_error
                        raise DriveDownloadError(
                            f"Drive download HTTP {status} for {file_id}")
                    cr = resp.headers.get("Content-Range", "")
                    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr or "")
                    if not m or int(m.group(1)) != start \
                            or int(m.group(2)) != end \
                            or int(m.group(3)) != expected_size:
                        raise DriveChangedError(
                            "Unexpected Content-Range for " + str(file_id))
                    written = 0
                    with shard_path.open("wb") as f:
                        while True:
                            if abort.is_set():
                                raise _ProgressAborted()
                            chunk = resp.read(self.chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            written += len(chunk)
                            if written > length:
                                raise DriveDownloadError(
                                    "Drive shard overran its range for "
                                    + str(file_id))
                            report(len(chunk))
                    if written == length:
                        return
                    last_error = DriveDownloadError(
                        "Drive shard short read for " + str(file_id))
            except (DriveAuthError, DriveChangedError, _NoRangeSupport,
                    _ProgressAborted):
                raise
            except urllib.error.HTTPError as e:
                if e.code == 401 and not refreshed_once:
                    refreshed_once = True
                    try:
                        self.token_provider.refresh()
                    except TokenUnavailable as exc:
                        raise DriveAuthError(
                            "Drive download 401 and no fresh token "
                            f"available for {file_id}") from exc
                    continue
                if e.code == 401:
                    raise DriveAuthError(
                        "Drive download 401 after token refresh for "
                        + str(file_id)) from e
                if e.code == 403:
                    raise DriveAuthError(
                        "Drive download 403 (revoked or insufficient "
                        f"permission) for {file_id}") from e
                if e.code in TRANSIENT_STATUSES:
                    last_error = DriveDownloadError(
                        f"Drive download HTTP {e.code} for {file_id}")
                    if attempt < self.max_attempts:
                        _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                        continue
                    raise DriveRetryExhaustedError(
                        "Drive download retries exhausted for "
                        f"{file_id}: {last_error}") from e
                raise DriveDownloadError(
                    f"Drive download HTTP {e.code} for {file_id}") from e
            except Exception as e:  # transport errors are transient
                last_error = e
            if attempt < self.max_attempts:
                _sleep(_backoff_delay(attempt, self.backoff_base_sec))
        raise DriveRetryExhaustedError(
            "Drive shard retries exhausted for " + str(file_id)
            + ": " + (str(last_error)[:120] if last_error else "unknown"))

    def _download_concurrent(self, file_id: str, expected_size: int,
                             part: Path, on_progress,
                             on_token_expiring) -> bool:
        """Shard a fresh large download across ``concurrent_workers``
        Range requests into independent temp files, then merge them in
        order into ``part``.

        Returns True when the merge is done (the caller then runs the
        standard final verification). Returns False to fall back to
        the single-stream path: the server ignored Range, or a shard
        exhausted its retries. A 403 (DriveAuthError), a metadata
        disagreement (DriveChangedError), or an ``on_progress``
        exception is fatal: the callback error is re-raised after the
        pool finishes, and no fallback is attempted. Shard files are
        cleaned up on every exit path.
        """
        # Token-expiry signal once, before the pool starts.
        if on_token_expiring is not None:
            remaining = None
            seconds_remaining = getattr(
                self.token_provider, "seconds_remaining", None)
            if callable(seconds_remaining):
                remaining = seconds_remaining()
            if remaining is not None and remaining < TOKEN_REFRESH_MARGIN_SEC:
                token_id = getattr(self.token_provider, "token_id", None)
                on_token_expiring(token_id, remaining)

        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        n = self.concurrent_workers
        shard_size = (expected_size + n - 1) // n
        ranges = []
        for i in range(n):
            start = i * shard_size
            end = min((i + 1) * shard_size, expected_size) - 1
            ranges.append((start, end))
        shard_paths = [Path(str(part) + f".shard{i}") for i in range(n)]

        done = [0]
        progress_lock = threading.Lock()
        progress_error: list = []
        abort = threading.Event()

        def _report(nbytes: int) -> None:
            # Held across the callback so concurrent shards report a
            # monotonic cumulative total.
            with progress_lock:
                done[0] += nbytes
                total = done[0]
                if on_progress is not None:
                    try:
                        on_progress(total, expected_size)
                    except Exception as exc:  # recorded; raised after the pool
                        if not progress_error:
                            progress_error.append(exc)
                        abort.set()

        errors: list = []
        try:
            with ThreadPoolExecutor(max_workers=n) as pool:
                futs = [pool.submit(
                    self._download_shard, url, file_id, s, e, expected_size,
                    shard_paths[i], _report, abort)
                    for i, (s, e) in enumerate(ranges)]
                for fut in futs:
                    try:
                        fut.result()
                    except Exception as exc:  # collected; classified below
                        errors.append(exc)
            if progress_error:
                raise progress_error[0]
            for exc in errors:
                if isinstance(exc, (DriveAuthError, DriveChangedError)):
                    raise exc
            if errors:
                return False  # no Range support, or a shard gave up
            # TASK-007.1: merge without a second full copy. The old code
            # opened a fresh .part and copied every shard into it while
            # all shards still existed — for the 25.3 GB main weight
            # that held ~70 GB at once, past the 45.1 GB restore
            # capacity gate (ENOSPC risk). Instead, move shard 0 into
            # place with zero-copy os.replace, then append each later
            # shard and unlink it immediately: the extra disk peak is
            # one shard, not the whole file. A mid-merge failure leaves
            # a valid prefix in .part (resume-safe) and the finally
            # below still cleans any shard that was not yet released.
            os.replace(shard_paths[0], part)
            with part.open("ab") as out:
                for shard_path in shard_paths[1:]:
                    with shard_path.open("rb") as f:
                        while True:
                            chunk = f.read(self.chunk_size)
                            if not chunk:
                                break
                            out.write(chunk)
                    shard_path.unlink(missing_ok=True)
            return True
        finally:
            for shard_path in shard_paths:
                shard_path.unlink(missing_ok=True)

    def probe_transport(self, file_id: str, probe_bytes: int = 1024 * 1024) -> dict:
        """Cheap auth + transport probe: metadata GET, then a single
        Range GET of the first ``probe_bytes`` bytes. Validates the
        206 status, the Content-Range header, and the exact byte
        count. Proves the token works and the server honours Range;
        it does NOT prove full-file integrity (the real restore does
        that with a full SHA-256). Raises DriveDownloadError on any
        failure. Never returns token material."""
        meta = self.get_metadata(file_id)
        total = meta.get("size")
        if total is None or int(total) <= 0:
            raise DriveDownloadError(
                f"Drive probe metadata missing size for {file_id}")
        want = min(int(probe_bytes), int(total))
        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        token = self.token_provider.get_token()
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + token,
                          "Range": f"bytes=0-{want - 1}"}, method="GET")
        try:
            with urlopen(req, timeout=self.timeout_sec) as resp:
                status = getattr(resp, "status", 200)
                if status != 206:
                    raise DriveDownloadError(
                        f"Drive probe expected HTTP 206, got {status} "
                        f"for {file_id}")
                cr = resp.headers.get("Content-Range", "")
                m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr or "")
                if not m or int(m.group(1)) != 0 or int(m.group(3)) != int(total):
                    raise DriveChangedError(
                        "Drive probe unexpected Content-Range for "
                        + str(file_id))
                got = 0
                while True:
                    chunk = resp.read(self.chunk_size)
                    if not chunk:
                        break
                    got += len(chunk)
                if got != want:
                    raise DriveDownloadError(
                        f"Drive probe short read for {file_id}: got "
                        f"{got}, wanted {want}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise DriveAuthError(
                    "Drive probe 401 for " + str(file_id)) from e
            if e.code == 403:
                raise DriveAuthError(
                    "Drive probe 403 for " + str(file_id)) from e
            raise DriveDownloadError(
                f"Drive probe HTTP {e.code} for {file_id}") from e
        return {"drive_file_id": file_id, "size": int(total),
                "probe_bytes": want}

    # -- many files --

    def download_all(self, files, dest_dir, on_progress=None,
                     on_token_expiring=None) -> list:
        """Download a list of pinned files.

        Each entry: ``{"file_id", "path", "size", "sha256",
        "md5" (optional)}``. ``path`` is relative to ``dest_dir``.
        Files download with bounded concurrency (``max_workers``;
        default 1 = sequential). Returns per-file result dicts in
        input order. On failure the first error is raised; files that
        already verified stay on disk and are reused on retry."""
        dest_dir = Path(dest_dir)

        def _one(entry):
            rel = entry["path"]
            return self.download_file(
                entry["file_id"], int(entry["size"]), entry["sha256"],
                dest_dir / rel, expected_md5=entry.get("md5"),
                on_progress=on_progress,
                on_token_expiring=on_token_expiring)

        if self.max_workers == 1:
            return [_one(entry) for entry in files]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(_one, files))
