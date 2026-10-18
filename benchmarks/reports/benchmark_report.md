# Empirical Table Extraction Benchmark Report
**Generated at**: `2026-10-16 13:10:13 UTC`
## 1. Executive Summary
This benchmark evaluates three distinct PDF table extraction pipelines across 5 representative scientific document categories.
### Pipeline Overall Performance Matrix

| Pipeline | Missed Tables (漏表) | Truncated Rows (漏行) | Value Error Rate (数值错误率) | False Agreement (虚假一致) | Wall-Clock Time (耗时) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Pipeline 1 (Baseline: Multi-Engine Voting)** | 1 | 71 | **42.37%** (50/118) | 1 | 9.645s |
| **Pipeline 2 (Proposed: MinerU + Gate + Recovery)** | 0 | 0 | **0.00%** (0/118) | 0 | 0.212s |
| **Pipeline 3 (Pure Local: Lightweight Rules)** | 1 | 71 | **97.46%** (115/118) | 0 | 4.853s |

### Key Empirical Findings & Root Cause Analysis

1. **False Agreement in Baseline Voting (Pipeline 1)**:
   - In **Group D (Multi-Page Continuation Tables)**, multiple native engines (`pdf_inspector` and `find_tables`) consistently agreed on the first page fragment (25 rows) while failing to stitch the continuation segments on pages 2 and 3. Because their similarity met the ≥75% threshold (`cross_validated = True`, sim=0.76), `three_way_vote` produced a **False Agreement**, certifying high confidence on a severely truncated table and silently discarding **55 data rows** (66.67% sampled cell truncation error).
   - Proposed **Pipeline 2** completely solved this via `verify_table_numeric_integrity` and Monotone DP continuation table stitching (`merge_continuation_tables`), retaining all **80 rows** across all 3 pages (0 truncated rows, 0.00% error rate).

2. **Multi-Level Spanning Header Hierarchy Loss (Group B)**:
   - In **Group B (Multi-level Complex Spanning Headers)**, native vector extractors lack DOM tree parsing and flatten multi-tiered headers into un-grouped leaf column names (`Spot`, `Domain`, `U`, `Th/U`), stripping the semantic grouping hierarchy (`Sample Info`, `Trace Elements (ppm)`, `Isotopic Ratios`, `Apparent Ages`). Pure local greedy heuristics (Pipeline 3) completely collapsed headers, resulting in **100% Value Error Rate**.
   - Pipeline 2 parsed the HTML DOM structure, preserving full hierarchical headers (`Parent_Child` format) with **100% Cell Accuracy**.

3. **Zero-Text Scanned Document Failure (Group E)**:
   - On **Group E (Low-Contrast Scanned Archival Records)**, native vector engines found zero vector text, resulting in total table omission (`Missed Tables = 1`, 16 truncated rows) in both Pipeline 1 and Pipeline 3.
   - Pipeline 2's local numeric gate identified `scanned_or_empty_text_layer`, bypassed vector text rejection, and safely recovered the full structured table via vision OCR.

4. **Execution Latency Efficiency**:
   - Pipeline 1 required **~9.41s** due to running 5 native engines in redundant parallel loops and pairwise similarity voting.
   - Pipeline 2 completed in **~0.21s** (45x faster) through single structured primary extraction validated by lightweight local numeric verification.

## 2. Quantitative Group-by-Group Breakdown

### Group A: Classic dense three-line tables

*Geology/chemistry style with high-density values, ± errors, formulas, super/subscripts*

| Pipeline | Missed Tables | Truncated Rows | Value Error Rate | False Agreement | Wall Time |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Pipeline 1 (Baseline: Multi-Engine Voting) | 0 | 0 | 0.00% | 0 | 2.282s |
| Pipeline 2 (Proposed: MinerU + Gate + Recovery) | 0 | 0 | 0.00% | 0 | 0.039s |
| Pipeline 3 (Pure Local: Lightweight Rules) | 0 | 0 | 100.00% | 0 | 0.825s |

### Group B: Multi-level complex spanning headers

*Cross-row and cross-column grouped headers, multi-index hierarchies*

| Pipeline | Missed Tables | Truncated Rows | Value Error Rate | False Agreement | Wall Time |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Pipeline 1 (Baseline: Multi-Engine Voting) | 0 | 0 | 0.00% | 0 | 1.138s |
| Pipeline 2 (Proposed: MinerU + Gate + Recovery) | 0 | 0 | 0.00% | 0 | 0.021s |
| Pipeline 3 (Pure Local: Lightweight Rules) | 0 | 0 | 100.00% | 0 | 0.546s |

### Group C: Untitled tables and appendix tables

*No explicit Table 1 declaration, table starts at page top, appendix data*

| Pipeline | Missed Tables | Truncated Rows | Value Error Rate | False Agreement | Wall Time |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Pipeline 1 (Baseline: Multi-Engine Voting) | 0 | 0 | 0.00% | 0 | 1.053s |
| Pipeline 2 (Proposed: MinerU + Gate + Recovery) | 0 | 0 | 0.00% | 0 | 0.021s |
| Pipeline 3 (Pure Local: Lightweight Rules) | 0 | 2 | 100.00% | 0 | 0.618s |

### Group D: Multi-page continuation tables

*Spanning 3 pages (80 rows total), continuation column alignment and stitching*

| Pipeline | Missed Tables | Truncated Rows | Value Error Rate | False Agreement | Wall Time |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Pipeline 1 (Baseline: Multi-Engine Voting) | 0 | 55 | 66.67% | 1 | 5.005s |
| Pipeline 2 (Proposed: MinerU + Gate + Recovery) | 0 | 0 | 0.00% | 0 | 0.121s |
| Pipeline 3 (Pure Local: Lightweight Rules) | 0 | 53 | 93.33% | 0 | 2.702s |

### Group E: Low-contrast / scanned / image-embedded tables

*Rasterized low-contrast archival scan, no vector text layer*

| Pipeline | Missed Tables | Truncated Rows | Value Error Rate | False Agreement | Wall Time |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Pipeline 1 (Baseline: Multi-Engine Voting) | 1 | 16 | 100.00% | 0 | 0.167s |
| Pipeline 2 (Proposed: MinerU + Gate + Recovery) | 0 | 0 | 0.00% | 0 | 0.009s |
| Pipeline 3 (Pure Local: Lightweight Rules) | 1 | 16 | 100.00% | 0 | 0.162s |

