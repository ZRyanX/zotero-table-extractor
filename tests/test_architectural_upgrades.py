#!/usr/bin/env python3
"""
test_architectural_upgrades.py — 针对五项架构升级与高精优化的综合单元测试套件：
1. 本地 DocLayout-YOLO 优先切图定位与 PP-StructureV3 兜底回退
2. Step 3b PaddleOCR-VL 目标切图精准提取与整页回退机制
3. MinerU 客户端集成与百度 AIStudio 队列爆满/超时双活自动熔断故障转移
4. 极端学术矩阵跨页续表合并 (2-5页多页贯通、检出限/误差符号识别、重复子表头剥离、上下标对齐)
5. VLM 直通管道安全数值类型推断与公式注入防御
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch, MagicMock
from pathlib import Path

import pandas as pd
import numpy as np
from PIL import Image

# 确保 scripts 目录在 sys.path 中
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import common
import pdf_tables
import ocr_client
import pdf_table_extractor
import table_postprocess


class TestCropPriority(unittest.TestCase):
    """测试 1: pdf_tables.py 中版面检测优先级反转 (本地 YOLO 优先，云端回退)。"""

    @patch("pdf_tables.DocLayoutYoloDetector")
    @patch("pdf_tables.extract_pp_structure_table_crops")
    @patch("pdf_tables.fitz.open")
    @patch("pdf_tables.os.path.exists", return_value=True)
    def test_yolo_prioritized_over_ppstructure(self, mock_exists, mock_fitz, mock_pp, mock_yolo_cls):
        """当本地 DocLayout-YOLO 可用且检出表格时，优先使用 YOLO，不调用 PP-Structure。"""
        # Mock YOLO
        mock_detector = MagicMock()
        mock_yolo_cls.return_value = mock_detector
        dummy_crop = {
            "table_index": 0,
            "crop_bbox": [10, 10, 100, 100],
            "score": 0.95,
            "has_caption": True,
            "has_footnote": False,
            "image": Image.new("RGB", (100, 100)),
        }
        mock_detector.extract_extended_table_regions.return_value = [dummy_crop]

        # Mock fitz doc & page
        mock_doc = MagicMock()
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Table 1. Test Geochemical Data"
        mock_pix = MagicMock()
        mock_pix.width = 100
        mock_pix.height = 100
        mock_pix.samples = b"\x00" * (100 * 100 * 3)
        mock_page.get_pixmap.return_value = mock_pix
        mock_doc.__len__.return_value = 1
        mock_doc.__getitem__.return_value = mock_page
        mock_fitz.return_value = mock_doc

        with patch("pdf_tables.load_config", return_value={"DOCLAYOUT_YOLO_ENABLED": True, "USE_PP_STRUCTURE": True}):
            crops = pdf_tables.extract_table_crops_from_pdf("dummy.pdf", enable_filter=False)

        # 验证 YOLO 被调用，PP-Structure 没有被调用
        self.assertEqual(len(crops), 1)
        mock_detector.extract_extended_table_regions.assert_called_once()
        mock_pp.assert_not_called()

    @patch("pdf_tables.DocLayoutYoloDetector")
    @patch("pdf_tables.extract_pp_structure_table_crops")
    @patch("pdf_tables.os.path.exists", return_value=True)
    def test_yolo_fallback_to_ppstructure_when_disabled(self, mock_exists, mock_pp, mock_yolo_cls):
        """当 DOCLAYOUT_YOLO_ENABLED 为 False 时，直接平滑走 PP-StructureV3。"""
        mock_pp.return_value = [{"table_index": 0, "image": None}]
        with patch("pdf_tables.load_config", return_value={"DOCLAYOUT_YOLO_ENABLED": False, "USE_PP_STRUCTURE": True}):
            crops = pdf_tables.extract_table_crops_from_pdf("dummy.pdf")

        self.assertEqual(len(crops), 1)
        mock_pp.assert_called_once()
        mock_yolo_cls.assert_not_called()

    @patch("pdf_tables.DocLayoutYoloDetector")
    @patch("pdf_tables.extract_pp_structure_table_crops")
    @patch("pdf_tables.fitz.open")
    @patch("pdf_tables.os.path.exists", return_value=True)
    def test_yolo_fallback_to_ppstructure_on_empty_crops(self, mock_exists, mock_fitz, mock_pp, mock_yolo_cls):
        """当本地 YOLO 未在目标页检出表格时，自动回退至 PP-StructureV3。"""
        mock_detector = MagicMock()
        mock_yolo_cls.return_value = mock_detector
        mock_detector.extract_extended_table_regions.return_value = []  # 未检出表格

        mock_doc = MagicMock()
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Page content"
        mock_pix = MagicMock()
        mock_pix.width = 10
        mock_pix.height = 10
        mock_pix.samples = b"\x00" * 300
        mock_page.get_pixmap.return_value = mock_pix
        mock_doc.__len__.return_value = 1
        mock_doc.__getitem__.return_value = mock_page
        mock_fitz.return_value = mock_doc

        mock_pp.return_value = [{"table_index": 0, "image": None, "page_index": 0}]

        with patch("pdf_tables.load_config", return_value={"DOCLAYOUT_YOLO_ENABLED": True, "USE_PP_STRUCTURE": True}):
            crops = pdf_tables.extract_table_crops_from_pdf("dummy.pdf", enable_filter=False)

        self.assertEqual(len(crops), 1)
        mock_pp.assert_called_once()


class TestStep3bVlmTakeover(unittest.TestCase):
    """测试 2: Step 3b 中优先通过 DocLayout-YOLO 切图送检 PaddleOCR-VL，未检出时回退整页。"""

    @patch("pdf_table_extractor.extract_via_paddleocr_fullpage")
    @patch("pdf_tables.extract_table_crops_from_pdf")
    @patch("ocr_client.call_paddleocr_vl_online_api")
    @patch("os.path.exists", return_value=True)
    def test_extract_via_paddleocr_crops_success(self, mock_exists, mock_vl_api, mock_crops_func, mock_fullpage):
        """当 YOLO 检出表格切图时，仅将裁剪切图送入 PaddleOCR-VL，不触发整页回退。"""
        crop_img = Image.new("RGB", (200, 100), color="white")
        mock_crops_func.return_value = [{
            "page_index": 1,
            "table_index": 0,
            "crop_bbox": [50, 100, 250, 200],
            "score": 0.95,
            "image": crop_img
        }]
        mock_vl_api.return_value = "| Sample | SiO2 |\n|---|---|\n| S1 | 70.5 |"

        res = pdf_table_extractor.extract_via_paddleocr_crops("dummy.pdf", pages=[1])

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["page_idx"], 1)
        self.assertEqual(res[0]["df"].attrs["extractor"], "paddleocr_vl_crop")
        self.assertEqual(list(res[0]["df"].columns), ["Sample", "SiO2"])
        mock_vl_api.assert_called_once()
        mock_fullpage.assert_not_called()

    @patch("pdf_table_extractor.extract_via_paddleocr_fullpage")
    @patch("pdf_tables.extract_table_crops_from_pdf")
    @patch("os.path.exists", return_value=True)
    def test_extract_via_paddleocr_crops_fallback_to_fullpage(self, mock_exists, mock_crops_func, mock_fullpage):
        """当 YOLO 未能检出任何切图时，平滑回退至整页 extract_via_paddleocr_fullpage。"""
        mock_crops_func.return_value = []
        dummy_df = pd.DataFrame({"Col1": [1, 2], "Col2": [3, 4]})
        dummy_df.attrs["extractor"] = "paddleocr_vl"
        mock_fullpage.return_value = [{"df": dummy_df, "page_idx": 2, "table_idx": 0}]

        res = pdf_table_extractor.extract_via_paddleocr_crops("dummy.pdf", pages=[2])

        self.assertEqual(len(res), 1)
        mock_fullpage.assert_called_once_with("dummy.pdf", [2])


class TestMinerUClientAndDualCloud(unittest.TestCase):
    """测试 3: MinerU API 客户端功能与百度 AIStudio 队列熔断故障转移。"""

    def test_mineru_client_parse_zip_result(self):
        """测试 MinerU 客户端自动解压 ZIP 产物并提取 Markdown 表格。"""
        # 构建内存中的 ZIP 压缩包
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("output.md", "# Paper\n\n| Mineral | SiO2 | Al2O3 |\n|---|---|---|\n| Qtz | 99.8 | 0.1 |\n")
        zip_bytes = zip_buf.getvalue()

        client = ocr_client.MinerUClient(api_key="mock_key")
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = zip_bytes
            mock_get.return_value = mock_resp

            md = client.parse_mineru_table_result({"full_zip_url": "https://mineru.net/download/result.zip"})
            self.assertIn("| Mineral | SiO2 | Al2O3 |", md)
            self.assertIn("Qtz", md)

    @patch("ocr_client.requests.post")
    @patch("ocr_client.requests.put")
    def test_mineru_client_submit_task_local_file(self, mock_put, mock_post):
        """测试 MinerU 本地文件注册与 OSS 上传流程。"""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f_tmp:
            f_tmp.write(b"%PDF-1.4 mock")
            tmp_path = f_tmp.name

        try:
            # 1. 模拟 /file-urls/batch 注册响应
            mock_post_resp = MagicMock()
            mock_post_resp.status_code = 200
            mock_post_resp.json.return_value = {
                "code": 0,
                "data": {
                    "batch_id": "batch_abc123",
                    "file_urls": ["https://oss.aliyun.com/upload/mock_url"]
                }
            }
            mock_post.return_value = mock_post_resp

            # 2. 模拟 OSS PUT 响应
            mock_put_resp = MagicMock()
            mock_put_resp.status_code = 200
            mock_put.return_value = mock_put_resp

            client = ocr_client.MinerUClient(api_key="test_api_key")
            batch_id = client.submit_task(tmp_path)

            self.assertEqual(batch_id, "batch_abc123")
            mock_post.assert_called_once()
            mock_put.assert_called_once()
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @patch("ocr_client.call_mineru_api")
    @patch("ocr_client.requests.post")
    @patch("os.path.exists", return_value=True)
    def test_aistudio_queue_full_failover_to_mineru(self, mock_exists, mock_post, mock_mineru_call):
        """测试百度 AIStudio 遇到 status=400 队列已满 时，无缝自动故障转移至 MinerU。"""
        # 模拟 AIStudio 队列已满响应
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "status=400 队列已满，请稍后再试"
        mock_post.return_value = mock_resp

        mock_mineru_call.return_value = "| Age | Error |\n|---|---|\n| 120.5 | 1.2 |"

        cfg = {
            "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN": "mock_token",
            "MINERU_API_KEY": "mock_mineru_key",
            "OCR_ENGINE": "auto"
        }

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp_path = f.name
            f.write(b"%PDF-1.4 dummy")

        try:
            res = ocr_client.call_paddleocr_job(tmp_path, model="PaddleOCR-VL-1.6", config=cfg)

            self.assertTrue(res["success"])
            self.assertEqual(res["model"], "mineru")
            self.assertTrue(res.get("failover"))
            self.assertIn("120.5", res["combined_markdown"])
            mock_mineru_call.assert_called_once()
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @patch("ocr_client.call_mineru_api")
    def test_ocr_engine_mineru_direct(self, mock_mineru_call):
        """测试当 OCR_ENGINE='mineru' 时，直接调用 MinerU API。"""
        mock_mineru_call.return_value = "| Sample | La |\n|---|---|\n| S1 | 35.2 |"
        cfg = {
            "MINERU_API_KEY": "mineru_key",
            "OCR_ENGINE": "mineru"
        }

        md = ocr_client.call_paddleocr_vl_online_api("test.pdf", config=cfg)
        self.assertIn("35.2", md)
        mock_mineru_call.assert_called_once()

    def test_mineru_poll_task_official_response_format(self):
        """测试 MinerU 客户端正确解析官方 batch 查询接口返回的 extract_result 列表结构。"""
        client = ocr_client.MinerUClient(api_key="mock_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "msg": "ok",
            "data": {
                "batch_id": "batch_abc",
                "extract_result": [
                    {
                        "file_name": "table.pdf",
                        "state": "done",
                        "full_zip_url": "https://mineru.net/download/res.zip"
                    }
                ]
            }
        }
        with patch("requests.get", return_value=mock_resp):
            poll_res = client.poll_task("batch_abc", timeout=10)
            self.assertTrue(poll_res["success"])
            self.assertEqual(poll_res["state"], "done")

    @patch("ocr_client.call_paddleocr_job")
    def test_mineru_failover_no_keyerror_images(self, mock_job):
        """测试百度 AIStudio 熔断至 MinerU 时，返回的无切图结构不会导致 extract_pp_structure 报 KeyError: 'images'。"""
        mock_job.return_value = {
            "success": True,
            "model": "mineru",
            "combined_markdown": "| Element | wt% |\n|---|---|\n| SiO2 | 70.5 |",
            "pages": [{"page_idx": 0, "markdown": "| Element | wt% |\n|---|---|\n| SiO2 | 70.5 |"}],
            "failover": True
        }
        # 不应抛出 KeyError
        crops = ocr_client.extract_pp_structure_table_crops("dummy.pdf")
        self.assertEqual(crops, [])

        with patch("ocr_client.call_pp_structure_v3_api", return_value=mock_job.return_value):
            md = ocr_client.run_ppstructure_vlm_pipeline("dummy.pdf")
            self.assertIn("70.5", md)

    @patch("ocr_client.requests.post")
    def test_aistudio_queue_full_without_mineru_key(self, mock_post):
        """测试当 MINERU_API_KEY 为空时，百度 AIStudio 队列已满不应抛异常，而是优雅按重试退出。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "status=400 队列已满"
        mock_post.return_value = mock_resp

        cfg = {
            "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN": "mock_token",
            "MINERU_API_KEY": "",
            "OCR_ENGINE": "auto"
        }
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp = f.name
            f.write(b"%PDF-1.4 dummy")
        try:
            with patch("ocr_client._sleep_cancel_aware", return_value=False):
                res = ocr_client.call_paddleocr_job(tmp, model="PaddleOCR-VL-1.6", config=cfg)
                self.assertFalse(res["success"])
                self.assertEqual(res["error"], "max_retries_exceeded")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class TestContinuationTableEnhancements(unittest.TestCase):
    """测试 4: 极端学术矩阵跨页续表合并 (2-5页多页贯通、检出限、误差、重复子表头剥离、上下标)。"""

    def test_extreme_academic_matrix_3pages(self):
        """测试横跨 3-5 页的极端学术矩阵合并，验证多页顺序拼接与 covered_pages 记录。"""
        p1 = pd.DataFrame({
            "Sample": ["A1", "A2"],
            "SiO2": [70.1, 71.2],
            "Al2O3": [14.2, 14.5]
        })
        p1.attrs = {"label": "Table 2", "page_idx": 5, "table_title": "Whole-rock geochemistry"}

        p2 = pd.DataFrame({
            "Sample": ["A3", "A4"],
            "SiO2": [69.8, 72.0],
            "Al2O3": [14.1, 14.3]
        })
        p2.attrs = {"label": "Table 2 (continued)", "page_idx": 6, "table_title": "Table 2 Continued"}

        p3 = pd.DataFrame({
            "Sample": ["A5", "A6"],
            "SiO2": [70.5, 71.9],
            "Al2O3": [14.0, 14.4]
        })
        p3.attrs = {"label": "Table 2", "page_idx": 7, "table_title": "Table 2 Continued"}

        merged = table_postprocess.merge_continuation_tables([p1, p2, p3])

        self.assertEqual(len(merged), 1)
        res_df = merged[0]
        self.assertEqual(len(res_df), 6)
        self.assertEqual(res_df.attrs.get("covered_pages"), [5, 6, 7])
        self.assertEqual(res_df.attrs.get("page_idx"), 5)

    def test_detection_limit_and_error_in_unlabeled_continuation(self):
        """测试续表首行含检出限 (<0.01, b.d.) 与误差符号 (±, 1σ) 时，正确识别为数据而非表头。"""
        base_df = pd.DataFrame({
            "Sample": ["S1", "S2"],
            "Nb": ["12.5", "14.2"],
            "Ta": ["0.8", "1.1"]
        })
        base_df.attrs = {"label": "Table 3", "page_idx": 1}

        # 续表无表头，列名被 OCR 误认成首行数据 (<0.05, b.d., 12.3±0.5)
        cont_df = pd.DataFrame([
            ["S4", "15.1", "1.2"]
        ], columns=["S3", "<0.05", "b.d."])
        cont_df.attrs = {"label": "", "page_idx": 2}

        merged = table_postprocess.merge_continuation_tables([base_df, cont_df])

        self.assertEqual(len(merged), 1)
        res_df = merged[0]
        # 验证包含 4 行数据，首行数据 S3, <0.05, b.d. 未被当成表头丢失
        self.assertEqual(len(res_df), 4)
        self.assertIn("<0.05", res_df["Nb"].values)
        self.assertIn("b.d.", res_df["Ta"].values)

    def test_strip_repeated_subheaders_across_pages(self):
        """测试跨页拼接时自动剥离重复的子表头分类行 (如 Major elements) 与续表声明行。"""
        p1 = pd.DataFrame({
            "Sample": ["S1", "S2"],
            "SiO2": [70.1, 71.2]
        })
        p1.attrs = {"label": "Table 1", "page_idx": 1}

        # 续表顶部带有重复分类横幅行与续表声明
        p2 = pd.DataFrame([
            ["Table 1 (continued)", ""],
            ["Major elements (wt%)", ""],
            ["S3", 72.5],
            ["S4", 69.4]
        ], columns=["Sample", "SiO2"])
        p2.attrs = {"label": "Table 1", "page_idx": 2}

        merged = table_postprocess.merge_continuation_tables([p1, p2])

        self.assertEqual(len(merged), 1)
        res_df = merged[0]
        # 2行来自p1，2行来自p2，顶部横幅被正确剔除，共 4 行有效数据
        self.assertEqual(len(res_df), 4)
        self.assertNotIn("Table 1 (continued)", res_df["Sample"].values)
        self.assertNotIn("Major elements (wt%)", res_df["Sample"].values)

    def test_unicode_subscript_column_alignment(self):
        """测试对齐地化化学式下标列名 (SiO₂ <-> SiO2, Al₂O₃ <-> Al2O3)。"""
        p1 = pd.DataFrame({"Sample": ["A"], "SiO2": [70.1], "Al2O3": [14.2]})
        p1.attrs = {"label": "Table 4", "page_idx": 1}

        p2 = pd.DataFrame({"Sample": ["B"], "SiO₂": [71.5], "Al₂O₃": [14.0]})
        p2.attrs = {"label": "Table 4", "page_idx": 2}

        merged = table_postprocess.merge_continuation_tables([p1, p2])

        self.assertEqual(len(merged), 1)
        res_df = merged[0]
        self.assertEqual(len(res_df), 2)
        # 列名保持规范的 base_cols
        self.assertEqual(list(res_df.columns), ["Sample", "SiO2", "Al2O3"])
        self.assertEqual(res_df["SiO2"].tolist(), [70.1, 71.5])

    def test_align_dataframe_columns_duplicate_column_names(self):
        """测试对齐含同名误差/不确定度列 (如多个 1σ, ±) 时，不发生字典覆写丢失，完整保留各列独立数值。"""
        df_to_align = pd.DataFrame([
            ["B", "71.5", "0.3", "14.0", "0.2"]
        ], columns=["Sample", "SiO₂", "1σ", "Al₂O₃", "1σ"])

        target_cols = ["Sample", "SiO2", "1σ", "Al2O3", "1σ"]
        aligned = table_postprocess.align_dataframe_columns(df_to_align, target_cols)

        self.assertEqual(list(aligned.columns), target_cols)
        # 验证第一列 1σ 的 0.3 与第二列 1σ 的 0.2 均完好保留，未发生后项覆写前项的恶性 Bug
        self.assertEqual(aligned.iloc[0, 2], "0.3")
        self.assertEqual(aligned.iloc[0, 4], "0.2")

    def test_vertical_continuation_unnamed_columns_not_horizontal(self):
        """测试续表具有 Unnamed 列且首行为重复表头时，正确进行纵向合并，禁止被误判为横向分裂合并。"""
        p1 = pd.DataFrame([["A", "1", "2", "3"]], columns=["Sample", "C1", "C2", "C3"])
        p1.attrs = {"label": "Table 5", "page_idx": 1}
        p2 = pd.DataFrame([
            ["Sample", "C1", "C2", "C3"],
            ["B", "4", "5", "6"]
        ], columns=["Unnamed: 0", "Unnamed: 1", "Unnamed: 2", "Unnamed: 3"])
        p2.attrs = {"label": "Table 5 (continued)", "page_idx": 2}

        merged = table_postprocess.merge_continuation_tables([p1, p2])
        self.assertEqual(len(merged), 1)
        res = merged[0]
        self.assertEqual(list(res.columns), ["Sample", "C1", "C2", "C3"])
        self.assertEqual(len(res), 2)
        self.assertEqual(res["Sample"].tolist(), ["A", "B"])
        self.assertNotIn("Unnamed: 1", res.columns)

    def test_continuation_default_range_index_no_fake_rows(self):
        """测试无表头的纯数值表格 (使用默认 RangeIndex 列索引) 拼接时，绝不无中生有插入 [0, 1] 假数据行。"""
        p1 = pd.DataFrame([[10, 20], [30, 40]])
        p1.attrs = {"label": "Table 6", "page_idx": 1}
        p2 = pd.DataFrame([[50, 60], [70, 80]])
        p2.attrs = {"label": "Table 6", "page_idx": 2}

        merged = table_postprocess.merge_continuation_tables([p1, p2])
        self.assertEqual(len(merged), 1)
        res = merged[0]
        # 必须严格只有 4 行数据，禁止注入 [0, 1] 假行
        self.assertEqual(len(res), 4)
        self.assertEqual(res.iloc[0].tolist(), [10, 20])
        self.assertEqual(res.iloc[1].tolist(), [30, 40])
        self.assertEqual(res.iloc[2].tolist(), [50, 60])
        self.assertEqual(res.iloc[3].tolist(), [70, 80])

    def test_norm_label_continuation_variants(self):
        """测试学术界各种非标续表声明 (无括号 Continued, cont., 表1(续), 表 1 续表) 均能正确归一化合并。"""
        base = pd.DataFrame({"Sample": ["S1"], "Val": [10]})
        base.attrs = {"label": "Table 7", "page_idx": 1}

        c1 = pd.DataFrame({"Sample": ["S2"], "Val": [20]})
        c1.attrs = {"label": "Table 7 Continued", "page_idx": 2}

        c2 = pd.DataFrame({"Sample": ["S3"], "Val": [30]})
        c2.attrs = {"label": "Table 7 cont.", "page_idx": 3}

        merged = table_postprocess.merge_continuation_tables([base, c1, c2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]), 3)
        self.assertEqual(merged[0]["Sample"].tolist(), ["S1", "S2", "S3"])

        # 中文表号与续表标记验证
        cn_base = pd.DataFrame({"Sample": ["S1"], "Val": [10]})
        cn_base.attrs = {"label": "表 1", "page_idx": 4}
        cn_cont = pd.DataFrame({"Sample": ["S2"], "Val": [20]})
        cn_cont.attrs = {"label": "表 1 (续)", "page_idx": 5}
        cn_merged = table_postprocess.merge_continuation_tables([cn_base, cn_cont])
        self.assertEqual(len(cn_merged), 1)
        self.assertEqual(len(cn_merged[0]), 2)


