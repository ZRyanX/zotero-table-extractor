#!/usr/bin/env python3
"""
extract_zotero_table.py — 主入口（CLI）：Zotero/本地 PDF 表格提取与 Excel 导出。

批量模式两阶段执行（预分析 → 并行提取）；单篇任务内 HTML ∥ Paddle 竞速。
整合 DocLayout-YOLO 本地 AI 视觉版面提取与回退保障。
"""
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
import json
import argparse
import threading
import concurrent.futures

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
    from .table_postprocess import parse_structured_vlm_content
    from .excel_export import make_safe_filename, save_tables_to_excel
    from .pdf_tables import get_page_effective_rotation, export_crops_to_excel
    from .pdf_table_extractor import extract_tables_from_pdf
except ImportError:
    from table_postprocess import parse_structured_vlm_content
    from excel_export import make_safe_filename, save_tables_to_excel
    from pdf_tables import get_page_effective_rotation, export_crops_to_excel
    try:
        from pdf_table_extractor import extract_tables_from_pdf
    except ImportError:
        extract_tables_from_pdf = None


def _count_pdf_table_captions(pdf_path: str) -> int:
    """
    轻量级扫描 PDF 文本层，统计表格 caption 数量（如 "表1" "Table 2" "表3.2"）。
    支持长文献（上限 150 页），使用 format_table_label 统一规范化表号，
    用于判断在线提取是否遗漏了表格并驱动本地增量补充。
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
        import pymupdf as fitz
        doc = fitz.open(pdf_path)
        labels = set()
        max_scan_pages = min(150, len(doc))
        for i in range(max_scan_pages):
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
        return len(labels)
    except Exception:
        return 0


def process_single_pdf(pdf_path, args, is_batch=False, plan=None):
    """Processes a single PDF file, extracting tables online or falling back to PDF.

    plan: optional pre-analysis result from batch_planner.analyze_pdf (doi/title/url/is_cnki),
          used to skip redundant metadata resolution and drive parallel racing.
    """
    print(f"\nProcessing: {pdf_path}")
    
    target_pdf_path = pdf_path
    temp_pdf_to_clean = None
    try:
        if os.path.exists(pdf_path) and os.path.isfile(pdf_path):
            import pymupdf as fitz

            try:
                from .pdf_table_extractor import has_text_layer, quick_classify_pdf
            except ImportError:
                from pdf_table_extractor import has_text_layer, quick_classify_pdf
            
            # 使用 pdf-inspector quick_classify_pdf (5~10ms) 进行超快速预检
            clf_res = quick_classify_pdf(pdf_path)
            if clf_res.get('confidence', 0) >= 0.8 and clf_res.get('pdf_type') in ('text_based', 'scanned'):
                is_native = (clf_res['pdf_type'] == 'text_based')
            else:
                is_native = has_text_layer(pdf_path)

            doc = fitz.open(pdf_path)
            page_rotations = [get_page_effective_rotation(p) for p in doc]
            has_rotation = any(r != 0 for r in page_rotations)
            doc.close()

            # 仅对扫描版 PDF 做旋转烘焙；native PDF 的文本层方向已正确，烘焙会破坏文本层
            if has_rotation and not is_native:
                print(f"[PDF] 检测到旋转页面或文字方向 (Baking page rotations)...")
                import tempfile
                fd, temp_pdf_to_clean = tempfile.mkstemp(prefix="baked_rotated_pdf_", suffix=".pdf")
                os.close(fd)
                
                src = fitz.open(pdf_path)
                dst = fitz.open()
                for i, src_page in enumerate(src):
                    rot = page_rotations[i]
                    if rot % 180 != 0:
                        new_w = src_page.rect.height
                        new_h = src_page.rect.width
                    else:
                        new_w = src_page.rect.width
                        new_h = src_page.rect.height
                    
                    pix = src_page.get_pixmap(dpi=150)
                    new_page = dst.new_page(width=new_w, height=new_h)
                    new_page.insert_image(new_page.rect, pixmap=pix)
                
                dst.save(temp_pdf_to_clean)
                dst.close()
                src.close()
                target_pdf_path = temp_pdf_to_clean
                print(f"[PDF] Baked rotated PDF saved to: {target_pdf_path}")
            elif has_rotation and is_native:
                print(f"[PDF] Native PDF 有旋转但保留文本层，跳过烘焙（避免破坏文本层）")

        return _do_process_single_pdf(target_pdf_path, orig_pdf_path=pdf_path, args=args, is_batch=is_batch, plan=plan)
    finally:
        if temp_pdf_to_clean and os.path.exists(temp_pdf_to_clean):
            try:
                os.remove(temp_pdf_to_clean)
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


def _do_process_single_pdf(pdf_path, orig_pdf_path, args, is_batch=False, plan=None):
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
    online_skip_labels = None  # 已由在线提取的表标签，PDF 补充时跳过

    # 1. 检查是否为 2020 年之前的中文文献 -> 直接跳过网页抓取，直通 PDF 管线
    if is_pre_2020_chinese_paper(orig_pdf_path, plan):
        print(f"[Online] 检测到 2020 年前中文文献，知网/万方无在线 HTML 结构，直接进入 PDF 提取管线")
    elif not pdf_only and online is not None:
        try:
            print("[Online] Attempting online HTML table extraction...")
            online_dfs, online_title = online.extract_tables_online(
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
            if online_dfs:
                extracted_online = True
                expected_count = _count_pdf_table_captions(orig_pdf_path)
                if expected_count > len(online_dfs):
                    print(f"[Online Warning] 在线提取获得 {len(online_dfs)} 个表，而 PDF 完整文档中检测到约 {expected_count} 个表 caption。")
                else:
                    print(f"[Online] Got {len(online_dfs)} tables via online HTML source. Saving...")

                # 始终保存已成功抓取的在线高保真表格
                save_ok = save_tables_to_excel(online_dfs, target_output,
                                              getattr(args, 'headers', None), single_file,
                                              skip_supplementary=getattr(args, 'skip_supplementary', False))
                if save_ok:
                    print(f"[Online] Successfully extracted tables via online HTML source -> {target_output}")
                    if online_only or expected_count <= len(online_dfs):
                        return True
                    # 收集已保存的 online 标签，让后续本地 PDF 提取仅补充缺失表格
                    online_skip_labels = set()
                    for o_df in online_dfs:
                        lbl = o_df.attrs.get('label') or o_df.attrs.get('table_title')
                        if lbl:
                            check_lbl = format_table_label(lbl)
                            online_skip_labels.add(make_safe_filename(check_lbl))
                    print(f"[Online -> PDF Supplement] 已保留 {len(online_dfs)} 个在线表格，将由本地 PDF 管线补充剩余缺失表格...")
            else:
                print("[Online] Online extraction returned no tables; falling back to PDF.")
        except Exception as e:
            print(f"[Online Warning] Online extraction failed or skipped: {e}")

    if online_only:
        print("[Mode] --online-only set. Skipping PDF fallback.")
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
                if save_tables_to_excel(vlm_dfs, target_output, getattr(args, 'headers', None), single_file,
                                        skip_supplementary=getattr(args, 'skip_supplementary', False)):
                    return True
        except Exception as e:
            print(f"[PDF -> Structured Warning] Failed to parse structured file: {e}")

    # 使用结构化提取管线
    pdf_extracted = False
    if extract_tables_from_pdf is not None:
        try:
            results, logs = extract_tables_from_pdf(pdf_path, use_ocr_fallback=True)
            for log_line in logs:
                print(f"  [Pipeline] {log_line}")

            if results:
                dfs = [r['df'] for r in results]
                extractors = [r['df'].attrs.get('extractor', 'unknown') for r in results]
                print(f"[PDF] 提取到 {len(dfs)} 个表格 (extractors: {set(extractors)})")
                if save_tables_to_excel(dfs, target_output, getattr(args, 'headers', None), single_file,
                                        skip_labels=online_skip_labels,
                                        skip_supplementary=getattr(args, 'skip_supplementary', False)):
                    print(f"[PDF] Successfully exported tables to: {target_output}")
                    pdf_extracted = True
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
                    if vlm_dfs and save_tables_to_excel(vlm_dfs, target_output, getattr(args, 'headers', None), single_file,
                                                        skip_labels=online_skip_labels,
                                                        skip_supplementary=getattr(args, 'skip_supplementary', False)):
                        print(f"[PDF -> PaddleOCR-VL-1.6] Successfully exported tables to: {target_output}")
                        return True
            except Exception as e:
                print(f"[PDF -> VLM Notice] VLM execution note: {e}")

        try:
            print("[PDF -> PP-StructureV3] 正在使用 PP-StructureV3 / 切图识别表格并导出 Excel...")
            if export_crops_to_excel(pdf_path, target_output):
                print(f"[PDF -> PP-StructureV3] 已成功识别并导出表格至: {target_output}")
                return True
        except Exception as e:
            print(f"[PDF -> PP-StructureV3 Notice] Export notice: {e}")

    # 如果在线已经提取并保存了部分表格，即使本地补充未抓取到更多表格，也视为任务成功
    if extracted_online:
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
