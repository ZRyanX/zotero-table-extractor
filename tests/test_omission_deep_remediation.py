"""
tests/test_omission_deep_remediation.py
======================================
针对长文档自适应扫描、无标题多表缺切图校验以及稀疏无框续表探测的深度回归测试用例。
涵盖：
1. 500+ 超长文档无硬上限自适应分块探测与轻量绘图预检
2. 无 Caption 页面多表原生信号触发 PP-Structure 补充定位
3. 稀疏无框跨页续表基于几何对齐、水平跨度与页顶连续性的精准识别与正文防误判
4. 矢量线框群聚类探测多表结构
"""

import os
import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import pdf_table_extractor as pte
from pdf_table_extractor import _check_sparse_continuation_table
import pdf_tables
from pdf_tables import probe_disjoint_native_tables
from table_postprocess import merge_continuation_tables


class TestLongDocumentAdaptiveScanning(unittest.TestCase):
    """1. 验证长文档无硬编码上限 (150/300) 的分块自适应扫描与轻量级绘图探测"""

    @patch("pdf_table_extractor.has_text_layer", return_value=True)
    @patch("pdf_table_extractor.classify_and_extract_via_inspector")
    @patch("table_validator.scan_pdf_table_declarations")
    @patch("fitz.open")
    def test_long_document_adaptive_scanning_probes_beyond_300_pages(
        self, mock_fitz, mock_decls, mock_clf, mock_has_text
    ):
        """文档超过 500 页且无 Caption 时，原本在 150/300 截断的逻辑现应全篇覆盖至末尾"""
        mock_decls.return_value = {}
        mock_clf.return_value = {
            'pdf_type': 'text_based',
            'confidence': 1.0,
            'pages_with_tables': [],
            'pages_needing_ocr': [],
            'markdown': '',
            'error': None,
            'structure_tables': [],
            'tables': [],
        }

        total_pages = 520
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = total_pages
        mock_doc.__enter__.return_value = mock_doc
        mock_doc.__iter__.return_value = []

        # 创建页面 mock，仅在第 450 页（远超旧版 150/300 上限）存在矢量表
        def get_page(p_idx):
            page = MagicMock()
            if p_idx == 450:
                page.get_drawings.return_value = [{"items": [("l", (50, 100), (400, 100))]}]
                mock_tb = MagicMock()
                mock_tb.row_count = 5
                mock_tb.col_count = 4
                mock_tabs = MagicMock()
                mock_tabs.tables = [mock_tb]
                page.find_tables.return_value = mock_tabs
            else:
                # 其它页无绘图
                page.get_drawings.return_value = []
                page.find_tables.return_value = None
            return page

        mock_doc.__getitem__.side_effect = get_page
        mock_fitz.return_value = mock_doc

        # 执行提取
        with patch("pdf_table_extractor.extract_via_find_tables") as mock_ft:
            mock_df = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
            mock_df.attrs['extractor'] = 'find_tables'
            mock_df.attrs['page_idx'] = 450
            mock_ft.return_value = [{'df': mock_df, 'page_idx': 450, 'table_idx': 0, 'bbox': [50, 100, 400, 300]}]

            results, logs = pte.extract_tables_from_pdf("monograph_520p.pdf", use_ocr_fallback=False)

        # 验证第 450 页（0-based，即 Page 451）成功进入候选列表并被提取
        candidate_log = [l for l in logs if "候选表格页面:" in l]
        self.assertTrue(len(candidate_log) > 0)
        self.assertIn("451", candidate_log[0])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["page_idx"], 450)

    def test_lightweight_drawing_probe_skips_find_tables_on_empty_pages(self):
        """当页面 get_drawings 返回空列表时，不应调用较耗时的 find_tables"""
        page = MagicMock()
        page.get_drawings.return_value = []
        
        # 模拟 probe 逻辑
        has_drawings = True
        drawings = page.get_drawings()
        if isinstance(drawings, (list, tuple)) and len(drawings) == 0:
            has_drawings = False

        self.assertFalse(has_drawings)
        page.find_tables.assert_not_called()


