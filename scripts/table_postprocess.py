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
            if len(clean_lines) < 2:
                return None
            csv_data = "\n".join([line.strip("|") for line in clean_lines])
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
        try:
            if '.' in val_clean:
                return float(val_clean)
            else:
                return int(val_clean)
        except ValueError:
            pass
    return val

def escape_formula(val):
    if isinstance(val, str) and val.startswith(('=', '+', '-', '@')):
        try:
            clean_val = val.replace(',', '').strip()
            float(clean_val)
            if '.' in clean_val:
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

    cols = [str(c).strip().lower() for c in df.columns if str(c).strip() and not str(c).startswith('Unnamed') and not str(c).startswith('Col_')]
    if not cols:
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
        if len(row_vals) >= 2:
            matches = sum(1 for v in row_vals if any(v == c or (len(v) >= 2 and v in c) for c in cols))
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

    # 5. 转义 Excel 公式
    df.columns = [escape_formula(c) for c in df.columns]
    df = df_map(df, escape_formula)

    if hasattr(df, 'attrs'):
        df.attrs.update(original_attrs)
    return df


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
            # 检查是否有第二层子表头
            has_repeated_parents = (len(new_headers) != len(set(new_headers))) or any(not h or h == 'nan' for h in new_headers)
            if has_repeated_parents and best_header_idx + 1 < len(df):
                sub_cells = [str(val).strip() for val in df.iloc[best_header_idx + 1]]
                sub_non_num = sum(1 for c in sub_cells if len(c) > 0 and not c.replace('.', '').replace('-', '').replace(',', '').replace('%', '').replace('+', '').replace('~', '').isdigit())
                max_sub_len = max((len(c) for c in sub_cells), default=0)
                if sub_non_num >= len(sub_cells) * 0.5 and max_sub_len <= 25:
                    combined_headers = []
                    for h1, h2 in zip(new_headers, sub_cells):
                        h1_c = clean_latex_and_ocr(clean_text(h1)).strip()
                        h2_c = clean_latex_and_ocr(clean_text(h2)).strip()
                        if h1_c == h2_c or not h2_c or h2_c == 'nan':
                            combined_headers.append(h1_c if h1_c and h1_c != 'nan' else "")
                        elif not h1_c or h1_c == 'nan':
                            combined_headers.append(h2_c)
                        else:
                            combined_headers.append(f"{h1_c}_{h2_c}")
                    new_headers = combined_headers
                    header_consumed = 2

            df.columns = [h if h and h != 'nan' else f"Col_{i+1}" for i, h in enumerate(new_headers)]
            df = df.iloc[best_header_idx + header_consumed:].reset_index(drop=True)

    return df


def _filter_noise_and_placeholders(df: pd.DataFrame) -> pd.DataFrame:
    """清理空列、无有效数据的占位列以及嵌入的跨页噪声行。"""
    # Drop all-NaN columns and trailing empty _nan columns
    df = df.dropna(how='all', axis=1)
    df = df.loc[:, ~df.columns.astype(str).str.lower().str.endswith('_nan')]
    df = df.loc[:, ~df.columns.astype(str).str.lower().str.endswith('_none')]
    
    # Drop any 'Unnamed', 'nan', integer, or placeholder column that has no real data or is sparse
    cols_to_drop = []
    # 确保在合并杂乱列前使用 object 类型，防止写入文本时触发 float64 类型异常
    df = df.astype(object)
    for c_idx, col in enumerate(df.columns):
        col_str = str(col).strip().lower()
        is_placeholder_col = (
            col_str in ['', 'nan', 'none', 'null', 'unnamed'] or
            col_str.isdigit() or
            col_str.startswith(('unnamed', 'col'))
        )
        if is_placeholder_col:
            valid_vals = [str(x).strip() for x in df[col] if pd.notnull(x) and str(x).strip() not in ['', 'nan', 'none', 'null']]
            if len(valid_vals) <= max(1, int(len(df) * 0.15)):
                # Merge any stray values into previous column if previous is text
                if c_idx > 0:
                    for r in range(len(df)):
                        val = df.iat[r, c_idx]
                        if pd.notnull(val) and str(val).strip() not in ['', 'nan', 'none', 'null']:
                            prev_val = df.iat[r, c_idx - 1]
                            df.iat[r, c_idx - 1] = f"{prev_val} {val}".strip() if pd.notnull(prev_val) else str(val).strip()
                cols_to_drop.append(col)
    if cols_to_drop:
        df = df.drop(columns=cols_to_drop)

    # Clean up noise rows (e.g. standalone "Table 2 (continued)", index rows, or embedded repeated header rows)
    def _is_table_noise_row(row):
        cells = [str(x).strip() for x in row.values if pd.notnull(x) and str(x).strip() != '']
        if not cells:
            return False
        row_text = " ".join(cells)
        if re.search(r'^\s*(?:Table|表)\s*\d+(?:\.\d+)?.*|\b(?:continued|cont\'d|contd)\b|\bTable\s*\(cont', row_text, re.IGNORECASE):
            num_cells = len(cells)
            non_text_cells = sum(1 for c in cells if re.search(r'\d', c))
            if num_cells <= 2 or non_text_cells == 0:
                return True
        # 识别跨页合并后残留的纯数字列索引行（如 0 1 2.00 3 4.0 5 ...）
        if len(cells) >= 3 and all(bool(re.match(r'^\d+(?:\.0+)?$', c)) for c in cells):
            return True
        # 识别跨页合并后残留的重复表头行（列名与 DataFrame 顶层列名高度重合）
        col_names_lower = [str(c).strip().lower() for c in df.columns]
        matching_header_cells = sum(1 for c in cells if c.lower() in col_names_lower)
        if len(cells) >= 3 and (matching_header_cells / len(cells) >= 0.4):
            return True
        return False

    df = df[~df.apply(_is_table_noise_row, axis=1)]
    return df


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

    # 1. 付费墙拦截判定
    if len(df) <= 4:
        cells_str = ' '.join(str(val) for val in df.values.flatten()).lower()
        paywall_keywords = ['login', 'sign in', 'purchase', 'subscription', 'rent', 'purchase access', 'institutional login', 'register', 'subscribe', 'cookie policy', 'accept cookies']
        if any(kw in cells_str for kw in paywall_keywords):
            print("Warning: Detected paywall or login warning table. Rejecting table.")
            return pd.DataFrame()

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


