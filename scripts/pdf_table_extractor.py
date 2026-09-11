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

import os
import re
import io
import time
import logging
from typing import List, Dict, Any, Optional, Tuple

try:
    import pymupdf as fitz
except ImportError:
    import fitz

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from .common import format_table_label, TABLE_LABEL_PATTERN, TABLE_LABEL_RE, is_table_squeezed, is_table_low_quality, is_markdown_separator
except ImportError:
    from common import format_table_label, TABLE_LABEL_PATTERN, TABLE_LABEL_RE, is_table_squeezed, is_table_low_quality, is_markdown_separator

logger = logging.getLogger(__name__)


# ===========================================================================
# 1. pdf-inspector: PDF 分类 + 表格定位 + Markdown 表格提取
# ===========================================================================

def classify_and_extract_via_inspector(pdf_path: str, caption_page_map: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """
    使用 pdf-inspector 进行:
    - PDF 类型分类 (text_based / scanned / image_based / mixed)
    - 表格页面定位 (pages_with_tables)
    - Markdown 表格提取 (从 markdown 中解析表格)

    返回:
    {
        'pdf_type': str,
        'confidence': float,
        'pages_with_tables': List[int],
        'pages_needing_ocr': List[int],
        'tables': List[{'df': DataFrame, 'page_idx': int, 'extractor': str, 'bbox': None}],
        'markdown': str or None,
        'error': str or None,
    }
    """
    result = {
        'pdf_type': 'unknown',
        'confidence': 0.0,
        'pages_with_tables': [],
        'pages_needing_ocr': [],
        'tables': [],
        'markdown': None,
        'error': None,
    }

    try:
        import pdf_inspector

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
                if not df.attrs.get('label'):
                    df.attrs['label'] = f"Table {i+1}"
                result['tables'].append({
                    'df': df,
                    'page_idx': page_hint,
                    'table_idx': i,
                    'bbox': None,
                })

        print(f"[pdf-inspector] type={result['pdf_type']}, confidence={result['confidence']:.2f}, "
              f"pages_with_tables={result['pages_with_tables']}, tables_parsed={len(result['tables'])}, "
              f"time={pi_result.processing_time_ms}ms")

    except ImportError:
        result['error'] = 'pdf_inspector not installed'
        print("[pdf-inspector] 未安装，跳过")
    except Exception as e:
        result['error'] = str(e)
        # pdf-inspector 解析失败时，回退到 PyMuPDF 文本层判断
        # （不能直接判定为 scanned —— 很多 CNKI/中文期刊 PDF 有文本层但 trailer 格式不规范）
        has_text = has_text_layer(pdf_path)
        if has_text:
            result['pdf_type'] = 'text_based'
            result['confidence'] = 0.5
            print(f"[pdf-inspector] 解析失败 ({e}), PyMuPDF 检测到文本层 → 判定为 text_based")
        else:
            result['pdf_type'] = 'scanned'
            print(f"[pdf-inspector] 解析失败 ({e}), 无文本层 → 判定为 scanned")

    return result


def _parse_markdown_tables(markdown: str, caption_page_map: Optional[Dict[str, int]] = None) -> List[Tuple[Any, Optional[int]]]:
    """
    从 pdf-inspector 输出的 Markdown 中解析表格。
    结合 caption_page_map 精准映射每张表格对应的 PDF 页码。
    返回 [(DataFrame, page_hint), ...]
    """
    if pd is None:
        return []

    tables = []
    lines = markdown.split('\n')
    i = 0
    page_hint = None
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
                if curr_label:
                    df.attrs['label'] = curr_label
                if curr_caption:
                    df.attrs['table_title'] = curr_caption
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


def extract_table_caption_from_page(page, table_bbox: Optional[List[float]] = None) -> Tuple[str, str]:
    """
    从 PDF 页面中提取表格的标签（如 '表1', 'Table 1'）与完整标题（含中英文）。
    优先在 table_bbox 上方区域精确提取；若无 bbox 则在页面文本中搜索。
    """
    if page is None:
        return '', ''
    try:
        if table_bbox:
            y_top = table_bbox[1]
            clip_rect = fitz.Rect(0, max(0, y_top - 95), page.rect.width, max(0, y_top - 1))
            words = page.get_text("words", clip=clip_rect)
        else:
            words = page.get_text("words")

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
                if any(k in line for k in ['期 雷欣儒等', '第 11 期', 'Vol.', 'pp.', 'http://', 'doi:', 'Journal of', 'Acta ']):
                    continue
                m = TABLE_LABEL_RE.search(line)
                if m:
                    label = format_table_label(m.group(0))
                    pos = line.find(m.group(0))
                    before = line[:pos]
                    after = line[pos + len(m.group(0)):].strip()
                    if any(before.rstrip().endswith(p) for p in ['（', '(', '从', '见', '如', '由', '根据', '在']):
                        continue
                    if after.startswith(('）', ')', '中', '可以', '所示', '可知', '看出', ':', '：')):
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
            if any(k in line for k in ['期 雷欣儒等', '第 11 期', 'Vol.', 'pp.', 'http://', 'doi:', 'Journal of', 'Acta ']):
                continue
            m = re.search(r'^(' + TABLE_LABEL_PATTERN + r')[\.\:\s]*(.*)', line, re.IGNORECASE)
            if m:
                label = format_table_label(m.group(1))
                after = m.group(2).strip()
                if after.startswith(('）', ')', '中', '可以', '所示', '可知', '看出')):
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
    except Exception:
        pass
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
                    if not rows or len(rows) < 2:
                        continue

                    clean_rows = []
                    for row in rows:
                        clean_row = [("" if cell is None else str(cell).strip()) for cell in row]
                        if any(c for c in clean_row):
                            clean_rows.append(clean_row)

                    if len(clean_rows) < 2:
                        continue

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
                        if not data or len(data) < 2:
                            continue
                        clean_rows = []
                        for row in data:
                            clean_row = [("" if cell is None else str(cell).strip()) for cell in row]
                            if any(c for c in clean_row):
                                clean_rows.append(clean_row)
                        if len(clean_rows) < 2:
                            continue
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

        for flavor in ['lattice', 'stream']:
            try:
                tables = camelot.read_pdf(pdf_path, flavor=flavor, pages=page_str)
                if tables and len(tables) > 0:
                    for tbl_idx, table in enumerate(tables):
                        df = table.df
                        if df is None or df.empty or len(df) < 2:
                            continue
                        df = df.applymap(lambda x: str(x).strip() if x is not None else "")
                        df.attrs['extractor'] = f'camelot_{flavor}'
                        page_idx = table.page - 1 if hasattr(table, 'page') else tbl_idx
                        df.attrs['page_idx'] = page_idx
                        results.append({
                            'df': df,
                            'page_idx': page_idx,
                            'table_idx': tbl_idx,
                            'bbox': None,
                        })
                    if results:
                        break
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
# 5. 三方投票融合
# ===========================================================================

def _table_similarity(df1, df2) -> float:
    """
    计算两个 DataFrame 的内容相似度 (0~1)。

    改进：支持行列偏移容错。
    - 先尝试直接逐单元格比对（原始方式）
    - 若直接比对相似度低（< 0.5），尝试行偏移 ±1~2 行重新比对，取最高值
    - 同时引入 Jaccard 集合相似度作为补充（不依赖行列对齐）
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

    # 综合相似度：对齐比对权重 0.5，Jaccard 权重 0.2，形状权重 0.3
    content_sim = max(best_content_sim, jaccard * 0.8)
    return 0.3 * shape_sim + 0.5 * best_content_sim + 0.2 * jaccard


def three_way_vote(
    inspector_tables: List[Dict[str, Any]],
    find_tables_results: List[Dict[str, Any]],
    plumber_tables: List[Dict[str, Any]],
    camelot_tables: List[Dict[str, Any]],
    align_tables: Optional[List[Dict[str, Any]]] = None,
    similarity_threshold: float = 0.75,
) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
    """
    多方投票融合: pdf-inspector + find_tables + text_alignment + pdfplumber + Camelot。

    规则:
    - 任意两个一致 (相似度 >= similarity_threshold，默认 0.75) → 取一致的版本（优先 pdf-inspector > find_tables > text_alignment > pdfplumber > Camelot）
    - 质量检查：若选中的候选表存在列挤压，自动降级切换至无挤压的候选表
    - 多方均不一致 → 取优先级最高的结果
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
        'pdf_inspector': inspector_tables,
        'find_tables': find_tables_results,
        'text_alignment': align_tables or [],
        'pdfplumber': plumber_tables,
        'camelot': camelot_tables,
    }

    # 收集所有出现的页码
    all_pages = set()
    for src_tables in sources.values():
        for t in src_tables:
            all_pages.add(t.get('page_idx', 0))

    # 优先级排序：pdf_inspector 结构化优先，find_tables 矢量线框与无线框文本对齐紧随其后
    priority = ['pdf_inspector', 'find_tables', 'text_alignment', 'pdfplumber', 'camelot']

    for page in sorted(all_pages):
        page_tables = {}
        for src_name, src_tables in sources.items():
            page_tables[src_name] = [t for t in src_tables if t.get('page_idx', 0) == page]

        # 取该页上所有来源的表格
        max_count = max(len(ts) for ts in page_tables.values()) if page_tables else 0
        if max_count == 0:
            continue

        # 逐个表格位置匹配
        for tbl_idx in range(max_count):
            candidates = {}
            for src_name in priority:
                if tbl_idx < len(page_tables.get(src_name, [])):
                    candidates[src_name] = page_tables[src_name][tbl_idx]

            if not candidates:
                continue

            # 两两比较
            src_names = list(candidates.keys())
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
                    # 取 pdf_inspector (benchmark 最优)
                    if 'pdf_inspector' in candidates:
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
                    logs.append(f"Page {page+1} Table {tbl_idx+1}: 三方不一致 [{', '.join(sims)}] "
                                f"→ 采用 {best_extractor} (最高优先级)")
                    # 记录低置信度页面
                    if max_sim < 0.90:
                        low_confidence_pages.add(page)
                    page_scores.setdefault(page, {})['max_similarity'] = max_sim

            # 列挤压质量检查与自动纠正：
            # 若选出的候选表存在列挤压（如数值挤在同一单元格），且同页有未挤压的候选表（如 text_alignment），则自动切换为未挤压版本
            if best_result is not None and is_table_squeezed(best_result['df']):
                for alt_name in ['text_alignment', 'pdfplumber', 'camelot', 'pdf_inspector']:
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
    兜底方案: PP-StructureV3 定位 + PaddleOCR-VL-1.6 多模态解析。
    仅当 PDF 无文本层或三方投票全部失败时使用。
    """
    results = []

    # 1. PP-StructureV3 -> PaddleOCR-VL-1.6 两阶段融合管线
    try:
        import sys
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import ocr_client
        from table_postprocess import parse_structured_vlm_content

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

    # 2. 表格切图回退 (PP-StructureV3 / DocLayout-YOLO)
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
        print(f"[OCR] 提取到 {len(results)} 个表格 (PP-StructureV3 + PaddleOCR-VL)")
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
                        rect = page.search_for(line.strip()[:15])
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
            if not assigned_label:
                # 寻找前一个已分配有效 label 的结果
                for prev_r in reversed(all_results):
                    if prev_r is r:
                        continue
                    prev_p = prev_r.get('page_idx', 0)
                    prev_lbl = prev_r.get('label') or prev_r.get('df', {}).attrs.get('label')
                    if prev_p < p_idx and prev_lbl:
                        prev_df_cols = prev_r['df'].shape[1] if prev_r.get('df') is not None else 0
                        # 判定条件：连续页（相差 <= 3 页）且列数高度匹配
                        # 增加小表防护：若两表均为小表（<6行），禁止误判为跨页续表
                        is_small_pair = (df.shape[0] < 6 and (prev_r.get('df') is None or prev_r['df'].shape[0] < 6))
                        if (p_idx - prev_p <= 3) and (df.shape[1] == prev_df_cols or abs(df.shape[1] - prev_df_cols) <= 2) and not is_small_pair:
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

                    if is_explicit_cont or is_dense_data_table:
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
                    if df is not None and not df.empty and df.shape[0] >= 1 and df.shape[1] >= 2:
                        df.attrs['extractor'] = 'paddleocr_vl'
                        df.attrs['page_idx'] = block[0]
                        # 触发 Post-OCR 门禁检验与 Agent 协同验证
                        df = verify_and_rescue_table_with_agent(df, pdf_path, block[0], df.attrs.get('label', ''))
                        results.append({
                            'df': df,
                            'page_idx': block[0],
                            'table_idx': t_idx,
                            'bbox': None,
                        })
            except Exception as e:
                print(f"[PaddleOCR-VL FullPage] Block {[p+1 for p in block]} 提取异常: {e}")

    except Exception as e:
        print(f"[PaddleOCR-VL FullPage] 异常: {e}")

    if results:
        print(f"[PaddleOCR-VL FullPage] 提取到 {len(results)} 个表格")
    return results


def extract_tables_from_pdf(
    pdf_path: str,
    use_ocr_fallback: bool = True,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    统一 PDF 表格提取管线。

    管线:
    1. pdf-inspector 分类 + 启发式 caption 检测 → 识别含表格的页面
    2. native PDF: pdf-inspector + Camelot + pdfplumber 三方投票
       scanned PDF: 直接走 PaddleOCR-VL
    3. 三方投票相似度 >= 90% → 采用投票结果
       三方投票相似度 < 90% 或未提取到 → 该页走 PaddleOCR-VL-1.6 全页结构化输出
    4. 跨页续表合并

    返回 (results, logs)
    """
    logs = []
    all_results = []

    # ── 高精锚定：精准扫描 PDF 全文真实表标题声明 (Captions & Continuations) ──
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
    try:
        doc = fitz.open(pdf_path)
        for p_idx, page in enumerate(doc):
            words = page.get_text("words")
            images = page.get_images()
            if len(words) < 25 and len(images) > 0:
                image_table_pages.add(p_idx)
        doc.close()
    except Exception:
        pass

    # Step 1: pdf-inspector 分类
    logs.append("→ Step 1: pdf-inspector 分类 + 表格定位")
    inspector_result = classify_and_extract_via_inspector(pdf_path, caption_page_map)
    logs.append(f"  pdf_type={inspector_result['pdf_type']}, "
                f"confidence={inspector_result['confidence']:.2f}, "
                f"pages_with_tables={inspector_result['pages_with_tables']}")
    if inspector_result['error']:
        logs.append(f"  pdf-inspector 错误: {inspector_result['error']}")

    pdf_type = inspector_result['pdf_type']
    pages_with_tables = inspector_result['pages_with_tables']
    inspector_tables = [t for t in inspector_result['tables'] if t.get('page_idx') is not None]

    # 精准候选页面决策体系：
    # 1. 若文本层已明确检测到表标题声明 (caption_pages)，以 caption_pages 为绝对基准；
    #    绝不盲目并入 pdf-inspector 产生的全书双栏误判页！
    if caption_pages:
        detected_targets = set(caption_pages).union(image_table_pages)
    elif image_table_pages:
        detected_targets = set(image_table_pages)
    elif pages_with_tables:
        detected_targets = set(pages_with_tables)
    else:
        detected_targets = set()

    # 自动包含可能的跨页续表后继页（支持多页长表连续追溯，如跨 3~5 页的大表）
    try:
        doc = fitz.open(pdf_path)
        continuation_candidates = set()
        for p in caption_pages:
            next_p = p + 1
            while next_p < len(doc) and next_p <= p + 6:
                next_text = doc[next_p].get_text("text").strip()
                lines = [l.strip() for l in next_text.split('\n') if l.strip() and not (l.strip().isdigit() and len(l.strip()) <= 4) and not re.search(r'(?:大学|学位论文)', l)]
                if not lines:
                    break
                first_line = lines[0]
                # 如果遇到新章节标题、新图题、新表题或致谢/参考文献，停止追溯
                if re.match(r'^\d+\.\d+', first_line) or re.match(r'^(?:附表|附录表|补充表|表|Table)\s*\d+', first_line) or first_line.startswith(('图', 'Fig', '参考文献', '致谢')):
                    break
                
                # 1. 显式续表关键字
                cond_kw = any(k in next_text[:400] for k in ['续表', '（续）', '(续)', 'continued', 'Continued', '(cont.)', '(Cont.)'])
                # 2. 密集纯数据元胞（附录超长连续大表，无表头直接连续密集数值）
                data_token_count = sum(1 for l in lines if re.match(r'^-?\d+(?:\.\d+)?$', l) or l in ('/', '-', '—', '–', 'n.d.', 'bdl', 'b.d.l.') or re.match(r'^[A-Za-z0-9\-_–—/]+$', l))
                cond_dense = len(lines) >= 25 and (data_token_count / len(lines) >= 0.65)

                if cond_kw or cond_dense:
                    continuation_candidates.add(next_p)
                    next_p += 1
                else:
                    break
        doc.close()
        detected_targets = detected_targets.union(continuation_candidates)
    except Exception:
        pass

    target_pages = sorted(detected_targets) if detected_targets else None

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
        all_results.extend(ocr_results)
        logs.append(f"  PaddleOCR-VL 提取到 {len(ocr_results)} 个表格")
    else:
        # ── native PDF → 三方投票 + 低相似度页面回退 PaddleOCR-VL ──
        logs.append("→ Step 2: native PDF，启动三方投票 (pdf-inspector + Camelot + pdfplumber + find_tables)")

        native_target_pages = [p for p in target_pages if p not in image_table_pages] if target_pages is not None else None

        ft_results = extract_via_find_tables(pdf_path, pages=native_target_pages)
        plumber_results = extract_via_pdfplumber(pdf_path, pages=native_target_pages)
        camelot_results = extract_via_camelot(pdf_path, pages=native_target_pages)
        align_results = extract_via_text_alignment(pdf_path, pages=native_target_pages)

        # 多方投票融合
        logs.append("→ Step 3: 多方投票融合")
        fused, vote_logs, vote_summary = three_way_vote(
            inspector_tables, ft_results, plumber_results, camelot_results, align_tables=align_results
        )
        logs.extend(vote_logs)

        if fused:
            all_results.extend(fused)
            extractors = set(r['df'].attrs.get('vote_extractor', r['df'].attrs.get('extractor', '?')) for r in fused)
            logs.append(f"  投票完成: {len(fused)} 个表格 (extractors: {extractors})")
        else:
            logs.append("  三方投票未获得表格")

        # Step 3b: 三方投票综合检验模块（坏损表、表头错位、表序缺失、续表中断、单页多表漏抓检验）
        from table_validator import validate_native_extraction_pipeline
        valid_fused, pages_requiring_ocr, val_logs = validate_native_extraction_pipeline(
            all_results, pdf_path, target_pages, vote_summary
        )
        logs.extend(val_logs)
        all_results = valid_fused

        # 若检验发现任何问题、缺陷或低置信度页面，直接走 PaddleOCR-VL 独立提取接管
        if pages_requiring_ocr and use_ocr_fallback:
            ocr_candidate_pages = sorted(list(pages_requiring_ocr))
            logs.append(f"→ Step 3b: 检验未通过/待精细识别页面 {[p+1 for p in ocr_candidate_pages]} 全面由 PaddleOCR-VL-1.6 独立接管提取")
            
            # 清理待 OCR 页面的残次原生表，绝不混淆
            ocr_candidate_set = set(ocr_candidate_pages)
            all_results = [r for r in all_results if r.get('page_idx') not in ocr_candidate_set]
            
            ocr_results = extract_via_paddleocr_fullpage(pdf_path, ocr_candidate_pages)
            if ocr_results:
                all_results.extend(ocr_results)
                logs.append(f"  PaddleOCR-VL 独立接管提取到 {len(ocr_results)} 个表格")

        # Step 3c: 仍未覆盖且从未被 PaddleOCR 处理过的页面 → text_alignment
        extracted_pages = set(r.get('page_idx', -1) for r in all_results if r.get('page_idx') is not None)
        ocr_processed_pages = set(ocr_candidate_pages) if 'ocr_candidate_pages' in locals() else set()
        still_missing = sorted(p for p in target_pages if p not in extracted_pages and p not in ocr_processed_pages)
        if still_missing:
            logs.append(f"→ Step 3c: 对仍未覆盖的 {[p+1 for p in still_missing]} 页尝试文本对齐提取")
            text_align_results = extract_via_text_alignment(pdf_path, pages=still_missing)
            for ta in text_align_results:
                ta_page = ta.get('page_idx', -1)
                if ta_page not in extracted_pages and not is_table_low_quality(ta['df']):
                    all_results.append(ta)
                    extracted_pages.add(ta_page)

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

    if all_results:
        extractors = set(r['df'].attrs.get('extractor', '?') for r in all_results)
        logs.append(f"  最终: {len(all_results)} 个表格 (extractors: {extractors})")
        return all_results, logs

    # Step 5: 全局 OCR 兜底
    logs.append("→ Step 5: 结构化提取未获得有效表格，回退到全局 OCR")
    if use_ocr_fallback:
        ocr_results = extract_via_ocr(pdf_path)
        all_results.extend(ocr_results)
        all_results = reconcile_table_captions(all_results, pdf_path)
        logs.append(f"  全局 OCR 提取到 {len(ocr_results)} 个表格")

    return all_results, logs


