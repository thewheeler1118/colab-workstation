#!/usr/bin/env python3
"""
generate_video.py — Google Colab 云端视频生成执行脚本 (SVD / 候选 Wan 2.2)

重要说明：
1. 本脚本设计为在远程 Google Colab GPU (优先 T4) 运行时上执行，严禁在 macOS 本地执行 GPU 推理。
2. 当前状态：本地未找到历史原始 SVD Notebook，SVD_INTEGRATION_STATUS = "PENDING_NOTEBOOK"，
   在获得授权传入 --allow-unverified-svd 时使用标准 Diffusers StableVideoDiffusionPipeline (fp16 + CPU offload) 执行真实生成。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

SVD_INTEGRATION_STATUS = "PENDING_NOTEBOOK"
RESULT_JSON_PREFIX = "__AIVIDEO_RESULT_JSON__:"
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SUPPORTED_MODELS = {"svd", "wan2.2"}
SUPPORTED_QUANTIZATIONS = {"none", "fp8", "int8", "nf4"}
SUPPORTED_OFFLOAD_STRATEGIES = {"model_cpu_offload", "sequential_cpu_offload", "none"}

# 细分模型资源配置矩阵（按具体 model_id、参数规模、基准显存与能力特征定义）
MODEL_RESOURCE_PROFILES: Dict[str, Dict[str, Any]] = {
    "stabilityai/stable-video-diffusion-img2vid-xt": {
        "family": "svd",
        "param_scale": "1.5B",
        "base_weights_fp16_gb": 5.5,
        "base_activation_576p_14f_gb": 5.82,
        "offload_residency_factor": {
            "sequential_cpu_offload": 0.62,
            "model_cpu_offload": 0.85,
            "none": 1.55,
        },
        "capabilities": {
            "max_scene_complexity": "standard",
            "motion_control": "standard",
            "reference_consistency": "strict",
        },
    },
    "Wan-AI/Wan2.2-TI2V-5B-Diffusers": {
        "family": "wan2.2",
        "param_scale": "5B",
        "base_weights_fp16_gb": 10.0,
        "base_activation_576p_14f_gb": 4.7,
        "offload_residency_factor": {
            "sequential_cpu_offload": 0.62,
            "model_cpu_offload": 0.80,
            "none": 1.35,
        },
        "capabilities": {
            "max_scene_complexity": "high",
            "motion_control": "high",
            "reference_consistency": "high",
        },
    },
    "Wan-AI/Wan2.2-I2V-A14B-Diffusers": {
        "family": "wan2.2",
        "param_scale": "A14B",
        "base_weights_fp16_gb": 28.0,  # MoE 27B total / 14B active
        "base_activation_576p_14f_gb": 5.5,
        "offload_residency_factor": {
            "sequential_cpu_offload": 0.45,
            "model_cpu_offload": 0.52,
            "none": 1.05,
        },
        "capabilities": {
            "max_scene_complexity": "ultra",
            "motion_control": "ultra",
            "reference_consistency": "strict",
        },
    },
}


class ParameterValidationError(ValueError):
    """Raised when video generation parameters or input files fail pre-execution validation."""


@dataclass
class VideoGenerationParams:
    input_path: str
    output_path: str
    active_model: str = "svd"
    model_id: str = "stabilityai/stable-video-diffusion-img2vid-xt"
    variant: str = "fp16"
    quantization: str = "none"
    offload_strategy: str = "model_cpu_offload"
    num_frames: int = 14
    width: int = 1024
    height: int = 576
    fps: int = 7
    seed: int = 42
    num_inference_steps: int = 25
    min_guidance_scale: float = 1.0
    max_guidance_scale: float = 3.0
    motion_bucket_id: int = 127
    noise_aug_strength: float = 0.02
    decode_chunk_size: int = 8
    validate_only: bool = False
    mock_render: bool = False
    allow_unverified_svd: bool = False


def detect_image_signature(file_path: Path) -> Optional[str]:
    """Check magic bytes of an image file without requiring Pillow locally."""
    try:
        with file_path.open("rb") as f:
            header = f.read(16)
    except OSError as exc:
        raise ParameterValidationError(f"无法读取输入图片文件: {file_path} ({exc})") from exc

    if len(header) < 8:
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith(b"BM"):
        return "bmp"
    return None


def inspect_mp4_atoms(file_path: Path) -> Dict[str, Any]:
    """
    Parse ISO BMFF / MP4 container atoms (ftyp, moov, mdat, mvhd, stsz) in pure Python
    to extract real frame counts, duration, and box structure.
    """
    info: Dict[str, Any] = {
        "has_ftyp": False,
        "has_moov": False,
        "has_mdat": False,
        "mdat_size": 0,
        "sample_count": 0,
        "duration_sec": 0.0,
        "is_mock": False,
    }
    if not file_path.exists() or not file_path.is_file():
        return info

    data = file_path.read_bytes()
    total_len = len(data)
    offset = 0

    def _scan_boxes(buf: bytes, start: int, end: int) -> None:
        pos = start
        while pos + 8 <= end:
            size = struct.unpack(">I", buf[pos : pos + 4])[0]
            box_type = buf[pos + 4 : pos + 8]
            header_size = 8
            if size == 1:
                if pos + 16 > end:
                    break
                size = struct.unpack(">Q", buf[pos + 8 : pos + 16])[0]
                header_size = 16
            elif size == 0:
                size = end - pos
            if size < header_size or pos + size > end:
                break

            payload_start = pos + header_size
            payload_end = pos + size

            if box_type == b"ftyp":
                info["has_ftyp"] = True
            elif box_type == b"moov":
                info["has_moov"] = True
                _scan_boxes(buf, payload_start, payload_end)
            elif box_type == b"mdat":
                info["has_mdat"] = True
                info["mdat_size"] += payload_end - payload_start
                if b"ai-video-mock-executor" in buf[payload_start : min(payload_end, payload_start + 512)]:
                    info["is_mock"] = True
            elif box_type in {b"trak", b"mdia", b"minf", b"stbl"}:
                _scan_boxes(buf, payload_start, payload_end)
            elif box_type == b"mvhd":
                if payload_end - payload_start >= 20:
                    version = buf[payload_start]
                    if version == 0 and payload_end - payload_start >= 20:
                        timescale, duration = struct.unpack(
                            ">II", buf[payload_start + 12 : payload_start + 20]
                        )
                        if timescale > 0:
                            info["duration_sec"] = round(duration / float(timescale), 3)
                    elif version == 1 and payload_end - payload_start >= 28:
                        timescale, duration = struct.unpack(
                            ">IQ", buf[payload_start + 20 : payload_start + 32]
                        )
                        if timescale > 0:
                            info["duration_sec"] = round(duration / float(timescale), 3)
            elif box_type == b"stsz":
                if payload_end - payload_start >= 12:
                    sample_count = struct.unpack(
                        ">I", buf[payload_start + 8 : payload_start + 12]
                    )[0]
                    if sample_count > info["sample_count"]:
                        info["sample_count"] = sample_count

            pos += size

    _scan_boxes(data, offset, total_len)
    return info


def is_valid_mp4_file(
    file_path: Path,
    min_size_bytes: int = 32,
    require_real_video: bool = False,
) -> Tuple[bool, str]:
    """
    Verify that a file exists, is non-empty, and has a valid MP4 ISO BMFF 'ftyp' box.
    If `require_real_video=True`, also requires a real 'moov' atom, non-mock 'mdat', and sample_count > 0.
    """
    if not file_path.exists():
        return False, f"MP4 文件不存在: {file_path}"
    if not file_path.is_file():
        return False, f"MP4 路径不是普通文件: {file_path}"
    try:
        size = file_path.stat().st_size
        if size < min_size_bytes:
            return False, f"MP4 文件体积异常过小 ({size} bytes < {min_size_bytes} bytes)"
        with file_path.open("rb") as f:
            header = f.read(16)
    except OSError as exc:
        return False, f"读取 MP4 文件失败: {exc}"

    if len(header) < 8 or header[4:8] != b"ftyp":
        return False, "MP4 文件缺少合法的 ISO BMFF 'ftyp' 容器签名头"

    if require_real_video:
        atoms = inspect_mp4_atoms(file_path)
        if atoms.get("is_mock"):
            return False, "检测到模拟生成的伪 MP4 文件 (mock_render)，非真实视频"
        if not atoms.get("has_moov") or atoms.get("sample_count", 0) <= 0:
            return (
                False,
                f"MP4 缺少有效的 moov 视频轨道或帧数为 0 (atoms={atoms})",
            )

    return True, f"MP4 验证通过 ({size} bytes)"


def validate_generation_params(
    params: VideoGenerationParams,
    check_input_exists: bool = True,
) -> List[str]:
    """
    Validate all video generation parameters prior to allocating or running GPU resources.
    Returns a list of error strings (empty if valid), or raises ParameterValidationError.
    """
    errors: List[str] = []

    if not params.input_path or not str(params.input_path).strip():
        errors.append("输入图片路径 (input_path) 不能为空")
    else:
        in_path = Path(params.input_path)
        ext = in_path.suffix.lower()
        if ext not in SUPPORTED_IMAGE_EXTENSIONS:
            errors.append(
                f"不支持的输入图片扩展名 '{ext}'，仅支持: {sorted(SUPPORTED_IMAGE_EXTENSIONS)}"
            )
        if check_input_exists:
            if not in_path.exists():
                errors.append(f"输入图片文件不存在: {in_path}")
            elif not in_path.is_file():
                errors.append(f"输入图片路径不是文件: {in_path}")
            elif in_path.stat().st_size == 0:
                errors.append(f"输入图片文件为空 (0 bytes): {in_path}")
            else:
                sig = detect_image_signature(in_path)
                if sig is None:
                    errors.append(f"输入图片文件头签名无效或已损坏: {in_path}")

    if not params.output_path or not str(params.output_path).strip():
        errors.append("输出视频路径 (output_path) 不能为空")
    else:
        out_path = Path(params.output_path)
        if out_path.suffix.lower() != ".mp4":
            errors.append(f"输出视频路径必须以 .mp4 结尾，当前为: '{out_path.suffix}'")

    if params.active_model not in SUPPORTED_MODELS:
        errors.append(
            f"不支持的模型类型 '{params.active_model}'，可选值: {sorted(SUPPORTED_MODELS)}"
        )

    if not params.model_id or not str(params.model_id).strip():
        errors.append("模型标识 (model_id) 不能为空")

    if not isinstance(params.num_frames, int) or not (1 <= params.num_frames <= 100):
        errors.append(f"帧数 (num_frames) 必须在 1 到 100 之间，当前为: {params.num_frames}")

    align_step = 64 if params.active_model == "svd" else 16
    if not isinstance(params.width, int) or not (256 <= params.width <= 2048):
        errors.append(f"宽度 (width) 必须在 256 到 2048 之间，当前为: {params.width}")
    elif params.width % align_step != 0:
        errors.append(
            f"宽度 (width) 必须为 {align_step} 的整数倍以兼容 {params.active_model}，当前为: {params.width}"
        )

    if not isinstance(params.height, int) or not (256 <= params.height <= 2048):
        errors.append(f"高度 (height) 必须在 256 到 2048 之间，当前为: {params.height}")
    elif params.height % align_step != 0:
        errors.append(
            f"高度 (height) 必须为 {align_step} 的整数倍以兼容 {params.active_model}，当前为: {params.height}"
        )

    if str(params.quantization).lower().strip() not in SUPPORTED_QUANTIZATIONS:
        errors.append(
            f"不支持的量化方式 '{params.quantization}'，可选值: {sorted(SUPPORTED_QUANTIZATIONS)}"
        )

    if str(params.offload_strategy).lower().strip() not in SUPPORTED_OFFLOAD_STRATEGIES:
        errors.append(
            f"不支持的 offload 策略 '{params.offload_strategy}'，可选值: {sorted(SUPPORTED_OFFLOAD_STRATEGIES)}"
        )

    if not isinstance(params.fps, int) or not (1 <= params.fps <= 60):
        errors.append(f"帧率 (fps) 必须在 1 到 60 之间，当前为: {params.fps}")

    if not isinstance(params.seed, int) or params.seed < 0:
        errors.append(f"随机种子 (seed) 必须为非负整数，当前为: {params.seed}")

    if not isinstance(params.num_inference_steps, int) or not (1 <= params.num_inference_steps <= 200):
        errors.append(
            f"推理步数 (num_inference_steps) 必须在 1 到 200 之间，当前为: {params.num_inference_steps}"
        )

    if params.min_guidance_scale < 0.0:
        errors.append(
            f"最小引导系数 (min_guidance_scale) 不能小于 0，当前为: {params.min_guidance_scale}"
        )

    if params.max_guidance_scale < params.min_guidance_scale:
        errors.append(
            f"最大引导系数 ({params.max_guidance_scale}) 不能小于最小引导系数 ({params.min_guidance_scale})"
        )

    if not isinstance(params.motion_bucket_id, int) or not (1 <= params.motion_bucket_id <= 255):
        errors.append(
            f"运动幅度参数 (motion_bucket_id) 必须在 1 到 255 之间，当前为: {params.motion_bucket_id}"
        )

    if not (0.0 <= params.noise_aug_strength <= 1.0):
        errors.append(
            f"噪声增强强度 (noise_aug_strength) 必须在 0.0 到 1.0 之间，当前为: {params.noise_aug_strength}"
        )

    if not isinstance(params.decode_chunk_size, int) or not (1 <= params.decode_chunk_size <= 64):
        errors.append(
            f"解码分块大小 (decode_chunk_size) 必须在 1 到 64 之间，当前为: {params.decode_chunk_size}"
        )

    if errors:
        raise ParameterValidationError("; ".join(errors))
    return errors


def resolve_model_profile(active_model: str, model_id: str) -> Dict[str, Any]:
    """Resolve concrete model resource profile by model_id and active_model."""
    mid = str(model_id).strip()
    if mid in MODEL_RESOURCE_PROFILES:
        return MODEL_RESOURCE_PROFILES[mid]
    mid_low = mid.lower()
    fam = str(active_model).strip().lower()
    if fam == "svd" or "stable-video-diffusion" in mid_low or "svd" in mid_low:
        return MODEL_RESOURCE_PROFILES["stabilityai/stable-video-diffusion-img2vid-xt"]
    if "5b" in mid_low:
        return MODEL_RESOURCE_PROFILES["Wan-AI/Wan2.2-TI2V-5B-Diffusers"]
    return MODEL_RESOURCE_PROFILES["Wan-AI/Wan2.2-I2V-A14B-Diffusers"]


def estimate_model_vram_requirement(params: VideoGenerationParams) -> Dict[str, Any]:
    """
    Calculate the minimum and recommended GPU VRAM (in GB) based on:
      1. Concrete `model_id` & parameter scale (`1.5B` SVD, `5B` Wan2.2-TI2V-5B, `A14B` Wan2.2-I2V-A14B)
      2. Precision (`variant`: `fp16` / `bf16` / `fp32`)
      3. Quantization (`quantization`: `none` / `fp8` / `int8` / `nf4`)
      4. Resolution (`width x height`)
      5. Frame count (`num_frames`)
      6. Offload strategy (`offload_strategy`: `sequential_cpu_offload` / `model_cpu_offload` / `none`)
      7. Decode chunk size (`decode_chunk_size`)
    Does NOT treat all Wan 2.2 configurations as flat 20 GB VRAM.
    """
    profile = resolve_model_profile(params.active_model, params.model_id)
    param_scale = str(profile["param_scale"])
    base_weights_gb = float(profile["base_weights_fp16_gb"])
    base_act_gb = float(profile["base_activation_576p_14f_gb"])

    variant_low = str(params.variant).lower().strip()
    precision_mult = 1.0 if variant_low in {"fp16", "bf16"} else 1.85

    quant_low = str(params.quantization).lower().strip() or "none"
    quant_mult_map = {
        "none": 1.0,
        "fp8": 0.58,
        "int8": 0.58,
        "nf4": 0.42,
    }
    quant_mult = quant_mult_map.get(quant_low, 1.0)

    offload_low = str(params.offload_strategy).lower().strip() or "model_cpu_offload"
    residency_map: Dict[str, float] = profile["offload_residency_factor"]
    offload_mult = float(residency_map.get(offload_low, residency_map["model_cpu_offload"]))

    pixel_ratio = max(0.30, (params.width * params.height) / float(1024 * 576))
    frame_ratio = max(0.50, params.num_frames / 14.0)
    chunk_factor = max(0.85, min(1.30, params.decode_chunk_size / 8.0))

    # Peak weight memory resident on GPU simultaneously
    resident_weights_gb = base_weights_gb * precision_mult * quant_mult * offload_mult
    # Peak activation + KV cache + VAE decode memory
    act_prec_mult = 1.0 if variant_low in {"fp16", "bf16"} else 1.45
    activation_gb = (
        base_act_gb
        * (pixel_ratio ** 0.85)
        * (frame_ratio ** 0.65)
        * chunk_factor
        * act_prec_mult
    )
    # CUDA context & allocator fragmentation reserve
    cuda_context_gb = 1.5

    raw_min_vram = resident_weights_gb + activation_gb + cuda_context_gb
    min_vram_gb = round(max(8.5, raw_min_vram), 2)

    # Recommended VRAM gives headroom so offload/chunking does not bottleneck or risk OOM
    headroom_factor = 1.20 if offload_low != "none" else 1.12
    recommended_vram_gb = round(max(min_vram_gb + 2.0, min_vram_gb * headroom_factor), 2)

    if min_vram_gb <= 14.5:
        vram_tier = "T4_compatible (<=14.5GB)"
    elif min_vram_gb <= 22.5:
        vram_tier = "L4_compatible (<=22.5GB)"
    elif min_vram_gb <= 39.0:
        vram_tier = "A100_compatible (<=39.0GB)"
    else:
        vram_tier = "H100_required (>39.0GB)"

    return {
        "active_model": params.active_model,
        "model_id": params.model_id,
        "param_scale": param_scale,
        "variant": params.variant,
        "quantization": quant_low,
        "offload_strategy": offload_low,
        "resolution": f"{params.width}x{params.height}",
        "num_frames": params.num_frames,
        "decode_chunk_size": params.decode_chunk_size,
        "resident_weights_gb": round(resident_weights_gb, 2),
        "activation_gb": round(activation_gb, 2),
        "min_vram_gb": min_vram_gb,
        "recommended_vram_gb": recommended_vram_gb,
        "vram_tier": vram_tier,
        "capabilities": profile["capabilities"],
    }


def write_mock_mp4(output_path: Path, params: VideoGenerationParams) -> int:
    """
    Generate a minimal valid ISO BMFF MP4 container file for offline unit test verification only.
    Does not require FFmpeg or GPU libraries.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ftyp_payload = b"isom\x00\x00\x02\x00isomiso2mp41"
    ftyp_box = struct.pack(">I4s", 8 + len(ftyp_payload), b"ftyp") + ftyp_payload

    meta_json = json.dumps(
        {
            "generator": "ai-video-mock-executor",
            "svd_integration_status": SVD_INTEGRATION_STATUS,
            "model_id": params.model_id,
            "num_frames": params.num_frames,
            "resolution": f"{params.width}x{params.height}",
            "fps": params.fps,
            "seed": params.seed,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    mdat_box = struct.pack(">I4s", 8 + len(meta_json), b"mdat") + meta_json

    content = ftyp_box + mdat_box
    output_path.write_bytes(content)
    return len(content)


def run_svd_inference(params: VideoGenerationParams) -> Dict[str, Any]:
    """
    Execute Stable Video Diffusion inference on a remote Colab GPU (e.g. T4).
    Verifies GPU availability, VRAM capacity, and performs real frame decoding check after generation.
    """
    if sys.platform == "darwin" and not params.mock_render:
        raise RuntimeError(
            "安全阻断：禁止在 macOS 本地执行 SVD GPU 模型推理或下载模型权重！"
            "请通过 colab_manager.py 在远程 Colab GPU 会话中运行。"
        )

    if SVD_INTEGRATION_STATUS == "PENDING_NOTEBOOK" and not params.allow_unverified_svd:
        raise RuntimeError(
            "SVD 模型集成当前标记为 '待接入' (PENDING_NOTEBOOK)：本地未找到此前成功的原始 SVD Notebook。"
            "如需使用标准 Diffusers SVD 管线执行，请显式传入 --allow-unverified-svd。"
        )

    import torch  # type: ignore
    from diffusers import StableVideoDiffusionPipeline  # type: ignore
    from diffusers.utils import export_to_video, load_image  # type: ignore

    if not torch.cuda.is_available():
        raise RuntimeError("远程环境未检测到可用 CUDA GPU，拒绝在 CPU 上运行 SVD 推理。")

    start_ts = time.time()
    torch.cuda.reset_peak_memory_stats(0)
    device_name = torch.cuda.get_device_name(0)
    props = torch.cuda.get_device_properties(0)
    vram_gb = round(props.total_memory / (1024**3), 2)
    vram_req = estimate_model_vram_requirement(params)
    min_req_vram = float(vram_req["min_vram_gb"])
    print(
        f"[generate_video] 检测到远程 GPU: {device_name} | CUDA: {torch.version.cuda} | VRAM: {vram_gb} GB (最低要求: {min_req_vram} GB)",
        flush=True,
    )
    if vram_gb < min_req_vram:
        raise RuntimeError(
            f"GPU 显存不足 ({vram_gb} GB < {min_req_vram} GB)，无法安全运行 {params.active_model} ({params.width}x{params.height}, {params.num_frames} frames)。"
        )

    print(
        f"[generate_video] 正在加载模型: {params.model_id} (variant={params.variant}, source=standard_diffusers_svd)...",
        flush=True,
    )

    pipe = StableVideoDiffusionPipeline.from_pretrained(
        params.model_id,
        torch_dtype=torch.float16 if params.variant == "fp16" else torch.float32,
        variant=params.variant if params.variant == "fp16" else None,
    )
    # 针对 Colab T4 (15-16GB VRAM) 启用显存优化
    pipe.enable_model_cpu_offload()
    try:
        if hasattr(pipe, "unet") and hasattr(pipe.unet, "enable_forward_chunking"):
            pipe.unet.enable_forward_chunking()
    except Exception as chunk_exc:  # noqa: BLE001
        print(f"[generate_video] 提示: enable_forward_chunking 跳过 ({chunk_exc})", flush=True)

    image = load_image(params.input_path)
    image = image.convert("RGB").resize((params.width, params.height))

    generator = torch.manual_seed(params.seed)
    print(
        f"[generate_video] 开始推理: frames={params.num_frames}, size={params.width}x{params.height}, "
        f"steps={params.num_inference_steps}, fps={params.fps}, seed={params.seed}",
        flush=True,
    )

    frames = pipe(
        image,
        width=params.width,
        height=params.height,
        num_frames=params.num_frames,
        num_inference_steps=params.num_inference_steps,
        min_guidance_scale=params.min_guidance_scale,
        max_guidance_scale=params.max_guidance_scale,
        fps=params.fps,
        motion_bucket_id=params.motion_bucket_id,
        noise_aug_strength=params.noise_aug_strength,
        decode_chunk_size=params.decode_chunk_size,
        generator=generator,
    ).frames[0]

    out_path = Path(params.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        export_to_video(frames, str(out_path), fps=params.fps)
    except Exception as exp_exc:  # noqa: BLE001
        print(f"[generate_video] export_to_video 回退至 imageio/cv2 ({exp_exc})", flush=True)
        import numpy as np  # type: ignore
        import imageio  # type: ignore

        np_frames = [np.array(f.convert("RGB")) if hasattr(f, "convert") else np.array(f) for f in frames]
        imageio.mimsave(str(out_path), np_frames, fps=params.fps, codec="libx264")

    elapsed = round(time.time() - start_ts, 2)
    valid, msg = is_valid_mp4_file(out_path, min_size_bytes=1024, require_real_video=True)
    if not valid:
        raise RuntimeError(f"生成的 MP4 输出文件校验失败: {msg}")

    # 使用 OpenCV / imageio 实测解码帧数与分辨率
    decoded_frames = 0
    decoded_w = 0
    decoded_h = 0
    decoded_fps = float(params.fps)
    try:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(str(out_path))
        if cap.isOpened():
            decoded_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            decoded_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps_val = cap.get(cv2.CAP_PROP_FPS)
            if fps_val and fps_val > 0:
                decoded_fps = round(float(fps_val), 2)
            while True:
                ret, _ = cap.read()
                if not ret:
                    break
                decoded_frames += 1
            cap.release()
    except Exception:
        atoms = inspect_mp4_atoms(out_path)
        decoded_frames = int(atoms.get("sample_count", len(frames)))
        decoded_w = params.width
        decoded_h = params.height

    if decoded_frames != params.num_frames:
        raise RuntimeError(
            f"解码帧数 ({decoded_frames}) 与预期帧数 ({params.num_frames}) 不一致"
        )

    peak_vram_gb = round(torch.cuda.max_memory_allocated(0) / (1024**3), 2)
    peak_reserved_gb = round(torch.cuda.max_memory_reserved(0) / (1024**3), 2)
    sha256_hex = hashlib.sha256(out_path.read_bytes()).hexdigest()
    return {
        "status": "completed",
        "code_source": "standard_diffusers_svd (original_notebook_missing)",
        "svd_integration_status": SVD_INTEGRATION_STATUS,
        "model_id": params.model_id,
        "gpu_device": device_name,
        "vram_gb": vram_gb,
        "peak_vram_gb": peak_vram_gb,
        "peak_vram_reserved_gb": peak_reserved_gb,
        "output_path": str(out_path),
        "file_size_bytes": out_path.stat().st_size,
        "sha256": sha256_hex,
        "decoded_frames": decoded_frames,
        "width": decoded_w or params.width,
        "height": decoded_h or params.height,
        "fps": decoded_fps,
        "duration_seconds": round(decoded_frames / float(params.fps), 2),
        "elapsed_seconds": elapsed,
    }


def _env_or_default(env_key: str, default: Any, cast_type: Any = str) -> Any:
    val = os.environ.get(env_key)
    if val is None or val == "":
        return default
    if cast_type is bool:
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return cast_type(val)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Colab 云端 AI 视频生成脚本 (Stable Video Diffusion / 候选 Wan 2.2)"
    )
    parser.add_argument(
        "--input",
        dest="input_path",
        default=_env_or_default("AIVIDEO_INPUT", ""),
        help="输入图片路径",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        default=_env_or_default("AIVIDEO_OUTPUT", ""),
        help="输出 MP4 视频路径",
    )
    parser.add_argument(
        "--active-model",
        default=_env_or_default("AIVIDEO_ACTIVE_MODEL", "svd"),
        help="当前使用的模型类型 (默认: svd)",
    )
    parser.add_argument(
        "--model-id",
        default=_env_or_default(
            "AIVIDEO_MODEL_ID", "stabilityai/stable-video-diffusion-img2vid-xt"
        ),
        help="HuggingFace 模型 ID",
    )
    parser.add_argument(
        "--variant",
        default=_env_or_default("AIVIDEO_VARIANT", "fp16"),
        help="模型精度变体 (默认: fp16)",
    )
    parser.add_argument(
        "--quantization",
        default=_env_or_default("AIVIDEO_QUANTIZATION", "none"),
        help="模型量化方式: none | fp8 | int8 | nf4 (默认: none)",
    )
    parser.add_argument(
        "--offload-strategy",
        default=_env_or_default("AIVIDEO_OFFLOAD_STRATEGY", "model_cpu_offload"),
        help="显存卸载策略: model_cpu_offload | sequential_cpu_offload | none (默认: model_cpu_offload)",
    )
    parser.add_argument(
        "--num-frames",
        type=int,
        default=_env_or_default("AIVIDEO_NUM_FRAMES", 14, int),
        help="生成视频总帧数 (默认: 14，即 2 秒 @ 7fps)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=_env_or_default("AIVIDEO_WIDTH", 1024, int),
        help="输出视频宽度，须为 64 的倍数 (默认: 1024)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=_env_or_default("AIVIDEO_HEIGHT", 576, int),
        help="输出视频高度，须为 64 的倍数 (默认: 576)",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=_env_or_default("AIVIDEO_FPS", 7, int),
        help="输出视频帧率 (默认: 7)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=_env_or_default("AIVIDEO_SEED", 42, int),
        help="随机种子 (默认: 42)",
    )
    parser.add_argument(
        "--num-inference-steps",
        type=int,
        default=_env_or_default("AIVIDEO_NUM_INFERENCE_STEPS", 25, int),
        help="扩散采样步数 (默认: 25)",
    )
    parser.add_argument(
        "--min-guidance-scale",
        type=float,
        default=_env_or_default("AIVIDEO_MIN_GUIDANCE_SCALE", 1.0, float),
        help="最小分类器引导强度 (默认: 1.0)",
    )
    parser.add_argument(
        "--max-guidance-scale",
        type=float,
        default=_env_or_default("AIVIDEO_MAX_GUIDANCE_SCALE", 3.0, float),
        help="最大分类器引导强度 (默认: 3.0)",
    )
    parser.add_argument(
        "--motion-bucket-id",
        type=int,
        default=_env_or_default("AIVIDEO_MOTION_BUCKET_ID", 127, int),
        help="运动幅度桶 ID 1-255 (默认: 127)",
    )
    parser.add_argument(
        "--noise-aug-strength",
        type=float,
        default=_env_or_default("AIVIDEO_NOISE_AUG_STRENGTH", 0.02, float),
        help="条件图噪声增强强度 (默认: 0.02)",
    )
    parser.add_argument(
        "--decode-chunk-size",
        type=int,
        default=_env_or_default("AIVIDEO_DECODE_CHUNK_SIZE", 8, int),
        help="VAE 解码分块大小，防止 T4 OOM (默认: 8)",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        default=_env_or_default("AIVIDEO_VALIDATE_ONLY", False, bool),
        help="仅验证参数与输入文件，不执行模型推理",
    )
    parser.add_argument(
        "--mock-render",
        action="store_true",
        default=_env_or_default("AIVIDEO_MOCK_RENDER", False, bool),
        help="模拟执行模式：生成带合法 ftyp 头的测试 MP4 文件（不消耗 GPU）",
    )
    parser.add_argument(
        "--allow-unverified-svd",
        action="store_true",
        default=_env_or_default("AIVIDEO_ALLOW_UNVERIFIED_SVD", False, bool),
        help="在原始 SVD Notebook 尚未核对接入时，允许使用标准 Diffusers SVD 管线执行",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    params = VideoGenerationParams(
        input_path=args.input_path,
        output_path=args.output_path,
        active_model=args.active_model,
        model_id=args.model_id,
        variant=args.variant,
        quantization=args.quantization,
        offload_strategy=args.offload_strategy,
        num_frames=args.num_frames,
        width=args.width,
        height=args.height,
        fps=args.fps,
        seed=args.seed,
        num_inference_steps=args.num_inference_steps,
        min_guidance_scale=args.min_guidance_scale,
        max_guidance_scale=args.max_guidance_scale,
        motion_bucket_id=args.motion_bucket_id,
        noise_aug_strength=args.noise_aug_strength,
        decode_chunk_size=args.decode_chunk_size,
        validate_only=args.validate_only,
        mock_render=args.mock_render,
        allow_unverified_svd=args.allow_unverified_svd,
    )

    try:
        validate_generation_params(params, check_input_exists=True)
    except ParameterValidationError as exc:
        err_payload = {
            "status": "validation_failed",
            "error": str(exc),
            "svd_integration_status": SVD_INTEGRATION_STATUS,
        }
        print(
            f"{RESULT_JSON_PREFIX}{json.dumps(err_payload, ensure_ascii=False)}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    if params.validate_only:
        result = {
            "status": "validated",
            "svd_integration_status": SVD_INTEGRATION_STATUS,
            "params": asdict(params),
        }
        print(f"{RESULT_JSON_PREFIX}{json.dumps(result, ensure_ascii=False)}", flush=True)
        return 0

    if params.mock_render:
        out_path = Path(params.output_path)
        size = write_mock_mp4(out_path, params)
        result = {
            "status": "mock_completed",
            "svd_integration_status": SVD_INTEGRATION_STATUS,
            "output_path": str(out_path),
            "file_size_bytes": size,
        }
        print(f"{RESULT_JSON_PREFIX}{json.dumps(result, ensure_ascii=False)}", flush=True)
        return 0

    try:
        result = run_svd_inference(params)
        print(f"{RESULT_JSON_PREFIX}{json.dumps(result, ensure_ascii=False)}", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        err_payload = {
            "status": "inference_failed",
            "error": str(exc),
            "svd_integration_status": SVD_INTEGRATION_STATUS,
        }
        print(
            f"{RESULT_JSON_PREFIX}{json.dumps(err_payload, ensure_ascii=False)}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
