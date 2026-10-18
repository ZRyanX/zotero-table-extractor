#!/usr/bin/env python3
"""
test_mineru_structured_upgrade.py — 针对 MinerU 架构升级、结构化提取与本地质量门禁的综合单元测试套件：
1. 结构化 ZIP 归档动态匹配 (*content_list*.json, middle*.json, structured_content.json)
2. 多态表标题 (string, list of dicts, dict) 与尾注提取
3. HTML 安全解析为 DataFrame、MultiIndex 干净展平与数据类型锁定 (样号 '001', 范围 '10-20' 保真)
4. df.attrs 元数据完整注入 ('extractor': 'mineru_api', 'page_idx', 'bbox', 'table_title', 'table_footnote')
5. table_postprocess.py 白名单零破坏路径保护 (mineru_api 真实首行数据绝不被抢夺为表头)
6. 引擎调度修复：OCR_ENGINE == 'mineru' 时直达 MinerU，阻断百度 AIStudio 旁路
7. page_ranges 参数智能规范化与提交
8. 零 Token 备用 Agent API (/api/v1/agent/parse/file) 降级保护 (<=10MB, <=20页)
9. 本地数值一致性与质量门禁 (verify_table_numeric_integrity)：旋转坐标反投影、坏损私有字库安全旁路、Bag-of-Numbers 校验
10. 全文献表格声明交叉比对 (cross_check_missed_tables) 漏表检出
"""

import io
import os
import sys
import json
import zipfile
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np

# 确保 scripts 目录在 sys.path 中
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    import pymupdf as fitz
except ImportError:
    import fitz

import ocr_client
import pdf_table_extractor
import table_postprocess
import table_validator


