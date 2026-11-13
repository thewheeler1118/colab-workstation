"""TASK-006 P1-A (Issue #48): offline simulator for the worker-side
resumable Drive upload, the drive.file token, the state-machine
migration, and the controller's remote-upload recording + resume ladder.

No test here touches a real GPU, real Drive, or real credentials. The
fake upload server scripts HTTP behaviour (initiate / 308 / offset
query / 200 / 401 / 403 / 429 / 5xx / mid-chunk drops / expired
sessions / response loss). Fake token values are SECRET-TOKEN-*; every
failure test asserts the value appears in NO exception message or
return value.
"""
import base64
import hashlib
import json
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import h3_drive_token as dt
import h3_drive_upload as du
import h3_run_manifest as rm
from h3_drive_upload import (
    DriveAuthError,
    DriveConflictError,
    DriveUploadError,
    DriveUploadRetryExhaustedError,
    DriveUploader,
    FileTokenProvider,
    token_id_for,
)

SECRET_A = "SECRET-TOKEN-AAAA"
SECRET_B = "SECRET-TOKEN-BBBB"
FOLDER = "folder-run-1"
RUN_ID = "h3-test-run"
CHUNK = 256 * 1024  # uploader chunk_size must be a multiple of 256 KiB


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def md5_b64(b: bytes) -> str:
    return base64.b64encode(hashlib.md5(b).digest()).decode("ascii")


def _http_error(url, code, headers=None):
    return urllib.error.HTTPError(url, code, f"HTTP {code}", headers or {}, None)


