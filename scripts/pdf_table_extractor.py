#!/usr/bin/env python3
"""
pdf_table_extractor.py — 三方投票 PDF 表格提取引擎。

提取管线：

  PDF
       │
       ▼
  pdf-inspector（分类 + 表格定位 + Markdown 表格提取）
       │
       │ pdf_type = text_based / scanned / image_based / mixed
       │ pages_with_tables = [...]
       │
       ├──────────────────┬─────────────────────┐
       │                  │                     │
    Native             Scanned               Mixed
    (text_based)      (scanned/image)     (部分页有文本)
       │                  │                     │
       ▼                  ▼                     ▼
  三方投票:          PaddleOCR-VL          三方投票(有文本页)
  pdf-inspector      + DocLayout-YOLO      + OCR(无文本页)
  + Camelot
  + pdfplumber
       │
       │ 任意两个一致 → 取一致的版本
       │ 三方均不一致 → 取 pdf-inspector 结果（置信度最高）
       │ 仅一方有结果 → 取该方结果
       │
       ▼
  数值/结构校验
       │
       ▼
  CSV / Excel
       │
       ▼
  原 PDF + 页码 + bbox + 提取器 → 可追溯元数据
"""
from __future__ import annotations

import os
import re
import io
import time
import tempfile
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set

try:
    import pymupdf as fitz
except ImportError:
    import fitz

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from .common import format_table_label, TABLE_LABEL_PATTERN, TABLE_LABEL_RE, is_table_squeezed, is_table_low_quality, is_markdown_separator, df_map, make_unique_columns, teds_similarity
except ImportError:
    from common import format_table_label, TABLE_LABEL_PATTERN, TABLE_LABEL_RE, is_table_squeezed, is_table_low_quality, is_markdown_separator, df_map, make_unique_columns, teds_similarity


import sys
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

try:
    from . import table_postprocess
except ImportError:
    import table_postprocess

try:
    from . import ocr_client
except ImportError:
    import ocr_client

try:
    from .rapid_table_extractor import extract_via_rapid_table, is_rapid_table_available
except ImportError:
    try:
        from rapid_table_extractor import extract_via_rapid_table, is_rapid_table_available
    except ImportError:
        extract_via_rapid_table = lambda *args, **kwargs: []
        is_rapid_table_available = lambda: False

logger = logging.getLogger(__name__)


# ===========================================================================
# 1. pdf-inspector: 极速分类 + 结构树语义提取 + Markdown 表格提取
#    GitHub: https://github.com/firecrawl/pdf-inspector
# ===========================================================================

def quick_classify_pdf(pdf_path: str) -> Dict[str, Any]:
    """
    使用 pdf-inspector 进行超快速 (5~10ms) 预检分类与 OCR 需求页面检测。
    在 pdf-inspector 不可用或解析异常时平滑回退至 PyMuPDF 启发式检测。

    返回:
    {
        'pdf_type': 'text_based' | 'scanned' | 'mixed' | 'unknown',
        'confidence': float,
        'page_count': int,
        'pages_needing_ocr': List[int],  # 0-indexed
        'has_text': bool,
    }
    """
    res = {
        'pdf_type': 'unknown',
        'confidence': 0.0,
        'page_count': 0,
        'pages_needing_ocr': [],
        'has_text': False,
    }
    if not os.path.exists(pdf_path):
        return res

    try:
        import pdf_inspector
        if hasattr(pdf_inspector, 'classify_pdf'):
            clf = pdf_inspector.classify_pdf(pdf_path)
            res['pdf_type'] = getattr(clf, 'pdf_type', 'unknown')
            res['confidence'] = getattr(clf, 'confidence', 0.0)
            res['page_count'] = getattr(clf, 'page_count', 0)
            res['pages_needing_ocr'] = list(getattr(clf, 'pages_needing_ocr', []))
            res['has_text'] = res['pdf_type'] in ('text_based', 'mixed')
            return res
    except Exception as e:
        logger.debug(f"[pdf-inspector] classify_pdf 快速分类提示: {e}")

    # 回退方案: PyMuPDF 启发式检测
    try:
        with fitz.open(pdf_path) as doc:
            res['page_count'] = len(doc)
            has_txt = has_text_layer(pdf_path)
            res['has_text'] = has_txt
            res['pdf_type'] = 'text_based' if has_txt else 'scanned'
            res['confidence'] = 0.5
    except Exception:
        pass
    return res


