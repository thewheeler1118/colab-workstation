import hashlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import drive_depot as d

REV='a'*40

class FakeResp:
    def __init__(self,status,content=b'',headers=None):
        self.status_code=status;self._content=content;self.headers=headers or {}
    def __enter__(self):return self
    def __exit__(self,*a):return False
    def iter_content(self,chunk_size=1024):
        for i in range(0,len(self._content),chunk_size):
            yield self._content[i:i+chunk_size]

def sha(b):return hashlib.sha256(b).hexdigest()

class DirectDownloadTest(unittest.TestCase):
    def _run(self,rel,content,expected_size=None,expected_sha=None,resp_side_effect=None,pre_part=None,pre_dest=None):
        import shutil as _shutil
        td=tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree,td,ignore_errors=True)
        dest_dir=Path(td)
        if pre_part is not None:
            p=dest_dir/rel; p.parent.mkdir(parents=True,exist_ok=True)
            Path(str(p)+'.part').write_bytes(pre_part)
        if pre_dest is not None:
            p=dest_dir/rel; p.parent.mkdir(parents=True,exist_ok=True)
            p.write_bytes(pre_dest)
        size=expected_size if expected_size is not None else len(content)
        h=expected_sha if expected_sha is not None else sha(content)
        with patch('requests.Session') as MockSession:
            sess=MockSession.return_value
            if resp_side_effect is not None:
                sess.get.side_effect=resp_side_effect
            else:
                sess.get.return_value=FakeResp(200,content)
            with patch('time.sleep'):
                out=d.download_pinned_model(rel,size,h,dest_dir,REV)
        return out, dest_dir/rel, sess

    def test_success_streaming_and_hash(self):
        out,dest,sess=self._run('vae/x.safetensors',b'0123456789')
        self.assertEqual(out,dest)
        self.assertEqual(dest.read_bytes(),b'0123456789')
        self.assertFalse(Path(str(dest)+'.part').exists())
        self.assertEqual(sess.get.call_count,1)
        called_url=sess.get.call_args.args[0]
        self.assertIn(REV,called_url);self.assertIn('vae/x.safetensors',called_url)

    def test_range_resume_appends(self):
        content=b'0123456789'
        out,dest,sess=self._run('vae/x.safetensors',content,resp_side_effect=[
            FakeResp(206,content[4:],{'Content-Range':'bytes 4-9/10'})],pre_part=content[:4])
        self.assertEqual(dest.read_bytes(),content)
        headers=sess.get.call_args.kwargs['headers']
        self.assertEqual(headers.get('Range'),'bytes=4-')

    def test_server_ignores_range_restarts(self):
        content=b'0123456789'
        out,dest,sess=self._run('vae/x.safetensors',content,resp_side_effect=[
            FakeResp(200,content),FakeResp(200,content)],pre_part=content[:4])
        self.assertEqual(dest.read_bytes(),content)
        self.assertEqual(sess.get.call_count,2)

    def test_bad_hash_raises_and_never_creates_dest(self):
        with tempfile.TemporaryDirectory() as td:
            dest_dir=Path(td)
            with patch('requests.Session') as MockSession:
                MockSession.return_value.get.return_value=FakeResp(200,b'0123456789')
                with patch('time.sleep'):
                    with self.assertRaisesRegex(RuntimeError,'checksum'):
                        d.download_pinned_model('vae/x.safetensors',10,sha(b'XXXXXXXXXX'),dest_dir,REV)
            self.assertFalse((dest_dir/'vae/x.safetensors').exists())
            self.assertFalse(Path(str(dest_dir/'vae/x.safetensors')+'.part').exists())

    def test_short_read_raises_after_bounded_retries(self):
        with tempfile.TemporaryDirectory() as td:
            dest_dir=Path(td)
            with patch('requests.Session') as MockSession:
                MockSession.return_value.get.return_value=FakeResp(200,b'01234')
                with patch('time.sleep'):
                    with self.assertRaisesRegex(RuntimeError,'checksum|short read|failed'):
                        d.download_pinned_model('vae/x.safetensors',10,sha(b'0123456789'),dest_dir,REV)
                self.assertEqual(MockSession.return_value.get.call_count,3)

    def test_transient_errors_retry_then_succeed(self):
        import requests as rq
        content=b'0123456789'
        out,dest,sess=self._run('vae/x.safetensors',content,resp_side_effect=[
            rq.exceptions.ConnectionError('down'),rq.exceptions.ConnectionError('down'),
            FakeResp(200,content)])
        self.assertEqual(dest.read_bytes(),content)
        self.assertEqual(sess.get.call_count,3)

    def test_exhausted_retries_raise(self):
        import requests as rq
        with tempfile.TemporaryDirectory() as td:
            dest_dir=Path(td)
            with patch('requests.Session') as MockSession:
                MockSession.return_value.get.side_effect=rq.exceptions.ConnectionError('down')
                with patch('time.sleep'):
                    with self.assertRaisesRegex(RuntimeError,'failed'):
                        d.download_pinned_model('vae/x.safetensors',10,sha(b'0123456789'),dest_dir,REV)
                self.assertEqual(MockSession.return_value.get.call_count,3)

    def test_416_with_complete_part_verifies(self):
        content=b'0123456789'
        out,dest,sess=self._run('vae/x.safetensors',content,resp_side_effect=[
            FakeResp(416,b'',{})],pre_part=content)
        self.assertEqual(dest.read_bytes(),content)

    def test_reuses_verified_dest_without_request(self):
        content=b'0123456789'
        out,dest,sess=self._run('vae/x.safetensors',content,pre_dest=content)
        self.assertEqual(out,dest)
        self.assertEqual(sess.get.call_count,0)


