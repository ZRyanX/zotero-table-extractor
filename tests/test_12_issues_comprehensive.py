import os
import io
import re
import tempfile
import unittest
import zipfile
import json
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

import scripts.common as common
import scripts.ocr_client as ocr_client
import scripts.table_postprocess as table_postprocess
import scripts.table_validator as table_validator
import scripts.pdf_table_extractor as pte
import scripts.extract_zotero_table as extract_zotero_table
import scripts.excel_export as excel_export
import benchmarks.evaluator as evaluator
import benchmarks.pipelines as pipelines


class TestTwelveIssuesComprehensive(unittest.TestCase):
    """
    全量回归核验：系统化覆盖 12 项生产缺陷与主抽取架构升级。
    """

    # -------------------------------------------------------------------------
    # Issue 1: OCR-Hub Parameter Error & Backend Isolation
    # -------------------------------------------------------------------------
    def test_issue1_ocr_hub_parameter_and_isolation(self):
        """测试 MinerUBackend.extract 与 API 的 pages/page_ranges 映射及后端隔离。"""
        backend = ocr_client.MinerUBackend()
        with patch.object(ocr_client, "call_mineru_api", return_value="TableMD") as mock_api:
            # pages 自动映射为 page_ranges
            res = backend.extract("dummy.pdf", pages=[0, 1])
            self.assertEqual(res, "TableMD")
            mock_api.assert_called_once()
            _, kwargs = mock_api.call_args
            self.assertEqual(kwargs.get("page_ranges"), [0, 1])

        with patch.object(ocr_client, "call_mineru_structured_api", return_value=[pd.DataFrame({"A": [1]})]) as mock_s_api:
            res_s = backend.extract("dummy.pdf", structured=True, pages="1-3")
            self.assertEqual(len(res_s), 1)
            mock_s_api.assert_called_once()
            _, kwargs_s = mock_s_api.call_args
            self.assertEqual(kwargs_s.get("page_ranges"), "1-3")

        # 测试 call_mineru_api 与 call_mineru_structured_api 直接接收 pages 参数
        with patch.object(ocr_client.MinerUClient, "submit_task", return_value="task-123") as mock_submit, \
             patch.object(ocr_client.MinerUClient, "poll_task", return_value={"success": True, "data": {}}):
            ocr_client.call_mineru_api("dummy.pdf", pages=[2, 3])
            mock_submit.assert_called_with("dummy.pdf", page_ranges=[2, 3])

        # 测试 pdf_table_extractor.py 中 OCR-Hub 循环单后端异常隔离 (TeleOCR 崩溃不影响 MinerU/RapidTable)
        registry = ocr_client.get_ocr_registry()
        mock_tele = MagicMock()
        mock_tele.probe_health.return_value = {"status": "healthy"}
        mock_tele.extract.side_effect = RuntimeError("TeleOCR GPU Out of Memory")

        mock_rapid = MagicMock()
        mock_rapid.probe_health.return_value = {"status": "healthy"}
        mock_rapid.extract.return_value = [
            {"df": pd.DataFrame({"X": [1, 2]}), "page_idx": 0, "table_idx": 0, "bbox": None, "extractor": "rapid_table"}
        ]

        def get_backend_mock(name):
            if name == "teleocr":
                return mock_tele
            if name == "mineru":
                return None
            if name == "rapid_table":
                return mock_rapid
            return None

        with patch.object(registry, "get_backend", side_effect=get_backend_mock):
            with patch("scripts.ocr_client.run_ppstructure_vlm_pipeline", return_value=""), \
                 patch("scripts.ocr_client.get_ocr_registry", return_value=registry):
                res_hub = pte.extract_via_ocr("dummy.pdf", pages=[0])
                # TeleOCR 抛出异常后不中断循环，成功进入 rapid_table
                self.assertTrue(len(res_hub) >= 1)
                self.assertEqual(res_hub[0]["extractor"], "rapid_table")

    # -------------------------------------------------------------------------
    # Issue 2: Incomplete Online Table Blocking Full PDF/MinerU Tables
    # -------------------------------------------------------------------------
    def test_issue2_incomplete_online_table_overwrite(self):
        """测试在线截断/预览表格不误设 is_complete，且合并时由本地完整表覆盖。"""
        # 1. 在线表格仅有 1 行或空元胞，不得设为 is_complete
        sparse_df = pd.DataFrame({"A": [""]})
        online_dfs = [sparse_df]
        # 校验 _merge_online_and_pdf_tables
        full_pdf_df = pd.DataFrame({
            "A": [f"Sample_{i}" for i in range(25)],
            "B": [float(i) for i in range(25)],
            "C": [float(i * 2) for i in range(25)],
        })
        full_pdf_df.attrs["label"] = "Table 1"

        truncated_online_df = pd.DataFrame({
            "A": ["Sample_0", "Sample_1"],
            "B": [0.0, 1.0],
            "C": [0.0, 2.0],
        })
        truncated_online_df.attrs["label"] = "Table 1"

        merged = extract_zotero_table._merge_online_and_pdf_tables([truncated_online_df], [full_pdf_df])
        self.assertEqual(len(merged), 1)
        # 本地完整版具有 25 行，成功覆盖仅有 2 行的在线预览截断版
        self.assertEqual(len(merged[0]), 25)

        # 2. 若本地表为新增表（不同标签），追加合并
        other_pdf_df = pd.DataFrame({"Col": [1, 2, 3]})
        other_pdf_df.attrs["label"] = "Table 2"
        merged_two = extract_zotero_table._merge_online_and_pdf_tables([truncated_online_df], [other_pdf_df])
        self.assertEqual(len(merged_two), 2)

    # -------------------------------------------------------------------------
    # Issue 3: NA, BDL, Na Swallowed into Footnotes
    # -------------------------------------------------------------------------
    def test_issue3_na_bdl_not_swallowed_into_footnotes(self):
        """测试 NA, BDL, 元素 Na 作为数据元胞时不被误判为学术表格尾注剥离。"""
        # STAT_NOTE_PATTERN 不匹配裸单词 na / bdl
        self.assertIsNone(table_postprocess.STAT_NOTE_PATTERN.search("Sample 1 12.5 Na 3.4"))
        self.assertIsNone(table_postprocess.STAT_NOTE_PATTERN.search("10.5 NA BDL 0.05"))
        # 但能精准匹配说明性语法
        self.assertIsNotNone(table_postprocess.STAT_NOTE_PATTERN.search("NA: not available"))
        self.assertIsNotNone(table_postprocess.STAT_NOTE_PATTERN.search("BDL = below detection limit"))
        self.assertIsNotNone(table_postprocess.STAT_NOTE_PATTERN.search("Values below detection limit"))

        # 测试包含 Na / NA / BDL 的多列数据行保留完整
        data = {
            "Sample": ["S1", "S2", "S3"],
            "Na": ["1.25", "2.30", "1.85"],
            "Fe": ["NA", "0.45", "BDL"],
            "Mg": ["5.6", "6.2", "4.8"],
        }
        df = pd.DataFrame(data)
        cleaned = table_postprocess.separate_table_footnotes(df)
        self.assertEqual(len(cleaned), 3)
        self.assertIn("Na", list(cleaned.columns))
        self.assertEqual(cleaned.iloc[2]["Fe"], "BDL")

    # -------------------------------------------------------------------------
    # Issue 4: Header Deduplication Deleting Legitimate Data Rows
    # -------------------------------------------------------------------------
    def test_issue4_header_deduplication_numeric_immunity(self):
        """测试数值免疫机制保护合法数据行不被误当作重复表头删除。"""
        # 行内包含合法数值或数值占比 >= 25%，拥有数值豁免权
        df = pd.DataFrame({
            "Sample": ["Header1", "S1", "S2"],
            "Concentration": ["Concentration", "12.5", "14.8"],
            "Ratio": ["Ratio", "0.85", "0.92"],
        })
        cleaned = table_postprocess.strip_embedded_duplicate_headers(df)
        # S1, S2 绝不能被删除
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned.iloc[1]["Sample"], "S1")

        # 真正纯文本重复表头行且匹配率 >= 75% 才被剥离
        df_with_repeat = pd.DataFrame({
            "Sample": ["S0", "Sample", "S1"],
            "Concentration": ["10.0", "Concentration", "12.0"],
            "Ratio": ["0.5", "Ratio", "0.6"],
        })
        cleaned_repeat = table_postprocess.strip_embedded_duplicate_headers(df_with_repeat)
        self.assertEqual(len(cleaned_repeat), 2)
        self.assertEqual(list(cleaned_repeat["Sample"]), ["S0", "S1"])

    # -------------------------------------------------------------------------
    # Issue 5: Continuation Merge Data Losses (Cross-page & Same-page)
    # -------------------------------------------------------------------------
    def test_issue5_continuation_merge_no_numeric_loss(self):
        """测试跨页不盲目拼接数值，同页续表基于 IoU < 0.1 纵向追加。"""
        # 1. 跨页断裂行：若两行各自包含独立合法数值 (12.5 和 13.5)，绝不拼合为 "12.5 13.5" 并丢弃行 2
        df_prev = pd.DataFrame({
            "Sample": ["S1"],
            "Val1": ["12.5"],
            "Val2": ["100.0"]
        })
        df_next = pd.DataFrame({
            "Sample": [""],
            "Val1": ["13.5"],
            "Val2": ["102.0"]
        })
        p_res, n_res = table_postprocess.merge_cross_page_split_cells(df_prev, df_next)
        # 独立数值冲突，两行必须各自完整保留
        self.assertEqual(len(p_res), 1)
        self.assertEqual(len(n_res), 1)
        self.assertEqual(p_res.iloc[0]["Val1"], "12.5")
        self.assertEqual(n_res.iloc[0]["Val1"], "13.5")

        # 2. 同页续表片段：标签相同但显式带有 (continued) 且空间分离 (IoU < 0.1)
        df1 = pd.DataFrame({"A": ["Row1", "Row2"], "B": ["1.0", "2.0"]})
        df1.attrs = {"label": "Table 1", "page_idx": 0, "bbox": [50, 50, 500, 250]}

        df2 = pd.DataFrame({"A": ["Row3", "Row4"], "B": ["3.0", "4.0"]})
        df2.attrs = {"label": "Table 1 (continued)", "page_idx": 0, "bbox": [50, 300, 500, 500], "is_continuation": True}

        merged_same_page = table_postprocess.merge_continuation_tables([df1, df2])
        self.assertEqual(len(merged_same_page), 1)
        # 纵向追加，保留全部 4 行
        self.assertEqual(len(merged_same_page[0]), 4)
        self.assertEqual(list(merged_same_page[0]["A"]), ["Row1", "Row2", "Row3", "Row4"])

    # -------------------------------------------------------------------------
    # Issue 6: Destructive Cleaning Decoupling & 'None' Cell Preservation
    # -------------------------------------------------------------------------
    def test_issue6_decouple_chemical_guessing_and_preserve_none_string(self):
        """测试解耦化学式竞猜（不把 CQ 转为 CO2，Tin 转为 Tm）且保留合法的 'None' 字符串单元格。"""
        # 默认模式下不破坏合法缩写与英文单词
        raw_cq = "Sample CQ-01 with Tin coating"
        cleaned_cq = common.clean_latex_and_ocr(raw_cq)
        self.assertIn("CQ", cleaned_cq)
        self.assertIn("Tin", cleaned_cq)
        self.assertNotIn("CO2-01", cleaned_cq)
        self.assertNotIn("Tm coating", cleaned_cq)

        # 单元格字符串 "None" 绝不被抹除为空白
        df_with_none = pd.DataFrame({
            "Sample": ["S1", "S2"],
            "Alteration": ["None", "Silicification"],
            "Grade": [None, "High"]
        })
        cleaned_df = table_postprocess.clean_extracted_table(df_with_none)
        self.assertEqual(cleaned_df.iloc[0]["Alteration"], "None")
        # Python 原生 None 转换为空白
        self.assertEqual(cleaned_df.iloc[0]["Grade"], "")

    # -------------------------------------------------------------------------
    # Issue 7: Long Numeric Sample IDs Collapsing in Excel
    # -------------------------------------------------------------------------
    def test_issue7_long_numeric_sample_ids_preserved(self):
        """测试超过 15 位的超长数值样号保留为字符串，杜绝 IEEE 754 截断。"""
        long_id = "123456789012345678"  # 18 digits
        res_num = table_postprocess.try_numeric(long_id)
        # 应保留为 str，不得转为 float/int 导致精度损失
        self.assertIsInstance(res_num, str)
        self.assertEqual(res_num, long_id)

        # escape_formula 中加单引号保护
        escaped = table_postprocess.escape_formula("=" + long_id)
        self.assertTrue(escaped.startswith("'"))

        # excel_export autofit_excel_columns 写入 text cell ('s')
        fd, tmp_xlsx = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        try:
            df_long = pd.DataFrame({"SampleID": [long_id], "Value": [12.5]})
            with pd.ExcelWriter(tmp_xlsx) as writer:
                df_long.to_excel(writer, index=False)
            excel_export.autofit_excel_columns(tmp_xlsx)

            from openpyxl import load_workbook
            wb = load_workbook(tmp_xlsx)
            ws = wb.active
            cell = ws.cell(row=2, column=1)
            self.assertEqual(cell.data_type, 's')
            self.assertEqual(str(cell.value), long_id)
            wb.close()
        finally:
            if os.path.exists(tmp_xlsx):
                os.remove(tmp_xlsx)

    # -------------------------------------------------------------------------
    # Issue 8: Single Malformed Item in MinerU Structured Archive
    # -------------------------------------------------------------------------
    def test_issue8_mineru_archive_malformed_item_isolated(self):
        """测试 MinerU ZIP 产物包中单个损坏 item (如 page_idx='unknown' 或非字符串 table_body) 不会导致整包失效。"""
        bio = io.BytesIO()
        valid_html = "<table><tr><th>ColA</th><th>ColB</th></tr><tr><td>1</td><td>2</td></tr></table>"
        content_list = [
            {"type": "table", "table_body": valid_html, "page_idx": 0, "label": "Table 1"},
            {"type": "table", "table_body": {"invalid": 123}, "page_idx": "corrupted", "label": "Bad Table"},
            {"type": "table", "table_body": valid_html, "page_idx": 1, "label": "Table 2"}
        ]
        with zipfile.ZipFile(bio, 'w') as zf:
            zf.writestr("content_list.json", json.dumps(content_list))
        bio.seek(0)

        dfs = ocr_client.extract_mineru_structured_archive(bio)
        # 损坏的条目被安全跳过，有效表格 2 个全部顺利产出
        self.assertEqual(len(dfs), 2)
        self.assertEqual(dfs[0].attrs["label"], "Table 1")
        self.assertEqual(dfs[1].attrs["label"], "Table 2")

    # -------------------------------------------------------------------------
    # Issue 9: Scanned Page MinerU Empty Result Triggers Recovery
    # -------------------------------------------------------------------------
    def test_issue9_scanned_page_empty_result_triggers_recovery(self):
        """测试扫描页 0 声明且无提取表格时，返回 coverage_unknown=True 并触发定向视觉修复。"""
        # 创建一个单页扫描版 PDF（纯图片，无文字）
        import pymupdf as fitz
        fd, tmp_pdf = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            doc = fitz.open()
            page = doc.new_page(width=400, height=400)
            # 插入虚拟图片
            img_rect = fitz.Rect(50, 50, 350, 350)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), 1)
            page.insert_image(img_rect, pixmap=pix)
            doc.save(tmp_pdf)
            doc.close()

            # 传入空提取列表
            report = table_validator.cross_check_missed_tables([], tmp_pdf)
            self.assertFalse(report["is_complete"])
            self.assertTrue(report["coverage_unknown"])
            self.assertIn(0, report["uncovered_scanned_pages"])
        finally:
            if os.path.exists(tmp_pdf):
                os.remove(tmp_pdf)

    # -------------------------------------------------------------------------
    # Issue 10: Evaluator Tightened Threshold & Precision/F1 Tracking
    # -------------------------------------------------------------------------
    def test_issue10_evaluator_tightened_threshold_and_f1(self):
        """测试评测器杜绝单纯同页码+同列数误匹配，并精确计算 Precision 与 F1。"""
        gt_spec = {
            "group": "Test",
            "file_name": "test.pdf",
            "tables": [
                {
                    "table_id": "Table 1",
                    "label": "Table 1",
                    "page_idx": 0,
                    "num_cols": 2,
                    "rows": [["100", "200"], ["300", "400"]],
                    "sampled_numeric_cells": []
                }
            ]
        }
        # 伪表格：同页码、同列数，但标签完全无关且 0 个共享数字
        fake_df = pd.DataFrame({"X": ["9999", "8888"], "Y": ["7777", "6666"]})
        fake_df.attrs["label"] = "Irrelevant Note"
        fake_df.attrs["page_idx"] = 0

        res = evaluator.evaluate_pipeline_output([{"df": fake_df}], gt_spec, 1.0, "Test Pipeline")
        # 由于缺少标签且无数字重合，严格门禁拒绝误配，漏表数为 1，虚假检出 1 个
        self.assertEqual(res["missed_tables_count"], 1)
        self.assertEqual(res["false_positive_count"], 1)
        self.assertEqual(res["table_precision"], 0.0)
        self.assertEqual(res["table_recall"], 0.0)
        self.assertEqual(res["table_f1"], 0.0)

    # -------------------------------------------------------------------------
    # Issue 12: Config Disconnect for MINERU_TABLE_MERGE_ENABLE
    # -------------------------------------------------------------------------
    def test_issue12_mineru_table_merge_enable_config_fallback(self):
        """测试 MINERU_TABLE_MERGE_ENABLE 在未设环境变量时能正确读取 config.json。"""
        with patch.dict(os.environ, {}, clear=True):
            if "MINERU_TABLE_MERGE_ENABLE" in os.environ:
                del os.environ["MINERU_TABLE_MERGE_ENABLE"]
            # 模拟 config.json 中关闭合并开关
            with patch("scripts.common.load_config", return_value={"MINERU_TABLE_MERGE_ENABLE": False}):
                df1 = pd.DataFrame({"A": [1]})
                df2 = pd.DataFrame({"A": [2]})
                df1.attrs = {"label": "Table 1", "page_idx": 0}
                df2.attrs = {"label": "Table 1", "page_idx": 1}
                res = table_postprocess.merge_continuation_tables([df1, df2])
                # 开关关闭，保持未合并状态（返回 2 个表）
                self.assertEqual(len(res), 2)

    # -------------------------------------------------------------------------
    # Production Entry Point Architecture Upgrade
    # -------------------------------------------------------------------------
    def test_entry_point_architecture_upgrade(self):
        """测试生产统一入口：结构树快速通道 -> MinerU -> 数值门禁 -> 定向修复 -> 兜底多方投票。"""
        # 1. 结构树全覆盖且通过门禁快速通道直接返回
        df_struct = pd.DataFrame({
            "Sample": ["S1", "S2"],
            "Val": ["10.0", "20.0"]
        })
        df_struct.attrs = {"label": "Table 1", "page_idx": 0}

        struct_res = [{"df": df_struct, "page_idx": 0, "table_idx": 0, "bbox": None, "extractor": "pdf_inspector_structure"}]

        with patch("scripts.pdf_table_extractor.classify_and_extract_via_inspector", return_value={"pdf_type": "text_based", "confidence": 0.95, "tables": [], "structure_tables": struct_res, "pages_with_tables": [1], "error": None}), \
             patch("scripts.pdf_table_extractor.extract_via_structure_tree", return_value=struct_res), \
             patch("scripts.table_validator.verify_table_numeric_integrity", return_value=(True, {"match_ratio": 1.0, "font_corrupted": False})), \
             patch("fitz.open") as mock_fitz:
            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 1
            mock_doc[0].get_text.return_value = "Table 1 text\n10.0 20.0"
            mock_doc[0].get_images.return_value = []
            mock_fitz.return_value = mock_doc

            # 结构树快速通道直接采纳
            results, logs = pte._do_extract_tables_from_pdf("dummy.pdf", use_ocr_fallback=False)
            self.assertTrue(len(results) >= 1)
            self.assertTrue(any("结构树快速通道" in line for line in logs))

    # -------------------------------------------------------------------------
    # Additional Robustness & Edge-Case Regressions
    # -------------------------------------------------------------------------
    def test_mineru_gate_failure_falls_back_to_legacy_voting(self):
        """测试 MinerU 解析出的表格全部未通过数值门禁时，平滑降级进入多方投票备用管线，不返回空表。"""
        df_bad = pd.DataFrame({"A": ["9999", "8888"], "B": ["7777", "6666"]})
        df_bad.attrs = {"page_idx": 0, "bbox": None}

        df_voted = pd.DataFrame({"A": ["10", "20"], "B": ["30", "40"]})
        voted_res = [{"df": df_voted, "page_idx": 0, "table_idx": 0, "bbox": None, "extractor": "camelot"}]

        with patch("scripts.pdf_table_extractor.classify_and_extract_via_inspector", return_value={"pdf_type": "text_based", "confidence": 0.95, "tables": [], "structure_tables": [], "pages_with_tables": [1], "error": None}), \
             patch("scripts.pdf_table_extractor.extract_via_structure_tree", return_value=[]), \
             patch("scripts.ocr_client.is_mineru_available", return_value=True), \
             patch("scripts.ocr_client.call_mineru_structured_api", return_value=[df_bad]), \
             patch("scripts.table_validator.verify_table_numeric_integrity", return_value=(False, {"match_ratio": 0.0, "font_corrupted": False})), \
             patch("scripts.pdf_table_extractor.three_way_vote", return_value=(voted_res, ["voting fallback triggered"], {})), \
             patch("scripts.table_validator.validate_native_extraction_pipeline", return_value=(voted_res, set(), [])), \
             patch("fitz.open") as mock_fitz:
            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 1
            mock_doc[0].get_text.return_value = "10 20 30 40"
            mock_doc[0].get_images.return_value = []
            mock_fitz.return_value = mock_doc

            results, logs = pte._do_extract_tables_from_pdf("dummy.pdf", use_ocr_fallback=False)
            self.assertTrue(len(results) >= 1)
            self.assertTrue(any("平滑降级进入原生多方投票备用管线" in line for line in logs))
            self.assertEqual(results[0]["df"].iloc[0]["A"], "10")

    def test_combine_df_group_numeric_immunity_in_continuation(self):
        """测试 combine_df_group 跨页合并时，下一页首行含数值或数值占比>=25%时豁免，不被误删为重复表头。"""
        # 第一页表格：列名 A, B, C, D
        df1 = pd.DataFrame({
            "Sample": ["S1"],
            "Location": ["Loc1"],
            "Val1": ["100"],
            "Val2": ["200"],
        })
        # 第二页表格：首行是数据行，但某一列文本与 df1 的 Location 重名，另一列是纯数字
        df2 = pd.DataFrame({
            "Sample": ["S2"],
            "Location": ["Location"],  # 与表头重名，但整行属于合法数据
            "Val1": ["150"],          # 包含纯数字，享有数值豁免权
            "Val2": ["250"],
        })
        combined = table_postprocess.combine_df_group([df1, df2])
        # df2 的首行绝不被误判为 repeat_header 丢弃
        self.assertEqual(len(combined), 2)
        self.assertEqual(combined.iloc[1]["Sample"], "S2")

    def test_signed_long_numeric_id_excel_export(self):
        """测试带符号的正负超长整数样号 (+123456789012345678 / -123456789012345678) 显式设为文本单元格。"""
        pos_id = "+123456789012345678"
        neg_id = "-123456789012345678"

        self.assertEqual(table_postprocess.try_numeric(pos_id), pos_id)
        self.assertEqual(table_postprocess.try_numeric(neg_id), neg_id)

        fd, tmp_xlsx = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        try:
            df = pd.DataFrame({"ID": [pos_id, neg_id]})
            with pd.ExcelWriter(tmp_xlsx) as writer:
                df.to_excel(writer, index=False)
            excel_export.autofit_excel_columns(tmp_xlsx)

            from openpyxl import load_workbook
            wb = load_workbook(tmp_xlsx)
            ws = wb.active
            c1 = ws.cell(row=2, column=1)
            c2 = ws.cell(row=3, column=1)
            self.assertEqual(c1.data_type, 's')
            self.assertEqual(c2.data_type, 's')
            self.assertEqual(str(c1.value), pos_id)
            self.assertEqual(str(c2.value), neg_id)
            wb.close()
        finally:
            if os.path.exists(tmp_xlsx):
                os.remove(tmp_xlsx)

    def test_horizontal_coverage_merge_online_and_pdf(self):
        """测试横向列数显著更多（shape[0]*shape[1] 显著更大）的本地 PDF 表格覆盖在线残表。"""
        online_narrow = pd.DataFrame({
            "A": ["S1", "S2", "S3", "S4"],
            "B": ["1.0", "2.0", "3.0", "4.0"]
        })
        online_narrow.attrs["label"] = "Table 1"

        pdf_wide = pd.DataFrame({
            f"Col_{i}": [f"Val_{i}_{r}" for r in range(4)]
            for i in range(12)
        })
        pdf_wide.attrs["label"] = "Table 1"

        merged = extract_zotero_table._merge_online_and_pdf_tables([online_narrow], [pdf_wide])
        self.assertEqual(len(merged), 1)
        # 本地 PDF 版本具有 12 列（容积 48 vs 8），成功覆盖在线残表
        self.assertEqual(merged[0].shape[1], 12)

    def test_continuation_merge_with_subtitled_labels(self):
        """测试前表带有副标题描述 (Table 1: Physical properties) 与续表 (Table 1 (continued)) 正常合并。"""
        t1 = pd.DataFrame({'Sample': ['S1'], 'Val': ['1.0']})
        t1.attrs = {'label': 'Table 1: Physical properties', 'table_title': 'Table 1: Physical properties', 'page_idx': 0}

        t2 = pd.DataFrame({'Sample': ['S2'], 'Val': ['2.0']})
        t2.attrs = {'label': 'Table 1 (continued)', 'table_title': 'Table 1 (continued)', 'page_idx': 1, 'is_continuation': True}

        res = table_postprocess.merge_continuation_tables([t1, t2])
        self.assertEqual(len(res), 1)
        self.assertEqual(len(res[0]), 2)

    def test_continuation_merge_distinct_numbered_tables(self):
        """测试多级独立表号 (Table 1-1 与 Table 1-2) 即使同列数也绝不误合并。"""
        t1 = pd.DataFrame({'Sample': ['S1'], 'Val': ['1.0']})
        t1.attrs = {'table_title': 'Table 1-1: First batch', 'page_idx': 0}

        t2 = pd.DataFrame({'Sample': ['S2'], 'Val': ['2.0']})
        t2.attrs = {'table_title': 'Table 1-2: Second batch', 'page_idx': 1}

        res = table_postprocess.merge_continuation_tables([t1, t2])
        self.assertEqual(len(res), 2)

    def test_same_page_continuation_rejects_following_independent_table(self):
        """测试同页续表片段后面跟随的独立表格 (Table 2) 绝对不被吞并。"""
        a = pd.DataFrame({'Sample': ['Alpha', 'Beta'], 'Val': ['13.5', '25.5']})
        a.attrs = {'label': 'Table 1', 'table_title': 'Table 1 (续)', 'page_idx': 0, 'bbox': [40, 50, 250, 200], 'is_continuation': True}
        b = pd.DataFrame({'Sample': ['Epsilon', 'Zeta'], 'Val': ['55.5', '66.5']})
        b.attrs = {'label': 'Table 2', 'table_title': 'Table 2', 'page_idx': 0, 'bbox': [40, 350, 250, 500]}

        res = table_postprocess.merge_continuation_tables([a, b])
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].attrs.get('label'), 'Table 1')
        self.assertEqual(res[1].attrs.get('label'), 'Table 2')

    def test_mineru_structured_result_direct_markdown_in_extract_result(self):
        """测试 MinerU 返回的 extract_result 列表中直接包含 markdown 字段时能正确解析为 DataFrame。"""
        client = ocr_client.MinerUClient(api_key='')
        payload = {
            'task_id': 'agent-test',
            'state': 'done',
            'extract_result': [{'markdown': '<table><tr><th>Sample</th><th>Value</th></tr><tr><td>S1</td><td>12.5</td></tr></table>'}]
        }
        dfs = client.parse_mineru_structured_result(payload)
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].shape, (1, 2))
        self.assertEqual(dfs[0].attrs.get('extractor'), 'mineru_api')

    def test_unlabeled_single_table_merge_online_and_pdf(self):
        """测试无显式表号标签的单表在线预览与完整本地 PDF 表格比对，更全的 PDF 表格覆盖在线残表。"""
        online = pd.DataFrame({'A': [f'S{i}' for i in range(4)], 'B': [str(10+i) for i in range(4)]})
        pdf = pd.DataFrame({'A': [f'S{i}' for i in range(21)], 'B': [str(10+i) for i in range(21)]})

        merged = extract_zotero_table._merge_online_and_pdf_tables([online], [pdf])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]), 21)


if __name__ == "__main__":
    unittest.main()