class FakeResp:
    """Minimal urlopen response: status, headers, read."""

    def __init__(self, status, content=b"", headers=None):
        self.status = status
        self._content = content
        self.headers = headers or {}
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._content) - self._pos
        chunk = self._content[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class FakeUploadDrive:
    """Scripted fake Drive upload server behind h3_drive_upload.urlopen.

    files: {file_id: {name, size, md5, sha256, parents, appProperties,
    content}} — the server-side record.
    sessions: {session_uri: {folder_id, name, run_id, variant, size,
    received: bytearray, mime}}.
    put_plan: [(status_or_raise, ...)] consumed per chunk PUT, in order.
    A ("drop_final_response",) entry finalises the file server-side
    but raises ConnectionError instead of returning the 200 — the
    'upload succeeded but the response was lost' case.
    """

    def __init__(self):
        self.files = {}
        self.sessions = {}
        self.calls = []  # (method, url, headers)
        self.put_plan = []
        self.initiations = 0
        self.corrupt_readback = False
        self._next_id = 1
        self._next_session = 1

    def _new_file(self, session, content: bytes):
        fid = f"drive-file-{self._next_id}"
        self._next_id += 1
        self.files[fid] = {
            "id": fid, "name": session["name"], "size": len(content),
            "md5Checksum": md5_b64(content), "sha256Checksum": sha(content),
            "parents": [session["folder_id"]],
            "appProperties": {"run_id": session["run_id"],
                              "variant": session["variant"]},
            "trashed": False, "content": bytes(content)}
        return fid

    def __call__(self, request, timeout=None):
        url = request.full_url
        method = request.get_method()
        headers = {k.lower(): v for k, v in
                   dict(getattr(request, "headers", {}) or {}).items()}
        self.calls.append((method, url, headers))
        if method == "POST" and "uploadType=resumable" in url:
            return self._initiate(request, url, headers)
        if method == "PUT" and url.startswith("https://www.googleapis.com/upload/"):
            return self._put(request, url, headers)
        if method == "GET" and "/files?q=" in url:
            return self._list(url)
        if method == "GET" and "/files/" in url and "alt=media" not in url:
            return self._metadata(url)
        if method == "GET" and "alt=media" in url:
            return self._media(url)
        raise AssertionError("unexpected fake-upload URL: " + method + " " + url)

    def _initiate(self, request, url, headers):
        self.initiations += 1
        meta = json.loads(request.data.decode("utf-8"))
        uri = ("https://www.googleapis.com/upload/drive/v3/files"
               f"?uploadType=resumable&upload_id=s{self._next_session}")
        self._next_session += 1
        self.sessions[uri] = {
            "folder_id": meta["parents"][0], "name": meta["name"],
            "run_id": (meta.get("appProperties") or {}).get("run_id", ""),
            "variant": (meta.get("appProperties") or {}).get("variant", ""),
            "size": int(headers.get("x-upload-content-length", "0")),
            "received": bytearray(), "mime": meta.get("mimeType", "")}
        return FakeResp(200, b"", {"Location": uri})

    def _put(self, request, url, headers):
        session = self.sessions.get(url)
        if session is None:
            raise _http_error(url, 404)
        content_range = headers.get("content-range", "")
        if content_range.startswith("bytes */"):
            total = int(content_range.split("/", 1)[1])
            received = len(session["received"])
            if received >= total:
                fid = self._finalise(session)
                return FakeResp(200, json.dumps({"id": fid}).encode(),
                                {"Content-Type": "application/json"})
            return self._raise_308(received)
        if self.put_plan:
            action = self.put_plan.pop(0)
            if action[0] == "raise":
                raise action[1]
            if action[0] == "drop_final_response":
                data = request.data or b""
                start = int(content_range.split(" ", 1)[1].split("-", 1)[0])
                session["received"][start:start + len(data)] = data
                if start + len(data) >= session["size"]:
                    self._finalise(session)
                raise ConnectionError("response lost")
            if action[0] == "status":
                raise _http_error(url, action[1])
            # ("ok",) falls through to the normal handling below.
        data = request.data or b""
        start = int(content_range.split(" ", 1)[1].split("-", 1)[0])
        end = int(content_range.split("-", 1)[1].split("/", 1)[0])
        total = int(content_range.split("/", 1)[1])
        if start != len(session["received"]):
            # Out-of-order chunk: the server keeps what it has.
            return self._raise_308(len(session["received"]))
        session["received"][start:start + len(data)] = data
        if end + 1 >= total:
            fid = self._finalise(session)
            return FakeResp(200, json.dumps({"id": fid}).encode(),
                            {"Content-Type": "application/json"})
        return self._raise_308(len(session["received"]))

    def _finalise(self, session):
        for fid, f in self.files.items():
            if f["name"] == session["name"] and \
                    f["appProperties"].get("run_id") == session["run_id"] and \
                    f["sha256Checksum"] == sha(bytes(session["received"])):
                return fid
        return self._new_file(session, bytes(session["received"]))

    @staticmethod
    def _raise_308(received):
        headers = {}
        if received > 0:
            headers["Range"] = f"bytes=0-{received - 1}"
        raise _http_error("https://www.googleapis.com/upload/drive/v3/files",
                          308, headers)

    def _list(self, url):
        out = []
        for f in self.files.values():
            if f.get("trashed"):
                continue
            out.append({k: f[k] for k in
                        ("id", "name", "size", "md5Checksum", "sha256Checksum",
                         "appProperties") if k in f})
        return FakeResp(200, json.dumps({"files": out}).encode(),
                        {"Content-Type": "application/json"})

    def _metadata(self, url):
        fid = url.split("/files/")[1].split("?")[0]
        f = self.files.get(fid)
        if f is None:
            raise _http_error(url, 404)
        body = json.dumps({
            "id": f["id"], "name": f["name"], "size": str(f["size"]),
            "md5Checksum": f["md5Checksum"], "sha256Checksum": f["sha256Checksum"],
            "trashed": f["trashed"], "parents": f["parents"]}).encode()
        return FakeResp(200, body, {"Content-Type": "application/json"})

    def _media(self, url):
        fid = url.split("/files/")[1].split("?")[0]
        f = self.files.get(fid)
        if f is None:
            raise _http_error(url, 404)
        content = f["content"]
        if self.corrupt_readback:
            content = b"X" + content[1:]
        return FakeResp(200, content)


class FakeProvider:
    """Stand-in for FileTokenProvider with scripted tokens."""

    def __init__(self, token=None, expires_in=3600, refresh_token=None):
        self._token = SECRET_A if token is None else token
        self._expires = time.time() + expires_in
        self.refresh_token = "refresh-x" if refresh_token is None else refresh_token
        self.refresh_calls = 0

    def get_token(self):
        return self._token

    def refresh(self):
        self.refresh_calls += 1
        self._token = SECRET_B
        self._expires = time.time() + 3600
        return self._token

    @property
    def token_id(self):
        return token_id_for(self._token)

    def seconds_remaining(self):
        return self._expires - time.time()


class UploadTestBase(unittest.TestCase):
    CONTENT = bytes(range(256)) * 3000  # 768 KiB -> 3 chunks at 256 KiB

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.fake = FakeUploadDrive()
        self.provider = FakeProvider()
        self.uploader = DriveUploader(self.provider, chunk_size=CHUNK,
                                      max_attempts=4, backoff_base_sec=0)
        self.patch_urlopen = patch.object(du, "urlopen", self.fake)
        self.patch_urlopen.start()
        self.patch_sleep = patch.object(du, "_sleep", lambda s: None)
        self.patch_sleep.start()
        self.addCleanup(self.patch_urlopen.stop)
        self.addCleanup(self.patch_sleep.stop)
        self.addCleanup(self.td.cleanup)

    def sidecar(self):
        return self.root / "A.upload_state.json"

    def upload(self, **over):
        kw = {"local_path": self.root / "A.mp4", "folder_id": FOLDER,
              "name": "A.mp4", "run_id": RUN_ID, "variant": "A",
              "expected_sha256": sha(self.CONTENT),
              "sidecar_path": self.sidecar()}
        kw.update(over)
        return self.uploader.upload_file(**kw)

    def write_content(self):
        (self.root / "A.mp4").write_bytes(self.CONTENT)


class HappyPathTests(UploadTestBase):
    def test_multichunk_upload_and_verify(self):
        self.write_content()
        evidence = self.upload()
        self.assertEqual(evidence["size"], len(self.CONTENT))
        self.assertEqual(evidence["sha256"], sha(self.CONTENT))
        self.assertFalse(evidence["reused"])
        self.assertEqual(self.fake.initiations, 1)
        verified = self.uploader.verify_uploaded(
            evidence["drive_file_id"], len(self.CONTENT), sha(self.CONTENT),
            expected_md5=md5_b64(self.CONTENT), parent_id=FOLDER)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["read_back_bytes"], len(self.CONTENT))
        self.assertFalse(self.sidecar().exists())
        self.assertNotIn(SECRET_A, json.dumps(evidence))

    def test_reconcile_reuses_matching_file_without_initiate(self):
        self.write_content()
        first = self.upload()
        second = self.upload()
        self.assertTrue(second["reused"])
        self.assertEqual(second["reused_via"], "reconcile_existing")
        self.assertEqual(second["drive_file_id"], first["drive_file_id"])
        self.assertEqual(self.fake.initiations, 1)
        self.assertEqual(len(self.fake.files), 1)

    def test_same_name_different_sha_is_conflict(self):
        self.write_content()
        self.upload()
        other = bytes([7]) * len(self.CONTENT)
        (self.root / "A.mp4").write_bytes(other)
        with self.assertRaises(DriveConflictError):
            self.upload(expected_sha256=sha(other))
        self.assertEqual(self.fake.initiations, 1)
        self.assertEqual(len(self.fake.files), 1)

    def test_local_sha_mismatch_refuses_before_any_call(self):
        self.write_content()
        with self.assertRaises(DriveUploadError):
            self.upload(expected_sha256=sha(b"not the content"))
        self.assertEqual(self.fake.initiations, 0)
        self.assertEqual(self.fake.calls, [])


