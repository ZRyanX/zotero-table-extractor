#!/usr/bin/env python3
"""
test_boost_enhancements.py — 针对五大架构增强特性的全面单元测试套件：
1. PP-StructureV3 + PaddleOCR-VL 保留与 IBM Docling (TableFormer) 离线备用降级
2. GMFT (TATR) 增强原生视觉表格提取与多方投票融合
3. img2table (OpenCV 纯 Python) 替代/增强 Camelot (免 Ghostscript 依赖)
4. PyZotero 与 Zotero-Better-Notes 生态联动 (默认关闭开关、附件挂载、HTML卡片预览)
5. DeepDoc / MinerU 启发的列特征投影分布与单调 DP 跨页续表对齐
"""

import os
import sys
import tempfile
import unittest
import sqlite3
from unittest.mock import patch, MagicMock
from pathlib import Path

import pandas as pd
import numpy as np

# 确保 scripts 目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import common
from scripts import ocr_client
from scripts import pdf_table_extractor
from scripts import table_postprocess
from scripts import zotero_sync
from scripts import extract_zotero_table


class TestDoclingOfflineFallback(unittest.TestCase):
    """测试 1: PP-StructureV3 + PaddleOCR-VL 保留，Docling (TableFormer) 作为离线备用。"""

    def test_ppstructure_paddleocr_retained(self):
        """验证 PP-StructureV3 与 PaddleOCR-VL 核心逻辑完整保留且未被移除。"""
        self.assertTrue(hasattr(ocr_client, "run_ppstructure_vlm_pipeline"))
        self.assertTrue(hasattr(ocr_client, "call_paddleocr_vl_online_api"))
        self.assertTrue(hasattr(ocr_client, "run_paddleocr_vl"))
        self.assertTrue(hasattr(ocr_client, "call_paddleocr_job"))

    def test_docling_availability_probe(self):
        """验证 is_docling_available 正确探测 Docling 模块。"""
        self.assertIsInstance(ocr_client.is_docling_available(), bool)

    @patch("scripts.ocr_client.is_docling_available", return_value=True)
    def test_extract_via_docling_mock(self, mock_avail):
        """测试 extract_via_docling 提取表格并标准化返回结构。"""
        dummy_df = pd.DataFrame({"Element": ["SiO2", "TiO2"], "wt%": [52.3, 0.8]})
        mock_table = MagicMock()
        mock_table.export_to_dataframe.return_value = dummy_df
        mock_table.caption_text.return_value = "Major element oxides"
        mock_prov = MagicMock()
        mock_prov.page_no = 1
        mock_bbox = MagicMock(l=50.0, t=100.0, r=400.0, b=250.0)
        mock_prov.bbox = mock_bbox
        mock_table.prov = [mock_prov]

        mock_doc = MagicMock()
        mock_doc.tables = [mock_table]
        mock_conv_res = MagicMock()
        mock_conv_res.document = mock_doc

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_conv_res

        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp_pdf:
            with patch("docling.document_converter.DocumentConverter", return_value=mock_converter):
                results = ocr_client.extract_via_docling(tmp_pdf.name)

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertEqual(res["page_idx"], 0)
        self.assertEqual(res["df"].iloc[0, 0], "SiO2")
        self.assertEqual(res["df"].attrs["extractor"], "docling_tableformer")
        self.assertEqual(res["df"].attrs["table_title"], "Major element oxides")
        self.assertEqual(res["bbox"], [50.0, 100.0, 400.0, 250.0])

    def test_extract_via_ocr_cascades_to_docling(self):
        """测试 extract_via_ocr 在 PaddleOCR 失败时无缝降级至 Docling 离线备用。"""
        dummy_docling_res = [{
            "df": pd.DataFrame({"A": [1, 2], "B": [3, 4]}),
            "page_idx": 2,
            "table_idx": 0,
            "bbox": None,
        }]

        with patch("scripts.ocr_client.run_ppstructure_vlm_pipeline", return_value=""), \
             patch("scripts.ocr_client.extract_via_docling", return_value=dummy_docling_res):
            ocr_results = pdf_table_extractor.extract_via_ocr("dummy.pdf", pages=[2])

        self.assertEqual(len(ocr_results), 1)
        self.assertEqual(ocr_results[0]["df"].iloc[0, 0], 1)


