#!/usr/bin/env python3
"""
colab_manager.py — Google Colab CLI (v0.7.5) 本地自动化编排、两阶段模型/GPU 智能调度与 CU 安全控制器

核心职责：
1. 封装官方 Google Colab CLI (`colab`)，严格使用 subprocess 参数列表调用（禁用 shell=True）。
2. 对齐 `google-colab-cli 0.7.5` 实测参数（纯 `-f` 包装脚本执行，不使用 `--env` 或 `-c`）。
3. 两阶段智能调度器 (`gpu_scheduler`)：
   - 第一阶段（模型与参数决策）：根据分镜复杂度 (`scene_complexity`)、动作控制 (`motion_control`)、参考图一致性 (`reference_consistency`)、任务模式及 CU 预算选择最合适的模型与配置（如 SVD 1.5B、Wan2.2-TI2V-5B 量化/Offload、Wan2.2-I2V-A14B）。
   - 第二阶段（按需匹配 GPU，拒绝盲目追高）：根据具体模型 ID、参数规模、精度、量化方式、分辨率、帧数与 offload 策略计算 `min_vram_gb` 和 `recommended_vram_gb`。在已满足 `recommended_vram_gb` 的候选中优先选择 CU 成本最低的 GPU（绝不把更贵的 GPU 当作同模型画质更好的保证）。
4. 严格安全锁与防绕过机制：
   - 严禁通过自然语言 Prompt 绕过 A100/H100 高阶 GPU 授权锁或 `config.yaml` 中的预算上限 (`max_cu_per_task` / `max_burn_rate_per_hour`)。
   - 支持显式传入 `--authorize-task-gpu` 在单次获得任务级授权时直接根据 Prompt 自动选择并运行 GPU，无需手动修改 `config.yaml`。
5. 三项分离的成本历史台账 (`logs/gpu_cost_history.jsonl`)：
   - 分别保存即时 CU 余额变化 (`cu_delta_immediate`)、按实时费率折算费用 (`cu_estimated_by_burn_rate`) 与最终确认结算费用 (`cu_confirmed_final`)。
   - 受 Colab 异步计费延迟影响的即时差值（如历史 T4 任务的 `0.03 CU`）标记为 `unconfirmed_async_lag` 且 `reliable_for_cost_prediction: false`，绝不直接用于成本预测。
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from generate_video import (
    MODEL_RESOURCE_PROFILES,
    RESULT_JSON_PREFIX,
    SVD_INTEGRATION_STATUS,
    ParameterValidationError,
    VideoGenerationParams,
    estimate_model_vram_requirement,
    inspect_mp4_atoms,
    is_valid_mp4_file,
    validate_generation_params,
)


# ============================================================================
# 异常定义
# ============================================================================


class ColabManagerError(Exception):
    """Base exception for colab_manager errors."""


class CLIMissingError(ColabManagerError):
    """Raised when the `colab` CLI executable cannot be found."""


class AuthenticationError(ColabManagerError):
    """Raised when Colab CLI OAuth2 / ADC authentication fails or prompts for login."""


class SafetyLockError(ColabManagerError):
    """Raised when GPU execution is blocked by safety configuration or unauthorized high-tier/budget bypass."""


class SchedulerValidationError(ParameterValidationError, ColabManagerError):
    """Raised when GPU scheduler mode, allowed_gpus, or budget overrides are invalid."""


class InsufficientVRAMError(ColabManagerError):
    """Raised when no candidate GPU or actual allocated GPU meets the model's minimum VRAM requirement."""


class InsufficientBalanceError(ColabManagerError):
    """Raised when CU balance is below threshold or burn rate exceeds budget guardrails."""


class SessionLimitError(ColabManagerError):
    """Raised when active managed sessions reach the configured limit."""


class SessionCreationError(ColabManagerError):
    """Raised when `colab new` fails to allocate a remote GPU runtime across allowed attempts."""


class FileUploadError(ColabManagerError):
    """Raised when `colab upload` fails."""


class DependencyInstallError(ColabManagerError):
    """Raised when `colab install` fails."""


class RemoteExecutionError(ColabManagerError):
    """Raised when `colab exec` fails, times out, or raises a remote kernel exception."""


class RemoteOutputInvalidError(ColabManagerError):
    """Raised when remote MP4 validation fails prior to download."""


class FileDownloadError(ColabManagerError):
    """Raised when `colab download` fails."""


class LocalOutputInvalidError(ColabManagerError):
    """Raised when downloaded local MP4 file is missing, empty, or corrupted."""


class SessionStopError(ColabManagerError):
    """Raised when `colab stop` fails to terminate a managed session."""


class TaskTimeoutError(ColabManagerError):
    """Raised when the overall task exceeds `task_max_runtime_sec` or budget ceiling."""


class UserCancelledError(ColabManagerError):
    """Raised when execution is interrupted by user signal (SIGINT/SIGTERM)."""


# ============================================================================
# GPU 硬件目录与调度常量
# ============================================================================

SUPPORTED_SCHEDULER_GPUS: Tuple[str, ...] = ("T4", "L4", "A100", "H100")
HIGH_TIER_GPUS = {"A100", "H100"}
VALID_SCHEDULER_MODES = {"economy", "balanced", "quality"}
GPU_PROBE_JSON_PREFIX = "__AIVIDEO_GPU_PROBE_JSON__:"

GPU_HARDWARE_CATALOG: Dict[str, Dict[str, Any]] = {
    "T4": {
        "gpu_type": "T4",
        "nominal_vram_gb": 15.0,
        "usable_vram_gb": 14.5,
        "cost_tier": 1,
        "quality_rank": 1,
        "real_verified": True,
        "verification_note": "已在本项目通过真实 Google Colab Tesla T4 (14.56 GB VRAM, 实测费率 1.15 CU/hr) 端到端 SVD 1.5B 视频生成验证。",
        "requires_high_tier_auth": False,
    },
    "L4": {
        "gpu_type": "L4",
        "nominal_vram_gb": 24.0,
        "usable_vram_gb": 22.5,
        "cost_tier": 2,
        "quality_rank": 2,
        "real_verified": False,
        "verification_note": "已通过离线调度、显存匹配与回退测试；尚未在真实 Colab L4 会话中实测。",
        "requires_high_tier_auth": False,
    },
    "A100": {
        "gpu_type": "A100",
        "nominal_vram_gb": 40.0,
        "usable_vram_gb": 39.0,
        "cost_tier": 3,
        "quality_rank": 3,
        "real_verified": False,
        "verification_note": "高阶 GPU：未经显式 CLI/配置安全授权 (--authorize-high-tier-gpu) 不得申请；尚未在真实 Colab A100 会话中实测。",
        "requires_high_tier_auth": True,
    },
    "H100": {
        "gpu_type": "H100",
        "nominal_vram_gb": 80.0,
        "usable_vram_gb": 78.0,
        "cost_tier": 4,
        "quality_rank": 4,
        "real_verified": False,
        "verification_note": "高阶 GPU：未经显式 CLI/配置安全授权 (--authorize-high-tier-gpu) 不得申请；尚未在真实 Colab H100 会话中实测。",
        "requires_high_tier_auth": True,
    },
}


# ============================================================================
# 敏感凭据脱敏工具
# ============================================================================

_SENSITIVE_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"ya29\.[A-Za-z0-9_\-\.]+"), "ya29.[REDACTED_ACCESS_TOKEN]"),
    (re.compile(r"1//[A-Za-z0-9_\-\.]+"), "1//[REDACTED_REFRESH_TOKEN]"),
    (re.compile(r"4/[A-Za-z0-9_\-\.]+"), "4/[REDACTED_AUTH_CODE]"),
    (
        re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9_\-\.\~+/]+=*"),
        r"\1[REDACTED_BEARER_TOKEN]",
    ),
    (
        re.compile(
            r'(?i)((?:access_token|refresh_token|client_secret|authorization_code|id_token|private_key)\s*[:=]\s*["\']?)([^"\'\s,}]+)'
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "[REDACTED_PRIVATE_KEY_BLOCK]",
    ),
]


def redact_sensitive_text(text: str) -> str:
    """Remove OAuth2 codes, access/refresh tokens, and private keys from logs/outputs."""
    if not text:
        return ""
    sanitized = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


# ============================================================================
# 轻量级 YAML 配置加载器（支持内联列表 [T4, L4, A100, H100]，零第三方依赖回退）
# ============================================================================


def _parse_scalar(val_str: str) -> Any:
    val = val_str.strip()
    if not val:
        return ""
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in inner.split(",") if item.strip()]
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        return val[1:-1]
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in {"null", "none", "~"}:
        return None
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def load_yaml_config(config_path: Path) -> Dict[str, Any]:
    """
    Load config.yaml using PyYAML if available, or fall back to built-in standard-library parser.
    """
    if not config_path.exists():
        raise ColabManagerError(f"配置文件不存在: {config_path}")

    raw_text = config_path.read_text(encoding="utf-8")

    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw_text)
        if isinstance(loaded, dict):
            return loaded
    except ImportError:
        pass

    result: Dict[str, Any] = {}
    current_section: Optional[str] = None

    for raw_line in raw_text.splitlines():
        line_no_comment = re.sub(r"\s+#.*$", "", raw_line)
        stripped = line_no_comment.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line_no_comment) - len(line_no_comment.lstrip(" "))
        if ":" not in stripped:
            continue

        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()

        if indent == 0:
            if not rest:
                current_section = key
                result.setdefault(current_section, {})
            else:
                current_section = None
                result[key] = _parse_scalar(rest)
        else:
            if current_section is None:
                result[key] = _parse_scalar(rest)
            else:
                if not isinstance(result.get(current_section), dict):
                    result[current_section] = {}
                result[current_section][key] = _parse_scalar(rest)

    return result


# ============================================================================
# 自然语言 Prompt 解析器（完整覆盖模型/模式/白名单/预算/视频参数，严禁绕过安全锁）
# ============================================================================


def parse_task_prompt(prompt: Optional[str]) -> Dict[str, Any]:
    """
    Parse natural-language user instructions into temporary task-level overrides.
    Supports specifying:
      - Mode: economy / balanced / quality (经济模式 / 均衡模式 / 画质优先模式)
      - Scene analysis cues: 分镜复杂度 (scene_complexity), 动作控制 (motion_control), 参考图一致性 (reference_consistency)
      - Model & scale: SVD / Wan 2.2 / Wan 2.2 5B / Wan 2.2 14B (A14B)
      - Precision, quantization, offload: fp16/bf16/fp32, int8/fp8/nf4, sequential_cpu_offload/model_cpu_offload/none
      - GPU whitelist & target GPU: 只允许 T4 / 允许 T4, L4 / 自动选择 GPU
      - Budget: 预算 X CU / 最大费率 Y CU/h / 最低余额 Z CU
      - Video parameters: 帧数, 分辨率 (WxH), 帧率 (fps), 步数 (steps), 随机种子 (seed), 运动幅度 (motion_bucket_id)
    Security guarantee:
      - NEVER sets `authorize_high_tier_gpu = True` from Prompt text (prevents Prompt bypass of A100/H100 lock).
      - Records `prompt_attempted_high_tier_auth = True` if the prompt tries to self-authorize A100/H100.
    """
    if not prompt or not str(prompt).strip():
        return {}

    text = str(prompt).strip()
    low = text.lower()
    overrides: Dict[str, Any] = {"raw_prompt": text}

    # 1. 解析调度模式 (economy / balanced / quality)
    if any(
        kw in low
        for kw in (
            "画质优先模式",
            "画质优先",
            "画质模式",
            "高质量模式",
            "质量优先",
            "高画质模式",
            "quality mode",
            "mode: quality",
            "mode=quality",
        )
    ) or re.search(r"\bquality\b", low):
        overrides["mode"] = "quality"
    elif any(
        kw in low
        for kw in (
            "经济模式",
            "省钱模式",
            "低成本模式",
            "最低成本",
            "economy mode",
            "mode: economy",
            "mode=economy",
        )
    ) or re.search(r"\beconomy\b", low):
        overrides["mode"] = "economy"
    elif any(
        kw in low
        for kw in (
            "均衡模式",
            "平衡模式",
            "标准模式",
            "balanced mode",
            "mode: balanced",
            "mode=balanced",
        )
    ) or re.search(r"\bbalanced\b", low):
        overrides["mode"] = "balanced"

    # 2. 解析分镜复杂度、动作控制、参考图一致性需求（用于第一阶段模型自动选择）
    if any(
        kw in low
        for kw in (
            "复杂分镜",
            "高复杂度",
            "分镜复杂",
            "多主体",
            "复杂场景",
            "电影级光影",
            "长镜头",
            "complex scene",
        )
    ):
        overrides["scene_complexity"] = "high"
    elif any(kw in low for kw in ("简单分镜", "低复杂度", "简单场景", "静态镜头", "simple scene")):
        overrides["scene_complexity"] = "low"

    if any(
        kw in low
        for kw in (
            "大幅度动作",
            "精细动作控制",
            "动作控制强",
            "动作控制优先",
            "复杂运镜",
            "复杂运动",
            "高速运动",
            "high motion",
        )
    ):
        overrides["motion_control"] = "high"
    elif any(kw in low for kw in ("轻微运动", "微动", "低动作", "low motion")):
        overrides["motion_control"] = "low"

    if any(
        kw in low
        for kw in (
            "高参考图一致性",
            "参考图一致性优先",
            "参考图一致性高",
            "严格保持角色一致",
            "主体一致性",
            "严格一致性",
            "strict consistency",
        )
    ):
        overrides["reference_consistency"] = "strict"

    # 3. 解析视频模型与具体规格 (SVD / Wan 2.2 5B / Wan 2.2 A14B)
    if any(kw in low for kw in ("wan 2.2", "wan2.2", "wan-2.2", "万相")):
        overrides["active_model"] = "wan2.2"
        if any(kw in low for kw in ("5b", "ti2v-5b", "轻量")):
            overrides["model_id"] = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
            overrides["model_explicitly_locked"] = True
        elif any(kw in low for kw in ("14b", "a14b", "i2v-a14b", "27b", "moe")):
            overrides["model_id"] = "Wan-AI/Wan2.2-I2V-A14B-Diffusers"
            overrides["model_explicitly_locked"] = True
        else:
            # 用户指定了 Wan 2.2 家族，但未锁死 5B 还是 A14B，交由第一阶段根据复杂度/预算/白名单决策
            overrides["model_family_requested"] = "wan2.2"
    elif re.search(r"\bsvd\b", low) or "stable video diffusion" in low:
        overrides["active_model"] = "svd"
        overrides["model_id"] = "stabilityai/stable-video-diffusion-img2vid-xt"
        overrides["model_explicitly_locked"] = True

    # 4. 解析精度、量化方式与 offload 策略
    if "fp32" in low or "单精度" in low:
        overrides["variant"] = "fp32"
    elif "bf16" in low:
        overrides["variant"] = "bf16"
    elif "fp16" in low or "半精度" in low:
        overrides["variant"] = "fp16"

    if any(kw in low for kw in ("nf4", "int4", "4bit", "4-bit", "4位量化")):
        overrides["quantization"] = "nf4"
    elif any(kw in low for kw in ("fp8", "8位浮点")):
        overrides["quantization"] = "fp8"
    elif any(kw in low for kw in ("int8", "8bit", "8-bit", "8位量化", "量化")):
        overrides["quantization"] = "int8"
    elif any(kw in low for kw in ("不量化", "无量化", "no quantization")):
        overrides["quantization"] = "none"

    if any(
        kw in low
        for kw in (
            "sequential_cpu_offload",
            "sequential offload",
            "逐层卸载",
            "深度卸载",
        )
    ):
        overrides["offload_strategy"] = "sequential_cpu_offload"
    elif any(
        kw in low
        for kw in (
            "不卸载",
            "无卸载",
            "禁用卸载",
            "全显存",
            "no offload",
            "offload=none",
            "offload: none",
        )
    ):
        overrides["offload_strategy"] = "none"
    elif any(kw in low for kw in ("model_cpu_offload", "cpu offload", "模型卸载", "显存卸载")):
        overrides["offload_strategy"] = "model_cpu_offload"

    # 5. 解析预算与费率
    budget_match = re.search(
        r"(?:预算|最多|上限|budget)\s*(?:为|是|:|=)?\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:compute\s*units|cu)",
        low,
    )
    if budget_match:
        overrides["max_cu_per_task"] = float(budget_match.group(1))

    rate_match = re.search(
        r"(?:最大费率|费率上限|burn\s*rate)\s*(?:为|是|:|=)?\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:cu\s*)?/\s*h(?:r|our)?",
        low,
    )
    if rate_match:
        overrides["max_burn_rate_per_hour"] = float(rate_match.group(1))

    min_bal_match = re.search(
        r"(?:最低余额|最小余额|min\s*balance)\s*(?:为|是|:|=)?\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:compute\s*units|cu)",
        low,
    )
    if min_bal_match:
        overrides["min_cu_balance"] = float(min_bal_match.group(1))

    # 6. 解析自动选择 / GPU 白名单 / 指定 GPU
    if any(
        kw in low
        for kw in (
            "自动选择",
            "自动匹配",
            "auto gpu",
            "gpu: auto",
            "gpu=auto",
        )
    ):
        overrides["gpu"] = "auto"

    restrict_match = re.search(
        r"(?:只允许|仅允许|只用|仅限|白名单(?:为|是|:)?|允许使用\s*gpu|only\s+allow)\s*([A-Za-z0-9_,\s、/和与\-]+?)(?:[。，；;,.]|$)",
        text,
        flags=re.IGNORECASE,
    )
    if restrict_match:
        clause = restrict_match.group(1).upper()
        tokens = re.findall(r"[A-Z]+[0-9]+", clause)
        if tokens:
            overrides["allowed_gpus"] = tokens
            if len(tokens) == 1 and "gpu" not in overrides:
                overrides["gpu"] = tokens[0]

    if "allowed_gpus" not in overrides and overrides.get("gpu") != "auto":
        explicit_gpu_match = re.search(
            r"(?:指定|强制使用|使用\s*gpu|gpu\s*[:=])\s*([A-Za-z]+[0-9]+)",
            text,
            flags=re.IGNORECASE,
        )
        if explicit_gpu_match:
            overrides["gpu"] = explicit_gpu_match.group(1).upper()

    # 7. 解析回退控制与检测非法安全绕过企图
    if any(kw in low for kw in ("禁止回退", "不允许回退", "不回退", "关闭回退", "no fallback")):
        overrides["fallback_enabled"] = False
    elif any(kw in low for kw in ("允许回退", "开启回退", "自动回退")):
        overrides["fallback_enabled"] = True

    # 安全红线：严禁通过 Prompt 文本自授高阶 GPU 权限！若检测到此类字样，记录企图以便校验拦截
    if any(
        kw in low
        for kw in (
            "授权使用 a100",
            "授权申请 a100",
            "允许申请 a100",
            "授权 a100",
            "授权使用 h100",
            "授权申请 h100",
            "允许申请 h100",
            "授权 h100",
            "绕过授权",
            "authorize high tier",
        )
    ):
        overrides["prompt_attempted_high_tier_auth"] = True

    if any(kw in low for kw in ("自动购买 cu", "自动充值", "auto purchase cu")):
        overrides["auto_purchase_cu"] = True

    # 8. 解析视频参数（帧数、分辨率、帧率、步数、种子、运动幅度、解码分块）
    frames_match = re.search(r"(?:帧数\s*[:=]?\s*|frames\s*[:=]?\s*)([0-9]{1,3})|([0-9]{1,3})\s*帧", low)
    if frames_match:
        val_f = frames_match.group(1) or frames_match.group(2)
        overrides["num_frames"] = int(val_f)

    res_match = re.search(r"([0-9]{3,4})\s*[xX×]\s*([0-9]{3,4})", text)
    if res_match:
        overrides["width"] = int(res_match.group(1))
        overrides["height"] = int(res_match.group(2))

    fps_match = re.search(r"(?:帧率|fps)\s*[:=]?\s*([0-9]{1,2})|([0-9]{1,2})\s*fps\b", low)
    if fps_match:
        val_fps = fps_match.group(1) or fps_match.group(2)
        overrides["fps"] = int(val_fps)

    steps_match = re.search(r"(?:步数|采样步数|steps)\s*[:=]?\s*([0-9]{1,3})|([0-9]{1,3})\s*步", low)
    if steps_match:
        val_steps = steps_match.group(1) or steps_match.group(2)
        overrides["num_inference_steps"] = int(val_steps)

    seed_match = re.search(r"(?:随机种子|种子|seed)\s*[:=]?\s*([0-9]+)", low)
    if seed_match:
        overrides["seed"] = int(seed_match.group(1))

    motion_id_match = re.search(r"(?:运动幅度|motion_bucket_id)\s*[:=]?\s*([0-9]{1,3})", low)
    if motion_id_match:
        overrides["motion_bucket_id"] = int(motion_id_match.group(1))

    chunk_match = re.search(r"(?:解码分块|decode_chunk_size)\s*[:=]?\s*([0-9]{1,2})", low)
    if chunk_match:
        overrides["decode_chunk_size"] = int(chunk_match.group(1))

    return overrides


