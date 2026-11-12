"""Live Drive resumable-upload check (NOT a unit test; run directly).

Creates a deterministic 128 MiB temp file, uploads it twice to a `_tests`
folder in the depot (once with an injected mid-upload network failure to
exercise 308/Range resume, once clean), verifies real server MD5/size/
parent/SHA-256, then deletes every temp file it created (Drive + local).

Usage:
    .venv/bin/python tests/live_drive_resume_check.py
Requires: authorized ADC + depot_config.json. Creates NO GPU, consumes NO CU.
"""
import random
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import google.auth
from google.auth.transport.requests import AuthorizedSession

from drive_api import file_digests, get_file_evidence, verify_uploaded_file
from drive_depot import (
    DEPOT_DIR, drive_service, ensure_folder, read_json, CFG, DRIVE_SCOPE,
)
from drive_resumable import upload_with_persistent_session

SIZE = 128 * 1024 * 1024
TEST_DIR = DEPOT_DIR / '_tests'
REV = '0' * 40


def make_temp_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261110)
    remaining = size
    with path.open('wb') as f:
        while remaining > 0:
            block = rng.randbytes(min(1024 * 1024, remaining))
            f.write(block)
            remaining -= len(block)


class FaultySession:
    """Wraps a real AuthorizedSession; fails exactly one put() call."""

    def __init__(self, real, fail_put_no: int):
        self._real = real
        self._put_count = 0
        self._fail_put_no = fail_put_no
        self.put_log = []  # (content-range, status, range-header)

    def post(self, *a, **k):
        return self._real.post(*a, **k)

    def put(self, *a, **k):
        self._put_count += 1
        if self._put_count == self._fail_put_no:
            raise ConnectionError('injected breakpoint after first chunk')
        r = self._real.put(*a, **k)
        headers = k.get('headers', {}) or {}
        self.put_log.append((headers.get('Content-Range'), r.status_code,
                             r.headers.get('Range')))
        return r


def main() -> int:
    service = drive_service()
    root_id = read_json(CFG)['drive_root_folder_id']
    folder_id = ensure_folder(service, root_id, '_tests', allow_write=True)
    local = TEST_DIR / 'resume_128m.bin'
    created_ids = []
    try:
        print('making deterministic 128 MiB temp file...', flush=True)
        make_temp_file(local, SIZE)
        digests = file_digests(local)
        print('local size', digests['size'], 'md5', digests['md5'][:16] + '...',
              'sha256', digests['sha256'][:16] + '...', flush=True)

        creds, _ = google.auth.default(scopes=[DRIVE_SCOPE])

        # --- Upload A: injected breakpoint, must resume via 308/Range ---
        faulty = FaultySession(AuthorizedSession(creds), fail_put_no=3)
        sidecar_a = DEPOT_DIR / '.upload_sessions' / 'live_resume_a.json'
        print('upload A (fault injected on 3rd put)...', flush=True)
        res_a = upload_with_persistent_session(
            faulty, local, 'resume_128m.bin', folder_id,
            '_tests/resume_128m.bin', REV, sidecar_a, digests['sha256'])
        created_ids.append(res_a['id'])
        print('upload A complete, file id', res_a['id'], flush=True)
        resumed = [e for e in faulty.put_log
                   if e[0] == 'bytes 0-8388607/134217728' and e[1] == 308
                   and e[2] == 'bytes=0-8388607']
        if not resumed:
            raise RuntimeError('308/Range resume not observed in put log: ' +
                               repr(faulty.put_log[:6]))
        print('308/Range resume observed: re-PUT of chunk 0 returned 308 bytes=0-8388607',
              flush=True)
        proof_a = verify_uploaded_file(service, res_a['id'], local)
        print('server verify A:', proof_a['size'], proof_a['server_md5'][:16] + '...',
              'server_sha256_present', bool(proof_a.get('server_sha256')), flush=True)

        # --- Upload B: clean path, same bytes, different name ---
        sidecar_b = DEPOT_DIR / '.upload_sessions' / 'live_resume_b.json'
        print('upload B (clean)...', flush=True)
        res_b = upload_with_persistent_session(
            AuthorizedSession(creds), local, 'clean_128m.bin', folder_id,
            '_tests/clean_128m.bin', REV, sidecar_b, digests['sha256'])
        created_ids.append(res_b['id'])
        proof_b = verify_uploaded_file(service, res_b['id'], local)
        print('server verify B:', proof_b['size'], proof_b['server_md5'][:16] + '...',
              flush=True)
        for fid in created_ids:
            meta = get_file_evidence(service, fid)
            assert folder_id in meta.get('parents', []), 'parent mismatch'
            assert int(meta['size']) == SIZE, 'size mismatch'
        print('LIVE_DRIVE_RESUME_OK files', len(created_ids), 'bytes_each', SIZE, flush=True)
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        for fid in created_ids:
            try:
                service.files().delete(fileId=fid, supportsAllDrives=True).execute()
                print('deleted Drive temp file', fid, flush=True)
            except Exception as e:
                print('cleanup delete failed for', fid, ':', str(e)[:100], flush=True)
        try:
            if local.exists():
                local.unlink()
                print('deleted local temp file', local, flush=True)
        except Exception as e:
            print('cleanup local failed:', str(e)[:100], flush=True)
        # Remove the _tests folder if it is now empty.
        try:
            remaining = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces='drive', pageSize=10, fields='files(id)',
                supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get('files', [])
            if not remaining:
                service.files().delete(fileId=folder_id, supportsAllDrives=True).execute()
                print('deleted empty _tests folder', flush=True)
        except Exception as e:
            print('cleanup folder failed:', str(e)[:100], flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
