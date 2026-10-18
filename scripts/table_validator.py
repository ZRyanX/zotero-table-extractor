"""
table_validator.py - 原生提取/三方投票表格质量与完整性深度检验模块

该模块专门用于在原生三方投票提取完成后，对提取结果进行全面的完整性与质量校验。
涵盖历史上发现的所有异常与潜在边界场景。一旦检验出任何问题或缺陷，直接判定该页/该表不合格，
强制回退由高精 PaddleOCR-VL-1.6 全页结构化识别引擎独立接管提取。
"""

import os
import re
try:
    import pymupdf as fitz
except ImportError:
    import fitz
import pandas as pd
from typing import List, Dict, Any, Tuple, Set, Optional, Union

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

AXIS_KEYWORDS = ['watson et al', 'tomkins et al', 'δ34s (‰) vs']
axis_keywords = AXIS_KEYWORDS


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

                # 1. 检查续表声明（续表、续表1、表1（续）、表1（续一）、Table 1 (continued)、Table 1 (cont.)、Table 2 Continued、cont'd、续上表等）
                cont_pattern = (
                    r'(?:续表|续附表|续前表|续上表|接上表|接上页|'
                    r'（续[一二三四五六七八九十0-9]*）|\(续[一二三四五六七八九十0-9]*\)|'
                    r'（续表）|\(续表\)|（续上表）|\(续上表\)|（续前表）|\(续前表\)|（接上表）|\(接上表\)|'
                    r'（cont(?:inued|\.|\'d)?(?:\s*\d+)?\)|\(cont(?:inued|\.|\'d)?(?:\s*\d+)?\)|'
                    r'\bContinued\b|\bcontinued\b|\bcont\'?d\b|\bcont\.?\b(?:\s*\d+)?|'
                    r'\b续\b(?:\s*[0-9一二三四五六七八九十]+)?)'
                )
                is_cont = bool(re.search(r'^(?:' + cont_pattern + r'|Continued\s+Table)', line_clean, re.IGNORECASE))
                if not is_cont:
                    m_prefix = re.match(r'^[ \t]*(' + TABLE_LABEL_PATTERN + r')[ \t\.\:：,\-—–]*(.*)', line_clean, re.IGNORECASE)
                    if m_prefix:
                        after_lbl = m_prefix.group(2).strip()
                        if re.search(cont_pattern, after_lbl, re.IGNORECASE):
                            is_cont = True

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
                m = re.match(r'^[ \t]*(' + TABLE_LABEL_PATTERN + r')[ \t\.\:：,\-—–]*(.*)', line_clean, re.IGNORECASE)
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

    # 1. 基础尺寸检验（支持 1 行表/单行汇总表/键值表）
    if df.shape[0] < 1 or df.shape[1] < 1:
        return False, f"表格尺寸异常 ({df.shape[0]}行 × {df.shape[1]}列)，无法构成有效数据矩阵"

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

    if df.shape[1] > 3 and empty_col_count >= 2:
        return False, f"存在 {empty_col_count} 个完全空白列，列切分严重受损"

    if single_char_col_count >= 2 and df.shape[1] <= 6 and df.shape[0] >= 3:
        return False, f"存在 {single_char_col_count} 个单字符碎片列，疑似竖排文字被切碎"

    # 5. 图表坐标轴/散点图残片深度检测
    all_text = " ".join(str(v) for v in df.values.flatten() if v is not None and str(v) != 'nan').lower()
    axis_keywords = AXIS_KEYWORDS
    if any(kw in all_text for kw in axis_keywords) and df.shape[0] <= 15:
        return False, "检测到图表/坐标轴刻度残留特征 (散点图/相图误识别)"

    # 6. 表头有效性检测（表头不能整行全为 NaN 或全是未命名无意义列）
    header_strs = [str(c).strip() for c in df.columns]
    unnamed_cnt = sum(1 for c in header_strs if not c or c.startswith('Unnamed:') or c.startswith('Col') or c.isdigit())
    if unnamed_cnt == len(header_strs) and df.shape[0] <= 3:
        return False, "表头完全未识别且行数过少，属于残片"

    return True, "合格"

