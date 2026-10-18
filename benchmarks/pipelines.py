#!/usr/bin/env python3
"""
benchmarks/pipelines.py — Implementation of the 3 benchmark evaluation pipelines:

1. Pipeline 1 (Baseline):
   Multi-engine voting arbitration (three_way_vote from scripts/pdf_table_extractor.py).
   Combines pdf-inspector, find_tables, pdfplumber, Camelot, text_alignment via majority voting.

2. Pipeline 2 (Proposed):
   MinerU structured primary extraction +
   Local numeric integrity gate (verify_table_numeric_integrity) +
   Targeted local recovery (Stage 1-4) +
   Monotone DP cross-page continuation table stitching.

3. Pipeline 3 (Pure local):
   Local lightweight rule-based extractors (find_tables, Camelot, pdfplumber, text_alignment)
   with spatial heuristic deduplication, zero cloud calls, zero heavy VLM models.
"""

import os
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import pandas as pd
import fitz

# Ensure scripts directory is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pdf_table_extractor as pte
import table_validator
import table_postprocess
import ocr_client


# =============================================================================
# PIPELINE 1: Baseline Multi-Engine Voting Arbitration
# =============================================================================
def run_pipeline_1_baseline(pdf_path: str) -> Tuple[List[Dict[str, Any]], float, List[str]]:
    """
    Pipeline 1 (Baseline): Existing multi-engine voting arbitration pipeline.
    """
    t0 = time.perf_counter()
    logs = []

    # 1. Caption declaration scan (Baseline relies on regex declarations)
    page_decls = table_validator.scan_pdf_table_declarations(pdf_path)
    caption_pages = set(page_decls.keys())
    caption_page_map = {}
    for p_idx, decls in page_decls.items():
        for d in decls:
            if not d.get('is_continuation'):
                lbl = d.get('label')
                if lbl and lbl not in caption_page_map:
                    caption_page_map[lbl] = p_idx

    # 2. pdf-inspector classification
    inspector_result = pte.classify_and_extract_via_inspector(pdf_path, caption_page_map)
    raw_pages = inspector_result.get('pages_with_tables', [])
    target_pages = sorted(list(set(p - 1 for p in raw_pages if isinstance(p, int) and p > 0)))

    if not target_pages:
        target_pages = sorted(list(caption_pages)) if caption_pages else list(range(len(fitz.open(pdf_path))))

    # 3. Extract via candidate native engines
    structure_results = pte.extract_via_structure_tree(pdf_path, pages=target_pages)
    ft_results = pte.extract_via_find_tables(pdf_path, pages=target_pages)
    plumber_results = pte.extract_via_pdfplumber(pdf_path, pages=target_pages)
    camelot_results = pte.extract_via_camelot(pdf_path, pages=target_pages)
    align_results = pte.extract_via_text_alignment(pdf_path, pages=target_pages)
    pymupdf4llm_results = pte.extract_via_pymupdf4llm(pdf_path, pages=target_pages) if hasattr(pte, "extract_via_pymupdf4llm") else []

    # 4. Multi-engine voting arbitration (three_way_vote)
    fused, vote_logs, vote_summary = pte.three_way_vote(
        inspector_result.get('tables', []),
        ft_results,
        plumber_results,
        camelot_results,
        align_tables=align_results,
        structure_tables=structure_results,
        pymupdf4llm_tables=pymupdf4llm_results,
    )
    logs.extend(vote_logs)

    # 5. Baseline postprocessing (no MinerU VLM passthrough, standard cleaning)
    processed_tables = []
    for item in fused:
        df = item.get("df")
        if df is not None and not df.empty:
            cleaned_df = table_postprocess.postprocess_dataframe(df.copy())
            if cleaned_df is not None and not cleaned_df.empty:
                is_cv = bool(df.attrs.get("cross_validated", item.get("cross_validated", False)))
                vote_cnt = 2 if is_cv else 1
                conf = 0.90 if is_cv else 0.60
                vote_ext = df.attrs.get("vote_extractor", df.attrs.get("extractor", item.get("extractor", "three_way_vote")))
                agreeing = df.attrs.get("agreeing_extractors", item.get("agreeing_extractors", []))
                p_idx = item.get("page_idx", df.attrs.get("page_idx", 0))
                lbl = item.get("label", df.attrs.get("label", ""))

                # Retain voting attributes
                cleaned_df.attrs["vote_count"] = vote_cnt
                cleaned_df.attrs["confidence"] = conf
                cleaned_df.attrs["cross_validated"] = is_cv
                cleaned_df.attrs["agreeing_extractors"] = agreeing
                cleaned_df.attrs["extractor"] = "three_way_vote"
                cleaned_df.attrs["vote_extractor"] = vote_ext
                cleaned_df.attrs["page_idx"] = p_idx
                cleaned_df.attrs["label"] = lbl

                processed_tables.append({
                    "df": cleaned_df,
                    "page_idx": p_idx,
                    "label": lbl,
                    "vote_count": vote_cnt,
                    "confidence": conf,
                    "cross_validated": is_cv,
                    "agreeing_extractors": agreeing,
                    "extractor": "three_way_vote",
                    "vote_extractor": vote_ext,
                })

    elapsed = time.perf_counter() - t0
    return processed_tables, elapsed, logs


