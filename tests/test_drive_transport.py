"""TASK-005 PR-7 (Issue #30): Drive transport over requests.

Proves, offline (no ADC, no network):
1. The RequestsHttp adapter honors the httplib2 ``request()`` contract
   the api client depends on (status, dict-style headers, bytes content)
   and translates requests exceptions into the types the client's retry
   loop and ``_is_transient_drive_error`` already handle.
2. A 308 resumable-upload response is returned to the caller, not
   followed — the chunk protocol depends on seeing it.
3. ``drive_service()`` / ``build_drive_service_from_adc()`` build on the
   requests adapter (not httplib2), with scopes and the service-account
   guard unchanged.
4. A REAL api-client service built on the adapter executes files().get,
   a small non-resumable files().create, and the full resumable flow
   (initiation 200 + Location, chunk PUT 308 + Range, final PUT 200 +
   JSON) against a scripted fake session — the compatibility proof that
   the client and the adapter speak the same protocol.
"""
import json, socket, ssl, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
try:
    # google-api-python-client + google-auth live in requirements-local.txt
    # (verified local venv), NOT in requirements-ci.txt: the GitHub CI
    # runner has no googleapiclient. Tests that need the real client are
    # skipped there (skipUnless below); the adapter tests run everywhere.
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    import google.auth  # noqa: F401  (needed by the patch targets below)
    HAS_GOOGLE_LIBS = True
except ImportError:
    HAS_GOOGLE_LIBS = False
import drive_api
import drive_depot as d
from h3_drive_transport import (
    DRIVE_TRANSPORT, RequestsHttp, RequestsHttpResponse,
)


class FakeResponse:
    """Minimal requests.Response stand-in: status_code, headers, content."""
    def __init__(self, status, headers=None, content=b""):
        self.status_code = status
        self.headers = headers or {}
        self.content = content


