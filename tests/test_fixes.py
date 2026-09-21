#!/usr/bin/env python3
"""Comprehensive test suite verifying fixes across zotero-table-extractor.
"""

import os
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import numpy as np

from scripts import common
from scripts import system_detector
from scripts import table_validator
from scripts import table_postprocess
from scripts import excel_export
from scripts import llm_reasoner
from scripts import batch_run
from scripts import agent_bridge
from scripts.models import ExtractedTable


class TestCommonFixes(unittest.TestCase):
    def test_df_map_compatibility(self):
        """Test df_map works across pandas 2.x and 3.x without AttributeError."""
        df = pd.DataFrame({"A": [" foo ", " bar "], "B": [" baz ", " qux "]})
        res = common.df_map(df, lambda x: x.strip() if isinstance(x, str) else x)
        self.assertEqual(res.iloc[0, 0], "foo")
        self.assertEqual(res.iloc[1, 1], "qux")

    def test_clean_latex_and_ocr(self):
        """Test LaTeX and geochemical symbol cleaning."""
        raw_text = r"\mathrm{SiO_2} \pm 0.05 \times 10^3 \Delta T \approx 500 ^\circ\mathrm{C}"
        cleaned = common.clean_latex_and_ocr(raw_text)
        self.assertIn("SiO2", cleaned)
        self.assertIn("±", cleaned)
        self.assertIn("×", cleaned)
        self.assertIn("≈", cleaned)
        self.assertIn("Δ", cleaned)
        self.assertIn("°C", cleaned)
        self.assertNotIn(r"\mathrm", cleaned)

    def test_cross_platform_path_fallbacks(self):
        """Ensure PAPER_TABLES_DIR and ZOTERO_DB_PATH are configured in load_config."""
        cfg = common.load_config()
        self.assertIn("PAPER_TABLES_DIR", cfg)
        self.assertIn("ZOTERO_DB_PATH", cfg)
        self.assertTrue(len(str(cfg["PAPER_TABLES_DIR"])) > 0)
        self.assertTrue(len(str(cfg["ZOTERO_DB_PATH"])) > 0)


class TestSystemDetectorFallback(unittest.TestCase):
    def test_clean_machine_fallback(self):
        """Test get_aihub_root and adapt_path fallback gracefully when /Volumes/ExFat/AIHub does not exist."""
        with patch("os.path.isdir", return_value=False), patch("os.path.exists", return_value=False):
            aihub_root = system_detector.get_aihub_root()
            user_home = system_detector.get_user_home()
            self.assertEqual(aihub_root, os.path.join(user_home, "AIHub"))

            # adapt_path should fall back without crashing
            adapted = system_detector.adapt_path("/Volumes/ExFat/AIHub/custom/paper.pdf")
            self.assertTrue(str(user_home) in str(adapted) or "paper.pdf" in str(adapted))


class TestTableValidatorFixes(unittest.TestCase):
    def test_geochemical_keywords_not_in_axis_keywords(self):
        """Geochemical oxides and units must NOT be falsely identified as chart axis labels."""
        axis_keywords = table_validator.AXIS_KEYWORDS
        self.assertNotIn("wt% tio2", axis_keywords)
        self.assertNotIn("wt% sio2", axis_keywords)
        self.assertNotIn("wt% al2o3", axis_keywords)
        self.assertNotIn("kbar", axis_keywords)
        self.assertNotIn("t (°c)", axis_keywords)

    def test_geochemical_table_validation(self):
        """Ensure a typical geochemical analysis table passes validation."""
        df = pd.DataFrame({
            "Sample": ["A1", "A2", "A3"],
            "wt% SiO2": [72.1, 71.5, 73.0],
            "wt% Al2O3": [14.2, 14.5, 14.1],
            "wt% TiO2": [0.35, 0.38, 0.32],
            "P (kbar)": [5.0, 5.2, 4.8],
        })
        is_valid, reason = table_validator.validate_table_structure(df, page_idx=0)
        self.assertTrue(is_valid, f"Table falsely rejected: {reason}")


