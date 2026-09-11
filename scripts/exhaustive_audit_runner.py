"""
8-Hour Exhaustive Self-Inspection Runner & Deep Diagnostic Engine
针对全库 700+ 篇文献开展多维度细致自检，记录所有表格提取质量指标、根本性问题归因与统计数据。
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
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
import glob
import time
import json
import sqlite3
import re
import fitz
import pandas as pd
from typing import List, Dict, Any, Tuple

# Add scripts directory to path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import pdf_table_extractor as pte
from table_validator import scan_pdf_table_declarations, validate_native_table_dataframe
from excel_export import save_tables_to_excel, TABLE_LABEL_RE, format_table_label
from common import is_metadata_table, is_supplementary
from excel_export import make_safe_filename

DB_PATH = get_audit_db_path("audit_inspection_8h.db")
OUTPUT_BASE_DIR = get_paper_tables_dir()
STORAGE_DIR = get_zotero_storage_dir()

def init_audit_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS papers (
        pdf_path TEXT PRIMARY KEY,
        title TEXT,
        num_pages INTEGER,
        doc_type TEXT,
        declared_tables_json TEXT,
        num_declared INTEGER,
        extracted_tables_json TEXT,
        num_extracted INTEGER,
        status TEXT,
        error_msg TEXT,
        is_100_pass INTEGER DEFAULT 0,
        missing_tables_json TEXT,
        extra_tables_json TEXT,
        cell_scramble_flags TEXT,
        continuation_flags TEXT,
        prose_flags TEXT,
        header_flags TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def classify_doc_type(pdf_path: str, doc: fitz.Document) -> str:
    if len(doc) == 0:
        return "empty"
    # Check if scanned
    first_few_pages = [doc[i].get_text("text") for i in range(min(5, len(doc)))]
    total_text_len = sum(len(t.strip()) for t in first_few_pages)
    if total_text_len < 100:
        return "scanned_image"
    if len(doc) >= 40:
        return "long_thesis"
    # Check if double column
    sample_page = doc[min(1, len(doc)-1)]
    blocks = sample_page.get_text("blocks")
    x0_coords = [b[0] for b in blocks if len(b[4].strip()) > 30]
    if len(x0_coords) >= 4:
        min_x, max_x = min(x0_coords), max(x0_coords)
        if max_x - min_x > 150:
            return "double_column_journal"
    return "single_column_journal"

def inspect_paper_tables(pdf_path: str) -> Dict[str, Any]:
    """
    Performs full 6-dimensional deep quality inspection on a single paper.
    """
    pdf_filename = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = os.path.join(OUTPUT_BASE_DIR, pdf_filename)
    try:
        doc = fitz.open(pdf_path)
        num_pages = len(doc)
        doc_type = classify_doc_type(pdf_path, doc)
    except Exception as e:
        return {
            "pdf_path": pdf_path,
            "title": pdf_filename,
            "num_pages": 0,
            "doc_type": "corrupted_pdf",
            "declared_tables_json": "[]",
            "num_declared": 0,
            "extracted_tables_json": "[]",
            "num_extracted": 0,
            "status": "CORRUPTED_PDF",
            "error_msg": str(e),
            "is_100_pass": 0,
            "missing_tables_json": "[]",
            "extra_tables_json": "[]",
            "cell_scramble_flags": f"Corrupted/Truncated PDF: {e}",
            "continuation_flags": "",
            "prose_flags": "",
            "header_flags": ""
        }
    
    # 1. Ground truth declarations
    declarations_map = scan_pdf_table_declarations(pdf_path)
    declared_labels = []
    for p_idx, caps in declarations_map.items():
        for cap in caps:
            if not cap.get("is_continuation"):
                declared_labels.append(format_table_label(cap["label"]))
    
    # Paper title / basename
    pdf_filename = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = os.path.join(OUTPUT_BASE_DIR, pdf_filename)
    
    # Check if we need to re-extract or if files already exist
    xlsx_files = sorted(glob.glob(os.path.join(out_dir, "*.xlsx")))
    manifest_path = os.path.join(out_dir, "table_captions.txt")
    
    # If no output or incomplete, perform extraction
    if not os.path.exists(out_dir) or not os.path.exists(manifest_path) or (len(declared_labels) > 0 and len(xlsx_files) == 0):
        try:
            results, logs = pte.extract_tables_from_pdf(pdf_path)
            dfs = [r["df"] for r in results]
            save_tables_to_excel(dfs, out_dir, single_file=False)
            xlsx_files = sorted(glob.glob(os.path.join(out_dir, "*.xlsx")))
        except Exception as e:
            doc.close()
            return {
                "pdf_path": pdf_path,
                "title": pdf_filename,
                "num_pages": num_pages,
                "doc_type": doc_type,
                "declared_tables_json": json.dumps(declared_labels, ensure_ascii=False),
                "num_declared": len(declared_labels),
                "extracted_tables_json": json.dumps([], ensure_ascii=False),
                "num_extracted": 0,
                "status": "EXTRACTION_ERROR",
                "error_msg": str(e),
                "is_100_pass": 0,
                "missing_tables_json": json.dumps(declared_labels, ensure_ascii=False),
                "extra_tables_json": "[]",
                "cell_scramble_flags": "",
                "continuation_flags": "",
                "prose_flags": "",
                "header_flags": ""
            }

    # 2. Inspect generated Excel files on disk
    xlsx_labels = []
    extracted_details = []
    
    cell_scrambles = []
    continuation_issues = []
    prose_issues = []
    header_issues = []
    
    for x in xlsx_files:
        base_name = os.path.basename(x)
        if base_name.startswith("._"):
            continue
        table_lbl = os.path.splitext(base_name)[0]
        xlsx_labels.append(table_lbl)
        
        try:
            df = pd.read_excel(x)
            extracted_details.append({
                "label": table_lbl,
                "shape": list(df.shape),
                "cols": [str(c) for c in df.columns[:5]]
            })
            
            # --- 6-Dimension Deep Diagnostics on this DataFrame ---
            
            # Dim 3: Cell Scramble / Column Shredding / Single Column Collapsing
            if df.shape[1] <= 1 and df.shape[0] > 3:
                cell_scrambles.append(f"{table_lbl}: Single-column collapsed (shape={df.shape})")
            
            total_cells = max(1, df.size)
            empty_cells = df.isna().sum().sum()
            if (empty_cells / total_cells) > 0.65 and df.shape[0] > 4 and df.shape[1] > 3:
                cell_scrambles.append(f"{table_lbl}: High sparsity / empty cell ratio ({empty_cells/total_cells:.2f})")
            
            # Dim 4: Continuation Stitching
            first_cols = [str(c).strip().lower() for c in df.columns if str(c).strip()]
            for r_idx in range(1, len(df)):
                row_vals = [str(v).strip().lower() for v in df.iloc[r_idx] if str(v).strip() and str(v) != 'nan']
                if len(row_vals) >= 3 and any(v in first_cols for v in row_vals):
                    match_count = sum(1 for v in row_vals if v in first_cols)
                    if match_count >= 3:
                        continuation_issues.append(f"{table_lbl}: Embedded duplicate header row at line {r_idx}")
                        break
            
            # Dim 5: Prose contamination
            prose_cell_count = 0
            for val in df.values.flatten():
                v_str = str(val).strip()
                if any(p in v_str for p in ['。', '；', '！', '？']) or (len(v_str) > 35 and '，' in v_str):
                    prose_cell_count += 1
            if (prose_cell_count / total_cells) > 0.30:
                prose_issues.append(f"{table_lbl}: High prose contamination ratio ({prose_cell_count/total_cells:.2f})")
                
            # Dim 6: Header preservation
            unnamed_headers = sum(1 for c in df.columns if str(c).startswith("Unnamed:") or str(c).startswith("Col_"))
            if unnamed_headers == len(df.columns) and len(df.columns) >= 3:
                header_issues.append(f"{table_lbl}: 100% Unnamed/anonymous headers")
                
        except Exception as e:
            cell_scrambles.append(f"{table_lbl}: Read Excel error ({e})")

    # Compare Declared vs Extracted
    declared_set = set(declared_labels)
    extracted_set = set(xlsx_labels)
    
    missing_tables = sorted(list(declared_set - extracted_set))
    extra_tables = sorted(list(extracted_set - declared_set))
    
    # Verify manifest
    has_valid_manifest = os.path.exists(manifest_path) and os.path.getsize(manifest_path) > 0
    
    is_100_pass = (
        len(missing_tables) == 0 and
        len(cell_scrambles) == 0 and
        len(continuation_issues) == 0 and
        len(prose_issues) == 0 and
        (len(declared_labels) == 0 or len(xlsx_labels) > 0) and
        (has_valid_manifest or len(xlsx_labels) == 0)
    )
    
    status = "100%_PASS" if is_100_pass else "ANOMALY_FLAGGED"
    if len(declared_labels) == 0 and len(xlsx_labels) == 0:
        status = "NO_TABLES_IN_DOC"
        is_100_pass = 1
        
    doc.close()
    return {
        "pdf_path": pdf_path,
        "title": pdf_filename,
        "num_pages": num_pages,
        "doc_type": doc_type,
        "declared_tables_json": json.dumps(declared_labels, ensure_ascii=False),
        "num_declared": len(declared_labels),
        "extracted_tables_json": json.dumps(extracted_details, ensure_ascii=False),
        "num_extracted": len(xlsx_labels),
        "status": status,
        "error_msg": "",
        "is_100_pass": 1 if is_100_pass else 0,
        "missing_tables_json": json.dumps(missing_tables, ensure_ascii=False),
        "extra_tables_json": json.dumps(extra_tables, ensure_ascii=False),
        "cell_scramble_flags": "; ".join(cell_scrambles),
        "continuation_flags": "; ".join(continuation_issues),
        "prose_flags": "; ".join(prose_issues),
        "header_flags": "; ".join(header_issues)
    }

def record_audit_result(res: Dict[str, Any]):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO papers (
        pdf_path, title, num_pages, doc_type,
        declared_tables_json, num_declared,
        extracted_tables_json, num_extracted,
        status, error_msg, is_100_pass,
        missing_tables_json, extra_tables_json,
        cell_scramble_flags, continuation_flags, prose_flags, header_flags,
        updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        res["pdf_path"], res["title"], res["num_pages"], res["doc_type"],
        res["declared_tables_json"], res["num_declared"],
        res["extracted_tables_json"], res["num_extracted"],
        res["status"], res["error_msg"], res["is_100_pass"],
        res["missing_tables_json"], res["extra_tables_json"],
        res["cell_scramble_flags"], res["continuation_flags"],
        res["prose_flags"], res["header_flags"]
    ))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_audit_db()
    conn = sqlite3.connect(DB_PATH)
    done_paths = set(r[0] for r in conn.execute("SELECT pdf_path FROM papers").fetchall())
    conn.close()

    all_pdfs = sorted(glob.glob(os.path.join(STORAGE_DIR, "*/*.pdf")))
    remaining_pdfs = [p for p in all_pdfs if p not in done_paths]
    print(f"=== Resuming 8-Hour Deep Diagnostic Audit: {len(done_paths)} done, {len(remaining_pdfs)} remaining ===")
    
    passed_count = 0
    anomaly_count = 0
    
    for idx, pdf in enumerate(remaining_pdfs):
        res = inspect_paper_tables(pdf)
        record_audit_result(res)
        if res['is_100_pass']:
            passed_count += 1
        else:
            anomaly_count += 1
            
        if (idx + 1) % 20 == 0 or idx == len(remaining_pdfs) - 1 or not res['is_100_pass']:
            print(f"[{len(done_paths) + idx + 1}/{len(all_pdfs)}] {os.path.basename(pdf)[:40]}... → {res['status']} (Decl: {res['num_declared']}, Ext: {res['num_extracted']}, Pass: {res['is_100_pass']}) | Pass: {passed_count}, Anomalies: {anomaly_count}")
            if not res['is_100_pass']:
                if res['missing_tables_json'] != "[]":
                    print(f"    Missing: {res['missing_tables_json']}")
                if res['cell_scramble_flags']:
                    print(f"    Scramble: {res['cell_scramble_flags']}")
                if res['prose_flags']:
                    print(f"    Prose: {res['prose_flags']}")
