#!/usr/bin/env python3
"""
pdf_tables.py — PDF 页面旋转坐标处理与 DocLayout-YOLO 本地 AI 表格提取集成。

功能：
1. 页面级旋转检测与烘焙 (get_page_effective_rotation)。
2. 结合启发式预过滤 (pdf_page_filter) 与 DocLayout-YOLO 目标检测 (doclayout_yolo_detector)，
   直接从本地 PDF 页面中精确定位表格、标题与脚注切图。
3. 提供原生离线 DataFrame 重建与 Excel 导出回退支持 (export_crops_to_excel)。
"""

import io
import os
import re
import sys
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import pymupdf as fitz
except ImportError:
    import fitz
sys.modules['fitz'] = fitz

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import pandas as pd
except ImportError:
    pd = None

# 引入本包内的预过滤与视觉检测逻辑
try:
    from .common import load_config, make_unique_columns
    from .ocr_client import extract_pp_structure_table_crops
    from .pdf_page_filter import page_may_contain_tables
    from .doclayout_yolo_detector import DocLayoutYoloDetector
    from .excel_export import save_tables_to_excel
except ImportError:
    try:
        from common import load_config, make_unique_columns
        from ocr_client import extract_pp_structure_table_crops
        from pdf_page_filter import page_may_contain_tables
        from doclayout_yolo_detector import DocLayoutYoloDetector
        from excel_export import save_tables_to_excel
    except ImportError:
        load_config = lambda: {}
        make_unique_columns = lambda cols: [str(c) for c in cols]
        extract_pp_structure_table_crops = None
        page_may_contain_tables = None
        DocLayoutYoloDetector = None
        save_tables_to_excel = None


def get_page_effective_rotation(page):
    """
    Returns the required counter-rotation angle (0, 90, 180, 270) to make text read horizontally.
    Checks page.rotation metadata.
    """
    try:
        if hasattr(page, 'rotation') and page.rotation != 0:
            return (-page.rotation) % 360
    except Exception:
        pass
    return 0



