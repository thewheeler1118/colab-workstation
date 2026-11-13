"""Short-lived Drive token minting for the noninteractive H3 path.

TASK-006 P0-B (Issue #44): the Muse control layer holds the long-lived
ADC and mints short-lived, read-only access tokens for the Colab worker.
The worker never sees a refresh token or the ADC file.

Rules:
- Minted tokens are scoped to ``drive.readonly`` for downloads and to
  ``drive.file`` for worker-side uploads (least privilege for each
  direction). Minting with any broader scope is a bug. The write token
  (TASK-006 P1-A, Issue #48) only ever touches files the app created
  in the pre-created run folder; the controller creates that folder
  with the full-scope service and independently re-verifies the upload.
- Service-account credentials are refused, same guard as the Drive
  transport: a service account does not guarantee the same personal
  Drive owner.
- A token value is NEVER logged, printed, or stored outside the 0600
  token file it is delivered in. Only ``token_id`` (first 16 hex chars
  of SHA-256(token)) may appear in diagnostics.
- Token files are written atomically with mode 0600 under a 0700
  parent directory (``drive_resumable.atomic_secret_json``).
"""

from __future__ import annotations

from pathlib import Path

from h3_drive_download import (
    MAX_TOKEN_RENEWALS,
    TOKEN_REFRESH_MARGIN_SEC,
    token_id_for,
    validate_token_doc,
)

READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
# TASK-006 P1-A: least-privilege write scope for the worker-side upload.
# drive.file only ever touches files this app created (the run folder is
# pre-created by the controller with the full-scope service). Whether a
# drive.file token can write into that folder is TESTABLE_HYPOTHESIS
# until a live run proves it; if live verification refutes it, the
# fallback is a short-lived full-drive token for the single run,
# recorded as a decision, never silently substituted.
WRITE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

__all__ = [
    "READONLY_SCOPE",
    "WRITE_FILE_SCOPE",
    "MAX_TOKEN_RENEWALS",
    "TOKEN_REFRESH_MARGIN_SEC",
    "mint_readonly_token",
    "mint_file_token",
    "write_token_file",
    "validate_token_doc",
    "token_id_for",
]


def _mint_token(scope: str) -> dict:
    """Mint a short-lived Drive access token for one scope from the
    user ADC. Returns ``{"access_token", "expires_at_utc", "token_id"}``.
    The caller must deliver the document through ``write_token_file``
    and never log the token value. Raises RuntimeError when no usable
    user ADC is present."""
    import google.auth
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default(scopes=[scope])
    if getattr(creds, "service_account_email", None):
        raise RuntimeError(
            "Service-account credentials do not guarantee same personal "
            "Drive owner; use user ADC")
    if not getattr(creds, "valid", False):
        creds.refresh(Request())
    token = getattr(creds, "token", None)
    expiry = getattr(creds, "expiry", None)
    if not token or expiry is None:
        raise RuntimeError("ADC refresh did not yield an access token")
    expires_at_utc = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "access_token": token,
        "expires_at_utc": expires_at_utc,
        "token_id": token_id_for(token),
    }


def mint_readonly_token() -> dict:
    """Mint a short-lived, read-only Drive access token from the user
    ADC. Returns ``{"access_token", "expires_at_utc", "token_id"}``.
    The caller must deliver the document through ``write_token_file``
    and never log the token value. Raises RuntimeError when no usable
    user ADC is present."""
    return _mint_token(READONLY_SCOPE)


def mint_file_token() -> dict:
    """Mint a short-lived ``drive.file``-scoped token for the worker's
    remote upload (TASK-006 P1-A). Same shape and rules as
    ``mint_readonly_token``; the worker must only ever write into the
    controller-pre-created run folder."""
    return _mint_token(WRITE_FILE_SCOPE)


def write_token_file(path, token_doc: dict) -> Path:
    """Atomically write a token document to a 0600 file. Validates the
    document shape first (never writes a malformed token file)."""
    from drive_resumable import atomic_secret_json

    validate_token_doc(token_doc)
    path = Path(path)
    atomic_secret_json(path, token_doc)
    return path


def token_fingerprint(token_doc: dict) -> str:
    """Audit-safe identifier for a minted token document."""
    return str(token_doc.get("token_id", ""))
