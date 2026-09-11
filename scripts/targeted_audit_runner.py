"""
targeted_audit_runner.py - 8小时精细化深度自检引擎（专项目标：含表学术文献）
针对全库 520+ 篇要求提取表格的文献开展 8 维微观数据质量量化质检。
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
from typing import List, Dict, Any, Tuple

# Enable unbuffered I/O
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Add scripts directory to path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import pdf_table_extractor as pte
from table_validator import scan_pdf_table_declarations, validate_native_table_dataframe
from excel_export import save_tables_to_excel, TABLE_LABEL_RE, format_table_label, make_safe_filename
from common import is_metadata_table, is_supplementary, is_table_low_quality

DB_PATH = get_targeted_audit_db_path()
OUTPUT_BASE_DIR = get_paper_tables_dir()
STORAGE_DIR = get_zotero_storage_dir()

def init_targeted_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS papers (
        pdf_path TEXT PRIMARY KEY,
        title TEXT,
        num_pages INTEGER,
        doc_type TEXT,
        num_declared INTEGER,
        num_extracted INTEGER,
        quality_score REAL,
        status TEXT,
        is_100_pass INTEGER DEFAULT 0,
        missing_tables_json TEXT,
        extra_tables_json TEXT,
        dim1_label_auth TEXT,
        dim2_header_units TEXT,
        dim3_numerical_purity TEXT,
        dim4_continuation_clean TEXT,
        dim5_symmetrical_layout TEXT,
        dim6_subrow_expansion TEXT,
        dim7_sparsity_exemption TEXT,
        dim8_footnote_isolation TEXT,
        detailed_anomalies TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def get_target_paper_list() -> List[str]:
    """
    Returns the strict list of PDFs that actually contain tables / require extraction.
    """
    # 1. From previous broad audit db, select papers with num_declared > 0 or num_extracted > 0
    broad_db = get_audit_db_path("audit_inspection_8h.db")
    target_pdfs = set()
    
    if os.path.exists(broad_db):
        conn = sqlite3.connect(broad_db)
        cursor = conn.cursor()
        cursor.execute("SELECT pdf_path FROM papers WHERE num_declared > 0 OR num_extracted > 0")
        for r in cursor.fetchall():
            if os.path.exists(r[0]):
                target_pdfs.add(r[0])
        conn.close()

    # 2. Check existing directories in paper_tables with .xlsx files
    for d in glob.glob(os.path.join(OUTPUT_BASE_DIR, "*")):
        if os.path.isdir(d):
            xlsx_files = glob.glob(os.path.join(d, "*.xlsx"))
            if xlsx_files:
                base_name = os.path.basename(d)
                # Find matching pdf in storage
                matching_pdfs = glob.glob(os.path.join(STORAGE_DIR, f"*/{base_name}.pdf"))
                for mp in matching_pdfs:
                    target_pdfs.add(mp)

    return sorted(list(target_pdfs))

def evaluate_table_micro_quality(df: pd.DataFrame, table_label: str) -> Dict[str, Any]:
    """
    Evaluates a single DataFrame across the 8 granular dimensions.
    """
    anomalies = []
    
    # Dim 2: Header Hierarchy & Units
    cols = [str(c).strip() for c in df.columns if pd.notna(c)]
    has_units = any(re.search(r'\(.*?(?:wt%|ppm|ppb|%|‰|Ma|Ga|ka|℃|°C|km|m|g/t|g/cm3).*?\)', c, re.IGNORECASE) or any(u in c for u in ['wt%', 'ppm', 'ppb', '‰', 'Ma', 'Ga', 'ka', 'wt.%']) for c in cols)
    has_multilevel = any('_' in c for c in cols)
    unnamed_cols = sum(1 for c in cols if c.startswith('Unnamed:') or c.startswith('Col_'))
    dim2_status = "GOOD" if unnamed_cols == 0 else f"UNNAMED_COLS_{unnamed_cols}"
    if unnamed_cols > max(1, len(cols) * 0.4):
        anomalies.append(f"Header: {unnamed_cols}/{len(cols)} columns unnamed")

    # Dim 3: Numerical Purity & Micro Notation
    numerical_cells = 0
    corrupted_cells = 0
    total_cells = max(1, df.size)
    
    for val in df.values.flatten():
        if val is None or pd.isna(val):
            continue
        v_str = str(val).strip()
        if not v_str or v_str.lower() in ('nan', 'none', '-'):
            continue
        # Pure number, range, exponent, isotope ratio, error notation
        if re.match(r'^[<>]?[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$', v_str):
            numerical_cells += 1
        elif re.match(r'^\d+(?:\.\d+)?\s*[±\+/-～~-]\s*\d+(?:\.\d+)?$', v_str):
            numerical_cells += 1
        elif re.search(r'\b(?:10[-+^]?\d+|×10[-+^]?\d+)\b', v_str):
            numerical_cells += 1
        elif re.search(r'[\u4e00-\u9fa5]', v_str):
            # Chinese text cell (nominal description)
            pass
        elif len(v_str) > 20 and any(c.isdigit() for c in v_str) and (' ' in v_str or ',' in v_str):
            # Potential squeezed unparsed numbers
            if sum(1 for p in v_str.split() if re.match(r'^-?\d+(?:\.\d+)?$', p)) >= 2:
                corrupted_cells += 1
                
    dim3_status = "PURITY_HIGH" if corrupted_cells == 0 else f"SQUEEZED_CELLS_{corrupted_cells}"
    if corrupted_cells > 0:
        anomalies.append(f"Numerical: {corrupted_cells} squeezed unparsed numeric cells")

    # Dim 4: Continuation 0-Duplicate Rows
    first_cols_norm = [c.lower() for c in cols if not c.startswith('Unnamed') and len(c) >= 2]
    duplicate_header_rows = 0
    for r_idx in range(1, len(df)):
        row_vals = [str(v).strip().lower() for v in df.iloc[r_idx] if pd.notna(v) and str(v).strip() and str(v).strip().lower() != 'nan']
        if len(row_vals) >= 2:
            matches = sum(1 for v in row_vals if any(v == c or (len(v) >= 2 and v in c) for c in first_cols_norm))
            if (matches / max(1, len(row_vals))) >= 0.50 and matches >= 2:
                duplicate_header_rows += 1
            elif any(re.search(r'^(?:续表|接上表|\(contd|\(continued|continued table)', v) for v in row_vals):
                duplicate_header_rows += 1
                
    dim4_status = "CLEAN_0_DUP" if duplicate_header_rows == 0 else f"FOUND_DUP_HEADERS_{duplicate_header_rows}"
    if duplicate_header_rows > 0:
        anomalies.append(f"Continuation: {duplicate_header_rows} duplicate printed header rows found in body")

    # Dim 5: Symmetrical Layout
    symmetrical_pairs = sum(1 for c in cols if f"{c}_1" in cols or f"{c}_part2" in cols)
    dim5_status = "SYMMETRICAL_PARSED" if symmetrical_pairs >= 2 else "STANDARD_LAYOUT"

    # Dim 6: Subrow Expansion
    subrow_count = sum(1 for v in df.values.flatten() if isinstance(v, str) and '\n' in v and len(v.split('\n')) >= 2)
    dim6_status = f"MULTILINE_CELLS_{subrow_count}" if subrow_count > 0 else "FLAT_CELLS"

    # Dim 7: Geochemical Sparsity Exemption
    empty_cells = df.isna().sum().sum() + sum(1 for v in df.values.flatten() if str(v).strip() in ('', 'nan', 'none', 'None'))
    sparsity_ratio = empty_cells / total_cells
    is_geo = any(re.search(r'\b(?:SiO2|TiO2|Al2O3|Fe2O3|FeO|MnO|MgO|CaO|Na2O|K2O|P2O5|LOI|Total|Fe|Cu|Pb|Zn|Au|Ag|Sb|As|Hg|W|Mo|Bi|Co|Ni|Cr|V|Ba|Sr|Rb|Cs|Zr|Hf|Nb|Ta|Th|U|La|Ce|Pr|Nd|Sm|Eu|Gd|Tb|Dy|Ho|Er|Tm|Yb|Lu|Y|REE|δ34S|δ18O|δ13C|δD|206Pb/204Pb|207Pb/204Pb|208Pb/204Pb|wt%|ppm|ppb)\b', c, re.IGNORECASE) for c in cols)
    dim7_status = "GEOCHEM_EXEMPTED" if (sparsity_ratio > 0.5 and is_geo) else ("NORMAL_DENSITY" if sparsity_ratio <= 0.5 else "HIGH_SPARSITY")

    # Dim 8: Footnote Isolation
    has_bottom_footnote = False
    if len(df) > 0:
        last_row_str = " ".join(str(v) for v in df.iloc[-1] if pd.notna(v)).lower()
        if any(kw in last_row_str for kw in ['注：', '注:', 'note:', 'notes:', '*表示', 'bdl: below', '数据来源:']):
            has_bottom_footnote = True
            anomalies.append("Footnote: Table footnote attached as last data row")
    dim8_status = "FOOTNOTE_EMBEDDED" if has_bottom_footnote else "CLEAN_ISOLATION"

    return {
        "dim2": dim2_status,
        "dim3": dim3_status,
        "dim4": dim4_status,
        "dim5": dim5_status,
        "dim6": dim6_status,
        "dim7": dim7_status,
        "dim8": dim8_status,
        "anomalies": anomalies
    }

def inspect_targeted_paper(pdf_path: str) -> Dict[str, Any]:
    """
    Performs full 8-dimensional deep quality inspection on a target paper.
    """
    pdf_filename = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = os.path.join(OUTPUT_BASE_DIR, pdf_filename)
    
    try:
        doc = fitz.open(pdf_path)
        num_pages = len(doc)
        doc_type = "double_column" if any(" " in doc[i].get_text("text") for i in range(min(3, len(doc)))) else "single_column"
    except Exception as e:
        return {
            "pdf_path": pdf_path,
            "title": pdf_filename,
            "num_pages": 0,
            "doc_type": "corrupted",
            "num_declared": 0,
            "num_extracted": 0,
            "quality_score": 0.0,
            "status": "CORRUPTED_PDF",
            "is_100_pass": 0,
            "missing_tables_json": "[]",
            "extra_tables_json": "[]",
            "dim1_label_auth": "FAIL",
            "dim2_header_units": "FAIL",
            "dim3_numerical_purity": "FAIL",
            "dim4_continuation_clean": "FAIL",
            "dim5_symmetrical_layout": "NONE",
            "dim6_subrow_expansion": "NONE",
            "dim7_sparsity_exemption": "NONE",
            "dim8_footnote_isolation": "NONE",
            "detailed_anomalies": str(e)
        }

    # 1. Ground truth declarations
    declarations_map = scan_pdf_table_declarations(pdf_path)
    declared_labels = []
    for p_idx, caps in declarations_map.items():
        for cap in caps:
            if not cap.get("is_continuation"):
                declared_labels.append(format_table_label(cap["label"]))

    # 2. Check or perform extraction
    xlsx_files = sorted(glob.glob(os.path.join(out_dir, "*.xlsx")))
    manifest_path = os.path.join(out_dir, "table_captions.txt")
    
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
                "num_declared": len(declared_labels),
                "num_extracted": 0,
                "quality_score": 0.0,
                "status": "EXTRACTION_ERROR",
                "is_100_pass": 0,
                "missing_tables_json": json.dumps(declared_labels, ensure_ascii=False),
                "extra_tables_json": "[]",
                "dim1_label_auth": "FAIL",
                "dim2_header_units": "FAIL",
                "dim3_numerical_purity": "FAIL",
                "dim4_continuation_clean": "FAIL",
                "dim5_symmetrical_layout": "NONE",
                "dim6_subrow_expansion": "NONE",
                "dim7_sparsity_exemption": "NONE",
                "dim8_footnote_isolation": "NONE",
                "detailed_anomalies": f"Extraction exception: {e}"
            }

    # 3. Micro quality audit on every Excel file
    xlsx_labels = []
    paper_anomalies = []
    
    dim2_list = []
    dim3_list = []
    dim4_list = []
    dim5_list = []
    dim6_list = []
    dim7_list = []
    dim8_list = []
    
    for x in xlsx_files:
        base_name = os.path.basename(x)
        if base_name.startswith("._"):
            continue
        lbl = os.path.splitext(base_name)[0]
        xlsx_labels.append(format_table_label(lbl))
        
        try:
            df = pd.read_excel(x)
            q = evaluate_table_micro_quality(df, lbl)
            dim2_list.append(q["dim2"])
            dim3_list.append(q["dim3"])
            dim4_list.append(q["dim4"])
            dim5_list.append(q["dim5"])
            dim6_list.append(q["dim6"])
            dim7_list.append(q["dim7"])
            dim8_list.append(q["dim8"])
            if q["anomalies"]:
                paper_anomalies.extend([f"[{lbl}] {a}" for a in q["anomalies"]])
        except Exception as e:
            paper_anomalies.append(f"[{lbl}] Read Excel Error: {e}")

    # Compare Declared vs Extracted
    declared_set = set(declared_labels)
    extracted_set = set(xlsx_labels)
    
    missing_tables = sorted(list(declared_set - extracted_set))
    extra_tables = sorted(list(extracted_set - declared_set))
    
    # Verify Manifest
    has_manifest = os.path.exists(manifest_path) and os.path.getsize(manifest_path) > 0
    if not has_manifest and len(xlsx_labels) > 0:
        paper_anomalies.append("Manifest: Missing or empty table_captions.txt")

    # Compute Quality Score (0 to 100)
    score = 100.0
    if missing_tables:
        score -= min(40.0, len(missing_tables) * 15.0)
    if not has_manifest and len(xlsx_labels) > 0:
        score -= 10.0
    if paper_anomalies:
        score -= min(30.0, len(paper_anomalies) * 5.0)
    score = max(0.0, score)
    
    is_100_pass = (score >= 95.0 and len(missing_tables) == 0 and len(paper_anomalies) == 0)
    status = "100%_PASS" if is_100_pass else ("HIGH_QUALITY" if score >= 85 else "ANOMALY_FLAGGED")
    
    doc.close()
    return {
        "pdf_path": pdf_path,
        "title": pdf_filename,
        "num_pages": num_pages,
        "doc_type": doc_type,
        "num_declared": len(declared_labels),
        "num_extracted": len(xlsx_labels),
        "quality_score": round(score, 1),
        "status": status,
        "is_100_pass": 1 if is_100_pass else 0,
        "missing_tables_json": json.dumps(missing_tables, ensure_ascii=False),
        "extra_tables_json": json.dumps(extra_tables, ensure_ascii=False),
        "dim1_label_auth": "PASS" if len(missing_tables) == 0 else f"MISSING_{len(missing_tables)}",
        "dim2_header_units": "; ".join(set(dim2_list)) if dim2_list else "NONE",
        "dim3_numerical_purity": "; ".join(set(dim3_list)) if dim3_list else "NONE",
        "dim4_continuation_clean": "; ".join(set(dim4_list)) if dim4_list else "NONE",
        "dim5_symmetrical_layout": "; ".join(set(dim5_list)) if dim5_list else "NONE",
        "dim6_subrow_expansion": "; ".join(set(dim6_list)) if dim6_list else "NONE",
        "dim7_sparsity_exemption": "; ".join(set(dim7_list)) if dim7_list else "NONE",
        "dim8_footnote_isolation": "; ".join(set(dim8_list)) if dim8_list else "NONE",
        "detailed_anomalies": "; ".join(paper_anomalies)
    }

def record_targeted_result(res: Dict[str, Any]):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO papers (
        pdf_path, title, num_pages, doc_type,
        num_declared, num_extracted, quality_score,
        status, is_100_pass, missing_tables_json, extra_tables_json,
        dim1_label_auth, dim2_header_units, dim3_numerical_purity,
        dim4_continuation_clean, dim5_symmetrical_layout, dim6_subrow_expansion,
        dim7_sparsity_exemption, dim8_footnote_isolation, detailed_anomalies,
        updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        res["pdf_path"], res["title"], res["num_pages"], res["doc_type"],
        res["num_declared"], res["num_extracted"], res["quality_score"],
        res["status"], res["is_100_pass"], res["missing_tables_json"], res["extra_tables_json"],
        res["dim1_label_auth"], res["dim2_header_units"], res["dim3_numerical_purity"],
        res["dim4_continuation_clean"], res["dim5_symmetrical_layout"], res["dim6_subrow_expansion"],
        res["dim7_sparsity_exemption"], res["dim8_footnote_isolation"], res["detailed_anomalies"]
    ))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_targeted_db()
    target_pdfs = get_target_paper_list()
    print(f"=== Starting 8-Hour Fine-Grained Deep Quality Audit on {len(target_pdfs)} Target Papers ===")
    
    passed_count = 0
    high_q_count = 0
    anomaly_count = 0
    
    for idx, pdf in enumerate(target_pdfs):
        res = inspect_targeted_paper(pdf)
        record_targeted_result(res)
        
        if res["is_100_pass"]:
            passed_count += 1
        elif res["quality_score"] >= 85:
            high_q_count += 1
        else:
            anomaly_count += 1
            
        print(f"[{idx+1}/{len(target_pdfs)}] {os.path.basename(pdf)[:38]}... → Score: {res['quality_score']}% ({res['status']}) | 100% Pass: {passed_count}, High Q: {high_q_count}, Anomaly: {anomaly_count}")
        if res["detailed_anomalies"]:
            print(f"    [Anomalies]: {res['detailed_anomalies'][:120]}...")