class ResumeTests(UploadTestBase):
    def test_mid_chunk_drop_resumes_from_offset(self):
        self.write_content()
        self.fake.put_plan = [("raise", ConnectionError("dropped"))]
        evidence = self.upload()
        self.assertEqual(evidence["sha256"], sha(self.CONTENT))
        self.assertEqual(self.fake.initiations, 1)
        stored = next(iter(self.fake.files.values()))
        self.assertEqual(stored["content"], self.CONTENT)

    def test_expired_session_reconciles_then_restarts(self):
        self.write_content()
        # First PUT 404s the session away; the uploader reconciles (no
        # file yet), starts a fresh session, and completes.
        self.fake.put_plan = [("status", 404)]
        evidence = self.upload()
        self.assertEqual(evidence["sha256"], sha(self.CONTENT))
        self.assertEqual(self.fake.initiations, 2)

    def test_sidecar_resume_across_uploader_instances(self):
        self.write_content()
        # Chunk 0 succeeds, then every chunk-1 PUT drops: the sidecar
        # keeps the first chunk's offset and the upload exhausts.
        self.fake.put_plan = ([("ok",)] +
                              [("raise", ConnectionError("dropped"))] * 10)
        with self.assertRaises(DriveUploadRetryExhaustedError):
            self.upload()
        self.assertTrue(self.sidecar().exists())
        sidecar = json.loads(self.sidecar().read_text())
        self.assertEqual(sidecar["uploaded_bytes"], CHUNK)
        # A fresh uploader with the same sidecar resumes the SAME
        # session (no second initiate) and completes.
        self.fake.put_plan = []
        uploader2 = DriveUploader(self.provider, chunk_size=CHUNK,
                                  max_attempts=4, backoff_base_sec=0)
        evidence = uploader2.upload_file(
            self.root / "A.mp4", FOLDER, "A.mp4", RUN_ID, "A",
            sha(self.CONTENT), self.sidecar())
        self.assertEqual(evidence["sha256"], sha(self.CONTENT))
        self.assertEqual(self.fake.initiations, 1)

    def test_response_lost_after_final_chunk_recovers_via_reconcile(self):
        self.write_content()
        # The first two chunks succeed; the final chunk's 200 is lost.
        self.fake.put_plan = [("ok",), ("ok",), ("drop_final_response",)]
        evidence = self.upload()
        self.assertEqual(evidence["sha256"], sha(self.CONTENT))
        self.assertEqual(len(self.fake.files), 1)