# =============================================================================
# PIPELINE 2: Proposed MinerU Structured + Local Numeric Gate + Targeted Recovery
# =============================================================================
def run_pipeline_2_proposed(pdf_path: str) -> Tuple[List[Dict[str, Any]], float, List[str]]:
    """
    Pipeline 2 (Proposed):
    - Primary: MinerU structured extraction (using structured ZIP archive or API)
    - Quality Gate: verify_table_numeric_integrity against PDF text layer
    - Targeted Local Recovery: Stage 1-4 for missing / failed / low-integrity tables
    - Monotone DP cross-page continuation table stitching
    """
    t0 = time.perf_counter()
    logs = []
    pdf_file = Path(pdf_path)
    paper_stem = pdf_file.stem

    # Check for cached MinerU structured archive
    cache_zip = pdf_file.parent / "mineru_cache" / f"{paper_stem}.zip"
    mineru_dfs = []

    if cache_zip.exists():
        logs.append(f"MinerU structured archive found in cache: {cache_zip.name}")
        try:
            mineru_dfs = ocr_client.extract_mineru_structured_archive(str(cache_zip))
        except Exception as e_c:
            logs.append(f"MinerU structured archive cache corrupt: {e_c}")
            mineru_dfs = []
        if not mineru_dfs:
            logs.append(f"MinerU cache {cache_zip.name} was corrupt or empty; purging and falling back to live API")
            try:
                cache_zip.unlink(missing_ok=True)
            except Exception:
                pass

    if not mineru_dfs:
        # Attempt live MinerU structured API if configured
        if ocr_client.is_mineru_available(file_path=str(pdf_file)):
            logs.append("Invoking live MinerU structured API...")
            try:
                mineru_dfs = ocr_client.call_mineru_structured_api(str(pdf_file))
            except Exception as e_mineru:
                logs.append(f"MinerU live API call failed: {e_mineru}")
                mineru_dfs = []

    logs.append(f"Primary MinerU extracted {len(mineru_dfs)} structured tables")

    # Step 2: Local Quality & Numeric Integrity Gate (verify_table_numeric_integrity)
    accepted_tables = []
    pages_needing_recovery = set()

    for i, df in enumerate(mineru_dfs):
        p_idx = df.attrs.get("page_idx", 0)
        bbox = df.attrs.get("bbox")

        is_valid, info = table_validator.verify_table_numeric_integrity(
            df=df,
            pdf_path=str(pdf_file),
            page_idx=p_idx,
            bbox=bbox,
            min_match_ratio=0.75,
        )

        logs.append(
            f"  Table #{i+1} (p={p_idx}): gate_valid={is_valid}, match_ratio={info.get('match_ratio', 0):.2f}, "
            f"font_corrupted={info.get('font_corrupted')}, reason={info.get('reason')}"
        )

        if is_valid:
            accepted_tables.append(df)
        else:
            pages_needing_recovery.add(p_idx)

    # Step 3: Cross-check declared tables and detect untitled tables
    report = table_validator.cross_check_missed_tables(accepted_tables, str(pdf_file))
    if not report.get("is_complete", True) or report.get("coverage_unknown", False):
        for missed in report.get("missed_tables", []):
            p_m = missed.get("page_idx")
            if p_m is not None:
                pages_needing_recovery.add(p_m)
                logs.append(f"Cross-check detected missed declared table {missed.get('label')} on page {p_m+1}")
        for p_scan in report.get("uncovered_scanned_pages", []):
            pages_needing_recovery.add(p_scan)
            logs.append(f"Scanned page {p_scan+1} coverage unknown (0 declarations found), triggering visual recovery")

    with fitz.open(str(pdf_file)) as doc:
        total_pages = len(doc)
        all_pages = set(range(total_pages))

    covered_pages = set(df.attrs.get("page_idx", 0) for df in accepted_tables)
    uncovered_pages = all_pages - covered_pages

    # Check for untitled tables on uncovered pages (must pass validate_native_table_dataframe)
    for p in uncovered_pages:
        ft_check = pte.extract_via_find_tables(str(pdf_file), pages=[p])
        for ft_item in ft_check:
            df_cand = ft_item.get("df")
            if df_cand is not None and not df_cand.empty:
                ok, reason = table_validator.validate_native_table_dataframe(df_cand, p)
                if ok and not pte.is_table_low_quality(df_cand) and len(df_cand) >= 2 and len(df_cand.columns) >= 3:
                    pages_needing_recovery.add(p)
                    logs.append(f"Detected valid untitled table on page {p+1}: {df_cand.shape}")
                    break

    # Step 4: Targeted Local Recovery (Stages 1-4)
    if pages_needing_recovery:
        recovery_pages = sorted(list(pages_needing_recovery))
        logs.append(f"Triggering Targeted Local Recovery (Stages 1-4) on pages: {recovery_pages}")

        recovered_results = []
        unresolved_pages = set(recovery_pages)

        # Stage 1: RapidTable / TSR
        if pte.is_rapid_table_available() and unresolved_pages:
            try:
                rapid_res = pte.extract_via_rapid_table(str(pdf_file), pages=list(unresolved_pages))
                valid_rapid = [r for r in (rapid_res or []) if not pte.is_table_low_quality(r.get('df'))]
                if valid_rapid:
                    logs.append(f"  Stage 1 (RapidTable): recovered {len(valid_rapid)} tables")
                    recovered_results.extend(valid_rapid)
                    for r in valid_rapid:
                        unresolved_pages.discard(r.get('page_idx'))
            except Exception as e_s1:
                logs.append(f"  Stage 1 error: {e_s1}")

        # Stage 2: DocLayout-YOLO crop + PaddleOCR / OCR
        if unresolved_pages:
            try:
                crop_pages = list(unresolved_pages)
                crop_res = pte.extract_via_paddleocr_crops(str(pdf_file), crop_pages)
                valid_crops = [r for r in (crop_res or []) if not pte.is_table_low_quality(r.get('df'))]
                if valid_crops:
                    logs.append(f"  Stage 2 (DocLayout-YOLO + PaddleOCR): recovered {len(valid_crops)} tables")
                    recovered_results.extend(valid_crops)
                    for r in valid_crops:
                        unresolved_pages.discard(r.get('page_idx'))
            except Exception as e_s2:
                logs.append(f"  Stage 2 error: {e_s2}")

        # Stage 3: Camelot Lattice / Docling TableFormer
        if unresolved_pages:
            try:
                cam_res = pte.extract_via_camelot(str(pdf_file), pages=list(unresolved_pages))
                valid_cam = [r for r in (cam_res or []) if not pte.is_table_low_quality(r.get('df'))]
                if valid_cam:
                    logs.append(f"  Stage 3 (Camelot Lattice): recovered {len(valid_cam)} tables")
                    recovered_results.extend(valid_cam)
                    for r in valid_cam:
                        unresolved_pages.discard(r.get('page_idx'))
            except Exception as e_s3:
                logs.append(f"  Stage 3 error: {e_s3}")

        # Stage 4: High-density text_alignment spatial clustering
        if unresolved_pages:
            try:
                align_res = pte.extract_via_text_alignment(str(pdf_file), pages=list(unresolved_pages))
                valid_align = [r for r in (align_res or []) if not pte.is_table_low_quality(r.get('df'))]
                if valid_align:
                    logs.append(f"  Stage 4 (text_alignment spatial): recovered {len(valid_align)} tables")
                    recovered_results.extend(valid_align)
                    for r in valid_align:
                        unresolved_pages.discard(r.get('page_idx'))
            except Exception as e_s4:
                logs.append(f"  Stage 4 error: {e_s4}")

        for rec in recovered_results:
            rec_df = rec.get("df")
            if rec_df is not None and not rec_df.empty:
                accepted_tables.append(rec_df)

    # Step 5: Postprocessing with DeepDoc/MinerU Monotone DP Continuation Stitching
    # Normalize input into list of DataFrames
    cleaned_tables = []
    for df in accepted_tables:
        cleaned_df = table_postprocess.postprocess_dataframe(df.copy())
        if cleaned_df is not None and not cleaned_df.empty:
            cleaned_tables.append(cleaned_df)

    # Monotone DP continuation table stitching
    stitched_tables = table_postprocess.merge_continuation_tables(cleaned_tables)
    logs.append(f"After Monotone DP stitching: {len(stitched_tables)} complete tables")

    final_results = []
    for df in stitched_tables:
        final_results.append({
            "df": df,
            "page_idx": df.attrs.get("page_idx", 0),
            "label": df.attrs.get("label", ""),
            "table_title": df.attrs.get("table_title", ""),
            "extractor": df.attrs.get("extractor", "mineru_proposed"),
            "confidence": 0.98,
            "vote_count": 1,
        })

    elapsed = time.perf_counter() - t0
    return final_results, elapsed, logs


