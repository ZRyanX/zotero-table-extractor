"""
table_validator.py - 原生提取/三方投票表格质量与完整性深度检验模块

该模块专门用于在原生三方投票提取完成后，对提取结果进行全面的完整性与质量校验。
涵盖历史上发现的所有异常与潜在边界场景。一旦检验出任何问题或缺陷，直接判定该页/该表不合格，
强制回退由高精 PaddleOCR-VL-1.6 全页结构化识别引擎独立接管提取。
"""

import os
import re
import fitz
import pandas as pd
from typing import List, Dict, Any, Tuple, Set, Optional

try:
    from .common import (
        TABLE_LABEL_PATTERN,
        TABLE_LABEL_RE,
        format_table_label,
        clean_text,
        clean_latex_and_ocr,
        is_table_low_quality,
        is_metadata_table,
        is_supplementary,
    )
except ImportError:
    from common import (
        TABLE_LABEL_PATTERN,
        TABLE_LABEL_RE,
        format_table_label,
        clean_text,
        clean_latex_and_ocr,
        is_table_low_quality,
        is_metadata_table,
        is_supplementary,
    )


def scan_pdf_table_declarations(pdf_path: str) -> Dict[int, List[Dict[str, Any]]]:
    """
    扫描 PDF 每一页中的独立表格标题声明（Caption）与续表声明。
    
    返回: page_idx -> [ {'label': '表1', 'title': '...', 'raw_line': '...', 'is_continuation': bool}, ... ]
    """
    page_declarations = {}
    if not os.path.exists(pdf_path):
        return page_declarations

    trans_fw = str.maketrans('０１２３４５６７８９', '0123456789')

    doc = None
    try:
        doc = fitz.open(pdf_path)
        num_pages = len(doc)
        for p_idx in range(num_pages):
            page = doc[p_idx]
            text = page.get_text("text")
            if not text or not text.strip():
                continue

            # 目录页与参考文献页前置过滤（防止目录中的表清单或参考文献引文被误判为正文表格声明）
            first_100_chars = text[:200].lower()
            if p_idx < 5 and any(kw in first_100_chars for kw in ['contents', 'table of contents', '目录', '目  录', '插表目录', 'list of tables']):
                continue
            if p_idx > num_pages * 0.85 and any(kw in first_100_chars for kw in ['references', 'bibliography', '参考文献', '致谢', 'acknowledgements']):
                continue

            # 排除前置目录/插表清单页
            first_lines = text[:250]
            if re.search(r'^(?:目\s*录|Contents|插表清单|表格清单|附表清单)', first_lines, re.MULTILINE):
                continue

            lines = text.split('\n')
            page_caps = []

            for idx_l, line in enumerate(lines):
                line_clean = line.strip().replace('\xa0', ' ').replace('\u3000', ' ').translate(trans_fw)
                if not line_clean:
                    continue

                # 1. 检查续表声明（续表、续表1、Table 1 (continued)、Continued Table 等）
                is_cont = bool(re.search(r'^(?:续表|续附表|（续）|\(continued\)|Table\s*\w*\s*\(continued\)|Continued\s+Table)', line_clean, re.IGNORECASE))
                if is_cont:
                    m_lbl = TABLE_LABEL_RE.search(line_clean)
                    lbl = format_table_label(m_lbl.group(0)) if m_lbl else "续表"
                    page_caps.append({
                        'label': lbl,
                        'title': line_clean,
                        'raw_line': line_clean,
                        'is_continuation': True,
                    })
                    continue

                # 2. 检查正规表标题
                m = re.match(r'^[ \t]*(' + TABLE_LABEL_PATTERN + r')[ \t\.\:：]*(.*)', line_clean, re.IGNORECASE)
                if m:
                    raw_lbl = m.group(1).strip()
                    title_part = m.group(2).strip()

                    # 排除目录和特殊谓语引导 (Table of contents, Table of figures, Table shows, etc.)
                    if re.match(r'^(?:of\s+contents|of\s+figures|of\s+tables|shows|lists|contains|presents)\b', title_part, re.IGNORECASE):
                        continue

                    # 排除正文引用（如 "如表1所示"、"在表1中"、"由表1可知"、句末点号）
                    title_no_brackets = re.sub(r'（.*?）|\(.*?\)', '', title_part)
                    if any(p in title_no_brackets for p in ['。', '！', '？', '!']):
                        continue
                    if any(title_part.startswith(p) for p in [')', '）', '中', '可以', '所示', '可知', '看出', '见', '综合表达', '显示', '表明', '为', '是', '由', '在中', '对于', '根据', 'shown', 'listed', 'summarized']):
                        continue
                    if any(title_part.endswith(p) for p in ['所示', '可以看出', '表明', '可知', '见表', '来自表', '列入表', 'shown in', 'listed in']):
                        continue

                    # 支持跨行标题前瞻
                    if not title_part and idx_l + 1 < len(lines):
                        next_l = lines[idx_l + 1].strip().replace('\xa0', ' ').replace('\u3000', ' ').translate(trans_fw)
                        if not re.search(r'^(?:表|Table|图|Fig|\d+\.)', next_l) and len(next_l) < 160 and not any(p in next_l for p in ['。', '；', '！', '？', '!']):
                            title_part = next_l

                    lbl = format_table_label(raw_lbl)
                    full_title = f"{lbl} {title_part}".strip() if title_part else lbl
                    full_title = clean_latex_and_ocr(full_title)

                    page_caps.append({
                        'label': lbl,
                        'title': full_title,
                        'raw_line': line_clean,
                        'is_continuation': False,
                    })

            if page_caps:
                page_declarations[p_idx] = page_caps
    except Exception:
        pass
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    return page_declarations