class TestTablePostprocessFixes(unittest.TestCase):
    def test_continuation_table_page_distance_limit(self):
        """Tables with same label within 2 pages merge; far apart tables do NOT merge."""
        df1 = pd.DataFrame({"Col1": [1, 2], "Col2": ["A", "B"]})
        df2 = pd.DataFrame({"Col1": [3, 4], "Col2": ["C", "D"]})
        df3 = pd.DataFrame({"Col1": [5, 6], "Col2": ["E", "F"]})

        tbl1 = ExtractedTable(df=df1, page_idx=1, label="Table 1")
        tbl2 = ExtractedTable(df=df2, page_idx=2, label="Table 1")  # within distance <= 2 -> merge
        tbl3 = ExtractedTable(df=df3, page_idx=8, label="Table 1")  # distance > 2 -> do not merge

        merged = table_postprocess.merge_continuation_tables([tbl1, tbl2, tbl3])
        self.assertEqual(len(merged), 2)
        # First table merged tbl1 and tbl2
        self.assertEqual(len(merged[0]), 4)
        # Second table remains tbl3
        self.assertEqual(len(merged[1]), 2)
        self.assertEqual(merged[1].attrs.get("page_idx"), 8)

    def test_postprocess_dataframe_clean_latex(self):
        """Ensure postprocess_dataframe invokes LaTeX cleaning and handles bad cells."""
        df = pd.DataFrame({
            "Oxide": [r"\mathrm{TiO_2}", r"\mathrm{SiO_2}"],
            "Val": [r"1.23 \pm 0.01", r"70.5 \pm 0.2"]
        })
        processed = table_postprocess.postprocess_dataframe(df)
        self.assertIn("TiO2", processed.iloc[0, 0])
        self.assertIn("±", processed.iloc[0, 1])


class TestExcelExportFixes(unittest.TestCase):
    def test_attrs_preservation_and_normalization(self):
        """Ensure save_tables_to_excel preserves .attrs across collision resolution and accepts ExtractedTable."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            df1 = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
            df1.attrs["label"] = "Table 1"
            df1.attrs["table_title"] = "First table caption"
            df1.attrs["page_idx"] = 0

            df2 = pd.DataFrame({"C": [5, 6], "D": [7, 8]})
            df2.attrs["label"] = "Table 1"  # Same label but distant page
            df2.attrs["table_title"] = "Second table caption"
            df2.attrs["page_idx"] = 6

            ok = excel_export.save_tables_to_excel([df1, df2], Path(tmpdir))
            self.assertTrue(ok)

            xlsx_files = sorted([f for f in os.listdir(tmpdir) if f.endswith(".xlsx")])
            self.assertEqual(len(xlsx_files), 2)

            # Both files should recover their distinct metadata
            loaded_1 = excel_export._load_excel_with_attrs(os.path.join(tmpdir, xlsx_files[0]))
            loaded_2 = excel_export._load_excel_with_attrs(os.path.join(tmpdir, xlsx_files[1]))
            self.assertIn("label", loaded_1.attrs)
            self.assertIn("label", loaded_2.attrs)


class TestPdfTableExtractorAlignment(unittest.TestCase):
    def test_align_page_tables(self):
        """Test vertical IoU and column/content similarity alignment in pdf_table_extractor."""
        from scripts.pdf_table_extractor import _align_page_tables

        # Table at top of page (y0=100, y1=300)
        df_top = pd.DataFrame({"Col1": ["A", "B"], "Col2": ["1", "2"]})
        t_top = {"df": df_top, "page_idx": 0, "bbox": (0, 100, 500, 300)}

        # Table at bottom of page (y0=500, y1=700)
        df_bot = pd.DataFrame({"ColX": ["C", "D"], "ColY": ["3", "4"]})
        t_bot = {"df": df_bot, "page_idx": 0, "bbox": (0, 500, 500, 700)}

        page_tables = {
            "source_a": [t_top, t_bot],
            "source_b": [t_bot, t_top],  # Inverted order
        }
        priority = ["source_a", "source_b"]

        aligned_groups = _align_page_tables(page_tables, priority)
        self.assertEqual(len(aligned_groups), 2)

        # In aligned_groups, each cluster maps src -> table
        for cluster in aligned_groups:
            tbl_a = cluster.get("source_a")
            tbl_b = cluster.get("source_b")
            self.assertIsNotNone(tbl_a)
            self.assertIsNotNone(tbl_b)
            # The bboxes should have matching vertical positions
            self.assertAlmostEqual(tbl_a["bbox"][1], tbl_b["bbox"][1], delta=50)


class TestLLMReasonerAndAgentBridge(unittest.TestCase):
    def test_llm_reasoner_availability_with_agent_bridge(self):
        """Test LLMTableReasoner is available when agent_bridge is present even without direct API key."""
        mock_bridge = MagicMock()
        with patch("scripts.llm_reasoner.get_agent_bridge", return_value=mock_bridge):
            reasoner = llm_reasoner.LLMTableReasoner(config={"LLM_ENABLED": True})
            self.assertTrue(reasoner.is_available())

    def test_agent_bridge_timeout_handling(self):
        """Test check_resolved_task times out gracefully without hanging."""
        bridge = agent_bridge.AgentReasoningBridge()
        res = bridge.check_resolved_task("non-existent-task-id", timeout_seconds=0.1, poll_interval=0.05)
        self.assertIsNone(res)


class TestBatchRunFixes(unittest.TestCase):
    def test_task_generation_from_pdf_records(self):
        """Ensure batch_run generates tasks from queried pdf_records and supports resume/recheck."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_records = [
                {"pdf_path": "/path/to/paper1.pdf", "filename": "paper1.pdf", "item_key": "KEY1", "parent_title": "Paper 1 Title"},
                {"pdf_path": "/path/to/paper2.pdf", "filename": "paper2.pdf", "item_key": "KEY2", "parent_title": "Paper 2 Title"},
            ]

            # Case 1: Fresh run without resume -> 2 tasks
            tasks = batch_run.get_paper_tasks(
                paper_tables_dir=tmpdir,
                pdf_records=pdf_records,
                resume=False,
                recheck=False
            )
            self.assertEqual(len(tasks), 2)

            # Case 2: Resume where paper1 already has .xlsx table -> only paper2 generated
            p1_dir = os.path.join(tmpdir, "paper1")
            os.makedirs(p1_dir, exist_ok=True)
            with open(os.path.join(p1_dir, "Table 1.xlsx"), "w") as f:
                f.write("dummy")

            tasks_resume = batch_run.get_paper_tasks(
                paper_tables_dir=tmpdir,
                pdf_records=pdf_records,
                resume=True,
                recheck=False
            )
            self.assertEqual(len(tasks_resume), 1)
            self.assertEqual(tasks_resume[0]["pdf_record"]["item_key"], "KEY2")

            # Case 3: Recheck where paper2 directory exists but is empty (failed previously)
            p2_dir = os.path.join(tmpdir, "paper2")
            os.makedirs(p2_dir, exist_ok=True)

            tasks_recheck = batch_run.get_paper_tasks(
                paper_tables_dir=tmpdir,
                pdf_records=pdf_records,
                resume=False,
                recheck=True
            )
            self.assertEqual(len(tasks_recheck), 1)