def extract_table_crops_from_pdf(
    pdf_path: str,
    dpi: int = 200,
    conf_threshold: float = 0.25,
    enable_filter: bool = True,
    cancel_event = None,
    pages: Optional[List[int]] = None
) -> List[Dict[str, Any]]:
    """
    从本地 PDF 文件中提取所有表格及相关上下文（Caption/Footnote）的图像区域 Crop。
    优先使用本地超快 DocLayout-YOLO 视觉目标检测（~15ms 本地离线推理，零 Token 消耗与零网络时延）；
    仅在本地模型被禁用、未检出有效表格或推理异常时，平滑回退至云端 PP-StructureV3。
    
    返回列表，各项结构：
    {
        "page_index": 0,           # 0-indexed 页码
        "table_index": 0,          # 页面内表格序号
        "crop_bbox": [x1,y1,x2,y2],# 图像像素坐标（可选）
        "score": 0.95,             # 目标检测置信度
        "has_caption": True,
        "has_footnote": False,
        "image": PIL.Image,        # 表格+上下文裁剪图像
        "page_text": "..."         # 该页原始文本/Markdown
    }
    """
    if not os.path.exists(pdf_path):
        print(f"[PDF-Tables] 文件不存在: {pdf_path}")
        return []

    config = load_config() if load_config else {}

    # 1. 优先使用本地超快 DocLayout-YOLO 目标检测
    yolo_enabled = config.get("DOCLAYOUT_YOLO_ENABLED", True)
    if yolo_enabled and DocLayoutYoloDetector is not None:
        try:
            print("[PDF-Tables] 正在使用本地 DocLayout-YOLO 进行超快版面定位与表格切图...")
            model_dir = config.get("DOCLAYOUT_MODEL_DIR")
            conf_thresh = conf_threshold if conf_threshold != 0.25 else config.get("DOCLAYOUT_CONF_THRESHOLD", conf_threshold)

            detector = None
            try:
                if model_dir:
                    try:
                        from .doclayout_yolo_detector import ensure_model_file
                    except ImportError:
                        from doclayout_yolo_detector import ensure_model_file
                    model_path = ensure_model_file(model_dir)
                    detector = DocLayoutYoloDetector(model_path=model_path)
            except Exception as e_m:
                print(f"[PDF-Tables] 尝试指定模型目录加载 DocLayout-YOLO 提示: {e_m}")

            if detector is None:
                detector = DocLayoutYoloDetector()

            doc = fitz.open(pdf_path)
            extracted_crops = []
            total_pages = len(doc)

            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)

            target_pages = [p for p in pages if 0 <= p < total_pages] if pages is not None else list(range(total_pages))

            for page_idx in target_pages:
                if cancel_event is not None and cancel_event.is_set():
                    break
                page = doc[page_idx]
                page_text = page.get_text("text")

                # 启发式预检：若页面不含图表特征且未显式指定目标页，则提前跳过
                if enable_filter and pages is None and page_may_contain_tables and not page_may_contain_tables(page_text):
                    continue

                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                # 运行 DocLayout-YOLO 表格+上下文检测
                regions = detector.extract_extended_table_regions(img, conf_threshold=conf_thresh)
                for r in regions:
                    r["page_index"] = page_idx
                    r["page_text"] = page_text
                    # 极速探测矢量文本质量与 OCR 需求感知 (Rust ~1ms)
                    if r.get("crop_bbox"):
                        reg_info = inspect_crop_region(pdf_path, page_idx, r["crop_bbox"], dpi=dpi)
                        r["needs_ocr"] = reg_info["needs_ocr"]
                        r["ocr_reason"] = reg_info["ocr_reason"]
                        r["region_vector_text"] = reg_info["text"]
                        r["pdf_bbox"] = reg_info["pdf_bbox"]
                    extracted_crops.append(r)

            doc.close()
            print(f"[PDF-Tables] 本地 DocLayout-YOLO 从 {total_pages} 页 PDF 中定位并提取出 {len(extracted_crops)} 个表格区域 Crop。")
            if extracted_crops:
                return extracted_crops
            else:
                print("[PDF-Tables] 本地 DocLayout-YOLO 未在目标页检出表格，尝试云端版面分析回退...")
        except Exception as e:
            print(f"[PDF-Tables] 本地 DocLayout-YOLO 视觉检测异常: {e}，尝试云端版面分析回退...")

    # 2. 回退：云端 PP-StructureV3 在线版面定位与表格切图
    if config.get("USE_PP_STRUCTURE", True) and extract_pp_structure_table_crops is not None:
        try:
            print("[PDF-Tables] 正在使用 PP-StructureV3 进行在线版面定位与表格切图回退...")
            pp_crops = extract_pp_structure_table_crops(pdf_path, config=config, cancel_event=cancel_event, pages=pages)
            if pp_crops:
                return pp_crops
        except Exception as e:
            print(f"[PDF-Tables] PP-StructureV3 切图回退提示: {e}")

    return []


def convert_crop_to_mediabox(
    pdf_path: str,
    page_index: int,
    crop_bbox: List[float],
    dpi: int = 200
) -> Tuple[float, float, float, float, float]:
    """
    将 DocLayout-YOLO 在 rendered pixmap (dpi 分辨率) 上的检测框 [px0, py0, px1, py1]
    精确换算为 PDF MediaBox 72-DPI 点坐标（top-left 原点: x_min, y_min, x_max, y_max），
    全面适配 page.rotation (0, 90, 180, 270) 与 page.cropbox 偏移。
    返回 (x_min, y_min, x_max, y_max, mediabox_height)。
    """
    eff_dpi = max(1.0, float(dpi)) if dpi else 200.0
    scale = 72.0 / eff_dpi
    try:
        with fitz.open(pdf_path) as doc:
            if 0 <= page_index < len(doc):
                page = doc[page_index]
                vis_rect = fitz.Rect(
                    crop_bbox[0] * scale,
                    crop_bbox[1] * scale,
                    crop_bbox[2] * scale,
                    crop_bbox[3] * scale
                )
                unrot_rect = vis_rect * page.derotation_matrix
                cb_x0 = page.cropbox.x0
                cb_y0 = page.cropbox.y0
                x0 = unrot_rect.x0 + cb_x0
                y0 = unrot_rect.y0 + cb_y0
                x1 = unrot_rect.x1 + cb_x0
                y1 = unrot_rect.y1 + cb_y0
                return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), page.mediabox.height
    except Exception as e:
        logger.debug(f"[PDF-Tables] convert_crop_to_mediabox error: {e}")

    x0, y0, x1, y1 = crop_bbox[0] * scale, crop_bbox[1] * scale, crop_bbox[2] * scale, crop_bbox[3] * scale
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), 842.0