def validate_native_table_dataframe(df: pd.DataFrame, page_idx: int) -> Tuple[bool, str]:
    """
    深度检验单个原生提取出的 DataFrame 数据结构与内容完整性。
    
    返回 (is_valid, failure_reason)
    """
    if df is None or df.empty:
        return False, "表格为空或 DataFrame 为 None"

    # 1. 基础尺寸检验
    if df.shape[0] < 2 or df.shape[1] < 2:
        return False, f"表格尺寸异常 ({df.shape[0]}行 × {df.shape[1]}列)，无法构成有效二维数据矩阵"

    # 2. 检查是否为低质量损坏表（已整合通用低质量过滤器）
    if is_table_low_quality(df):
        return False, "触发通用低质量/坏损表规则 (正文污染、列切分断裂或严重稀疏)"

    # 3. 检查是否为元数据/参考文献/版权信息
    if is_metadata_table(df):
        return False, "判定为论文元数据、作者简介、插图图注或参考文献列表"

    # 4. 空列与破碎列检验（原生提取最常见问题：因空白间距估计错误产生的假性空列）
    empty_col_count = 0
    single_char_col_count = 0
    for c in range(df.shape[1]):
        col_vals = [str(df.iat[r, c]).strip() for r in range(df.shape[0]) if str(df.iat[r, c]).strip() not in ['', 'nan', 'none', 'None']]
        if len(col_vals) == 0:
            empty_col_count += 1
        elif all(len(v) <= 1 for v in col_vals) and len(col_vals) >= 3:
            single_char_col_count += 1

    if empty_col_count >= 2:
        return False, f"存在 {empty_col_count} 个完全空白列，列切分严重受损"

    if single_char_col_count >= 2 and df.shape[1] <= 6:
        return False, f"存在 {single_char_col_count} 个单字符碎片列，疑似竖排文字被切碎"

    # 5. 图表坐标轴/散点图残片深度检测
    all_text = " ".join(str(v) for v in df.values.flatten() if v is not None and str(v) != 'nan').lower()
    axis_keywords = ['watson et al', 'tomkins et al', 'kbar', 't , °c', 't (°c)', 'wt% tio2', 'wt% sio2', 'wt% al2o3', 'δ34s (‰) vs']
    if any(kw in all_text for kw in axis_keywords) and df.shape[0] <= 15:
        return False, "检测到图表/坐标轴刻度残留特征 (散点图/相图误识别)"

    # 6. 表头有效性检测（表头不能整行全为 NaN 或全是未命名无意义列）
    header_strs = [str(c).strip() for c in df.columns]
    unnamed_cnt = sum(1 for c in header_strs if not c or c.startswith('Unnamed:') or c.startswith('Col') or c.isdigit())
    if unnamed_cnt == len(header_strs) and df.shape[0] <= 3:
        return False, "表头完全未识别且行数过少，属于残片"

    return True, "合格"