class SeedSourceTest(unittest.TestCase):
    def _base_pin(self):
        return {rel:{'size':10,'sha256':sha(b'X'*10)} for rel in d.REQUIRED}

    def test_direct_uses_pin_and_download_not_hub(self):
        import sys as _sys
        fake_hub=MagicMock()
        fake_hub.HfApi.side_effect=AssertionError('hub must not be used for direct')
        with tempfile.TemporaryDirectory() as td:
            dep=Path(td)
            with patch.object(d,'DEPOT_DIR',dep),patch.object(d,'PIN_FILE',dep/'pinned_hf_files.json'),\
                 patch.object(d,'CFG',dep/'depot_config.json'),patch.object(d,'READY',dep/'verified_manifest.json'),\
                 patch.object(d,'STATE',dep/'depot_state.json'):
                d.atomic_json(dep/'depot_config.json',{'drive_root_folder_id':'root123'})
                pin={'repo':d.HF_REPO,'revision':REV,'files':self._base_pin(),'other_files':[]}
                d.atomic_json(dep/'pinned_hf_files.json',pin)
                calls=[]
                def fake_download(rel,size,h,dd,rev):
                    calls.append(rel)
                    p=dd/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'X'*10);return p
                entry={'path':'','drive_file_id':'fid','parent_id':'f','size':10,'sha256':sha(b'X'*10),
                       'server_md5':'0'*32,'server_verified':True,'hf_revision':REV}
                with patch.object(d,'download_pinned_model',side_effect=fake_download),\
                     patch.object(d,'ensure_folder',return_value='folder'),\
                     patch.object(d,'upload_one',return_value={**entry}),\
                     patch.object(d,'verify_drive_entry',return_value={}),\
                     patch.object(d,'publish_manifest_to_drive',return_value={'drive_file_id':'ready1'}),\
                     patch.object(d,'maybe_copy_verified_legacy_vae',return_value=False),\
                     patch.dict(_sys.modules,{'huggingface_hub':fake_hub}):
                    result=d.seed(MagicMock(),'root123',REV,source='direct')
                self.assertTrue(result['ready']);self.assertEqual(result['files'],4)
                self.assertEqual(sorted(calls),sorted(d.REQUIRED))
                fake_hub.HfApi.assert_not_called()

    def test_hub_still_uses_huggingface_hub(self):
        import sys as _sys
        content=b'X'*10
        class Sib:
            def __init__(self,rfilename):self.rfilename=rfilename;self.size=10;self.lfs={'sha256':sha(content)}
        class Info:
            sha=REV;siblings=[Sib(r) for r in d.REQUIRED]
        fake_hub=MagicMock()
        fake_hub.HfApi.return_value.model_info.return_value=Info()
        def fake_dl(repo_id=None,filename=None,revision=None,local_dir=None):
            p=Path(local_dir)/filename;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content);return str(p)
        fake_hub.hf_hub_download.side_effect=fake_dl
        with tempfile.TemporaryDirectory() as td:
            dep=Path(td)
            with patch.object(d,'DEPOT_DIR',dep),patch.object(d,'CFG',dep/'depot_config.json'),\
                 patch.object(d,'READY',dep/'verified_manifest.json'),patch.object(d,'STATE',dep/'depot_state.json'):
                d.atomic_json(dep/'depot_config.json',{'drive_root_folder_id':'root123'})
                entry={'path':'','drive_file_id':'fid','parent_id':'f','size':10,'sha256':sha(content),
                       'server_md5':'0'*32,'server_verified':True,'hf_revision':REV}
                with patch.object(d,'ensure_folder',return_value='folder'),\
                     patch.object(d,'upload_one',return_value={**entry}),\
                     patch.object(d,'verify_drive_entry',return_value={}),\
                     patch.object(d,'publish_manifest_to_drive',return_value={'drive_file_id':'ready1'}),\
                     patch.object(d,'maybe_copy_verified_legacy_vae',return_value=False),\
                     patch.dict(_sys.modules,{'huggingface_hub':fake_hub}):
                    result=d.seed(MagicMock(),'root123',REV,source='hub')
                self.assertTrue(result['ready']);self.assertEqual(result['files'],4)
                fake_hub.hf_hub_download.assert_called()
                self.assertEqual(fake_hub.hf_hub_download.call_count,4)

if __name__=='__main__':unittest.main()