def inspect_crop_region(pdf_path: str, page_index: int, crop_bbox: List[float], dpi: int = 200) -> Dict[str, Any]:
    """
    使用 pdf-inspector (Rust 极速 ~1ms) 判定给定目标检测区域内是否具有高质量矢量文本，
    或是否由于纯图像/损坏字体导致必须触发 OCR (needs_ocr)。

    参数:
    - pdf_path: PDF 路径
    - page_index: 0-indexed 页码
    - crop_bbox: [px1, py1, px2, py2] 像素坐标
    - dpi: 渲染分辨率

    返回:
    {
        'needs_ocr': bool,
        'ocr_reason': Optional[str],
        'text': str,
        'pdf_bbox': [x1, y1, x2, y2], # 72 DPI PDF 点坐标（top-left 原点）
    }
    """
    x1, y1, x2, y2, mb_height = convert_crop_to_mediabox(pdf_path, page_index, crop_bbox, dpi)
    pdf_bbox = [x1, y1, x2, y2]

    res = {
        'needs_ocr': False,
        'ocr_reason': None,
        'text': '',
        'pdf_bbox': pdf_bbox,
    }

    try:
        import pdf_inspector
        if hasattr(pdf_inspector, 'extract_text_in_regions'):
            region_texts = pdf_inspector.extract_text_in_regions(pdf_path, [(page_index, [[x1, y1, x2, y2]])])
            if region_texts and region_texts[0].regions:
                reg = region_texts[0].regions[0]
                text = (reg.text or "").strip()
                res['text'] = text
                # 判定条件：显式 needs_ocr 标记，或仅包含图片标记 [Image: ...]，或文本极度匮乏
                is_image_only = text.startswith('[Image:') or (len(text) < 5 and not any(c.isalnum() for c in text))
                res['needs_ocr'] = bool(reg.needs_ocr or is_image_only)
                res['ocr_reason'] = reg.ocr_reason if reg.needs_ocr else ('image_only' if is_image_only else None)
                return res
    except Exception as e:
        logger.debug(f"[PDF-Tables] inspect_crop_region 提示: {e}")

    # 回退方案: PyMuPDF 启发式检测
    try:
        with fitz.open(pdf_path) as doc:
            if 0 <= page_index < len(doc):
                page = doc[page_index]
                clip_rect = fitz.Rect(
                    x1 - page.cropbox.x0,
                    y1 - page.cropbox.y0,
                    x2 - page.cropbox.x0,
                    y2 - page.cropbox.y0
                )
                txt = page.get_text("text", clip=clip_rect).strip()
                res['text'] = txt
                imgs = page.get_images()
                res['needs_ocr'] = len(txt) < 8 and len(imgs) > 0
                res['ocr_reason'] = 'heuristic_low_text_image' if res['needs_ocr'] else None
    except Exception:
        pass

    return res


