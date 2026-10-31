#!/usr/bin/env python3
"""
benchmarks/run_benchmark.py —
Empirical benchmark runner for Zotero Table Extractor.

Executes and compares 3 distinct extraction pipelines across 5 table topologies:
1. Pipeline 1 (Baseline): Existing multi-engine voting arbitration (three_way_vote)
2. Pipeline 2 (Proposed): MinerU structured primary + numeric integrity gate + targeted local recovery (Stage 1-4)
3. Pipeline 3 (Pure Local): Local lightweight rules (find_tables / Camelot / text_alignment)

Records 5 quantitative metrics:
1. Missed Tables (漏表数)
2. Truncated Rows (漏行/截断数)
3. Value Error Rate (数值错误率 / 抽样数值单元格精确度)
4. False Agreement Count (多引擎同时漏掉表底数据却一致判定的频次)
5. End-to-end Wall-Clock Time (端到端耗时)

Usage:
  python3 benchmarks/run_benchmark.py [--pipelines p1,p2,p3] [--groups a,b,c,d,e] [--output-dir benchmarks/reports]
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from typing import List, Dict, Any, Tuple

# Path setup
BENCHMARK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_DIR.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from benchmarks import pipelines
from benchmarks import evaluator
from benchmarks import generate_benchmark_papers


GROUPS_CONFIG = [
    {
        "id": "A",
        "name": "Group A: Classic dense three-line tables",
        "pdf": "group_a_dense_three_line.pdf",
        "gt": "group_a_dense_three_line.json",
        "desc": "Geology/chemistry style with high-density values, ± errors, formulas, super/subscripts",
    },
    {
        "id": "B",
        "name": "Group B: Multi-level complex spanning headers",
        "pdf": "group_b_complex_spanning_headers.pdf",
        "gt": "group_b_complex_spanning_headers.json",
        "desc": "Cross-row and cross-column grouped headers, multi-index hierarchies",
    },
    {
        "id": "C",
        "name": "Group C: Untitled tables and appendix tables",
        "pdf": "group_c_untitled_appendix_tables.pdf",
        "gt": "group_c_untitled_appendix_tables.json",
        "desc": "No explicit Table 1 declaration, table starts at page top, appendix data",
    },
    {
        "id": "D",
        "name": "Group D: Multi-page continuation tables",
        "pdf": "group_d_multipage_continuation.pdf",
        "gt": "group_d_multipage_continuation.json",
        "desc": "Spanning 3 pages (80 rows total), continuation column alignment and stitching",
    },
    {
        "id": "E",
        "name": "Group E: Low-contrast / scanned / image-embedded tables",
        "pdf": "group_e_scanned_low_contrast.pdf",
        "gt": "group_e_scanned_low_contrast.json",
        "desc": "Rasterized low-contrast archival scan, no vector text layer",
    },
]

PIPELINES_CONFIG = {
    "p1": {
        "id": "p1",
        "name": "Pipeline 1 (Baseline: Multi-Engine Voting)",
        "func": pipelines.run_pipeline_1_baseline,
        "desc": "Baseline multi-engine voting arbitration (three_way_vote)",
    },
    "p2": {
        "id": "p2",
        "name": "Pipeline 2 (Proposed: MinerU + Gate + Recovery)",
        "func": pipelines.run_pipeline_2_proposed,
        "desc": "MinerU primary + numeric integrity gate + targeted local recovery (Stage 1-4)",
    },
    "p3": {
        "id": "p3",
        "name": "Pipeline 3 (Pure Local: Lightweight Rules)",
        "func": pipelines.run_pipeline_3_pure_local,
        "desc": "Pure local rules (find_tables, Camelot, text_alignment, greedy deduplication)",
    },
}


def ensure_benchmark_assets():
    """Ensure all test PDFs and ground-truth JSON files exist."""
    missing = False
    papers_dir = BENCHMARK_DIR / "papers"
    gt_dir = BENCHMARK_DIR / "ground_truth"

    for grp in GROUPS_CONFIG:
        pdf_path = papers_dir / grp["pdf"]
        gt_path = gt_dir / grp["gt"]
        if not pdf_path.exists() or not gt_path.exists():
            missing = True
            break

    if missing:
        print("[Benchmark Setup] Generating missing benchmark documents and ground truth...")
        generate_benchmark_papers.generate_all_papers()


def run_benchmark_suite(
    selected_pipelines: List[str],
    selected_groups: List[str],
    output_dir: Path,
    verbose: bool = False,
    mineru_mode: str = 'fixture',
) -> Dict[str, Any]:
    """Execute the empirical benchmark suite and collect quantitative results."""
    ensure_benchmark_assets()
    if mineru_mode not in ('fixture', 'live'):
        raise ValueError('mineru_mode must be fixture or live')

    output_dir.mkdir(parents=True, exist_ok=True)
    papers_dir = BENCHMARK_DIR / "papers"
    gt_dir = BENCHMARK_DIR / "ground_truth"

    target_groups = [g for g in GROUPS_CONFIG if g["id"].lower() in [x.lower() for x in selected_groups]]
    if not target_groups:
        target_groups = GROUPS_CONFIG

    target_pipelines = [PIPELINES_CONFIG[p.lower()] for p in selected_pipelines if p.lower() in PIPELINES_CONFIG]
    if not target_pipelines:
        target_pipelines = list(PIPELINES_CONFIG.values())

    print("\n" + "=" * 80)
    print("🚀 STARTING EMPIRICAL TABLE EXTRACTION BENCHMARK RUNNER")
    print("=" * 80)
    print(f"Target Groups:    {[g['id'] for g in target_groups]}")
    print(f"Target Pipelines: {[p['id'] for p in target_pipelines]}")
    print(f"Output Directory: {output_dir}")
    print("=" * 80 + "\n")

    results_matrix = []  # Detailed results per (group, pipeline)
    aggregated_by_pipeline = {p["id"]: {
        "name": p["name"],
        "total_time_s": 0.0,
        "total_gt_tables": 0,
        "total_extracted_tables": 0,
        "total_missed_tables": 0,
        "total_truncated_rows": 0,
        "total_sampled_cells": 0,
        "total_errored_cells": 0,
        "total_false_agreements": 0,
        "group_runs": [],
    } for p in target_pipelines}

    for grp in target_groups:
        pdf_path = papers_dir / grp["pdf"]
        gt_path = gt_dir / grp["gt"]

        with open(gt_path, "r", encoding="utf-8") as f:
            gt_spec = json.load(f)

        print(f"▶ Testing [{grp['id']}] {grp['name']}")
        print(f"  PDF: {pdf_path.name} | GT Tables: {len(gt_spec.get('tables', []))}")

        for p_cfg in target_pipelines:
            p_id = p_cfg["id"]
            p_name = p_cfg["name"]
            p_func = p_cfg["func"]

            print(f"    • Running {p_name}...", end="", flush=True)
            if p_id == 'p2':
                extracted_tables, elapsed_s, logs = p_func(str(pdf_path), mineru_mode=mineru_mode)
            else:
                extracted_tables, elapsed_s, logs = p_func(str(pdf_path))
            print(f" done in {elapsed_s:.3f}s. Extracted {len(extracted_tables)} tables.")

            # Evaluate against Ground Truth
            eval_res = evaluator.evaluate_pipeline_output(
                extracted_tables=extracted_tables,
                gt_spec=gt_spec,
                wall_clock_time=elapsed_s,
                pipeline_name=p_name,
            )
            eval_res["group_id"] = grp["id"]
            eval_res["pipeline_id"] = p_id
            eval_res['input_source'] = ('synthetic_ground_truth' if mineru_mode == 'fixture' else 'live_service') if p_id == 'p2' else 'local_pdf_extraction'
            eval_res['logs'] = logs

            if verbose:
                for l in logs[:5]:
                    print(f"        [Log] {l}")

            results_matrix.append(eval_res)

            # Update aggregation
            agg = aggregated_by_pipeline[p_id]
            agg["total_time_s"] += elapsed_s
            agg["total_gt_tables"] += eval_res["total_gt_tables"]
            agg["total_extracted_tables"] += eval_res["extracted_tables_count"]
            agg["total_missed_tables"] += eval_res["missed_tables_count"]
            agg["total_truncated_rows"] += eval_res["total_truncated_rows"]
            agg["total_sampled_cells"] += eval_res["total_sampled_cells"]
            agg["total_errored_cells"] += eval_res["errored_cells_count"]
            agg["total_false_agreements"] += eval_res["false_agreement_count"]
            agg["group_runs"].append({
                "group_id": grp["id"],
                "group_name": grp["name"],
                "time_s": eval_res["wall_clock_time_s"],
                "missed_tables": eval_res["missed_tables_count"],
                "truncated_rows": eval_res["total_truncated_rows"],
                "value_error_rate_pct": eval_res["value_error_rate_pct"],
                "cell_accuracy_pct": eval_res["cell_accuracy_pct"],
                "false_agreements": eval_res["false_agreement_count"],
            })

    # Finalize aggregations
    for p_id, agg in aggregated_by_pipeline.items():
        tot_cells = agg["total_sampled_cells"]
        err_cells = agg["total_errored_cells"]
        agg["overall_value_error_rate_pct"] = round((err_cells / tot_cells * 100.0) if tot_cells > 0 else 0.0, 2)
        agg["overall_cell_accuracy_pct"] = round(100.0 - agg["overall_value_error_rate_pct"], 2)
        agg["total_time_s"] = round(agg["total_time_s"], 3)

    # Print summary console table
    print_console_summary(target_groups, target_pipelines, results_matrix, aggregated_by_pipeline)

    # Save Reports
    report_data = {
        "benchmark_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        'mineru_mode': mineru_mode,
        'evidence_scope': 'postprocessing_regression' if mineru_mode == 'fixture' else 'synthetic_pdf_live_extraction',
        'mineru_service_latency_included': mineru_mode == 'live',
        "summary": aggregated_by_pipeline,
        "detailed_results": results_matrix,
    }

    json_report_path = output_dir / "benchmark_report.json"
    with open(json_report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)

    md_report_path = output_dir / "benchmark_report.md"
    generate_markdown_report(md_report_path, target_groups, target_pipelines, results_matrix, aggregated_by_pipeline, mineru_mode=mineru_mode)

    print(f"\n[✓] Benchmark Report JSON written to: {json_report_path}")
    print(f"[✓] Benchmark Report Markdown written to: {md_report_path}")

    return report_data


def print_console_summary(groups, pipelines_list, results_matrix, aggregated):
    """Print clean ASCII summary tables to stdout."""
    print("\n" + "=" * 96)
    print("📊 BENCHMARK METRIC 1: SUMMARY COMPARISON ACROSS ALL 5 GROUPS")
    print("=" * 96)
    header = f"{'Pipeline':<40} | {'Missed Tables':<14} | {'Trunc. Rows':<12} | {'Val Error Rate':<15} | {'False Agree':<12} | {'Wall Time':<10}"
    print(header)
    print("-" * 96)
    for p in pipelines_list:
        p_id = p["id"]
        agg = aggregated[p_id]
        row = (
            f"{agg['name'][:40]:<40} | "
            f"{agg['total_missed_tables']:<14} | "
            f"{agg['total_truncated_rows']:<12} | "
            f"{agg['overall_value_error_rate_pct']:>6.2f}% ({agg['total_errored_cells']}/{agg['total_sampled_cells']}) | "
            f"{agg['total_false_agreements']:<12} | "
            f"{agg['total_time_s']:>7.3f}s"
        )
        print(row)
    print("=" * 96)

    print("\n" + "=" * 96)
    print("📋 BENCHMARK METRIC 2: GROUP-BY-GROUP BREAKDOWN")
    print("=" * 96)
    for grp in groups:
        g_id = grp["id"]
        print(f"\n[{g_id}] {grp['name']}:")
        sub_header = f"  {'Pipeline':<38} | {'Missed':<8} | {'Trunc. Rows':<12} | {'Val Error':<12} | {'False Agree':<12} | {'Time (s)':<10}"
        print(sub_header)
        print("  " + "-" * 92)
        for p in pipelines_list:
            res = next((r for r in results_matrix if r["group_id"] == g_id and r["pipeline_id"] == p["id"]), None)
            if res:
                print(
                    f"  {p['name'][:38]:<38} | "
                    f"{res['missed_tables_count']:<8} | "
                    f"{res['total_truncated_rows']:<12} | "
                    f"{res['value_error_rate_pct']:>6.2f}%      | "
                    f"{res['false_agreement_count']:<12} | "
                    f"{res['wall_clock_time_s']:>8.3f}s"
                )
    print("=" * 96 + "\n")


def generate_markdown_report(md_path: Path, groups, pipelines_list, results_matrix, aggregated, *, mineru_mode='fixture'):
    """Write publication-grade Markdown benchmark report."""
    lines = []
    lines.append("# Table Extraction Benchmark Report\n")
    if mineru_mode == 'fixture':
        lines.append('**Scope: postprocessing regression only.** Pipeline 2 consumes synthetic tables generated from ground truth. Its accuracy does not measure MinerU recognition, and its elapsed time excludes model/service processing. Cross-pipeline speedup claims are invalid in this mode.\n\n')
    else:
        lines.append('**Scope: live extraction of synthetic PDFs.** Generated MinerU caches are excluded; elapsed time includes the service call. Generalization to real papers requires independently annotated real documents.\n\n')
    lines.append(f"**Generated at**: `{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}`\n")
    lines.append("## 1. Executive Summary\n")
    lines.append("This benchmark evaluates three distinct PDF table extraction pipelines across 5 representative scientific document categories.\n")

    # Table 1: High Level Comparison
    lines.append("### Pipeline Overall Performance Matrix\n\n")
    lines.append("| Pipeline | Missed Tables (漏表) | Truncated Rows (漏行) | Value Error Rate (数值错误率) | False Agreement (虚假一致) | Wall-Clock Time (耗时) |\n")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: |\n")
    for p in pipelines_list:
        p_id = p["id"]
        agg = aggregated[p_id]
        lines.append(
            f"| **{agg['name']}** | {agg['total_missed_tables']} | {agg['total_truncated_rows']} | "
            f"**{agg['overall_value_error_rate_pct']:.2f}%** ({agg['total_errored_cells']}/{agg['total_sampled_cells']}) | "
            f"{agg['total_false_agreements']} | {agg['total_time_s']:.3f}s |\n"
        )
    lines.append("\n")

    # Report observations from this run, never a prewritten success narrative.
    lines.append("### Observed results\n\n")
    for row in results_matrix:
        lines.append(
            f"- Group {row['group_id']} / {row['pipeline_id']}: "
            f"{row['missed_tables_count']} missed tables, "
            f"{row['total_truncated_rows']} truncated rows, "
            f"{row['value_error_rate_pct']:.2f}% sampled value error.\n"
        )
    lines.append("\n")

    # Detailed Group Breakdown Table
    lines.append("## 2. Quantitative Group-by-Group Breakdown\n\n")
    for grp in groups:
        g_id = grp["id"]
        lines.append(f"### {grp['name']}\n\n")
        lines.append(f"*{grp['desc']}*\n\n")
        lines.append("| Pipeline | Missed Tables | Truncated Rows | Value Error Rate | False Agreement | Wall Time |\n")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: |\n")
        for p in pipelines_list:
            res = next((r for r in results_matrix if r["group_id"] == g_id and r["pipeline_id"] == p["id"]), None)
            if res:
                lines.append(
                    f"| {p['name']} | {res['missed_tables_count']} | {res['total_truncated_rows']} | "
                    f"{res['value_error_rate_pct']:.2f}% | {res['false_agreement_count']} | {res['wall_clock_time_s']:.3f}s |\n"
                )
        lines.append("\n")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Empirical Table Extraction Benchmark Runner")
    parser.add_argument("--pipelines", type=str, default="p1,p2,p3", help="Comma-separated pipelines: p1,p2,p3")
    parser.add_argument("--groups", type=str, default="A,B,C,D,E", help="Comma-separated test groups: A,B,C,D,E")
    parser.add_argument("--output-dir", type=str, default=str(BENCHMARK_DIR / "reports"), help="Output reports directory")
    parser.add_argument("--verbose", action="store_true", help="Print verbose step logs")
    parser.add_argument('--mineru-mode', choices=['fixture', 'live'], default='fixture', help='fixture: synthetic postprocessing regression; live: submit PDFs to MinerU and measure extraction')

    args = parser.parse_args()

    selected_pipelines = [x.strip() for x in args.pipelines.split(",") if x.strip()]
    selected_groups = [x.strip() for x in args.groups.split(",") if x.strip()]
    out_dir = Path(args.output_dir)

    run_benchmark_suite(
        selected_pipelines=selected_pipelines,
        selected_groups=selected_groups,
        output_dir=out_dir,
        verbose=args.verbose,
        mineru_mode=args.mineru_mode,
    )


if __name__ == "__main__":
    main()
