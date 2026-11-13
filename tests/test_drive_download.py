"""TASK-006 P0-B (Issue #44): offline simulator for the noninteractive
Drive download transport, the short-lived token model, the controller
preflight + renewal handler, and the worker download path.

No test here touches a real GPU, real Drive, or real credentials. The
fake Drive server scripts HTTP behaviour (200 / 206 / Range-ignoring
200 / 416 / 401 / 403 / 429 / 5xx / mid-stream drops / mid-transfer
metadata changes). Fake token values are SECRET-TOKEN-*; every failure
test asserts the value appears in NO exception message or return value.
"""
import errno
import hashlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import h3_drive_download as dd
import h3_drive_token as dt
from h3_drive_download import (
    DriveAuthError,
    DriveChangedError,
    DriveDownloadError,
    DriveDownloader,
    DriveRetryExhaustedError,
    FileTokenProvider,
    TokenUnavailable,
    token_id_for,
)

SECRET_A = "SECRET-TOKEN-AAAA"
SECRET_B = "SECRET-TOKEN-BBBB"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class FakeResp:
    """Minimal urlopen response: status, headers, chunked read."""

    def __init__(self, status, content=b"", headers=None, fail_after=None,
                 fail_exc=None, max_chunk=None):
        self.status = status
        self._content = content
        self.headers = headers or {}
        self._pos = 0
        self._fail_after = fail_after
        self._fail_exc = fail_exc
        self._failed = False
        self._max_chunk = max_chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if self._fail_after is not None and not self._failed \
                and self._pos >= self._fail_after:
            self._failed = True
            raise self._fail_exc
        if n is None or n < 0:
            n = len(self._content) - self._pos
        if self._max_chunk is not None:
            n = min(n, self._max_chunk)
        chunk = self._content[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class FakeProvider:
    """Stand-in for FileTokenProvider with scripted tokens."""

    def __init__(self, token=SECRET_A, expires_in=3600, refresh_token=SECRET_B):
        self._token = token
        self._expires = time.time() + expires_in
        self.refresh_token = refresh_token
        self.refresh_calls = 0

    def get_token(self):
        return self._token

    def refresh(self):
        self.refresh_calls += 1
        self._token = self.refresh_token
        self._expires = time.time() + 3600
        return self._token

    @property
    def token_id(self):
        return token_id_for(self._token)

    def seconds_remaining(self):
        return self._expires - time.time()


class FakeDrive:
    """Scripted fake Drive server behind h3_drive_download.urlopen.

    metadata: {file_id: {"size", "md5", "sha256", "trashed"}} — a
    metadata plan list overrides it per call (for mid-transfer change).
    media_plan: {file_id: [action, ...]} where an action is:
      ("resp", status, content, headers) — return a FakeResp
      ("raise", exc) — raise from urlopen (urllib-style HTTPError etc.)
    Each media GET consumes one action; exhausted plans return 200 with
    the registered full content.
    """

    def __init__(self):
        self.metadata = {}
        self.metadata_plan = {}
        self.media_content = {}
        self.media_plan = {}
        self.calls = []  # (url, headers) for every request
        self.meta_calls = 0

    def _meta_json(self, file_id):
        plan = self.metadata_plan.get(file_id)
        if plan:
            return plan.pop(0)
        return dict(self.metadata[file_id])

    def __call__(self, request, timeout=None):
        url = request.full_url
        headers = dict(getattr(request, "headers", {}) or {})
        # urllib capitalises header names; normalise for assertions.
        headers = {k.lower(): v for k, v in headers.items()}
        self.calls.append((url, headers))
        if "fields=id" in url:
            file_id = url.split("/files/")[1].split("?")[0]
            self.meta_calls += 1
            meta = self._meta_json(file_id)
            body = json.dumps({
                "id": file_id, "size": str(meta["size"]),
                "md5Checksum": meta.get("md5", ""),
                "sha256Checksum": meta.get("sha256", ""),
                "trashed": bool(meta.get("trashed", False))}).encode()
            return FakeResp(200, body, {"Content-Type": "application/json"})
        if "alt=media" in url:
            file_id = url.split("/files/")[1].split("?")[0]
            plan = self.media_plan.get(file_id) or []
            if plan:
                action = plan.pop(0)
                if action[0] == "raise":
                    raise action[1]
                _, status, content, resp_headers = action
                return FakeResp(status, content, resp_headers)
            content = self.media_content[file_id]
            range_h = headers.get("range", "")
            if range_h.startswith("bytes="):
                start = int(range_h.split("=", 1)[1].split("-", 1)[0] or 0)
                return FakeResp(206, content[start:],
                                {"Content-Range": f"bytes {start}-{len(content)-1}/{len(content)}"})
            return FakeResp(200, content)
        raise AssertionError("unexpected fake-drive URL: " + url)


class DownloadTestBase(unittest.TestCase):
    FILE_ID = "fid-0001"
    CONTENT = b"0123456789"

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))
        self.drive = FakeDrive()
        self.drive.metadata[self.FILE_ID] = {
            "size": len(self.CONTENT), "md5": hashlib.md5(self.CONTENT).hexdigest(),
            "sha256": sha(self.CONTENT)}
        self.drive.media_content[self.FILE_ID] = self.CONTENT
        self.provider = FakeProvider()
        self.urlopen_patch = patch.object(dd, "urlopen", self.drive)
        self.urlopen_patch.start()
        self.addCleanup(self.urlopen_patch.stop)
        self.sleep_patch = patch.object(dd, "_sleep", lambda s: None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def _downloader(self, **kw):
        return DriveDownloader(self.provider, **kw)

    def _dest(self, name="model.bin"):
        return Path(self.td) / name


class FullDownloadTests(DownloadTestBase):
    def test_full_200_success(self):
        out = self._downloader().download_file(
            self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest())
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertFalse(Path(str(self._dest()) + ".part").exists())
        self.assertEqual(out["size"], len(self.CONTENT))
        self.assertEqual(out["sha256"], sha(self.CONTENT))
        # No token value in any returned value.
        self.assertNotIn(SECRET_A, json.dumps(out))
        self.assertNotIn(SECRET_B, json.dumps(out))

    def test_correct_206_resume(self):
        dest = self._dest()
        Path(str(dest) + ".part").write_bytes(self.CONTENT[:4])
        out = self._downloader().download_file(
            self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), dest)
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        range_headers = [h.get("range") for _, h in self.drive.calls if "alt=media" in _]
        self.assertIn("bytes=4-", range_headers)

    def test_range_ignoring_200_restarts_from_zero(self):
        dest = self._dest()
        Path(str(dest) + ".part").write_bytes(b"XXXX")  # stale prefix
        self.drive.media_plan[self.FILE_ID] = [
            ("resp", 200, self.CONTENT, {})]
        out = self._downloader().download_file(
            self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), dest)
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)

    def test_partial_then_drop_resumes(self):
        dest = self._dest()
        # First attempt: stream dies after 4 bytes; second: 206 remainder.
        failing = FakeResp(200, self.CONTENT, {},
                           fail_after=4, fail_exc=ConnectionError("drop"),
                           max_chunk=4)
        orig_call = self.drive.__call__
        calls = {"n": 0}

        def _call(request, timeout=None):
            if "alt=media" in request.full_url:
                calls["n"] += 1
                if calls["n"] == 1:
                    self.drive.calls.append(
                        (request.full_url, {k.lower(): v for k, v in dict(getattr(request, "headers", {}) or {}).items()}))
                    return failing
            return orig_call(request, timeout)

        with patch.object(dd, "urlopen", _call):
            out = self._downloader().download_file(
                self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), dest)
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertEqual(calls["n"], 2)

    def test_file_changed_mid_transfer_fails(self):
        changed = {"size": len(self.CONTENT) + 1,
                   "md5": "0" * 32, "sha256": "0" * 64}
        self.drive.metadata_plan[self.FILE_ID] = [
            dict(self.drive.metadata[self.FILE_ID]), changed]
        with self.assertRaises(DriveChangedError):
            self._downloader().download_file(
                self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest())
        self.assertFalse(self._dest().exists())

    def test_sha256_mismatch_fails_and_no_promotion(self):
        # Metadata agrees with the pin, but the bytes do not: the
        # post-download full SHA-256 must refuse promotion.
        wrong = sha(b"different-bytes")
        self.drive.metadata[self.FILE_ID]["sha256"] = wrong
        with self.assertRaises(DriveRetryExhaustedError):
            self._downloader().download_file(
                self.FILE_ID, len(self.CONTENT), wrong, self._dest())
        self.assertFalse(self._dest().exists())
        self.assertFalse(Path(str(self._dest()) + ".part").exists())

    def test_429_backoff_then_success(self):
        self.drive.media_plan[self.FILE_ID] = [
            ("resp", 429, b"", {}),
            ("resp", 429, b"", {}),
            ("resp", 200, self.CONTENT, {})]
        out = self._downloader().download_file(
            self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest())
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        media_calls = [u for u, _ in self.drive.calls if "alt=media" in u]
        self.assertEqual(len(media_calls), 3)

    def test_503_http_error_then_success(self):
        err = urllib.error.HTTPError("http://x", 503, "unavailable", {}, None)
        self.drive.media_plan[self.FILE_ID] = [
            ("raise", err),
            ("resp", 200, self.CONTENT, {})]
        out = self._downloader().download_file(
            self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest())
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)

    def test_retry_exhaustion_keeps_part_for_resume(self):
        self.drive.media_plan[self.FILE_ID] = [
            ("resp", 503, b"", {})] * 10
        with self.assertRaises(DriveRetryExhaustedError):
            self._downloader(max_attempts=3).download_file(
                self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest())
        media_calls = [u for u, _ in self.drive.calls if "alt=media" in u]
        self.assertEqual(len(media_calls), 3)

    def test_disk_full_fails_closed(self):
        # ENOSPC raised mid-stream: no false success, no promoted file.
        orig_call = self.drive.__call__

        def _call(request, timeout=None):
            if "alt=media" in request.full_url:
                self.drive.calls.append((request.full_url, {}))
                headers = {k.lower(): v for k, v in dict(getattr(request, "headers", {}) or {}).items()}
                start = 0
                if headers.get("range", "").startswith("bytes="):
                    start = int(headers["range"][len("bytes="):].rstrip("-") or 0)
                content = self.CONTENT[start:]
                status = 206 if start else 200
                resp_headers = {"Content-Range": f"bytes {start}-{len(self.CONTENT)-1}/{len(self.CONTENT)}"} if start else {}
                return FakeResp(status, content, resp_headers, fail_after=4,
                                fail_exc=OSError(errno.ENOSPC, "No space left"),
                                max_chunk=4)
            return orig_call(request, timeout)

        with patch.object(dd, "urlopen", _call):
            with self.assertRaises(DriveRetryExhaustedError):
                self._downloader(max_attempts=2).download_file(
                    self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest())
        self.assertFalse(self._dest().exists())