class TestMinerUStructuredArchiveExtraction(unittest.TestCase):
    """测试 1: 结构化 ZIP 归档动态匹配、多态解析与类型锁定。"""

    def _create_dummy_zip(self, json_filename: str, json_content: Any) -> bytes:
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(json_filename, json.dumps(json_content))
        return bio.getvalue()

    def test_extract_content_list_json(self):
        """测试动态匹配 *content_list*.json 并正确解析多态表标题与 MultiIndex 表格。"""
        html = """
        <table>
          <thead>
            <tr><th colspan="2">Sample Group</th><th colspan="2">Concentration</th></tr>
            <tr><th>Sample ID</th><th>Batch</th><th>Range</th><th>ppm</th></tr>
          </thead>
          <tbody>
            <tr><td>001</td><td>08</td><td>10-20</td><td>12.5</td></tr>
            <tr><td>002</td><td>09</td><td>20-30</td><td>15.8</td></tr>
          </tbody>
        </table>
        """
        content_list_data = [
            {
                "type": "text",
                "text": "Introduction paragraph"
            },
            {
                "type": "table",
                "table_body": html,
                "bbox": [50.0, 100.0, 500.0, 300.0],
                "page_idx": 2,
                "table_caption": [
                    {"text": "Table 1."},
                    {"text": "Summary of Geochemical Samples."}
                ],
                "table_footnote": {"text": "Note: BDL = Below Detection Limit."}
            }
        ]
        zip_bytes = self._create_dummy_zip("doc_content_list.json", content_list_data)
        dfs = ocr_client.extract_mineru_structured_archive(zip_bytes)

        self.assertEqual(len(dfs), 1)
        df = dfs[0]
        # 1. 验证元数据
        self.assertEqual(df.attrs.get("extractor"), "mineru_api")
        self.assertEqual(df.attrs.get("page_idx"), 2)
        self.assertEqual(df.attrs.get("bbox"), [50.0, 100.0, 500.0, 300.0])
        self.assertIn("Table 1", df.attrs.get("table_title", ""))
        self.assertIn("BDL", df.attrs.get("table_footnote", ""))
        self.assertEqual(df.attrs.get("label"), "Table 1")

        # 2. 验证 MultiIndex 干净展平
        expected_cols = [
            "Sample Group_Sample ID",
            "Sample Group_Batch",
            "Concentration_Range",
            "Concentration_ppm"
        ]
        self.assertEqual(list(df.columns), expected_cols)

        # 3. 验证字符串类型锁定（样号 001、08 与范围 10-20 未被转为数值/日期）
        self.assertEqual(str(df.iat[0, 0]), "001")
        self.assertEqual(str(df.iat[0, 1]), "08")
        self.assertEqual(str(df.iat[0, 2]), "10-20")

    def test_extract_middle_json_with_pdf_info(self):
        """测试动态匹配 middle*.json 且结构包含 pdf_info 的层级解析。"""
        html = """
        <table>
          <tr><th>Element</th><th>Value</th></tr>
          <tr><td>SiO2</td><td>72.4</td></tr>
        </table>
        """
        middle_data = {
            "pdf_info": [
                {
                    "page_idx": 1,
                    "preproc_blocks": [
                        {
                            "type": "table",
                            "table_body": html,
                            "bbox": [40, 60, 400, 200],
                            "table_caption": "Table 2 Major oxides (wt%)",
                            "table_footnote": "Data from XRF"
                        }
                    ]
                }
            ]
        }
        zip_bytes = self._create_dummy_zip("paper_middle.json", middle_data)
        dfs = ocr_client.extract_mineru_structured_archive(zip_bytes)

        self.assertEqual(len(dfs), 1)
        df = dfs[0]
        self.assertEqual(df.attrs.get("extractor"), "mineru_api")
        self.assertEqual(df.attrs.get("page_idx"), 1)
        self.assertEqual(df.attrs.get("bbox"), [40, 60, 400, 200])
        self.assertEqual(df.attrs.get("table_title"), "Table 2 Major oxides (wt%)")
        self.assertEqual(df.attrs.get("table_footnote"), "Data from XRF")
        self.assertEqual(df.attrs.get("label"), "Table 2")

    def test_extract_structured_content_json(self):
        """测试动态匹配 structured_content.json 与单字典格式标题。"""
        html = """
        <table>
          <tr><th>Site</th><th>Depth</th></tr>
          <tr><td>ST01</td><td>10.5</td></tr>
        </table>
        """
        sc_data = {
            "elements": [
                {
                    "type": "table",
                    "table_body": html,
                    "bbox": [10, 20, 300, 150],
                    "page_idx": 0,
                    "table_caption": {"content": "Table 3 Station information"},
                    "table_footnote": [{"content": "GPS accuracy ±3m"}]
                }
            ]
        }
        zip_bytes = self._create_dummy_zip("structured_content.json", sc_data)
        dfs = ocr_client.extract_mineru_structured_archive(zip_bytes)

        self.assertEqual(len(dfs), 1)
        df = dfs[0]
        self.assertEqual(df.attrs.get("table_title"), "Table 3 Station information")
        self.assertIn("GPS", df.attrs.get("table_footnote", ""))

    def test_fallback_unnamed_json_containing_table(self):
        """测试当 JSON 命名不规范时，自动探测包含 table_body 的 JSON 文件。"""
        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        custom_data = [
            {"type": "table", "table_body": html, "page_idx": 3, "table_caption": "Table 4"}
        ]
        zip_bytes = self._create_dummy_zip("arbitrary_model_output.json", custom_data)
        dfs = ocr_client.extract_mineru_structured_archive(zip_bytes)

        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].attrs.get("page_idx"), 3)


