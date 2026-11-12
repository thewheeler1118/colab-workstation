#!/usr/bin/env bash
# check_outputs.sh — OFFLINE. Validates a downloaded final MP4 on this machine.
# Usage: ./scripts/check_outputs.sh <path/to/final.mp4>
# Runs h3_video_qa.validate_final_video: ffprobe must read exactly 274 frames
# at 24 fps and 1080x1920 (h3_output_check.OUTPUT_CHECK_JSON is the REMOTE
# equivalent that runs on the Colab runtime before download).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ $# -ne 1 ]; then echo "Usage: $0 <path/to/final.mp4>" >&2; exit 2; fi
python3 - "$1" <<'PY'
import json, sys
from pathlib import Path
from h3_video_qa import validate_final_video
print(json.dumps(validate_final_video(Path(sys.argv[1])), ensure_ascii=False, indent=2))
PY