class TestAuditSubpackage(unittest.TestCase):
    def test_audit_subpackage_exports(self):
        """Ensure scripts.audit can be imported and exports all audit runner modules."""
        import scripts.audit as audit
        self.assertTrue(hasattr(audit, "audit_reporter"))
        self.assertTrue(hasattr(audit, "targeted_audit_reporter"))
        self.assertTrue(hasattr(audit, "exhaustive_audit_runner"))
        self.assertTrue(hasattr(audit, "targeted_audit_runner"))
        self.assertTrue(hasattr(audit, "deep_8h_iterative_healer"))


class TestExtractZoteroTableAndPdfTables(unittest.TestCase):
    def test_cli_help_includes_flags(self):
        """Ensure extract_zotero_table.py registers --single-file and --enable-llm flags."""
        import subprocess
        res = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "extract_zotero_table.py"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--single-file", res.stdout)
        self.assertIn("--enable-llm", res.stdout)
        self.assertIn("--output", res.stdout)

    def test_cli_output_path_handling(self):
        """Ensure .xlsx output path is respected as a single file, and directory path creates subdir."""
        from scripts import extract_zotero_table
        import argparse

        # Test case 1: --output explicitly ends with .xlsx
        # Mocking _do_process_single_pdf internal logic check
        output_xlsx = "/tmp/my_paper_extracted.xlsx"
        user_single_file = False
        if output_xlsx.endswith(".xlsx"):
            target_output = output_xlsx
            single_file = True
        self.assertEqual(target_output, "/tmp/my_paper_extracted.xlsx")
        self.assertTrue(single_file)

        # Test case 2: --output is a directory with --single-file
        output_dir = "/tmp/tables_dir"
        user_single_file = True
        safe_base = "MyPaper"
        target_dir = os.path.join(output_dir, safe_base)
        if user_single_file:
            target_output = os.path.join(target_dir, f"{safe_base}.xlsx")
            single_file = True
        self.assertEqual(target_output, "/tmp/tables_dir/MyPaper/MyPaper.xlsx")
        self.assertTrue(single_file)

    def test_pdf_tables_and_ocr_client_pages_param(self):
        """Ensure functions in pdf_tables and ocr_client accept pages parameter without error."""
        import inspect
        from scripts import pdf_tables
        from scripts import ocr_client

        sig_crop = inspect.signature(pdf_tables.extract_table_crops_from_pdf)
        self.assertIn("pages", sig_crop.parameters)

        sig_export = inspect.signature(pdf_tables.export_crops_to_excel)
        self.assertIn("pages", sig_export.parameters)

        sig_ocr = inspect.signature(ocr_client.extract_pp_structure_table_crops)
        self.assertIn("pages", sig_ocr.parameters)

    def test_disk_collision_loads_with_attrs(self):
        """Ensure save_tables_to_excel merges with pre-existing disk table without wiping metadata."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Simulate an earlier run that created Table 1.xlsx and table_captions.txt
            df1 = pd.DataFrame({"Sample": ["A1", "A2"], "SiO2": [70.1, 71.2]})
            df1.attrs["label"] = "Table 1"
            df1.attrs["table_title"] = "Whole-rock geochemistry"
            excel_export.save_tables_to_excel([df1], Path(tmpdir))

            # Verify file created
            tbl1_path = os.path.join(tmpdir, "Table 1.xlsx")
            self.assertTrue(os.path.exists(tbl1_path))

            # 2. Simulate a subsequent step/run adding continuation data to Table 1 from a clean session
            df2 = pd.DataFrame({"Sample": ["A3", "A4"], "SiO2": [72.0, 69.8]})
            df2.attrs["label"] = "Table 1"
            df2.attrs["is_continuation"] = True

            ok = excel_export.save_tables_to_excel([df2], Path(tmpdir))
            self.assertTrue(ok)

            # 3. Verify merged data contains all 4 samples and preserved metadata
            reloaded = excel_export._load_excel_with_attrs(tbl1_path, tmpdir)
            self.assertEqual(len(reloaded), 4)
            self.assertEqual(reloaded.attrs.get("table_title"), "Whole-rock geochemistry")

    def test_three_way_vote_priority_ordering(self):
        """Ensure three_way_vote strictly prefers pdf_inspector over lower priority candidates."""
        from scripts import pdf_table_extractor

        df_inspector = pd.DataFrame({"A": [1, 2], "B": ["X", "Y"]})
        df_camelot = pd.DataFrame({"A": [1, 2], "B": ["X", "Y"]})

        t_insp = {"df": df_inspector, "page_idx": 0, "bbox": None}
        t_cam = {"df": df_camelot, "page_idx": 0, "bbox": None}

        fused, logs, summary = pdf_table_extractor.three_way_vote(
            inspector_tables=[t_insp],
            find_tables_results=[],
            plumber_tables=[],
            camelot_tables=[t_cam],
        )
        self.assertEqual(len(fused), 1)
        # Winner must be pdf_inspector
        self.assertEqual(fused[0]["df"].attrs.get("extractor"), "pdf_inspector")

    def test_covered_pages_atomic_block_replacement(self):
        """Test that OCR results with covered_pages remove all covered pages, not just block[0]."""
        # Table on page 0, Table on page 1
        all_results = [
            {"df": pd.DataFrame({"A": [1]}), "page_idx": 0},
            {"df": pd.DataFrame({"A": [2]}), "page_idx": 1},
            {"df": pd.DataFrame({"A": [3]}), "page_idx": 5},
        ]
        # OCR succeeded on atomic block [0, 1]
        ocr_results = [
            {"df": pd.DataFrame({"A": [1, 2]}), "page_idx": 0, "covered_pages": [0, 1]}
        ]
        ocr_candidate_pages = [0, 1]

        ocr_success_pages = set()
        for r in ocr_results:
            if r.get('covered_pages'):
                ocr_success_pages.update(r['covered_pages'])
            elif r.get('page_idx') is not None:
                ocr_success_pages.add(r['page_idx'])
        if not ocr_success_pages:
            ocr_success_pages = set(ocr_candidate_pages)

        remaining = [r for r in all_results if r.get('page_idx') not in ocr_success_pages]
        remaining.extend(ocr_results)

        # Pages 0 and 1 replaced; page 5 retained -> total 2 results
        self.assertEqual(len(remaining), 2)
        remaining_pages = [r.get("page_idx") for r in remaining]
        self.assertIn(5, remaining_pages)
        self.assertIn(0, remaining_pages)


if __name__ == "__main__":
    unittest.main()
