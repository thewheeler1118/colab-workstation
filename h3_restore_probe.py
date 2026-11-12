"""Remote 256MB read-throughput probe on the mounted Drive model depot.

Self-contained (stdlib only): reads the mounted manifest + mount config
uploaded by the acceptance driver, checks free disk on /content, then reads
the first 256 MiB of the largest model file through the DriveFS mount and
reports wall-clock throughput. No copy, no inference, no writes.
"""
import json
import shutil
import time
from pathlib import Path

root = Path("/content/muse_h3")
manifest = json.loads((root / "model_manifest.json").read_text())
conf = json.loads((root / "drive_mount_config.json").read_text())
base = Path(conf["drive_cache_dir"])
if not str(base).startswith("/content/drive/MyDrive/Muse-H3-Model-Depot"):
    raise RuntimeError("Unexpected Drive mount path")
if not base.is_dir():
    raise RuntimeError("Drive mount failed or wrong MyDrive account")

free = shutil.disk_usage("/content").free
print(f"RESTORE_DISK_FREE_BYTES: {free}", flush=True)
print(f"RESTORE_DISK_REQUIRED_BYTES: {40073842159 + 5 * 1024 * 1024 * 1024}", flush=True)

files = sorted(manifest["files"], key=lambda f: f["size"], reverse=True)
target = base / files[0]["path"]
if not target.is_file():
    raise RuntimeError("Largest model file not visible on mount: " + str(files[0]["path"]))
n = 256 * 1024 * 1024
t0 = time.monotonic()
read = 0
with open(target, "rb") as fh:
    while read < n:
        chunk = fh.read(min(8 * 1024 * 1024, n - read))
        if not chunk:
            break
        read += len(chunk)
dt = time.monotonic() - t0
mbps = read / 1_000_000 / dt if dt > 0 else 0.0
print(f"RESTORE_256MB_FILE: {files[0]['path']}", flush=True)
print(f"RESTORE_256MB_BYTES: {read}", flush=True)
print(f"RESTORE_256MB_SEC: {dt:.1f}", flush=True)
print(f"RESTORE_256MB_MBPS: {mbps:.1f}", flush=True)
print("RESTORE_PROBE_OK", flush=True)
