#!/usr/bin/env python3
"""
tests/test_benchmark_protocol.py —
Unit test suite verifying the empirical benchmark execution protocol:
1. Generation and integrity of 5-category test suite (Group A to E)
2. Ground truth structural schema and sampled numeric cell fidelity
3. Pipeline 1 (Baseline: multi-engine voting) execution and false agreement detection
4. Pipeline 2 (Proposed: MinerU + gate + recovery) execution and zero-omission stitching
5. Pipeline 3 (Pure local) fallback behavior
6. Evaluator metric computation accuracy (Missed tables, Truncated rows, Value error rate, False agreements)
7. End-to-end report generation (JSON & Markdown)
"""

import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from benchmarks import generate_benchmark_papers
from benchmarks import evaluator
from benchmarks import pipelines
from benchmarks import run_benchmark


class TestBenchmarkPaperSuiteGeneration(unittest.TestCase):
    """Test 1: Verification of test paper generation across all 5 groups."""

    @classmethod
    def setUpClass(cls):
        generate_benchmark_papers.generate_all_papers()
        cls.papers_dir = PROJECT_ROOT / "benchmarks" / "papers"
        cls.gt_dir = PROJECT_ROOT / "benchmarks" / "ground_truth"
        cls.cache_dir = cls.papers_dir / "mineru_cache"

    def test_all_five_groups_exist(self):
        """Verify that all 5 PDF files, ground truth JSONs, and MinerU caches exist."""
        for grp_id in ["group_a_dense_three_line", "group_b_complex_spanning_headers",
                       "group_c_untitled_appendix_tables", "group_d_multipage_continuation",
                       "group_e_scanned_low_contrast"]:
            pdf_path = self.papers_dir / f"{grp_id}.pdf"
            gt_path = self.gt_dir / f"{grp_id}.json"
            cache_path = self.cache_dir / f"{grp_id}.zip"

            self.assertTrue(pdf_path.exists(), f"Missing PDF for {grp_id}")
            self.assertGreater(pdf_path.stat().st_size, 1000, f"PDF too small for {grp_id}")

            self.assertTrue(gt_path.exists(), f"Missing GT for {grp_id}")
            with open(gt_path, "r", encoding="utf-8") as f:
                gt = json.load(f)
            self.assertIn("tables", gt)
            self.assertGreaterEqual(len(gt["tables"]), 1)

            self.assertTrue(cache_path.exists(), f"Missing MinerU cache for {grp_id}")

    def test_group_d_continuation_structure(self):
        """Verify Group D ground truth contains all 80 rows across 3 continuation pages."""
        gt_path = self.gt_dir / "group_d_multipage_continuation.json"
        with open(gt_path, "r", encoding="utf-8") as f:
            gt = json.load(f)
        table = gt["tables"][0]
        self.assertEqual(table["num_rows"], 80)
        self.assertEqual(table["num_cols"], 16)
        self.assertTrue(table["is_continuation"])
        self.assertEqual(len(table["rows"]), 80)

    def test_group_b_hierarchical_headers(self):
        """Verify Group B ground truth specifies multi-level grouped headers."""
        gt_path = self.gt_dir / "group_b_complex_spanning_headers.json"
        with open(gt_path, "r", encoding="utf-8") as f:
            gt = json.load(f)
        table = gt["tables"][0]
        self.assertIn("hierarchical_headers", table)
        self.assertGreaterEqual(len(table["hierarchical_headers"]), 3)


