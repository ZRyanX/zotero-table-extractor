#!/usr/bin/env python3
"""
mineru_popo.py — opendatalab/MinerU-Popo 可选文档级后处理钩子。

设计定位：
1. 默认严格关闭 (`MINERU_POPO_ENABLED = false`)，不引入任何重依赖或冷启动开销；
2. 支持三态开关 (`false` / `"auto"` / `true`) 与跨平台硬件自动探测 (`detect_popo_hardware`)：
   - `"auto"` 模式（或 CLI `--mineru-popo auto`）：自动检测机器配置，仅当识别到 **NVIDIA CUDA**
     或 **Apple Silicon M 系列处理器 (macOS arm64 / MPS)** 时自动开启；纯 CPU 机器保持关闭；
   - `true` 模式（或 CLI `--enable-popo` / `--mineru-popo on`）：显式开启，优先使用远端/本地
     `vLLM` 服务，或在本机 CUDA / Apple Silicon 上通过 `transformers` / `native_repo` 运行；
3. 完整支持 MinerU-Popo 三大后端：
   - `vllm`       : OpenAI-Compatible / vLLM 服务端点 (`POPO_VLLM_URL` / `MINERU_POPO_VLLM_URL`)
   - `native_repo`: 本地克隆的 `opendatalab/MinerU-Popo` 仓库 (`post_processing.model_utils.popo_generate`)
   - `transformers`: 本地 4B 视觉语言模型 (`DreamEternal/MinerU-Popo` / Qwen3-VL-4B，自动选择 CUDA bfloat16 或 Apple MPS float16)
4. 聚焦本技能核心场景：
   - 表格截断分析（Table Truncation Analysis）：预测相邻跨页表对的逻辑连续性 $p_i \in \{0, 1\}$
     与列级单元格垂直合并向量 $Q_i \in \{0, 1\}^x$；
   - 跨页表题 / 脚注恢复与文档级关联（Caption / Footnote Association）；
   - 严格受本技能科学数据安全门禁保护（表号冲突、异源表冲突、数值-数值冲突硬否决），任何后端异常均优雅降级返回原表。
"""

import base64
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

try:
    from . import common
except ImportError:
    try:
        import common
    except ImportError:
        common = None


DEFAULT_POPO_MODEL_PATH = "DreamEternal/MinerU-Popo"
DEFAULT_POPO_MODEL_NAME = "Popo"
DEFAULT_POPO_TIMEOUT = 60

_HW_CACHE_LOCK = threading.Lock()
_HW_CACHE: Optional[Dict[str, Any]] = None

_LOCAL_MODEL_LOCK = threading.Lock()
_LOCAL_MODEL_CACHE: Dict[str, Tuple[Any, Any, str]] = {}


# ---------------------------------------------------------------------------
# 1. 跨平台硬件加速探测 (NVIDIA CUDA / Apple Silicon M 系列)
# ---------------------------------------------------------------------------

def _probe_apple_silicon_chip() -> Tuple[bool, str]:
    """检测 macOS 是否为 Apple Silicon (M1/M2/M3/M4 等 ARM64 芯片)。"""
    if platform.system() != "Darwin":
        return False, ""
    machine = (platform.machine() or "").lower()
    if machine not in ("arm64", "aarch64"):
        return False, ""
    chip_name = "Apple Silicon (arm64)"
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        brand = (proc.stdout or "").strip()
        if brand:
            chip_name = brand
    except Exception:
        pass
    return True, chip_name


def _probe_nvidia_smi() -> Tuple[bool, str, Optional[float]]:
    """通过轻量 `nvidia-smi` 探测 NVIDIA GPU（零 torch 导入与 Windows DLL 风险）。"""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return False, "", None
    try:
        proc = subprocess.run(
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return False, "", None
        first_line = proc.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in first_line.split(",")]
        gpu_name = parts[0] if parts else "NVIDIA CUDA GPU"
        vram_gb = None
        if len(parts) >= 2:
            try:
                vram_gb = round(float(parts[1]) / 1024.0, 2)
            except ValueError:
                vram_gb = None
        return True, gpu_name, vram_gb
    except Exception:
        return False, "", None


def _probe_torch_accelerators() -> Dict[str, Any]:
    """可选探测 PyTorch 运行时 CUDA / MPS 可用性（Windows 下预加载 onnxruntime 防 DLL 冲突）。"""
    info = {
        "torch_installed": False,
        "torch_cuda": False,
        "torch_mps": False,
        "device_name": "",
        "vram_gb": None,
    }
    if importlib.util.find_spec("torch") is None:
        return info
    try:
        if sys.platform.startswith("win") and importlib.util.find_spec("onnxruntime") is not None:
            import onnxruntime  # noqa: F401
        import torch

        info["torch_installed"] = True
        if torch.cuda.is_available():
            info["torch_cuda"] = True
            try:
                info["device_name"] = str(torch.cuda.get_device_name(0))
                props = torch.cuda.get_device_properties(0)
                info["vram_gb"] = round(float(props.total_memory) / (1024.0 ** 3), 2)
            except Exception:
                pass
        mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
        if mps_backend is not None and callable(getattr(mps_backend, "is_available", None)):
            info["torch_mps"] = bool(mps_backend.is_available())
    except Exception:
        pass
    return info


