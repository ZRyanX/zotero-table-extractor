#!/usr/bin/env python3
"""
tests/test_orientation_audit_fixes.py — 针对方向检测与旋转处理 5 处修正的深度单元测试套件。

覆盖项：
1. Item 1: 旋转页面上的标题提取 (_get_words_visual 视口坐标归一化保障与 table_bbox 坐标精准匹配)；
2. Item 2: extract_tables_from_pdf 透传 remediated=True 杜绝冗余重复扫描；
3. Item 3: 扫描版旋转烘焙 (baking) 分支严禁 swap 宽高，保障尺寸与视口方向完全一致，严防图像压扁畸变；
4. Item 4: detect_crop_orientation 对模型轨 180° 翻转实施安全抑制 (OOD 保护)，切图 180° 严格仅信任矢量检测；
5. Item 5: ocr_client.py 在本地检测器不可用时动态开启在线 useDocOrientationClassify: True，防止两道防线全失。
"""

import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import numpy as np
from PIL import Image
import pymupdf as fitz

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(1, SCRIPTS_DIR)

import doc_orientation_detector as dod
from doc_orientation_detector import detect_crop_orientation
import pdf_table_extractor as pte
from pdf_table_extractor import (
    extract_table_caption_from_page,
    extract_tables_from_pdf,
    _get_words_visual,
    _search_for_visual,
    _extract_page_via_text_alignment,
    extract_via_structure_tree,
)
from pdf_tables import bake_pdf_rotations
import ocr_client