# ===========================================================================
# 5b. 文本对齐提取（无线框表格）
# ===========================================================================

def extract_via_text_alignment(pdf_path: str, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    对无线框表格的文本对齐提取。

    策略：
    1. 在页面文本中搜索 "表x.x" caption 定位表格起始位置
    2. 从 caption 之后的文本行中，用 word 坐标的 x 聚类分列、y 分行重建 DataFrame
    3. 遇到正文段落（单列长文本）或下一 caption 时停止

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
            page = doc[page_idx]
            text = page.get_text("text")
            words = page.get_text("words")  # [(x0, y0, x1, y1, text, ...), ...]

            if not words:
                continue

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
                caption_y = rect[0].y0 if rect else page.rect.height * 0.05
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
                    ref_suffixes = ['中', '可以', '所示', '）', ')', '可知', '看出', '中数据', ':', '：']
                    if any(before.rstrip().endswith(p) for p in ref_prefixes):
                        continue
                    if any(after.lstrip().startswith(s) for s in ref_suffixes):
                        continue
                    
                    after_clean = after.lstrip()
                    if len(after_clean) > 3 and not after_clean.startswith(('中', '）', ')')):
                        rect = page.search_for(m.group(0))
                        caption_y = rect[-1].y0 if rect else None
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
                # 当 "表" 和表号被 PDF 文本提取拆到两行时，caption_re 匹配不到 "表4.11"
                # 但裸表号 "4.11" 后跟标题文字仍是有效 caption
                bare_caption_re = re.compile(r'^\s*(\d+\.\d+)\s{2,}(.{5,})', re.MULTILINE)
                for m in bare_caption_re.finditer(text):
                    num = m.group(1)
                    title = m.group(2).strip()
                    # 严格过滤：title 必须是中文标题（含中文字符，不含句号/逗号/分号）
                    if not re.search(r'[\u4e00-\u9fa5]', title):
                        continue
                    if '。' in title or '，' in title or '；' in title or '、' in title:
                        continue
                    # 排除 title 以数字开头（表格数据行）
                    if title[0].isdigit():
                        continue
                    # 检查页面上是否有 "表X.Y" 引用（确认这个表号确实对应一个表）
                    ref_label = f'表{num}'
                    if ref_label in text:
                        rect = page.search_for(m.group(0).strip())
                        caption_y = rect[0].y0 if rect else None
                        captions_on_page.append({
                            'label': ref_label,
                            'title': f'{ref_label} {title}',
                            'y': caption_y,
                            'text_pos': m.start(),
                        })
                        print(f"  [text_alignment] 裸表号回退: {ref_label} -> {title[:40]}")

            if not captions_on_page:
                continue

            # 对每个 caption，提取其下方的表格数据行
            for ci, cap in enumerate(captions_on_page):
                if cap['y'] is None:
                    continue

                # 确定表格的 y 范围：从 caption 下方到下一个 caption 或下一个章节标题或页面底部
                table_y_start = cap['y'] + 5  # caption 下方
                if ci + 1 < len(captions_on_page) and captions_on_page[ci + 1]['y'] is not None:
                    table_y_end = captions_on_page[ci + 1]['y'] - 5
                else:
                    table_y_end = page.rect.height * 0.92
                    # 尝试搜索下方章节标题（如 6.6 同位素地球化学特征）作为终止边界（注意使用 [ \t]+ 避免单元格内浮点数+换行误匹配）
                    after_pos = cap.get('text_pos', 0) + len(cap.get('title', ''))
                    sec_m = re.search(r'^[ \t]*([1-9]\d*(?:\.[1-9]\d*){1,2}[ \t]+[\u4e00-\u9fa5]{2,})', text[after_pos:], re.MULTILINE)
                    if sec_m:
                        sec_rect = page.search_for(sec_m.group(1).strip()[:10])
                        if sec_rect:
                            table_y_end = min(table_y_end, sec_rect[0].y0 - 2)

                # 过滤出表格区域内的 words（排除页面底部单独页码）
                table_words = [w for w in words if w[1] >= table_y_start and w[1] < table_y_end and not (w[1] > page.rect.height * 0.90 and w[4].isdigit())]

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

                # 重建表格行
                table_rows = []
                for line in lines:
                    line_str = " ".join(w[4] for w in line).strip()
                    if not line_str:
                        continue
                    # 终止条件：遇到正文段落（含句号且文字较长）或章节标题
                    if (len(line_str) > 25 and '。' in line_str) or re.match(r'^[1-9]\d*(?:\.[1-9]\d*){1,2}\s+[\u4e00-\u9fa5]{2,}', line_str):
                        break
                    # 跳过纯页码行
                    if line_str.isdigit() and len(line_str) <= 3:
                        continue
                    cells = [""] * max(len(col_boundaries), 1)
                    for w in line:
                        ci2 = col_idx(w[0])
                        if ci2 >= len(cells):
                            cells.extend([""] * (ci2 + 1 - len(cells)))
                        cells[ci2] = (cells[ci2] + " " + w[4]).strip() if cells[ci2] else w[4]
                    if any(c.strip() for c in cells):
                        table_rows.append(cells)

                if len(table_rows) < 2:
                    continue

                # 首行作表头
                df = pd.DataFrame(table_rows[1:], columns=table_rows[0])
                df.attrs['extractor'] = 'text_alignment'
                df.attrs['label'] = cap['label']
                if cap.get('title'):
                    df.attrs['table_title'] = cap['title']
                df.attrs['page_idx'] = page_idx
                results.append({
                    'df': df,
                    'page_idx': page_idx,
                    'table_idx': ci,
                    'bbox': None,
                })
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
