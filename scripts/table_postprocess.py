#!/usr/bin/env python3
"""
table_postprocess.py — 表格 DataFrame 纯文本后处理（自 extract_zotero_table.py 拆出）。

包含：VLM/Markdown 结构化内容解析、LaTeX/OCR 文本清洗、多行子行展开、
数值类型推断、Excel 公式转义、section 类别推断、续表合并、参考文献换行识别。
仅依赖 pandas，无 PDF / 浏览器等重依赖。
"""

import io
import os
import re
from typing import List, Optional, Tuple, Dict, Any, Union

import pandas as pd

try:
    from .common import format_table_label, TABLE_LABEL_PATTERN, TABLE_LABEL_RE, is_markdown_separator, df_map
except ImportError:
    from common import format_table_label, TABLE_LABEL_PATTERN, TABLE_LABEL_RE, is_markdown_separator, df_map


def clean_text(val):
    if val is None:
        return ""
    if not isinstance(val, str):
        return val
    val = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', val)
    val = val.encode('utf-8', 'ignore').decode('utf-8')
    return val.strip()

def parse_structured_vlm_content(content):
    """
    Parses full-document Markdown & HTML text output by PaddleOCR-VL-1.6 / Layout VLM into DataFrames with table labels.
    Handles both HTML <table>...</table> blocks (with rowspan/colspan) and Markdown |---| tables.
    Strictly bounds caption extraction between consecutive tables to prevent multi-table cross-contamination.
    """
    tables = []
    
    # 1. Parse HTML <table> blocks first
    table_re = re.compile(r'<table.*?>.*?</table>', re.DOTALL | re.IGNORECASE)
    div_all_re = re.compile(r'<div[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE)
    
    html_matches = list(table_re.finditer(content))
    prev_end = 0
    
    for match in html_matches:
        table_html = match.group(0)
        chunk_before = content[prev_end : match.start()]
        prev_end = match.end()
        
        # 提取 chunk_before 尾部的所有 <div> 标签（PaddleOCR-VL 标准 caption 格式）
        divs = [clean_text(m.group(1)) for m in div_all_re.finditer(chunk_before[-1000:]) if clean_text(m.group(1))]
        
        # 检查是否为插图误识别为表格（例如矿物共生顺序图、柱状图等只有图题没有表题的图形）
        has_table_cap = any(TABLE_LABEL_RE.search(d) for d in divs)
        has_fig_cap = any(re.search(r'^(?:图|Fig(?:ure)?\.?)\s*\d+', d, re.IGNORECASE) for d in divs)
        if not has_table_cap and has_fig_cap:
            # 确定为纯插图（如 图4 板溪锑矿床成矿阶段与矿物生成顺序图），跳过
            continue
            
        curr_caption = ""
        # 1. 优先从 <div> 中寻找包含表题的条目并合并可能跨 div 的表描述
        # 若包含中英文双语 caption，优先提取中文表题
        table_div_indices = [i for i, d in enumerate(divs) if TABLE_LABEL_RE.search(d)]
        if table_div_indices:
            # 优先选择包含中文的表题
            cn_indices = [i for i in table_div_indices if re.search(r'[\u4e00-\u9fa5]', divs[i])]
            chosen_idx = cn_indices[-1] if cn_indices else table_div_indices[-1]
            combined_parts = [divs[chosen_idx]]
            if chosen_idx + 1 < len(divs) and not re.search(r'^(?:图|表|Table|Fig)', divs[chosen_idx + 1]):
                combined_parts.append(divs[chosen_idx + 1])
            curr_caption = " ".join(combined_parts)
        else:
            # 回退：从 chunk_before 向上逆序查找最近的表标题行
            cap_lines = []
            rev_lines = [re.sub(r'<[^>]+>', ' ', l).strip() for l in chunk_before.split('\n') if l.strip()]
            rev_lines.reverse()
            for l_c in rev_lines:
                if not l_c:
                    continue
                m_num = re.match(r'^(\d+\.\d+)\s+([^\n]+)', l_c)
                if TABLE_LABEL_RE.search(l_c) and not l_c.startswith(('（', '(')):
                    if any(kw in l_c for kw in ['见表', '从表', '、表', '和表', '或表', '及表']):
                        continue
                    cap_lines.append(l_c)
                    break
                elif m_num and not l_c.startswith(('（', '(')):
                    cap_lines.append(l_c)
                    break
                elif len(l_c) > 60:
                    break
                    
            if cap_lines:
                cap_lines.reverse()
                curr_caption = ' '.join(cap_lines)
            
        curr_caption = clean_latex_and_ocr(curr_caption)
        
        t_num_match = TABLE_LABEL_RE.search(curr_caption)
        m_pure_num = re.search(r'\b\d+\.\d+\b', curr_caption)
        if t_num_match:
            curr_label = format_table_label(t_num_match.group(0))
        elif m_pure_num:
            curr_label = format_table_label(m_pure_num.group(0))
        else:
            curr_label = ""
        
        try:
            dfs = pd.read_html(io.StringIO(table_html))
            if dfs:
                df = dfs[0]
                if isinstance(df.columns, pd.MultiIndex):
                    flat_cols = []
                    for col_tuple in df.columns:
                        parts = [clean_latex_and_ocr(clean_text(str(p))).strip() for p in col_tuple if p is not None and str(p).strip() and not str(p).lower().startswith(('unnamed', 'col')) and str(p).lower() not in ['nan', 'none']]
                        dedup_parts = []
                        for p in parts:
                            if not dedup_parts or p != dedup_parts[-1]:
                                dedup_parts.append(p)
                        flat_cols.append('_'.join(dedup_parts) if dedup_parts else '')
                    df.columns = flat_cols
                if curr_label:
                    df.attrs['label'] = curr_label
                if curr_caption:
                    df.attrs['table_title'] = curr_caption
                df.attrs['extractor'] = 'paddleocr_vl'
                tables.append(df)
        except Exception:
            pass
            
    # 2. Parse Markdown |---| tables if HTML tables are not present
    if not tables:
        tables = parse_structured_markdown_tables(content)
        
    return tables

def parse_structured_markdown_tables(md_content):
    """Parses markdown text output by PaddleOCR-VL-1.6 / Layout VLM into DataFrames with table labels.

    修复：增加列碎片化检测——当解析出的列数异常多（>30）且单元格填充率极低（<30%）时，
    判定为 OCR 碎片化失败，跳过该表避免产出 889r×131c 类垃圾表。
    """
    tables = []
    lines = md_content.split("\n")
    curr_caption = ""
    curr_label = ""
    curr_table_lines = []
    in_table = False
    
    def _try_parse_table(table_lines, caption, label):
        """Parse markdown table lines into DataFrame with fragmentation guard."""
        try:
            clean_lines = [line for line in table_lines if not is_markdown_separator(line)]
            if len(clean_lines) < 1:
                return None
            csv_data = "\n".join([line.strip("|") for line in clean_lines])
            if len(clean_lines) == 1:
                raw_cells = [c.strip() for c in clean_lines[0].split('|') if c.strip()]
                if not raw_cells:
                    return None
                df = pd.DataFrame([raw_cells], columns=[f"Col_{i+1}" for i in range(len(raw_cells))])
            else:
                df = pd.read_csv(io.StringIO(csv_data), sep=r'\s*\|\s*', engine='python')
            
            # 碎片化检测：列数 >30 且填充率 <30% -> 跳过
            if df.shape[1] > 30:
                total_cells = df.shape[0] * df.shape[1]
                non_empty = sum(1 for v in df.values.flatten() if v is not None and str(v).strip())
                fill_ratio = non_empty / total_cells if total_cells > 0 else 0
                if fill_ratio < 0.30:
                    print(f"[Postprocess] 跳过碎片化表: {df.shape[0]}r×{df.shape[1]}c, fill={fill_ratio:.1%}")
                    return None
            
            if label:
                df.attrs['label'] = label
            if caption:
                df.attrs['table_title'] = caption
            return df
        except Exception:
            return None
    
    for l in lines:
        l_str = l.strip()
        m_lbl = re.search(r'\b(' + TABLE_LABEL_PATTERN + r'[\.\:\s]*[^\n]*)', l_str, re.I)
        if m_lbl and not in_table:
            curr_caption = m_lbl.group(1).strip()
            m_num = TABLE_LABEL_RE.search(curr_caption)
            if m_num:
                curr_label = format_table_label(m_num.group(0))
                
        if l_str.startswith("|") and l_str.endswith("|"):
            in_table = True
            curr_table_lines.append(l_str)
        else:
            if in_table and curr_table_lines:
                df = _try_parse_table(curr_table_lines, curr_caption, curr_label)
                if df is not None:
                    tables.append(df)
                curr_table_lines = []
                curr_caption = ""
                curr_label = ""
            in_table = False
            
    if in_table and curr_table_lines:
        df = _try_parse_table(curr_table_lines, curr_caption, curr_label)
        if df is not None:
            tables.append(df)
            
    return tables

def clean_latex_and_ocr(val):
    """Clean raw LaTeX math markup, OCR typos, and garbled symbols."""
    if not isinstance(val, str) or not val:
        return val
    
    # 0. Remove HTML tags if present
    val = re.sub(r'<[^>]+>', ' ', val)
    val = re.sub(r'\\n', ' ', val)
    
    # 1. Clean LaTeX cases environments
    val = re.sub(r'\\begin\{[^}]*\}|\\end\{[^}]*\}', '', val)
    val = re.sub(r'\\\\', ' ', val)
    val = re.sub(r'&amp;|&', ' ', val)

    # 2. Remove \text{...}, \mathrm{...}, \mathbf{...}
    val = re.sub(r'\\(text|mathrm|mathbf|mathit|boldsymbol)\{([^}]*)\}', r'\2', val)
    val = re.sub(r'\\math[a-z]+\{([^}]*)\}', r'\1', val)
    
    # 3. Standardize math symbols & superscripts
    val = val.replace(r'\times', '×').replace(r'\pm', '±').replace(r'\cdot', '·').replace(r'^\circ', '°').replace(r'\circ', '°').replace(r'^{\circ}', '°')
    val = val.replace(r'\Delta', 'Δ').replace(r'\delta', 'δ').replace(r'\mu', 'μ').replace(r'\omega', 'ω').replace(r'\phi', 'φ').replace(r'\beta', 'β').replace(r'\gamma', 'γ')
    
    # Superscripts / subscripts cleanup: ^{206} -> 206, _{...} -> ...
    val = re.sub(r'\^\{+([0-9a-zA-Z\+\-\*\s]+)\}+', r'^\1', val)
    val = re.sub(r'_\{+([0-9a-zA-Z\+\-\*\s]+)\}+', r'_\1', val)
    val = re.sub(r'(?<=\d)\s*times\s*(?=\d)', ' × ', val)
    
    # Strip LaTeX $ delimiters and braces
    val = val.replace('$', '').replace('\\', '').replace('{', '').replace('}', '')
    val = re.sub(r'([A-Za-z])_([0-9]+)', r'\1\2', val)
    val = re.sub(r'\s*±\s*', ' ± ', val)
    val = re.sub(r'\s*\^\s*([0-9+-]+)\s*', r'^\1', val)
    
    # 4. Fix common OCR typos in chemical formulas & mineral terms
    val = re.sub(r'\bNaC1\b', 'NaCl', val)
    val = re.sub(r'\bCaC12\b', 'CaCl2', val)
    val = re.sub(r'\bH20\b', 'H2O', val)
    val = re.sub(r'\bCQ\b', 'CO2', val)
    val = re.sub(r'\bCtL\b', 'CH4', val)
    val = re.sub(r'\bTin-\b', 'Tm -', val)
    val = re.sub(r'\bTin\b', 'Tm', val)
    val = re.sub(r'\bcastiterite\b', 'cassiterite', val, flags=re.I)
    val = re.sub(r'\bberthlerite\b', 'berthierite', val, flags=re.I)
    val = re.sub(r'\bFable\b', 'Table', val, flags=re.I)
    
    # Fix common OCR garbled text in mineral paragenesis
    val = val.replace('(_+qz)', '(± qz)').replace('(_+ qz)', '(± qz)')
    val = re.sub(r'\b200o\b', '200°', val)
    val = val.replace("'-", "-")
    
    # Normalize space line-by-line while preserving \n for subrow expansion
    lines = [re.sub(r'[ \t]+', ' ', l).strip() for l in re.split(r'\n|\\n', val)]
    return '\n'.join([l for l in lines if l])


UNICODE_SUP_MAP = {
    '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4',
    '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
    '⁺': '+', '⁻': '-', '⁼': '=', '⁽': '(', '⁾': ')',
    'ⁿ': 'n', 'ⁱ': 'i'
}
UNICODE_SUB_MAP = {
    '₀': '0', '₁': '1', '₂': '2', '₃': '3', '₄': '4',
    '₅': '5', '₆': '6', '₇': '7', '₈': '8', '₉': '9',
    '₊': '+', '₋': '-', '₌': '=', '₍': '(', '₎': ')',
    'ₐ': 'a', 'ₑ': 'e', 'ₒ': 'o', 'ₓ': 'x', 'ₕ': 'h',
    'ₖ': 'k', 'ₗ': 'l', 'ₘ': 'm', 'ₙ': 'n', 'ₚ': 'p',
    'ₛ': 's', 'ₜ': 't'
}

def convert_latex_math_to_unicode(text: str) -> str:
    """将常见学术 LaTeX 数学符号转换为标准 Unicode 符号。"""
    if not isinstance(text, str) or not text:
        return text
    t = text
    t = t.replace(r'\pm', '±').replace(r'\times', '×').replace(r'\cdot', '·')
    t = t.replace(r'^\circ', '°').replace(r'^{\circ}', '°').replace(r'\circ', '°').replace(r'\degree', '°')
    t = t.replace(r'\leq', '≤').replace(r'\geq', '≥').replace(r'\neq', '≠').replace(r'\sim', '~')
    t = t.replace(r'\Delta', 'Δ').replace(r'\delta', 'δ').replace(r'\mu', 'μ')
    t = t.replace(r'\alpha', 'α').replace(r'\beta', 'β').replace(r'\gamma', 'γ')
    t = t.replace(r'\theta', 'θ').replace(r'\sigma', 'σ').replace(r'\omega', 'ω').replace(r'\phi', 'φ')
    return t


def parse_academic_runs(text: str) -> List[Tuple[str, Optional[str]]]:
    """
    将包含 LaTeX 上标/下标 (如 10^{-3}, CaCO_3, H_2O, Fe^{3+}) 或 Unicode 上下标 (如 10⁻³, CO₂)
    的学术字符串拆解为可供富文本渲染的文本段列表: [(chunk, vertAlign), ...]
    vertAlign 为 None, 'superscript', 或 'subscript'。
    严格保护 snake_case 变量标识符 (如 sample_id, station_name, table_1) 不被误认为上下标。
    """
    if not isinstance(text, str) or not text:
        return [(text or "", None)]

    t = convert_latex_math_to_unicode(text)
    if t.startswith('$') and t.endswith('$') and len(t) >= 2:
        t = t[1:-1]

    pattern = re.compile(
        r'(\^\{[^}]+\})|'
        r'(_\{[^}]+\})|'
        r'(\^(?:[+-]?\d+[+-]?|[+-]|[a-zA-Z]))|'
        r'(_\d+[+-]?)|'
        r'([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ]+)|'
        r'([₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ]+)'
    )

    runs = []
    last_idx = 0
    for m in pattern.finditer(t):
        start, end = m.span()
        sup1, sub1, sup2, sub2, uni_sup, uni_sub = m.groups()

        # Guard for sub2 (_digits): only treat as subscript if preceding text looks like a chemical symbol,
        # single variable, or bracket, NOT an identifier word like table_1, sample_1, group_1
        if sub2:
            prefix = t[:start]
            pre_match = re.search(r'([a-zA-Z0-9]+)$', prefix)
            if pre_match:
                pre_word = pre_match.group(1)
                if re.match(r'^[a-z]{3,}$', pre_word):
                    continue

        if start > last_idx:
            runs.append((t[last_idx:start], None))

        if sup1:
            runs.append((sup1[2:-1], 'superscript'))
        elif sub1:
            runs.append((sub1[2:-1], 'subscript'))
        elif sup2:
            runs.append((sup2[1:], 'superscript'))
        elif sub2:
            runs.append((sub2[1:], 'subscript'))
        elif uni_sup:
            converted = ''.join(UNICODE_SUP_MAP.get(c, c) for c in uni_sup)
            runs.append((converted, 'superscript'))
        elif uni_sub:
            converted = ''.join(UNICODE_SUB_MAP.get(c, c) for c in uni_sub)
            runs.append((converted, 'subscript'))
        last_idx = end

    if last_idx < len(t):
        runs.append((t[last_idx:], None))

    return [(r[0], r[1]) for r in runs if r[0]]


NOTE_PREFIX_PATTERN = re.compile(
    r'^(?:'
    r'\*+|\†|\‡|\§|\#|\¶|'
    r'Note\s*[:：]|Notes\s*[:：]|Footnote\s*[:：]|Source\s*[:：]|Sources\s*[:：]|'
    r'Legend\s*[:：]|Abbreviations?\s*[:：]|Abbr\s*[:：]|Remarks?\s*[:：]|'
    r'注\s*[:：]|说明\s*[:：]|数据来源\s*[:：]|附注\s*[:：]|本表\s*[:：]|'
    r'(?:\([a-z0-9]+\)|\[[a-z0-9]+\]|[a-e1-9]\))\s+|'
    r'(?:[a-e1-9])\s+(?=[A-Z\u4e00-\u9fa5])'
    r')',
    re.IGNORECASE
)

STAT_NOTE_PATTERN = re.compile(
    r'(\*+\s*p\s*[<>=]|\bp\s*<\s*0\.\d+|\bp\s*>\s*0\.\d+|\bmean\s*±|\bvalues\s+(?:in\s+parentheses|represent)|\bdiffer\s+significantly|\bnot\s+detected|\bbdl\b|\bna\b|\bp-value\b)',
    re.IGNORECASE
)

def is_academic_footnote_row(row_values: list, col_count: Optional[int] = None) -> Tuple[bool, str]:
    """
    判断单行是否为学术表格尾注/注释行 (olmOCR 2 启发式特征)。
    返回 (is_note, note_text)。
    """
    if col_count is None:
        col_count = len(row_values) if row_values else 1
    non_empty = []
    for v in row_values:
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ('nan', 'none', '-', '--', '—', ''):
            non_empty.append(s)

    if not non_empty:
        return False, ""

    all_same = (len(set(non_empty)) == 1)
    full_text = " ".join(dict.fromkeys(non_empty)) if all_same else " ".join(non_empty)
    full_text_clean = full_text.strip()

    # 1. 明确的尾注前缀 (Note:, 注:, * 等)
    if NOTE_PREFIX_PATTERN.search(full_text_clean):
        if len(non_empty) <= 2 or all_same or len(full_text_clean) > 20:
            return True, full_text_clean

    # 2. 显著性检验与统计学说明 (* p < 0.05, mean ± SD 等)
    if STAT_NOTE_PATTERN.search(full_text_clean):
        if len(non_empty) <= 2 or all_same or len(full_text_clean) > 15:
            return True, full_text_clean

    # 3. 散文解释句：多列表 (col_count > 1) 底部的单单元格/全跨列说明
    # 注意：单列表 (col_count <= 1) 本身每个单元格就是 1 个有效单元格且为正文数据，
    # 严禁在无明确注记前缀 (Note:, 注:, * 等) 时将正文长句误判为尾注
    if col_count > 1 and (len(non_empty) == 1 or all_same) and len(full_text_clean) >= 25:
        num_words = len(full_text_clean.split())
        chinese_count = len(re.findall(r'[\u4e00-\u9fa5]', full_text_clean))
        if num_words >= 5 or chinese_count >= 10:
            num_tokens = sum(1 for tok in re.split(r'[\s,]+', full_text_clean) if re.match(r'^-?\d+(?:\.\d+)?$', tok))
            if num_tokens <= 2:
                return True, full_text_clean

    return False, ""


def separate_table_footnotes(df: pd.DataFrame) -> pd.DataFrame:
    """
    olmOCR 2 启发的学术表格边界与尾注分离启发式算法。
    从表格底部向上扫描并剥离附注/注释行（如 '* p < 0.05', 'Note: ...', 'Source: ...', '注：...'），
    防止其污染数据矩阵，并将提取到的注释存入 df.attrs['footnotes']。
    同时检测第 0 行是否误将表题纳入数据体。
    """
    if df is None or df.empty or len(df) < 2:
        return df

    working_df = df.copy()
    extracted_notes = []

    # 1. 从底部向上剥离尾注
    while len(working_df) > 1:
        last_row_idx = len(working_df) - 1
        last_row_vals = working_df.iloc[last_row_idx].tolist()
        is_note, note_text = is_academic_footnote_row(last_row_vals, working_df.shape[1])
        if is_note:
            extracted_notes.append(note_text)
            working_df = working_df.iloc[:-1].reset_index(drop=True)
        else:
            break

    # 2. 检查顶行 (Row 0) 误包含表题边界
    if len(working_df) > 1:
        row0_vals = [str(v).strip() for v in working_df.iloc[0].tolist() if v is not None and str(v).strip() and str(v).lower() not in ('nan', 'none', '')]
        if len(row0_vals) == 1 or (len(row0_vals) > 1 and len(set(row0_vals)) == 1):
            r0_text = row0_vals[0]
            if TABLE_LABEL_RE.search(r0_text) or re.match(r'^(?:Table|表|Tab\.|Supplementary\s+Table|附表)\s*\d+', r0_text, re.IGNORECASE):
                if not working_df.attrs.get('table_title'):
                    working_df.attrs['table_title'] = r0_text
                if not working_df.attrs.get('label'):
                    m_lbl = TABLE_LABEL_RE.search(r0_text)
                    if m_lbl:
                        working_df.attrs['label'] = format_table_label(m_lbl.group(0))
                working_df = working_df.iloc[1:].reset_index(drop=True)

    # 3. 同步属性
    if extracted_notes:
        extracted_notes.reverse()
        existing = working_df.attrs.get('footnotes', [])
        if isinstance(existing, list):
            working_df.attrs['footnotes'] = existing + extracted_notes
        else:
            working_df.attrs['footnotes'] = [str(existing)] + extracted_notes

    return working_df


def expand_squeezed_columns(df):

    """
    检测并拆分因 PDF 提取器列边界合并而挤入单个单元格的空格分隔数值/元素列。
    例如：表头为 'Tb Dy Ho Er Tm Yb Lu Y ΣREE ΣREE+Y'，单元格数据为 '0.03 0.17 0.06 0.14 0.03 0.13 0.03 2.47 1.16 3.64'。
    本函数自动识别列内空格分隔值的众数模长 K (K >= 2)，并将该列拆分为 K 个独立列。
    """
    if df is None or df.empty or df.shape[1] == 0:
        return df

    from collections import Counter

    new_cols = []
    col_data = {}

    def is_pure_number_token(t: str) -> bool:
        return bool(re.match(r'^[<>]?[-+]?\d+(?:\.\d+)?(?:[%‰]|×10[-+]?\d+)?$', t.strip()))

    for col_idx in range(df.shape[1]):
        col_name = str(df.columns[col_idx])
        col_series = df.iloc[:, col_idx].tolist()

        non_empty = [str(v).strip() for v in col_series if v is not None and str(v).strip() and str(v).strip().lower() not in ['nan', 'none', '']]
        if not non_empty:
            new_cols.append(col_name)
            col_data[col_name] = col_series
            continue

        # 仅统计纯数值/测量数据 token 的空格分隔计数（排除中文字符串、代码、公式段落）
        numeric_token_counts = []
        for v in non_empty:
            parts = str(v).split()
            if len(parts) >= 2 and all(is_pure_number_token(p) for p in parts):
                numeric_token_counts.append(len(parts))

        # 当列内 >= 50% 且至少 3 行非空数据包含 >= 2 个纯数值的挤压值时才触发拆分
        if numeric_token_counts and len(non_empty) >= 4:
            cnt = Counter(numeric_token_counts)
            modal_k, modal_freq = cnt.most_common(1)[0]
            should_split = modal_k >= 2 and modal_freq >= 3 and (modal_freq / len(non_empty)) >= 0.5
        else:
            should_split = False

        # 当列内 >= 30% 的非空数据行为包含 >= 2 个数值的挤压值时触发拆分
        if should_split:
            h_tokens = [t for t in col_name.split() if not t.startswith('_') and not t.startswith('Unnamed')]
            if len(h_tokens) == modal_k:
                sub_names = h_tokens
            else:
                sub_names = [f'{col_name}_{i+1}' for i in range(modal_k)]

            print(f"[Postprocess] 自动拆分挤压列: {col_name!r} -> {modal_k} 列: {sub_names}")

            split_rows = []
            for v in col_series:
                if v is None or str(v).strip().lower() in ['nan', 'none', '']:
                    split_rows.append([''] * modal_k)
                else:
                    parts = str(v).split()
                    if len(parts) == modal_k:
                        split_rows.append(parts)
                    elif len(parts) < modal_k:
                        split_rows.append(parts + [''] * (modal_k - len(parts)))
                    else:
                        # 超过 modal_k 部分合并到最后一列
                        split_rows.append(parts[:modal_k-1] + [' '.join(parts[modal_k-1:])])

            for sub_i, sub_name in enumerate(sub_names):
                new_cols.append(sub_name)
                col_data[sub_name] = [r[sub_i] for r in split_rows]
        else:
            new_cols.append(col_name)
            col_data[col_name] = col_series

    res_df = pd.DataFrame(col_data)
    res_df.attrs = getattr(df, 'attrs', {}).copy()
    return res_df


def expand_multiline_subrows(df):
    """
    Expand multi-line merged cells (separated by \\n or \\\\n) into individual structured rows.
    Propagates parent metadata across expanded sub-rows while correctly absorbing single-column
    split identifiers from subsequent rows created by table extractors.
    """
    if df.empty:
        return df
        
    new_rows = []
    num_cols = len(df.columns)
    skip_indices = set()
    row_count = len(df)
    has_multiline = False
    
    for row_pos in range(row_count):
        if row_pos in skip_indices:
            continue
            
        row_iloc = df.iloc[row_pos]
        col_splits = {}
        max_splits = 1
        for col_pos in range(num_cols):
            cell = row_iloc.iloc[col_pos]
            is_na = False
            try:
                is_na = pd.isna(cell)
            except (ValueError, TypeError):
                is_na = False
            val = '' if is_na else str(cell)
            lines = [l.strip() for l in re.split(r'\n|\\n', val) if l.strip()]
            col_splits[col_pos] = lines
            if len(lines) > max_splits:
                max_splits = len(lines)

        if max_splits > 1:
            has_multiline = True
            # Check if subsequent rows contain values only for columns that have fewer lines
            # (e.g. col 0 has 1 line, but following rows have only col 0 filled)
            subsequent_col_values = {}
            lookahead = 1
            while row_pos + lookahead < row_count and lookahead < max_splits:
                next_row = df.iloc[row_pos + lookahead]
                next_non_empty_cols = []
                for cp in range(num_cols):
                    cval = next_row.iloc[cp]
                    if pd.notna(cval) and str(cval).strip():
                        next_non_empty_cols.append(cp)
                
                # If next_row only has values in columns where len(col_splits[cp]) < max_splits
                if next_non_empty_cols and all(len(col_splits[cp]) < max_splits for cp in next_non_empty_cols):
                    for cp in next_non_empty_cols:
                        if cp not in subsequent_col_values:
                            subsequent_col_values[cp] = []
                        subsequent_col_values[cp].append(str(next_row.iloc[cp]).strip())
                    skip_indices.add(row_pos + lookahead)
                    lookahead += 1
                else:
                    break
            
            for cp, extra_vals in subsequent_col_values.items():
                col_splits[cp].extend(extra_vals)

            for k in range(max_splits):
                row_list = []
                for col_pos in range(num_cols):
                    lines = col_splits[col_pos]
                    if len(lines) == 0:
                        row_list.append('')
                    elif len(lines) == 1:
                        row_list.append(lines[0])
                    elif k < len(lines):
                        row_list.append(lines[k])
                    else:
                        row_list.append(lines[-1])
                new_rows.append(row_list)
        else:
            new_rows.append([row_iloc.iloc[cp] for cp in range(num_cols)])
            
    if has_multiline:
        res_df = pd.DataFrame(new_rows, columns=df.columns)
        res_df.attrs = df.attrs.copy() if hasattr(df, 'attrs') else {}
        return res_df
    return df

def try_numeric(val):
    if isinstance(val, str):
        val_clean = val.replace(',', '').strip()
        # 保护带前导零的代码字符串（如 001, 08, -001）避免失真转为 1, 8
        if re.match(r'^[+-]?0\d+$', val_clean):
            return val
        try:
            if '.' in val_clean or 'e' in val_clean.lower():
                return float(val_clean)
            else:
                return int(val_clean)
        except ValueError:
            pass
    return val

def escape_formula(val):
    if isinstance(val, str) and val.startswith(('=', '+', '-', '@')):
        clean_val = val.replace(',', '').strip()
        # 保护带前导零的代码字符串（如 -001, +02），避免被转换为数值丢失前导零，加单引号防 Excel 解释
        if re.match(r'^[+-]?0\d+$', clean_val):
            return "'" + val
        try:
            if '.' in clean_val or 'e' in clean_val.lower():
                return float(clean_val)
            else:
                return int(clean_val)
        except ValueError:
            return "'" + val
    return val

def populate_section_categories(df):
    """
    Detect embedded section header rows (e.g. 'Sample BX4-5...', 'Group 1...')
    and propagate category labels into an explicit 'Sample / Ore Type' column.
    """
    if df.empty or len(df.columns) < 2:
        return df
        
    first_col = df.columns[0]
    cat_keywords = ['sample', 'group', 'zone', 'unit', 'phase', 'formation', 'member', 'type', 'deposit', '区段', '样品', '分组']
    
    has_cat_rows = False
    curr_cat = None
    sample_col_vals = []
    
    for idx, row in df.iterrows():
        c0_str = str(row[first_col]).strip() if pd.notna(row[first_col]) else ''
        other_cells = [str(x).strip() for x in row.iloc[1:] if pd.notna(x) and str(x).strip()]
        
        is_cat_row = False
        if c0_str:
            c0_lower = c0_str.lower()
            if any(c0_lower.startswith(kw) for kw in cat_keywords) and len(other_cells) <= (len(row) - 1) * 0.3:
                is_cat_row = True
                curr_cat = c0_str
                has_cat_rows = True
                
        sample_col_vals.append(curr_cat)
        
    if has_cat_rows and 'Sample / Ore Type' not in df.columns:
        df.insert(1, 'Sample / Ore Type', sample_col_vals)
        
    return df

def strip_embedded_duplicate_headers(df: pd.DataFrame) -> pd.DataFrame:
    """
    Scans the body of a DataFrame (rows 1 to end) for any embedded duplicate header rows
    reprinted across multi-page tables (e.g., repeating column names or unit rows midway through the table).
    """
    if df is None or df.empty or len(df) <= 3:
        return df

    real_cols = [
        str(c).strip().lower() for c in df.columns
        if str(c).strip()
        and not str(c).strip().isdigit()
        and not str(c).startswith(('Unnamed', 'Col_', 'col', 'unnamed'))
        and str(c).strip().lower() not in ('nan', 'none', 'null')
    ]
    if not real_cols:
        return df

    rows_to_drop = []
    for r_idx in range(1, len(df)):
        row_vals = [str(v).strip().lower() for v in df.iloc[r_idx] if pd.notna(v) and str(v).strip() and str(v).strip().lower() != 'nan']
        if not row_vals:
            continue
        # 1. 优先检查是否为 "续表" / "(continued)" 跨页注记行
        if len(row_vals) <= 2 and any(re.search(r'^(?:续表|接上表|\(contd|\(continued|continued table)', v) for v in row_vals):
            rows_to_drop.append(r_idx)
            continue
        # 2. 检查是否为跨页重复打印的表头行 (列名匹配率 >= 40%)
        # 真实表头应由非纯数据文本构成，严禁把包含合法数据值的行误删为重复表头
        if len(row_vals) >= 2:
            non_data_vals = [v for v in row_vals if not is_pure_data_value(v)]
            if non_data_vals:
                matches = sum(1 for v in non_data_vals if any(v == c or (len(v) >= 2 and v in c) for c in real_cols))
                if (matches / max(1, len(row_vals))) >= 0.40 and matches >= 2:
                    rows_to_drop.append(r_idx)
                    continue

    if rows_to_drop:
        print(f"[Postprocess] 自动剥离跨页重复表头/续表标记行 (共 {len(rows_to_drop)} 行: {rows_to_drop})")
        drop_set = set(rows_to_drop)
        keep_indices = [i for i in range(len(df)) if i not in drop_set]
        attrs = df.attrs.copy() if hasattr(df, 'attrs') else {}
        df = df.iloc[keep_indices].reset_index(drop=True)
        df.attrs = attrs

    return df

def _clean_vlm_dataframe(df: pd.DataFrame, original_attrs: dict) -> pd.DataFrame:
    """针对 PaddleOCR-VL-1.6 的零破坏直通管道 (Zero-Destructive Pass-Through)。"""
    # 1. 规范化表头字符（保留 MultiIndex 展开的真实列名）
    df.columns = [clean_latex_and_ocr(clean_text(str(c))) if c is not None else f"Col_{i+1}" for i, c in enumerate(df.columns)]
    
    # 确保列名唯一性
    if not df.columns.is_unique:
        seen = {}
        unique_cols = []
        for col in df.columns:
            c_s = str(col)
            if c_s in seen:
                seen[c_s] += 1
                unique_cols.append(f"{c_s}_{seen[c_s]}")
            else:
                seen[c_s] = 0
                unique_cols.append(c_s)
        df.columns = unique_cols

    # 2. 安全单元格清洗与地学符号无损规整（不拆分文本）
    df = df_map(df, lambda x: clean_latex_and_ocr(clean_text(str(x))) if pd.notna(x) and str(x).strip() not in ('nan', 'None') else "")

    # 3. 剥离跨页拼接中可能残留的印刷重复表头行
    df = strip_embedded_duplicate_headers(df)

    # 4. 去除完全空白行与列
    df = df.dropna(how='all', axis=0).dropna(how='all', axis=1)

    # 4b. olmOCR 2 学术尾注与注释分离
    df = separate_table_footnotes(df)

    # 5. 数值类型安全推断（使数值列在 Excel 中呈现为可计算的真实数值，同时保护文本标签与前导零编号）
    for col in df.columns:
        vals = df[col].tolist()
        non_empty = [v for v in vals if pd.notna(v) and str(v).strip() != ""]
        if not non_empty:
            continue

        def _can_be_numeric(v):
            if isinstance(v, bool):
                return False
            if isinstance(v, (int, float)):
                return True
            s = str(v).replace(',', '').strip()
            # 保护带前导零的代码/样号（如 001, 08, -001）避免失真
            if re.match(r'^[+-]?0\d+$', s):
                return False
            try:
                float(s)
                return True
            except (ValueError, TypeError):
                return False

        num_count = sum(1 for v in non_empty if _can_be_numeric(v))
        # 若非空单元格大部分为数值（例如 >= 50%），安全推断为数值类型
        if num_count / len(non_empty) >= 0.5:
            df[col] = df[col].apply(lambda x: try_numeric(x) if _can_be_numeric(x) else x)

    # 6. 转义 Excel 公式
    df.columns = [escape_formula(c) for c in df.columns]
    df = df_map(df, escape_formula)

    if hasattr(df, 'attrs'):
        current_fn = df.attrs.get('footnotes', [])
        df.attrs.update(original_attrs)
        if current_fn:
            existing_fn = df.attrs.get('footnotes', [])
            if not existing_fn:
                df.attrs['footnotes'] = current_fn
            elif isinstance(existing_fn, list) and current_fn != existing_fn:
                df.attrs['footnotes'] = list(dict.fromkeys(existing_fn + current_fn))
    return df


def smart_flatten_headers(
    header_rows: List[List[str]],
    delimiter: str = " > ",
    unit_brackets: bool = True
) -> List[str]:
    """
    SMART (Semantic Header Flattening) 启发式多级表头语义展平算法。
    参考: SMART: Semantic Header Flattening and Pseudo-Code-Style Reasoning for LLM-based Complex Table QA
    
    核心机制:
    1. 跨列前向填充 (Colspan Forward-Fill): 顶级表头跨多列时自动延展覆盖其辖下子列;
    2. 语义层级保真拼接 (Hierarchical Lineage): 使用语义层级符号 (' > ' 或 '/') 拼接路径;
    3. 语义冗余消解 (Deduplication): 当子列名已包含父列核心词时自动消解冗余前缀;
    4. 科学单位/计量括号保护: 将尾部单位规范化绑定至叶子节点 (如 '[wt%]' 或 '(g/t)')。
    """
    if not header_rows:
        return []
    if len(header_rows) == 1:
        return [str(c).strip() if str(c).strip() and str(c).lower() != 'nan' else f"Col_{i+1}" for i, c in enumerate(header_rows[0])]

    num_cols = max(len(r) for r in header_rows)
    padded_rows = []
    for r in header_rows:
        row_str = [str(c).strip() if c is not None and str(c).strip().lower() != 'nan' else '' for c in r]
        if len(row_str) < num_cols:
            row_str.extend([''] * (num_cols - len(row_str)))
        padded_rows.append(row_str[:num_cols])

    # 1. 跨列前向填充 (Colspan Forward-Fill，严格受限于上层父分类边界)
    filled_rows = []
    for r_idx, r in enumerate(padded_rows):
        if r_idx < len(padded_rows) - 1:
            filled = []
            curr_parent = ""
            for c_idx, c in enumerate(r):
                # 若存在上层表头，检查当前列的上层父分类是否发生边界切换
                if r_idx > 0 and c_idx > 0:
                    upper_prev = filled_rows[r_idx - 1][c_idx - 1]
                    upper_curr = filled_rows[r_idx - 1][c_idx]
                    if upper_prev != upper_curr:
                        curr_parent = ""
                if c:
                    curr_parent = c
                filled.append(curr_parent if curr_parent else c)
            filled_rows.append(filled)
        else:
            filled_rows.append(r)

    # 2. 逐列语义层级拼接与冗余消解
    flattened_cols = []
    for c_idx in range(num_cols):
        col_hierarchy = []
        for r in filled_rows:
            val = r[c_idx]
            if not val or val.lower() == 'nan':
                continue
            val_clean = clean_latex_and_ocr(clean_text(val)).strip()
            if not val_clean or val_clean.lower() == 'nan':
                continue
            if col_hierarchy and col_hierarchy[-1].lower() == val_clean.lower():
                continue
            col_hierarchy.append(val_clean)

        if not col_hierarchy:
            flattened_cols.append(f"Col_{c_idx+1}")
            continue

        if len(col_hierarchy) == 1:
            flattened_cols.append(col_hierarchy[0])
            continue

        # 3. 语义冗余消解 (例如 父="SiO2 (wt%)", 子="SiO2" -> "SiO2 (wt%)")
        deduped = []
        for level in col_hierarchy:
            if not deduped:
                deduped.append(level)
            else:
                prev = deduped[-1]
                if prev.lower() in level.lower() and len(level) > len(prev):
                    deduped[-1] = level
                elif level.lower() in prev.lower():
                    pass
                else:
                    deduped.append(level)

        joined_name = delimiter.join(deduped)
        flattened_cols.append(joined_name)

    return flattened_cols


def _detect_and_promote_header(df: pd.DataFrame) -> pd.DataFrame:
    """启发式探测并重塑表头，消除正文污染与多层子表头粘连。"""
    first_col_str = str(df.columns[0]).strip()
    unnamed_count = sum(1 for c in df.columns if str(c).startswith('Unnamed:') or str(c) in ('nan', 'None', '', '_1', '_2', '_3') or str(c).isdigit())
    
    is_running_header = (
        first_col_str.isdigit() or 
        re.match(r'^(?:19|20)\d{2}$', first_col_str) or
        any(kw in first_col_str.lower() for kw in ['journal', 'reviews', 'geol', 'mineral', 'vol.', 'pp.', 'page', 'et al', 'sciencedirect', 'springer'])
    )
    is_default_cols = all(isinstance(c, int) or (isinstance(c, str) and (c.isdigit() or c.startswith('Col') or c.startswith('Unnamed'))) for c in df.columns)
    is_corrupted_header = is_running_header or (unnamed_count >= max(2, len(df.columns) * 0.4)) or is_default_cols

    scientific_kws = [
        'sample', 'mineral', 'location', 'stage', 'type', 'element',
        'composition', 'fluid', 'temperature', 'melting', 'density',
        'th', 'tm', 'formula', 'paragenesis', 'gangue', 'ore',
        'isotope', 'delta', 'δ', 'age', 'date', 'ppm', 'ppb', 'wt', 'wt%', 'at%',
        'sb', 'au', 'w', 'cu', 'pb', 'zn', 'as', 'fe', 's', 'sio2', 'tio2', 'al2o3',
        '样品', '矿物', '阶段', '流体', '同位素', '年龄', '地层',
        '温阶', '代号', '岩性', '含量', '品位', '深度', '标高',
        'w_b', '10^', '×10', 'icp', 'la-icp', 'epma', '点号', '测点',
        '矿床类型', '矿床名称', '规模', '大地构造', '控矿构造', '赋矿层位', '矿体形态', '蚀变特征', '成矿年龄', '数据来源'
    ]

    # 检查第一列列名是否为纯浮点数值/纯数据误当表头（如 33.42, 0.025）
    is_numeric_col_name = bool(re.match(r'^-?\d+(?:\.\d+)?$', first_col_str))
    if is_numeric_col_name and not is_corrupted_header:
        # 首行是纯数值数据，恢复为数据行，表头设为默认占位符待下方促进
        df = pd.DataFrame([list(df.columns)] + df.values.tolist())
        is_corrupted_header = True

    if is_corrupted_header and len(df) > 0:
        best_header_idx = -1
        # 扫描前 80 行寻找真实科学数据表头起始行，彻底剔除上方误卷入的正文段落
        for idx in range(min(80, len(df))):
            row_cells = [str(val).strip() for val in df.iloc[idx]]
            row_str = ' '.join(row_cells).lower()
            non_num_cells = sum(1 for c in row_cells if len(c) > 0 and not c.replace('.', '').replace('-', '').replace(',', '').replace('%', '').replace('+', '').isdigit())
            # 排除纯页眉行和纯正文陈述句行
            if any(kw in row_str for kw in ['reviews', 'sciencedirect', 'vol.', 'pp.', 'page ']):
                continue
            if '。' in row_str or '以期建立' in row_str or '进而研究' in row_str:
                continue
            if any(kw in row_str for kw in scientific_kws):
                best_header_idx = idx
                break
            if idx < 5 and non_num_cells >= len(row_cells) * 0.4:
                best_header_idx = idx
                break

        if best_header_idx != -1:
            new_headers = [str(x).strip() for x in df.iloc[best_header_idx]]
            header_consumed = 1
            # 检查是否有第二层或更多层子表头/单位行 (最高支持 3 层)
            header_layers = [new_headers]
            for extra_offset in range(1, 3):
                if best_header_idx + extra_offset < len(df):
                    sub_cells = [str(val).strip() for val in df.iloc[best_header_idx + extra_offset]]
                    sub_non_num = sum(1 for c in sub_cells if len(c) > 0 and not c.replace('.', '').replace('-', '').replace(',', '').replace('%', '').replace('+', '').replace('~', '').isdigit())
                    max_sub_len = max((len(c) for c in sub_cells), default=0)
                    is_all_units = all(re.match(r'^\(.*\)$', c) or c in ('%', 'wt%', 'wt.%', 'ppm', 'ppb', 'g/t', 'MPa', '°C', '‰') for c in sub_cells if c)
                    if (sub_non_num >= len(sub_cells) * 0.4 and max_sub_len <= 30) or is_all_units:
                        header_layers.append(sub_cells)
                        header_consumed += 1
                    else:
                        break
            if len(header_layers) > 1:
                new_headers = smart_flatten_headers(header_layers)

            df.columns = [h if h and h != 'nan' else f"Col_{i+1}" for i, h in enumerate(new_headers)]
            df = df.iloc[best_header_idx + header_consumed:].reset_index(drop=True)

    return df


def _filter_noise_and_placeholders(df: pd.DataFrame) -> pd.DataFrame:
    """清理空列、无有效数据的占位列以及嵌入的跨页噪声行。"""
    # Drop all-NaN columns and trailing empty _nan columns
    df = df.dropna(how='all', axis=1)
    df = df.loc[:, ~df.columns.astype(str).str.lower().str.endswith('_nan')]
    df = df.loc[:, ~df.columns.astype(str).str.lower().str.endswith('_none')]
    
    # Drop any 'Unnamed', 'nan', integer, or placeholder column that has no real data
    cols_to_drop = []
    # 确保在处理前使用 object 类型，防止写入文本时触发 float64 类型异常
    df = df.astype(object)
    for c_idx, col in enumerate(df.columns):
        col_str = str(col).strip().lower()
        is_placeholder_col = (
            col_str in ['', 'nan', 'none', 'null', 'unnamed'] or
            col_str.isdigit() or
            bool(re.match(r'^(?:unnamed(?:[\s_:]*\d+)?|col(?:umn)?(?:[\s_:]*\d+)?)$', col_str))
        )
        if is_placeholder_col:
            valid_vals = [str(x).strip() for x in df[col] if pd.notnull(x) and str(x).strip() not in ['', 'nan', 'none', 'null']]
            # 严格保障数据完整性：仅当占位列完全不含任何有效数据 (len(valid_vals) == 0) 时才安全移除；
            # 绝不把低密度测量列误判为冗余，更不能将其合并到相邻列
            if len(valid_vals) == 0 and (len(df.columns) - len(cols_to_drop) > 1):
                cols_to_drop.append(col)
    if cols_to_drop:
        df = df.drop(columns=cols_to_drop)

    # 识别并过滤噪声行与列索引行（严格依据位置、序列与表头证据）
    rows_to_drop = set()
    prev_was_repeated_header = False
    col_names_lower = [str(c).strip().lower() for c in df.columns]
    real_col_names = {
        c for c in col_names_lower
        if not c.isdigit() and c not in ['', 'nan', 'none', 'null', 'unnamed']
        and not re.match(r'^(?:unnamed|col(?:umn)?)(?:[\s_:]*\d+)?$', c)
    }

    for r_idx in range(len(df)):
        row = df.iloc[r_idx]
        cells = [str(x).strip() for x in row.values if pd.notnull(x) and str(x).strip() != '']
        if not cells:
            continue
        row_text = " ".join(cells)

        # 1. 独立续表标题横幅行 (e.g. "Table 2 (continued)")
        if re.search(r'^\s*(?:Table|表)\s*\d+(?:\.\d+)?.*|\b(?:continued|cont\'d|contd)\b|\bTable\s*\(cont', row_text, re.IGNORECASE):
            num_cells = len(cells)
            non_text_cells = sum(1 for c in cells if re.search(r'\d', c))
            if num_cells <= 2 or non_text_cells == 0:
                rows_to_drop.add(r_idx)
                continue

        # 2. 识别跨页合并后残留的重复表头行 (仅针对真实文本表头，严禁对数字或占位列名匹配)
        matching_header_cells = sum(1 for c in cells if c.lower() in real_col_names) if real_col_names else 0
        is_repeat_header = len(cells) >= 3 and bool(real_col_names) and (matching_header_cells / len(cells) >= 0.4)
        if is_repeat_header and r_idx > 0:
            rows_to_drop.add(r_idx)
            prev_was_repeated_header = True
            continue

        # 3. 识别列索引行 (必须具有明确的显式语法证据如 (1), (2) 或 [1], [2]，绝不凭单纯裸整数误删合法数据)
        is_valid_position = (r_idx == 0) or prev_was_repeated_header
        if is_valid_position and len(cells) >= 3 and len(cells) == len(df.columns):
            raw_ints = []
            has_explicit_syntax = False
            for c in cells:
                m_idx = re.match(r'^(?:[\(\[]\s*(\d+)\s*[\)\]]|(\d+)(?:\.0+)?)$', c)
                if m_idx:
                    raw_ints.append(int(m_idx.group(1) or m_idx.group(2)))
                    if m_idx.group(1) is not None:
                        has_explicit_syntax = True
                else:
                    break
            if len(raw_ints) == len(cells):
                is_seq = (raw_ints == list(range(len(cells))) or raw_ints == list(range(1, len(cells) + 1)))
                if is_seq and has_explicit_syntax:
                    rows_to_drop.add(r_idx)
                    prev_was_repeated_header = False
                    continue

        prev_was_repeated_header = False

    if rows_to_drop:
        df = df.drop(index=df.index[list(rows_to_drop)]).reset_index(drop=True)
    df = separate_table_footnotes(df)
    return df



def is_paywall_table(df) -> bool:
    """
    检查表格是否为付费墙、登录拦截或Cookie弹窗提示。
    采用严格词边界匹配与强/弱关键词共现策略，彻底避免 parent/different/register of samples 误杀。
    """
    if df is None or df.empty or len(df) > 4:
        return False
    cells_text = ' '.join(str(val) for val in df.values.flatten() if val is not None)
    strong_paywall_patterns = [
        r'\binstitutional\s+login\b',
        r'\bpurchase\s+access\b',
        r'\baccept\s+cookies\b',
        r'\bcookie\s+policy\b',
        r'\bsubscription\s+required\b',
        r'\bsign\s+in\s+to\s+(?:view|access|read)\b',
        r'\baccess\s+through\s+your\s+institution\b',
        r'\brent\s+this\s+article\b',
        r'\bpurchase\s+instant\s+access\b',
    ]
    weak_paywall_terms = [
        r'\blogin\b',
        r'\bsign\s+in\b',
        r'\bsubscription\b',
        r'\brent\b',
        r'\bsubscribe\b',
        r'\bpaywall\b',
        r'\bopenathens\b',
        r'\bshibboleth\b',
    ]
    has_strong = any(re.search(pat, cells_text, re.IGNORECASE) for pat in strong_paywall_patterns)
    weak_hits = sum(1 for pat in weak_paywall_terms if re.search(pat, cells_text, re.IGNORECASE))
    return bool(has_strong or weak_hits >= 2)


def postprocess_dataframe(df, headers=None):
    """Clean up and format the DataFrame safely and losslessly."""
    if df is None:
        return df

    # 领域模型向下解耦：若传入 ExtractedTable 统一对象，则处理其 underlying df 并同步属性
    if hasattr(df, 'df') and isinstance(df.df, pd.DataFrame):
        df.df = postprocess_dataframe(df.df, headers=headers)
        if hasattr(df, 'sync_attrs'):
            df.sync_attrs()
        return df

    if df.empty:
        return df
        
    original_attrs = df.attrs.copy() if hasattr(df, 'attrs') else {}
    is_vlm = (original_attrs.get('extractor') == 'paddleocr_vl' or original_attrs.get('source') == 'paddleocr_vl')

    # 1. 付费墙拦截判定（严格词边界匹配与多关键词共现，彻底避免 parent/different/register of samples 误杀）
    if is_paywall_table(df) or original_attrs.get('rejected_reason') == 'paywall':
        print("Warning: Detected paywall or login warning table. Isolating and rejecting table.")
        res_df = pd.DataFrame()
        res_df.attrs = original_attrs.copy()
        res_df.attrs['rejected_reason'] = 'paywall'
        res_df.attrs['rejected_raw_df'] = df.copy()
        return res_df

    df = df.copy().reset_index(drop=True)

    # 2. 针对 PaddleOCR-VL-1.6 的零破坏直通管道
    if is_vlm:
        return _clean_vlm_dataframe(df, original_attrs)

    # 3. 碎片化表检测：列数 >30 且填充率 <30% -> 丢弃
    if df.shape[1] > 30:
        total_cells = df.shape[0] * df.shape[1]
        non_empty = sum(1 for v in df.values.flatten() if v is not None and str(v).strip())
        fill_ratio = non_empty / total_cells if total_cells > 0 else 0
        if fill_ratio < 0.30:
            print(f"[Postprocess] 丢弃碎片化表: {df.shape[0]}r×{df.shape[1]}c, fill={fill_ratio:.1%}")
            return pd.DataFrame()

    # 4. 原生规则 / 本地兜底路径的启发式清洗流水线
    # 步骤 A: 表头探测与层级重组
    df = _detect_and_promote_header(df)

    # 步骤 B: 表头覆盖与字符清洗
    if headers:
        if len(headers) < len(df.columns):
            headers = headers + [f"Col{i}" for i in range(len(headers), len(df.columns))]
        else:
            headers = headers[:len(df.columns)]
        df.columns = headers
        
    df.columns = [clean_latex_and_ocr(clean_text(c)) for c in df.columns]
    df = df_map(df, lambda x: clean_latex_and_ocr(clean_text(x)) if isinstance(x, str) else x)

    # 保证列名唯一
    if not df.columns.is_unique:
        seen = {}
        unique_cols = []
        for col in df.columns:
            col_str = str(col) if col is not None else "Unnamed"
            if col_str in seen:
                seen[col_str] += 1
                unique_cols.append(f"{col_str}_{seen[col_str]}")
            else:
                seen[col_str] = 0
                unique_cols.append(col_str)
        df.columns = unique_cols

    # 步骤 C: 行列展开（紧缩列、多行子行、章节分类）
    df = expand_squeezed_columns(df)
    df = expand_multiline_subrows(df)
    df = populate_section_categories(df)

    # 步骤 C2: olmOCR 2 启发式学术表格边界与尾注分离
    df = separate_table_footnotes(df)

    # 步骤 D: 噪声过滤与占位列清理
    df = _filter_noise_and_placeholders(df)


    # 步骤 E: 数值类型推断与公式转义
    df = df_map(df, try_numeric)
        
    df = strip_embedded_duplicate_headers(df)

    df.columns = [escape_formula(c) for c in df.columns]
    df = df_map(df, escape_formula)
        
    # 步骤 F: 智能体/大模型语义修复（当启用 LLM 且表头/结构异常时）
    if os.getenv("LLM_TABLE_REASONER_ENABLED") or original_attrs.get('enable_llm'):
        try:
            from llm_reasoner import get_llm_reasoner
            reasoner = get_llm_reasoner()
            if reasoner.is_available():
                unnamed_cols = sum(1 for c in df.columns if str(c).startswith("Unnamed") or not str(c).strip())
                if unnamed_cols > len(df.columns) * 0.4:
                    repaired = reasoner.repair_defective_table(df)
                    if repaired is not None and not repaired.empty:
                        df = repaired
        except Exception:
            pass

    if hasattr(df, 'attrs'):
        df.attrs.update(original_attrs)
        
    return df


def is_merged_row_table(table):
    """
    Checks if a table extracted via line-based strategy has merged rows.
    This happens when a table lacks horizontal grid lines, causing the extraction 
    to group multiple lines into a single row separated by newlines.
    """
    try:
        rows = table.extract()
        if not rows or len(rows) <= 1:
            return False
        # If the table has very few rows (e.g. <= 3) but cells contain newlines
        if len(rows) <= 3:
            newline_cells = 0
            total_cells = 0
            for r in rows:
                for c in r:
                    if c:
                        total_cells += 1
                        if '\n' in str(c):
                            newline_cells += 1
            if total_cells > 0 and (newline_cells / total_cells) > 0.3:
                return True
    except Exception:
        pass
    return False


def _normalize_col_name(name: str) -> str:
    """学术表头规范化：消除上下标、单位括号、不确定度格式差异与标点空格。"""
    s = str(name).lower().strip()
    # 替换下标与上标数字为普通数字 (如 SiO₂ -> sio2)
    sub_map = str.maketrans("₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹", "01234567890123456789")
    s = s.translate(sub_map)
    # 规整不确定度与误差表示
    s = re.sub(r'1\s*(?:σ|sigma|s\b)', '1sigma', s)
    s = re.sub(r'2\s*(?:σ|sigma|s\b)', '2sigma', s)
    s = s.replace('±', '+/-')
    # 去除括号单位如 (wt%), [ppm], (ppm), (%), (ma)
    s = re.sub(r'[\(\[\{](?:wt\.?%?|ppm|ppb|%|‰|ma|ga|ka|℃|°c)[\)\]\}]', '', s)
    # 去除标点空格
    s = re.sub(r'[\s_\(\)（）\[\]\{\}\.\/\:\-]+', '', s)
    return s


def compute_column_profile(series: pd.Series) -> Dict[str, float]:
    """
    计算单列数据的语义与数据类型特征分布向量（借鉴 DeepDoc 与 MinerU 版面投影特征）。
    包含：数值比例、空值比例、平均文本长度。
    """
    total = len(series)
    if total == 0:
        return {'num_ratio': 0.0, 'empty_ratio': 1.0, 'avg_len': 0.0}
    vals = [str(x).strip() for x in series if pd.notna(x)]
    non_empty = [v for v in vals if v]
    num_cnt = sum(1 for v in non_empty if is_pure_data_value(v))
    empty_cnt = total - len(non_empty)
    avg_l = sum(len(v) for v in non_empty) / max(1, len(non_empty))
    return {
        'num_ratio': num_cnt / max(1, len(non_empty)),
        'empty_ratio': empty_cnt / total,
        'avg_len': avg_l,
    }


def deepdoc_mineru_align_columns(
    df_to_align: pd.DataFrame,
    target_cols: List[str],
    base_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    基于 DeepDoc / MinerU 版面与列投影机制的多页续表自适应列对齐。
    
    核心思想：
    1. 列名语义对齐：学术规范化、缩写匹配与模糊相似度；
    2. 列投影分布对齐 (Column Projection Profile)：当续表缺失表头或为 generic 列名时，
       通过数值比例、文本长度与稀疏度分布投影，计算两表列特征相似度；
    3. 单调动态规划保序投影 (Monotone DP Assignment)：跨页表格严格维持从左至右的物理与语义列序，
       杜绝列穿插与乱序投影，并自动处理列省略、列拆分与补齐；
    4. 空间与相对位置约束：融合相对水平投影中心惩罚，确保局部列与全局列对齐。
    """
    if df_to_align is None or df_to_align.empty:
        return pd.DataFrame(columns=target_cols)

    curr_cols = list(df_to_align.columns)
    if curr_cols == target_cols and base_df is None:
        return df_to_align

    num_rows = len(df_to_align)
    n_curr = len(curr_cols)
    n_target = len(target_cols)

    if n_curr == 0 or n_target == 0:
        return pd.DataFrame(columns=target_cols)

    # 1. 预计算列特征分布向量
    curr_profiles = [compute_column_profile(df_to_align.iloc[:, i]) for i in range(n_curr)]
    base_profiles = []
    if base_df is not None and not base_df.empty:
        base_profiles = [compute_column_profile(base_df.iloc[:, min(j, len(base_df.columns)-1)]) for j in range(n_target)]
    else:
        base_profiles = [{'num_ratio': 0.8 if any(c.isdigit() or c in '±%' for c in str(tc)) else 0.2, 'empty_ratio': 0.05, 'avg_len': 5.0} for tc in target_cols]

    # 2. 构建二维相似度矩阵 S[i][j] (curr_cols[i] -> target_cols[j])
    score_matrix = [[0.0 for _ in range(n_target)] for _ in range(n_curr)]

    for i, c_col in enumerate(curr_cols):
        c_clean = _normalize_col_name(c_col)
        c_is_generic = not c_clean or c_clean.startswith(('unnamed', 'col')) or str(c_col).isdigit() or c_clean in ('none', 'nan', 'null')
        c_prof = curr_profiles[i]
        c_rel_pos = i / max(1, n_curr - 1)

        for j, t_col in enumerate(target_cols):
            t_clean = _normalize_col_name(t_col)
            t_is_generic = not t_clean or t_clean.startswith(('unnamed', 'col')) or str(t_col).isdigit() or t_clean in ('none', 'nan', 'null')
            t_prof = base_profiles[j]
            t_rel_pos = j / max(1, n_target - 1)

            # (1) 列名相似度
            name_sim = 0.0
            if not c_is_generic and not t_is_generic:
                if c_clean == t_clean:
                    name_sim = 1.0
                elif c_clean in t_clean or t_clean in c_clean:
                    name_sim = min(len(c_clean), len(t_clean)) / max(len(c_clean), len(t_clean))
                else:
                    from difflib import SequenceMatcher
                    name_sim = SequenceMatcher(None, c_clean, t_clean).ratio()

            # (2) 数据特征投影分布相似度 (DeepDoc / MinerU)
            num_diff = abs(c_prof['num_ratio'] - t_prof['num_ratio'])
            empty_diff = abs(c_prof['empty_ratio'] - t_prof['empty_ratio'])
            len_diff = min(1.0, abs(c_prof['avg_len'] - t_prof['avg_len']) / 15.0)
            prof_sim = max(0.0, 1.0 - (0.50 * num_diff + 0.30 * empty_diff + 0.20 * len_diff))

            # (3) 物理位置相对跨度单调惩罚
            pos_penalty = abs(c_rel_pos - t_rel_pos)

            # 综合加权
            if not c_is_generic and not t_is_generic:
                score = 0.50 * name_sim + 0.35 * prof_sim + 0.15 * max(0.0, 1.0 - pos_penalty)
            else:
                score = 0.70 * prof_sim + 0.30 * max(0.0, 1.0 - pos_penalty)

            score_matrix[i][j] = score

    # 3. 单调动态规划保序对齐 (Monotone Dynamic Programming Alignment)
    # 支持当前表列跳过 (n_curr > n_target)、目标列省略 (n_curr < n_target) 以及双向单调匹配
    dp = [[0.0 for _ in range(n_target + 1)] for _ in range(n_curr + 1)]
    parent = [[None for _ in range(n_target + 1)] for _ in range(n_curr + 1)]

    for i in range(1, n_curr + 1):
        for j in range(1, n_target + 1):
            # 选项 1: 跳过当前续表列 curr[i-1] (当前列可能是多余/噪声列)
            best_val = dp[i - 1][j]
            best_p = (i - 1, j, None)

            # 选项 2: 目标列 target[j-1] 在续表中未出现 (续表省略了该列)
            if dp[i][j - 1] > best_val:
                best_val = dp[i][j - 1]
                best_p = (i, j - 1, None)

            # 选项 3: 将 curr[i-1] 与 target[j-1] 匹配
            match_score = score_matrix[i - 1][j - 1]
            if dp[i - 1][j - 1] + match_score > best_val:
                best_val = dp[i - 1][j - 1] + match_score
                best_p = (i - 1, j - 1, (i - 1, j - 1))

            dp[i][j] = best_val
            parent[i][j] = best_p

    # 回溯最优单调对齐路径
    curr_to_target = {}
    target_to_curr = {}
    ci, cj = n_curr, n_target
    while ci > 0 and cj > 0:
        p = parent[ci][cj]
        if not p:
            break
        pi, pj, action = p
        if action:
            curr_idx, target_idx = action
            if score_matrix[curr_idx][target_idx] >= 0.20 or n_curr == n_target:
                curr_to_target[curr_idx] = target_idx
                target_to_curr[target_idx] = curr_idx
        ci, cj = pi, pj

    # 4. 根据单调映射顺序构造对齐列 Series
    aligned_series = []
    for j, t_col in enumerate(target_cols):
        if j in target_to_curr:
            src_idx = target_to_curr[j]
            s = df_to_align.iloc[:, src_idx].reset_index(drop=True)
            s.name = t_col
            aligned_series.append(s)
        else:
            aligned_series.append(pd.Series([""] * num_rows, name=t_col))

    res_df = pd.concat(aligned_series, axis=1)
    res_df.columns = target_cols
    res_df.attrs = getattr(df_to_align, 'attrs', {}).copy()
    return res_df


def align_dataframe_columns(
    df_to_align: pd.DataFrame,
    target_cols: List[str],
    base_df: Optional[pd.DataFrame] = None
) -> pd.DataFrame:
    """
    基于 DeepDoc / MinerU 列投影机制，将 df_to_align 对齐至目标列。
    兼容原有签名，支持可选的 base_df 进行数据分布投影。
    """
    return deepdoc_mineru_align_columns(df_to_align, target_cols, base_df=base_df)


def is_pure_data_value(s_in):
    """
    判断字符串是否为纯数据值（包括标准数值、科学计数法、检出限缩写与误差表示）。
    """
    s = str(s_in).strip()
    if not s:
        return False
    # 常见地质化学式/同位素/单位/字段词，属于表头文本，非纯数据
    if (re.match(r'^(?:[A-Z][a-z]?\d*)+$', s) or '/' in s or 'δ' in s or '‰' in s or 
        '%' in s or any(w in s.lower() for w in ['ppm', 'ppb', 'wt', 'sample', 'spot', 'no.', 'mineral', 'rock', 'age', 'stage', 'type', 'depth'])):
        return False
    s_lower = s.lower().strip()
    # 1. 常见缺失值与检出限缩写 (<, >, b.d., n.d., etc.)
    if s_lower in ('-', '—', '–', 'n.d.', 'nd', 'bdl', 'b.d.l.', 'b.d.', 'n/a', 'na', 'n.a.', 'tr', 'trace', 'loq', 'lod', 'dl', 'nil', 'none', 'below detection'):
        return True
    # 2. 误差与不确定度符号缩写 (±, 1σ, 2σ, etc.)
    if s_lower in ('1σ', '2σ', '1s', '2s', '±', '1sigma', '2sigma', '±1σ', '±2σ'):
        return True
    # 3. 标准整数、浮点数、科学计数法 (如 123, -4.56, 1.2e-4)
    if re.match(r'^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$', s):
        return True
    # 4. 带检出限/不等号符号的数值 (如 <0.01, >100, ≤0.05, ≥99, ~0.5)
    if re.match(r'^[<>≤≥~]\s*-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$', s):
        return True
    # 5. 带误差与不确定度的数值 (如 12.34±0.56, 12.34 ± 0.56, 12.34(56), 12.3+0.5/-0.2, 12.3(1σ))
    if re.match(r'^-?\d+(?:\.\d+)?\s*±\s*\d+(?:\.\d+)?$', s):
        return True
    if re.match(r'^-?\d+(?:\.\d+)?\s*\(\d+\)$', s):
        return True
    if re.match(r'^-?\d+(?:\.\d+)?\s*[+-]\s*\d+(?:\.\d+)?\s*/\s*[+-]\s*\d+(?:\.\d+)?$', s):
        return True
    if re.match(r'^-?\d+(?:\.\d+)?\s*(?:±\s*\d+(?:\.\d+)?)?\s*(?:1σ|2σ|1s|2s|1sigma|2sigma)$', s, re.IGNORECASE):
        return True
    return False


def merge_cross_page_split_cells(prev_df: pd.DataFrame, next_df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    MinerU 跨页断裂单元格合并检测与拼接 (Cross-Page Broken Cell / Split Row Merge)。
    当跨页换行导致最后一行数据被切为两半时（例如：上一页末行文本未完或仅有前几列，下一页首行无键名且仅为续接描述），
    将拆分的单元格内容无损合并至上一页末行，并剔除下一页冗余残行。
    """
    if prev_df.empty or next_df.empty or len(prev_df) < 1 or len(next_df) < 1:
        return prev_df, next_df

    last_row = [str(x).strip() if pd.notna(x) else "" for x in prev_df.iloc[-1].values]
    first_row = [str(x).strip() if pd.notna(x) else "" for x in next_df.iloc[0].values]

    # 判定特征 1: 下表首行第一列为空白/无新键名，而后序列存在内容
    col0_empty_in_next = not first_row[0] or first_row[0].lower() in ('nan', 'none', '-', '—')
    
    # 若下表首列具有独立的键名/样本名（且上表末行首列未以连字符截断），则绝对是完整独立数据行，不得误并
    if not col0_empty_in_next:
        if not (last_row[0].endswith(('-', '—')) and first_row[0] and first_row[0][0].islower()):
            return prev_df, next_df

    # 判定特征 2: 上表末行文本以连字符 '-' 或逗号等未完标点结尾，或下表首行文本以小写字母/连续词开头
    text_split = False
    for c_i in range(min(len(last_row), len(first_row))):
        c_prev, c_next = last_row[c_i], first_row[c_i]
        if c_prev and c_next:
            if c_prev.endswith(('-', '—', ',', 'and', 'with', 'of', 'in', '±', '~', '(', '（')):
                text_split = True
                break
            if len(c_next) > 0 and c_next[0].islower() and c_next not in ('b.d.', 'n.d.', 'wt%', 'ppm', 'ppb'):
                text_split = True
                break

    # 判定特征 3: 上表末行部分列为空，下表首行相应列有填充（互补断行）
    complementary = False
    if col0_empty_in_next and any(first_row[1:]):
        complementary = True

    if text_split or (col0_empty_in_next and complementary):
        merged_last = list(last_row)
        for i in range(min(len(merged_last), len(first_row))):
            p_val = merged_last[i]
            n_val = first_row[i]
            if n_val:
                if p_val and p_val.endswith(('-', '—')):
                    merged_last[i] = p_val[:-1] + n_val
                elif p_val:
                    merged_last[i] = p_val + " " + n_val
                else:
                    merged_last[i] = n_val

        prev_df_copy = prev_df.copy()
        prev_df_copy.iloc[-1] = merged_last
        next_df_copy = next_df.iloc[1:].reset_index(drop=True)
        return prev_df_copy, next_df_copy

    return prev_df, next_df


def combine_df_group(group):
    if not group:
        return None
    if len(group) == 1:
        return group[0]
        
    base_df = group[0]
    base_cols = list(base_df.columns)

    # Detect horizontal column-split tables (e.g. Table 4 Part 1 on Page 17 & Part 2 on Page 18)
    # 必须满足：两表均为具有独立表头文本的横向分块，首列同名，且列名非纯数字或数据特征
    is_horizontal = False
    if len(group) >= 2:
        col1_list = [str(c).strip() for c in group[0].columns[1:]]
        col2_list = [str(c).strip() for c in group[1].columns[1:]]
        has_numeric_cols = sum(1 for c in col2_list if is_pure_data_value(c)) / max(1, len(col2_list)) >= 0.25
        has_named_cols_1 = sum(1 for c in group[0].columns if not str(c).startswith('Unnamed:') and str(c).strip()) >= 2
        has_named_cols_2 = sum(1 for c in group[1].columns if not str(c).startswith('Unnamed:') and str(c).strip()) >= 2
        if not has_numeric_cols and has_named_cols_1 and has_named_cols_2:
            first_c0 = _normalize_col_name(group[0].columns[0])
            first_c1 = _normalize_col_name(group[1].columns[0])
            first_col_compatible = (first_c0 == first_c1 or not first_c0 or not first_c1 or
                                    any(w in first_c0 and w in first_c1 for w in ['sample', 'spot', 'id', 'no', 'site', 'name', 'station']))
            col1_set = set(_normalize_col_name(c) for c in col1_list)
            col2_set = set(_normalize_col_name(c) for c in col2_list)
            row_counts = [len(g) for g in group if g is not None and not g.empty]
            max_rows = max(row_counts) if row_counts else 0
            min_rows = min(row_counts) if row_counts else 0
            rows_compatible = max_rows > 0 and min_rows / max_rows >= 0.5
            if (col1_set and col2_set and
                    len(col1_set & col2_set) / max(1, len(col1_set)) < 0.35 and
                    rows_compatible and first_col_compatible):
                is_horizontal = True
                
    if is_horizontal:
        merged = group[0].copy()
        first_col = merged.columns[0]
        merged[first_col] = merged[first_col].astype(str)
        
        c_series = merged[first_col].iloc[:, 0] if isinstance(merged[first_col], pd.DataFrame) else merged[first_col]
        use_merge = bool(c_series.is_unique)
        for next_df in group[1:]:
            if next_df is None or next_df.empty:
                continue
            next_renamed = next_df.rename(columns={next_df.columns[0]: first_col})
            next_renamed[first_col] = next_renamed[first_col].astype(str)
            n_series = next_renamed[first_col].iloc[:, 0] if isinstance(next_renamed[first_col], pd.DataFrame) else next_renamed[first_col]
            if use_merge and bool(n_series.is_unique):
                merged = pd.merge(merged, next_renamed, on=first_col, how='outer')
            else:
                use_merge = False
                merged = merged.reset_index(drop=True)
                next_renamed = next_renamed.reset_index(drop=True)
                max_rows = max(len(merged), len(next_renamed))
                if len(merged) < max_rows:
                    merged = merged.reindex(range(max_rows))
                next_aligned = next_renamed.reindex(range(max_rows))
                next_aligned = next_aligned.drop(columns=[first_col], errors='ignore')
                existing_cols = set(merged.columns)
                new_cols = []
                for c in next_aligned.columns:
                    c_str = str(c)
                    if c_str in existing_cols:
                        suffix_idx = 2
                        while f"{c_str}_part{suffix_idx}" in existing_cols or f"{c_str}_part{suffix_idx}" in new_cols:
                            suffix_idx += 1
                        new_cols.append(f"{c_str}_part{suffix_idx}")
                    else:
                        new_cols.append(c_str)
                next_aligned.columns = new_cols
                merged = pd.concat([merged, next_aligned], axis=1)
        merged.attrs = base_df.attrs.copy()
        return merged
        
    concat_rows = [base_df.reset_index(drop=True)]
    for next_df in group[1:]:
        if next_df is None or next_df.empty:
            continue
        next_df = next_df.reset_index(drop=True)
        
        # 保护 pandas 自动生成的 RangeIndex/纯数字序号列名，避免误判为第一行数据
        is_seq_int = (
            isinstance(next_df.columns, pd.RangeIndex) or
            list(next_df.columns) == list(range(len(next_df.columns))) or
            [str(c).strip() for c in next_df.columns] == [str(i) for i in range(len(next_df.columns))]
        )
        # 检查 next_df 的 columns 是否实质上是第一行数据（续表无表头时常见现象）
        next_cols = [str(c).strip() for c in next_df.columns]
        data_col_count = sum(1 for c in next_cols if is_pure_data_value(c))
        is_data_in_columns = (not is_seq_int) and (data_col_count / max(1, len(next_cols)) >= 0.4)
        
        if is_data_in_columns:
            # 将 next_df.columns 转回为数据首行，并基于 DeepDoc/MinerU 列投影对齐至 base_cols
            full_data = [list(next_df.columns)] + next_df.values.tolist()
            temp_df = pd.DataFrame(full_data, columns=[f"Col_{i}" for i in range(len(next_df.columns))])
            next_df = align_dataframe_columns(temp_df, base_cols, base_df=base_df)
        else:
            # 动态语义对齐：当列数不完全相同或列名略有变动时，执行基于相似度的自适应对齐
            has_named_cols = sum(1 for c in next_df.columns if not str(c).startswith('Unnamed:') and str(c).strip()) >= 2
            if list(next_df.columns) == list(base_cols):
                pass
            elif len(next_df.columns) == len(base_cols):
                if has_named_cols:
                    next_df = align_dataframe_columns(next_df, base_cols, base_df=base_df)
                else:
                    next_df.columns = base_cols
            elif abs(len(next_df.columns) - len(base_cols)) <= 6:
                if has_named_cols:
                    next_df = align_dataframe_columns(next_df, base_cols, base_df=base_df)
                else:
                    first_row = [str(x).strip() for x in next_df.iloc[0].values if pd.notna(x)] if len(next_df) > 0 else []
                    row_has_named = sum(1 for c in first_row if c and not c.startswith('Unnamed:') and any(_normalize_col_name(c) == _normalize_col_name(b) for b in base_cols)) >= 2
                    if row_has_named:
                        next_df.columns = [str(x).strip() if pd.notna(x) and str(x).strip() else f"Col_{i}" for i, x in enumerate(next_df.iloc[0].values)]
                        next_df = next_df.iloc[1:].reset_index(drop=True)
                        next_df = align_dataframe_columns(next_df, base_cols, base_df=base_df)
                    else:
                        # 运用 DeepDoc / MinerU 列投影自适应映射不同列数分块
                        next_df = align_dataframe_columns(next_df, base_cols, base_df=base_df)
            else:
                # 列数差异过大，跳过合并
                continue

            # 逐行剥离 next_df 顶部的多层重复表头行、序号行、单位行与重复子表头/分节横幅行
            while len(next_df) > 0:
                first_row = [str(x).strip() for x in next_df.iloc[0].values if pd.notna(x)]
                row_str = " ".join(first_row).lower()
                raw_first_ints = []
                has_exp_idx = False
                for c in first_row:
                    m = re.match(r'^(?:[\(\[]\s*(\d+)\s*[\)\]]|(\d+)(?:\.0+)?)$', c)
                    if m:
                        raw_first_ints.append(int(m.group(1) or m.group(2)))
                        if m.group(1) is not None:
                            has_exp_idx = True
                    else:
                        break
                is_seq = len(raw_first_ints) == len(first_row) and (
                    raw_first_ints == list(range(len(first_row))) or
                    raw_first_ints == list(range(1, len(first_row) + 1))
                )
                first_row_is_data = sum(1 for c in first_row if is_pure_data_value(c)) / max(1, len(first_row)) >= 0.3
                is_subsequent_text = False
                if len(next_df) > 1:
                    for val in next_df.iloc[1].values:
                        if pd.notnull(val) and not is_pure_data_value(val) and len(str(val).strip()) > 1:
                            is_subsequent_text = True
                            break
                is_index_row = len(first_row) >= 3 and is_seq and (has_exp_idx or (is_subsequent_text and not first_row_is_data))
                is_repeat_header = (not first_row_is_data) and sum(1 for c in first_row if c and any(_normalize_col_name(c) == _normalize_col_name(b) for b in base_cols)) / max(1, len(base_cols)) >= 0.25
                is_unit_row = len(first_row) > 0 and all(any(u in c.lower() for u in ['wt%', 'ppm', 'ppb', '%', '‰', 'ma', 'ga', 'ka', '℃', '°c']) or c.startswith('(') for c in first_row if c)
                # 续表标题横幅行 (如 "Table 1 (continued)", "Table 1 Continued", "接上表", "Table 1 (cont'd)")
                is_cont_banner = bool(re.search(r'(?:table\s*\d+|表\s*\d+|续表|接上表|续上表|continued|cont\'?d|\(cont\b|cont\.)', row_str)) and len([c for c in first_row if c]) <= 3
                # 跨页重复子表头分类行 (如 "Major elements (wt%)", "Trace elements", "Rare earth elements" 等)
                is_category_banner = len([c for c in first_row if c]) <= 2 and any(kw in row_str for kw in ['major element', 'trace element', 'rare earth', 'ree', 'oxide', 'isotope', 'sample', 'host rock', 'mineral', '主量', '微量', '稀土', '同位素', '元素', '氧化物', '样品'])
                if is_index_row or is_repeat_header or is_unit_row or is_cont_banner or is_category_banner:
                    next_df = next_df.iloc[1:].reset_index(drop=True)
                else:
                    break
            
        if not next_df.empty:
            if concat_rows and len(concat_rows) > 0 and len(next_df) > 0:
                concat_rows[-1], next_df = merge_cross_page_split_cells(concat_rows[-1], next_df)
            if not next_df.empty:
                concat_rows.append(next_df)
        
    def make_df_columns_unique(df_in):
        seen_cols = {}
        new_col_list = []
        for col_name in df_in.columns:
            col_s = str(col_name) if col_name is not None else "Unnamed"
            if col_s in seen_cols:
                seen_cols[col_s] += 1
                new_col_list.append(f"{col_s}_{seen_cols[col_s]}")
            else:
                seen_cols[col_s] = 0
                new_col_list.append(col_s)
        df_in.columns = new_col_list
        return df_in

    for d in concat_rows:
        make_df_columns_unique(d)
        
    try:
        res = pd.concat(concat_rows, ignore_index=True)
    except Exception:
        # 兼容处理：若不同分块的数据类型冲突（如 float 列与字符串列混存），强制转为 object 避免奔溃
        safe_obj_rows = [make_df_columns_unique(d.astype(object)) for d in concat_rows]
        res = pd.concat(safe_obj_rows, ignore_index=True)
    res.attrs = base_df.attrs.copy()
    return res

def merge_continuation_tables(dfs):
    """
    Merges contiguous or matching continuation DataFrames into single DataFrames per table.
    """
    if not dfs:
        return []

    # 规范化输入：兼容 ExtractedTable 统一对象、dict 管道与原生 pd.DataFrame
    normalized_dfs = []
    for d in dfs:
        if d is None:
            continue
        if hasattr(d, 'to_dataframe'):
            normalized_dfs.append(d.to_dataframe())
        elif hasattr(d, 'df') and isinstance(d.df, pd.DataFrame):
            if hasattr(d, 'sync_attrs'):
                d.sync_attrs()
            normalized_dfs.append(d.df)
        elif isinstance(d, pd.DataFrame):
            normalized_dfs.append(d)
        elif isinstance(d, dict) and 'df' in d:
            df_item = d['df']
            if hasattr(df_item, 'attrs'):
                if 'label' in d and not df_item.attrs.get('label'):
                    df_item.attrs['label'] = d['label']
                if 'table_title' in d and not df_item.attrs.get('table_title'):
                    df_item.attrs['table_title'] = d['table_title']
                if 'page_idx' in d and 'page_idx' not in df_item.attrs:
                    df_item.attrs['page_idx'] = d['page_idx']
            normalized_dfs.append(df_item)

    if not normalized_dfs:
        return []

    # 环境变量与配置开关检查 (MinerU cross-page table merge switch)
    if os.getenv("MINERU_TABLE_MERGE_ENABLE", "true").lower() in ("0", "false", "no", "off"):
        return normalized_dfs

    def norm_label(lbl):
        if not lbl:
            return None
        # 移除常见的续表后缀如 (continued), Continued, (cont.), cont., cont'd, (续), （续）, 续, 续表, 接上表
        clean = re.sub(r'[\s,\-–—]*[\(（]?(?:cont\'?d|continued|cont|续表|续上表|接上表|续)\.?[）\)]?', '', str(lbl), flags=re.IGNORECASE)
        # 规整中英文表号前缀空格（如 "表1" -> "表 1", "Table1" -> "Table 1"）
        clean = re.sub(r'^(table|表)\s*([0-9a-zA-Z]+)', r'\1 \2', clean, flags=re.IGNORECASE)
        clean = re.sub(r'[:.\-–—\s]+$', '', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean

    merged_dfs = []
    current_group = []

    def _finalize_merged_group(grp):
        res_combined = combine_df_group(grp)
        if res_combined is not None:
            all_pages = []
            for g in grp:
                p = g.attrs.get('page_idx')
                if p is not None and p not in all_pages:
                    all_pages.append(p)
                for cp in g.attrs.get('covered_pages', []):
                    if cp not in all_pages:
                        all_pages.append(cp)
            if all_pages:
                res_combined.attrs['covered_pages'] = sorted(all_pages)
                res_combined.attrs['page_idx'] = all_pages[0]
        return res_combined

    for df in normalized_dfs:
        if df is None or df.empty:
            continue

        lbl = norm_label(df.attrs.get('label'))
        
        if not current_group:
            current_group.append(df)
            continue

        prev_df = current_group[-1]
        prev_lbl = norm_label(prev_df.attrs.get('label'))
        prev_page = prev_df.attrs.get('page_idx')
        curr_page = df.attrs.get('page_idx')
        
        # 严格校验：跨页续表满足页码严格递增（curr_page == prev_page + 1）或同属于一个连续提取块
        is_next_page = (
            prev_page is not None and curr_page is not None and (curr_page == prev_page + 1)
        )
        is_same_page = (
            prev_page is not None and curr_page is not None and (curr_page == prev_page)
        )
        is_block_sequence = (prev_page is None and curr_page is None)
        
        # 页面跨度距离约束：防止相隔甚远的独立章节/附表同名表被错误合并为续表 (如跨度超过2页)
        page_dist = abs(curr_page - prev_page) if (prev_page is not None and curr_page is not None) else None
        page_nearby = (page_dist is None) or (page_dist <= 2)

        # 跨页续表必须满足标签完全相同且页码邻近(<=2页)，或者明确带有续表标识且页码邻近，或者无标签且列数/语义高度匹配
        is_same_label = bool(lbl and prev_lbl and lbl.lower() == prev_lbl.lower() and page_nearby)
        raw_lbl = str(df.attrs.get('label', '')).lower()
        title_str = str(df.attrs.get('table_title', '')).lower()
        combined_hint = f"{raw_lbl} {title_str}"
        is_explicit_cont = bool(re.search(r'(?:续表|接上表|续上表|（续）|\(续\)|continued|cont\'?d|\(cont\b|cont\.)', combined_hint) or df.attrs.get('is_continuation')) and page_nearby
        
        # MinerU 跨页续表判定 1: 列名归一化与集合重合度 (Jaccard)
        norm_curr_cols = set(_normalize_col_name(c) for c in df.columns if c and not str(c).startswith(('Unnamed', 'Col')))
        norm_prev_cols = set(_normalize_col_name(c) for c in prev_df.columns if c and not str(c).startswith(('Unnamed', 'Col')))
        inter_cols = norm_curr_cols & norm_prev_cols
        union_cols = norm_curr_cols | norm_prev_cols
        col_jaccard = len(inter_cols) / max(1, len(union_cols)) if union_cols else 0.0

        common_cols = set(_normalize_col_name(c) for c in df.columns) & set(_normalize_col_name(c) for c in prev_df.columns)
        has_common_cols = len(common_cols - {'', 'unnamed', 'unnamed0', 'unnamed1', 'unnamed2', 'col1', 'col2', 'col3'}) >= 2

        # 小表安全防护：若续表列名实质上是数据行（如检出限、误差、纯数值），则确系续表首行（保护 RangeIndex）
        is_seq_int = (
            isinstance(df.columns, pd.RangeIndex) or
            list(df.columns) == list(range(len(df.columns))) or
            [str(c).strip() for c in df.columns] == [str(i) for i in range(len(df.columns))]
        )
        cols_are_data = (not is_seq_int) and (sum(1 for c in df.columns if is_pure_data_value(c)) / max(1, len(df.columns)) >= 0.3)

        # MinerU 跨页续表判定 2: 表格页宽/宽度比例对齐 (Page Width Ratio Alignment)
        prev_bbox = prev_df.attrs.get('bbox')
        curr_bbox = df.attrs.get('bbox')
        width_aligned = True
        if prev_bbox and curr_bbox and len(prev_bbox) >= 4 and len(curr_bbox) >= 4:
            w_prev = abs(prev_bbox[2] - prev_bbox[0])
            w_curr = abs(curr_bbox[2] - curr_bbox[0])
            if max(w_prev, w_curr) > 0 and (min(w_prev, w_curr) / max(w_prev, w_curr) < 0.60):
                width_aligned = False

        # MinerU 跨页续表判定 3: 表头独立性与联合条件判定
        has_distinct_header = (not cols_are_data) and any(not str(c).startswith(('Unnamed', 'Col')) for c in df.columns)
        if has_distinct_header and not has_common_cols and not is_explicit_cont:
            cols_match = False
        else:
            if is_explicit_cont or cols_are_data:
                cols_match = has_common_cols or (abs(df.shape[1] - prev_df.shape[1]) <= 2 and width_aligned)
            else:
                cols_match = (col_jaccard >= 0.35 or has_common_cols) and width_aligned and (abs(df.shape[1] - prev_df.shape[1]) <= 2)

        # MinerU 跨页续表判定 4: 小表独立性保护——后表若只有 1~5 行且无明确续表标识、且非数据行列名且列集合差异大，坚决独立保留，杜绝前大后小误合并
        is_small_unlabeled_independent = (
            df.shape[0] < 6 and 
            not is_explicit_cont and 
            not cols_are_data and 
            (col_jaccard < 0.40 or not has_common_cols)
        )

        is_unlabeled_cont = (
            (not lbl or is_explicit_cont or cols_are_data) and
            (is_next_page or is_block_sequence or (page_dist is not None and page_dist <= 2)) and
            prev_lbl and
            cols_match and
            not is_small_unlabeled_independent
        )

        if is_same_page and is_same_label:
            # 同页同标签为重复提取，保留质量更好/单元格更完整的版本，不作追加
            if df.size > prev_df.size:
                current_group[-1] = df
            continue

        if is_same_label or is_unlabeled_cont:
            if (not df.attrs.get('label') or df.attrs.get('is_continuation')) and prev_df.attrs.get('label'):
                df.attrs['label'] = prev_df.attrs.get('label')
            if (not df.attrs.get('table_title') or df.attrs.get('is_continuation')) and prev_df.attrs.get('table_title'):
                df.attrs['table_title'] = prev_df.attrs.get('table_title')
            current_group.append(df)
        else:
            combined = _finalize_merged_group(current_group)
            if combined is not None:
                merged_dfs.append(combined)
            current_group = [df]

    if current_group:
        combined = _finalize_merged_group(current_group)
        if combined is not None:
            merged_dfs.append(combined)

    return merged_dfs


def is_line_wrapped_reference(lines, idx):
    """
    Checks if lines[idx] matching the table pattern is actually a body text reference
    wrapped from the previous line.
    """
    if idx <= 0:
        return False
    prev_line = lines[idx-1].strip()
    if not prev_line:
        return False
        
    # If the match is deep inside a text block (preceded by substantial body text),
    # it is a body text reference, not a standalone caption.
    preceding_text_len = sum(len(l) for l in lines[:idx])
    if preceding_text_len > 120:
        return True
        
    # Check the current line text after the matched table label
    curr_line = lines[idx].strip()
    pattern = r'^((?:[Tt][Aa][Bb][Ll][Ee]|[Tt][Aa][Bb][Ll][Ee][Aa][Uu][Xx]?|[Tt][Aa][Bb]\.|表|[Ss][Uu][Pp][Pp][Ll][Ee][Mm][Ee][Nn][Tt][Aa][Rr][Yy]\s+[Mm][Aa][Tt][Ee][Rr][Ii][Aa][Ll]|[Aa][Dd][Dd][Ii][Tt][Ii][Oo][Nn][Aa][Ll]\s+[Tt][Aa][Bb][Ll][Ee]|[Aa][Dd][Dd][Ii][Tt][Ii][Oo][Nn][Aa][Ll]\s+[Ff][Ii][Ll][Ee])\s*(?:[Ss]\s*)?(\d+|[IVXLCDMivxlcdm]+|ni|rn|[A-Z]))'
    match = re.match(pattern, curr_line)
    if match:
        label = match.group(1)
        remaining_text = curr_line[len(label):].strip()
        # If there is a substantial title after the label (e.g. more than 10 characters),
        # then it is a real caption, not a simple reference, so do NOT treat it as wrapped.
        clean_rem = re.sub(r'^[\.\:\-\—\–\s]+', '', remaining_text).strip()
        if len(clean_rem) > 10:
            return False
            
    # If the previous line explicitly ends with reference prepositions
    if re.search(r'\b(?:in|see|shown|presented|supp|suppl|supplementary|fig|figure)\s*$', prev_line, re.IGNORECASE):
        return True
    # If the previous line doesn't end with sentence-ending punctuation AND is long enough to be a body line
    if not re.search(r'[\.\!\?\:]\s*$', prev_line) and len(prev_line) > 35:
        return True
    return False