class TestMinerUDispatchAndFallback(unittest.TestCase):
    """测试 2: OCR_ENGINE 调度修复、page_ranges 格式化与 Agent API 零 Token 降级。"""

    def test_format_page_ranges(self):
        """测试 0-based 整数列表与字符串页码范围格式化。"""
        self.assertEqual(ocr_client.format_page_ranges([0, 1, 2]), "1-3")
        self.assertEqual(ocr_client.format_page_ranges([0, 2, 4]), "1,3,5")
        self.assertEqual(ocr_client.format_page_ranges("1-5,7"), "1-5,7")
        self.assertIsNone(ocr_client.format_page_ranges(None))
        self.assertIsNone(ocr_client.format_page_ranges([]))

    @patch("ocr_client.requests.post")
    def test_submit_task_with_page_ranges(self, mock_post):
        """测试 submit_task 在 payload 中正确包含 page_ranges。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "data": {"batch_id": "batch_123"}}
        mock_post.return_value = mock_resp

        client = ocr_client.MinerUClient(api_key="valid_token")
        task_id = client.submit_task("https://example.com/paper.pdf", page_ranges=[0, 1, 2])

        self.assertEqual(task_id, "batch_123")
        mock_post.assert_called_once()
        sent_payload = mock_post.call_args[1]["json"]
        self.assertEqual(sent_payload.get("page_ranges"), "1-3")

    @patch("ocr_client.requests.post")
    def test_zero_token_agent_api_fallback_eligible(self, mock_post):
        """测试无 API_KEY 且 <=10MB, <=20页时，自动触发 Agent API 零 Token 备用提交。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "data": "agent_task_999"}
        mock_post.return_value = mock_resp

        # 创建一个 1 页的临时 PDF
        doc = fitz.open()
        doc.new_page(width=400, height=500)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = f.name
        doc.save(pdf_path)
        doc.close()

        try:
            client = ocr_client.MinerUClient(api_key="")  # 空 API Key
            task_id = client.submit_task(pdf_path, page_ranges=[0])

            self.assertEqual(task_id, "agent_task_999")
            self.assertIn("agent_task_999", client._agent_tasks)
            mock_post.assert_called_once()
            called_url = mock_post.call_args[0][0]
            self.assertTrue(called_url.endswith("/api/v1/agent/parse/file"))
        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    def test_zero_token_agent_api_exceeds_limits_rejected(self):
        """测试无 API_KEY 且超过 20 页时，绝不误发，直接安全返回 None。"""
        doc = fitz.open()
        for _ in range(25):  # 25 页 > 20 页
            doc.new_page()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = f.name
        doc.save(pdf_path)
        doc.close()

        try:
            client = ocr_client.MinerUClient(api_key="")
            task_id = client.submit_task(pdf_path)
            self.assertIsNone(task_id)
        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    @patch("ocr_client.call_mineru_api")
    def test_run_ppstructure_vlm_pipeline_routes_to_mineru(self, mock_mineru_call):
        """测试 run_ppstructure_vlm_pipeline 在配置 OCR_ENGINE='mineru' 时直接调度 MinerU。"""
        mock_mineru_call.return_value = "| A | B |\n|---|---|\n| 1 | 2 |"
        with patch.dict(os.environ, {"OCR_ENGINE": "mineru"}):
            md = ocr_client.run_ppstructure_vlm_pipeline("dummy.pdf", pages=[0, 1])
            self.assertEqual(md, "| A | B |\n|---|---|\n| 1 | 2 |")
            mock_mineru_call.assert_called_once()
            self.assertEqual(mock_mineru_call.call_args[1].get("page_ranges"), [0, 1])

    @patch("ocr_client.call_mineru_structured_api")
    def test_extract_via_ocr_routes_to_mineru_structured(self, mock_mineru_struct):
        """测试 pdf_table_extractor.extract_via_ocr 在 OCR_ENGINE='mineru' 时优先直通 MinerU。"""
        dummy_df = pd.DataFrame({"X": ["10"], "Y": ["20"]})
        dummy_df.attrs["extractor"] = "mineru_api"
        dummy_df.attrs["page_idx"] = 0
        dummy_df.attrs["bbox"] = [10, 20, 100, 200]
        mock_mineru_struct.return_value = [dummy_df]

        with patch.dict(os.environ, {"OCR_ENGINE": "mineru"}):
            results = pdf_table_extractor.extract_via_ocr("dummy.pdf", pages=[0])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["extractor"], "mineru_api")
            self.assertEqual(results[0]["bbox"], [10, 20, 100, 200])
            mock_mineru_struct.assert_called_once()