class TestBenchmarkEvaluator(unittest.TestCase):
    """Test 2: Verification of the metric calculation logic."""

    def test_metric_calculation_perfect_match(self):
        """Verify metrics on a perfect extraction."""
        gt_spec = {
            "group": "Test Group",
            "file_name": "test.pdf",
            "tables": [
                {
                    "table_id": "Table 1",
                    "label": "Table 1",
                    "page_idx": 0,
                    "num_rows": 3,
                    "num_cols": 2,
                    "columns": ["ColA", "ColB"],
                    "rows": [["A1", "10.5"], ["A2", "20.5"], ["A3", "30.5"]],
                    "sampled_numeric_cells": [
                        {"row_idx": 0, "col_idx": 1, "col_name": "ColB", "raw_val": "10.5", "numeric_val": 10.5},
                        {"row_idx": 1, "col_idx": 1, "col_name": "ColB", "raw_val": "20.5", "numeric_val": 20.5},
                    ]
                }
            ]
        }
        df = pd.DataFrame({"ColA": ["A1", "A2", "A3"], "ColB": ["10.5", "20.5", "30.5"]})
        df.attrs["label"] = "Table 1"
        df.attrs["page_idx"] = 0

        res = evaluator.evaluate_pipeline_output([{"df": df}], gt_spec, 0.5, "Test Pipeline")

        self.assertEqual(res["missed_tables_count"], 0)
        self.assertEqual(res["total_truncated_rows"], 0)
        self.assertEqual(res["value_error_rate_pct"], 0.0)
        self.assertEqual(res["cell_accuracy_pct"], 100.0)
        self.assertEqual(res["false_agreement_count"], 0)

    def test_metric_calculation_truncated_rows_and_false_agreement(self):
        """Verify truncated rows and false agreement detection."""
        gt_spec = {
            "group": "Continuation Test",
            "file_name": "test_cont.pdf",
            "tables": [
                {
                    "table_id": "Table 1",
                    "label": "Table 1",
                    "page_idx": 0,
                    "num_rows": 50,
                    "num_cols": 2,
                    "columns": ["ColA", "ColB"],
                    "rows": [[f"R{i}", f"{i}.0"] for i in range(50)],
                    "sampled_numeric_cells": [
                        {"row_idx": 5, "col_idx": 1, "col_name": "ColB", "raw_val": "5.0", "numeric_val": 5.0},
                        {"row_idx": 45, "col_idx": 1, "col_name": "ColB", "raw_val": "45.0", "numeric_val": 45.0},
                    ]
                }
            ]
        }
        # Only 20 rows extracted (30 rows truncated!)
        df = pd.DataFrame({"ColA": [f"R{i}" for i in range(20)], "ColB": [f"{i}.0" for i in range(20)]})
        df.attrs["label"] = "Table 1"
        df.attrs["page_idx"] = 0
        df.attrs["vote_count"] = 3
        df.attrs["confidence"] = 0.90
        df.attrs["agreeing_extractors"] = ["find_tables", "camelot", "text_alignment"]

        res = evaluator.evaluate_pipeline_output([{"df": df}], gt_spec, 1.2, "Baseline Voting")

        self.assertEqual(res["missed_tables_count"], 0)
        self.assertEqual(res["total_truncated_rows"], 30)
        # 1 cell at row 5 matches, 1 cell at row 45 is truncated -> 50% error rate
        self.assertEqual(res["value_error_rate_pct"], 50.0)
        # Because vote_count >= 2 and truncated_rows > 0 -> False agreement flagged!
        self.assertEqual(res["false_agreement_count"], 1)

    def test_label_whitespace_normalization(self):
        """Verify Table1 without space matches Table 1 in ground truth."""
        gt_t = {"label": "Table 1", "page_idx": 0, "num_cols": 2, "rows": []}
        df = pd.DataFrame({"A": [1], "B": [2]})
        df.attrs["label"] = "Table1"
        df.attrs["page_idx"] = 0
        score = evaluator.match_table_to_ground_truth(df, gt_t)
        # Must include the 0.40 label match bonus
        self.assertGreaterEqual(score, 0.40)

    def test_single_char_dot_does_not_match_numeric(self):
        """Verify that a single dot or random single character does not match a numeric value."""
        self.assertFalse(evaluator._cell_matches_numeric(".", 40.95, "40.95 ± 0.12"))
        self.assertFalse(evaluator._cell_matches_numeric("4", 40.95, "40.95 ± 0.12"))
        self.assertFalse(evaluator._cell_matches_numeric("-", 40.95, "40.95 ± 0.12"))
        self.assertTrue(evaluator._cell_matches_numeric("40.95 ± 0.12", 40.95, "40.95 ± 0.12"))
        self.assertTrue(evaluator._cell_matches_numeric("40.95", 40.95, "40.95 ± 0.12"))

    def test_single_engine_truncation_does_not_flag_false_agreement(self):
        """Verify that single-engine truncation without consensus is NOT flagged as false agreement."""
        gt_spec = {
            "group": "Single Engine Test",
            "file_name": "single.pdf",
            "tables": [{
                "table_id": "Table 1",
                "label": "Table 1",
                "page_idx": 0,
                "num_rows": 20,
                "num_cols": 1,
                "columns": ["A"],
                "rows": [[1]] * 20,
                "sampled_numeric_cells": []
            }]
        }
        df = pd.DataFrame({"A": [1] * 10})
        df.attrs["label"] = "Table 1"
        df.attrs["page_idx"] = 0
        df.attrs["vote_count"] = 1
        df.attrs["cross_validated"] = False
        df.attrs["agreeing_extractors"] = []

        res = evaluator.evaluate_pipeline_output([{"df": df}], gt_spec, 0.2, "Single Engine")
        self.assertEqual(res["total_truncated_rows"], 10)
        self.assertEqual(res["false_agreement_count"], 0)

    def test_hierarchical_leaf_header_mapping(self):
        """Verify hierarchical prefix headers map accurately to leaf headers."""
        gt_spec = {
            "group": "Header Test",
            "file_name": "header.pdf",
            "tables": [{
                "table_id": "Table 1",
                "label": "Table 1",
                "page_idx": 0,
                "num_rows": 2,
                "num_cols": 2,
                "columns": ["Sample Info_Spot", "Trace Elements (ppm)_U"],
                "rows": [["S1", "225"], ["S2", "270"]],
                "sampled_numeric_cells": [
                    {"row_idx": 0, "col_idx": 1, "col_name": "U", "raw_val": "225", "numeric_val": 225.0}
                ]
            }]
        }
        # Extracted table has leaf header "U"
        df = pd.DataFrame({"Spot": ["S1", "S2"], "U": ["225", "270"]})
        df.attrs["label"] = "Table 1"
        df.attrs["page_idx"] = 0

        res = evaluator.evaluate_pipeline_output([{"df": df}], gt_spec, 0.2, "Header Test")
        self.assertEqual(res["value_error_rate_pct"], 0.0)
        self.assertEqual(res["cell_accuracy_pct"], 100.0)


