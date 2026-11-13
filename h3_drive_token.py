"""Short-lived Drive token minting for the noninteractive H3 path.

TASK-006 P0-B (Issue #44): the Muse control layer holds the long-lived
ADC and mints short-lived, read-only access tokens for the Colab worker.
The worker never sees a refresh token or the ADC file.

Rules:
- Minted tokens are scoped to ``drive.readonly`` only (least privilege
  for a download-only worker). Minting with any broader scope is a bug.
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

__all__ = [
    "READONLY_SCOPE",
    "MAX_TOKEN_RENEWALS",
    "TOKEN_REFRESH_MARGIN_SEC",
    "mint_readonly_token",
    "write_token_file",
    "validate_token_doc",
    "token_id_for",
]


def mint_readonly_token() -> dict:
    """Mint a short-lived, read-only Drive access token from the user
    ADC. Returns ``{"access_token", "expires_at_utc", "token_id"}``.
    The caller must deliver the document through ``write_token_file``
    and never log the token value. Raises RuntimeError when no usable
    user ADC is present."""
    import google.auth
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default(scopes=[READONLY_SCOPE])
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