def rebuild_df_with_style_hierarchy(
    pdf_path: str,
    page_index: int,
    crop_rect: fitz.Rect,
    page_height: float,
    crop_bbox: Optional[List[float]] = None,
    dpi: int = 200,
) -> Tuple[Optional[Any], str, str]:
    """
    利用 pdf_inspector.extract_text_with_positions 的丰富样式属性 (is_bold, font_size, font, mcid)
    从裁剪区域精准重建带有复合表头分层与附注隔离的 Pandas DataFrame。

    特点:
    - 样式感知表头：首行与次行连续加粗或次行为括号单位时，自动合成多行复合表头
    - 字号感知注脚：利用字号差异 (font_size < data_font_size) 完美分离底部小字号注释，杜绝污染数据行
    - 坐标区间投影：基于真实 x 轴物理区间对齐列边界，杜绝单元格内空格切词破坏
    - 全面适配页面旋转 (90/180/270 度) 与 CropBox/MediaBox 物理偏移
    """
    if pd is None or not os.path.exists(pdf_path):
        return None, "", ""

    try:
        import pdf_inspector
        if not hasattr(pdf_inspector, 'extract_text_with_positions'):
            return None, "", ""
        # extract_text_with_positions 内部使用 1-indexed pages
        items = pdf_inspector.extract_text_with_positions(pdf_path, pages=[page_index + 1])
    except Exception:
        return None, "", ""

    if not items:
        return None, "", ""

    # 精确计算 MediaBox 坐标区间 (top-left 原点)
    if crop_bbox is not None:
        x_min, y_min, x_max, y_max, mb_height = convert_crop_to_mediabox(pdf_path, page_index, crop_bbox, dpi)
    else:
        try:
            with fitz.open(pdf_path) as doc:
                page = doc[page_index]
                unrot_rect = crop_rect * page.derotation_matrix
                cb_x0, cb_y0 = page.cropbox.x0, page.cropbox.y0
                x0 = unrot_rect.x0 + cb_x0
                y0 = unrot_rect.y0 + cb_y0
                x1 = unrot_rect.x1 + cb_x0
                y1 = unrot_rect.y1 + cb_y0
                x_min, y_min, x_max, y_max = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
                mb_height = page.mediabox.height
        except Exception:
            x_min, y_min, x_max, y_max = crop_rect.x0, crop_rect.y0, crop_rect.x1, crop_rect.y1
            mb_height = page_height

    # 过滤落在 crop 范围内的 items (top-left 坐标空间: top_y = mb_height - it.y)
    filtered = []
    for it in items:
        top_y = mb_height - it.y
        if (x_min - 4.0 <= it.x <= x_max + 4.0 and
                y_min - 4.0 <= top_y <= y_max + 4.0):
            filtered.append(it)

    if len(filtered) < 4:
        return None, "", ""

    # 按 top_y 坐标分行（4.5pt 容差），同行内按 x 排序
    sorted_items = sorted(filtered, key=lambda it: (mb_height - it.y, it.x))
    lines = []
    curr_line = []
    curr_y = None
    for it in sorted_items:
        y_mid = mb_height - it.y
        if curr_y is None or abs(y_mid - curr_y) < 4.5:
            curr_line.append(it)
            if curr_y is None:
                curr_y = y_mid
        else:
            lines.append(sorted(curr_line, key=lambda x: x.x))
            curr_line = [it]
            curr_y = y_mid
    if curr_line:
        lines.append(sorted(curr_line, key=lambda x: x.x))

    if not lines:
        return None, "", ""

    caption = ""
    footnote_parts = []
    table_lines = []

    # 识别顶部 1~2 行的 Caption
    for l in lines:
        line_str = " ".join(it.text.strip() for it in l if it.text.strip())
        if not line_str:
            continue
        if re.match(r'^(?:Table|Tab\.|表)\s*\d+(?:\.\d+)?', line_str, re.IGNORECASE) and len(line_str) < 200:
            if not caption:
                caption = line_str
            continue
        table_lines.append(l)

    if not table_lines:
        return None, caption, ""

    # 识别并剔除底部附注/Footnote（利用字号差异与特定关键字）
    all_sizes = [it.font_size for l in table_lines for it in l if it.font_size > 0]
    median_size = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 9.0

    filtered_table_lines = []
    for l in reversed(table_lines):
        line_str = " ".join(it.text.strip() for it in l if it.text.strip())
        avg_sz = sum(it.font_size for it in l) / len(l) if l else median_size
        low = line_str.lower()
        is_fn = (
            line_str.startswith(("注", "*", "†", "‡", "§")) or
            low.startswith("note") or
            "minimum" in low or "standard deviation" in low or
            "p < 0." in low or
            re.search(r'\b(?:n\s*=\s*\d|df\s*=)', line_str)
        )
        if not filtered_table_lines and (is_fn or avg_sz <= median_size - 1.2):
            footnote_parts.insert(0, line_str)
        else:
            filtered_table_lines.insert(0, l)

    table_lines = filtered_table_lines
    if not table_lines:
        return None, caption, " ".join(footnote_parts)

    # 复合多行表头识别：若前 1~2 行全为加粗或括号单位，合并为复合表头
    header_rows = [table_lines[0]]
    data_start_idx = 1
    if len(table_lines) >= 3:
        l0 = table_lines[0]
        l1 = table_lines[1]
        b0 = sum(1 for it in l0 if it.is_bold) / len(l0) if l0 else 0
        b1 = sum(1 for it in l1 if it.is_bold) / len(l1) if l1 else 0
        is_units = all(
            re.match(r'^\(.*\)$', it.text.strip()) or it.text.strip() in ('%', 'wt.%', 'MPa', 'g/t', 'm', 'km', '°C')
            for it in l1 if it.text.strip()
        )
        if (b0 >= 0.4 and b1 >= 0.4) or is_units:
            header_rows.append(l1)
            data_start_idx = 2

    # 动态自适应列聚类容差：根据表头内部最小非零水平间距确定，杜绝密集多列表格将相邻两列误合并
    all_header_xs = sorted(it.x for h_row in header_rows for it in h_row)
    gaps = [all_header_xs[i+1] - all_header_xs[i] for i in range(len(all_header_xs)-1) if all_header_xs[i+1] - all_header_xs[i] > 6.0]
    if gaps:
        min_gap = min(gaps)
        cluster_tol = max(6.0, min(18.0, min_gap * 0.45))
    else:
        cluster_tol = 16.0

    # 基于表头项的 x 坐标聚类列边界
    col_clusters = []
    for h_row in header_rows:
        for it in h_row:
            matched = False
            for clust in col_clusters:
                # 检查该 cluster 中是否已存在本行的其他不同 item（防止同行两列误合）
                same_row_items = [x for x in clust if any(x is item for item in h_row)]
                if same_row_items:
                    # 同行中只有紧挨着的连贯词（距离 < 8pt）才允许归入同一列头
                    if any(abs(it.x - (x.x + getattr(x, 'width', 0))) > 8.0 and abs(it.x - x.x) > 10.0 for x in same_row_items):
                        continue
                avg_cx = sum(x.x for x in clust) / len(clust)
                if abs(it.x - avg_cx) < cluster_tol:
                    clust.append(it)
                    matched = True
                    break
            if not matched:
                col_clusters.append([it])

    if not col_clusters:
        return None, caption, " ".join(footnote_parts)

    col_clusters = sorted(col_clusters, key=lambda cl: sum(x.x for x in cl) / len(cl))
    col_headers = []
    col_centers = []
    for cl in col_clusters:
        cl_sorted = sorted(cl, key=lambda it: (mb_height - it.y, it.x))
        col_headers.append(" ".join(it.text.strip() for it in cl_sorted if it.text.strip()))
        col_centers.append(sum(it.x for it in cl) / len(cl))

    col_headers = [c if c else f"Col_{i+1}" for i, c in enumerate(col_headers)]
    col_headers = make_unique_columns(col_headers)

    # 重建数据行
    table_rows = []
    for l in table_lines[data_start_idx:]:
        row_cells = [""] * len(col_headers)
        for it in l:
            best_idx = min(range(len(col_centers)), key=lambda i: abs(col_centers[i] - it.x))
            if row_cells[best_idx]:
                row_cells[best_idx] += " " + it.text.strip()
            else:
                row_cells[best_idx] = it.text.strip()
        if any(c for c in row_cells):
            table_rows.append(row_cells)

    if not table_rows:
        return None, caption, " ".join(footnote_parts)

    df = pd.DataFrame(table_rows, columns=col_headers)
    return df, caption, " ".join(footnote_parts)