class TestMinerUTableProtectionInPostprocess(unittest.TestCase):
    """测试 3: table_postprocess.py 中针对 mineru_api 的零破坏白名单直通保护。"""

    def test_mineru_table_row0_not_consumed_as_header(self):
        """
        测试当 extractor 为 mineru_api 时，postprocess_table 绝不调用 _detect_and_promote_header，
        真实数据第 0 行必须毫发无损地保留为数据行。
        """
        # 构造带有字符串数据行但已有合法列名的 DataFrame
        df = pd.DataFrame(
            data=[
                ["Sample A", "Basalt", "0.45"],
                ["Sample B", "Granite", "0.82"],
            ],
            columns=["Sample_Code", "Lithology", "Au_ppm"]
        )
        df.attrs["extractor"] = "mineru_api"
        df.attrs["page_idx"] = 1

        cleaned_df = table_postprocess.postprocess_table(df)

        # 验证行数仍为 2，第 0 行并未被提升为表头
        self.assertEqual(len(cleaned_df), 2)
        self.assertEqual(cleaned_df.iat[0, 0], "Sample A")
        self.assertEqual(cleaned_df.iat[1, 0], "Sample B")
        self.assertEqual(list(cleaned_df.columns), ["Sample_Code", "Lithology", "Au_ppm"])
        self.assertEqual(cleaned_df.attrs.get("extractor"), "mineru_api")


