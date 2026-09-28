#!/usr/bin/env python3
"""
doclayout_yolo_detector.py — 基于 aoiandroid/doclayout-yolo-docstructbench-imgsz1280-onnx 模型的本地 DocLayout-YOLO 目标检测器。

功能：
1. 自动从技能目录 (./models) 或系统全局目录加载 doclayout_yolo_docstructbench_imgsz1280_2501.onnx 模型（若缺失自动从 Hugging Face / 镜像站下载）。
2. 使用 ONNX Runtime 对 PDF 页面图片进行版面结构识别（识别 table, table_caption, table_footnote 等），
   原生解析 1280x1280 端到端 NMS 输出 [1, 300, 6]，并平滑兼容旧版 640x640 输出 [1, 14, 8400]。
3. 结合空间位置解算表格主体与关联标题、脚注的扩展裁剪框（Extended Crop Box）。
"""

import io
import os
import re
import urllib.request
from typing import Dict, List, Optional, Tuple, Any

try:
    import numpy as np
except ImportError:
    np = None

try:
    import onnxruntime as ort
except ImportError:
    ort = None

try:
    from PIL import Image
except ImportError:
    Image = None


MODEL_FILENAME = "doclayout_yolo_docstructbench_imgsz1280_2501.onnx"
LEGACY_MODEL_FILENAME = "doclayout-yolo-docstructbench-q8-6c25a56c.onnx"

# 相对路径计算：根据当前脚本位置找到技能根目录下的 models/
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT_DIR = os.path.dirname(SCRIPT_DIR)
SKILL_MODEL_DIR = os.path.join(SKILL_ROOT_DIR, "models")
GLOBAL_MODEL_DIR = os.path.expanduser("~/.zotero_models")

HF_MODEL_URL = "https://huggingface.co/aoiandroid/doclayout-yolo-docstructbench-imgsz1280-onnx/resolve/main/doclayout_yolo_docstructbench_imgsz1280_2501.onnx"
HF_MIRROR_URL = "https://hf-mirror.com/aoiandroid/doclayout-yolo-docstructbench-imgsz1280-onnx/resolve/main/doclayout_yolo_docstructbench_imgsz1280_2501.onnx"

CLASS_LABELS = {
    0: "title",
    1: "plain_text",
    2: "abandon",
    3: "figure",
    4: "figure_caption",
    5: "table",
    6: "table_caption",
    7: "table_footnote",
    8: "isolate_formula",
    9: "formula_caption",
}

CLASS_THRESHOLDS = {
    "table": 0.25,
    "table_caption": 0.20,
    "table_footnote": 0.15,
    "default": 0.20,
}


