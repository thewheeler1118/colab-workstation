"""Remote output check (runs on the Colab runtime, stdlib only).

Verifies the real final MP4 BEFORE the controller downloads it: the file
must exist under /content/muse_h3/output/ and ffprobe must read exactly
274 frames at 24 fps and 1080x1920. Prints one line:
    OUTPUT_CHECK_JSON:{"ok": true, "exists": true, "size_bytes": ...,
                       "frames": 274, "fps": 24, "width": 1080,
                       "height": 1920, "codec": "..."}
or ok=false with a reason. The controller's check_remote_output() fails
closed on anything other than the full pass.
"""
import json
import os
import subprocess
import sys

EXPECTED_FRAMES = 274
EXPECTED_FPS = 24
EXPECTED_WIDTH = 1080
EXPECTED_HEIGHT = 1920


def main():
    variant = os.environ.get("H3_VARIANT", "A")
    path = f"/content/muse_h3/output/shao_nian_h3_{variant}.mp4"
    info = {"ok": False, "exists": False, "path": path}
    if not os.path.exists(path):
        info["reason"] = "mp4_missing"
        print("OUTPUT_CHECK_JSON:" + json.dumps(info), flush=True)
        return
    info["exists"] = True
    info["size_bytes"] = os.path.getsize(path)
    if info["size_bytes"] <= 0:
        info["reason"] = "mp4_empty"
        print("OUTPUT_CHECK_JSON:" + json.dumps(info), flush=True)
        return
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,codec_name,nb_read_frames",
             "-of", "json", path],
            capture_output=True, text=True, timeout=120, check=True)
        streams = json.loads(p.stdout).get("streams", [])
        if not streams:
            info["reason"] = "no_video_stream"
            print("OUTPUT_CHECK_JSON:" + json.dumps(info), flush=True)
            return
        s = streams[0]
        num, den = s.get("r_frame_rate", "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
        info.update({"width": int(s.get("width", 0)), "height": int(s.get("height", 0)),
                     "fps": round(fps, 3), "codec": s.get("codec_name"),
                     "frames": int(s.get("nb_read_frames", 0))})
        info["ok"] = (info["frames"] == EXPECTED_FRAMES and info["fps"] == EXPECTED_FPS
                      and info["width"] == EXPECTED_WIDTH and info["height"] == EXPECTED_HEIGHT)
        if not info["ok"]:
            info["reason"] = "ffprobe_mismatch"
    except Exception as e:  # noqa: BLE001
        info["reason"] = f"ffprobe_failed: {type(e).__name__}: {str(e)[:300]}"
    print("OUTPUT_CHECK_JSON:" + json.dumps(info), flush=True)


if __name__ == "__main__":
    main()
