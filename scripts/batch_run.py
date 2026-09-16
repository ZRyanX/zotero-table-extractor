#!/usr/bin/env python3
"""
batch_run.py — Zotero 全库批量提取入口。

边界说明：
- 本脚本面向「Zotero 全库」场景：遍历 Zotero 文库导出的 PDF 清单，
  每篇文献派生一个独立子进程调用 extract_zotero_table.py
  （子进程隔离避免单篇崩溃/Paddle 异常拖垮整批，--workers 控制并行度）。
- 若只是手工批量处理若干 PDF 文件/文件夹，无需经过 Zotero 数据库，
  直接用主入口的批量模式即可：
    python3 scripts/extract_zotero_table.py --pdf <目录或清单.txt> --output <目录> --workers N
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


import os
import sys
import json
import re
import unicodedata
import subprocess
import tempfile
import time
import argparse
import threading
import concurrent.futures
from difflib import SequenceMatcher

try:
    import common
except ImportError:
    common = None

# ---------------------------------------------------------------------------
# 路径默认值（跨平台：main() 启动时按 CLI > config > 平台默认重新解析，
# macOS 下保持历史默认向后兼容）
# ---------------------------------------------------------------------------
_LEGACY_PAPER_TABLES = get_paper_tables_dir()  # macOS 保持历史默认，Windows 自动适配

paper_tables = _LEGACY_PAPER_TABLES
zotero_db = os.path.join(os.path.expanduser("~"), "Zotero", "zotero.sqlite")
temp_db = os.path.join(tempfile.gettempdir(), "zotero_copy.sqlite")
log_path = os.path.join(paper_tables, "batch_extraction_log.txt")
zotero_storage_dir = os.path.join(os.path.dirname(zotero_db), "storage")


def _resolve_paths(cli_paper_tables=None, cli_db_path=None):
    """按 CLI > config.json > 平台默认解析关键路径，并派生 storage/log/temp 路径。"""
    global paper_tables, zotero_db, temp_db, log_path, zotero_storage_dir

    config = common.load_config() if common else {}

    if cli_paper_tables:
        paper_tables = cli_paper_tables
    elif config.get("PAPER_TABLES_DIR"):
        paper_tables = config["PAPER_TABLES_DIR"]
    elif not os.path.isdir(_LEGACY_PAPER_TABLES):
        # 非 macOS 历史环境（如 Windows）回退到用户目录
        paper_tables = os.path.join(os.path.expanduser("~"), "paper_tables")

    if cli_db_path:
        zotero_db = cli_db_path
    elif config.get("ZOTERO_DB_PATH"):
        zotero_db = config["ZOTERO_DB_PATH"]
    # 默认已是 ~/Zotero/zotero.sqlite（macOS/Windows 一致）

    zotero_storage_dir = os.path.join(os.path.dirname(zotero_db), "storage")
    temp_db = os.path.join(tempfile.gettempdir(), "zotero_copy.sqlite")
    log_path = os.path.join(paper_tables, "batch_extraction_log.txt")
    os.makedirs(paper_tables, exist_ok=True)

_log_lock = threading.Lock()

def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    with _log_lock:
        print(formatted)
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(formatted + "\n")

def clean_name(name):
    # Remove HTML tags
    name = re.sub(r'<[^>]+>', ' ', name)
    name = unicodedata.normalize('NFKD', name)
    name = "".join([c for c in name if not unicodedata.combining(c)])
    name = re.sub(r'\s*-\s*\d{4}\s*-\s*', ' ', name)
    name = re.sub(r'\s*\b\d{4}\b\s*', ' ', name)
    name = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5]', ' ', name)
    return " ".join(name.lower().split())

def get_similarity_score(folder, title, filename):
    folder_clean = clean_name(folder)
    title_clean = clean_name(title)
    file_clean = clean_name(filename)
    
    has_chinese = any('\u4e00' <= char <= '\u9fa5' for char in folder_clean)
    
    if has_chinese:
        f_set = set(folder_clean.replace(" ", ""))
        t_set = set(title_clean.replace(" ", ""))
        fi_set = set(file_clean.replace(" ", ""))
        if not f_set:
            return 0
        score_t = len(f_set.intersection(t_set)) / len(f_set)
        score_fi = len(f_set.intersection(fi_set)) / len(f_set)
        return max(score_t, score_fi)
    else:
        f_words = set(folder_clean.split())
        t_words = set(title_clean.split())
        fi_words = set(file_clean.split())
        if not f_words:
            return 0
        score_t = len(f_words.intersection(t_words)) / len(f_words)
        score_fi = len(f_words.intersection(fi_words)) / len(f_words)
        sim_t = SequenceMatcher(None, folder_clean, title_clean).ratio()
        sim_fi = SequenceMatcher(None, folder_clean, file_clean).ratio()
        return max(score_t, score_fi, sim_t, sim_fi)

def run_task(idx, total, folder, pdf_path, plan=None, online=False, sequential=False, timeout=300, db_path=None):
    """Runs a single extraction subprocess. Returns (folder, success, elapsed, detail)."""
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extract_zotero_table.py")

    cmd = [
        sys.executable, script_path,
        "--pdf", pdf_path,
        "--skip-supplementary",
        "--output", paper_tables
    ]
    if db_path:
        cmd += ["--db-path", db_path]
    # 预分析结果（doi/url/title）注入子进程，跳过其内部重复的元数据解析
    if plan:
        if plan.get("doi"):
            cmd += ["--doi", str(plan["doi"])]
        if plan.get("url"):
            cmd += ["--url", str(plan["url"])]
        if plan.get("title"):
            cmd += ["--title", str(plan["title"])]
    if sequential:
        cmd.append("--sequential")
    if not online:
        cmd.append("--pdf-only")

    log(f"[{idx}/{total}] Processing paper: {folder}")
    log(f"  PDF Path: {pdf_path}")

    try:
        start_time = time.time()
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - start_time

        if res.returncode == 0:
            log(f"  SUCCESS! Processed in {elapsed:.1f}s. [{folder}]")
            return folder, True, elapsed, ""
        else:
            detail = (res.stderr or "").strip()[-500:]
            log(f"  FAILED! Exit code: {res.returncode}. Stderr: {detail} [{folder}]")
            return folder, False, elapsed, detail
    except subprocess.TimeoutExpired:
        log(f"  FAILED! Timeout ({timeout}s) expired. [{folder}]")
        return folder, False, timeout, "timeout"
    except Exception as e:
        log(f"  FAILED! Error: {e} [{folder}]")
        return folder, False, 0, str(e)


def generate_batch_tasks(paper_tables_dir, pdf_records, resume=False, recheck=False, ignore_folders=None):
    """
    根据 Zotero 数据库检索到的 pdf_records 生成批处理任务列表。
    自动根据文献标题和已有 paper_tables 子目录进行相似度模糊匹配，并支持 --resume 和 --recheck 模式。
    """
    paper_tables_str = str(paper_tables_dir)
    if ignore_folders is None:
        ignore_folders = {
            "MDPI_test", "__pycache__", "temp_albitization_ocr_targeted", "temp_test_auto", "temp_ocr_debug",
            "temp_test_online", "temp_test_xujiashan", "temp_test_xujiashan_ocr", "test_output",
            "test_output_online", "test_output_verification", "test_output_verification_pdf", "test_paiting",
            "test_supp_download_costerfield", "test_yakutia"
        }

    existing_folders = []
    if os.path.exists(paper_tables_str):
        existing_folders = [
            d for d in os.listdir(paper_tables_str)
            if os.path.isdir(os.path.join(paper_tables_str, d)) and not d.startswith(".") and d not in ignore_folders
        ]

    try:
        from excel_export import make_safe_filename
    except ImportError:
        make_safe_filename = lambda s: re.sub(r'[\\/*?:"<>|]', '_', s)[:100].strip()

    tasks = []
    for r in (pdf_records or []):
        fname = r.get('filename') or os.path.basename(r.get('pdf_path', ''))
        fname_base = os.path.splitext(fname)[0]
        safe_folder = make_safe_filename(fname_base)

        matched_folder = safe_folder
        best_score = 0.0
        parent_title = r.get('parent_title', '')
        for ef in existing_folders:
            score = get_similarity_score(ef, parent_title, fname)
            if score > best_score:
                best_score = score
                if score >= 0.7:
                    matched_folder = ef

        folder_path = os.path.join(paper_tables_str, matched_folder)
        has_xlsx = False
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            try:
                has_xlsx = any(f.lower().endswith(".xlsx") and not f.startswith("~$") for f in os.listdir(folder_path))
            except Exception:
                has_xlsx = False

        # --resume: 若已存在提取好的 Excel 产物，跳过该文献
        if resume and has_xlsx:
            continue

        # --recheck: 仅重新检查已有目录但结果为空/未完成的文献
        if recheck:
            if not os.path.exists(folder_path) or has_xlsx:
                continue

        tasks.append({
            "folder": matched_folder,
            "pdf_path": r.get('pdf_path', ''),
            "pdf_record": r,
        })
    return tasks


get_paper_tasks = generate_batch_tasks


def main():
    parser = argparse.ArgumentParser(description="Batch extraction of Zotero library tables (parallel).")
    parser.add_argument("--workers", type=int, default=4,
                        help="并行子进程数量（默认 4；不同出版社的 HTML 抓取与 PaddleOCR 解析可同时进行）")
    try:
        parser.add_argument("--online", action=argparse.BooleanOptionalAction, default=True,
                            help="启用在线 HTML ∥ Paddle 竞速模式（默认启用；可用 --no-online 或 --pdf-only 禁用）")
    except AttributeError:
        parser.add_argument("--online", dest="online", action="store_true", default=True,
                            help="启用在线 HTML ∥ Paddle 竞速模式（默认启用）")
        parser.add_argument("--no-online", dest="online", action="store_false",
                            help="禁用在线提取")
    parser.add_argument("--pdf-only", action="store_true", default=False,
                        help="禁用在线提取，仅用 PaddleOCR 本地识别")
    parser.add_argument("--sequential", action="store_true",
                        help="传递给提取脚本：禁用竞速，走旧版串行管线")
    parser.add_argument("--resume", action="store_true", default=False,
                        help="断点续传：跳过已经提取完成（包含有效 .xlsx 文件）的文献")
    parser.add_argument("--recheck", action="store_true", default=False,
                        help="重新核验：仅对已有子目录重新检查并尝试补充提取（如之前失败或结果为空）")
    parser.add_argument("--timeout", type=int, default=300, help="单篇超时秒数（默认 300）")
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 篇（调试用，0 表示全部）")
    parser.add_argument("--paper-tables", default=None,
                        help="paper_tables 根目录（默认：config PAPER_TABLES_DIR > macOS 历史路径 > ~/paper_tables）")
    parser.add_argument("--db-path", default=None,
                        help="zotero.sqlite 路径（默认：config ZOTERO_DB_PATH > ~/Zotero/zotero.sqlite）")
    args = parser.parse_args()
    if args.pdf_only:
        args.online = False

    _resolve_paths(args.paper_tables, args.db_path)

    log(f"=== STARTING BATCH EXTRACTION PROCESS === (workers={args.workers}, online={args.online}, sequential={args.sequential})")
    log(f"[Paths] paper_tables={paper_tables} | zotero_db={zotero_db}")

    # 1. Query Zotero database for PDFs
    import shutil
    try:
        shutil.copy2(zotero_db, temp_db)
        conn = sqlite3.connect(temp_db) if 'sqlite3' in sys.modules else None
        if not conn:
            import sqlite3
            conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        
        cursor.execute("SELECT fieldID, fieldName FROM fields WHERE fieldName IN ('title', 'DOI');")
        field_map = {name: fid for fid, name in cursor.fetchall()}
        title_fid = field_map.get('title')
        
        cursor.execute("""
            SELECT ia.itemID, ia.parentItemID, ia.path, i.key 
            FROM itemAttachments ia
            JOIN items i ON ia.itemID = i.itemID
            WHERE ia.path LIKE '%.pdf';
        """)
        attachments = cursor.fetchall()
        
        parent_titles = {}
        if title_fid:
            cursor.execute("""
                SELECT id.itemID, idv.value
                FROM itemData id
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                WHERE id.fieldID = ?;
            """, (title_fid,))
            parent_titles = {item_id: val for item_id, val in cursor.fetchall()}
            
        pdf_records = []
        for item_id, parent_id, path, key in attachments:
            filename = None
            if path:
                if path.startswith("storage:"):
                    filename = path[len("storage:"):]
                else:
                    filename = os.path.basename(path)
            if filename:
                actual_path = os.path.join(zotero_storage_dir, key, filename)
                if os.path.exists(actual_path):
                    pdf_records.append({
                        "pdf_path": actual_path,
                        "parent_title": parent_titles.get(parent_id, ""),
                        "key": key,
                        "filename": filename
                    })
        conn.close()
    except Exception as e:
        log(f"Error querying Zotero database: {e}")
        sys.exit(1)
    finally:
        if os.path.exists(temp_db):
            os.remove(temp_db)
            
    log(f"Found {len(pdf_records)} PDFs in Zotero library.")
    
    # 2. 从 Zotero 数据库检索到的 pdf_records 生成提取任务
    raw_tasks = generate_batch_tasks(
        paper_tables_dir=paper_tables,
        pdf_records=pdf_records,
        resume=args.resume,
        recheck=args.recheck
    )
    tasks = [(t["folder"], t["pdf_path"]) for t in raw_tasks]
            
    log(f"Generated {len(tasks)} tasks from {len(pdf_records)} Zotero records for processing (resume={args.resume}, recheck={args.recheck}).")

    if args.limit and args.limit > 0:
        tasks = tasks[:args.limit]
        log(f"--limit active: only first {len(tasks)} tasks will be processed.")

    # 2. [在线模式] 预分析阶段：先并行解析全部 PDF 的 DOI/URL/出版社分组，
    #    随后把每篇的 plan（doi/url/title）经 CLI 参数注入对应子进程，
    #    跳过子进程内部重复的元数据解析；不同出版社网站的 HTML 抓取天然并行。
    plans = {}
    if args.online and tasks:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import batch_planner
            plans = batch_planner.analyze_batch(
                [p for _, p in tasks],
                db_path=zotero_db,
                max_workers=min(8, len(tasks))
            )
            groups = batch_planner.group_by_publisher([p for _, p in tasks], plans)
            log(f"[Planner] 出版社分组: " + ", ".join(f"{k}:{len(v)}" for k, v in sorted(groups.items())))
        except Exception as e:
            log(f"[Planner] 预分析失败（不影响后续提取，逐篇即时解析）: {e}")
            plans = {}

    # 3. Execute extraction in parallel
    success_count = 0
    fail_count = 0
    total = len(tasks)
    start_all = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        fut_map = {
            ex.submit(run_task, idx + 1, total, folder, pdf_path,
                      plan=plans.get(pdf_path), online=args.online, sequential=args.sequential,
                      timeout=args.timeout, db_path=args.db_path): folder
            for idx, (folder, pdf_path) in enumerate(tasks)
        }
        for fut in concurrent.futures.as_completed(fut_map):
            try:
                _folder, ok, _elapsed, _detail = fut.result()
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                log(f"  FAILED! Worker exception: {e}")
                fail_count += 1

    elapsed_all = time.time() - start_all
    log(f"=== BATCH EXTRACTION COMPLETE === (total elapsed {elapsed_all/60:.1f} min)")
    log(f"Total processed: {total}, Success: {success_count}, Failed: {fail_count}")

if __name__ == "__main__":
    main()