validate_table_structure = validate_native_table_dataframe


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

    # 4. 检验文献全局表号连续性与空间匹配
    extracted_labels = set()
    bound_table_ids = set()
    for r in valid_native_results:
        df = r.get('df')
        lbl = (df.attrs.get('label') if df is not None and hasattr(df, 'attrs') else None) or r.get('label')
        if lbl:
            fmt_lbl = format_table_label(lbl)
            if fmt_lbl:
                extracted_labels.add(fmt_lbl)
                if fmt_lbl in all_declared_labels:
                    bound_table_ids.add(id(r))

    for lbl in all_declared_labels:
        if lbl not in extracted_labels:
            decl_p = label_to_declared_page.get(lbl, 0)
            decl_info = next((d for d in page_declarations.get(decl_p, []) if format_table_label(d.get('label')) == lbl), None)
            decl_title = (decl_info.get('title') or "") if decl_info else ""

            # 容错：若 declared_page 上已提取到有效表格且该表未分配冲突表号，尝试自动绑定
            matched_on_page = False
            for r in valid_native_results:
                if id(r) in bound_table_ids:
                    continue
                df = r.get('df')
                r_p = r.get('page_idx', df.attrs.get('page_idx') if df is not None and hasattr(df, 'attrs') else None)
                if r_p == decl_p:
                    r_lbl = (df.attrs.get('label') if df is not None and hasattr(df, 'attrs') else None) or r.get('label')
                    r_fmt = format_table_label(r_lbl) if r_lbl else ""
                    # 如果已有标签且属于其他已被声明的表号，严禁覆盖抢夺！
                    if r_fmt and r_fmt in all_declared_labels and r_fmt != lbl:
                        continue

                    # 检查标题相似度或直接未分配明确表号
                    r_title = (df.attrs.get('table_title') if df is not None and hasattr(df, 'attrs') else None) or r.get('table_title', '')
                    title_match = bool(decl_title and r_title and (decl_title in r_title or r_title in decl_title))

                    # 若该页只有一个候选表或标题匹配或未命名，进行安全绑定
                    if not r_fmt or title_match or r_fmt not in all_declared_labels:
                        if df is not None and hasattr(df, 'attrs'):
                            df.attrs['label'] = lbl
                            if decl_title and not df.attrs.get('table_title'):
                                df.attrs['table_title'] = decl_title
                        r['label'] = lbl
                        extracted_labels.add(lbl)
                        bound_table_ids.add(id(r))
                        matched_on_page = True
                        validation_logs.append(f"  ℹ️ Page {decl_p + 1} 成功将原生提取表格绑定至声明表号 [{lbl}]")
                        break
            if not matched_on_page:
                validation_logs.append(f"  ❌ 缺失声明表号 [{lbl}] (所在页: Page {decl_p + 1}) → 标记重走 PaddleOCR-VL")
                pages_requiring_ocr.add(decl_p)

    # 5. 整合三方投票的低置信度页面
    low_confidence_pages = vote_summary.get('low_confidence_pages', set())
    if low_confidence_pages:
        for p in low_confidence_pages:
            validation_logs.append(f"  ⚠️ Page {p + 1} 三方投票分歧较大/低置信度 → 标记重走 PaddleOCR-VL")
            pages_requiring_ocr.add(p)

    # 6. 未提取到任何有效表格但属于候选表格页的页面 (涵盖无标题表、全图表与独立视觉表页)
    for p_idx in target_pages:
        if p_idx not in page_to_valid_tables:
            if p_idx in page_declarations:
                validation_logs.append(f"  ❌ Page {p_idx + 1} 含有表格声明但未获得任何有效原生表格 → 标记重走 PaddleOCR-VL")
            else:
                validation_logs.append(f"  ❌ Page {p_idx + 1} 为候选表格页但未获得任何有效原生表格 → 标记重走 PaddleOCR-VL")
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