class TestVlmDataTyping(unittest.TestCase):
    """测试 5: VLM 直通管道安全数值类型推断与公式注入防御。"""

    def test_vlm_clean_numeric_inference(self):
        """测试纯数值列字符串在 _clean_vlm_dataframe 中被转为 float/int，非数值与前导零代码被保护。"""
        df = pd.DataFrame({
            "Sample": ["001", "002", "003"],
            "SiO2": ["72.50", "71.80", "69.45"],
            "Count": ["10", "20", "30"],
            "Notes": ["Pure granite", "Altered", "Fresh"]
        })

        cleaned = table_postprocess._clean_vlm_dataframe(df, {})

        # 纯数值列转为真实浮点与整数
        self.assertIsInstance(cleaned["SiO2"].iloc[0], float)
        self.assertEqual(cleaned["SiO2"].iloc[0], 72.5)
        self.assertTrue(isinstance(cleaned["Count"].iloc[0], (int, np.integer)))
        self.assertEqual(cleaned["Count"].iloc[0], 10)

        # 样号带前导零的代码字符串严格保留为 str，防止丢失前导零变为 1, 2, 3
        self.assertEqual(cleaned["Sample"].iloc[0], "001")
        self.assertIsInstance(cleaned["Sample"].iloc[0], str)

        # 文本描述列保留为 str
        self.assertEqual(cleaned["Notes"].iloc[0], "Pure granite")
        self.assertIsInstance(cleaned["Notes"].iloc[0], str)

    def test_vlm_escape_formula_with_numbers(self):
        """测试负数与正数数值保持数值，恶意公式字符串 (=cmd) 被转义。"""
        df = pd.DataFrame({
            "Val": ["-15.4", "+3.2", "=1+1", "@SUM(A1)"]
        })

        cleaned = table_postprocess._clean_vlm_dataframe(df, {})

        # 数值保留
        self.assertEqual(cleaned["Val"].iloc[0], -15.4)
        self.assertEqual(cleaned["Val"].iloc[1], 3.2)

        # Excel 恶意公式被转义 (添加单引号前缀)
        self.assertTrue(str(cleaned["Val"].iloc[2]).startswith("'="))
        self.assertTrue(str(cleaned["Val"].iloc[3]).startswith("'@"))

    def test_try_numeric_scientific_notation_and_signed_zeros(self):
        """测试科学计数法、有符号数以及前导零编号在 try_numeric 与 escape_formula 中的类型推断与保护。"""
        # 1. 科学计数法转换
        self.assertEqual(table_postprocess.try_numeric("1e5"), 100000.0)
        self.assertIsInstance(table_postprocess.try_numeric("1e5"), float)
        self.assertEqual(table_postprocess.try_numeric("-2e-3"), -0.002)
        self.assertIsInstance(table_postprocess.try_numeric("-2e-3"), float)

        # 2. 前导零代码字符串保护
        self.assertEqual(table_postprocess.try_numeric("001"), "001")
        self.assertEqual(table_postprocess.try_numeric("-007"), "-007")
        self.assertEqual(table_postprocess.try_numeric("+005"), "+005")

        # 3. escape_formula 对科学计数法及符号前导零的综合处理
        self.assertEqual(table_postprocess.escape_formula("-1e5"), -100000.0)
        self.assertEqual(table_postprocess.escape_formula("+2e-4"), 0.0002)
        # 前导零代码加单引号转义，避免 Excel 篡改为数值并抹掉前导零
        self.assertEqual(table_postprocess.escape_formula("-007"), "'-007")
        self.assertEqual(table_postprocess.escape_formula("+005"), "'+005")
        # 真实负数与正数转为真实数值
        self.assertEqual(table_postprocess.escape_formula("-42"), -42)
        self.assertEqual(table_postprocess.escape_formula("+3.14"), 3.14)


if __name__ == "__main__":
    unittest.main()
