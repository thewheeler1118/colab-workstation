"""Cheap remote *visibility* probe after official Drive mount, before pip/model load.

Metadata/hash authority remains the pre-GPU Drive v3 verification on Muse, and
sha256 is rechecked when copying each weight to NVMe in the remote worker.
"""
import json
from pathlib import Path
root=Path('/content/muse_h3')
manifest=json.loads((root/'model_manifest.json').read_text())
conf=json.loads((root/'drive_mount_config.json').read_text())
base=Path(conf['drive_cache_dir'])
if not str(base).startswith('/content/drive/MyDrive/Muse-H3-Model-Depot'):
    raise RuntimeError('Unexpected Drive mount path')
if not base.is_dir():
    raise RuntimeError('Drive mount failed or wrong MyDrive account')
for item in manifest['files']:
    rel=Path(item['path'])
    candidate=(base/rel).resolve()
    if not candidate.is_relative_to(base.resolve()):
        raise RuntimeError('Unsafe model cache path')
    if not candidate.is_file() or candidate.stat().st_size != item['size']:
        raise RuntimeError('Mounted model missing/size mismatch: '+str(rel))
print('MUSE_DRIVE_MOUNT_MODELS_VISIBLE',len(manifest['files']))