def detect_popo_hardware(force_refresh: bool = False, probe_torch: bool = False) -> Dict[str, Any]:
    """
    自动检测本机是否具备运行 MinerU-Popo 的硬件加速条件：
    - NVIDIA CUDA GPU（Linux / Windows）
    - Apple Silicon M 系列处理器（macOS arm64 / MPS）

    默认使用零 DLL 风险的轻量系统探测；仅在 `probe_torch=True` 时导入 PyTorch 校验运行时设备。
    """
    global _HW_CACHE
    with _HW_CACHE_LOCK:
        if _HW_CACHE is not None and not force_refresh and not probe_torch:
            return dict(_HW_CACHE)

    sys_name = platform.system()
    arch = platform.machine() or ""
    apple_m, apple_chip = _probe_apple_silicon_chip()
    cuda_smi, gpu_name, vram_gb = _probe_nvidia_smi()

    torch_info = _probe_torch_accelerators() if probe_torch else {
        "torch_installed": importlib.util.find_spec("torch") is not None,
        "torch_cuda": False,
        "torch_mps": False,
        "device_name": "",
        "vram_gb": None,
    }

    cuda_available = bool(cuda_smi or torch_info["torch_cuda"])
    mps_available = bool(torch_info["torch_mps"] or apple_m)
    eligible = bool(cuda_available or apple_m)

    if cuda_available:
        accelerator = "cuda"
        device = "cuda"
        device_name = torch_info["device_name"] or gpu_name or "NVIDIA CUDA GPU"
        vram_final = torch_info["vram_gb"] if torch_info["vram_gb"] is not None else vram_gb
        recommended_backend = "vllm" if sys_name == "Linux" else "transformers"
    elif apple_m:
        accelerator = "apple_silicon"
        device = "mps"
        device_name = apple_chip or "Apple Silicon"
        vram_final = None
        recommended_backend = "transformers"
    else:
        accelerator = "cpu"
        device = "cpu"
        device_name = platform.processor() or arch or "CPU"
        vram_final = None
        recommended_backend = "none"

    result = {
        "os": sys_name,
        "arch": arch,
        "cuda_available": cuda_available,
        "apple_silicon": apple_m,
        "mps_available": mps_available,
        "eligible": eligible,
        "accelerator": accelerator,
        "device": device,
        "device_name": device_name,
        "vram_gb": vram_final,
        "torch_installed": bool(torch_info["torch_installed"]),
        "transformers_installed": importlib.util.find_spec("transformers") is not None,
        "recommended_backend": recommended_backend,
    }
    if not probe_torch:
        with _HW_CACHE_LOCK:
            _HW_CACHE = dict(result)
    return result


# ---------------------------------------------------------------------------
# 2. 配置解析与三态开关决策（默认关闭 / auto 硬件感知开启 / 显式开启）
# ---------------------------------------------------------------------------

def _load_effective_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if isinstance(config, dict):
        return dict(config)
    if common is not None and callable(getattr(common, "load_config", None)):
        try:
            return common.load_config() or {}
        except Exception:
            return {}
    return {}


def resolve_popo_mode(config: Optional[Dict[str, Any]] = None) -> str:
    """
    解析 MinerU-Popo 配置模式，返回 'off' | 'auto' | 'on'：
    - 环境变量 `MINERU_POPO_ENABLED` 优先；
    - 其次读取 `config.json` 中的 `MINERU_POPO_ENABLED`（默认 False -> 'off'）。
    """
    env_raw = os.getenv("MINERU_POPO_ENABLED")
    if env_raw is not None and env_raw.strip() != "":
        val = env_raw.strip().lower()
        if val in ("auto", "hw", "hardware"):
            return "auto"
        if val in ("1", "true", "yes", "on"):
            return "on"
        return "off"

    cfg = _load_effective_config(config)
    raw = cfg.get("MINERU_POPO_ENABLED", False)
    if isinstance(raw, bool):
        return "on" if raw else "off"
    if isinstance(raw, str):
        val = raw.strip().lower()
        if val in ("auto", "hw", "hardware"):
            return "auto"
        if val in ("1", "true", "yes", "on"):
            return "on"
        return "off"
    return "on" if bool(raw) else "off"


def get_popo_vllm_url(config: Optional[Dict[str, Any]] = None) -> str:
    cfg = _load_effective_config(config)
    return (
        os.getenv("POPO_VLLM_URL")
        or os.getenv("MINERU_POPO_VLLM_URL")
        or str(cfg.get("MINERU_POPO_VLLM_URL") or "")
    ).strip()


