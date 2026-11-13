import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import pymupdf as fitz
from PIL import Image

sys.path[:0] = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
]

import agent_bridge
import common
import doc_orientation_detector as ori
import doclayout_yolo_detector as dl
import excel_export as ex
import extract_zotero_table as ezt
import ocr_client
import omnidocbench
import pdf_table_extractor as pte
import rapid_table_extractor as rte
import table_postprocess as tp
import table_validator as tv
from llm_reasoner import LLMTableReasoner
from online import graph as online_graph


class TestAuditRemediationRegressions(unittest.TestCase):
    def test_clean_latex_preserve_scripts_and_excel_rich_text(self):
        """Python 3.14-safe script preservation in clean_latex_and_ocr and Excel CellRichText export."""
        self.assertEqual(tp.clean_latex_and_ocr("$CaCO_3$", preserve_scripts=True), "CaCO_3")
        self.assertEqual(tp.clean_latex_and_ocr("10^{-3}", preserve_scripts=True), "10^{-3}")
        self.assertEqual(common.clean_latex_and_ocr("$H_{2}O$", preserve_scripts=True), "H_{2}O")
        self.assertEqual(common.clean_latex_and_ocr("$CaCO_3$", preserve_scripts=False), "CaCO3")

        from openpyxl import load_workbook
        from openpyxl.cell.rich_text import CellRichText

        df = pd.DataFrame({"Species": ["$CaCO_3$", "$10^{-3}$"], "Value": [12.5, 0.001]})
        df.attrs["label"] = "Table 1"
        with tempfile.TemporaryDirectory() as td:
            out_xlsx = os.path.join(td, "scripts.xlsx")
            ex.save_tables_to_excel([df], out_xlsx, single_file=True)
            wb = load_workbook(out_xlsx, rich_text=True)
            ws = wb["Table 1"]
            v_sub = ws["A2"].value
            v_sup = ws["A3"].value
            self.assertIsInstance(v_sub, CellRichText)
            self.assertIsInstance(v_sup, CellRichText)
            self.assertTrue(any(getattr(run.font, "vertAlign", None) == "subscript" for run in v_sub))
            self.assertTrue(any(getattr(run.font, "vertAlign", None) == "superscript" for run in v_sup))
            wb.close()

    def test_copy_sqlite_snapshot_wal_and_locked_db_non_blocking(self):
        """copy_sqlite_snapshot copies WAL pages and never blocks on an active SQLite write lock."""
        with tempfile.TemporaryDirectory() as td:
            src_db = os.path.join(td, "live_zotero.sqlite")
            dst_db = os.path.join(td, "snapshot.sqlite")

            conn1 = sqlite3.connect(src_db)
            conn1.execute("PRAGMA journal_mode=WAL")
            conn1.execute("PRAGMA wal_autocheckpoint=0")
            conn1.execute("CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT)")
            conn1.execute("INSERT INTO items VALUES (1, 'KEY_WAL_1')")
            conn1.commit()

            # Simulate live Zotero holding an exclusive write lock on a new uncommitted txn
            conn2 = sqlite3.connect(src_db, timeout=0.1)
            conn2.execute("BEGIN IMMEDIATE")
            conn2.execute("INSERT INTO items VALUES (2, 'UNCOMMITTED')")

            t0 = time.monotonic()
            out = common.copy_sqlite_snapshot(src_db, dst_db)
            elapsed = time.monotonic() - t0

            self.assertEqual(out, dst_db)
            self.assertLess(elapsed, 2.0)

            ro_conn = sqlite3.connect(f"file:{dst_db}?mode=ro", uri=True)
            rows = ro_conn.execute("SELECT key FROM items ORDER BY itemID").fetchall()
            ro_conn.close()
            self.assertEqual(rows, [("KEY_WAL_1",)])

            conn2.rollback()
            conn2.close()
            conn1.close()
            common.cleanup_sqlite_snapshot(dst_db)

    def test_race_candidates_grace_window_waits_for_authoritative_channel(self):
        """_race_candidates uses monotonic grace clock to wait for slower authoritative channel."""
        def fast_html():
            time.sleep(0.01)
            return [pd.DataFrame({"Col": ["fast_partial"]})]

        def slow_arxiv():
            time.sleep(0.18)
            return [pd.DataFrame({"Col": ["r1", "r2", "r3"]})]

        dfs, winner = online_graph._race_candidates(
            [("DirectHTTP", fast_html), ("ArxivHTML", slow_arxiv)],
            authoritative_names=("ArxivHTML",),
            grace_timeout=1.5,
        )
        self.assertEqual(winner, "ArxivHTML")
        self.assertEqual(len(dfs), 1)
        self.assertEqual(len(dfs[0]), 3)

    def test_cli_doi_url_title_headers_propagation(self):
        """extract_zotero_table.main forwards --doi, --url, --title, and --headers into process_single_pdf."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "sample.pdf")
            doc = fitz.open()
            doc.new_page()
            doc.save(pdf_path)
            doc.close()

            out_dir = os.path.join(td, "out")
            argv = [
                "extract_zotero_table.py",
                "--pdf", pdf_path,
                "--output", out_dir,
                "--doi", "10.1016/j.test.2025.001",
                "--url", "https://example.org/paper",
                "--title", "Explicit Title",
                "--headers", "Sample,SiO2",
            ]
            with patch.object(sys, "argv", argv), \
                 patch.object(common, "load_config", return_value={}), \
                 patch.object(ezt, "process_single_pdf", return_value=True) as mock_proc:
                rc = ezt.main()
                self.assertEqual(rc, 0)
                mock_proc.assert_called_once()
                passed_args = mock_proc.call_args.args[1]
                self.assertEqual(passed_args.headers, ["Sample", "SiO2"])
                kwargs = mock_proc.call_args.kwargs
                plan = kwargs.get("plan") or {}
                self.assertEqual(plan.get("doi"), "10.1016/j.test.2025.001")
                self.assertEqual(plan.get("url"), "https://example.org/paper")
                self.assertEqual(plan.get("title"), "Explicit Title")

    def test_extract_via_structure_tree_deep_headers_and_continuation_th(self):
        """extract_via_structure_tree preserves >2 header levels and strips continuation TH from data rows."""
        se = lambda mcid, role, page=1: types.SimpleNamespace(page=page, role=role, mcid=mcid)
        item = lambda mcid, text, x, y, width=40.0: types.SimpleNamespace(mcid=mcid, text=text, x=x, y=y, width=width)

        # Panel 0 (top_y ~700 -> 640): 3 TH rows exercising is_hierarchy (len(leaf_row)=4 >= 4):
        #   Row 0: Major Elements (spanning cols 1..3)
        #   Row 1: Sample, Oxides (spanning cols 1..2), LOI
        #   Row 2: ID, SiO2, TiO2, wt%
        # Panel 1 (column break y jump > 45pt back up to 700, shifted x): repeated TH row + 1 TD row
        se_elems = [
            se(1, "Caption"),
            # Level 1 header (spanning)
            se(10, "TH"), se(11, "TH"),
            # Level 2 header
            se(20, "TH"), se(21, "TH"), se(22, "TH"),
            # Level 3 header (4 leaf columns)
            se(30, "TH"), se(31, "TH"), se(32, "TH"), se(33, "TH"),
            # Data row 1 in Panel 0
            se(40, "TD"), se(41, "TD"), se(42, "TD"), se(43, "TD"),
            # Panel 1 continuation repeated TH row + Data row 2
            se(50, "TH"), se(51, "TH"), se(52, "TH"), se(53, "TH"),
            se(60, "TD"), se(61, "TD"), se(62, "TD"), se(63, "TD"),
        ]
        items = [
            item(1, "Table 1. Multi-level structure", 50, 740, 200),
            # Panel 0
            item(10, "Sample", 40, 700), item(11, "Major", 100, 700),
            item(20, "Site", 40, 680), item(21, "Oxides", 100, 680), item(22, "Volatile", 220, 680),
            item(30, "ID", 40, 660), item(31, "SiO2", 100, 660), item(32, "TiO2", 160, 660), item(33, "LOI", 220, 660),
            item(40, "S-01", 40, 640), item(41, "71.2", 100, 640), item(42, "0.4", 160, 640), item(43, "1.1", 220, 640),
            # Panel 1 (y jumps back to 700 after 640 -> +60pt > 45pt, shifted x >= 320)
            item(50, "ID", 320, 700), item(51, "SiO2", 380, 700), item(52, "TiO2", 440, 700), item(53, "LOI", 500, 700),
            item(60, "S-02", 320, 680), item(61, "69.8", 380, 680), item(62, "0.5", 440, 680), item(63, "1.3", 500, 680),
        ]

        mock_pi = types.SimpleNamespace(
            extract_structure_elements=lambda path, pages=None: se_elems,
            extract_text_with_positions=lambda path, pages=None: items,
        )
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "tagged.pdf")
            doc = fitz.open()
            doc.new_page()
            doc.save(pdf_path)
            doc.close()

            with patch.dict(sys.modules, {"pdf_inspector": mock_pi}):
                res = pte.extract_via_structure_tree(pdf_path)

            self.assertEqual(len(res), 1)
            df = res[0]["df"]
            cols = [str(c) for c in df.columns]
            self.assertTrue(any("Major" in c and "Oxides" in c and "SiO2" in c for c in cols), cols)
            values_flat = [str(v) for row in df.values.tolist() for v in row]
            self.assertIn("S-01", values_flat)
            self.assertIn("S-02", values_flat)
            self.assertEqual(len(df), 2)
            self.assertNotIn("LOI", values_flat)

    def test_doclayout_ensemble_retains_intermediate_iou_candidates(self):
        """DocLayoutEnsembleDetector preserves [0.20, 0.40) IoU boxes instead of dropping them."""
        # Overlap = 100x50 = 5000; area_y = 100x100 = 10000; area_p = 100x100 = 10000;
        # IoU = 5000 / 15000 = 0.333 in [0.20, 0.40), contain = 5000 / 10000 = 0.50 < 0.70
        yolo_det = MagicMock()
        yolo_det.extract_extended_table_regions.return_value = [
            {"table_bbox": [0, 0, 100, 100], "crop_bbox": [0, 0, 100, 100], "score": 0.85, "has_caption": False, "has_footnote": False}
        ]
        pp_det = MagicMock()
        pp_det.is_available.return_value = True
        pp_det.extract_extended_table_regions.return_value = [
            {"table_bbox": [0, 50, 100, 150], "crop_bbox": [0, 50, 100, 150], "score": 0.90, "has_caption": True, "has_footnote": False}
        ]

        ens = dl.DocLayoutEnsembleDetector(yolo_detector=yolo_det, pp_detector=pp_det)
        fused = ens.extract_extended_table_regions(Image.new("RGB", (300, 300), "white"))
        self.assertEqual(len(fused), 2)

    def test_remediate_pdf_pages_lossless_handles_pre_rotated_page(self):
        """remediate_pdf_pages_lossless composes correction on pages where page.rotation != 0."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "rotated.pdf")
            doc = fitz.open()
            p = doc.new_page()
            p.set_rotation(90)
            p.insert_text((72, 72), "Header Sample Data")
            doc.save(pdf_path)
            doc.close()

            with patch.object(
                ori,
                "detect_page_orientation",
                return_value={"needs_rotation": True, "correction_angle": 90, "confidence": 0.95},
            ):
                eff_path, was_mod, tmp_clean = ori.remediate_pdf_pages_lossless(pdf_path, candidate_pages=[0])

            try:
                self.assertTrue(was_mod)
                self.assertIsNotNone(tmp_clean)
                with fitz.open(eff_path) as out_doc:
                    self.assertEqual(out_doc[0].rotation, 180)
            finally:
                if tmp_clean and os.path.exists(tmp_clean):
                    os.remove(tmp_clean)

    def test_is_supplementary_distinguishes_samples_and_elements(self):
        """is_supplementary avoids false positives on 'Samples S1-S5' and 'Si_ppm'."""
        self.assertFalse(common.is_supplementary("Table 1. Samples S1-S5"))
        self.assertFalse(common.is_supplementary("Si_ppm"))
        self.assertTrue(common.is_supplementary("Table S1"))
        self.assertTrue(common.is_supplementary("SI_Table_1"))
        self.assertTrue(common.is_supplementary("Appendix A"))

    def test_omnidocbench_compute_iou_supports_dict_boxes(self):
        """compute_iou handles dict bbox representations as well as coordinate lists."""
        self.assertAlmostEqual(
            omnidocbench.compute_iou({"bbox": [0, 0, 10, 10]}, [0, 0, 10, 10]),
            1.0,
            places=5,
        )
        self.assertAlmostEqual(
            omnidocbench.compute_iou({"x0": 0, "y0": 0, "x1": 10, "y1": 10}, {"left": 5, "top": 0, "right": 15, "bottom": 10}),
            1.0 / 3.0,
            places=5,
        )
        self.assertAlmostEqual(
            omnidocbench.compute_iou({"x": 0, "y": 0, "w": 10, "h": 10}, [0, 0, 10, 10]),
            1.0,
            places=5,
        )

    def test_llm_reasoner_empty_api_key_guard_and_agent_bridge_dir(self):
        """LLMTableReasoner skips remote HTTP without key; AgentReasoningBridge uses custom bridge_dir."""
        with tempfile.TemporaryDirectory() as td:
            custom_bridge = os.path.join(td, "custom_bridge")
            bridge = agent_bridge.AgentReasoningBridge(bridge_dir=custom_bridge)
            self.assertTrue(os.path.isdir(os.path.join(custom_bridge, "pending")))
            self.assertTrue(os.path.isdir(os.path.join(custom_bridge, "resolved")))
            self.assertTrue(os.path.isdir(os.path.join(custom_bridge, "crops")))
            self.assertEqual(bridge.bridge_dir, custom_bridge)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "LLM_API_KEY": ""}, clear=False):
            reasoner = LLMTableReasoner(
                config={"LLM_ENABLED": True, "LLM_API_BASE": "https://api.example.com/v1", "LLM_API_KEY": ""}
            )
            with patch("llm_reasoner.requests.post") as mock_post:
                out = reasoner._call_chat_completion([{"role": "user", "content": "hi"}])
                self.assertIsNone(out)
                mock_post.assert_not_called()

    def test_table_validator_post_ref_citation_vs_standalone_heading(self):
        """Lines like 'Tables of constants...' inside References do not exit references section, while standalone 'Tables' does."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "refs_test.pdf")
            doc = fitz.open()
            p0 = doc.new_page()
            p0.insert_text((72, 72), "Introduction text.")
            p1 = doc.new_page()
            p1.insert_text(
                (72, 72),
                "References\nTables of constants (2020) doi:10.1/x\nTables of isotopes, 2021\nTable 99. Fake Ref Entry",
            )
            p2 = doc.new_page()
            p2.insert_text((72, 72), "Tables\nTable 1. Real Appendix Table After Refs")
            doc.save(pdf_path)
            doc.close()

            decls = tv.scan_pdf_table_declarations(pdf_path)
            all_labels = [d["label"] for p_list in decls.values() for d in p_list]
            self.assertNotIn("Table 99", all_labels)
            self.assertIn("Table 1", all_labels)

    def test_call_mineru_api_compat_and_explicit_overrides(self):
        """call_mineru_api preserves minimal submit_task args by default and forwards explicit overrides."""
        with patch.object(ocr_client, "is_mineru_cli_available", return_value=False), \
             patch.object(ocr_client.MinerUClient, "submit_task", return_value="tid-1") as mock_sub, \
             patch.object(ocr_client.MinerUClient, "poll_task", return_value={"success": True, "data": {}}), \
             patch.object(ocr_client.MinerUClient, "parse_mineru_table_result", return_value="| A |\n|---|\n| 1 |"):
            md = ocr_client.call_mineru_api(
                "dummy.pdf",
                config={"MINERU_API_KEY": "tok", "MINERU_USE_CLI": "api"},
                page_ranges=[1],
            )
            self.assertIn("| A |", md)
            mock_sub.assert_called_once_with("dummy.pdf", page_ranges=[1])

        with patch.object(ocr_client, "is_mineru_cli_available", return_value=False), \
             patch.object(ocr_client.MinerUClient, "submit_task", return_value="tid-2") as mock_sub2, \
             patch.object(ocr_client.MinerUClient, "poll_task", return_value={"success": True, "data": {}}), \
             patch.object(ocr_client.MinerUClient, "parse_mineru_table_result", return_value="| B |\n|---|\n| 2 |"):
            ocr_client.call_mineru_api(
                "dummy.pdf",
                config={"MINERU_API_KEY": "tok", "MINERU_USE_CLI": "api", "MINERU_MODEL": "pipeline"},
                page_ranges=[2],
                is_ocr=False,
            )
            mock_sub2.assert_called_once_with("dummy.pdf", page_ranges=[2], is_ocr=False, model_version="pipeline")

    def test_rapid_table_mock_without_modeltype_does_not_import_or_unload_torch(self):
        """Mock rapid_table lacking ModelType/RapidTableInput does not trigger lazy torch import inside patch.dict."""
        class MockRapid:
            def __call__(self, img):
                return types.SimpleNamespace(pred_html="<table><tr><th>H</th></tr><tr><td>1</td></tr></table>")

        torch_before = "torch" in sys.modules
        with patch.dict(sys.modules, {"rapid_table": types.SimpleNamespace(RapidTable=MockRapid)}):
            ext = rte.RapidTableExtractor()
            ext._init_engine()
            self.assertIsNotNone(ext._wired_engine)
            self.assertIsNone(ext._wireless_engine)
        self.assertEqual("torch" in sys.modules, torch_before)

    def test_docling_hf_offline_guard_when_cached_and_restores_state(self):
        """_docling_hf_offline_if_cached sets HF_HUB_OFFLINE when snapshots exist locally and restores it."""
        import huggingface_hub.constants as hc

        with tempfile.TemporaryDirectory() as tmpdir:
            for repo in (
                "models--docling-project--docling-layout-heron",
                "models--docling-project--docling-models",
            ):
                snap = os.path.join(tmpdir, repo, "snapshots", "rev1")
                os.makedirs(snap, exist_ok=True)
                with open(os.path.join(snap, "config.json"), "w", encoding="utf-8") as f:
                    f.write("{}")

            prev_attr = hc.HF_HUB_OFFLINE
            prev_env = os.environ.pop("HF_HUB_OFFLINE", None)
            try:
                hc.HF_HUB_OFFLINE = False
                with patch.object(hc, "HF_HUB_CACHE", tmpdir):
                    with ocr_client._docling_hf_offline_if_cached():
                        self.assertTrue(hc.HF_HUB_OFFLINE)
                        self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
                self.assertFalse(hc.HF_HUB_OFFLINE)
                self.assertIsNone(os.environ.get("HF_HUB_OFFLINE"))

                # Explicit HF_HUB_OFFLINE=0 must not be overridden
                os.environ["HF_HUB_OFFLINE"] = "0"
                with patch.object(hc, "HF_HUB_CACHE", tmpdir):
                    with ocr_client._docling_hf_offline_if_cached():
                        self.assertFalse(hc.HF_HUB_OFFLINE)
                        self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "0")
            finally:
                hc.HF_HUB_OFFLINE = prev_attr
                if prev_env is None:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                else:
                    os.environ["HF_HUB_OFFLINE"] = prev_env

    def test_structure_tree_multipanel_auto_index_and_shifted_th_no_crash(self):
        """Multi-panel Tagged PDF with repeated TH omitting stub Index and shifted columns builds valid DF."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "tagged.pdf")
            doc = fitz.open()
            doc.new_page(width=600, height=800)
            doc.save(pdf_path)
            doc.close()

            # Panel 0 (left): TH SiO2(150), CaO(220); TD rows with omitted-TH stub S1/S2 at x=80
            # Panel 1 (right, y jumps up 720 > 660+45): TH SiO2(420), CaO(490); TD row with stub S3 at x=350
            se_all = [
                types.SimpleNamespace(page=1, mcid=1, role="TH"),
                types.SimpleNamespace(page=1, mcid=2, role="TH"),
                types.SimpleNamespace(page=1, mcid=3, role="TD"),
                types.SimpleNamespace(page=1, mcid=4, role="TD"),
                types.SimpleNamespace(page=1, mcid=5, role="TD"),
                types.SimpleNamespace(page=1, mcid=6, role="TD"),
                types.SimpleNamespace(page=1, mcid=7, role="TD"),
                types.SimpleNamespace(page=1, mcid=8, role="TD"),
                types.SimpleNamespace(page=1, mcid=9, role="TH"),
                types.SimpleNamespace(page=1, mcid=10, role="TH"),
                types.SimpleNamespace(page=1, mcid=11, role="TD"),
                types.SimpleNamespace(page=1, mcid=12, role="TD"),
                types.SimpleNamespace(page=1, mcid=13, role="TD"),
            ]
            items = [
                types.SimpleNamespace(mcid=1, text="SiO2", x=150.0, y=700.0, width=30.0),
                types.SimpleNamespace(mcid=2, text="CaO", x=220.0, y=700.0, width=30.0),
                types.SimpleNamespace(mcid=3, text="S1", x=80.0, y=680.0, width=20.0),
                types.SimpleNamespace(mcid=4, text="65.1", x=150.0, y=680.0, width=25.0),
                types.SimpleNamespace(mcid=5, text="1.2", x=220.0, y=680.0, width=20.0),
                types.SimpleNamespace(mcid=6, text="S2", x=80.0, y=660.0, width=20.0),
                types.SimpleNamespace(mcid=7, text="66.2", x=150.0, y=660.0, width=25.0),
                types.SimpleNamespace(mcid=8, text="0.9", x=220.0, y=660.0, width=20.0),
                types.SimpleNamespace(mcid=9, text="SiO2", x=420.0, y=720.0, width=30.0),
                types.SimpleNamespace(mcid=10, text="CaO", x=490.0, y=720.0, width=30.0),
                types.SimpleNamespace(mcid=11, text="S3", x=350.0, y=700.0, width=20.0),
                types.SimpleNamespace(mcid=12, text="69.0", x=420.0, y=700.0, width=25.0),
                types.SimpleNamespace(mcid=13, text="0.8", x=490.0, y=700.0, width=20.0),
            ]
            mock_insp = types.SimpleNamespace(
                extract_structure_elements=lambda *a, **kw: se_all,
                extract_text_with_positions=lambda *a, **kw: items,
            )
            with patch.dict(sys.modules, {"pdf_inspector": mock_insp}):
                res = pte.extract_via_structure_tree(pdf_path)
            self.assertEqual(len(res), 1)
            df = res[0]["df"]
            self.assertEqual(list(df.columns), ["Index", "SiO2", "CaO"])
            self.assertEqual(len(df), 3)
            self.assertEqual(list(df["Index"]), ["S1", "S2", "S3"])

    def test_step5_no_early_exit_when_vector_find_tables_page_uncovered(self):
        """Step 5 does not early-exit when an untitled vector_find_tables candidate page yields 0 tables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "two_pages_one_untitled.pdf")
            doc = fitz.open()
            p0 = doc.new_page(width=600, height=800)
            p0.insert_text((50, 50), "Table 1. Declared summary")
            for y in (80, 100, 120, 140):
                p0.draw_line((50, y), (350, y))
            for x in (50, 150, 250, 350):
                p0.draw_line((x, 80), (x, 140))
            for r_i, row in enumerate([["Sample", "SiO2", "CaO"], ["A", "60.1", "1.1"], ["B", "61.2", "1.3"]]):
                for c_i, val in enumerate(row):
                    p0.insert_text((55 + c_i * 100, 95 + r_i * 20), val)

            p1 = doc.new_page(width=600, height=800)
            p1.insert_text((50, 45), "Appendix data without table label")
            for y in (80, 100, 120, 140):
                p1.draw_line((50, y), (350, y))
            for x in (50, 150, 250, 350):
                p1.draw_line((x, 80), (x, 140))
            for r_i, row in enumerate([["ID", "MgO", "FeO"], ["X1", "4.2", "5.1"], ["X2", "4.8", "5.5"]]):
                for c_i, val in enumerate(row):
                    p1.insert_text((55 + c_i * 100, 95 + r_i * 20), val)
            doc.save(pdf_path)
            doc.close()

            df0 = pd.DataFrame([["A", "60.1", "1.1"], ["B", "61.2", "1.3"]], columns=["Sample", "SiO2", "CaO"])
            df0.attrs["label"] = "Table 1"
            df0.attrs["extractor"] = "pymupdf"

            df1_ocr = pd.DataFrame([["X1", "4.2", "5.1"], ["X2", "4.8", "5.5"]], columns=["ID", "MgO", "FeO"])
            df1_ocr.attrs["extractor"] = "ocr"

            with patch.object(pte, "classify_and_extract_via_inspector", return_value={
                    "pdf_type": "text_based", "confidence": 1.0, "pages_with_tables": [1], "tables": [], "structure_tables": []
                 }), \
                 patch.object(pte, "extract_via_structure_tree", return_value=[]), \
                 patch.object(pte, "extract_via_find_tables", return_value=[{"df": df0, "page_idx": 0, "table_idx": 0, "bbox": [50, 80, 350, 140]}]), \
                 patch.object(pte, "extract_via_pdfplumber", return_value=[]), \
                 patch.object(pte, "extract_via_camelot", return_value=[]), \
                 patch.object(pte, "extract_via_img2table", return_value=[]), \
                 patch.object(pte, "extract_via_gmft", return_value=[]), \
                 patch.object(pte, "extract_via_text_alignment", return_value=[]), \
                 patch.object(pte, "extract_via_rapid_table", return_value=[]), \
                 patch.object(pte, "extract_via_paddleocr_crops", return_value=[]), \
                 patch.object(ocr_client, "is_mineru_available", return_value=False), \
                 patch.object(ocr_client, "extract_via_docling", return_value=[]), \
                 patch.object(pte, "extract_via_ocr", return_value=[{"df": df1_ocr, "page_idx": 1, "table_idx": 0, "bbox": None}]) as mock_ocr:
                results, _ = pte._do_extract_tables_from_pdf(pdf_path, use_ocr_fallback=True)

            mock_ocr.assert_called_once_with(pdf_path, pages=[1])
            self.assertEqual(len(results), 2)

    def test_run_paddleocr_vl_and_ppstructure_subset_page_remapping(self):
        """Subset block and failover outputs remap <!-- Page K --> markers back to original PDF page indices."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "five_pages.pdf")
            doc = fitz.open()
            for i in range(5):
                pg = doc.new_page(width=400, height=500)
                pg.insert_text((40, 40), f"Page {i + 1}")
            doc.save(pdf_path)
            doc.close()

            with patch.object(
                ocr_client,
                "call_paddleocr_vl_online_api",
                return_value="<!-- Page 1 -->\n<table><tr><th>A</th></tr><tr><td>1</td></tr></table>\n<!-- Page 2 -->\n<table><tr><th>B</th></tr><tr><td>2</td></tr></table>",
            ):
                md_vl = ocr_client.run_paddleocr_vl(pdf_path, pages=[2, 3], config={"OCR_ENGINE": "paddleocr"})
            self.assertIn("<!-- Page 3 -->", md_vl)
            self.assertIn("<!-- Page 4 -->", md_vl)
            self.assertNotIn("<!-- Page 1 -->", md_vl)
            self.assertNotIn("<!-- Page 2 -->", md_vl)

            with patch.object(
                ocr_client,
                "call_pp_structure_v3_api",
                return_value={"success": True, "failover": True, "combined_markdown": "<!-- Page 1 -->\n| X |\n|---|\n| 9 |", "pages": []},
            ):
                md_pp = ocr_client.run_ppstructure_vlm_pipeline(pdf_path, pages=[4], config={"OCR_ENGINE": "paddleocr"})
            self.assertIn("<!-- Page 5 -->", md_pp)
            self.assertNotIn("<!-- Page 1 -->", md_pp)

    def test_url_isolation_and_single_file_crop_sync_preserves_na(self):
        """HTTP URLs skip local PDF/OCR fallback and single_file crop sync reloads all sheets keeping literal NA."""
        from online import __dict__ as _online_check  # ensure online pkg loaded
        import online

        args_url = types.SimpleNamespace(
            output=None, doi=None, url=None, title=None, headers=None,
            pdf_only=False, online_only=False, online_timeout=5,
            skip_supplementary=False, single_file=False, remediate_pdf="off",
            zotero_note=False, sync_zotero=False, zotero_api_key=None,
            zotero_user_id=None, db_path=None, table_idx="all",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            args_url.output = os.path.join(tmpdir, "url_out.xlsx")
            df_u = pd.DataFrame([["S1", "65.1", "NA"]], columns=["Sample", "SiO2", "Note"])
            df_u.attrs["label"] = "Table 1"
            with patch.object(online, "extract_tables_online", return_value=[df_u]), \
                 patch.object(ezt, "extract_tables_from_pdf", side_effect=AssertionError("URL success must not call PDF")):
                self.assertTrue(ezt.process_single_pdf("https://example.com/paper", args_url))

            class _HtmlResp:
                status_code = 200
                headers = {"Content-Type": "text/html"}
                content = b"<html><body>No PDF</body></html>"
                def iter_content(self, chunk_size=65536):
                    return iter([self.content])
                def close(self):
                    pass

            with patch.object(online, "extract_tables_online", return_value=[]), \
                 patch("requests.head", return_value=_HtmlResp()), \
                 patch("requests.get", return_value=_HtmlResp()), \
                 patch.object(ezt, "extract_tables_from_pdf", side_effect=AssertionError("Non-PDF URL failure must not call PDF")):
                self.assertFalse(ezt.process_single_pdf("https://example.com/paper", args_url))

            # Single-file crop export + Zotero sync reload
            pdf_path = os.path.join(tmpdir, "paper.pdf")
            doc = fitz.open()
            doc.new_page(width=500, height=600).insert_text((40, 40), "Table 1. A\nTable 2. B")
            doc.save(pdf_path)
            doc.close()
            out_single = os.path.join(tmpdir, "combined.xlsx")

            df_online = pd.DataFrame([["S1", "NA"]], columns=["Sample", "Note"])
            df_online.attrs["label"] = "Table 1"
            df_crop = pd.DataFrame([["S2", "NA"]], columns=["Sample", "Note"])
            df_crop.attrs["label"] = "Table 2"

            def _fake_export(p, target, existing_dfs=None, **kw):
                with pd.ExcelWriter(target, engine="openpyxl") as writer:
                    df_online.to_excel(writer, sheet_name="Table 1", index=False)
                    df_crop.to_excel(writer, sheet_name="Table 2", index=False)
                return True

            synced = {}
            def _capture_sync(p, target, dfs, a):
                synced["dfs"] = dfs

            args_pdf = types.SimpleNamespace(
                output=out_single, doi=None, url=None, title=None, headers=None,
                pdf_only=False, online_only=False, online_timeout=5,
                skip_supplementary=False, single_file=True, remediate_pdf="off",
                zotero_note=False, sync_zotero=True, zotero_api_key=None,
                zotero_user_id=None, db_path=None, table_idx="all",
            )
            with patch.object(online, "extract_tables_online", return_value=([df_online], "Paper")), \
                 patch.object(ezt, "extract_tables_from_pdf", return_value=([], [])), \
                 patch.object(ezt, "ocr_client", None), \
                 patch.object(ezt, "export_crops_to_excel", side_effect=_fake_export), \
                 patch.object(ezt, "_trigger_zotero_sync", side_effect=_capture_sync):
                ok = ezt._do_process_single_pdf(pdf_path, pdf_path, args_pdf)

            self.assertTrue(ok)
            self.assertEqual(len(synced["dfs"]), 2)
            self.assertEqual(synced["dfs"][0].iloc[0]["Note"], "NA")
            self.assertEqual(synced["dfs"][1].iloc[0]["Note"], "NA")

    def test_parse_structured_vlm_content_preserves_html_sub_sup_to_excel(self):
        """HTML <sub>/<sup> in VLM tables survives parse_structured_vlm_content -> save_tables_to_excel as CellRichText."""
        from openpyxl import load_workbook
        from openpyxl.cell.rich_text import CellRichText

        vlm_md = (
            "Table 1. Geochemistry\n"
            "<table>\n"
            "<tr><th>Sample</th><th>δ<sup>13</sup>C<sub>VPDB</sub></th><th>SO<sub>4</sub><sup>2-</sup></th></tr>\n"
            "<tr><td>CaCO<sub>3</sub></td><td>-5.2</td><td>12.3</td></tr>\n"
            "</table>\n"
        )
        dfs = tp.parse_structured_vlm_content(vlm_md)
        self.assertEqual(len(dfs), 1)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
            out_x = tf.name
        try:
            self.assertTrue(ex.save_tables_to_excel(dfs, out_x, single_file=True))
            wb = load_workbook(out_x, rich_text=True)
            ws = wb.active
            header_vals = [c.value for c in ws[1]]
            data_vals = [c.value for c in ws[2]]
            self.assertIsInstance(header_vals[1], CellRichText)
            self.assertIsInstance(header_vals[2], CellRichText)
            self.assertIsInstance(data_vals[0], CellRichText)
        finally:
            if os.path.exists(out_x):
                os.remove(out_x)

    def test_scrapling_fetch_html_wait_timeout_and_signature_filter(self):
        """_scrapling_fetch_html passes wait/timeout and preserves cookies when filtering unsupported kwargs."""
        from online import strategies

        captured = {}

        class StrictFetcher:
            @staticmethod
            def fetch(url, headless=True, wait=0, timeout=1000, cookies=None):
                captured["kwargs"] = {
                    "url": url,
                    "headless": headless,
                    "wait": wait,
                    "timeout": timeout,
                    "cookies": cookies,
                }
                return types.SimpleNamespace(html_content="<table><tr><th>A</th></tr><tr><td>1</td></tr></table>")

        fake_mod = types.SimpleNamespace(StealthyFetcher=StrictFetcher)
        with patch.dict(sys.modules, {"scrapling.fetchers": fake_mod, "scrapling": fake_mod}):
            html = strategies._scrapling_fetch_html("https://example.com", {"cookies": [{"name": "sid", "value": "1"}]})
        self.assertIn("<table>", html)
        self.assertEqual(captured["kwargs"]["wait"], 1200)
        self.assertEqual(captured["kwargs"]["timeout"], 45000)
        self.assertEqual(captured["kwargs"]["cookies"], [{"name": "sid", "value": "1"}])
        self.assertTrue(captured["kwargs"]["headless"])

    def test_remote_pdf_url_downloads_and_rejects_html_payloads(self):
        """Direct .pdf and Content-Type: application/pdf URLs stream to temp PDF and run local pipeline; HTML payloads fail safely."""
        import online

        with tempfile.TemporaryDirectory() as tmpdir:
            src_pdf = os.path.join(tmpdir, "src.pdf")
            doc = fitz.open()
            p1 = doc.new_page(width=595, height=842)
            p1.insert_text((72, 100), "Table 1. Remote PDF sample", fontsize=11)
            doc.save(src_pdf)
            doc.close()
            with open(src_pdf, "rb") as fh:
                pdf_bytes = fh.read()

            df_pdf = pd.DataFrame([["S1", "65.1", "15.2"]], columns=["Sample", "SiO2", "Al2O3"])
            df_pdf.attrs["label"] = "Table 1"
            df_pdf.attrs["page_idx"] = 0

            captured_paths = []
            seen_temp_paths = []

            def _fake_extract_pdf(path, **kwargs):
                captured_paths.append((path, os.path.isfile(path)))
                with open(path, "rb") as rf:
                    self.assertTrue(rf.read(5).startswith(b"%PDF-"))
                seen_temp_paths.append(path)
                return ([{"df": df_pdf.copy(), "page_idx": 0, "table_idx": 0, "bbox": None, "label": "Table 1"}], [])

            class _FakeResp:
                def __init__(self, payload, ct="application/pdf", status=200):
                    self.status_code = status
                    self.headers = {"Content-Type": ct}
                    self._payload = payload
                    self.content = payload

                def iter_content(self, chunk_size=65536):
                    for idx in range(0, len(self._payload), 128):
                        yield self._payload[idx:idx + 128]

                def close(self):
                    pass

            args_pdf = types.SimpleNamespace(
                output=os.path.join(tmpdir, "remote_out.xlsx"),
                doi=None, url=None, title=None, headers=None,
                pdf_only=False, online_only=False, online_timeout=5,
                skip_supplementary=False, single_file=True, remediate_pdf="off",
                zotero_note=False, sync_zotero=False, zotero_api_key=None,
                zotero_user_id=None, db_path=None, table_idx="all",
            )

            # 1. Direct .pdf URL -> downloads & runs extract_tables_from_pdf, then cleans temp file
            with patch.object(online, "extract_tables_online", return_value=[]), \
                 patch("requests.get", return_value=_FakeResp(pdf_bytes, "application/pdf")), \
                 patch.object(ezt, "extract_tables_from_pdf", side_effect=_fake_extract_pdf):
                ok = ezt.process_single_pdf("https://example.org/files/paper.pdf", args_pdf)
            self.assertTrue(ok)
            self.assertTrue(os.path.isfile(args_pdf.output))
            self.assertEqual(len(captured_paths), 1)
            self.assertTrue(captured_paths[0][1])
            for tp_file in seen_temp_paths:
                self.assertFalse(os.path.exists(tp_file), "Downloaded temp PDF should be cleaned up in finally")

            # 2. Extensionless download URL serving Content-Type: application/pdf & %PDF- bytes
            args_pdf2 = types.SimpleNamespace(**vars(args_pdf))
            args_pdf2.output = os.path.join(tmpdir, "remote_dl.xlsx")
            captured_paths.clear()
            seen_temp_paths.clear()
            with patch.object(online, "extract_tables_online", return_value=[]), \
                 patch("requests.head", return_value=_FakeResp(b"", "application/pdf")), \
                 patch("requests.get", return_value=_FakeResp(pdf_bytes, "application/pdf")), \
                 patch.object(ezt, "extract_tables_from_pdf", side_effect=_fake_extract_pdf):
                ok2 = ezt.process_single_pdf("https://example.org/download?article_id=42", args_pdf2)
            self.assertTrue(ok2)
            self.assertEqual(len(captured_paths), 1)
            for tp_file in seen_temp_paths:
                self.assertFalse(os.path.exists(tp_file))

            # 3. URL claiming .pdf extension that serves HTML paywall must never pass HTML to fitz/extract_tables_from_pdf
            html_bytes = b"<!DOCTYPE html><html><body>Login required</body></html>"
            with patch.object(online, "extract_tables_online", return_value=[]), \
                 patch("requests.get", return_value=_FakeResp(html_bytes, "text/html")), \
                 patch.object(ezt, "extract_tables_from_pdf", side_effect=AssertionError("HTML must not reach PDF extractor")):
                ok_html = ezt.process_single_pdf("https://example.org/paywall.pdf", args_pdf)
            self.assertFalse(ok_html)

    def test_mineru_subset_page_ranges_remap_and_step2_dedup(self):
        """MinerU subset page_ranges remaps subset-relative page_idx and deduplicates against pdf_inspector."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "two_page.pdf")
            doc = fitz.open()
            p0 = doc.new_page(width=595, height=842)
            p0.insert_text((72, 100), "Introduction page with no tables.", fontsize=11)
            p1 = doc.new_page(width=595, height=842)
            p1.insert_text((72, 100), "Table 3. Geochemical analyses", fontsize=11)
            x0, y0 = 72, 120
            col_w = [80, 70, 70, 70]
            row_h = 22
            rows = [
                ["Sample", "SiO2", "TiO2", "Al2O3"],
                ["G1", "65.1", "0.52", "15.4"],
                ["G2", "68.3", "0.41", "14.8"],
            ]
            for r_i, row in enumerate(rows):
                for c_i, val in enumerate(row):
                    rx0 = x0 + sum(col_w[:c_i])
                    ry0 = y0 + r_i * row_h
                    rect = fitz.Rect(rx0, ry0, rx0 + col_w[c_i], ry0 + row_h)
                    p1.draw_rect(rect, color=(0, 0, 0), width=0.8)
                    p1.insert_text((rx0 + 4, ry0 + 15), val, fontsize=9)
            doc.save(pdf_path)
            doc.close()

            # Direct helper check: subset [1] -> remaps page_idx=0 to 1, updates page_size, and is idempotent
            df_sub = pd.DataFrame(rows[1:], columns=rows[0])
            df_sub.attrs["page_idx"] = 0
            df_sub.attrs["extractor"] = "mineru_api"
            remapped = ocr_client._remap_mineru_subset_dfs([df_sub.copy()], page_ranges=[1], file_path=pdf_path)
            self.assertEqual(remapped[0].attrs["page_idx"], 1)
            self.assertEqual(remapped[0].attrs["page_size"], [595.0, 842.0])
            remapped_again = ocr_client._remap_mineru_subset_dfs(remapped, page_ranges=[1], file_path=pdf_path)
            self.assertEqual(remapped_again[0].attrs["page_idx"], 1)

            # Multi-page subset [1, 3] with relative [0, 1] -> [1, 3] (while true [1, 3] stays unchanged)
            df_m0 = pd.DataFrame([["A", "1"]], columns=["K", "V"])
            df_m0.attrs["page_idx"] = 0
            df_m1 = pd.DataFrame([["B", "2"]], columns=["K", "V"])
            df_m1.attrs["page_idx"] = 1
            multi_remapped = ocr_client._remap_mineru_subset_dfs([df_m0, df_m1], page_ranges="2,4")
            self.assertEqual([d.attrs["page_idx"] for d in multi_remapped], [1, 3])
            multi_idemp = ocr_client._remap_mineru_subset_dfs(multi_remapped, page_ranges="2,4")
            self.assertEqual([d.attrs["page_idx"] for d in multi_idemp], [1, 3])

            # REST API wrapper test: call_mineru_structured_api remaps subset-relative result
            fake_cli = MagicMock()
            fake_cli.is_available.return_value = False
            df_rest = pd.DataFrame(rows[1:], columns=rows[0])
            df_rest.attrs["page_idx"] = 0
            df_rest.attrs["extractor"] = "mineru_api"
            fake_rest_client = MagicMock()
            fake_rest_client.submit_task.return_value = "task-1"
            fake_rest_client.poll_task.return_value = {"success": True, "data": {}}
            fake_rest_client.parse_mineru_structured_result.return_value = [df_rest]
            with patch.object(ocr_client, "MinerUCLIClient", return_value=fake_cli), \
                 patch.object(ocr_client, "MinerUClient", return_value=fake_rest_client):
                api_dfs = ocr_client.call_mineru_structured_api(
                    pdf_path, config={"MINERU_API_KEY": "k", "MINERU_USE_CLI": "off"}, page_ranges=[1]
                )
            self.assertEqual(len(api_dfs), 1)
            self.assertEqual(api_dfs[0].attrs["page_idx"], 1)

            # CLI wrapper test: MinerUCLIClient.extract remaps subset-relative archive DataFrames
            df_cli_raw = pd.DataFrame(rows[1:], columns=rows[0])
            df_cli_raw.attrs["page_idx"] = 0
            cli_inst = ocr_client.MinerUCLIClient(cli_path=__file__)
            fake_proc = MagicMock(returncode=0)
            fake_proc.communicate.return_value = ("", "")
            with patch("ocr_client.subprocess.Popen", return_value=fake_proc), \
                 patch.object(ocr_client, "extract_mineru_structured_archive", return_value=[df_cli_raw]), \
                 patch.object(cli_inst, "_read_markdown_from_dir", return_value="<!-- Page 1 -->\n| Sample |\n|---|"):
                cli_res = cli_inst.extract(pdf_path, page_ranges=[1])
            self.assertTrue(cli_res["success"])
            self.assertEqual(cli_res["dfs"][0].attrs["page_idx"], 1)
            self.assertIn("<!-- Page 2 -->", cli_res["markdown"])

            # End-to-end Step 2 test even if call_mineru_structured_api itself is mocked with stale page_idx=0 and no label
            df_mock = pd.DataFrame(rows[1:], columns=rows[0])
            df_mock.attrs["page_idx"] = 0
            df_mock.attrs["extractor"] = "mineru_api"
            with patch.object(pte.ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(pte.ocr_client, "call_mineru_structured_api", return_value=[df_mock]):
                res, _ = pte.extract_tables_from_pdf(pdf_path, candidate_pages=[1], use_ocr_fallback=False)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["page_idx"], 1)
            self.assertEqual(res[0]["label"], "Table 3")

    def test_mixed_orientation_sideways_table_no_duplicates(self):
        """Mixed upright+90°-rotated vector table produces 1 clean Table 3 without transposed/reversed duplicates or fake Table 2."""
        # 1. Unit check: _align_page_tables clusters strong spatial matches despite low content similarity
        #    while refusing to collapse small nested sub-tables (area_ratio > 2.5)
        df_a = pd.DataFrame([["Th_ppm", "34.5"], ["U_ppm", "12.4"]], columns=["A", "B"])
        df_b = pd.DataFrame([["mpp_hT", "5.43"], ["mpp_U", "4.21"]], columns=["X", "Y"])
        df_nested = pd.DataFrame([["Sub1", "Sub2"], ["0.1", "0.2"]], columns=["P", "Q"])
        clusters = pte._align_page_tables(
            {
                "find_tables": [{"df": df_a, "page_idx": 1, "bbox": [180.0, 350.0, 264.0, 620.0]}],
                "pdfplumber": [{"df": df_b, "page_idx": 1, "bbox": [180.0, 350.0, 264.0, 620.0]}],
            },
            ["find_tables", "pdfplumber"],
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(set(clusters[0].keys()), {"find_tables", "pdfplumber"})

        clusters_nested = pte._align_page_tables(
            {
                "find_tables": [{"df": df_a, "page_idx": 1, "bbox": [50.0, 50.0, 500.0, 500.0]}],
                "pdfplumber": [{"df": df_nested, "page_idx": 1, "bbox": [100.0, 100.0, 140.0, 140.0]}],
            },
            ["find_tables", "pdfplumber"],
        )
        self.assertEqual(len(clusters_nested), 2)

        # 2. End-to-end 2-page PDF: Page 1 upright Table 1, Page 2 upright header + 90°-rotated Table 3
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "sideways_mixed.pdf")
            doc = fitz.open()
            p1 = doc.new_page(width=595, height=842)
            p1.insert_text((72, 100), "Table 1. Whole-rock compositions", fontsize=11)
            x0, y0 = 72, 120
            col_w = [100, 80, 80]
            row_h = 24
            rows1 = [["Sample", "SiO2", "Al2O3"], ["S1", "65.2", "15.1"], ["S2", "68.4", "14.5"]]
            for r_i, row in enumerate(rows1):
                for c_i, val in enumerate(row):
                    rx0 = x0 + sum(col_w[:c_i])
                    ry0 = y0 + r_i * row_h
                    rect = fitz.Rect(rx0, ry0, rx0 + col_w[c_i], ry0 + row_h)
                    p1.draw_rect(rect, color=(0, 0, 0), width=0.8)
                    p1.insert_text((rx0 + 6, ry0 + 16), val, fontsize=10)

            p2 = doc.new_page(width=595, height=842)
            p2.insert_text((72, 50), "Journal Header 2024", fontsize=11)
            p2.insert_text((150, 620), "Table 3. Trace element concentrations", fontsize=11, rotate=90)
            rows3 = [["Sample", "U_ppm", "Th_ppm"], ["A1", "12.4", "34.5"], ["A2", "18.1", "41.2"]]
            base_x, base_y, r_w, c_h = 180, 620, 28, 90
            for r_i, row in enumerate(rows3):
                for c_i, val in enumerate(row):
                    rx0 = base_x + r_i * r_w
                    rx1 = rx0 + r_w
                    ry1 = base_y - c_i * c_h
                    ry0 = ry1 - c_h
                    rect = fitz.Rect(rx0, ry0, rx1, ry1)
                    p2.draw_rect(rect, color=(0, 0, 0), width=0.8)
                    p2.insert_text((rx0 + 18, ry1 - 10), val, fontsize=10, rotate=90)

            doc.save(pdf_path)
            doc.close()

            with patch.object(pte.ocr_client, "is_mineru_available", return_value=False):
                results, _ = pte.extract_tables_from_pdf(pdf_path, use_ocr_fallback=False)

            self.assertEqual(len(results), 2)
            self.assertEqual([r["page_idx"] for r in results], [0, 1])
            self.assertEqual([r["label"] for r in results], ["Table 1", "Table 3"])
            self.assertEqual(list(results[1]["df"].columns), ["Sample", "U_ppm", "Th_ppm"])

            # 3. Direct dedup check: fallback label on colliding bbox cannot survive alongside verified Table 3
            df_clean = pd.DataFrame([["A1", "12.4", "34.5"]], columns=["Sample", "U_ppm", "Th_ppm"])
            df_clean.attrs.update({"page_idx": 1, "label": "Table 3", "extractor": "pdf_inspector"})
            df_dup = pd.DataFrame([["A1", "12.4", "34.5"]], columns=["Sample", "U_ppm", "Th_ppm"])
            df_dup.attrs.update({"page_idx": 1, "label": "Table 2", "_label_is_fallback": True, "extractor": "camelot"})
            deduped = pte._dedup_same_page_tables([
                {"df": df_clean, "page_idx": 1, "bbox": [180.0, 350.0, 264.0, 620.0], "label": "Table 3", "extractor": "pdf_inspector"},
                {"df": df_dup, "page_idx": 1, "bbox": [181.0, 351.0, 263.0, 619.0], "label": "Table 2", "extractor": "camelot"},
            ])
            self.assertEqual(len(deduped), 1)
            self.assertEqual(deduped[0]["label"], "Table 3")

    def test_remote_pdf_split_magic_and_error_payload_and_sync_provenance(self):
        """Split %PDF- chunks succeed, 403 text masked as application/pdf fails safely, and _trigger_zotero_sync keeps original URL."""
        import online

        class _ChunkResp:
            def __init__(self, ct, chunks):
                self.status_code = 200
                self.headers = {"Content-Type": ct}
                self._chunks = chunks
                self.content = b"".join(chunks)

            def iter_content(self, chunk_size=65536):
                return iter(self._chunks)

            def close(self):
                pass

        temps = []
        try:
            # 1. Split %PDF- magic across tiny chunks under application/octet-stream
            with patch("requests.head", return_value=_ChunkResp("application/octet-stream", [])), \
                 patch("requests.get", return_value=_ChunkResp("application/octet-stream", [b"%\x50", b"DF-1.5\n%test"])):
                dl = ezt._download_remote_pdf("https://example.org/download?id=9", temps)
            self.assertIsNotNone(dl)
            self.assertTrue(os.path.isfile(dl))

            # 2. Non-PDF error text ("403 Forbidden") even with Content-Type: application/pdf must be rejected cleanly
            with patch("requests.get", return_value=_ChunkResp("application/pdf", [b"403 Forbidden by CDN"])):
                bad_dl = ezt._download_remote_pdf("https://example.org/forbidden.pdf", temps)
            self.assertIsNone(bad_dl)
        finally:
            for tp_file in temps:
                if os.path.exists(tp_file):
                    os.remove(tp_file)

        # 3. _trigger_zotero_sync must receive original URL rather than temp /tmp/zotero_remote_*.pdf path
        with tempfile.TemporaryDirectory() as tmpdir:
            src_pdf = os.path.join(tmpdir, "src.pdf")
            doc = fitz.open()
            doc.new_page(width=595, height=842).insert_text((72, 100), "Table 1. Remote test", fontsize=11)
            doc.save(src_pdf)
            doc.close()
            with open(src_pdf, "rb") as fh:
                pdf_bytes = fh.read()

            df_pdf = pd.DataFrame([["S1", "65.1", "15.2"]], columns=["Sample", "SiO2", "Al2O3"])
            df_pdf.attrs["label"] = "Table 1"
            df_pdf.attrs["page_idx"] = 0
            sync_paths = []

            args_pdf = types.SimpleNamespace(
                output=os.path.join(tmpdir, "remote_sync.xlsx"),
                doi=None, url=None, title=None, headers=None,
                pdf_only=True, online_only=False, online_timeout=5,
                skip_supplementary=False, single_file=True, remediate_pdf="off",
                zotero_note=False, sync_zotero=False, zotero_api_key=None,
                zotero_user_id=None, db_path=None, table_idx="all",
            )
            with patch.object(online, "extract_tables_online", return_value=[]), \
                 patch("requests.get", return_value=_ChunkResp("application/pdf", [pdf_bytes[:3], pdf_bytes[3:]])), \
                 patch.object(ezt, "extract_tables_from_pdf", return_value=([{"df": df_pdf, "page_idx": 0, "table_idx": 0, "bbox": None, "label": "Table 1"}], [])), \
                 patch.object(ezt, "_trigger_zotero_sync", side_effect=lambda p, *a, **kw: sync_paths.append(p)):
                ok = ezt.process_single_pdf("https://example.org/files/paper.pdf?token=abc", args_pdf)
            self.assertTrue(ok)
            self.assertEqual(sync_paths, ["https://example.org/files/paper.pdf?token=abc"])

    def test_270deg_sideways_upside_down_and_step2_partial_mineru_fallback(self):
        """270°-rotated table avoids upside-down pdf_inspector & Camelot stream wrap; Step 2 partial MinerU miss recovers Page 2 cleanly."""
        # 1. _detect_pdf_needs_ocr respects string page_ranges="1" vs "2"
        with tempfile.TemporaryDirectory() as tmpdir:
            ocr_probe_pdf = os.path.join(tmpdir, "ocr_probe.pdf")
            doc = fitz.open()
            doc.new_page().insert_text((72, 100), "Rich selectable text on page 1. " * 10)
            doc.new_page()  # blank page 2
            doc.save(ocr_probe_pdf)
            doc.close()
            self.assertFalse(ocr_client._detect_pdf_needs_ocr(ocr_probe_pdf, page_ranges="1"))
            self.assertTrue(ocr_client._detect_pdf_needs_ocr(ocr_probe_pdf, page_ranges="2"))

            # 2. 270°-rotated sideways mixed-orientation PDF (both WITH and WITHOUT pdf_inspector)
            pdf_270 = os.path.join(tmpdir, "sideways_270.pdf")
            doc2 = fitz.open()
            pg0 = doc2.new_page(width=595, height=842)
            pg0.insert_text((72, 100), "Table 1. Normal summary")
            for r_idx, row in enumerate([["Site", "Au_ppb", "Ag_ppm"], ["S1", "12.5", "0.8"], ["S2", "18.1", "1.2"]]):
                for c_idx, val in enumerate(row):
                    pg0.insert_text((72 + c_idx * 100, 140 + r_idx * 20), val)
            pg0.draw_rect(fitz.Rect(65, 125, 365, 195))

            pg1 = doc2.new_page(width=595, height=842)
            pg1.insert_text((72, 50), "Journal Header 2024")
            pg1.insert_text((100, 700), "Table 3. Sideways 270 Geochemistry", rotate=270)
            rows270 = [["Sample", "U_ppm", "Th_ppm"], ["R-01", "4.5", "18.2"], ["R-02", "6.1", "21.0"]]
            for r_idx, row in enumerate(rows270):
                for c_idx, val in enumerate(row):
                    pg1.insert_text((300 - r_idx * 40, 200 + c_idx * 120), val, rotate=270)
            for x in [195, 235, 275, 315]:
                pg1.draw_line((x, 180), (x, 540))
            for y in [180, 300, 420, 540]:
                pg1.draw_line((195, y), (315, y))
            doc2.save(pdf_270)
            doc2.close()

            with patch.object(pte.ocr_client, "is_mineru_available", return_value=False):
                res_normal, _ = pte.extract_tables_from_pdf(pdf_270, use_ocr_fallback=False)
                self.assertEqual([r["label"] for r in res_normal], ["Table 1", "Table 3"])
                self.assertEqual(list(res_normal[1]["df"].columns), ["Sample", "U_ppm", "Th_ppm"])

                with patch.object(pte, "classify_and_extract_via_inspector", return_value={"pdf_type": "text_based", "confidence": 1.0, "pages_with_tables": [], "tables": []}):
                    res_no_insp, _ = pte.extract_tables_from_pdf(pdf_270, use_ocr_fallback=False)
                self.assertEqual([r["label"] for r in res_no_insp], ["Table 1", "Table 3"])
                self.assertEqual(list(res_no_insp[1]["df"].columns), ["Sample", "U_ppm", "Th_ppm"])

                # 3. Step 2 partial MinerU failure (Page 1 rescued via inspector, Page 2 missed by MinerU) recovers Page 2 via Step 3
                insp0 = pte.classify_and_extract_via_inspector(pdf_270)
                insp0["tables"] = [t for t in insp0["tables"] if t.get("page_idx") == 0]
                insp0["pages_with_tables"] = [0]
                with patch.object(pte, "classify_and_extract_via_inspector", return_value=insp0), \
                     patch.object(pte.ocr_client, "is_mineru_available", return_value=True), \
                     patch.object(pte.ocr_client, "call_mineru_structured_api", return_value=[]):
                    res_partial, _ = pte.extract_tables_from_pdf(pdf_270, use_ocr_fallback=False)
                self.assertEqual([r["label"] for r in res_partial], ["Table 1", "Table 3"])
                self.assertEqual(list(res_partial[1]["df"].columns), ["Sample", "U_ppm", "Th_ppm"])

    def test_90deg_sideways_split_cells_bidirectional_dedup_and_alignment(self):
        """90°-sideways table with split cells (cov_gc < 0.55, cov_cg >= 0.65) suppresses garbled find_tables across alignment, voting, dedup, and Step 2."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_90 = os.path.join(tmpdir, "side_90.pdf")
            doc = fitz.open()
            pg0 = doc.new_page(width=595, height=842)
            pg0.insert_text((72, 100), "Table 1. Normal summary")
            for r_idx, row in enumerate([["Site", "Au_ppb", "Ag_ppm"], ["S1", "12.5", "0.8"], ["S2", "18.1", "1.2"]]):
                for c_idx, val in enumerate(row):
                    pg0.insert_text((72 + c_idx * 100, 140 + r_idx * 20), val)
            pg0.draw_rect(fitz.Rect(65, 125, 365, 195))

            pg1 = doc.new_page(width=595, height=842)
            pg1.insert_text((250, 30), "Journal Header 2024")
            pg1.insert_text((100, 700), "Table 3. Sideways 90 Geochemistry", rotate=90)
            rows90 = [["Sample", "U_ppm", "Th_ppm"], ["S1", "4.2", "12.1"], ["S2", "5.8", "10.5"], ["S3", "7.1", "15.0"]]
            for r_idx, row in enumerate(rows90):
                for c_idx, val in enumerate(row):
                    pg1.insert_text((155 + r_idx * 25, 632 - c_idx * 160), val, rotate=90)
            for x in [140, 165, 190, 215, 240]:
                pg1.draw_line((x, 150), (x, 650))
            for y in [150, 300, 450, 650]:
                pg1.draw_line((140, y), (240, y))
            doc.save(pdf_90)
            doc.close()

            # Verify split-cell condition actually triggers cov_gc < 0.55 and cov_cg >= 0.65
            ft_list = pte.extract_via_find_tables(pdf_90, pages=[1])
            self.assertEqual(len(ft_list), 1)
            df_ft_garbled = ft_list[0]["df"]
            df_clean = pd.DataFrame(rows90[1:], columns=rows90[0])
            cov_gc, n_gc = pte._atomic_token_containment(df_ft_garbled, df_clean, allow_reversed=True)
            cov_cg, n_cg = pte._atomic_token_containment(df_clean, df_ft_garbled, allow_reversed=True)
            self.assertGreaterEqual(n_gc, 3)
            self.assertGreaterEqual(n_cg, 3)
            self.assertLess(cov_gc, 0.55)
            self.assertGreaterEqual(cov_cg, 0.65)

            # 1. End-to-end native extraction (pdf_inspector enabled, MinerU mocked off, OCR off)
            with patch.object(pte.ocr_client, "is_mineru_available", return_value=False):
                res, _ = pte.extract_tables_from_pdf(pdf_90, use_ocr_fallback=False)
            self.assertEqual(len(res), 2)
            self.assertEqual([r["label"] for r in res], ["Table 1", "Table 3"])
            self.assertEqual(list(res[1]["df"].columns), ["Sample", "U_ppm", "Th_ppm"])

            # 2. Step 2 MinerU unboxed unlabeled clean tables must absorb rotated find_tables without duplicating
            m_df1 = pd.DataFrame([["S1", "12.5", "0.8"], ["S2", "18.1", "1.2"]], columns=["Site", "Au_ppb", "Ag_ppm"])
            m_df1.attrs.update({"page_idx": 0, "extractor": "mineru_api"})
            m_df3 = pd.DataFrame(rows90[1:], columns=rows90[0])
            m_df3.attrs.update({"page_idx": 1, "extractor": "mineru_api"})
            with patch.object(pte.ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(pte.ocr_client, "call_mineru_structured_api", return_value=[m_df1, m_df3]):
                res_m, _ = pte.extract_tables_from_pdf(pdf_90, use_ocr_fallback=False)
            self.assertEqual(len(res_m), 2)
            self.assertEqual([r["label"] for r in res_m], ["Table 1", "Table 3"])
            self.assertEqual(list(res_m[1]["df"].columns), ["Sample", "U_ppm", "Th_ppm"])

            # 3. Direct unit checks: _align_page_tables, three_way_vote, _dedup_same_page_tables
            df_pi = df_clean.copy()
            df_pi.attrs.update({"page_idx": 1, "label": "Table 3", "extractor": "pdf_inspector"})
            df_ft_copy = df_ft_garbled.copy()
            df_ft_copy.attrs.update({"page_idx": 1, "label": "Table 3", "extractor": "find_tables"})
            df_other = pd.DataFrame([["X1", "0.1", "0.2"], ["X2", "0.3", "0.4"]], columns=["Site", "Fe_pct", "Mn_ppm"])
            df_other.attrs.update({"page_idx": 1, "extractor": "find_tables"})

            with fitz.open(pdf_90) as pdf_doc:
                clusters_same = pte._align_page_tables(
                    {
                        "pdf_inspector": [{"df": df_pi, "page_idx": 1, "bbox": None, "label": "Table 3"}],
                        "find_tables": [{"df": df_ft_copy, "page_idx": 1, "bbox": [140.0, 150.0, 240.0, 650.0], "label": "Table 3"}],
                    },
                    ["pdf_inspector", "find_tables"],
                    pdf_doc=pdf_doc,
                )
                self.assertEqual(len(clusters_same), 1)
                self.assertEqual(set(clusters_same[0].keys()), {"pdf_inspector", "find_tables"})

                # Distinct table on the same page must NOT collapse with unboxed pdf_inspector
                clusters_diff = pte._align_page_tables(
                    {
                        "pdf_inspector": [{"df": df_pi, "page_idx": 1, "bbox": None, "label": "Table 3"}],
                        "find_tables": [{"df": df_other, "page_idx": 1, "bbox": [300.0, 150.0, 500.0, 400.0]}],
                    },
                    ["pdf_inspector", "find_tables"],
                    pdf_doc=pdf_doc,
                )
                self.assertEqual(len(clusters_diff), 2)

            # Force separate clusters in three_way_vote (disjoint bboxes) to exercise post-cluster clean_ctm_items suppression
            fused_vote, _, _ = pte.three_way_vote(
                inspector_tables=[{"df": df_pi.copy(), "page_idx": 1, "bbox": None, "label": "Table 3"}],
                find_tables_results=[{"df": df_ft_copy.copy(), "page_idx": 1, "bbox": [140.0, 150.0, 240.0, 650.0], "label": "Table 3"}],
                plumber_tables=[],
                camelot_tables=[],
                pdf_path=pdf_90,
            )
            self.assertEqual(len(fused_vote), 1)
            self.assertEqual(fused_vote[0]["df"].attrs.get("extractor"), "pdf_inspector")

            fused_separate, _, _ = pte.three_way_vote(
                inspector_tables=[{"df": df_pi.copy(), "page_idx": 1, "bbox": [300.0, 150.0, 500.0, 400.0], "label": "Table 3"}],
                find_tables_results=[{"df": df_ft_copy.copy(), "page_idx": 1, "bbox": [140.0, 150.0, 240.0, 650.0], "label": "Table 3"}],
                plumber_tables=[],
                camelot_tables=[],
                pdf_path=pdf_90,
            )
            self.assertEqual(len(fused_separate), 1)
            self.assertEqual(fused_separate[0]["df"].attrs.get("extractor"), "pdf_inspector")

            # Unlabeled garbled candidate against clean unboxed table still suppressed via cov_cg >= 0.65
            df_ft_unlabeled = df_ft_garbled.copy()
            df_ft_unlabeled.attrs.update({"page_idx": 1, "label": None, "extractor": "find_tables"})
            deduped = pte._dedup_same_page_tables(
                [
                    {"df": df_ft_unlabeled, "page_idx": 1, "bbox": [140.0, 150.0, 240.0, 650.0], "extractor": "find_tables"},
                    {"df": df_pi.copy(), "page_idx": 1, "bbox": None, "label": "Table 3", "extractor": "pdf_inspector"},
                ],
                pdf_path=pdf_90,
            )
            self.assertEqual(len(deduped), 1)
            self.assertEqual(deduped[0]["df"].attrs.get("extractor"), "pdf_inspector")

            # Conflicting verified labels must keep both tables
            df_ft_t4 = df_ft_garbled.copy()
            df_ft_t4.attrs.update({"page_idx": 1, "label": "Table 4", "extractor": "find_tables"})
            deduped_conflict = pte._dedup_same_page_tables(
                [
                    {"df": df_ft_t4, "page_idx": 1, "bbox": [140.0, 150.0, 240.0, 650.0], "label": "Table 4", "extractor": "find_tables"},
                    {"df": df_pi.copy(), "page_idx": 1, "bbox": None, "label": "Table 3", "extractor": "pdf_inspector"},
                ],
                pdf_path=pdf_90,
            )
            self.assertEqual(len(deduped_conflict), 2)

            # Same verified label without pdf_path must still deduplicate non-reversed split-cell find_tables vs unboxed pdf_inspector
            df_ft_t3 = df_ft_garbled.copy()
            df_ft_t3.attrs.update({"page_idx": 1, "label": "Table 3", "extractor": "find_tables"})
            deduped_no_pdf = pte._dedup_same_page_tables(
                [
                    {
                        "df": df_ft_t3,
                        "page_idx": 1,
                        "bbox": [140.0, 150.0, 240.0, 650.0],
                        "label": "Table 3",
                        "extractor": "find_tables",
                    },
                    {
                        "df": df_pi.copy(),
                        "page_idx": 1,
                        "bbox": None,
                        "label": "Table 3",
                        "extractor": "pdf_inspector",
                    },
                ],
                pdf_path=None,
            )
            self.assertEqual(len(deduped_no_pdf), 1)
            self.assertEqual(deduped_no_pdf[0]["df"].attrs.get("extractor"), "pdf_inspector")

            # Fallback synthetic label ("Table 2", _label_is_fallback=True) must align with verified "Table 3",
            # whereas two verified conflicting labels must remain separate.
            df_pi_fb = df_pi.copy()
            df_pi_fb.attrs.update(
                {
                    "page_idx": 1,
                    "label": "Table 2",
                    "table_title": "Table 2",
                    "_label_is_fallback": True,
                    "extractor": "pdf_inspector",
                }
            )
            df_cam_t3 = df_pi.copy()
            df_cam_t3.attrs.update({"page_idx": 1, "label": "Table 3", "extractor": "camelot"})
            with fitz.open(pdf_90) as doc_90:
                align_prio = ["pdf_inspector", "camelot", "find_tables"]
                clusters_fb = pte._align_page_tables(
                    {
                        "pdf_inspector": [
                            {
                                "df": df_pi_fb.copy(),
                                "page_idx": 1,
                                "bbox": None,
                                "label": "Table 2",
                                "_label_is_fallback": True,
                                "extractor": "pdf_inspector",
                            },
                        ],
                        "find_tables": [
                            {
                                "df": df_ft_t3.copy(),
                                "page_idx": 1,
                                "bbox": [140.0, 150.0, 240.0, 650.0],
                                "label": "Table 3",
                                "extractor": "find_tables",
                            },
                        ],
                    },
                    align_prio,
                    pdf_doc=doc_90,
                )
                self.assertEqual(len(clusters_fb), 1)

                clusters_fb_clean = pte._align_page_tables(
                    {
                        "pdf_inspector": [
                            {
                                "df": df_pi_fb.copy(),
                                "page_idx": 1,
                                "bbox": None,
                                "label": "Table 2",
                                "_label_is_fallback": True,
                                "extractor": "pdf_inspector",
                            },
                        ],
                        "camelot": [
                            {
                                "df": df_cam_t3.copy(),
                                "page_idx": 1,
                                "bbox": [140.0, 150.0, 240.0, 650.0],
                                "label": "Table 3",
                                "extractor": "camelot",
                            },
                        ],
                    },
                    align_prio,
                    pdf_doc=doc_90,
                )
                self.assertEqual(len(clusters_fb_clean), 1)

                df_pi_verified_t2 = df_pi.copy()
                df_pi_verified_t2.attrs.update({"page_idx": 1, "label": "Table 2", "extractor": "pdf_inspector"})
                clusters_verified_conflict = pte._align_page_tables(
                    {
                        "pdf_inspector": [
                            {
                                "df": df_pi_verified_t2,
                                "page_idx": 1,
                                "bbox": None,
                                "label": "Table 2",
                                "extractor": "pdf_inspector",
                            },
                        ],
                        "camelot": [
                            {
                                "df": df_cam_t3.copy(),
                                "page_idx": 1,
                                "bbox": [140.0, 150.0, 240.0, 650.0],
                                "label": "Table 3",
                                "extractor": "camelot",
                            },
                        ],
                    },
                    align_prio,
                    pdf_doc=doc_90,
                )
                self.assertEqual(len(clusters_verified_conflict), 2)

            # Rotated caption emitted AFTER the 90°-rotated table (x=270, y=650):
            # pdf_inspector initially synthesizes fallback "Table 2" before seeing the trailing caption,
            # yet neither native Step 3 nor Step 2 MinerU may emit a duplicate ("Table 2" + "Table 3").
            from pathlib import Path
            from unittest import mock
            pdf_cap_right = str(Path(pdf_90).parent / "side_90_cap_right.pdf")
            with fitz.open() as doc_cr:
                p0 = doc_cr.new_page(width=595, height=842)
                p0.insert_text((50, 80), "Table 1. Major Elements", fontsize=11)
                xs0 = [50, 140, 230, 320]
                ys0 = [100, 124, 148, 172, 196]
                for x in xs0:
                    p0.draw_line((x, ys0[0]), (x, ys0[-1]))
                for y in ys0:
                    p0.draw_line((xs0[0], y), (xs0[-1], y))
                rows0 = [
                    ["Sample", "SiO2", "Al2O3"],
                    ["A1", "65.2", "14.8"],
                    ["A2", "68.1", "13.9"],
                    ["A3", "70.4", "13.2"],
                ]
                for r_i, row_vals in enumerate(rows0):
                    for c_i, val in enumerate(row_vals):
                        p0.insert_text((xs0[c_i] + 6, ys0[r_i] + 16), val, fontsize=10)

                p1 = doc_cr.new_page(width=595, height=842)
                p1.insert_text((250, 30), "Journal Header 2024", fontsize=10)
                xs1 = [140, 165, 190, 215, 240]
                ys1 = [650, 483, 316, 150]
                for x in xs1:
                    p1.draw_line((x, ys1[-1]), (x, ys1[0]))
                for y in ys1:
                    p1.draw_line((xs1[0], y), (xs1[-1], y))
                rows1 = [
                    ["Sample", "U_ppm", "Th_ppm"],
                    ["S1", "4.2", "12.1"],
                    ["S2", "5.8", "10.5"],
                    ["S3", "6.1", "15.0"],
                ]
                for r_i, row_vals in enumerate(rows1):
                    for c_i, val in enumerate(row_vals):
                        p1.insert_text((xs1[r_i] + 17, ys1[c_i] - 12), val, fontsize=10, rotate=90)
                p1.insert_text((270, 650), "Table 3. Sideways 90 Geochemistry", fontsize=11, rotate=90)
                doc_cr.save(pdf_cap_right)

            with mock.patch.object(pte.ocr_client, "is_mineru_available", return_value=False):
                cr_native, _ = pte.extract_tables_from_pdf(pdf_cap_right, use_ocr_fallback=False)
            self.assertEqual(len(cr_native), 2)
            self.assertEqual([t["df"].attrs.get("label") for t in cr_native], ["Table 1", "Table 3"])
            self.assertEqual(list(cr_native[1]["df"].columns), ["Sample", "U_ppm", "Th_ppm"])

            df_m_t1 = pd.DataFrame(
                [["A1", "65.2", "14.8"], ["A2", "68.1", "13.9"], ["A3", "70.4", "13.2"]],
                columns=["Sample", "SiO2", "Al2O3"],
            )
            df_m_t1.attrs.update(
                {"page_idx": 0, "bbox": None, "label": "Table 1", "table_title": "Table 1. Major Elements", "extractor": "mineru_api"}
            )
            df_m_t3 = pd.DataFrame(
                [["S1", "4.2", "12.1"], ["S2", "5.8", "10.5"], ["S3", "6.1", "15.0"]],
                columns=["Sample", "U_ppm", "Th_ppm"],
            )
            df_m_t3.attrs.update(
                {"page_idx": 1, "bbox": None, "label": "Table 3", "table_title": "Table 3. Sideways 90 Geochemistry", "extractor": "mineru_api"}
            )
            with mock.patch.object(pte.ocr_client, "is_mineru_available", return_value=True), \
                 mock.patch.object(pte.ocr_client, "call_mineru_structured_api", return_value=[df_m_t1, df_m_t3]):
                cr_mineru, _ = pte.extract_tables_from_pdf(pdf_cap_right, use_ocr_fallback=False)
            self.assertEqual(len(cr_mineru), 2)
            self.assertEqual([t["df"].attrs.get("label") for t in cr_mineru], ["Table 1", "Table 3"])
            self.assertEqual(list(cr_mineru[1]["df"].columns), ["Sample", "U_ppm", "Th_ppm"])

            # Even if Step 2 MinerU returns an unlabeled Page 2 table, Step 2 coverage from
            # pdf_inspector's fallback "Table 2" must preserve _label_is_fallback so caption
            # reconciliation binds the trailing rotated "Table 3" instead of sticking at "Table 2".
            df_m_t3_unlabeled = df_m_t3.copy()
            df_m_t3_unlabeled.attrs = {"page_idx": 1, "bbox": None, "extractor": "mineru_api"}
            with mock.patch.object(pte.ocr_client, "is_mineru_available", return_value=True), \
                 mock.patch.object(pte.ocr_client, "call_mineru_structured_api", return_value=[df_m_t1.copy(), df_m_t3_unlabeled]):
                cr_mineru_unlabeled, _ = pte.extract_tables_from_pdf(pdf_cap_right, use_ocr_fallback=False)
            self.assertEqual(len(cr_mineru_unlabeled), 2)
            self.assertEqual([t["df"].attrs.get("label") for t in cr_mineru_unlabeled], ["Table 1", "Table 3"])
            self.assertEqual(list(cr_mineru_unlabeled[1]["df"].columns), ["Sample", "U_ppm", "Th_ppm"])


if __name__ == "__main__":
    unittest.main()
