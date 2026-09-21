#!/usr/bin/env python3
"""
test_pdf_inspector_advanced.py — 验证 pdf-inspector 进阶应用套件：
1. quick_classify_pdf 极速分类与预检 (5~10ms)
2. inspect_crop_region (extract_text_in_regions) 矢量文本速提与 needs_ocr 感知
3. rebuild_df_with_style_hierarchy (extract_text_with_positions) 样式感知复合表头与注脚剔除
4. extract_via_structure_tree (extract_structure_elements) Tagged PDF / PDF/UA 语义结构树高精提取
5. three_way_vote 多方投票对 pdf_inspector_structure 优先级的支持
6. 坐标系统与无缝回退测试 (PDF 点 top-left vs bottom-left vs 图像像素，多 DPI 转换)
7. 异常与边界情况测试 (截断损坏文件、0面积选区、全加粗表、全正常体表、中英混排与同位素符号)
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import fitz
import pandas as pd
from PIL import Image

import pdf_inspector
from scripts import pdf_table_extractor as pte
from scripts import pdf_tables


class TestPdfInspectorAdvanced(unittest.TestCase):
    def setUp(self):
        self.temp_files = []

    def tearDown(self):
        for p in self.temp_files:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def _create_synthetic_native_pdf(self) -> str:
        """生成包含加粗表头、多列数据、小字号附注的标准 Native PDF"""
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        # Caption
        page.insert_text((50, 100), "Table 1. Experimental Geochemical Analyses", fontsize=11, fontname="helv")
        # Header Row 1 (bold)
        page.insert_text((50, 130), "Sample", fontsize=10, fontname="times-bold")
        page.insert_text((150, 130), "SiO2", fontsize=10, fontname="times-bold")
        page.insert_text((270, 130), "Al2O3", fontsize=10, fontname="times-bold")
        page.insert_text((390, 130), "Fe2O3", fontsize=10, fontname="times-bold")
        # Header Row 2: Units (bold)
        page.insert_text((150, 142), "(wt.%)", fontsize=10, fontname="times-bold")
        page.insert_text((270, 142), "(wt.%)", fontsize=10, fontname="times-bold")
        page.insert_text((390, 142), "(wt.%)", fontsize=10, fontname="times-bold")
        # Data Row 1
        page.insert_text((50, 165), "SMP-01", fontsize=10, fontname="times-roman")
        page.insert_text((150, 165), "72.45", fontsize=10, fontname="times-roman")
        page.insert_text((270, 165), "14.12", fontsize=10, fontname="times-roman")
        page.insert_text((390, 165), "2.31", fontsize=10, fontname="times-roman")
        # Data Row 2
        page.insert_text((50, 185), "SMP-02", fontsize=10, fontname="times-roman")
        page.insert_text((150, 185), "71.80", fontsize=10, fontname="times-roman")
        page.insert_text((270, 185), "14.50", fontsize=10, fontname="times-roman")
        page.insert_text((390, 185), "2.15", fontsize=10, fontname="times-roman")
        # Footnote (small font: 7.0pt)
        page.insert_text((50, 215), "* Note: All major oxides normalized to 100% volatile-free basis.", fontsize=7.0, fontname="times-roman")

        doc.save(path)
        doc.close()
        return path

    def _create_synthetic_image_pdf(self) -> str:
        """生成纯图片扫描版 PDF"""
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        fd_img, img_path = tempfile.mkstemp(suffix=".png")
        os.close(fd_img)
        self.temp_files.append(img_path)

        img = Image.new("RGB", (300, 300), color="white")
        img.save(img_path)

        doc = fitz.open()
        page = doc.new_page(width=500, height=500)
        page.insert_image(fitz.Rect(50, 50, 450, 450), filename=img_path)
        doc.save(path)
        doc.close()
        return path

    # -----------------------------------------------------------------------
    # 1. quick_classify_pdf 测试与边界
    # -----------------------------------------------------------------------
    def test_quick_classify_native_pdf(self):
        pdf_path = self._create_synthetic_native_pdf()
        clf = pte.quick_classify_pdf(pdf_path)
        self.assertIn(clf["pdf_type"], ("text_based", "mixed"))
        self.assertTrue(clf["has_text"])
        self.assertGreater(clf["confidence"], 0.5)
        self.assertEqual(clf["page_count"], 1)

    def test_quick_classify_scanned_pdf(self):
        pdf_path = self._create_synthetic_image_pdf()
        clf = pte.quick_classify_pdf(pdf_path)
        self.assertEqual(clf["pdf_type"], "scanned")
        self.assertFalse(clf["has_text"])
        self.assertEqual(clf["page_count"], 1)

    def test_quick_classify_nonexistent_file(self):
        clf = pte.quick_classify_pdf("/non/existent/path/paper.pdf")
        self.assertEqual(clf["pdf_type"], "unknown")
        self.assertFalse(clf["has_text"])
        self.assertEqual(clf["confidence"], 0.0)

    def test_quick_classify_corrupted_file(self):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"%PDF-1.4\ncorrupted bytes content here")
        os.close(fd)
        self.temp_files.append(path)

        clf = pte.quick_classify_pdf(path)
        self.assertIsInstance(clf, dict)
        self.assertIn("pdf_type", clf)

    def test_quick_classify_fallback_when_inspector_fails(self):
        pdf_path = self._create_synthetic_native_pdf()
        with patch.object(pdf_inspector, "classify_pdf", side_effect=RuntimeError("Mock error")):
            clf = pte.quick_classify_pdf(pdf_path)
            self.assertEqual(clf["pdf_type"], "text_based")
            self.assertTrue(clf["has_text"])
            self.assertEqual(clf["page_count"], 1)

    # -----------------------------------------------------------------------
    # 2. inspect_crop_region (extract_text_in_regions) 测试与多 DPI 转换
    # -----------------------------------------------------------------------
    def test_inspect_crop_region_vector_text(self):
        pdf_path = self._create_synthetic_native_pdf()
        scale_to_px = 200.0 / 72.0
        crop_bbox = [50 * scale_to_px, 120 * scale_to_px, 450 * scale_to_px, 200 * scale_to_px]
        
        info = pdf_tables.inspect_crop_region(pdf_path, 0, crop_bbox, dpi=200)
        self.assertFalse(info["needs_ocr"])
        self.assertIsNone(info["ocr_reason"])
        self.assertIn("SMP-01", info["text"])
        self.assertIn("SiO2", info["text"])
        self.assertAlmostEqual(info["pdf_bbox"][0], 50.0, places=1)
        self.assertAlmostEqual(info["pdf_bbox"][1], 120.0, places=1)

    def test_inspect_crop_region_multi_dpi_scaling(self):
        pdf_path = self._create_synthetic_native_pdf()
        # 测试 300 DPI
        scale_300 = 300.0 / 72.0
        crop_bbox_300 = [50 * scale_300, 120 * scale_300, 450 * scale_300, 200 * scale_300]
        info_300 = pdf_tables.inspect_crop_region(pdf_path, 0, crop_bbox_300, dpi=300)
        self.assertAlmostEqual(info_300["pdf_bbox"][0], 50.0, places=1)
        self.assertAlmostEqual(info_300["pdf_bbox"][1], 120.0, places=1)
        self.assertFalse(info_300["needs_ocr"])

    def test_inspect_crop_region_image_needs_ocr(self):
        pdf_path = self._create_synthetic_image_pdf()
        crop_bbox = [50 * (200/72), 50 * (200/72), 450 * (200/72), 450 * (200/72)]
        info = pdf_tables.inspect_crop_region(pdf_path, 0, crop_bbox, dpi=200)
        self.assertTrue(info["needs_ocr"])
        self.assertTrue(info["ocr_reason"] in ("image_only", "heuristic_low_text_image") or len(info["text"]) < 5)

    def test_inspect_crop_region_zero_area(self):
        pdf_path = self._create_synthetic_native_pdf()
        crop_bbox = [100.0, 100.0, 100.0, 100.0]
        info = pdf_tables.inspect_crop_region(pdf_path, 0, crop_bbox, dpi=200)
        self.assertIsInstance(info, dict)
        self.assertIn("needs_ocr", info)

    # -----------------------------------------------------------------------
    # 3. rebuild_df_with_style_hierarchy 测试与样式边缘情况
    # -----------------------------------------------------------------------
    def test_rebuild_df_with_style_hierarchy_multiline_header_and_footnote(self):
        pdf_path = self._create_synthetic_native_pdf()
        crop_rect = fitz.Rect(40, 90, 500, 230)
        df, cap, fn = pdf_tables.rebuild_df_with_style_hierarchy(pdf_path, 0, crop_rect, 842.0)

        self.assertIsNotNone(df)
        self.assertIn("Table 1", cap)
        self.assertIn("normalized to 100%", fn)
        cols = list(df.columns)
        self.assertIn("Sample", cols[0])
        self.assertTrue(any("SiO2" in c and "wt.%" in c for c in cols))
        self.assertTrue(any("Al2O3" in c and "wt.%" in c for c in cols))
        self.assertEqual(len(df), 2)
        self.assertIn("SMP-01", df.iloc[0, 0])
        self.assertIn("SMP-02", df.iloc[1, 0])
        self.assertNotIn("normalized", df.to_string())

    def test_rebuild_df_all_normal_font(self):
        """测试全表均无加粗样式时的优雅回退（默认第 0 行为表头）"""
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        doc = fitz.open()
        page = doc.new_page(width=500, height=500)
        page.insert_text((50, 50), "ColA", fontsize=10, fontname="times-roman")
        page.insert_text((150, 50), "ColB", fontsize=10, fontname="times-roman")
        page.insert_text((50, 80), "val1", fontsize=10, fontname="times-roman")
        page.insert_text((150, 80), "val2", fontsize=10, fontname="times-roman")
        doc.save(path)
        doc.close()

        crop_rect = fitz.Rect(40, 40, 300, 100)
        df, cap, fn = pdf_tables.rebuild_df_with_style_hierarchy(path, 0, crop_rect, 500.0)
        self.assertIsNotNone(df)
        self.assertEqual(list(df.columns), ["ColA", "ColB"])
        self.assertEqual(len(df), 1)

    # -----------------------------------------------------------------------
    # 4. extract_via_structure_tree 测试
    # -----------------------------------------------------------------------
    def test_extract_via_structure_tree_untagged_pdf(self):
        pdf_path = self._create_synthetic_native_pdf()
        tbls = pte.extract_via_structure_tree(pdf_path)
        self.assertEqual(tbls, [])

    def test_extract_via_structure_tree_real_tagged_pdf(self):
        real_pdf = "/Users/ryanx/Downloads/ScienceDirect_articles_19Jul2026_11-01-38.482/Strength-prediction-of-cemented-paste-backfill-with-differen_2025_Results-in.pdf"
        if not os.path.exists(real_pdf):
            self.skipTest("Real tagged PDF not present in environment")

        tbls = pte.extract_via_structure_tree(real_pdf)
        self.assertEqual(len(tbls), 4)

        # 验证 Page 3 Table 1: 跨栏两栏流动解算
        t1 = tbls[0]
        self.assertEqual(t1["page_idx"], 2)
        df1 = t1["df"]
        self.assertEqual(df1.shape, (120, 6))
        self.assertEqual(df1.attrs["extractor"], "pdf_inspector_structure")
        self.assertTrue(df1.attrs.get("has_semantic_th"))

        # 验证 Page 5 Table 2: 复合多行表头合成
        t2 = tbls[1]
        df2 = t2["df"]
        self.assertEqual(df2.shape, (5, 6))
        cols2 = list(df2.columns)
        self.assertIn("Variable", cols2)
        self.assertTrue(any("Lower" in c and "Bound" in c for c in cols2))
        self.assertTrue(any("Upper" in c and "Bound" in c for c in cols2))

        # 验证 Page 9 Table 4: 复合多行表头子列合成与列名唯一性
        t4 = tbls[3]
        df4 = t4["df"]
        self.assertEqual(df4.shape, (6, 9))
        cols4 = list(df4.columns)
        self.assertEqual(len(cols4), len(set(cols4)), "Column names must be unique without duplicates")
        self.assertTrue(any("Training Phase R" in c for c in cols4))
        self.assertTrue(any("Testing Phase R" in c for c in cols4))

        # 验证所有结构化表格的 bbox 均为合规 top-down 坐标 (y0 < y1)
        for t in tbls:
            bbox = t["bbox"]
            self.assertLess(bbox[1], bbox[3], f"BBox y0 must be less than y1: {bbox}")
            self.assertLess(bbox[0], bbox[2], f"BBox x0 must be less than x1: {bbox}")

    def test_extract_via_structure_tree_page_filtering(self):
        real_pdf = "/Users/ryanx/Downloads/ScienceDirect_articles_19Jul2026_11-01-38.482/Strength-prediction-of-cemented-paste-backfill-with-differen_2025_Results-in.pdf"
        if not os.path.exists(real_pdf):
            self.skipTest("Real tagged PDF not present in environment")

        # 仅请求 0-indexed 的第 4 页 (Page 5)
        tbls = pte.extract_via_structure_tree(real_pdf, pages=[4])
        self.assertEqual(len(tbls), 1)
        self.assertEqual(tbls[0]["page_idx"], 4)
        self.assertEqual(tbls[0]["df"].shape, (5, 6))

    def test_extract_via_structure_tree_multiple_tables_on_same_page(self):
        """测试在同一页内存在多个表格时，通过 Caption/段落/TH 正确切分独立表格且不发生合并吞并"""
        class MockSE:
            def __init__(self, page, role, mcid):
                self.page = page
                self.role = role
                self.mcid = mcid

        class MockItem:
            def __init__(self, mcid, text, x, y, width=50, height=10, font_size=10, is_bold=False):
                self.mcid = mcid
                self.text = text
                self.x = x
                self.y = y
                self.width = width
                self.height = height
                self.font_size = font_size
                self.is_bold = is_bold

        se_list = [
            MockSE(1, 'Caption', 0),
            MockSE(1, 'TH', 1),
            MockSE(1, 'TH', 2),
            MockSE(1, 'TD', 3),
            MockSE(1, 'TD', 4),
            MockSE(1, 'P', 5),
            MockSE(1, 'Caption', 6),
            MockSE(1, 'TH', 7),
            MockSE(1, 'TH', 8),
            MockSE(1, 'TD', 9),
            MockSE(1, 'TD', 10),
        ]

        items_list = [
            MockItem(0, 'Table 1. Alpha Experiments', 50, 750, is_bold=True),
            MockItem(1, 'ColA', 50, 720, is_bold=True),
            MockItem(2, 'ColB', 150, 720, is_bold=True),
            MockItem(3, '100', 50, 690),
            MockItem(4, '200', 150, 690),
            MockItem(5, 'Paragraph text between tables', 50, 650),
            MockItem(6, 'Table 2. Beta Experiments', 50, 600, is_bold=True),
            MockItem(7, 'ColX', 50, 570, is_bold=True),
            MockItem(8, 'ColY', 150, 570, is_bold=True),
            MockItem(9, '300', 50, 540),
            MockItem(10, '400', 150, 540),
        ]

        with patch('pdf_inspector.extract_structure_elements', return_value=se_list), \
             patch('pdf_inspector.extract_text_with_positions', return_value=items_list), \
             patch('os.path.exists', return_value=True), \
             patch('fitz.open') as mock_fitz:
            doc = MagicMock()
            page = MagicMock()
            page.rect.height = 842.0
            doc.__len__.return_value = 1
            doc.__getitem__.return_value = page
            mock_fitz.return_value = doc

            tbls = pte.extract_via_structure_tree('dummy.pdf')
            self.assertEqual(len(tbls), 2, "Must extract 2 distinct tables on the same page")
            t1, t2 = tbls[0], tbls[1]
            self.assertEqual(t1['page_idx'], 0)
            self.assertEqual(t2['page_idx'], 0)
            self.assertEqual(t1['table_idx'], 0)
            self.assertEqual(t2['table_idx'], 1)
            self.assertIn("Table 1", t1['df'].attrs.get('table_title', ''))
            self.assertIn("Table 2", t2['df'].attrs.get('table_title', ''))
            self.assertEqual(list(t1['df'].columns), ['ColA', 'ColB'])
            self.assertEqual(list(t2['df'].columns), ['ColX', 'ColY'])
            self.assertEqual(t1['df'].iloc[0, 0], '100')
            self.assertEqual(t2['df'].iloc[0, 0], '300')

    # -----------------------------------------------------------------------
    # 5. three_way_vote 多方投票结构树优先权测试
    # -----------------------------------------------------------------------
    def test_three_way_vote_structure_priority(self):
        df_struct = pd.DataFrame({"ColA": ["1", "2"], "ColB": ["3", "4"]})
        df_struct.attrs["extractor"] = "pdf_inspector_structure"
        struct_cand = [{"df": df_struct, "page_idx": 0, "table_idx": 0, "bbox": None}]

        df_ft = pd.DataFrame({"ColA": ["1", "2"], "ColB": ["3", "4"]})
        df_ft.attrs["extractor"] = "find_tables"
        ft_cand = [{"df": df_ft, "page_idx": 0, "table_idx": 0, "bbox": None}]

        fused, logs, summary = pte.three_way_vote(
            inspector_tables=[],
            find_tables_results=ft_cand,
            plumber_tables=[],
            camelot_tables=[],
            structure_tables=struct_cand,
        )

        self.assertEqual(len(fused), 1)
        winner = fused[0]["df"].attrs["extractor"]
        self.assertEqual(winner, "pdf_inspector_structure")

    # -----------------------------------------------------------------------
    # 6. extract_via_text_alignment 样式感知测试
    # -----------------------------------------------------------------------
    def test_extract_via_text_alignment_with_positions(self):
        pdf_path = self._create_synthetic_native_pdf()
        tbls = pte.extract_via_text_alignment(pdf_path, pages=[0])
        self.assertTrue(len(tbls) >= 1)
        df = tbls[0]["df"]
        self.assertEqual(df.attrs["extractor"], "text_alignment")
        self.assertTrue(df.shape[0] >= 2)
        self.assertTrue(df.shape[1] >= 3)

    # -----------------------------------------------------------------------
    # 7. 综合管线端到端 extract_tables_from_pdf 测试
    # -----------------------------------------------------------------------
    def test_extract_tables_from_pdf_pipeline(self):
        pdf_path = self._create_synthetic_native_pdf()
        results, logs = pte.extract_tables_from_pdf(pdf_path, use_ocr_fallback=False)
        self.assertTrue(len(results) >= 1)
        df = results[0]["df"]
        self.assertTrue(df.shape[0] >= 2)
        self.assertTrue(df.shape[1] >= 3)
        self.assertIn("Sample", list(df.columns)[0])


    # -----------------------------------------------------------------------
    # 8. 进阶旋转与 CropBox 物理坐标自适应测试
    # -----------------------------------------------------------------------
    def test_inspect_crop_region_rotated_pdf(self):
        """测试在页面旋转 90/180/270 度时，inspect_crop_region 仍能精准提取矢量文本并识别 needs_ocr=False"""
        import numpy as np
        for rot in (90, 180, 270):
            fd, path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            self.temp_files.append(path)

            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((150, 200), f"Rotated_{rot}_Text", fontsize=12)
            page.set_rotation(rot)
            doc.save(path)
            doc.close()

            doc2 = fitz.open(path)
            p2 = doc2[0]
            pix = p2.get_pixmap(dpi=72)
            arr = np.array(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
            non_white = np.where(arr < 250)
            py0, py1 = int(non_white[0].min()), int(non_white[0].max())
            px0, px1 = int(non_white[1].min()), int(non_white[1].max())
            crop_bbox = [px0 - 4, py0 - 4, px1 + 4, py1 + 4]
            doc2.close()

            info = pdf_tables.inspect_crop_region(path, 0, crop_bbox, dpi=72)
            self.assertFalse(info["needs_ocr"], f"Failed on rot={rot}")
            self.assertIn(f"Rotated_{rot}_Text", info["text"])

    def test_inspect_crop_region_cropbox_offset(self):
        """测试非零 CropBox 偏移下 inspect_crop_region 的坐标自适应能力"""
        import numpy as np
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text((150, 150), "CropboxOffsetContent", fontsize=12)
        page.set_cropbox(fitz.Rect(100, 100, 550, 750))
        doc.save(path)
        doc.close()

        doc2 = fitz.open(path)
        p2 = doc2[0]
        pix = p2.get_pixmap(dpi=72)
        arr = np.array(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
        non_white = np.where(arr < 250)
        py0, py1 = int(non_white[0].min()), int(non_white[0].max())
        px0, px1 = int(non_white[1].min()), int(non_white[1].max())
        crop_bbox = [px0 - 4, py0 - 4, px1 + 4, py1 + 4]
        doc2.close()

        info = pdf_tables.inspect_crop_region(path, 0, crop_bbox, dpi=72)
        self.assertFalse(info["needs_ocr"])
        self.assertIn("CropboxOffsetContent", info["text"])

    def test_rebuild_df_rotated_and_cropbox(self):
        """测试页面旋转 + CropBox 偏移组合场景下的 DataFrame 样式分层重构"""
        import numpy as np
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text((120, 120), "Header1", fontsize=11, fontname="times-bold")
        page.insert_text((220, 120), "Header2", fontsize=11, fontname="times-bold")
        page.insert_text((120, 150), "ValA", fontsize=10, fontname="times-roman")
        page.insert_text((220, 150), "ValB", fontsize=10, fontname="times-roman")
        page.set_cropbox(fitz.Rect(50, 50, 550, 750))
        page.set_rotation(90)
        doc.save(path)
        doc.close()

        doc2 = fitz.open(path)
        p2 = doc2[0]
        pix = p2.get_pixmap(dpi=72)
        arr = np.array(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
        non_white = np.where(arr < 250)
        py0, py1 = int(non_white[0].min()), int(non_white[0].max())
        px0, px1 = int(non_white[1].min()), int(non_white[1].max())
        crop_bbox = [px0 - 5, py0 - 5, px1 + 5, py1 + 5]

        df, cap, fn = pdf_tables.rebuild_df_with_style_hierarchy(
            path, 0, fitz.Rect(crop_bbox), p2.rect.height, crop_bbox=crop_bbox, dpi=72
        )
        doc2.close()

        self.assertIsNotNone(df)
        self.assertEqual(list(df.columns), ["Header1", "Header2"])
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0, 0], "ValA")
        self.assertEqual(df.iloc[0, 1], "ValB")

    def test_extract_via_text_alignment_rotated_page(self):
        """测试在旋转 90 度的页面上基于 extract_text_with_positions 的样式感知文本对齐提取"""
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text((80, 80), "Table 1. Rotated Aligned Table", fontsize=12)
        page.insert_text((80, 110), "ColA", fontsize=11, fontname="times-bold")
        page.insert_text((180, 110), "ColB", fontsize=11, fontname="times-bold")
        page.insert_text((80, 140), "DataA", fontsize=10, fontname="times-roman")
        page.insert_text((180, 140), "DataB", fontsize=10, fontname="times-roman")
        page.set_rotation(90)
        doc.save(path)
        doc.close()

        tbls = pte.extract_via_text_alignment(path, pages=[0])
        self.assertTrue(len(tbls) >= 1)
        df = tbls[0]["df"]
        self.assertIn("ColA", list(df.columns)[0])

    def test_classify_and_extract_via_inspector_decoupled_tables(self):
        """测试 classify_and_extract_via_inspector 保持 structure_tables 与 tables 解耦，杜绝三方投票自投"""
        real_pdf = "/Users/ryanx/Downloads/ScienceDirect_articles_19Jul2026_11-01-38.482/Strength-prediction-of-cemented-paste-backfill-with-differen_2025_Results-in.pdf"
        if not os.path.exists(real_pdf):
            self.skipTest("Real tagged PDF not present in environment")

        res = pte.classify_and_extract_via_inspector(real_pdf)
        self.assertTrue(len(res["structure_tables"]) >= 1)
        for t in res["tables"]:
            self.assertNotIn(t["df"].attrs.get("extractor"), ("pdf_inspector_structure",))

    # -----------------------------------------------------------------------
    # 9. 专项健壮性回归测试：杜绝单元格跨行串位、表头截断与密集列挤压
    # -----------------------------------------------------------------------
    def test_structure_tree_empty_cells_no_drifting(self):
        """验证存在空单元格时，严禁按元素总数取模切片，确保基于物理 y 聚类与 x 投影，绝不跨行串位"""
        class MockSE:
            def __init__(self, page, role, mcid):
                self.page = page
                self.role = role
                self.mcid = mcid

        class MockItem:
            def __init__(self, mcid, text, x, y, width=40, height=10):
                self.mcid = mcid
                self.text = text
                self.x = x
                self.y = y
                self.width = width
                self.height = height

        # 3 列: ColA (x=50), ColB (x=150), ColC (x=250)
        # Row 1: R1A (x=50), R1C (x=250) -> ColB 缺失
        # Row 2: R2A (x=50), R2B (x=150), R2C (x=250)
        # Row 3: R3B (x=150) -> ColA, ColC 缺失
        # 数据单元格总数 = 2 + 3 + 1 = 6 个。
        # 6 % 3 == 0！若按旧逻辑顺序切片，Row 1 就会错误吃进 R2A，产生致命全表错位！
        se_list = [
            MockSE(1, 'TH', 0), MockSE(1, 'TH', 1), MockSE(1, 'TH', 2),
            MockSE(1, 'TD', 3), MockSE(1, 'TD', 4),
            MockSE(1, 'TD', 5), MockSE(1, 'TD', 6), MockSE(1, 'TD', 7),
            MockSE(1, 'TD', 8)
        ]
        items = [
            MockItem(0, 'ColA', 50, 750),
            MockItem(1, 'ColB', 150, 750),
            MockItem(2, 'ColC', 250, 750),
            # Row 1 (y=720): ColB 为空
            MockItem(3, 'R1A', 50, 720),
            MockItem(4, 'R1C', 250, 720),
            # Row 2 (y=690): 完整
            MockItem(5, 'R2A', 50, 690),
            MockItem(6, 'R2B', 150, 690),
            MockItem(7, 'R2C', 250, 690),
            # Row 3 (y=660): 仅 ColB
            MockItem(8, 'R3B', 150, 660),
        ]

        with patch('pdf_inspector.extract_structure_elements', return_value=se_list), \
             patch('pdf_inspector.extract_text_with_positions', return_value=items), \
             patch('os.path.exists', return_value=True), \
             patch('fitz.open') as mock_fitz:
            doc = MagicMock()
            page = MagicMock()
            page.rect.height = 842.0
            doc.__len__.return_value = 1
            doc.__getitem__.return_value = page
            mock_fitz.return_value = doc

            tbls = pte.extract_via_structure_tree('dummy.pdf')
            self.assertEqual(len(tbls), 1)
            df = tbls[0]['df']
            self.assertEqual(df.shape, (3, 3))
            self.assertEqual(list(df.columns), ['ColA', 'ColB', 'ColC'])
            # 严格验证各行各列内容与空单元格
            self.assertEqual(df.iloc[0, 0], 'R1A')
            self.assertEqual(df.iloc[0, 1], '')
            self.assertEqual(df.iloc[0, 2], 'R1C')

            self.assertEqual(df.iloc[1, 0], 'R2A')
            self.assertEqual(df.iloc[1, 1], 'R2B')
            self.assertEqual(df.iloc[1, 2], 'R2C')

            self.assertEqual(df.iloc[2, 0], '')
            self.assertEqual(df.iloc[2, 1], 'R3B')
            self.assertEqual(df.iloc[2, 2], '')

    def test_structure_tree_multiline_composite_header(self):
        """验证多行复合表头（高度超过 25pt）能够被完整聚类合并为表头，绝不被截断掉入数据行"""
        class MockSE:
            def __init__(self, page, role, mcid):
                self.page = page
                self.role = role
                self.mcid = mcid

        class MockItem:
            def __init__(self, mcid, text, x, y):
                self.mcid = mcid
                self.text = text
                self.x = x
                self.y = y
                self.width = 40
                self.height = 10

        # 表头共 3 行：
        # Header Row 1 (y=750): CategoryA, CategoryB
        # Header Row 2 (y=735): SubA, SubB (距上一行 15pt <= 20pt)
        # Header Row 3 (y=720): (wt.%), (ppm) (距上一行 15pt <= 20pt, 总高度 30pt > 25pt)
        # 数据行 (y=680): 12.5, 340
        se_list = [
            MockSE(1, 'TH', 0), MockSE(1, 'TH', 1),
            MockSE(1, 'TH', 2), MockSE(1, 'TH', 3),
            MockSE(1, 'TH', 4), MockSE(1, 'TH', 5),
            MockSE(1, 'TD', 6), MockSE(1, 'TD', 7),
        ]
        items = [
            MockItem(0, 'Major', 50, 750),
            MockItem(1, 'Trace', 150, 750),
            MockItem(2, 'SiO2', 50, 735),
            MockItem(3, 'Sr', 150, 735),
            MockItem(4, '(wt.%)', 50, 720),
            MockItem(5, '(ppm)', 150, 720),
            MockItem(6, '72.3', 50, 680),
            MockItem(7, '450', 150, 680),
        ]

        with patch('pdf_inspector.extract_structure_elements', return_value=se_list), \
             patch('pdf_inspector.extract_text_with_positions', return_value=items), \
             patch('os.path.exists', return_value=True), \
             patch('fitz.open') as mock_fitz:
            doc = MagicMock()
            page = MagicMock()
            page.rect.height = 842.0
            doc.__len__.return_value = 1
            doc.__getitem__.return_value = page
            mock_fitz.return_value = doc

            tbls = pte.extract_via_structure_tree('dummy.pdf')
            self.assertEqual(len(tbls), 1)
            df = tbls[0]['df']
            self.assertEqual(len(df), 1, "数据行应仅有 1 行，表头不得溢出为数据行")
            cols = list(df.columns)
            self.assertTrue(any("SiO2" in c and "(wt.%)" in c for c in cols))
            self.assertTrue(any("Sr" in c and "(ppm)" in c for c in cols))
            self.assertEqual(df.iloc[0, 0], '72.3')
            self.assertEqual(df.iloc[0, 1], '450')

    def test_dense_columns_adaptive_clustering(self):
        """验证在密集多列表格（如 10 列间距仅 18pt）中，自适应容差与同行互斥机制防止相邻列被合并"""
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        doc = fitz.open()
        page = doc.new_page(width=600, height=400)
        # 写入 10 个紧凑列，间距 16pt (50, 66, 82, 98, 114, 130, 146, 162, 178, 194)
        xs = [50 + i * 16 for i in range(10)]
        for i, x in enumerate(xs):
            page.insert_text((x, 50), f"C{i+1}", fontsize=9, fontname="times-bold")
            page.insert_text((x, 80), f"V{i+1}", fontsize=9, fontname="times-roman")
        doc.save(path)
        doc.close()

        crop_rect = fitz.Rect(40, 40, 220, 100)
        df, cap, fn = pdf_tables.rebuild_df_with_style_hierarchy(path, 0, crop_rect, 400.0)
        self.assertIsNotNone(df)
        self.assertEqual(df.shape[1], 10, f"必须精准识别出 10 列，实际得到 {df.shape[1]} 列: {list(df.columns)}")
        for i in range(10):
            self.assertIn(f"C{i+1}", df.columns[i])
            self.assertEqual(df.iloc[0, i], f"V{i+1}")

    def test_structure_tree_unheaded_index_column(self):
        """验证左侧无表头的数据列（如样品编号）在自适应门限 (-8pt) 下能够精准识别并生成 Index 列"""
        class MockSE:
            def __init__(self, page, role, mcid):
                self.page = page
                self.role = role
                self.mcid = mcid

        class MockItem:
            def __init__(self, mcid, text, x, y):
                self.mcid = mcid
                self.text = text
                self.x = x
                self.y = y
                self.width = 30
                self.height = 10

        # 表头从 x=100 开始（Col1=100, Col2=200）
        # 数据行在 x=88 处存在样品名 (100 - 88 = 12pt < 旧门限 20pt, 但 > 新门限 8pt)
        se_list = [
            MockSE(1, 'TH', 0), MockSE(1, 'TH', 1),
            MockSE(1, 'TD', 2), MockSE(1, 'TD', 3), MockSE(1, 'TD', 4)
        ]
        items = [
            MockItem(0, 'ParamA', 100, 750),
            MockItem(1, 'ParamB', 200, 750),
            MockItem(2, 'SMP-01', 88, 720),
            MockItem(3, '10.5', 100, 720),
            MockItem(4, '20.5', 200, 720),
        ]

        with patch('pdf_inspector.extract_structure_elements', return_value=se_list), \
             patch('pdf_inspector.extract_text_with_positions', return_value=items), \
             patch('os.path.exists', return_value=True), \
             patch('fitz.open') as mock_fitz:
            doc = MagicMock()
            page = MagicMock()
            page.rect.height = 842.0
            doc.__len__.return_value = 1
            doc.__getitem__.return_value = page
            mock_fitz.return_value = doc

            tbls = pte.extract_via_structure_tree('dummy.pdf')
            self.assertEqual(len(tbls), 1)
            df = tbls[0]['df']
            self.assertEqual(df.shape, (1, 3))
            self.assertEqual(list(df.columns), ['Index', 'ParamA', 'ParamB'])
            self.assertEqual(df.iloc[0, 0], 'SMP-01')
            self.assertEqual(df.iloc[0, 1], '10.5')
            self.assertEqual(df.iloc[0, 2], '20.5')

    def test_crop_to_dataframe_quality_gate_squeezed_fallback(self):
        """验证当 rebuild_df_with_style_hierarchy 提取出挤压或低质量 DataFrame 时，质量门禁触发并回退"""
        squeezed_df = pd.DataFrame({'Col1': ['a', 'b', 'c']}) # 仅 1 列
        with patch('scripts.pdf_tables.rebuild_df_with_style_hierarchy', return_value=(squeezed_df, 'Cap', 'Fn')), \
             patch('scripts.pdf_tables._rebuild_df_from_words') as mock_word_fallback:
            mock_fallback_df = pd.DataFrame({'A': [1], 'B': [2]})
            mock_fallback_df.attrs['extractor'] = 'pymupdf_words'
            mock_word_fallback.return_value = (mock_fallback_df, 'Cap', 'Fn')

            pdf_path = self._create_synthetic_native_pdf()
            crop_info = {
                'page_index': 0,
                'crop_bbox': [40, 100, 500, 200],
                'dpi': 72,
                'label': 'table',
                'caption': 'Table 1'
            }
            res_df, res_cap, res_fn = pdf_tables.crop_to_dataframe(pdf_path, crop_info)
            self.assertIsNotNone(res_df)
            # 必须成功拒绝仅 1 列的 squeezed_df，回退到后续策略
            self.assertEqual(res_df.shape[1], 2)
            self.assertEqual(res_df.attrs.get('extractor'), 'pymupdf_words')


if __name__ == "__main__":
    unittest.main()

