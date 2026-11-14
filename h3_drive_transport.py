"""Drive API transport over requests (TASK-005 PR-7, Issue #30).

The stock google-api-python-client transport is httplib2. On this VM the
egress is an authenticated TLS-intercepting proxy and every Drive API call
through httplib2 fails with ``ssl.SSLError: [SSL: WRONG_VERSION_NUMBER]``
(measured TASK-005 §6/§7: same ADC, same scopes — requests ``files.get``
OK in 0.76 s, httplib2 fails). ``build('drive','v3', credentials=creds)``
does NOT fix it: google-api-python-client 2.202.0 routes credentials
through ``google_auth_httplib2.AuthorizedHttp`` — httplib2 again.

This module is the replacement: a small httplib2-compatible adapter
around ``google.auth.transport.requests.AuthorizedSession`` (requests).
The api client only ever calls ``http.request(uri, method, body=...,
headers=...)`` and reads ``resp.status`` plus dict-style header access,
so the adapter implements exactly that contract and translates requests
exceptions into the types the client's retry loop and our own
``_is_transient_drive_error`` already handle.

308 (resumable-upload) responses are returned to the caller, not
followed: requests only follows a redirect when a Location header is
present, and 308 chunk responses carry Range, not Location. Redirects
that ARE followed strip Authorization on cross-host hops (safer than
httplib2, which re-sends headers).

``verify`` stays True (certifi): §7 proved requests+certifi works through
this proxy. Do not switch it to SSL_CERT_FILE without re-measuring.

No credentials are logged, printed, or stored here. The AuthorizedSession
refreshes the same user ADC in memory, exactly as before.
"""

from __future__ import annotations

import socket
import ssl

DRIVE_TRANSPORT = "requests"

# Generous per-request timeout: the client issues 8 MiB resumable-upload
# chunks through this transport, and the depot restore path is latency
# bound through the egress proxy. The client's own retry loop handles
# failures; a short timeout here would manufacture them.
DEFAULT_TIMEOUT_SEC = 300


class RequestsHttpResponse(dict):
    """httplib2.Response-shaped view of a requests response.

    httplib2.Response is a dict subclass with a ``.status`` int attribute
    and lowercased header keys. The api client reads ``resp.status``,
    ``resp["location"]``, ``resp["content-type"]`` and ``"location" in
    resp``; this class satisfies all four.
    """

    def __init__(self, status: int, headers) -> None:
        super().__init__({str(k).lower(): v for k, v in dict(headers).items()})
        self.status = int(status)


class RequestsHttp:
    """httplib2-compatible ``http`` object backed by a requests session.

    ``session`` must be an authorised session (normally
    ``google.auth.transport.requests.AuthorizedSession``); the adapter
    never touches credentials itself. A plain ``requests.Session`` is
    accepted for tests, which is how the fake-session tests inject
    scripted responses.
    """

    def __init__(self, session, timeout: float = DEFAULT_TIMEOUT_SEC) -> None:
        self.session = session
        self.timeout = timeout

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        """httplib2 ``Http.request`` contract: ``(response, content)``.

        Extra kwargs (e.g. ``redirections``, ``connection_type``) are
        accepted and ignored: requests follows redirects by default and
        manages connections itself.
        """
        import requests

        try:
            resp = self.session.request(
                method,
                uri,
                data=body,
                headers=headers,
                allow_redirects=True,
                timeout=self.timeout,
            )
        except requests.exceptions.SSLError as exc:
            raise ssl.SSLError(str(exc)) from exc
        except requests.exceptions.ConnectTimeout as exc:
            raise socket.timeout(str(exc)) from exc
        except requests.exceptions.ReadTimeout as exc:
            raise socket.timeout(str(exc)) from exc
        except requests.exceptions.Timeout as exc:
            raise socket.timeout(str(exc)) from exc
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError(str(exc)) from exc
        return RequestsHttpResponse(resp.status_code, resp.headers), resp.content


def build_requests_drive_service(creds, scopes=None):
    """Build a Drive v3 api-client service on the requests transport.

    ``creds`` are google.auth credentials (normally the user ADC, loaded
    by the caller with the Drive scopes). Service-account credentials are
    refused, same guard as the old ``drive_service()``: a service account
    does not guarantee the same personal Drive owner.
    """
    from google.auth.transport.requests import AuthorizedSession
    from googleapiclient.discovery import build

    if getattr(creds, "service_account_email", None):
        raise RuntimeError(
            "Service-account credentials do not guarantee same personal "
            "Drive owner; use user ADC"
        )
    session = AuthorizedSession(creds)
    return build("drive", "v3", http=RequestsHttp(session), cache_discovery=False)


def build_token_drive_service(access_token: str):
    """Build a Drive v3 service on the requests transport, authenticated
    with one short-lived access token (TASK-007).

    Host-side preflight probes mint least-privilege tokens and must
    prove them against the live Drive API through this VM's
    authenticated TLS-intercepting egress proxy — the same requests +
    certifi transport ``drive_service()`` uses. The token is wrapped in
    a ``Credentials`` object with no refresh data, so the session never
    tries to renew it; a token that is about to expire simply fails
    and the caller mints a fresh one. The token value is never logged,
    printed, or stored here."""
    from google.oauth2.credentials import Credentials

    return build_requests_drive_service(Credentials(token=access_token))