def align_dataframe_columns(df_to_align: pd.DataFrame, target_cols: List[str]) -> pd.DataFrame:
    """
    Dynamically aligns columns of df_to_align to target_cols using string similarity,
    fuzzy element/unit matching, and data type alignment.
    Missing target columns are filled with empty string.
    """
    if df_to_align is None or df_to_align.empty:
        return df_to_align

    curr_cols = list(df_to_align.columns)
    if curr_cols == target_cols:
        return df_to_align

    aligned_data = {}
    matched_curr = set()

    for t_col in target_cols:
        t_clean = re.sub(r'[\s_\(\)（）\.\/]+', '', str(t_col).lower())
        best_match = None
        best_score = 0.0

        for c_idx, c_col in enumerate(curr_cols):
            if c_idx in matched_curr:
                continue
            c_clean = re.sub(r'[\s_\(\)（）\.\/]+', '', str(c_col).lower())
            
            # Exact or substring match
            if t_clean == c_clean:
                best_match = c_idx
                best_score = 1.0
                break
            elif t_clean and c_clean and (t_clean in c_clean or c_clean in t_clean):
                score = min(len(t_clean), len(c_clean)) / max(len(t_clean), len(c_clean))
                if score > best_score:
                    best_score = score
                    best_match = c_idx

        if best_match is not None and best_score >= 0.40:
            aligned_data[t_col] = df_to_align.iloc[:, best_match].tolist()
            matched_curr.add(best_match)
        else:
            aligned_data[t_col] = [""] * len(df_to_align)

    return pd.DataFrame(aligned_data, columns=target_cols)


