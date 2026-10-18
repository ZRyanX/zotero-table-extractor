#!/usr/bin/env python3
"""
extract_zotero_table.py — 主入口（CLI）：Zotero/本地 PDF 表格提取与 Excel 导出。

批量模式两阶段执行（预分析 → 并行提取）；单篇任务内 HTML ∥ Paddle 竞速。
整合 DocLayout-YOLO 本地 AI 视觉版面提取与回退保障。
"""
from __future__ import annotations

try:
    from .system_detector import (
        IS_MACOS, IS_WINDOWS, get_paper_tables_dir, get_zotero_db_path,
        get_zotero_storage_dir, get_journal_downloader_script,
        get_agent_reasoning_dir, get_audit_db_path, get_targeted_audit_db_path, adapt_path
    )
except ImportError:
    from system_detector import (
        IS_MACOS, IS_WINDOWS, get_paper_tables_dir, get_zotero_db_path,
        get_zotero_storage_dir, get_journal_downloader_script,
        get_agent_reasoning_dir, get_audit_db_path, get_targeted_audit_db_path, adapt_path
    )


import sys
import os
import re
from decimal import Decimal, InvalidOperation
import json
import argparse
import threading
import concurrent.futures
from typing import Tuple, Set, Optional, List, Dict, Any, Union

import pandas as pd

try:
    import pymupdf as fitz
except ImportError:
    import fitz
sys.modules['fitz'] = fitz

# Force UTF-8 stdout and stderr encoding on Windows to prevent encoding errors
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8', errors='ignore', line_buffering=True)
    sys.stderr.reconfigure(encoding='utf-8', errors='ignore', line_buffering=True)


# Import online extraction package (online/ 子包)
try:
    from . import online
except ImportError:
    try:
        import online
    except ImportError:
        online = None

# Import OCR client helper
try:
    from . import ocr_client
except ImportError:
    try:
        import ocr_client
    except ImportError:
        ocr_client = None

# Split-out helpers
try:
    from .common import format_table_label
    from .table_postprocess import parse_structured_vlm_content
    from .excel_export import make_safe_filename, save_tables_to_excel
    from .pdf_tables import get_page_effective_rotation, export_crops_to_excel, bake_pdf_rotations
    from .pdf_table_extractor import extract_tables_from_pdf
except ImportError:
    try:
        from common import format_table_label
    except ImportError:
        format_table_label = lambda x: str(x).strip() if x else ""
    from table_postprocess import parse_structured_vlm_content
    from excel_export import make_safe_filename, save_tables_to_excel
    from pdf_tables import get_page_effective_rotation, export_crops_to_excel, bake_pdf_rotations
    try:
        from pdf_table_extractor import extract_tables_from_pdf
    except ImportError:
        extract_tables_from_pdf = None


def scan_pdf_table_expected_labels(pdf_path: str) -> Tuple[Set[str], int]:
    """
    轻量级扫描 PDF 文本层，提取全文预期的标准表格标题声明与表号集合。
    支持长文献（全篇扫描，无 150 页人为截断），返回 (expected_labels, expected_count)。
    """
    try:
        try:
            from .common import format_table_label, TABLE_LABEL_RE
        except ImportError:
            from common import format_table_label, TABLE_LABEL_RE
    except ImportError:
        format_table_label = lambda x: x
        import re
        TABLE_LABEL_RE = re.compile(r'\b((?:TABLE|Table|表)\s*\d+(?:\.\d+)?)', re.IGNORECASE)

    try:
        try:
            from .table_validator import scan_pdf_table_declarations
        except ImportError:
            from table_validator import scan_pdf_table_declarations
        decls = scan_pdf_table_declarations(pdf_path)
        labels = set()
        for p, d_list in decls.items():
            for d in d_list:
                if not d.get('is_continuation'):
                    lbl = d.get('label')
                    if lbl:
                        labels.add(format_table_label(lbl))
        if labels:
            return labels, len(labels)
    except Exception:
        pass

    try:
        import pymupdf as fitz
        doc = fitz.open(pdf_path)
        labels = set()
        num_pages = len(doc)
        for i in range(num_pages):
            text = doc[i].get_text("text")
            if not text:
                continue
            for m in TABLE_LABEL_RE.finditer(text):
                pos = m.start()
                before = text[max(0, pos-15):pos]
                after = text[pos+len(m.group(0)):pos+len(m.group(0))+15]
                # 排除明显的正文引用前缀与后缀
                if any(before.rstrip().endswith(p) for p in ['见', '从', '（', '(', '如', '由', '根据', '在', 'in', 'see', 'from']):
                    continue
                if any(after.lstrip().startswith(s) for s in ['）', ')', '中', '可以', '所示', '可知', '看出']):
                    continue
                raw_lbl = m.group(0)
                formatted = format_table_label(raw_lbl)
                if formatted:
                    labels.add(formatted)
        doc.close()
        return labels, len(labels)
    except Exception:
        return set(), 0


def _count_pdf_table_captions(pdf_path: str) -> int:
    """保持向后兼容的轻量级表数统计"""
    _, count = scan_pdf_table_expected_labels(pdf_path)
    return count


