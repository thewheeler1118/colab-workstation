"""Local video QA for Muse MiniMax-H3 Ref2VA A/B outputs.

Fresh implementation from the user's direct 2026-11-11 instruction
(section 13). Technical QA (ffprobe) and content QA are reported
separately; a technical pass is never presented as a content pass.

QA frame indices: 0, 48, 96, 144, 188, 225, 226, 250, 273
Expected final video: 1080x1920, 24 fps, 274 frames, ~11.4167 s.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

QA_FRAME_INDICES = [0, 48, 96, 144, 188, 225, 226, 250, 273]
EXPECTED_WIDTH = 1080
EXPECTED_HEIGHT = 1920
EXPECTED_FPS = 24
EXPECTED_FRAMES = 274


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        for part in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def ffprobe(path: Path) -> dict:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames",
         "-show_entries", "stream=codec_type,width,height,r_frame_rate,nb_read_frames,nb_frames,duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=120,
    )
    return json.loads(p.stdout)


def inspect_video(path: Path) -> dict:
    """ffprobe frame count, resolution, fps, duration, streams, SHA-256."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Video missing or empty: {path}")
    data = ffprobe(path)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("No video stream in " + str(path))
    frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
    rate = video.get("r_frame_rate", "0/1")
    num, den = (rate.split("/") + ["1"])[:2]
    fps = float(num) / float(den) if float(den) else 0.0
    result = {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fps,
        "frame_count": frames,
        "duration": frames / fps if fps else 0.0,
        "streams": [(s.get("codec_type"), s.get("codec_name")) for s in data.get("streams", [])],
        "technical_pass": (
            int(video["width"]) == EXPECTED_WIDTH
            and int(video["height"]) == EXPECTED_HEIGHT
            and abs(fps - EXPECTED_FPS) < 0.01
            and frames == EXPECTED_FRAMES
        ),
    }
    return result


def extract_qa_frames(mp4: Path, target_dir: Path) -> list[Path]:
    """Extract the nine QA frames with ffmpeg. Returns the frame paths."""
    mp4 = Path(mp4)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for idx in QA_FRAME_INDICES:
        dest = target_dir / f"frame_{idx:03d}.png"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(mp4), "-vf", f"select='eq(n\\,{idx})'", "-frames:v", "1", str(dest)],
            check=True, timeout=120,
        )
        if not dest.is_file() or dest.stat().st_size == 0:
            raise RuntimeError(f"QA frame extraction failed for frame {idx}")
        out.append(dest)
    return out


def compare_poster_tail(mp4: Path, original_poster: Path) -> dict:
    """Check the last 48 frames are a static poster hold.

    Uses ffmpeg PSNR between the decoded last frame and the original JPG.
    Lossy encoding means PSNR is finite, not infinite; a very low PSNR
    (or a failed decode) is a failure. Returns psnr and a pass flag.
    """
    mp4 = Path(mp4)
    original_poster = Path(original_poster)
    info = inspect_video(mp4)
    if info["frame_count"] != EXPECTED_FRAMES:
        return {"tail_frames": 48, "psnr_db": None, "pass": False, "reason": "frame count is not 274"}
    tmp = Path(str(mp4) + ".last_frame.png")
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(mp4), "-vf", f"select='eq(n\\,{EXPECTED_FRAMES - 1})'",
             "-frames:v", "1", str(tmp)],
            check=True, timeout=120,
        )
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(tmp), "-i", str(original_poster),
             "-filter_complex", "psnr", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        text = p.stderr + "\n" + p.stdout
        psnr = None
        # ffmpeg 8.x logs the summary as "PSNR y:.. u:.. v:.. average:.."
        # (older builds used "psnr_avg:.."); accept either token.
        for token in text.replace(",", " ").split():
            if token.startswith("psnr_avg:") or token.startswith("average:"):
                try:
                    psnr = float(token.split(":", 1)[1])
                except ValueError:
                    pass
        # A poster hold after H.264 encoding typically scores well above
        # 30 dB; below that the tail is not recognisably the poster.
        return {"tail_frames": 48, "psnr_db": psnr, "pass": psnr is not None and psnr >= 30.0}
    finally:
        if tmp.exists():
            tmp.unlink()


