import os,stat,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import auth_support as a
import drive_depot as d
import h3_controller as h
from build_api_candidate import convert

class AuthAndTransportTests(unittest.TestCase):
 def test_resolve_explicit_cli(self):
  with tempfile.TemporaryDirectory() as td:
   exe=Path(td)/'colab';exe.write_text('#!/bin/sh\nexit 0\n');exe.chmod(0o700)
   with patch.dict(os.environ,{'COLAB_CLI_PATH':str(exe)}):
    self.assertEqual(a.resolve_colab_executable(),str(exe.resolve()))
 def test_resolve_venv_colab_in_python_dir(self):
  with tempfile.TemporaryDirectory() as td:
   exe=Path(td)/'colab';exe.write_text('#!/bin/sh\nexit 0\n');exe.chmod(0o700)
   with patch.dict(os.environ,{},clear=True), patch.object(a.sys,'executable',str(Path(td)/'python')):
    self.assertEqual(a.resolve_colab_executable(),str(exe.resolve()))
 def test_adc_metadata_no_credentials_read(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'.config/gcloud/application_default_credentials.json';p.parent.mkdir(parents=True)
   p.write_text('DO_NOT_READ');p.chmod(0o600)
   with patch.object(a.Path,'home',return_value=Path(td)):
    r=a.audit_user_adc_file()
   self.assertTrue(r['present']);self.assertFalse(r['credentials_read']);self.assertNotIn('DO_NOT_READ',str(r))
 def test_adc_bad_permissions_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'.config/gcloud/application_default_credentials.json';p.parent.mkdir(parents=True)
   p.write_text('DO_NOT_READ');p.chmod(0o644)
   with patch.object(a.Path,'home',return_value=Path(td)):
    with self.assertRaisesRegex(RuntimeError,'permissions'):a.audit_user_adc_file()
 def test_official_mount_not_dfs(self):
  text=Path(h.__file__).read_text()
  self.assertIn("['drivemount','--help']",text)
  self.assertIn("'drivemount', '-s', session",text)
  self.assertNotIn("['dfs','mount'",text)
 def test_interactive_mount_refuses_non_tty(self):
  with patch.object(h.sys,'stdin',MagicMock(isatty=MagicMock(return_value=False))):
   with self.assertRaisesRegex(RuntimeError,'Interactive terminal'):
    h.mount_drive_interactive('sample')
 def test_smoke_requires_write_flag(self):
  with patch.object(sys,'argv',['drive_depot.py','smoke']):
   with self.assertRaises(SystemExit) as cm:d.cli()
   self.assertEqual(cm.exception.code,2)
 def test_idempotent_drive_root(self):
  with patch.object(d,'list_named',return_value=[{'id':'existing','mimeType':d.FOLDER_MIME}]):
   s=MagicMock();self.assertEqual(d.ensure_folder(s,None,'Muse-H3-Model-Depot',True),'existing')
   self.assertEqual(s.files().create.call_count,0)
 def test_converter_rejects_ambiguous_gui(self):
  with self.assertRaisesRegex(ValueError,'Exactly one SaveVideo'):
   convert({'nodes':[],'links':[]})
if __name__=='__main__':unittest.main()

class RemoteManifestTests(unittest.TestCase):
 def test_verify_fails_without_remote_ready_in_config(self):
  import json
  from h3_contract import MODEL_FILES
  with tempfile.TemporaryDirectory() as td:
   td=Path(td);ready=td/'r.json';cfg=td/'cfg.json'
   rows=[{'path':p,'drive_file_id':'id','size':100,'sha256':'a'*64,
          'parent_id':'p','server_md5':'a'*32,'hf_revision':'a'*40} for p in MODEL_FILES]
   ready.write_text(json.dumps({'status':'READY','verification_method':'drive_v3_server_api',
       'depot_root_id':'p','hf_revision':'a'*40,'files':rows}))
   cfg.write_text(json.dumps({'drive_root_folder_id':'p'}))
   with patch.object(d,'CFG',cfg):
    with self.assertRaisesRegex(RuntimeError,'No server-verified READY'):
     d.verify_ready(MagicMock(),True,ready)

class MountedCacheProbeTests(unittest.TestCase):
 def test_probe_occurs_before_variants(self):
  # 2026-11-11: run is single-variant (A or B per invocation), so there is
  # no longer a `for variant in ('A','B')` loop in the controller. Intent
  # is unchanged: the cache probe must run before the remote worker that
  # does H3 inference. TASK-006 P0-B: the DEFAULT path probes the
  # noninteractive Drive download; the legacy mount path keeps its own
  # probe-before-worker order. TASK-006 P1-B: the bootstrap (probe +
  # upload) was factored out of run_variant into _run_bootstrap; the
  # order is asserted there, and run_variant must call _run_bootstrap
  # before launch_worker.
  text=Path(h.__file__).read_text()
  body=text[text.index("def _run_bootstrap"):]
  self.assertIn("h3_drive_download_probe.py",body)
  self.assertIn("h3_remote_worker.py",body)
  self.assertLess(body.index("h3_drive_download_probe.py"),body.index("h3_remote_worker.py"))
  self.assertIn("h3_drive_probe.py",body)
  self.assertLess(body.index("h3_drive_probe.py"),body.index("h3_remote_worker.py"))
  rv=text[text.index("def run_variant"):]
  self.assertLess(rv.index("_run_bootstrap"),rv.index("launch_worker"))

class LegacyVaeReuseTests(unittest.TestCase):
 def test_do_not_copy_legacy_without_matching_checksum(self):
  with tempfile.TemporaryDirectory() as td:
   source=Path(td)/'minimax_h3_video_vae_int8_convrot.safetensors'
   source.write_bytes(b'test bytes')
   svc=MagicMock()
   with patch.object(d,'list_named',return_value=[]),patch.object(d,'get_file_evidence',return_value={
        'name':source.name,'size':len(b'test bytes'),'md5Checksum':'0'*32}):
    self.assertFalse(d.maybe_copy_verified_legacy_vae(svc,source,'folder',source.name,
         'vae/minimax_h3_video_vae_int8_convrot.safetensors'))
    self.assertEqual(svc.files().copy.call_count,0)