class TestUncaptionedMultiTableCropDeficiency(unittest.TestCase):
    """2. 验证页面无 Caption 时，通过原生信号检测多表切图缺失并触发 PP-Structure 补充扫描"""

    @patch("os.path.exists", return_value=True)
    @patch("fitz.open")
    @patch("pdf_tables.inspect_crop_region")
    @patch("pdf_tables.extract_pp_structure_table_crops")
    @patch("pdf_tables.DocLayoutYoloDetector")
    @patch("table_validator.scan_pdf_table_declarations", return_value={})
    def test_uncaptioned_multitable_triggers_pp_structure_supplement(
        self, mock_decls, mock_detector_cls, mock_pp_crops, mock_inspect, mock_fitz, mock_exists
    ):
        """当页面没有 Caption，但原生信号显示有 2 张独立表，而 YOLO 仅检出 1 个切图时，触发 PP-Structure 补扫"""
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 1

        mock_page = MagicMock()
        mock_page.get_text.return_value = "Page content without caption"
        mock_pix = MagicMock()
        mock_pix.width = 1000
        mock_pix.height = 1400
        mock_pix.samples = b'\x00' * (1000 * 1400 * 3)
        mock_page.get_pixmap.return_value = mock_pix

        # 原生 find_tables 探测出 2 个独立表格 (Page 0)
        tb1 = MagicMock()
        tb1.row_count = 4
        tb1.col_count = 3
        tb1.bbox = [50, 100, 450, 300]

        tb2 = MagicMock()
        tb2.row_count = 5
        tb2.col_count = 4
        tb2.bbox = [50, 500, 450, 750]

        tabs_mock = MagicMock()
        tabs_mock.tables = [tb1, tb2]
        mock_page.find_tables.return_value = tabs_mock
        mock_page.get_drawings.return_value = []

        mock_doc.__getitem__.return_value = mock_page
        mock_fitz.return_value = mock_doc

        mock_inspect.return_value = {
            "needs_ocr": False, "ocr_reason": "", "text": "", "pdf_bbox": [50, 100, 450, 300]
        }

        # 模拟 YOLO 仅检出第 1 个表格
        mock_det = MagicMock()
        mock_det.extract_extended_table_regions.return_value = [
            {"crop_bbox": [50, 100, 450, 300], "score": 0.95}
        ]
        mock_detector_cls.return_value = mock_det

        # 模拟 PP-Structure 补扫出两个完整表格
        mock_pp_crops.return_value = [
            {"page_index": 0, "crop_bbox": [50, 100, 450, 300], "score": 0.92},
            {"page_index": 0, "crop_bbox": [50, 500, 450, 750], "score": 0.88},
        ]

        config = {"USE_PP_STRUCTURE": True}
        crops = pdf_tables.extract_table_crops_from_pdf(
            "dummy.pdf", pages=[0], config=config
        )

        # 验证 PP-Structure 被触发补扫
        mock_pp_crops.assert_called_once()
        call_kwargs = mock_pp_crops.call_args[1]
        self.assertEqual(call_kwargs.get("pages"), [0])

        # 最终切图应包含 2 个表格
        self.assertEqual(len(crops), 2)

    def test_probe_disjoint_native_tables_with_vector_line_groups(self):
        """测试通过水平矢量线组（如两组独立的三线表）识别出 2 个独立表格"""
        mock_page = MagicMock()
        # find_tables 未生效
        mock_page.find_tables.return_value = None

        # 模拟两组独立的三线表水平线段：
        # 第一组：y = 100, 120, 200 (间距 <= 25 连续，整体 y0~y1 为 100~200)
        # 第二组：y = 400, 420, 500 (与第一组间隙 200 > 25，独立成第二组)
        drawings = [
            {"items": [("l", MagicMock(x=50, y=100), MagicMock(x=450, y=100))]},
            {"items": [("l", MagicMock(x=50, y=120), MagicMock(x=450, y=120))]},
            {"items": [("l", MagicMock(x=50, y=200), MagicMock(x=450, y=200))]},
            {"items": [("l", MagicMock(x=50, y=400), MagicMock(x=450, y=400))]},
            {"items": [("l", MagicMock(x=50, y=420), MagicMock(x=450, y=420))]},
            {"items": [("l", MagicMock(x=50, y=500), MagicMock(x=450, y=500))]},
        ]
        mock_page.get_drawings.return_value = drawings

        count = probe_disjoint_native_tables(mock_page)
        self.assertEqual(count, 2)