class TestTableNumericIntegrityAndGate(unittest.TestCase):
    """测试 4: 本地数值一致性门禁 (verify_table_numeric_integrity) 与漏表交叉核对。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_numeric_integrity_normal_match(self):
        """测试正常数值重合通过门禁校验。"""
        pdf_path = os.path.join(self.temp_dir, "normal.pdf")
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text(fitz.Point(100, 100), "Table 1 Results")
        page.insert_text(fitz.Point(100, 150), "12.5 35.8 100 0.05")
        doc.save(pdf_path)
        doc.close()

        df = pd.DataFrame({
            "Val1": ["12.5", "35.8"],
            "Val2": ["100", "0.05"]
        })
        bbox = [90, 80, 500, 250]

        is_valid, info = table_validator.verify_table_numeric_integrity(
            df=df, pdf_path=pdf_path, page_idx=0, bbox=bbox, min_match_ratio=0.8
        )
        self.assertTrue(is_valid)
        self.assertGreaterEqual(info["match_ratio"], 0.8)
        self.assertFalse(info["font_corrupted"])

    def test_numeric_integrity_rotation_aware_deprojection(self):
        """测试旋转感知的坐标反投影：当 PDF 页面旋转 90° 时，视觉切图坐标能准确定位原文本。"""
        pdf_path = os.path.join(self.temp_dir, "rotated.pdf")
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text(fitz.Point(150, 150), "45.6 78.9 99.1")
        page.set_rotation(90)  # 视觉宽高变为 (800, 600)
        doc.save(pdf_path)
        doc.close()

        df = pd.DataFrame({"A": ["45.6"], "B": ["78.9"], "C": ["99.1"]})

        # 在旋转后的视觉坐标中，原本 (150, 150) 的文字视觉位置在 (650, 150)，并沿 Y 轴展开
        visual_bbox = [600, 100, 700, 350]

        is_valid, info = table_validator.verify_table_numeric_integrity(
            df=df, pdf_path=pdf_path, page_idx=0, bbox=visual_bbox, min_match_ratio=0.7
        )
        self.assertTrue(is_valid)
        self.assertGreaterEqual(info["match_ratio"], 0.7)

    def test_numeric_integrity_broken_font_safeguard(self):
        """测试损坏矢量字库 (私有编码 U+F000~U+F8FF 或 U+FFFD) 安全旁路：不误拒绝表格。"""
        pdf_path = os.path.join(self.temp_dir, "broken_font.pdf")
        doc = fitz.open()
        doc.new_page(width=600, height=800)
        doc.save(pdf_path)
        doc.close()

        df = pd.DataFrame({"A": ["12.5"], "B": ["34.2"]})
        bbox = [50, 50, 400, 200]

        # 模拟字库映射损坏，从 PDF 文本流中提取到私有区乱码或替换符号
        with patch.object(fitz.Page, "get_text", return_value="\uf031\uf032.\uf035 \ufffd\ufffd"):
            is_valid, info = table_validator.verify_table_numeric_integrity(
                df=df, pdf_path=pdf_path, page_idx=0, bbox=bbox, min_match_ratio=0.8
            )
            # 必须触发安全旁路，跳过拒绝并信任视觉结果
            self.assertTrue(is_valid)
            self.assertTrue(info["font_corrupted"])
            self.assertEqual(info["reason"], "trusted_visual_due_to_broken_font")

    def test_cross_check_missed_tables(self):
        """测试全文表格声明交叉核对并成功发现漏检表。"""
        pdf_path = os.path.join(self.temp_dir, "declared.pdf")
        doc = fitz.open()
        p1 = doc.new_page(width=600, height=800)
        p1.insert_text(fitz.Point(100, 100), "Table 1 Geochemical properties of rocks")
        p2 = doc.new_page(width=600, height=800)
        p2.insert_text(fitz.Point(100, 100), "Table 2 Trace element concentrations")
        doc.save(pdf_path)
        doc.close()

        # 仅提取到了 Table 1，缺失 Table 2
        extracted_df = pd.DataFrame({"A": [1, 2]})
        extracted_df.attrs["label"] = "Table 1"
        extracted_df.attrs["page_idx"] = 0

        report = table_validator.cross_check_missed_tables([extracted_df], pdf_path)

        self.assertFalse(report["is_complete"])
        self.assertEqual(report["declared_count"], 2)
        self.assertEqual(report["extracted_count"], 1)
        self.assertEqual(len(report["missed_tables"]), 1)
        self.assertEqual(report["missed_tables"][0]["label"], "Table 2")
        self.assertEqual(report["missed_tables"][0]["page_idx"], 1)

    def test_cross_check_missed_tables_fallback_to_title(self):
        """测试当表格缺少 explicit label 属性但 table_title 包含表号时，不发生漏检误报。"""
        pdf_path = os.path.join(self.temp_dir, "title_declared.pdf")
        doc = fitz.open()
        p1 = doc.new_page(width=600, height=800)
        p1.insert_text(fitz.Point(100, 100), "Table 1 Main experimental results")
        doc.save(pdf_path)
        doc.close()

        # 构造未填 label 但已填 table_title 的提取表
        extracted_df = pd.DataFrame({"X": [10]})
        extracted_df.attrs["table_title"] = "Table 1 Main experimental results"
        extracted_df.attrs["page_idx"] = 0

        report = table_validator.cross_check_missed_tables([extracted_df], pdf_path)
        self.assertTrue(report["is_complete"])
        self.assertEqual(len(report["missed_tables"]), 0)

    def test_numeric_integrity_cropbox_offset_with_rotation(self):
        """测试 PDF 页面同时带有非零 CropBox 偏移与 90° 旋转时，不发生二次偏移且数值校验准确。"""
        pdf_path = os.path.join(self.temp_dir, "cropbox_rot.pdf")
        doc = fitz.open()
        page = doc.new_page(width=800, height=1000)
        page.set_cropbox(fitz.Rect(100, 200, 700, 900))
        page.insert_text(fitz.Point(150, 250), "45.6 78.9 99.1")
        page.set_rotation(90)
        doc.save(pdf_path)
        doc.close()

        # 视觉旋转坐标系下的 bbox
        vis_bbox = [440, 140, 470, 260]

        # 真实正确表格
        valid_df = pd.DataFrame({"A": ["45.6"], "B": ["78.9"], "C": ["99.1"]})
        is_valid, info = table_validator.verify_table_numeric_integrity(
            valid_df, pdf_path, page_idx=0, bbox=vis_bbox, min_match_ratio=0.7
        )
        self.assertTrue(is_valid)
        self.assertGreaterEqual(info["match_ratio"], 0.7)

        # 构造伪造/幻觉表格，必须被门禁准确拒绝 (绝不因坐标漂移而伪装为 scanned_or_empty_text_layer)
        hallucinated_df = pd.DataFrame({"A": ["111.1"], "B": ["222.2"], "C": ["333.3"]})
        h_valid, h_info = table_validator.verify_table_numeric_integrity(
            hallucinated_df, pdf_path, page_idx=0, bbox=vis_bbox, min_match_ratio=0.5
        )
        self.assertFalse(h_valid)
        self.assertIn("numeric_integrity_failed", h_info["reason"])

    def test_numeric_tokens_range_and_comma_normalization(self):
        """测试区间连字符 (10-20, 10–20, 10~20) 与多千分位逗号的数字规范化。"""
        tokens1 = table_validator._extract_numeric_tokens("10-20")
        self.assertEqual(tokens1, ["10", "20"])

        tokens2 = table_validator._extract_numeric_tokens("10–20")
        self.assertEqual(tokens2, ["10", "20"])

        tokens3 = table_validator._extract_numeric_tokens("10~20")
        self.assertEqual(tokens3, ["10", "20"])

        tokens4 = table_validator._extract_numeric_tokens("1,234,567.89")
        self.assertEqual(len(tokens4), 1)

    def test_column_deduplication_collision_protection(self):
        """测试列名去重算法在遇到类似 ['Col', 'Col', 'Col_1'] 时绝不生成重复列名。"""
        cols = ["Col", "Col", "Col_1"]
        unique_cols = ocr_client._make_columns_unique(cols)
        self.assertEqual(len(unique_cols), len(set(unique_cols)))
        self.assertEqual(unique_cols, ["Col", "Col_1", "Col_1_1"])

    def test_mineru_continuation_table_detection(self):
        """测试 MinerU 结构化提取中正确识别并标记 (continued) 续表元数据。"""
        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        content_list = [
            {
                "type": "table",
                "table_body": html,
                "page_idx": 1,
                "table_caption": "Table 1 (continued) Geochemical data",
            }
        ]
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w") as zf:
            zf.writestr("content_list.json", json.dumps(content_list))
        dfs = ocr_client.extract_mineru_structured_archive(bio.getvalue())

        self.assertEqual(len(dfs), 1)
        self.assertTrue(dfs[0].attrs.get("is_continuation"))
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")

    @patch("ocr_client.requests.post")
    def test_zero_token_agent_api_failover_when_aistudio_fails(self, mock_post):
        """测试当未配置 MINERU_API_KEY 且 AIStudio 返回 500 时，零 Token 文档自动熔断至 MinerU Agent API。"""
        # 1. 模拟 AIStudio 第一次调用返回 500
        aistudio_resp = MagicMock()
        aistudio_resp.status_code = 500
        aistudio_resp.text = "Internal Server Error"

        # 2. 模拟 MinerU Agent API 提交成功
        agent_resp = MagicMock()
        agent_resp.status_code = 200
        agent_resp.json.return_value = {"code": 0, "data": "agent_failover_123"}

        mock_post.side_effect = [aistudio_resp, agent_resp]

        # 创建 1 页微型临时 PDF
        doc = fitz.open()
        doc.new_page(width=400, height=500)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = f.name
        doc.save(pdf_path)
        doc.close()

        try:
            with patch.dict(os.environ, {"MINERU_API_KEY": "", "OCR_ENGINE": "auto"}):
                with patch("ocr_client.call_mineru_api", return_value="| Col1 |\n|---|\n| val |") as mock_call_mineru:
                    res = ocr_client.call_paddleocr_job(
                        pdf_path,
                        model="PaddleOCR-VL-1.6",
                        config={"OCR_ENGINE": "auto", "PADDLEOCR_VL_TOKEN": "tok"},
                    )
                    self.assertTrue(res.get("success"))
                    self.assertTrue(res.get("failover"))
                    mock_call_mineru.assert_called_once()
        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)


if __name__ == "__main__":
    unittest.main()