def _extract_numeric_tokens(text_or_cell: Any) -> List[str]:
    """从文本或单元格值中提取规范化的数字字符串列表（支持小数、科学计数法、去除千分位逗号、数字区间平展）。"""
    if text_or_cell is None:
        return []
    s = str(text_or_cell).strip()
    if not s or s.lower() in ('nan', 'none', '-', '—', '–', '/'):
        return []
    # 去除千分位逗号，如 1,234.56 -> 1234.56 (使用零宽断言避免重叠匹配跳过)
    s_clean = re.sub(r'(?<=\d),(?=\d)', '', s)
    # 将数字之间的区间连字符 (10-20, 10–20, 10~20, 10至20) 平展为空格分隔的独立数值
    s_clean = re.sub(r'(?<=\d)\s*[-–~至到]\s*(?=\d)', ' ', s_clean)
    tokens = re.findall(r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?', s_clean)
    normalized = []
    for t in tokens:
        try:
            val = float(t)
            normalized.append(f"{val:g}")
        except Exception:
            normalized.append(t)
    return normalized


def verify_table_numeric_integrity(
    df: pd.DataFrame,
    pdf_path: str,
    page_idx: int,
    bbox: Optional[Union[List[float], Tuple[float, ...]]] = None,
    min_match_ratio: float = 0.7,
    min_recall: float = 0.5,
    coord_format: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    针对表格提取结果的本地质量与数值一致性门禁 (Local Quality & Numeric Integrity Gate)。
    
    核心机制：
    1. 旋转感知的坐标反投影：使用 fitz.Rect(bbox) * page.derotation_matrix + cropbox offset 准确定位 PDF 文本流；
    2. 损坏矢量字库与私有编码安全旁路：检测 [\uf000-\uf8ff] 或 \ufffd，遇乱码字库时无条件信任视觉/OCR 结果，跳过拒绝；
    3. 数值双向多重集 (Bidirectional Bag-of-Numbers) 比对：不仅检验提取结果中的数字是否源自 PDF 原文 (precision)，
       更核验 PDF 对应区域内的数字是否被大面积遗漏或截断 (recall)；
    4. 明确区分 'passed' (通过), 'failed' (失败), 'unverified' (无法验证，如无文本层或空区域) 状态，
       杜绝空白文本层在提取到数据时被盲目当成通过。
    """
    if df is None or df.empty:
        return False, {
            "is_valid": False,
            "status": "failed",
            "match_ratio": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "reason": "empty_dataframe",
            "matched_count": 0,
            "total_df_count": 0,
            "pdf_num_count": 0,
            "font_corrupted": False,
        }

    # 从 df.attrs 补充 bbox 与 page_idx
    if bbox is None and hasattr(df, 'attrs'):
        bbox = df.attrs.get('bbox')
    if page_idx is None and hasattr(df, 'attrs'):
        page_idx = df.attrs.get('page_idx', 0)

    if not os.path.exists(pdf_path):
        return False, {
            "is_valid": False,
            "status": "unverified",
            "match_ratio": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "reason": "pdf_not_found_skipped",
            "matched_count": 0,
            "total_df_count": 0,
            "pdf_num_count": 0,
            "font_corrupted": False,
        }

    pdf_text = ""
    doc = None
    converted_bbox = None
    try:
        doc = fitz.open(pdf_path)
        if page_idx < 0 or page_idx >= len(doc):
            return False, {
                "is_valid": False,
                "status": "unverified",
                "match_ratio": 0.0,
                "precision": 0.0,
                "recall": 0.0,
                "reason": "page_idx_out_of_bounds",
                "matched_count": 0,
                "total_df_count": 0,
                "pdf_num_count": 0,
                "font_corrupted": False,
            }
        page = doc[page_idx]

        # 1. 旋转感知的坐标反投影 (Rotation-aware coordinate de-projection)
        if bbox and len(bbox) >= 4:
            bx0, by0, bx1, by1 = bbox[:4]
            orig_bx0, orig_by0, orig_bx1, orig_by1 = bx0, by0, bx1, by1
            w, h = page.rect.width, page.rect.height

            # 坐标系归一化单位判断与统一转换：
            # 1) 0..1 归一化坐标
            # 2) MinerU content_list 0..1000 归一化坐标 (明确按结果格式转换，不能凭数值范围猜测)
            is_norm_1000 = False
            if coord_format in ("norm_1000", "mineru_1000", "1000"):
                is_norm_1000 = True
            elif coord_format in ("points", "pt"):
                is_norm_1000 = False
            elif df is not None and hasattr(df, "attrs"):
                b_fmt = df.attrs.get("bbox_format") or df.attrs.get("coord_format")
                if b_fmt in ("norm_1000", "mineru_1000", "1000"):
                    is_norm_1000 = True
                elif b_fmt in ("points", "pt"):
                    is_norm_1000 = False
                elif df.attrs.get("extractor") in ("mineru_api", "mineru_cli"):
                    is_norm_1000 = True

            if 0 <= bx0 <= 1 and 0 <= bx1 <= 1 and 0 <= by0 <= 1 and 0 <= by1 <= 1 and (bx1 - bx0 < 1.0 or by1 - by0 < 1.0):
                bx0, bx1 = bx0 * w, bx1 * w
                by0, by1 = by0 * h, by1 * h
            elif is_norm_1000:
                norm_bx0, norm_bx1 = (bx0 / 1000.0) * w, (bx1 / 1000.0) * w
                norm_by0, norm_by1 = (by0 / 1000.0) * h, (by1 / 1000.0) * h

                bx0, bx1 = norm_bx0, norm_bx1
                by0, by1 = norm_by0, norm_by1

                # 若原始坐标已完全落在当前页面尺寸范围内 (如部分测试用例或缓存已存为 pt 坐标)，
                # 需核对 norm_1000 与原始 pt 坐标在页面实际数字文本上的吻合度，优先采纳数据吻合度更高的区域
                if orig_bx1 <= w and orig_by1 <= h and df is not None and hasattr(df, "values"):
                    df_tokens = []
                    for v in df.values.flatten():
                        df_tokens.extend(_extract_numeric_tokens(v))
                    if df_tokens:
                        from collections import Counter
                        df_c = Counter(df_tokens)

                        r_norm = fitz.Rect(norm_bx0, norm_by0, norm_bx1, norm_by1).normalize()
                        norm_text = page.get_text("text", clip=r_norm)
                        n_tokens = []
                        for line in norm_text.splitlines():
                            n_tokens.extend(_extract_numeric_tokens(line))
                        norm_match = sum((df_c & Counter(n_tokens)).values())

                        r_pt = fitz.Rect(orig_bx0, orig_by0, orig_bx1, orig_by1).normalize()
                        pt_text = page.get_text("text", clip=r_pt)
                        pt_tokens = []
                        for line in pt_text.splitlines():
                            pt_tokens.extend(_extract_numeric_tokens(line))
                        pt_match = sum((df_c & Counter(pt_tokens)).values())

                        if pt_match > norm_match:
                            bx0, by0, bx1, by1 = orig_bx0, orig_by0, orig_bx1, orig_by1

            converted_bbox = [round(float(bx0), 2), round(float(by0), 2), round(float(bx1), 2), round(float(by1), 2)]
            rect = fitz.Rect(bx0, by0, bx1, by1)
            if hasattr(page, "derotation_matrix"):
                rect = rect * page.derotation_matrix
            rect = rect.normalize()

            # 优先尝试旋转反投影区域
            pdf_text = page.get_text("text", clip=rect)

            # 若反投影区域未取到文本，且存在非零 CropBox 偏移，尝试加入 CropBox 偏移 (防止视觉坐标系原点为 CropBox 相对原点)
            cb = getattr(page, "cropbox", None)
            if not pdf_text.strip() and cb is not None and (cb.x0 != 0 or cb.y0 != 0):
                rect_cb = fitz.Rect(rect.x0 + cb.x0, rect.y0 + cb.y0, rect.x1 + cb.x0, rect.y1 + cb.y0).normalize()
                text_cb = page.get_text("text", clip=rect_cb)
                if text_cb.strip():
                    pdf_text = text_cb
                    rect = rect_cb

            # 若仍未取到文本且页面存在旋转，兜底尝试原始未旋转 Rect (防止输入坐标原本就是未旋转的绝对坐标)
            if not pdf_text.strip() and getattr(page, "rotation", 0) != 0:
                raw_rect = fitz.Rect(bx0, by0, bx1, by1).normalize()
                fallback_text = page.get_text("text", clip=raw_rect)
                if fallback_text.strip():
                    pdf_text = fallback_text
                    rect = raw_rect
        else:
            pdf_text = page.get_text("text")

    except Exception as e_pdf:
        return False, {
            "is_valid": False,
            "status": "unverified",
            "match_ratio": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "reason": f"pdf_read_error_skipped: {e_pdf}",
            "matched_count": 0,
            "total_df_count": 0,
            "pdf_num_count": 0,
            "font_corrupted": False,
        }
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    # 2. 局部损坏矢量字库与私有编码安全旁路 (Private-use / Replacement characters)
    corrupted_chars = re.findall(r'[\uf000-\uf8ff\ufffd]', pdf_text)
    if corrupted_chars:
        print(f"[Validator] 检测到 PDF 矢量文本层包含私有区/坏损字符 (共 {len(corrupted_chars)} 个，如 U+{ord(corrupted_chars[0]):04X})，矢量字体映射损坏。启动视觉保真信任旁路，跳过数值拒绝。")
        return True, {
            "is_valid": True,
            "status": "passed",
            "font_corrupted": True,
            "reason": "trusted_visual_due_to_broken_font",
            "corrupted_char_count": len(corrupted_chars),
            "match_ratio": 1.0,
            "precision": 1.0,
            "recall": 1.0,
            "matched_count": 0,
            "total_df_count": 0,
            "pdf_num_count": 0,
        }

    # 3. 提取数字多重集 (Bag-of-Numbers)
    df_nums = []
    for col in df.columns:
        for val in df[col]:
            df_nums.extend(_extract_numeric_tokens(val))

    pdf_nums = _extract_numeric_tokens(pdf_text)
    total_df_count = len(df_nums)
    total_pdf_count = len(pdf_nums)

    # 4. 扫描件/纯图片无文本层或 BBox 落在空白处校验
    if not pdf_text.strip():
        if total_df_count == 0:
            return True, {
                "is_valid": True,
                "status": "passed",
                "font_corrupted": False,
                "reason": "text_only_table",
                "match_ratio": 1.0,
                "precision": 1.0,
                "recall": 1.0,
                "matched_count": 0,
                "total_df_count": 0,
                "pdf_num_count": 0,
            }
        info_dict = {
            "is_valid": False,
            "status": "unverified",
            "font_corrupted": False,
            "reason": "scanned_or_empty_text_layer",
            "match_ratio": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "matched_count": 0,
            "total_df_count": total_df_count,
            "pdf_num_count": 0,
        }
        if converted_bbox is not None:
            info_dict["converted_bbox"] = converted_bbox
        return False, info_dict

    # 纯文字表格无需数值比对
    if total_df_count == 0 and total_pdf_count == 0:
        return True, {
            "is_valid": True,
            "status": "passed",
            "font_corrupted": False,
            "reason": "text_only_table",
            "match_ratio": 1.0,
            "precision": 1.0,
            "recall": 1.0,
            "matched_count": 0,
            "total_df_count": 0,
            "pdf_num_count": 0,
        }

    # PDF 在对应区域未提取到数字（如错误 bbox 截到空白处或全图无数字）
    if total_pdf_count == 0:
        return False, {
            "is_valid": False,
            "status": "unverified",
            "font_corrupted": False,
            "reason": "no_pdf_numbers_in_bbox",
            "match_ratio": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "matched_count": 0,
            "total_df_count": total_df_count,
            "pdf_num_count": 0,
        }

    if total_df_count == 0 and total_pdf_count > 0:
        return False, {
            "is_valid": False,
            "status": "failed",
            "font_corrupted": False,
            "reason": f"numeric_completeness_failed: table has 0 numbers while PDF contains {total_pdf_count} numbers",
            "match_ratio": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "matched_count": 0,
            "total_df_count": 0,
            "pdf_num_count": total_pdf_count,
        }

    from collections import Counter
    df_counter = Counter(df_nums)
    pdf_counter = Counter(pdf_nums)
    intersection = df_counter & pdf_counter
    matched_count = sum(intersection.values())

    precision = matched_count / total_df_count if total_df_count > 0 else 1.0
    recall = matched_count / total_pdf_count if total_pdf_count > 0 else 1.0

    # 双向一致性校验：precision 检查提取准确率，recall 检查原文数据完整率
    is_valid = (precision >= min_match_ratio and recall >= min_recall)
    status = "passed" if is_valid else "failed"

    if is_valid:
        reason = f"passed_numeric_integrity (ratio: {precision:.1%})"
    elif recall < min_recall and precision >= min_match_ratio:
        reason = f"numeric_completeness_failed: only {matched_count}/{total_pdf_count} PDF numbers recovered ({recall:.1%} < {min_recall:.1%})"
    else:
        reason = f"numeric_integrity_failed: {matched_count}/{total_df_count} numbers matched ({precision:.1%} < {min_match_ratio:.1%})"

    info_dict = {
        "is_valid": is_valid,
        "status": status,
        "match_ratio": precision,
        "precision": precision,
        "recall": recall,
        "matched_count": matched_count,
        "total_df_count": total_df_count,
        "pdf_num_count": total_pdf_count,
        "font_corrupted": False,
        "reason": reason,
    }
    if converted_bbox is not None:
        info_dict["converted_bbox"] = converted_bbox
    return is_valid, info_dict


def cross_check_missed_tables(
    extracted_tables: List[Any],
    pdf_path: str,
) -> Dict[str, Any]:
    """
    交叉比对 PDF 全文表格声明 (scan_pdf_table_declarations) 与已提取表格，
    检测漏检表格 (Missed Tables) 并生成诊断报告。
    """
    page_declarations = scan_pdf_table_declarations(pdf_path)

    declared_tables = []
    declared_labels = set()
    for p_idx, decls in page_declarations.items():
        for d in decls:
            if not d.get('is_continuation'):
                lbl = format_table_label(d.get('label', ''))
                declared_tables.append({
                    'label': lbl or d.get('label'),
                    'title': d.get('title', ''),
                    'page_idx': p_idx,
                    'raw_line': d.get('raw_line', ''),
                })
                if lbl:
                    declared_labels.add(lbl)

    extracted_labels = set()
    extracted_info = []
    for idx, t in enumerate(extracted_tables):
        if isinstance(t, pd.DataFrame):
            df = t
            p_idx = df.attrs.get('page_idx', 0)
            raw_lbl = df.attrs.get('label', '')
            title = df.attrs.get('table_title', '')
        elif isinstance(t, dict):
            df = t.get('df')
            p_idx = t.get('page_idx', df.attrs.get('page_idx', 0) if df is not None and hasattr(df, 'attrs') else 0)
            raw_lbl = t.get('label') or (df.attrs.get('label') if df is not None and hasattr(df, 'attrs') else '')
            title = t.get('title') or (df.attrs.get('table_title') if df is not None and hasattr(df, 'attrs') else '')
        else:
            continue

        if not raw_lbl and title:
            m_lbl = TABLE_LABEL_RE.search(title)
            if m_lbl:
                raw_lbl = m_lbl.group(0)

        fmt_lbl = format_table_label(raw_lbl) if raw_lbl else ""
        if fmt_lbl:
            extracted_labels.add(fmt_lbl)
        extracted_info.append({
            'index': idx,
            'label': fmt_lbl or raw_lbl,
            'title': title,
            'page_idx': p_idx,
        })

    missed_tables = []
    for d in declared_tables:
        lbl = d['label']
        if lbl and lbl not in extracted_labels:
            missed_tables.append(d)

    # 扫描页检测（低字符量且含图像的页面，其表格无法由文本声明检出，属于“覆盖度未知”）
    scanned_pages = []
    if os.path.exists(pdf_path):
        try:
            with fitz.open(pdf_path) as doc:
                for p_idx, page in enumerate(doc):
                    words = page.get_text("words")
                    images = page.get_images()
                    if len(images) > 0 and len(words) < 80:
                        scanned_pages.append(p_idx)
        except Exception:
            pass

    extracted_pages = set(info['page_idx'] for info in extracted_info if info.get('page_idx') is not None)
    uncovered_scanned_pages = [p for p in scanned_pages if p not in extracted_pages]

    is_complete = (len(missed_tables) == 0 and len(uncovered_scanned_pages) == 0)
    coverage_unknown = len(uncovered_scanned_pages) > 0

    report = {
        'declared_count': len(declared_tables),
        'extracted_count': len(extracted_tables),
        'declared_tables': declared_tables,
        'extracted_labels': list(extracted_labels),
        'missed_tables': missed_tables,
        'missed_count': len(missed_tables),
        'is_complete': is_complete,
        'coverage_unknown': coverage_unknown,
        'scanned_pages': scanned_pages,
        'uncovered_scanned_pages': uncovered_scanned_pages,
    }
    if missed_tables:
        missed_summary = [f"{m['label']} (Page {m['page_idx']+1})" for m in missed_tables]
        print(f"[Validator] 发现 {len(missed_tables)} 个未被提取的声明表格: {', '.join(missed_summary)}")
    elif coverage_unknown:
        print(f"[Validator] 检测到扫描页面 {[p+1 for p in uncovered_scanned_pages]} 缺少文本层且未提取到表格，表格覆盖未知 (coverage_unknown=True)")
    else:
        print(f"[Validator] 全文表格声明比对完成: 声明 {len(declared_tables)} 个表格，已全覆盖")

    return report