def should_enable_mineru_popo(
    config: Optional[Dict[str, Any]] = None,
    hw_info: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    判断当前运行是否应激活 MinerU-Popo 后处理钩子，返回 `(enabled, reason)`：
    - mode == 'off'（默认）: 严格关闭；
    - mode == 'auto'       : 自动检测本机硬件，仅识别到 CUDA 或 Apple Silicon M 系列处理器时开启
                             （或用户显式配置了远端 POPO_VLLM_URL 时开启）；
    - mode == 'on'         : 显式开启；若未配置远端 vLLM 且 `MINERU_POPO_AUTO_HARDWARE=True`，
                             在无 CUDA/Apple Silicon 的纯 CPU 机器上仍做硬件保护提示并跳过本地 4B 模型。
    """
    cfg = _load_effective_config(config)
    mode = resolve_popo_mode(cfg)
    if mode == "off":
        return False, "disabled_by_default"

    vllm_url = get_popo_vllm_url(cfg)
    hw = hw_info if isinstance(hw_info, dict) else detect_popo_hardware()

    if mode == "auto":
        if hw.get("eligible"):
            return True, f"auto_hardware:{hw.get('accelerator')}"
        if vllm_url:
            return True, "auto_remote_vllm"
        return False, "auto_hardware_ineligible_cpu"

    # mode == 'on'
    if vllm_url:
        return True, "explicit_on_vllm"
    auto_hw_guard = cfg.get("MINERU_POPO_AUTO_HARDWARE", True)
    env_hw = os.getenv("MINERU_POPO_AUTO_HARDWARE")
    if env_hw is not None and env_hw.strip() != "":
        auto_hw_guard = env_hw.strip().lower() not in ("0", "false", "no", "off")
    if hw.get("eligible"):
        return True, f"explicit_on:{hw.get('accelerator')}"
    if not auto_hw_guard:
        return True, "explicit_on_cpu_forced"
    return False, "explicit_on_blocked_cpu_only"


def get_popo_readiness(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """供 `setup_env.py --check` 与诊断工具调用的 MinerU-Popo 就绪状态摘要。"""
    cfg = _load_effective_config(config)
    hw = detect_popo_hardware()
    mode = resolve_popo_mode(cfg)
    enabled, reason = should_enable_mineru_popo(cfg, hw_info=hw)
    vllm_url = get_popo_vllm_url(cfg)
    repo_path = (
        os.getenv("MINERU_POPO_REPO_PATH")
        or str(cfg.get("MINERU_POPO_REPO_PATH") or "")
    ).strip()
    model_path = (
        os.getenv("POPO_MODEL_PATH")
        or os.getenv("MINERU_POPO_MODEL_PATH")
        or str(cfg.get("MINERU_POPO_MODEL_PATH") or DEFAULT_POPO_MODEL_PATH)
    ).strip()

    backend_ready = bool(
        vllm_url
        or (repo_path and os.path.isdir(repo_path))
        or (hw["transformers_installed"] and hw["torch_installed"] and (os.path.isdir(model_path) or cfg.get("MINERU_POPO_ALLOW_DOWNLOAD", False)))
    )
    return {
        "mode": mode,
        "enabled": enabled,
        "reason": reason,
        "hardware_eligible": bool(hw["eligible"]),
        "accelerator": hw["accelerator"],
        "device": hw["device"],
        "device_name": hw["device_name"],
        "vram_gb": hw["vram_gb"],
        "vllm_configured": bool(vllm_url),
        "repo_configured": bool(repo_path and os.path.isdir(repo_path)),
        "model_path": model_path,
        "torch_installed": hw["torch_installed"],
        "transformers_installed": hw["transformers_installed"],
        "backend_ready": backend_ready,
    }


# ---------------------------------------------------------------------------
# 3. 统一 Schema 归一化（兼容 MinerU content_list 与 DataFrame 列表）
# ---------------------------------------------------------------------------

def _df_to_compact_html(df: pd.DataFrame, max_rows: int = 4, from_tail: bool = False) -> str:
    """将 DataFrame 边界行转为紧凑 HTML 表格片段，供 MinerU-Popo 模型判别。"""
    if df is None or df.empty:
        return "<table></table>"
    sub = df.tail(max_rows) if from_tail else df.head(max_rows)
    cols = [str(c) for c in sub.columns]
    lines = ["<table>", "<thead><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead>", "<tbody>"]
    for _, row in sub.iterrows():
        cells = [str(v).strip() if pd.notna(v) else "" for v in row.values]
        lines.append("<tr>" + "".join(f"<td>{v}</td>" for v in cells) + "</tr>")
    lines.append("</tbody></table>")
    return "\n".join(lines)


def tables_to_popo_content_list(dfs: List[pd.DataFrame]) -> List[Dict[str, Any]]:
    """将提取的 DataFrame 列表归一化为 MinerU-Popo 兼容的 `content_list` 结构块。"""
    blocks: List[Dict[str, Any]] = []
    for idx, df in enumerate(dfs or []):
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            continue
        attrs = getattr(df, "attrs", None) or {}
        caption = attrs.get("table_title") or attrs.get("label") or ""
        footnotes = attrs.get("footnotes") or attrs.get("table_footnote") or []
        if isinstance(footnotes, str):
            footnotes = [footnotes] if footnotes.strip() else []
        blocks.append({
            "index": idx,
            "type": "table",
            "page_idx": attrs.get("page_idx"),
            "bbox": attrs.get("bbox"),
            "label": attrs.get("label") or "",
            "table_caption": [caption] if caption else [],
            "table_footnote": list(footnotes),
            "table_body": _df_to_compact_html(df, max_rows=6, from_tail=False),
            "tail_preview": _df_to_compact_html(df, max_rows=3, from_tail=True),
            "head_preview": _df_to_compact_html(df, max_rows=3, from_tail=False),
            "num_rows": int(df.shape[0]),
            "num_cols": int(df.shape[1]),
            "columns": [str(c) for c in df.columns],
        })
    return blocks


def _render_table_boundary_image_b64(pdf_path: Optional[str], page_idx: Optional[int], bbox: Optional[List[float]]) -> Optional[str]:
    """可选裁剪 PDF 表格边界图像（Base64 PNG）以支持 MinerU-Popo 多模态视觉判别。"""
    if not pdf_path or page_idx is None or not os.path.isfile(pdf_path):
        return None
    try:
        import fitz

        with fitz.open(pdf_path) as doc:
            if page_idx < 0 or page_idx >= len(doc):
                return None
            page = doc[page_idx]
            clip = None
            if bbox and len(bbox) >= 4:
                x0, y0, x1, y1 = [float(v) for v in bbox[:4]]
                pw, ph = page.rect.width, page.rect.height
                if max(x1, y1) > max(pw, ph) * 1.05 or (max(x1, y1) <= 1000.0 and pw < 900):
                    # 0-1000 归一化坐标转 PDF 点坐标
                    if max(x1, y1) <= 1000.0 and (x1 > pw or y1 > ph):
                        x0, x1 = x0 * pw / 1000.0, x1 * pw / 1000.0
                        y0, y1 = y0 * ph / 1000.0, y1 * ph / 1000.0
                clip = fitz.Rect(max(0.0, x0), max(0.0, y0), min(pw, x1), min(ph, y1))
                if clip.is_empty or clip.width < 10 or clip.height < 10:
                    clip = None
            pix = page.get_pixmap(dpi=120, clip=clip)
            return base64.b64encode(pix.tobytes("png")).decode("ascii")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 4. MinerU-Popo 三后端推理客户端 (vLLM / native_repo / transformers)
# ---------------------------------------------------------------------------

_POPO_TABLE_TRUNCATION_SYSTEM_PROMPT = (
    "You are MinerU-Popo, a universal structured document post-processing assistant specialized in "
    "Table Truncation Analysis and Caption/Footnote Association across page boundaries.\n"
    "Given two consecutive extracted tables (prev_table on page N, next_table on page N+1), determine:\n"
    "1. is_continuation (0 or 1): whether next_table is a cross-page continuation of prev_table.\n"
    "2. cell_merge_vector: array of 0/1 of length min(prev_cols, next_cols) indicating for each column j "
    "whether the last row of prev_table and first row of next_table are vertically split parts of one cell.\n"
    "   NEVER set cell_merge_vector[j]=1 when both cells contain independent numeric measurements.\n"
    "3. recovered_caption: optional missing caption/label string if associated, else empty string.\n"
    "4. recovered_footnotes: optional list of footnote strings belonging to the merged table.\n"
    "Respond ONLY with valid JSON: "
    '{"is_continuation": 0|1, "cell_merge_vector": [0, ...], "recovered_caption": "", "recovered_footnotes": [], "confidence": 0.0}'
)


def _parse_popo_json_response(raw_text: str, n_cols: int) -> Optional[Dict[str, Any]]:
    """容错解析 MinerU-Popo 返回的 JSON 决策对象。"""
    if not raw_text or not isinstance(raw_text, str):
        return None
    cleaned = raw_text.strip()
    m_fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if m_fence:
        cleaned = m_fence.group(1)
    else:
        m_brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m_brace:
            cleaned = m_brace.group(0)
    try:
        obj = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None

    is_cont_raw = obj.get("is_continuation", obj.get("p_i", obj.get("merge", 0)))
    is_cont = bool(int(is_cont_raw)) if isinstance(is_cont_raw, (int, float, str)) and str(is_cont_raw).strip().isdigit() else bool(is_cont_raw)

    raw_vec = obj.get("cell_merge_vector", obj.get("Q_i", obj.get("q_vec", [])))
    vec: List[int] = []
    if isinstance(raw_vec, list):
        for v in raw_vec[:n_cols]:
            vec.append(1 if v in (1, True, "1", "true", "True") else 0)
    while len(vec) < n_cols:
        vec.append(0)

    try:
        conf = float(obj.get("confidence", 0.85))
    except (TypeError, ValueError):
        conf = 0.85

    rec_cap = str(obj.get("recovered_caption") or "").strip()
    rec_fn = obj.get("recovered_footnotes") or []
    if isinstance(rec_fn, str):
        rec_fn = [rec_fn.strip()] if rec_fn.strip() else []
    elif not isinstance(rec_fn, list):
        rec_fn = []

    return {
        "is_continuation": is_cont,
        "cell_merge_vector": vec,
        "recovered_caption": rec_cap,
        "recovered_footnotes": [str(x).strip() for x in rec_fn if str(x).strip()],
        "confidence": conf,
    }


class MinerUPopoClient:
    """
    MinerU-Popo 统一推理客户端，按优先级自动路由：
    1. `vllm`       : 显式配置 POPO_VLLM_URL / MINERU_POPO_VLLM_URL 时走 OpenAI-Compatible API；
    2. `native_repo`: 显式配置 MINERU_POPO_REPO_PATH 时直接调用官方 `post_processing.model_utils.popo_generate`；
    3. `transformers`: 在 CUDA / Apple Silicon M 系列硬件上通过本地 HuggingFace Transformers 加载 4B 权重。
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, hw_info: Optional[Dict[str, Any]] = None):
        self.config = _load_effective_config(config)
        self.hw = hw_info if isinstance(hw_info, dict) else detect_popo_hardware()
        self.backend = (
            os.getenv("POPO_INFERENCE_BACKEND")
            or os.getenv("MINERU_POPO_BACKEND")
            or str(self.config.get("MINERU_POPO_BACKEND") or "auto")
        ).strip().lower()
        self.vllm_url = get_popo_vllm_url(self.config).rstrip("/")
        self.vllm_api_key = (
            os.getenv("POPO_VLLM_API_KEY")
            or os.getenv("MINERU_POPO_VLLM_API_KEY")
            or str(self.config.get("MINERU_POPO_VLLM_API_KEY") or "")
        ).strip()
        self.model_name = (
            os.getenv("POPO_MODEL_NAME")
            or os.getenv("MINERU_POPO_MODEL_NAME")
            or str(self.config.get("MINERU_POPO_MODEL_NAME") or DEFAULT_POPO_MODEL_NAME)
        ).strip()
        self.model_path = (
            os.getenv("POPO_MODEL_PATH")
            or os.getenv("MINERU_POPO_MODEL_PATH")
            or str(self.config.get("MINERU_POPO_MODEL_PATH") or DEFAULT_POPO_MODEL_PATH)
        ).strip()
        self.repo_path = (
            os.getenv("MINERU_POPO_REPO_PATH")
            or str(self.config.get("MINERU_POPO_REPO_PATH") or "")
        ).strip()
        try:
            self.timeout = int(
                os.getenv("MINERU_POPO_TIMEOUT")
                or self.config.get("MINERU_POPO_TIMEOUT")
                or DEFAULT_POPO_TIMEOUT
            )
        except (TypeError, ValueError):
            self.timeout = DEFAULT_POPO_TIMEOUT
        env_dl = os.getenv("MINERU_POPO_ALLOW_DOWNLOAD")
        if env_dl is not None and env_dl.strip() != "":
            self.allow_download = env_dl.strip().lower() in ("1", "true", "yes", "on")
        else:
            self.allow_download = bool(self.config.get("MINERU_POPO_ALLOW_DOWNLOAD", False))

    def resolve_active_backend(self) -> Optional[str]:
        """确定当前可用的实际推理后端名称。"""
        if self.backend in ("vllm", "openai"):
            return "vllm" if self.vllm_url else None
        if self.backend == "native_repo":
            return "native_repo" if (self.repo_path and os.path.isdir(self.repo_path)) else None
        if self.backend == "transformers":
            return "transformers" if self._can_use_local_transformers() else None

        # auto 路由优先级: vLLM 服务端点 > 官方本地仓库 > 本地 transformers (CUDA/MPS)
        if self.vllm_url:
            return "vllm"
        if self.repo_path and os.path.isdir(self.repo_path):
            return "native_repo"
        if self._can_use_local_transformers():
            return "transformers"
        return None

    def _can_use_local_transformers(self) -> bool:
        if importlib.util.find_spec("transformers") is None or importlib.util.find_spec("torch") is None:
            return False
        if os.path.isdir(self.model_path):
            return True
        return bool(self.allow_download)

    def _build_pair_prompt_text(self, prev_block: Dict[str, Any], next_block: Dict[str, Any]) -> str:
        payload = {
            "prev_table": {
                "page_idx": prev_block.get("page_idx"),
                "label": prev_block.get("label"),
                "caption": prev_block.get("table_caption"),
                "bbox": prev_block.get("bbox"),
                "num_rows": prev_block.get("num_rows"),
                "num_cols": prev_block.get("num_cols"),
                "columns": prev_block.get("columns"),
                "tail_html": prev_block.get("tail_preview"),
            },
            "next_table": {
                "page_idx": next_block.get("page_idx"),
                "label": next_block.get("label"),
                "caption": next_block.get("table_caption"),
                "bbox": next_block.get("bbox"),
                "num_rows": next_block.get("num_rows"),
                "num_cols": next_block.get("num_cols"),
                "columns": next_block.get("columns"),
                "head_html": next_block.get("head_preview"),
                "footnotes": next_block.get("table_footnote"),
            },
        }
        return (
            "Analyze whether next_table is a cross-page continuation of prev_table and predict "
            "the column-wise boundary cell merge vector Q_i:\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    def _infer_via_vllm(
        self,
        prompt_text: str,
        images_b64: Optional[List[str]] = None,
    ) -> Optional[str]:
        if not self.vllm_url:
            return None
        endpoint = self.vllm_url
        if not endpoint.endswith("/chat/completions"):
            if endpoint.endswith("/v1"):
                endpoint = f"{endpoint}/chat/completions"
            else:
                endpoint = f"{endpoint}/v1/chat/completions"

        headers = {"Content-Type": "application/json"}
        if self.vllm_api_key:
            headers["Authorization"] = f"Bearer {self.vllm_api_key}"

        if images_b64:
            user_content: Any = [{"type": "text", "text": prompt_text}]
            for b64 in images_b64:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                })
        else:
            user_content = prompt_text

        body = {
            "model": self.model_name,
            "temperature": 0.0,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": _POPO_TABLE_TRUNCATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        try:
            resp = requests.post(endpoint, headers=headers, json=body, timeout=self.timeout)
            if resp.status_code != 200:
                print(f"[MinerU-Popo] vLLM 端点返回 HTTP {resp.status_code}，回退本地规则。")
                return None
            data = resp.json()
            choices = data.get("choices") or []
            if choices:
                return str((choices[0].get("message") or {}).get("content") or "").strip()
        except Exception as exc:
            print(f"[MinerU-Popo] vLLM 推理请求异常，优雅降级: {exc}")
        return None

    def _infer_via_native_repo(self, prompt_text: str) -> Optional[str]:
        """调用本地克隆的 opendatalab/MinerU-Popo (`post_processing.model_utils.popo_generate`)。"""
        if not self.repo_path or not os.path.isdir(self.repo_path):
            return None
        added_path = False
        try:
            if self.repo_path not in sys.path:
                sys.path.insert(0, self.repo_path)
                added_path = True
            os.environ.setdefault("POPO_MODEL_PATH", self.model_path)
            if self.vllm_url:
                os.environ.setdefault("POPO_INFERENCE_BACKEND", "vllm")
                os.environ.setdefault("POPO_VLLM_URL", self.vllm_url)
            else:
                os.environ.setdefault("POPO_INFERENCE_BACKEND", "transformers")
            from post_processing.model_utils import popo_generate  # type: ignore

            full_prompt = f"{_POPO_TABLE_TRUNCATION_SYSTEM_PROMPT}\n\n{prompt_text}"
            out = popo_generate(full_prompt)
            return str(out).strip() if out is not None else None
        except Exception as exc:
            print(f"[MinerU-Popo] 本地仓库 post_processing 调用失败，优雅降级: {exc}")
            return None
        finally:
            if added_path and self.repo_path in sys.path:
                try:
                    sys.path.remove(self.repo_path)
                except ValueError:
                    pass

    def _load_local_transformers(self) -> Tuple[Any, Any, str]:
        cache_key = f"{self.model_path}|{self.hw.get('device', 'cpu')}"
        with _LOCAL_MODEL_LOCK:
            if cache_key in _LOCAL_MODEL_CACHE:
                return _LOCAL_MODEL_CACHE[cache_key]

            if sys.platform.startswith("win") and importlib.util.find_spec("onnxruntime") is not None:
                import onnxruntime  # noqa: F401
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor, AutoTokenizer

            hw_live = detect_popo_hardware(probe_torch=True)
            if hw_live["cuda_available"]:
                device = "cuda"
                dtype = torch.bfloat16
            elif hw_live["apple_silicon"] and hw_live["mps_available"]:
                device = "mps"
                dtype = torch.float16
            else:
                device = "cpu"
                dtype = torch.float32

            local_only = not self.allow_download
            try:
                processor_or_tok = AutoProcessor.from_pretrained(
                    self.model_path, local_files_only=local_only, trust_remote_code=True
                )
            except Exception:
                processor_or_tok = AutoTokenizer.from_pretrained(
                    self.model_path, local_files_only=local_only, trust_remote_code=True
                )

            try:
                from transformers import AutoModelForImageTextToText
                model_cls = AutoModelForImageTextToText
            except Exception:
                model_cls = AutoModelForCausalLM

            load_kwargs: Dict[str, Any] = {
                "torch_dtype": dtype,
                "local_files_only": local_only,
                "trust_remote_code": True,
            }
            if device == "cuda":
                load_kwargs["device_map"] = "auto"

            model = model_cls.from_pretrained(self.model_path, **load_kwargs)
            if device != "cuda":
                model = model.to(device)
            model.eval()
            _LOCAL_MODEL_CACHE[cache_key] = (processor_or_tok, model, device)
            return processor_or_tok, model, device

    def _infer_via_transformers(self, prompt_text: str) -> Optional[str]:
        if not self._can_use_local_transformers():
            return None
        try:
            import torch

            processor_or_tok, model, device = self._load_local_transformers()
            tokenizer = getattr(processor_or_tok, "tokenizer", processor_or_tok)
            messages = [
                {"role": "system", "content": _POPO_TABLE_TRUNCATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt_text},
            ]
            if hasattr(tokenizer, "apply_chat_template"):
                rendered = tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
            else:
                rendered = f"{_POPO_TABLE_TRUNCATION_SYSTEM_PROMPT}\n\n{prompt_text}\n"
            inputs = tokenizer(rendered, return_tensors="pt")
            inputs = {k: v.to(device) for k, v in inputs.items()}
            with torch.no_grad():
                out_ids = model.generate(**inputs, max_new_tokens=256, do_sample=False)
            gen_ids = out_ids[0][inputs["input_ids"].shape[1]:]
            return tokenizer.decode(gen_ids, skip_special_tokens=True).strip()
        except Exception as exc:
            print(f"[MinerU-Popo] 本地 transformers ({self.hw.get('device')}) 推理失败，优雅降级: {exc}")
            return None

    def analyze_table_pair(
        self,
        prev_df: pd.DataFrame,
        next_df: pd.DataFrame,
        pdf_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """对相邻跨页表对执行 MinerU-Popo 表格截断分析，返回结构化决策字典或 None。"""
        active = self.resolve_active_backend()
        if not active:
            return None

        blocks = tables_to_popo_content_list([prev_df, next_df])
        if len(blocks) < 2:
            return None
        prompt_text = self._build_pair_prompt_text(blocks[0], blocks[1])

        raw_out = None
        if active == "vllm":
            images: List[str] = []
            if pdf_path:
                for b in blocks:
                    b64 = _render_table_boundary_image_b64(pdf_path, b.get("page_idx"), b.get("bbox"))
                    if b64:
                        images.append(b64)
            raw_out = self._infer_via_vllm(prompt_text, images_b64=images or None)
        elif active == "native_repo":
            raw_out = self._infer_via_native_repo(prompt_text)
        elif active == "transformers":
            raw_out = self._infer_via_transformers(prompt_text)

        n_cols = min(prev_df.shape[1], next_df.shape[1])
        return _parse_popo_json_response(raw_out or "", n_cols=n_cols)


# ---------------------------------------------------------------------------
# 5. 安全后处理钩子：将 MinerU-Popo 决策无损接入表格管线
# ---------------------------------------------------------------------------

def _has_hard_table_label_conflict(df1: pd.DataFrame, df2: pd.DataFrame) -> bool:
    """严防 MinerU-Popo 将显式不同表号（如 Table 1 与 Table 2）误合并。"""
    def _extract_id(df: pd.DataFrame) -> Optional[str]:
        attrs = getattr(df, "attrs", None) or {}
        for key in ("label", "table_title"):
            val = str(attrs.get(key) or "").strip()
            if not val:
                continue
            m = re.search(
                r"(?:supplementary\s+table|extended\s+data\s+table|appendix\s+table|table|tab\.|附表|附录表|补充表|表)\s*([A-Za-z]?\d+(?:[.\-]\d+)*|[ivxlcdm]+)",
                val,
                re.IGNORECASE,
            )
            if m:
                prefix = "supp_" if re.search(r"supp|附|补", val[:m.start() + 15], re.IGNORECASE) else ""
                return f"{prefix}{m.group(1).lower()}"
        return None

    id1, id2 = _extract_id(df1), _extract_id(df2)
    return bool(id1 and id2 and id1 != id2)


def _is_candidate_cross_page_pair(df1: pd.DataFrame, df2: pd.DataFrame) -> bool:
    """
    动态候选对过滤（对应 MinerU-Popo 启发式预筛）：
    仅对相邻跨页 (1 <= page_dist <= 2)、无显式表号冲突、来源一致且列数接近的残片调用 4B 模型。
    """
    if df1 is None or df2 is None or df1.empty or df2.empty:
        return False
    if _has_hard_table_label_conflict(df1, df2):
        return False
    a1 = getattr(df1, "attrs", None) or {}
    a2 = getattr(df2, "attrs", None) or {}

    src1 = str(a1.get("source") or a1.get("extractor") or "").lower()
    src2 = str(a2.get("source") or a2.get("extractor") or "").lower()
    is_online_1 = any(k in src1 for k in ("online", "jats", "arxiv", "html"))
    is_online_2 = any(k in src2 for k in ("online", "jats", "arxiv", "html"))
    if is_online_1 != is_online_2:
        return False

    p1 = a1.get("page_idx")
    p2 = a2.get("page_idx")
    if p1 is not None and p2 is not None:
        dist = p2 - p1
        if dist < 1 or dist > 2:
            return False
    elif (p1 is None) != (p2 is None):
        return False

    if abs(df1.shape[1] - df2.shape[1]) > 3:
        return False
    return True


def _apply_verified_cell_merge_vector(
    prev_df: pd.DataFrame,
    next_df: pd.DataFrame,
    q_vec: List[int],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    在科学数据数值安全门禁下应用 MinerU-Popo 预测的列级合并向量 Q_i：
    任意列存在独立数值冲突时强制将 q_j 置 0，保护原始科学数据绝不被拼接破坏。
    """
    if not q_vec or not any(q_vec) or prev_df.empty or next_df.empty:
        return prev_df, next_df

    try:
        from .table_postprocess import is_pure_data_value, _join_split_cell_text
    except ImportError:
        from table_postprocess import is_pure_data_value, _join_split_cell_text

    n_cols = min(prev_df.shape[1], next_df.shape[1], len(q_vec))
    last_row = [str(x).strip() if pd.notna(x) else "" for x in prev_df.iloc[-1].values]
    first_row = [str(x).strip() if pd.notna(x) else "" for x in next_df.iloc[0].values]

    safe_q = list(q_vec[:n_cols])
    for j in range(n_cols):
        if safe_q[j] and last_row[j] and first_row[j]:
            if is_pure_data_value(last_row[j]) and is_pure_data_value(first_row[j]):
                # 数值冲突硬否决整行折叠
                return prev_df, next_df

    # 若下一页首行存在未被授权合并的非空列，则不可丢弃整行（避免数据丢失）
    for j in range(min(len(first_row), n_cols)):
        if first_row[j] and not safe_q[j]:
            return prev_df, next_df

    merged_last = list(last_row)
    for j in range(n_cols):
        if not safe_q[j] or not first_row[j]:
            continue
        if j == 0 and merged_last[j] and merged_last[j] == first_row[j]:
            continue
        if merged_last[j]:
            if is_pure_data_value(merged_last[j]) or is_pure_data_value(first_row[j]):
                return prev_df, next_df
            merged_last[j] = _join_split_cell_text(merged_last[j], first_row[j])
        else:
            merged_last[j] = first_row[j]

    prev_copy = prev_df.copy()
    prev_copy.iloc[-1] = merged_last
    next_copy = next_df.iloc[1:].reset_index(drop=True)
    return prev_copy, next_copy


def run_popo_postprocess_hook(
    dfs: List[pd.DataFrame],
    pdf_path: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    client: Optional[MinerUPopoClient] = None,
    hw_info: Optional[Dict[str, Any]] = None,
) -> List[pd.DataFrame]:
    """
    MinerU-Popo 可选后处理主钩子：
    - 默认关闭 (`MINERU_POPO_ENABLED=false`) 时零开销直通返回 `dfs`；
    - 当处于 `"auto"`（且本机具备 CUDA 或 Apple Silicon M 系列）或 `"on"` 模式时，
      对经规则后处理后仍相邻存在的候选跨页表对执行 4B 语义截断复核、列级单元格合并与表注聚合。
    """
    if not dfs or len(dfs) < 2:
        return list(dfs) if dfs else []

    enabled, reason = should_enable_mineru_popo(config=config, hw_info=hw_info)
    if not enabled:
        return list(dfs)

    popo_client = client or MinerUPopoClient(config=config, hw_info=hw_info)
    active_backend = popo_client.resolve_active_backend()
    if not active_backend and client is None:
        return list(dfs)

    try:
        from .table_postprocess import combine_df_group, _aggregate_group_footnotes
    except ImportError:
        from table_postprocess import combine_df_group, _aggregate_group_footnotes

    out: List[pd.DataFrame] = [dfs[0]]
    merges_applied = 0

    for nxt in dfs[1:]:
        prev = out[-1]
        if not _is_candidate_cross_page_pair(prev, nxt):
            out.append(nxt)
            continue

        decision = popo_client.analyze_table_pair(prev, nxt, pdf_path=pdf_path)
        if not decision or not decision.get("is_continuation") or float(decision.get("confidence", 0.0)) < 0.55:
            out.append(nxt)
            continue

        prev_adj, nxt_adj = _apply_verified_cell_merge_vector(
            prev, nxt, decision.get("cell_merge_vector") or []
        )

        # 补充 MinerU-Popo 恢复的脚注与标题元信息
        if decision.get("recovered_footnotes"):
            nxt_adj = nxt_adj.copy()
            nxt_adj.attrs = dict(getattr(nxt, "attrs", None) or {})
            existing_fn = nxt_adj.attrs.get("footnotes") or []
            if isinstance(existing_fn, str):
                existing_fn = [existing_fn] if existing_fn.strip() else []
            nxt_adj.attrs["footnotes"] = list(existing_fn) + list(decision["recovered_footnotes"])
        else:
            nxt_adj.attrs = dict(getattr(nxt, "attrs", None) or {})
        prev_adj.attrs = dict(getattr(prev, "attrs", None) or {})

        if nxt_adj.empty:
            merged = prev_adj
            _aggregate_group_footnotes([prev_adj, nxt], merged.attrs)
        else:
            merged = combine_df_group([prev_adj, nxt_adj])
            if merged is None:
                # 无法置信对齐的横向分块严格保留独立片段
                out.append(nxt)
                continue

        # 同步页码与审计元数据
        pages = []
        for frag in (prev, nxt):
            fa = getattr(frag, "attrs", None) or {}
            if fa.get("covered_pages"):
                for p in fa["covered_pages"]:
                    if p not in pages:
                        pages.append(p)
            elif fa.get("page_idx") is not None and fa["page_idx"] not in pages:
                pages.append(fa["page_idx"])
        if pages:
            merged.attrs["covered_pages"] = sorted(pages)
            merged.attrs["page_idx"] = min(pages)
        if decision.get("recovered_caption") and not merged.attrs.get("table_title"):
            merged.attrs["table_title"] = decision["recovered_caption"]
        merged.attrs["popo_postprocessed"] = True
        merged.attrs["popo_activation_reason"] = reason
        out[-1] = merged
        merges_applied += 1

    if merges_applied > 0:
        print(f"[MinerU-Popo] 后处理钩子完成 {merges_applied} 组跨页截断表格语义合并 ({reason}, backend={active_backend or 'custom'})。")
    return out