def _rebuild_df_from_words(words, clip_rect=None):
    """
    从 word 列表按 y 坐标分行、x 坐标聚类分列重建 DataFrame。
    替代旧的 str.split() 方案——保留多词单元格内容（如 "Sample BX4-5" 不再被拆成两列）。
    同时识别并分离 caption 与 footnote 行。
    """
    if not words:
        return None, "", ""

    # 按 y 坐标分行（5pt 容差），同行内按 x 排序
    words_sorted = sorted(words, key=lambda w: (round(w[1] / 5) * 5, w[0]))
    lines = []
    current_line = []
    current_y = None
    for w in words_sorted:
        y_mid = (w[1] + w[3]) / 2
        if current_y is None or abs(y_mid - current_y) < 5:
            current_line.append(w)
            if current_y is None:
                current_y = y_mid
        else:
            lines.append(current_line)
            current_line = [w]
            current_y = y_mid
    if current_line:
        lines.append(current_line)

    # 从所有 word 的 x0 聚类出列边界（间隔 > 15pt 视为新列）
    all_x0 = sorted(w[0] for line in lines for w in line)
    col_boundaries = []
    if all_x0:
        col_boundaries = [all_x0[0]]
        for x in all_x0[1:]:
            if x - col_boundaries[-1] > 15:
                col_boundaries.append(x)

    def col_idx(x0):
        idx = 0
        for i, b in enumerate(col_boundaries):
            if x0 >= b - 5:
                idx = i
            else:
                break
        return idx

    table_rows = []
    caption = ""
    footnote = ""
    for line in lines:
        line_str = " ".join(w[4] for w in line).strip()
        if not line_str:
            continue
        # Caption: "Table N ..." / "表N ..." (不再硬编码 Table 1/2)
        if re.match(r'^(?:Table|Tab\.|表)\s*\d+(?:\.\d+)?', line_str, re.IGNORECASE) and len(line_str) < 200:
            caption = line_str
            continue
        # Footnote
        low = line_str.lower()
        if (line_str.startswith("注") or low.startswith("note") or
                "minimum" in low or "standard deviation" in low or
                "MIN =" in line_str or low.startswith("n =") or
                re.search(r'\b(?:n\s*=\s*\d|df\s*=)', line_str)):
            footnote = line_str
            continue
        cells = [""] * max(len(col_boundaries), 1)
        for w in line:
            ci = col_idx(w[0])
            if ci >= len(cells):
                cells.extend([""] * (ci + 1 - len(cells)))
            cells[ci] = (cells[ci] + " " + w[4]).strip() if cells[ci] else w[4]
        if any(c.strip() for c in cells):
            table_rows.append(cells)

    if not table_rows:
        return None, caption, footnote

    # 首行作 header，其余作 data
    if len(table_rows) >= 2:
        df = pd.DataFrame(table_rows[1:], columns=table_rows[0])
    else:
        df = pd.DataFrame([table_rows[0]])
    return df, caption, footnote


