#!/usr/bin/env python3
"""
benchmarks/evaluator.py — Quantitative evaluation metrics engine.

Calculates:
1. Missed Tables (漏表数)
2. Truncated Rows (漏行/截断数)
3. Value Error Rate (数值错误率 / 抽样数值单元格精确度)
4. False Agreement Count (方案1中多引擎同时漏掉表底数据却一致判定的频次)
5. End-to-end Wall-Clock Time (端到端耗时)
"""

import re
import difflib
from typing import List, Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np


def _normalize_token(s: Any) -> str:
    """Normalize string token for comparison."""
    if s is None:
        return ""
    txt = str(s).strip()
    txt = re.sub(r'[\s,\$¥€]', '', txt)
    return txt.lower()


def _extract_numbers_from_text(text: str) -> List[float]:
    """Extract numeric values from arbitrary text."""
    if not text:
        return []
    # Match numbers including decimals and negative signs
    matches = re.findall(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', str(text).replace(",", ""))
    nums = []
    for m in matches:
        try:
            nums.append(float(m))
        except ValueError:
            pass
    return nums


def _norm_label(s: Any) -> str:
    """Normalize table label by removing non-alphanumeric chars and lowercasing."""
    if s is None:
        return ""
    return re.sub(r'[^a-zA-Z0-9]', '', str(s)).lower()


def _cell_matches_numeric(extracted_val: Any, expected_num: float, expected_raw: str, tol: float = 1e-3) -> bool:
    """Check if extracted cell contains the expected numeric value."""
    if extracted_val is None:
        return False
    ext_str = str(extracted_val).strip()
    if not ext_str:
        return False

    # 1. Exact string match (ignoring whitespace)
    clean_exp = str(expected_raw).replace(" ", "").strip()
    clean_ext = ext_str.replace(" ", "").strip()
    if clean_exp == clean_ext:
        return True
    # Containment: only if extracted contains the full expected token (e.g. "40.95 wt%")
    if len(clean_exp) >= 2 and clean_exp in clean_ext:
        return True

    # 2. Number extraction and float tolerance
    ext_nums = _extract_numbers_from_text(ext_str)
    for n in ext_nums:
        if abs(n - expected_num) <= max(tol, abs(expected_num) * 0.01):
            return True

    return False


def match_table_to_ground_truth(
    extracted_df: pd.DataFrame,
    gt_table: Dict[str, Any],
) -> float:
    """
    Compute a matching score between an extracted table and a ground truth table.
    Returns score between 0.0 and 1.0.
    """
    if extracted_df is None or extracted_df.empty:
        return 0.0

    score = 0.0

    # 1. Label / Caption match (normalized to ignore spacing differences like Table1 vs Table 1)
    ext_label = _norm_label(extracted_df.attrs.get("label", ""))
    gt_label = _norm_label(gt_table.get("label", ""))
    has_label_match = bool(ext_label and gt_label and ext_label == gt_label)
    if has_label_match:
        score += 0.40

    # 2. Page match
    ext_p = extracted_df.attrs.get("page_idx")
    gt_p = gt_table.get("page_idx")
    page_indices = gt_table.get("page_indices", [])
    if ext_p is not None:
        if gt_p is not None and ext_p == gt_p:
            score += 0.20
        elif page_indices and ext_p in page_indices:
            # For multi-page tables, matching the starting page gets top priority
            if ext_p == page_indices[0]:
                score += 0.20
            else:
                score += 0.10

    # 3. Column Count match
    ext_cols = len(extracted_df.columns)
    gt_cols = gt_table.get("num_cols", 0)
    if ext_cols > 0 and gt_cols > 0:
        col_ratio = min(ext_cols, gt_cols) / max(ext_cols, gt_cols)
        score += 0.20 * col_ratio

    # 4. Bag-of-Numbers overlap (Jaccard + Recall blend)
    gt_nums = set()
    for row in gt_table.get("rows", []):
        for cell in row:
            for n in _extract_numbers_from_text(str(cell)):
                gt_nums.add(round(n, 2))

    ext_nums = set()
    for v in extracted_df.values.flatten():
        for n in _extract_numbers_from_text(str(v)):
            ext_nums.add(round(n, 2))

    intersection = 0
    overlap = 0.0
    if gt_nums and ext_nums:
        intersection = len(gt_nums & ext_nums)
        union = len(gt_nums | ext_nums)
        recall = intersection / len(gt_nums)
        jaccard = intersection / union if union > 0 else 0.0
        overlap = 0.50 * recall + 0.50 * jaccard
        score += 0.40 * min(1.0, overlap)

    # 5. Cross-validation bonus
    if extracted_df.attrs.get("cross_validated", False):
        score += 0.05

    # 严格门禁：必须满足标签匹配，或具有显著的数值重合（overlap >= 0.20 或至少 3 个共享数字）
    # 严禁仅凭同页码 + 同列数伪匹配将不相干的表格错配为 Ground Truth
    has_substantial_overlap = (has_label_match or (intersection >= 3 or overlap >= 0.20))
    if not has_substantial_overlap:
        score = min(score, 0.30)

    return score


def evaluate_pipeline_output(
    extracted_tables: List[Dict[str, Any]],
    gt_spec: Dict[str, Any],
    wall_clock_time: float,
    pipeline_name: str,
) -> Dict[str, Any]:
    """
    Perform deep quantitative evaluation of pipeline results against Ground Truth.
    """
    gt_tables = gt_spec.get("tables", [])
    total_gt_tables = len(gt_tables)
    
    matched_pairs = []  # (gt_table, extracted_table_dict, match_score)
    used_ext_indices = set()

    for gt_t in gt_tables:
        best_score = 0.0
        best_idx = None
        for i_ext, ext_item in enumerate(extracted_tables):
            if i_ext in used_ext_indices:
                continue
            df = ext_item.get("df")
            s = match_table_to_ground_truth(df, gt_t)
            if s > best_score:
                best_score = s
                best_idx = i_ext

        # Minimum threshold to be considered a match: 0.35
        if best_idx is not None and best_score >= 0.35:
            matched_pairs.append((gt_t, extracted_tables[best_idx], best_score))
            used_ext_indices.add(best_idx)
        else:
            matched_pairs.append((gt_t, None, 0.0))

    # Metric 1: Missed Tables (漏表数)
    missed_tables_count = sum(1 for gt_t, ext, score in matched_pairs if ext is None)

    # Metric 1b: False Positive Tables & Table-level Precision, Recall, F1
    false_positive_count = len(extracted_tables) - len(used_ext_indices)
    true_positive_count = total_gt_tables - missed_tables_count
    table_precision = (true_positive_count / len(extracted_tables)) if extracted_tables else (1.0 if total_gt_tables == 0 else 0.0)
    table_recall = (true_positive_count / total_gt_tables) if total_gt_tables > 0 else 1.0
    table_f1 = (2 * table_precision * table_recall / (table_precision + table_recall)) if (table_precision + table_recall) > 0 else 0.0

    # Metric 2: Truncated Rows (漏行/截断数)
    total_truncated_rows = 0
    table_truncation_details = []

    # Metric 3: Value Error Rate & Sampled Cell Accuracy
    total_sampled_cells = 0
    errored_cells_count = 0
    cell_eval_details = []

    # Metric 4: False Agreement Count
    false_agreement_count = 0

    for gt_t, ext_dict, score in matched_pairs:
        t_id = gt_t.get("table_id", "Unknown")
        expected_rows = gt_t.get("num_rows", len(gt_t.get("rows", [])))

        if ext_dict is None:
            # Table completely missed
            total_truncated_rows += expected_rows
            table_truncation_details.append({
                "table_id": t_id,
                "status": "missed",
                "expected_rows": expected_rows,
                "extracted_rows": 0,
                "truncated_rows": expected_rows,
            })
            # All sampled cells for this table are missed
            for sc in gt_t.get("sampled_numeric_cells", []):
                total_sampled_cells += 1
                errored_cells_count += 1
                cell_eval_details.append({
                    "table_id": t_id,
                    "row_idx": sc["row_idx"],
                    "col_idx": sc["col_idx"],
                    "col_name": sc["col_name"],
                    "expected": sc["raw_val"],
                    "extracted": None,
                    "matched": False,
                    "reason": "table_missed"
                })
        else:
            df = ext_dict.get("df")
            extracted_rows = len(df)
            trunc = max(0, expected_rows - extracted_rows)
            total_truncated_rows += trunc

            table_truncation_details.append({
                "table_id": t_id,
                "status": "extracted",
                "expected_rows": expected_rows,
                "extracted_rows": extracted_rows,
                "truncated_rows": trunc,
            })

            # Check for False Agreement in voting pipeline:
            # Multiple engines agreed in voting (e.g. cross_validated is True or vote_count >= 2
            # or len(agreeing_extractors) >= 2) BUT the table actually has truncated rows!
            vote_count = df.attrs.get("vote_count", ext_dict.get("vote_count", 1))
            is_cross_val = bool(df.attrs.get("cross_validated", ext_dict.get("cross_validated", False)))
            agreeing_engines = df.attrs.get("agreeing_extractors", ext_dict.get("agreeing_extractors", []))

            has_consensus = is_cross_val or (vote_count >= 2) or (len(agreeing_engines) >= 2)
            if has_consensus and trunc > 0:
                false_agreement_count += 1

            # Column mapping between GT and Extracted DF
            # Map by exact name, leaf header name, or high-confidence fuzzy match
            col_map = {}
            gt_cols = gt_t.get("columns", [])
            for c_idx, c_name in enumerate(gt_cols):
                clean_gt_c = re.sub(r'[^a-zA-Z0-9]', '', str(c_name)).lower()
                leaf_gt_c = re.sub(r'[^a-zA-Z0-9]', '', str(c_name).split('_')[-1]).lower()
                best_ext_col = None
                best_sim = 0.0
                for ext_c_idx, ext_col_name in enumerate(df.columns):
                    clean_ext_c = re.sub(r'[^a-zA-Z0-9]', '', str(ext_col_name)).lower()
                    if clean_gt_c == clean_ext_c or leaf_gt_c == clean_ext_c:
                        best_ext_col = ext_c_idx
                        best_sim = 1.0
                        break
                    sim = difflib.SequenceMatcher(None, clean_gt_c, clean_ext_c).ratio()
                    if sim > best_sim:
                        best_sim = sim
                        best_ext_col = ext_c_idx

                if best_ext_col is not None and best_sim >= 0.5:
                    col_map[c_idx] = best_ext_col
                elif c_idx < len(df.columns):
                    col_map[c_idx] = c_idx

            # Sampled cells evaluation
            for sc in gt_t.get("sampled_numeric_cells", []):
                total_sampled_cells += 1
                r_i = sc["row_idx"]
                c_i = sc["col_idx"]
                exp_raw = sc["raw_val"]
                exp_num = sc["numeric_val"]

                if r_i >= len(df):
                    errored_cells_count += 1
                    cell_eval_details.append({
                        "table_id": t_id,
                        "row_idx": r_i,
                        "col_idx": c_i,
                        "col_name": sc["col_name"],
                        "expected": exp_raw,
                        "extracted": None,
                        "matched": False,
                        "reason": "row_truncated"
                    })
                    continue

                mapped_c = col_map.get(c_i, c_i if c_i < len(df.columns) else None)
                if mapped_c is None or mapped_c >= len(df.columns):
                    errored_cells_count += 1
                    cell_eval_details.append({
                        "table_id": t_id,
                        "row_idx": r_i,
                        "col_idx": c_i,
                        "col_name": sc["col_name"],
                        "expected": exp_raw,
                        "extracted": None,
                        "matched": False,
                        "reason": "col_missing"
                    })
                    continue

                cell_val = df.iat[r_i, mapped_c]
                is_match = _cell_matches_numeric(cell_val, exp_num, exp_raw)
                if not is_match:
                    errored_cells_count += 1

                cell_eval_details.append({
                    "table_id": t_id,
                    "row_idx": r_i,
                    "col_idx": c_i,
                    "col_name": sc["col_name"],
                    "expected": exp_raw,
                    "extracted": str(cell_val),
                    "matched": is_match,
                    "reason": "ok" if is_match else "value_mismatch"
                })

    value_error_rate = (errored_cells_count / total_sampled_cells * 100.0) if total_sampled_cells > 0 else 0.0
    cell_accuracy = 100.0 - value_error_rate

    return {
        "pipeline": pipeline_name,
        "group": gt_spec.get("group", "Unknown"),
        "file_name": gt_spec.get("file_name", ""),
        "wall_clock_time_s": round(wall_clock_time, 3),
        "total_gt_tables": total_gt_tables,
        "extracted_tables_count": len(extracted_tables),
        "missed_tables_count": missed_tables_count,
        "false_positive_count": false_positive_count,
        "table_precision": round(table_precision, 4),
        "table_recall": round(table_recall, 4),
        "table_f1": round(table_f1, 4),
        "total_truncated_rows": total_truncated_rows,
        "total_sampled_cells": total_sampled_cells,
        "errored_cells_count": errored_cells_count,
        "value_error_rate_pct": round(value_error_rate, 2),
        "cell_accuracy_pct": round(cell_accuracy, 2),
        "false_agreement_count": false_agreement_count,
        "table_truncation_details": table_truncation_details,
        "cell_eval_sample": cell_eval_details[:10],
    }