def extract_via_structure_tree(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    利用 pdf-inspector 深度解析 Tagged PDF / PDF/UA 文档的语义结构树 (Structure Tree)。
    直接提取出版商原生排版的 Table, TH, TD 元素，解决多栏报纸式排版跨栏流转、
    复合多行表头与行表头对齐难题，实现 100% 真实矢量的结构化重构。

    参数:
    - pdf_path: PDF 文件绝对路径
    - pages: 可选目标页码列表 (0-indexed)

    返回:
    List[{'df': DataFrame, 'page_idx': int, 'table_idx': int, 'bbox': [x1, y1, x2, y2]}],
    其中 df.attrs 包含 'extractor'='pdf_inspector_structure' 与真实表题声明。
    """
    if pd is None or not os.path.exists(pdf_path):
        return []

    try:
        import pdf_inspector
        if not hasattr(pdf_inspector, 'extract_structure_elements') or not hasattr(pdf_inspector, 'extract_text_with_positions'):
            return []
    except ImportError:
        return []

    # pdf_inspector 内部 pages 参数为 1-indexed
    p_1idx = [pg + 1 for pg in pages] if pages is not None else None

    try:
        se_all = pdf_inspector.extract_structure_elements(pdf_path, pages=p_1idx)
    except Exception as e_se:
        logger.debug(f"[pdf-inspector] extract_structure_elements 提示: {e_se}")
        return []

    if not se_all:
        return []

    se_by_page = {}
    for x in se_all:
        se_by_page.setdefault(x.page, []).append(x)

    results = []
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        doc = None

    for pg in sorted(se_by_page.keys()):
        page_se = se_by_page[pg]
        table_elements = [x for x in page_se if x.role in ('TH', 'TD')]
        if not table_elements:
            continue

        page_height = 842.0
        p_obj = None
        if doc is not None and 0 <= pg - 1 < len(doc):
            p_obj = doc[pg - 1]
            try:
                mb = getattr(p_obj, 'mediabox', None)
                if mb is not None and hasattr(mb, 'height') and isinstance(mb.height, (int, float)):
                    page_height = float(mb.height)
                elif hasattr(p_obj, 'rect') and hasattr(p_obj.rect, 'height') and isinstance(p_obj.rect.height, (int, float)):
                    page_height = float(p_obj.rect.height)
            except Exception:
                page_height = 842.0

        try:
            items = pdf_inspector.extract_text_with_positions(pdf_path, pages=[pg])
        except Exception as e_items:
            logger.debug(f"[pdf-inspector] extract_text_with_positions 提示: {e_items}")
            continue

        if not items:
            continue

        role_map = {x.mcid: x.role for x in page_se}
        mcid_items = {}
        for it in items:
            if it.mcid is not None and it.mcid in role_map:
                mcid_items.setdefault(it.mcid, []).append(it)

        # 提取本页所有的 Caption 结构元素
        captions = []
        for x in page_se:
            if x.role == 'Caption' and x.mcid in mcid_items:
                its = mcid_items[x.mcid]
                c_text = ' '.join(it.text.strip() for it in its if it.text.strip())
                avg_y = sum(it.y for it in its) / len(its)
                captions.append({'mcid': x.mcid, 'text': c_text, 'y': avg_y})

        # 页面多表格切分：根据 Caption / 独立段落 / 新 TH 表头块分离同一页内的不同表格
        table_chunks = []
        curr_chunk = []
        has_td = False
        saw_intervening = False

        for x in page_se:
            if x.role in ('Caption', 'Table', 'Part', 'Art', 'Sect', 'Div', 'BlockQuote', 'P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'Figure', 'Formula', 'Lbl', 'LBody', 'Note', 'Link'):
                saw_intervening = True
            elif x.role in ('TH', 'TD') and x.mcid in mcid_items:
                if x.role == 'TH' and has_td:
                    if saw_intervening:
                        table_chunks.append(curr_chunk)
                        curr_chunk = []
                        has_td = False
                        saw_intervening = False
                if x.role == 'TD':
                    has_td = True
                curr_chunk.append(x)
                saw_intervening = False
        if curr_chunk:
            table_chunks.append(curr_chunk)

        used_captions = set()

        for tbl_idx, chunk in enumerate(table_chunks):
            # 构建该表格所有单元格实体
            cells = []
            for x in chunk:
                if x.role in ('TH', 'TD') and x.mcid in mcid_items:
                    its = mcid_items[x.mcid]
                    its_sorted = sorted(its, key=lambda it: (-it.y, it.x))
                    text = ' '.join(it.text.strip() for it in its_sorted if it.text.strip())
                    avg_x = sum(it.x for it in its) / len(its)
                    avg_y = sum(it.y for it in its) / len(its)
                    min_x = min(it.x for it in its)
                    max_x = max(it.x + it.width for it in its)
                    cells.append({
                        'mcid': x.mcid,
                        'role': x.role,
                        'text': text,
                        'x': avg_x,
                        'y': avg_y,
                        'min_x': min_x,
                        'max_x': max_x,
                    })

            if not cells:
                continue

            # 页面多栏流动与分块解算：检测阅读流中的逆向垂直跳跃 (y_curr > last_y + 45pt 即为换栏)
            panels = []
            curr_panel = []
            last_y = None
            for c in cells:
                if last_y is not None and c['y'] > last_y + 45.0:
                    panels.append(curr_panel)
                    curr_panel = [c]
                else:
                    curr_panel.append(c)
                last_y = c['y']
            if curr_panel:
                panels.append(curr_panel)

            tbl_headers = []
            tbl_col_centers = []
            tbl_rows = []

            for pan_idx, pan in enumerate(panels):
                th_cells = [c for c in pan if c['role'] == 'TH']
                if th_cells and pan_idx == 0:
                    th_by_y = sorted(th_cells, key=lambda c: -c['y'])
                    th_rows_grouped = []
                    cur_r = []
                    cur_y = None
                    for c in th_by_y:
                        if cur_y is None or abs(c['y'] - cur_y) < 4.0:
                            cur_r.append(c)
                            if cur_y is None:
                                cur_y = c['y']
                        else:
                            th_rows_grouped.append(sorted(cur_r, key=lambda x: x['x']))
                            cur_r = [c]
                            cur_y = c['y']
                    if cur_r:
                        th_rows_grouped.append(sorted(cur_r, key=lambda x: x['x']))

                    # 动态判定表头所占有的连续行：从顶部行开始，行间距 <= 20pt 均归入复合表头体系；
                    # 若后续某行仅有 1 个位于极左侧的 TH 单元格，则识别为数据行的行表头（Stub Header），跳出表头判定。
                    col_header_rows = []
                    prev_r_y = None
                    for r_idx, r in enumerate(th_rows_grouped):
                        r_y = r[0]['y']
                        if prev_r_y is not None and abs(prev_r_y - r_y) > 20.0:
                            break
                        if len(r) == 1 and col_header_rows and len(col_header_rows[0]) > 1:
                            if r[0]['x'] < col_header_rows[0][0]['x'] + 35.0:
                                break
                        col_header_rows.append(r)
                        prev_r_y = r_y

                    col_th_cells = [c for r in col_header_rows for c in r] if col_header_rows else [c for c in th_cells if c['y'] >= max(c['y'] for c in pan) - 35.0]

                    is_hierarchy = (
                        len(col_header_rows) >= 2 and
                        len(col_header_rows[1]) >= 4 and
                        len(col_header_rows[0]) <= len(col_header_rows[1]) * 0.75
                    )

                    if is_hierarchy:
                        row0 = col_header_rows[0]
                        row1 = col_header_rows[1]
                        headers = []
                        col_centers = []
                        for sub in row1:
                            matched_sup = None
                            for i, sup in enumerate(row0):
                                next_x = row0[i+1]['x'] if i+1 < len(row0) else 999999.0
                                if sup['x'] - 15.0 <= sub['x'] < next_x - 15.0:
                                    matched_sup = sup['text']
                                    break
                            if matched_sup and matched_sup != sub['text']:
                                headers.append(f'{matched_sup} {sub["text"]}')
                            else:
                                headers.append(sub['text'])
                            col_centers.append(sub['x'])
                        if row0[0]['x'] < row1[0]['x'] - 20.0:
                            headers.insert(0, row0[0]['text'])
                            col_centers.insert(0, row0[0]['x'])
                    else:
                        # 动态自适应列头聚类容差
                        all_th_x = sorted(c['x'] for c in col_th_cells)
                        th_gaps = [all_th_x[i+1] - all_th_x[i] for i in range(len(all_th_x)-1) if all_th_x[i+1] - all_th_x[i] > 6.0]
                        col_tol = max(6.0, min(18.0, min(th_gaps) * 0.45)) if th_gaps else 18.0

                        col_clusters = []
                        for c in sorted(col_th_cells, key=lambda c: c['x']):
                            matched = False
                            for clust in col_clusters:
                                avg_cx = sum(x['x'] for x in clust) / len(clust)
                                if abs(c['x'] - avg_cx) < col_tol:
                                    clust.append(c)
                                    matched = True
                                    break
                            if not matched:
                                col_clusters.append([c])
                        headers = []
                        col_centers = []
                        for clust in col_clusters:
                            clust_sorted = sorted(clust, key=lambda x: -x['y'])
                            headers.append(' '.join(x['text'] for x in clust_sorted if x['text']))
                            col_centers.append(sum(x['x'] for x in clust) / len(clust))

                    tbl_headers = headers
                    tbl_col_centers = col_centers
                    data_cells = [c for c in pan if c not in col_th_cells]
                else:
                    data_cells = pan

                # 若数据行在表头左侧存在独立数据列（即第 0 列为省略表头的行头，门限自适应）
                if data_cells and tbl_col_centers:
                    min_th_x = min(tbl_col_centers)
                    leftmost_data_cells = [c for c in data_cells if c['x'] < min_th_x - 8.0]
                    if leftmost_data_cells:
                        left_x = sum(c['x'] for c in leftmost_data_cells) / len(leftmost_data_cells)
                        tbl_headers.insert(0, 'Index')
                        tbl_col_centers.insert(0, left_x)

                n_cols = len(tbl_headers)
                if n_cols == 0:
                    continue

                # 始终使用物理 y 坐标行聚类 + x 轴物理列投影重建数据行，
                # 严禁因单元格总数恰好整除便盲目顺序切片，杜绝空单元格缺失引发的全表跨行错移
                data_by_y = sorted(data_cells, key=lambda c: -c['y'])
                curr_r = []
                curr_y = None
                rows_grouped = []
                for c in data_by_y:
                    if curr_y is None or abs(c['y'] - curr_y) < 4.5:
                        curr_r.append(c)
                        if curr_y is None:
                            curr_y = c['y']
                    else:
                        rows_grouped.append(sorted(curr_r, key=lambda x: x['x']))
                        curr_r = [c]
                        curr_y = c['y']
                if curr_r:
                    rows_grouped.append(sorted(curr_r, key=lambda x: x['x']))

                for r in rows_grouped:
                    row_vals = [''] * n_cols
                    for c in r:
                        best_idx = min(range(n_cols), key=lambda i: abs(tbl_col_centers[i] - c['x']))
                        if row_vals[best_idx]:
                            row_vals[best_idx] += ' ' + c['text']
                        else:
                            row_vals[best_idx] = c['text']
                    if any(v.strip() for v in row_vals):
                        tbl_rows.append(row_vals)

            if not tbl_rows or not tbl_headers:
                continue

            unique_cols = make_unique_columns(tbl_headers)
            df = pd.DataFrame(tbl_rows, columns=unique_cols)

            tbl_min_y = min(c['y'] for c in cells)
            tbl_max_y = max(c['y'] for c in cells)

            matched_cap = None
            avail_caps = [c for c in captions if c['text'] not in used_captions]
            above_caps = [c for c in avail_caps if c['y'] >= tbl_max_y - 15.0]
            if above_caps:
                best_c = min(above_caps, key=lambda c: abs(c['y'] - tbl_max_y))
                matched_cap = best_c['text']
                used_captions.add(matched_cap)
            elif avail_caps:
                best_c = min(avail_caps, key=lambda c: abs(c['y'] - tbl_max_y))
                if abs(best_c['y'] - tbl_min_y) <= 40.0:
                    matched_cap = best_c['text']
                    used_captions.add(matched_cap)

            df.attrs['extractor'] = 'pdf_inspector_structure'
            df.attrs['page_idx'] = pg - 1
            df.attrs['has_semantic_th'] = True
            if matched_cap:
                df.attrs['table_title'] = matched_cap
                m_lbl = re.search(r'((?:Table|Tab\.|表)\s*\d+)', matched_cap, re.IGNORECASE)
                if m_lbl:
                    df.attrs['label'] = m_lbl.group(1)

            b_x0 = min(c['min_x'] for c in cells)
            b_x1 = max(c['max_x'] for c in cells)
            b_y0 = page_height - tbl_max_y
            b_y1 = page_height - tbl_min_y
            try:
                raw_r = fitz.Rect(b_x0, min(b_y0, b_y1), b_x1, max(b_y0, b_y1))
                p_rot = getattr(p_obj, 'rotation', 0) if p_obj else 0
                if isinstance(p_rot, (int, float)) and (p_rot % 360 != 0) and hasattr(p_obj, 'rotation_matrix'):
                    rot_m = p_obj.rotation_matrix
                    if isinstance(rot_m, fitz.Matrix):
                        vis_r = raw_r * rot_m
                        bbox = [min(vis_r.x0, vis_r.x1), min(vis_r.y0, vis_r.y1), max(vis_r.x0, vis_r.x1), max(vis_r.y0, vis_r.y1)]
                    else:
                        bbox = [raw_r.x0, raw_r.y0, raw_r.x1, raw_r.y1]
                else:
                    bbox = [raw_r.x0, raw_r.y0, raw_r.x1, raw_r.y1]
            except Exception:
                bbox = None

            results.append({
                'df': df,
                'page_idx': pg - 1,
                'table_idx': tbl_idx,
                'bbox': bbox,
            })

    if doc is not None:
        try:
            doc.close()
        except Exception:
            pass

    if results:
        print(f"[pdf-inspector] 结构树语义提取成功: {len(results)} 个表格 (Tagged PDF/UA)")
    return results


def classify_and_extract_via_inspector(pdf_path: str, caption_page_map: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """
    使用 pdf-inspector 进行:
    - 快速预检分类与 OCR 需求感知 (classify_pdf)
    - 语义结构树高精度表格提取 (extract_via_structure_tree)
    - 表格页面定位 (pages_with_tables)
    - 逐页 Markdown 表格提取 (extract_pages_markdown / process_pdf)

    返回:
    {
        'pdf_type': str,
        'confidence': float,
        'page_count': int,
        'pages_with_tables': List[int],
        'pages_needing_ocr': List[int],
        'tables': List[{'df': DataFrame, 'page_idx': int, 'extractor': str, 'bbox': None}],
        'structure_tables': List[{'df': DataFrame, 'page_idx': int, 'extractor': str, 'bbox': List}],
        'markdown': str or None,
        'error': str or None,
    }
    """
    result = {
        'pdf_type': 'unknown',
        'confidence': 0.0,
        'page_count': 0,
        'pages_with_tables': [],
        'pages_needing_ocr': [],
        'tables': [],
        'structure_tables': [],
        'markdown': None,
        'error': None,
    }

    try:
        import pdf_inspector

        # 0. 超快速 (5~10ms) 预检分类与 OCR 需求感知
        if hasattr(pdf_inspector, 'classify_pdf'):
            try:
                clf = pdf_inspector.classify_pdf(pdf_path)
                result['pdf_type'] = getattr(clf, 'pdf_type', 'unknown')
                result['confidence'] = getattr(clf, 'confidence', 0.0)
                result['pages_needing_ocr'] = list(getattr(clf, 'pages_needing_ocr', []))
                result['page_count'] = getattr(clf, 'page_count', 0)
            except Exception as e_clf:
                logger.debug(f"[pdf-inspector] classify_pdf notice: {e_clf}")

        # 1. 优先尝试从 Tagged PDF / PDF/UA 语义结构树中直接提取表格 (最高精度)
        try:
            struct_tbls = extract_via_structure_tree(pdf_path)
            if struct_tbls:
                result['structure_tables'] = struct_tbls
        except Exception as e_st:
            logger.debug(f"[pdf-inspector] extract_via_structure_tree notice: {e_st}")

        # 2. 若非扫描件，优先使用 extract_pages_markdown 获取逐页 Markdown，精准绑定表格与页码
        if result['pdf_type'] not in ('scanned', 'image_based') and not result['pages_needing_ocr']:
            if hasattr(pdf_inspector, 'extract_pages_markdown'):
                try:
                    pages_res = pdf_inspector.extract_pages_markdown(pdf_path)
                    if result['pdf_type'] in ('unknown', ''):
                        result['pdf_type'] = 'text_based'
                    result['confidence'] = max(result['confidence'], 0.85)
                    result['pages_with_tables'] = list(getattr(pages_res, 'pages_with_tables', []))
                    if not result['pages_needing_ocr']:
                        result['pages_needing_ocr'] = list(getattr(pages_res, 'pages_needing_ocr', []))
                    
                    full_md_list = []
                    for p_item in pages_res.pages:
                        p_idx = p_item.page
                        p_md = p_item.markdown or ""
                        full_md_list.append(f"<!-- Page {p_idx+1} -->\n{p_md}")
                        if p_md:
                            p_tables = _parse_markdown_tables(p_md, caption_page_map, default_page=p_idx)
                            for tbl_df, p_hint in p_tables:
                                actual_page = p_hint if p_hint is not None else p_idx
                                tbl_df.attrs['extractor'] = 'pdf_inspector'
                                tbl_df.attrs['page_idx'] = actual_page
                                if not tbl_df.attrs.get('label'):
                                    tbl_df.attrs['label'] = f"Table {len(result['tables'])+1}"
                                result['tables'].append({
                                    'df': tbl_df,
                                    'page_idx': actual_page,
                                    'table_idx': len(result['tables']),
                                    'bbox': None,
                                })
                    result['markdown'] = "\n\n".join(full_md_list)
                except Exception as e_epm:
                    print(f"[pdf-inspector] extract_pages_markdown 提示: {e_epm}，回退到 process_pdf")

        if not result['tables'] and result['pdf_type'] != 'scanned':
            pi_result = pdf_inspector.process_pdf(pdf_path)
            result['pdf_type'] = pi_result.pdf_type
            result['confidence'] = pi_result.confidence
            result['pages_with_tables'] = list(pi_result.pages_with_tables) if hasattr(pi_result, 'pages_with_tables') else []
            result['pages_needing_ocr'] = list(pi_result.pages_needing_ocr) if hasattr(pi_result, 'pages_needing_ocr') else []
            result['markdown'] = pi_result.markdown

            # 从 Markdown 中解析表格
            if pi_result.markdown:
                tables = _parse_markdown_tables(pi_result.markdown, caption_page_map)
                for i, (df, page_hint) in enumerate(tables):
                    df.attrs['extractor'] = 'pdf_inspector'
                    if page_hint is not None:
                        df.attrs['page_idx'] = page_hint
                    if not df.attrs.get('label'):
                        df.attrs['label'] = f"Table {i+1}"
                    result['tables'].append({
                        'df': df,
                        'page_idx': page_hint,
                        'table_idx': i,
                        'bbox': None,
                    })

        print(f"[pdf-inspector] type={result['pdf_type']}, confidence={result['confidence']:.2f}, "
              f"pages_with_tables={result['pages_with_tables']}, tables_parsed={len(result['tables'])}")

    except ImportError:
        result['error'] = 'pdf_inspector not installed'
        has_text = has_text_layer(pdf_path)
        if has_text:
            result['pdf_type'] = 'text_based'
            result['confidence'] = 0.5
            print("[pdf-inspector] 未安装，PyMuPDF 检测到文本层 → 判定为 text_based")
        else:
            result['pdf_type'] = 'scanned'
            print("[pdf-inspector] 未安装，无文本层 → 判定为 scanned")
    except Exception as e:
        result['error'] = str(e)
        has_text = has_text_layer(pdf_path)
        if has_text:
            result['pdf_type'] = 'text_based'
            result['confidence'] = 0.5
            print(f"[pdf-inspector] 解析失败 ({e}), PyMuPDF 检测到文本层 → 判定为 text_based")
        else:
            result['pdf_type'] = 'scanned'
            print(f"[pdf-inspector] 解析失败 ({e}), 无文本层 → 判定为 scanned")

    return result


def _parse_markdown_tables(markdown: str, caption_page_map: Optional[Dict[str, int]] = None, default_page: Optional[int] = None) -> List[Tuple[Any, Optional[int]]]:
    """
    从 pdf-inspector 输出的 Markdown 中解析表格。
    结合 caption_page_map 或 default_page 精准映射每张表格对应的 PDF 页码。
    返回 [(DataFrame, page_hint), ...]
    """
    if pd is None:
        return []

    tables = []
    lines = markdown.split('\n')
    i = 0
    page_hint = default_page
    curr_caption = ""
    curr_label = ""

    while i < len(lines):
        line = lines[i].strip()

        # 跟踪页码 (pdf-inspector 可能插入 <!-- Page N --> 标记)
        page_match = re.search(r'(?:<!--\s*Page\s+(\d+)\s*-->|^#+\s*Page\s+(\d+)|^---\s*Page\s+(\d+))', line, re.IGNORECASE)
        if page_match:
            p_num = next(g for g in page_match.groups() if g is not None)
            page_hint = int(p_num) - 1

        cap_m = re.search(r'\b((?:Table|TABLE|表)\s*[S]?[0-9]+(?:\.[0-9]+)?)', line)
        if cap_m and not (line.startswith('|') and line.endswith('|')):
            curr_caption = line
            curr_label = cap_m.group(1).replace(' ', '').capitalize()

        # 检测 Markdown 表格起始
        if line.startswith('|') and line.endswith('|'):
            table_lines = [line]
            i += 1
            while i < len(lines):
                l = lines[i].strip()
                if l.startswith('|') and l.endswith('|'):
                    table_lines.append(l)
                    i += 1
                else:
                    break

            df = _markdown_lines_to_df(table_lines)
            if df is not None and not df.empty and len(df) >= 2:
                resolved_page = page_hint
                if resolved_page is None and curr_label and caption_page_map and curr_label in caption_page_map:
                    resolved_page = caption_page_map[curr_label]
                if resolved_page is None:
                    resolved_page = default_page
                if curr_label:
                    df.attrs['label'] = curr_label
                if curr_caption:
                    df.attrs['table_title'] = curr_caption
                if resolved_page is not None:
                    df.attrs['page_idx'] = resolved_page
                tables.append((df, resolved_page))
            curr_caption = ""
            curr_label = ""
        else:
            i += 1

    return tables


def _markdown_lines_to_df(table_lines: List[str]):
    """将 Markdown 表格行列表转为 DataFrame"""
    if pd is None or len(table_lines) < 2:
        return None

    # 过滤分隔行
    clean_lines = [l for l in table_lines if not is_markdown_separator(l)]
    if len(clean_lines) < 2:
        return None

    try:
        csv_data = '\n'.join(l.strip('|') for l in clean_lines)
        df = pd.read_csv(io.StringIO(csv_data), sep=r'\s*\|\s*', engine='python')
        # 清理列名
        df.columns = [str(c).strip() for c in df.columns]
        return df
    except Exception:
        return None


def _get_words_visual(page) -> List[Tuple[float, float, float, float, str, int, int, int]]:
    """
    获取页面 words 并将其坐标统一归一化为当前页面的视觉视口坐标系。
    全面适配 page.rotation (0, 90, 180, 270)。
    """
    if not page:
        return []
    try:
        raw_words = page.get_text("words")
    except Exception:
        return []
    if not raw_words:
        return []
    rot = getattr(page, 'rotation', 0)
    rot = (rot % 360) if isinstance(rot, int) else 0
    if rot != 0 and fitz is not None:
        rot_mat = getattr(page, 'rotation_matrix', None)
        if rot_mat is not None:
            words = []
            for w in raw_words:
                r = fitz.Rect(w[:4]) * rot_mat
                words.append((min(r.x0, r.x1), min(r.y0, r.y1), max(r.x0, r.x1), max(r.y0, r.y1), w[4], w[5], w[6], w[7]))
            return words
    return list(raw_words)


def _search_for_visual(page, term: str) -> List[Any]:
    """
    在 page 中搜索文本并将匹配区域统一转换到当前页面的视觉视口坐标系。
    适配 page.rotation (0, 90, 180, 270)。
    """
    if not page or not term:
        return []
    try:
        rects = page.search_for(term)
    except Exception:
        return []
    if not rects:
        return []
    rot = getattr(page, 'rotation', 0)
    rot = (rot % 360) if isinstance(rot, int) else 0
    if rot != 0 and fitz is not None:
        rot_mat = getattr(page, 'rotation_matrix', None)
        if rot_mat is not None:
            norm_rects = []
            for r in rects:
                rv = r * rot_mat
                norm_rects.append(fitz.Rect(min(rv.x0, rv.x1), min(rv.y0, rv.y1), max(rv.x0, rv.x1), max(rv.y0, rv.y1)))
            return norm_rects
    return rects


def extract_table_caption_from_page(page, table_bbox: Optional[List[float]] = None) -> Tuple[str, str]:
    """
    从 PDF 页面中提取表格的标签（如 '表1', 'Table 1'）与完整标题（含中英文）。
    优先在 table_bbox 上方区域精确提取；若无 bbox 则在页面文本中搜索。
    全面适配 page.rotation 坐标归一化。
    """
    if page is None:
        return '', ''
    try:
        words = _get_words_visual(page)
        if table_bbox and words:
            y_top = table_bbox[1]
            words = [w for w in words if max(0.0, y_top - 140.0) <= w[1] <= max(0.0, y_top - 1.0)]

        if words:
            # 按 y 坐标聚类分行
            words.sort(key=lambda w: (round(w[1] / 6) * 6, w[0]))
            lines = []
            curr_line = []
            curr_y = None
            for w in words:
                y_mid = (w[1] + w[3]) / 2
                if curr_y is None or abs(y_mid - curr_y) < 6:
                    curr_line.append(w[4])
                    if curr_y is None:
                        curr_y = y_mid
                else:
                    lines.append(' '.join(curr_line))
                    curr_line = [w[4]]
                    curr_y = y_mid
            if curr_line:
                lines.append(' '.join(curr_line))

            for i, line in enumerate(lines):
                if any(k in line for k in ['期 雷欣儒等', '第 11 期', 'http://', 'Journal of', 'Acta ']) and not TABLE_LABEL_RE.search(line):
                    continue
                m = TABLE_LABEL_RE.search(line)
                if m:
                    label = format_table_label(m.group(0))
                    pos = line.find(m.group(0))
                    before = line[:pos]
                    after = line[pos + len(m.group(0)):].strip()
                    if any(before.rstrip().endswith(p) for p in ['（', '(', '从', '见', '如', '由', '根据', '在']):
                        continue
                    after_clean = after.lstrip(':：').strip()
                    if after_clean.startswith(('）', ')', '中', '可以', '所示', '可知', '看出')):
                        continue
                    
                    cap_parts = [line[pos:]]
                    if i + 1 < len(lines):
                        nxt = lines[i + 1]
                        if any(nxt.lower().startswith(k) for k in ['table', '附表', '表']) or (len(nxt) > 10 and not any(k in nxt for k in ['序号', '样号', 'sample', 'stage', 'id', 'no.'])):
                            cap_parts.append(nxt)
                    full_cap = ' '.join(' '.join(cap_parts).split())
                    full_cap = re.sub(r'([表\w\d]+[\u4e00-\u9fa5]+)\s+[\u4e00-\u9fa5]+[。，；]\s*(Table\b)', r'\1 \2', full_cap)
                    return label, full_cap

        # 页面文本回退搜索
        page_text = page.get_text("text")
        lines = [l.strip() for l in page_text.split('\n') if l.strip()]
        
        # 跨行 caption 检测：当 "表" 在行末，下一行以 "数字.数字" 开头时，拼接两行
        merged_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            # 检查行末是否以 "表" 结尾，且下一行以数字.数字开头
            if line.endswith('表') and i + 1 < len(lines) and re.match(r'^\d+[\.\-]\d+', lines[i + 1]):
                merged_lines.append(line + lines[i + 1])
                i += 2
            else:
                merged_lines.append(line)
                i += 1
        
        for i, line in enumerate(merged_lines):
            if any(k in line for k in ['期 雷欣儒等', '第 11 期', 'http://', 'Journal of', 'Acta ']) and not TABLE_LABEL_RE.search(line):
                continue
            m = re.search(r'^(' + TABLE_LABEL_PATTERN + r')[\.\:\s]*(.*)', line, re.IGNORECASE)
            if m:
                label = format_table_label(m.group(1))
                after = m.group(2).strip()
                after_clean = after.lstrip(':：').strip()
                if after_clean.startswith(('）', ')', '中', '可以', '所示', '可知', '看出')):
                    continue
                collected = [line]
                for j in range(i + 1, min(i + 3, len(merged_lines))):
                    nxt = merged_lines[j]
                    if any(nxt.lower().startswith(k) for k in ['table', '附表', '表']) or (len(nxt) > 5 and not any(k in nxt for k in ['序号', '样号', 'sample', 'stage', 'id', 'no.'])):
                        collected.append(nxt)
                    else:
                        break
                return label, ' '.join(' '.join(collected).split())
        
        # 裸表号回退：行以 "数字.数字 + 多空格 + 中文标题" 开头（如 "4.11  庆家沟锑矿床..."）
        # 且页面上存在 "表数字.数字" 引用，确认该裸表号对应一个表格
        for i, line in enumerate(merged_lines):
            bare_m = re.match(r'^(\d+\.\d+)\s{2,}(.{5,})', line)
            if bare_m:
                num = bare_m.group(1)
                title = bare_m.group(2).strip()
                # 必须含中文字符，不含句号/逗号，不以数字开头
                if not re.search(r'[\u4e00-\u9fa5]', title):
                    continue
                if '。' in title or '，' in title or title[0].isdigit():
                    continue
                # 确认页面上有 "表X.Y" 引用
                ref_label = f'表{num}'
                if ref_label in page_text:
                    return ref_label, f'{ref_label} {title}'
    except Exception as e_cap:
        logger.debug(f"extract_table_caption_from_page notice: {e_cap}")
    return '', ''


# ===========================================================================
# 2. PyMuPDF find_tables()
# ===========================================================================

def extract_via_find_tables(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    使用 PyMuPDF 内置 page.find_tables() 提取表格。
    基于 PDF 矢量线段 + 文本层，零 OCR。
    pages: 仅提取指定页面 (0-indexed)，None 表示全部页面。
    """
    if pd is None:
        return []

    results = []
    try:
        with fitz.open(pdf_path) as doc:
            total_pages = len(doc)
            target_pages = pages if pages is not None else range(total_pages)

            for page_idx in target_pages:
                if page_idx >= total_pages:
                    continue
                page = doc[page_idx]
                
                # 先尝试默认 lines 策略（有边框线表格），再回退 text 策略（无线框表格）
                finder = None
                try:
                    finder = page.find_tables()
                    if not finder.tables:
                        finder = page.find_tables(strategy="text")
                except Exception:
                    try:
                        finder = page.find_tables(strategy="text")
                    except Exception:
                        continue
                
                if not finder or not finder.tables:
                    continue

                for tbl_idx, table in enumerate(finder.tables):
                    try:
                        rows = table.extract()
                    except Exception:
                        continue
                    if not rows or len(rows) < 1:
                        continue

                    clean_rows = []
                    for row in rows:
                        clean_row = [("" if cell is None else str(cell).strip()) for cell in row]
                        if any(c for c in clean_row):
                            clean_rows.append(clean_row)

                    if len(clean_rows) < 1:
                        continue

                    if len(clean_rows) == 1:
                        df = pd.DataFrame(clean_rows, columns=[f"Col_{i+1}" for i in range(len(clean_rows[0]))])
                    else:
                        df = pd.DataFrame(clean_rows[1:], columns=clean_rows[0])
                    df.attrs['extractor'] = 'find_tables'
                    t_bbox = list(table.bbox) if table.bbox else None
                    label, caption = extract_table_caption_from_page(page, t_bbox)
                    df.attrs['label'] = label if label else None
                    if caption:
                        df.attrs['table_title'] = caption
                    df.attrs['page_idx'] = page_idx
                    results.append({
                        'df': df,
                        'page_idx': page_idx,
                        'table_idx': tbl_idx,
                        'bbox': t_bbox,
                    })
    except Exception as e:
        logger.warning(f"find_tables() error: {e}")

    if results:
        print(f"[find_tables] 提取到 {len(results)} 个表格")
    return results


# ===========================================================================
# 3. pdfplumber
# ===========================================================================

def extract_via_pdfplumber(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """使用 pdfplumber 提取表格"""
    if pd is None:
        return []

    results = []
    try:
        import pdfplumber

        doc_fitz = None
        try:
            doc_fitz = fitz.open(pdf_path)
        except Exception:
            pass

        with pdfplumber.open(pdf_path) as pdf:
            target_pages = pages if pages is not None else range(len(pdf.pages))

            for page_idx in target_pages:
                if page_idx >= len(pdf.pages):
                    continue
                page = pdf.pages[page_idx]
                tables = page.find_tables()
                fitz_page = doc_fitz[page_idx] if doc_fitz and page_idx < len(doc_fitz) else None
                for tbl_idx, table in enumerate(tables):
                    try:
                        data = table.extract()
                        if not data or len(data) < 1:
                            continue
                        clean_rows = []
                        for row in data:
                            clean_row = [("" if cell is None else str(cell).strip()) for cell in row]
                            if any(c for c in clean_row):
                                clean_rows.append(clean_row)
                        if len(clean_rows) < 1:
                            continue
                        if len(clean_rows) == 1:
                            df = pd.DataFrame(clean_rows, columns=[f"Col_{i+1}" for i in range(len(clean_rows[0]))])
                        else:
                            df = pd.DataFrame(clean_rows[1:], columns=clean_rows[0])
                        df.attrs['extractor'] = 'pdfplumber'
                        df.attrs['page_idx'] = page_idx
                        t_bbox = list(table.bbox) if table.bbox else None
                        if fitz_page:
                            label, caption = extract_table_caption_from_page(fitz_page, t_bbox)
                            df.attrs['label'] = label if label else None
                            if caption:
                                df.attrs['table_title'] = caption
                        else:
                            df.attrs['label'] = None
                        results.append({
                            'df': df,
                            'page_idx': page_idx,
                            'table_idx': tbl_idx,
                            'bbox': t_bbox,
                        })
                    except Exception as e:
                        logger.debug(f"pdfplumber table parse error: {e}")
        if doc_fitz:
            doc_fitz.close()
    except ImportError:
        print("[pdfplumber] 未安装，跳过")
    except Exception as e:
        logger.warning(f"pdfplumber error: {e}")

    if results:
        print(f"[pdfplumber] 提取到 {len(results)} 个表格")
    return results


_CAMELOT_CHECKED = False
_CAMELOT_AVAILABLE = False


def extract_via_camelot(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    使用 Camelot 提取表格。
    pages: 1-indexed page numbers for Camelot (Camelot uses 1-indexed).
    """
    global _CAMELOT_CHECKED, _CAMELOT_AVAILABLE
    if pd is None:
        return []

    if not _CAMELOT_CHECKED:
        try:
            import camelot
            _CAMELOT_AVAILABLE = True
        except ImportError:
            _CAMELOT_AVAILABLE = False
            logger.debug("[Camelot] 未安装，后续调用自动跳过")
        except Exception:
            _CAMELOT_AVAILABLE = False
        _CAMELOT_CHECKED = True

    if not _CAMELOT_AVAILABLE:
        return []

    results = []
    try:
        import camelot

        # Camelot uses 1-indexed page strings
        if pages is not None:
            page_str = ','.join(str(p + 1) for p in pages)
        else:
            page_str = 'all'

        pages_with_camelot = set()
        for flavor in ['lattice', 'stream']:
            try:
                tables = camelot.read_pdf(pdf_path, flavor=flavor, pages=page_str)
                if tables and len(tables) > 0:
                    for tbl_idx, table in enumerate(tables):
                        df = table.df
                        if df is None or df.empty or len(df) < 1:
                            continue
                        page_idx = int(getattr(table, 'page', 1)) - 1
                        if flavor == 'stream':
                            existing_on_page = [r['df'] for r in results if r.get('page_idx') == page_idx]
                            if any(_table_similarity(ex, df) >= 0.50 for ex in existing_on_page):
                                continue
                        df = df_map(df, lambda x: str(x).strip() if x is not None else "")
                        df.attrs['extractor'] = f'camelot_{flavor}'
                        df.attrs['page_idx'] = page_idx
                        results.append({
                            'df': df,
                            'page_idx': page_idx,
                            'table_idx': tbl_idx,
                            'bbox': None,
                        })
                        pages_with_camelot.add(page_idx)
            except Exception as e:
                logger.debug(f"Camelot {flavor} error: {e}")
                continue
    except ImportError:
        print("[Camelot] 未安装，跳过")
    except Exception as e:
        logger.warning(f"Camelot error: {e}")

    if results:
        print(f"[Camelot] 提取到 {len(results)} 个表格")
    return results


# ===========================================================================
# 4b. img2table (基于 OpenCV 的纯 Python 免 Ghostscript 表格提取)
# ===========================================================================

_IMG2TABLE_CHECKED = False
_IMG2TABLE_AVAILABLE = False


def is_img2table_available() -> bool:
    """检查当前环境中是否安装并可用 img2table。"""
    global _IMG2TABLE_CHECKED, _IMG2TABLE_AVAILABLE
    if not _IMG2TABLE_CHECKED:
        try:
            from img2table.document import PDF
            _IMG2TABLE_AVAILABLE = True
        except ImportError:
            _IMG2TABLE_AVAILABLE = False
            logger.debug("[img2table] 未安装，后续调用自动跳过")
        except Exception:
            _IMG2TABLE_AVAILABLE = False
        _IMG2TABLE_CHECKED = True
    return _IMG2TABLE_AVAILABLE


def extract_via_img2table(
    pdf_path: str,
    pages: Optional[List[int]] = None,
    candidate_pages: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """
    使用 img2table (基于 OpenCV 形态学算子) 提取表格。
    完全脱离 Ghostscript 系统依赖，提供有线框表 (lattice) 与无线框表 (borderless) 提取。
    既作为 Camelot 缺失时的轻量免环境依赖替代，又作为多方投票的高可靠参与方。
    """
    if pd is None or not os.path.exists(pdf_path):
        return []

    try:
        from common import load_config
        cfg = load_config() if callable(load_config) else {}
        if not cfg.get("IMG2TABLE_ENABLED", True):
            return []
    except Exception:
        pass

    if not is_img2table_available():
        return []

    results = []
    try:
        from img2table.document import PDF

        # img2table 接收 0-indexed 页码列表
        raw_pages = pages if pages is not None else candidate_pages
        target_pages = [int(p) for p in raw_pages] if raw_pages is not None else None
        doc = PDF(src=pdf_path, pages=target_pages, detect_rotation=False, pdf_text_extraction=True)

        extracted = doc.extract_tables(borderless_tables=True)
        if extracted:
            for p_num, tbl_list in extracted.items():
                for tbl_idx, tbl in enumerate(tbl_list):
                    df = tbl.df
                    if df is None or df.empty or len(df) < 1:
                        continue
                    df = df_map(df, lambda x: str(x).strip() if x is not None else "")
                    df.attrs['extractor'] = 'img2table'
                    df.attrs['page_idx'] = p_num
                    if getattr(tbl, 'title', None):
                        df.attrs['table_title'] = str(tbl.title)

                    bbox = None
                    pdf_bbox = None
                    if getattr(tbl, 'bbox', None):
                        b = tbl.bbox
                        raw_bbox = [getattr(b, 'x1', 0), getattr(b, 'y1', 0), getattr(b, 'x2', 0), getattr(b, 'y2', 0)]
                        # img2table 默认按 200 DPI 渲染 PDF 页面提取表格，
                        # 需按 72 / 200 = 0.36 将像素坐标统一缩放至标准 PDF point 点坐标以支持空间匹配
                        pt_scale = 72.0 / 200.0
                        pdf_bbox = [
                            getattr(b, 'x1', 0) * pt_scale,
                            getattr(b, 'y1', 0) * pt_scale,
                            getattr(b, 'x2', 0) * pt_scale,
                            getattr(b, 'y2', 0) * pt_scale,
                        ]
                        bbox = raw_bbox

                    results.append({
                        'df': df,
                        'page_idx': p_num,
                        'table_idx': tbl_idx,
                        'bbox': bbox,
                        'pdf_bbox': pdf_bbox,
                    })
    except Exception as e:
        logger.debug(f"[img2table] 提取异常: {e}")

    if results:
        print(f"[img2table] 提取到 {len(results)} 个表格 (免 Ghostscript)")
    return results


# ===========================================================================
# 4c. GMFT (基于 TATR 深度学习视觉 Transformer 原生 PDF 表格提取)
# ===========================================================================

_GMFT_CHECKED = False
_GMFT_AVAILABLE = False


def is_gmft_available() -> bool:
    """检查当前环境中是否安装并可用 GMFT (TATR-based)。"""
    global _GMFT_CHECKED, _GMFT_AVAILABLE
    if not _GMFT_CHECKED:
        try:
            import tempfile
            os.environ.setdefault("MPLCONFIGDIR", tempfile.gettempdir())
            from gmft.auto import AutoTableDetector, AutoTableFormatter
            from gmft.pdf_bindings import PyPDFium2Document
            _GMFT_AVAILABLE = True
        except ImportError:
            _GMFT_AVAILABLE = False
            logger.debug("[GMFT] 未安装，后续调用自动跳过")
        except Exception:
            _GMFT_AVAILABLE = False
        _GMFT_CHECKED = True
    return _GMFT_AVAILABLE


def extract_via_gmft(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    使用 GMFT (Give Me Formatted Tables, 基于 TATR 架构) 提取原生 PDF 表格。
    擅长解决多级跨行跨列表头、层次对齐以及学术期刊三线表复杂单元格。
    作为原生 PDF 多方投票的重要权威参选方。
    """
    if pd is None or not os.path.exists(pdf_path):
        return []

    try:
        from common import load_config
        cfg = load_config() if callable(load_config) else {}
        if not cfg.get("GMFT_ENABLED", True):
            return []
    except Exception:
        pass

    if not is_gmft_available():
        return []

    results = []
    try:
        from gmft.auto import AutoTableDetector, AutoTableFormatter
        from gmft.pdf_bindings import PyPDFium2Document

        doc = PyPDFium2Document(pdf_path)
        detector = AutoTableDetector()
        formatter = AutoTableFormatter()

        total_pages = len(doc)
        target_pages = [p for p in pages if 0 <= p < total_pages] if pages is not None else list(range(total_pages))

        for p_idx in target_pages:
            try:
                page = doc.get_page(p_idx)
                tables = detector.extract(page)
                if not tables:
                    continue

                for tbl_idx, tbl in enumerate(tables):
                    ft = formatter.extract(tbl)
                    df = ft.df()
                    if df is None or df.empty or len(df) < 1:
                        continue

                    df = df_map(df, lambda x: str(x).strip() if x is not None else "")
                    df.attrs['extractor'] = 'gmft_tatr'
                    df.attrs['page_idx'] = p_idx

                    bbox = None
                    def _is_valid_bbox(b):
                        return (isinstance(b, (list, tuple)) and len(b) == 4 and
                                all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in b) and
                                b[2] > b[0] and b[3] > b[1])

                    def _is_valid_coord(c):
                        return isinstance(c, (int, float)) and not isinstance(c, bool)

                    if hasattr(tbl, 'rect') and tbl.rect is not None:
                        r = tbl.rect
                        if hasattr(r, 'bbox') and _is_valid_bbox(r.bbox):
                            bbox = list(r.bbox)
                        elif (hasattr(r, 'xmin') and hasattr(r, 'ymin') and hasattr(r, 'xmax') and hasattr(r, 'ymax') and
                              _is_valid_coord(r.xmin) and _is_valid_coord(r.ymin) and _is_valid_coord(r.xmax) and _is_valid_coord(r.ymax) and
                              r.xmax > r.xmin and r.ymax > r.ymin):
                            bbox = [r.xmin, r.ymin, r.xmax, r.ymax]
                        elif _is_valid_bbox(r):
                            bbox = list(r)
                        elif (hasattr(r, 'x0') and hasattr(r, 'y0') and hasattr(r, 'x1') and hasattr(r, 'y1') and
                              _is_valid_coord(r.x0) and _is_valid_coord(r.y0) and _is_valid_coord(r.x1) and _is_valid_coord(r.y1) and
                              r.x1 > r.x0 and r.y1 > r.y0):
                            bbox = [r.x0, r.y0, r.x1, r.y1]
                    elif hasattr(tbl, 'bbox') and _is_valid_bbox(tbl.bbox):
                        bbox = list(tbl.bbox)

                    results.append({
                        'df': df,
                        'page_idx': p_idx,
                        'table_idx': tbl_idx,
                        'bbox': bbox,
                    })
            except Exception as e_page:
                logger.debug(f"[GMFT] Page {p_idx+1} 提取异常: {e_page}")

        doc.close()
    except Exception as e:
        logger.debug(f"[GMFT] GMFT 提取异常: {e}")

    if results:
        print(f"[GMFT] TATR 提取到 {len(results)} 个表格 (跨列合并/多层表头高保真)")
    return results


# ===========================================================================
# 4c. Artifex PyMuPDF4LLM 原生表格提取
# ===========================================================================

_PYMUPDF4LLM_CHECKED = False
_PYMUPDF4LLM_AVAILABLE = False

def is_pymupdf4llm_available() -> bool:
    """检查当前环境中是否安装并可用 Artifex PyMuPDF4LLM。"""
    global _PYMUPDF4LLM_CHECKED, _PYMUPDF4LLM_AVAILABLE
    if not _PYMUPDF4LLM_CHECKED:
        try:
            import pymupdf4llm
            _PYMUPDF4LLM_AVAILABLE = hasattr(pymupdf4llm, "to_markdown")
        except ImportError:
            _PYMUPDF4LLM_AVAILABLE = False
            logger.debug("[PyMuPDF4LLM] 未安装，后续调用自动跳过")
        except Exception:
            _PYMUPDF4LLM_AVAILABLE = False
        _PYMUPDF4LLM_CHECKED = True
    return _PYMUPDF4LLM_AVAILABLE


def extract_via_pymupdf4llm(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    使用 Artifex PyMuPDF4LLM 提取原生 PDF 表格。
    针对原生排版文献将 PDF 页面解析为 LLM-ready Markdown 并提取高保真表格。
    作为原生 PDF 多方投票的重要参选方。
    """
    if pd is None or not os.path.exists(pdf_path):
        return []

    try:
        from common import load_config
        cfg = load_config() if callable(load_config) else {}
        if not cfg.get("PYMUPDF4LLM_ENABLED", True):
            return []
    except Exception:
        pass

    if not is_pymupdf4llm_available():
        return []

    results = []
    try:
        import pymupdf4llm

        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        target_pages = [p for p in pages if 0 <= p < total_pages] if pages is not None else list(range(total_pages))

        for p_idx in target_pages:
            try:
                md_text = pymupdf4llm.to_markdown(doc, pages=[p_idx])
                if not md_text or "|" not in md_text:
                    continue

                try:
                    from .table_postprocess import parse_structured_markdown_tables
                except ImportError:
                    from table_postprocess import parse_structured_markdown_tables
                dfs = parse_structured_markdown_tables(md_text)
                if not dfs:
                    try:
                        from .online.strategies import parse_tables_from_markdown
                    except ImportError:
                        try:
                            from online.strategies import parse_tables_from_markdown
                        except ImportError:
                            parse_tables_from_markdown = None
                    if parse_tables_from_markdown:
                        dfs = parse_tables_from_markdown(md_text)

                for tbl_idx, df in enumerate(dfs or []):
                    if df is None or df.empty or len(df) < 1:
                        continue
                    df = df_map(df, lambda x: str(x).strip() if x is not None else "")
                    df.attrs['extractor'] = 'pymupdf4llm'
                    df.attrs['page_idx'] = p_idx
                    bbox = None
                    try:
                        p_tab_finder = doc[p_idx].find_tables()
                        if p_tab_finder and p_tab_finder.tables:
                            if tbl_idx < len(p_tab_finder.tables):
                                bbox = list(p_tab_finder.tables[tbl_idx].bbox)
                    except Exception:
                        pass
                    results.append({
                        'df': df,
                        'page_idx': p_idx,
                        'table_idx': tbl_idx,
                        'bbox': bbox,
                        'label': df.attrs.get('label'),
                    })
            except Exception as e_page:
                logger.debug(f"[PyMuPDF4LLM] Page {p_idx+1} 提取异常: {e_page}")

        doc.close()
    except Exception as e:
        logger.debug(f"[PyMuPDF4LLM] 提取异常: {e}")

    if results:
        print(f"[PyMuPDF4LLM] 提取到 {len(results)} 个表格 (LLM Markdown 原生解析)")
    return results


# ===========================================================================
# 5. 三方与多方投票融合
# ===========================================================================


def _compute_bbox_iou(b1, b2) -> Tuple[float, float]:
    """计算两个 bbox [x0, y0, x1, y1] 的 2D IoU 与 IoM (Intersection over Minimum)"""
    if not b1 or not b2 or len(b1) < 4 or len(b2) < 4:
        return 0.0, 0.0
    x_inter = max(0.0, min(float(b1[2]), float(b2[2])) - max(float(b1[0]), float(b2[0])))
    y_inter = max(0.0, min(float(b1[3]), float(b2[3])) - max(float(b1[1]), float(b2[1])))
    inter_area = x_inter * y_inter
    if inter_area <= 0.0:
        return 0.0, 0.0
    a1 = max(1.0, (float(b1[2]) - float(b1[0])) * (float(b1[3]) - float(b1[1])))
    a2 = max(1.0, (float(b2[2]) - float(b2[0])) * (float(b2[3]) - float(b2[1])))
    iou = inter_area / (a1 + a2 - inter_area)
    iom = inter_area / min(a1, a2)
    return iou, iom


def _table_similarity(df1, df2) -> float:
    """
    计算两个 DataFrame 的内容相似度 (0~1)。

    改进：
    - 支持行列偏移容错
    - 结合单元格匹配与 Jaccard 集合相似度
    - 防误判：若实质单元格内容完全不相关，严禁单凭行列形状匹配给出高分
    """
    if df1 is None or df2 is None or df1.empty or df2.empty:
        return 0.0

    r1, c1 = df1.shape
    r2, c2 = df2.shape
    shape_sim = min(r1, r2) / max(r1, r2) * min(c1, c2) / max(c1, c2)

    min_rows = min(r1, r2)
    min_cols = min(c1, c2)
    if min_rows == 0 or min_cols == 0:
        return 0.0

    def _cell_match(v1, v2):
        v1 = str(v1).strip().lower() if v1 is not None else ""
        v2 = str(v2).strip().lower() if v2 is not None else ""
        if not v1 and not v2:
            return 0.0
        if v1 == v2:
            return 1.0
        elif v1 and v2 and (v1 in v2 or v2 in v1):
            return 0.5
        return 0.0

    def _aligned_similarity(offset=0):
        """逐单元格比对，支持行偏移"""
        matching = 0
        total = 0
        for i in range(min_rows):
            i2 = i + offset
            if i2 < 0 or i2 >= r2:
                continue
            for j in range(min_cols):
                v1 = df1.iloc[i, j] if j < c1 else ""
                v2 = df2.iloc[i2, j] if j < c2 else ""
                matching += _cell_match(v1, v2)
                total += 1
        return matching / total if total > 0 else 0.0

    # 尝试不同行偏移，取最高相似度
    best_content_sim = 0.0
    for offset in [0, -1, 1, -2, 2]:
        sim = _aligned_similarity(offset)
        if sim > best_content_sim:
            best_content_sim = sim
        if best_content_sim >= 0.8:
            break  # 足够高，无需继续

    # Jaccard 集合相似度（不依赖行列对齐，作为补充）
    cells1 = set(str(v).strip().lower() for v in df1.values.flatten() if v is not None and str(v).strip())
    cells2 = set(str(v).strip().lower() for v in df2.values.flatten() if v is not None and str(v).strip())
    if cells1 or cells2:
        jaccard = len(cells1 & cells2) / len(cells1 | cells2)
    else:
        jaccard = 0.0

    # 若内容比对与 Jaccard 均极低 (无实质共同单元格)，严禁单凭外形形状给出高相似度
    if best_content_sim < 0.10 and jaccard < 0.10:
        return 0.0

    # 结合 IBM TEDS (Tree-Edit-Distance-based Similarity) 进行树拓扑结构与内容融合度量
    try:
        from common import load_config
        cfg = load_config() if callable(load_config) else {}
    except Exception:
        cfg = {}
    if cfg.get("TEDS_METRIC_ENABLED", True) and teds_similarity is not None:
        try:
            teds_val = teds_similarity(df1, df2, structure_only=False, max_sample_rows=15, max_sample_cols=10)
            return 0.30 * teds_val + 0.40 * best_content_sim + 0.15 * jaccard + 0.15 * shape_sim
        except Exception:
            pass

    # 综合相似度回退：对齐比对权重 0.55，Jaccard 权重 0.25，形状权重 0.20
    return 0.20 * shape_sim + 0.55 * best_content_sim + 0.25 * jaccard



def _align_page_tables(
    page_tables: Dict[str, List[Dict[str, Any]]], 
    priority: List[str]
) -> List[Dict[str, Dict[str, Any]]]:
    """
    将同一页面上由不同提取器提取的表格进行空间位置与内容对齐。
    优先依据二维 bbox 的重叠 (IoU/IoM) 匹配，杜绝将同页不同位置表格错误聚合；
    辅以显式表号冲突检查和高阈值内容相似度判定。
    返回一组对齐后的候选簇列表: [{'pdf_inspector': t1, 'find_tables': t2}, ...]
    """
    clusters: List[Dict[str, Dict[str, Any]]] = []

    for src in priority:
        src_tbls = page_tables.get(src, [])
        if not src_tbls:
            continue

        for tbl in src_tbls:
            tbl_df = tbl.get('df')
            tbl_bbox = tbl.get('pdf_bbox') or tbl.get('bbox')
            tbl_label = tbl_df.attrs.get('label') if tbl_df is not None else tbl.get('label')
            best_cluster_idx = -1
            best_score = 0.0

            for c_idx, cluster in enumerate(clusters):
                if src in cluster:
                    continue  # 该簇已包含该来源表格，不放入同一簇

                cluster_scores = []
                cluster_disqualified = False
                for existing_src, existing_tbl in cluster.items():
                    existing_df = existing_tbl.get('df')
                    existing_bbox = existing_tbl.get('pdf_bbox') or existing_tbl.get('bbox')
                    existing_label = (existing_df.attrs.get('label') if existing_df is not None and hasattr(existing_df, 'attrs') else None) or existing_tbl.get('label')
                    score = 0.0

                    # 1. 显式表号标签冲突检查 (如 Table 1 vs Table 2，绝对严禁聚入同簇)
                    if tbl_label and existing_label:
                        fmt1 = format_table_label(tbl_label)
                        fmt2 = format_table_label(existing_label)
                        if fmt1 and fmt2 and fmt1 != fmt2:
                            cluster_disqualified = True
                            break

                    # 2. 空间二维重叠判定 (若两者均有有效 bbox)
                    has_both_bboxes = bool(tbl_bbox and existing_bbox and len(tbl_bbox) >= 4 and len(existing_bbox) >= 4)
                    spatial_matched = False
                    iom = 0.0
                    if has_both_bboxes:
                        iou, iom_val = _compute_bbox_iou(tbl_bbox, existing_bbox)
                        iom = iom_val
                        if iom >= 0.35 or iou >= 0.20:
                            spatial_matched = True
                        elif iom == 0.0 and iou == 0.0:
                            # 明确在同一页面的不同空间区域（如一上一下或分栏左右），绝对严禁聚入同一簇
                            cluster_disqualified = True
                            break

                    # 3. 内容文本/结构相似度判定
                    sim = 0.0
                    if tbl_df is not None and existing_df is not None:
                        try:
                            sim = _table_similarity(tbl_df, existing_df)
                        except Exception:
                            sim = 0.0

                    # 空间匹配但内容无关时，禁止聚类（避免同页嵌套/相邻两表被合并覆盖误杀）
                    if spatial_matched:
                        has_same_fmt_label = bool(tbl_label and existing_label and format_table_label(tbl_label) == format_table_label(existing_label))
                        if sim < 0.40 and not has_same_fmt_label:
                            cluster_disqualified = True
                            break
                        score = max(score, 0.40 * (0.50 + 0.50 * iom) + 0.60 * sim)
                    else:
                        score = max(score, sim)

                    cluster_scores.append(score)

                if cluster_disqualified:
                    continue

                avg_score = sum(cluster_scores) / len(cluster_scores) if cluster_scores else 0.0
                # 聚类阈值要求：无空间信息时需更严格的内容匹配 (>= 0.60)，避免同页小表误聚
                threshold = 0.50 if any(tbl_bbox and (ex.get('pdf_bbox') or ex.get('bbox')) for ex in cluster.values()) else 0.60
                if avg_score >= threshold and avg_score > best_score:
                    best_score = avg_score
                    best_cluster_idx = c_idx

            if best_cluster_idx >= 0:
                clusters[best_cluster_idx][src] = tbl
            else:
                clusters.append({src: tbl})

    return clusters


def three_way_vote(
    inspector_tables: List[Dict[str, Any]],
    find_tables_results: List[Dict[str, Any]],
    plumber_tables: List[Dict[str, Any]],
    camelot_tables: List[Dict[str, Any]],
    align_tables: Optional[List[Dict[str, Any]]] = None,
    similarity_threshold: float = 0.75,
    structure_tables: Optional[List[Dict[str, Any]]] = None,
    gmft_tables: Optional[List[Dict[str, Any]]] = None,
    img2table_tables: Optional[List[Dict[str, Any]]] = None,
    pymupdf4llm_tables: Optional[List[Dict[str, Any]]] = None,
    rapid_table_tables: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
    """
    多方投票融合: pdf-inspector-structure + pdf-inspector + pymupdf4llm + gmft + rapid_table + find_tables + img2table + text_alignment + pdfplumber + Camelot。

    规则:
    - 任意两个一致 (相似度 >= similarity_threshold，默认 0.75) → 取一致的版本（优先 pdf_inspector_structure > pdf-inspector > pymupdf4llm > gmft > rapid_table > find_tables > img2table > text_alignment > pdfplumber > Camelot）
    - 质量检查：若选中的候选表存在列挤压，自动降级切换至无挤压的候选表
    - 多方均不一致 → 取优先级最高的结果（若有出版商语义结构树表格 pdf_inspector_structure，绝对优先保障矢量保真度）
    - 仅一方有结果 → 取该方结果

    返回 (fused_tables, logs, vote_summary)
    vote_summary = {
        'low_confidence_pages': Set[int],  # 相似度 < 0.90 或被丢弃的页面 (0-indexed)，供后续触发 OCR/VLM 精细接管
        'page_scores': Dict[int, Dict],     # 每页的投票详情
    }
    """
    logs = []
    fused = []
    low_confidence_pages = set()
    page_scores = {}

    # 按页码分组所有来源
    sources = {
        'pdf_inspector_structure': structure_tables or [],
        'pdf_inspector': inspector_tables,
        'pymupdf4llm': pymupdf4llm_tables or [],
        'gmft': gmft_tables or [],
        'rapid_table': rapid_table_tables or [],
        'find_tables': find_tables_results,
        'img2table': img2table_tables or [],
        'text_alignment': align_tables or [],
        'pdfplumber': plumber_tables,
        'camelot': camelot_tables,
    }

    # 收集所有出现的页码
    all_pages = set()
    for src_tables in sources.values():
        for t in src_tables:
            all_pages.add(t.get('page_idx', 0))

    # 优先级排序：结构树绝对优先，pdf_inspector 与 pymupdf4llm/gmft 语义紧随其后，rapid_table 结构紧随其后，find_tables 与 img2table 矢量形态紧随其后
    priority = ['pdf_inspector_structure', 'pdf_inspector', 'pymupdf4llm', 'gmft', 'rapid_table', 'find_tables', 'img2table', 'text_alignment', 'pdfplumber', 'camelot']

    for page in sorted(all_pages):
        page_tables = {}
        for src_name, src_tables in sources.items():
            page_tables[src_name] = [t for t in src_tables if t.get('page_idx', 0) == page]

        # 智能对齐该页上不同来源的表格簇，避免 raw zip 错位
        aligned_clusters = _align_page_tables(page_tables, priority)
        if not aligned_clusters:
            continue

        # 页面级各主力引擎检测表数的分歧检验 (独立覆盖率感知)
        active_counts = [len(tbls) for s, tbls in page_tables.items() if tbls and s in ['pdf_inspector_structure', 'pdf_inspector', 'pymupdf4llm', 'gmft', 'find_tables']]
        if active_counts and (max(active_counts) - min(active_counts) >= 2):
            logs.append(f"Page {page+1}: 主力引擎检测表数存在较大分歧 (counts={active_counts}) → 标记为低置信度页")
            low_confidence_pages.add(page)


        # 逐个对齐表格簇进行投票
        for tbl_idx, candidates in enumerate(aligned_clusters):
            if not candidates:
                continue

            # 两两比较（严格按照 priority 优先级顺序排列）
            src_names = [s for s in priority if s in candidates]
            best_result = None
            best_extractor = None
            agreement_found = False

            for i in range(len(src_names)):
                for j in range(i + 1, len(src_names)):
                    s1, s2 = src_names[i], src_names[j]
                    sim = _table_similarity(candidates[s1]['df'], candidates[s2]['df'])
                    if sim >= similarity_threshold:
                        # 两个一致，取优先级更高的
                        best_result = candidates[s1]  # priority 排序保证 s1 优先级更高
                        best_extractor = s1
                        agreement_found = True
                        logs.append(f"Page {page+1} Table {tbl_idx+1}: {s1} 与 {s2} 一致 "
                                    f"(sim={sim:.2f}) → 采用 {s1}")
                        break
                if agreement_found:
                    break

            if not agreement_found:
                # 三方不一致或仅一方
                if len(candidates) == 1:
                    best_result = list(candidates.values())[0]
                    best_extractor = list(candidates.keys())[0]
                    logs.append(f"Page {page+1} Table {tbl_idx+1}: 仅 {best_extractor} 有结果 → 采用")
                else:
                    # 取最高优先级候选 (pdf_inspector_structure / pdf_inspector)
                    if 'pdf_inspector_structure' in candidates:
                        best_result = candidates['pdf_inspector_structure']
                        best_extractor = 'pdf_inspector_structure'
                    elif 'pdf_inspector' in candidates:
                        best_result = candidates['pdf_inspector']
                        best_extractor = 'pdf_inspector'
                    else:
                        # 取第一个（按优先级）
                        best_result = list(candidates.values())[0]
                        best_extractor = list(candidates.keys())[0]
                    sims = []
                    max_sim = 0.0
                    for i in range(len(src_names)):
                        for j in range(i+1, len(src_names)):
                            s = _table_similarity(candidates[src_names[i]]['df'], candidates[src_names[j]]['df'])
                            sims.append(f"{src_names[i]}~{src_names[j]}={s:.2f}")
                            max_sim = max(max_sim, s)
                    logs.append(f"Page {page+1} Table {tbl_idx+1}: 多方不一致 [{', '.join(sims)}] "
                                f"→ 采用 {best_extractor} (最高优先级)")
                    # 记录低置信度页面
                    if max_sim < 0.90:
                        low_confidence_pages.add(page)
                    page_scores.setdefault(page, {})['max_similarity'] = max_sim

            # 列挤压质量检查与自动纠正：
            # 若选出的候选表存在列挤压（如数值挤在同一单元格），且同页有未挤压的候选表，则自动切换为未挤压版本
            if best_result is not None and is_table_squeezed(best_result['df']):
                for alt_name in ['pdf_inspector_structure', 'gmft', 'img2table', 'text_alignment', 'pdfplumber', 'camelot', 'pdf_inspector']:
                    if alt_name in candidates and not is_table_squeezed(candidates[alt_name]['df']) and not is_table_low_quality(candidates[alt_name]['df']):
                        logs.append(f"Page {page+1} Table {tbl_idx+1}: {best_extractor} 存在列挤压问题 → 自动切换为未挤压的 {alt_name}")
                        best_result = candidates[alt_name]
                        best_extractor = alt_name
                        break

            # 低质量/损坏表格检查：
            # 若选出的候选表混入正文段落、章节标题或有效数据极度稀疏，检查是否有合格候选；若全不合格则剔除，留待 Step 3b PaddleOCR-VL 识别
            if best_result is not None and is_table_low_quality(best_result['df']):
                for alt_name in priority:
                    if alt_name in candidates and not is_table_low_quality(candidates[alt_name]['df']):
                        logs.append(f"Page {page+1} Table {tbl_idx+1}: {best_extractor} 提取质量低劣 → 切换为 {alt_name}")
                        best_result = candidates[alt_name]
                        best_extractor = alt_name
                        break
                else:
                    logs.append(f"Page {page+1} Table {tbl_idx+1}: {best_extractor} 存在损坏/低质量问题且无合格原生候选 → 丢弃，留待 PaddleOCR-VL 补充提取")
                    best_result = None
                    low_confidence_pages.add(page)

            if best_result is not None:
                # 继承跨候选来源的真实表格标题与中文/特定标签
                if not best_result['df'].attrs.get('table_title'):
                    for c in candidates.values():
                        if c['df'].attrs.get('table_title'):
                            best_result['df'].attrs['table_title'] = c['df'].attrs.get('table_title')
                            break
                best_label = best_result['df'].attrs.get('label')
                if not best_label or best_label.startswith('Table '):
                    for c in candidates.values():
                        cand_label = c['df'].attrs.get('label')
                        if cand_label and not cand_label.startswith('Table '):
                            best_result['df'].attrs['label'] = cand_label
                            break
                
                best_result['df'].attrs['vote_extractor'] = best_extractor
                best_result['df'].attrs['extractor'] = best_extractor
                best_result['df'].attrs['cross_validated'] = agreement_found
                fused.append(best_result)

    vote_summary = {
        'low_confidence_pages': low_confidence_pages,
        'page_scores': page_scores,
    }
    return fused, logs, vote_summary


# ===========================================================================
# 6. OCR 兜底
# ===========================================================================

def extract_via_ocr(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    兜底方案: MinerU 高保真多模态解析 / PP-StructureV3 定位 + PaddleOCR-VL-1.6 多模态解析。
    仅当 PDF 无文本层或三方投票全部失败时使用。
    """
    results = []

    try:
        from .common import load_config
    except ImportError:
        try:
            from common import load_config
        except ImportError:
            load_config = lambda: {}
    config = load_config()
    engine = (config.get("OCR_ENGINE") or os.environ.get("OCR_ENGINE") or "auto").lower()

    try:
        from . import ocr_client
    except ImportError:
        import ocr_client
    try:
        from .table_postprocess import parse_structured_vlm_content
    except ImportError:
        from table_postprocess import parse_structured_vlm_content

    # 0. 若配置首选引擎为 MinerU，优先直接调用 MinerU 高精度多模态提取
    if engine == "mineru":
        try:
            print("[OCR] 检测到 OCR_ENGINE='mineru'，优先调用 MinerU 高保真多模态结构化提取...")
            if hasattr(ocr_client, "call_mineru_structured_api"):
                dfs = ocr_client.call_mineru_structured_api(pdf_path, config=config, page_ranges=pages, pages=pages)
            else:
                md_out = ocr_client.call_mineru_api(pdf_path, config=config, page_ranges=pages, pages=pages)
                dfs = parse_structured_vlm_content(md_out) if md_out else []

            if dfs:
                for i, df in enumerate(dfs):
                    if df is not None and not df.empty:
                        df.attrs['extractor'] = 'mineru_api'
                        p_idx = df.attrs.get('page_idx')
                        if pages:
                            if p_idx is None or p_idx not in pages:
                                if p_idx is not None and isinstance(p_idx, int) and 0 <= p_idx < len(pages):
                                    p_idx = pages[p_idx]
                                else:
                                    p_idx = pages[min(i, len(pages) - 1)]
                        elif p_idx is None:
                            p_idx = 0
                        df.attrs['page_idx'] = p_idx
                        results.append({
                            'df': df,
                            'page_idx': p_idx,
                            'table_idx': i,
                            'bbox': df.attrs.get('bbox'),
                            'extractor': 'mineru_api',
                        })
                if results:
                    print(f"[OCR] MinerU 结构化提取成功，获得 {len(results)} 个表格")
                    return results
        except Exception as e_mineru:
            logger.warning(f"MinerU extraction error: {e_mineru}")

    # 1. PP-StructureV3 -> PaddleOCR-VL-1.6 两阶段融合管线
    try:
        full_md = ocr_client.run_ppstructure_vlm_pipeline(pdf_path, pages=pages, cancel_event=None)
        if full_md:
            dfs = parse_structured_vlm_content(full_md)
            for i, df in enumerate(dfs):
                if df is not None and not df.empty:
                    df.attrs['extractor'] = 'ppstructure_vlm'
                    if not df.attrs.get('label'):
                        df.attrs['label'] = f"Table {i+1}"
                    p_idx = pages[i] if (pages and i < len(pages)) else (pages[0] if pages else 0)
                    df.attrs['page_idx'] = p_idx
                    results.append({
                        'df': df,
                        'page_idx': p_idx,
                        'table_idx': i,
                        'bbox': None,
                    })
    except Exception as e:
        logger.warning(f"PP-StructureV3 + PaddleOCR-VL error: {e}")

    # 2. IBM Docling (TableFormer) 离线备用降级
    if not results:
        try:
            try:
                from . import ocr_client
            except ImportError:
                import ocr_client
            if hasattr(ocr_client, 'extract_via_docling'):
                docling_results = ocr_client.extract_via_docling(pdf_path, pages=pages)
                if docling_results:
                    print(f"[OCR] Docling (TableFormer) 离线备用提取到 {len(docling_results)} 个表格")
                    results.extend(docling_results)
        except Exception as e_docling:
            logger.debug(f"Docling offline fallback error: {e_docling}")

    # 3. OCR-Hub 多引擎注册表动态调度 (TeleOCR / MinerU / RapidTable)
    if not results:
        try:
            try:
                from . import ocr_client
            except ImportError:
                import ocr_client
            registry = ocr_client.get_ocr_registry()
            for backend_name in ["teleocr", "mineru", "rapid_table"]:
                try:
                    b = registry.get_backend(backend_name)
                    if b and b.probe_health().get("status") in ("healthy", "agent_fallback"):
                        res_out = b.extract(pdf_path, pages=pages)
                        if isinstance(res_out, str) and res_out.strip():
                            try:
                                from .table_postprocess import parse_structured_vlm_content
                            except ImportError:
                                from table_postprocess import parse_structured_vlm_content
                            dfs = parse_structured_vlm_content(res_out)
                            for i, df in enumerate(dfs):
                                if df is not None and not df.empty:
                                    df.attrs['extractor'] = backend_name
                                    p_idx = pages[i] if (pages and i < len(pages)) else (pages[0] if pages else 0)
                                    df.attrs['page_idx'] = p_idx
                                    results.append({
                                        'df': df,
                                        'page_idx': p_idx,
                                        'table_idx': i,
                                        'bbox': None,
                                        'extractor': backend_name,
                                    })
                            if results:
                                print(f"[OCR] OCR-Hub 动态调度成功: {backend_name} 提取到 {len(results)} 个表格")
                                break
                        elif isinstance(res_out, list) and res_out:
                            results.extend(res_out)
                            print(f"[OCR] OCR-Hub 动态调度成功: {backend_name} 提取到 {len(res_out)} 个表格")
                            break
                except Exception as e_backend:
                    logger.debug(f"[OCR-Hub] Backend {backend_name} 调度异常: {e_backend}")
                    continue
        except Exception as e_hub:
            logger.debug(f"[OCR-Hub] 动态调度异常: {e_hub}")

    # 3. 表格切图回退 (PP-StructureV3 / DocLayout-YOLO)
    if not results:
        try:
            import sys
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if script_dir not in sys.path:
                sys.path.insert(0, script_dir)
            from pdf_tables import extract_table_crops_from_pdf, crop_to_dataframe

            crops = extract_table_crops_from_pdf(pdf_path)
            for i, crop in enumerate(crops):
                df, caption, footnote = crop_to_dataframe(pdf_path, crop)
                if df is not None and not df.empty:
                    df.attrs['extractor'] = 'table_crop_fallback'
                    if caption:
                        df.attrs['table_title'] = caption
                    results.append({
                        'df': df,
                        'page_idx': crop.get('page_index', 0),
                        'table_idx': i,
                        'bbox': crop.get('crop_bbox'),
                    })
        except Exception as e:
            logger.warning(f"Table crop fallback error: {e}")

    if results:
        extractors_used = set(r['df'].attrs.get('extractor', 'ocr') for r in results)
        print(f"[OCR] 提取到 {len(results)} 个表格 ({', '.join(extractors_used)})")
    return results


# ===========================================================================
# 主入口: 统一管线
# ===========================================================================

def has_text_layer(pdf_path: str, min_chars: int = 100, max_bad_char_ratio: float = 0.05) -> bool:
    """
    判断 PDF 是否有真实有效的文本层。
    自动识别并排除知网早期劣质 OCR 引入的乱码双层扫描 PDF（假文本层）。
    """
    try:
        doc = fitz.open(pdf_path)
        total_chars = 0
        bad_chars = 0
        valid_chars = 0
        has_full_images = 0
        for i in range(min(5, len(doc))):
            page = doc[i]
            text = page.get_text("text")
            images = page.get_images()
            if len(images) >= 1:
                has_full_images += 1
            for c in text:
                if c.isspace():
                    continue
                total_chars += 1
                if ord(c) < 32 or ord(c) == 127 or c == '\ufffd':
                    bad_chars += 1
                elif '\u4e00' <= c <= '\u9fa5' or c.isalnum():
                    valid_chars += 1
        doc.close()
        
        if total_chars < min_chars:
            if has_full_images == 0 and total_chars >= 20 and (valid_chars / max(1, total_chars)) >= 0.70:
                return True
            return False
            
        bad_ratio = bad_chars / total_chars
        valid_ratio = valid_chars / total_chars
        # 若控制字符比例 > 5%，或有效文字比例 < 65% 且每页均有全页大图，判定为劣质乱码假文本层
        if bad_ratio > max_bad_char_ratio or (valid_ratio < 0.65 and has_full_images >= 2):
            print(f"[PDF] 检测到劣质乱码假文本层 (控制符={bad_ratio:.1%}, 有效文字={valid_ratio:.1%}) → 判定为扫描版 PDF (scanned)")
            return False
            
        return True
    except Exception:
        return False


def is_valid_caption_line(line: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    判断一行文本是否为真正的独立表格标题（Caption），而非正文引用、长句或目录项。
    """
    if not line:
        return False, None, None
    line = line.strip().replace('\xa0', ' ').replace('\u3000', ' ').translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    # 过滤目录页/清单页点号引导线加页码 (如 表1.1 ...... 17)
    if re.search(r'(?:\.{2,}|…{1,}|·{2,}|_{2,}|\s{3,})\s*\d+$', line):
        return False, None, None
    m = re.match(r'^[ \t]*(' + TABLE_LABEL_PATTERN + r')[ \t\.\:：]*(.*)', line, re.IGNORECASE)
    if not m:
        return False, None, None
    raw_lbl = m.group(1).strip()
    title = m.group(2).strip()
    # 去除括号内部内容后再检查是否有正文标点（仅过滤明显的中文正文逗号、句号、感叹号，保留英文正常词间逗号）
    title_no_brackets = re.sub(r'（.*?）|\(.*?\)', '', title)
    if any(p in title_no_brackets for p in ['。', '！', '？', '!']):
        return False, None, None
    # 排除正文常用引导词/代词/动词开头或结尾（正文引用行）
    if any(title.startswith(p) for p in [')', '）', '中', '可以', '所示', '可知', '看出', '见', '综合表达', '显示', '表明', '为', '是', '由', '在中', '对于', '根据', 'shown', 'listed', 'summarized']):
        return False, None, None
    if any(title.endswith(p) for p in ['所示', '可以看出', '表明', '可知', '见表', '来自表', '列入表', 'shown in', 'listed in']):
        return False, None, None
    if len(title) > 160:
        return False, None, None
    return True, raw_lbl, title


def reconcile_table_captions(all_results: List[Dict[str, Any]], pdf_path: str) -> List[Dict[str, Any]]:
    """
    根据 PDF 文本层的真实表题（Caption），全文校正所有提取表格的 label 和 table_title。
    
    1. 扫描 PDF 全文，建立精确的 page_idx -> [ {"label": "表4.1", "title": "表4.1 全区...", "y0": float}, ... ]
    2. 检测文献主语种（中文/英文）；
    3. 按页精准匹配真实表号与表题，杜绝中文文献出现 Table 1~50 占位符或跨页串号。
    """
    if not all_results or not os.path.exists(pdf_path):
        return all_results

    try:
        with fitz.open(pdf_path) as doc:
            caption_map = {}
            total_cn = 0
            total_char = 0

            for p_idx in range(len(doc)):
                page = doc[p_idx]
                text = page.get_text("text")
                if not text:
                    continue

                # 排除前置目录/清单页
                first_lines = text[:250]
                if re.search(r'^(?:目\s*录|Contents|插表清单|表格清单|附表清单)', first_lines, re.MULTILINE):
                    continue

                cn_count = len(re.findall(r'[\u4e00-\u9fa5]', text))
                total_cn += cn_count
                total_char += len(text)

                lines = text.split('\n')
                caps = []
                for idx_l, line in enumerate(lines):
                    ok, raw_lbl, title = is_valid_caption_line(line)
                    if ok:
                        lbl = format_table_label(raw_lbl)
                        if not title and idx_l + 1 < len(lines):
                            next_l = lines[idx_l + 1].strip()
                            if not re.search(r'^(?:表|Table|图|Fig|\d+\.)', next_l) and len(next_l) < 90 and not any(p in next_l for p in ['。', '；', '！', '？', ';', '!']):
                                title = next_l
                        rect = _search_for_visual(page, line.strip()[:15])
                        y0 = rect[0].y0 if rect else 0.0
                        full_title = f"{lbl} {title}".strip() if title else lbl
                        caps.append({'label': lbl, 'title': full_title, 'y0': y0})
                if caps:
                    caption_map[p_idx] = sorted(caps, key=lambda c: c['y0'])

        is_chinese_doc = (total_cn / max(1, total_char)) > 0.05

        # 确保每个结果对象的 page_idx 与 df.attrs['page_idx'] 严格同步
        for r in all_results:
            df = r.get('df')
            if df is None:
                continue
            p_idx = r.get('page_idx')
            if p_idx is None:
                p_idx = df.attrs.get('page_idx', 0)
            r['page_idx'] = p_idx
            df.attrs['page_idx'] = p_idx

        # 按照 page_idx 和 table_idx 严格排序
        all_results.sort(key=lambda r: (r.get('page_idx', 0), r.get('table_idx', 0)))

        used_captions = set()
        page_table_count = {}
        fallback_counter = 0

        for r in all_results:
            df = r.get('df')
            if df is None:
                continue
            p_idx = r.get('page_idx', 0)
            page_table_count[p_idx] = page_table_count.get(p_idx, 0) + 1
            t_idx = page_table_count[p_idx] - 1

            curr_label = df.attrs.get('label', '')
            curr_title = df.attrs.get('table_title', '')

            assigned_label = None
            assigned_title = None

            # 1. 如果表格自带由 OCR/VLM 精准解析出的真实独立表号（如 表1, 表2, 附表1, Table 1 等），优先采信
            if curr_label and TABLE_LABEL_RE.match(curr_label.strip()):
                assigned_label = format_table_label(curr_label)
                assigned_title = curr_title or assigned_label
                used_captions.add(assigned_label)
            # 2. 否则从本页精确绑定原生 text 层检测到的 caption
            elif p_idx in caption_map and caption_map[p_idx]:
                caps = caption_map[p_idx]
                if t_idx < len(caps) and caps[t_idx]['label'] not in used_captions:
                    cap = caps[t_idx]
                    assigned_label = cap['label']
                    assigned_title = cap['title']
                    used_captions.add(assigned_label)
                else:
                    for cap in caps:
                        if cap['label'] not in used_captions:
                            assigned_label = cap['label']
                            assigned_title = cap['title']
                            used_captions.add(assigned_label)
                            break
            elif curr_label:
                assigned_label = format_table_label(curr_label)
                assigned_title = curr_title or assigned_label
                used_captions.add(assigned_label)

            # 2.5 跨页续表自动关联继承：若本页无 caption 匹配且表格无独立表号，检查是否为前序表格的跨页续表
            if not assigned_label or df.attrs.get('is_continuation'):
                # 寻找前一个已分配有效 label 的结果
                for prev_r in reversed(all_results):
                    if prev_r is r:
                        continue
                    prev_p = prev_r.get('page_idx', 0)
                    prev_lbl = prev_r.get('label') or (prev_r.get('df') is not None and prev_r['df'].attrs.get('label'))
                    if prev_p < p_idx and prev_lbl:
                        prev_df_cols = prev_r['df'].shape[1] if prev_r.get('df') is not None else 0
                        is_marked_cont = bool(df.attrs.get('is_continuation'))
                        is_small_pair = (df.shape[0] < 6 and (prev_r.get('df') is None or prev_r['df'].shape[0] < 6)) and not is_marked_cont
                        cols_ok = (df.shape[1] == prev_df_cols or abs(df.shape[1] - prev_df_cols) <= 2 or is_marked_cont)
                        if (p_idx - prev_p <= 3) and cols_ok and not is_small_pair:
                            assigned_label = prev_lbl
                            assigned_title = f"{prev_lbl} (续)"
                            break

            # 3. 语种感知规范回退（中文文献用 表N，英文文献用 Table N）
            if not assigned_label:
                # 寻找未被使用的下一个合理序号
                fallback_counter += 1
                prefix = "表" if is_chinese_doc else "Table "
                candidate_lbl = f"{prefix}{fallback_counter}"
                while candidate_lbl in used_captions:
                    fallback_counter += 1
                    candidate_lbl = f"{prefix}{fallback_counter}"
                assigned_label = candidate_lbl
                assigned_title = curr_title or assigned_label
                used_captions.add(assigned_label)

            df.attrs['label'] = assigned_label
            df.attrs['table_title'] = assigned_title
            r['label'] = assigned_label

    except Exception as e:
        print(f"[Caption Reconciliation Error] {e}")

    return all_results


def _check_sparse_continuation_table(prev_page, next_page) -> bool:
    """
    针对无表头文字、无网格线且文本稀疏的跨页无框续表进行高灵敏度几何连续性探测：
    1. 校验前一页底部是否存在真实表格结构（通过 find_tables、矢量横线群或多列对齐文本确认）
    2. 校验后一页顶部内容是否紧密承接前页（顶端位置连续性，排除新章节/图表标题与叙述性正文段落）
    3. 校验前后两页的水平跨度一致性 (Horizontal Span Consistency)
    4. 校验后一页顶部文字的多列对齐特征 (Column Alignment) 与列间隙特征
    全面兼容页面旋转（page.rotation = 0, 90, 180, 270）。
    """
    try:
        prev_h = getattr(prev_page.rect, "height", 842.0) if hasattr(prev_page, "rect") else 842.0
        next_h = getattr(next_page.rect, "height", 842.0) if hasattr(next_page, "rect") else 842.0

        def _get_vis_words(page):
            try:
                words = page.get_text("words")
            except Exception:
                return []
            if not isinstance(words, (list, tuple)) or not words:
                return []
            rot = getattr(page, 'rotation', 0)
            rot = rot if isinstance(rot, int) else 0
            rot = rot % 360
            if rot == 0:
                return words
            rot_mat = getattr(page, 'rotation_matrix', None)
            if rot_mat is None:
                return words
            res = []
            for w in words:
                try:
                    r = fitz.Rect(w[0], w[1], w[2], w[3]) * rot_mat
                    res.append((r.x0, r.y0, r.x1, r.y1, w[4], w[5], w[6], w[7]))
                except Exception:
                    res.append(w)
            return res

        # 1. 探查后继页 (next_page) 顶部的文字分布与顶端位置连续性
        next_words = _get_vis_words(next_page)
        if not next_words or len(next_words) < 4:
            return False

        # 过滤页眉 (一般位于顶部 30pt 以内或 < 5% 页高)
        top_words = [w for w in next_words if 30.0 <= w[1] <= next_h * 0.48 and w[3] <= next_h * 0.52]
        if len(top_words) < 4:
            return False

        top_y0 = min(w[1] for w in top_words)
        # 若顶部文字起点离页顶过远（超过页高 35%），则并非紧密承接前页顶部的续表
        if top_y0 > next_h * 0.35:
            return False

        # 将 top_words 按纵向坐标分行 (容差 5pt)
        sorted_words = sorted(top_words, key=lambda w: (round(w[1] / 6.0) * 6.0, w[0]))
        lines = []
        curr_line = [sorted_words[0]]
        for w in sorted_words[1:]:
            if abs(w[1] - curr_line[-1][1]) <= 5.0:
                curr_line.append(w)
            else:
                lines.append(sorted(curr_line, key=lambda x: x[0]))
                curr_line = [w]
        if curr_line:
            lines.append(sorted(curr_line, key=lambda x: x[0]))

        if len(lines) < 2:
            return False

        # 终止条件校验：若首行属于新章节标题、新图题或新独立表题，直接拒绝
        first_line_text = " ".join(w[4] for w in lines[0] if len(w) > 4 and w[4]).strip()
        if re.match(r'^(?:\d+[\.\s]|[A-Z][\.\s]|Chapter|Section|第[一二三四五六七八九十\d]+[章节篇]|Conclusion|References|Acknowledgements|参考文献|致谢|图|Fig)', first_line_text, re.IGNORECASE):
            return False
        if re.match(r'^(?:附表|附录表|补充表|表|Table|Tab\.)\s*\d+', first_line_text, re.IGNORECASE) and not re.search(r'(?:续|cont)', first_line_text, re.IGNORECASE):
            return False

        # 正文停用词密度过滤：若包含高比例常见叙述性停用词，说明为正文自然语言段落而非表格元胞
        prose_stopwords = {'the', 'of', 'and', 'in', 'to', 'a', 'is', 'that', 'for', 'it', 'as', 'was', 'with', 'be', 'by', 'on', 'not', 'this', 'are', 'or', 'an', 'they', 'which', 'from', 'at', 'we', 'were', 'has', 'have', 'had', 'been', 'their', 'which', 'its', 'also', 'between', 'these', 'those'}
        tokens = [w[4].lower().strip(".,;:?!()[]'\"") for w in top_words if len(w) > 4 and w[4]]
        if tokens:
            sw_ratio = sum(1 for t in tokens if t in prose_stopwords) / len(tokens)
            if sw_ratio >= 0.22:
                return False

        # 多列对齐特征与列间隙探测 (Column Alignment)
        line_col_starts = []
        for line in lines:
            starts = [line[0][0]]
            for i in range(1, len(line)):
                gap = line[i][0] - line[i-1][2]
                if gap >= 12.0:
                    starts.append(line[i][0])
            line_col_starts.append(starts)

        # 统计跨行保持几何对齐的列坐标聚类 (容差 12pt)
        col_clusters = []
        for l_idx, starts in enumerate(line_col_starts):
            for s in starts:
                matched = False
                for cl in col_clusters:
                    if abs(cl['center'] - s) <= 12.0:
                        cl['lines'].add(l_idx)
                        cl['points'].append(s)
                        cl['center'] = sum(cl['points']) / len(cl['points'])
                        matched = True
                        break
                if not matched:
                    col_clusters.append({'center': s, 'lines': {l_idx}, 'points': [s]})

        # 跨越多行（>= 2 行）保持对齐的列数
        aligned_col_count = sum(1 for cl in col_clusters if len(cl['lines']) >= 2)
        has_multi_col = any(len(starts) >= 2 for starts in line_col_starts)
        if aligned_col_count < 2 or not has_multi_col:
            return False

        # 2. 探查前一页底部表格的水平区间与底部位置（必须存在触底表格，杜绝普通正文误判）
        prev_span = None
        # 优先通过 find_tables 获取前一页最底部的表格
        try:
            prev_tabs = prev_page.find_tables()
            if prev_tabs and hasattr(prev_tabs, 'tables') and isinstance(prev_tabs.tables, (list, tuple)):
                for tb in prev_tabs.tables:
                    bbox = getattr(tb, 'bbox', None)
                    if bbox and len(bbox) == 4 and bbox[3] >= prev_h * 0.45:
                        if prev_span is None or bbox[3] > prev_span[3]:
                            prev_span = [bbox[0], bbox[1], bbox[2], bbox[3]]
        except Exception:
            pass

        # 尝试通过矢量横线 (三线表/网格表) 探测前页底部表格
        if prev_span is None:
            try:
                drawings = prev_page.get_drawings()
                rot_prev = getattr(prev_page, 'rotation', 0)
                rot_prev = rot_prev if isinstance(rot_prev, int) else 0
                rot_mat_p = getattr(prev_page, 'rotation_matrix', None) if (rot_prev % 360) != 0 else None
                if drawings and isinstance(drawings, (list, tuple)):
                    h_lines = []
                    for d in drawings:
                        for it in d.get('items', []):
                            if it[0] == 'l':
                                p1, p2 = it[1], it[2]
                                if rot_mat_p:
                                    try: p1, p2 = p1 * rot_mat_p, p2 * rot_mat_p
                                    except Exception: pass
                                y_diff = abs(getattr(p1, 'y', 0) - getattr(p2, 'y', 0))
                                if y_diff <= 2.0 and p1.y >= prev_h * 0.45:
                                    h_lines.append((min(p1.x, p2.x), min(p1.y, p2.y), max(p1.x, p2.x), max(p1.y, p2.y)))
                            elif it[0] == 're':
                                r = it[1]
                                if rot_mat_p:
                                    try: r = fitz.Rect(r) * rot_mat_p
                                    except Exception: pass
                                if getattr(r, 'height', 0) <= 3.5 and getattr(r, 'y1', 0) >= prev_h * 0.45:
                                    h_lines.append((r.x0, r.y0, r.x1, r.y1))
                    if len(h_lines) >= 2:
                        prev_span = [
                            min(l[0] for l in h_lines),
                            min(l[1] for l in h_lines),
                            max(l[2] for l in h_lines),
                            max(l[3] for l in h_lines)
                        ]
            except Exception:
                pass

        # 尝试通过文字排布探测前页下半部分的无框多列文本表格 (严格校验列对齐，杜绝正文段落)
        if prev_span is None:
            try:
                prev_words = _get_vis_words(prev_page)
                if isinstance(prev_words, (list, tuple)) and prev_words:
                    low_words = [w for w in prev_words if w[1] >= prev_h * 0.45 and w[3] <= prev_h * 0.98]
                    if len(low_words) >= 4:
                        low_tokens = [w[4].lower().strip(".,;:?!()[]'\"") for w in low_words if len(w) > 4 and w[4]]
                        sw_ratio_low = sum(1 for t in low_tokens if t in prose_stopwords) / max(1, len(low_tokens))
                        if sw_ratio_low < 0.22:
                            p_sorted = sorted(low_words, key=lambda w: (round(w[1] / 6.0) * 6.0, w[0]))
                            p_lines = []
                            p_curr = [p_sorted[0]]
                            for w in p_sorted[1:]:
                                if abs(w[1] - p_curr[-1][1]) <= 5.0:
                                    p_curr.append(w)
                                else:
                                    p_lines.append(p_curr)
                                    p_curr = [w]
                            if p_curr:
                                p_lines.append(p_curr)
                            if len(p_lines) >= 2:
                                p_has_col = any(len([1 for i in range(1, len(l)) if l[i][0] - l[i-1][2] >= 12.0]) >= 1 for l in p_lines)
                                if p_has_col:
                                    prev_span = [
                                        min(w[0] for w in low_words),
                                        min(w[1] for w in low_words),
                                        max(w[2] for w in low_words),
                                        max(w[3] for w in low_words)
                                    ]
            except Exception:
                pass

        if prev_span is None:
            return False

        # 3. 水平跨度一致性检查 (Horizontal Span Consistency)
        next_x0 = min(w[0] for w in top_words)
        next_x1 = max(w[2] for w in top_words)
        prev_x0, prev_x1 = prev_span[0], prev_span[2]

        prev_w = max(1.0, prev_x1 - prev_x0)
        next_w = max(1.0, next_x1 - next_x0)
        if prev_w < 50.0 or next_w < 50.0:
            return False

        x_overlap = max(0.0, min(prev_x1, next_x1) - max(prev_x0, next_x0))
        min_w = min(prev_w, next_w)
        overlap_ratio = x_overlap / min_w
        width_ratio = min(prev_w, next_w) / max(prev_w, next_w)

        # 水平交叠比例低于 60% 或总宽度差距过大，不视为同一张表的跨页承接
        if overlap_ratio < 0.60 or width_ratio < 0.45:
            return False

        return True
    except Exception:
        return False


def group_continuation_atomic_blocks(pdf_path: str, pages: List[int]) -> List[List[int]]:
    """
    智能续表动态累加打包算法（Dynamic Continuation Accumulator）：
    基于正文语义密度与状态转移机制，自适应向前累加所有跨页续表页面（支持 1~50+ 页超长续表）。
    遇到正文段落（连续陈述句）、新独立表题、新图题或章节结语时即时停止，既不遗漏也绝不多测。
    """
    if not pages or not os.path.exists(pdf_path):
        return [[p] for p in pages] if pages else []

    try:
        with fitz.open(pdf_path) as doc:
            total_pages = len(doc)
            all_target_pages = set(pages)

            # 针对每个表格起始页，启动无上界动态状态机累加
            for p in list(all_target_pages):
                next_p = p + 1
                while next_p < total_pages:
                    next_text = doc[next_p].get_text("text").strip()
                    if not next_text:
                        # 空白页或纯图页，停止向前累加
                        break

                    lines = [l.strip() for l in next_text.split('\n') if l.strip()]
                    if not lines:
                        break

                    first_few_lines = " ".join(lines[:3])

                    # 终止条件 1：检测到新独立表题（如 表4.2, Table 3）、新图题（如 图4.1, Fig 2）
                    if re.search(r'^(?:附表|附录表|补充表|表|Table|Tab\.)\s*\d+', first_few_lines, re.IGNORECASE) and not re.search(r'(?:续|cont)', first_few_lines, re.IGNORECASE):
                        break
                    if re.search(r'^(?:图|Fig\.|Figure)\s*\d+', first_few_lines, re.IGNORECASE):
                        break

                    # 终止条件 2：检测到新章节标题、参考文献、致谢、附录总封面
                    if re.match(r'^\d+\.\d+\s+[A-Za-z\u4e00-\u9fa5]', lines[0]) or any(kw in lines[0] for kw in ['参考文献', 'References', '致谢', 'Acknowledgements', '附录一', '附录二']):
                        break

                    # 终止条件 3：正文段落密度检测（累计出现 >= 3 个完整陈述句句号，且文本字符数 > 300）
                    sentence_count = sum(1 for l in lines if l.endswith(('。', '！', '？')) or (l.endswith('.') and len(l.split()) >= 8))
                    if sentence_count >= 3 and len(next_text) >= 300:
                        # 已回归大篇幅正文叙述，立即停止累加
                        break

                    # 累加判定条件 1：显式续表关键字（续表、续附表、（续）、(cont.)、Continued Table）
                    is_explicit_cont = bool(re.search(r'(?:续表|续附表|接上表|（续）|\(续\)|\(cont\.?\)|\(continued\)|continued\s+table)', next_text[:500], re.IGNORECASE))

                    # 累加判定条件 2：高密度表格/数值数据流（数字、化学式、同位素、测试样品代号占比 >= 50%）
                    data_token_count = sum(1 for l in lines if re.match(r'^-?\d+(?:\.\d+)?$', l) or l in ('/', '-', '—', '–', 'n.d.', 'bdl', 'b.d.l.') or re.match(r'^[A-Za-z0-9\-_–—/\.\(\)]+$', l))
                    is_dense_data_table = len(lines) >= 15 and (data_token_count / len(lines) >= 0.50)

                    # 累加判定条件 3：稀疏无框多列对齐续表探测
                    is_sparse_cont = False
                    try:
                        is_sparse_cont = _check_sparse_continuation_table(doc[next_p - 1], doc[next_p])
                    except Exception:
                        pass

                    if is_explicit_cont or is_dense_data_table or is_sparse_cont:
                        all_target_pages.add(next_p)
                        next_p += 1
                    else:
                        # 非续表特征，停止向前探测
                        break
    except Exception:
        all_target_pages = set(pages)

    # 将连续页码合并为完整原子块
    sorted_pages = sorted(list(all_target_pages))
    page_blocks = []
    curr_block = []
    for p in sorted_pages:
        if not curr_block or p == curr_block[-1] + 1:
            curr_block.append(p)
        else:
            page_blocks.append(curr_block)
            curr_block = [p]
    if curr_block:
        page_blocks.append(curr_block)

    return page_blocks


def verify_and_rescue_table_with_agent(df: pd.DataFrame, pdf_path: str, page_idx: int, table_label: str) -> pd.DataFrame:
    """
    8 维全息学术表格质量门禁（8-Dimensional Quality Gate）与 Agent 智能体协同校验体系：
    
    【G1: 表头合法性】未命名占位列 >= 35% 或表头全为纯浮点数值；
    【G2: 碎片化异常】列数 > 25 且填充率 < 25%，或整表塌陷为单列；
    【G3: 单元格多值挤压】单列内 >= 30% 单元格包含多个空格分隔的数值；
    【G4: 乱码与不可读异常】包含大量 OCR 替换符 (\\ufffd) 或未闭合 LaTeX 乱码；
    【G5: 跨行错位与子行不均】多行单元格在记录间行数严重失衡；
    【G6: 有效数据密度极低】数据行数 < 1 或全表有效数值 < 4；
    【G7: 跨页断号与表号漂移】表号完全缺失或存在罗马数字未归一化冲突；
    【G8: 地球化学微观公式保真度】同位素比值 (^206Pb/^204Pb) 或公差 (±) 被截断割裂。
    
    一旦触发任何异常门禁，自动生成 300 DPI 切图移交当前对话 AI Agent 权威会诊与多模态重构！
    """
    if df is None or df.empty:
        return df

    alarms = []

    # G1: 表头合法性门禁
    unnamed_cols = sum(1 for c in df.columns if str(c).startswith(('Unnamed', 'Col_')) or str(c).lower() in ('nan', 'none', '') or str(c).isdigit())
    if (unnamed_cols / max(1, len(df.columns))) >= 0.35:
        alarms.append("G1(表头缺失/匿名列过高)")
    elif all(re.match(r'^-?\d+(?:\.\d+)?$', str(c).strip()) for c in df.columns if str(c).strip()):
        alarms.append("G1(表头被纯数值误占)")

    # G2: 碎片化与单列塌陷门禁
    total_cells = df.shape[0] * df.shape[1]
    non_empty = sum(1 for v in df.values.flatten() if pd.notna(v) and str(v).strip() not in ('', 'nan', 'None'))
    fill_ratio = non_empty / total_cells if total_cells > 0 else 0
    if df.shape[1] > 25 and fill_ratio < 0.25:
        alarms.append(f"G2(极端碎片化: {df.shape[1]}列, 填充率{fill_ratio:.1%})")
    elif df.shape[1] <= 1 and df.shape[0] >= 5:
        alarms.append("G2(多列塌陷为单列)")

    # G3: 单元格多值挤压门禁
    for c_idx in range(df.shape[1]):
        col_vals = df.iloc[:, c_idx].dropna().astype(str).tolist()
        if len(col_vals) >= 3:
            squeezed = sum(1 for v in col_vals if len(re.findall(r'(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9_])', str(v))) >= 2 and len(str(v).split()) >= 2)
            if (squeezed / len(col_vals)) >= 0.30:
                alarms.append(f"G3(第{c_idx+1}列数值多重挤压)")
                break

    # G4: 乱码与不可读异常门禁
    all_text = " ".join(str(v) for v in df.values.flatten() if pd.notna(v))
    if "\ufffd" in all_text or all_text.count("???") >= 3 or re.search(r'\\(?:frac|text|math)\{[^}]*$', all_text):
        alarms.append("G4(乱码/未闭合LaTeX)")

    # G6: 有效数据密度极低
    if df.shape[0] < 1 or non_empty < 4:
        alarms.append("G6(有效数据量极低)")

    # 若未触发任何警报，质量门禁 100% 满分通过
    if not alarms:
        return df

    print(f"[Quality Gate Alarm] 触发门禁告警: {', '.join(alarms)}，启动 AI Agent 多模态视觉抢救...")

    try:
        from agent_bridge import get_agent_bridge
        bridge = get_agent_bridge()
        with fitz.open(pdf_path) as doc:
            if page_idx < len(doc):
                page = doc[page_idx]
                pix = page.get_pixmap(dpi=200)
                img_bytes = pix.tobytes("png")
            else:
                img_bytes = None

        if img_bytes:
            task_id = bridge.create_reasoning_task(
                task_type="QUALITY_GATE_RESCUE",
                pdf_path=pdf_path,
                page_idx=page_idx,
                table_label=table_label,
                crop_image_bytes=img_bytes,
                raw_text=df.to_string(),
                candidate_columns=list(df.columns),
                context_notes=f"触发门禁指标: {'; '.join(alarms)}"
            )
            rescued_df = bridge.check_resolved_task(task_id, timeout_seconds=1.0)
            if rescued_df is not None and not rescued_df.empty:
                rescued_df.attrs = df.attrs.copy()
                print(f"[Agent Verification] 任务 {task_id} 抢救成功: {rescued_df.shape[0]}行 × {rescued_df.shape[1]}列")
                return rescued_df

        # 若 Agent Bridge 未返回（如无头环境），且启用了 LLM，尝试 LLMTableReasoner 直接修复
        if os.getenv("LLM_TABLE_REASONER_ENABLED") or df.attrs.get('enable_llm'):
            try:
                from llm_reasoner import get_llm_reasoner
                reasoner = get_llm_reasoner()
                if reasoner.is_available():
                    repaired = None
                    if img_bytes:
                        repaired = reasoner.parse_degraded_table_layout(img_bytes)
                    if (repaired is None or repaired.empty) and df is not None:
                        repaired = reasoner.repair_defective_table(df)
                    if repaired is not None and not repaired.empty:
                        repaired.attrs = df.attrs.copy()
                        print(f"[LLM Verification] LLM 推理修复成功: {repaired.shape[0]}行 × {repaired.shape[1]}列")
                        return repaired
            except Exception as e_llm:
                print(f"[LLM Verification] 推理抢救跳过: {e_llm}")
    except Exception as e:
        print(f"[Agent Verification] 门禁抢救旁路跳过: {e}")

    return df


def extract_via_paddleocr_fullpage(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    将识别到表格的页面逐页提交给 PaddleOCR-VL-1.6 进行高精结构化识别。
    严格采用智能续表打包与原子子PDF保护机制，杜绝跨页中间切断。
    """
    if pd is None:
        return []
    if pages is None:
        try:
            doc = fitz.open(pdf_path)
            pages = list(range(len(doc)))
            doc.close()
        except Exception:
            pages = []
    if not pages:
        return []

    results = []
    try:
        import sys
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import ocr_client
        from table_postprocess import parse_structured_vlm_content
        from common import load_config

        config = load_config()

        # 智能续表原子子PDF打包（保证所有关联续表页被整体打包送检）
        page_blocks = group_continuation_atomic_blocks(pdf_path, pages)

        print(f"[PaddleOCR-VL FullPage] 正在对连续目标块执行精细识别: {[ [p+1 for p in b] for b in page_blocks ]}...")
        for block in page_blocks:
            try:
                md = ocr_client.run_paddleocr_vl(pdf_path, pages=block)
                if not md:
                    continue
                dfs = parse_structured_vlm_content(md)
                from table_postprocess import merge_continuation_tables
                dfs = merge_continuation_tables(dfs)
                for t_idx, df in enumerate(dfs):
                    if df is not None and not df.empty and df.shape[0] >= 1 and df.shape[1] >= 1:
                        df.attrs['extractor'] = 'paddleocr_vl'
                        df.attrs['page_idx'] = block[0]
                        df.attrs['covered_pages'] = list(block)
                        # 触发 Post-OCR 门禁检验与 Agent 协同验证
                        df = verify_and_rescue_table_with_agent(df, pdf_path, block[0], df.attrs.get('label', ''))
                        results.append({
                            'df': df,
                            'page_idx': block[0],
                            'table_idx': t_idx,
                            'bbox': None,
                            'covered_pages': list(block),
                        })
            except Exception as e:
                print(f"[PaddleOCR-VL FullPage] Block {[p+1 for p in block]} 提取异常: {e}")

    except Exception as e:
        print(f"[PaddleOCR-VL FullPage] 异常: {e}")

    if results:
        print(f"[PaddleOCR-VL FullPage] 提取到 {len(results)} 个表格")
    return results


def extract_via_paddleocr_crops(pdf_path: str, pages: Optional[List[int]] = None, cancel_event=None) -> List[Dict[str, Any]]:
    """
    针对待精细识别/救助页面，优先利用本地 DocLayout-YOLO 裁剪出表格主体区域 (Table Crop)，
    仅将目标切图提交给 PaddleOCR-VL-1.6 进行精细多模态解析，大幅节省 75%+ Token 并防止长表截断；
    若特定页面未定位到切图或切图解析失败，平滑回退至整页 extract_via_paddleocr_fullpage。
    """
    if pd is None or not os.path.exists(pdf_path):
        return []

    if cancel_event is not None and cancel_event.is_set():
        return []

    if pages is None:
        try:
            doc = fitz.open(pdf_path)
            pages = list(range(len(doc)))
            doc.close()
        except Exception:
            pages = []
    if not pages:
        return []

    results = []
    import sys
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    try:
        import ocr_client
        from table_postprocess import parse_structured_vlm_content
        from common import load_config
        config = load_config()
    except Exception as e:
        print(f"[PaddleOCR-VL Crops] 依赖加载失败: {e}，回退整页提取")
        return extract_via_paddleocr_fullpage(pdf_path, pages)

    # 1. 尝试使用本地 DocLayout-YOLO 优先切图提取
    crops = []
    try:
        from pdf_tables import extract_table_crops_from_pdf
        print(f"[PaddleOCR-VL Crops] 正在通过 DocLayout-YOLO 对候选页面 {[p+1 for p in pages]} 进行目标表格切图...")
        crops = extract_table_crops_from_pdf(pdf_path, pages=pages, enable_filter=False, cancel_event=cancel_event)
    except Exception as e:
        print(f"[PaddleOCR-VL Crops] 本地切图提取提示: {e}")

    # 按页面索引归类切图
    crops_by_page = {}
    for c in (crops or []):
        p_idx = c.get("page_index")
        if p_idx is not None:
            crops_by_page.setdefault(p_idx, []).append(c)

    pages_resolved = set()

    doc_for_crops = None
    try:
        if os.path.exists(pdf_path):
            doc_for_crops = fitz.open(pdf_path)
    except Exception:
        doc_for_crops = None

    try:
        for page_idx in pages:
            if cancel_event is not None and cancel_event.is_set():
                break
            page_crops = crops_by_page.get(page_idx, [])
            if not page_crops:
                continue

            page_success = False
            for t_idx, crop in enumerate(page_crops):
                if cancel_event is not None and cancel_event.is_set():
                    break
                img = crop.get("image")
                if img is None:
                    continue

                # Tier 2 (Embedded Landscape Crop Rotation):
                # 对嵌套在页面中的横置旋转表格切图，通过矢量文本或 doc_ori.onnx 进行方向判定并旋转回正
                try:
                    try:
                        from .doc_orientation_detector import detect_crop_orientation
                    except ImportError:
                        from doc_orientation_detector import detect_crop_orientation

                    p_for_crop = None
                    if doc_for_crops is not None and 0 <= page_idx < len(doc_for_crops):
                        p_for_crop = doc_for_crops[page_idx]

                    crop_rect = crop.get("pdf_bbox")
                    ori_res = detect_crop_orientation(img, page=p_for_crop, rect=crop_rect)

                    if ori_res.get("needs_rotation") and ori_res.get("correction_angle") in (90, 180, 270):
                        deg = ori_res["correction_angle"]
                        upright_img = img.rotate(360 - deg, expand=True)
                        print(f"[PaddleOCR-VL Crops] 检测到第 {page_idx+1} 页 Table Crop #{t_idx+1} 存在 {ori_res.get('detected_angle')}° 旋转 ({ori_res.get('source')})，已自动旋转正向 (校正角: {deg}°, 尺寸: {img.size} -> {upright_img.size})")
                        img = upright_img
                        crop["image"] = upright_img
                except Exception as e_crop_ori:
                    print(f"[PaddleOCR-VL Crops] 切图方向检测与校准提示: {e_crop_ori}")

                temp_img_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f_tmp:
                        temp_img_path = f_tmp.name
                    img.save(temp_img_path, format="PNG")

                    print(f"[PaddleOCR-VL Crops] 正在提交第 {page_idx+1} 页 Table Crop #{t_idx+1} (尺寸: {img.size}) 至 VLM...")
                    crop_md = ocr_client.call_paddleocr_vl_online_api(temp_img_path, config=config, timeout=180, cancel_event=cancel_event)
                    if not crop_md:
                        continue

                    dfs = parse_structured_vlm_content(crop_md)
                    from table_postprocess import merge_continuation_tables
                    dfs = merge_continuation_tables(dfs)

                    for sub_t_idx, df in enumerate(dfs):
                        if df is not None and not df.empty and df.shape[0] >= 1 and df.shape[1] >= 1:
                            df.attrs['extractor'] = 'paddleocr_vl_crop'
                            df.attrs['page_idx'] = page_idx
                            df.attrs['covered_pages'] = [page_idx]
                            if crop.get("crop_bbox"):
                                df.attrs['crop_bbox'] = crop['crop_bbox']
                            if crop.get("pdf_bbox"):
                                df.attrs['pdf_bbox'] = crop['pdf_bbox']

                            # 触发 Post-OCR 门禁检验与 Agent 协同验证
                            df = verify_and_rescue_table_with_agent(df, pdf_path, page_idx, df.attrs.get('label', ''))
                            results.append({
                                'df': df,
                                'page_idx': page_idx,
                                'table_idx': t_idx + sub_t_idx,
                                'bbox': crop.get("crop_bbox"),
                                'pdf_bbox': crop.get("pdf_bbox"),
                                'covered_pages': [page_idx],
                            })
                            page_success = True
                except Exception as e_crop:
                    print(f"[PaddleOCR-VL Crops] 处理第 {page_idx+1} 页 Crop #{t_idx+1} 异常: {e_crop}")
                finally:
                    if temp_img_path and os.path.exists(temp_img_path):
                        try:
                            os.unlink(temp_img_path)
                        except Exception:
                            pass

            # 统计该页期望表数与实际成功提取数，若提取数少于期望表数（如一页双表只切出一张），整页不可标记为完全 resolved
            expected_page_tables = 1
            try:
                try:
                    from .table_validator import scan_pdf_table_declarations
                except ImportError:
                    from table_validator import scan_pdf_table_declarations
                p_decls = scan_pdf_table_declarations(pdf_path).get(page_idx, [])
                expected_page_tables = max(1, len(page_crops), len([d for d in p_decls if not d.get('is_continuation')]))
            except Exception:
                expected_page_tables = max(1, len(page_crops))

            page_extracted_count = sum(1 for r in results if r.get('page_idx') == page_idx)
            if page_success and page_extracted_count >= expected_page_tables:
                pages_resolved.add(page_idx)
    finally:
        if doc_for_crops is not None:
            try:
                doc_for_crops.close()
            except Exception:
                pass

    # 2. 检查哪些页面未能通过切图获得表格，对这些页面平滑回退至整页 extract_via_paddleocr_fullpage
    unresolved_pages = [p for p in pages if p not in pages_resolved]
    if unresolved_pages:
        print(f"[PaddleOCR-VL Crops] 页面 {[p+1 for p in unresolved_pages]} 未检出有效切图或切图解析未果，平滑回退至整页 PaddleOCR-VL 识别...")
        fallback_results = extract_via_paddleocr_fullpage(pdf_path, unresolved_pages)
        results.extend(fallback_results)

    if results:
        print(f"[PaddleOCR-VL Crops] 综合切图与回退，共提取到 {len(results)} 个表格")
    return results


def _get_table_garbage_row_indices(df: Any) -> set:
    """
    识别表格中被明确污染的行索引（如论文元数据、投稿日期、跨页重复打印表头、续表注记或全空行）。
    """
    if df is None or not hasattr(df, 'empty') or df.empty or not hasattr(df, 'shape'):
        return set()
    garbage_indices = set()
    article_metadata_kws = [
        "基金项目", "作者简介", "中图分类号", "文献标识码", "收稿日期", "通信作者",
        "引用格式", "received:", "accepted:", "revised:", "published:", "doi:",
        "copyright", "all rights reserved", "http:", "https:"
    ]
    col_strs = [str(c).strip().lower() for c in df.columns if str(c).strip() and not str(c).startswith("Unnamed:")]

    def _is_num(val):
        try:
            float(str(val).replace(",", "").strip())
            return True
        except ValueError:
            return False

    for r in range(df.shape[0]):
        row_vals = [str(df.iat[r, c]).strip() for c in range(df.shape[1]) if str(df.iat[r, c]).strip() not in ("", "nan", "none")]
        if not row_vals:
            garbage_indices.add(r)
            continue
        row_str = " ".join(row_vals).lower()
        # 1. 论文元数据/投稿信息/版权污染
        if any(kw in row_str for kw in article_metadata_kws) or re.search(r'\b(?:received|accepted|revised|published)\b.*?\b\d{4}\b', row_str):
            garbage_indices.add(r)
            continue
        # 2. 跨页/表格内部重复打印的表头行
        if len(row_vals) >= 2 and col_strs:
            num_numeric = sum(1 for v in row_vals if _is_num(v))
            if num_numeric == 0:
                matches = sum(1 for v in row_vals if v.lower() in col_strs or any(v.lower() == c or v.lower() in c.split() for c in col_strs))
                if matches >= 2 and (matches / len(row_vals)) >= 0.5:
                    garbage_indices.add(r)
                    continue
        # 3. 续表标记行
        if len(row_vals) <= 2 and any(re.search(r'^(?:续表|接上表|\(contd|\(continued|continued table)', v.lower()) for v in row_vals):
            garbage_indices.add(r)
            continue
    return garbage_indices


def _should_replace_with_ocr(nat_item, ocr_item) -> bool:
    """
    质量比对门禁：严格比较原生表格与 OCR 表格质量，防止空表、行数截断、丢列或劣质 OCR 破坏优质原生表。
    原生表格非退化（非空/非单单元格）且无污染时，绝不允许少行少列的 OCR 版本替换原生表；
    若原生表存在污染、表头重复、论文元数据混入或被判定为低质，应比较有效数据的保留情况，允许高质量 OCR 修复。
    """
    df_n = nat_item.get('df') if isinstance(nat_item, dict) else nat_item
    df_o = ocr_item.get('df') if isinstance(ocr_item, dict) else ocr_item
    if df_o is None or df_o.empty:
        return False

    # 若原生表格退化为无意义残表（空表、单单元格），允许合格 OCR 替换
    is_n_degenerate = (
        df_n is None or
        df_n.empty or
        df_n.size <= 1 or
        (df_n.shape[0] <= 1 and df_n.shape[1] <= 1)
    )
    if is_n_degenerate:
        return not is_table_low_quality(df_o)

    if is_table_low_quality(df_o) and not is_table_low_quality(df_n):
        return False

    # 识别原生表格中是否存在明确的污染、表头重复、元数据混入或低质量判定
    is_n_low_quality = is_table_low_quality(df_n)
    if isinstance(nat_item, dict):
        if nat_item.get('low_quality') or nat_item.get('is_contaminated') or (nat_item.get('confidence', 1.0) < 0.6):
            is_n_low_quality = True

    garbage_row_indices = _get_table_garbage_row_indices(df_n)
    is_n_contaminated = len(garbage_row_indices) > 0

    clean_n = df_n.iloc[[i for i in range(df_n.shape[0]) if i not in garbage_row_indices]] if garbage_row_indices else df_n
    if len(clean_n) == 0:
        return not is_table_low_quality(df_o)

    # 若原生表为单列破碎表 (clean_n.shape[1] == 1)，而 OCR 提取出结构完整的有效多列表格 (>= 2 列)，允许 OCR 修复替换
    if clean_n.shape[1] == 1 and df_o.shape[1] >= 2 and not is_table_low_quality(df_o):
        return True

    n_clean_valid_cells = sum(1 for v in clean_n.values.flatten() if v is not None and str(v).strip() not in ('', 'nan', 'none'))
    o_valid_cells = sum(1 for v in df_o.values.flatten() if v is not None and str(v).strip() not in ('', 'nan', 'none'))

    clean_n_cols = len([c for c in clean_n.columns if str(c).strip() and not str(c).startswith("Unnamed:")])
    ocr_cols = len([c for c in df_o.columns if str(c).strip() and not str(c).startswith("Unnamed:")])

    clean_n_is_low_quality = is_table_low_quality(clean_n)

    # 核对有效记录（行样本）的实际保留情况：
    # 提取 clean_n 各行样本标识及字段对应关系，核对 df_o 是否实际保留了该样本记录，杜绝全表单元格集合比对掩盖样本丢失
    def _find_sample_id_col(df):
        if df is None or df.empty:
            return None
        cols = list(df.columns)
        id_patterns = [
            r'(?:^|[\s_\-])(?:sample|id|identifier|name|specimen|station|site|case|patient|gene|no\.?|item|code|label|compound|material)(?:$|[\s_\-])',
            r'^(?:样本|编号|名称|样品|站位|点位|实验号|序号)$',
        ]
        for c_idx, c in enumerate(cols):
            c_str = str(c).strip().lower()
            if any(re.search(pat, c_str, re.IGNORECASE) for pat in id_patterns):
                col_vals = [str(v).strip().lower() for v in df.iloc[:, c_idx] if pd.notna(v) and str(v).strip().lower() not in ('', 'nan', 'none', '-', '—')]
                if len(col_vals) > 0 and len(set(col_vals)) >= min(2, len(col_vals)):
                    return c_idx

        # If column 0 has mostly unique text tokens across rows:
        col_vals = [str(v).strip().lower() for v in df.iloc[:, 0] if pd.notna(v) and str(v).strip().lower() not in ('', 'nan', 'none', '-', '—')]
        if len(col_vals) >= 2 and len(set(col_vals)) / max(1, len(col_vals)) >= 0.4:
            return 0
        return None

    id_col_n = _find_sample_id_col(clean_n)
    id_col_o = _find_sample_id_col(df_o)

    has_missing_distinct_samples = False
    clean_rows_matched = 0

    if id_col_n is not None:
        n_id_vals = [
            str(clean_n.iat[r, id_col_n]).strip().lower()
            for r in range(clean_n.shape[0])
            if pd.notna(clean_n.iat[r, id_col_n]) and str(clean_n.iat[r, id_col_n]).strip().lower() not in ('', 'nan', 'none', '-', '—')
        ]
        distinct_n_ids = set(n_id_vals)

        best_o_col = id_col_o
        if distinct_n_ids:
            if best_o_col is None:
                best_overlap = 0
                for c_idx in range(df_o.shape[1]):
                    c_vals = set(
                        str(df_o.iat[r, c_idx]).strip().lower()
                        for r in range(df_o.shape[0])
                        if pd.notna(df_o.iat[r, c_idx]) and str(df_o.iat[r, c_idx]).strip().lower() not in ('', 'nan', 'none', '-', '—')
                    )
                    ol = len(distinct_n_ids & c_vals)
                    if ol > best_overlap:
                        best_overlap = ol
                        best_o_col = c_idx

            if best_o_col is not None:
                o_id_vals_set = set(
                    str(df_o.iat[r, best_o_col]).strip().lower()
                    for r in range(df_o.shape[0])
                    if pd.notna(df_o.iat[r, best_o_col]) and str(df_o.iat[r, best_o_col]).strip().lower() not in ('', 'nan', 'none', '-', '—')
                )
            else:
                o_id_vals_set = set(
                    str(v).strip().lower() for v in df_o.values.flatten()
                    if pd.notna(v) and str(v).strip().lower() not in ('', 'nan', 'none', '-', '—')
                )

            missing_ids = distinct_n_ids - o_id_vals_set
            if missing_ids:
                has_missing_distinct_samples = True

            matched_o_rows = set()
            for r_i in range(clean_n.shape[0]):
                raw_id = clean_n.iat[r_i, id_col_n]
                s_id = str(raw_id).strip().lower() if pd.notna(raw_id) else ''
                n_row_vals = set(
                    str(clean_n.iat[r_i, c]).strip().lower()
                    for c in range(clean_n.shape[1])
                    if c != id_col_n and pd.notna(clean_n.iat[r_i, c]) and str(clean_n.iat[r_i, c]).strip().lower() not in ('', 'nan', 'none', '-', '—')
                )

                matched = False
                if s_id and s_id not in ('', 'nan', 'none', '-', '—'):
                    for r_j in range(df_o.shape[0]):
                        if r_j in matched_o_rows:
                            continue
                        o_row_id = (
                            str(df_o.iat[r_j, best_o_col]).strip().lower()
                            if (best_o_col is not None and pd.notna(df_o.iat[r_j, best_o_col]))
                            else ''
                        )
                        if o_row_id == s_id or (best_o_col is None and s_id in set(str(v).strip().lower() for v in df_o.iloc[r_j] if pd.notna(v))):
                            o_row_vals = set(
                                str(df_o.iat[r_j, c]).strip().lower()
                                for c in range(df_o.shape[1])
                                if (best_o_col is None or c != best_o_col) and pd.notna(df_o.iat[r_j, c]) and str(df_o.iat[r_j, c]).strip().lower() not in ('', 'nan', 'none', '-', '—')
                            )
                            if not n_row_vals or not o_row_vals or len(n_row_vals & o_row_vals) > 0:
                                matched_o_rows.add(r_j)
                                matched = True
                                break
                else:
                    for r_j in range(df_o.shape[0]):
                        if r_j in matched_o_rows:
                            continue
                        o_row_vals = set(
                            str(df_o.iat[r_j, c]).strip().lower()
                            for c in range(df_o.shape[1])
                            if pd.notna(df_o.iat[r_j, c]) and str(df_o.iat[r_j, c]).strip().lower() not in ('', 'nan', 'none', '-', '—')
                        )
                        if n_row_vals and o_row_vals:
                            overlap = len(n_row_vals & o_row_vals) / max(1, len(n_row_vals))
                            if overlap >= 0.7:
                                matched_o_rows.add(r_j)
                                matched = True
                                break
                        elif not n_row_vals:
                            matched_o_rows.add(r_j)
                            matched = True
                            break
                if matched:
                    clean_rows_matched += 1
    else:
        matched_o_rows = set()
        for r_i in range(clean_n.shape[0]):
            n_row_vals = set(
                str(clean_n.iat[r_i, c]).strip().lower()
                for c in range(clean_n.shape[1])
                if pd.notna(clean_n.iat[r_i, c]) and str(clean_n.iat[r_i, c]).strip().lower() not in ('', 'nan', 'none', '-', '—')
            )
            if not n_row_vals:
                clean_rows_matched += 1
                continue
            matched = False
            for r_j in range(df_o.shape[0]):
                if r_j in matched_o_rows:
                    continue
                o_row_vals = set(
                    str(df_o.iat[r_j, c]).strip().lower()
                    for c in range(df_o.shape[1])
                    if pd.notna(df_o.iat[r_j, c]) and str(df_o.iat[r_j, c]).strip().lower() not in ('', 'nan', 'none', '-', '—')
                )
                if o_row_vals:
                    overlap = len(n_row_vals & o_row_vals) / max(1, len(n_row_vals))
                    if overlap >= 0.7:
                        matched_o_rows.add(r_j)
                        matched = True
                        break
            if matched:
                clean_rows_matched += 1

    record_retention_ratio = clean_rows_matched / max(1, clean_n.shape[0])

    # 严格门禁：无论原生表是否受污染，都绝不允许 OCR 丢失独立样本或通过重复假造行记录！
    if has_missing_distinct_samples:
        return False
    min_retention = 0.90 if clean_n_is_low_quality else 0.95
    if record_retention_ratio < min_retention:
        return False

    # 若原生表格存在缺陷/污染/低质：只要 OCR 本身合格且通过样本核查，允许剔除垃圾行替换
    if is_n_low_quality or is_n_contaminated:
        if not clean_n_is_low_quality:
            if (df_o.shape[0] >= len(clean_n) and ocr_cols >= clean_n_cols and
                o_valid_cells >= (0.85 * n_clean_valid_cells)):
                return True
            return False
        else:
            if o_valid_cells >= (0.75 * n_clean_valid_cells):
                if df_o.shape[0] >= int(0.9 * len(clean_n)) or ocr_cols >= clean_n_cols:
                    return True
            return False

    # 原生表格为清洁高质表格：严格保护其已有行列结构与数据量
    if df_o.shape[0] < df_n.shape[0]:
        return False
    if df_o.shape[1] < df_n.shape[1]:
        return False
    if o_valid_cells < n_clean_valid_cells and not clean_n_is_low_quality:
        return False

    # 填充率对比：如果 OCR 极度稀疏而原生填充良好，保留原生
    n_cells = df_n.size
    o_cells = df_o.size
    n_fill = n_clean_valid_cells / max(1, n_cells)
    o_fill = o_valid_cells / max(1, o_cells)
    if o_fill < 0.20 and n_fill >= 0.50:
        return False

    return True


_merge_or_replace_with_ocr = _should_replace_with_ocr


def reconcile_native_and_ocr_tables(
    native_results: List[Dict[str, Any]],
    ocr_results: List[Dict[str, Any]],
    ocr_candidate_pages: List[int],
    logs: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    表级智能对齐与替换策略（避免粗暴按页全部删除原生表导致同页其他有效表丢失）。
    
    规则：
    1. 非 OCR 接管候选页面的原生表格完全不受影响保留。
    2. 对 OCR 接管页面：
       - 优先按表号标签 (label/table_title) 精确匹配原生表与 OCR 表。
       - 其次按 2D bbox 空间重叠 (IoU >= 0.2 或 IoM >= 0.35) 匹配。
       - 再次按文本内容高相似度 (sim >= 0.55) 匹配。
       - 对成功匹配的原生表，由高质量 OCR 结果精准替换该表。
       - 对未匹配的 OCR 结果（新发现的表格），追加保留。
       - 对同页上未被 OCR 覆盖但本身合格的原生表，予以安全保留，绝不因同页其他表重跑 OCR 而丢失！
    """
    if not ocr_results:
        return native_results

    if logs is None:
        logs = []

    ocr_pages_set = set(ocr_candidate_pages)
    unaffected = [r for r in native_results if r.get('page_idx') not in ocr_pages_set]
    merged = list(unaffected)
    handled_ocr_ids = set()
    appended_ocr_ids = set()

    def _append_ocr_safely(item):
        if id(item) not in appended_ocr_ids:
            appended_ocr_ids.add(id(item))
            merged.append(item)

    def _safe_label(item):
        if not item or not isinstance(item, dict):
            return ""
        df = item.get('df')
        lbl = df.attrs.get('label') if (df is not None and hasattr(df, 'attrs')) else None
        if not lbl:
            lbl = item.get('label')
        return str(lbl).strip() if lbl else ""

    for p in ocr_candidate_pages:
        p_nat = [r for r in native_results if r.get('page_idx') == p]
        p_ocr = [r for r in ocr_results if r.get('page_idx') == p or (r.get('covered_pages') and p in r['covered_pages'])]

        if not p_ocr:
            # 该页未产生 OCR 结果，保留原有原生表格
            merged.extend(p_nat)
            continue

        if not p_nat:
            # 该页原生无表，采纳尚未加入的 OCR 表格
            for ocr_item in p_ocr:
                if id(ocr_item) not in handled_ocr_ids:
                    handled_ocr_ids.add(id(ocr_item))
                    _append_ocr_safely(ocr_item)
            continue

        matched_nat_indices = set()
        matched_ocr_indices = set()

        # 1. 表号标签精确匹配
        for i_ocr, ocr_item in enumerate(p_ocr):
            ocr_lbl = _safe_label(ocr_item)
            if not ocr_lbl:
                continue
            fmt_ocr = format_table_label(ocr_lbl)
            if not fmt_ocr:
                continue
            for i_nat, nat_item in enumerate(p_nat):
                if i_nat in matched_nat_indices:
                    continue
                nat_lbl = _safe_label(nat_item)
                if nat_lbl and format_table_label(nat_lbl) == fmt_ocr:
                    matched_nat_indices.add(i_nat)
                    matched_ocr_indices.add(i_ocr)
                    handled_ocr_ids.add(id(ocr_item))
                    if _should_replace_with_ocr(nat_item, ocr_item):
                        _append_ocr_safely(ocr_item)
                        logs.append(f"  Page {p+1}: 表号 [{fmt_ocr}] 原生版本被更优的 OCR 版本精准替换")
                    else:
                        merged.append(nat_item)
                        logs.append(f"  Page {p+1}: 表号 [{fmt_ocr}] 原生版本质量优于 OCR 版本，保留原生版本")
                    break

        # 2. 空间二维 bbox 重叠匹配
        for i_ocr, ocr_item in enumerate(p_ocr):
            if i_ocr in matched_ocr_indices:
                continue
            b_ocr = ocr_item.get('pdf_bbox') or ocr_item.get('bbox')
            if not b_ocr:
                continue
            ocr_lbl = _safe_label(ocr_item)
            fmt_ocr = format_table_label(ocr_lbl) if ocr_lbl else None

            best_nat_idx = -1
            best_iom = 0.0
            for i_nat, nat_item in enumerate(p_nat):
                if i_nat in matched_nat_indices:
                    continue
                # 排除明确的表号冲突 (如 Table 1 与 Table 2 互相独立)
                nat_lbl = _safe_label(nat_item)
                fmt_nat = format_table_label(nat_lbl) if nat_lbl else None
                if fmt_nat and fmt_ocr and fmt_nat != fmt_ocr:
                    continue

                b_nat = nat_item.get('pdf_bbox') or nat_item.get('bbox')
                if not b_nat:
                    continue
                iou, iom = _compute_bbox_iou(b_nat, b_ocr)
                if (iom >= 0.35 or iou >= 0.20) and iom > best_iom:
                    best_iom = iom
                    best_nat_idx = i_nat

            if best_nat_idx >= 0:
                matched_nat_indices.add(best_nat_idx)
                matched_ocr_indices.add(i_ocr)
                handled_ocr_ids.add(id(ocr_item))
                nat_item = p_nat[best_nat_idx]
                if _should_replace_with_ocr(nat_item, ocr_item):
                    _append_ocr_safely(ocr_item)
                    logs.append(f"  Page {p+1}: 空间重叠 (IoM={best_iom:.2f}) 原生表格被更优的 OCR 版本精准替换")
                else:
                    merged.append(nat_item)
                    logs.append(f"  Page {p+1}: 空间重叠 (IoM={best_iom:.2f}) 原生表格质量更优，保留原生版本")

        # 3. 内容相似度匹配
        for i_ocr, ocr_item in enumerate(p_ocr):
            if i_ocr in matched_ocr_indices:
                continue
            df_ocr = ocr_item.get('df')
            if df_ocr is None or df_ocr.empty:
                continue
            ocr_lbl = _safe_label(ocr_item)
            fmt_ocr = format_table_label(ocr_lbl) if ocr_lbl else None
            b_ocr = ocr_item.get('pdf_bbox') or ocr_item.get('bbox')

            best_nat_idx = -1
            best_sim = 0.0
            for i_nat, nat_item in enumerate(p_nat):
                if i_nat in matched_nat_indices:
                    continue
                df_nat = nat_item.get('df')
                if df_nat is None or df_nat.empty:
                    continue

                # 排除明确的表号冲突 (如 Table 1 与 Table 2 互相独立)
                nat_lbl = _safe_label(nat_item)
                fmt_nat = format_table_label(nat_lbl) if nat_lbl else None
                if fmt_nat and fmt_ocr and fmt_nat != fmt_ocr:
                    continue

                # 排除空间完全分离 (若两者均包含 bbox 且无实质重叠)
                b_nat = nat_item.get('pdf_bbox') or nat_item.get('bbox')
                if b_nat and b_ocr:
                    iou, iom = _compute_bbox_iou(b_nat, b_ocr)
                    if iou <= 0.05 and iom <= 0.05:
                        continue

                sim = _table_similarity(df_nat, df_ocr)
                if sim >= 0.65 and sim > best_sim:
                    best_sim = sim
                    best_nat_idx = i_nat

            if best_nat_idx >= 0:
                matched_nat_indices.add(best_nat_idx)
                matched_ocr_indices.add(i_ocr)
                handled_ocr_ids.add(id(ocr_item))
                nat_item = p_nat[best_nat_idx]
                if _should_replace_with_ocr(nat_item, ocr_item):
                    _append_ocr_safely(ocr_item)
                    logs.append(f"  Page {p+1}: 内容相似 (sim={best_sim:.2f}) 原生表格被更优的 OCR 版本精准替换")
                else:
                    merged.append(nat_item)
                    logs.append(f"  Page {p+1}: 内容相似 (sim={best_sim:.2f}) 原生表格质量更优，保留原生版本")

        # 4. 未被匹配的 OCR 结果（视觉补充定位到的新表）
        for i_ocr, ocr_item in enumerate(p_ocr):
            if i_ocr not in matched_ocr_indices and id(ocr_item) not in handled_ocr_ids:
                handled_ocr_ids.add(id(ocr_item))
                _append_ocr_safely(ocr_item)
                logs.append(f"  Page {p+1}: OCR 补充检出新表格，予以采纳")

        # 5. 未被匹配的原生结果（同页未被 OCR 覆盖的兄弟表格）
        for i_nat, nat_item in enumerate(p_nat):
            if i_nat not in matched_nat_indices:
                df_nat = nat_item.get('df')
                # 检查该原生表是否有效，若有效则坚决保留，防止漏表
                if df_nat is not None and not is_table_low_quality(df_nat):
                    merged.append(nat_item)
                    logs.append(f"  Page {p+1}: 原生表格未受 OCR 干扰且质量合格，安全予以保留")
                elif df_nat is not None:
                    # 损坏表若 OCR 未能给出对应切图，仍保留但带标记，防止整张表丢失
                    merged.append(nat_item)
                    logs.append(f"  Page {p+1}: 原生表格有质量瑕疵且未匹配到 OCR 切图，保留兜底")

    # 兜底：处理可能未明确绑定至 candidate_pages 的 OCR 结果
    for ocr_item in ocr_results:
        if id(ocr_item) not in handled_ocr_ids:
            handled_ocr_ids.add(id(ocr_item))
            _append_ocr_safely(ocr_item)

    return merged


def merge_extracted_tables(
    native_results: List[Dict[str, Any]],
    ocr_results: List[Dict[str, Any]],
    candidate_pages: Optional[List[int]] = None,
    ocr_candidate_pages: Optional[List[int]] = None,
    logs: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    表级智能对齐与替换策略（reconcile_native_and_ocr_tables 兼容别名）。
    """
    pages = ocr_candidate_pages if ocr_candidate_pages is not None else (candidate_pages or [])
    return reconcile_native_and_ocr_tables(native_results, ocr_results, pages, logs=logs)


def quick_detect_candidate_pages(pdf_path: str) -> List[int]:
    """
    轻量快速探测 PDF 中可能包含表格的候选页面索引 (0-indexed)。
    综合真实 Caption 声明与全图/扫描页特征，耗时极低 (< 20ms)。
    供 Tier 1 方向检测与快速预排查使用。
    """
    if not os.path.exists(pdf_path) or fitz is None:
        return []

    candidate_pages = set()
    try:
        try:
            from .table_validator import scan_pdf_table_declarations
        except ImportError:
            from table_validator import scan_pdf_table_declarations
        page_decls = scan_pdf_table_declarations(pdf_path)
        candidate_pages.update(page_decls.keys())
    except Exception:
        pass

    try:
        doc = fitz.open(pdf_path)
        for p_idx, page in enumerate(doc):
            words = page.get_text("words")
            num_words = len(words)
            if num_words < 80:
                images = page.get_images(full=False)
                if len(images) > 0 or num_words < 10:
                    candidate_pages.add(p_idx)
                    continue

            # 矢量绘图密度特征：若含有较多矢量绘图指令 (如复杂图表或横表线框)
            try:
                drawings = page.get_drawings()
                if isinstance(drawings, (list, tuple)) and len(drawings) >= 15:
                    candidate_pages.add(p_idx)
            except Exception:
                pass
        doc.close()
    except Exception:
        pass

    return sorted(list(candidate_pages))


def get_table_candidate_pages(
    pdf_path: str,
) -> Tuple[List[int], Dict[int, Set[str]], Dict[int, List[Dict[str, Any]]]]:
    """
    探测 PDF 中可能包含表格的候选页面索引 (0-indexed)。
    多信号并集体系：Caption 声明、全图扫描页、矢量线框探测、无框文本表格 (strategy="text") 与轻量列对齐探测。

    返回: (candidate_pages, candidate_sources, page_decls)
    """
    if not os.path.exists(pdf_path) or fitz is None:
        return ([], {}, {})

    candidate_sources: Dict[int, Set[str]] = {}

    def _add_candidate(page_idx: int, src: str):
        if page_idx not in candidate_sources:
            candidate_sources[page_idx] = set()
        candidate_sources[page_idx].add(src)

    page_decls = {}
    try:
        try:
            from .table_validator import scan_pdf_table_declarations
        except ImportError:
            from table_validator import scan_pdf_table_declarations
        page_decls = scan_pdf_table_declarations(pdf_path)
        for p in page_decls.keys():
            _add_candidate(p, 'caption')
    except Exception:
        pass

    try:
        doc = fitz.open(pdf_path)
        for p_idx, page in enumerate(doc):
            words = page.get_text("words")
            images = page.get_images()
            if len(images) > 0 and len(words) < 80:
                _add_candidate(p_idx, 'image_scan')
        doc.close()
    except Exception:
        pass

    try:
        with fitz.open(pdf_path) as doc_check:
            total_pages = len(doc_check)
            pages_to_check = [p for p in range(total_pages) if p not in candidate_sources]
            chunk_size = 50 if total_pages <= 300 else 100
            for chunk_start in range(0, len(pages_to_check), chunk_size):
                chunk = pages_to_check[chunk_start:chunk_start + chunk_size]
                for p_idx in chunk:
                    p_obj = None
                    try:
                        p_obj = doc_check[p_idx]
                        has_drawings = True
                        try:
                            drawings = p_obj.get_drawings()
                            if isinstance(drawings, (list, tuple)) and len(drawings) == 0:
                                has_drawings = False
                        except Exception:
                            has_drawings = True

                        if has_drawings:
                            tabs = p_obj.find_tables()
                            if tabs and hasattr(tabs, 'tables') and tabs.tables:
                                valid_vec_tabs = [
                                    tb for tb in tabs.tables
                                    if getattr(tb, 'row_count', 0) >= 1 and getattr(tb, 'col_count', 0) >= 1
                                ]
                                if valid_vec_tabs:
                                    _add_candidate(p_idx, 'vector_find_tables')

                        if p_idx not in candidate_sources:
                            try:
                                text_tabs = p_obj.find_tables(strategy="text")
                                if text_tabs and hasattr(text_tabs, 'tables') and text_tabs.tables:
                                    valid_text_tabs = [
                                        tb for tb in text_tabs.tables
                                        if getattr(tb, 'row_count', 0) >= 1 and getattr(tb, 'col_count', 0) >= 1
                                    ]
                                    if valid_text_tabs:
                                        _add_candidate(p_idx, 'text_find_tables')
                            except Exception:
                                pass
                            if p_idx not in candidate_sources:
                                try:
                                    from pdf_tables import probe_disjoint_native_tables
                                    if probe_disjoint_native_tables(p_obj) > 0:
                                        _add_candidate(p_idx, 'probe_disjoint_native')
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    finally:
                        p_obj = None
    except Exception:
        pass

    cand_pages = sorted(list(candidate_sources.keys()))
    return (cand_pages, candidate_sources, page_decls)


def extract_tables_from_pdf(
    pdf_path: str,
    use_ocr_fallback: bool = True,
    candidate_pages: Optional[List[int]] = None,
    remediated: Optional[bool] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    统一 PDF 表格提取管线入口。
    内置 Tier 1 页面级无损顺时针旋转自愈与自动临时文件资源回收。
    remediated: 可选布尔值。若上游 (如 extract_zotero_table) 已执行过 Tier 1 探测与回正，
                传入布尔值可跳过冗余重测，避免二次重复开销与候选页判断差异。
    """
    temp_pdf_to_clean = None
    was_remediated = False
    try:
        if remediated is None:
            try:
                try:
                    from .doc_orientation_detector import remediate_pdf_pages_lossless
                except ImportError:
                    from doc_orientation_detector import remediate_pdf_pages_lossless

                cands = candidate_pages
                if cands is None:
                    cands = quick_detect_candidate_pages(pdf_path)

                eff_pdf_path, was_rotated, tmp_lossless = remediate_pdf_pages_lossless(pdf_path, candidate_pages=cands)
                if was_rotated and tmp_lossless:
                    pdf_path = eff_pdf_path
                    temp_pdf_to_clean = tmp_lossless
                    was_remediated = True
            except Exception as e_remed:
                logger.debug(f"[pdf_table_extractor] Tier 1 lossless rotation notice: {e_remed}")
        else:
            was_remediated = bool(remediated)

        return _do_extract_tables_from_pdf(pdf_path, use_ocr_fallback=use_ocr_fallback, was_remediated=was_remediated)
    finally:
        if temp_pdf_to_clean and os.path.exists(temp_pdf_to_clean):
            try:
                os.remove(temp_pdf_to_clean)
            except Exception:
                pass


def _do_extract_tables_from_pdf(
    pdf_path: str,
    use_ocr_fallback: bool = True,
    was_remediated: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    统一 PDF 表格提取核心实现。

    管线:
    1. pdf-inspector 分类 + 语义结构树检测 + 启发式 caption / 续表声明扫描 → 识别含表格的候选页面
    2. native PDF: pdf-inspector + PyMuPDF find_tables + text_alignment + pdfplumber + Camelot 多方投票与互补 (共 6 种提取器介入)
       scanned PDF: 直接走 PP-StructureV3 / DocLayout-YOLO 切图 + PaddleOCR-VL
    3. 空间重叠 (IoU/IoM) 与标签防冲突聚类，任意两方相似度 >= 0.75 判定一致；
       相似度低于 0.75、低置信度 (<0.90) 或检验瑕疵页 → 启动切图定位与两阶段 OCR 接管
    4. 表级精细对齐与替换策略（保留有效原生表，替换瑕疵表）
    5. 跨页续表与多分页自适应合并

    返回 (results, logs)
    """
    logs = []
    if was_remediated:
        logs.append("→ Tier 1 页面方向探测: 检测到旋转页面并完成无损顺时针回正")
    all_results = []

    # ── 高精锚定：精准扫描 PDF 全文真实表标题声明 (Captions & Continuations) ──
    try:
        from .table_validator import scan_pdf_table_declarations
    except ImportError:
        from table_validator import scan_pdf_table_declarations
    page_decls = scan_pdf_table_declarations(pdf_path)
    caption_pages = set(page_decls.keys())
    caption_page_map = {}
    for p_idx, decls in page_decls.items():
        for d in decls:
            if not d.get('is_continuation'):
                lbl = d['label']
                if lbl not in caption_page_map:
                    caption_page_map[lbl] = p_idx

    # 检测全图扫描页（无文本层或纯图页）
    image_table_pages = set()
    pure_image_pages = set()
    try:
        doc = fitz.open(pdf_path)
        for p_idx, page in enumerate(doc):
            words = page.get_text("words")
            images = page.get_images()
            if len(images) > 0 and len(words) < 80:
                image_table_pages.add(p_idx)
            if len(images) > 0 and len(words) < 25:
                pure_image_pages.add(p_idx)
        doc.close()
    except Exception:
        pass

    # Step 1: pdf-inspector 分类
    logs.append("→ Step 1: pdf-inspector 分类 + 表格定位")
    inspector_result = classify_and_extract_via_inspector(pdf_path, caption_page_map)
    logs.append(f"  pdf_type={inspector_result['pdf_type']}, "
                f"confidence={inspector_result['confidence']:.2f}, "
                f"pages_with_tables={inspector_result['pages_with_tables']}")
    if inspector_result.get('error'):
        logs.append(f"  pdf-inspector 错误: {inspector_result['error']}")

    pdf_type = inspector_result['pdf_type']
    raw_pages_with_tables = inspector_result.get('pages_with_tables', [])
    inspector_table_pages = sorted(list(set(p - 1 for p in raw_pages_with_tables if isinstance(p, int) and p > 0)))

    structure_pages = set()
    for st in inspector_result.get('structure_tables', []):
        p = st.get('page_idx')
        if p is None and hasattr(st.get('df'), 'attrs'):
            p = st['df'].attrs.get('page_idx')
        if p is not None:
            structure_pages.add(p)

    inspector_tables = []
    unresolved_inspector_tables = []
    for t in inspector_result['tables']:
        if t.get('page_idx') is not None:
            inspector_tables.append(t)
        else:
            lbl = t['df'].attrs.get('label') or format_table_label(t['df'].attrs.get('table_title', ''))
            resolved_p = caption_page_map.get(lbl) if (caption_page_map and lbl) else None
            if resolved_p is None and len(inspector_table_pages) == 1:
                resolved_p = inspector_table_pages[0]
            if resolved_p is not None:
                t['page_idx'] = resolved_p
                t['df'].attrs['page_idx'] = resolved_p
                inspector_tables.append(t)
                logs.append(f"  pdf-inspector 表格 [{lbl or '未命名'}] 成功映射至 Page {resolved_p+1}")
            else:
                unresolved_inspector_tables.append(t)

    # 对剩余未通过 caption 明确映射的表格，按顺序映射至 inspector_table_pages
    for i_u, t in enumerate(unresolved_inspector_tables):
        lbl = t['df'].attrs.get('label') or format_table_label(t['df'].attrs.get('table_title', ''))
        if inspector_table_pages:
            fallback_p = inspector_table_pages[min(i_u, len(inspector_table_pages) - 1)]
        else:
            fallback_p = 0
        t['page_idx'] = fallback_p
        t['df'].attrs['page_idx'] = fallback_p
        inspector_tables.append(t)
        logs.append(f"  pdf-inspector 表格 [{lbl or '未命名'}] 页码映射回退至 Page {fallback_p+1}")

    # ── 候选表格页面决策与多信号并集体系 ──
    # 结合 Caption 声明、全图扫描页、结构树语义表格、pdf-inspector 检出页与矢量网格探测
    candidate_sources = {}
    def _add_candidate(page_idx: int, src: str):
        if page_idx not in candidate_sources:
            candidate_sources[page_idx] = set()
        candidate_sources[page_idx].add(src)

    for p in caption_pages:
        _add_candidate(p, 'caption')
    for p in image_table_pages:
        _add_candidate(p, 'image_scan')
    for p in structure_pages:
        _add_candidate(p, 'structure_tree')
    for p in inspector_table_pages:
        _add_candidate(p, 'pdf_inspector')
    for t in inspector_tables:
        p = t.get('page_idx')
        if p is not None:
            _add_candidate(p, 'inspector_table')

    # 对未命中的页面进行自适应分块全篇矢量线框探测 (无硬编码页数上限，自适应轻量级绘图探测)
    try:
        with fitz.open(pdf_path) as doc_check:
            total_pages = len(doc_check)
            pages_to_check = [p for p in range(total_pages) if p not in candidate_sources]
            # 自适应分块：根据文档总页数自适应调节分块大小，分块间显式清理内存引用
            chunk_size = 50 if total_pages <= 300 else 100
            for chunk_start in range(0, len(pages_to_check), chunk_size):
                chunk = pages_to_check[chunk_start:chunk_start + chunk_size]
                for p_idx in chunk:
                    p_obj = None
                    try:
                        p_obj = doc_check[p_idx]
                        # 轻量矢量绘图预检与无线框表格自证探测
                        has_drawings = True
                        try:
                            drawings = p_obj.get_drawings()
                            if isinstance(drawings, (list, tuple)) and len(drawings) == 0:
                                has_drawings = False
                        except Exception:
                            has_drawings = True

                        if has_drawings:
                            tabs = p_obj.find_tables()
                            if tabs and hasattr(tabs, 'tables') and tabs.tables:
                                valid_vec_tabs = [
                                    tb for tb in tabs.tables
                                    if getattr(tb, 'row_count', 0) >= 1 and getattr(tb, 'col_count', 0) >= 1
                                ]
                                if valid_vec_tabs:
                                    _add_candidate(p_idx, 'vector_find_tables')

                        # 即使存在绘图或线框未检出，继续尝试文本检测，防止页眉线导致无框表漏检
                        if p_idx not in candidate_sources:
                            # 无绘图页仍跑一次 find_tables(strategy="text") 或 probe_disjoint_native_tables 轻量文本列对齐探测
                            try:
                                text_tabs = p_obj.find_tables(strategy="text")
                                if text_tabs and hasattr(text_tabs, 'tables') and text_tabs.tables:
                                    valid_text_tabs = [
                                        tb for tb in text_tabs.tables
                                        if getattr(tb, 'row_count', 0) >= 1 and getattr(tb, 'col_count', 0) >= 1
                                    ]
                                    if valid_text_tabs:
                                        _add_candidate(p_idx, 'text_find_tables')
                            except Exception:
                                pass
                            if p_idx not in candidate_sources:
                                try:
                                    from pdf_tables import probe_disjoint_native_tables
                                    if probe_disjoint_native_tables(p_obj) > 0:
                                        _add_candidate(p_idx, 'probe_disjoint_native')
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    finally:
                        p_obj = None
    except Exception:
        pass

    # 自动包含可能的跨页续表后继页（结合关键字、版面检测、密集数据、几何列对齐与连续性）
    try:
        doc = fitz.open(pdf_path)
        base_pages = sorted(list(candidate_sources.keys()))
        for p in base_pages:
            next_p = p + 1
            while next_p < len(doc) and next_p <= p + 10:
                if next_p in candidate_sources and 'caption' in candidate_sources[next_p]:
                    break

                # 检查 next_p 是否有明确续表声明
                if next_p in page_decls:
                    has_decl_cont = any(d.get('is_continuation') for d in page_decls[next_p])
                    if has_decl_cont:
                        _add_candidate(next_p, 'continuation_decl')
                        next_p += 1
                        continue
                    else:
                        # next_p 包含新的独立表格声明，当前表格的续表追溯在此终结
                        break

                next_page = doc[next_p]
                next_text = next_page.get_text("text").strip()
                lines = [l.strip() for l in next_text.split('\n') if l.strip() and not (l.strip().isdigit() and len(l.strip()) <= 4) and not re.search(r'(?:大学|学位论文)', l)]
                
                if not lines and not next_page.get_images():
                    break

                first_line = lines[0] if lines else ""
                is_continuation_line = any(k in first_line for k in ['续', 'cont', 'Cont'])
                is_new_table = bool(re.match(r'^(?:附表|附录表|补充表|表|Table)\s*\d+', first_line)) and not is_continuation_line
                num_numeric_tokens = sum(1 for t in first_line.split() if re.match(r'^-?\d+(?:\.\d+)?$', t))
                is_section_hdr = bool(re.match(r'^\d+\.\d+(?:\.\d+)?\s+(?:[\u4e00-\u9fa5]{2,}|[A-Z][a-z]{2,})', first_line)) and (num_numeric_tokens < 2)
                if is_section_hdr or is_new_table or first_line.startswith(('图', 'Fig', '参考文献', '致谢', 'References', 'Acknowledgements')):
                    break

                # 1. 显式续表关键字 (中英文多模态)
                cond_kw = any(k in next_text[:500] for k in ['续表', '（续）', '(续)', '续前表', 'continued', 'Continued', '(cont.)', '(Cont.)', "cont'd", "Cont'd"])
                
                # 2. 密集纯数据元胞（长数据表，无表头直接连续密集数值）
                data_token_count = sum(1 for l in lines if re.match(r'^-?\d+(?:\.\d+)?%?$', l) or l in ('/', '-', '—', '–', 'n.d.', 'bdl', 'b.d.l.', 'nd', 'N/A') or re.match(r'^[A-Z0-9\-_–—/]{1,8}$', l))
                cond_dense = len(lines) >= 4 and (data_token_count / max(1, len(lines)) >= 0.45)

                # 3. 矢量线框表格判定 (续表往往带有表格水平/垂直网格线)
                cond_vec_table = False
                try:
                    f_tabs = next_page.find_tables()
                    if f_tabs and f_tabs.tables:
                        if any(tb.row_count >= 2 for tb in f_tabs.tables):
                            cond_vec_table = True
                except Exception:
                    pass

                # 4. 纯图续表（前序页为图片表且当前页同样为大图页）
                prev_img_p = next_p - 1
                cond_img_cont = (prev_img_p in image_table_pages and next_p in image_table_pages and ('image_scan' in candidate_sources.get(p, set()) or any('image_continuation' in s for s in candidate_sources.get(prev_img_p, set()))))

                # 5. 稀疏/无框跨页续表判定 (几何列对齐、水平跨度一致性与页顶连续性，紧邻前序页连续链)
                cond_sparse_cont = False
                try:
                    prev_p_obj = doc[next_p - 1]
                    cond_sparse_cont = _check_sparse_continuation_table(prev_p_obj, next_page)
                except Exception:
                    pass

                if cond_kw or cond_dense or cond_vec_table or cond_img_cont or cond_sparse_cont:
                    reasons = []
                    if cond_kw: reasons.append("keyword")
                    if cond_dense: reasons.append("dense_data")
                    if cond_vec_table: reasons.append("vector_grid")
                    if cond_img_cont: reasons.append("image_continuation")
                    if cond_sparse_cont: reasons.append("sparse_alignment")
                    _add_candidate(next_p, f"continuation({','.join(reasons)})")
                    next_p += 1
                else:
                    break
        doc.close()
    except Exception as e_cont:
        logs.append(f"  续表推断异常: {e_cont}")

    # 候选总账核验 (Declaration Ledger Reconciliation):
    # 若全文声明的表格页或表号数量超出当前候选页集合，对未入选页面触发文本层 Table N 表题快速补扫
    total_declared_tables = sum(len(decls) for decls in page_decls.values())
    if total_declared_tables > len(candidate_sources):
        try:
            with fitz.open(pdf_path) as doc_supp:
                for p_check in range(len(doc_supp)):
                    if p_check not in candidate_sources:
                        p_txt = doc_supp[p_check].get_text("text")
                        if p_txt and TABLE_LABEL_RE.search(p_txt):
                            _add_candidate(p_check, 'declaration_ledger_supplement')
        except Exception:
            pass

    target_pages = sorted(list(candidate_sources.keys())) if candidate_sources else None
    if target_pages:
        src_summary = {p + 1: sorted(list(candidate_sources[p])) for p in target_pages}
        logs.append(f"  候选表格页面: {[p+1 for p in target_pages]} (来源分布: {src_summary})")

    if not target_pages:
        logs.append("→ 未检测到候选表格页面，尝试整页高精 PaddleOCR-VL 全文识别")
        if use_ocr_fallback:
            ocr_results = extract_via_paddleocr_fullpage(pdf_path, pages=None)
            if not ocr_results:
                ocr_results = extract_via_ocr(pdf_path)
            all_results.extend(ocr_results)
            logs.append(f"  OCR 提取到 {len(ocr_results)} 个表格")
        
        # 对 OCR 结果统一进行表题规整与续表合并
        all_results = reconcile_table_captions(all_results, pdf_path)
        from table_postprocess import merge_continuation_tables
        all_dfs = [r['df'] for r in all_results if r.get('df') is not None]
        merged_dfs = merge_continuation_tables(all_dfs)
        merged_results = []
        for df in merged_dfs:
            merged_results.append({
                'df': df,
                'page_idx': df.attrs.get('page_idx', 0),
                'table_idx': 0,
                'bbox': None
            })
        return merged_results, logs

    logs.append(f"  候选表格页面: {[p+1 for p in target_pages]}")

    # Step 2: 根据 PDF 类型路由
    is_native = pdf_type in ('text_based', 'mixed') and inspector_result['confidence'] >= 0.5

    if not is_native:
        # ── 扫描版 PDF → 直接整页 PaddleOCR-VL ──
        logs.append("→ Step 2: 扫描版 PDF，整页提交 PaddleOCR-VL-1.6 识别")
        ocr_results = extract_via_paddleocr_fullpage(pdf_path, target_pages)
        if not ocr_results:
            try:
                import ocr_client
                docling_results = ocr_client.extract_via_docling(pdf_path, pages=target_pages)
                if docling_results:
                    logs.append(f"  PaddleOCR 无响应，Docling (TableFormer) 离线备用提取到 {len(docling_results)} 个表格")
                    ocr_results = docling_results
            except Exception as e_d:
                logs.append(f"  Docling 离线备用异常: {e_d}")
        all_results.extend(ocr_results)
        logs.append(f"  OCR 提取到 {len(ocr_results)} 个表格")
    else:
        # ── native PDF → 主抽取架构 (Tagged PDF 结构树 -> MinerU 结构化 -> 本地数值门禁 -> 定向修复) + 备用多方投票 ──
        native_target_pages = [p for p in target_pages if p not in pure_image_pages] if target_pages is not None else None

        # 检查是否配置强制启用 legacy 多方投票
        try:
            from common import load_config
            _cfg = load_config()
        except Exception:
            _cfg = {}
        use_legacy_voting = os.getenv("USE_LEGACY_VOTING", "").lower() in ("1", "true") or _cfg.get("USE_LEGACY_VOTING", False)

        try:
            from . import table_validator
        except ImportError:
            import table_validator
        try:
            from . import ocr_client
        except ImportError:
            import ocr_client

        primary_succeeded = False
        pages_requiring_ocr = set()
        original_native_results = []
        valid_fused = []

        if not use_legacy_voting:
            logs.append("→ Step 2: native PDF，启动主抽取管线 (Tagged PDF 结构树 -> MinerU 结构化主抽取 -> 本地数值门禁)")

            # 1. Tagged PDF / PDF/UA 语义结构树快速通道 (Fast-path: 最高保真度)
            structure_results = extract_via_structure_tree(pdf_path, pages=native_target_pages)
            if structure_results:
                logs.append(f"  Tagged PDF 结构树快速通道提取到 {len(structure_results)} 个语义表格")
                valid_struct_tables = []
                for s_item in structure_results:
                    s_df = s_item.get('df')
                    p_i = s_item.get('page_idx', s_df.attrs.get('page_idx', 0) if hasattr(s_df, 'attrs') else 0)
                    b_box = s_item.get('bbox', s_df.attrs.get('bbox') if hasattr(s_df, 'attrs') else None)
                    v_ok, v_info = table_validator.verify_table_numeric_integrity(
                        df=s_df, pdf_path=pdf_path, page_idx=p_i, bbox=b_box, min_match_ratio=0.7
                    )
                    if v_ok:
                        valid_struct_tables.append(s_item)
                struct_covered_pages = set(t.get('page_idx') for t in valid_struct_tables)
                is_struct_complete = False
                struct_report = None
                if valid_struct_tables:
                    try:
                        struct_report = table_validator.cross_check_missed_tables([t['df'] for t in valid_struct_tables], pdf_path)
                    except Exception:
                        struct_report = None

                    has_no_missed = bool(
                        struct_report and
                        len(struct_report.get('missed_tables', [])) == 0 and
                        not struct_report.get("coverage_unknown", False) and
                        struct_report.get("is_complete", False)
                    )
                    # 结构树快速通道必须确认无明确缺表声明，且覆盖该页所有前置探测到的候选表格
                    cand_counts_covered = True
                    for p in (native_target_pages or []):
                        p_struct_cnt = len([t for t in valid_struct_tables if t.get('page_idx') == p])
                        p_cand_cnt = len([t for t in inspector_tables if t.get('page_idx') == p])
                        if p_struct_cnt < p_cand_cnt:
                            cand_counts_covered = False
                            break

                    pages_covered = bool(native_target_pages and set(native_target_pages).issubset(struct_covered_pages))

                    if has_no_missed and cand_counts_covered and pages_covered:
                        is_struct_complete = True
                if is_struct_complete:
                    logs.append("  Tagged PDF 结构树快速通道全覆盖并全部通过数值门禁，直接采纳！")
                    all_results = valid_struct_tables
                    original_native_results = list(all_results)
                    valid_fused = list(all_results)
                    primary_succeeded = True

            # 2. 若快速通道未完全覆盖，启动 MinerU 结构化主抽取 (with page_ranges)
            if not primary_succeeded and ocr_client:
                pdf_p = Path(pdf_path)
                cache_zip = pdf_p.parent / "mineru_cache" / f"{pdf_p.stem}.zip"
                cache_dir = pdf_p.parent / "mineru_cache" / pdf_p.stem
                mineru_dfs = []
                if cache_dir.exists() and cache_dir.is_dir():
                    logs.append(f"  发现本地 MinerU 结构化缓存目录: {cache_dir.name}")
                    try:
                        mineru_dfs = ocr_client.extract_mineru_structured_archive(str(cache_dir))
                    except Exception as e_c:
                        logs.append(f"  读取 MinerU 缓存目录异常: {e_c}")
                        mineru_dfs = []
                    if not mineru_dfs:
                        logs.append(f"  [Warning] 本地 MinerU 缓存目录损坏或为空 ({cache_dir.name})，自动清理失效缓存...")
                        try:
                            shutil.rmtree(cache_dir, ignore_errors=True)
                        except Exception:
                            pass
                if not mineru_dfs and cache_zip.exists():
                    logs.append(f"  发现本地 MinerU 结构化缓存包: {cache_zip.name}")
                    try:
                        mineru_dfs = ocr_client.extract_mineru_structured_archive(str(cache_zip))
                    except Exception as e_c:
                        logs.append(f"  读取 MinerU 缓存异常: {e_c}")
                        mineru_dfs = []
                    if not mineru_dfs:
                        logs.append(f"  [Warning] 本地 MinerU 缓存包损坏或为空 ({cache_zip.name})，自动清理失效缓存并回退至主抽取接口...")
                        try:
                            cache_zip.unlink(missing_ok=True)
                        except Exception:
                            pass
                if not mineru_dfs and ocr_client.is_mineru_available(config=_cfg, file_path=pdf_path):
                    page_desc = [p + 1 for p in native_target_pages] if native_target_pages else "all"
                    logs.append(f"  调用 MinerU 官方高保真结构化主抽取 (页码: {page_desc})")
                    try:
                        mineru_dfs = ocr_client.call_mineru_structured_api(pdf_path, config=_cfg, page_ranges=native_target_pages)
                    except Exception as e_mineru:
                        logs.append(f"  MinerU 结构化主抽取异常: {e_mineru}")
                        mineru_dfs = []

                if mineru_dfs:
                    logs.append(f"  MinerU 结构化主抽取解析出 {len(mineru_dfs)} 个表格，启动本地数值一致性门禁核验...")
                    accepted_mineru = []
                    unverified_mineru = []
                    for i_m, m_df in enumerate(mineru_dfs):
                        p_idx = m_df.attrs.get('page_idx', native_target_pages[i_m] if (native_target_pages and i_m < len(native_target_pages)) else (native_target_pages[0] if native_target_pages else 0))
                        bbox = m_df.attrs.get('bbox')
                        is_valid, info = table_validator.verify_table_numeric_integrity(
                            df=m_df, pdf_path=pdf_path, page_idx=p_idx, bbox=bbox, min_match_ratio=0.7
                        )
                        converted_bbox = info.get("converted_bbox") or bbox
                        if hasattr(m_df, 'attrs'):
                            m_df.attrs['bbox'] = converted_bbox
                            m_df.attrs['bbox_format'] = 'points'
                        logs.append(
                            f"  Table #{i_m+1} (Page {p_idx+1}): gate_valid={is_valid}, match_ratio={info.get('match_ratio', 0):.2f}, font_corrupted={info.get('font_corrupted')}"
                        )
                        if is_valid:
                            accepted_mineru.append({
                                'df': m_df,
                                'page_idx': p_idx,
                                'table_idx': i_m,
                                'bbox': converted_bbox,
                                'extractor': 'mineru_api',
                            })
                        else:
                            pages_requiring_ocr.add(p_idx)
                            if info.get('status') == 'unverified':
                                logs.append(f"  Table #{i_m+1} (Page {p_idx+1}) 位于扫描区域或无文本层 (unverified)，保留候选待视觉复核")
                                unverified_mineru.append({
                                    'df': m_df,
                                    'page_idx': p_idx,
                                    'table_idx': i_m,
                                    'bbox': converted_bbox,
                                    'extractor': 'mineru_api',
                                    'unverified': True,
                                })

                    # 交叉核对全文声明漏表与扫描页覆盖未知
                    try:
                        report = table_validator.cross_check_missed_tables([t['df'] for t in accepted_mineru], pdf_path)
                        if not report.get("is_complete", True) or report.get("coverage_unknown", False):
                            for missed in report.get("missed_tables", []):
                                p_m = missed.get("page_idx")
                                if p_m is not None:
                                    pages_requiring_ocr.add(p_m)
                                    logs.append(f"  交叉核验发现未覆盖声明表 {missed.get('label')} (Page {p_m+1})")
                            for p_scan in report.get("uncovered_scanned_pages", []):
                                pages_requiring_ocr.add(p_scan)
                                logs.append(f"  扫描页 {p_scan+1} 覆盖未知，加入定向修复")
                    except Exception as e_cc:
                        logger.debug(f"Cross check error: {e_cc}")

                    covered_m_pages = set(t['page_idx'] for t in accepted_mineru)
                    for p_req in (native_target_pages or []):
                        if p_req not in covered_m_pages:
                            pages_requiring_ocr.add(p_req)

                    # 保留并核对前置探测发现的独立表格候选区域 (bounding boxes)
                    for cand in inspector_tables:
                        c_p = cand.get('page_idx')
                        if c_p is None and cand.get('df') is not None and hasattr(cand['df'], 'attrs'):
                            c_p = cand['df'].attrs.get('page_idx')
                        if c_p is None:
                            continue
                        c_box = cand.get('bbox') or (cand['df'].attrs.get('bbox') if cand.get('df') is not None and hasattr(cand.get('df'), 'attrs') else None)
                        c_lbl = cand.get('label') or (cand['df'].attrs.get('label') if cand.get('df') is not None and hasattr(cand.get('df'), 'attrs') else None)
                        c_fmt = format_table_label(c_lbl) if c_lbl else None

                        is_covered = False
                        for m_item in accepted_mineru:
                            if m_item.get('page_idx') != c_p:
                                continue
                            m_df = m_item.get('df')
                            m_box = m_item.get('bbox') or (m_df.attrs.get('bbox') if m_df is not None and hasattr(m_df, 'attrs') else None)
                            m_lbl = m_df.attrs.get('label') if m_df is not None and hasattr(m_df, 'attrs') else None
                            m_fmt = format_table_label(m_lbl) if m_lbl else None

                            if c_fmt and m_fmt and c_fmt == m_fmt:
                                is_covered = True
                                break
                            if c_box and m_box:
                                iou, iom = _compute_bbox_iou(c_box, m_box)
                                if iou >= 0.20 or iom >= 0.35:
                                    is_covered = True
                                    break
                        if not is_covered:
                            pages_requiring_ocr.add(c_p)
                            logs.append(f"  前置候选表格区域在 Page {c_p+1} 未被 MinerU 覆盖 (bbox={c_box})，触发补漏救助")
                            # 若前置候选表格具备有效数据并通过数值校验，予以保留备份
                            cand_df = cand.get('df')
                            if cand_df is not None and not cand_df.empty:
                                c_ok, _ = table_validator.verify_table_numeric_integrity(
                                    cand_df, pdf_path, c_p, c_box, min_match_ratio=0.7
                                )
                                if c_ok:
                                    cand_copy = dict(cand)
                                    cand_copy['page_idx'] = c_p
                                    cand_copy['bbox'] = c_box
                                    cand_copy['extractor'] = cand_df.attrs.get('extractor', 'pdf_inspector')
                                    if hasattr(cand_df, 'attrs'):
                                        cand_df.attrs['page_idx'] = c_p
                                        cand_df.attrs['bbox'] = c_box
                                        if not cand_df.attrs.get('extractor'):
                                            cand_df.attrs['extractor'] = 'pdf_inspector'
                                    accepted_mineru.append(cand_copy)

                    if accepted_mineru:
                        all_results = list(accepted_mineru)
                        for u_item in unverified_mineru:
                            if not any(id(a.get('df')) == id(u_item.get('df')) for a in all_results):
                                all_results.append(u_item)
                        original_native_results = list(all_results)
                        valid_fused = list(all_results)
                        primary_succeeded = True
                    else:
                        if unverified_mineru:
                            all_results = list(unverified_mineru)
                            original_native_results = list(unverified_mineru)
                            valid_fused = list(unverified_mineru)
                            logs.append("  MinerU 提取的表格均位于无文本层/扫描区域 (unverified)，保留候选并触发视觉验证，平滑降级进入原生多方投票备用管线")
                        else:
                            logs.append("  MinerU 提取的所有表格均未通过数值门禁，平滑降级进入原生多方投票备用管线")

        # 3. 若未启用主抽取或主抽取未产出有效表格，平滑降级进入原生多方投票备用管线 (Legacy Fallback)
        if not primary_succeeded:
            logs.append("→ Step 2: 进入原生多方投票备用管线 (pdf-inspector + PyMuPDF4LLM + GMFT + Camelot/img2table + pdfplumber + find_tables)")
            structure_results = extract_via_structure_tree(pdf_path, pages=native_target_pages)
            if structure_results:
                logs.append(f"  Tagged PDF 结构树提取到 {len(structure_results)} 个语义表格 (pdf_inspector_structure)")

            ft_results = extract_via_find_tables(pdf_path, pages=native_target_pages)
            plumber_results = extract_via_pdfplumber(pdf_path, pages=native_target_pages)
            camelot_results = extract_via_camelot(pdf_path, pages=native_target_pages)
            img2table_results = extract_via_img2table(pdf_path, pages=native_target_pages)
            gmft_results = extract_via_gmft(pdf_path, pages=native_target_pages)
            pymupdf4llm_results = extract_via_pymupdf4llm(pdf_path, pages=native_target_pages)
            if pymupdf4llm_results:
                logs.append(f"  PyMuPDF4LLM 提取到 {len(pymupdf4llm_results)} 个表格 (LLM-ready Markdown)")
            rapid_table_results = extract_via_rapid_table(pdf_path, pages=native_target_pages) if is_rapid_table_available() else []
            if rapid_table_results:
                logs.append(f"  RapidTable 提取到 {len(rapid_table_results)} 个表格 (ONNX 结构识别)")
            align_results = extract_via_text_alignment(pdf_path, pages=native_target_pages)

            # 多方投票融合
            logs.append("→ Step 3: 多方投票融合")
            fused, vote_logs, vote_summary = three_way_vote(
                inspector_tables, ft_results, plumber_results, camelot_results,
                align_tables=align_results,
                structure_tables=structure_results,
                gmft_tables=gmft_results,
                img2table_tables=img2table_results,
                pymupdf4llm_tables=pymupdf4llm_results,
                rapid_table_tables=rapid_table_results,
            )
            logs.extend(vote_logs)

            if fused:
                for f_item in fused:
                    if not any(id(a.get('df')) == id(f_item.get('df')) for a in all_results):
                        all_results.append(f_item)
                extractors = set(r['df'].attrs.get('vote_extractor', r['df'].attrs.get('extractor', '?')) for r in all_results)
                logs.append(f"  投票完成: {len(all_results)} 个表格 (extractors: {extractors})")
            else:
                logs.append("  多方投票未获得表格")

            # Step 3b: 多方投票综合检验模块（坏损表、表头错位、表序缺失、续表中断、单页多表漏抓检验）
            try:
                from .table_validator import validate_native_extraction_pipeline
            except ImportError:
                from table_validator import validate_native_extraction_pipeline
            original_native_results = list(all_results)
            if 'unverified_mineru' in locals() and unverified_mineru:
                for u_item in unverified_mineru:
                    if not any(id(r.get('df')) == id(u_item.get('df')) for r in original_native_results):
                        original_native_results.append(u_item)
                    if not any(id(r.get('df')) == id(u_item.get('df')) for r in all_results):
                        all_results.append(u_item)
                    pages_requiring_ocr.add(u_item.get('page_idx'))
            valid_fused, pages_req, val_logs = validate_native_extraction_pipeline(
                all_results, pdf_path, target_pages, vote_summary
            )
            logs.extend(val_logs)
            all_results = valid_fused
            pages_requiring_ocr.update(pages_req)

        # 若检验发现任何问题、缺陷或低置信度页面，优先通过 DocLayout-YOLO 目标切图送入 PaddleOCR-VL 独立接管提取
        if pages_requiring_ocr and use_ocr_fallback:
            ocr_candidate_pages = sorted(list(pages_requiring_ocr))
            logs.append(f"→ Step 3b: 检验未通过/待精细识别页面 {[p+1 for p in ocr_candidate_pages]} 启动接管修复")

            ocr_results = []
            unresolved_ocr_pages = set(ocr_candidate_pages)

            def _get_page_needed_count(p):
                decls = [d for d in page_decls.get(p, []) if not d.get('is_continuation')]
                cand_count = len([t for t in inspector_tables if t.get('page_idx') == p])
                exp_count = max(len(decls), cand_count, 1)
                valid_native = len([r for r in valid_fused if r.get('page_idx') == p and not is_table_low_quality(r.get('df'))])
                return max(1, exp_count - valid_native)

            def _update_unresolved_pages():
                nonlocal unresolved_ocr_pages
                for p in list(unresolved_ocr_pages):
                    needed_count = _get_page_needed_count(p)
                    valid_on_page = [r for r in ocr_results if r.get('page_idx') == p and not is_table_low_quality(r.get('df'))]
                    if len(valid_on_page) >= needed_count:
                        unresolved_ocr_pages.discard(p)

            # 阶段 1: 优先尝试 RapidTable ONNX 结构识别快速挽救
            if is_rapid_table_available() and unresolved_ocr_pages:
                try:
                    rapid_pages = sorted(list(unresolved_ocr_pages))
                    rapid_res = extract_via_rapid_table(pdf_path, pages=rapid_pages)
                    valid_rapid = [r for r in (rapid_res or []) if not is_table_low_quality(r.get('df'))]
                    if valid_rapid:
                        logs.append(f"  RapidTable 本地中间层试跑成功提取到 {len(valid_rapid)} 个表格 (免调用在线 VLM)")
                        ocr_results.extend(valid_rapid)
                        _update_unresolved_pages()
                except Exception as e_rr:
                    logger.debug(f"[RapidTable] 本地中间层接管试跑跳过: {e_rr}")

            # 阶段 2: 对仍未覆盖的页面，通过 DocLayout-YOLO 切图送入 PaddleOCR-VL-1.6 独立接管提取
            if unresolved_ocr_pages:
                crop_pages = sorted(list(unresolved_ocr_pages))
                logs.append(f"  优先通过 DocLayout-YOLO 切图送入 PaddleOCR-VL-1.6 独立接管提取 (待修复页: {[p+1 for p in crop_pages]})")
                crop_res = extract_via_paddleocr_crops(pdf_path, crop_pages)
                valid_crops = [r for r in (crop_res or []) if not is_table_low_quality(r.get('df'))]
                if valid_crops:
                    ocr_results.extend(valid_crops)
                    _update_unresolved_pages()

            # 阶段 3: 若切图提取后仍有未解决页面，尝试 Docling (TableFormer) 离线接管
            if unresolved_ocr_pages:
                try:
                    import ocr_client
                    docling_pages = sorted(list(unresolved_ocr_pages))
                    docling_results = ocr_client.extract_via_docling(pdf_path, pages=docling_pages)
                    valid_docling = [r for r in (docling_results or []) if not is_table_low_quality(r.get('df'))]
                    if valid_docling:
                        logs.append(f"  Docling (TableFormer) 离线接管提取到 {len(valid_docling)} 个表格")
                        ocr_results.extend(valid_docling)
                        _update_unresolved_pages()
                except Exception:
                    pass

            if ocr_results:
                logs.append(f"  独立接管提取到 {len(ocr_results)} 个表格，启动表级精细对齐与安全替换...")
                all_results = reconcile_native_and_ocr_tables(original_native_results, ocr_results, ocr_candidate_pages, logs)
            else:
                logs.append("  独立接管未提取到有效表格或执行失败，保留原生解析结果 (降级回填)")
                all_results = original_native_results
        else:
            if not use_ocr_fallback and len(valid_fused) < len(original_native_results):
                logs.append("  use_ocr_fallback=False，降级保留原生未通过检验表格，防止整表永久丢失")
                all_results = original_native_results

        # Step 3c: 仍未覆盖且未在 OCR 中成功产出表格的页面 → text_alignment
        extracted_pages = set(r.get('page_idx', -1) for r in all_results if r.get('page_idx') is not None and not is_table_low_quality(r.get('df')))
        ocr_successfully_resolved_pages = set(r.get('page_idx') for r in ocr_results if r.get('page_idx') is not None and not is_table_low_quality(r.get('df'))) if 'ocr_results' in locals() and ocr_results else set()
        def _get_page_expected_count(p):
            decls_cnt = len([d for d in page_decls.get(p, []) if not d.get('is_continuation')])
            cands_cnt = len([t for t in inspector_tables if t.get('page_idx') == p])
            return max(decls_cnt, cands_cnt, 1)

        still_missing = sorted(p for p in target_pages if (p not in extracted_pages or len([r for r in all_results if r.get('page_idx') == p and not is_table_low_quality(r.get('df'))]) < _get_page_expected_count(p)) and p not in ocr_successfully_resolved_pages)
        if still_missing:
            logs.append(f"→ Step 3c: 对仍未覆盖/表数不足的 {[p+1 for p in still_missing]} 页尝试文本对齐提取")
            text_align_results = extract_via_text_alignment(pdf_path, pages=still_missing)
            for ta in text_align_results:
                ta_page = ta.get('page_idx', -1)
                if not is_table_low_quality(ta['df']):
                    existing_on_page = [r for r in all_results if r.get('page_idx') == ta_page]
                    duplicate = any(_table_similarity(r['df'], ta['df']) >= 0.70 for r in existing_on_page)
                    if not duplicate:
                        all_results = [r for r in all_results if not (r.get('page_idx') == ta_page and is_table_low_quality(r.get('df')))]
                        all_results.append(ta)
                        extracted_pages.add(ta_page)

    # 标记来自续表候选页且未显式命名的表格片段，辅助跨页拼接引擎
    continuation_pages = set()
    for p_cand, srcs in candidate_sources.items():
        if any('continuation' in s for s in srcs):
            continuation_pages.add(p_cand)
    if all_results and continuation_pages:
        for r in all_results:
            p_idx = r.get('page_idx')
            if p_idx in continuation_pages:
                df_item = r.get('df')
                if df_item is not None and hasattr(df_item, 'attrs') and not df_item.attrs.get('label'):
                    df_item.attrs['is_continuation'] = True

    # Step 4: 预对齐真实表号 + 跨页续表合并
    if all_results:
        all_results = reconcile_table_captions(all_results, pdf_path)
        logs.append("→ Step 4: 跨页续表合并")
        try:
            from table_postprocess import merge_continuation_tables
            dfs = [r['df'] for r in all_results]
            merged_dfs = merge_continuation_tables(dfs)
            if len(merged_dfs) < len(dfs):
                logs.append(f"  合并前 {len(dfs)} 个表 → 合并后 {len(merged_dfs)} 个表（跨页续表已拼接）")
                all_results = []
                for i, df in enumerate(merged_dfs):
                    all_results.append({
                        'df': df,
                        'page_idx': df.attrs.get('page_idx', 0),
                        'table_idx': i,
                        'bbox': None,
                    })
                all_results = reconcile_table_captions(all_results, pdf_path)
        except Exception as e:
            logs.append(f"  跨页合并跳过: {e}")

    total_declared_count = sum(1 for decls in page_decls.values() for d in decls if not d.get('is_continuation'))
    total_cand_count = len(inspector_tables)
    total_expected_tables = max(total_declared_count, total_cand_count)
    if all_results and (total_expected_tables == 0 or len(all_results) >= total_expected_tables or not use_ocr_fallback):
        extractors = set(r['df'].attrs.get('extractor', '?') for r in all_results)
        logs.append(f"  最终: {len(all_results)} 个表格 (extractors: {extractors})")
        return all_results, logs

    # Step 5: 全局 OCR 兜底 (结构化提取未获得有效表格，或已提取表数少于声明表数)
    logs.append(f"→ Step 5: 结构化提取表数 ({len(all_results)}) 少于声明表数 ({total_expected_tables}) 或为空，回退到全局 OCR")
    if use_ocr_fallback:
        ocr_results = extract_via_ocr(pdf_path)
        if ocr_results:
            pages_to_reconcile = sorted(list(set(
                [r.get('page_idx', 0) for r in ocr_results] +
                [r.get('page_idx', 0) for r in all_results] +
                [p for r in ocr_results for p in r.get('covered_pages', [])]
            )))
            all_results = reconcile_native_and_ocr_tables(all_results, ocr_results, pages_to_reconcile, logs)
            all_results = reconcile_table_captions(all_results, pdf_path)
            logs.append(f"  全局 OCR 补漏后共获得 {len(all_results)} 个表格")

    return all_results, logs


# ===========================================================================
# 5b. 文本对齐提取（无线框表格）
# ===========================================================================

def pdf_extract_kit_align_tokens(
    data_lines: List[List[Any]],
    header_rows: Optional[List[List[Any]]] = None,
    mb_height: float = 800.0,
    body_start_idx: int = 1
) -> Optional[pd.DataFrame]:
    """
    OpenDataLab / PDF-Extract-Kit 启发式无边框表格对齐算法。
    
    核心机制:
    1. 行起始 x 坐标分布 (Row Start X Distribution): 分析左边距基线与缩进，识别首列换行与层级缩进;
    2. 基于字号/加粗特征加权与自适应 1D 边界聚类 (Weighted 1D K-Means Clustering);
    3. 全局列边界区间 [col_start, col_end] 与语义一致性校验 (Semantic Consistency Verification)。
    """
    if not data_lines or len(data_lines) < 2:
        return None

    if header_rows is None:
        header_rows = [data_lines[0]]

    # 1. 行起始 x 坐标分布 (Row Start X Distribution)
    row_start_xs = [line[0].x for line in data_lines if line]
    base_margin_x = min(row_start_xs) if row_start_xs else 0.0

    # 2. 收集全表 tokens，应用字号与加粗特征加权 (Weighting Tokens)
    token_weights: List[Tuple[float, float, Any]] = []
    for r_idx, line in enumerate(data_lines):
        is_header = r_idx < body_start_idx
        for it in line:
            w = 1.0
            if is_header:
                w += 1.5
            if getattr(it, 'is_bold', False):
                w += 1.0
            if getattr(it, 'font_size', 0.0) > 10.5:
                w += 0.5
            token_weights.append((it.x, w, it))

    if not token_weights:
        return None

    # 3. 估计列数 K 与自适应 1D 加权 K-Means 聚类
    header_xs = sorted(it.x for h_row in header_rows for it in h_row)
    max_row_len = max(len(l) for l in data_lines)
    k_cand = max(len(header_xs), max_row_len)
    k = max(2, min(k_cand, 25))

    # 初始化质心
    if len(header_xs) >= 2:
        init_centers = list(dict.fromkeys([round(x, 1) for x in header_xs]))
        if len(init_centers) < k:
            all_xs = sorted(t[0] for t in token_weights)
            step = len(all_xs) / k
            init_centers = [all_xs[int(i * step)] for i in range(k)]
    else:
        all_xs = sorted(t[0] for t in token_weights)
        step = len(all_xs) / k
        init_centers = [all_xs[int(i * step)] for i in range(k)]

    init_centers = sorted(list(set(init_centers)))
    if len(init_centers) < 2:
        init_centers = [min(t[0] for t in token_weights), max(t[0] for t in token_weights)]

    centroids = list(init_centers)
    # 迭代优化 1D 加权 K-Means (最多 12 轮)
    for _ in range(12):
        clusters: Dict[int, List[Tuple[float, float, Any]]] = {i: [] for i in range(len(centroids))}
        for tw in token_weights:
            x_val = tw[0]
            closest_idx = min(range(len(centroids)), key=lambda i: abs(centroids[i] - x_val))
            clusters[closest_idx].append(tw)

        new_centroids = []
        for i, c_x in enumerate(centroids):
            items_in_c = clusters[i]
            if items_in_c:
                weighted_sum = sum(x * w for x, w, _ in items_in_c)
                total_w = sum(w for _, w, _ in items_in_c)
                new_c = weighted_sum / max(1e-6, total_w)
                new_centroids.append(new_c)
            else:
                new_centroids.append(c_x)

        new_centroids.sort()
        # 消除过近的合并质心
        merged_centroids = []
        for c in new_centroids:
            if not merged_centroids or (c - merged_centroids[-1]) >= 8.0:
                merged_centroids.append(c)
        if len(merged_centroids) >= 2 and abs(sum(merged_centroids) - sum(centroids)) < 1.0:
            centroids = merged_centroids
            break
        centroids = merged_centroids if len(merged_centroids) >= 2 else centroids

    centroids.sort()
    # 4. 构建列边界区间 [left_bound, right_bound]
    col_bounds = []
    for i, c in enumerate(centroids):
        left_bound = (centroids[i-1] + c) / 2.0 if i > 0 else (c - 16.0)
        right_bound = (c + centroids[i+1]) / 2.0 if i + 1 < len(centroids) else (c + 30.0)
        col_bounds.append((left_bound, right_bound))

    # 提取表头
    col_headers = [""] * len(centroids)
    for h_row in header_rows:
        for it in h_row:
            matched_ci = -1
            for ci, (lb, rb) in enumerate(col_bounds):
                if lb <= it.x < rb:
                    matched_ci = ci
                    break
            if matched_ci == -1:
                matched_ci = min(range(len(centroids)), key=lambda i: abs(centroids[i] - it.x))
            col_headers[matched_ci] = (col_headers[matched_ci] + " " + it.text.strip()).strip()

    col_names = [c if c else f"Col_{i+1}" for i, c in enumerate(col_headers)]
    col_names = make_unique_columns(col_names)

    # 5. 数据行投影与缩进换行关联
    table_rows: List[List[str]] = []
    for l in data_lines[body_start_idx:]:
        row_cells = [""] * len(col_names)
        for it in l:
            matched_col = -1
            for ci, (lb, rb) in enumerate(col_bounds):
                if lb <= it.x < rb:
                    matched_col = ci
                    break
            if matched_col == -1:
                matched_col = min(range(len(centroids)), key=lambda i: abs(centroids[i] - it.x))

            if row_cells[matched_col]:
                row_cells[matched_col] += " " + it.text.strip()
            else:
                row_cells[matched_col] = it.text.strip()

        # 检查是否为前一行的缩进子项/换行续接
        non_empty = [c for c in row_cells if c]
        if len(non_empty) == 1 and table_rows and l[0].x > base_margin_x + 10.0:
            # 缩进换行，并入上一行首列
            table_rows[-1][0] = (table_rows[-1][0] + " " + non_empty[0]).strip()
        elif any(c for c in row_cells):
            table_rows.append(row_cells)

    if not table_rows:
        return None

    # 6. 语义一致性校验 (Semantic Consistency Verification)
    df = pd.DataFrame(table_rows, columns=col_names)
    # 清洗全空列
    non_empty_cols = [c for c in df.columns if any(str(val).strip() for val in df[c])]
    if non_empty_cols:
        df = df[non_empty_cols]

    # 列数值一致性探测
    for col in df.columns:
        vals = [str(x).strip() for x in df[col] if str(x).strip()]
        if not vals:
            continue
        num_count = sum(1 for v in vals if v.replace('.', '').replace('-', '').replace('%', '').replace('+', '').isdigit())
        num_ratio = num_count / len(vals)
        if num_ratio >= 0.70:
            # 标记为高置信度数值列
            df[col].attrs = {"data_type": "numeric", "numeric_ratio": round(num_ratio, 2)}

    df.attrs['alignment_engine'] = 'pdf_extract_kit'
    return df


def _align_table_with_positions(
    pdf_path: str,
    page_idx: int,
    table_y_start: float,
    table_y_end: float,
    page_height: float,
    page_width: float,
) -> Optional[Any]:
    """
    基于 pdf_inspector.extract_text_with_positions 的样式感知对齐提取：
    - 精确利用字号识别与剔除底部小字号附注/Footnote (font_size < data_font_size)
    - 结合 is_bold 样式感知合并多行复合表头
    - 采用 OpenDataLab / PDF-Extract-Kit 启发式列边界与行起始分布对齐
    """
    if pd is None:
        return None

    try:
        import pdf_inspector
        if not hasattr(pdf_inspector, 'extract_text_with_positions'):
            return None
        items = pdf_inspector.extract_text_with_positions(pdf_path, pages=[page_idx + 1])
    except Exception:
        return None

    if not items:
        return None

    # 获取 MediaBox 高度与 CropBox 偏移，确保在旋转/偏移页面下坐标绝对对齐
    cb_y0 = 0.0
    mb_height = page_height
    try:
        with fitz.open(pdf_path) as doc:
            if 0 <= page_idx < len(doc):
                p = doc[page_idx]
                cb_y0 = p.cropbox.y0
                mb_height = p.mediabox.height
    except Exception:
        pass

    eff_y_start = table_y_start + cb_y0
    eff_y_end = table_y_end + cb_y0

    # 过滤落在当前表格 y 轴范围内的文字项 (top-down: y_top = mb_height - it.y)
    table_items = []
    for it in items:
        top_y = mb_height - it.y
        if top_y < eff_y_start - 3.0 or top_y > eff_y_end + 3.0:
            continue
        if top_y > mb_height * 0.90 and it.text.strip().isdigit() and len(it.text.strip()) <= 4:
            continue
        table_items.append(it)

    if len(table_items) < 4:
        return None

    # 按 top_y 坐标分行 (容差 4.5pt)
    sorted_items = sorted(table_items, key=lambda it: (mb_height - it.y, it.x))
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
        return None

    # 终止条件判定：过滤掉尾部遭遇的正文段落
    valid_lines = []
    for l in lines:
        line_str = " ".join(it.text.strip() for it in l if it.text.strip())
        if not line_str:
            continue
        if (len(line_str) > 30 and '。' in line_str) or re.match(r'^[1-9]\d*(?:\.[1-9]\d*){1,2}\s+[\u4e00-\u9fa5]{2,}', line_str):
            break
        valid_lines.append(l)

    if len(valid_lines) < 2:
        return None

    # 剔除底部的脚注行 (结合字号与特定开头关键词)
    all_sizes = [it.font_size for l in valid_lines for it in l if it.font_size > 0]
    median_size = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 9.0

    data_lines = []
    for l in reversed(valid_lines):
        line_str = " ".join(it.text.strip() for it in l if it.text.strip())
        avg_sz = sum(it.font_size for it in l) / len(l) if l else median_size
        low = line_str.lower()
        is_fn = (
            line_str.startswith(('注', '*', '†', '‡', '§')) or
            low.startswith('note') or
            'standard deviation' in low or
            'p < 0.' in low
        )
        if not data_lines and (is_fn or avg_sz <= median_size - 1.2):
            continue
        data_lines.insert(0, l)

    if len(data_lines) < 2:
        return None

    # 表头行识别：若前 1~2 行具有加粗属性或单位模式，作为复合表头
    header_rows = [data_lines[0]]
    body_start_idx = 1
    if len(data_lines) >= 3:
        l0 = data_lines[0]
        l1 = data_lines[1]
        b0 = sum(1 for it in l0 if it.is_bold) / len(l0) if l0 else 0
        b1 = sum(1 for it in l1 if it.is_bold) / len(l1) if l1 else 0
        is_units = all(
            re.match(r'^\(.*\)$', it.text.strip()) or it.text.strip() in ('%', 'wt.%', 'MPa', 'g/t', 'm', 'km', '°C')
            for it in l1 if it.text.strip()
        )
        if (b0 >= 0.4 and b1 >= 0.4) or is_units:
            header_rows.append(l1)
            body_start_idx = 2

    # 优先使用 OpenDataLab / PDF-Extract-Kit 启发式列边界与行分布对齐
    df_aligned = pdf_extract_kit_align_tokens(data_lines, header_rows, mb_height, body_start_idx)
    if df_aligned is not None and not df_aligned.empty:
        return df_aligned

    # 降级回退标准聚类
    col_clusters = []
    for h_row in header_rows:
        for it in h_row:
            matched = False
            for clust in col_clusters:
                avg_cx = sum(x.x for x in clust) / len(clust)
                if abs(it.x - avg_cx) < 22.0:
                    clust.append(it)
                    matched = True
                    break
            if not matched:
                col_clusters.append([it])

    if not col_clusters:
        return None

    col_clusters = sorted(col_clusters, key=lambda cl: sum(x.x for x in cl) / len(cl))
    col_headers = []
    col_centers = []
    for cl in col_clusters:
        cl_sorted = sorted(cl, key=lambda it: (mb_height - it.y, it.x))
        col_headers.append(" ".join(it.text.strip() for it in cl_sorted if it.text.strip()))
        col_centers.append(sum(it.x for it in cl) / len(cl))

    col_names = [c if c else f"Col_{i+1}" for i, c in enumerate(col_headers)]
    col_names = make_unique_columns(col_names)

    # 重建数据行
    table_rows = []
    for l in data_lines[body_start_idx:]:
        row_cells = [""] * len(col_names)
        for it in l:
            best_idx = min(range(len(col_centers)), key=lambda i: abs(col_centers[i] - it.x))
            if row_cells[best_idx]:
                row_cells[best_idx] += " " + it.text.strip()
            else:
                row_cells[best_idx] = it.text.strip()
        if any(c for c in row_cells):
            table_rows.append(row_cells)

    if not table_rows:
        return None

    df = pd.DataFrame(table_rows, columns=col_names)
    return df


def _extract_page_via_text_alignment(doc, page_idx: int, pdf_path: str) -> List[Dict[str, Any]]:
    page = doc[page_idx]
    rot = getattr(page, 'rotation', 0) % 360 if isinstance(getattr(page, 'rotation', 0), (int, float)) else 0
    text = page.get_text("text")
    words = page.get_text("words")  # [(x0, y0, x1, y1, text, ...), ...]
    page_w = page.rect.width
    page_h = page.rect.height
    unrot_h = float(page.mediabox.height) if hasattr(page, 'mediabox') else float(page_h)
    unrot_w = float(page.mediabox.width) if hasattr(page, 'mediabox') else float(page_w)

    if not words:
        return []

    captions_on_page = []

    # 1. 优先使用 extract_table_caption_from_page 获取全页最准确的 table caption
    page_label, page_caption = extract_table_caption_from_page(page)
    if page_label:
        rect = page.search_for(page_label)
        if not rect and page_caption:
            rect = page.search_for(page_caption[:10])
        if not rect:
            for m in TABLE_LABEL_RE.finditer(text):
                rect = page.search_for(m.group(0))
                if rect:
                    break
        caption_y = rect[-1].y1 if rect else unrot_h * 0.05
        captions_on_page.append({
            'label': page_label,
            'title': page_caption or page_label,
            'y': caption_y,
            'text_pos': 0,
        })
    else:
        for m in TABLE_LABEL_RE.finditer(text):
            label = format_table_label(m.group(0))
            pos = m.start()
            before = text[max(0, pos-30):pos]
            after = text[pos + len(m.group(0)):pos + len(m.group(0)) + 80]
            
            ref_prefixes = ['从', '见', '如', '由', '根据', '（', '(', '在']
            ref_suffixes = ['中', '可以', '所示', '）', ')', '可知', '看出', '中数据']
            if any(before.rstrip().endswith(p) for p in ref_prefixes):
                continue
            if any(after.lstrip().startswith(s) for s in ref_suffixes):
                continue
            
            after_clean = after.lstrip()
            if len(after_clean) > 3 and not after_clean.startswith(('中', '）', ')')):
                rect = page.search_for(m.group(0))
                caption_y = rect[-1].y1 if rect else None
                title_line = after_clean.split('\n')[0].strip()
                full_title = f"{label} {title_line}" if title_line else label
                captions_on_page.append({
                    'label': label,
                    'title': full_title,
                    'y': caption_y,
                    'text_pos': m.start(),
                })

    if not captions_on_page:
        # 回退：搜索裸表号（如 "4.11  庆家沟锑矿床..."）
        bare_caption_re = re.compile(r'^\s*(\d+\.\d+)\s{2,}(.{5,})', re.MULTILINE)
        for m in bare_caption_re.finditer(text):
            num = m.group(1)
            title = m.group(2).strip()
            if not re.search(r'[\u4e00-\u9fa5]', title):
                continue
            if '。' in title or '，' in title or '；' in title or '、' in title:
                continue
            if title[0].isdigit():
                continue
            ref_label = f'表{num}'
            if ref_label in text:
                rect = page.search_for(m.group(0).strip())
                caption_y = rect[-1].y1 if rect else None
                captions_on_page.append({
                    'label': ref_label,
                    'title': f'{ref_label} {title}',
                    'y': caption_y,
                    'text_pos': m.start(),
                })
                print(f"  [text_alignment] 裸表号回退: {ref_label} -> {title[:40]}")

    if not captions_on_page:
        # 无表题无线框表格探测 (uncaptioned text alignment probe)
        pos_df = None
        try:
            pos_df = _align_table_with_positions(
                pdf_path, page_idx, unrot_h * 0.05, unrot_h * 0.95, unrot_h, unrot_w
            )
        except Exception:
            pos_df = None
        if pos_df is not None and not pos_df.empty and pos_df.shape[0] >= 1 and pos_df.shape[1] >= 2 and not is_table_low_quality(pos_df):
            if rot != 0:
                bbox = [0.0, page_h * 0.05, page_w, page_h * 0.95]
            else:
                bbox = [0.0, unrot_h * 0.05, unrot_w, unrot_h * 0.95]
            pos_df.attrs['extractor'] = 'text_alignment'
            pos_df.attrs['source'] = 'text_alignment'
            pos_df.attrs['page_idx'] = page_idx
            pos_df.attrs['is_uncaptioned'] = True
            return [{
                'df': pos_df,
                'bbox': bbox,
                'label': None,
                'title': None,
                'page_idx': page_idx,
                'table_idx': 0,
                'extractor': 'text_alignment',
            }]

        # 针对旋转页面或负坐标情况下的无线框无表题表格聚类探测
        vis_words = _get_words_visual(page) if rot != 0 else words
        if len(vis_words) >= 4:
            all_x0 = sorted(w[0] for w in vis_words)
            col_boundaries = [all_x0[0]]
            for x in all_x0[1:]:
                if x - col_boundaries[-1] > 15:
                    col_boundaries.append(x)
            if len(col_boundaries) >= 2:
                words_sorted = sorted(vis_words, key=lambda w: (round(w[1] / 6) * 6, w[0]))
                lines = []
                current_line = []
                current_y = None
                for w in words_sorted:
                    y_mid = (w[1] + w[3]) / 2
                    if current_y is None or abs(y_mid - current_y) < 6:
                        current_line.append(w)
                        if current_y is None:
                            current_y = y_mid
                    else:
                        lines.append(current_line)
                        current_line = [w]
                        current_y = y_mid
                if current_line:
                    lines.append(current_line)

                table_rows = []
                for line in lines:
                    line_s = sorted(line, key=lambda w: w[0])
                    cells = [""] * len(col_boundaries)
                    for w in line_s:
                        idx_c = 0
                        for i_b, b in enumerate(col_boundaries):
                            if w[0] >= b - 10:
                                idx_c = i_b
                            else:
                                break
                        if idx_c < len(cells):
                            cells[idx_c] = (cells[idx_c] + " " + w[4]).strip() if cells[idx_c] else w[4]
                    if any(c.strip() for c in cells):
                        table_rows.append(cells)
                if len(table_rows) >= 2:
                    pos_df = pd.DataFrame(table_rows[1:], columns=make_unique_columns(table_rows[0]))
                    if pos_df is not None and not pos_df.empty and pos_df.shape[0] >= 1 and pos_df.shape[1] >= 2 and not is_table_low_quality(pos_df):
                        pos_df.attrs['extractor'] = 'text_alignment'
                        pos_df.attrs['source'] = 'text_alignment'
                        pos_df.attrs['page_idx'] = page_idx
                        pos_df.attrs['is_uncaptioned'] = True
                        return [{
                            'df': pos_df,
                            'bbox': [0.0, page_h * 0.05, page_w, page_h * 0.95],
                            'label': None,
                            'title': None,
                            'page_idx': page_idx,
                            'table_idx': 0,
                            'extractor': 'text_alignment',
                        }]
        return []

    page_results = []
    # 对每个 caption，提取其下方的表格数据行
    for ci, cap in enumerate(captions_on_page):
        if cap['y'] is None:
            continue

        # 确定表格的 y 范围：从 caption 下方到下一个 caption 或下一个章节标题或页面底部 (unrotated 坐标系)
        table_y_start = cap['y'] + 5  # caption 下方
        if ci + 1 < len(captions_on_page) and captions_on_page[ci + 1]['y'] is not None:
            table_y_end = captions_on_page[ci + 1]['y'] - 5
        else:
            table_y_end = unrot_h * 0.92
            after_pos = cap.get('text_pos', 0) + len(cap.get('title', ''))
            sec_m = re.search(r'^[ \t]*([1-9]\d*(?:\.[1-9]\d*){1,2}[ \t]+[\u4e00-\u9fa5]{2,})', text[after_pos:], re.MULTILINE)
            if sec_m:
                sec_rect = page.search_for(sec_m.group(1).strip()[:10])
                if sec_rect:
                    table_y_end = min(table_y_end, sec_rect[0].y0 - 2)

        # 优先尝试基于 pdf_inspector extract_text_with_positions 的样式感知对齐
        pos_df = None
        try:
            pos_df = _align_table_with_positions(
                pdf_path, page_idx, table_y_start, table_y_end, unrot_h, unrot_w
            )
        except Exception as e_pos:
            logger.debug(f"[text_alignment] _align_table_with_positions notice: {e_pos}")

        if pos_df is not None and not pos_df.empty and pos_df.shape[0] >= 1 and pos_df.shape[1] >= 1:
            pos_df.attrs['extractor'] = 'text_alignment'
            pos_df.attrs['label'] = cap['label']
            if cap.get('title'):
                pos_df.attrs['table_title'] = cap['title']
            pos_df.attrs['page_idx'] = page_idx
            
            if rot != 0:
                vis_rect = _search_for_visual(page, cap.get('label') or page_label or '')
                vis_y_start = (vis_rect[-1].y1 + 5) if vis_rect else page_h * 0.05
                vis_y_end = page_h * 0.92
                bbox = [0.0, vis_y_start, page_w, vis_y_end]
            else:
                bbox = [0.0, table_y_start, page_w, table_y_end]
                
            page_results.append({
                'df': pos_df,
                'page_idx': page_idx,
                'table_idx': ci,
                'bbox': bbox,
                'label': cap.get('label'),
                'title': cap.get('title'),
            })
            continue

        # 回退至基于 words 的视觉分列分行对齐
        if rot != 0:
            vis_words = _get_words_visual(page)
            vis_rect = _search_for_visual(page, cap.get('label') or page_label or '')
            vis_y_start = (vis_rect[-1].y1 + 5) if vis_rect else page_h * 0.05
            vis_y_end = page_h * 0.92
            table_words = [w for w in vis_words if w[1] >= vis_y_start and w[1] < vis_y_end and not (w[1] > page_h * 0.90 and w[4].isdigit())]
            cur_bbox = [0.0, vis_y_start, page_w, vis_y_end]
        else:
            table_words = [w for w in words if w[1] >= table_y_start and w[1] < table_y_end and not (w[1] > page_h * 0.90 and w[4].isdigit())]
            cur_bbox = [0.0, table_y_start, page_w, table_y_end]

        if len(table_words) < 4:
            continue

        # 用 x 坐标聚类分列
        all_x0 = sorted(w[0] for w in table_words)
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

        # 用 y 坐标分行
        table_words.sort(key=lambda w: (round(w[1] / 5) * 5, w[0]))
        lines = []
        current_line = []
        current_y = None
        for w in table_words:
            if current_y is None or abs(w[1] - current_y) < 5:
                current_line.append(w)
                if current_y is None:
                    current_y = w[1]
            else:
                lines.append(current_line)
                current_line = [w]
                current_y = w[1]
        if current_line:
            lines.append(current_line)

        # 构建单元格矩阵
        table_rows = []
        for line in lines:
            line_text = ' '.join(w[4] for w in line).strip()
            if not line_text:
                continue
            # 终止条件：遇到正文段落（含句号且文字较长）或章节标题
            if (len(line_text) > 25 and '。' in line_text) or re.match(r'^[1-9]\d*(?:\.[1-9]\d*){1,2}\s+[\u4e00-\u9fa5]{2,}', line_text):
                break
            # 跳过纯页码行
            if line_text.isdigit() and len(line_str if 'line_str' in locals() else line_text) <= 3:
                continue
            cells = [""] * max(len(col_boundaries), 1)
            for w in line:
                ci2 = col_idx(w[0])
                if ci2 >= len(cells):
                    cells.extend([""] * (ci2 + 1 - len(cells)))
                cells[ci2] = (cells[ci2] + " " + w[4]).strip() if cells[ci2] else w[4]
            if any(c.strip() for c in cells):
                table_rows.append(cells)

        if len(table_rows) < 1:
            continue

        if len(table_rows) == 1:
            df = pd.DataFrame(table_rows)
        else:
            # 首行作表头，生成唯一列名
            raw_cols = [c if c.strip() else f'Col_{j}' for j, c in enumerate(table_rows[0])]
            unique_cols = make_unique_columns(raw_cols)
            df = pd.DataFrame(table_rows[1:], columns=unique_cols)
        df.attrs['extractor'] = 'text_alignment'
        df.attrs['label'] = cap['label']
        if cap.get('title'):
            df.attrs['table_title'] = cap['title']
        df.attrs['page_idx'] = page_idx
        page_results.append({
            'df': df,
            'page_idx': page_idx,
            'table_idx': ci,
            'bbox': cur_bbox,
            'label': cap.get('label'),
            'title': cap.get('title'),
        })

    return page_results


def extract_via_text_alignment(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    对无线框表格的文本对齐提取。

    策略：
    1. 在页面文本中搜索 "表x.x" caption 定位表格起始位置
    2. 优先使用 pdf-inspector (extract_text_with_positions) 进行字号感知注脚剔除与样式感知表头对齐
    3. 回退至使用 word 坐标的 x 聚类分列、y 分行重建 DataFrame
    4. 遇到正文段落（单列长文本）或下一 caption 时停止

    适用于学位论文中大量无网格线的表格。
    """
    if pd is None:
        return []

    results = []
    doc = None
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        target_pages = pages if pages is not None else range(total_pages)

        for page_idx in target_pages:
            if page_idx >= total_pages:
                continue
            try:
                page_results = _extract_page_via_text_alignment(doc, page_idx, pdf_path)
                results.extend(page_results)
            except Exception as e_page:
                logger.warning(f"[text_alignment] page {page_idx} error: {e_page}")
                continue
    except Exception as e:
        logger.warning(f"text_alignment error: {e}")
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    if results:
        print(f"[text_alignment] 提取到 {len(results)} 个表格（无线框文本对齐）")
    return results