def crop_to_dataframe(pdf_path: str, crop_info: Dict[str, Any], dpi: int = 200) -> Tuple[Optional[Any], str, str]:
    """
    从 DocLayout-YOLO 裁剪出的物理坐标范围重建 Pandas DataFrame，
    同时提取其对应关联的 Caption 与 Footnote。

    优先使用 PyMuPDF 内置 page.find_tables() 获得结构化单元格
    （自动识别列边界、合并单元格），回退到按 x 坐标聚类的 word-level 重建。
    """
    if pd is None:
        return None, "", ""

    crop_bbox = crop_info.get("crop_bbox")
    if not crop_bbox:
        # PP-Structure 或 VLM 产出的结构化 markdown / html 兜底解析
        if crop_info.get("markdown"):
            try:
                from table_postprocess import parse_structured_vlm_content
                dfs = parse_structured_vlm_content(crop_info["markdown"])
                if dfs:
                    df = dfs[0]
                    if crop_info.get('label') and not df.attrs.get('label'):
                        df.attrs['label'] = crop_info['label']
                    return df, df.attrs.get('table_title', crop_info.get('caption', '')), ""
            except Exception as e:
                print(f"[PDF-Tables] 解析 crop markdown 异常: {e}")
        if crop_info.get("html"):
            try:
                from io import StringIO
                tables = pd.read_html(StringIO(crop_info["html"]))
                if tables:
                    df = tables[0]
                    df.attrs['label'] = crop_info.get('label', '')
                    df.attrs['table_title'] = crop_info.get('caption', '')
                    return df, crop_info.get('caption', ''), ""
            except Exception as e:
                print(f"[PDF-Tables] 解析 crop html 异常: {e}")
        return None, "", ""

    doc = fitz.open(pdf_path)
    page = doc[crop_info["page_index"]]

    # 像素坐标 -> PDF 坐标 (72 dpi 基准)；dpi 从 crop_info 取，兼容非 200 dpi
    crop_dpi = crop_info.get("dpi", dpi)
    scale = 72.0 / float(crop_dpi)
    x1, y1, x2, y2 = [v * scale for v in crop_bbox]
    crop_rect = fitz.Rect(x1, y1, x2, y2)

    df = None
    caption = ""
    footnote = ""

    # 极速探测矢量文本质量与 OCR 需求感知
    if "needs_ocr" not in crop_info:
        reg_info = inspect_crop_region(pdf_path, crop_info["page_index"], crop_bbox, dpi=crop_dpi)
        crop_info["needs_ocr"] = reg_info["needs_ocr"]
        crop_info["ocr_reason"] = reg_info["ocr_reason"]
        crop_info["region_vector_text"] = reg_info["text"]

    # --- 优先: PyMuPDF find_tables() 结构化提取 ---
    try:
        finder = page.find_tables()
        best_table = None
        best_overlap = 0.0
        crop_area = crop_rect.width * crop_rect.height
        for t in finder.tables:
            t_rect = fitz.Rect(t.bbox)
            inter = crop_rect & t_rect
            if inter.is_empty or inter.width <= 0 or inter.height <= 0:
                continue
            overlap = (inter.width * inter.height) / crop_area if crop_area > 0 else 0
            if overlap > best_overlap:
                best_overlap = overlap
                best_table = t
        if best_table is not None and best_overlap > 0.3:
            rows = best_table.extract()
            clean_rows = []
            for row in rows:
                clean_row = [("" if cell is None else str(cell).strip()) for cell in row]
                if any(c for c in clean_row):
                    clean_rows.append(clean_row)
            if len(clean_rows) >= 2:
                df = pd.DataFrame(clean_rows[1:], columns=clean_rows[0])
                df.attrs['label'] = crop_info.get('label', '')
                df.attrs['table_title'] = crop_info.get('caption', '')
    except Exception as e:
        print(f"[PDF-Tables] find_tables() 不可用，回退样式感知/word-level: {e}")

    # --- 增强回退 1: 基于 pdf_inspector 的样式感知多行表头与注脚分离重建 ---
    if df is None or df.empty:
        try:
            df, cap_style, fn_style = rebuild_df_with_style_hierarchy(
                pdf_path, crop_info["page_index"], crop_rect, page.rect.height,
                crop_bbox=crop_bbox, dpi=crop_dpi
            )
            if df is not None and not df.empty:
                # 质量门禁：检查是否发生列挤压、列数过少或低质量
                try:
                    from common import is_table_squeezed, is_table_low_quality
                    if is_table_squeezed(df) or is_table_low_quality(df) or df.shape[1] < 2:
                        logger.debug("[PDF-Tables] rebuild_df_with_style_hierarchy 存在列挤压或低质量，回退到后续策略")
                        df = None
                except Exception:
                    pass
            if df is not None and not df.empty:
                df.attrs['label'] = crop_info.get('label', '')
                df.attrs['table_title'] = cap_style or crop_info.get('caption', '')
                caption = cap_style
                footnote = fn_style
        except Exception as e_style:
            logger.debug(f"[PDF-Tables] rebuild_df_with_style_hierarchy 提示: {e_style}")

    # --- 传统兜底 2: word-level 按列聚类重建 ---
    if df is None or df.empty:
        words = page.get_text("words", clip=crop_rect)
        if words:
            df, caption, footnote = _rebuild_df_from_words(words, crop_rect)

    doc.close()
    return df, caption, footnote


