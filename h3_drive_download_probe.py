"""Remote Drive-download probe (TASK-006 P0-B, Issue #44).

Executed by the controller via a SHORT `colab exec -f` call, after the
GPU is allocated and before the worker starts. It downloads one SMALL
file (the ComfyUI source-cache `.sha256` text file) through the same
noninteractive Drive transport the worker will use for the 40 GB model
restore, and prints exactly one success marker. A probe failure is a
fail-closed refusal to start the worker: the GPU is released without
spending the model-restore window on a transport that does not work.

Inputs (uploaded by the controller):
- /content/muse_h3/drive_download_probe.json: {"file_id"} for the
  probe file (the smallest pinned model file from the verified
  manifest). The probe downloads only its first 1 MiB (Range) plus a
  metadata GET: enough to prove the token and the transport work,
  without spending the restore window on gigabytes.
- /content/muse_h3/drive_token_probe.json: a 0600 token document for
  THIS probe only. The probe reads and deletes it immediately; the
  worker's own token file is a separate delivery and is untouched.

The probe never prints a token value. It exits non-zero on any
failure, with the reason on stderr (no token material).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("/content/muse_h3")
sys.path.insert(0, str(ROOT))

from h3_drive_download import DriveDownloader, FileTokenProvider  # noqa: E402


def main() -> None:
    probe_path = ROOT / "drive_download_probe.json"
    spec = json.loads(probe_path.read_text(encoding="utf-8"))
    if not spec.get("file_id"):
        raise RuntimeError("probe spec missing file_id")
    provider = FileTokenProvider(ROOT / "drive_token_probe.json")
    provider.load_initial()
    downloader = DriveDownloader(provider)
    result = downloader.probe_transport(spec["file_id"])
    print("MUSE_DRIVE_DOWNLOAD_PROBE_OK", result["drive_file_id"],
          result["size"], result["probe_bytes"], flush=True)


if __name__ == "__main__":
    main()