def combine_df_group(group):
    if not group:
        return None
    if len(group) == 1:
        return group[0]
        
    base_df = group[0]
    base_cols = list(base_df.columns)
    
    def is_pure_data_value(s_in):
        s = str(s_in).strip()
        if not s:
            return False
        # 常见地质化学式/同位素/单位/字段词，属于表头文本，非纯数据
        if (re.match(r'^(?:[A-Z][a-z]?\d*)+$', s) or '/' in s or 'δ' in s or '‰' in s or 
            '%' in s or any(w in s.lower() for w in ['ppm', 'ppb', 'wt', 'sample', 'spot', 'no.', 'mineral', 'rock', 'age', 'stage', 'type', 'depth'])):
            return False
        return bool(re.match(r'^-?\d+(?:\.\d+)?$', s) or s in ('-', '—', 'n.d.', 'bdl', 'b.d.l.'))

    # Detect horizontal column-split tables (e.g. Table 4 Part 1 on Page 17 & Part 2 on Page 18)
    # 必须满足：两表均为具有独立表头文本的横向分块，首列同名，且列名非纯数字或数据特征
    is_horizontal = False
    if len(group) >= 2:
        col1_list = [str(c).strip() for c in group[0].columns[1:]]
        col2_list = [str(c).strip() for c in group[1].columns[1:]]
        has_numeric_cols = sum(1 for c in col2_list if is_pure_data_value(c)) / max(1, len(col2_list)) >= 0.25
        if not has_numeric_cols:
            col1_set = set(c.lower() for c in col1_list)
            col2_set = set(c.lower() for c in col2_list)
            row_counts = [len(g) for g in group if g is not None and not g.empty]
            max_rows = max(row_counts) if row_counts else 0
            min_rows = min(row_counts) if row_counts else 0
            rows_compatible = max_rows > 0 and min_rows / max_rows >= 0.5
            if (col1_set and col2_set and
                    len(col1_set & col2_set) / max(1, len(col1_set)) < 0.35 and
                    rows_compatible):
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
        
        # 检查 next_df 的 columns 是否实质上是第一行数据（续表无表头时常见现象）
        next_cols = [str(c).strip() for c in next_df.columns]
        data_col_count = sum(1 for c in next_cols if is_pure_data_value(c))
        is_data_in_columns = (data_col_count / max(1, len(next_cols)) >= 0.4)
        
        if is_data_in_columns:
            # 将 next_df.columns 转回为第一行数据，避免丢失数据首行
            col_vals = [c for c in next_df.columns]
            if len(col_vals) > len(base_cols):
                col_vals = col_vals[:len(base_cols)]
            elif len(col_vals) < len(base_cols):
                col_vals = col_vals + [""] * (len(base_cols) - len(col_vals))
            row_from_cols = pd.DataFrame([col_vals], columns=base_cols)
            
            if len(next_df.columns) < len(base_cols):
                for i in range(len(base_cols) - len(next_df.columns)):
                    next_df[f"Extra_{i}"] = ""
                next_df.columns = base_cols
            elif len(next_df.columns) > len(base_cols):
                next_df = next_df.iloc[:, :len(base_cols)]
                next_df.columns = base_cols
            else:
                next_df.columns = base_cols
            next_df = pd.concat([row_from_cols, next_df], ignore_index=True)
        else:
            # 动态语义对齐：当列数不完全相同或列名略有变动时，执行基于相似度的自适应对齐
            if len(next_df.columns) == len(base_cols):
                next_df.columns = base_cols
            elif abs(len(next_df.columns) - len(base_cols)) <= 4:
                next_df = align_dataframe_columns(next_df, base_cols)
            else:
                # 列数差异过大，跳过合并
                continue

            # 逐行剥离 next_df 顶部的多层重复表头行、序号行与单位行
            while len(next_df) > 0:
                first_row = [str(x).strip() for x in next_df.iloc[0].values if pd.notna(x)]
                is_index_row = len(first_row) >= 3 and all(re.match(r'^\d+$', c) for c in first_row if c)
                is_repeat_header = sum(1 for c in first_row if c and any(c.lower() == str(b).strip().lower() for b in base_cols)) / max(1, len(base_cols)) >= 0.25
                is_unit_row = len(first_row) > 0 and all(any(u in c.lower() for u in ['wt%', 'ppm', 'ppb', '%', '‰', 'ma', 'ga', 'ka', '℃', '°c']) or c.startswith('(') for c in first_row if c)
                if is_index_row or is_repeat_header or is_unit_row:
                    next_df = next_df.iloc[1:].reset_index(drop=True)
                else:
                    break
            
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

    def norm_label(lbl):
        if not lbl:
            return None
        clean = re.sub(r'\s*\((?:contd|continued|cont)\.?\)', '', str(lbl), flags=re.IGNORECASE)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean

    merged_dfs = []
    current_group = []

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

        # 跨页续表必须满足标签完全相同且页码邻近(<=2页)，或者明确带有续表标识且页码邻近，或者无标签且列数高度匹配
        is_same_label = bool(lbl and prev_lbl and lbl.lower() == prev_lbl.lower() and page_nearby)
        title_str = str(df.attrs.get('table_title', '')).lower()
        is_explicit_cont = bool(re.search(r'(?:续表|接上表|continued|\(cont\)|cont\.)', title_str)) and page_nearby
        
        cols_match = (
            df.shape[1] == prev_df.shape[1] or
            abs(df.shape[1] - prev_df.shape[1]) <= 2
        )

        # 小表安全防护：若两表均为独立小表（<6行）且无明确续表标识，禁止单纯因列数相同盲目合并
        is_small_unlabeled_pair = (df.shape[0] < 6 and prev_df.shape[0] < 6 and not is_explicit_cont)

        is_unlabeled_cont = (
            (not lbl or is_explicit_cont) and
            (is_next_page or is_block_sequence) and
            prev_lbl and
            cols_match and
            not is_small_unlabeled_pair
        )

        if is_same_page and is_same_label:
            # 同页同标签为重复提取，保留质量更好/单元格更完整的版本，不作追加
            if df.size > prev_df.size:
                current_group[-1] = df
            continue

        if is_same_label or is_unlabeled_cont:
            if not df.attrs.get('label') and prev_df.attrs.get('label'):
                df.attrs['label'] = prev_df.attrs.get('label')
            current_group.append(df)
        else:
            combined = combine_df_group(current_group)
            if combined is not None:
                merged_dfs.append(combined)
            current_group = [df]

    if current_group:
        combined = combine_df_group(current_group)
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
