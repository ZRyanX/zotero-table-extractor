#!/usr/bin/env python3
"""
scripts/doc_orientation_detector.py — 基于 TurboOCR doc_ori.onnx (PP-LCNet) 与矢量文本流的双轨旋转检测与校准器。

功能：
1. 模型管理与按需下载：
   自动检测并加载 models/doc_ori.onnx (PP-LCNet_x1_0_doc_ori, ~6.47MB, 4-class {0: 0°, 1: 90°, 2: 180°, 3: 270°})。
   若本地未下载，支持从官方 GitHub Release 安全原子下载并进行 SHA256 完整性校验；在无网络或模型缺失时平滑优雅降级。
2. 图像前处理与方差导向裁剪 (Variance-Guided Crop)：
   短边等比缩放至 256；对宽表/稀疏版面采用基于灰度方差的自适应滑动窗口裁切 224x224（避开大面积纯白留白），
   结合 ImageNet 标准化 (mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]) 输出 [1, 3, 224, 224] float32 张量。
3. 双轨两阶旋转识别与置信度门限：
   - 极速矢量轨：基于 PyMuPDF span["dir"]/line["dir"] 0-时延、100% 精准识别原生矢量文字旋转；
   - 视觉模型轨：基于 ONNX Runtime CPU 进行 doc_ori 推理，计算 Softmax 裕度 probs[top1] - probs[top2]。
     当裕度 < 0.25 (kDocOriMargin) 时触发安全回退返回 0°，避免弱特征下的误翻转。
4. 两级翻转矫正 (Dual-Track Two-Tier Remediation)：
   - Tier 1: 页面级无损顺时针校准（通过 page.set_rotation 与 doc.save 无损重写 PDF /Rotate，不破坏矢量文本与排版）；
   - Tier 2: 嵌套横表切图正向矫正（通过 PIL 旋转切图使其在送入 VLM/PaddleOCR 前恢复正向，彻底防止注意力崩溃与列串位）。
"""

import os
import sys
import math
import urllib.request
import hashlib
import tempfile
from typing import Optional, Tuple, Dict, Any, List, Union
from collections import Counter

try:
    import numpy as np
except ImportError:
    np = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import onnxruntime as ort
except ImportError:
    ort = None

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None


MODEL_FILENAME = "doc_ori.onnx"
OFFICIAL_MODEL_URL = "https://github.com/aiptimizer/TurboOCR/releases/download/models-v3.0.0-ppocrv6/doc_ori.onnx"
EXPECTED_SHA256 = "96e898f047a0e460ba0652e9afb8c874e53872821cfd7a3fec53a5ab62df92f0"

# 4 类别对应文字顺时针旋转角度
# 0: 0° (正向正常阅读)
# 1: 90° (顺时针旋转 90°, 从上往下读)
# 2: 180° (倒置 180°, 从右往左倒着读)
# 3: 270° (顺时针旋转 270° / 逆时针 90°, 从下往上读)
CLASS_TO_DEGREE = {0: 0, 1: 90, 2: 180, 3: 270}
DEGREE_TO_CLASS = {0: 0, 90: 1, 180: 2, 270: 3}

DEFAULT_MARGIN_THRESHOLD = 0.25  # kDocOriMargin

IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT_DIR = os.path.dirname(SCRIPT_DIR)
SKILL_MODEL_DIR = os.path.join(SKILL_ROOT_DIR, "models")
GLOBAL_MODEL_DIR = os.path.expanduser("~/.zotero_models")


