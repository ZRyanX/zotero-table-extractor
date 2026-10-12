import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pymupdf as fitz
from PIL import Image, ImageDraw

# Add scripts directory to sys.path
scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

import excel_export as ex
import extract_zotero_table as main
import pdf_table_extractor as pte
import table_postprocess as tp
from omnidocbench import compute_layout_detection_map


class TestRecheck4CriticalFixes(unittest.TestCase):
    """验证第 4 轮核查发现的 6 项关键缺陷修复。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_scanned_pdf_completeness_unknown_triggers_local_fallback(self):
        """
        1. [P1] 扫描 PDF 完整性属于未知，不能因未检测到文本或矢量线条而提前判定为完整。
        在线只返回 Table 1 时，必须调用本地 PDF 提取管线以避免遗漏后续表格。
        """
        pdf_path = os.path.join(self.temp_dir, "scanned_doc.pdf")
        doc = fitz.open()
        for n in [1, 2]:
            im = Image.new("RGB", (600, 800), "white")
            ImageDraw.Draw(im).text(
                (50, 50),
                f"Table {n}\nSample     Value\nA               {n}\nB               {n+1}",
                fill="black",
                font_size=24,
            )
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            p = doc.new_page(width=600, height=800)
            p.insert_image(p.rect, stream=buf.getvalue())
        doc.save(pdf_path)
        doc.close()

        out_path = os.path.join(self.temp_dir, "out.xlsx")
        d = pd.DataFrame({"Sample": ["A", "B"], "Value": [1, 2]})
        d.attrs["label"] = "Table 1"

        online_mock = MagicMock()
        online_mock.extract_tables_online.return_value = ([d], "Test")

        with patch.object(main, "online", online_mock), \
             patch.object(main, "is_pre_2020_chinese_paper", return_value=False), \
             patch.object(main, "extract_tables_from_pdf", return_value=([], [])) as local_mock, \
             patch.object(main, "ocr_client", None), \
             patch.object(main, "export_crops_to_excel", return_value=False), \
             patch.object(main, "_trigger_zotero_sync"):
            ok = main._do_process_single_pdf(pdf_path, pdf_path, types.SimpleNamespace(output=out_path))

        # 扫描件完整性未知，本地管线必须被调用过至少 1 次以补充检查
        self.assertTrue(ok)
        self.assertGreaterEqual(local_mock.call_count, 1, "本地 PDF 提取管线必须被调用以核实扫描页中的表格")

    def test_distinct_tables_not_replaced_by_similarity(self):
        """
        2. [P1] 两张独立表表号冲突且空间完全分离时，禁止因数值内容相似而被互相替换。
        """
        values = [["A", 2.5, 3.5], ["B", 4.5, 5.5], ["C", 6.5, 7.5]]
        df_a = pd.DataFrame(values, columns=["Sample", "Value", "Error"])
        df_b = df_a.copy()
        df_a.attrs.update(label="Table 1", extractor="native_find_tables")
        df_b.attrs.update(label="Table 2", extractor="paddleocr_vl")

        na = {"df": df_a, "page_idx": 0, "bbox": [10, 10, 200, 110]}
        ob = {"df": df_b, "page_idx": 0, "bbox": [10, 400, 200, 500]}
        logs = []
        result = pte.reconcile_native_and_ocr_tables([na], [ob], [0], logs)

        labels = [item["df"].attrs.get("label") for item in result]
        self.assertEqual(len(result), 2, "两张独立表格均应保留")
        self.assertIn("Table 1", labels)
        self.assertIn("Table 2", labels)

    def test_replicate_measurements_preserved_in_export(self):
        """
        3. [P1] 导出时严禁无条件去重，合法的重复实验记录（如重复测量 Control, 2.5）必须完整保留。
        """
        df = pd.DataFrame({
            "Group": ["Control", "Control", "Treatment"],
            "Value": [2.5, 2.5, 4.5]
        })
        df.attrs["label"] = "Table 1"
        out_file = os.path.join(self.temp_dir, "replicates.xlsx")
        ex.save_tables_to_excel([df], out_file)

        saved_df = pd.read_excel(out_file)
        self.assertEqual(len(saved_df), 3, "3 行原始数据在导出后必须保持 3 行，重复行不得丢失")
        self.assertEqual(saved_df["Group"].tolist(), ["Control", "Control", "Treatment"])

    def test_single_column_text_table_not_stripped_as_footnotes(self):
        """
        4. [P1] 单列文字表中的正文长句严禁因无数字或字数长而被误当成尾注剥离。
        """
        sentences = [
            "Water was collected from the upstream river location",
            "Water was collected from the downstream river location",
            "Water was collected from the reference lake location"
        ]
        df = pd.DataFrame({"Sampling protocol": sentences})
        processed = tp.postprocess_dataframe(df)

        self.assertEqual(len(processed), 3, "单列文字表中的所有数据行必须完整保留")
        self.assertIsNone(processed.attrs.get("footnotes"), "正文句子不应被误判并转入 footnotes")

    def test_quarantine_all_rejected_returns_false(self):
        """
        5. [P1] 全部结果被隔离拒绝时，目标工作簿未生成，保存函数必须返回 False。
        """
        out_file = os.path.join(self.temp_dir, "paywall_only.xlsx")
        df_paywall = pd.DataFrame({"Message": ["Institutional login required"]})
        df_paywall.attrs["label"] = "Table 1"

        ok = ex.save_tables_to_excel([df_paywall], out_file)
        self.assertFalse(ok, "全部表格被拒绝隔离时应返回 False 以区分导出成功与拒绝隔离")
        self.assertFalse(os.path.exists(out_file), "目标工作簿不应存在")

        rej_dir = os.path.join(self.temp_dir, "rejected")
        self.assertTrue(os.path.exists(rej_dir), "隔离目录 rejected/ 应该已创建")
        rej_files = os.listdir(rej_dir)
        self.assertTrue(any(f.startswith("paywall_table_") for f in rej_files), "付费墙表应被隔离至 rejected/ 目录")

    def test_map_evaluation_no_oracle_ground_truth_sorting(self):
        """
        6. [P2] mAP 计算严格按预测置信度排序，无置信度时保留原顺序，严禁利用真实标注 IoU 作弊排序。
        9 个错误框 + 1 个正确框在缺少置信度时，其 mAP 必须为 0.1 而非 1.0。
        """
        fp = [[20 + i * 20, 20, 30 + i * 20, 30] for i in range(9)]
        tpbox = [0, 0, 10, 10]
        gt = [tpbox]

        # 缺少置信度时
        res_no_conf = compute_layout_detection_map(fp + [tpbox], gt)
        # 提供相同置信度时
        res_equal_conf = compute_layout_detection_map([b + [0.5] for b in fp + [tpbox]], gt)

        self.assertAlmostEqual(res_no_conf["mAP@50"], 0.1, places=3)
        self.assertAlmostEqual(res_equal_conf["mAP@50"], 0.1, places=3)
        self.assertAlmostEqual(res_no_conf["mAP@50:95"], 0.1, places=3)

    def test_unbound_local_error_not_raised_when_expected_labels_incomplete(self):
        """
        验证当数字 PDF 存在明确预期表号 (expected_labels) 且在线结果不完整时，
        _do_process_single_pdf 不会抛出 UnboundLocalError: has_scanned_page，
        并正确触发本地管线回退。
        """
        pdf_path = os.path.join(self.temp_dir, "digital_two_tables.pdf")
        doc = fitz.open()
        p1 = doc.new_page(width=600, height=800)
        p1.insert_text((50, 50), "Table 1: First experimental measurement\nCol1 Col2\nA 1\nB 2")
        p2 = doc.new_page(width=600, height=800)
        p2.insert_text((50, 50), "Table 2: Second experimental measurement\nCol1 Col2\nC 3\nD 4")
        doc.save(pdf_path)
        doc.close()

        out_path = os.path.join(self.temp_dir, "digital_out.xlsx")
        d = pd.DataFrame({"Sample": ["A", "B"], "Value": [1, 2]})
        d.attrs["label"] = "Table 1"

        online_mock = MagicMock()
        online_mock.extract_tables_online.return_value = ([d], "Digital Paper")

        with patch.object(main, "online", online_mock), \
             patch.object(main, "is_pre_2020_chinese_paper", return_value=False), \
             patch.object(main, "extract_tables_from_pdf", return_value=([], [])) as local_mock, \
             patch.object(main, "ocr_client", None), \
             patch.object(main, "export_crops_to_excel", return_value=False), \
             patch.object(main, "_trigger_zotero_sync"):
            # 必须顺利执行不发生 UnboundLocalError
            ok = main._do_process_single_pdf(pdf_path, pdf_path, types.SimpleNamespace(output=out_path))

        self.assertTrue(ok)
        self.assertGreaterEqual(local_mock.call_count, 1, "本地管线应被调用以补查 Table 2")

    def test_mixed_digital_and_scanned_triggers_local_fallback(self):
        """
        验证混合型 PDF（第 1 页有文字表 1，第 2 页为扫描页表 2）：
        即使在线获取了表 1 且文本层仅能检出表 1，
        只要包含扫描页，完整性即属于未知，必须触发本地回退。
        """
        pdf_path = os.path.join(self.temp_dir, "mixed_doc.pdf")
        doc = fitz.open()
        # 第 1 页：包含文字 Table 1
        p1 = doc.new_page(width=600, height=800)
        p1.insert_text((50, 50), "Table 1: Digital table\nA 1\nB 2")
        # 第 2 页：纯扫描图
        im = Image.new("RGB", (600, 800), "white")
        ImageDraw.Draw(im).text((50, 50), "Table 2\nScanned Image", fill="black", font_size=24)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        p2 = doc.new_page(width=600, height=800)
        p2.insert_image(p2.rect, stream=buf.getvalue())
        doc.save(pdf_path)
        doc.close()

        out_path = os.path.join(self.temp_dir, "mixed_out.xlsx")
        d = pd.DataFrame({"A": [1, 2]})
        d.attrs["label"] = "Table 1"

        online_mock = MagicMock()
        online_mock.extract_tables_online.return_value = ([d], "Mixed Paper")

        with patch.object(main, "online", online_mock), \
             patch.object(main, "is_pre_2020_chinese_paper", return_value=False), \
             patch.object(main, "extract_tables_from_pdf", return_value=([], [])) as local_mock, \
             patch.object(main, "ocr_client", None), \
             patch.object(main, "export_crops_to_excel", return_value=False), \
             patch.object(main, "_trigger_zotero_sync"):
            ok = main._do_process_single_pdf(pdf_path, pdf_path, types.SimpleNamespace(output=out_path))

        self.assertTrue(ok)
        self.assertGreaterEqual(local_mock.call_count, 1, "混合型包含扫描页时完整性未知，必须触发本地管线")

    def test_replicate_measurements_preserved_in_multi_file_export(self):
        """
        验证当 single_file=False (输出为目录) 且目标表格已存在时，
        合并覆盖时严禁调用 drop_duplicates 误删合法平行重复测定行。
        """
        df = pd.DataFrame({
            "Group": ["Control", "Control", "Treatment"],
            "Value": [2.5, 2.5, 4.5]
        })
        df.attrs["label"] = "Table 1"
        target_dir = os.path.join(self.temp_dir, "multi_export_dir")

        # 首次保存
        ex.save_tables_to_excel([df], target_dir, single_file=False)
        # 二次保存（模拟更新或合并）
        ex.save_tables_to_excel([df], target_dir, single_file=False)

        saved_file = os.path.join(target_dir, "Table 1.xlsx")
        saved_df = pd.read_excel(saved_file)
        self.assertEqual(len(saved_df), 3, "目录多文件导出合并时，3 行重复测量记录必须完整保留")

    def test_conflicting_labels_not_merged_in_spatial_matching(self):
        """
        验证空间匹配 (Step 2) 排除明确的表号冲突：
        即使两张表 bbox 高度重叠 (如切图框偏差)，若各自标有明确的不同表号 (Table 1 vs Table 2)，
        严禁判定为同一张表而互相覆写。
        """
        df1 = pd.DataFrame({"A": [1, 2]})
        df2 = pd.DataFrame({"B": [3, 4]})
        df1.attrs["label"] = "Table 1"
        df2.attrs["label"] = "Table 2"

        na = {"df": df1, "page_idx": 0, "bbox": [10, 10, 200, 200]}
        ob = {"df": df2, "page_idx": 0, "bbox": [10, 10, 200, 200]}
        logs = []
        result = pte.reconcile_native_and_ocr_tables([na], [ob], [0], logs)

        labels = [item["df"].attrs.get("label") for item in result]
        self.assertEqual(len(result), 2, "表号冲突的两张表即使 bbox 重叠也不得合并覆写")
        self.assertIn("Table 1", labels)
        self.assertIn("Table 2", labels)

    def test_is_academic_footnote_row_optional_col_count(self):
        """
        验证 is_academic_footnote_row 在不传 col_count 时保持向后兼容，正常返回元组。
        """
        is_note, text = tp.is_academic_footnote_row(["Sample A", "12.5"])
        self.assertFalse(is_note)
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
