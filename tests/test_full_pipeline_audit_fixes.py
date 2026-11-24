import io
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import pymupdf as fitz
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
for p in (REPO_ROOT, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts import common
from scripts import doclayout_yolo_detector as yolo_mod
from scripts import extract_zotero_table as ezt
from scripts import ocr_client
from scripts import pdf_table_extractor as pte
from scripts import pdf_tables
from scripts import table_validator
from scripts.online import strategies


def _make_text_pdf(path: str, page_texts: list, image_pages: dict = None):
    """Helper to generate synthetic PDFs with exact text (including CJK) and optional raster images."""
    doc = fitz.open()
    image_pages = image_pages or {}
    for idx, txt in enumerate(page_texts):
        page = doc.new_page(width=595, height=842)
        if txt:
            has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in txt)
            if has_cjk:
                page.insert_textbox(fitz.Rect(40, 40, 555, 780), txt, fontsize=10, fontname="china-s")
            else:
                page.insert_textbox(fitz.Rect(40, 40, 555, 780), txt, fontsize=10)
        if idx in image_pages:
            rect = image_pages[idx]
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 120), 1)
            page.insert_image(rect, pixmap=pix)
    doc.save(path)
    doc.close()


class TestFullPipelineAuditFixes(unittest.TestCase):
    """Regression tests for all 10 root causes (P0-1 through P1-10) from the 290-paper full pipeline audit."""

    # ------------------------------------------------------------------
    # P0-1: PP-StructureV3 page subset upload & double-invocation guard
    # ------------------------------------------------------------------
    def test_p0_1_pp_structure_crops_uploads_subset_pdf_and_remaps_pages(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "eight_pages.pdf")
            _make_text_pdf(pdf_path, [f"Page {i+1} content" for i in range(8)])

            submitted_page_counts = []
            submitted_paths = []

            def fake_pp_api(target_file, config=None, timeout=600, cancel_event=None):
                submitted_paths.append(target_file)
                with fitz.open(target_file) as d:
                    submitted_page_counts.append(len(d))
                return {
                    "success": True,
                    "pages": [
                        {"page_idx": 0, "markdown": "Table 1\n| A | B |\n|---|---|\n| 1 | 2 |", "images": {"table_0.png": "http://example.invalid/t0.png"}},
                        {"page_idx": 1, "markdown": "Table 2\n| C | D |\n|---|---|\n| 3 | 4 |", "images": {"table_1.png": "http://example.invalid/t1.png"}},
                    ],
                }

            buf = io.BytesIO()
            Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
            png_bytes = buf.getvalue()
            mock_resp = MagicMock(status_code=200, content=png_bytes)

            with patch.object(ocr_client, "call_pp_structure_v3_api", side_effect=fake_pp_api), \
                 patch.object(ocr_client.requests, "get", return_value=mock_resp):
                crops = ocr_client.extract_pp_structure_table_crops(pdf_path, config={}, pages=[2, 5])

            self.assertEqual(submitted_page_counts, [2], "Must upload a 2-page subset PDF, not all 8 pages")
            self.assertNotEqual(submitted_paths[0], pdf_path)
            self.assertFalse(os.path.exists(submitted_paths[0]), "Temp subset PDF must be cleaned up")
            self.assertEqual([c["page_index"] for c in crops], [2, 5], "Subset indices 0,1 must remap to 2,5")

            # Out-of-range page selection must return [] without calling cloud API
            with patch.object(ocr_client, "call_pp_structure_v3_api") as mock_api:
                empty_crops = ocr_client.extract_pp_structure_table_crops(pdf_path, config={}, pages=[99])
            self.assertEqual(empty_crops, [])
            mock_api.assert_not_called()

    def test_p0_1_extract_table_crops_no_double_pp_structure_call(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "three_pages.pdf")
            _make_text_pdf(pdf_path, ["Page 1", "Page 2", "Page 3"])

            mock_det = MagicMock()
            mock_det.extract_extended_table_regions.return_value = []

            with patch.object(pdf_tables, "DocLayoutYoloDetector", return_value=mock_det), \
                 patch.object(pdf_tables, "extract_pp_structure_table_crops", return_value=[]) as mock_pp:
                res = pdf_tables.extract_table_crops_from_pdf(
                    pdf_path, pages=[0, 1], enable_filter=False, config={"USE_PP_STRUCTURE": True}
                )

            self.assertEqual(res, [])
            self.assertEqual(mock_pp.call_count, 1, "PP-StructureV3 must not be called twice when YOLO returns 0 crops")

    def test_p0_1_step5_skips_pages_already_scanned_in_step3b(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "two_pages.pdf")
            _make_text_pdf(pdf_path, ["Table 1. Summary\nSample Value\nA 1.0", "Table 2. Missing\nSome text"])

            df0 = pd.DataFrame([["A", "1.0"], ["B", "2.0"]], columns=["Sample", "Value"])
            df0.attrs.update({"label": "Table 1", "page_idx": 0, "extractor": "pymupdf"})

            def fake_crops(path, pages=None, cancel_event=None, allow_fullpage_fallback=True, time_budget_s=None, attempted_pages=None):
                if attempted_pages is not None and pages:
                    attempted_pages.update(pages)
                return []

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0, "pages_with_tables": [1, 2], "tables": [], "structure_tables": []
                 }), \
                 patch.object(pte, "extract_via_structure_tree", return_value=[]), \
                 patch.object(pte, "extract_via_find_tables", return_value=[{"df": df0, "page_idx": 0, "table_idx": 0, "bbox": [40, 40, 300, 140]}]), \
                 patch.object(pte, "extract_via_pdfplumber", return_value=[]), \
                 patch.object(pte, "extract_via_camelot", return_value=[]), \
                 patch.object(pte, "extract_via_img2table", return_value=[]), \
                 patch.object(pte, "extract_via_gmft", return_value=[]), \
                 patch.object(pte, "extract_via_pymupdf4llm", return_value=[]), \
                 patch.object(pte, "extract_via_rapid_table", return_value=[]), \
                 patch.object(pte, "extract_via_text_alignment", return_value=[]), \
                 patch.object(ocr_client, "is_mineru_available", return_value=False), \
                 patch.object(ocr_client, "extract_via_docling", return_value=[]), \
                 patch.object(pte, "extract_via_paddleocr_crops", side_effect=fake_crops) as mock_crops, \
                 patch.object(pte, "extract_via_ocr", return_value=[]) as mock_step5:
                results, logs = pte._do_extract_tables_from_pdf(pdf_path, use_ocr_fallback=True)

            self.assertEqual(mock_crops.call_count, 1)
            mock_step5.assert_not_called()
            self.assertTrue(any("Step 5" in l and ("去重保护" in l or "跳过冗余全局 OCR" in l) for l in logs))

    # ------------------------------------------------------------------
    # P0-2: PaddleOCR-VL controlled concurrency & non-interactive Agent Bridge
    # ------------------------------------------------------------------
    def test_p0_2_paddleocr_crops_concurrent_execution(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "crops.pdf")
            _make_text_pdf(pdf_path, ["Page 1", "Page 2", "Page 3"])
            crops = [
                {"page_index": i, "image": Image.new("RGB", (60, 60), "white"), "crop_bbox": [0, 0, 60, 60], "pdf_bbox": [0, 0, 60, 60]}
                for i in range(3)
            ]

            in_flight = 0
            max_in_flight = 0
            lock = threading.Lock()

            def slow_vlm(path, **kwargs):
                nonlocal in_flight, max_in_flight
                with lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                try:
                    time.sleep(0.20)
                    return "| ColA | ColB |\n|---|---|\n| 1 | 2 |"
                finally:
                    with lock:
                        in_flight -= 1

            import pdf_tables as top_pdf_tables
            import ocr_client as top_ocr_client
            with patch.object(pdf_tables, "extract_table_crops_from_pdf", return_value=crops), \
                 patch.object(top_pdf_tables, "extract_table_crops_from_pdf", return_value=crops), \
                 patch.object(pte.ocr_client, "call_paddleocr_vl_online_api", side_effect=slow_vlm), \
                 patch.object(top_ocr_client, "call_paddleocr_vl_online_api", side_effect=slow_vlm), \
                 patch.object(pte, "verify_and_rescue_table_with_agent", side_effect=lambda df, *a: df), \
                 patch.object(pte, "extract_via_paddleocr_fullpage", return_value=[]):
                t0 = time.monotonic()
                res = pte.extract_via_paddleocr_crops(pdf_path, pages=[0, 1, 2], allow_fullpage_fallback=False)
                elapsed = time.monotonic() - t0

            self.assertEqual(len(res), 3)
            self.assertEqual([r["page_idx"] for r in res], [0, 1, 2], "Results must keep deterministic page ordering")
            self.assertGreaterEqual(max_in_flight, 2, f"Expected concurrent VLM workers (>=2 in flight), saw max_in_flight={max_in_flight}")
            self.assertLess(elapsed, 0.75, f"Expected parallel execution (not sequential ~0.6s+overhead), took {elapsed:.3f}s")

    def test_p0_2_agent_bridge_skipped_in_non_interactive_batch_mode(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "dummy.pdf")
            _make_text_pdf(pdf_path, ["Table 1"])
            # Trigger G1 alarm (unnamed columns >= 35%)
            bad_df = pd.DataFrame([["1", "2"], ["3", "4"]], columns=["Unnamed: 0", "Unnamed: 1"])

            fake_stdin = MagicMock()
            fake_stdin.isatty.return_value = False
            env_clean = {
                k: v for k, v in os.environ.items()
                if k not in ("AGENT_INTERACTIVE", "AGENT_BRIDGE_WAIT", "AGENT_BRIDGE_ENABLED", "LLM_TABLE_REASONER_ENABLED")
            }
            with patch.dict(os.environ, env_clean, clear=True), \
                 patch.object(sys, "stdin", fake_stdin), \
                 patch("scripts.agent_bridge.get_agent_bridge") as mock_get_bridge, \
                 patch("agent_bridge.get_agent_bridge") as mock_get_bridge2:
                out = pte.verify_and_rescue_table_with_agent(bad_df, pdf_path, 0, "Table 1")

            self.assertIs(out, bad_df)
            mock_get_bridge.assert_not_called()
            mock_get_bridge2.assert_not_called()

    # ------------------------------------------------------------------
    # P0-3: Step 2 engine timeout guardrail & GMFT TF/oneDNN suppression
    # ------------------------------------------------------------------
    def test_p0_3_engine_timeout_guardrail_isolates_hung_engine(self):
        logs = []

        def hung_engine():
            time.sleep(2.0)
            return [{"df": pd.DataFrame([[1, 2]], columns=["A", "B"]), "page_idx": 0}]

        t0 = time.monotonic()
        res = pte._run_engine_with_timeout(hung_engine, timeout_s=0.15, engine_name="GMFT", logs=logs)
        elapsed = time.monotonic() - t0

        self.assertEqual(res, [])
        self.assertLess(elapsed, 0.8)
        self.assertTrue(any("GMFT" in l and "超时" in l for l in logs))

    # ------------------------------------------------------------------
    # P0-4: DocLayout-YOLO ONNX thread cap & candidate page cap
    # ------------------------------------------------------------------
    def test_p0_4_doclayout_yolo_configures_onnx_thread_limits(self):
        fake_ort = MagicMock()
        fake_opts = MagicMock()
        fake_ort.SessionOptions.return_value = fake_opts
        fake_ort.ExecutionMode.ORT_SEQUENTIAL = "SEQ"
        fake_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = "ALL"

        with patch.object(yolo_mod, "ort", fake_ort), \
             patch.dict(os.environ, {"DOCLAYOUT_ONNX_THREADS": "3"}):
            opts = yolo_mod._build_onnx_session_options()
            self.assertIs(opts, fake_opts)
            self.assertEqual(fake_opts.intra_op_num_threads, 3)
            self.assertEqual(fake_opts.inter_op_num_threads, 1)
            self.assertEqual(fake_opts.execution_mode, "SEQ")

            yolo_mod._create_onnx_session("dummy.onnx", ["CPUExecutionProvider"])
            fake_ort.InferenceSession.assert_called_once_with(
                "dummy.onnx", sess_options=fake_opts, providers=["CPUExecutionProvider"]
            )

    # ------------------------------------------------------------------
    # P0-5: Global PDF budget & online table preservation on local timeout
    # ------------------------------------------------------------------
    def test_p0_5_local_pdf_timeout_preserves_and_saves_online_tables(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "paper.pdf")
            out_dir = os.path.join(td, "out_tables")
            _make_text_pdf(
                pdf_path,
                [
                    "Table 1. Major element geochemistry\nSample SiO2\nA 50.1",
                    "Table 2. Trace element data\nSample Sb\nA 12.4",
                ],
            )
            online_df1 = pd.DataFrame([["A", "50.1"], ["B", "52.3"]], columns=["Sample", "SiO2"])
            online_df1.attrs.update({"label": "Table 1", "table_title": "Table 1. Major element geochemistry", "source": "online"})

            def hung_pdf_pipeline(*args, **kwargs):
                time.sleep(3.0)
                return [], []

            args = MagicMock()
            args.pdf = pdf_path
            args.output = out_dir
            args.title = "Test Paper"
            args.doi = "10.1016/j.test.2026.001"
            args.url = "https://www.sciencedirect.com/science/article/pii/S0000000000000001"
            args.table_idx = "all"
            args.headers = None
            args.online_only = False
            args.pdf_only = False
            args.no_online = False
            args.skip_supplementary = False
            args.structured_file = None
            args.zotero_sync = False
            args.db_path = None
            args.sequential = False
            args.single_file = False
            args.sync_zotero = False
            args.no_sync = True

            with patch.dict(os.environ, {"PDF_EXTRACTION_BUDGET_S": "0.2"}), \
                 patch.object(ezt.online, "extract_tables_online", return_value=([online_df1], "Test Paper")), \
                 patch.object(ezt, "extract_tables_from_pdf", side_effect=hung_pdf_pipeline), \
                 patch.object(ezt, "_preprocess_local_pdf", return_value=(pdf_path, None, False)):
                t0 = time.monotonic()
                ok = ezt.process_single_pdf(pdf_path, args)
                elapsed = time.monotonic() - t0

            self.assertTrue(ok, "Must return True and save already-extracted online tables when local PDF times out")
            self.assertLess(elapsed, 1.5, f"Budget guard must return promptly, took {elapsed:.2f}s")
            saved_paths = []
            for root_d, _, fns in os.walk(out_dir):
                saved_paths.extend([os.path.join(root_d, f) for f in fns if f.endswith(".xlsx")])
            self.assertTrue(saved_paths, "Online table must be saved to an .xlsx workbook")
            sheets = pd.read_excel(saved_paths[0], sheet_name=None)
            self.assertTrue(
                "Table 1" in sheets or any(os.path.basename(p) == "Table 1.xlsx" for p in saved_paths),
                f"Expected Table 1 in saved outputs, got files={saved_paths}, sheets={list(sheets.keys())}",
            )

    def test_p0_6_online_complete_with_figure_pages_skips_local_pdf(self):
        """Modern text-based PDF with full-page figures/plates and 100% online tables must finish without local PDF fallback."""
        with tempfile.TemporaryDirectory() as td:
            lorem = " ".join(["antimony", "stibnite", "hydrothermal", "mineralization", "fluid"] * 22)
            pages = [
                lorem,
                lorem,
                lorem,
                lorem,
                lorem + "\nTable 1. Major element data\nSample SiO2\nA 50.1",
                lorem,
                "Fig. 4. Geological map of the deposit.",
                "",  # trailing plate
            ]
            pdf_path = os.path.join(td, "modern_fig.pdf")
            out_dir = os.path.join(td, "out_fig")
            _make_text_pdf(
                pdf_path,
                pages,
                image_pages={
                    6: fitz.Rect(30, 80, 560, 700),
                    7: fitz.Rect(20, 20, 575, 820),
                },
            )
            online_df1 = pd.DataFrame([["A", "50.1"], ["B", "52.3"]], columns=["Sample", "SiO2"])
            online_df1.attrs.update({"label": "Table 1", "table_title": "Table 1. Major element data", "source": "online"})

            args = MagicMock()
            args.pdf = pdf_path
            args.output = out_dir
            args.title = "Modern Fig Paper"
            args.doi = "10.1016/j.oregeorev.2018.12.024"
            args.url = "https://www.sciencedirect.com/science/article/pii/S0169136818302634"
            args.table_idx = "all"
            args.headers = None
            args.online_only = False
            args.pdf_only = False
            args.no_online = False
            args.skip_supplementary = False
            args.structured_file = None
            args.zotero_sync = False
            args.db_path = None
            args.sequential = False
            args.single_file = False
            args.sync_zotero = False
            args.no_sync = True

            with patch.object(ezt.online, "extract_tables_online", return_value=([online_df1], "Modern Fig Paper")), \
                 patch.object(ezt, "extract_tables_from_pdf") as mock_pdf_pipe:
                ok = ezt.process_single_pdf(pdf_path, args)

            self.assertTrue(ok)
            mock_pdf_pipe.assert_not_called()
            saved_paths = []
            for root_d, _, fns in os.walk(out_dir):
                saved_paths.extend([os.path.join(root_d, f) for f in fns if f.endswith(".xlsx")])
            self.assertTrue(saved_paths, "Online table must be saved to an .xlsx workbook")
            sheets = pd.read_excel(saved_paths[0], sheet_name=None)
            self.assertTrue(
                "Table 1" in sheets or any(os.path.basename(p) == "Table 1.xlsx" for p in saved_paths),
                f"Expected Table 1 in saved outputs, got files={saved_paths}, sheets={list(sheets.keys())}",
            )

    # ------------------------------------------------------------------
    # P0-6: Pure figure/plate pages in text-based PDF not marked scanned
    # ------------------------------------------------------------------
    def test_p0_6_text_based_pdf_figure_pages_not_misclassified_as_scanned(self):
        with tempfile.TemporaryDirectory() as td:
            # Modern 10-page text-based paper: 6 rich text pages, 2 table pages, 2 figure/plate pages
            lorem = " ".join(["geochemistry", "antimony", "mineralization", "quartz", "stibnite"] * 22)
            pages = [
                lorem,
                lorem,
                lorem,
                lorem,
                lorem + "\nTable 1. Chemical composition of stibnite\nSample Sb S\nA 71.2 28.1",
                lorem + "\nTable 2. Fluid inclusion temperatures\nSample Th\nA 210",
                lorem,
                "Fig. 5. Photomicrographs of ore textures in the Zhaxikang deposit.",  # figure page (<80 words + image)
                "",  # trailing plate page after last rich text page (e.g. author manuscript appendix figure)
                "",  # trailing plate page
            ]
            pdf_path = os.path.join(td, "modern_paper.pdf")
            _make_text_pdf(
                pdf_path,
                pages,
                image_pages={
                    7: fitz.Rect(30, 80, 560, 700),
                    8: fitz.Rect(20, 20, 575, 820),
                    9: fitz.Rect(20, 20, 575, 820),
                },
            )

            df1 = pd.DataFrame([["A", "71.2"]], columns=["Sample", "Sb"])
            df1.attrs.update({"label": "Table 1", "page_idx": 4})
            df2 = pd.DataFrame([["A", "210"]], columns=["Sample", "Th"])
            df2.attrs.update({"label": "Table 2", "page_idx": 5})

            report = table_validator.cross_check_missed_tables([df1, df2], pdf_path)
            self.assertTrue(report["is_text_based_pdf"])
            self.assertEqual(report["scanned_pages"], [])
            self.assertEqual(report["uncovered_scanned_pages"], [])
            self.assertFalse(report["coverage_unknown"])
            self.assertTrue(report["is_complete"])

            # Contrast: genuine 1-page scanned PDF must STILL be flagged coverage_unknown=True
            scanned_pdf = os.path.join(td, "genuine_scan.pdf")
            _make_text_pdf(scanned_pdf, [""], image_pages={0: fitz.Rect(20, 20, 575, 820)})
            scan_report = table_validator.cross_check_missed_tables([], scanned_pdf)
            self.assertFalse(scan_report["is_complete"])
            self.assertTrue(scan_report["coverage_unknown"])
            self.assertEqual(scan_report["uncovered_scanned_pages"], [0])

    # ------------------------------------------------------------------
    # P0-7: Large PDF / Thesis subsetting for MinerU & deterministic remap
    # ------------------------------------------------------------------
    def test_p0_7_large_pdf_mineru_physically_uploads_subset_and_remaps(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "thesis_45p.pdf")
            pages = [f"Chapter text on page {i+1}" for i in range(45)]
            pages[0] = "Table 1. Intro summary"
            pages[5] = "Table 2. Main data"
            pages[20] = "Table 3. Isotope results"
            _make_text_pdf(pdf_path, pages)

            submitted_counts = []
            submitted_page_ranges = []

            def fake_submit(self_client, file_path, **kwargs):
                with fitz.open(file_path) as d:
                    submitted_counts.append(len(d))
                submitted_page_ranges.append(kwargs.get("page_ranges"))
                return "task-subset-1"

            def fake_parse(self_client, data):
                # Subset PDF has 3 pages [0, 5, 20]; table on subset page 2 must deterministically map to page 20
                df = pd.DataFrame([["S1", "1.23"]], columns=["Sample", "Val"])
                df.attrs["page_idx"] = 2
                return [df]

            with patch.object(ocr_client, "is_mineru_cli_available", return_value=False), \
                 patch.object(ocr_client, "resolve_mineru_token", return_value="fake-token"), \
                 patch.object(ocr_client.MinerUClient, "submit_task", autospec=True, side_effect=fake_submit), \
                 patch.object(ocr_client.MinerUClient, "poll_task", return_value={"success": True, "data": {}}), \
                 patch.object(ocr_client.MinerUClient, "parse_mineru_structured_result", autospec=True, side_effect=fake_parse):
                dfs = ocr_client.call_mineru_structured_api(pdf_path, config={"MINERU_USE_CLI": "false"}, page_ranges=[0, 5, 20])

            self.assertEqual(submitted_counts, [3], "45-page thesis must upload only the 3-page subset PDF")
            self.assertEqual(submitted_page_ranges, [None], "Subset PDF should be submitted with page_ranges=None")
            self.assertEqual(len(dfs), 1)
            self.assertEqual(dfs[0].attrs["page_idx"], 20, "Subset page_idx=2 must remap to original page_idx=20")

    # ------------------------------------------------------------------
    # P1-8: Elsevier HTTP 200 soft degradation & quota circuit breaker
    # ------------------------------------------------------------------
    def test_p1_8_elsevier_soft_degradation_and_quota_circuit_breaker(self):
        meta_xml = "<full-text-retrieval-response><coredata><dc:title>Test</dc:title></coredata></full-text-retrieval-response>"
        resp_meta = MagicMock(status_code=200, text=meta_xml, headers={"X-ELS-Status": "OK", "X-RateLimit-Remaining": "500"})
        deg_meta = strategies._detect_elsevier_soft_degradation(resp_meta)
        self.assertTrue(deg_meta["meta_only"])
        self.assertFalse(deg_meta["quota_exhausted"])

        resp_quota = MagicMock(status_code=200, text=meta_xml, headers={"X-ELS-Status": "QUOTA_EXCEEDED", "X-RateLimit-Remaining": "0"})
        deg_quota = strategies._detect_elsevier_soft_degradation(resp_quota)
        self.assertTrue(deg_quota["quota_exhausted"])

        with patch.object(strategies, "load_config", return_value={"ELSEVIER_API_KEY": "k123"}), \
             patch.object(strategies, "_elsevier_api_get", return_value=resp_quota), \
             patch.object(strategies, "extract_elsevier_supplementary_tables") as mock_supp, \
             patch("scripts.online.negative_cache.is_negatively_cached", return_value=None), \
             patch("scripts.online.negative_cache.record_negative_cache") as mock_rec:
            tables = strategies.extract_tables_via_elsevier_api("10.1016/j.test.2026.999")

        self.assertEqual(tables, [])
        mock_supp.assert_not_called()
        mock_rec.assert_called_once()

        # Verify subsequent call short-circuits when negative cache hit dict is returned
        with patch.object(strategies, "load_config", return_value={"ELSEVIER_API_KEY": "k123"}), \
             patch.object(strategies, "_elsevier_api_get") as mock_get2, \
             patch("scripts.online.negative_cache.is_negatively_cached", return_value={"target_key": "domain:api.elsevier.com", "reason": "http_429"}):
            tables2 = strategies.extract_tables_via_elsevier_api("10.1016/j.test.2026.999")
        self.assertEqual(tables2, [])
        mock_get2.assert_not_called()

    # ------------------------------------------------------------------
    # P1-9: Bilingual caption deduplication & Chinese numeral normalization
    # ------------------------------------------------------------------
    def test_p1_9_chinese_numeral_normalization_and_bilingual_dedup(self):
        self.assertEqual(common.format_table_label("表一"), "表1")
        self.assertEqual(common.format_table_label("表三"), "表3")
        self.assertEqual(common.format_table_label("表十二"), "表12")
        self.assertEqual(common.format_table_label("附表二"), "附表2")
        self.assertEqual(common.format_table_label("表二-三"), "表2-3")
        self.assertEqual(common.canonicalize_table_label_key("表3"), ("main", "3"))
        self.assertEqual(common.canonicalize_table_label_key("Table 3"), ("main", "3"))
        self.assertEqual(common.canonicalize_table_label_key("表三"), ("main", "3"))
        self.assertEqual(common.canonicalize_table_label_key("表3.13"), common.canonicalize_table_label_key("Table 3.13"))

        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "bilingual_cn.pdf")
            _make_text_pdf(
                pdf_path,
                [
                    "表3 中国锑矿床成矿系列划分\nTable 3 Division of metallogenic series of antimony deposits\nSample Sb\nA 10.5",
                    "表一 矿石化学成分\nSample Au\nB 2.3",
                ],
            )
            decls = table_validator.scan_pdf_table_declarations(pdf_path)
            self.assertEqual(len(decls[0]), 1, "Bilingual '表3' + 'Table 3' on same page must count as 1 table")
            self.assertEqual(decls[0][0]["label"], "表3")
            self.assertEqual(decls[1][0]["label"], "表1", "Chinese numeral '表一' must normalize to '表1'")

            # Extracting Table 3 (English label) and 表1 (Arabic label) covers both declarations cleanly
            df1 = pd.DataFrame([["A", "10.5"]], columns=["Sample", "Sb"])
            df1.attrs.update({"label": "Table 3", "page_idx": 0})
            df2 = pd.DataFrame([["B", "2.3"]], columns=["Sample", "Au"])
            df2.attrs.update({"label": "表1", "page_idx": 1})

            report = table_validator.cross_check_missed_tables([df1, df2], pdf_path)
            self.assertEqual(report["declared_count"], 2)
            self.assertEqual(report["missed_count"], 0)
            self.assertTrue(report["is_complete"])

    # ------------------------------------------------------------------
    # P1-10: Corrupt / 0-page PDF fail-fast circuit breaker
    # ------------------------------------------------------------------
    def test_p1_10_corrupt_and_zero_page_pdf_fail_fast(self):
        with tempfile.TemporaryDirectory() as td:
            empty_pdf = os.path.join(td, "empty.pdf")
            with open(empty_pdf, "wb") as f:
                pass  # 0-byte PDF

            with patch.object(ocr_client, "is_mineru_available", return_value=True) as mock_m_avail, \
                 patch.object(pte, "extract_via_ocr") as mock_ocr:
                res, logs = pte._do_extract_tables_from_pdf(empty_pdf, use_ocr_fallback=True)
            self.assertEqual(res, [])
            mock_m_avail.assert_not_called()
            mock_ocr.assert_not_called()
            self.assertTrue(any("Fail-Fast" in l for l in logs))

            corrupt_pdf = os.path.join(td, "corrupt.pdf")
            with open(corrupt_pdf, "wb") as f:
                f.write(b"NOT_A_VALID_PDF_HEADER_12345")

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "scanned", "confidence": 0.0, "pages_with_tables": [],
                    "tables": [], "structure_tables": [], "error": "Invalid PDF structure",
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=True) as mock_m2, \
                 patch.object(pte, "extract_via_ocr") as mock_ocr2:
                res2, logs2 = pte._do_extract_tables_from_pdf(corrupt_pdf, use_ocr_fallback=True)
            self.assertEqual(res2, [])
            mock_m2.assert_not_called()
            mock_ocr2.assert_not_called()
            self.assertTrue(any("Fail-Fast" in l for l in logs2))

    # ------------------------------------------------------------------
    # Review Round 2 Deep-Fix Regressions (Bugs 1-9)
    # ------------------------------------------------------------------
    def test_r2_extract_via_ocr_respects_cancel_and_forwards_pages_to_crop_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "ocr_cancel.pdf")
            _make_text_pdf(pdf_path, ["P1", "P2", "P3"])

            cancel_ev = threading.Event()

            def cancel_in_stage1(*args, **kwargs):
                cancel_ev.set()
                return ""

            import pdf_tables as top_pdf_tables
            with patch.object(ocr_client, "run_ppstructure_vlm_pipeline", side_effect=cancel_in_stage1), \
                 patch.object(ocr_client, "extract_via_docling") as mock_docling, \
                 patch.object(ocr_client, "get_ocr_registry") as mock_reg, \
                 patch.object(top_pdf_tables, "extract_table_crops_from_pdf") as mock_crops:
                out = pte.extract_via_ocr(pdf_path, pages=[2], cancel_event=cancel_ev, time_budget_s=30.0)

            self.assertEqual(out, [])
            mock_docling.assert_not_called()
            mock_reg.assert_not_called()
            mock_crops.assert_not_called()

            # When not cancelled and earlier stages return empty, crop fallback MUST receive pages=[2] (not full PDF)
            captured_crop_kwargs = {}

            def fake_crop_fb(path, **kwargs):
                captured_crop_kwargs.update(kwargs)
                return []

            empty_reg = MagicMock()
            empty_reg.get_backend.return_value = None
            with patch.object(ocr_client, "run_ppstructure_vlm_pipeline", return_value=""), \
                 patch.object(ocr_client, "extract_via_docling", return_value=[]), \
                 patch.object(ocr_client, "get_ocr_registry", return_value=empty_reg), \
                 patch.object(top_pdf_tables, "extract_table_crops_from_pdf", side_effect=fake_crop_fb):
                pte.extract_via_ocr(pdf_path, pages=[2], cancel_event=threading.Event(), time_budget_s=30.0)

            self.assertEqual(captured_crop_kwargs.get("pages"), [2], "Crop fallback must be scoped to requested pages")
            self.assertIsNotNone(captured_crop_kwargs.get("cancel_event"))
            self.assertIsNotNone(captured_crop_kwargs.get("time_budget_s"))

    def test_r2_run_paddleocr_vl_explicit_pages_never_uploads_full_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "ten_pages.pdf")
            _make_text_pdf(pdf_path, [f"Page {i+1} narrative text without tables." for i in range(10)])

            submitted_files = []

            def fake_vl_api(target_path, **kwargs):
                submitted_files.append(target_path)
                return "Plain text paragraph without any markdown or html table."

            with patch.object(ocr_client, "call_paddleocr_vl_online_api", side_effect=fake_vl_api), \
                 patch.object(ocr_client, "call_docling_table_extractor") as mock_docling:
                md = ocr_client.run_paddleocr_vl(pdf_path, pages=[0], config={"OCR_ENGINE": "paddleocr"})

            self.assertEqual(len(submitted_files), 1, f"Explicit pages=[0] must only submit the 1-page block, got {submitted_files}")
            self.assertNotEqual(submitted_files[0], pdf_path, "Must never fall through to upload the full 10-page PDF")
            self.assertIn("Plain text", md)
            mock_docling.assert_not_called()

    def test_r2_pp_structure_crops_disables_wasted_mineru_failover(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "queue_full.pdf")
            _make_text_pdf(pdf_path, ["Page 1", "Page 2", "Page 3"])

            resp_400 = MagicMock(status_code=400, text='{"code":400,"msg":"队列已满"}')
            with patch.object(ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(ocr_client, "call_mineru_api") as mock_mineru, \
                 patch.object(ocr_client, "_sleep_cancel_aware", return_value=True), \
                 patch.object(ocr_client.requests, "post", return_value=resp_400):
                crops = ocr_client.extract_pp_structure_table_crops(
                    pdf_path, config={"PADDLEOCR_ACCESS_TOKEN": "tok"}, pages=[1], timeout=30
                )

            self.assertEqual(crops, [])
            mock_mineru.assert_not_called()

    def test_r2_crop_pool_timeout_returns_promptly_without_blocking_on_hung_worker(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "hung_crop.pdf")
            _make_text_pdf(pdf_path, ["Page 1", "Page 2"])
            crops = [
                {"page_index": 0, "image": Image.new("RGB", (60, 60), "white"), "crop_bbox": [0, 0, 60, 60], "pdf_bbox": [0, 0, 60, 60]},
                {"page_index": 1, "image": Image.new("RGB", (60, 60), "white"), "crop_bbox": [0, 0, 60, 60], "pdf_bbox": [0, 0, 60, 60]},
            ]

            call_idx = {"n": 0}
            stop_hung = threading.Event()

            def one_fast_one_hung(path, **kwargs):
                call_idx["n"] += 1
                if call_idx["n"] == 1:
                    time.sleep(0.08)
                    return "| A | B |\n|---|---|\n| 1 | 2 |"
                stop_hung.wait(timeout=3.0)
                return ""

            import pdf_tables as top_pdf_tables
            import ocr_client as top_ocr_client
            try:
                with patch.object(pdf_tables, "extract_table_crops_from_pdf", return_value=crops), \
                     patch.object(top_pdf_tables, "extract_table_crops_from_pdf", return_value=crops), \
                     patch.object(pte.ocr_client, "call_paddleocr_vl_online_api", side_effect=one_fast_one_hung), \
                     patch.object(top_ocr_client, "call_paddleocr_vl_online_api", side_effect=one_fast_one_hung), \
                     patch.object(pte, "verify_and_rescue_table_with_agent", side_effect=lambda df, *a: df), \
                     patch.object(pte, "extract_via_paddleocr_fullpage", return_value=[]):
                    t0 = time.monotonic()
                    res = pte.extract_via_paddleocr_crops(
                        pdf_path, pages=[0, 1], allow_fullpage_fallback=False, time_budget_s=0.35
                    )
                    elapsed = time.monotonic() - t0
            finally:
                stop_hung.set()

            self.assertLess(elapsed, 1.5, f"Hung crop thread must not block pool shutdown for 4s, took {elapsed:.2f}s")
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["page_idx"], 0)

    def test_r2_mineru_cli_mode_also_uploads_physical_subset_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "thesis_cli_45p.pdf")
            pages = [f"Chapter text on page {i+1}" for i in range(45)]
            pages[0] = "Table 1. Intro"
            pages[5] = "Table 2. Main"
            pages[20] = "Table 3. Isotope"
            _make_text_pdf(pdf_path, pages)

            cli_submitted_counts = []
            cli_submitted_paths = []
            cli_submitted_ranges = []

            def fake_cli_extract(self_cli, file_path, **kwargs):
                cli_submitted_paths.append(file_path)
                cli_submitted_ranges.append(kwargs.get("page_ranges"))
                with fitz.open(file_path) as d:
                    cli_submitted_counts.append(len(d))
                df = pd.DataFrame([["S1", "9.9"]], columns=["Sample", "Val"])
                df.attrs["page_idx"] = 2
                return {"success": True, "dfs": [df], "markdown": ""}

            with patch.object(ocr_client, "is_mineru_cli_available", return_value=True), \
                 patch.object(ocr_client, "resolve_mineru_token", return_value="fake-token"), \
                 patch.object(ocr_client.MinerUCLIClient, "extract", autospec=True, side_effect=fake_cli_extract):
                dfs = ocr_client.call_mineru_structured_api(
                    pdf_path, config={"MINERU_USE_CLI": "cli"}, page_ranges=[0, 5, 20]
                )

            self.assertEqual(cli_submitted_counts, [3], "MinerU CLI mode must also upload only the 3-page subset PDF")
            self.assertEqual(cli_submitted_ranges, [None])
            self.assertNotEqual(cli_submitted_paths[0], pdf_path)
            self.assertFalse(os.path.exists(cli_submitted_paths[0]), "Temp CLI subset PDF must be cleaned up")
            self.assertEqual(len(dfs), 1)
            self.assertEqual(dfs[0].attrs["page_idx"], 20)

    def test_r2_interior_springer_figure_and_elsevier_pre_table_figures(self):
        with tempfile.TemporaryDirectory() as td:
            lorem = " ".join(["geochemistry", "antimony", "stibnite", "quartz", "ore"] * 22)
            # Case A: 12-page Springer PDF with interior full-page geological map on Page 6 (idx 5)
            # and its caption "Fig. 3." on Page 5 (idx 4)
            springer_pages = [lorem for _ in range(12)]
            springer_pages[3] = lorem + "\nTable 1. Isotope data\nSample d34S\nA 1.2"
            springer_pages[4] = lorem + "\nFig. 3. Geological map of the deposit shown on the next page."
            springer_pages[5] = ""  # full-page raster map with 0 words
            springer_pdf = os.path.join(td, "springer_interior_fig.pdf")
            _make_text_pdf(springer_pdf, springer_pages, image_pages={5: fitz.Rect(15, 15, 580, 825)})

            info_s = table_validator.classify_pdf_scanned_pages(springer_pdf)
            self.assertTrue(info_s["is_text_based_pdf"])
            self.assertEqual(info_s["scanned_pages"], [], "Interior full-page figure with adjacent Fig. caption must not be marked scanned")

            # Case B: 20-page Elsevier manuscript where Pages 1-12 are text (with Fig. captions),
            # Pages 13-17 are 5 figure plates, and Pages 18-20 are text-rich tables (after figures!)
            ms_pages = [lorem for _ in range(20)]
            ms_pages[10] = lorem + "\nFigure 1. Regional geology.\nFigure 2. Ore photomicrographs."
            for p_fig in range(12, 17):
                ms_pages[p_fig] = ""
            ms_pages[17] = lorem + "\nTable 1. Major elements\nSample SiO2\nS1 60.1"
            ms_pages[18] = lorem + "\nTable 2. Trace elements\nSample Sb\nS1 45.2"
            ms_pdf = os.path.join(td, "elsevier_pre_table_figs.pdf")
            _make_text_pdf(
                ms_pdf,
                ms_pages,
                image_pages={p: fitz.Rect(15, 15, 580, 825) for p in range(12, 17)},
            )
            info_m = table_validator.classify_pdf_scanned_pages(ms_pdf)
            self.assertTrue(info_m["is_text_based_pdf"])
            self.assertEqual(info_m["scanned_pages"], [], "Manuscript figure block preceding end-of-manuscript tables must not be marked scanned")

    def test_r2_elsevier_int_zero_ratelimit_cn_enum_comma_and_corrupt_preprocess(self):
        # Integer 0 in X-RateLimit-Remaining must still trigger quota_exhausted=True
        resp_int_zero = MagicMock(status_code=200, text="<coredata/>", headers={"X-RateLimit-Remaining": 0})
        deg = strategies._detect_elsevier_soft_degradation(resp_int_zero)
        self.assertTrue(deg["quota_exhausted"])
        self.assertEqual(deg["rate_remaining"], "0")

        # Chinese enumeration comma & 〇 numeral normalization
        self.assertEqual(common.format_table_label("表三、中国锑矿床统计"), "表3")
        self.assertEqual(common.format_table_label("表一〇 成矿特征"), "表10")

        # _preprocess_local_pdf Fail-Fast on 0-byte / corrupt PDF skips Doc-Ori
        with tempfile.TemporaryDirectory() as td:
            bad_pdf = os.path.join(td, "broken.pdf")
            with open(bad_pdf, "wb") as f:
                f.write(b"CORRUPT_DATA")
            with patch("scripts.doc_orientation_detector.remediate_pdf_pages_lossless") as mock_rem:
                out_path, cand, remediated = ezt._preprocess_local_pdf(bad_pdf, [])
            self.assertEqual(out_path, bad_pdf)
            self.assertIsNone(cand)
            self.assertFalse(remediated)
            mock_rem.assert_not_called()

    # ------------------------------------------------------------------
    # Review Round 3 Live Spot-Check Fixes (DJU8Q9RA / URLEUPS6 / 4B3NHFW7)
    # ------------------------------------------------------------------
    def test_r3_preprocess_does_not_leak_orientation_candidates_as_caller_candidates(self):
        """_preprocess_local_pdf must keep quick_detect_candidate_pages strictly for Tier-1 Doc-Ori and return cand_pages=None."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "cn_thesis_12p.pdf")
            cn_body = "雪峰弧形构造带位于扬子陆块东南缘，发育多期构造变形与金锑矿化作用。" * 12  # ~380 CJK chars, 0 spaces
            doc = fitz.open()
            for idx in range(12):
                page = doc.new_page(width=595, height=842)
                if idx == 5:
                    # Figure plate page with Chinese caption + raster map
                    page.insert_textbox(fitz.Rect(40, 40, 555, 120), "图3.1 雪峰弧形构造带区域地质简图", fontsize=10, fontname="china-s")
                    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 120), 1)
                    page.insert_image(fitz.Rect(40, 140, 540, 680), pixmap=pix)
                else:
                    txt = cn_body
                    if idx == 3:
                        txt = "表3.1 矿石主量元素化学成分\n" + cn_body
                    elif idx == 7:
                        txt = "表3.2 流体包裹体均一温度统计\n" + cn_body
                    page.insert_textbox(fitz.Rect(40, 40, 555, 760), txt, fontsize=10, fontname="china-s")
                # Add >=18 decorative header/footer vector marks on EVERY page (triggers quick_detect_candidate_pages drawings >= 15
                # without forming a real table grid, mimicking CNKI thesis borders/watermarks/vector glyphs)
                for i_mark in range(18):
                    page.draw_circle(fitz.Point(45 + i_mark * 25, 810), 1.5)
            doc.save(pdf_path)
            doc.close()

            # 1) CJK-aware classify_pdf_scanned_pages must detect Chinese thesis as text_based and NOT flag figure page 6 as scanned
            scan_info = table_validator.classify_pdf_scanned_pages(pdf_path)
            self.assertTrue(scan_info["is_text_based_pdf"], "Chinese text-based thesis must be recognized as text_based")
            self.assertEqual(scan_info["scanned_pages"], [], "Chinese figure page with 图3.1 caption must not pollute image_scan")

            # 2) _preprocess_local_pdf passes probe pages ONLY to remediate_pdf_pages_lossless and returns cand_pages=None
            ori_seen = {}

            def fake_remediate(path, candidate_pages=None):
                ori_seen["cands"] = candidate_pages
                return path, False, None

            with patch("scripts.doc_orientation_detector.remediate_pdf_pages_lossless", side_effect=fake_remediate), \
                 patch("doc_orientation_detector.remediate_pdf_pages_lossless", side_effect=fake_remediate), \
                 patch.object(pte, "quick_classify_pdf", return_value={"pdf_type": "text_based", "confidence": 1.0}):
                out_path, cand_pages, was_rem = ezt._preprocess_local_pdf(pdf_path, [])

            self.assertEqual(out_path, pdf_path)
            self.assertIsNone(cand_pages, "_preprocess_local_pdf must NOT leak orientation probe pages as caller_candidate")
            self.assertFalse(was_rem)
            self.assertTrue(ori_seen.get("cands"), "Doc-Ori should still receive quick_detect_candidate_pages internally")

            # 3) Running _do_extract_tables_from_pdf with candidate_pages=None scopes Step 2 strictly to real table pages
            ft_pages_seen = []
            plumber_pages_seen = []
            df_t1 = pd.DataFrame([["S1", "65.1"], ["S2", "66.2"]], columns=["样品", "SiO2"])
            df_t1.attrs.update({"label": "表3.1", "page_idx": 3, "extractor": "pdf_inspector"})
            df_t2 = pd.DataFrame([["S1", "210"], ["S2", "225"]], columns=["样品", "Th"])
            df_t2.attrs.update({"label": "表3.2", "page_idx": 7, "extractor": "pdf_inspector"})

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0,
                    "pages_with_tables": [4, 8],
                    "tables": [
                        {"df": df_t1, "page_idx": 3, "table_idx": 0, "bbox": [40, 40, 300, 160]},
                        {"df": df_t2, "page_idx": 7, "table_idx": 0, "bbox": [40, 40, 300, 160]},
                    ],
                    "structure_tables": [], "_structure_tree_ran": True,
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=False), \
                 patch.object(pte, "extract_via_find_tables", side_effect=lambda p, pages=None: (ft_pages_seen.append(pages), [])[1]), \
                 patch.object(pte, "extract_via_pdfplumber", side_effect=lambda p, pages=None: (plumber_pages_seen.append(pages), [])[1]), \
                 patch.object(pte, "extract_via_camelot", return_value=[]), \
                 patch.object(pte, "extract_via_img2table", return_value=[]), \
                 patch.object(pte, "extract_via_gmft", return_value=[]), \
                 patch.object(pte, "extract_via_pymupdf4llm", return_value=[]), \
                 patch.object(pte, "extract_via_rapid_table", return_value=[]), \
                 patch.object(pte, "extract_via_text_alignment", return_value=[]):
                results, logs = pte._do_extract_tables_from_pdf(pdf_path, use_ocr_fallback=False, candidate_pages=None)

            self.assertEqual(len(results), 2)
            self.assertTrue(ft_pages_seen and len(ft_pages_seen[0]) <= 3, f"Expected tightly scoped step3_pages, got {ft_pages_seen}")
            self.assertEqual(set(ft_pages_seen[0]), {3, 7})
            self.assertEqual(set(plumber_pages_seen[0]), {3, 7})
            self.assertFalse(any("caller_candidate" in l for l in logs), "No caller_candidate pollution should appear in logs")

    def test_r3_blanket_caller_candidate_guard_ignores_full_pdf_list_but_preserves_targeted(self):
        """Passing all pages of a multi-page native PDF as candidate_pages is ignored; a targeted subset is still honored."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "ten_pages_native.pdf")
            lorem = " ".join(["antimony", "mineralization", "quartz", "geochemistry", "hydrothermal"] * 22)
            pages = [lorem for _ in range(10)]
            pages[2] = "Table 1. Ore composition\nSample Sb\nA 12.3\n" + lorem
            _make_text_pdf(pdf_path, pages)

            df1 = pd.DataFrame([["A", "12.3"], ["B", "14.5"]], columns=["Sample", "Sb"])
            df1.attrs.update({"label": "Table 1", "page_idx": 2, "extractor": "pdf_inspector"})

            # Case A: Blanket candidate_pages=[0..9] must be ignored on a 10-page native PDF
            ft_pages_blanket = []
            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0,
                    "pages_with_tables": [3],
                    "tables": [{"df": df1, "page_idx": 2, "table_idx": 0, "bbox": [40, 40, 300, 140]}],
                    "structure_tables": [], "_structure_tree_ran": True,
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=False), \
                 patch.object(pte, "extract_via_find_tables", side_effect=lambda p, pages=None: (ft_pages_blanket.append(pages), [])[1]), \
                 patch.object(pte, "extract_via_pdfplumber", return_value=[]), \
                 patch.object(pte, "extract_via_camelot", return_value=[]), \
                 patch.object(pte, "extract_via_img2table", return_value=[]), \
                 patch.object(pte, "extract_via_gmft", return_value=[]), \
                 patch.object(pte, "extract_via_pymupdf4llm", return_value=[]), \
                 patch.object(pte, "extract_via_rapid_table", return_value=[]), \
                 patch.object(pte, "extract_via_text_alignment", return_value=[]):
                _, logs_blanket = pte._do_extract_tables_from_pdf(
                    pdf_path, use_ocr_fallback=False, candidate_pages=list(range(10))
                )

            self.assertTrue(any("忽略全页宽泛 caller_candidate" in l for l in logs_blanket))
            self.assertEqual(ft_pages_blanket, [[2]], f"Blanket caller_candidate must not pollute step3_pages: {ft_pages_blanket}")

            # Case B: Targeted candidate_pages=[5] (1/10 pages) MUST still be honored
            ft_pages_targeted = []
            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0,
                    "pages_with_tables": [3],
                    "tables": [{"df": df1, "page_idx": 2, "table_idx": 0, "bbox": [40, 40, 300, 140]}],
                    "structure_tables": [], "_structure_tree_ran": True,
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=False), \
                 patch.object(pte, "extract_via_find_tables", side_effect=lambda p, pages=None: (ft_pages_targeted.append(pages), [])[1]), \
                 patch.object(pte, "extract_via_pdfplumber", return_value=[]), \
                 patch.object(pte, "extract_via_camelot", return_value=[]), \
                 patch.object(pte, "extract_via_img2table", return_value=[]), \
                 patch.object(pte, "extract_via_gmft", return_value=[]), \
                 patch.object(pte, "extract_via_pymupdf4llm", return_value=[]), \
                 patch.object(pte, "extract_via_rapid_table", return_value=[]), \
                 patch.object(pte, "extract_via_text_alignment", return_value=[]):
                _, logs_targeted = pte._do_extract_tables_from_pdf(
                    pdf_path, use_ocr_fallback=False, candidate_pages=[5]
                )

            self.assertEqual(ft_pages_targeted, [[2, 5]], "Targeted caller_candidate=[5] must be preserved alongside page 2")
            self.assertTrue(any("caller_candidate" in l and "忽略全页宽泛" not in l for l in logs_targeted))

    def test_r3_budget_exhaustion_gracefully_returns_inspector_tables_without_zero_sec_engine_calls(self):
        """Reproduces 4B3NHFW7: when budget/cancel triggers during MinerU or Step 2 voting, stop invoking engines and return inspector_tables."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "thesis_budget.pdf")
            lorem = " ".join(["antimony", "deposit", "geology", "fluid", "isotope"] * 20)
            pages = [lorem for _ in range(6)]
            pages[1] = "Table 1. Major oxides\nSample SiO2\nA1 60.1\nA2 61.2\n" + lorem
            pages[4] = "Table 2. Trace elements\nSample Sb\nA1 12.4\nA2 15.8\n" + lorem
            _make_text_pdf(pdf_path, pages)

            def _make_insp_tables():
                d1 = pd.DataFrame([["A1", "60.1"], ["A2", "61.2"]], columns=["Sample", "SiO2"])
                d1.attrs.update({"extractor": "pdf_inspector", "page_idx": 1})
                d2 = pd.DataFrame([["A1", "12.4"], ["A2", "15.8"]], columns=["Sample", "Sb"])
                d2.attrs.update({"extractor": "pdf_inspector", "page_idx": 4})
                return [
                    {"df": d1, "page_idx": 1, "table_idx": 0, "bbox": [40, 40, 300, 140]},
                    {"df": d2, "page_idx": 4, "table_idx": 0, "bbox": [40, 40, 300, 140]},
                ]

            # Subcase A: cancel_event fires inside MinerU (before Step 2 voting enters) -> 0 engine calls, returns 2 inspector tables!
            cancel_a = threading.Event()

            def mineru_cancels(*args, **kwargs):
                cancel_a.set()
                return []

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0,
                    "pages_with_tables": [2, 5],
                    "tables": _make_insp_tables(),
                    "structure_tables": [], "_structure_tree_ran": True,
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(ocr_client, "call_mineru_structured_api", side_effect=mineru_cancels), \
                 patch.object(pte, "extract_via_find_tables") as m_ft, \
                 patch.object(pte, "extract_via_pdfplumber") as m_pl, \
                 patch.object(pte, "extract_via_camelot") as m_cam, \
                 patch.object(pte, "extract_via_img2table") as m_img, \
                 patch.object(pte, "extract_via_gmft") as m_gmft, \
                 patch.object(pte, "extract_via_pymupdf4llm") as m_llm, \
                 patch.object(pte, "extract_via_text_alignment") as m_ta, \
                 patch.object(pte, "extract_via_paddleocr_crops") as m_crops, \
                 patch.object(pte, "extract_via_ocr") as m_ocr:
                res_a, logs_a = pte._do_extract_tables_from_pdf(
                    pdf_path, use_ocr_fallback=True, time_budget_s=150.0, cancel_event=cancel_a
                )

            self.assertEqual(len(res_a), 2, f"Must gracefully return the 2 already-parsed inspector tables, got {len(res_a)}")
            self.assertEqual([r["df"].attrs.get("label") for r in res_a], ["Table 1", "Table 2"])
            for mock_eng in (m_ft, m_pl, m_cam, m_img, m_gmft, m_llm, m_ta, m_crops, m_ocr):
                mock_eng.assert_not_called()

            # Subcase B: cancel_event fires inside find_tables -> pdfplumber & all later engines NEVER called (never 0s timeout!), returns 2 inspector tables!
            cancel_b = threading.Event()

            def ft_cancels(*args, **kwargs):
                cancel_b.set()
                return []

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0,
                    "pages_with_tables": [2, 5],
                    "tables": _make_insp_tables(),
                    "structure_tables": [], "_structure_tree_ran": True,
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=False), \
                 patch.object(pte, "extract_via_find_tables", side_effect=ft_cancels), \
                 patch.object(pte, "extract_via_pdfplumber") as m_pl_b, \
                 patch.object(pte, "extract_via_camelot") as m_cam_b, \
                 patch.object(pte, "extract_via_img2table") as m_img_b, \
                 patch.object(pte, "extract_via_gmft") as m_gmft_b, \
                 patch.object(pte, "extract_via_pymupdf4llm") as m_llm_b, \
                 patch.object(pte, "extract_via_text_alignment") as m_ta_b, \
                 patch.object(table_validator, "validate_native_extraction_pipeline") as m_val_b, \
                 patch.object(pte, "extract_via_paddleocr_crops") as m_crops_b, \
                 patch.object(pte, "extract_via_ocr") as m_ocr_b:
                res_b, logs_b = pte._do_extract_tables_from_pdf(
                    pdf_path, use_ocr_fallback=True, time_budget_s=150.0, cancel_event=cancel_b
                )

            self.assertEqual(len(res_b), 2, f"Must preserve 2 inspector tables when cancelled mid-Step 2, got {len(res_b)}")
            for mock_eng in (m_pl_b, m_cam_b, m_img_b, m_gmft_b, m_llm_b, m_ta_b, m_val_b, m_crops_b, m_ocr_b):
                mock_eng.assert_not_called()
            self.assertFalse(any("单引擎执行超时 (0s)" in l for l in logs_b), "Must never invoke an engine with 0s timeout")

            # Subcase C: _run_engine_with_timeout aborts promptly when cancel_event fires mid-run
            cancel_c = threading.Event()
            stop_slow = threading.Event()
            logs_c = []

            def slow_eng():
                stop_slow.wait(timeout=3.0)
                return [{"df": pd.DataFrame([[1, 2]], columns=["A", "B"]), "page_idx": 0}]

            timer_c = threading.Timer(0.12, cancel_c.set)
            timer_c.daemon = True
            timer_c.start()
            try:
                t0 = time.monotonic()
                out_c = pte._run_engine_with_timeout(slow_eng, timeout_s=10.0, engine_name="pdfplumber", logs=logs_c, cancel_event=cancel_c)
                elapsed_c = time.monotonic() - t0
            finally:
                timer_c.cancel()
                stop_slow.set()

            self.assertEqual(out_c, [])
            self.assertLess(elapsed_c, 1.0, f"Cancel-aware _run_engine_with_timeout must exit promptly, took {elapsed_c:.2f}s")
            self.assertTrue(any("取消信号" in l for l in logs_c))

    def test_r3_outer_pdf_budget_guard_recovers_partial_results_sink_instead_of_zero_tables(self):
        """Even if a C-extension blocks past the outer PDF Budget Guard, partial_results_sink rescues already-parsed tables."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "thesis_hard_timeout.pdf")
            out_dir = os.path.join(td, "out_rescued")
            _make_text_pdf(
                pdf_path,
                [
                    "表1 矿床地质特征\n矿床 品位\nA 2.1",
                    "表2 同位素组成\n样品 d34S\nS1 1.5",
                ],
            )
            df1 = pd.DataFrame([["A", "2.1"], ["B", "3.4"]], columns=["矿床", "品位"])
            df1.attrs.update({"label": "表1", "table_title": "表1 矿床地质特征", "extractor": "pdf_inspector", "page_idx": 0})
            df2 = pd.DataFrame([["S1", "1.5"], ["S2", "2.0"]], columns=["样品", "d34S"])
            df2.attrs.update({"label": "表2", "table_title": "表2 同位素组成", "extractor": "pdf_inspector", "page_idx": 1})

            stop_hung = threading.Event()

            def hung_after_checkpoint(*args, **kwargs):
                sink = kwargs.get("partial_results_sink")
                if sink is not None:
                    sink[:] = [
                        {"df": df1, "page_idx": 0, "table_idx": 0},
                        {"df": df2, "page_idx": 1, "table_idx": 0},
                    ]
                stop_hung.wait(timeout=3.0)
                return [], []

            args = MagicMock()
            args.pdf = pdf_path
            args.output = out_dir
            args.title = "南秦岭锑矿床研究"
            args.doi = None
            args.url = None
            args.table_idx = "all"
            args.headers = None
            args.online_only = False
            args.pdf_only = True
            args.no_online = True
            args.skip_supplementary = False
            args.structured_file = None
            args.zotero_sync = False
            args.db_path = None
            args.sequential = False
            args.single_file = False
            args.sync_zotero = False
            args.no_sync = True

            try:
                with patch.dict(os.environ, {"PDF_EXTRACTION_BUDGET_S": "0.25"}), \
                     patch.object(ezt, "_preprocess_local_pdf", return_value=(pdf_path, None, False)), \
                     patch.object(ezt, "extract_tables_from_pdf", side_effect=hung_after_checkpoint):
                    t0 = time.monotonic()
                    ok = ezt.process_single_pdf(pdf_path, args)
                    elapsed = time.monotonic() - t0
            finally:
                stop_hung.set()

            self.assertTrue(ok, "Must rescue and save partial_results_sink tables instead of failing with 0 tables")
            self.assertLess(elapsed, 2.0, f"Outer budget guard must return promptly, took {elapsed:.2f}s")
            saved_xlsx = []
            for root_d, _, fns in os.walk(out_dir):
                saved_xlsx.extend([os.path.join(root_d, f) for f in fns if f.endswith(".xlsx")])
            self.assertEqual(len(saved_xlsx), 2, f"Expected 2 rescued .xlsx files, got {saved_xlsx}")

    def test_r3_checkpoint_snapshot_isolation_no_duplicate_labels_and_monotonic_merge(self):
        """Checkpointing must not mutate live inspector_tables, must avoid duplicate page labels, must not clobber multi-page checkpoints with partial subsets, and must checkpoint ft_results/plumber_results."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "multi_table_checkpoint.pdf")
            lorem = " ".join(["antimony", "quartz", "ore", "deposit", "fluid"] * 20)
            pages = [
                "Table 1. First table\nSample SiO2\nS1 60.1\nTable 2. Second table\nSample Sb\nS1 12.3\n" + lorem,
                "Table 3. Third table\nSample Au\nS1 4.5\n" + lorem,
                "Table 4. Fourth table\nSample Ag\nS1 9.8\n" + lorem,
            ]
            _make_text_pdf(pdf_path, pages)

            # Live inspector tables: Page 0 has 1 already-labeled ("Table 1") + 1 unlabeled; Page 1 & 2 unlabeled
            df_p0_a = pd.DataFrame([["S1", "60.1"], ["S2", "61.0"]], columns=["Sample", "SiO2"])
            df_p0_a.attrs.update({"label": "Table 1", "page_idx": 0, "extractor": "pdf_inspector"})
            df_p0_b = pd.DataFrame([["S1", "12.3"], ["S2", "14.2"]], columns=["Sample", "Sb"])
            df_p0_b.attrs.update({"page_idx": 0, "extractor": "pdf_inspector"})
            df_p1 = pd.DataFrame([["S1", "4.5"], ["S2", "5.1"]], columns=["Sample", "Au"])
            df_p1.attrs.update({"page_idx": 1, "extractor": "pdf_inspector"})
            df_p2 = pd.DataFrame([["S1", "9.8"], ["S2", "10.2"]], columns=["Sample", "Ag"])
            df_p2.attrs.update({"page_idx": 2, "extractor": "pdf_inspector"})

            # Partial valid_struct_tables covers ONLY Page 0 (1 table) -> must NOT clobber Pages 1 & 2 in sink!
            df_struct_p0 = pd.DataFrame([["S1", "60.1"], ["S2", "61.0"]], columns=["Sample", "SiO2"])
            df_struct_p0.attrs.update({"label": "Table 1", "page_idx": 0, "extractor": "pdf_inspector_structure"})

            insp_list = [
                {"df": df_p0_a, "page_idx": 0, "table_idx": 0, "bbox": [40, 40, 300, 120]},
                {"df": df_p0_b, "page_idx": 0, "table_idx": 1, "bbox": [40, 200, 300, 280]},
                {"df": df_p1, "page_idx": 1, "table_idx": 0, "bbox": [40, 40, 300, 120]},
                {"df": df_p2, "page_idx": 2, "table_idx": 0, "bbox": [40, 40, 300, 120]},
            ]
            sink = []
            cancel_ev = threading.Event()
            seen_live_labels_in_mineru = {}

            def fake_mineru(*args, **kwargs):
                # At this moment Step 1 + valid_struct_tables checkpoints have already run:
                # 1) Live inspector DataFrames must NOT have been mutated in-place
                seen_live_labels_in_mineru["p0_b_live"] = df_p0_b.attrs.get("label")
                seen_live_labels_in_mineru["p1_live"] = df_p1.attrs.get("label")
                # 2) Snapshot sink must retain ALL 3 pages (0, 1, 2) even though valid_struct_tables only had Page 0!
                seen_live_labels_in_mineru["sink_pages"] = sorted({r.get("page_idx") for r in sink})
                seen_live_labels_in_mineru["sink_p0_labels"] = [
                    r["df"].attrs.get("label") for r in sink if r.get("page_idx") == 0
                ]
                cancel_ev.set()
                return []

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0,
                    "pages_with_tables": [1, 2, 3],
                    "tables": insp_list,
                    "structure_tables": [{"df": df_struct_p0, "page_idx": 0, "table_idx": 0, "bbox": [40, 40, 300, 120]}],
                    "_structure_tree_ran": True,
                 }), \
                 patch.object(table_validator, "verify_table_numeric_integrity", return_value=(True, {"match_ratio": 1.0})), \
                 patch.object(table_validator, "cross_check_missed_tables", return_value={"is_complete": False, "missed_tables": [{"label": "Table 3", "page_idx": 1}], "coverage_unknown": False}), \
                 patch.object(ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(ocr_client, "call_mineru_structured_api", side_effect=fake_mineru):
                res, _ = pte._do_extract_tables_from_pdf(
                    pdf_path, use_ocr_fallback=True, time_budget_s=150.0,
                    cancel_event=cancel_ev, partial_results_sink=sink,
                )

            self.assertIsNone(seen_live_labels_in_mineru["p0_b_live"], "Checkpointing must NOT mutate live inspector df.attrs['label']")
            self.assertIsNone(seen_live_labels_in_mineru["p1_live"], "Checkpointing must NOT mutate live inspector df.attrs['label']")
            self.assertEqual(seen_live_labels_in_mineru["sink_pages"], [0, 1, 2], "Partial struct checkpoint must NOT wipe out pages 1 & 2")
            self.assertEqual(len(res), 4, "Graceful harvest must preserve all 4 tables across 3 pages")
            p0_final_labels = [r["df"].attrs.get("label") for r in res if r.get("page_idx") == 0]
            self.assertEqual(p0_final_labels, ["Table 1", "Table 2"], f"Second unlabeled table on Page 0 must get Table 2, not duplicate Table 1: {p0_final_labels}")

            # Subcase B: Empty inspector_tables -> ft_results & plumber_results MUST be checkpointed into sink before later engines run
            sink_b = []
            cancel_b = threading.Event()
            df_ft = pd.DataFrame([["S1", "60.1"], ["S2", "61.0"]], columns=["Sample", "SiO2"])
            df_ft.attrs.update({"page_idx": 0, "extractor": "find_tables"})
            df_pl = pd.DataFrame([["S1", "4.5"], ["S2", "5.1"]], columns=["Sample", "Au"])
            df_pl.attrs.update({"page_idx": 1, "extractor": "pdfplumber"})
            sink_seen_in_camelot = []

            def camelot_checks_sink(*args, **kwargs):
                sink_seen_in_camelot.extend(list(sink_b))
                cancel_b.set()
                return []

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0,
                    "pages_with_tables": [1, 2],
                    "tables": [], "structure_tables": [], "_structure_tree_ran": True,
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=False), \
                 patch.object(pte, "extract_via_find_tables", return_value=[{"df": df_ft, "page_idx": 0, "table_idx": 0, "bbox": [40, 40, 300, 120]}]), \
                 patch.object(pte, "extract_via_pdfplumber", return_value=[{"df": df_pl, "page_idx": 1, "table_idx": 0, "bbox": [40, 40, 300, 120]}]), \
                 patch.object(pte, "extract_via_camelot", side_effect=camelot_checks_sink):
                pte._do_extract_tables_from_pdf(
                    pdf_path, use_ocr_fallback=False, time_budget_s=150.0,
                    cancel_event=cancel_b, partial_results_sink=sink_b,
                )

            self.assertEqual(sorted({r.get("page_idx") for r in sink_seen_in_camelot}), [0, 1], "Both ft_results (p0) and plumber_results (p1) must be checkpointed into partial_results_sink")

    def test_r3_large_thesis_mineru_subset_avoids_next_page_bloat_and_cooperative_engine_stop(self):
        """Large thesis (>35p) MinerU subset must not unconditionally append non-table p+1 prose pages, m_timeout must respect tight budgets, and timed-out engines must receive cooperative stop signal."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "thesis_45p.pdf")
            cn_body = "南秦岭镇安旬阳盆地金汞锑矿床发育典型层控热液成矿特征与多阶段流体演化。" * 10
            doc = fitz.open()
            for idx in range(45):
                pg = doc.new_page(width=595, height=842)
                txt = cn_body
                if idx in (5, 12, 20, 28, 36):
                    txt = f"表{idx}.1 矿床地球化学数据\n" + cn_body
                elif idx == 40:
                    # Caption page WITHOUT an inspector table on page 40 -> page 41 allowed as spill-over buffer
                    txt = "表40.1 跨页起始声明\n" + cn_body
                pg.insert_textbox(fitz.Rect(40, 40, 555, 760), txt, fontsize=10, fontname="china-s")
            doc.save(pdf_path)
            doc.close()

            insp_tables = []
            for p_i in (5, 12, 20, 28, 36):
                df_i = pd.DataFrame([["S1", "10.1"], ["S2", "12.2"]], columns=["样品", "Sb"])
                df_i.attrs.update({"page_idx": p_i, "extractor": "pdf_inspector"})
                insp_tables.append({"df": df_i, "page_idx": p_i, "table_idx": 0, "bbox": [40, 40, 300, 140]})

            captured_mineru = {}

            def fake_mineru_call(path, config=None, page_ranges=None, timeout=None, cancel_event=None):
                captured_mineru["pages"] = list(page_ranges or [])
                captured_mineru["timeout"] = timeout
                return []

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0,
                    "pages_with_tables": [6, 13, 21, 29, 37],
                    "tables": insp_tables,
                    "structure_tables": [], "_structure_tree_ran": True,
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(ocr_client, "call_mineru_structured_api", side_effect=fake_mineru_call), \
                 patch.object(pte, "extract_via_find_tables", return_value=[]), \
                 patch.object(pte, "extract_via_pdfplumber", return_value=[]), \
                 patch.object(pte, "extract_via_camelot", return_value=[]), \
                 patch.object(pte, "extract_via_img2table", return_value=[]), \
                 patch.object(pte, "extract_via_gmft") as m_gmft_tight, \
                 patch.object(pte, "extract_via_pymupdf4llm", return_value=[]), \
                 patch.object(pte, "extract_via_rapid_table") as m_rt_tight, \
                 patch.object(pte, "extract_via_text_alignment", return_value=[]):
                pte._do_extract_tables_from_pdf(pdf_path, use_ocr_fallback=False, time_budget_s=2.5)

            m_gmft_tight.assert_not_called()
            m_rt_tight.assert_not_called()

            m_pages = captured_mineru.get("pages", [])
            self.assertTrue({5, 12, 20, 28, 36, 40}.issubset(set(m_pages)), f"All focused table pages must be included: {m_pages}")
            self.assertIn(41, m_pages, "Spill-over p+1 buffer should be kept for caption-only page 40")
            for prose_next in (6, 13, 21, 29, 37):
                self.assertNotIn(prose_next, m_pages, f"Non-table prose page {prose_next+1} after already-parsed table must not bloat MinerU subset")
            self.assertLessEqual(captured_mineru.get("timeout", 999), 2.5, "MinerU timeout must never exceed remaining time_budget_s")

            # Cooperative engine stop: worker thread sees _is_current_engine_stopped() immediately on timeout and exits cleanly
            stopped_flag = {"saw_stop": False, "exited": threading.Event()}

            def coop_engine():
                t_start = time.monotonic()
                while time.monotonic() - t_start < 3.0:
                    if pte._is_current_engine_stopped():
                        stopped_flag["saw_stop"] = True
                        stopped_flag["exited"].set()
                        return []
                    time.sleep(0.02)
                return []

            out = pte._run_engine_with_timeout(coop_engine, timeout_s=0.12, engine_name="pdfplumber")
            self.assertEqual(out, [])
            self.assertTrue(stopped_flag["exited"].wait(timeout=1.0), "Timed-out engine worker must exit promptly via cooperative stop signal")
            self.assertTrue(stopped_flag["saw_stop"])

    # ------------------------------------------------------------------
    # Run 2 Optimization Round (Directions 1-5)
    # ------------------------------------------------------------------
    def test_opt_dir1_medium_pdf_subset_chunked_mineru_and_no_failover_loop(self):
        """Dir 1: >8p PDFs physically slice subsets, >24p subsets chunk into <=20p batches, and allow_mineru_failover=False blocks duplicate MinerU loops."""
        with tempfile.TemporaryDirectory() as td:
            # 1) Medium 16-page PDF with 3 target pages physically slices to 3 pages
            med_pdf = os.path.join(td, "med_16p.pdf")
            _make_text_pdf(med_pdf, [f"Page {i+1} body text" for i in range(16)])
            eff_p, tmp_p, sub_p, empty_sel = ocr_client._build_pdf_subset(
                med_pdf, [2, 7, 12], prefix="t_med_", min_total_pages=8, min_file_bytes=10 * 1024 * 1024, max_page_ratio=0.75
            )
            try:
                self.assertFalse(empty_sel)
                self.assertIsNotNone(tmp_p)
                self.assertEqual(sub_p, [2, 7, 12])
                with fitz.open(eff_p) as d:
                    self.assertEqual(len(d), 3)
            finally:
                if tmp_p and os.path.exists(tmp_p):
                    os.remove(tmp_p)

            # 2) >24 target pages auto-chunk into <=20p batches and aggregate partial wins
            big_pdf = os.path.join(td, "thesis_60p.pdf")
            _make_text_pdf(big_pdf, [f"Thesis page {i+1}" for i in range(60)])
            submitted_chunks = []

            def fake_chunk_mineru(path, config=None, timeout=600, cancel_event=None, page_ranges=None, pages=None, is_ocr=None, _chunked=False):
                if not _chunked:
                    return orig_call(path, config=config, timeout=timeout, cancel_event=cancel_event, page_ranges=page_ranges, pages=pages, is_ocr=is_ocr, _chunked=False)
                submitted_chunks.append(list(page_ranges))
                if len(submitted_chunks) == 2:
                    return []  # simulate chunk 2 timeout
                df = pd.DataFrame([["S1", "1.2"]], columns=["Sample", "Sb"])
                df.attrs["page_idx"] = page_ranges[0]
                return [df]

            orig_call = ocr_client.call_mineru_structured_api
            with patch.object(ocr_client, "call_mineru_structured_api", side_effect=fake_chunk_mineru):
                dfs = orig_call(big_pdf, config={"MINERU_CHUNK_PAGES": 20}, timeout=90, page_ranges=list(range(0, 45)))
            self.assertEqual([len(c) for c in submitted_chunks], [20, 20, 5])
            self.assertEqual(len(dfs), 2, "Chunk 1 & 3 wins must be preserved even when chunk 2 times out")

            # 3) allow_mineru_failover=False prevents PaddleOCR-VL from re-calling MinerU on AIStudio failure
            with patch.object(ocr_client, "call_paddleocr_job", return_value={"success": False, "combined_markdown": "", "failover": False}), \
                 patch.object(ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(ocr_client, "call_mineru_api") as m_mineru_fo:
                md = ocr_client.call_paddleocr_vl_online_api(med_pdf, config={"OCR_ENGINE": "auto"}, timeout=30, allow_mineru_failover=False)
            self.assertEqual(md, "")
            m_mineru_fo.assert_not_called()

    def test_opt_dir2_collapsed_1col_bib_fig_cover_sheet_and_roman_canonical(self):
        """Dir 2 & 5: reject 1-col collapsed tables, bib lists, fig captions, and Excel Title cover sheets while preserving valid 1-col lists & Roman numerals."""
        # Valid 1-col parameter list MUST survive
        df_ok_1col = pd.DataFrame([["Param1"], ["Param2"], ["Param3"]], columns=["Variables"])
        self.assertFalse(common.is_table_low_quality(df_ok_1col))
        self.assertFalse(common.is_metadata_table(df_ok_1col))

        # Collapsed multi-column table into 1 col MUST be flagged low quality
        df_collapsed = pd.DataFrame(
            [
                ["Sample-1   61.24   14.32   2.15   0.85"],
                ["Sample-2   62.10   13.88   2.40   0.91"],
                ["Sample-3   60.95   14.12   2.05   0.77"],
                ["Sample-4   63.01   13.55   2.22   0.82"],
            ],
            columns=["Table 1 Major element geochemistry of ore samples"],
        )
        self.assertTrue(common.is_table_low_quality(df_collapsed))

        # Figure caption pseudo-table MUST be flagged low quality
        df_fig = pd.DataFrame(
            [["Aggregate of", "arsenopyrite", "and stibnite"], ["Photomicrograph", "under reflected", "light"]],
            columns=["Fig. 3 A", "Mineral", "Texture"],
        )
        self.assertTrue(common.is_table_low_quality(df_fig))

        # Bibliography list pseudo-table MUST be flagged metadata
        df_bib = pd.DataFrame(
            [
                ["[1]包民伟,康晓涛,单志杰,等.豫西花岗伟晶岩型稀有金属矿成矿作用研究[J].矿床地质,2020"],
                ["[2]陈衍景,倪培,范宏瑞,等.不同类型热液金矿系统的流体包裹体特征[J].岩石学报,2007"],
                ["[3]胡瑞忠,毕献武,彭建堂,等.华南地区中生代金属成矿规律[J].矿床地质,2010"],
            ],
            columns=["参考文献"],
        )
        self.assertTrue(common.is_metadata_table(df_bib))

        # Multi-sheet supplementary Excel skips 1-col Title sheet and keeps real data sheet
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            pd.DataFrame([["Title of paper"], ["Authors: A, B"], ["Supplementary material"]]).to_excel(writer, sheet_name="Title", index=False)
            pd.DataFrame([["S1", 12.3, 4.5], ["S2", 14.1, 3.8]], columns=["Sample", "Sb", "Au"]).to_excel(writer, sheet_name="Table S1", index=False)
        parsed_supp = strategies._parse_supplementary_bytes(buf.getvalue(), "126_2025_MOESM1_ESM.xlsx")
        self.assertEqual(len(parsed_supp), 1, f"Cover Title sheet must be skipped, got {[d.attrs.get('label') for d in parsed_supp]}")
        self.assertEqual(parsed_supp[0].shape, (2, 3))

        # Roman numeral canonicalization
        self.assertEqual(common.canonicalize_table_label_key("Table II"), ("main", "2"))
        self.assertEqual(common.canonicalize_table_label_key("Table IV"), ("main", "4"))
        self.assertEqual(common.canonicalize_table_label_key("Table II"), common.canonicalize_table_label_key("Table 2"))

    def test_opt_dir3_large_thesis_step2_focuses_on_missing_pages_and_preserves_all_inspector(self):
        """Dir 3: >50p thesis with >=10 good inspector pages scopes Step 2 local engines only to missing pages while keeping all inspector tables."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "thesis_65p.pdf")
            lorem = " ".join(["antimony", "deposit", "geology", "fluid", "isotope"] * 18)
            pages = [lorem for _ in range(65)]
            insp_pages_0 = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
            missing_pages_0 = [12, 38, 60]
            for idx_i, p0 in enumerate(insp_pages_0, 1):
                pages[p0] = f"Table {idx_i}. Inspected table\nSample Sb\nA 1.1\n" + lorem
            for idx_m, p0 in enumerate(missing_pages_0, 20):
                pages[p0] = f"Table {idx_m}. Missed by inspector\nSample Au\nB 2.2\n" + lorem
            _make_text_pdf(pdf_path, pages)

            insp_tables = []
            for idx_i, p0 in enumerate(insp_pages_0, 1):
                df_i = pd.DataFrame([["A1", "1.1"], ["A2", "2.3"]], columns=["Sample", "Sb"])
                df_i.attrs.update({"label": f"Table {idx_i}", "page_idx": p0, "extractor": "pdf_inspector"})
                insp_tables.append({"df": df_i, "page_idx": p0, "table_idx": 0, "bbox": [40, 40, 300, 140]})

            ft_seen_pages = []

            def fake_ft(p, pages=None):
                ft_seen_pages.append(list(pages or []))
                out = []
                for p0 in (pages or []):
                    df_f = pd.DataFrame([["B1", "2.2"], ["B2", "3.4"]], columns=["Sample", "Au"])
                    df_f.attrs.update({"page_idx": p0, "extractor": "find_tables"})
                    out.append({"df": df_f, "page_idx": p0, "table_idx": 0, "bbox": [40, 40, 300, 140]})
                return out

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0,
                    "pages_with_tables": [p + 1 for p in insp_pages_0],
                    "tables": insp_tables,
                    "structure_tables": [], "_structure_tree_ran": True,
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=False), \
                 patch.object(pte, "extract_via_find_tables", side_effect=fake_ft), \
                 patch.object(pte, "extract_via_pdfplumber", return_value=[]), \
                 patch.object(pte, "extract_via_camelot", return_value=[]), \
                 patch.object(pte, "extract_via_img2table", return_value=[]), \
                 patch.object(pte, "extract_via_gmft", return_value=[]), \
                 patch.object(pte, "extract_via_pymupdf4llm", return_value=[]), \
                 patch.object(pte, "extract_via_rapid_table", return_value=[]), \
                 patch.object(pte, "extract_via_text_alignment", return_value=[]):
                res, logs = pte._do_extract_tables_from_pdf(pdf_path, use_ocr_fallback=False, time_budget_s=150.0)

            self.assertTrue(ft_seen_pages, "find_tables should run on missing pages")
            self.assertEqual(set(ft_seen_pages[0]), set(missing_pages_0), f"Expected Step 2 to focus strictly on missing pages {missing_pages_0}, got {ft_seen_pages[0]}")
            self.assertTrue(any("Large Thesis Focus" in l for l in logs))
            self.assertGreaterEqual(len(res), len(insp_pages_0) + len(missing_pages_0), "Must keep all 11 inspector tables AND all 3 rescued missing tables")

    def test_opt_dir4_scanned_short_pdf_caps_mineru_and_falls_back_to_rapidtable(self):
        """Dir 4: short scanned PDF (<=15p) caps MinerU at <=55s, passes allow_mineru_failover=False, and rescues via RapidTable."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "scanned_10p.pdf")
            _make_text_pdf(pdf_path, ["" for _ in range(10)], image_pages={p: fitz.Rect(20, 20, 570, 820) for p in range(10)})

            seen = {}

            def fake_mineru_scan(path, config=None, page_ranges=None, timeout=None, cancel_event=None):
                seen["mineru_timeout"] = timeout
                return []

            def fake_crops_scan(path, pages=None, **kwargs):
                seen["crops_allow_fo"] = kwargs.get("allow_mineru_failover")
                return []

            df_rt = pd.DataFrame([["S1", "10.5"], ["S2", "12.1"]], columns=["Sample", "Sb"])
            df_rt.attrs.update({"page_idx": 4, "extractor": "rapid_table"})

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "scanned", "confidence": 0.95,
                    "pages_with_tables": [], "tables": [], "structure_tables": [], "_structure_tree_ran": True,
                 }), \
                 patch.object(ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(ocr_client, "call_mineru_structured_api", side_effect=fake_mineru_scan), \
                 patch.object(pte, "extract_via_paddleocr_crops", side_effect=fake_crops_scan), \
                 patch.object(pte, "is_rapid_table_available", return_value=True), \
                 patch.object(pte, "extract_via_rapid_table", return_value=[{"df": df_rt, "page_idx": 4, "table_idx": 0, "bbox": None}]):
                res, logs = pte._do_extract_tables_from_pdf(
                    pdf_path, use_ocr_fallback=True, candidate_pages=[4, 5], time_budget_s=150.0
                )

            self.assertLessEqual(seen.get("mineru_timeout", 999), 55, f"Short scanned PDF must cap MinerU at <=55s, got {seen}")
            self.assertIs(seen.get("crops_allow_fo"), False, "Must disable duplicate MinerU failover after MinerU already ran")
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["df"].attrs.get("extractor"), "rapid_table")

    def test_opt_dir5_external_only_supp_label_skips_redundant_local_pdf_fallback(self):
        """Dir 5: when online got all main tables and missing label is strictly an external Table S1 absent from local PDF grids, skip 120s local PDF fallback."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "mdpi_ext_supp.pdf")
            out_dir = os.path.join(td, "out_mdpi")
            lorem = " ".join(["antimony", "mineralization", "quartz", "stibnite", "ore"] * 22)
            _make_text_pdf(
                pdf_path,
                [
                    lorem + "\nTable 1. Major elements\nSample Sb\nA 1.2",
                    lorem + "\nTable 2. Trace elements\nSample Au\nA 3.4",
                    lorem + "\nTable S1. Supplementary analytical data available online at mdpi.com/s1",
                    lorem,
                ],
            )
            df1 = pd.DataFrame([["A", "1.2"], ["B", "2.3"]], columns=["Sample", "Sb"])
            df1.attrs.update({"label": "Table 1", "table_title": "Table 1. Major elements", "extractor": "direct_http"})
            df2 = pd.DataFrame([["A", "3.4"], ["B", "4.5"]], columns=["Sample", "Au"])
            df2.attrs.update({"label": "Table 2", "table_title": "Table 2. Trace elements", "extractor": "direct_http"})

            args = MagicMock()
            args.pdf = pdf_path
            args.output = out_dir
            args.title = "MDPI Antimony Paper"
            args.doi = "10.3390/min9020087"
            args.url = "https://www.mdpi.com/2075-163X/9/2/87"
            args.table_idx = "all"
            args.headers = None
            args.online_only = False
            args.pdf_only = False
            args.no_online = False
            args.skip_supplementary = False
            args.structured_file = None
            args.zotero_sync = False
            args.db_path = None
            args.sequential = False
            args.single_file = False
            args.sync_zotero = False
            args.no_sync = True

            with patch.object(ezt.online, "extract_tables_online", return_value=([df1, df2], "MDPI Antimony Paper")), \
                 patch.object(ezt, "extract_tables_from_pdf") as m_local_pdf:
                ok = ezt.process_single_pdf(pdf_path, args)

            self.assertTrue(ok)
            m_local_pdf.assert_not_called()


if __name__ == "__main__":
    unittest.main()