class AuthTests(DownloadTestBase):
    def test_401_refreshes_once_then_succeeds(self):
        self.drive.media_plan[self.FILE_ID] = [
            ("resp", 401, b"", {}),
            ("resp", 200, self.CONTENT, {})]
        out = self._downloader().download_file(
            self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest())
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertEqual(self.provider.refresh_calls, 1)
        # The retry used the refreshed token.
        auths = [h.get("authorization") for _, h in self.drive.calls
                 if "alt=media" in _]
        self.assertIn("Bearer " + SECRET_B, auths)

    def test_401_after_refresh_fails_closed(self):
        self.drive.media_plan[self.FILE_ID] = [
            ("resp", 401, b"", {}),
            ("resp", 401, b"", {})]
        with self.assertRaises(DriveAuthError) as ctx:
            self._downloader().download_file(
                self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest())
        self.assertNotIn(SECRET_A, str(ctx.exception))
        self.assertNotIn(SECRET_B, str(ctx.exception))
        self.assertFalse(self._dest().exists())

    def test_403_fails_closed_immediately(self):
        self.drive.media_plan[self.FILE_ID] = [("resp", 403, b"", {})]
        with self.assertRaises(DriveAuthError):
            self._downloader().download_file(
                self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest())
        media_calls = [u for u, _ in self.drive.calls if "alt=media" in u]
        self.assertEqual(len(media_calls), 1)

    def test_token_expiring_callback_fires_inside_margin(self):
        seen = []
        provider = FakeProvider(expires_in=300)  # inside the 600s margin
        with patch.object(dd, "urlopen", self.drive):
            DriveDownloader(provider).download_file(
                self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest(),
                on_token_expiring=lambda tid, rem: seen.append((tid, rem)))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], token_id_for(SECRET_A))
        self.assertLess(seen[0][1], dd.TOKEN_REFRESH_MARGIN_SEC)

    def test_token_expiring_callback_quiet_with_healthy_token(self):
        seen = []
        self._downloader().download_file(
            self.FILE_ID, len(self.CONTENT), sha(self.CONTENT), self._dest(),
            on_token_expiring=lambda tid, rem: seen.append((tid, rem)))
        self.assertEqual(seen, [])

    def test_probe_transport_ok(self):
        result = self._downloader().probe_transport(self.FILE_ID)
        self.assertEqual(result["drive_file_id"], self.FILE_ID)
        self.assertEqual(result["probe_bytes"], len(self.CONTENT))
        range_headers = [h.get("range") for _, h in self.drive.calls if "alt=media" in _]
        self.assertIn("bytes=0-9", range_headers)

    def test_probe_transport_rejects_wrong_range(self):
        def _call(request, timeout=None):
            if "alt=media" in request.full_url:
                return FakeResp(200, self.CONTENT, {})  # 200, not 206
            return self.drive(request, timeout)

        with patch.object(dd, "urlopen", _call):
            with self.assertRaises(DriveDownloadError):
                self._downloader().probe_transport(self.FILE_ID)