class TestGMFTIntegration(unittest.TestCase):
    """测试 2: GMFT (TATR-based) 增强原生 PDF 表格提取与多方投票。"""

    def test_gmft_function_exists(self):
        """验证 extract_via_gmft 函数存在。"""
        self.assertTrue(hasattr(pdf_table_extractor, "extract_via_gmft"))

    def test_extract_via_gmft_mock(self):
        """验证 GMFT 表格提取与属性填充。"""
        dummy_df = pd.DataFrame({"Header1": ["val1"], "Header2": ["val2"]})
        mock_ft = MagicMock()
        mock_ft.df.return_value = dummy_df
        mock_tbl = MagicMock()
        mock_tbl.rect = MagicMock(x0=10, y0=20, x1=300, y1=150)

        mock_detector = MagicMock()
        mock_detector.extract.return_value = [mock_tbl]
        mock_formatter = MagicMock()
        mock_formatter.extract.return_value = mock_ft

        mock_page = MagicMock()
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 1
        mock_doc.get_page.return_value = mock_page

        mock_gmft = MagicMock()
        mock_gmft_auto = MagicMock()
        mock_gmft_auto.AutoTableDetector.return_value = mock_detector
        mock_gmft_auto.AutoTableFormatter.return_value = mock_formatter
        mock_gmft_pdf = MagicMock()
        mock_gmft_pdf.PyPDFium2Document.return_value = mock_doc

        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp_pdf:
            with patch.dict(sys.modules, {
                "gmft": mock_gmft,
                "gmft.auto": mock_gmft_auto,
                "gmft.pdf_bindings": mock_gmft_pdf,
            }), patch("scripts.pdf_table_extractor._GMFT_AVAILABLE", True), \
                patch("scripts.pdf_table_extractor._GMFT_CHECKED", True):
                tables = pdf_table_extractor.extract_via_gmft(tmp_pdf.name, pages=[0])

        self.assertEqual(len(tables), 1)
        tbl = tables[0]
        self.assertEqual(tbl["df"].attrs["extractor"], "gmft_tatr")
        self.assertEqual(tbl["page_idx"], 0)
        self.assertEqual(tbl["bbox"], [10, 20, 300, 150])

    def test_three_way_vote_with_gmft(self):
        """验证 GMFT 作为独立参选方接入多方投票，高相似度时达成多数共识。"""
        df_gmft = pd.DataFrame({"A": ["x", "y"], "B": ["1", "2"]})
        df_find = pd.DataFrame({"A": ["x", "y"], "B": ["1", "2"]})
        df_plumber = pd.DataFrame({"A": ["x", "y"], "B": ["1", "noise"]})

        gmft_res = [{"df": df_gmft, "page_idx": 0, "table_idx": 0, "bbox": None}]
        find_res = [{"df": df_find, "page_idx": 0, "table_idx": 0, "bbox": None}]
        plumb_res = [{"df": df_plumber, "page_idx": 0, "table_idx": 0, "bbox": None}]

        fused, logs, vote_summary = pdf_table_extractor.three_way_vote(
            inspector_tables=[],
            find_tables_results=find_res,
            plumber_tables=plumb_res,
            camelot_tables=[],
            gmft_tables=gmft_res,
        )

        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0]["df"].iloc[0, 0], "x")
        # 共识方优先选取 gmft
        self.assertIn("gmft", fused[0]["df"].attrs.get("vote_extractor", ""))

    def test_gmft_disabled_in_config(self):
        """验证当 GMFT_ENABLED 为 False 时跳过提取。"""
        with patch("scripts.common.load_config", return_value={"GMFT_ENABLED": False}):
            tables = pdf_table_extractor.extract_via_gmft("dummy.pdf")
            self.assertEqual(tables, [])