def download_model(target_path: str) -> str:
    """
    从 Hugging Face 或 hf-mirror 镜像源下载 DocLayout-YOLO 1280 模型。
    采用原子写入 (.download.tmp -> 最终文件) 防止断网导致模型文件损坏。
    """
    urls = [HF_MODEL_URL, HF_MIRROR_URL]
    target_dir = os.path.dirname(target_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
    temp_path = target_path + ".download.tmp"

    last_error = None
    for url in urls:
        print(f"[DocLayout-YOLO] 正在下载 1280 ONNX 模型: {url} -> {target_path} ...")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(temp_path, "wb") as f_out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f_out.write(chunk)

            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 10 * 1024 * 1024:
                os.replace(temp_path, target_path)
                print(f"[DocLayout-YOLO] 模型已成功保存至技能目录: {target_path}")
                return target_path
            else:
                raise ValueError("下载的模型文件尺寸异常 (小于 10MB)")
        except Exception as e:
            print(f"[DocLayout-YOLO] 下载失败 ({url}): {e}")
            last_error = e
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    raise RuntimeError(f"无法从任何源下载 DocLayout-YOLO 1280 模型: {last_error}")


def ensure_model_file(model_dir: Optional[str] = None) -> str:
    """
    检索并确保 ONNX 模型存在。
    优先检查技能目录 (./models/) 或指定目录下的新版 1280 模型，
    其次检查全局缓存 (~/.zotero_models/)，
    若皆不存在，自动从 Hugging Face / 镜像站下载存入技能目录；
    若下载异常且存在本地旧版 640 模型，平滑回退至旧版模型。
    """
    if model_dir:
        expanded_dir = os.path.abspath(os.path.expanduser(model_dir))
        if os.path.isfile(expanded_dir) and os.path.getsize(expanded_dir) > 10 * 1024 * 1024:
            return expanded_dir
        target_dir = expanded_dir
    else:
        target_dir = SKILL_MODEL_DIR

    # 1. 检查目标目录中的新版 1280 模型
    target_path = os.path.join(target_dir, MODEL_FILENAME)
    if os.path.exists(target_path) and os.path.getsize(target_path) > 10 * 1024 * 1024:
        return target_path

    # 2. 检查全局缓存目录中的新版 1280 模型
    global_path = os.path.join(GLOBAL_MODEL_DIR, MODEL_FILENAME)
    if os.path.exists(global_path) and os.path.getsize(global_path) > 10 * 1024 * 1024:
        try:
            os.makedirs(target_dir, exist_ok=True)
            import shutil
            shutil.copy2(global_path, target_path)
            print(f"[DocLayout-YOLO] 已将全局缓存模型复制至技能目录: {target_path}")
            return target_path
        except Exception as e_copy:
            print(f"[DocLayout-YOLO] 复制全局缓存至目标目录失败 ({e_copy})，直接使用全局缓存模型: {global_path}")
            return global_path

    # 3. 自动从 Hugging Face / 镜像源下载 1280 模型
    try:
        download_model(target_path)
        return target_path
    except Exception as e:
        print(f"[DocLayout-YOLO] 下载 1280 模型异常: {e}，尝试检测旧版 640 模型兜底...")

    # 4. 回退检查目标目录中的旧版 640 模型
    legacy_target = os.path.join(target_dir, LEGACY_MODEL_FILENAME)
    if os.path.exists(legacy_target) and os.path.getsize(legacy_target) > 10 * 1024 * 1024:
        print(f"[DocLayout-YOLO] 使用本地既有旧版 640 模型作为回退: {legacy_target}")
        return legacy_target

    # 5. 回退检查全局缓存中的旧版 640 模型
    legacy_global = os.path.join(GLOBAL_MODEL_DIR, LEGACY_MODEL_FILENAME)
    if os.path.exists(legacy_global) and os.path.getsize(legacy_global) > 10 * 1024 * 1024:
        try:
            os.makedirs(target_dir, exist_ok=True)
            import shutil
            shutil.copy2(legacy_global, legacy_target)
            print(f"[DocLayout-YOLO] 已将全局旧版缓存模型复制至技能目录: {legacy_target}")
            return legacy_target
        except Exception:
            return legacy_global

    raise FileNotFoundError(f"未找到 DocLayout-YOLO 模型，且下载失败: {target_path}")


class DocLayoutYoloDetector:
    _session = None
    _loaded_model_path = None
    _input_size = (1280, 1280)

    def __init__(self, model_path: Optional[str] = None):
        if ort is None or np is None or Image is None:
            raise RuntimeError("缺少必要依赖 (onnxruntime, numpy, pillow)。请先安装：pip install onnxruntime numpy pillow")

        if model_path is None:
            model_path = ensure_model_file()

        model_path = os.path.abspath(os.path.expanduser(model_path))

        if DocLayoutYoloDetector._session is None or DocLayoutYoloDetector._loaded_model_path != model_path:
            print(f"[DocLayout-YOLO] 正在初始化 ONNX 推理会话: {os.path.basename(model_path)}")
            DocLayoutYoloDetector._session = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
            DocLayoutYoloDetector._loaded_model_path = model_path

            in_w, in_h = 1280, 1280
            try:
                inp = DocLayoutYoloDetector._session.get_inputs()[0]
                shape = inp.shape
                if len(shape) >= 4:
                    h_val, w_val = shape[2], shape[3]
                    if isinstance(h_val, (int, np.integer)) and h_val > 0:
                        in_h = int(h_val)
                    if isinstance(w_val, (int, np.integer)) and w_val > 0:
                        in_w = int(w_val)
            except Exception:
                in_w, in_h = 1280, 1280
            DocLayoutYoloDetector._input_size = (in_w, in_h)

        self.session = DocLayoutYoloDetector._session
        self.model_path = model_path
        self.input_size = DocLayoutYoloDetector._input_size

    def detect(self, img: Image.Image, conf_threshold: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        对输入的 PIL Image 进行版面结构检测。
        支持:
        - 新版 1280x1280 模型输出 [1, 300, 6] (x1, y1, x2, y2, score, class_id) 内置端到端 NMS
        - 旧版 640x640 模型输出 [1, 14, 8400] 或 [1, 8400, 14] 兼容解析与后置 NMS
        返回包含 type, score, bbox ([x1, y1, x2, y2] 原始图片像素坐标) 的检测项列表。
        """
        orig_w, orig_h = img.size
        if orig_w <= 0 or orig_h <= 0:
            return []

        if img.mode != "RGB":
            img = img.convert("RGB")

        target_w, target_h = self.input_size if hasattr(self, "input_size") else (1280, 1280)

        scale = min(float(target_w) / orig_w, float(target_h) / orig_h)
        new_w = max(1, min(target_w, int(round(orig_w * scale))))
        new_h = max(1, min(target_h, int(round(orig_h * scale))))

        img_resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
        pad_img = Image.new("RGB", (target_w, target_h), (114, 114, 114))
        pad_w = (target_w - new_w) // 2
        pad_h = (target_h - new_h) // 2
        pad_img.paste(img_resized, (pad_w, pad_h))

        img_np = np.array(pad_img, dtype=np.float32) / 255.0
        img_np = np.transpose(img_np, (2, 0, 1))  # HWC -> CHW
        img_np = np.expand_dims(img_np, axis=0)    # CHW -> NCHW

        input_name = self.session.get_inputs()[0].name if self.session.get_inputs() else "images"
        outputs = self.session.run(None, {input_name: img_np})[0]

        raw_results = []
        out = np.asarray(outputs)

        # 模式 1: [1, 300, 6] 或 [N, 6] (内置端到端 NMS 格式: [x1, y1, x2, y2, score, class_id])
        if (out.ndim == 3 and out.shape[-1] == 6) or (out.ndim == 2 and out.shape[-1] == 6):
            preds = out[0] if out.ndim == 3 else out
            for det in preds:
                x1_pad, y1_pad, x2_pad, y2_pad, score, cid = det[:6]
                score = float(score)
                if np.isnan(score) or np.isinf(score) or score <= 0.0:
                    continue
                cid = int(round(float(cid)))
                label = CLASS_LABELS.get(cid, "unknown")
                thresh = conf_threshold if conf_threshold is not None else CLASS_THRESHOLDS.get(label, CLASS_THRESHOLDS["default"])
                if score < thresh:
                    continue

                x1 = max(0.0, min(float(orig_w), (float(x1_pad) - pad_w) / scale))
                y1 = max(0.0, min(float(orig_h), (float(y1_pad) - pad_h) / scale))
                x2 = max(0.0, min(float(orig_w), (float(x2_pad) - pad_w) / scale))
                y2 = max(0.0, min(float(orig_h), (float(y2_pad) - pad_h) / scale))

                if np.isnan(x1) or np.isnan(y1) or np.isnan(x2) or np.isnan(y2) or x2 <= x1 or y2 <= y1:
                    continue

                raw_results.append({
                    "type": label,
                    "score": round(score, 4),
                    "bbox": [round(float(x1), 1), round(float(y1), 1), round(float(x2), 1), round(float(y2), 1)]
                })

        # 模式 2: [1, C, N] (旧版 640 通道优先输出，如 [1, 14, 8400])
        elif out.ndim == 3 and out.shape[1] < out.shape[2]:
            preds = out[0].T  # [N, C]
            boxes = preds[:, :4]  # cx, cy, w, h
            scores_matrix = preds[:, 4:]  # 类别得分

            class_ids = np.argmax(scores_matrix, axis=1)
            scores = np.max(scores_matrix, axis=1)

            for (cx, cy, w, h), score, cid in zip(boxes, scores, class_ids):
                score = float(score)
                if np.isnan(score) or np.isinf(score) or score <= 0.0:
                    continue
                cid = int(cid)
                label = CLASS_LABELS.get(cid, "unknown")
                thresh = conf_threshold if conf_threshold is not None else CLASS_THRESHOLDS.get(label, CLASS_THRESHOLDS["default"])
                if score < thresh:
                    continue

                x1_pad = cx - w / 2.0
                y1_pad = cy - h / 2.0
                x2_pad = cx + w / 2.0
                y2_pad = cy + h / 2.0

                x1 = max(0.0, min(float(orig_w), (float(x1_pad) - pad_w) / scale))
                y1 = max(0.0, min(float(orig_h), (float(y1_pad) - pad_h) / scale))
                x2 = max(0.0, min(float(orig_w), (float(x2_pad) - pad_w) / scale))
                y2 = max(0.0, min(float(orig_h), (float(y2_pad) - pad_h) / scale))

                if np.isnan(x1) or np.isnan(y1) or np.isnan(x2) or np.isnan(y2) or x2 <= x1 or y2 <= y1:
                    continue

                raw_results.append({
                    "type": label,
                    "score": round(score, 4),
                    "bbox": [round(float(x1), 1), round(float(y1), 1), round(float(x2), 1), round(float(y2), 1)]
                })

        # 模式 3: [1, N, C] (旧版 640 已转置输出，如 [1, 8400, 14])
        elif out.ndim == 3 and out.shape[2] >= 5 and out.shape[1] > out.shape[2]:
            preds = out[0]  # [N, C]
            boxes = preds[:, :4]
            scores_matrix = preds[:, 4:]

            class_ids = np.argmax(scores_matrix, axis=1)
            scores = np.max(scores_matrix, axis=1)

            for (cx, cy, w, h), score, cid in zip(boxes, scores, class_ids):
                score = float(score)
                if np.isnan(score) or np.isinf(score) or score <= 0.0:
                    continue
                cid = int(cid)
                label = CLASS_LABELS.get(cid, "unknown")
                thresh = conf_threshold if conf_threshold is not None else CLASS_THRESHOLDS.get(label, CLASS_THRESHOLDS["default"])
                if score < thresh:
                    continue

                x1_pad = cx - w / 2.0
                y1_pad = cy - h / 2.0
                x2_pad = cx + w / 2.0
                y2_pad = cy + h / 2.0

                x1 = max(0.0, min(float(orig_w), (float(x1_pad) - pad_w) / scale))
                y1 = max(0.0, min(float(orig_h), (float(y1_pad) - pad_h) / scale))
                x2 = max(0.0, min(float(orig_w), (float(x2_pad) - pad_w) / scale))
                y2 = max(0.0, min(float(orig_h), (float(y2_pad) - pad_h) / scale))

                if np.isnan(x1) or np.isnan(y1) or np.isnan(x2) or np.isnan(y2) or x2 <= x1 or y2 <= y1:
                    continue

                raw_results.append({
                    "type": label,
                    "score": round(score, 4),
                    "bbox": [round(float(x1), 1), round(float(y1), 1), round(float(x2), 1), round(float(y2), 1)]
                })

        # 分类别 NMS 去重（消除同类近距离重复检测框，保证确定性迭代顺序）
        final_results = []
        for label_name in sorted(set(r["type"] for r in raw_results)):
            cls_items = [r for r in raw_results if r["type"] == label_name]
            cls_items.sort(key=lambda x: x["score"], reverse=True)
            keep = []
            while cls_items:
                best = cls_items.pop(0)
                keep.append(best)
                cls_items = [item for item in cls_items if self._iou(best["bbox"], item["bbox"]) < 0.45]
            final_results.extend(keep)

        # 统一按置信度降序排列
        final_results.sort(key=lambda x: x["score"], reverse=True)

        # 跨类别近重叠抑制（抑制同一物理区域被不同类别多重输出的幽灵框，保留高置信度项）
        cross_deduped = []
        for item in final_results:
            if any(self._iou(item["bbox"], kept["bbox"]) >= 0.85 for kept in cross_deduped):
                continue
            cross_deduped.append(item)

        return cross_deduped

    @staticmethod
    def _iou(b1: List[float], b2: List[float]) -> float:
        x1 = max(b1[0], b2[0])
        y1 = max(b1[1], b2[1])
        x2 = min(b1[2], b2[2])
        y2 = min(b1[3], b2[3])
        inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        area1 = max(0.0, b1[2] - b1[0]) * max(0.0, b1[3] - b1[1])
        area2 = max(0.0, b2[2] - b2[0]) * max(0.0, b2[3] - b2[1])
        denom = area1 + area2 - inter
        if denom <= 0:
            return 0.0
        return float(inter / denom)

    def extract_extended_table_regions(self, img: Image.Image, padding: int = 15, conf_threshold: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        核心合并逻辑：找到每个表格主体 (table)，自动搜寻并联合其附近的表格标题 (table_caption) 
        和表格脚注 (table_footnote)，生成包含完整语义上下文的扩充矩形框 (Extended Bounding Box)。
        采用全局二分/最佳匹配算法，彻底杜绝跨列多表格以及上下多表格间的标题/脚注误抢与裁剪框重叠。
        """
        orig_w, orig_h = img.size
        if orig_w <= 0 or orig_h <= 0:
            return []

        # 保证统一以 RGB 模式进行裁切，杜绝 alpha 通道或调色板模式异常
        img_rgb = img.convert("RGB") if img.mode != "RGB" else img

        detections = self.detect(img_rgb, conf_threshold=conf_threshold)
        tables = [d for d in detections if d["type"] == "table"]
        captions = [d for d in detections if d["type"] == "table_caption"]
        footnotes = [d for d in detections if d["type"] == "table_footnote"]

        if not tables:
            return []

        # 按从上到下、从左到右确定性排序
        tables.sort(key=lambda t: (t["bbox"][1], t["bbox"][0]))

        # 全局为每个 Caption 寻找最佳匹配表格（避免先到先得的贪心误抢）
        assigned_captions: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(len(tables))}
        for cap in captions:
            cx1, cy1, cx2, cy2 = cap["bbox"]
            c_w = cx2 - cx1
            best_table_idx = None
            min_cost = float("inf")

            for t_idx, tbl in enumerate(tables):
                tx1, ty1, tx2, ty2 = tbl["bbox"]
                t_w = tx2 - tx1
                t_h = ty2 - ty1

                # 排除与表格主体高度重合的误检项
                if self._iou(tbl["bbox"], cap["bbox"]) >= 0.3:
                    continue

                # 水平交集检验：必须具备实质性横向重合（杜绝跨栏/跨列误抢）
                h_overlap = min(tx2, cx2) - max(tx1, cx1)
                min_w = min(t_w, c_w)
                if min_w <= 0 or h_overlap <= 0:
                    continue
                overlap_ratio = h_overlap / min_w
                if overlap_ratio < 0.20:
                    continue

                # 垂直关联判定：上方表题 (常见) 或 下方表题 (罕见)
                cost = None
                # 情况 A: 位于表格上方 (允许 15px 框容差)
                if cy1 < ty2 and cy2 <= ty1 + 15:
                    gap = max(0.0, ty1 - cy2)
                    max_gap = max(50.0, min(t_h * 0.25, 90.0))
                    if gap <= max_gap:
                        cost = gap + (1.0 - overlap_ratio) * 30.0

                # 情况 B: 位于表格下方 (允许 15px 框容差，附加优先级惩罚项)
                elif cy2 > ty1 and cy1 >= ty2 - 15:
                    gap = max(0.0, cy1 - ty2)
                    max_gap = max(40.0, min(t_h * 0.20, 70.0))
                    if gap <= max_gap:
                        cost = gap + 25.0 + (1.0 - overlap_ratio) * 30.0

                if cost is not None and cost < min_cost:
                    min_cost = cost
                    best_table_idx = t_idx

            if best_table_idx is not None:
                assigned_captions[best_table_idx].append(cap)

        # 全局为每个 Footnote 寻找最佳匹配表格
        assigned_footnotes: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(len(tables))}
        for fn in footnotes:
            fx1, fy1, fx2, fy2 = fn["bbox"]
            f_w = fx2 - fx1
            best_table_idx = None
            min_cost = float("inf")

            for t_idx, tbl in enumerate(tables):
                tx1, ty1, tx2, ty2 = tbl["bbox"]
                t_w = tx2 - tx1
                t_h = ty2 - ty1

                if self._iou(tbl["bbox"], fn["bbox"]) >= 0.3:
                    continue

                h_overlap = min(tx2, fx2) - max(tx1, fx1)
                min_w = min(t_w, f_w)
                if min_w <= 0 or h_overlap <= 0:
                    continue
                overlap_ratio = h_overlap / min_w
                if overlap_ratio < 0.20:
                    continue

                # 脚注恒位于表格下方
                if fy2 >= ty2 and fy1 >= ty2 - 20:
                    gap = max(0.0, fy1 - ty2)
                    max_gap = max(50.0, min(t_h * 0.30, 90.0))
                    if gap <= max_gap:
                        cost = gap + (1.0 - overlap_ratio) * 30.0
                        if cost < min_cost:
                            min_cost = cost
                            best_table_idx = t_idx

            if best_table_idx is not None:
                assigned_footnotes[best_table_idx].append(fn)

        extended_regions = []
        for idx, tbl in enumerate(tables):
            matched_captions = assigned_captions[idx]
            matched_footnotes = assigned_footnotes[idx]

            all_boxes = [tbl["bbox"]] + [c["bbox"] for c in matched_captions] + [f["bbox"] for f in matched_footnotes]
            min_x1 = min(b[0] for b in all_boxes)
            min_y1 = min(b[1] for b in all_boxes)
            max_x2 = max(b[2] for b in all_boxes)
            max_y2 = max(b[3] for b in all_boxes)

            # 确定裁切边界上限，防止 padding 侵入同一页面的邻近表格主体
            limit_x1 = 0
            limit_y1 = 0
            limit_x2 = orig_w
            limit_y2 = orig_h

            for o_idx, other_tbl in enumerate(tables):
                if o_idx == idx:
                    continue
                ox1, oy1, ox2, oy2 = other_tbl["bbox"]
                # 检查与当前表格扩展框在水平方向是否有投影对齐
                if min(max_x2, ox2) > max(min_x1, ox1):
                    if oy2 <= min_y1:
                        limit_y1 = max(limit_y1, int(round((oy2 + min_y1) / 2.0)))
                    elif oy1 >= max_y2:
                        limit_y2 = min(limit_y2, int(round((oy1 + max_y2) / 2.0)))
                # 检查与当前表格扩展框在垂直方向是否有投影对齐
                if min(max_y2, oy2) > max(min_y1, oy1):
                    if ox2 <= min_x1:
                        limit_x1 = max(limit_x1, int(round((ox2 + min_x1) / 2.0)))
                    elif ox1 >= max_x2:
                        limit_x2 = min(limit_x2, int(round((ox1 + max_x2) / 2.0)))

            crop_x1 = max(limit_x1, int(round(min_x1 - padding)))
            crop_y1 = max(limit_y1, int(round(min_y1 - padding)))
            crop_x2 = min(limit_x2, int(round(max_x2 + padding)))
            crop_y2 = min(limit_y2, int(round(max_y2 + padding)))

            if crop_x2 <= crop_x1 or crop_y2 <= crop_y1:
                continue

            cropped_img = img_rgb.crop((crop_x1, crop_y1, crop_x2, crop_y2))

            extended_regions.append({
                "table_index": idx,
                "table_bbox": [float(b) for b in tbl["bbox"]],
                "crop_bbox": [crop_x1, crop_y1, crop_x2, crop_y2],
                "score": float(tbl["score"]),
                "has_caption": len(matched_captions) > 0,
                "has_footnote": len(matched_footnotes) > 0,
                "image": cropped_img
            })

        return extended_regions