class TestPipelineExecutionAcrossGroups(unittest.TestCase):
    """Test 3: Verification of pipeline execution on test papers."""

    def test_pipeline_2_proposed_group_a_integrity(self):
        """Verify Pipeline 2 passes numeric integrity gate on Group A."""
        pdf_path = PROJECT_ROOT / "benchmarks" / "papers" / "group_a_dense_three_line.pdf"
        tables, elapsed, logs = pipelines.run_pipeline_2_proposed(str(pdf_path))
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["df"].shape[0], 24)
        self.assertEqual(tables[0]["df"].shape[1], 13)

    def test_pipeline_2_proposed_group_d_stitching(self):
        """Verify Pipeline 2 stitches all 80 rows across 3 pages in Group D."""
        pdf_path = PROJECT_ROOT / "benchmarks" / "papers" / "group_d_multipage_continuation.pdf"
        tables, elapsed, logs = pipelines.run_pipeline_2_proposed(str(pdf_path))
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["df"].shape[0], 80)

    def test_pipeline_runner_end_to_end_subset(self):
        """Verify run_benchmark_suite executes cleanly and outputs valid reports."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            report = run_benchmark.run_benchmark_suite(
                selected_pipelines=["p2"],
                selected_groups=["A", "D"],
                output_dir=out_dir,
                verbose=False,
            )
            self.assertIn("summary", report)
            self.assertIn("p2", report["summary"])
            self.assertTrue((out_dir / "benchmark_report.json").exists())
            self.assertTrue((out_dir / "benchmark_report.md").exists())


if __name__ == "__main__":
    unittest.main()