def validate_native_extraction_pipeline(
    native_results: List[Dict[str, Any]],
    pdf_path: str,
    target_pages: List[int],
    vote_summary: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Set[int], List[str]]:
    """
    全流程原生提取综合检验总控模块。
    
    检验项：
    1. 单表数据质量与坏损检验 (validate_native_table_dataframe)；
    2. 页级标题与表数匹配检验 (Page-level caption count vs extracted count)；
    3. 全文献表号连续性与缺失检验 (Table sequence completeness)；
    4. 跨页续表完整性检验 (Continuation table validation)；
    5. 三方投票一致性与低置信度页面检验 (Low confidence fallback)。
    
    返回:
    - valid_native_results: 通过检验的合格原生表格
    - pages_requiring_ocr: 检验失败、必须由 PaddleOCR-VL 接管的页面集合
    - validation_logs: 详细检验日志
    """
    validation_logs = []
    valid_native_results = []
    pages_requiring_ocr = set()

    # 1. 扫描 PDF 全文的真实表标题声明
    page_declarations = scan_pdf_table_declarations(pdf_path)
    all_declared_labels = []
    label_to_declared_page = {}
    for p, decls in page_declarations.items():
        for d in decls:
            if not d.get('is_continuation'):
                lbl = d['label']
                all_declared_labels.append(lbl)
                if lbl not in label_to_declared_page:
                    label_to_declared_page[lbl] = p

    validation_logs.append(f"→ [验证模块] PDF 全文检测到 {len(all_declared_labels)} 个表格标题声明: {all_declared_labels}")

    # 2. 逐一检验原生提取的单个 DataFrame
    page_to_valid_tables = {}
    for r in native_results:
        df = r.get('df')
        p_idx = r.get('page_idx', df.attrs.get('page_idx', 0) if df is not None else 0)
        
        ok, reason = validate_native_table_dataframe(df, p_idx)
        if not ok:
            validation_logs.append(f"  ❌ Page {p_idx + 1} 原生表格检验未通过: {reason} → 标记重走 PaddleOCR-VL")
            pages_requiring_ocr.add(p_idx)
        else:
            valid_native_results.append(r)
            if p_idx not in page_to_valid_tables:
                page_to_valid_tables[p_idx] = []
            page_to_valid_tables[p_idx].append(r)

    # 3. 检验页级声明与提取表数匹配
    for p_idx in target_pages:
        decls = page_declarations.get(p_idx, [])
        non_cont_decls = [d for d in decls if not d.get('is_continuation')]
        extracted_tables = page_to_valid_tables.get(p_idx, [])
        
        if len(non_cont_decls) > 0:
            if len(extracted_tables) < len(non_cont_decls):
                validation_logs.append(f"  ❌ Page {p_idx + 1} 声明了 {len(non_cont_decls)} 个表格，但原生仅提取到 {len(extracted_tables)} 个 (可能存在漏表或多表粘连) → 标记重走 PaddleOCR-VL")
                pages_requiring_ocr.add(p_idx)

        # 检查续表声明页
        cont_decls = [d for d in decls if d.get('is_continuation')]
        if len(cont_decls) > 0:
            # 续表所在页必须纳入 OCR，以保证跨页续表能够无损缝合
            validation_logs.append(f"  ℹ️ Page {p_idx + 1} 检测到续表声明 → 确保纳入 OCR 连续块")
            pages_requiring_ocr.add(p_idx)
            # 前一页也一同纳入保证上下文
            if p_idx - 1 >= 0:
                pages_requiring_ocr.add(p_idx - 1)

    # 4. 检验文献全局表号连续性
    extracted_labels = set()
    for r in valid_native_results:
        lbl = r['df'].attrs.get('label') or r.get('label')
        if lbl:
            extracted_labels.add(format_table_label(lbl))

    for lbl in all_declared_labels:
        if lbl not in extracted_labels:
            decl_p = label_to_declared_page.get(lbl, 0)
            validation_logs.append(f"  ❌ 缺失声明表号 [{lbl}] (所在页: Page {decl_p + 1}) → 标记重走 PaddleOCR-VL")
            pages_requiring_ocr.add(decl_p)

    # 5. 整合三方投票的低置信度页面
    low_confidence_pages = vote_summary.get('low_confidence_pages', set())
    if low_confidence_pages:
        for p in low_confidence_pages:
            validation_logs.append(f"  ⚠️ Page {p + 1} 三方投票分歧较大/低置信度 → 标记重走 PaddleOCR-VL")
            pages_requiring_ocr.add(p)

    # 6. 未提取到任何有效表格但属于候选表格页的页面
    for p_idx in target_pages:
        if p_idx not in page_to_valid_tables and p_idx in page_declarations:
            validation_logs.append(f"  ❌ Page {p_idx + 1} 含有表格声明但未获得任何有效原生表格 → 标记重走 PaddleOCR-VL")
            pages_requiring_ocr.add(p_idx)

    # 7. 扩展跨页上下文（连续 block 处理）
    if pages_requiring_ocr:
        try:
            with fitz.open(pdf_path) as doc:
                num_pages = len(doc)
            expanded_ocr_pages = set(pages_requiring_ocr)
            for p in list(pages_requiring_ocr):
                # 检查后序页是否有续表
                for offset in range(1, 4):
                    next_p = p + offset
                    if next_p < num_pages:
                        if next_p in page_declarations:
                            for d in page_declarations[next_p]:
                                if d.get('is_continuation'):
                                    expanded_ocr_pages.add(next_p)
            pages_requiring_ocr = expanded_ocr_pages
        except Exception:
            pass

    validation_logs.append(f"→ [验证模块完成] 判定合格原生表格: {len(valid_native_results)} 个, 必须由 PaddleOCR-VL 接管页面: {[p+1 for p in sorted(pages_requiring_ocr)]}")
    return valid_native_results, pages_requiring_ocr, validation_logs