class TestImg2tableIntegration(unittest.TestCase):
    """测试 3: img2table 纯 Python OpenCV 提取 (免 Ghostscript 依赖)。"""

    def test_img2table_function_exists(self):
        """验证 extract_via_img2table 函数存在。"""
        self.assertTrue(hasattr(pdf_table_extractor, "extract_via_img2table"))

    def test_extract_via_img2table_mock(self):
        """验证 img2table 无 Ghostscript 依赖提取逻辑。"""
        dummy_df = pd.DataFrame({"ColA": ["10", "20"], "ColB": ["30", "40"]})
        mock_table = MagicMock()
        mock_table.df = dummy_df
        mock_table.title = "Sample Measurements"
        mock_bbox = MagicMock(x1=15, y1=25, x2=250, y2=180)
        mock_table.bbox = mock_bbox

        mock_doc = MagicMock()
        mock_doc.extract_tables.return_value = {0: [mock_table]}

        mock_img2table = MagicMock()
        mock_img2table_doc = MagicMock()
        mock_img2table_doc.PDF.return_value = mock_doc

        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp_pdf:
            with patch.dict(sys.modules, {
                "img2table": mock_img2table,
                "img2table.document": mock_img2table_doc,
            }), patch("scripts.pdf_table_extractor._IMG2TABLE_AVAILABLE", True), \
                patch("scripts.pdf_table_extractor._IMG2TABLE_CHECKED", True):
                tables = pdf_table_extractor.extract_via_img2table(tmp_pdf.name, pages=[0])

        self.assertEqual(len(tables), 1)
        t = tables[0]
        self.assertEqual(t["df"].attrs["extractor"], "img2table")
        self.assertEqual(t["df"].attrs["table_title"], "Sample Measurements")
        self.assertEqual(t["bbox"], [15, 25, 250, 180])

    def test_img2table_disabled_in_config(self):
        """验证当 IMG2TABLE_ENABLED 为 False 时跳过提取。"""
        with patch("scripts.common.load_config", return_value={"IMG2TABLE_ENABLED": False}):
            tables = pdf_table_extractor.extract_via_img2table("dummy.pdf")
            self.assertEqual(tables, [])

    def test_vote_with_img2table_replaces_missing_camelot(self):
        """验证当 Camelot 缺失时，img2table 与 find_tables 达成共识完成提取。"""
        df_img2table = pd.DataFrame({"X": ["a", "b"], "Y": ["1", "2"]})
        df_find = pd.DataFrame({"X": ["a", "b"], "Y": ["1", "2"]})

        img_res = [{"df": df_img2table, "page_idx": 0, "table_idx": 0, "bbox": None}]
        find_res = [{"df": df_find, "page_idx": 0, "table_idx": 0, "bbox": None}]

        fused, logs, vote_summary = pdf_table_extractor.three_way_vote(
            inspector_tables=[],
            find_tables_results=find_res,
            plumber_tables=[],
            camelot_tables=[],  # Camelot 缺失为空
            img2table_tables=img_res,
        )

        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0]["df"].iloc[0, 0], "a")