class FakeSession:
    """Scripted responses in call order; records every request."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url,
                           "headers": kw.get("headers"), "data": kw.get("data"),
                           "allow_redirects": kw.get("allow_redirects"),
                           "timeout": kw.get("timeout")})
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {url}")
        return self.responses.pop(0)


class RaisingSession:
    def __init__(self, exc):
        self.exc = exc

    def request(self, *args, **kw):
        raise self.exc


class FakeCreds:
    service_account_email = None


def _service(fake_session):
    return build("drive", "v3", http=RequestsHttp(fake_session),
                 cache_discovery=False)


class AdapterTest(unittest.TestCase):
    def test_get_returns_status_lowercase_headers_bytes(self):
        s = FakeSession([FakeResponse(200, {"Content-Type": "application/json",
                                            "X-Custom": "v"}, b'{"a":1}')])
        resp, content = RequestsHttp(s).request(
            "https://example.invalid/x", "GET", headers={"Accept": "application/json"})
        self.assertIsInstance(resp, RequestsHttpResponse)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp["content-type"], "application/json")
        self.assertEqual(resp["x-custom"], "v")
        self.assertEqual(content, b'{"a":1}')
        self.assertTrue(s.calls[0]["allow_redirects"])
        self.assertEqual(s.calls[0]["timeout"], 300)
        self.assertEqual(s.calls[0]["headers"], {"Accept": "application/json"})

    def test_post_passes_body_and_headers(self):
        s = FakeSession([FakeResponse(200, {}, b"ok")])
        RequestsHttp(s).request("https://example.invalid/x", "POST",
                                body=b"payload", headers={"Content-Type": "text/plain"})
        self.assertEqual(s.calls[0]["data"], b"payload")
        self.assertEqual(s.calls[0]["headers"], {"Content-Type": "text/plain"})

    def test_308_returned_not_followed(self):
        # The resumable-upload chunk protocol depends on the client SEEING
        # the 308 + Range. If the adapter followed it, the upload would
        # lose its place.
        s = FakeSession([FakeResponse(308, {"Range": "bytes=0-7"}, b"")])
        resp, content = RequestsHttp(s).request(
            "https://example.invalid/upload", "PUT", body=b"12345678",
            headers={"Content-Range": "bytes 0-7/16"})
        self.assertEqual(resp.status, 308)
        self.assertEqual(resp["range"], "bytes=0-7")
        self.assertEqual(len(s.calls), 1)

    def test_404_returned_not_raised(self):
        s = FakeSession([FakeResponse(404, {"Content-Type": "application/json"},
                                      b'{"error": {"code": 404}}')])
        resp, _ = RequestsHttp(s).request("https://example.invalid/x")
        self.assertEqual(resp.status, 404)

    def test_ssl_error_translated(self):
        with self.assertRaises(ssl.SSLError):
            RequestsHttp(RaisingSession(
                requests.exceptions.SSLError("bad version"))).request("https://x")

    def test_connection_error_translated_to_builtin(self):
        with self.assertRaises(ConnectionError):
            RequestsHttp(RaisingSession(
                requests.exceptions.ConnectionError("reset"))).request("https://x")

    def test_timeout_translated_to_socket_timeout(self):
        with self.assertRaises(socket.timeout):
            RequestsHttp(RaisingSession(
                requests.exceptions.ReadTimeout("slow"))).request("https://x")


@unittest.skipUnless(HAS_GOOGLE_LIBS,
                       "google-api-python-client not installed (CI)")
class DriveServiceTest(unittest.TestCase):
    def test_drive_service_uses_requests_transport(self):
        creds = FakeCreds()
        with patch("google.auth.default", return_value=(creds, None)) as auth_default, \
             patch("googleapiclient.discovery.build") as mock_build:
            # build_requests_drive_service imports build inside the
            # function, so patching googleapiclient.discovery.build is
            # the lookup point.
            d.drive_service()
        args, kwargs = mock_build.call_args
        self.assertEqual(args[0], "drive")
        self.assertEqual(args[1], "v3")
        self.assertIsInstance(kwargs["http"], RequestsHttp)
        self.assertEqual(kwargs["cache_discovery"], False)
        scopes = auth_default.call_args.kwargs["scopes"]
        self.assertIn("https://www.googleapis.com/auth/drive", scopes)
        self.assertIn("https://www.googleapis.com/auth/userinfo.email", scopes)

    def test_drive_service_refuses_service_account(self):
        creds = FakeCreds()
        creds.service_account_email = "svc@example.iam.gserviceaccount.com"
        with patch("google.auth.default", return_value=(creds, None)):
            with self.assertRaisesRegex(RuntimeError, "Service-account"):
                d.drive_service()

    def test_transport_constant_is_requests(self):
        self.assertEqual(DRIVE_TRANSPORT, "requests")

    def test_build_drive_service_from_adc_uses_requests(self):
        creds = FakeCreds()
        with patch("google.auth.default", return_value=(creds, None)), \
             patch("googleapiclient.discovery.build") as mock_build:
            drive_api.build_drive_service_from_adc()
        _, kwargs = mock_build.call_args
        self.assertIsInstance(kwargs["http"], RequestsHttp)


@unittest.skipUnless(HAS_GOOGLE_LIBS,
                       "google-api-python-client not installed (CI)")
class RealClientInteropTest(unittest.TestCase):
    """A real api-client service on the adapter, scripted fake session."""

    def test_files_get(self):
        body = json.dumps({"id": "x", "name": "a.mp4",
                           "size": "7453808"}).encode()
        s = FakeSession([FakeResponse(200, {"Content-Type": "application/json"}, body)])
        got = _service(s).files().get(fileId="x", fields="id,name,size").execute()
        self.assertEqual(got, {"id": "x", "name": "a.mp4", "size": "7453808"})
        self.assertIn("drive/v3/files/x", s.calls[0]["url"])
        self.assertEqual(s.calls[0]["method"], "GET")

    def test_small_nonresumable_create(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "small.bin"
            path.write_bytes(b"0123456789abcdef")
            media = MediaFileUpload(str(path), mimetype="application/octet-stream",
                                    chunksize=8 * 1024 * 1024, resumable=False)
            s = FakeSession([FakeResponse(
                200, {"Content-Type": "application/json"},
                json.dumps({"id": "new-id", "name": "small.bin"}).encode())])
            got = _service(s).files().create(
                body={"name": "small.bin", "parents": ["folder"]},
                media_body=media, fields="id,name").execute()
        self.assertEqual(got["id"], "new-id")
        self.assertEqual(s.calls[0]["method"], "POST")
        # Multipart body carries the file bytes.
        data = s.calls[0]["data"]
        if hasattr(data, "read"):
            data = data.read()
        self.assertIn(b"0123456789abcdef", data)

    def test_resumable_upload_flow(self):
        # 16-byte file, 8-byte chunks: initiation + chunk 1 (308), chunk 2 (200).
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "up.bin"
            path.write_bytes(b"0123456789abcdef")
            media = MediaFileUpload(str(path), mimetype="application/octet-stream",
                                    chunksize=8, resumable=True)
            s = FakeSession([
                FakeResponse(200, {"Location": "https://example.invalid/resumable/1"}, b""),
                FakeResponse(308, {"Range": "bytes=0-7"}, b""),
                FakeResponse(200, {"Content-Type": "application/json"},
                             json.dumps({"id": "up-id", "name": "up.bin"}).encode()),
            ])
            req = _service(s).files().create(
                body={"name": "up.bin", "parents": ["folder"]},
                media_body=media, fields="id,name")
            status, resp = req.next_chunk()
            self.assertIsNone(resp)
            self.assertIsNotNone(status)
            status, resp = req.next_chunk()
        self.assertEqual(resp["id"], "up-id")
        self.assertEqual(len(s.calls), 3)
        initiation, chunk1, chunk2 = s.calls
        self.assertEqual(initiation["method"], "POST")
        self.assertEqual(initiation["headers"]["X-Upload-Content-Length"], "16")
        self.assertEqual(chunk1["method"], "PUT")
        self.assertEqual(chunk1["headers"]["Content-Range"], "bytes 0-7/16")
        self.assertEqual(chunk2["method"], "PUT")
        self.assertEqual(chunk2["headers"]["Content-Range"], "bytes 8-15/16")


if __name__ == "__main__":
    unittest.main()