def export_crops_to_excel(
    pdf_path: str,
    output_excel_path: str,
    crops: Optional[List[Dict[str, Any]]] = None,
    pages: Optional[List[int]] = None
) -> bool:
    """
    将 DocLayout-YOLO 检测到的所有表格 Crop 重建并保存至 Excel 文件中。

    修复：先收集所有有效 DataFrame，仅当至少有一个非空 df 时才创建 ExcelWriter，
    避免零 sheet 时 writer.close() 抛 IndexError 并在磁盘残留损坏 xlsx。
    """
    if pd is None:
        print("[PDF-Tables] pandas 未安装，跳过原生 Excel 导出。")
        return False

    if crops is None:
        crops = extract_table_crops_from_pdf(pdf_path, pages=pages)

    if not crops:
        print("[PDF-Tables] 未提取到有效的表格 Crop。")
        return False

    # 先收集有效 df，不触碰磁盘
    valid_tables = []  # list of (df, page_index, table_index)
    for i, crop in enumerate(crops):
        df, caption, footnote = crop_to_dataframe(pdf_path, crop)
        if df is not None and not df.empty:
            # 附加 caption/footnote 到 attrs
            if not df.attrs.get('table_title') and caption:
                df.attrs['table_title'] = caption
            if not df.attrs.get('label') and crop.get('label'):
                df.attrs['label'] = crop['label']
            valid_tables.append((df, crop['page_index'], i))
            print(f"[PDF-Tables] Page {crop['page_index']+1} crop#{i+1} -> DataFrame {df.shape}")
        else:
            print(f"[PDF-Tables] Page {crop['page_index']+1} crop#{i+1} -> 空 DataFrame，跳过")

    if not valid_tables:
        print("[PDF-Tables] 所有 Crop 重建均为空，不生成 Excel 文件。")
        return False

    dfs = [item[0] for item in valid_tables]
    if save_tables_to_excel is not None:
        single_file = output_excel_path.lower().endswith('.xlsx')
        return save_tables_to_excel(dfs, output_excel_path, single_file=single_file)

    # 兜底：若无 save_tables_to_excel，则按原生 writer 导出（若路径为目录则追加 tables.xlsx）
    actual_output = output_excel_path
    if not actual_output.lower().endswith('.xlsx'):
        os.makedirs(actual_output, exist_ok=True)
        actual_output = os.path.join(actual_output, "tables.xlsx")

    try:
        writer = pd.ExcelWriter(actual_output, engine="openpyxl")
        for df, page_idx, crop_idx in valid_tables:
            sheet_name = f"Table_{crop_idx+1}_Page_{page_idx+1}"
            df.to_excel(writer, sheet_name=sheet_name, index=False, header=True)
            print(f"[PDF-Tables] 已将 Page {page_idx+1} 表格导出至 Sheet: {sheet_name}")
        writer.close()
        print(f"[PDF-Tables] 成功保存 {len(valid_tables)} 个表格至: {actual_output}")
        return True
    except Exception as e:
        print(f"[PDF-Tables] Excel 写出失败: {e}")
        if os.path.exists(actual_output) and os.path.isfile(actual_output):
            try:
                os.remove(actual_output)
            except Exception:
                pass
        return False