# ============================================================================
# 数据结构定义
# ============================================================================


@dataclass
class CommandResult:
    command: List[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_sec: float


@dataclass
class UsageInfo:
    raw_output: str
    cu_balance: Optional[float] = None
    burn_rate_per_hour: Optional[float] = None
    active_assignments: Optional[int] = None


@dataclass
class RemoteSessionInfo:
    session_id: str
    gpu_type: str = "UNKNOWN"
    status: str = "ACTIVE"
    raw_line: str = ""


@dataclass
class ManagedSessionRecord:
    session_name: str
    task_id: str
    gpu_type: str
    created_at: str
    status: str  # creating, active, stopped, orphan_needs_cleanup, create_failed
    input_path: str
    output_path: str
    remote_input_path: str
    remote_output_path: str
    updated_at: str = ""
    stop_Verified: bool = False
    last_error: str = ""


@dataclass
class GPUSchedulerPlan:
    mode: str
    requested_gpu_setting: str
    allowed_gpus: List[str]
    max_sessions: int
    fallback_enabled: bool
    max_allocation_attempts: int
    require_budget_check: bool
    auto_stop: bool
    authorize_high_tier_gpu: bool
    max_cu_per_task: float
    min_cu_balance: float
    max_burn_rate_per_hour: float
    scene_profile: Dict[str, str]
    model_selection_reason: str
    model_vram_requirement: Dict[str, Any]
    evaluated_candidates: List[Dict[str, Any]]
    compatible_gpus: List[str]
    planned_candidates: List[str]
    executable_candidates: List[str]
    selected_primary_gpu: str
    filtered_out_gpus: List[Dict[str, str]]
    blocked_high_tier_gpus: List[str]
    selection_reason: str
    prompt_overrides_applied: Dict[str, Any] = field(default_factory=dict)
    pre_allocation_disclaimer: str = (
        "Colab 官方 CLI 在创建会话前无法查询各 GPU 实时可用性与精确实时 CU/h；"
        "本计划不伪造实时报价，也不承诺分配必定成功。分配会话后将通过硬件探针与 `colab usage` 实时校验型号、显存及费率。"
    )


# ============================================================================
# CLI 输出解析工具 (兼容 google-colab-cli 0.7.5)
# ============================================================================

_AUTH_ERROR_KEYWORDS = (
    "unauthenticated",
    "invalid_grant",
    "authentication failed",
    "not authenticated",
    "credentials",
    "oauth",
    "login required",
    "token expired",
    "enter the authorization code",
    "to authorize colab-cli",
    "eof when reading a line",
    "401",
    "403",
)


def is_auth_failure(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return any(keyword in combined for keyword in _AUTH_ERROR_KEYWORDS)


def parse_usage_output(output: str) -> UsageInfo:
    """
    Parse `colab usage` output to extract CU balance, burn rate, and active assignments.
    """
    cleaned = redact_sensitive_text(output.strip())
    info = UsageInfo(raw_output=cleaned)

    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            data = json.loads(cleaned)
            for k in ("balance", "cu_balance", "compute_units", "currentBalance", "paidComputeUnitsBalance"):
                if k in data and data[k] is not None:
                    info.cu_balance = float(data[k])
                    break
            for k in ("burn_rate", "usage_rate", "rate_per_hour", "consumptionRateHourly"):
                if k in data and data[k] is not None:
                    info.burn_rate_per_hour = float(data[k])
                    break
            for k in ("active_assignments", "assignments", "assignmentsCount"):
                if k in data and data[k] is not None:
                    info.active_assignments = int(data[k])
                    break
            return info
        except (ValueError, TypeError):
            pass

    balance_patterns = [
        # 2026-11-11: 'available' and 'remaining' were removed from this
        # alternation — the CLI's update notice ('available: 0.8.0') was
        # being read as a CU balance of 0.8, falsely tripping the hard-stop
        # gate and killing a healthy G4 run. Only 'Current balance:' /
        # 'balance' / 'compute units' may introduce the balance number.
        r"(?i)(?:current\s*balance|balance|compute\s*units|cu\s*余额)[^0-9\n]*([0-9]+(?:\.[0-9]+)?)",
        r"(?i)([0-9]+(?:\.[0-9]+)?)\s*(?:compute\s*units|cu)\b",
    ]
    for pat in balance_patterns:
        m = re.search(pat, cleaned)
        if m:
            info.cu_balance = float(m.group(1))
            break

    rate_patterns = [
        r"(?i)(?:usage\s*rate|burn\s*rate|consumption\s*rate|rate)[^0-9\n]*([0-9]+(?:\.[0-9]+)?)\s*(?:cu\s*)?/\s*h(?:r|our)?",
        r"(?i)([0-9]+(?:\.[0-9]+)?)\s*(?:cu\s*)?/\s*h(?:r|our)?",
    ]
    for pat in rate_patterns:
        m = re.search(pat, cleaned)
        if m:
            info.burn_rate_per_hour = float(m.group(1))
            break

    assign_patterns = [
        r"(?i)(?:active\s*assignments|active\s*sessions|assignments)[^0-9\n]*([0-9]+)",
    ]
    for pat in assign_patterns:
        m = re.search(pat, cleaned)
        if m:
            info.active_assignments = int(m.group(1))
            break

    return info


_COLAB_075_SESSION_RE = re.compile(
    r"^\[(?P<name>[^\]]+)\]\s+(?P<endpoint>\S+)\s*\|\s*Hardware:\s*(?P<hw>[^|\s]+)"
)


def parse_sessions_output(output: str) -> List[RemoteSessionInfo]:
    """
    Parse `colab sessions` output into a list of RemoteSessionInfo objects.
    """
    cleaned = redact_sensitive_text(output.strip())
    if not cleaned:
        return []

    low = cleaned.lower()
    if any(
        phrase in low
        for phrase in (
            "no active sessions",
            "no sessions found",
            "0 active sessions",
            "无活动会话",
        )
    ):
        return []

    if cleaned.startswith("[") and cleaned.endswith("]") and not cleaned.startswith("[?]") and "|" not in cleaned.splitlines()[0]:
        try:
            items = json.loads(cleaned)
            sessions: List[RemoteSessionInfo] = []
            for item in items:
                if isinstance(item, dict):
                    sid = str(
                        item.get("name")
                        or item.get("session")
                        or item.get("id")
                        or ""
                    ).strip()
                    if sid:
                        sessions.append(
                            RemoteSessionInfo(
                                session_id=sid,
                                gpu_type=str(item.get("gpu") or item.get("hardware") or "UNKNOWN"),
                                status=str(item.get("status") or "ACTIVE"),
                                raw_line=json.dumps(item, ensure_ascii=False),
                            )
                        )
            return sessions
        except (ValueError, TypeError):
            pass

    sessions = []
    header_keywords = {
        "session",
        "session_id",
        "name",
        "id",
        "status",
        "hardware",
        "gpu",
        "backend",
        "shape",
    }
    for line in cleaned.splitlines():
        s = line.strip()
        if not s or set(s) <= {"-", "=", "+", "|", " "}:
            continue
        if s.lower().startswith(("[colab] no active", "total:", "active sessions:", "sessions:")):
            continue

        m075 = _COLAB_075_SESSION_RE.match(s)
        if m075:
            name = m075.group("name").strip()
            endpoint = m075.group("endpoint").strip()
            hw = m075.group("hw").strip().upper()
            sid = f"?:{endpoint}" if name == "?" else name
            sessions.append(
                RemoteSessionInfo(
                    session_id=sid,
                    gpu_type=hw,
                    status="ACTIVE",
                    raw_line=s,
                )
            )
            continue

        tokens = [tok.strip("|[]()") for tok in s.split() if tok.strip("|[]()")]
        if not tokens:
            continue
        if tokens[0].lower() in header_keywords and len(tokens) > 1:
            continue

        session_id = tokens[0]
        gpu_type = "CPU"
        for tok in tokens[1:]:
            up = tok.upper()
            if up in {"T4", "L4", "G4", "A100", "H100", "V100", "TPU"}:
                gpu_type = up
                break
        sessions.append(
            RemoteSessionInfo(
                session_id=session_id,
                gpu_type=gpu_type,
                status="ACTIVE",
                raw_line=s,
            )
        )
    return sessions


def extract_exec_json_result(
    stdout: str,
    stderr: str,
    prefix: str = RESULT_JSON_PREFIX,
) -> Optional[Dict[str, Any]]:
    """Extract structured JSON result emitted by remote scripts."""
    combined = f"{stdout}\n{stderr}"
    for line in reversed(combined.splitlines()):
        s = line.strip()
        if s.startswith(prefix):
            payload = s[len(prefix) :].strip()
            try:
                return json.loads(payload)
            except ValueError:
                pass
        elif prefix == RESULT_JSON_PREFIX and s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict) and ("status" in obj or "valid_mp4" in obj):
                    return obj
            except ValueError:
                pass
    return None


# ============================================================================
# 核心控制器：ColabWorkstationManager
# ============================================================================


class ColabWorkstationManager:
    """
    Orchestrates Google Colab CLI commands with two-stage model/GPU scheduling and strict CU/GPU safety guardrails.
    """

    def __init__(
        self,
        config_path: Path,
        project_root: Optional[Path] = None,
        runner: Optional[Callable[..., CommandResult]] = None,
    ) -> None:
        self.config_path = config_path.resolve()
        self.project_root = (project_root or self.config_path.parent).resolve()
        self.config = load_yaml_config(self.config_path)
        self._runner = runner or self._default_subprocess_runner

        paths_cfg = self.config.get("paths", {})
        self.input_dir = self.project_root / paths_cfg.get("input_dir", "input")
        self.output_dir = self.project_root / paths_cfg.get("output_dir", "output")
        self.logs_dir = self.project_root / paths_cfg.get("logs_dir", "logs")
        self.state_file = self.project_root / paths_cfg.get(
            "state_file", "logs/managed_state.json"
        )
        self.audit_log_file = self.project_root / paths_cfg.get(
            "audit_log_file", "logs/tasks_audit.jsonl"
        )
        self.cost_history_file = self.project_root / paths_cfg.get(
            "cost_history_file", "logs/gpu_cost_history.jsonl"
        )
        self.requirements_colab = self.project_root / paths_cfg.get(
            "requirements_colab", "requirements-colab.txt"
        )
        self.remote_script = self.project_root / paths_cfg.get(
            "remote_script", "generate_video.py"
        )

        for d in (
            self.input_dir,
            self.output_dir,
            self.logs_dir,
            self.state_file.parent,
            self.cost_history_file.parent,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------------
    # 属性读取辅助
    # ------------------------------------------------------------------------

    @property
    def cli_configured_path(self) -> str:
        env_cli = os.environ.get("COLAB_CLI_PATH", "").strip()
        if env_cli:
            return env_cli
        return str(self.config.get("colab", {}).get("cli_path", "colab"))

    @property
    def auth_mode(self) -> str:
        return str(self.config.get("colab", {}).get("auth_mode", "oauth2"))

    @property
    def gpu_type(self) -> str:
        return str(self.config.get("colab", {}).get("gpu_type", "T4"))

    @property
    def session_prefix(self) -> str:
        return str(self.config.get("colab", {}).get("session_prefix", "aivideo-svd"))

    @property
    def remote_work_dir(self) -> str:
        raw = str(self.config.get("colab", {}).get("remote_work_dir", "/content")).rstrip("/")
        return raw or "/content"

    def resolve_cli_executable(self) -> str:
        """Resolve the colab CLI executable path or raise CLIMissingError."""
        candidate = self.cli_configured_path
        if os.path.sep in candidate:
            p = Path(candidate).expanduser()
            if p.exists() and os.access(p, os.X_OK):
                return str(p)
            raise CLIMissingError(f"指定的 Colab CLI 路径不存在或不可执行: {candidate}")

        found = shutil.which(candidate)
        if found:
            return found

        fallback_local = Path.home() / ".local" / "bin" / candidate
        if fallback_local.exists() and os.access(fallback_local, os.X_OK):
            return str(fallback_local)

        raise CLIMissingError(
            f"在当前系统 PATH 及 ~/.local/bin 中未找到 '{candidate}' 可执行文件。"
            "请先在 Python 3.12+ 独立环境中安装 google-colab-cli 并完成 OAuth2 授权，"
            "或通过环境变量 COLAB_CLI_PATH 指定其绝对路径。"
        )

    # ------------------------------------------------------------------------
    # 安全 subprocess 调用封装 (禁用 shell=True，强制 stdin=DEVNULL 防止交互式挂起)
    # ------------------------------------------------------------------------

    def _default_subprocess_runner(
        self,
        cmd: Sequence[str],
        timeout_sec: float,
    ) -> CommandResult:
        cmd_list = [str(part) for part in cmd]
        start_ts = time.monotonic()
        try:
            proc = subprocess.run(
                cmd_list,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            elapsed = round(time.monotonic() - start_ts, 3)
            return CommandResult(
                command=cmd_list,
                returncode=proc.returncode,
                stdout=redact_sensitive_text(proc.stdout or ""),
                stderr=redact_sensitive_text(proc.stderr or ""),
                elapsed_sec=elapsed,
            )
        except FileNotFoundError as exc:
            raise CLIMissingError(f"无法执行命令，找不到可执行文件: {cmd_list[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            elapsed = round(time.monotonic() - start_ts, 3)
            out = redact_sensitive_text(
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            err = redact_sensitive_text(
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            raise TaskTimeoutError(
                f"CLI 命令执行超时 ({elapsed}s > {timeout_sec}s): {' '.join(cmd_list)}\n"
                f"stdout: {out}\nstderr: {err}"
            ) from exc

    def run_cli(
        self,
        subcommand_args: Sequence[str],
        timeout_sec: float,
        include_auth: bool = True,
    ) -> CommandResult:
        """Build and execute a `colab` CLI command safely as an argument list."""
        cli_bin = self.resolve_cli_executable()
        cmd: List[str] = [cli_bin]
        if include_auth:
            cmd.append(f"--auth={self.auth_mode}")
        cmd.extend(str(a) for a in subcommand_args)
        return self._runner(cmd, timeout_sec=timeout_sec)

    # ------------------------------------------------------------------------
    # 本地受管状态持久化、审计日志与三项分离的真实成本台账
    # ------------------------------------------------------------------------

    def load_managed_state(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {"managed_sessions": {}, "updated_at": ""}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "managed_sessions" in data:
                return data
        except (ValueError, OSError):
            pass
        return {"managed_sessions": {}, "updated_at": ""}

    def save_managed_state(self, state: Dict[str, Any]) -> None:
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp_file = self.state_file.with_suffix(".json.tmp")
        tmp_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_file.replace(self.state_file)

    def upsert_session_record(self, record: ManagedSessionRecord) -> None:
        state = self.load_managed_state()
        record.updated_at = datetime.now(timezone.utc).isoformat()
        state.setdefault("managed_sessions", {})[record.session_name] = asdict(record)
        self.save_managed_state(state)

    def append_audit_log(self, event: Dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        with self.audit_log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def append_cost_history(self, record: Dict[str, Any]) -> None:
        """
        Save structured cost history separating:
          1. `cu_delta_immediate`: immediate CU balance change right after `colab stop`
          2. `cu_estimated_by_burn_rate`: cost calculated from `burn_rate_cu_per_hour * (gpu_active_seconds / 3600)`
          3. `cu_confirmed_final`: confirmed final settled cost (or None if async billing lag is detected)
        """
        payload = dict(record)
        payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        with self.cost_history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def load_historical_gpu_stats(self) -> Dict[str, Dict[str, Any]]:
        """
        Read historical measured burn rates and cost data from `logs/gpu_cost_history.jsonl`.
        Rule:
          - Never use unconfirmed immediate CU deltas (e.g. `0.03 CU` with `settlement_status == "unconfirmed_async_lag"`)
            for reliable cost prediction!
          - Only use `cu_confirmed_final` or `burn_rate_cu_per_hour` / `cu_estimated_by_burn_rate`.
        """
        stats: Dict[str, Dict[str, Any]] = {}
        if not self.cost_history_file.exists():
            return stats
        try:
            for line in self.cost_history_file.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s:
                    continue
                item = json.loads(s)
                tier = str(item.get("gpu_tier") or item.get("requested_gpu") or "").upper()
                if tier not in SUPPORTED_SCHEDULER_GPUS:
                    continue
                entry = stats.setdefault(
                    tier,
                    {
                        "runs": 0,
                        "last_burn_rate_cu_per_hour": None,
                        "reliable_task_cu_estimates": [],
                        "reliable_avg_task_cu": None,
                        "unconfirmed_immediate_deltas_excluded": 0,
                        "max_peak_vram_gb": None,
                        "verified_real_gpu": False,
                    },
                )
                entry["runs"] += 1
                if item.get("burn_rate_cu_per_hour") is not None:
                    entry["last_burn_rate_cu_per_hour"] = float(item["burn_rate_cu_per_hour"])
                if item.get("peak_vram_gb") is not None:
                    prev_vram = entry["max_peak_vram_gb"] or 0.0
                    entry["max_peak_vram_gb"] = max(prev_vram, float(item["peak_vram_gb"]))
                if item.get("verified_real_gpu"):
                    entry["verified_real_gpu"] = True

                # 成本预测过滤规则：拒绝使用未确认的即时余额变化 (如 0.03 CU)
                settlement = str(item.get("settlement_status", "unconfirmed_async_lag"))
                is_reliable = bool(item.get("reliable_for_cost_prediction", False))
                confirmed_val = item.get("cu_confirmed_final")
                est_by_rate = item.get("cu_estimated_by_burn_rate")

                if settlement == "confirmed" and is_reliable and confirmed_val is not None:
                    entry["reliable_task_cu_estimates"].append(float(confirmed_val))
                else:
                    if item.get("cu_delta_immediate") is not None:
                        entry["unconfirmed_immediate_deltas_excluded"] += 1
                    if est_by_rate is not None and float(est_by_rate) > 0:
                        entry["reliable_task_cu_estimates"].append(float(est_by_rate))

            for tier_entry in stats.values():
                ests = tier_entry.pop("reliable_task_cu_estimates", [])
                if ests:
                    tier_entry["reliable_avg_task_cu"] = round(sum(ests) / len(ests), 4)
        except (ValueError, OSError):
            pass
        return stats

    def is_owned_managed_session(
        self,
        session_name: str,
        state: Optional[Dict[str, Any]] = None,
        allow_prefix_only: bool = False,
    ) -> bool:
        """
        Verify whether a remote session belongs to this project before stopping it.
        Never touch user's own unrelated sessions!
        """
        if not session_name.startswith(f"{self.session_prefix}-"):
            return False
        if allow_prefix_only:
            return True
        st = state if state is not None else self.load_managed_state()
        managed = st.get("managed_sessions", {})
        return session_name in managed

    # ------------------------------------------------------------------------
    # 第一阶段：根据分镜复杂度、动作控制、参考图一致性、模式与预算选择模型与参数
    # ------------------------------------------------------------------------

    def build_generation_params(
        self,
        input_path: str,
        output_path: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        prompt: Optional[str] = None,
        scheduler_overrides: Optional[Dict[str, Any]] = None,
    ) -> VideoGenerationParams:
        """
        Build VideoGenerationParams by combining default model config, natural language prompt,
        and explicit overrides. Also executes Stage-1 model selection when in `quality` mode
        or when `Wan 2.2` family is requested without locking a specific parameter scale.
        """
        model_cfg = copy.deepcopy(self.config.get("model", {}))
        base_sched = self.config.get("gpu_scheduler", {})
        base_safety = self.config.get("safety", {})

        prompt_parsed = parse_task_prompt(prompt)
        merged_sched = dict(prompt_parsed)
        if scheduler_overrides:
            for k, v in scheduler_overrides.items():
                if v is not None:
                    merged_sched[k] = v

        mode = str(merged_sched.get("mode", base_sched.get("mode", "balanced"))).lower().strip()
        scene_complexity = str(merged_sched.get("scene_complexity", "standard")).lower()
        motion_control = str(merged_sched.get("motion_control", "standard")).lower()
        reference_consistency = str(merged_sched.get("reference_consistency", "standard")).lower()
        budget_cu = float(
            merged_sched.get("max_cu_per_task", base_safety.get("max_cu_per_task", 5.0))
        )
        raw_allowed = merged_sched.get(
            "allowed_gpus", base_sched.get("allowed_gpus", list(SUPPORTED_SCHEDULER_GPUS))
        )
        if isinstance(raw_allowed, str):
            allowed_up = [
                x.strip().upper()
                for x in re.split(r"[,，\s]+", raw_allowed.strip("[]"))
                if x.strip()
            ]
        elif isinstance(raw_allowed, list):
            allowed_up = [str(x).strip().upper() for x in raw_allowed]
        else:
            allowed_up = list(SUPPORTED_SCHEDULER_GPUS)

        # Apply direct parameter overrides from prompt first
        param_keys = {
            "active_model",
            "model_id",
            "variant",
            "quantization",
            "offload_strategy",
            "num_frames",
            "width",
            "height",
            "fps",
            "seed",
            "num_inference_steps",
            "min_guidance_scale",
            "max_guidance_scale",
            "motion_bucket_id",
            "noise_aug_strength",
            "decode_chunk_size",
            "allow_unverified_svd",
        }
        for k in param_keys:
            if k in prompt_parsed and prompt_parsed[k] is not None:
                model_cfg[k] = prompt_parsed[k]

        if overrides:
            for k, v in overrides.items():
                if v is not None and k in param_keys:
                    model_cfg[k] = v

        explicit_model_locked = bool(
            prompt_parsed.get("model_explicitly_locked")
            or (overrides and (overrides.get("model_id") or overrides.get("active_model")))
        )
        wan_family_requested = (
            prompt_parsed.get("model_family_requested") == "wan2.2"
            or str(model_cfg.get("active_model", "svd")).lower() == "wan2.2"
        )
        only_t4_allowed = allowed_up == ["T4"]

        # ---- Stage 1: 根据分镜复杂度、动作控制、参考图一致性与预算选择具体模型 ----
        if not explicit_model_locked:
            if wan_family_requested:
                model_cfg["active_model"] = "wan2.2"
                if (
                    scene_complexity == "high"
                    and motion_control == "high"
                    and reference_consistency == "strict"
                    and not only_t4_allowed
                    and budget_cu >= 1.5
                ):
                    model_cfg["model_id"] = "Wan-AI/Wan2.2-I2V-A14B-Diffusers"
                elif mode == "quality" and not only_t4_allowed and budget_cu >= 2.0 and (
                    scene_complexity == "high" or reference_consistency == "strict"
                ):
                    model_cfg["model_id"] = "Wan-AI/Wan2.2-I2V-A14B-Diffusers"
                else:
                    # 默认选择显存友好且支持高动态的 Wan2.2-TI2V-5B
                    model_cfg["model_id"] = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
                    if only_t4_allowed and "quantization" not in prompt_parsed:
                        # 仅允许 T4 时自动启用 int8 量化或匹配 T4 14.5GB 显存
                        model_cfg.setdefault("quantization", "none")
            elif mode == "quality":
                # quality 模式下：先看分镜复杂度、动作控制和参考图一致性！
                if scene_complexity == "high" and motion_control == "high" and not only_t4_allowed and budget_cu >= 2.0:
                    model_cfg["active_model"] = "wan2.2"
                    model_cfg["model_id"] = (
                        "Wan-AI/Wan2.2-I2V-A14B-Diffusers"
                        if reference_consistency == "strict"
                        else "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
                    )
                elif (scene_complexity == "high" or motion_control == "high") and budget_cu >= 1.0:
                    model_cfg["active_model"] = "wan2.2"
                    model_cfg["model_id"] = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
                else:
                    # 标准/简单分镜或仅要求严格参考图静态一致性：保持经过真实验证的 SVD 1.5B，不盲目切大模型
                    model_cfg["active_model"] = "svd"
                    model_cfg["model_id"] = "stabilityai/stable-video-diffusion-img2vid-xt"

        if (
            str(model_cfg.get("active_model", "svd")).lower() == "wan2.2"
            and "svd" in str(model_cfg.get("model_id", "")).lower()
        ):
            model_cfg["model_id"] = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"

        in_p = Path(input_path)
        if output_path:
            out_p = Path(output_path)
        else:
            stem = in_p.stem or "video"
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_p = self.output_dir / f"{stem}_{ts}.mp4"

        return VideoGenerationParams(
            input_path=str(in_p),
            output_path=str(out_p),
            active_model=str(model_cfg.get("active_model", "svd")),
            model_id=str(
                model_cfg.get(
                    "model_id", "stabilityai/stable-video-diffusion-img2vid-xt"
                )
            ),
            variant=str(model_cfg.get("variant", "fp16")),
            quantization=str(model_cfg.get("quantization", "none")),
            offload_strategy=str(model_cfg.get("offload_strategy", "model_cpu_offload")),
            num_frames=int(model_cfg.get("num_frames", 14)),
            width=int(model_cfg.get("width", 1024)),
            height=int(model_cfg.get("height", 576)),
            fps=int(model_cfg.get("fps", 7)),
            seed=int(model_cfg.get("seed", 42)),
            num_inference_steps=int(model_cfg.get("num_inference_steps", 25)),
            min_guidance_scale=float(model_cfg.get("min_guidance_scale", 1.0)),
            max_guidance_scale=float(model_cfg.get("max_guidance_scale", 3.0)),
            motion_bucket_id=int(model_cfg.get("motion_bucket_id", 127)),
            noise_aug_strength=float(model_cfg.get("noise_aug_strength", 0.02)),
            decode_chunk_size=int(model_cfg.get("decode_chunk_size", 8)),
            allow_unverified_svd=bool(model_cfg.get("allow_unverified_svd", True)),
        )

    # ------------------------------------------------------------------------
    # 第二阶段：结合模型显存需求与 CU 预算选择 GPU（拒绝“越贵画质越好”误区）
    # ------------------------------------------------------------------------

    def resolve_gpu_schedule(
        self,
        params: VideoGenerationParams,
        prompt: Optional[str] = None,
        scheduler_overrides: Optional[Dict[str, Any]] = None,
        current_cu_balance: Optional[float] = None,
    ) -> GPUSchedulerPlan:
        """
        Validate all scheduler & budget settings, enforce security locks against Prompt bypass,
        compute fine-grained model VRAM requirements, and rank candidate GPUs.
        """
        base_sched = copy.deepcopy(self.config.get("gpu_scheduler", {}))
        base_safety = copy.deepcopy(self.config.get("safety", {}))

        cfg_max_cu_ceiling = float(base_safety.get("max_cu_per_task", 5.0))
        cfg_max_rate_ceiling = float(base_safety.get("max_burn_rate_per_hour", 5.0))
        cfg_min_bal_floor = float(base_safety.get("min_cu_balance", 5.0))
        cfg_auth_high_tier = bool(base_safety.get("authorize_high_tier_gpu", False))

        sched_cfg: Dict[str, Any] = {
            "mode": base_sched.get("mode", "balanced"),
            "gpu": base_sched.get("gpu", self.config.get("colab", {}).get("gpu_type", "auto")),
            "allowed_gpus": base_sched.get(
                "allowed_gpus", list(SUPPORTED_SCHEDULER_GPUS)
            ),
            "max_sessions": base_sched.get(
                "max_sessions", base_safety.get("max_active_managed_sessions", 1)
            ),
            "fallback_enabled": base_sched.get("fallback_enabled", True),
            "max_allocation_attempts": base_sched.get("max_allocation_attempts", 2),
            "require_budget_check": base_sched.get("require_budget_check", True),
            "auto_stop": base_sched.get("auto_stop", True),
        }

        safety_cfg: Dict[str, Any] = {
            "min_cu_balance": cfg_min_bal_floor,
            "max_cu_per_task": cfg_max_cu_ceiling,
            "max_burn_rate_per_hour": cfg_max_rate_ceiling,
            "auto_purchase_cu": base_safety.get("auto_purchase_cu", False),
            "authorize_high_tier_gpu": cfg_auth_high_tier,
        }

        prompt_parsed = parse_task_prompt(prompt)
        combined_overrides: Dict[str, Any] = {}
        combined_overrides.update(prompt_parsed)
        if scheduler_overrides:
            for k, v in scheduler_overrides.items():
                if v is not None:
                    combined_overrides[k] = v

        # ---- 安全锁防绕过检查（Prompt 绝不能绕过高阶 GPU 授权或系统预算上限） ----
        explicit_cli_high_tier_auth = bool(
            scheduler_overrides and scheduler_overrides.get("authorize_high_tier_gpu") is True
        )
        if prompt_parsed.get("prompt_attempted_high_tier_auth") and not (
            cfg_auth_high_tier or explicit_cli_high_tier_auth
        ):
            raise SafetyLockError(
                "安全锁阻断：不得通过自然语言 Prompt 绕过 A100/H100 高阶 GPU 授权限制！"
                "如需授权高阶 GPU，必须显式传入命令行安全标志 `--authorize-high-tier-gpu`。"
            )

        if "max_cu_per_task" in prompt_parsed:
            p_budget = float(prompt_parsed["max_cu_per_task"])
            if p_budget > cfg_max_cu_ceiling:
                raise SafetyLockError(
                    f"安全锁阻断：Prompt 指定的单任务预算 ({p_budget} CU) 超过系统安全上限 "
                    f"safety.max_cu_per_task ({cfg_max_cu_ceiling} CU)，不得通过 Prompt 绕过预算限制！"
                )

        if "max_burn_rate_per_hour" in prompt_parsed:
            p_rate = float(prompt_parsed["max_burn_rate_per_hour"])
            if p_rate > cfg_max_rate_ceiling:
                raise SafetyLockError(
                    f"安全锁阻断：Prompt 指定的费率上限 ({p_rate} CU/hr) 超过系统安全上限 "
                    f"safety.max_burn_rate_per_hour ({cfg_max_rate_ceiling} CU/hr)，不得通过 Prompt 绕过预算限制！"
                )

        if "min_cu_balance" in prompt_parsed:
            p_min_bal = float(prompt_parsed["min_cu_balance"])
            if p_min_bal < cfg_min_bal_floor:
                raise SafetyLockError(
                    f"安全锁阻断：Prompt 指定的最低余额警戒线 ({p_min_bal} CU) 低于系统安全下限 "
                    f"safety.min_cu_balance ({cfg_min_bal_floor} CU)，不得通过 Prompt 绕过安全限制！"
                )

        for k in (
            "mode",
            "gpu",
            "allowed_gpus",
            "max_sessions",
            "fallback_enabled",
            "max_allocation_attempts",
            "require_budget_check",
            "auto_stop",
        ):
            if k in combined_overrides and combined_overrides[k] is not None:
                sched_cfg[k] = combined_overrides[k]

        for k in (
            "min_cu_balance",
            "max_cu_per_task",
            "max_burn_rate_per_hour",
            "auto_purchase_cu",
            "authorize_high_tier_gpu",
        ):
            if k in combined_overrides and combined_overrides[k] is not None:
                safety_cfg[k] = combined_overrides[k]

        # ---- 严格参数验证 ----
        errors: List[str] = []

        mode = str(sched_cfg.get("mode", "balanced")).strip().lower()
        if mode not in VALID_SCHEDULER_MODES:
            errors.append(
                f"不支持的 GPU 调度模式 '{mode}'，仅支持: {sorted(VALID_SCHEDULER_MODES)}"
            )

        raw_allowed = sched_cfg.get("allowed_gpus", list(SUPPORTED_SCHEDULER_GPUS))
        if isinstance(raw_allowed, str):
            raw_allowed = [
                x.strip() for x in re.split(r"[,，\s]+", raw_allowed.strip("[]")) if x.strip()
            ]
        if not isinstance(raw_allowed, list) or not raw_allowed:
            errors.append("allowed_gpus 必须为非空 GPU 列表")
            allowed_gpus: List[str] = []
        else:
            allowed_gpus = []
            for g in raw_allowed:
                g_up = str(g).strip().upper()
                if g_up not in SUPPORTED_SCHEDULER_GPUS:
                    errors.append(
                        f"allowed_gpus 包含不受支持的 GPU 型号 '{g}'，仅支持预设白名单: {list(SUPPORTED_SCHEDULER_GPUS)}"
                    )
                elif g_up not in allowed_gpus:
                    allowed_gpus.append(g_up)

        req_gpu_raw = str(sched_cfg.get("gpu", "auto")).strip()
        req_gpu = "auto" if req_gpu_raw.lower() == "auto" else req_gpu_raw.upper()
        if req_gpu != "auto":
            if req_gpu not in SUPPORTED_SCHEDULER_GPUS:
                errors.append(
                    f"指定的 GPU 型号 '{req_gpu_raw}' 无效，仅支持 'auto' 或 {list(SUPPORTED_SCHEDULER_GPUS)}"
                )
            elif allowed_gpus and req_gpu not in allowed_gpus:
                errors.append(
                    f"指定的 GPU '{req_gpu}' 不在当前允许的白名单 allowed_gpus ({allowed_gpus}) 中"
                )

        try:
            max_sessions = int(sched_cfg.get("max_sessions", 1))
            if max_sessions != 1:
                errors.append(
                    f"安全红线限制 max_sessions 必须为 1（单会话限制），当前为: {max_sessions}"
                )
        except (ValueError, TypeError):
            max_sessions = 1
            errors.append("max_sessions 必须为整数 1")

        try:
            max_attempts = int(sched_cfg.get("max_allocation_attempts", 2))
            if not (1 <= max_attempts <= 4):
                errors.append(
                    f"max_allocation_attempts 必须在 1 到 4 之间（禁止无限重试），当前为: {max_attempts}"
                )
        except (ValueError, TypeError):
            max_attempts = 2
            errors.append("max_allocation_attempts 必须为整数")

        try:
            max_cu_per_task = float(safety_cfg.get("max_cu_per_task", 5.0))
            if max_cu_per_task <= 0 or max_cu_per_task > 50.0:
                errors.append(
                    f"单次任务预算 max_cu_per_task 必须在 (0, 50] CU 之间，当前为: {max_cu_per_task}"
                )
        except (ValueError, TypeError):
            max_cu_per_task = 5.0
            errors.append("max_cu_per_task 必须为正数")

        try:
            min_cu_balance = float(safety_cfg.get("min_cu_balance", 5.0))
            if min_cu_balance < 0:
                errors.append(f"min_cu_balance 不能为负数，当前为: {min_cu_balance}")
        except (ValueError, TypeError):
            min_cu_balance = 5.0
            errors.append("min_cu_balance 必须为非负数")

        try:
            max_burn_rate = float(safety_cfg.get("max_burn_rate_per_hour", 5.0))
            if max_burn_rate <= 0:
                errors.append(f"max_burn_rate_per_hour 必须大于 0，当前为: {max_burn_rate}")
        except (ValueError, TypeError):
            max_burn_rate = 5.0
            errors.append("max_burn_rate_per_hour 必须为正数")

        if bool(safety_cfg.get("auto_purchase_cu", False)):
            errors.append("安全红线阻断：严禁开启 auto_purchase_cu（禁止自动购买 CU）")

        if errors:
            raise SchedulerValidationError("; ".join(errors))

        # ---- 计算细分模型最低与推荐显存需求 ----
        vram_req = estimate_model_vram_requirement(params)
        min_vram_gb = float(vram_req["min_vram_gb"])
        rec_vram_gb = float(vram_req["recommended_vram_gb"])

        scene_profile = {
            "scene_complexity": str(combined_overrides.get("scene_complexity", "standard")),
            "motion_control": str(combined_overrides.get("motion_control", "standard")),
            "reference_consistency": str(combined_overrides.get("reference_consistency", "standard")),
        }
        model_selection_reason = (
            f"分镜复杂度={scene_profile['scene_complexity']}、动作控制={scene_profile['motion_control']}、"
            f"参考图一致性={scene_profile['reference_consistency']} -> 选定模型 '{params.model_id}' "
            f"(规模={vram_req['param_scale']}, 精度={params.variant}, 量化={params.quantization}, "
            f"卸载={params.offload_strategy}, 最低显存={min_vram_gb} GB, 推荐显存={rec_vram_gb} GB)。"
        )

        historical_stats = self.load_historical_gpu_stats()
        authorize_high_tier = bool(safety_cfg.get("authorize_high_tier_gpu", False))
        fallback_enabled = bool(sched_cfg.get("fallback_enabled", True))
        require_budget_check = bool(sched_cfg.get("require_budget_check", True))
        auto_stop = bool(sched_cfg.get("auto_stop", True))

        candidate_pool = [req_gpu] if req_gpu != "auto" else list(allowed_gpus)

        if mode == "economy" and req_gpu == "auto":
            economy_order = ["T4", "L4"]
            econ_filtered = [g for g in economy_order if g in allowed_gpus]
            candidate_pool = econ_filtered if econ_filtered else list(allowed_gpus)

        evaluated_candidates: List[Dict[str, Any]] = []
        compatible_gpus: List[str] = []
        filtered_out_gpus: List[Dict[str, str]] = []

        for g in candidate_pool:
            hw = GPU_HARDWARE_CATALOG[g]
            usable_vram = float(hw["usable_vram_gb"])
            hist = historical_stats.get(g, {})
            measured_rate = hist.get("last_burn_rate_cu_per_hour")
            reliable_task_cu = hist.get("reliable_avg_task_cu")
            meets_min_vram = usable_vram >= min_vram_gb
            meets_rec_vram = usable_vram >= rec_vram_gb
            is_high_tier = g in HIGH_TIER_GPUS

            eval_entry = {
                "gpu_type": g,
                "nominal_vram_gb": hw["nominal_vram_gb"],
                "usable_vram_gb": usable_vram,
                "cost_tier": hw["cost_tier"],
                "quality_rank": hw["quality_rank"],
                "meets_min_vram": meets_min_vram,
                "meets_recommended_vram": meets_rec_vram,
                "real_verified": bool(hw["real_verified"] or hist.get("verified_real_gpu")),
                "historical_measured_cu_per_hour": measured_rate,
                "historical_reliable_task_cu": reliable_task_cu,
                "pre_allocation_availability": "unknown_until_allocation",
                "requires_high_tier_auth": is_high_tier,
                "high_tier_authorized_for_task": authorize_high_tier if is_high_tier else True,
            }
            evaluated_candidates.append(eval_entry)

            if not meets_min_vram:
                filtered_out_gpus.append(
                    {
                        "gpu_type": g,
                        "reason": (
                            f"可用显存 ({usable_vram} GB) 低于模型 '{params.model_id}' "
                            f"({params.width}x{params.height}, {params.num_frames}帧, {params.variant}, "
                            f"quant={params.quantization}, offload={params.offload_strategy}) "
                            f"最低显存要求 ({min_vram_gb} GB)"
                        ),
                    }
                )
                continue

            if require_budget_check and measured_rate is not None:
                if measured_rate > max_burn_rate:
                    filtered_out_gpus.append(
                        {
                            "gpu_type": g,
                            "reason": (
                                f"历史实测费率 ({measured_rate} CU/hr) 超过任务最大允许费率 ({max_burn_rate} CU/hr)"
                            ),
                        }
                    )
                    continue
                if reliable_task_cu is not None and reliable_task_cu > max_cu_per_task:
                    filtered_out_gpus.append(
                        {
                            "gpu_type": g,
                            "reason": (
                                f"基于实测费率折算的单任务成本 ({reliable_task_cu} CU) 超过任务预算 ({max_cu_per_task} CU)"
                            ),
                        }
                    )
                    continue
                if current_cu_balance is not None and current_cu_balance < min_cu_balance:
                    filtered_out_gpus.append(
                        {
                            "gpu_type": g,
                            "reason": f"当前账户余额 ({current_cu_balance} CU) 低于最低余额要求 ({min_cu_balance} CU)",
                        }
                    )
                    continue

            compatible_gpus.append(g)

        if not compatible_gpus:
            reasons_str = "; ".join(
                f"{item['gpu_type']}: {item['reason']}" for item in filtered_out_gpus
            )
            raise InsufficientVRAMError(
                f"在模式 '{mode}' 及白名单 {allowed_gpus} 下，没有任何 GPU 满足模型 '{params.model_id}' "
                f"(最低显存要求 {min_vram_gb} GB) 与预算约束。淘汰详情: {reasons_str}"
            )

        # ---- 第二阶段：根据模型实际显存需求与模式排序 GPU（不盲目将更贵的 GPU 当作画质更好） ----
        if req_gpu != "auto":
            ranked_gpus = [req_gpu]
            mode_reason = (
                f"{model_selection_reason} 用户显式指定 GPU '{req_gpu}' 且通过显存校验 "
                f"({GPU_HARDWARE_CATALOG[req_gpu]['usable_vram_gb']} GB >= {min_vram_gb} GB)。"
            )
        elif mode == "economy":
            ranked_gpus = sorted(
                compatible_gpus, key=lambda x: GPU_HARDWARE_CATALOG[x]["cost_tier"]
            )
            mode_reason = (
                f"{model_selection_reason} 经济模式 (economy)：优先选择满足最低显存 ({min_vram_gb} GB) "
                f"的最低成本 GPU (排序: {ranked_gpus})。"
            )
        elif mode == "balanced":
            ranked_gpus = sorted(
                compatible_gpus,
                key=lambda x: (
                    GPU_HARDWARE_CATALOG[x]["cost_tier"],
                    0 if GPU_HARDWARE_CATALOG[x]["usable_vram_gb"] >= rec_vram_gb else 1,
                ),
            )
            mode_reason = (
                f"{model_selection_reason} 均衡模式 (balanced)：优先选择满足最低显存 ({min_vram_gb} GB) "
                f"且预计 CU 成本最低的 GPU (排序: {ranked_gpus})。"
            )
        else:
            # quality 模式修正逻辑：
            # 1. 先按分镜复杂度/动作控制/参考图一致性选定模型及推荐显存 rec_vram_gb。
            # 2. 只要某 GPU 已满足该模型的 recommended_vram_gb（即无需降低分辨率/帧数或极端卸载），
            #    继续换用更昂贵的 GPU 不会提升同模型的生成画质，因此在已满足 recommended_vram_gb 的候选中，
            #    按 cost_tier 从低到高排序（按需匹配，拒绝盲目上 A100/H100）！
            #    但对于确实需要高推荐显存的模型（如 Wan2.2-A14B rec_vram=25.95GB > L4 22.5GB），
            #    满足 rec_vram_gb 的 A100 会自然排在仅满足 min_vram_gb 的 L4 之前（或当 SVD 跑 quality 模式但用户期望充足显存余量时优先 L4 > T4）。
            def _quality_right_sized_key(g_name: str) -> Tuple[int, int, int]:
                usable = float(GPU_HARDWARE_CATALOG[g_name]["usable_vram_gb"])
                cost = int(GPU_HARDWARE_CATALOG[g_name]["cost_tier"])
                meets_rec = 0 if usable >= rec_vram_gb else 1
                # 在 quality 模式下，若多个 GPU 均满足 recommended_vram_gb，优先选择 >= 22.5GB (L4) 或满足推荐显存中成本最低者
                t4_penalty = 1 if (g_name == "T4" and len(compatible_gpus) > 1) else 0
                if meets_rec == 0:
                    return (0, t4_penalty, cost)
                # 若尚未达到 recommended_vram_gb（仅满足 min_vram_gb），优先显存更大的 GPU
                return (1, -int(usable * 10), cost)

            ranked_gpus = sorted(compatible_gpus, key=_quality_right_sized_key)
            mode_reason = (
                f"{model_selection_reason} 画质优先模式 (quality)：先定模型再按需选卡；"
                f"在满足模型推荐显存 ({rec_vram_gb} GB) 与画质余量的候选中优先选择低 CU 成本 GPU，"
                f"不盲目追求更昂贵硬件 (排序: {ranked_gpus})。"
            )

        blocked_high_tier: List[str] = []
        executable_pool: List[str] = []
        for g in ranked_gpus:
            if g in HIGH_TIER_GPUS and not authorize_high_tier:
                blocked_high_tier.append(g)
            else:
                executable_pool.append(g)

        effective_limit = max_attempts if fallback_enabled else 1
        planned_candidates = ranked_gpus[:effective_limit]
        executable_candidates = executable_pool[:effective_limit]

        selected_primary = (
            executable_candidates[0]
            if executable_candidates
            else planned_candidates[0]
        )

        if blocked_high_tier:
            mode_reason += (
                f" 注意：{blocked_high_tier} 属于高阶 GPU，未经显式安全授权 (--authorize-high-tier-gpu) "
                f"不得实际申请，当前可执行候选为 {executable_candidates}。"
            )

        return GPUSchedulerPlan(
            mode=mode,
            requested_gpu_setting=req_gpu,
            allowed_gpus=allowed_gpus,
            max_sessions=max_sessions,
            fallback_enabled=fallback_enabled,
            max_allocation_attempts=max_attempts,
            require_budget_check=require_budget_check,
            auto_stop=auto_stop,
            authorize_high_tier_gpu=authorize_high_tier,
            max_cu_per_task=max_cu_per_task,
            min_cu_balance=min_cu_balance,
            max_burn_rate_per_hour=max_burn_rate,
            scene_profile=scene_profile,
            model_selection_reason=model_selection_reason,
            model_vram_requirement=vram_req,
            evaluated_candidates=evaluated_candidates,
            compatible_gpus=ranked_gpus,
            planned_candidates=planned_candidates,
            executable_candidates=executable_candidates,
            selected_primary_gpu=selected_primary,
            filtered_out_gpus=filtered_out_gpus,
            blocked_high_tier_gpus=blocked_high_tier,
            selection_reason=mode_reason,
            prompt_overrides_applied=combined_overrides,
        )

    # ------------------------------------------------------------------------
    # 生成远程执行与硬件探针包装脚本（100% 兼容 `colab exec -f`）
    # ------------------------------------------------------------------------

    def write_remote_probe_script(self, task_id: str, attempt_idx: int = 1) -> Path:
        """
        Generate a lightweight remote Python script (`probe_<task_id>_try<attempt>.py`)
        that queries actual GPU model, total VRAM, and available free VRAM on the Colab VM.
        """
        probe_path = self.logs_dir / f"probe_{task_id}_try{attempt_idx}.py"
        probe_code = f"""#!/usr/bin/env python3
import json
import torch

if not torch.cuda.is_available():
    payload = {{
        "status": "gpu_probe",
        "cuda_available": False,
        "gpu_name": "CPU",
        "total_vram_gb": 0.0,
        "free_vram_gb": 0.0,
    }}
else:
    name = torch.cuda.get_device_name(0)
    props = torch.cuda.get_device_properties(0)
    total_gb = round(props.total_memory / (1024**3), 2)
    try:
        free_b, total_b = torch.cuda.mem_get_info(0)
        free_gb = round(free_b / (1024**3), 2)
    except Exception:
        free_gb = total_gb
    payload = {{
        "status": "gpu_probe",
        "cuda_available": True,
        "gpu_name": name,
        "cuda_version": str(torch.version.cuda),
        "total_vram_gb": total_gb,
        "free_vram_gb": free_gb,
    }}

print("{GPU_PROBE_JSON_PREFIX}" + json.dumps(payload, ensure_ascii=False), flush=True)
"""
        probe_path.write_text(probe_code, encoding="utf-8")
        return probe_path

    def write_remote_exec_scripts(
        self,
        task_id: str,
        params: VideoGenerationParams,
        remote_in: str,
        remote_out: str,
    ) -> Tuple[Path, Path]:
        """
        Generate two self-contained Python scripts in `logs/`:
        1. `exec_<task_id>.py`: injects `sys.argv` and executes `generate_video.py` source.
        2. `verify_<task_id>.py`: verifies that `remote_out` exists, is non-empty, and has a valid MP4 header.
        """
        exec_script_path = self.logs_dir / f"exec_{task_id}.py"
        verify_script_path = self.logs_dir / f"verify_{task_id}.py"

        argv_list = [
            "generate_video.py",
            "--input",
            remote_in,
            "--output",
            remote_out,
            "--active-model",
            params.active_model,
            "--model-id",
            params.model_id,
            "--variant",
            params.variant,
            "--quantization",
            params.quantization,
            "--offload-strategy",
            params.offload_strategy,
            "--num-frames",
            str(params.num_frames),
            "--width",
            str(params.width),
            "--height",
            str(params.height),
            "--fps",
            str(params.fps),
            "--seed",
            str(params.seed),
            "--num-inference-steps",
            str(params.num_inference_steps),
            "--min-guidance-scale",
            str(params.min_guidance_scale),
            "--max-guidance-scale",
            str(params.max_guidance_scale),
            "--motion-bucket-id",
            str(params.motion_bucket_id),
            "--noise-aug-strength",
            str(params.noise_aug_strength),
            "--decode-chunk-size",
            str(params.decode_chunk_size),
        ]
        if params.allow_unverified_svd:
            argv_list.append("--allow-unverified-svd")
        if params.mock_render:
            argv_list.append("--mock-render")
        if params.validate_only:
            argv_list.append("--validate-only")

        base_source = self.remote_script.read_text(encoding="utf-8")
        base_source_clean = base_source.replace(
            'if __name__ == "__main__":\n    sys.exit(main())',
            'if __name__ == "__main__":\n    _rc = main()\n    if _rc != 0:\n        raise RuntimeError(f"generate_video exited with code {_rc}")',
        )
        prelude = (
            "# Auto-generated Colab execution wrapper (no --env required)\n"
            "import sys\n"
            f"sys.argv = {argv_list!r}\n\n"
        )
        exec_script_path.write_text(prelude + base_source_clean, encoding="utf-8")

        verify_code = f"""#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
import struct

p = Path({remote_out!r})
exists = p.is_file()
size = p.stat().st_size if exists else 0
has_ftyp = False
sha256_hex = ""
if exists and size >= 32:
    with p.open("rb") as f:
        header = f.read(16)
        has_ftyp = len(header) >= 8 and header[4:8] == b"ftyp"
    sha256_hex = hashlib.sha256(p.read_bytes()).hexdigest()

valid_mp4 = bool(exists and size >= 32 and has_ftyp)
payload = {{
    "status": "verified" if valid_mp4 else "invalid_remote_mp4",
    "exists": exists,
    "size": size,
    "valid_mp4": valid_mp4,
    "sha256": sha256_hex,
}}
print("{RESULT_JSON_PREFIX}" + json.dumps(payload, ensure_ascii=False), flush=True)
if not valid_mp4:
    raise RuntimeError(f"Remote MP4 validation failed: {{payload}}")
"""
        verify_script_path.write_text(verify_code, encoding="utf-8")
        return exec_script_path, verify_script_path

    # ------------------------------------------------------------------------
    # 子命令 1: check (只读检查 CLI、账户、CU 与会话状态，不启动运行时)
    # ------------------------------------------------------------------------

    def check_environment(self) -> Dict[str, Any]:
        timeouts_cfg = self.config.get("timeouts", {})
        check_timeout = float(timeouts_cfg.get("cli_check_timeout_sec", 30))

        report: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "os": {
                "platform": sys.platform,
                "mac_ver": platform.mac_ver()[0] or platform.release(),
                "machine": platform.machine(),
            },
            "python": {
                "executable": sys.executable,
                "version": platform.python_version(),
            },
            "tools": {
                "git": shutil.which("git"),
                "uv": shutil.which("uv") or (str(Path.home() / ".local/bin/uv") if (Path.home() / ".local/bin/uv").exists() else None),
                "ffmpeg": shutil.which("ffmpeg"),
                "brew": shutil.which("brew"),
            },
            "safety_config": self.config.get("safety", {}),
            "gpu_scheduler_config": self.config.get("gpu_scheduler", {}),
            "historical_gpu_stats": self.load_historical_gpu_stats(),
            "svd_integration_status": SVD_INTEGRATION_STATUS,
            "cli_available": False,
            "cli_path": None,
            "cli_version": None,
            "auth_ok": False,
            "usage": None,
            "remote_sessions": [],
            "managed_active_sessions": [],
            "errors": [],
        }

        try:
            cli_path = self.resolve_cli_executable()
            report["cli_available"] = True
            report["cli_path"] = cli_path
        except CLIMissingError as exc:
            report["errors"].append(str(exc))
            return report

        ver_res = self.run_cli(["version"], timeout_sec=check_timeout, include_auth=False)
        if ver_res.returncode == 0:
            report["cli_version"] = (ver_res.stdout or ver_res.stderr).strip()
        else:
            report["errors"].append(
                f"获取 colab version 失败 (code={ver_res.returncode}): {ver_res.stderr.strip()}"
            )

        usage_res = self.run_cli(["usage"], timeout_sec=check_timeout, include_auth=True)
        if usage_res.returncode != 0:
            if is_auth_failure(usage_res.stdout, usage_res.stderr):
                err_msg = (
                    "Colab CLI OAuth2 尚未完成授权或令牌已过期。"
                    "请在终端运行 `/Users/skycjq/.local/bin/colab --auth=oauth2 usage` 完成浏览器授权。"
                )
                report["errors"].append(err_msg)
                return report
            report["errors"].append(
                f"查询 colab usage 失败 (code={usage_res.returncode}): {(usage_res.stderr or usage_res.stdout).strip()}"
            )
        else:
            report["auth_ok"] = True
            usage_info = parse_usage_output(usage_res.stdout)
            report["usage"] = asdict(usage_info)

        sess_res = self.run_cli(["sessions"], timeout_sec=check_timeout, include_auth=True)
        if sess_res.returncode != 0:
            if is_auth_failure(sess_res.stdout, sess_res.stderr):
                report["auth_ok"] = False
                report["errors"].append(
                    f"查询 colab sessions 认证失败: {(sess_res.stderr or sess_res.stdout).strip()}"
                )
            else:
                report["errors"].append(
                    f"查询 colab sessions 失败 (code={sess_res.returncode}): {(sess_res.stderr or sess_res.stdout).strip()}"
                )
        else:
            remote_sessions = parse_sessions_output(sess_res.stdout)
            report["remote_sessions"] = [asdict(s) for s in remote_sessions]

        state = self.load_managed_state()
        active_managed = [
            rec
            for rec in state.get("managed_sessions", {}).values()
            if rec.get("status") in {"creating", "active", "orphan_needs_cleanup"}
        ]
        report["managed_active_sessions"] = active_managed
        return report

    # ------------------------------------------------------------------------
    # 构建 11 步命令执行计划 (供 dry-run 和 generate 共用)
    # ------------------------------------------------------------------------

    def build_execution_plan(
        self,
        params: VideoGenerationParams,
        session_name: Optional[str] = None,
        task_id: Optional[str] = None,
        schedule_plan: Optional[GPUSchedulerPlan] = None,
        prompt: Optional[str] = None,
        scheduler_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        t_id = task_id or f"task-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        short_id = t_id[-6:]
        s_name = session_name or f"{self.session_prefix}-{short_id}"
        cli_bin = self.cli_configured_path
        auth_flag = f"--auth={self.auth_mode}"

        sched = schedule_plan or self.resolve_gpu_schedule(
            params=params,
            prompt=prompt,
            scheduler_overrides=scheduler_overrides,
        )
        primary_gpu = sched.selected_primary_gpu

        in_name = Path(params.input_path).name or "input.png"
        out_name = Path(params.output_path).name or "output.mp4"
        remote_in = f"/content/aivideo_in_{short_id}_{in_name}"
        remote_out = f"/content/aivideo_out_{short_id}_{out_name}"

        exec_script_path = self.logs_dir / f"exec_{t_id}.py"
        verify_script_path = self.logs_dir / f"verify_{t_id}.py"

        timeouts_cfg = self.config.get("timeouts", {})
        exec_idle_timeout = int(timeouts_cfg.get("exec_idle_timeout_sec", 600))
        task_max_runtime = int(timeouts_cfg.get("task_max_runtime_sec", 900))
        check_timeout = int(timeouts_cfg.get("cli_check_timeout_sec", 30))

        steps = [
            {
                "step": 1,
                "name": "检查 CU 余额与消耗速率",
                "command": [cli_bin, auth_flag, "usage"],
            },
            {
                "step": 2,
                "name": "检查已有远程会话与本地受管会话",
                "command": [cli_bin, auth_flag, "sessions"],
            },
            {
                "step": 3,
                "name": f"按调度策略 ({sched.mode}) 创建 {primary_gpu} 命名会话 (候选: {sched.planned_candidates})",
                "command": [
                    cli_bin,
                    auth_flag,
                    "new",
                    "-s",
                    s_name,
                    "--gpu",
                    primary_gpu,
                ],
            },
            {
                "step": 4,
                "name": "上传输入图片到远程 /content 目录",
                "command": [
                    cli_bin,
                    auth_flag,
                    "upload",
                    "-s",
                    s_name,
                    params.input_path,
                    remote_in,
                ],
            },
            {
                "step": 5,
                "name": "安装或检查云端 GPU 模型依赖",
                "command": [
                    cli_bin,
                    auth_flag,
                    "install",
                    "-s",
                    s_name,
                    "-r",
                    str(self.requirements_colab),
                ],
            },
            {
                "step": 6,
                "name": "执行远程视频生成包装脚本 (纯 -f 方式，不使用 --env)",
                "command": [
                    cli_bin,
                    auth_flag,
                    "exec",
                    "-s",
                    s_name,
                    "-f",
                    str(exec_script_path),
                    "--timeout",
                    str(exec_idle_timeout),
                ],
            },
            {
                "step": 7,
                "name": "验证远程生成的 MP4 文件完整性与 SHA256",
                "command": [
                    cli_bin,
                    auth_flag,
                    "exec",
                    "-s",
                    s_name,
                    "-f",
                    str(verify_script_path),
                    "--timeout",
                    str(check_timeout),
                ],
            },
            {
                "step": 8,
                "name": "下载远程 MP4 文件到 Mac 本地",
                "command": [
                    cli_bin,
                    auth_flag,
                    "download",
                    "-s",
                    s_name,
                    remote_out,
                    params.output_path,
                ],
            },
            {
                "step": 9,
                "name": "验证 Mac 本地 MP4 文件签名、大小与 SHA256",
                "command": ["<local_python_mp4_validator>", params.output_path],
            },
            {
                "step": 10,
                "name": "停止并释放受管 GPU 会话 (try/finally 保障)",
                "command": [cli_bin, auth_flag, "stop", "-s", s_name],
            },
            {
                "step": 11,
                "name": "核实最终会话清理结果与 CU 消耗",
                "command": [
                    f"{cli_bin} {auth_flag} sessions",
                    f"{cli_bin} {auth_flag} usage",
                ],
            },
        ]

        return {
            "mode": "dry-run",
            "task_id": t_id,
            "session_name": s_name,
            "gpu_type": primary_gpu,
            "gpu_scheduler": asdict(sched),
            "svd_integration_status": SVD_INTEGRATION_STATUS,
            "safety": {
                "dry_run_config": bool(self.config.get("safety", {}).get("dry_run", True)),
                "allow_gpu_config": bool(
                    self.config.get("safety", {}).get("allow_gpu", False)
                ),
                "min_cu_balance": sched.min_cu_balance,
                "max_cu_per_task": sched.max_cu_per_task,
                "max_burn_rate_per_hour": sched.max_burn_rate_per_hour,
                "exec_idle_timeout_sec": exec_idle_timeout,
                "task_max_runtime_sec": task_max_runtime,
            },
            "params": asdict(params),
            "remote_paths": {
                "remote_input": remote_in,
                "remote_output": remote_out,
                "local_exec_wrapper": str(exec_script_path),
                "local_verify_wrapper": str(verify_script_path),
            },
            "steps": steps,
        }

    # ------------------------------------------------------------------------
    # 子命令 2: dry-run (不申请 GPU，验证参数与两阶段调度计划并输出完整 11 步执行计划)
    # ------------------------------------------------------------------------

    def dry_run(
        self,
        input_path: str,
        output_path: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        check_input_exists: bool = True,
        prompt: Optional[str] = None,
        scheduler_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params = self.build_generation_params(
            input_path=input_path,
            output_path=output_path,
            overrides=overrides,
            prompt=prompt,
            scheduler_overrides=scheduler_overrides,
        )
        validate_generation_params(params, check_input_exists=check_input_exists)
        sched = self.resolve_gpu_schedule(
            params=params,
            prompt=prompt,
            scheduler_overrides=scheduler_overrides,
        )
        plan = self.build_execution_plan(
            params=params,
            schedule_plan=sched,
        )
        self.append_audit_log(
            {
                "action": "dry_run",
                "task_id": plan["task_id"],
                "session_name": plan["session_name"],
                "scheduler_mode": sched.mode,
                "selected_model": params.model_id,
                "candidate_gpus": sched.planned_candidates,
                "selected_primary_gpu": sched.selected_primary_gpu,
                "selection_reason": sched.selection_reason,
                "input_path": params.input_path,
                "output_path": params.output_path,
                "gpu_allocated": False,
            }
        )
        return plan

    # ------------------------------------------------------------------------
    # 子命令 3: generate (受安全锁保护的两阶段智能 GPU 调度、探针校验、回退与 11 步管线)
    # ------------------------------------------------------------------------

    def generate(
        self,
        input_path: str,
        output_path: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        execute: bool = False,
        confirm_gpu: bool = False,
        prompt: Optional[str] = None,
        scheduler_overrides: Optional[Dict[str, Any]] = None,
        authorize_task_gpu: bool = False,
    ) -> Dict[str, Any]:
        safety_cfg = self.config.get("safety", {})
        cfg_dry_run = bool(safety_cfg.get("dry_run", True))
        cfg_allow_gpu = bool(safety_cfg.get("allow_gpu", False))

        if not execute:
            plan = self.dry_run(
                input_path=input_path,
                output_path=output_path,
                overrides=overrides,
                check_input_exists=True,
                prompt=prompt,
                scheduler_overrides=scheduler_overrides,
            )
            plan["notice"] = (
                "当前处于默认 dry_run 模式，未申请任何 GPU 资源。"
                "如需在获得任务级授权后直接根据 Prompt 自动选择并运行 GPU（无需手动修改 config.yaml），"
                "请同时传入 `--execute --confirm-gpu --authorize-task-gpu`。"
            )
            return plan

        # 支持单次任务级显式授权 (--authorize-task-gpu)，无需手动编辑 config.yaml；
        # 若未传入 authorize_task_gpu，则严格检查 config.yaml 中的 allow_gpu 与 dry_run 锁。
        if not authorize_task_gpu:
            if not cfg_allow_gpu:
                raise SafetyLockError(
                    "安全锁阻断：config.yaml 中 safety.allow_gpu 当前为 false。"
                    "如需在获得单次任务授权后免编辑 config.yaml 执行，请显式传入 `--authorize-task-gpu` (或 authorize_task_gpu=True)。"
                )
            if cfg_dry_run:
                raise SafetyLockError(
                    "安全锁阻断：config.yaml 中 safety.dry_run 当前仍为 true。"
                    "如需在获得单次任务授权后免编辑 config.yaml 执行，请显式传入 `--authorize-task-gpu` (或 authorize_task_gpu=True)。"
                )

        if not confirm_gpu:
            raise SafetyLockError(
                "安全锁阻断：执行真实 GPU 任务必须同时显式传入 --confirm-gpu 确认标志。"
            )

        params = self.build_generation_params(
            input_path=input_path,
            output_path=output_path,
            overrides=overrides,
            prompt=prompt,
            scheduler_overrides=scheduler_overrides,
        )
        validate_generation_params(params, check_input_exists=True)

        sched = self.resolve_gpu_schedule(
            params=params,
            prompt=prompt,
            scheduler_overrides=scheduler_overrides,
        )

        # 检查高阶 GPU (A100 / H100) 授权锁：若所有候选均为未授权的高阶 GPU，立即阻断
        if not sched.executable_candidates:
            raise SafetyLockError(
                f"安全锁阻断：候选 GPU {sched.planned_candidates} 属于高阶 GPU (A100/H100)，"
                "未经本次任务显式安全标志授权 (--authorize-high-tier-gpu)，禁止申请！"
            )

        plan = self.build_execution_plan(params=params, schedule_plan=sched)
        task_id = plan["task_id"]
        base_session_name = plan["session_name"]
        remote_in = plan["remote_paths"]["remote_input"]
        remote_out = plan["remote_paths"]["remote_output"]
        exec_script_path, verify_script_path = self.write_remote_exec_scripts(
            task_id=task_id,
            params=params,
            remote_in=remote_in,
            remote_out=remote_out,
        )

        timeouts_cfg = self.config.get("timeouts", {})
        check_timeout = float(timeouts_cfg.get("cli_check_timeout_sec", 30))
        create_timeout = float(timeouts_cfg.get("session_create_timeout_sec", 180))
        upload_timeout = float(timeouts_cfg.get("upload_timeout_sec", 120))
        install_timeout = float(timeouts_cfg.get("install_timeout_sec", 600))
        exec_idle_timeout = int(timeouts_cfg.get("exec_idle_timeout_sec", 600))
        task_max_runtime = float(timeouts_cfg.get("task_max_runtime_sec", 900))
        download_timeout = float(timeouts_cfg.get("download_timeout_sec", 180))
        stop_timeout = float(timeouts_cfg.get("stop_timeout_sec", 60))

        min_cu_balance = sched.min_cu_balance
        max_cu_per_task = sched.max_cu_per_task
        max_burn_rate = sched.max_burn_rate_per_hour
        max_managed_sessions = sched.max_sessions
        min_req_vram_gb = float(sched.model_vram_requirement["min_vram_gb"])

        task_log_path = self.logs_dir / f"{task_id}.log"

        def _log_step(msg: str) -> None:
            ts = datetime.now(timezone.utc).isoformat()
            sanitized = redact_sensitive_text(msg)
            with task_log_path.open("a", encoding="utf-8") as lf:
                lf.write(f"[{ts}] {sanitized}\n")

        _log_step(
            f"Task {task_id} started. Mode={sched.mode}, model={params.model_id}, "
            f"candidates={sched.executable_candidates}, reason={sched.selection_reason}"
        )

        # --------------------------------------------------------------------
        # Step 1: 检查 CU 余额与消耗速率
        # --------------------------------------------------------------------
        usage_before_res = self.run_cli(["usage"], timeout_sec=check_timeout)
        if usage_before_res.returncode != 0:
            if is_auth_failure(usage_before_res.stdout, usage_before_res.stderr):
                raise AuthenticationError(
                    f"Colab OAuth2 认证失败: {(usage_before_res.stderr or usage_before_res.stdout).strip()}"
                )
            raise ColabManagerError(
                f"Step 1 检查 CU 失败 (code={usage_before_res.returncode}): "
                f"{(usage_before_res.stderr or usage_before_res.stdout).strip()}"
            )

        usage_before = parse_usage_output(usage_before_res.stdout)
        _log_step(
            f"Step 1 usage check: balance={usage_before.cu_balance}, "
            f"burn_rate={usage_before.burn_rate_per_hour}/hr"
        )

        if usage_before.cu_balance is not None:
            if usage_before.cu_balance < min_cu_balance:
                raise InsufficientBalanceError(
                    f"CU 余额不足：当前余额 {usage_before.cu_balance} CU 低于安全警戒线 {min_cu_balance} CU，已拒绝启动新任务。"
                )
            if sched.require_budget_check and usage_before.cu_balance < max_cu_per_task and usage_before.cu_balance < 0.5:
                raise InsufficientBalanceError(
                    f"CU 余额 ({usage_before.cu_balance} CU) 不足以支撑本次任务预算。"
                )

        if (
            usage_before.burn_rate_per_hour is not None
            and usage_before.burn_rate_per_hour > max_burn_rate
        ):
            raise InsufficientBalanceError(
                f"当前账户 CU 消耗速率 ({usage_before.burn_rate_per_hour} CU/hr) 超过警戒上限 ({max_burn_rate} CU/hr)，已中止新任务。"
            )

        # --------------------------------------------------------------------
        # Step 2: 检查已有会话 (保护用户原有会话，限制受管会话数量 <= 1)
        # --------------------------------------------------------------------
        sess_before_res = self.run_cli(["sessions"], timeout_sec=check_timeout)
        if sess_before_res.returncode != 0:
            if is_auth_failure(sess_before_res.stdout, sess_before_res.stderr):
                raise AuthenticationError(
                    f"查询会话时认证失败: {(sess_before_res.stderr or sess_before_res.stdout).strip()}"
                )
            raise ColabManagerError(
                f"Step 2 检查活动会话失败 (code={sess_before_res.returncode}): "
                f"{(sess_before_res.stderr or sess_before_res.stdout).strip()}"
            )

        remote_sessions_before = parse_sessions_output(sess_before_res.stdout)
        state = self.load_managed_state()
        active_owned_remote = [
            s.session_id
            for s in remote_sessions_before
            if self.is_owned_managed_session(s.session_id, state=state, allow_prefix_only=True)
        ]
        if len(active_owned_remote) >= max_managed_sessions:
            raise SessionLimitError(
                f"检测到已有 {len(active_owned_remote)} 个由本项目创建的活动会话 ({active_owned_remote})，"
                f"达到单会话安全上限 ({max_managed_sessions})。请先运行 cleanup 清理遗留会话。"
            )

        # --------------------------------------------------------------------
        # Step 3: 智能 GPU 分配、分配后实时 CU/h 与显存校验、超预算立停与有限次数回退
        # --------------------------------------------------------------------
        task_start_monotonic = time.monotonic()

        prev_sigint = signal.getsignal(signal.SIGINT)
        prev_sigterm = signal.getsignal(signal.SIGTERM)

        def _signal_handler(signum: int, _frame: Any) -> None:
            raise UserCancelledError(f"收到中断信号 (signal={signum})，正在安全中止任务并清理 GPU 会话...")

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        session_name = base_session_name
        record: Optional[ManagedSessionRecord] = None
        session_allocated = False
        session_alloc_start_ts: Optional[float] = None
        allocated_gpu_tier = sched.selected_primary_gpu
        actual_gpu_name = allocated_gpu_tier
        actual_vram_gb = float(GPU_HARDWARE_CATALOG[allocated_gpu_tier]["usable_vram_gb"])
        active_burn_rate: Optional[float] = None
        allocation_attempts_log: List[Dict[str, Any]] = []
        probe_scripts_created: List[Path] = []

        usage_after: Optional[UsageInfo] = None
        remote_sessions_after: List[RemoteSessionInfo] = []
        stop_error_to_raise: Optional[SessionStopError] = None
        primary_error: Optional[BaseException] = None
        remote_exec_meta: Optional[Dict[str, Any]] = None
        inference_elapsed_sec: float = 0.0

        try:
            last_alloc_exc: Optional[BaseException] = None
            candidates_to_try = list(sched.executable_candidates)

            for attempt_idx, candidate_gpu in enumerate(candidates_to_try, start=1):
                attempt_sess_name = base_session_name
                session_name = attempt_sess_name
                record = ManagedSessionRecord(
                    session_name=attempt_sess_name,
                    task_id=task_id,
                    gpu_type=candidate_gpu,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    status="creating",
                    input_path=params.input_path,
                    output_path=params.output_path,
                    remote_input_path=remote_in,
                    remote_output_path=remote_out,
                )
                self.upsert_session_record(record)

                _log_step(
                    f"Step 3 attempt {attempt_idx}/{len(candidates_to_try)}: requesting GPU={candidate_gpu} "
                    f"(session={attempt_sess_name})"
                )
                new_res = self.run_cli(
                    ["new", "-s", attempt_sess_name, "--gpu", candidate_gpu],
                    timeout_sec=create_timeout,
                )
                if new_res.returncode != 0:
                    err_txt = (new_res.stderr or new_res.stdout).strip()
                    record.status = "create_failed"
                    record.last_error = err_txt
                    self.upsert_session_record(record)
                    allocation_attempts_log.append(
                        {
                            "attempt": attempt_idx,
                            "session_name": attempt_sess_name,
                            "requested_gpu": candidate_gpu,
                            "status": "create_failed",
                            "error": err_txt,
                        }
                    )
                    _log_step(
                        f"Attempt {attempt_idx} ({candidate_gpu}) allocation failed: {err_txt}"
                    )
                    last_alloc_exc = SessionCreationError(
                        f"Step 3 创建 GPU ({candidate_gpu}) 会话失败 (code={new_res.returncode}): {err_txt}"
                    )
                    continue

                session_allocated = True
                session_alloc_start_ts = time.monotonic()
                record.status = "active"
                self.upsert_session_record(record)

                post_alloc_usage_res = self.run_cli(["usage"], timeout_sec=check_timeout)
                if post_alloc_usage_res.returncode == 0:
                    post_usage = parse_usage_output(post_alloc_usage_res.stdout)
                    active_burn_rate = post_usage.burn_rate_per_hour

                probe_script = self.write_remote_probe_script(task_id, attempt_idx)
                probe_scripts_created.append(probe_script)
                probe_res = self.run_cli(
                    [
                        "exec",
                        "-s",
                        attempt_sess_name,
                        "-f",
                        str(probe_script),
                        "--timeout",
                        str(int(check_timeout)),
                    ],
                    timeout_sec=check_timeout,
                )
                probe_meta = extract_exec_json_result(
                    probe_res.stdout, probe_res.stderr, prefix=GPU_PROBE_JSON_PREFIX
                )
                if probe_meta and probe_meta.get("status") == "gpu_probe":
                    actual_gpu_name = str(probe_meta.get("gpu_name") or candidate_gpu)
                    actual_vram_gb = float(
                        probe_meta.get("free_vram_gb")
                        or probe_meta.get("total_vram_gb")
                        or GPU_HARDWARE_CATALOG[candidate_gpu]["usable_vram_gb"]
                    )
                else:
                    actual_gpu_name = candidate_gpu
                    actual_vram_gb = float(
                        GPU_HARDWARE_CATALOG[candidate_gpu]["usable_vram_gb"]
                    )

                est_task_hours = 0.12
                est_task_cu = (active_burn_rate or 0.0) * est_task_hours
                over_burn_rate = (
                    sched.require_budget_check
                    and active_burn_rate is not None
                    and active_burn_rate > max_burn_rate
                )
                over_task_budget = (
                    sched.require_budget_check
                    and active_burn_rate is not None
                    and est_task_cu > max_cu_per_task
                )
                insufficient_actual_vram = actual_vram_gb < min_req_vram_gb

                if over_burn_rate or over_task_budget or insufficient_actual_vram:
                    if over_burn_rate or over_task_budget:
                        reject_status = "rejected_over_budget"
                        reject_reason = (
                            f"分配的 GPU ({actual_gpu_name}) 实时费率 {active_burn_rate} CU/hr "
                            f"(预计单任务 {est_task_cu:.2f} CU) 超出预算策略 "
                            f"(最大费率 {max_burn_rate} CU/hr, 单任务预算 {max_cu_per_task} CU)"
                        )
                        last_alloc_exc = InsufficientBalanceError(reject_reason)
                    else:
                        reject_status = "rejected_insufficient_vram"
                        reject_reason = (
                            f"分配的 GPU ({actual_gpu_name}) 实际可用显存 ({actual_vram_gb} GB) "
                            f"低于模型最低显存要求 ({min_req_vram_gb} GB)"
                        )
                        last_alloc_exc = InsufficientVRAMError(reject_reason)

                    _log_step(
                        f"Attempt {attempt_idx} ({candidate_gpu}) rejected: {reject_reason}. Stopping immediately..."
                    )
                    stop_rej_res = self.run_cli(
                        ["stop", "-s", attempt_sess_name],
                        timeout_sec=stop_timeout,
                    )
                    if stop_rej_res.returncode == 0:
                        record.status = "stopped"
                        record.stop_Verified = True
                        session_allocated = False
                    else:
                        record.status = "orphan_needs_cleanup"
                        record.stop_Verified = False
                    record.last_error = reject_reason
                    self.upsert_session_record(record)

                    allocation_attempts_log.append(
                        {
                            "attempt": attempt_idx,
                            "session_name": attempt_sess_name,
                            "requested_gpu": candidate_gpu,
                            "actual_gpu": actual_gpu_name,
                            "actual_vram_gb": actual_vram_gb,
                            "real_burn_rate_cu_per_hour": active_burn_rate,
                            "status": reject_status,
                            "reason": reject_reason,
                            "stopped_immediately": stop_rej_res.returncode == 0,
                        }
                    )
                    continue

                allocated_gpu_tier = candidate_gpu
                allocation_attempts_log.append(
                    {
                        "attempt": attempt_idx,
                        "session_name": attempt_sess_name,
                        "requested_gpu": candidate_gpu,
                        "actual_gpu": actual_gpu_name,
                        "actual_vram_gb": actual_vram_gb,
                        "real_burn_rate_cu_per_hour": active_burn_rate,
                        "status": "accepted",
                    }
                )
                _log_step(
                    f"Step 3 session '{attempt_sess_name}' accepted: GPU={actual_gpu_name}, "
                    f"VRAM={actual_vram_gb}GB, burn_rate={active_burn_rate}/hr"
                )
                break

            if not session_allocated or record is None:
                if last_alloc_exc is not None:
                    raise last_alloc_exc
                raise SessionCreationError("未能成功分配任何候选 GPU 会话。")

            def _check_budget_and_wallclock() -> float:
                elapsed = time.monotonic() - task_start_monotonic
                remaining = task_max_runtime - elapsed
                if remaining <= 0:
                    raise TaskTimeoutError(
                        f"任务已达到最大允许墙钟运行时间上限 ({task_max_runtime}s)，触发安全中止与 GPU 释放。"
                    )
                est_rate = (
                    active_burn_rate
                    if (active_burn_rate and active_burn_rate > 0)
                    else (
                        usage_before.burn_rate_per_hour
                        if (usage_before.burn_rate_per_hour and usage_before.burn_rate_per_hour > 0)
                        else 2.0
                    )
                )
                est_cu_used = (elapsed / 3600.0) * est_rate
                if est_cu_used > max_cu_per_task:
                    raise TaskTimeoutError(
                        f"任务预计消耗 ({est_cu_used:.2f} CU) 超出单任务预算警戒线 ({max_cu_per_task} CU)，触发安全中止。"
                    )
                return remaining

            # ----------------------------------------------------------------
            # Step 4: 上传输入图片
            # ----------------------------------------------------------------
            rem = _check_budget_and_wallclock()
            up_res = self.run_cli(
                ["upload", "-s", session_name, params.input_path, remote_in],
                timeout_sec=min(upload_timeout, rem),
            )
            if up_res.returncode != 0:
                raise FileUploadError(
                    f"Step 4 上传输入图片失败 (code={up_res.returncode}): "
                    f"{(up_res.stderr or up_res.stdout).strip()}"
                )
            _log_step(f"Step 4 uploaded {params.input_path} -> {remote_in}")

            # ----------------------------------------------------------------
            # Step 5: 安装或检查云端依赖
            # ----------------------------------------------------------------
            rem = _check_budget_and_wallclock()
            inst_res = self.run_cli(
                ["install", "-s", session_name, "-r", str(self.requirements_colab)],
                timeout_sec=min(install_timeout, rem),
            )
            if inst_res.returncode != 0 or "Traceback (most recent call last)" in inst_res.stderr:
                raise DependencyInstallError(
                    f"Step 5 安装云端依赖失败 (code={inst_res.returncode}): "
                    f"{(inst_res.stderr or inst_res.stdout).strip()}"
                )
            _log_step("Step 5 cloud dependencies verified/installed.")

            # ----------------------------------------------------------------
            # Step 6: 执行视频生成脚本
            # ----------------------------------------------------------------
            rem = _check_budget_and_wallclock()
            infer_start_ts = time.monotonic()
            exec_res = self.run_cli(
                [
                    "exec",
                    "-s",
                    session_name,
                    "-f",
                    str(exec_script_path),
                    "--timeout",
                    str(exec_idle_timeout),
                ],
                timeout_sec=rem,
            )
            inference_elapsed_sec = round(time.monotonic() - infer_start_ts, 2)
            remote_exec_meta = extract_exec_json_result(exec_res.stdout, exec_res.stderr)
            has_kernel_err = (
                exec_res.returncode != 0
                or (
                    remote_exec_meta is not None
                    and remote_exec_meta.get("status")
                    in {"inference_failed", "validation_failed"}
                )
                or (
                    remote_exec_meta is None
                    and "Traceback (most recent call last)" in exec_res.stderr
                )
            )
            if has_kernel_err:
                raise RemoteExecutionError(
                    f"Step 6 远程视频生成脚本执行失败 (code={exec_res.returncode}): "
                    f"{(exec_res.stderr or exec_res.stdout).strip()}"
                )
            _log_step(f"Step 6 remote script execution finished: {exec_res.stdout.strip()}")

            # ----------------------------------------------------------------
            # Step 7: 验证远程 MP4
            # ----------------------------------------------------------------
            rem = _check_budget_and_wallclock()
            ver_mp4_res = self.run_cli(
                [
                    "exec",
                    "-s",
                    session_name,
                    "-f",
                    str(verify_script_path),
                    "--timeout",
                    str(int(check_timeout)),
                ],
                timeout_sec=min(check_timeout, rem),
            )
            verify_meta = extract_exec_json_result(ver_mp4_res.stdout, ver_mp4_res.stderr)
            if (
                ver_mp4_res.returncode != 0
                or (verify_meta is not None and not verify_meta.get("valid_mp4", False))
                or "Remote MP4 validation failed" in ver_mp4_res.stderr
            ):
                raise RemoteOutputInvalidError(
                    f"Step 7 远程 MP4 文件验证未通过 (code={ver_mp4_res.returncode}): "
                    f"{(ver_mp4_res.stderr or ver_mp4_res.stdout).strip()}"
                )
            _log_step(f"Step 7 remote MP4 verified: {ver_mp4_res.stdout.strip()}")

            # ----------------------------------------------------------------
            # Step 8: 下载 MP4 到 Mac
            # ----------------------------------------------------------------
            rem = _check_budget_and_wallclock()
            Path(params.output_path).parent.mkdir(parents=True, exist_ok=True)
            dl_res = self.run_cli(
                ["download", "-s", session_name, remote_out, params.output_path],
                timeout_sec=min(download_timeout, rem),
            )
            if dl_res.returncode != 0:
                raise FileDownloadError(
                    f"Step 8 下载远程 MP4 到本地失败 (code={dl_res.returncode}): "
                    f"{(dl_res.stderr or dl_res.stdout).strip()}"
                )
            _log_step(f"Step 8 downloaded {remote_out} -> {params.output_path}")

            # ----------------------------------------------------------------
            # Step 9: 验证本地 MP4 文件
            # ----------------------------------------------------------------
            local_out_path = Path(params.output_path)
            valid_local, valid_msg = is_valid_mp4_file(local_out_path)
            if not valid_local:
                raise LocalOutputInvalidError(
                    f"Step 9 本地下载的 MP4 文件无效或已损坏: {valid_msg}"
                )
            sha256_hex = hashlib.sha256(local_out_path.read_bytes()).hexdigest()
            _log_step(f"Step 9 local MP4 verified ({valid_msg}, sha256={sha256_hex[:16]}...)")

        except BaseException as exc:
            primary_error = exc
            if record is not None:
                record.last_error = str(exc)
            _log_step(f"ERROR during pipeline: {type(exc).__name__}: {exc}")
            raise
        finally:
            signal.signal(signal.SIGINT, prev_sigint)
            signal.signal(signal.SIGTERM, prev_sigterm)

            for tmp_script in [exec_script_path, verify_script_path, *probe_scripts_created]:
                try:
                    if tmp_script.exists():
                        tmp_script.unlink()
                except OSError:
                    pass

            gpu_active_seconds = (
                round(time.monotonic() - session_alloc_start_ts, 2)
                if session_alloc_start_ts is not None
                else 0.0
            )

            # ----------------------------------------------------------------
            # Step 10: 停止 GPU 会话
            # ----------------------------------------------------------------
            if record is not None and (session_allocated or record.status == "creating"):
                try:
                    if self.is_owned_managed_session(session_name):
                        stop_res = self.run_cli(
                            ["stop", "-s", session_name],
                            timeout_sec=stop_timeout,
                        )
                        if stop_res.returncode == 0:
                            record.status = "stopped"
                            _log_step(f"Step 10 stopped managed session '{session_name}'.")
                        else:
                            record.status = "orphan_needs_cleanup"
                            err_detail = (stop_res.stderr or stop_res.stdout).strip()
                            record.last_error = (
                                f"{record.last_error}; stop_failed: {err_detail}".strip("; ")
                            )
                            _log_step(
                                f"CRITICAL: Step 10 failed to stop session '{session_name}': {err_detail}"
                            )
                            stop_error_to_raise = SessionStopError(
                                f"Step 10 停止受管 GPU 会话 '{session_name}' 失败 (code={stop_res.returncode}): {err_detail}。"
                                "会话已标记为 orphan_needs_cleanup，请立即运行 `python3 colab_manager.py cleanup`！"
                            )
                except Exception as stop_exc:  # noqa: BLE001
                    record.status = "orphan_needs_cleanup"
                    record.last_error = (
                        f"{record.last_error}; stop_exception: {stop_exc}".strip("; ")
                    )
                    _log_step(f"CRITICAL exception while stopping '{session_name}': {stop_exc}")
                    stop_error_to_raise = SessionStopError(
                        f"Step 10 停止受管会话 '{session_name}' 时发生异常: {stop_exc}"
                    )

            # ----------------------------------------------------------------
            # Step 11: 检查最终 CU 与会话状态，并分别计算三项成本指标
            # ----------------------------------------------------------------
            try:
                sess_after_res = self.run_cli(["sessions"], timeout_sec=check_timeout)
                if sess_after_res.returncode == 0:
                    remote_sessions_after = parse_sessions_output(sess_after_res.stdout)
                    still_present = any(
                        s.session_id == session_name for s in remote_sessions_after
                    )
                    if record is not None:
                        record.stop_Verified = not still_present
                        if still_present and session_allocated:
                            record.status = "orphan_needs_cleanup"
                            _log_step(
                                f"WARNING: Session '{session_name}' still present in `colab sessions` after stop!"
                            )
                            if stop_error_to_raise is None:
                                stop_error_to_raise = SessionStopError(
                                    f"Step 11 核实发现受管会话 '{session_name}' 仍在远程活动列表中，请运行 cleanup！"
                                )

                usage_after_res = self.run_cli(["usage"], timeout_sec=check_timeout)
                if usage_after_res.returncode == 0:
                    usage_after = parse_usage_output(usage_after_res.stdout)
                    _log_step(
                        f"Step 11 final usage: balance={usage_after.cu_balance}, "
                        f"burn_rate={usage_after.burn_rate_per_hour}/hr"
                    )
            except Exception as verify_exc:  # noqa: BLE001
                _log_step(f"Warning: Step 11 post-verification failed: {verify_exc}")

            if record is not None:
                self.upsert_session_record(record)

            # ---- 三项费用分离记录规则 ----
            # 1. 即时 CU 余额变化 (cu_delta_immediate)
            cu_delta_immediate: Optional[float] = None
            if (
                usage_before.cu_balance is not None
                and usage_after is not None
                and usage_after.cu_balance is not None
            ):
                cu_delta_immediate = round(
                    usage_before.cu_balance - usage_after.cu_balance, 4
                )

            # 2. 按实时费率折算的费用 (cu_estimated_by_burn_rate)
            cu_estimated_by_burn_rate: Optional[float] = None
            if active_burn_rate is not None and active_burn_rate > 0 and gpu_active_seconds > 0:
                cu_estimated_by_burn_rate = round(
                    active_burn_rate * (gpu_active_seconds / 3600.0), 4
                )

            # 3. 最终确认费用 (cu_confirmed_final) 与结算状态判定
            # 若即时余额差值显著低于按费率折算值（例如小于折算值的 75%），判定为 Colab 异步计费延迟 (unconfirmed_async_lag)
            cu_confirmed_final: Optional[float] = None
            settlement_status = "unconfirmed_async_lag"
            reliable_for_prediction = False
            if (
                cu_delta_immediate is not None
                and cu_estimated_by_burn_rate is not None
                and cu_estimated_by_burn_rate > 0
            ):
                if abs(cu_delta_immediate - cu_estimated_by_burn_rate) <= max(
                    0.02, 0.25 * cu_estimated_by_burn_rate
                ):
                    cu_confirmed_final = cu_delta_immediate
                    settlement_status = "confirmed"
                    reliable_for_prediction = True
                else:
                    cu_confirmed_final = None
                    settlement_status = "unconfirmed_async_lag"
                    reliable_for_prediction = False

            peak_vram_recorded = (
                remote_exec_meta.get("peak_vram_gb")
                if isinstance(remote_exec_meta, dict) and "peak_vram_gb" in remote_exec_meta
                else None
            )
            infer_sec_recorded = (
                float(remote_exec_meta["elapsed_seconds"])
                if isinstance(remote_exec_meta, dict) and "elapsed_seconds" in remote_exec_meta
                else inference_elapsed_sec
            )

            cost_entry = {
                "task_id": task_id,
                "session_name": session_name,
                "status": "failed" if (primary_error or stop_error_to_raise) else "completed",
                "scheduler_mode": sched.mode,
                "candidate_gpus": sched.executable_candidates,
                "requested_gpu": allocated_gpu_tier,
                "actual_gpu": (
                    remote_exec_meta.get("gpu_device", actual_gpu_name)
                    if isinstance(remote_exec_meta, dict)
                    else actual_gpu_name
                ),
                "gpu_tier": allocated_gpu_tier,
                "model": params.active_model,
                "model_id": params.model_id,
                "param_scale": sched.model_vram_requirement.get("param_scale"),
                "variant": params.variant,
                "quantization": params.quantization,
                "offload_strategy": params.offload_strategy,
                "resolution": f"{params.width}x{params.height}",
                "num_frames": params.num_frames,
                "inference_seconds": infer_sec_recorded,
                "gpu_active_seconds": gpu_active_seconds,
                "total_vram_gb": actual_vram_gb,
                "peak_vram_gb": peak_vram_recorded,
                "burn_rate_cu_per_hour": active_burn_rate,
                "cu_before": usage_before.cu_balance,
                "cu_after": usage_after.cu_balance if usage_after else None,
                "cu_delta_immediate": cu_delta_immediate,
                "cu_estimated_by_burn_rate": cu_estimated_by_burn_rate,
                "cu_confirmed_final": cu_confirmed_final,
                "settlement_status": settlement_status,
                "reliable_for_cost_prediction": reliable_for_prediction,
                "selection_reason": sched.selection_reason,
                "allocation_attempts": allocation_attempts_log,
            }
            self.append_cost_history(cost_entry)

            self.append_audit_log(
                {
                    "action": "generate",
                    "task_id": task_id,
                    "session_name": session_name,
                    "status": "failed" if (primary_error or stop_error_to_raise) else "completed",
                    "session_status": record.status if record else "not_created",
                    "stop_verified": record.stop_Verified if record else True,
                    "scheduler_mode": sched.mode,
                    "candidate_gpus": sched.executable_candidates,
                    "actual_gpu": cost_entry["actual_gpu"],
                    "peak_vram_gb": peak_vram_recorded,
                    "inference_seconds": infer_sec_recorded,
                    "selection_reason": sched.selection_reason,
                    "allocation_attempts": allocation_attempts_log,
                    "cu_before": usage_before.cu_balance,
                    "cu_after": usage_after.cu_balance if usage_after else None,
                    "cu_delta_immediate": cu_delta_immediate,
                    "cu_estimated_by_burn_rate": cu_estimated_by_burn_rate,
                    "cu_confirmed_final": cu_confirmed_final,
                    "settlement_status": settlement_status,
                    "output_path": params.output_path,
                    "remote_meta": remote_exec_meta,
                    "error": str(primary_error or stop_error_to_raise or ""),
                }
            )

            if primary_error is None and stop_error_to_raise is not None:
                raise stop_error_to_raise

        return {
            "mode": "execute",
            "status": "completed",
            "task_id": task_id,
            "session_name": session_name,
            "session_status": record.status if record else "stopped",
            "stop_verified": record.stop_Verified if record else True,
            "scheduler_mode": sched.mode,
            "selected_model": params.model_id,
            "candidate_gpus": sched.executable_candidates,
            "selected_gpu": allocated_gpu_tier,
            "actual_gpu": (
                remote_exec_meta.get("gpu_device", actual_gpu_name)
                if isinstance(remote_exec_meta, dict)
                else actual_gpu_name
            ),
            "actual_vram_gb": actual_vram_gb,
            "peak_vram_gb": (
                remote_exec_meta.get("peak_vram_gb")
                if isinstance(remote_exec_meta, dict)
                else None
            ),
            "real_burn_rate_cu_per_hour": active_burn_rate,
            "selection_reason": sched.selection_reason,
            "allocation_attempts": allocation_attempts_log,
            "output_path": params.output_path,
            "cu_before": usage_before.cu_balance,
            "cu_after": usage_after.cu_balance if usage_after else None,
            "cu_delta_immediate": cu_delta_immediate,
            "cu_estimated_by_burn_rate": cu_estimated_by_burn_rate,
            "cu_confirmed_final": cu_confirmed_final,
            "settlement_status": settlement_status,
            "remote_meta": remote_exec_meta,
            "tasks_log": str(task_log_path),
        }

    # ------------------------------------------------------------------------
    # 子命令 4: status (查看受管任务与会话状态)
    # ------------------------------------------------------------------------

    def get_status(self, query_remote: bool = False) -> Dict[str, Any]:
        state = self.load_managed_state()
        managed_map = state.get("managed_sessions", {})
        active_or_orphan = {
            k: v
            for k, v in managed_map.items()
            if v.get("status") in {"creating", "active", "orphan_needs_cleanup"}
        }

        recent_audit: List[Dict[str, Any]] = []
        if self.audit_log_file.exists():
            lines = self.audit_log_file.read_text(encoding="utf-8").splitlines()
            for line in lines[-10:]:
                try:
                    recent_audit.append(json.loads(line))
                except ValueError:
                    pass

        result: Dict[str, Any] = {
            "state_file": str(self.state_file),
            "cost_history_file": str(self.cost_history_file),
            "historical_gpu_stats": self.load_historical_gpu_stats(),
            "total_managed_records": len(managed_map),
            "unresolved_managed_sessions": active_or_orphan,
            "recent_audit_events": recent_audit,
        }

        if query_remote:
            check_timeout = float(
                self.config.get("timeouts", {}).get("cli_check_timeout_sec", 30)
            )
            sess_res = self.run_cli(["sessions"], timeout_sec=check_timeout)
            if sess_res.returncode == 0:
                result["remote_sessions"] = [
                    asdict(s) for s in parse_sessions_output(sess_res.stdout)
                ]
            else:
                result["remote_error"] = (sess_res.stderr or sess_res.stdout).strip()

        return result

    # ------------------------------------------------------------------------
    # 子命令 5: cleanup (跨异常/关机后的遗留会话核对与恢复清理)
    # ------------------------------------------------------------------------

    def cleanup_orphaned_sessions(
        self,
        include_untracked_prefix: bool = False,
    ) -> Dict[str, Any]:
        timeouts_cfg = self.config.get("timeouts", {})
        check_timeout = float(timeouts_cfg.get("cli_check_timeout_sec", 30))
        stop_timeout = float(timeouts_cfg.get("stop_timeout_sec", 60))

        state = self.load_managed_state()
        managed_map: Dict[str, Any] = state.get("managed_sessions", {})

        sess_res = self.run_cli(["sessions"], timeout_sec=check_timeout)
        if sess_res.returncode != 0:
            if is_auth_failure(sess_res.stdout, sess_res.stderr):
                raise AuthenticationError(
                    f"清理遗留会话时 OAuth2 认证失败: {(sess_res.stderr or sess_res.stdout).strip()}"
                )
            raise ColabManagerError(
                f"查询远程会话失败 (code={sess_res.returncode}): {(sess_res.stderr or sess_res.stdout).strip()}"
            )

        remote_sessions = parse_sessions_output(sess_res.stdout)
        remote_ids = {s.session_id for s in remote_sessions}

        stopped_sessions: List[str] = []
        skipped_foreign_sessions: List[str] = []
        already_gone_sessions: List[str] = []
        failed_to_stop: Dict[str, str] = {}

        for r_sess in remote_sessions:
            sid = r_sess.session_id
            if self.is_owned_managed_session(
                sid, state=state, allow_prefix_only=include_untracked_prefix
            ):
                stop_res = self.run_cli(["stop", "-s", sid], timeout_sec=stop_timeout)
                if stop_res.returncode == 0:
                    stopped_sessions.append(sid)
                    if sid in managed_map:
                        managed_map[sid]["status"] = "stopped"
                        managed_map[sid]["stop_Verified"] = True
                        managed_map[sid]["updated_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    err = (stop_res.stderr or stop_res.stdout).strip()
                    failed_to_stop[sid] = err
                    if sid in managed_map:
                        managed_map[sid]["status"] = "orphan_needs_cleanup"
                        managed_map[sid]["last_error"] = f"cleanup_stop_failed: {err}"
            else:
                skipped_foreign_sessions.append(sid)

        for sid, rec in managed_map.items():
            if rec.get("status") in {"creating", "active", "orphan_needs_cleanup"}:
                if sid not in remote_ids and sid not in failed_to_stop:
                    rec["status"] = "stopped"
                    rec["stop_Verified"] = True
                    rec["updated_at"] = datetime.now(timezone.utc).isoformat()
                    already_gone_sessions.append(sid)

        self.save_managed_state(state)

        verify_sess_res = self.run_cli(["sessions"], timeout_sec=check_timeout)
        remaining_remote = (
            [s.session_id for s in parse_sessions_output(verify_sess_res.stdout)]
            if verify_sess_res.returncode == 0
            else []
        )

        usage_res = self.run_cli(["usage"], timeout_sec=check_timeout)
        usage_after = (
            asdict(parse_usage_output(usage_res.stdout))
            if usage_res.returncode == 0
            else None
        )

        summary = {
            "stopped_managed_sessions": stopped_sessions,
            "reconciled_already_stopped": already_gone_sessions,
            "skipped_user_foreign_sessions": skipped_foreign_sessions,
            "failed_to_stop": failed_to_stop,
            "remaining_remote_sessions": remaining_remote,
            "usage_after_cleanup": usage_after,
        }
        self.append_audit_log({"action": "cleanup", **summary})

        if failed_to_stop:
            raise SessionStopError(f"部分遗留受管会话停止失败: {failed_to_stop}")

        return summary


# ============================================================================
# 命令行入口 (CLI)
# ============================================================================


def _add_common_task_args(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--input", required=True, help="本地输入图片路径")
    subparser.add_argument("--output", default=None, help="本地输出 MP4 路径")
    subparser.add_argument(
        "--prompt",
        default=None,
        help="自然语言任务指令（如：'本次使用画质优先模式，复杂分镜，预算 5 CU。'），仅作用于本次任务",
    )
    subparser.add_argument(
        "--mode",
        default=None,
        help="覆盖本次任务 GPU 调度模式: economy | balanced | quality",
    )
    subparser.add_argument(
        "--gpu",
        default=None,
        help="覆盖本次任务 GPU 选择: auto | T4 | L4 | A100 | H100",
    )
    subparser.add_argument(
        "--allowed-gpus",
        default=None,
        help="覆盖本次任务允许的 GPU 白名单，逗号分隔，如: T4,L4",
    )
    subparser.add_argument(
        "--budget-cu",
        type=float,
        default=None,
        help="覆盖本次任务 CU 预算上限 (不得超过 config.yaml 安全上限)",
    )
    subparser.add_argument(
        "--active-model",
        default=None,
        help="覆盖本次任务视频模型家族: svd | wan2.2",
    )
    subparser.add_argument(
        "--model-id",
        default=None,
        help="覆盖具体 HuggingFace 模型 ID (如 Wan-AI/Wan2.2-TI2V-5B-Diffusers 或 Wan-AI/Wan2.2-I2V-A14B-Diffusers)",
    )
    subparser.add_argument(
        "--variant",
        default=None,
        help="覆盖模型精度: fp16 | bf16 | fp32",
    )
    subparser.add_argument(
        "--quantization",
        default=None,
        help="覆盖量化方式: none | fp8 | int8 | nf4",
    )
    subparser.add_argument(
        "--offload-strategy",
        default=None,
        help="覆盖显存卸载策略: model_cpu_offload | sequential_cpu_offload | none",
    )
    subparser.add_argument(
        "--authorize-high-tier-gpu",
        action="store_true",
        default=None,
        help="显式安全授权本次任务可申请 A100 / H100 高阶 GPU（不可通过 Prompt 文本绕过）",
    )
    subparser.add_argument("--num-frames", type=int, default=None, help="覆盖帧数")
    subparser.add_argument("--width", type=int, default=None, help="覆盖宽度")
    subparser.add_argument("--height", type=int, default=None, help="覆盖高度")
    subparser.add_argument("--fps", type=int, default=None, help="覆盖帧率")
    subparser.add_argument("--seed", type=int, default=None, help="覆盖随机种子")


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AI 视频生成工作站 — Google Colab CLI (0.7.5) 编排与两阶段模型/GPU 调度控制器"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径 (默认: config.yaml)",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_check = subparsers.add_parser(
        "check", help="检查本地环境、Colab CLI、账户 CU 余额与当前会话（不消耗 CU）"
    )
    p_check.add_argument(
        "--strict",
        action="store_true",
        help="若 CLI 缺失或认证失败则返回非 0 退出码",
    )

    p_dry = subparsers.add_parser(
        "dry-run", help="验证参数、执行两阶段模型/GPU 调度规划并显示 11 步 CLI 流程（不申请 GPU）"
    )
    _add_common_task_args(p_dry)
    p_dry.add_argument(
        "--skip-file-check",
        action="store_true",
        help="仅预览命令序列与调度计划，跳过输入图片存在性检查",
    )

    p_gen = subparsers.add_parser(
        "generate", help="执行视频生成流程（默认 dry-run，实际申请 GPU 需授权与确认标志）"
    )
    _add_common_task_args(p_gen)
    p_gen.add_argument(
        "--execute",
        action="store_true",
        help="请求实际执行远程 GPU 管线",
    )
    p_gen.add_argument(
        "--confirm-gpu",
        action="store_true",
        help="明确确认已获授权申请收费 Colab GPU 资源",
    )
    p_gen.add_argument(
        "--authorize-task-gpu",
        action="store_true",
        help="单次任务级 GPU 执行授权（无需手动修改 config.yaml 中的 dry_run/allow_gpu）",
    )

    p_status = subparsers.add_parser(
        "status", help="查看由本项目管理的任务记录、历史 GPU 成本统计与会话状态"
    )
    p_status.add_argument(
        "--remote",
        action="store_true",
        help="同时调用 `colab sessions` 查询远程实时活动会话",
    )

    p_cleanup = subparsers.add_parser(
        "cleanup", help="核对会话归属并清理由本项目创建的遗留会话（保护用户原有会话）"
    )
    p_cleanup.add_argument(
        "--include-prefix",
        action="store_true",
        help="同时清理匹配本项目会话前缀 (aivideo-svd-*) 但未在本地状态文件登记的孤儿会话",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()

    try:
        manager = ColabWorkstationManager(config_path=config_path)

        if args.subcommand == "check":
            report = manager.check_environment()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if args.strict and report.get("errors"):
                return 1
            return 0

        if args.subcommand in {"dry-run", "generate"}:
            overrides = {
                "active_model": args.active_model,
                "model_id": args.model_id,
                "variant": args.variant,
                "quantization": args.quantization,
                "offload_strategy": args.offload_strategy,
                "num_frames": args.num_frames,
                "width": args.width,
                "height": args.height,
                "fps": args.fps,
                "seed": args.seed,
            }
            sched_overrides: Dict[str, Any] = {
                "mode": args.mode,
                "gpu": args.gpu,
                "allowed_gpus": args.allowed_gpus,
                "max_cu_per_task": args.budget_cu,
            }
            if args.authorize_high_tier_gpu:
                sched_overrides["authorize_high_tier_gpu"] = True

            if args.subcommand == "dry-run":
                plan = manager.dry_run(
                    input_path=args.input,
                    output_path=args.output,
                    overrides=overrides,
                    check_input_exists=not args.skip_file_check,
                    prompt=args.prompt,
                    scheduler_overrides=sched_overrides,
                )
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                return 0

            res = manager.generate(
                input_path=args.input,
                output_path=args.output,
                overrides=overrides,
                execute=args.execute,
                confirm_gpu=args.confirm_gpu,
                prompt=args.prompt,
                scheduler_overrides=sched_overrides,
                authorize_task_gpu=bool(args.authorize_task_gpu),
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if args.subcommand == "status":
            st = manager.get_status(query_remote=args.remote)
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return 0

        if args.subcommand == "cleanup":
            res = manager.cleanup_orphaned_sessions(
                include_untracked_prefix=args.include_prefix
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        return 1

    except ParameterValidationError as exc:
        print(
            json.dumps(
                {"status": "error", "error_type": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    except ColabManagerError as exc:
        print(
            json.dumps(
                {"status": "error", "error_type": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