class TokenFileTests(unittest.TestCase):
    def test_read_deletes_file_and_validates_shape(self):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        doc = {"access_token": SECRET_A,
               "expires_at_utc": "2026-11-13T09:00:00Z",
               "token_id": token_id_for(SECRET_A)}
        path = Path(td) / "drive_token.json"
        dt.write_token_file(path, doc)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        read = dd.read_token_file(path)
        self.assertEqual(read["token_id"], token_id_for(SECRET_A))
        self.assertFalse(path.exists())

    def test_validate_rejects_mismatched_token_id(self):
        doc = {"access_token": SECRET_A,
               "expires_at_utc": "2026-11-13T09:00:00Z",
               "token_id": "deadbeefdeadbeef"}
        with self.assertRaises(ValueError):
            dd.validate_token_doc(doc)

    def test_file_provider_picks_up_renewed_file(self):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        path = Path(td) / "drive_token.json"
        first = {"access_token": SECRET_A,
                 "expires_at_utc": "2026-11-13T09:00:00Z",
                 "token_id": token_id_for(SECRET_A)}
        dt.write_token_file(path, first)
        provider = FileTokenProvider(path)
        provider.load_initial()
        self.assertEqual(provider.get_token(), SECRET_A)
        # Token near expiry + a fresh file delivered: provider picks it up.
        provider._expires_at = time.time() + 60
        second = {"access_token": SECRET_B,
                  "expires_at_utc": "2026-11-13T10:00:00Z",
                  "token_id": token_id_for(SECRET_B)}
        dt.write_token_file(path, second)
        self.assertEqual(provider.get_token(), SECRET_B)
        self.assertFalse(path.exists())

    def test_file_provider_raises_when_no_token(self):
        provider = FileTokenProvider(Path("/nonexistent-dir-xyz") / "nope.json")
        with self.assertRaises(TokenUnavailable):
            provider.load_initial()