class AuthTests(UploadTestBase):
    def test_401_refreshes_once_then_succeeds(self):
        self.write_content()
        self.fake.put_plan = [("status", 401)]
        evidence = self.upload()
        self.assertEqual(evidence["sha256"], sha(self.CONTENT))
        self.assertEqual(self.provider.refresh_calls, 1)

    def test_second_401_fails_closed(self):
        self.write_content()
        self.fake.put_plan = [("status", 401), ("status", 401)]
        with self.assertRaises(DriveAuthError) as ctx:
            self.upload()
        self.assertNotIn(SECRET_A, str(ctx.exception))
        self.assertNotIn(SECRET_B, str(ctx.exception))

    def test_403_fails_immediately(self):
        self.write_content()
        self.fake.put_plan = [("status", 403)]
        with self.assertRaises(DriveAuthError):
            self.upload()
        self.assertEqual(self.provider.refresh_calls, 0)

    def test_transient_5xx_retries_then_succeeds(self):
        self.write_content()
        self.fake.put_plan = [("status", 503), ("status", 500)]
        evidence = self.upload()
        self.assertEqual(evidence["sha256"], sha(self.CONTENT))

    def test_transient_exhaustion_keeps_sidecar(self):
        self.write_content()
        self.fake.put_plan = [("status", 503)] * 10
        with self.assertRaises(DriveUploadRetryExhaustedError):
            self.upload()
        self.assertTrue(self.sidecar().exists())