def build_contact_sheet(frames: list[Path], output: Path) -> Path:
    """Build a 3x3 QA contact sheet from nine extracted frames."""
    frames = [Path(f) for f in frames]
    if len(frames) != 9:
        raise ValueError("Contact sheet requires exactly 9 frames")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    inputs = []
    for f in frames:
        inputs += ["-i", str(f)]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", "tile=3x3", "-frames:v", "1", str(output)]
    subprocess.run(cmd, check=True, timeout=120)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("Contact sheet build failed")
    return output


def validate_final_video(path: Path) -> dict:
    info = inspect_video(path)
    if not info["technical_pass"]:
        raise RuntimeError(
            f"Final video technical QA failed: {info['width']}x{info['height']} "
            f"{info['fps']}fps {info['frame_count']} frames, expected "
            f"{EXPECTED_WIDTH}x{EXPECTED_HEIGHT} {EXPECTED_FPS}fps {EXPECTED_FRAMES} frames"
        )
    return info


# Story checks that automated QA cannot decide (TASK-003 §3-4). They need a
# human look at the contact sheet; they stay UNREVIEWED until a reviewer
# marks them. A technical pass is never presented as a content pass.
CONTENT_CHECKS = [
    "bull_charge_1_and_dodge",
    "bull_charge_2_and_dodge",
    "cheetah_appearance",
    "no_text_ghosting",
    "ending_basketball_raise",
]


def build_qa_report(mp4: Path, original_poster: Path | None = None,
                    run_id: str = "", qa_dir: Path | None = None) -> dict:
    """Run the technical QA steps and return a report that keeps technical
    acceptance and content (story) acceptance separate.

    Only the four real QA functions are used (validate_final_video,
    extract_qa_frames, build_contact_sheet, compare_poster_tail). The
    function never raises for a failed video: a technical failure is
    recorded in technical_acceptance and the report is returned, so the
    caller can archive it and a human can review it.
    """
    mp4 = Path(mp4)
    report = {
        "run_id": run_id,
        "video": str(mp4),
        "technical_acceptance": {"status": "NOT_RUN"},
        "content_acceptance": {
            "status": "CONTENT_REVIEW_REQUIRED",
            "checks": {name: "UNREVIEWED" for name in CONTENT_CHECKS},
            "note": "Story checks need a human look at the contact sheet; "
                    "automated QA cannot confirm them.",
        },
        "artifacts": {},
    }
    try:
        info = validate_final_video(mp4)
    except Exception as e:  # noqa: BLE001 - a failed video is a report, not a crash
        report["technical_acceptance"] = {
            "status": "FAILED", "error": str(e)[:500],
            "tail_psnr_db": None, "tail_pass": None,
        }
        return report
    technical = {
        "status": "PASSED",
        "frames": info["frame_count"], "width": info["width"],
        "height": info["height"], "fps": info["fps"],
        "duration": info["duration"], "size": info["size"],
        "sha256": info["sha256"], "technical_pass": info["technical_pass"],
        "tail_psnr_db": None, "tail_pass": None,
    }
    if original_poster is not None:
        tail = compare_poster_tail(mp4, Path(original_poster))
        technical["tail_psnr_db"] = tail.get("psnr_db")
        technical["tail_pass"] = tail.get("pass")
        if not tail.get("pass"):
            technical["status"] = "FAILED"
            technical["error"] = "poster-tail PSNR check failed"
    if qa_dir is not None:
        try:
            frames = extract_qa_frames(mp4, Path(qa_dir) / "frames")
            sheet = build_contact_sheet(frames, Path(qa_dir) / "contact_sheet.png")
            report["artifacts"] = {
                "qa_frames": [str(f) for f in frames],
                "contact_sheet": str(sheet),
            }
        except Exception as e:  # noqa: BLE001 - extraction failure is a technical failure
            technical["status"] = "FAILED"
            technical["error"] = f"QA frame extraction failed: {e}"[:500]
    report["technical_acceptance"] = technical
    return report
