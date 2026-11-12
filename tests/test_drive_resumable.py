import json,os,stat,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from drive_resumable import atomic_secret_json,progress_offset,upload_with_persistent_session

class R:
 def __init__(self,status,headers=None,obj=None):
  self.status_code=status;self.headers=headers or {};self._obj=obj or {}
 def json(self):return self._obj

class DriveResumableTest(unittest.TestCase):
 def test_private_session_file(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'secret'/'.session.json'
   atomic_secret_json(p,{'session_uri':'secret'})
   self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
   self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode),0o700)
 def test_range_parse(self):
  self.assertEqual(progress_offset(R(308,{'Range':'bytes=0-524287'}),1000000),524288)
  self.assertEqual(progress_offset(R(308,{}),1000000),0)
  with self.assertRaisesRegex(RuntimeError,'Invalid'):
   progress_offset(R(308,{'Range':'nope'}),100)
 def test_initial_upload_and_verified_final_response(self):
  with tempfile.TemporaryDirectory() as td:
   source=Path(td)/'m.bin';source.write_bytes(b'ABCD')
   sidecar=Path(td)/'private'/'session.json'
   s=MagicMock()
   s.post.return_value=R(200,{'Location':'https://www.googleapis.com/upload/drive/v3/files?id=abc'})
   s.put.side_effect=[R(308),R(201,obj={'id':'file-123'})]
   result=upload_with_persistent_session(s,source,'m.bin','parent','vae/m.bin','f'*40,sidecar,'a'*64)
   self.assertEqual(result['id'],'file-123');self.assertFalse(sidecar.exists())
   self.assertEqual(s.post.call_count,1)
   self.assertEqual(s.put.call_args_list[1].kwargs['headers']['Content-Range'],'bytes 0-3/4')
 def test_status_query_411_falls_back_to_offset_zero(self):
  # Muse VM egress strips Content-Length: 0 from the empty status query, so
  # Google answers 411; the upload must proceed from 0 and let the chunk PUT
  # discover the server offset instead of raising.
  with tempfile.TemporaryDirectory() as td:
   source=Path(td)/'m.bin';source.write_bytes(b'ABCD')
   sidecar=Path(td)/'private'/'session.json'
   s=MagicMock()
   s.post.return_value=R(200,{'Location':'https://www.googleapis.com/upload/drive/v3/files?id=abc'})
   s.put.side_effect=[R(411),R(201,obj={'id':'file-411'})]
   result=upload_with_persistent_session(s,source,'m.bin','parent','vae/m.bin','f'*40,sidecar,'a'*64)
   self.assertEqual(result['id'],'file-411');self.assertFalse(sidecar.exists())
   self.assertEqual(s.put.call_args_list[1].kwargs['headers']['Content-Range'],'bytes 0-3/4')
 def test_lost_final_response_confirmed_by_final_chunk_reput(self):
  with tempfile.TemporaryDirectory() as td:
   source=Path(td)/'m.bin';source.write_bytes(b'ABCD')
   sidecar=Path(td)/'private'/'session.json'
   s=MagicMock()
   s.post.return_value=R(200,{'Location':'https://www.googleapis.com/upload/drive/v3/files?id=abc'})
   s.put.side_effect=[R(308),R(308,{'Range':'bytes=0-3'}),R(201,obj={'id':'file-final'})]
   result=upload_with_persistent_session(s,source,'m.bin','parent','vae/m.bin','f'*40,sidecar,'a'*64)
   self.assertEqual(result['id'],'file-final');self.assertFalse(sidecar.exists())
   self.assertEqual(s.put.call_args_list[2].kwargs['headers']['Content-Range'],'bytes 0-3/4')
 def test_cross_process_resume_from_saved_uri(self):
  with tempfile.TemporaryDirectory() as td:
   source=Path(td)/'m.bin';source.write_bytes(b'ABCDEFG')
   sidecar=Path(td)/'private'/'session.json'
   atomic_secret_json(sidecar,{'source_size':7,'source_sha256':'a'*64,
     'name':'m.bin','parent_id':'parent','logical_rel':'vae/m.bin','revision':'f'*40,
     'session_uri':'https://www.googleapis.com/upload/drive/v3/files?upload_id=SECRETS'})
   s=MagicMock();s.put.side_effect=[R(308,{'Range':'bytes=0-3'}),R(201,obj={'id':'file-recovered'})]
   result=upload_with_persistent_session(s,source,'m.bin','parent','vae/m.bin','f'*40,sidecar,'a'*64)
   self.assertEqual(result['id'],'file-recovered')
   self.assertEqual(s.post.call_count,0)
   self.assertEqual(s.put.call_args_list[1].kwargs['headers']['Content-Range'],'bytes 4-6/7')
 def test_upload_breakpoint_resumes_and_completes(self):
  # Chunk PUT fails once mid-upload; the retry must resume via the saved
  # sidecar and finish with the same file id, not restart from scratch.
  from unittest.mock import patch as _patch
  with tempfile.TemporaryDirectory() as td:
   source=Path(td)/'m.bin';source.write_bytes(b'ABCD')
   sidecar=Path(td)/'private'/'session.json'
   s=MagicMock()
   s.post.return_value=R(200,{'Location':'https://www.googleapis.com/upload/drive/v3/files?id=abc'})
   import requests as _rq
   s.put.side_effect=[R(308),_rq.exceptions.ConnectionError('boom'),R(308),R(201,obj={'id':'file-resumed'})]
   with _patch('time.sleep'):
    result=upload_with_persistent_session(s,source,'m.bin','parent','vae/m.bin','f'*40,sidecar,'a'*64)
   self.assertEqual(result['id'],'file-resumed');self.assertFalse(sidecar.exists())
   self.assertEqual(s.put.call_count,4)
   # The retry re-PUTs the first chunk; the server corrects the offset via 308.
   self.assertEqual(s.put.call_args_list[3].kwargs['headers']['Content-Range'],'bytes 0-3/4')
 def test_invalid_saved_session_source_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   source=Path(td)/'m.bin';source.write_bytes(b'1234');sidecar=Path(td)/'session.json'
   atomic_secret_json(sidecar,{'source_size':123,'session_uri':'https://www.googleapis.com/upload/drive/v3/files?id=abc'})
   with self.assertRaisesRegex(RuntimeError,'different source'):
    upload_with_persistent_session(MagicMock(),source,'m.bin','parent','vae/m.bin','f'*40,sidecar,'a'*64)
if __name__=='__main__':unittest.main()
