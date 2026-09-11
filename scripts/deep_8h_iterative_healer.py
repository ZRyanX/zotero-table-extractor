"""
deep_8h_iterative_healer.py - 8小时持续深度自修复与动态质检演进引擎
对全库 375 篇存在微观缺陷与未达满分的含表文献进行逐篇重新提取、AI 视觉修复、跨页去重并回写数据库。
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
import glob
import time
import json
import sqlite3
import re
import fitz
import pandas as pd
from typing import List, Dict, Any

# Enable unbuffered I/O
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Add scripts directory to path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import pdf_table_extractor as pte
from table_validator import scan_pdf_table_declarations
from excel_export import save_tables_to_excel, format_table_label
from targeted_audit_runner import evaluate_table_micro_quality, record_targeted_result, inspect_targeted_paper

DB_PATH = get_targeted_audit_db_path()
OUTPUT_BASE_DIR = get_paper_tables_dir()

def run_deep_iterative_healing():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT pdf_path, title, quality_score, status 
        FROM papers 
        WHERE is_100_pass = 0 
        ORDER BY quality_score ASC
    """)
    papers_to_heal = cursor.fetchall()
    conn.close()

    total = len(papers_to_heal)
    print(f"================================================================================")
    print(f"=== 启动 8 小时超长程真实深度自修复与动态重提引擎: 共 {total} 篇目标文献 ===")
    print(f"=== 预计总运行时长: 4 ~ 8 小时 (采用 PaddleOCR-VL-1.6 逐页精细视觉修复) ===")
    print(f"================================================================================")

    healed_count = 0
    start_time = time.time()

    for idx, (pdf_path, title, old_score, old_status) in enumerate(papers_to_heal, 1):
        if not os.path.exists(pdf_path):
            continue

        paper_start = time.time()
        print(f"\n[{idx}/{total}] 正在对文献执行全量深度重提与 AI 视觉自愈:")
        print(f"  • 文献名: {title}")
        print(f"  • 原质量分: {old_score}% ({old_status})")

        out_dir = os.path.join(OUTPUT_BASE_DIR, title)
        
        try:
            # 1. 重新执行全流程高精提取（包含新加入的跨页去重、列展开与 OCR 兜底）
            results, logs = pte.extract_tables_from_pdf(pdf_path)
            dfs = [r["df"] for r in results]
            
            # 2. 规范化覆写 Excel 与清单文件
            save_tables_to_excel(dfs, out_dir, single_file=False)
            
            # 3. 重新执行 8 维微观质检
            res = inspect_targeted_paper(pdf_path)
            record_targeted_result(res)

            paper_elapsed = time.time() - paper_start
            total_elapsed_min = (time.time() - start_time) / 60.0

            if res["quality_score"] > old_score:
                healed_count += 1
                print(f"  ✅ 修复成功! 质量分: {old_score}% -> {res['quality_score']}% ({res['status']}) | 耗时: {paper_elapsed:.1f}s")
            elif res["is_100_pass"]:
                healed_count += 1
                print(f"  ✅ 满分达标! 质量分: {res['quality_score']}% (100%_PASS) | 耗时: {paper_elapsed:.1f}s")
            else:
                print(f"  ℹ️ 质检复核完成: {res['quality_score']}% ({res['status']}) | 耗时: {paper_elapsed:.1f}s")

            if res["detailed_anomalies"]:
                print(f"  🔍 当前残留微观特征: {res['detailed_anomalies'][:100]}...")

            print(f"  📊 总体进度: 已处理 {idx}/{total} 篇 ({idx/total*100:.1f}%) | 修复提升: {healed_count} 篇 | 累计运行时长: {total_elapsed_min:.1f} 分钟")

        except Exception as e:
            print(f"  ❌ 处理异常: {e}")

        # 每 10 篇自动更新一次报告 Markdown
        if idx % 10 == 0 or idx == total:
            try:
                from targeted_audit_reporter import generate_targeted_audit_report
                generate_targeted_audit_report()
            except Exception:
                pass

    print(f"\n================================================================================")
    print(f"=== 8 小时全量深度自修复全部完成! 共修复优化 {healed_count}/{total} 篇文献 ===")
    print(f"================================================================================")

if __name__ == "__main__":
    run_deep_iterative_healing()
