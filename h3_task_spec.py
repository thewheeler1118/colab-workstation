"""Generic declarative task specification for MiniMax-H3 video tasks.

TASK-007 architecture correction: a new video task must never require
editing .py sources. Everything that varies per task -- reference
images, prompt, generation parameters -- lives in one JSON file that
this module validates fail-closed before any GPU exists. The
controller and the remote worker take a TaskSpec and nothing else
task-specific.

The old --variant A|B path survives only as a built-in test-fixture
adapter (task_spec_for_variant): it builds the exact TaskSpec the old
hardcoded A/B code paths used, so historical regression tests and the
original cheetah-poster runs keep working as data, not as code.

Schema (all keys are validated; unknown keys are rejected):

    {
      "task_id": "h3-ab-20261111",          # required, [A-Za-z0-9][A-Za-z0-9_-]{0,63}
      "output_prefix": "A",                # required, same charset; names every output
      "ref_image_1": {                     # required, ComfyUI node 137 <Picture 1>
        "local_path": "assets/original_poster.jpg",  # OR "drive_file_id": "<id>"
        "sha256": "<64 hex>"               # required, verified before any upload
      },
      "ref_image_2": { ... },              # optional, node 139 <Picture 2>;
                                            # omitted means "reuse ref_image_1"
      "prompt": "<full structured prompt>", # exactly one of prompt / shots
      "shots": [{"text": "...", "label": "FIRST", "start_s": 0, "end_s": 2}, ...],
      "width": 736, "height": 1312,        # model resolution, 16-multiples
      "fps": 24, "animation_frames": 226,  # animation_frames % 17 == 5 (H3 rule)
      "tail_card_frames": 0,               # static tail hold (0 = no tail card)
      "steps": 25, "seed": 42,             # seed is required (reproducibility)
      "output_width": 1080, "output_height": 1920  # assembled delivery size
    }

No credentials ever appear in a spec: reference images carry only a
path or a Drive file id plus a SHA-256.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from h3_contract import (
    ANIMATION_FRAMES,
    FPS,
    HEIGHT,
    POSTER_FRAMES,
    POSTER_SHA256,
    STEPS,
    WIDTH,
    prompt_for,
    sha256_file,
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

MAX_PROMPT_CHARS = 20000
MAX_SHOTS = 32

# Generation-parameter bounds. The defaults are the frozen A/B production
# values from h3_contract; the bounds are the legal ranges the MiniMax-H3
# workflow accepts, not guesses about quality.
WIDTH_MIN, WIDTH_MAX = 64, 2048
HEIGHT_MIN, HEIGHT_MAX = 64, 2048
FPS_MIN, FPS_MAX = 1, 60
ANIMATION_FRAMES_MIN, ANIMATION_FRAMES_MAX = 17, 2048
TAIL_CARD_FRAMES_MIN, TAIL_CARD_FRAMES_MAX = 0, 480
STEPS_MIN, STEPS_MAX = 1, 100
SEED_MIN, SEED_MAX = 0, 2**63 - 1
OUTPUT_DIM_MIN, OUTPUT_DIM_MAX = 64, 4096

OUTPUT_WIDTH_DEFAULT = 1080
OUTPUT_HEIGHT_DEFAULT = 1920

_TOP_LEVEL_KEYS = {
    "task_id", "output_prefix", "ref_image_1", "ref_image_2",
    "prompt", "shots", "width", "height", "fps", "animation_frames",
    "tail_card_frames", "steps", "seed", "output_width", "output_height",
}
_REF_IMAGE_KEYS = {"local_path", "drive_file_id", "sha256"}
_SHOT_KEYS = {"text", "label", "start_s", "end_s"}


def _fail(msg: str) -> ValueError:
    return ValueError(f"TaskSpec invalid: {msg}")


def _require_str(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise _fail(f"{key!r} must be a non-empty string")
    return v


def _check_name(value: str, field_name: str) -> str:
    if not _NAME_RE.match(value):
        raise _fail(
            f"{field_name} {value!r} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}} "
            "(it names ledgers, folders and output files)")
    return value


def _check_sha256(value, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise _fail(f"{field_name} must be a 64-character hex SHA-256")
    return value.lower()


def _require_int(d: dict, key: str, default, lo: int, hi: int) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise _fail(f"{key!r} must be an integer")
    if not lo <= v <= hi:
        raise _fail(f"{key!r}={v} is outside the legal range {lo}..{hi}")
    return v


def _fmt_num(value) -> str:
    # Shots render deterministically: 2.0 -> "2", 7.5 -> "7.5".
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _render_shots(shots: list[dict]) -> str:
    """Deterministically concatenate a shots[] timeline into one prompt.

    Each shot renders as "[<start_s>-<end_s>s] <label>: <text>" with the
    bracketed time range and the label omitted when absent. Shots keep
    their file order; no sorting, no deduplication, no rewording.
    """
    lines = []
    for shot in shots:
        prefix = ""
        if "start_s" in shot:
            prefix += f"[{_fmt_num(shot['start_s'])}-{_fmt_num(shot['end_s'])}s] "
        if shot.get("label"):
            prefix += f"{shot['label']}: "
        lines.append(prefix + shot["text"])
    return "\n".join(lines)


def _validate_shots(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise _fail("'shots' must be a non-empty list")
    if len(value) > MAX_SHOTS:
        raise _fail(f"'shots' has {len(value)} entries, max is {MAX_SHOTS}")
    timed = None
    prev_end = None
    for i, shot in enumerate(value):
        if not isinstance(shot, dict):
            raise _fail(f"shots[{i}] must be an object")
        unknown = set(shot) - _SHOT_KEYS
        if unknown:
            raise _fail(f"shots[{i}] has unknown keys: {sorted(unknown)}")
        text = shot.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _fail(f"shots[{i}].text must be a non-empty string")
        if shot.get("label") is not None and not isinstance(shot["label"], str):
            raise _fail(f"shots[{i}].label must be a string")
        has_start = "start_s" in shot
        has_end = "end_s" in shot
        if has_start != has_end:
            raise _fail(f"shots[{i}] must carry both start_s and end_s, or neither")
        if timed is None:
            timed = has_start
        elif timed != has_start:
            raise _fail("shots must either all carry start_s/end_s or none do")
        if has_start:
            start, end = shot["start_s"], shot["end_s"]
            if isinstance(start, bool) or not isinstance(start, (int, float)):
                raise _fail(f"shots[{i}].start_s must be a number")
            if isinstance(end, bool) or not isinstance(end, (int, float)):
                raise _fail(f"shots[{i}].end_s must be a number")
            if start < 0 or end <= start:
                raise _fail(f"shots[{i}] needs 0 <= start_s < end_s")
            if prev_end is not None and start < prev_end:
                raise _fail(f"shots[{i}] starts before the previous shot ends")
            prev_end = end
    return value


@dataclass(frozen=True)
class RefImage:
    """One reference image: a local file or a Drive file, plus its SHA-256.

    Exactly one of local_path / drive_file_id must be set. The SHA-256
    is always required: local files are verified against it before any
    upload, Drive files are verified against it right after download,
    and the remote worker re-verifies the received bytes before
    inference. A Drive file id alone can never prove content identity.
    """

    sha256: str
    local_path: str | None = None
    drive_file_id: str | None = None

    @classmethod
    def from_dict(cls, value, field_name: str) -> "RefImage":
        if not isinstance(value, dict):
            raise _fail(f"{field_name} must be an object")
        unknown = set(value) - _REF_IMAGE_KEYS
        if unknown:
            raise _fail(f"{field_name} has unknown keys: {sorted(unknown)}")
        sha256 = _check_sha256(value.get("sha256"), f"{field_name}.sha256")
        local_path = value.get("local_path")
        drive_file_id = value.get("drive_file_id")
        if (local_path is None) == (drive_file_id is None):
            raise _fail(
                f"{field_name} needs exactly one of 'local_path' / 'drive_file_id'")
        if local_path is not None and (not isinstance(local_path, str) or not local_path):
            raise _fail(f"{field_name}.local_path must be a non-empty string")
        if drive_file_id is not None and (not isinstance(drive_file_id, str) or not drive_file_id):
            raise _fail(f"{field_name}.drive_file_id must be a non-empty string")
        return cls(sha256=sha256, local_path=local_path, drive_file_id=drive_file_id)

    def to_dict(self) -> dict:
        d: dict = {"sha256": self.sha256}
        if self.local_path is not None:
            d["local_path"] = self.local_path
        else:
            d["drive_file_id"] = self.drive_file_id
        return d


@dataclass(frozen=True)
class TaskSpec:
    """A validated, self-contained MiniMax-H3 video task.

    `origin` is provenance metadata, not spec content: "task-spec" for a
    spec loaded from JSON, "variant-adapter" for the built-in A/B
    fixture. It is never read from (or written to) the JSON file.
    """

    task_id: str
    output_prefix: str
    ref_image_1: RefImage
    prompt: str
    seed: int
    ref_image_2: RefImage | None = None
    width: int = WIDTH
    height: int = HEIGHT
    fps: int = FPS
    animation_frames: int = ANIMATION_FRAMES
    tail_card_frames: int = 0
    steps: int = STEPS
    output_width: int = OUTPUT_WIDTH_DEFAULT
    output_height: int = OUTPUT_HEIGHT_DEFAULT
    origin: str = field(default="task-spec", compare=False)

    @property
    def effective_ref_image_2(self) -> RefImage:
        """Node 139 reuses ref_image_1 when no second image is given."""
        return self.ref_image_2 if self.ref_image_2 is not None else self.ref_image_1

    @property
    def total_frames(self) -> int:
        return self.animation_frames + self.tail_card_frames

    @property
    def duration_sec(self) -> float:
        return self.total_frames / self.fps

    def to_dict(self) -> dict:
        d: dict = {
            "task_id": self.task_id,
            "output_prefix": self.output_prefix,
            "ref_image_1": self.ref_image_1.to_dict(),
            "prompt": self.prompt,
            "seed": self.seed,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "animation_frames": self.animation_frames,
            "tail_card_frames": self.tail_card_frames,
            "steps": self.steps,
            "output_width": self.output_width,
            "output_height": self.output_height,
        }
        if self.ref_image_2 is not None:
            d["ref_image_2"] = self.ref_image_2.to_dict()
        return d

    @classmethod
    def from_dict(cls, value) -> "TaskSpec":
        """Parse + validate a spec dict. Fail-closed on any deviation."""
        if not isinstance(value, dict):
            raise _fail("spec must be a JSON object")
        unknown = set(value) - _TOP_LEVEL_KEYS
        if unknown:
            raise _fail(f"unknown top-level keys: {sorted(unknown)}")
        task_id = _check_name(_require_str(value, "task_id"), "task_id")
        output_prefix = _check_name(_require_str(value, "output_prefix"), "output_prefix")
        if "ref_image_1" not in value:
            raise _fail("'ref_image_1' is required (ComfyUI node 137)")
        ref_image_1 = RefImage.from_dict(value["ref_image_1"], "ref_image_1")
        ref_image_2 = None
        if value.get("ref_image_2") is not None:
            ref_image_2 = RefImage.from_dict(value["ref_image_2"], "ref_image_2")
        has_prompt = "prompt" in value and value["prompt"] is not None
        has_shots = "shots" in value and value["shots"] is not None
        if has_prompt == has_shots:
            raise _fail("exactly one of 'prompt' / 'shots' must be present")
        if has_prompt:
            prompt = value["prompt"]
            if not isinstance(prompt, str) or not prompt.strip():
                raise _fail("'prompt' must be a non-empty string")
            if len(prompt) > MAX_PROMPT_CHARS:
                raise _fail(f"'prompt' is {len(prompt)} chars, max is {MAX_PROMPT_CHARS}")
        else:
            prompt = _render_shots(_validate_shots(value["shots"]))
        if "seed" not in value or value["seed"] is None:
            raise _fail("'seed' is required (a fixed seed is what makes a task reproducible)")
        seed = _require_int(value, "seed", None, SEED_MIN, SEED_MAX)
        width = _require_int(value, "width", WIDTH, WIDTH_MIN, WIDTH_MAX)
        if width % 16 != 0:
            raise _fail(f"'width'={width} must be a multiple of 16")
        height = _require_int(value, "height", HEIGHT, HEIGHT_MIN, HEIGHT_MAX)
        if height % 16 != 0:
            raise _fail(f"'height'={height} must be a multiple of 16")
        fps = _require_int(value, "fps", FPS, FPS_MIN, FPS_MAX)
        animation_frames = _require_int(
            value, "animation_frames", ANIMATION_FRAMES,
            ANIMATION_FRAMES_MIN, ANIMATION_FRAMES_MAX)
        if animation_frames % 17 != 5:
            raise _fail(
                f"'animation_frames'={animation_frames} violates the MiniMax H3 "
                "frame rule (animation_frames % 17 must be 5)")
        tail_card_frames = _require_int(
            value, "tail_card_frames", 0,
            TAIL_CARD_FRAMES_MIN, TAIL_CARD_FRAMES_MAX)
        steps = _require_int(value, "steps", STEPS, STEPS_MIN, STEPS_MAX)
        output_width = _require_int(
            value, "output_width", OUTPUT_WIDTH_DEFAULT,
            OUTPUT_DIM_MIN, OUTPUT_DIM_MAX)
        if output_width % 2 != 0:
            raise _fail(f"'output_width'={output_width} must be even (H.264 yuv420p)")
        output_height = _require_int(
            value, "output_height", OUTPUT_HEIGHT_DEFAULT,
            OUTPUT_DIM_MIN, OUTPUT_DIM_MAX)
        if output_height % 2 != 0:
            raise _fail(f"'output_height'={output_height} must be even (H.264 yuv420p)")
        return cls(
            task_id=task_id, output_prefix=output_prefix,
            ref_image_1=ref_image_1, ref_image_2=ref_image_2,
            prompt=prompt, seed=seed, width=width, height=height, fps=fps,
            animation_frames=animation_frames, tail_card_frames=tail_card_frames,
            steps=steps, output_width=output_width, output_height=output_height,
        )


def load_task_spec(path) -> TaskSpec:
    """Read a task-spec JSON file and validate it. Any failure raises
    ValueError naming the file; a spec that loads here is safe to run."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _fail(f"task-spec file not found: {path}") from None
    except (OSError, ValueError) as e:
        raise _fail(f"task-spec file {path} is not readable JSON: {e}") from None
    try:
        return TaskSpec.from_dict(raw)
    except ValueError as e:
        raise ValueError(f"{e} (in {path})") from None


