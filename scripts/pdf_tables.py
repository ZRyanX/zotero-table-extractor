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
from typing import List, Dict, Any, Optional, Tuple

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
    from .common import load_config
    from .ocr_client import extract_pp_structure_table_crops
    from .pdf_page_filter import page_may_contain_tables
    from .doclayout_yolo_detector import DocLayoutYoloDetector
    from .excel_export import save_tables_to_excel
except ImportError:
    try:
        from common import load_config
        from ocr_client import extract_pp_structure_table_crops
        from pdf_page_filter import page_may_contain_tables
        from doclayout_yolo_detector import DocLayoutYoloDetector
        from excel_export import save_tables_to_excel
    except ImportError:
        load_config = lambda: {}
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
    cancel_event = None
) -> List[Dict[str, Any]]:
    """
    从本地 PDF 文件中提取所有表格及相关上下文（Caption/Footnote）的图像区域 Crop。
    优先使用在线 PP-StructureV3 进行精准版面分析与表格切图；
    若离线或未配置 Token，平滑回退至本地 DocLayout-YOLO 目标检测。
    
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

    # 1. 优先使用 PP-StructureV3 进行在线版面定位与切图
    if config.get("USE_PP_STRUCTURE", True) and extract_pp_structure_table_crops is not None:
        try:
            print("[PDF-Tables] 正在使用 PP-StructureV3 进行在线版面定位与表格切图...")
            pp_crops = extract_pp_structure_table_crops(pdf_path, config=config, cancel_event=cancel_event)
            if pp_crops:
                return pp_crops
        except Exception as e:
            print(f"[PDF-Tables] PP-StructureV3 切图提示: {e}，尝试本地模型回退...")

    # 2. 回退：本地 DocLayout-YOLO 视觉检测
    if DocLayoutYoloDetector is None:
        print("[PDF-Tables] 提示: 未检测到 DocLayoutYoloDetector 依赖，跳过本地 AI 表格定位。")
        return []

    detector = DocLayoutYoloDetector()
    doc = fitz.open(pdf_path)
    extracted_crops = []
    total_pages = len(doc)

    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    for page_idx in range(total_pages):
        if cancel_event is not None and cancel_event.is_set():
            break
        page = doc[page_idx]
        page_text = page.get_text("text")

        # 启发式预检：若页面不含图表特征则提前跳过
        if enable_filter and page_may_contain_tables and not page_may_contain_tables(page_text):
            continue

        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        # 运行 DocLayout-YOLO 表格+上下文检测
        regions = detector.extract_extended_table_regions(img)
        for r in regions:
            r["page_index"] = page_idx
            r["page_text"] = page_text
            extracted_crops.append(r)

    doc.close()
    print(f"[PDF-Tables] 共从 {total_pages} 页 PDF 中定位并提取出 {len(extracted_crops)} 个表格区域 Crop。")
    return extracted_crops


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
        print(f"[PDF-Tables] find_tables() 不可用，回退 word-level: {e}")

    # --- 回退: word-level 按列聚类重建 ---
    if df is None or df.empty:
        words = page.get_text("words", clip=crop_rect)
        if words:
            df, caption, footnote = _rebuild_df_from_words(words, crop_rect)

    doc.close()
    return df, caption, footnote


def export_crops_to_excel(pdf_path: str, output_excel_path: str, crops: Optional[List[Dict[str, Any]]] = None) -> bool:
    """
    将 DocLayout-YOLO 检测到的所有表格 Crop 重建并保存至 Excel 文件中。

    修复：先收集所有有效 DataFrame，仅当至少有一个非空 df 时才创建 ExcelWriter，
    避免零 sheet 时 writer.close() 抛 IndexError 并在磁盘残留损坏 xlsx。
    """
    if pd is None:
        print("[PDF-Tables] pandas 未安装，跳过原生 Excel 导出。")
        return False

    if crops is None:
        crops = extract_table_crops_from_pdf(pdf_path)

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