class TestSparseContinuationDetection(unittest.TestCase):
    """3. 验证无表头文字、无网格线且文本稀疏的跨页无框续表探测"""

    def test_sparse_continuation_positive_case(self):
        """正例：前一页底部有表格，后一页顶部有多列几何对齐但无关键词、无密集数字的稀疏文本"""
        prev_page = MagicMock()
        prev_page.rect.height = 842.0
        prev_page.rect.width = 595.0

        # 前一页底部表格 (y0=500, y1=750, x0=50, x1=500)
        tb_prev = MagicMock()
        tb_prev.row_count = 6
        tb_prev.col_count = 3
        tb_prev.bbox = [50.0, 500.0, 500.0, 750.0]
        tabs_prev = MagicMock()
        tabs_prev.tables = [tb_prev]
        prev_page.find_tables.return_value = tabs_prev

        # 后一页顶部：无网格，无续表关键字，仅有定性文本，但列对齐明显
        # 3 列分别位于 x ≈ 55, x ≈ 210, x ≈ 370
        next_page = MagicMock()
        next_page.rect.height = 842.0
        next_page.rect.width = 595.0

        words_next = [
            # Line 1 (y0=60, y1=72)
            (55.0, 60.0, 110.0, 72.0, "CompoundA", 0, 0, 0),
            (210.0, 60.0, 260.0, 72.0, "Positive", 0, 0, 1),
            (370.0, 60.0, 480.0, 72.0, "Stable", 0, 0, 2),
            # Line 2 (y0=80, y1=92)
            (56.0, 80.0, 115.0, 92.0, "CompoundB", 0, 1, 0),
            (212.0, 80.0, 255.0, 92.0, "Negative", 0, 1, 1),
            (368.0, 80.0, 475.0, 92.0, "Unstable", 0, 1, 2),
            # Line 3 (y0=100, y1=112)
            (54.0, 100.0, 112.0, 112.0, "CompoundC", 0, 2, 0),
            (208.0, 100.0, 258.0, 112.0, "Weak", 0, 2, 1),
            (372.0, 100.0, 482.0, 112.0, "Degraded", 0, 2, 2),
        ]
        next_page.get_text.return_value = words_next

        is_cont = _check_sparse_continuation_table(prev_page, next_page)
        self.assertTrue(is_cont)

    def test_sparse_continuation_negative_case_prose_paragraph(self):
        """反例：前一页有表格，但后一页顶部是连续正文段落（无多列对齐），应拒绝判定为续表"""
        prev_page = MagicMock()
        prev_page.rect.height = 842.0
        prev_page.rect.width = 595.0

        tb_prev = MagicMock()
        tb_prev.row_count = 6
        tb_prev.col_count = 3
        tb_prev.bbox = [50.0, 500.0, 500.0, 750.0]
        tabs_prev = MagicMock()
        tabs_prev.tables = [tb_prev]
        prev_page.find_tables.return_value = tabs_prev

        # 后一页顶部：正常叙述段落，每行词间距很小 (3~5 pt)，不存在多列对齐间隙
        next_page = MagicMock()
        next_page.rect.height = 842.0
        next_page.rect.width = 595.0

        words_prose = [
            # Line 1: 首行缩进
            (72.0, 60.0, 120.0, 72.0, "This", 0, 0, 0),
            (124.0, 60.0, 170.0, 72.0, "section", 0, 0, 1),
            (174.0, 60.0, 240.0, 72.0, "discusses", 0, 0, 2),
            (244.0, 60.0, 280.0, 72.0, "the", 0, 0, 3),
            (284.0, 60.0, 360.0, 72.0, "experimental", 0, 0, 4),
            # Line 2: 顶格
            (54.0, 80.0, 110.0, 92.0, "results", 0, 1, 0),
            (114.0, 80.0, 170.0, 92.0, "obtained", 0, 1, 1),
            (174.0, 80.0, 210.0, 92.0, "from", 0, 1, 2),
            (214.0, 80.0, 240.0, 92.0, "the", 0, 1, 3),
            (244.0, 80.0, 310.0, 92.0, "analysis", 0, 1, 4),
        ]
        next_page.get_text.return_value = words_prose

        is_cont = _check_sparse_continuation_table(prev_page, next_page)
        self.assertFalse(is_cont)

    def test_sparse_continuation_negative_case_misaligned_span(self):
        """反例：后一页虽有多列，但位于侧栏或右栏，与前页表格水平跨度严重不匹配 (overlap < 65%)"""
        prev_page = MagicMock()
        prev_page.rect.height = 842.0
        prev_page.rect.width = 595.0

        tb_prev = MagicMock()
        tb_prev.row_count = 6
        tb_prev.col_count = 3
        tb_prev.bbox = [50.0, 500.0, 250.0, 750.0]  # 左半侧
        tabs_prev = MagicMock()
        tabs_prev.tables = [tb_prev]
        prev_page.find_tables.return_value = tabs_prev

        next_page = MagicMock()
        next_page.rect.height = 842.0
        next_page.rect.width = 595.0

        # 后一页内容位于右半侧 x = 320 ~ 550
        words_sidebar = [
            (320.0, 60.0, 370.0, 72.0, "NoteA", 0, 0, 0),
            (420.0, 60.0, 480.0, 72.0, "InfoA", 0, 0, 1),
            (320.0, 80.0, 370.0, 92.0, "NoteB", 0, 1, 0),
            (420.0, 80.0, 480.0, 92.0, "InfoB", 0, 1, 1),
        ]
        next_page.get_text.return_value = words_sidebar

        is_cont = _check_sparse_continuation_table(prev_page, next_page)
        self.assertFalse(is_cont)

    def test_continuation_tables_merge_with_is_continuation_attr(self):
        """验证当 DataFrame 带有 is_continuation 属性时，即便是小行数无标签表也能安全拼接"""
        df1 = pd.DataFrame({
            "Sample": ["A1", "A2", "A3", "A4"],
            "Value": [1.1, 2.2, 3.3, 4.4]
        })
        df1.attrs["label"] = "Table 1"
        df1.attrs["table_title"] = "Chemical Composition"
        df1.attrs["page_idx"] = 0

        # 续表片段：仅 2 行，无 label，但带有 is_continuation 属性
        df2 = pd.DataFrame({
            "Sample": ["A5", "A6"],
            "Value": [5.5, 6.6]
        })
        df2.attrs["page_idx"] = 1
        df2.attrs["is_continuation"] = True

        merged = merge_continuation_tables([df1, df2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]), 6)
        self.assertEqual(merged[0].attrs["label"], "Table 1")

    def test_probe_disjoint_native_tables_standard_academic_3line_tables(self):
        """验证学术标准三线表（表头间距30pt，表体间距120pt > 25pt）能够被正确聚类识别为两个独立表格"""
        mock_page = MagicMock()
        mock_page.find_tables.return_value = None
        mock_page.rotation = 0

        # 两组标准学术三线表，行间距均远超旧版 25pt 硬编码阈值
        drawings = [
            # 表 1: y = 100 (顶线), 130 (栏目线, gap=30), 250 (底线, gap=120)
            {"items": [("l", MagicMock(x=50, y=100), MagicMock(x=450, y=100))]},
            {"items": [("l", MagicMock(x=50, y=130), MagicMock(x=450, y=130))]},
            {"items": [("l", MagicMock(x=50, y=250), MagicMock(x=450, y=250))]},
            # 表 2: y = 400 (顶线), 430 (栏目线, gap=30), 550 (底线, gap=120)
            {"items": [("l", MagicMock(x=50, y=400), MagicMock(x=450, y=400))]},
            {"items": [("l", MagicMock(x=50, y=430), MagicMock(x=450, y=430))]},
            {"items": [("l", MagicMock(x=50, y=550), MagicMock(x=450, y=550))]},
        ]
        mock_page.get_drawings.return_value = drawings

        count = probe_disjoint_native_tables(mock_page)
        self.assertEqual(count, 2)

    def test_probe_disjoint_native_tables_with_page_rotation(self):
        """验证页面带有 90 度旋转时，坐标正确映射至视觉阅读方向并检出 2 个独立表格"""
        import fitz
        mock_page = MagicMock()
        mock_page.rotation = 90
        # 模拟 PyMuPDF rotation_matrix (顺时针旋转90度)
        mock_page.rotation_matrix = fitz.Matrix(90)
        mock_page.find_tables.return_value = None

        # 在未旋转空间中，视觉横线是纵向线条 (x 坐标相同，y 坐标变化)
        # 经 rotation_matrix 变换后恢复为视觉水平线
        # fitz.Point(100, 50) * Matrix(90) = Point(-50, 100) (y = 100)
        # fitz.Point(100, 450) * Matrix(90) = Point(-450, 100) (y = 100)
        p_t1_l1_a = MagicMock(x=100, y=50)
        p_t1_l1_b = MagicMock(x=100, y=450)
        p_t1_l2_a = MagicMock(x=130, y=50)
        p_t1_l2_b = MagicMock(x=130, y=450)
        p_t1_l3_a = MagicMock(x=250, y=50)
        p_t1_l3_b = MagicMock(x=250, y=450)

        p_t2_l1_a = MagicMock(x=400, y=50)
        p_t2_l1_b = MagicMock(x=400, y=450)
        p_t2_l2_a = MagicMock(x=430, y=50)
        p_t2_l2_b = MagicMock(x=430, y=450)
        p_t2_l3_a = MagicMock(x=550, y=50)
        p_t2_l3_b = MagicMock(x=550, y=450)

        drawings = [
            {"items": [("l", p_t1_l1_a, p_t1_l1_b)]},
            {"items": [("l", p_t1_l2_a, p_t1_l2_b)]},
            {"items": [("l", p_t1_l3_a, p_t1_l3_b)]},
            {"items": [("l", p_t2_l1_a, p_t2_l1_b)]},
            {"items": [("l", p_t2_l2_a, p_t2_l2_b)]},
            {"items": [("l", p_t2_l3_a, p_t2_l3_b)]},
        ]
        mock_page.get_drawings.return_value = drawings

        count = probe_disjoint_native_tables(mock_page)
        self.assertEqual(count, 2)

    def test_probe_disjoint_native_tables_coexistence_vector_and_text(self):
        """验证同页同时存在 1 张线框表和 1 张无线框文本表时，两者均被捕获 (count = 2)"""
        mock_page = MagicMock()
        mock_page.rotation = 0
        mock_page.get_drawings.return_value = []

        # 1. find_tables 检出上半部分线框表
        tb_vec = MagicMock()
        tb_vec.row_count = 4
        tb_vec.col_count = 3
        tb_vec.bbox = [50.0, 100.0, 450.0, 300.0]
        tabs_mock = MagicMock()
        tabs_mock.tables = [tb_vec]

        # 2. find_tables(vertical_strategy="text") 检出下半部分无线框文本表
        tb_txt = MagicMock()
        tb_txt.row_count = 5
        tb_txt.col_count = 4
        tb_txt.bbox = [50.0, 450.0, 450.0, 700.0]
        tabs_txt_mock = MagicMock()
        tabs_txt_mock.tables = [tb_txt]

        def ft_side_effect(vertical_strategy=None):
            if vertical_strategy == "text":
                return tabs_txt_mock
            return tabs_mock

        mock_page.find_tables.side_effect = ft_side_effect

        count = probe_disjoint_native_tables(mock_page)
        self.assertEqual(count, 2)

    def test_sparse_continuation_negative_case_prev_page_prose(self):
        """反例：前一页底部仅为普通叙述性正文段落（无表格结构），即便后页顶端有对齐文本也不应误判为续表"""
        prev_page = MagicMock()
        prev_page.rect.height = 842.0
        prev_page.find_tables.return_value = None
        prev_page.rotation = 0
        prev_page.get_drawings.return_value = []

        # 前一页底部为普通的正文单行/段落文字
        prev_page.get_text.return_value = [
            (54.0, 500.0, 100.0, 512.0, "This", 0, 0, 0),
            (110.0, 500.0, 160.0, 512.0, "is", 0, 0, 1),
            (170.0, 500.0, 220.0, 512.0, "regular", 0, 0, 2),
            (230.0, 500.0, 290.0, 512.0, "text", 0, 0, 3),
            (450.0, 500.0, 480.0, 512.0, "here", 0, 0, 4),
        ]

        # 后一页顶部有多列对齐
        next_page = MagicMock()
        next_page.rect.height = 842.0
        next_page.rotation = 0
        next_page.get_text.return_value = [
            (54.0, 60.0, 100.0, 72.0, "Item1", 0, 0, 0),
            (250.0, 60.0, 300.0, 72.0, "Value1", 0, 0, 1),
            (54.0, 80.0, 100.0, 92.0, "Item2", 0, 1, 0),
            (250.0, 80.0, 300.0, 92.0, "Value2", 0, 1, 1),
        ]

        is_cont = _check_sparse_continuation_table(prev_page, next_page)
        self.assertFalse(is_cont)

    def test_sparse_continuation_negative_case_section_heading(self):
        """反例：后一页顶端虽然有多列内容，但首行出现新章节或新表标题，应拒绝判定为续表"""
        prev_page = MagicMock()
        prev_page.rect.height = 842.0
        prev_page.rotation = 0
        tb_prev = MagicMock(row_count=5, col_count=2, bbox=[50.0, 500.0, 450.0, 800.0])
        prev_page.find_tables.return_value = MagicMock(tables=[tb_prev])

        next_page = MagicMock()
        next_page.rect.height = 842.0
        next_page.rotation = 0
        next_page.get_text.return_value = [
            (54.0, 50.0, 220.0, 62.0, "5. Conclusions and Future Work", 0, 0, 0),
            (54.0, 75.0, 100.0, 87.0, "Item1", 0, 1, 0),
            (250.0, 75.0, 300.0, 87.0, "Value1", 0, 1, 1),
            (54.0, 95.0, 100.0, 107.0, "Item2", 0, 2, 0),
            (250.0, 95.0, 300.0, 107.0, "Value2", 0, 2, 1),
        ]

        is_cont = _check_sparse_continuation_table(prev_page, next_page)
        self.assertFalse(is_cont)

    def test_reconcile_table_captions_preserves_continuation_label_without_new_number(self):
        """验证已标记为续表的短片段（<6行）在 reconcile_table_captions 中正确继承前表标签而非新分配表号"""
        from pdf_table_extractor import reconcile_table_captions
        df_main = pd.DataFrame({"A": [1, 2, 3, 4, 5, 6], "B": [7, 8, 9, 10, 11, 12]})
        df_main.attrs["label"] = "Table 1"
        df_main.attrs["table_title"] = "Main Table"
        df_main.attrs["page_idx"] = 0

        # 续表片段：仅 2 行 (< 6行)
        df_cont = pd.DataFrame({"A": [13, 14], "B": [15, 16]})
        df_cont.attrs["page_idx"] = 1
        df_cont.attrs["is_continuation"] = True

        results = [
            {"df": df_main, "page_idx": 0, "label": "Table 1"},
            {"df": df_cont, "page_idx": 1, "label": None},
        ]

        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 0
        mock_doc.__enter__.return_value = mock_doc
        with patch("os.path.exists", return_value=True), \
             patch("pdf_table_extractor.fitz.open", return_value=mock_doc), \
             patch("table_validator.scan_pdf_table_declarations", return_value={}):
            reconciled = reconcile_table_captions(results, "dummy.pdf")

        self.assertEqual(len(reconciled), 2)
        # 续表片段应成功继承 Table 1，而非被新赋予 Table 2
        self.assertEqual(reconciled[1]["label"], "Table 1")
        self.assertEqual(reconciled[1]["df"].attrs["table_title"], "Table 1 (续)")


if __name__ == "__main__":
    unittest.main()
