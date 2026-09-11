#!/usr/bin/env python3
"""
doclayout_yolo_detector.py — 基于 zotero-figure 模型的本地 DocLayout-YOLO 目标检测器。

功能：
1. 自动从技能目录 (./models) 或系统全局目录解压/加载 doclayout-yolo-docstructbench-q8-6c25a56c.onnx 模型。
2. 使用 ONNX Runtime 对 PDF 页面图片进行版面结构识别（识别 table, table_caption, table_footnote 等）。
3. 结合空间位置解算表格主体与关联标题、脚注的扩展裁剪框（Extended Crop Box）。
"""

import io
import os
import re
import urllib.request
import zipfile
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


MODEL_FILENAME = "doclayout-yolo-docstructbench-q8-6c25a56c.onnx"

# 相对路径计算：根据当前脚本位置找到技能根目录下的 models/
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT_DIR = os.path.dirname(SCRIPT_DIR)
SKILL_MODEL_DIR = os.path.join(SKILL_ROOT_DIR, "models")
GLOBAL_MODEL_DIR = os.path.expanduser("~/.zotero_models")

XPI_RELEASE_URL = "https://github.com/MuiseDestiny/zotero-figure/releases/download/v1.0.0/zotero-figure.xpi"
XPI_INTERNAL_PATH = "chrome/content/models/darknoah99/DocLayout-YOLO-DocStructBench-onnx/doclayout-yolo-docstructbench-q8-6c25a56c.onnx"

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