class VerifyTests(UploadTestBase):
    def test_verify_rejects_size_mismatch(self):
        self.write_content()
        evidence = self.upload()
        with self.assertRaises(DriveUploadError):
            self.uploader.verify_uploaded(
                evidence["drive_file_id"], len(self.CONTENT) + 1,
                sha(self.CONTENT))

    def test_verify_rejects_md5_mismatch(self):
        self.write_content()
        evidence = self.upload()
        with self.assertRaises(DriveUploadError):
            self.uploader.verify_uploaded(
                evidence["drive_file_id"], len(self.CONTENT),
                sha(self.CONTENT), expected_md5=md5_b64(b"other"))

    def test_verify_rejects_wrong_parent(self):
        self.write_content()
        evidence = self.upload()
        with self.assertRaises(DriveUploadError):
            self.uploader.verify_uploaded(
                evidence["drive_file_id"], len(self.CONTENT),
                sha(self.CONTENT), parent_id="some-other-folder")

    def test_verify_rejects_corrupt_readback(self):
        self.write_content()
        evidence = self.upload()
        self.fake.corrupt_readback = True
        with self.assertRaises(DriveUploadError):
            self.uploader.verify_uploaded(
                evidence["drive_file_id"], len(self.CONTENT),
                sha(self.CONTENT))


class MintTests(unittest.TestCase):
    """mint_file_token uses the drive.file scope, nothing broader.

    google.auth is NOT installed under system python3, so the tests
    stub the google.auth package in sys.modules (the same shape the
    real mint functions import: google.auth.default + the Request
    transport class).
    """

    def _install_fake_google_auth(self, scopes_seen):
        import datetime

        fake_requests = types.ModuleType("google.auth.transport.requests")

        class FakeRequest:
            pass

        fake_requests.Request = FakeRequest
        fake_transport = types.ModuleType("google.auth.transport")
        fake_transport.requests = fake_requests

        class FakeCreds:
            service_account_email = None
            valid = True
            token = SECRET_A

            def __init__(self):
                self.expiry = datetime.datetime(2026, 11, 13, 10, 0, 0)

        def fake_default(scopes=None):
            scopes_seen["scopes"] = scopes
            return FakeCreds(), None

        fake_auth = types.ModuleType("google.auth")
        fake_auth.default = fake_default
        fake_auth.transport = fake_transport
        google_pkg = types.ModuleType("google")
        google_pkg.__path__ = []
        google_pkg.auth = fake_auth
        added = []
        for name, mod in (("google", google_pkg),
                          ("google.auth", fake_auth),
                          ("google.auth.transport", fake_transport),
                          ("google.auth.transport.requests", fake_requests)):
            if name not in sys.modules:
                sys.modules[name] = mod
                added.append(name)
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in added])

    def test_file_token_scope(self):
        seen = {}
        self._install_fake_google_auth(seen)
        doc = dt.mint_file_token()
        self.assertEqual(seen["scopes"], [dt.WRITE_FILE_SCOPE])
        self.assertEqual(doc["token_id"], token_id_for(SECRET_A))
        self.assertNotIn(SECRET_A, json.dumps({k: v for k, v in doc.items()
                                               if k != "access_token"}))

    def test_readonly_token_scope_unchanged(self):
        seen = {}
        self._install_fake_google_auth(seen)
        dt.mint_readonly_token()
        self.assertEqual(seen["scopes"], [dt.READONLY_SCOPE])