def process_single_pdf(pdf_path, args, is_batch=False, plan=None):
    """Processes a single PDF file, extracting tables online or falling back to PDF.

    plan: optional pre-analysis result from batch_planner.analyze_pdf (doi/title/url/is_cnki),
          used to skip redundant metadata resolution and drive parallel racing.
    """
    print(f"\nProcessing: {pdf_path}")
    
    target_pdf_path = pdf_path
    temps_to_clean = []
    try:
        if os.path.exists(pdf_path) and os.path.isfile(pdf_path):
            import pymupdf as fitz

            # Tier 1: 页面级无损顺时针方向校准 (Lossless Page Rotation Remediation)
            was_remediated = False
            try:
                try:
                    from .doc_orientation_detector import remediate_pdf_pages_lossless
                except ImportError:
                    from doc_orientation_detector import remediate_pdf_pages_lossless

                cand_pages = None
                try:
                    try:
                        from .pdf_table_extractor import quick_detect_candidate_pages
                    except ImportError:
                        from pdf_table_extractor import quick_detect_candidate_pages
                    cand_pages = quick_detect_candidate_pages(target_pdf_path)
                except Exception:
                    pass

                lossless_path, was_rotated, tmp_lossless = remediate_pdf_pages_lossless(
                    target_pdf_path,
                    candidate_pages=cand_pages
                )
                if was_rotated and tmp_lossless:
                    target_pdf_path = lossless_path
                    temps_to_clean.append(tmp_lossless)
                    was_remediated = True
            except Exception as e_ori:
                print(f"[Doc-Ori Tier 1] 无损旋转探测提示: {e_ori}")

            try:
                from .pdf_table_extractor import has_text_layer, quick_classify_pdf
            except ImportError:
                from pdf_table_extractor import has_text_layer, quick_classify_pdf
            
            # 使用 pdf-inspector quick_classify_pdf (5~10ms) 进行超快速预检
            clf_res = quick_classify_pdf(target_pdf_path)
            if clf_res.get('confidence', 0) >= 0.8 and clf_res.get('pdf_type') in ('text_based', 'scanned'):
                is_native = (clf_res['pdf_type'] == 'text_based')
            else:
                is_native = has_text_layer(target_pdf_path)

            doc = fitz.open(target_pdf_path)
            page_rotations = [get_page_effective_rotation(p) for p in doc]
            has_rotation = any(r != 0 for r in page_rotations)
            doc.close()

            # 全页像素光栅化烘焙 (baking) 会把页面栅格化为位图图片并彻底抹除原生矢量文本层与 Tagged 结构树。
            # 因此仅对纯扫描版 PDF 执行旋转烘焙 (以兼容外部 OCR 工具)；
            # 对 native PDF，保留原生数字化文本层，视口与视觉切图走正向渲染，矢量几何提取已适配 rotation_matrix。
            if has_rotation and not is_native:
                print(f"[PDF] 检测到旋转页面或文字方向 (Baking page rotations)...")
                eff_baked, was_baked, tmp_baked = bake_pdf_rotations(target_pdf_path, dpi=150)
                if was_baked and tmp_baked:
                    target_pdf_path = eff_baked
                    temps_to_clean.append(tmp_baked)
                    print(f"[PDF] Baked rotated PDF saved to: {target_pdf_path}")
            elif has_rotation and is_native:
                print(f"[PDF] Native PDF 存在旋转页面 (保留原生矢量文本层与 Tagged 结构，视口与视觉切图走正向渲染，矢量几何通过 rotation_matrix 归一化)")

        return _do_process_single_pdf(
            target_pdf_path,
            orig_pdf_path=pdf_path,
            args=args,
            is_batch=is_batch,
            plan=plan,
            candidate_pages=cand_pages,
            remediated=was_remediated,
        )
    finally:
        for t_p in temps_to_clean:
            if t_p and os.path.exists(t_p):
                try:
                    os.remove(t_p)
                except Exception:
                    pass


def is_pre_2020_chinese_paper(pdf_path: str, plan: dict = None) -> bool:
    """
    判断文献是否为 2020 年之前的中文文献。
    此类文献在知网/万方上绝大多数无在线 HTML 结构（仅有扫描版或纯 PDF），
    应直接进入本地/PaddleOCR PDF 提取管线，跳过无谓的网页请求以大幅提高效率。
    """
    filename = os.path.basename(pdf_path)
    title = (plan.get('title') or filename) if plan else filename

    # 1. 中文字符检测
    has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', title))
    if not has_chinese:
        return False

    # 2. 年份提取
    m_year = re.search(r'(?:^|[\D_])(19\d{2}|20\d{2})(?:[\D_]|$)', filename)
    if not m_year and plan and plan.get('year'):
        m_year = re.search(r'(?:^|[\D_])(19\d{2}|20\d{2})(?:[\D_]|$)', str(plan.get('year')))

    if m_year:
        year = int(m_year.group(1))
        if year < 2020:
            return True

    return False


def call_journal_supp_downloader(doi_or_url: str, output_dir: str) -> bool:
    """
    调用独立的 journal-supp-downloader 技能下载学术论文补充数据与附表。
    """
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "journal-supp-downloader", "scripts", "journal_downloader.py"),
        os.path.expanduser("~/.gemini/antigravity-cli/skills/journal-supp-downloader/scripts/journal_downloader.py"),
        get_journal_downloader_script(),
    ]
    supp_downloader_script = None
    for cand in candidates:
        if os.path.exists(cand):
            supp_downloader_script = cand
            break

    if not supp_downloader_script:
        print(f"[Supp Downloader] 未找到附表下载技能脚本 (已探测路径: {candidates[0]})")
        return False

    import subprocess
    cmd = [sys.executable, supp_downloader_script, doi_or_url, "-o", output_dir]
    try:
        print(f"[Supp Downloader] 正在调用 journal-supp-downloader 技能抓取附表...")
        ret = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if ret.returncode == 0:
            print(f"[Supp Downloader] 附表下载成功完成！")
            return True
        else:
            print(f"[Supp Downloader] 下载返回状态码 {ret.returncode}: {ret.stderr[:200]}")
    except Exception as e:
        print(f"[Supp Downloader] 调用异常: {e}")
    return False


def _trigger_zotero_sync(pdf_path, target_output, dfs, args):
    """触发 Zotero 与 Zotero-Better-Notes 生态联动（若配置或命令行显式开启）。"""
    try:
        try:
            from .zotero_sync import sync_extracted_tables_to_zotero
        except ImportError:
            from zotero_sync import sync_extracted_tables_to_zotero
        try:
            from .common import load_config
        except ImportError:
            from common import load_config

        force_sync = getattr(args, 'sync_zotero', False)
        excel_target = None
        if os.path.isfile(target_output) and target_output.endswith('.xlsx'):
            excel_target = target_output
        elif os.path.isdir(target_output):
            import glob
            xlsxs = sorted(glob.glob(os.path.join(target_output, "*.xlsx")))
            if xlsxs:
                excel_target = xlsxs[0]

        if excel_target and os.path.exists(excel_target) and dfs:
            cfg = load_config() if callable(load_config) else {}
            if getattr(args, 'zotero_api_key', None):
                cfg['ZOTERO_API_KEY'] = args.zotero_api_key
            if getattr(args, 'zotero_user_id', None):
                cfg['ZOTERO_USER_ID'] = args.zotero_user_id
            if getattr(args, 'db_path', None):
                cfg['ZOTERO_DB_PATH'] = args.db_path

            sync_extracted_tables_to_zotero(
                pdf_path, excel_target, dfs, config=cfg, force_sync=force_sync
            )
    except Exception as e_sync:
        print(f"[ZoteroSync Hook Notice] {e_sync}")