class TestZoteroSyncAndBetterNotes(unittest.TestCase):
    """测试 4: PyZotero 与 Zotero-Better-Notes 生态联动 (可选开关默认关闭)。"""

    def test_zotero_sync_disabled_by_default(self):
        """【强制约束】验证 ZOTERO_SYNC_ENABLED 默认必须为 False。"""
        cfg = common.load_config()
        self.assertFalse(cfg.get("ZOTERO_SYNC_ENABLED", True))

    def test_sync_skipped_when_disabled(self):
        """当开关为 False 时，sync_extracted_tables_to_zotero 立即返回 False，不执行任何写操作。"""
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as tmp_xlsx:
            res = zotero_sync.sync_extracted_tables_to_zotero(
                "dummy.pdf",
                tmp_xlsx.name,
                [pd.DataFrame({"A": [1]})],
                config={"ZOTERO_SYNC_ENABLED": False},
                force_sync=False,
            )
            self.assertFalse(res)

    def test_better_notes_html_formatting(self):
        """验证生成的 Better-Notes HTML 预览卡片结构完整规范。"""
        df = pd.DataFrame({
            "Sample": ["ZRN-01", "ZRN-02"],
            "SiO2": [72.5, 73.1],
            "TiO2": [0.15, 0.12]
        })
        df.attrs = {
            "label": "Table 1",
            "table_title": "Major elements of granite",
            "page_idx": 3,
            "vote_extractor": "gmft_tatr",
        }

        html = zotero_sync.format_better_notes_html(
            [df],
            pdf_path="/path/to/test_paper.pdf",
            excel_path="/path/to/Table_1.xlsx",
            metadata={"title": "Test Granitic Paper", "DOI": "10.1016/j.gr.2024.01.001"}
        )

        self.assertIn("better-notes-table-summary", html)
        self.assertIn("Test Granitic Paper", html)
        self.assertIn("10.1016/j.gr.2024.01.001", html)
        self.assertIn("Table 1: Major elements of granite", html)
        self.assertIn("Page 4", html)
        self.assertIn("2行 × 3列", html)
        self.assertIn("ZRN-01", html)
        self.assertIn("72.5", html)

    def test_resolve_zotero_item_key_from_db(self):
        """测试从 sqlite 模拟库中精准解析 Parent Item Key。"""
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp_db:
            conn = sqlite3.connect(tmp_db.name)
            c = conn.cursor()
            c.execute("CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT)")
            c.execute("CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER, path TEXT)")
            c.execute("CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT)")
            c.execute("CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT)")
            c.execute("CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER)")

            c.execute("INSERT INTO items VALUES (100, 'PARENTKEY'), (101, 'ATTACHKEY')")
            c.execute("INSERT INTO itemAttachments VALUES (101, 100, 'storage:my_paper.pdf')")
            c.execute("INSERT INTO fields VALUES (1, 'title')")
            c.execute("INSERT INTO itemDataValues VALUES (1, 'Geochemical Study')")
            c.execute("INSERT INTO itemData VALUES (100, 1, 1)")
            conn.commit()
            conn.close()

            parent_key, attach_key, meta = zotero_sync.resolve_zotero_item_key(
                "/tmp/storage/ATTACHKEY/my_paper.pdf",
                db_path=tmp_db.name
            )

            self.assertEqual(parent_key, "PARENTKEY")
            self.assertEqual(attach_key, "ATTACHKEY")
            self.assertEqual(meta.get("title"), "Geochemical Study")

    def test_sync_enabled_creates_local_preview_and_handles_api(self):
        """当开启 force_sync 时，成功生成本地 _better_notes.html 预览文件。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "paper.pdf")
            xlsx_path = os.path.join(tmp_dir, "paper_Table1.xlsx")
            with open(pdf_path, "w") as f:
                f.write("%PDF-1.4 dummy")
            df = pd.DataFrame({"ColA": [1, 2]})
            df.to_excel(xlsx_path, index=False)

            with patch("scripts.zotero_sync.PYZOTERO_AVAILABLE", False):
                ok = zotero_sync.sync_extracted_tables_to_zotero(
                    pdf_path,
                    xlsx_path,
                    [df],
                    config={"ZOTERO_SYNC_ENABLED": True},
                    force_sync=True,
                )
                self.assertTrue(ok)
                preview_file = os.path.join(tmp_dir, "paper_Table1_better_notes.html")
                self.assertTrue(os.path.exists(preview_file))
                with open(preview_file, encoding="utf-8") as pf:
                    content = pf.read()
                    self.assertIn("better-notes-table-card", content)

    def test_zotero_sync_loads_tables_from_excel_when_dfs_empty(self):
        """验证当未直接传入 dfs 时，若 Excel 文件存在，可自适应从工作簿读取表格并生成 Better-Notes 预览。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "paper.pdf")
            xlsx_path = os.path.join(tmp_dir, "paper_Table1.xlsx")
            with open(pdf_path, "w") as f:
                f.write("%PDF-1.4 dummy")
            df = pd.DataFrame({"Sample": ["A", "B"], "Val": [10, 20]})
            df.to_excel(xlsx_path, sheet_name="Table 1", index=False)

            with patch("scripts.zotero_sync.PYZOTERO_AVAILABLE", False):
                ok = zotero_sync.sync_extracted_tables_to_zotero(
                    pdf_path,
                    xlsx_path,
                    [],
                    config={"ZOTERO_SYNC_ENABLED": True},
                    force_sync=True,
                )
                self.assertTrue(ok)
                preview_file = os.path.join(tmp_dir, "paper_Table1_better_notes.html")
                self.assertTrue(os.path.exists(preview_file))

    def test_resolve_zotero_item_key_standalone_attachment(self):
        """测试当附件为独立条目 (parentItemID 为 NULL) 时，不会错误误绑其他同名文件的父条目。"""
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp_db:
            conn = sqlite3.connect(tmp_db.name)
            c = conn.cursor()
            c.execute("CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT)")
            c.execute("CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER, path TEXT)")
            c.execute("CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT)")
            c.execute("CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT)")
            c.execute("CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER)")

            # 插入一个独立附件 (没有 parentItemID)
            c.execute("INSERT INTO items VALUES (200, 'STANDALONEKEY')")
            c.execute("INSERT INTO itemAttachments VALUES (200, NULL, 'storage:main.pdf')")

            # 插入另一个不相关的条目，也恰好有名为 main.pdf 的附件
            c.execute("INSERT INTO items VALUES (300, 'OTHERPARENT'), (301, 'OTHERATTACH')")
            c.execute("INSERT INTO itemAttachments VALUES (301, 300, 'storage:main.pdf')")
            conn.commit()
            conn.close()

            parent_key, attach_key, meta = zotero_sync.resolve_zotero_item_key(
                "/dummy/storage/STANDALONEKEY/main.pdf",
                db_path=tmp_db.name
            )

            # 应该精确识别出独立附件，且 parent_key 保持 None，绝不能误绑 OTHERPARENT
            self.assertIsNone(parent_key)
            self.assertEqual(attach_key, "STANDALONEKEY")