class TestOrientationAuditFixes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_ori_audit_")

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Item 1: 旋转页面标题提取与坐标归一化
    # -------------------------------------------------------------------------
    def test_caption_extraction_on_rotated_page_with_table_bbox(self):
        """测试在旋转 90/270 度页面上，通过 _get_words_visual 将 words 归一化至视口空间，精确匹配 table_bbox 上方标题。"""
        doc = fitz.open()
        p = doc.new_page(width=400, height=600)
        # 在原始未旋转空间写入标题与表格
        # 当页面旋转 90 度后，原 (x, y) 映射为视口 (600 - y, x)
        p.insert_text((50, 50), "Table 1. Experimental Performance Comparison", fontsize=12)
        p.set_rotation(90)
        pdf_path = os.path.join(self.temp_dir, "rot_caption.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_read = fitz.open(pdf_path)
        page = doc_read[0]
        self.assertEqual(page.rotation, 90)
        # 视口尺寸应为 600 x 400
        self.assertEqual(page.rect.width, 600)
        self.assertEqual(page.rect.height, 400)

        # 验证 _get_words_visual 返回的 words 处于视口空间 (0..600, 0..400)
        v_words = _get_words_visual(page)
        self.assertTrue(len(v_words) > 0)
        for w in v_words:
            self.assertGreaterEqual(w[0], 0.0)
            self.assertLessEqual(w[2], 600.0)
            self.assertGreaterEqual(w[1], 0.0)
            self.assertLessEqual(w[3], 400.0)

        # 构造视口空间下的 table_bbox，刚好在 caption 下方
        table_bbox_vis = [50.0, 100.0, 550.0, 350.0]
        label, caption = extract_table_caption_from_page(page, table_bbox=table_bbox_vis)
        self.assertEqual(label, "Table 1")
        self.assertIn("Experimental Performance Comparison", caption)
        doc_read.close()

    # -------------------------------------------------------------------------
    # Item 2: Tier 1 冗余重复扫描消除
    # -------------------------------------------------------------------------
    def test_extract_tables_from_pdf_skips_tier1_when_remediated_passed(self):
        """测试当 extract_tables_from_pdf 接收到 remediated 参数时，跳过重复的 remediate_pdf_pages_lossless 调用。"""
        doc = fitz.open()
        p = doc.new_page(width=500, height=700)
        p.insert_text((50, 50), "Sample Document Text")
        pdf_path = os.path.join(self.temp_dir, "test_remediated_param.pdf")
        doc.save(pdf_path)
        doc.close()

        with patch("pdf_table_extractor._do_extract_tables_from_pdf", return_value=([], [])) as mock_do_extract, \
             patch("doc_orientation_detector.remediate_pdf_pages_lossless") as mock_remed:
            # Case A: remediated=True (已在上游完成修复) -> 绝不调用 remediate_pdf_pages_lossless
            extract_tables_from_pdf(pdf_path, remediated=True)
            mock_remed.assert_not_called()
            mock_do_extract.assert_called_once_with(pdf_path, use_ocr_fallback=True, was_remediated=True)

        with patch("pdf_table_extractor._do_extract_tables_from_pdf", return_value=([], [])) as mock_do_extract, \
             patch("doc_orientation_detector.remediate_pdf_pages_lossless") as mock_remed:
            # Case B: remediated=False (上游已检测无需修复) -> 绝不调用 remediate_pdf_pages_lossless
            extract_tables_from_pdf(pdf_path, remediated=False)
            mock_remed.assert_not_called()
            mock_do_extract.assert_called_once_with(pdf_path, use_ocr_fallback=True, was_remediated=False)

        with patch("pdf_table_extractor._do_extract_tables_from_pdf", return_value=([], [])) as mock_do_extract, \
             patch("doc_orientation_detector.remediate_pdf_pages_lossless", return_value=(pdf_path, False, None)) as mock_remed:
            # Case C: remediated=None (独立调用) -> 正常调用 remediate_pdf_pages_lossless
            extract_tables_from_pdf(pdf_path, remediated=None)
            mock_remed.assert_called_once()
            mock_do_extract.assert_called_once_with(pdf_path, use_ocr_fallback=True, was_remediated=False)

    # -------------------------------------------------------------------------
    # Item 3: 烘焙分支的宽高严禁 swap（尺寸与视口方向完全一致，严防图像压扁畸变）
    # -------------------------------------------------------------------------
    def test_baking_preserves_aspect_ratio_and_no_squash(self):
        """测试扫描版旋转烘焙分支对侧向页面 (rotation=90) 的宽高比保持，验证调用真实 bake_pdf_rotations 且不进行错误的宽高互换。"""
        # 创建一个 400x800 (窄高) 的页面，并设置 rotation=90
        # 旋转 90 度后，视口空间应该为 800 宽 x 400 高 (扁平横向)
        doc = fitz.open()
        p = doc.new_page(width=400, height=800)
        p.draw_rect(fitz.Rect(50, 50, 350, 750), color=(1, 0, 0), width=2)
        p.set_rotation(90)
        pdf_path = os.path.join(self.temp_dir, "sideways_scanned.pdf")
        doc.save(pdf_path)
        doc.close()

        # 调用实际生产函数 bake_pdf_rotations
        baked_pdf_path, was_baked, tmp_clean = bake_pdf_rotations(pdf_path, dpi=150)
        self.assertTrue(was_baked)
        self.assertIsNotNone(tmp_clean)
        self.assertTrue(os.path.exists(baked_pdf_path))

        # 校验烘焙后的 PDF
        baked_doc = fitz.open(baked_pdf_path)
        baked_page = baked_doc[0]
        # 烘焙后页面应为 0 旋转，且宽高严格与原视口尺寸一致 (800 x 400)，绝不能被倒置为 400 x 800
        self.assertEqual(baked_page.rotation, 0)
        self.assertEqual(baked_page.rect.width, 800.0)
        self.assertEqual(baked_page.rect.height, 400.0)

        # 验证插入图片的比例与页面完全贴合 (宽高比 2.0，未被压扁为 0.5)
        aspect_ratio = baked_page.rect.width / baked_page.rect.height
        self.assertAlmostEqual(aspect_ratio, 2.0, places=2)
        baked_doc.close()

    def test_bake_pdf_rotations_multipage(self):
        """测试 bake_pdf_rotations 对包含不同旋转角 (90°, 0°, 270°) 的多页 PDF 进行烘焙，验证全部回正且尺寸与视口一致。"""
        doc = fitz.open()
        p0 = doc.new_page(width=400, height=600)  # 400x600, rot 90 -> visual 600x400
        p0.set_rotation(90)
        p1 = doc.new_page(width=500, height=700)  # 500x700, rot 0  -> visual 500x700
        p2 = doc.new_page(width=300, height=800)  # 300x800, rot 270 -> visual 800x300
        p2.set_rotation(270)
        multi_pdf = os.path.join(self.temp_dir, "multipage_rot.pdf")
        doc.save(multi_pdf)
        doc.close()

        baked_pdf, was_baked, _ = bake_pdf_rotations(multi_pdf, dpi=150)
        self.assertTrue(was_baked)
        baked_doc = fitz.open(baked_pdf)
        self.assertEqual(len(baked_doc), 3)

        self.assertEqual(baked_doc[0].rotation, 0)
        self.assertEqual(baked_doc[0].rect.width, 600.0)
        self.assertEqual(baked_doc[0].rect.height, 400.0)

        self.assertEqual(baked_doc[1].rotation, 0)
        self.assertEqual(baked_doc[1].rect.width, 500.0)
        self.assertEqual(baked_doc[1].rect.height, 700.0)

        self.assertEqual(baked_doc[2].rotation, 0)
        self.assertEqual(baked_doc[2].rect.width, 800.0)
        self.assertEqual(baked_doc[2].rect.height, 300.0)
        baked_doc.close()

    # -------------------------------------------------------------------------
    # Item 4: 切图模型轨对 180° 翻转的安全抑制 (OOD 保护)
    # -------------------------------------------------------------------------
    def test_detect_crop_orientation_suppresses_model_180(self):
        """测试 detect_crop_orientation 在视觉模型轨预测 180° 时安全抑制，防止分布外误判导致 VLM 串行丢弃。"""
        dummy_crop = Image.new("RGB", (300, 100), color="white")

        # 模拟视觉检测器预测 180° (倒置)
        mock_detector = MagicMock()
        mock_detector.is_available.return_value = True
        mock_detector.predict.return_value = {
            "detected_angle": 180,
            "correction_angle": 180,
            "confidence": 0.95,
            "margin": 0.80,
            "probs": [0.02, 0.01, 0.95, 0.02],
            "needs_rotation": True,
            "source": "model"
        }

        # Case A: 模型轨预测 180° -> 必须安全抑制为 needs_rotation=False, detected_angle=0
        res = detect_crop_orientation(dummy_crop, detector=mock_detector)
        self.assertFalse(res["needs_rotation"])
        self.assertEqual(res["detected_angle"], 0)
        self.assertEqual(res["correction_angle"], 0)
        self.assertEqual(res["source"], "model_crop_180_suppressed")

        # Case B: 模型轨预测 90° -> 正常采纳
        mock_detector.predict.return_value = {
            "detected_angle": 90,
            "correction_angle": 270,
            "confidence": 0.90,
            "margin": 0.70,
            "probs": [0.05, 0.90, 0.02, 0.03],
            "needs_rotation": True,
            "source": "model"
        }
        res_90 = detect_crop_orientation(dummy_crop, detector=mock_detector)
        self.assertTrue(res_90["needs_rotation"])
        self.assertEqual(res_90["detected_angle"], 90)
        self.assertEqual(res_90["correction_angle"], 270)

        # Case C: 矢量文字检测轨 (Track 1) 检出 180° -> 真实字符矩阵信息，正常采纳
        doc = fitz.open()
        p = doc.new_page(width=500, height=500)
        mat180 = fitz.Matrix(180)
        p.insert_text(fitz.Point(300, 300), "Upside Down Vector Table Row Text", fontsize=12, morph=(fitz.Point(300, 300), mat180))
        res_vec_180 = detect_crop_orientation(dummy_crop, page=p, rect=[200, 200, 400, 400], detector=mock_detector)
        self.assertTrue(res_vec_180["needs_rotation"])
        self.assertEqual(res_vec_180["detected_angle"], 180)
        self.assertEqual(res_vec_180["correction_angle"], 180)
        self.assertEqual(res_vec_180["source"], "vector_text")
        doc.close()

    # -------------------------------------------------------------------------
    # Item 5: ocr_client 本地检测器缺失时动态启用 useDocOrientationClassify
    # -------------------------------------------------------------------------
    def test_ocr_client_dynamic_doc_orientation_classify(self):
        """测试 ocr_client.get_optional_payload 在本地检测器可用时禁用在线翻转，不可用时动态开启在线翻转。"""
        # Case A: 本地检测器就绪 -> useDocOrientationClassify=False (防二次重复翻转)
        with patch("doc_orientation_detector.DocOrientationDetector.is_available", return_value=True):
            payload_avail = ocr_client.get_optional_payload()
            self.assertFalse(payload_avail["useDocOrientationClassify"])

        # Case B: 本地检测器不可用 (模型缺失或运行异常降级) -> useDocOrientationClassify=True (云端方向防线兜底)
        with patch("doc_orientation_detector.DocOrientationDetector.is_available", return_value=False):
            payload_unavail = ocr_client.get_optional_payload()
            self.assertTrue(payload_unavail["useDocOrientationClassify"])

        # Case C: 用户在 config 中显式配置 -> 尊重用户显式配置 (支持 UPPER_CASE 与 camelCase)
        cfg_force_false = {"USE_DOC_ORIENTATION_CLASSIFY": False}
        payload_override = ocr_client.get_optional_payload(cfg_force_false)
        self.assertFalse(payload_override["useDocOrientationClassify"])

        cfg_force_true = {"USE_DOC_ORIENTATION_CLASSIFY": True}
        payload_override2 = ocr_client.get_optional_payload(cfg_force_true)
        self.assertTrue(payload_override2["useDocOrientationClassify"])

        cfg_camel_false = {"useDocOrientationClassify": False}
        payload_override3 = ocr_client.get_optional_payload(cfg_camel_false)
        self.assertFalse(payload_override3["useDocOrientationClassify"])

    # -------------------------------------------------------------------------
    # 深度增强：旋转 Native PDF 的文本对齐与结构树坐标归一化
    # -------------------------------------------------------------------------
    def test_search_for_visual_on_rotated_page(self):
        """测试 _search_for_visual 在 rotation=90 页面上将搜索结果准确映射至视口空间。"""
        doc = fitz.open()
        p = doc.new_page(width=500, height=700)
        # 在 (100, 100) 插入文本
        p.insert_text(fitz.Point(100, 100), "TargetLabel")
        p.set_rotation(90)  # 视口尺寸变为 700x500
        pdf_path = os.path.join(self.temp_dir, "test_vis_search.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_r = fitz.open(pdf_path)
        page = doc_r[0]
        rects = _search_for_visual(page, "TargetLabel")
        self.assertTrue(len(rects) > 0)
        r = rects[0]
        # 在 90 度旋转下，视口空间中 x 位于 0..700, y 位于 0..500
        self.assertGreaterEqual(r.x0, 0.0)
        self.assertLessEqual(r.x1, 700.0)
        self.assertGreaterEqual(r.y0, 0.0)
        self.assertLessEqual(r.y1, 500.0)
        doc_r.close()

    def test_extract_page_via_text_alignment_on_rotated_page(self):
        """测试 _extract_page_via_text_alignment 在旋转 90 度 native PDF 上基于视觉 words 和视口宽高提取表格。"""
        doc = fitz.open()
        p = doc.new_page(width=600, height=800)
        # 模拟侧向表格：在 unrotated 空间中，文字顺时针旋转 -90 度绘制
        # 旋转 90 度后，视口空间为 800 x 600
        p.insert_text(fitz.Point(200, 250), "Col B", morph=(fitz.Point(200, 250), fitz.Matrix(-90)))
        p.insert_text(fitz.Point(200, 400), "Col A", morph=(fitz.Point(200, 400), fitz.Matrix(-90)))
        p.insert_text(fitz.Point(300, 250), "Val 2", morph=(fitz.Point(300, 250), fitz.Matrix(-90)))
        p.insert_text(fitz.Point(300, 400), "Val 1", morph=(fitz.Point(300, 400), fitz.Matrix(-90)))
        p.set_rotation(90)
        pdf_path = os.path.join(self.temp_dir, "rot_align_test.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_r = fitz.open(pdf_path)
        res = _extract_page_via_text_alignment(doc_r, 0, pdf_path)
        self.assertTrue(len(res) > 0)
        tbl = res[0]
        df = tbl.get("df")
        self.assertIsNotNone(df)
        self.assertFalse(df.empty)
        # 校验 bbox 宽度严格为视口宽度 (800.0)，绝不能为未旋转的 mediabox 宽度 (600.0)
        bbox = tbl.get("bbox")
        self.assertIsNotNone(bbox)
        self.assertEqual(bbox[2], 800.0)
        self.assertEqual(doc_r[0].rect.width, 800.0)
        doc_r.close()

    def test_extract_page_via_text_alignment_with_caption_on_rotated_page(self):
        """测试 _extract_page_via_text_alignment 在旋转 90 度 native PDF 上带标题的情况。"""
        doc = fitz.open()
        p = doc.new_page(width=600, height=800)
        p.insert_text(fitz.Point(100, 250), "Table 1. Experimental Overview", morph=(fitz.Point(100, 250), fitz.Matrix(-90)))
        p.insert_text(fitz.Point(200, 250), "Col B", morph=(fitz.Point(200, 250), fitz.Matrix(-90)))
        p.insert_text(fitz.Point(200, 400), "Col A", morph=(fitz.Point(200, 400), fitz.Matrix(-90)))
        p.insert_text(fitz.Point(300, 250), "Val 2", morph=(fitz.Point(300, 250), fitz.Matrix(-90)))
        p.insert_text(fitz.Point(300, 400), "Val 1", morph=(fitz.Point(300, 400), fitz.Matrix(-90)))
        p.set_rotation(90)
        pdf_path = os.path.join(self.temp_dir, "rot_align_cap_test.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_r = fitz.open(pdf_path)
        res = _extract_page_via_text_alignment(doc_r, 0, pdf_path)
        self.assertTrue(len(res) > 0)
        tbl = res[0]
        df = tbl.get("df")
        self.assertIsNotNone(df)
        self.assertEqual(tbl.get("label"), "Table 1")
        self.assertIn("Experimental Overview", tbl.get("title", ""))
        self.assertEqual(tbl.get("bbox")[2], 800.0)
        doc_r.close()

    def test_structure_tree_bbox_normalized_on_rotated_page(self):
        """测试 extract_via_structure_tree 在旋转页面上将 bbox 正确转换至视觉视口空间。"""
        doc = fitz.open()
        p = doc.new_page(width=600, height=800)
        p.insert_text(fitz.Point(200, 200), "Cell 1")
        p.set_rotation(90)  # 视口空间为 800 x 600
        pdf_path = os.path.join(self.temp_dir, "rot_struct_test.pdf")
        doc.save(pdf_path)
        doc.close()

        class DummySE:
            def __init__(self, page, role, text, mcid):
                self.page = page
                self.role = role
                self.text = text
                self.mcid = mcid

        class DummyTextItem:
            def __init__(self, text, x, y, width=50.0, mcid=0):
                self.text = text
                self.x = x
                self.y = y
                self.width = width
                self.mcid = mcid

        dummy_se = [
            DummySE(page=1, role='TH', text='Header', mcid=1),
            DummySE(page=1, role='TD', text='Cell 1', mcid=2),
        ]
        # 在 unrotated PDF 坐标系下，y 取值范围为 200..600
        dummy_items = [
            DummyTextItem("Header", 150.0, 600.0, width=50.0, mcid=1),
            DummyTextItem("Cell 1", 300.0, 400.0, width=50.0, mcid=2),
        ]

        with patch("pdf_inspector.extract_structure_elements", return_value=dummy_se), \
             patch("pdf_inspector.extract_text_with_positions", return_value=dummy_items):
            res = extract_via_structure_tree(pdf_path, pages=[0])
            self.assertTrue(len(res) > 0)
            bbox = res[0].get("bbox")
            self.assertIsNotNone(bbox)
            # 在 90 度旋转视口下，x 坐标最大值不超过 800，y 坐标最大值不超过 600
            self.assertLessEqual(bbox[2], 800.0)
            self.assertLessEqual(bbox[3], 600.0)
            self.assertGreaterEqual(bbox[0], 0.0)
            self.assertGreaterEqual(bbox[1], 0.0)


if __name__ == "__main__":
    unittest.main()