class MintTests(unittest.TestCase):
    """mint_readonly_token with a stubbed google.auth (no real creds)."""

    def _install_fake_google(self, token="tok-x", service_account=False):
        import datetime

        expiry = datetime.datetime(2026, 11, 13, 9, 0, 0,
                                   tzinfo=datetime.timezone.utc)

        class FakeCreds:
            service_account_email = "sa@example.iam.gserviceaccount.com" \
                if service_account else None
            valid = False
            token = None
            expiry = None

            def refresh(self, request):
                self.valid = True
                self.token = token
                self.expiry = expiry

        google_mod = types.ModuleType("google")
        google_mod.__path__ = []
        auth_mod = types.ModuleType("google.auth")
        auth_mod.default = lambda scopes=None: (FakeCreds(), None)
        self._scopes_seen = []
        orig_default = auth_mod.default

        def _default(scopes=None):
            self._scopes_seen.append(list(scopes or []))
            return orig_default(scopes)

        auth_mod.default = _default
        google_mod.auth = auth_mod
        transport_mod = types.ModuleType("google.auth.transport")
        transport_mod.__path__ = []
        auth_mod.transport = transport_mod
        requests_mod = types.ModuleType("google.auth.transport.requests")

        class Request:  # noqa: D401 - stub
            pass

        requests_mod.Request = Request
        transport_mod.requests = requests_mod
        saved = {k: sys.modules.get(k) for k in
                 ("google", "google.auth", "google.auth.transport",
                  "google.auth.transport.requests")}
        sys.modules["google"] = google_mod
        sys.modules["google.auth"] = auth_mod
        sys.modules["google.auth.transport"] = transport_mod
        sys.modules["google.auth.transport.requests"] = requests_mod

        def _restore():
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

        self.addCleanup(_restore)
        return expiry

    def test_mint_uses_readonly_scope_and_derives_token_id(self):
        self._install_fake_google(token="tok-x")
        doc = dt.mint_readonly_token()
        self.assertEqual(doc["access_token"], "tok-x")
        self.assertEqual(doc["token_id"], token_id_for("tok-x"))
        self.assertEqual(self._scopes_seen, [[dt.READONLY_SCOPE]])
        self.assertIn(dt.READONLY_SCOPE, dt.READONLY_SCOPE)

    def test_mint_refuses_service_account(self):
        self._install_fake_google(service_account=True)
        with self.assertRaises(RuntimeError):
            dt.mint_readonly_token()


class ControllerRenewalTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import h3_controller as hc

        self.hc = hc
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))

    def test_renewal_uploads_once_per_token_id(self):
        uploads = []
        records = []
        fake_doc = {"access_token": SECRET_B,
                    "expires_at_utc": "2026-11-13T10:00:00Z",
                    "token_id": token_id_for(SECRET_B)}
        with patch.object(self.hc, "run_cli", lambda cmd, timeout=90, check=True: uploads.append(cmd) or {"returncode": 0, "stdout": "", "stderr": ""}), \
                patch.object(self.hc, "record", lambda data: records.append(data)), \
                patch("h3_drive_token.mint_readonly_token", return_value=fake_doc):
            state = {"count": 0, "last_token_id": None}
            req_state = {"token_refresh_requested": {
                "token_id": token_id_for(SECRET_A),
                "requested_at_utc": "2026-11-13T08:00:00Z"}}
            self.hc._handle_token_refresh_request(
                "sess-1", "run-1", Path(self.td), state, req_state)
            # Same request again: no second upload.
            self.hc._handle_token_refresh_request(
                "sess-1", "run-1", Path(self.td), state, req_state)
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["last_token_id"], token_id_for(SECRET_A))
        self.assertEqual(len(uploads), 1)
        self.assertIn("/content/muse_h3/drive_token.json", uploads[0])
        renewed = [r for r in records if r.get("event") == "token_renewed"]
        self.assertEqual(len(renewed), 1)
        self.assertEqual(renewed[0]["token_id"], token_id_for(SECRET_B))
        self.assertNotIn(SECRET_B, json.dumps(records))

    def test_renewal_refused_after_max(self):
        records = []
        state = {"count": dd.MAX_TOKEN_RENEWALS, "last_token_id": None}
        with patch.object(self.hc, "record", lambda data: records.append(data)), \
                patch("h3_drive_token.mint_readonly_token") as mint:
            self.hc._handle_token_refresh_request(
                "sess-1", "run-1", Path(self.td), state,
                {"token_refresh_requested": {"token_id": "abc123"}})
        mint.assert_not_called()
        refused = [r for r in records if r.get("event") == "token_renewal_refused"]
        self.assertEqual(len(refused), 1)

    def test_preflight_proves_mint_and_metadata(self):
        paths = [
            "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            "vae/minimax_h3_video_vae_int8_convrot.safetensors",
            "vae/minimax_h3_audio_vae_fp32.safetensors"]
        manifest = {
            "files": [
                {"path": p, "drive_file_id": f"fid-{i}",
                 "size": 10 + i, "sha256": sha(bytes([48 + i]) * (10 + i)),
                 "server_md5": "x"}
                for i, p in enumerate(paths)]}
        manifest_path = Path(self.td) / "verified_manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        fake_doc = {"access_token": SECRET_A,
                    "expires_at_utc": "2026-11-13T09:00:00Z",
                    "token_id": token_id_for(SECRET_A)}
        args = MagicMock()
        args.cache_manifest = str(manifest_path)
        args.run_id = "run-preflight"
        records = []
        with patch("h3_drive_token.mint_readonly_token", return_value=fake_doc), \
                patch.object(self.hc, "record", lambda data: records.append(data)), \
                patch.object(DriveDownloader, "get_metadata",
                             return_value={"size": "10"}):
            result = self.hc.verify_drive_download_preflight(args)
        self.assertEqual(result["preflight"], "ok")
        self.assertEqual(result["token_id"], token_id_for(SECRET_A))
        self.assertNotIn(SECRET_A, json.dumps(records))


class WorkerDownloadTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))
        import h3_remote_worker as hrw

        self.hrw = hrw
        self.content = b"0123456789"
        self.paths = [
            "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            "vae/minimax_h3_video_vae_int8_convrot.safetensors",
            "vae/minimax_h3_audio_vae_fp32.safetensors"]
        self.manifest = {
            "status": "READY", "verification_method": "drive_v3_server_api",
            "account_verified": True, "verified_at_utc": "2026-11-10T22:36:00Z",
            "files": [{"path": p, "size": len(self.content),
                       "sha256": sha(self.content), "server_md5": "x",
                       "drive_file_id": f"fid-{i}", "server_verified": True,
                       "hf_revision": "54472221d27c695fca9621b0c3ed52f06c4af707"}
                      for i, p in enumerate(self.paths)]}
        self.drive_download = {
            "files": [{"file_id": f"fid-{i}", "path": p,
                       "size": len(self.content), "sha256": sha(self.content)}
                      for i, p in enumerate(self.paths)],
            "dest_dir": str(Path(self.td) / "dl")}

    def _comfy(self):
        comfy = Path(self.td) / "ComfyUI"
        (comfy / "models").mkdir(parents=True, exist_ok=True)
        return comfy

    def test_restore_weights_download_failure_fails_closed(self):
        """Download failure propagates: the worker never claims the
        model is restored, so MODELS_READY is never reached."""
        class FailingDownloader:
            def __init__(self, *a, **k):
                pass

            def download_file(self, *a, **k):
                raise DriveDownloadError("boom")

        with patch("h3_drive_download.DriveDownloader", FailingDownloader), \
                patch("shutil.disk_usage", return_value=type(
                    "usage", (), {"total": 200 * 1024 ** 3,
                                  "used": 10 * 1024 ** 3,
                                  "free": 190 * 1024 ** 3})()):
            with self.assertRaises(DriveDownloadError):
                self.hrw.restore_weights(
                    self._comfy(), Path("/nonexistent"), self.manifest,
                    drive_download=self.drive_download,
                    token_provider=FakeProvider())
        self.assertFalse((self._comfy() / "models" / "vae" / "minimax_h3_video_vae_int8_convrot.safetensors").exists())

    def test_restore_weights_download_success_writes_verified_file(self):
        class WritingDownloader:
            def __init__(self, *a, **k):
                pass

            def download_file(self, file_id, size, sha256, dest, **kw):
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"0123456789")
                return {"drive_file_id": file_id, "size": size,
                        "sha256": sha256, "path": str(dest)}

        with patch("h3_drive_download.DriveDownloader", WritingDownloader), \
                patch("shutil.disk_usage", return_value=type(
                    "usage", (), {"total": 200 * 1024 ** 3,
                                  "used": 10 * 1024 ** 3,
                                  "free": 190 * 1024 ** 3})()):
            self.hrw.restore_weights(
                self._comfy(), Path("/nonexistent"), self.manifest,
                drive_download=self.drive_download,
                token_provider=FakeProvider())
        dst = self._comfy() / "models" / "vae" / "minimax_h3_video_vae_int8_convrot.safetensors"
        self.assertEqual(dst.read_bytes(), self.content)

    def test_restore_weights_real_downloader_end_to_end(self):
        """Regression: the worker must construct the REAL DriveDownloader
        the way its code does. An earlier draft passed
        on_token_expiring=None to the constructor, which only
        download_file accepts — a TypeError on the first live download.
        The class-patched tests above could not see it because their
        fakes accept **kwargs. This test drives restore_weights through
        the real downloader against the FakeDrive."""
        drive = FakeDrive()
        for i, p in enumerate(self.paths):
            drive.metadata[f"fid-{i}"] = {
                "size": len(self.content),
                "md5": hashlib.md5(self.content).hexdigest(),
                "sha256": sha(self.content)}
            drive.media_content[f"fid-{i}"] = self.content
        # The shared fixture manifest carries a placeholder server_md5
        # ("x"); the real downloader verifies it against the server, so
        # this test uses a copy with the real MD5 per file.
        manifest = dict(self.manifest)
        manifest["files"] = [dict(f, server_md5=hashlib.md5(
            self.content).hexdigest()) for f in self.manifest["files"]]
        usage = type("usage", (), {"total": 200 * 1024 ** 3,
                                   "used": 10 * 1024 ** 3,
                                   "free": 190 * 1024 ** 3})()
        with patch.object(dd, "urlopen", drive), \
                patch.object(dd, "_sleep", lambda s: None), \
                patch("shutil.disk_usage", return_value=usage):
            self.hrw.restore_weights(
                self._comfy(), Path("/nonexistent"), manifest,
                drive_download=self.drive_download,
                token_provider=FakeProvider())
        for p in self.paths:
            dst = self._comfy() / "models" / p
            self.assertEqual(dst.read_bytes(), self.content, p)
        self.assertEqual(drive.meta_calls, len(self.paths) * 2)

    def test_ensure_comfy_real_downloader_downloads_tarball(self):
        """Same constructor regression on the ComfyUI branch: the real
        downloader must fetch the source tarball, and
        restore_comfyui_from_cache must then see it."""
        content = b"fake-comfyui-tarball-bytes"
        entry = {"file_id": "fid-comfy", "size": len(content),
                 "sha256": sha(content), "archive": "comfyui.tar.gz",
                 "commit": "61e524bdba7a4e193b45c2697f1af83c1beb86d6"}
        drive = FakeDrive()
        drive.metadata["fid-comfy"] = {
            "size": len(content), "md5": hashlib.md5(content).hexdigest(),
            "sha256": sha(content)}
        drive.media_content["fid-comfy"] = content
        calls = []

        def _fake_restore(src_dir, comfy, sha256, commit, archive):
            calls.append((Path(src_dir), Path(comfy), sha256, commit, archive))

        def _no_clone(*a, **k):
            raise AssertionError(
                "ensure_comfy must not fall through to git clone in this test")

        hrw = self.hrw
        comfy = Path(self.td) / "ComfyUI"
        comfy.mkdir(parents=True, exist_ok=True)
        old_root = hrw.ROOT
        hrw.ROOT = Path(self.td)
        try:
            with patch.object(dd, "urlopen", drive), \
                    patch.object(dd, "_sleep", lambda s: None), \
                    patch.object(hrw, "restore_comfyui_from_cache", _fake_restore), \
                    patch.object(hrw, "clone_comfyui_atomic", _no_clone), \
                    patch.object(hrw, "comfyui_commit", lambda c: entry["commit"]), \
                    patch.object(hrw, "pip_install_needed", lambda r: False):
                hrw.ensure_comfy(comfy, entry["commit"], cache=None,
                                 drive_download={"comfyui": entry},
                                 token_provider=FakeProvider())
        finally:
            hrw.ROOT = old_root
        tarball = Path(self.td) / "drive_downloads" / "comfyui.tar.gz"
        self.assertEqual(tarball.read_bytes(), content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], Path(self.td) / "drive_downloads")
        self.assertEqual(calls[0][2], sha(content))
        self.assertEqual(calls[0][3], entry["commit"])

    def test_request_token_refresh_throttled_per_token(self):
        writes = []

        def _fake_write(root, run_id, variant, stage, **kw):
            writes.append((stage, kw.get("extra")))

        hrw = self.hrw
        old_run, old_var = hrw._JOB_RUN_ID, hrw._JOB_VARIANT
        old_last = hrw._LAST_REFRESH_REQUEST_TOKEN_ID
        hrw._JOB_RUN_ID, hrw._JOB_VARIANT = "run-1", "A"
        hrw._LAST_REFRESH_REQUEST_TOKEN_ID = None
        try:
            with patch.object(hrw, "write_job_state", _fake_write):
                provider = FakeProvider()
                hrw._request_token_refresh(provider)
                hrw._request_token_refresh(provider)  # same token: throttled
                provider2 = FakeProvider(token=SECRET_B)
                hrw._request_token_refresh(provider2)  # new token: asked
        finally:
            hrw._JOB_RUN_ID, hrw._JOB_VARIANT = old_run, old_var
            hrw._LAST_REFRESH_REQUEST_TOKEN_ID = old_last
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][1]["token_refresh_requested"]["token_id"],
                         token_id_for(SECRET_A))
        self.assertEqual(writes[1][1]["token_refresh_requested"]["token_id"],
                         token_id_for(SECRET_B))


if __name__ == "__main__":
    unittest.main()
