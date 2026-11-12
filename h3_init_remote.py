from pathlib import Path
for d in ('/content/muse_h3','/content/muse_h3/output','/content/ComfyUI/input'):
    Path(d).mkdir(parents=True, exist_ok=True)
print('MUSE_H3_REMOTE_DIRS_READY')