class WorkerUploadTests(unittest.TestCase):
    """The real worker upload path against the fake upload server."""

    CONTENT = bytes(range(256)) * 40  # ~10 KiB fake MP4 bytes

    def setUp(self):
        import h3_remote_worker as hw

        self.hw = hw
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.mp4 = self.root / "A.mp4"
        self.mp4.write_bytes(self.CONTENT)
        self.fake = FakeUploadDrive()
        self.info = {"file": str(self.mp4), "frames": 274,
                     "seconds": 274 / 24, "sha256": sha(self.CONTENT),
                     "with_audio": True}
        self.task = {"variant": "A", "run_id": RUN_ID,
                     "drive_upload": {"folder_id": FOLDER, "file_name": "A.mp4"}}
        dt.write_token_file(
            self.root / "drive_upload_token.json",
            {"access_token": SECRET_A,
             "expires_at_utc": "2030-01-01T10:00:00Z",
             "token_id": token_id_for(SECRET_A)})
        self.patches = [
            patch.object(hw, "ROOT", self.root),
            patch.object(hw, "ffprobe", lambda p: {
                "streams": [{"codec_type": "video", "nb_read_frames": "274",
                             "r_frame_rate": "24/1", "width": 1080,
                             "height": 1920}]}),
            patch("subprocess.run", return_value=unittest.mock.MagicMock(returncode=0)),
            patch.object(du, "urlopen", self.fake),
            patch.object(du, "_sleep", lambda s: None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.addCleanup(self.td.cleanup)

    def test_happy_path_returns_worker_evidence(self):
        evidence = self.hw.upload_remote_archive(
            self.mp4, self.info, self.task, RUN_ID, "A")
        self.assertEqual(evidence["sha256"], sha(self.CONTENT))
        self.assertEqual(evidence["md5"], md5_b64(self.CONTENT))
        self.assertEqual(evidence["uploaded_by"], "worker")
        self.assertEqual(evidence["verified_by"], "worker_readback")
        self.assertEqual(evidence["read_back_bytes"], len(self.CONTENT))
        self.assertFalse(evidence["reused"])
        self.assertEqual(evidence["drive_output_folder_id"], FOLDER)
        self.assertNotIn(SECRET_A, json.dumps(evidence))

    def test_validation_rejects_wrong_frame_count(self):
        with patch.object(self.hw, "ffprobe", lambda p: {
                "streams": [{"codec_type": "video", "nb_read_frames": "100",
                             "r_frame_rate": "24/1", "width": 1080,
                             "height": 1920}]}):
            with self.assertRaises(ValueError):
                self.hw.upload_remote_archive(
                    self.mp4, self.info, self.task, RUN_ID, "A")
        self.assertEqual(self.fake.initiations, 0)

    def test_no_drive_upload_block_returns_none(self):
        self.assertIsNone(self.hw.upload_remote_archive(
            self.mp4, self.info, {"variant": "A"}, RUN_ID, "A"))
        self.assertEqual(self.fake.calls, [])

    def test_upload_failure_raises_for_caller_to_record(self):
        self.fake.put_plan = [("status", 503)] * 10
        with self.assertRaises(DriveUploadRetryExhaustedError):
            self.hw.upload_remote_archive(
                self.mp4, self.info, self.task, RUN_ID, "A")


class ControllerRecordTests(unittest.TestCase):
    """Controller-side recording of worker upload evidence, the
    scope-aware renewal handler, and the upload-block builder."""

    def setUp(self):
        import h3_controller as hc

        self.hc = hc
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "model_cache").mkdir()
        self.events = []
        self.patches = [
            patch.object(hc, "ROOT", self.root),
            patch.object(hc, "record", lambda data: self.events.append(data)),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.addCleanup(self.tmp.cleanup)
        rm.create_run_manifest(self.root, "h3-ab-20261111", RUN_ID, variant="A")

    def _evidence(self):
        return {"drive_file_id": "drive-file-7", "size": 1234,
                "sha256": "b" * 64, "md5": md5_b64(b"x"),
                "uploaded_by": "worker", "verified_by": "worker_readback",
                "upload_started_at_utc": "2026-11-13T08:00:00Z",
                "upload_finished_at_utc": "2026-11-13T08:01:00Z",
                "reused": False, "reused_via": None,
                "drive_output_folder_id": FOLDER, "read_back_bytes": 1234}

    def test_record_remote_upload_marks_manifest_and_dedupes(self):
        state = {"remote_upload": self._evidence()}
        got = self.hc._record_remote_upload(RUN_ID, state)
        self.assertEqual(got["drive_file_id"], "drive-file-7")
        m = rm.load_run_manifest(self.root, RUN_ID)
        self.assertEqual(m["artifacts"]["delivery_state"],
                         "REMOTE_UPLOAD_VERIFIED")
        self.assertEqual(m["artifacts"]["drive_file_id"], "drive-file-7")
        self.assertTrue(m["artifacts"]["drive_verified_before_stop"])
        self.assertEqual(m["artifacts"]["remote_upload"]["drive_file_id"],
                         "drive-file-7")
        # 08:00:00 -> 08:01:00 in the fixture evidence.
        self.assertEqual(m["artifacts"]["upload_wall_seconds"], 60.0)
        self.assertEqual(len([e for e in self.events
                              if e.get("event") == "remote_upload_verified"]), 1)
        # A second poll carrying the same evidence records no new event.
        self.hc._record_remote_upload(RUN_ID, state)
        self.assertEqual(len([e for e in self.events
                              if e.get("event") == "remote_upload_verified"]), 1)

    def test_record_remote_upload_failed_event(self):
        self.hc._record_remote_upload(
            RUN_ID, {"remote_upload_failed": {"error_type": "DriveAuthError",
                                              "error": "token expired"}})
        self.assertEqual(len([e for e in self.events
                              if e.get("event") == "remote_upload_failed"]), 1)

    def test_renewal_serves_file_scope_to_upload_token_file(self):
        minted = {}

        def fake_file_token():
            minted["scope"] = "file"
            return {"access_token": SECRET_B,
                    "expires_at_utc": "2030-01-01T10:00:00Z",
                    "token_id": token_id_for(SECRET_B)}

        uploads = []
        state = {"count": 0, "last_token_id": None}
        with patch.object(dt, "mint_file_token", fake_file_token), \
                patch.object(dt, "mint_readonly_token",
                             side_effect=AssertionError("wrong mint")), \
                patch.object(self.hc, "_upload_drive_token",
                             lambda s, d, doc, name: uploads.append(name)):
            self.hc._handle_token_refresh_request(
                "sess-1", RUN_ID, Path(self.tmp.name), state,
                {"token_refresh_requested": {"token_id": "tid-file",
                                             "scope": "drive.file"}})
        self.assertEqual(minted.get("scope"), "file")
        self.assertEqual(uploads, ["drive_upload_token.json"])
        self.assertEqual(state["count"], 1)
        renewed = [e for e in self.events if e.get("event") == "token_renewed"]
        self.assertEqual(renewed[0]["remote_name"], "drive_upload_token.json")

    def test_renewal_defaults_to_readonly_download_token(self):
        uploads = []
        state = {"count": 0, "last_token_id": None}
        fake_doc = {"access_token": SECRET_B,
                    "expires_at_utc": "2030-01-01T10:00:00Z",
                    "token_id": token_id_for(SECRET_B)}
        with patch.object(dt, "mint_readonly_token", return_value=fake_doc), \
                patch.object(dt, "mint_file_token",
                             side_effect=AssertionError("wrong mint")), \
                patch.object(self.hc, "_upload_drive_token",
                             lambda s, d, doc, name: uploads.append(name)):
            self.hc._handle_token_refresh_request(
                "sess-1", RUN_ID, Path(self.tmp.name), state,
                {"token_refresh_requested": {"token_id": "tid-ro"}})
        self.assertEqual(uploads, ["drive_token.json"])

    def test_build_drive_upload_block_creates_run_folder(self):
        import drive_depot

        seen = []

        def fake_ensure(service, parent, name, allow_write):
            seen.append((parent, name))
            return f"id-{name}"

        with patch.object(drive_depot, "drive_service",
                          return_value=object()), \
                patch.object(drive_depot, "ensure_folder",
                             side_effect=fake_ensure):
            block = self.hc._build_drive_upload_block(RUN_ID, "A")
        self.assertEqual(block["folder_id"], f"id-{RUN_ID}")
        self.assertEqual(block["file_name"], "A.mp4")
        self.assertEqual(block["run_id"], RUN_ID)
        self.assertEqual(seen, [(None, "Muse-H3-Video-Outputs"),
                                ("id-Muse-H3-Video-Outputs", RUN_ID)])


if __name__ == "__main__":
    unittest.main()