def ensure_model_file(model_dir: Optional[str] = None) -> str:
    """
    检索并确保 ONNX 模型存在。
    优先检查技能目录 (./models/)，其次检查全局缓存 (~/.zotero_models/)，
    若皆不存在，自动从 zotero-figure 官方 Release 解压下载存入技能目录。
    """
    if model_dir:
        target_dir = os.path.abspath(os.path.expanduser(model_dir))
    else:
        target_dir = SKILL_MODEL_DIR

    target_path = os.path.join(target_dir, MODEL_FILENAME)
    if os.path.exists(target_path) and os.path.getsize(target_path) > 10 * 1024 * 1024:
        return target_path

    # 检查全局缓存回退路径
    global_path = os.path.join(GLOBAL_MODEL_DIR, MODEL_FILENAME)
    if os.path.exists(global_path) and os.path.getsize(global_path) > 10 * 1024 * 1024:
        # 如果全局有，拷贝一份到技能目录
        os.makedirs(target_dir, exist_ok=True)
        import shutil
        shutil.copy2(global_path, target_path)
        print(f"[DocLayout-YOLO] 已将全局缓存模型复制至技能目录: {target_path}")
        return target_path

    # 下载至目标目录
    os.makedirs(target_dir, exist_ok=True)
    print(f"[DocLayout-YOLO] 首次运行，正在自动下载模型至技能目录...")
    print(f"[DocLayout-YOLO] 下载源: {XPI_RELEASE_URL}")
    req = urllib.request.Request(XPI_RELEASE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        xpi_data = resp.read()

    with zipfile.ZipFile(io.BytesIO(xpi_data)) as zf:
        if XPI_INTERNAL_PATH not in zf.namelist():
            raise FileNotFoundError(f"zotero-figure.xpi 中未找到模型文件: {XPI_INTERNAL_PATH}")
        with open(target_path, "wb") as f_out:
            f_out.write(zf.read(XPI_INTERNAL_PATH))

    print(f"[DocLayout-YOLO] 模型已成功保存至技能目录: {target_path}")
    return target_path


class DocLayoutYoloDetector:
    _session = None

    def __init__(self, model_path: Optional[str] = None):
        if ort is None or np is None or Image is None:
            raise RuntimeError("缺少必要依赖 (onnxruntime, numpy, pillow)。请先安装：pip install onnxruntime numpy pillow")

        if model_path is None:
            model_path = ensure_model_file()

        if DocLayoutYoloDetector._session is None:
            print(f"[DocLayout-YOLO] 正在初始化 ONNX 推理会话: {os.path.basename(model_path)}")
            DocLayoutYoloDetector._session = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
        self.session = DocLayoutYoloDetector._session

    def detect(self, img: Image.Image, conf_threshold: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        对输入的 PIL Image 进行版面结构检测。
        返回包含 type, score, bbox ([x1, y1, x2, y2] 原始图片像素坐标) 的检测项列表。
        """
        orig_w, orig_h = img.size
        scale = min(640.0 / orig_w, 640.0 / orig_h)
        new_w, new_h = int(orig_w * scale), int(orig_h * scale)

        img_resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
        pad_img = Image.new("RGB", (640, 640), (114, 114, 114))
        pad_w = (640 - new_w) // 2
        pad_h = (640 - new_h) // 2
        pad_img.paste(img_resized, (pad_w, pad_h))

        img_np = np.array(pad_img, dtype=np.float32) / 255.0
        img_np = np.transpose(img_np, (2, 0, 1))  # HWC -> CHW
        img_np = np.expand_dims(img_np, axis=0)    # CHW -> NCHW

        outputs = self.session.run(None, {"images": img_np})[0]  # [1, 14, 8400]
        preds = outputs[0].T  # [8400, 14]

        boxes = preds[:, :4]
        scores_matrix = preds[:, 4:]  # 10 类别得分

        class_ids = np.argmax(scores_matrix, axis=1)
        scores = np.max(scores_matrix, axis=1)

        raw_results = []
        for (cx, cy, w, h), score, cid in zip(boxes, scores, class_ids):
            label = CLASS_LABELS.get(cid, "unknown")
            thresh = conf_threshold if conf_threshold is not None else CLASS_THRESHOLDS.get(label, CLASS_THRESHOLDS["default"])
            if score < thresh:
                continue

            x1_pad = cx - w / 2.0
            y1_pad = cy - h / 2.0
            x2_pad = cx + w / 2.0
            y2_pad = cy + h / 2.0

            x1 = max(0.0, min(orig_w, (x1_pad - pad_w) / scale))
            y1 = max(0.0, min(orig_h, (y1_pad - pad_h) / scale))
            x2 = max(0.0, min(orig_w, (x2_pad - pad_w) / scale))
            y2 = max(0.0, min(orig_h, (y2_pad - pad_h) / scale))

            raw_results.append({
                "type": label,
                "score": float(score),
                "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)]
            })

        # 分类别 NMS
        final_results = []
        for label_name in set(r["type"] for r in raw_results):
            cls_items = [r for r in raw_results if r["type"] == label_name]
            cls_items.sort(key=lambda x: x["score"], reverse=True)
            keep = []
            while cls_items:
                best = cls_items.pop(0)
                keep.append(best)
                cls_items = [item for item in cls_items if self._iou(best["bbox"], item["bbox"]) < 0.45]
            final_results.extend(keep)

        return final_results

    @staticmethod
    def _iou(b1: List[float], b2: List[float]) -> float:
        x1 = max(b1[0], b2[0])
        y1 = max(b1[1], b2[1])
        x2 = min(b1[2], b2[2])
        y2 = min(b1[3], b2[3])
        inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        area1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
        area2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
        return inter / (area1 + area2 - inter + 1e-6)

    def extract_extended_table_regions(self, img: Image.Image, padding: int = 15) -> List[Dict[str, Any]]:
        """
        核心合并逻辑：找到每个表格主体 (table)，自动搜寻并联合其附近的表格标题 (table_caption) 
        和表格脚注 (table_footnote)，生成包含完整语义上下文的扩充矩形框 (Extended Bounding Box)。
        """
        detections = self.detect(img)
        tables = [d for d in detections if d["type"] == "table"]
        captions = [d for d in detections if d["type"] == "table_caption"]
        footnotes = [d for d in detections if d["type"] == "table_footnote"]

        if not tables:
            return []

        orig_w, orig_h = img.size
        used_captions = set()
        used_footnotes = set()
        extended_regions = []

        tables.sort(key=lambda t: t["bbox"][1])

        for idx, tbl in enumerate(tables):
            tx1, ty1, tx2, ty2 = tbl["bbox"]
            t_height = ty2 - ty1

            matched_captions = []
            for c_idx, cap in enumerate(captions):
                if c_idx in used_captions:
                    continue
                cx1, cy1, cx2, cy2 = cap["bbox"]
                h_overlap = min(tx2, cx2) - max(tx1, cx1)
                if h_overlap > 0 or abs(cx1 - tx1) < orig_w * 0.3:
                    if abs(cy2 - ty1) < max(t_height * 0.4, 80) or abs(cy1 - ty2) < max(t_height * 0.3, 60):
                        matched_captions.append(cap)
                        used_captions.add(c_idx)

            matched_footnotes = []
            for f_idx, fn in enumerate(footnotes):
                if f_idx in used_footnotes:
                    continue
                fx1, fy1, fx2, fy2 = fn["bbox"]
                if abs(fy1 - ty2) < max(t_height * 0.35, 70):
                    matched_footnotes.append(fn)
                    used_footnotes.add(f_idx)

            all_boxes = [tbl["bbox"]] + [c["bbox"] for c in matched_captions] + [f["bbox"] for f in matched_footnotes]
            min_x1 = min(b[0] for b in all_boxes)
            min_y1 = min(b[1] for b in all_boxes)
            max_x2 = max(b[2] for b in all_boxes)
            max_y2 = max(b[3] for b in all_boxes)

            crop_x1 = max(0, int(min_x1 - padding))
            crop_y1 = max(0, int(min_y1 - padding))
            crop_x2 = min(orig_w, int(max_x2 + padding))
            crop_y2 = min(orig_h, int(max_y2 + padding))

            cropped_img = img.crop((crop_x1, crop_y1, crop_x2, crop_y2))

            extended_regions.append({
                "table_index": idx,
                "table_bbox": tbl["bbox"],
                "crop_bbox": [crop_x1, crop_y1, crop_x2, crop_y2],
                "score": tbl["score"],
                "has_caption": len(matched_captions) > 0,
                "has_footnote": len(matched_footnotes) > 0,
                "image": cropped_img
            })

        return extended_regions