class TestDeepDocMinerUContinuationMerging(unittest.TestCase):
    """测试 5: DeepDoc / MinerU 启发的列特征投影分布与单调 DP 跨页续表对齐。"""

    def test_compute_column_profile(self):
        """测试列特征分布分析 (数值比例、空值比例、平均长度)。"""
        s_numeric = pd.Series(["52.3", "14.2", "0.85", "<0.01", "bdl"])
        prof_num = table_postprocess.compute_column_profile(s_numeric)
        self.assertGreaterEqual(prof_num["num_ratio"], 0.90)
        self.assertEqual(prof_num["empty_ratio"], 0.0)

        s_text = pd.Series(["Basalt", "Andesite", "Rhyolite", "Dacite"])
        prof_txt = table_postprocess.compute_column_profile(s_text)
        self.assertLess(prof_txt["num_ratio"], 0.10)
        self.assertGreater(prof_txt["avg_len"], 4.0)

    def test_deepdoc_mineru_align_columns_monotonic_order(self):
        """测试单调动态规划对齐：严格保持列左右序，自动对齐错位/省略列。"""
        base_df = pd.DataFrame({
            "Sample": ["S1", "S2"],
            "SiO2": [52.1, 53.0],
            "TiO2": [0.8, 0.9],
            "Al2O3": [15.2, 14.8],
            "Fe2O3": [8.5, 9.1],
        })
        base_cols = list(base_df.columns)

        # 续表片段丢失了 TiO2 列 (仅有 Sample, SiO2, Al2O3, Fe2O3)，且列名为 generic
        cont_df = pd.DataFrame([
            ["S3", "54.2", "15.0", "8.9"],
            ["S4", "51.8", "14.9", "9.3"]
        ], columns=["Unnamed: 0", "Unnamed: 1", "Unnamed: 2", "Unnamed: 3"])

        aligned = table_postprocess.deepdoc_mineru_align_columns(
            cont_df, base_cols, base_df=base_df
        )

        # 验证对齐后拥有完整的 5 列
        self.assertEqual(list(aligned.columns), base_cols)
        # Sample 对齐
        self.assertEqual(aligned["Sample"].tolist(), ["S3", "S4"])
        # SiO2 对齐
        self.assertEqual(aligned["SiO2"].tolist(), ["54.2", "51.8"])
        # 缺失的 TiO2 自动安全填空
        self.assertEqual(aligned["TiO2"].tolist(), ["", ""])
        # 后续 Al2O3, Fe2O3 依然正确保序对齐
        self.assertEqual(aligned["Al2O3"].tolist(), ["15.0", "14.9"])
        self.assertEqual(aligned["Fe2O3"].tolist(), ["8.9", "9.3"])

    def test_combine_df_group_with_projection(self):
        """测试 combine_df_group 在多页学术长表拼接时完整保留数据行并对齐列投影。"""
        df1 = pd.DataFrame({
            "ID": ["A1", "A2"],
            "Value1": [10.5, 20.3],
            "Value2": [100.0, 200.0],
        })
        df1.attrs = {"page_idx": 0, "label": "Table 1"}

        # 第 2 页续表：包含重复表头行，第 2 行起为真实数据
        df2 = pd.DataFrame({
            "ID": ["Table 1 (continued)", "A3", "A4"],
            "Value1": ["Value1", "30.1", "40.5"],
            "Value2": ["Value2", "300.0", "400.0"],
        })
        df2.attrs = {"page_idx": 1, "label": "Table 1 (continued)", "is_continuation": True}

        merged = table_postprocess.combine_df_group([df1, df2])
        self.assertIsNotNone(merged)
        # 重复表头行被剥离，总行数应为 4
        self.assertEqual(len(merged), 4)
        self.assertEqual(merged["ID"].tolist(), ["A1", "A2", "A3", "A4"])

    def test_deepdoc_mineru_align_columns_more_current_columns_than_target(self):
        """测试单调动态规划在当前续表列数多于目标表 (n_curr > n_target) 时的正确保序跳过与对齐。"""
        df = pd.DataFrame({
            "A": ["1", "2"],
            "Noise1": ["x", "y"],
            "C": ["5", "6"],
            "Noise2": ["foo", "bar"],
            "E": ["9", "10"],
        })
        target_cols = ["A", "C", "E"]
        aligned = table_postprocess.deepdoc_mineru_align_columns(df, target_cols)
        self.assertEqual(list(aligned.columns), target_cols)
        self.assertEqual(aligned["A"].tolist(), ["1", "2"])
        self.assertEqual(aligned["C"].tolist(), ["5", "6"])
        self.assertEqual(aligned["E"].tolist(), ["9", "10"])

    def test_combine_df_group_with_data_in_columns(self):
        """测试当续表首行数据被解析为 columns 时，自适应列投影能正确对齐并挽回全部数据行。"""
        df1 = pd.DataFrame({
            "Sample": ["S1", "S2"],
            "SiO2": ["52.1", "53.0"],
            "TiO2": ["0.8", "0.9"],
            "Al2O3": ["15.2", "14.8"],
        })
        df1.attrs = {"page_idx": 0, "label": "Table 1"}

        # df2 的 columns 实质上是数据首行 (S3, 54.2, 0.7, 15.0)，第二行为 S4
        df2 = pd.DataFrame([
            ["S4", "51.8", "0.85", "14.9"]
        ], columns=["S3", "54.2", "0.7", "15.0"])
        df2.attrs = {"page_idx": 1, "label": "Table 1 (continued)", "is_continuation": True}

        merged = table_postprocess.combine_df_group([df1, df2])
        self.assertIsNotNone(merged)
        self.assertEqual(len(merged), 4)
        self.assertEqual(merged["Sample"].tolist(), ["S1", "S2", "S3", "S4"])
        self.assertEqual(merged["SiO2"].tolist(), ["52.1", "53.0", "54.2", "51.8"])


if __name__ == "__main__":
    unittest.main()