def validate_ref_image_file(path, sha256: str) -> str:
    """Fail-closed local reference-image check: the file must exist and
    its SHA-256 must equal the spec value. Returns the digest."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"Reference image missing: {p}")
    digest = sha256_file(p)
    if digest.lower() != sha256.lower():
        raise ValueError(
            f"Reference image SHA-256 mismatch for {p}: "
            f"expected {sha256[:16]}..., got {digest[:16]}...")
    return digest


def task_spec_for_variant(variant: str, production_task_id: str,
                          poster_path, seed: int | None = None) -> TaskSpec:
    """Built-in test-fixture adapter: --variant A|B as a TaskSpec.

    This is the ONLY place the old hardcoded A/B contract lives now.
    It reproduces the frozen production values exactly (poster reference
    on both nodes, the A/B storyboard prompt, 736x1312, 226 animation
    frames + a 48-frame poster tail, 25 steps, seed 42/43). New tasks
    must use --task-spec instead; this adapter exists so historical
    regression tests and the original poster runs keep working.
    """
    if variant not in ("A", "B"):
        raise ValueError("variant must be A or B")
    _check_name(production_task_id, "production_task_id")
    poster_path = str(poster_path)
    if not poster_path:
        raise ValueError("poster_path is required for the variant adapter")
    return TaskSpec(
        task_id=production_task_id,
        output_prefix=variant,
        ref_image_1=RefImage(sha256=POSTER_SHA256, local_path=poster_path),
        ref_image_2=None,
        prompt=prompt_for(variant),
        seed=seed if seed is not None else (42 if variant == "A" else 43),
        width=WIDTH, height=HEIGHT, fps=FPS,
        animation_frames=ANIMATION_FRAMES, tail_card_frames=POSTER_FRAMES,
        steps=STEPS, output_width=OUTPUT_WIDTH_DEFAULT,
        output_height=OUTPUT_HEIGHT_DEFAULT,
        origin="variant-adapter",
    )


def spec_with_seed(spec: TaskSpec, seed: int) -> TaskSpec:
    """Return a copy of a spec with a different fixed seed."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise _fail("'seed' must be an integer")
    if not SEED_MIN <= seed <= SEED_MAX:
        raise _fail(f"'seed'={seed} is outside the legal range {SEED_MIN}..{SEED_MAX}")
    return replace(spec, seed=seed)


__all__ = [
    "TaskSpec", "RefImage", "load_task_spec", "validate_ref_image_file",
    "task_spec_for_variant", "spec_with_seed",
    "OUTPUT_WIDTH_DEFAULT", "OUTPUT_HEIGHT_DEFAULT",
    "MAX_PROMPT_CHARS", "MAX_SHOTS",
]