def compute_sha256(filepath: str) -> str:
    """计算指定文件的 SHA256 散列值。"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def download_model(target_path: Optional[str] = None, url: Optional[str] = None) -> str:
    """
    从官方源或指定 URL 下载 TurboOCR doc_ori.onnx 模型。
    采用原子写入 (.download.tmp -> 最终文件) 与 SHA256 校验防止文件损坏。
    """
    dest_path = target_path or os.path.join(SKILL_MODEL_DIR, MODEL_FILENAME)
    src_url = url or OFFICIAL_MODEL_URL
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    temp_path = dest_path + ".download.tmp"

    print(f"[Doc-Ori] 正在下载 TurboOCR 页面方向模型: {src_url} -> {dest_path} ...")
    try:
        req = urllib.request.Request(
            src_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZoteroTableExtractor"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp, open(temp_path, "wb") as f_out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f_out.write(chunk)

        if not os.path.exists(temp_path) or os.path.getsize(temp_path) < 1 * 1024 * 1024:
            raise ValueError("下载的模型文件尺寸异常 (< 1MB)")

        digest = compute_sha256(temp_path)
        if EXPECTED_SHA256 and digest.lower() != EXPECTED_SHA256.lower():
            raise ValueError(f"下载的 doc_ori 模型 SHA256 散列值不匹配 (得到: {digest}, 预期: {EXPECTED_SHA256})")

        os.replace(temp_path, dest_path)
        print(f"[Doc-Ori] 模型已成功保存并校验: {dest_path}")
        return dest_path
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise RuntimeError(f"无法下载 TurboOCR doc_ori 模型: {e}")


def ensure_model_file(model_dir: Optional[str] = None, auto_download: bool = False) -> Optional[str]:
    """
    检索并确认 doc_ori.onnx 模型路径。
    检索顺序：
    1. 用户显式指定目录
    2. 技能自带 models/
    3. 全局缓存目录 ~/.zotero_models/
    4. 若未找到且 auto_download 为 True，尝试自动下载至技能 models/
    若均未找到，返回 None 以实现优雅降级。
    """
    candidates = []
    if model_dir:
        exp_dir = os.path.abspath(os.path.expanduser(model_dir))
        if os.path.isfile(exp_dir) and os.path.getsize(exp_dir) > 1 * 1024 * 1024:
            return exp_dir
        if exp_dir.lower().endswith(".onnx"):
            # 用户显式指定了具体 .onnx 文件路径但该文件不存在，不应静默覆盖
            return None
        candidates.append(os.path.join(exp_dir, MODEL_FILENAME))

    candidates.append(os.path.join(SKILL_MODEL_DIR, MODEL_FILENAME))
    candidates.append(os.path.join(GLOBAL_MODEL_DIR, MODEL_FILENAME))

    for p in candidates:
        if os.path.isfile(p) and os.path.getsize(p) > 1 * 1024 * 1024:
            return p

    if auto_download:
        try:
            return download_model()
        except Exception as e:
            print(f"[Doc-Ori] 自动下载失败: {e}，将平滑降级跳过视觉方向推理")

    return None


def get_clockwise_correction_angle(detected_angle: int) -> int:
    """
    根据检测到的文字/图像顺时针旋转角度 (0, 90, 180, 270)，
    返回将其旋转回正向 (0° 正常阅读) 所需的顺时针补正角度 (0, 90, 180, 270)。
    例如：
    - detected_angle = 90° (顺时针旋转了90度) -> 需要顺时针补正 (360 - 90) = 270° (或逆时针 90°)
    - detected_angle = 270° (顺时针旋转了270度) -> 需要顺时针补正 (360 - 270) = 90°
    - detected_angle = 180° -> 需要顺时针补正 180°
    - detected_angle = 0° -> 0°
    """
    return (360 - (detected_angle % 360)) % 360


def detect_vector_text_details(page: Any, rect: Optional[Union[List[float], Tuple[float, ...]]] = None) -> Tuple[int, int, float]:
    """
    极速检测 PDF 页面或局部区域 (rect) 内原生矢量文本的主导阅读旋转方向、总字符数及主导方向占比。
    利用 PyMuPDF 原生 line["dir"] 与 span["dir"] 方向矢量计算角度，实现 0 时延、100% 精确识别。
    
    参数:
      page: PyMuPDF Page 对象
      rect: 可选的裁剪区域 [x0, y0, x1, y1] (PDF 72-dpi 坐标)
      
    返回:
      (dominant_angle, total_char_count, dominant_ratio)
      - dominant_angle: 0 | 90 | 180 | 270
      - total_char_count: 区域内统计到的矢量字符总数
      - dominant_ratio: 主导方向字符数占总字符数的比例 (0.0 ~ 1.0)
    """
    if page is None or fitz is None:
        return 0, 0, 0.0

    try:
        clip_rect = fitz.Rect(rect) if rect is not None else None
        text_dict = page.get_text("dict", clip=clip_rect) if clip_rect else page.get_text("dict")
    except Exception:
        return 0, 0, 0.0

    counts = Counter()
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            dir_vec = line.get("dir")
            if not dir_vec:
                for span in line.get("spans", []):
                    if span.get("dir"):
                        dir_vec = span["dir"]
                        break

            if not dir_vec or len(dir_vec) < 2:
                continue

            dx, dy = float(dir_vec[0]), float(dir_vec[1])
            if abs(dx) < 1e-4 and abs(dy) < 1e-4:
                continue

            spans = line.get("spans", [])
            text_len = sum(len(s.get("text", "").strip()) for s in spans)
            if text_len == 0:
                continue

            # PyMuPDF 中 y 轴向下，(dx, dy) 表达行文本走向向量：
            # (1, 0) -> 水平向右 0°
            # (0, 1) -> 垂直向下 90° (顺时针 90°)
            # (-1, 0) -> 水平向左 180°
            # (0, -1) -> 垂直向上 270° (顺时针 270°)
            angle_rad = math.atan2(dy, dx)
            angle_deg = (math.degrees(angle_rad)) % 360
            snapped = int(round(angle_deg / 90.0) * 90) % 360
            counts[snapped] += text_len

    if not counts:
        return 0, 0, 0.0

    most_common_angle, most_len = counts.most_common(1)[0]
    total_len = sum(counts.values())
    ratio = (most_len / float(total_len)) if total_len > 0 else 0.0
    return most_common_angle, total_len, ratio


def detect_vector_text_rotation(page: Any, rect: Optional[Union[List[float], Tuple[float, ...]]] = None) -> int:
    """
    极速检测 PDF 页面或局部区域 (rect) 内原生矢量文本的主导阅读旋转方向。
    保持原有签名兼容，当主导方向占比 >= 60% 时返回对应角度 (0, 90, 180, 270)，否则返回 0。
    """
    angle, total_len, ratio = detect_vector_text_details(page, rect=rect)
    if total_len > 0 and ratio >= 0.60:
        return angle
    return 0


def preprocess_image(
    img: Union[Any, "np.ndarray"],
    use_variance_crop: bool = True
) -> "np.ndarray":
    """
    TurboOCR doc_ori 标准图像前处理：
    1. 转为 RGB 模式；
    2. 短边等比缩放至 256；
    3. 方差导向自适应裁剪 (Variance-Guided Crop) 224x224：
       在长边方向上自适应评估多个候选裁剪窗口的灰度方差，选取纹理信息量最丰富的区域（规避空白表格边缘与大边距）；
    4. ImageNet 归一化 (mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])；
    5. HWC -> CHW，添加 batch 维度，返回 [1, 3, 224, 224] float32 张量。
    """
    if Image is None or np is None:
        raise RuntimeError("PIL 与 numpy 是运行图像前处理的必要依赖")

    if isinstance(img, np.ndarray):
        pil_img = Image.fromarray(img)
    elif hasattr(img, "convert"):
        pil_img = img
    else:
        raise TypeError(f"不支持的图像类型: {type(img)}")

    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")

    w, h = pil_img.size
    if w <= 0 or h <= 0:
        raise ValueError(f"无效的图像尺寸: {pil_img.size}")

    # 短边缩放至 256
    if w < h:
        new_w = 256
        new_h = max(256, int(round(h * (256.0 / float(w)))))
    else:
        new_h = 256
        new_w = max(256, int(round(w * (256.0 / float(h)))))

    resized = pil_img.resize((new_w, new_h), Image.Resampling.BILINEAR)

    # 224x224 裁剪
    if use_variance_crop and (new_w > 256 or new_h > 256):
        # 宽表或长页：在长轴滑动窗口中寻找文本特征方差最大的切块，短轴保持居中
        if new_w > 256:
            fixed_y = (new_h - 224) // 2
            max_x = new_w - 224
            num_samples = 7
            best_crop = None
            max_var = -1.0
            step_x = max_x / max(1, num_samples - 1)
            for i in range(num_samples):
                cx = int(round(i * step_x))
                cand = resized.crop((cx, fixed_y, cx + 224, fixed_y + 224))
                cand_gray = np.array(cand.convert("L"), dtype=np.float32)
                v = float(np.var(cand_gray))
                if v > max_var:
                    max_var = v
                    best_crop = cand
            crop = best_crop if best_crop is not None else resized.crop(((new_w - 224) // 2, fixed_y, (new_w + 224) // 2, fixed_y + 224))
        else:
            fixed_x = (new_w - 224) // 2
            max_y = new_h - 224
            num_samples = 7
            best_crop = None
            max_var = -1.0
            step_y = max_y / max(1, num_samples - 1)
            for i in range(num_samples):
                cy = int(round(i * step_y))
                cand = resized.crop((fixed_x, cy, fixed_x + 224, cy + 224))
                cand_gray = np.array(cand.convert("L"), dtype=np.float32)
                v = float(np.var(cand_gray))
                if v > max_var:
                    max_var = v
                    best_crop = cand
            crop = best_crop if best_crop is not None else resized.crop((fixed_x, (new_h - 224) // 2, fixed_x + 224, (new_h + 224) // 2))
    else:
        left = (new_w - 224) // 2
        top = (new_h - 224) // 2
        crop = resized.crop((left, top, left + 224, top + 224))

    arr = np.array(crop, dtype=np.float32) / 255.0
    mean = np.array(IMAGENET_MEAN, dtype=np.float32)
    std = np.array(IMAGENET_STD, dtype=np.float32)
    arr = (arr - mean) / std

    # HWC -> CHW -> NCHW
    arr = np.transpose(arr, (2, 0, 1))
    arr = np.expand_dims(arr, 0).astype(np.float32)
    return arr


class DocOrientationDetector:
    """
    基于 ONNX Runtime CPU 的轻量级单例/类文档方向推理检测器。
    """

    def __init__(self, model_path: Optional[str] = None):
        self.session = None
        self.input_name = None
        self.output_name = None
        self.model_path = None

        if ort is None:
            print("[Doc-Ori] onnxruntime 未安装，模型推理功能处于禁用状态")
            return

        resolved_path = ensure_model_file(model_path, auto_download=False)
        if not resolved_path or not os.path.isfile(resolved_path):
            return

        try:
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 2
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            self.session = ort.InferenceSession(
                resolved_path,
                sess_options=opts,
                providers=["CPUExecutionProvider"]
            )
            self.input_name = self.session.get_inputs()[0].name
            self.output_name = self.session.get_outputs()[0].name
            self.model_path = resolved_path
            print(f"[Doc-Ori] 成功加载 TurboOCR 文档方向 ONNX 模型: {resolved_path}")
        except Exception as e:
            print(f"[Doc-Ori] 加载 ONNX 模型异常: {e}")
            self.session = None

    def is_available(self) -> bool:
        """模型会话是否已就绪。"""
        return self.session is not None

    def predict(
        self,
        img: Union[Any, "np.ndarray"],
        margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
        use_variance_crop: bool = True
    ) -> Dict[str, Any]:
        """
        对给定输入图片进行方向预测。
        
        返回字典:
        {
            "detected_angle": 0 | 90 | 180 | 270,    # 当前图片顺时针旋转的角度
            "correction_angle": 0 | 90 | 180 | 270,  # 使其回正所需顺时针旋转的角度
            "confidence": float,                     # Top 1 置信度
            "margin": float,                         # Top 1 与 Top 2 之间的概率裕度
            "probs": List[float],                    # 4 类 Softmax 概率列表
            "needs_rotation": bool,                  # 是否需要且有足够裕度进行翻转矫正
            "source": "model" | "fallback"
        }
        """
        fallback_res = {
            "detected_angle": 0,
            "correction_angle": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "probs": [1.0, 0.0, 0.0, 0.0],
            "needs_rotation": False,
            "source": "fallback"
        }

        if not self.is_available() or np is None:
            return fallback_res

        try:
            arr = preprocess_image(img, use_variance_crop=use_variance_crop)
            logits = self.session.run([self.output_name], {self.input_name: arr})[0][0]

            # Softmax
            exp = np.exp(logits - np.max(logits))
            probs = (exp / np.sum(exp)).tolist()

            ranked = sorted(enumerate(probs), key=lambda x: x[1], reverse=True)
            top1_cls, top1_prob = ranked[0]
            top2_cls, top2_prob = ranked[1]
            margin = float(top1_prob - top2_prob)

            raw_degree = CLASS_TO_DEGREE.get(top1_cls, 0)

            # 裕度门限守卫：若区分度过低 (< margin_threshold) 或本来就是 0°，安全回退至 0°
            if margin < margin_threshold or raw_degree == 0:
                detected_angle = 0
                needs_rotation = False
            else:
                detected_angle = raw_degree
                needs_rotation = True

            correction_angle = get_clockwise_correction_angle(detected_angle)

            return {
                "detected_angle": detected_angle,
                "correction_angle": correction_angle,
                "confidence": float(top1_prob),
                "margin": margin,
                "probs": probs,
                "needs_rotation": needs_rotation,
                "source": "model"
            }
        except Exception as e:
            print(f"[Doc-Ori] 推理过程发生异常: {e}")
            return fallback_res


# 单例缓存
_GLOBAL_DETECTOR: Optional[DocOrientationDetector] = None


def get_orientation_detector(model_path: Optional[str] = None) -> DocOrientationDetector:
    """获取全局共享的 DocOrientationDetector 实例。"""
    global _GLOBAL_DETECTOR
    if _GLOBAL_DETECTOR is None or not _GLOBAL_DETECTOR.is_available() or model_path:
        _GLOBAL_DETECTOR = DocOrientationDetector(model_path=model_path)
    return _GLOBAL_DETECTOR


def detect_page_orientation(
    page: Any,
    detector: Optional[DocOrientationDetector] = None,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD
) -> Dict[str, Any]:
    """
    对 PDF 单页进行双轨方向检测（优先 0-时延矢量文本轨，兜底视觉模型轨）。
    """
    # Track 1: 极速矢量文本轨
    angle, total_len, ratio = detect_vector_text_details(page)
    if total_len >= 15 and ratio >= 0.60:
        if angle in (90, 180, 270):
            corr = get_clockwise_correction_angle(angle)
            return {
                "detected_angle": angle,
                "correction_angle": corr,
                "confidence": 1.0,
                "margin": 1.0,
                "needs_rotation": True,
                "source": "vector_text"
            }
        elif angle == 0:
            return {
                "detected_angle": 0,
                "correction_angle": 0,
                "confidence": 1.0,
                "margin": 1.0,
                "needs_rotation": False,
                "source": "vector_text_upright"
            }

    # Track 2: 视觉模型轨（针对扫描页或少字图表页）
    det = detector or get_orientation_detector()
    if not det.is_available():
        return {
            "detected_angle": 0,
            "correction_angle": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "needs_rotation": False,
            "source": "fallback"
        }

    try:
        pix = page.get_pixmap(dpi=100)
        im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        return det.predict(im, margin_threshold=margin_threshold, use_variance_crop=True)
    except Exception as e:
        print(f"[Doc-Ori] 页面渲染缩略图推理异常: {e}")
        return {
            "detected_angle": 0,
            "correction_angle": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "needs_rotation": False,
            "source": "fallback"
        }


def detect_crop_orientation(
    crop_img: Any,
    page: Optional[Any] = None,
    rect: Optional[Union[List[float], Tuple[float, ...]]] = None,
    detector: Optional[DocOrientationDetector] = None,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD
) -> Dict[str, Any]:
    """
    对嵌套表格裁剪区域 (Table Crop) 进行双轨方向检测。
    """
    # Track 1: 若提供对应 PDF Page 与坐标，优先尝试矢量文字检测
    if page is not None and rect is not None:
        try:
            angle, total_len, ratio = detect_vector_text_details(page, rect=rect)
            if total_len >= 10 and ratio >= 0.60:
                if angle in (90, 180, 270):
                    corr = get_clockwise_correction_angle(angle)
                    return {
                        "detected_angle": angle,
                        "correction_angle": corr,
                        "confidence": 1.0,
                        "margin": 1.0,
                        "needs_rotation": True,
                        "source": "vector_text"
                    }
                elif angle == 0:
                    return {
                        "detected_angle": 0,
                        "correction_angle": 0,
                        "confidence": 1.0,
                        "margin": 1.0,
                        "needs_rotation": False,
                        "source": "vector_text_upright"
                    }
        except Exception:
            pass

    # Track 2: 视觉模型轨
    det = detector or get_orientation_detector()
    if not det.is_available():
        return {
            "detected_angle": 0,
            "correction_angle": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "needs_rotation": False,
            "source": "fallback"
        }

    return det.predict(crop_img, margin_threshold=margin_threshold, use_variance_crop=True)


def remediate_pdf_pages_lossless(
    pdf_path: str,
    candidate_pages: Optional[List[int]] = None,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD
) -> Tuple[str, bool, Optional[str]]:
    """
    Tier 1: 页面级无损顺时针方向校准。
    在提取管线入口前快速检测 candidate_pages（或前几页与含表声明页）。
    若检测到页面文字处于旋转状态 (90°, 180°, 270°)，通过 page.set_rotation() 进行无损顺时针校准，
    并保存为临时 PDF 文件。
    
    返回:
      (effective_pdf_path, was_modified, temp_pdf_to_clean)
      - 若无需翻转，返回 (orig_pdf_path, False, None)；
      - 若完成校准，返回 (temp_pdf_path, True, temp_pdf_path)。
    """
    if not os.path.exists(pdf_path) or fitz is None:
        return pdf_path, False, None

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"[Doc-Ori Tier 1] 打开 PDF 失败: {e}")
        return pdf_path, False, None

    total_pages = len(doc)
    if total_pages == 0:
        doc.close()
        return pdf_path, False, None

    # 确定检测范围：若有候选页列表则检测候选页；若无，文献页数 <= 15 全检，> 15 检前 5 页及含表声明页
    if candidate_pages is not None:
        pages_to_check = [p for p in candidate_pages if 0 <= p < total_pages]
    elif total_pages <= 15:
        pages_to_check = list(range(total_pages))
    else:
        pages_to_check = list(range(min(5, total_pages)))
        # 补充含 Caption 的潜在页面
        try:
            from table_validator import scan_pdf_table_declarations
            decls = scan_pdf_table_declarations(pdf_path)
            for p in decls.keys():
                if 0 <= p < total_pages and p not in pages_to_check:
                    pages_to_check.append(p)
        except Exception:
            pass

    detector = get_orientation_detector()
    modified = False

    for p_idx in pages_to_check:
        page = doc[p_idx]
        # 仅针对未经旋转定义 (page.rotation == 0) 的页面进行检测；已设 rotation 的页面已在视口层声明过旋转
        if getattr(page, "rotation", 0) != 0:
            continue

        res = detect_page_orientation(page, detector=detector, margin_threshold=margin_threshold)
        if res.get("needs_rotation") and res.get("correction_angle") in (90, 180, 270):
            deg = res["correction_angle"]
            new_rot = (page.rotation + deg) % 360
            page.set_rotation(new_rot)
            modified = True
            print(f"[Doc-Ori Tier 1] 第 {p_idx + 1} 页检测到文字旋转 {res.get('detected_angle')}° ({res.get('source')})，已无损设置 page.set_rotation({new_rot}°)")

    if modified:
        fd, temp_pdf = tempfile.mkstemp(prefix="lossless_upright_", suffix=".pdf")
        os.close(fd)
        try:
            doc.save(temp_pdf, garbage=3, deflate=True)
            doc.close()
            print(f"[Doc-Ori Tier 1] 无损正向校准 PDF 已保存至: {temp_pdf}")
            return temp_pdf, True, temp_pdf
        except Exception as e_save:
            print(f"[Doc-Ori Tier 1] 保存无损校准 PDF 失败: {e_save}")
            doc.close()
            if os.path.exists(temp_pdf):
                try:
                    os.remove(temp_pdf)
                except OSError:
                    pass
            return pdf_path, False, None
    else:
        doc.close()
        return pdf_path, False, None