def _normalize_merge_key(val: Any) -> str:
    """
    标准化合并连接键：消除大小写、首尾空白以及浮点数/整数格式差异 (如 '25.0' -> '25')。
    采用 Decimal 无损解析，杜绝大整数编号精度塌陷与高精度浮点碰撞。
    保护带前导零的样本编号 (如 '01', '007', '+007') 不被截断为整数。
    """
    if pd.isna(val) or val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("", "nan", "none", "null", "-", "—", "infinity", "inf"):
        return ""
    # 保护前导零整数编码 (如 01, 007, +007)
    if re.match(r'^[+-]?0\d+$', s):
        clean = s.lstrip('+')
        return clean.casefold()
    try:
        # 纯整数无损保留
        if re.match(r'^[+-]?\d+$', s):
            return str(int(s))
        d = Decimal(s)
        if not d.is_finite():
            return s.casefold()
        if d == d.to_integral():
            return str(int(d))
        sign, digits, exp = d.as_tuple()
        if exp < 0:
            return f"{d:f}".rstrip('0').rstrip('.')
        elif exp >= 0:
            return str(int(d))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        pass
    return s.casefold()


def _merge_complementary_tables(primary_df: pd.DataFrame, secondary_df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    智能合并两张属于同一表格但行列/字段互补的 DataFrame：
    保留 primary_df 作为主干结构（包含其全量行与数据），
    安全补充 secondary_df 中独有的列数据，杜绝字段丢失。
    严禁盲目按行位置补列，必须在确认行身份后对齐合并；
    保留双方未匹配行（避免丢行），若存在重复键冲突或无法置信确认行身份时返回 None 以便独立保留版本。
    """
    prim = primary_df.reset_index(drop=True)
    sec = secondary_df.reset_index(drop=True)

    p_cols = list(prim.columns)
    s_cols = list(sec.columns)
    p_map = {str(c).lower().strip(): c for c in p_cols}
    s_map = {str(c).lower().strip(): c for c in s_cols}

    common_lower = [c for c in p_map if c in s_map and not c.startswith(('unnamed', 'col')) and c not in ('', 'nan', 'none')]
    extra_s_lower = [c for c in s_map if c not in p_map and not c.startswith(('unnamed', 'col')) and c not in ('', 'nan', 'none')]

    extra_s_cols = [s_map[c] for c in extra_s_lower]

    merged = None
    if common_lower:
        p_on = [p_map[c] for c in common_lower]
        try:
            rename_dict = {s_map[c]: p_map[c] for c in common_lower}
            sec_renamed = sec.rename(columns=rename_dict)
            on_cols = p_on

            prim_join = prim.copy()
            sec_join = sec_renamed.copy()
            for c in on_cols:
                prim_join[f'_key_{c}'] = prim_join[c].apply(_normalize_merge_key)
                sec_join[f'_key_{c}'] = sec_join[c].apply(_normalize_merge_key)

            key_cols = [f'_key_{c}' for c in on_cols]

            prim_valid = prim_join[prim_join[key_cols].apply(lambda row: any(v != "" for v in row), axis=1)]
            sec_valid = sec_join[sec_join[key_cols].apply(lambda row: any(v != "" for v in row), axis=1)]

            # 1. 检查复合键唯一性与数据冲突：
            # 若任意一方存在重复复合键且非键列数据存在冲突，或键重复导致对应歧义，严禁盲目去重覆盖数值，直接放弃合并
            has_composite_conflict = False
            sec_has_dups = sec_valid.duplicated(subset=key_cols, keep=False).any()
            prim_has_dups = prim_valid.duplicated(subset=key_cols, keep=False).any()

            if sec_has_dups:
                for _, group in sec_valid.groupby(key_cols):
                    if len(group) > 1:
                        non_key_cols = [c for c in sec_renamed.columns if c not in on_cols and not c.startswith('_key_')]
                        for col in non_key_cols:
                            distinct_vals = set(
                                str(v).strip() for v in group[col]
                                if pd.notna(v) and str(v).strip().lower() not in ('nan', 'none')
                            )
                            if len(distinct_vals) > 1:
                                has_composite_conflict = True
                                break
                    if has_composite_conflict:
                        break

            if not has_composite_conflict and prim_has_dups:
                prim_non_key = [c for c in prim.columns if c not in on_cols and not c.startswith('_key_')]
                for _, group in prim_valid.groupby(key_cols):
                    if len(group) > 1:
                        for col in prim_non_key:
                            distinct_vals = set(
                                str(v).strip() for v in group[col]
                                if pd.notna(v) and str(v).strip().lower() not in ('nan', 'none')
                            )
                            if len(distinct_vals) > 1:
                                has_composite_conflict = True
                                break
                    if has_composite_conflict:
                        break

            # 复合键唯一性保护：若键重复导致对应歧义或存在冲突，放弃盲目去重合并
            if sec_has_dups or prim_has_dups or has_composite_conflict:
                has_composite_conflict = True

            if not has_composite_conflict and len(sec_valid) > 0 and len(prim_valid) > 0:
                sec_to_merge = sec_valid[key_cols + extra_s_cols].copy()
                sec_to_merge['_sec_matched_idx'] = sec_valid.index

                merged_candidate = pd.merge(
                    prim_join,
                    sec_to_merge,
                    on=key_cols,
                    how='left'
                )

                matched_sec_indices = set(int(x) for x in merged_candidate['_sec_matched_idx'].dropna())
                merged_candidate = merged_candidate.drop(columns=key_cols + ['_sec_matched_idx'])

                non_empty_added = sum(
                    1 for c in extra_s_cols if c in merged_candidate.columns
                    for v in merged_candidate[c] if pd.notna(v) and str(v).strip() not in ('', 'nan', 'none')
                )

                # 只有在双方存在置信匹配（至少匹配到有效数据）时才接受
                if len(matched_sec_indices) > 0 and (non_empty_added > 0 or not extra_s_cols):
                    unmatched_sec = sec_renamed[~sec_renamed.index.isin(matched_sec_indices)]
                    all_cols = list(prim.columns) + [c for c in extra_s_cols if c not in prim.columns]
                    if not unmatched_sec.empty:
                        aligned_unmatched = pd.DataFrame(index=unmatched_sec.index)
                        for c in all_cols:
                            aligned_unmatched[c] = unmatched_sec[c] if c in unmatched_sec.columns else ''
                        merged_candidate = pd.concat([merged_candidate, aligned_unmatched], ignore_index=True)
                    merged = merged_candidate

            if merged is None and not has_composite_conflict:
                # 复合键未能匹配，仅在存在明确样本/实体标识列且无数据冲突时，尝试单标识列对齐
                id_keywords = ('sample', 'id', 'name', 'specimen', 'no', 'num', 'number', 'code', 'site', 'station', 'point', '样', '编号', '样品', '名称')
                id_key_cols = [k for k in key_cols if any(w in k.lower() for w in id_keywords)]
                candidate_fallback_keys = id_key_cols if id_key_cols else []

                best_single = None
                max_matches = 0
                for k in candidate_fallback_keys:
                    prim_keys = prim_join[prim_join[k] != ''][k]
                    sec_keys = sec_join[sec_join[k] != ''][k]
                    # 标识键必须在双方非空记录中唯一，严禁一对多或多对多匹配
                    if len(prim_keys) == 0 or len(sec_keys) == 0:
                        continue
                    if len(prim_keys) != prim_keys.nunique() or len(sec_keys) != sec_keys.nunique():
                        continue

                    sec_k_valid = sec_join[sec_join[k] != ''].copy()
                    has_data_conflict = False
                    other_common = [c for c in on_cols if f'_key_{c}' != k]
                    if other_common:
                        sec_check = sec_k_valid.set_index(k)
                        for idx, row in prim_join.iterrows():
                            key_val = row[k]
                            if key_val and key_val in sec_check.index:
                                sec_row = sec_check.loc[key_val]
                                for c in other_common:
                                    kc = f'_key_{c}'
                                    pv = row[kc]
                                    sv = sec_row[kc]
                                    if pv and sv and pv != sv:
                                        has_data_conflict = True
                                        break
                            if has_data_conflict:
                                break
                    if has_data_conflict:
                        continue

                    sec_to_merge = sec_k_valid[[k] + extra_s_cols].copy()
                    sec_to_merge['_sec_matched_idx'] = sec_k_valid.index
                    cand = pd.merge(
                        prim_join,
                        sec_to_merge,
                        on=[k],
                        how='left'
                    )

                    matched_sec_indices = set(int(x) for x in cand['_sec_matched_idx'].dropna())
                    cand = cand.drop(columns=key_cols + ['_sec_matched_idx'])

                    n_added = sum(
                        1 for c in extra_s_cols if c in cand.columns
                        for v in cand[c] if pd.notna(v) and str(v).strip() not in ('', 'nan', 'none')
                    )
                    if len(matched_sec_indices) > 0 and (n_added > max_matches or (max_matches == 0 and not extra_s_cols)):
                        unmatched_sec = sec_renamed[~sec_renamed.index.isin(matched_sec_indices)]
                        all_cols = list(prim.columns) + [c for c in extra_s_cols if c not in prim.columns]
                        if not unmatched_sec.empty:
                            aligned_unmatched = pd.DataFrame(index=unmatched_sec.index)
                            for c in all_cols:
                                aligned_unmatched[c] = unmatched_sec[c] if c in unmatched_sec.columns else ''
                            cand = pd.concat([cand, aligned_unmatched], ignore_index=True)
                        max_matches = max(n_added, 1)
                        best_single = cand
                if best_single is not None:
                    merged = best_single
        except Exception:
            merged = None

    if merged is None:
        # 共同字段无法置信匹配行身份或存在冲突时，绝不按物理位置盲目补列，直接返回 None 告知调用方独立保留冲突双方数据
        return None

    merged = merged.fillna('')
    merged.attrs = dict(prim.attrs)
    if not merged.attrs.get('label') and sec.attrs.get('label'):
        merged.attrs['label'] = sec.attrs['label']
    if not merged.attrs.get('table_title') and sec.attrs.get('table_title'):
        merged.attrs['table_title'] = sec.attrs['table_title']
    return merged


def _merge_online_and_pdf_tables(online_list, pdf_list):
    """
    智能合并在线高保真表格与本地 PDF / MinerU 提取表格：
    比对数据覆盖度 (shape[0] * shape[1] 以及有效非空数据元胞数)。
    若本地 PDF / MinerU 版本相比在线版本显著具有更多数据行/单元格 (例如在线版本仅为付费墙折叠/预览截断)，
    优先使用本地 PDF 完整表覆盖在线残表，杜绝因盲目保存在线表而丢弃完整本地大表；
    若两端字段互补（例如在线拥有独有列），执行字段合并保留全量列，防止丢列。
    严格核对表格身份，杜绝无表号表误替换不同页已标定表。
    """
    if not online_list:
        return list(pdf_list) if pdf_list else []
    if not pdf_list:
        return list(online_list)

    for o_item in online_list:
        if hasattr(o_item, 'attrs') and not o_item.attrs.get('source'):
            o_item.attrs['source'] = 'online'
    for p_item in pdf_list:
        if hasattr(p_item, 'attrs') and not p_item.attrs.get('source'):
            p_item.attrs['source'] = 'pdf'

    combined = list(online_list)

    def _calc_cells(df):
        if df is None or df.empty:
            return 0
        return sum(1 for v in df.values.flatten() if pd.notna(v) and str(v).strip() not in ('', 'nan', 'none'))

    for p_df in pdf_list:
        p_lbl = p_df.attrs.get('label') or p_df.attrs.get('table_title')
        fmt_p_lbl = format_table_label(p_lbl) if p_lbl else None
        p_page = p_df.attrs.get('page_idx')

        matched_idx = None
        if fmt_p_lbl:
            # 1. 显式表号精准匹配
            for idx, o_df in enumerate(combined):
                o_lbl = o_df.attrs.get('label') or o_df.attrs.get('table_title')
                fmt_o_lbl = format_table_label(o_lbl) if o_lbl else None
                if fmt_o_lbl and fmt_o_lbl == fmt_p_lbl:
                    matched_idx = idx
                    break
        else:
            # 2. PDF 表格无明确表号：必须核对列重合度与页面归属，严禁无条件认定或跨页顶替
            for idx, o_df in enumerate(combined):
                o_lbl = o_df.attrs.get('label') or o_df.attrs.get('table_title')
                fmt_o_lbl = format_table_label(o_lbl) if o_lbl else None
                o_page = o_df.attrs.get('page_idx')

                # 若两端均明确指定了页码且不在同一页，绝不可匹配
                if p_page is not None and o_page is not None and p_page != o_page:
                    continue

                o_cells = set(
                    str(v).strip().lower() for v in o_df.values.flatten()
                    if pd.notna(v) and str(v).strip().lower() not in ('', 'nan', 'none', '-', '—')
                )
                p_cells = set(
                    str(v).strip().lower() for v in p_df.values.flatten()
                    if pd.notna(v) and str(v).strip().lower() not in ('', 'nan', 'none', '-', '—')
                )
                shared_cells = o_cells & p_cells

                o_cols = set(str(c).lower().strip() for c in o_df.columns if not str(c).startswith(('Unnamed', 'Col')) and str(c).strip() not in ('', 'nan', 'none'))
                p_cols = set(str(c).lower().strip() for c in p_df.columns if not str(c).startswith(('Unnamed', 'Col')) and str(c).strip() not in ('', 'nan', 'none'))
                
                cols_match = False
                if o_cols and p_cols:
                    jaccard = len(o_cols & p_cols) / max(1, len(o_cols | p_cols))
                    is_subset = o_cols.issubset(p_cols) or p_cols.issubset(o_cols)
                    if jaccard >= 0.5 or (is_subset and len(o_cols & p_cols) >= 2):
                        cols_match = True

                # 若在线表有明确标签（如 Table 1），而 PDF 表格无明确表号：
                # 严禁仅凭列名重合就顶替已标定表！必须满足数据真值重合
                if fmt_o_lbl and not fmt_p_lbl:
                    cell_overlap_ratio = len(shared_cells) / max(1, min(len(o_cells), len(p_cells))) if (o_cells and p_cells) else 0.0
                    if cols_match and len(shared_cells) >= 2 and cell_overlap_ratio >= 0.25:
                        matched_idx = idx
                        break
                elif not fmt_o_lbl and not fmt_p_lbl:
                    cell_overlap_ratio = len(shared_cells) / max(1, min(len(o_cells), len(p_cells))) if (o_cells and p_cells) else 0.0
                    if cols_match and (cell_overlap_ratio >= 0.25 or (p_page is not None and o_page is not None and p_page == o_page)):
                        matched_idx = idx
                        break

        if matched_idx is not None:
            o_df = combined[matched_idx]
            p_rows, o_rows = len(p_df), len(o_df)
            p_cells = _calc_cells(p_df)
            o_cells = _calc_cells(o_df)
            p_coverage = p_df.shape[0] * p_df.shape[1]
            o_coverage = o_df.shape[0] * o_df.shape[1]

            tbl_desc = fmt_p_lbl or o_df.attrs.get('label') or "Table"

            # 字段互补分析：检查是否有任一方拥有对方缺失的有效列
            o_cols_clean = set(str(c).lower().strip() for c in o_df.columns if not str(c).startswith(('Unnamed', 'Col')) and str(c).strip() not in ('', 'nan', 'none'))
            p_cols_clean = set(str(c).lower().strip() for c in p_df.columns if not str(c).startswith(('Unnamed', 'Col')) and str(c).strip() not in ('', 'nan', 'none'))
            common_cols = o_cols_clean & p_cols_clean
            extra_o_cols = [c for c in o_df.columns if str(c).lower().strip() in (o_cols_clean - p_cols_clean)] if common_cols else []
            extra_p_cols = [c for c in p_df.columns if str(c).lower().strip() in (p_cols_clean - o_cols_clean)] if common_cols else []

            # 如果本地 PDF 表格列数显著更多 (例如大于等于在线列数的 2 倍) 或容积显著更大且缺乏共同字段重合，视为本地完整表覆盖截断的在线残表
            is_p_overwhelming = (
                (p_df.shape[1] >= 2 * o_df.shape[1]) or
                (p_coverage >= 2 * o_coverage and p_df.shape[1] > o_df.shape[1])
            )

            if extra_o_cols:
                # 在线表具有 PDF 缺失的补充列：绝不能因 PDF 列数更多而跳过补列丢弃在线独有字段，优先互补合并
                merged = _merge_complementary_tables(p_df, o_df)
                if merged is not None:
                    print(f"[Merge] 本地 PDF 表格 [{tbl_desc}] 与在线表格字段互补，合并保留在线补充列 {extra_o_cols} 并保留全量行 ({len(merged)}行/{len(merged.columns)}列)")
                    combined[matched_idx] = merged
                else:
                    # 无法置信对齐行身份，保留冲突双方独立数据，杜绝数据错配与遗失
                    print(f"[Merge] 本地 PDF 表格 [{tbl_desc}] 与在线表格无法置信匹配行身份，保留双方独立版本")
                    combined.append(p_df)
            elif extra_p_cols:
                # 在线行数或行内容与 PDF 字段互补：以在线为主干，补充 PDF 独有列并保留全量行
                merged = _merge_complementary_tables(o_df, p_df)
                if merged is not None:
                    print(f"[Merge] 在线表格 [{tbl_desc}] 与本地 PDF 表格字段互补，合并保留 PDF 补充列 {extra_p_cols} 并保留全量行 ({len(merged)}行/{len(merged.columns)}列)")
                    combined[matched_idx] = merged
                else:
                    print(f"[Merge] 在线表格 [{tbl_desc}] 与本地 PDF 表格无法置信匹配行身份，保留双方独立版本")
                    combined.append(p_df)
            else:
                # 结构完全包含或一方在所有维度显著更全
                is_p_better = (
                    is_p_overwhelming or
                    (not extra_o_cols and p_rows > o_rows) or
                    (not extra_o_cols and p_cells > o_cells) or
                    (not extra_o_cols and p_coverage > o_coverage and p_cells >= o_cells)
                )
                if is_p_better:
                    # 若双方存在共同字段，优先尝试互补合并保留双方全量行（杜绝丢弃在线独有样本）
                    merged = None
                    if common_cols:
                        merged = _merge_complementary_tables(p_df, o_df)
                    if merged is not None:
                        print(f"[Merge] 本地 PDF 表格 [{tbl_desc}] 覆盖在线残表并合并保留双方全量行 ({len(merged)}行/{len(merged.columns)}列)")
                        combined[matched_idx] = merged
                    else:
                        print(f"[Merge] 本地 PDF 表格 [{tbl_desc}] 数据覆盖显著更全 ({p_rows}行/{p_cells}单元格/容积{p_coverage} vs 在线{o_rows}行/{o_cells}单元格/容积{o_coverage})，覆盖截断的在线表")
                        p_df_copy = p_df.copy()
                        p_df_copy.attrs = dict(p_df.attrs)
                        if not p_df_copy.attrs.get('label') and o_df.attrs.get('label'):
                            p_df_copy.attrs['label'] = o_df.attrs['label']
                        if not p_df_copy.attrs.get('table_title') and o_df.attrs.get('table_title'):
                            p_df_copy.attrs['table_title'] = o_df.attrs['table_title']
                        combined[matched_idx] = p_df_copy
        else:
            combined.append(p_df)

    return combined


def _do_process_single_pdf(pdf_path, orig_pdf_path, args, is_batch=False, plan=None, candidate_pages=None, remediated=None):
    output_path = args.output
    filename_base = os.path.splitext(os.path.basename(orig_pdf_path))[0]
    safe_base = make_safe_filename(filename_base)
    user_single_file = getattr(args, 'single_file', False)

    if output_path.endswith(".xlsx"):
        target_output = output_path
        single_file = True
    else:
        parent_name = os.path.basename(os.path.normpath(output_path))
        from difflib import SequenceMatcher
        sim = SequenceMatcher(None, parent_name.lower(), safe_base.lower()).ratio()
        if sim > 0.5:
            target_dir = output_path
        else:
            target_dir = os.path.join(output_path, safe_base)
        os.makedirs(target_dir, exist_ok=True)

        if user_single_file:
            target_output = os.path.join(target_dir, f"{safe_base}.xlsx")
            single_file = True
        else:
            target_output = target_dir
            single_file = False

    if getattr(args, 'enable_llm', False):
        os.environ["LLM_TABLE_REASONER_ENABLED"] = "1"

    extracted_online = False
    online_only = getattr(args, 'online_only', False)
    pdf_only = getattr(args, 'pdf_only', False)
    online_dfs = []
    online_labels = set()
    online_skip_labels = set()

    # 1. 检查是否为 2020 年之前的中文文献 -> 直接跳过网页抓取，直通 PDF 管线
    if is_pre_2020_chinese_paper(orig_pdf_path, plan):
        print(f"[Online] 检测到 2020 年前中文文献，知网/万方无在线 HTML 结构，直接进入 PDF 提取管线")
    elif not pdf_only and online is not None:
        try:
            print("[Online] Attempting online HTML table extraction...")
            res = online.extract_tables_online(
                pdf_path=orig_pdf_path,
                table_idx=getattr(args, 'table_idx', 'all'),
                db_path=getattr(args, 'db_path', None),
                api_key=getattr(args, 'firecrawl_key', None),
                cnki_strategy=getattr(args, 'cnki_strategy', 'auto'),
                headed=getattr(args, 'headed', False),
                user_data_dir=getattr(args, 'user_data_dir', None),
                pre_resolved=plan,
                race=not getattr(args, 'sequential', False),
                cancel_event=None,
            )
            if res and isinstance(res, tuple):
                online_dfs = list(res[0] or [])
                online_title = res[1]
            elif isinstance(res, list):
                online_dfs = list(res)
                online_title = None
            if online_dfs:
                expected_labels, expected_count = scan_pdf_table_expected_labels(orig_pdf_path)

                # 收集已获取的在线表格标签
                online_labels = set()
                online_skip_labels = set()
                for o_df in online_dfs:
                    lbl = o_df.attrs.get('label') or o_df.attrs.get('table_title')
                    if lbl:
                        check_lbl = format_table_label(lbl)
                        if check_lbl:
                            online_labels.add(check_lbl)
                            online_skip_labels.add(make_safe_filename(check_lbl))

                missing_labels = (expected_labels - online_labels) if expected_labels else set()

                # 检查 PDF 是否为扫描件/含扫描页，以及是否有潜在表格线框或矢量结构
                has_pdf_table_cues = False
                has_scanned_page = False
                try:
                    try:
                        from .pdf_table_extractor import has_text_layer
                    except ImportError:
                        from pdf_table_extractor import has_text_layer
                    if not has_text_layer(orig_pdf_path):
                        has_scanned_page = True
                except Exception:
                    pass

                try:
                    import pymupdf as fitz
                    doc = fitz.open(orig_pdf_path)
                    for page in doc:
                        txt = page.get_text("text").strip()
                        imgs = page.get_images()
                        # 扫描页缺少文本层，无法通过字符排查表格，属于“完整性未知”
                        if len(txt) <= 50 and (len(imgs) > 0 or not txt):
                            has_scanned_page = True
                        tabs = page.find_tables()
                        text_tabs = page.find_tables(strategy="text") if not (tabs and tabs.tables) else None
                        if (tabs and tabs.tables) or (text_tabs and text_tabs.tables) or len(page.get_drawings()) > 5:
                            has_pdf_table_cues = True
                    doc.close()
                except Exception:
                    has_scanned_page = True

                is_complete = False
                has_sparse_or_truncated = False
                for o_df in online_dfs:
                    if o_df is None or len(o_df) <= 3:
                        has_sparse_or_truncated = True
                        break
                    non_empty_count = sum(
                        1 for v in o_df.values.flatten()
                        if pd.notna(v) and str(v).strip() not in ('', 'nan', 'none', '-', '—')
                    )
                    density = non_empty_count / max(1, o_df.size)
                    if non_empty_count < 6 or density < 0.25:
                        has_sparse_or_truncated = True
                        break

                if has_scanned_page or has_sparse_or_truncated:
                    # 扫描页或在线预览截断表，完整性未知，严禁提前判定为完整，必须由本地管线补充核查
                    is_complete = False
                elif expected_labels:
                    is_complete = (len(missing_labels) == 0 and len(online_dfs) >= expected_count)
                elif expected_count > 0:
                    is_complete = (len(online_dfs) >= expected_count)
                else:
                    # expected_count == 0 且无扫描页：若既无明确表声明也无矢量表格线索，视为完整
                    is_complete = (not has_pdf_table_cues and len(online_dfs) > 0)

                if not is_complete:
                    if has_scanned_page:
                        print(f"[Online Warning] PDF 包含扫描页或无文本层，表格完整性未知，将由本地管线补充核查。")
                    elif missing_labels:
                        print(f"[Online Warning] 在线提取获得 {len(online_dfs)} 个表，缺失预期表号: {sorted(list(missing_labels))} (全文共检测到 {expected_count} 个表声明)。")
                    else:
                        print(f"[Online Warning] 在线提取获得 {len(online_dfs)} 个表，少于全文预期数量 {expected_count} 个表。")
                else:
                    print(f"[Online] Got {len(online_dfs)} tables via online HTML source (覆盖全部 {len(online_labels)} 个预期表号). Saving...")

                # 始终保存已成功抓取的在线高保真表格
                save_ok = save_tables_to_excel(online_dfs, target_output,
                                              getattr(args, 'headers', None), single_file,
                                              skip_supplementary=getattr(args, 'skip_supplementary', False))
                if save_ok:
                    extracted_online = True
                    print(f"[Online] Successfully extracted tables via online HTML source -> {target_output}")
                    if online_only:
                        _trigger_zotero_sync(orig_pdf_path, target_output, online_dfs, args)
                        return True
                    print(f"[Online -> PDF Supplement] 已保留 {len(online_dfs)} 个在线表格，将由本地 PDF 管线补充剩余缺失表格...")
            else:
                print("[Online] Online extraction returned no tables; falling back to PDF.")
        except Exception as e:
            print(f"[Online Warning] Online extraction failed or skipped: {e}")

    if online_only:
        print("[Mode] --online-only set. Skipping PDF fallback.")
        if extracted_online:
            _trigger_zotero_sync(orig_pdf_path, target_output, online_dfs, args)
        return extracted_online

    # 2. PDF 本地提取模式（结构化优先，OCR 兜底）
    print("[PDF] Executing structured table extraction pipeline...")

    # 本地离线结构化文件支持
    structured = getattr(args, 'structured_file', None)
    if structured and os.path.exists(structured):
        try:
            with open(structured, encoding='utf-8') as f:
                content = f.read()
            vlm_dfs = parse_structured_vlm_content(content)
            if vlm_dfs:
                print(f"[PDF -> Structured] Extracted {len(vlm_dfs)} structured tables from local file.")
                combined_struct_dfs = _merge_online_and_pdf_tables(online_dfs, vlm_dfs)

                if save_tables_to_excel(combined_struct_dfs, target_output, getattr(args, 'headers', None), single_file,
                                        skip_supplementary=getattr(args, 'skip_supplementary', False)):
                    _trigger_zotero_sync(orig_pdf_path, target_output, combined_struct_dfs, args)
                    return True
        except Exception as e:
            print(f"[PDF -> Structured Warning] Failed to parse structured file: {e}")

    # 使用结构化提取管线
    pdf_extracted = False
    if extract_tables_from_pdf is not None:
        try:
            results, logs = extract_tables_from_pdf(
                pdf_path,
                use_ocr_fallback=True,
                candidate_pages=candidate_pages,
                remediated=remediated,
            )
            for log_line in logs:
                print(f"  [Pipeline] {log_line}")

            if results:
                dfs = [r['df'] for r in results]
                extractors = [r['df'].attrs.get('extractor', 'unknown') for r in results]
                print(f"[PDF] 提取到 {len(dfs)} 个表格 (extractors: {set(extractors)})")
                
                # 在线高保真表格与 PDF 补漏表格智能合并（比对覆盖度，杜绝盲目丢弃完整本地表）
                combined_dfs = _merge_online_and_pdf_tables(online_dfs, dfs)

                if save_tables_to_excel(combined_dfs, target_output, getattr(args, 'headers', None), single_file,
                                        skip_supplementary=getattr(args, 'skip_supplementary', False)):
                    print(f"[PDF] Successfully exported tables to: {target_output}")
                    pdf_extracted = True
                    _trigger_zotero_sync(orig_pdf_path, target_output, combined_dfs, args)
                    return True
            else:
                print("[PDF] 结构化管线未提取到表格，尝试 OCR/切图备用管线...")
        except Exception as e:
            print(f"[PDF] 管线异常: {e}，尝试 OCR/切图备用管线...")

    # 当原生结构化管线不可用、未提取到表格或异常失败时，触发 OCR / 切图兜底回退
    if not pdf_extracted:
        print("[PDF] 正在进入 PaddleOCR / PP-StructureV3 兜底切图回退管线...")
        if ocr_client:
            try:
                full_md = ocr_client.run_paddleocr_vl(pdf_path, pages=None, cancel_event=None)
                if full_md:
                    vlm_dfs = parse_structured_vlm_content(full_md)
                    combined_vlm_dfs = _merge_online_and_pdf_tables(online_dfs, vlm_dfs)

                    if combined_vlm_dfs and save_tables_to_excel(combined_vlm_dfs, target_output, getattr(args, 'headers', None), single_file,
                                                                        skip_supplementary=getattr(args, 'skip_supplementary', False)):
                        print(f"[PDF -> PaddleOCR-VL-1.6] Successfully exported tables to: {target_output}")
                        _trigger_zotero_sync(orig_pdf_path, target_output, combined_vlm_dfs, args)
                        return True
            except Exception as e:
                print(f"[PDF -> VLM Notice] VLM execution note: {e}")

        try:
            print("[PDF -> PP-StructureV3] 正在使用 PP-StructureV3 / 切图识别表格并导出 Excel...")
            if export_crops_to_excel(pdf_path, target_output, existing_dfs=online_dfs):
                print(f"[PDF -> PP-StructureV3] 已成功识别并导出表格至: {target_output}")
                exported_dfs = list(online_dfs) if online_dfs else []
                _trigger_zotero_sync(orig_pdf_path, target_output, exported_dfs, args)
                return True
        except Exception as e:
            print(f"[PDF -> PP-StructureV3 Notice] Export notice: {e}")

    # 如果在线已经提取并保存了部分表格，即使本地补充未抓取到更多表格，也视为任务成功
    if extracted_online:
        _trigger_zotero_sync(orig_pdf_path, target_output, online_dfs, args)
        return True

    return False


def main():
    parser = argparse.ArgumentParser(description="Zotero Table Extractor - Extract tables from PDF/HTML to Excel")
    parser.add_argument("--pdf", required=True, help="Path to a PDF file, a directory of PDFs, or a .txt file containing PDF paths")
    parser.add_argument("--output", required=True, help="Path to save the output Excel file (or directory for batch mode)")
    parser.add_argument("--headers", help="Comma-separated custom column names")
    parser.add_argument("--table-idx", default="all", help="Table index to extract (0-based integer, 'first', or 'all')")
    
    parser.add_argument("--db-path", help="Path to zotero.sqlite database (optional)")
    parser.add_argument("--firecrawl-key", help="Firecrawl API Key (optional)")
    parser.add_argument("--online-only", action="store_true", help="Extract online only, do not fall back to PDF")
    parser.add_argument("--pdf-only", action="store_true", help="Skip online extraction and extract from PDF directly")
    parser.add_argument("--skip-supplementary", action="store_true", help="Skip extraction of supplementary/appendix tables")
    parser.add_argument("--single-file", action="store_true", help="Save all extracted tables into a single Excel file with multiple sheets")
    parser.add_argument("--enable-llm", action="store_true", help="Enable LLM table reasoner for complex hierarchical headers and recovery")
    
    # 生态与增强引擎联动选项 (可选开关，默认关闭或自动)
    parser.add_argument("--sync-zotero", action="store_true", help="Enable PyZotero & Zotero-Better-Notes ecosystem sync (attach .xlsx and create preview note, default disabled)")
    parser.add_argument("--zotero-api-key", help="Zotero Web API Key for PyZotero sync")
    parser.add_argument("--zotero-user-id", help="Zotero User ID / Library ID for PyZotero sync")
    parser.add_argument("--ocr-engine", choices=["auto", "paddleocr", "docling", "mineru"], default="auto", help="OCR engine preference")

    parser.add_argument("--cnki-strategy", choices=["auto", "scrapling", "playwright"], default="auto",
                        help="CNKI extraction strategy: 'auto' (try both), 'scrapling', 'playwright'")
    parser.add_argument("--headed", action="store_true", help="Launch Playwright in headed mode")
    parser.add_argument("--user-data-dir", help="Path to browser user profile data directory")
    parser.add_argument("--workers", type=int, default=4, help="Number of worker threads for batch mode")
    parser.add_argument("--sequential", action="store_true", help="Disable single-item race mode")
    parser.add_argument("--doi", help="Pre-resolved DOI")
    parser.add_argument("--url", help="Pre-resolved URL")
    parser.add_argument("--title", help="Pre-resolved paper Title")
    
    args = parser.parse_args()
    
    if getattr(args, 'ocr_engine', None) and args.ocr_engine != 'auto':
        os.environ["OCR_ENGINE"] = args.ocr_engine

    pdf_input = args.pdf
    pdf_list = []

    if os.path.isfile(pdf_input):
        if pdf_input.lower().endswith(".pdf"):
            pdf_list = [pdf_input]
        elif pdf_input.lower().endswith(".txt"):
            try:
                with open(pdf_input, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, start=1):
                        line_str = line.strip().strip('"').strip("'")
                        if not line_str or line_str.startswith("#"):
                            continue
                        if os.path.isfile(line_str) and line_str.lower().endswith(".pdf"):
                            pdf_list.append(line_str)
                        elif line_str.startswith(("http://", "https://")):
                            pdf_list.append(line_str)
                        else:
                            print(f"[Warning] .txt 清单第 {line_no} 行路径不存在或非 PDF，已跳过: {line_str}")
            except Exception as e:
                print(f"Error reading path list file {pdf_input}: {e}")
                sys.exit(1)
        else:
            print(f"Error: 不支持的文件格式 '{os.path.splitext(pdf_input)[1]}'。本技能仅支持 PDF 文件 (.pdf)、清单文件 (.txt) 或 PDF 目录。")
            sys.exit(1)
    elif os.path.isdir(pdf_input):
        import glob
        pdf_list = sorted(glob.glob(os.path.join(pdf_input, "**", "*.pdf"), recursive=True))
        if not pdf_list:
            pdf_list = sorted(glob.glob(os.path.join(pdf_input, "*.pdf")))
    else:
        # 支持直接传入在线文献 URL
        if pdf_input.startswith(("http://", "https://")):
            pdf_list = [pdf_input]
        else:
            print(f"Error: 指定的路径或文件不存在: {pdf_input}")
            sys.exit(1)

    if not pdf_list:
        print(f"Error: 未在指定输入路径找到任何有效 PDF 文件: {pdf_input}")
        sys.exit(1)

    if len(pdf_list) == 1:
        target = pdf_list[0]
        print(f"Processing PDF target: {target}")
        success = process_single_pdf(target, args)
        print(f"\nCompleted! Processed 1 file: {'Success' if success else 'Failed'}")
    else:
        workers = getattr(args, 'workers', 4) or 4
        print(f"=== Starting batch extraction of {len(pdf_list)} PDF files (workers={workers}) ===")
        success_count = 0
        fail_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            future_to_pdf = {executor.submit(process_single_pdf, p, args, is_batch=True): p for p in pdf_list}
            for fut in concurrent.futures.as_completed(future_to_pdf):
                p = future_to_pdf[fut]
                try:
                    res = fut.result()
                    if res:
                        success_count += 1
                        print(f"[Batch Success] {os.path.basename(p)}")
                    else:
                        fail_count += 1
                        print(f"[Batch Failed] {os.path.basename(p)}")
                except Exception as e:
                    fail_count += 1
                    print(f"[Batch Error] {os.path.basename(p)}: {e}")
        print(f"\nBatch Completed! Total: {len(pdf_list)}, Success: {success_count}, Failed: {fail_count}")

if __name__ == "__main__":
    main()