# =============================================================================
# PIPELINE 3: Pure Local Lightweight Rules
# =============================================================================
def run_pipeline_3_pure_local(pdf_path: str) -> Tuple[List[Dict[str, Any]], float, List[str]]:
    """
    Pipeline 3 (Pure Local):
    Uses only local rule-based extractors (find_tables, Camelot, pdfplumber, text_alignment)
    with greedy spatial heuristics, zero cloud calls, zero heavy VLM models.
    """
    t0 = time.perf_counter()
    logs = []

    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)
    all_pages = list(range(total_pages))

    # Run native extractors
    ft_res = pte.extract_via_find_tables(pdf_path, pages=all_pages)
    cam_res = pte.extract_via_camelot(pdf_path, pages=all_pages)
    plumb_res = pte.extract_via_pdfplumber(pdf_path, pages=all_pages)
    align_res = pte.extract_via_text_alignment(pdf_path, pages=all_pages)

    rapid_res = []
    if pte.is_rapid_table_available():
        try:
            rapid_res = pte.extract_via_rapid_table(pdf_path, pages=all_pages) or []
            logs.append(f"RapidTable extracted {len(rapid_res)} candidates")
        except Exception as e_rt:
            logs.append(f"RapidTable extraction failed: {e_rt}")

    all_raw = ft_res + cam_res + plumb_res + align_res + rapid_res
    logs.append(f"Pure local extractors collected {len(all_raw)} raw candidates")

    # Greedy deduplication by page and row count
    tables_by_page = {}
    for item in all_raw:
        df = item.get("df")
        if df is None or df.empty:
            continue
        p = item.get("page_idx", df.attrs.get("page_idx", 0))
        if p not in tables_by_page:
            tables_by_page[p] = []
        tables_by_page[p].append(item)

    chosen_tables = []
    for p, items in tables_by_page.items():
        # Select candidate with highest cell density / largest row count
        best_item = max(items, key=lambda x: (len(x["df"]), len(x["df"].columns)))
        chosen_tables.append(best_item)

    # Clean dataframes
    final_results = []
    for item in chosen_tables:
        df = item["df"]
        cleaned_df = table_postprocess.postprocess_dataframe(df.copy())
        if cleaned_df is not None and not cleaned_df.empty:
            final_results.append({
                "df": cleaned_df,
                "page_idx": item.get("page_idx", 0),
                "label": item.get("label", ""),
                "extractor": item.get("extractor", "pure_local"),
                "confidence": 0.70,
                "vote_count": 1,
            })

    elapsed = time.perf_counter() - t0
    return final_results, elapsed, logs
