import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common
import doclayout_yolo_detector
import extract_zotero_table
import ocr_client
import pdf_page_filter
import pdf_table_extractor as pte
import pdf_tables
import table_postprocess as tp
import table_validator as tv


class TestFrameworkAndOmissionAuditFixes(unittest.TestCase):
    """针对框架编排、表格漏检/误杀/吞噬及开源组件集成修复的专项测试。"""

    def test_online_complete_short_circuits_local_pdf_pipeline(self):
        """验证在线提取 is_complete=True 时直接短路本地 PDF 管线且只写盘一次。"""
        import fitz

        df_online = pd.DataFrame(
            {
                "Sample": ["S1", "S2", "S3", "S4", "S5"],
                "SiO2": ["65.2", "66.1", "67.0", "64.8", "68.1"],
                "Al2O3": ["14.2", "13.9", "14.5", "15.0", "13.7"],
            }
        )
        df_online.attrs["label"] = "Table 1"
        df_online.attrs["table_title"] = "Table 1 Major elements"

        with tempfile.TemporaryDirectory() as tmpdir:
            dummy_pdf = os.path.join(tmpdir, "paper.pdf")
            doc = fitz.open()
            p = doc.new_page()
            p.insert_text(
                (50, 100),
                "Modern geological study with sufficient searchable body text on this page. Table 1 Major elements.",
            )
            doc.save(dummy_pdf)
            doc.close()

            args = MagicMock()
            args.output = tmpdir
            args.single_file = False
            args.online_only = False
            args.pdf_only = False
            args.enable_llm = False
            args.table_idx = "all"
            args.db_path = None
            args.firecrawl_key = None
            args.cnki_strategy = "auto"
            args.headed = False
            args.user_data_dir = None
            args.sequential = False
            args.headers = None
            args.skip_supplementary = False

            mock_online = MagicMock()
            mock_online.extract_tables_online.return_value = ([df_online], "Test Paper")

            with patch.object(extract_zotero_table, "online", mock_online), patch.object(
                extract_zotero_table, "is_pre_2020_chinese_paper", return_value=False
            ), patch.object(
                extract_zotero_table, "scan_pdf_table_expected_labels", return_value=({"Table 1"}, 1)
            ), patch.object(
                extract_zotero_table, "save_tables_to_excel", return_value=True
            ) as mock_save, patch.object(
                extract_zotero_table, "_trigger_zotero_sync"
            ), patch.object(
                extract_zotero_table, "extract_tables_from_pdf"
            ) as mock_local_pdf:
                ok = extract_zotero_table._do_process_single_pdf(
                    pdf_path=dummy_pdf,
                    orig_pdf_path=dummy_pdf,
                    args=args,
                )
                self.assertTrue(ok)
                self.assertEqual(mock_save.call_count, 1)
                mock_local_pdf.assert_not_called()

    def test_scan_pdf_table_declarations_toc_and_appendix_safety(self):
        """验证前5页含 'element contents' 不被误判为目录页跳过，且文末含 References 同页的附表不漏扫。"""
        import fitz

        doc = fitz.open()
        # Page 1: contains "element contents" and Table 1
        p0 = doc.new_page()
        p0.insert_text((50, 60), "Trace element contents in hydrothermal pyrite from the deposit.")
        p0.insert_text((50, 120), "Table 1 Trace element contents of pyrite (ppm)")
        for i in range(4):
            doc.new_page()
        # Last page: contains Acknowledgements & References followed by Appendix Table A1
        p_last = doc.new_page()
        p_last.insert_text((50, 60), "Acknowledgements: We thank the reviewers. References:")
        p_last.insert_text((50, 200), "Table A1 Supplementary isotope data")

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            pdf_path = tf.name
        try:
            doc.save(pdf_path)
            doc.close()
            decls = tv.scan_pdf_table_declarations(pdf_path)
            self.assertIn(0, decls)
            self.assertIn(5, decls)
        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    def test_verify_table_numeric_integrity_duplicate_columns_and_header_nums(self):
        """验证存在重名列及表头含数值时 verify_table_numeric_integrity 不崩溃且召回率计算准确。"""
        import fitz

        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((50, 100), "100 200 12.5 34.8 56.1 78.9")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            pdf_path = tf.name
        try:
            doc.save(pdf_path)
            doc.close()
            df = pd.DataFrame(
                [["12.5", "34.8"], ["56.1", "78.9"]],
                columns=["100", "200"],
            )
            # Duplicate column names
            df.columns = ["100", "100"]
            is_valid, info = tv.verify_table_numeric_integrity(
                df=df, pdf_path=pdf_path, page_idx=0, bbox=[40, 80, 400, 150]
            )
            self.assertTrue(is_valid)
            self.assertGreater(info["match_ratio"], 0.7)
        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    def test_text_alignment_section_header_above_table_and_multitable(self):
        """验证表格上方有中文小节标题时不发生区间倒挂，且同页双表各自独立提取。"""
        import fitz

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        # Section heading ABOVE Table 1
        page.insert_text((50, 70), "4.2 矿床地球化学特征")
        page.insert_text((50, 110), "Table 1 Major element composition (%)")
        page.insert_text((50, 135), "Sample")
        page.insert_text((150, 135), "SiO2")
        page.insert_text((250, 135), "Al2O3")
        page.insert_text((350, 135), "Fe2O3")
        page.insert_text((50, 155), "ZK01")
        page.insert_text((150, 155), "65.12")
        page.insert_text((250, 155), "14.23")
        page.insert_text((350, 155), "3.45")
        page.insert_text((50, 175), "ZK02")
        page.insert_text((150, 175), "67.80")
        page.insert_text((250, 175), "13.95")
        page.insert_text((350, 175), "2.88")

        # Second table on the same page
        page.insert_text((50, 300), "Table 2 Trace element composition (ppm)")
        page.insert_text((50, 325), "Sample")
        page.insert_text((150, 325), "Rb")
        page.insert_text((250, 325), "Sr")
        page.insert_text((350, 325), "Ba")
        page.insert_text((50, 345), "ZK01")
        page.insert_text((150, 345), "120")
        page.insert_text((250, 345), "340")
        page.insert_text((350, 345), "560")
        page.insert_text((50, 365), "ZK02")
        page.insert_text((150, 365), "135")
        page.insert_text((250, 365), "310")
        page.insert_text((350, 365), "610")

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            pdf_path = tf.name
        try:
            doc.save(pdf_path)
            doc.close()
            with fitz.open(pdf_path) as open_doc:
                results = pte._extract_page_via_text_alignment(open_doc, page_idx=0, pdf_path=pdf_path)
            self.assertGreaterEqual(len(results), 2)
            labels = [r["df"].attrs.get("label") for r in results]
            self.assertIn("Table 1", labels)
            self.assertIn("Table 2", labels)
        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    def test_low_quality_and_metadata_guards_preserve_valid_scientific_tables(self):
        """验证 is_table_low_quality 和 is_metadata_table 不误杀含文献引用、Fig. 提及、浮点首列及单字分类列的科学表。"""
        # 1. Float in col 0 + Chinese in col 1 (must not match section heading regex)
        df_float_cn = pd.DataFrame({
            "Depth(m)": ["12.5", "24.0", "35.5"],
            "Lithology": ["花岗斑岩", "夕卡岩", "大理岩"],
            "Cu(%)": ["0.85", "1.42", "0.31"],
        })
        self.assertFalse(common.is_table_low_quality(df_float_cn))

        # 2. Geochemical table mentioning 'wt% SiO2' and 'et al., 2020'
        df_geochem = pd.DataFrame({
            "Deposit": ["Dexing", "Yulong", "Qulong", "Duolong"],
            "wt% SiO2": ["62.1–65.4", "64.0±1.2", "<68.0", "63.5"],
            "Age (Ma)": ["171.0±1.5", "41.2±0.8", "16.0±0.5", "118.0±1.1"],
            "Reference": [
                "Wang et al., 2020 (see Fig. 2)",
                "Li et al., 2019",
                "Hou et al., 2021",
                "Tang et al., 2018",
            ],
        })
        df_geochem.attrs["label"] = "Table 1"
        self.assertFalse(common.is_table_low_quality(df_geochem))
        self.assertFalse(common.is_metadata_table(df_geochem))

    def test_postprocess_preserve_indexed_rows_short_elements_and_pure_data_tokens(self):
        """验证：
        1. 序号 1..9 开头的正式数据行不被 NOTE_PREFIX_PATTERN 剥离为尾注；
        2. smart_flatten_headers 不吞掉短元素符号 S, C, O, Si, Al；
        3. is_pure_data_value 对大写 ND, BDL, NA, Tr 返回 True，对化学元素 Na, Nd 返回 False；
        4. combine_df_group 跨页列数差 > 6 时仍保留合并而非丢弃。
        """
        # 1. Indexed data rows 1..5 with capital/Chinese sample names in col 1
        df_indexed = pd.DataFrame(
            [
                ["1", "Granite", "68.5", "14.2"],
                ["2", "Diorite", "58.1", "16.4"],
                ["3", "Skarn", "45.0", "12.1"],
                ["Note: Analytical uncertainty is < 2%.", "", "", ""],
            ],
            columns=["No.", "Rock Type", "SiO2", "Al2O3"],
        )
        cleaned = tp.postprocess_dataframe(df_indexed.copy())
        self.assertEqual(len(cleaned), 3)
        self.assertEqual([str(v) for v in cleaned.iloc[:, 0]], ["1", "2", "3"])
        self.assertTrue(any("Analytical uncertainty" in fn for fn in cleaned.attrs.get("footnotes", [])))

        # 2. smart_flatten_headers preserves S, C, O, Si, Al under "Content (wt%)"
        header_rows = [
            ["Content (wt%)", "Content (wt%)", "Content (wt%)", "Composition", "Composition"],
            ["S", "C", "O", "Si", "Al"],
        ]
        flat_cols = tp.smart_flatten_headers(header_rows)
        cols_str = " | ".join(str(c) for c in flat_cols)
        for elem in ("S", "C", "O", "Si", "Al"):
            self.assertRegex(cols_str, rf"(?:^|[_\s|]){elem}(?:$|[_\s|])")

        # 3. is_pure_data_value
        for tok in ("ND", "BDL", "NA", "Tr", "LOQ", "LOD"):
            self.assertTrue(tp.is_pure_data_value(tok), f"{tok} should be recognized as pure data value")
        for elem in ("Na", "Nd", "Fe", "SiO2"):
            self.assertFalse(tp.is_pure_data_value(elem), f"{elem} should not be treated as missing-data token")

        # 4. combine_df_group with > 6 column difference
        df_p1 = pd.DataFrame([[str(i) for i in range(10)]], columns=[f"C{i}" for i in range(10)])
        df_p2 = pd.DataFrame([[str(i) for i in range(2)]], columns=["C0", "C1"])
        combined = tp.combine_df_group([df_p1, df_p2])
        self.assertEqual(len(combined), 2)

    def test_mineru_structured_priority_and_page_idx_none_when_unknown(self):
        """验证 MinerU parse_mineru_structured_result 优先解析结构化 ZIP/JSON 而非退化 Markdown，且未知页码不硬编码为 0。"""
        bio = io.BytesIO()
        content_list = [
            {
                "type": "table",
                "table_body": "<table><tr><th>Sample</th><th>Au</th></tr><tr><td>S1</td><td>12.3</td></tr></table>",
                "bbox": [100, 120, 450, 300],
                "table_caption": "Table 2 Gold concentrations",
                "table_footnote": "Note: Au in ppm.",
            }
        ]
        with zipfile.ZipFile(bio, "w") as zf:
            zf.writestr("content_list.json", json.dumps(content_list))
        zip_bytes = bio.getvalue()

        # When both markdown and content_list are present in extract_result, content_list wins and preserves bbox & footnote
        payload = {
            "extract_result": [
                {
                    "markdown": "| Wrong | Header |\n| --- | --- |\n| X | Y |",
                    "content_list": content_list,
                }
            ]
        }
        client = ocr_client.MinerUClient(api_key="dummy")
        dfs = client.parse_mineru_structured_result(payload)
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].attrs.get("bbox"), [100, 120, 450, 300])
        self.assertEqual(dfs[0].attrs.get("table_footnote"), "Note: Au in ppm.")
        # Since neither page_idx nor page_no was in content_list, page_idx must be None (not 0)
        self.assertIsNone(dfs[0].attrs.get("page_idx"))

    def test_pp_doclayout_v3_polygon_to_tight_bbox_in_detect(self):
        """验证 PPDocLayoutV3Detector.detect 在输出包含多边形顶点时自动调用 polygon_to_tight_bbox 紧致化边界框。"""
        import numpy as np
        from PIL import Image

        det = doclayout_yolo_detector.PPDocLayoutV3Detector.__new__(doclayout_yolo_detector.PPDocLayoutV3Detector)
        det._available = True
        mock_sess = MagicMock()
        mock_inp = MagicMock()
        mock_inp.name = "image"
        mock_inp.shape = [1, 3, 800, 800]
        mock_sess.get_inputs.return_value = [mock_inp]

        # 6 base values (cls_id=21 'table', score=0.95, loose box 10,10,700,700) + 4 polygon vertices (50,60)-(400,60)-(400,300)-(50,300)
        row = np.array(
            [[21, 0.95, 10.0, 10.0, 700.0, 700.0, 50.0, 60.0, 400.0, 60.0, 400.0, 300.0, 50.0, 300.0]],
            dtype=np.float32,
        )
        mock_sess.run.return_value = [row]
        det._session = mock_sess

        img = Image.new("RGB", (800, 800))
        res = det.detect(img, conf_threshold=0.5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["label"], "table")
        self.assertEqual(res[0]["bbox"], [50.0, 60.0, 400.0, 300.0])

    def test_crop_to_dataframe_rapidtable_docling_and_ocr_fallback(self):
        """验证 crop_to_dataframe 真实接入 get_rapid_table_extractor / extract_via_docling / extract_tables_with_fallback，且在字体编码损坏时强制走视觉/OCR 通道。"""
        import fitz
        import rapid_table_extractor as rte

        # 确认真实符号存在（严禁 create=True 掩耳盗铃）
        self.assertTrue(hasattr(rte, "get_rapid_table_extractor"))
        self.assertTrue(hasattr(rte.RapidTableExtractor, "is_available"))
        self.assertTrue(hasattr(ocr_client, "extract_tables_with_fallback"))

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            pdf_path = tf.name
        try:
            doc.save(pdf_path)
            doc.close()

            crop_info = {
                "page_index": 0,
                "table_index": 0,
                "crop_bbox": [50, 50, 400, 300],
                "needs_ocr": True,
                "ocr_reason": "empty_or_image_table",
            }

            # 1) RapidTable fallback
            fake_rt = MagicMock()
            fake_rt.is_available.return_value = True
            fake_rt.extract_table_from_image.return_value = pd.DataFrame(
                [["ZK01", "65.2"], ["ZK02", "67.1"]], columns=["Sample", "SiO2"]
            )
            with patch.object(rte, "get_rapid_table_extractor", return_value=fake_rt):
                df_rt, _, _ = pdf_tables.crop_to_dataframe(pdf_path, dict(crop_info))
            self.assertIsNotNone(df_rt)
            self.assertEqual(df_rt.attrs.get("extractor"), "rapid_table_crop")

            # 2) Docling fallback when RapidTable is unavailable
            fake_rt_off = MagicMock()
            fake_rt_off.is_available.return_value = False
            docling_df = pd.DataFrame([["D1", "51.0"]], columns=["Sample", "FeO"])
            with patch.object(rte, "get_rapid_table_extractor", return_value=fake_rt_off), patch.object(
                ocr_client,
                "extract_via_docling",
                return_value=[{"df": docling_df, "page_idx": 0, "table_idx": 0, "bbox": [20, 20, 200, 150]}],
            ):
                df_doc, _, _ = pdf_tables.crop_to_dataframe(pdf_path, dict(crop_info))
            self.assertIsNotNone(df_doc)
            self.assertEqual(df_doc.attrs.get("extractor"), "docling_crop_fallback")

            # 3) OCR/VLM fallback when both RapidTable and Docling return empty, even if vector text is corrupted
            corrupted_crop = dict(crop_info)
            corrupted_crop["ocr_reason"] = "encoding_corrupted"
            with patch.object(rte, "get_rapid_table_extractor", return_value=fake_rt_off), patch.object(
                ocr_client, "extract_via_docling", return_value=[]
            ), patch.object(
                ocr_client,
                "extract_tables_with_fallback",
                return_value="| Sample | Au |\n| --- | --- |\n| S1 | 12.4 |\n| S2 | 15.8 |",
            ):
                df_ocr, _, _ = pdf_tables.crop_to_dataframe(pdf_path, corrupted_crop)
            self.assertIsNotNone(df_ocr)
            self.assertEqual(df_ocr.attrs.get("extractor"), "ocr_crop_fallback")
        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    def test_export_crops_to_excel_skip_labels_post_extraction(self):
        """验证 export_crops_to_excel 在 crop 初始无 label、仅在 crop_to_dataframe 提取后获得表题/表号时仍能按 skip_labels 去重。"""
        df1 = pd.DataFrame({"A": ["1", "2"], "B": ["3", "4"]})
        df1.attrs["table_title"] = "Table 1 Major elements"
        df2 = pd.DataFrame({"A": ["5", "6"], "B": ["7", "8"]})
        df2.attrs["table_title"] = "Table 2 Trace elements"

        crops = [
            {"page_index": 0, "table_index": 0, "crop_bbox": [10, 10, 100, 100]},
            {"page_index": 0, "table_index": 1, "crop_bbox": [10, 120, 100, 220]},
        ]
        side_effects = [(df1, "Table 1 Major elements", ""), (df2, "Table 2 Trace elements", "")]

        with patch.object(pdf_tables, "crop_to_dataframe", side_effect=side_effects), patch.object(
            pdf_tables, "save_tables_to_excel", return_value=True
        ) as mock_save:
            ok = pdf_tables.export_crops_to_excel(
                "dummy.pdf",
                "out.xlsx",
                crops=crops,
                existing_dfs=[df1],
                skip_labels={"Table_1"},
            )
            self.assertTrue(ok)
            saved_dfs = mock_save.call_args[0][0]
            # df1 (existing) + df2 (crop#2); crop#1 (Table 1) must be skipped
            self.assertEqual(len(saved_dfs), 2)

    def test_verify_table_numeric_integrity_text_only_table_with_default_columns(self):
        """验证默认整数列名 (0, 1, 2) 的纯文本表不会因列名索引数字被误判为 no_pdf_numbers_in_bbox。"""
        import fitz

        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((50, 100), "Granite Quartz Biotite Diorite Hornblende Plagioclase")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            pdf_path = tf.name
        try:
            doc.save(pdf_path)
            doc.close()
            df = pd.DataFrame([["Granite", "Quartz", "Biotite"], ["Diorite", "Hornblende", "Plagioclase"]])
            is_valid, info = tv.verify_table_numeric_integrity(
                df=df, pdf_path=pdf_path, page_idx=0, bbox=[40, 80, 450, 150]
            )
            self.assertTrue(is_valid)
            self.assertEqual(info.get("reason"), "text_only_table")
        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    def test_postprocess_parenthesized_index_and_multicolumn_text_rows(self):
        """验证首列为 (1)、1. 等序号的多列纯文本数据行不会被误剥离为表尾注。"""
        df = pd.DataFrame(
            [
                ["(1)", "Early stage", "Quartz + Pyrite", "Disseminated"],
                ["(2)", "Main ore stage", "Chalcopyrite + Bornite", "Veinlet-disseminated"],
                ["(3)", "Late stage", "Calcite + Chlorite", "Barren veinlets"],
            ],
            columns=["Stage", "Name", "Assemblage", "Texture"],
        )
        cleaned = tp.postprocess_dataframe(df.copy())
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned.attrs.get("footnotes", []), [])

    def test_doc_orientation_detector_selects_hardware_onnx_providers(self):
        """验证 DocOrientationDetector 按硬件可用性优先选择 CoreML/CUDA 而非硬编码 CPUExecutionProvider。"""
        import doc_orientation_detector as dod

        mock_ort = MagicMock()
        mock_ort.get_available_providers.return_value = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_sess = MagicMock()
        mock_inp = MagicMock()
        mock_inp.name = "x"
        mock_out = MagicMock()
        mock_out.name = "y"
        mock_sess.get_inputs.return_value = [mock_inp]
        mock_sess.get_outputs.return_value = [mock_out]
        mock_ort.InferenceSession.return_value = mock_sess

        with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as tf:
            onnx_path = tf.name
        try:
            with patch.object(dod, "ort", mock_ort), patch.object(
                dod, "ensure_model_file", return_value=onnx_path
            ):
                detector = dod.DocOrientationDetector(model_path=onnx_path)
                self.assertTrue(detector.is_available())
                _, kwargs = mock_ort.InferenceSession.call_args
                self.assertEqual(kwargs.get("providers"), ["CoreMLExecutionProvider", "CPUExecutionProvider"])
        finally:
            if os.path.exists(onnx_path):
                os.remove(onnx_path)

    def test_mineru_backend_default_structured_without_explicit_kwarg(self):
        """验证 MinerUBackend.extract 默认走 call_mineru_structured_api 且无需显式传 structured=True。"""
        backend = ocr_client.MinerUBackend()
        expected_df = pd.DataFrame({"Sample": ["S1"], "Au": ["1.2"]})
        with patch.object(ocr_client, "call_mineru_structured_api", return_value=[expected_df]) as mock_struct:
            res = backend.extract("dummy.pdf", pages=[0])
            self.assertEqual(len(res), 1)
            mock_struct.assert_called_once()


if __name__ == "__main__":
    unittest.main()
