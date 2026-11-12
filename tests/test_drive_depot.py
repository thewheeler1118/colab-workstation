import sys,tempfile,unittest,json,subprocess
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import drive_depot as d

class TestDepot(unittest.TestCase):
    def test_atomic_json(self):
        with tempfile.TemporaryDirectory() as td:
            file=Path(td)/'a.json';d.atomic_json(file,{'s':'READY'});self.assertEqual(d.read_json(file)['s'],'READY')
    def test_reject_missing_account(self):
        with self.assertRaisesRegex(RuntimeError,'account'):
            d.verify_ready(MagicMock(),False,Path('not_a_file.json'))
    def test_reject_wrong_server_md5(self):
        srv=MagicMock()
        with patch.object(d,'get_file_evidence',return_value={'id':'123','size':'123','md5Checksum':'0'*32,'parents':['p'],'mimeType':'application/octet-stream'}):
            with self.assertRaisesRegex(RuntimeError,'MD5'):
                d.verify_drive_entry(srv,{'path':'vae/a','size':123,'drive_file_id':'123','parent_id':'p','server_md5':'1'*32,'sha256':'a'*64})
    def test_reject_wrong_server_parent(self):
        with patch.object(d,'get_file_evidence',return_value={'id':'123','size':'123','md5Checksum':'0'*32,'parents':['other'],'mimeType':'application/octet-stream'}):
            with self.assertRaisesRegex(RuntimeError,'Parent'):
                d.verify_drive_entry(MagicMock(),{'path':'vae/a','size':123,'drive_file_id':'123','parent_id':'p','server_md5':'0'*32,'sha256':'a'*64})
    def test_accept_live_server_metadata(self):
        with patch.object(d,'get_file_evidence',return_value={'id':'123','size':'123','md5Checksum':'0'*32,'parents':['p'],'mimeType':'application/octet-stream'}):
            r=d.verify_drive_entry(MagicMock(),{'path':'vae/a','size':123,'drive_file_id':'123','parent_id':'p','server_md5':'0'*32,'sha256':'a'*64})
        self.assertEqual(r['size'],123)
    def test_reject_server_sha256_mismatch(self):
        with patch.object(d,'get_file_evidence',return_value={'id':'123','size':'123','md5Checksum':'0'*32,'sha256Checksum':'b'*64,'parents':['p'],'mimeType':'application/octet-stream'}):
            with self.assertRaisesRegex(RuntimeError,'SHA-256'):
                d.verify_drive_entry(MagicMock(),{'path':'vae/a','size':123,'drive_file_id':'123','parent_id':'p','server_md5':'0'*32,'sha256':'a'*64})
    def test_accept_server_sha256_match(self):
        with patch.object(d,'get_file_evidence',return_value={'id':'123','size':'123','md5Checksum':'0'*32,'sha256Checksum':'a'*64,'parents':['p'],'mimeType':'application/octet-stream'}):
            r=d.verify_drive_entry(MagicMock(),{'path':'vae/a','size':123,'drive_file_id':'123','parent_id':'p','server_md5':'0'*32,'sha256':'a'*64})
        self.assertTrue(r['server_sha256_available'])
    def test_do_not_overwrite_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'w.safetensors';f.write_bytes(b'good')
            with patch.object(d,'list_named',return_value=[{'id':'bad'}]),patch.object(d,'get_file_evidence',return_value={'size':3,'md5Checksum':'wrong'}):
                with self.assertRaisesRegex(RuntimeError,'DO NOT overwrite'):
                    d.upload_one(MagicMock(),f,'folder','w.safetensors','vae/w.safetensors','a'*64,'b'*40)
    def test_account_match_no_email_leak(self):
        s=MagicMock();s.about().get().execute.return_value={'user':{'emailAddress':'someone@example.com'}}
        mock_p=subprocess.CompletedProcess(['colab'],0,stdout='Auth provider: oauth2\nEmail: someone@example.com\n',stderr='')
        with patch.object(d.shutil,'which',return_value='/bin/true'),patch.object(d.subprocess,'run',return_value=mock_p):
            self.assertTrue(d.account_matches(s,'oauth2'))
        s.about().get().execute.return_value={'user':{'emailAddress':'other@example.com'}}
        with patch.object(d.shutil,'which',return_value='/bin/true'),patch.object(d.subprocess,'run',return_value=mock_p):
            self.assertFalse(d.account_matches(s,'oauth2'))
    def test_h3_controller_has_online_gate(self):
        # 2026-11-11: single-variant run uses allocation_command() (policy
        # module) instead of a literal run_cli(['new'...]) call. Intent is
        # unchanged: live Drive READY verification must precede any GPU
        # allocation.
        text=(Path(__file__).resolve().parents[1]/'h3_controller.py').read_text()
        self.assertIn('verify_ready(drive, same_account',text)
        self.assertIn('allocation_command(session, chosen_key',text)
        self.assertLess(text.index('verify_ready(drive, same_account'),text.index('allocation_command(session, chosen_key'))
    def test_seed_requires_write_and_big_transfer_flags(self):
        with patch.object(sys,'argv',['drive_depot.py','seed']):
            with self.assertRaises(SystemExit) as cm:d.cli()
        self.assertEqual(cm.exception.code,2)
    def test_transient_drive_error_classification(self):
        import ssl, http.client
        self.assertTrue(d._is_transient_drive_error(ssl.SSLEOFError(8, 'EOF occurred in violation of protocol')))
        self.assertTrue(d._is_transient_drive_error(ssl.SSLError('tls fail')))
        self.assertTrue(d._is_transient_drive_error(ConnectionResetError('reset')))
        self.assertTrue(d._is_transient_drive_error(http.client.RemoteDisconnected('closed')))
        self.assertTrue(d._is_transient_drive_error(TimeoutError('timed out')))
        self.assertFalse(d._is_transient_drive_error(RuntimeError('Hugging Face LFS checksum mismatch')))
        self.assertFalse(d._is_transient_drive_error(RuntimeError('Duplicate Drive filenames')))
        self.assertFalse(d._is_transient_drive_error(ValueError('bad revision')))

    def _seed_with_mocked_upload(self, upload_side_effect):
        import ssl
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            fake = td / 'x.safetensors'
            fake.write_bytes(b'fake-bytes')
            pin = {'files': {'diffusion_models/x.safetensors': {'size': 10, 'sha256': 'a' * 64}}}
            entry = {'path': 'diffusion_models/x.safetensors', 'drive_file_id': 'id1',
                     'parent_id': 'folder1', 'size': 10, 'sha256': 'a' * 64,
                     'server_md5': '0' * 32, 'server_verified': True, 'hf_revision': 'b' * 40}
            cfg = td / 'depot_config.json'
            cfg.write_text('{}')
            with (patch.object(d, 'REQUIRED', ('diffusion_models/x.safetensors',)),
                  patch.object(d, 'DEPOT_DIR', td),
                  patch.object(d, 'STATE', td / 'depot_state.json'),
                  patch.object(d, 'CFG', cfg),
                  patch.object(d, 'READY', td / 'verified_manifest.json'),
                  patch.object(d, 'load_pinned_metadata', return_value=pin),
                  patch.object(d, 'download_pinned_model', return_value=fake),
                  patch.object(d, 'sha256_file', return_value='a' * 64),
                  patch.object(d, 'ensure_folder', return_value='folder1'),
                  patch.object(d, 'upload_one', side_effect=upload_side_effect),
                  patch.object(d, 'verify_drive_entry', return_value=entry),
                  patch.object(d, 'publish_manifest_to_drive', return_value={'id': 'm1'}),
                  patch.object(d.time, 'sleep', return_value=None)):
                rebuilt = []
                with patch.object(d, 'drive_service', side_effect=lambda: rebuilt.append(object()) or MagicMock()):
                    result = d.seed(MagicMock(), 'root1', 'b' * 40, source='direct')
                return result, len(rebuilt), entry

    def test_seed_retries_upload_one_on_ssl_eof(self):
        import ssl
        entry = {'path': 'diffusion_models/x.safetensors', 'drive_file_id': 'id1',
                 'parent_id': 'folder1', 'size': 10, 'sha256': 'a' * 64,
                 'server_md5': '0' * 32, 'server_verified': True, 'hf_revision': 'b' * 40}
        result, rebuilds, _ = self._seed_with_mocked_upload(
            [ssl.SSLEOFError(8, 'EOF occurred in violation of protocol'),
             ssl.SSLEOFError(8, 'EOF occurred in violation of protocol'),
             entry])
        self.assertTrue(result['ready'])
        self.assertEqual(result['files'], 1)
        self.assertEqual(rebuilds, 2)

    def test_seed_falls_back_to_upload_when_legacy_copy_fails_transiently(self):
        import ssl
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            fake = td / 'a.safetensors'
            fake.write_bytes(b'fake-bytes')
            pin = {'files': {'vae/minimax_h3_audio_vae_fp32.safetensors': {'size': 10, 'sha256': 'a' * 64}}}
            entry = {'path': 'vae/minimax_h3_audio_vae_fp32.safetensors', 'drive_file_id': 'id1',
                     'parent_id': 'folder1', 'size': 10, 'sha256': 'a' * 64,
                     'server_md5': '0' * 32, 'server_verified': True, 'hf_revision': 'b' * 40}
            cfg = td / 'depot_config.json'
            cfg.write_text('{}')
            with (patch.object(d, 'REQUIRED', ('vae/minimax_h3_audio_vae_fp32.safetensors',)),
                  patch.object(d, 'DEPOT_DIR', td),
                  patch.object(d, 'STATE', td / 'depot_state.json'),
                  patch.object(d, 'CFG', cfg),
                  patch.object(d, 'READY', td / 'verified_manifest.json'),
                  patch.object(d, 'load_pinned_metadata', return_value=pin),
                  patch.object(d, 'download_pinned_model', return_value=fake),
                  patch.object(d, 'sha256_file', return_value='a' * 64),
                  patch.object(d, 'ensure_folder', return_value='folder1'),
                  patch.object(d, 'maybe_copy_verified_legacy_vae',
                               side_effect=ssl.SSLEOFError(8, 'EOF occurred in violation of protocol')),
                  patch.object(d, 'upload_one', return_value=entry),
                  patch.object(d, 'verify_drive_entry', return_value=entry),
                  patch.object(d, 'publish_manifest_to_drive', return_value={'id': 'm1'}),
                  patch.object(d.time, 'sleep', return_value=None)):
                result = d.seed(MagicMock(), 'root1', 'b' * 40, source='direct')
            self.assertTrue(result['ready'])
            self.assertEqual(result['files'], 1)

    def test_seed_does_not_retry_checksum_mismatch(self):
        with self.assertRaisesRegex(RuntimeError, 'DO NOT overwrite'):
            self._seed_with_mocked_upload([RuntimeError('DO NOT overwrite: bad')])
    def test_no_plaintext_auth_in_repo(self):
        root=Path(__file__).resolve().parents[1]
        self.assertFalse(list(root.glob('token.json')))
        self.assertFalse(list(root.glob('application_default_credentials.json')))
if __name__=='__main__':unittest.main()
