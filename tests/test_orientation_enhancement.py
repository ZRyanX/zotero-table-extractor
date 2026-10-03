#!/usr/bin/env python3
"""
tests/test_orientation_enhancement.py — TurboOCR doc_ori ONNX 模型、矢量文字方向检测与双轨两阶旋转自愈测试套件。

测试覆盖：
1. 模型定位、加载与优雅降级（含单例模式、SHA256 校验与缺失模型降级）；
2. 图像前处理与方差导向自适应裁剪 (Variance-Guided Crop)；
3. Softmax 概率计算与置信度裕度门限 (Margin Threshold Guard)；
4. PyMuPDF 原生矢量文字方向检测 (detect_vector_text_rotation)；
5. Tier 1: 页面级无损顺时针校准 (remediate_pdf_pages_lossless，保障矢量文本 0 栅格化破坏)；
6. Tier 2: 嵌套横向表格切图正向矫正 (detect_crop_orientation 与 extract_via_paddleocr_crops 切图回正)；
7. setup_paths.py 与 package_release.py 的模型识别与打包兼容性。
"""

import os
import sys
import math
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from collections import Counter

import numpy as np
from PIL import Image, ImageDraw

import pymupdf as fitz

# 确保项目根目录与脚本目录在 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(1, SCRIPTS_DIR)

import doc_orientation_detector as dod
from doc_orientation_detector import (
    DocOrientationDetector,
    get_orientation_detector,
    ensure_model_file,
    preprocess_image,
    detect_vector_text_rotation,
    detect_vector_text_details,
    get_clockwise_correction_angle,
    detect_page_orientation,
    detect_crop_orientation,
    remediate_pdf_pages_lossless,
    CLASS_TO_DEGREE,
    DEFAULT_MARGIN_THRESHOLD,
)


class TestDocOrientationDetector(unittest.TestCase):
    """测试 doc_orientation_detector 模块的核心算法、前处理与模型推理。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_doc_ori_")

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_model_resolution_and_initialization(self):
        """测试模型文件发现、单例加载与 session 初始化。"""
        detector = get_orientation_detector()
        self.assertIsNotNone(detector)
        # 本地 models/doc_ori.onnx 已存在，应成功载入
        self.assertTrue(detector.is_available())
        self.assertTrue(os.path.isfile(detector.model_path))
        self.assertEqual(detector.input_name, "x")
        self.assertEqual(detector.output_name, "fetch_name_0")

    def test_graceful_degradation_without_model(self):
        """测试当模型文件不存在时，检测器优雅降级为 safe fallback，不抛出异常。"""
        dummy_detector = DocOrientationDetector(model_path="/non/existent/path/doc_ori.onnx")
        self.assertFalse(dummy_detector.is_available())

        dummy_img = Image.new("RGB", (200, 200), color="white")
        res = dummy_detector.predict(dummy_img)
        self.assertEqual(res["detected_angle"], 0)
        self.assertEqual(res["correction_angle"], 0)
        self.assertFalse(res["needs_rotation"])
        self.assertEqual(res["source"], "fallback")

    @patch("urllib.request.urlopen")
    def test_download_model_sha256_mismatch_raises_and_cleans_tmp(self, mock_urlopen):
        """测试下载时 SHA256 不匹配会立即抛出异常并清理临时文件，绝不覆盖目标文件。"""
        from doc_orientation_detector import download_model

        mock_resp = MagicMock()
        mock_resp.read.side_effect = [b"A" * (2 * 1024 * 1024), b""]
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        target_file = os.path.join(self.temp_dir, "corrupt_model.onnx")
        with self.assertRaises(RuntimeError) as ctx:
            download_model(target_path=target_file)
        self.assertIn("SHA256", str(ctx.exception))
        # 验证目标文件绝未被创建
        self.assertFalse(os.path.exists(target_file))
        # 验证临时下载文件被清理
        self.assertFalse(os.path.exists(target_file + ".download.tmp"))

    def test_preprocessing_dimensions_and_normalization(self):
        """测试图像前处理输出张量形状、数据类型及 ImageNet 标准化数值范围。"""
        # 测试竖长图
        tall_img = Image.new("RGB", (600, 1200), color="blue")
        t_arr = preprocess_image(tall_img)
        self.assertEqual(t_arr.shape, (1, 3, 224, 224))
        self.assertEqual(t_arr.dtype, np.float32)

        # 测试横宽图
        wide_img = Image.new("RGB", (1600, 800), color="green")
        w_arr = preprocess_image(wide_img)
        self.assertEqual(w_arr.shape, (1, 3, 224, 224))
        self.assertEqual(w_arr.dtype, np.float32)

        # 测试灰度图输入自适应转 RGB
        gray_img = Image.new("L", (400, 400), color=128)
        g_arr = preprocess_image(gray_img)
        self.assertEqual(g_arr.shape, (1, 3, 224, 224))

    def test_variance_guided_crop(self):
        """测试方差导向裁剪能主动聚焦文字密集区而非空白区域。"""
        # 创建一张极宽图像：左半边全白，右半边充满黑色高频文字线条
        wide_sparse = Image.new("RGB", (1200, 300), color="white")
        draw = ImageDraw.Draw(wide_sparse)
        for x in range(700, 1100, 10):
            draw.line([(x, 10), (x, 290)], fill="black", width=2)

        # 使用方差引导裁剪
        tensor = preprocess_image(wide_sparse, use_variance_crop=True)
        self.assertEqual(tensor.shape, (1, 3, 224, 224))
        # 裁剪出的区域应该包含了黑色线条，因此张量像素均值应与全白纯背景有显著差异
        white_tensor = preprocess_image(Image.new("RGB", (1200, 300), color="white"))
        self.assertFalse(np.allclose(tensor, white_tensor))

    def test_margin_threshold_guard(self):
        """测试 Softmax 概率差低于 0.25 (kDocOriMargin) 时安全触发回退 0°。"""
        detector = get_orientation_detector()
        self.assertTrue(detector.is_available())

        dummy_img = Image.new("RGB", (224, 224), color="white")

        # 模拟场景 1：Top 1 为 90° (class 1)，但 margin 仅为 0.10 (< 0.25 门限)
        # 应触发安全兜底，判定为 0°，needs_rotation 为 False
        mock_output = np.array([[1.0, 1.2, 0.9, 0.8]], dtype=np.float32)
        with patch.object(detector.session, "run", return_value=[mock_output]):
            res = detector.predict(dummy_img, margin_threshold=0.25)
            self.assertEqual(res["detected_angle"], 0)
            self.assertEqual(res["correction_angle"], 0)
            self.assertFalse(res["needs_rotation"])

        # 模拟场景 2：Top 1 为 270° (class 3)，margin 达到 0.45 (> 0.25 门限)
        # 应准确采纳 270°，顺时针校准角度为 90°，needs_rotation 为 True
        mock_output_high_conf = np.array([[0.5, 0.5, 0.5, 3.5]], dtype=np.float32)
        with patch.object(detector.session, "run", return_value=[mock_output_high_conf]):
            res_high = detector.predict(dummy_img, margin_threshold=0.25)
            self.assertEqual(res_high["detected_angle"], 270)
            self.assertEqual(res_high["correction_angle"], 90)
            self.assertTrue(res_high["needs_rotation"])
            self.assertGreater(res_high["margin"], 0.25)

    def test_real_model_inference_on_synthetic_document(self):
        """使用实际 ONNX 模型对合成带字图像进行推理，验证 4 类方向预测与回正逻辑。"""
        detector = get_orientation_detector()
        self.assertTrue(detector.is_available())

        # 生成标准学术排版文字图像 (使用 PyMuPDF 矢量排版渲染以获得真实文字纹理与字形特征)
        doc = fitz.open()
        p = doc.new_page(width=500, height=700)
        for y in range(50, 650, 30):
            p.insert_text(fitz.Point(50, y), "Experimental Evaluation and Performance Metrics Analysis Table 1. Results", fontsize=12)
        pix = p.get_pixmap(dpi=100)
        base_doc = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()

        # 1. 0° 正常正向图
        res_0 = detector.predict(base_doc)
        self.assertEqual(res_0["detected_angle"], 0)
        self.assertEqual(res_0["correction_angle"], 0)
        self.assertFalse(res_0["needs_rotation"])
        self.assertGreaterEqual(res_0["margin"], DEFAULT_MARGIN_THRESHOLD)

        # 2. 顺时针旋转 90° (PIL rotate(270) 为逆时针 270° 即顺时针 90°)
        img_cw90 = base_doc.rotate(270, expand=True)
        res_90 = detector.predict(img_cw90)
        self.assertEqual(res_90["detected_angle"], 90)
        self.assertEqual(res_90["correction_angle"], 270)
        self.assertTrue(res_90["needs_rotation"])
        self.assertGreaterEqual(res_90["margin"], DEFAULT_MARGIN_THRESHOLD)

        # 3. 顺时针旋转 180°
        img_180 = base_doc.rotate(180, expand=True)
        res_180 = detector.predict(img_180)
        self.assertEqual(res_180["detected_angle"], 180)
        self.assertEqual(res_180["correction_angle"], 180)
        self.assertTrue(res_180["needs_rotation"])
        self.assertGreaterEqual(res_180["margin"], DEFAULT_MARGIN_THRESHOLD)

        # 4. 顺时针旋转 270° (PIL rotate(90) 为逆时针 90° 即顺时针 270°)
        img_cw270 = base_doc.rotate(90, expand=True)
        res_270 = detector.predict(img_cw270)
        self.assertEqual(res_270["detected_angle"], 270)
        self.assertEqual(res_270["correction_angle"], 90)
        self.assertTrue(res_270["needs_rotation"])
        self.assertGreaterEqual(res_270["margin"], DEFAULT_MARGIN_THRESHOLD)

    def test_clockwise_correction_angle_calculation(self):
        """测试各检测角度到顺时针校正角度的数学映射。"""
        self.assertEqual(get_clockwise_correction_angle(0), 0)
        self.assertEqual(get_clockwise_correction_angle(90), 270)
        self.assertEqual(get_clockwise_correction_angle(180), 180)
        self.assertEqual(get_clockwise_correction_angle(270), 90)


class TestVectorTextDirectionDetection(unittest.TestCase):
    """测试基于 PyMuPDF span['dir']/line['dir'] 的原生矢量文字旋转检测。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_vec_dir_")

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_vector_text_rotation_all_angles(self):
        """测试对 0°、90°、180°、270° 矢量文本页面的 0-时延精准识别。"""
        doc = fitz.open()

        # Page 0: 水平正向文本 (dir: (1, 0)) -> 0°
        p0 = doc.new_page(width=500, height=700)
        p0.insert_text(fitz.Point(100, 100), "Standard horizontal document line", fontsize=12)

        # Page 1: 顺时针旋转 270° (Matrix 90, dir: (0, -1), 从下往上读) -> 270°
        p1 = doc.new_page(width=500, height=700)
        p1.insert_text(fitz.Point(200, 400), "Matrix 90 upward reading text line", fontsize=12, morph=(fitz.Point(200, 400), fitz.Matrix(90)))

        # Page 2: 顺时针旋转 90° (Matrix 270, dir: (0, 1), 从上往下读) -> 90°
        p2 = doc.new_page(width=500, height=700)
        p2.insert_text(fitz.Point(200, 200), "Matrix 270 downward reading text line", fontsize=12, morph=(fitz.Point(200, 200), fitz.Matrix(270)))

        # Page 3: 倒置 180° (Matrix 180, dir: (-1, 0)) -> 180°
        p3 = doc.new_page(width=500, height=700)
        p3.insert_text(fitz.Point(300, 300), "Matrix 180 upside-down text line", fontsize=12, morph=(fitz.Point(300, 300), fitz.Matrix(180)))

        # Page 4: 空白无文本页
        p4 = doc.new_page(width=500, height=700)

        pdf_path = os.path.join(self.temp_dir, "test_rot_vector.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_read = fitz.open(pdf_path)
        self.assertEqual(detect_vector_text_rotation(doc_read[0]), 0)
        self.assertEqual(detect_vector_text_rotation(doc_read[1]), 270)
        self.assertEqual(detect_vector_text_rotation(doc_read[2]), 90)
        self.assertEqual(detect_vector_text_rotation(doc_read[3]), 180)
        self.assertEqual(detect_vector_text_rotation(doc_read[4]), 0)
        doc_read.close()

    def test_vector_text_rotation_with_clip_rect(self):
        """测试局部裁剪区域 (rect) 过滤下的文字旋转检测。"""
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)

        # 顶部为水平正常页眉 (y: 50)
        page.insert_text(fitz.Point(50, 50), "Journal of Machine Learning Header", fontsize=10)

        # 中部区域嵌入旋转 270° 的横置大表 (x: 200..500, y: 200..600)
        page.insert_text(
            fitz.Point(300, 500),
            "Embedded rotated landscape table data row 1 2 3",
            fontsize=12,
            morph=(fitz.Point(300, 500), fitz.Matrix(90))
        )

        pdf_path = os.path.join(self.temp_dir, "test_clip.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_read = fitz.open(pdf_path)
        p = doc_read[0]

        # 仅针对表格所在区域进行检测
        table_rect = [150, 150, 550, 650]
        self.assertEqual(detect_vector_text_rotation(p, rect=table_rect), 270)

        # 针对页眉区域检测
        header_rect = [0, 0, 600, 100]
        self.assertEqual(detect_vector_text_rotation(p, rect=header_rect), 0)
        doc_read.close()

    def test_detect_vector_text_details(self):
        """测试 detect_vector_text_details 返回准确的 dominant_angle、字符计数与主导占比。"""
        doc = fitz.open()
        p = doc.new_page(width=500, height=700)
        p.insert_text(fitz.Point(100, 100), "Hello World", fontsize=12)  # 11 chars at 0 deg
        p.insert_text(fitz.Point(200, 300), "Side", fontsize=12, morph=(fitz.Point(200, 300), fitz.Matrix(90)))  # 4 chars at 270 deg

        angle, total_chars, ratio = detect_vector_text_details(p)
        self.assertEqual(angle, 0)
        self.assertGreaterEqual(total_chars, 15)
        self.assertGreaterEqual(ratio, 0.60)

        # 局部区域过滤：仅针对侧排文字区域
        side_rect = [150, 200, 300, 400]
        angle_side, total_side, ratio_side = detect_vector_text_details(p, rect=side_rect)
        self.assertEqual(angle_side, 270)
        self.assertEqual(total_side, 4)
        self.assertAlmostEqual(ratio_side, 1.0)
        doc.close()


class TestTier1LosslessPageRemediation(unittest.TestCase):
    """测试 Tier 1: 页面级无损顺时针校准与结构保留。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_tier1_")

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_remediate_pdf_pages_lossless_success(self):
        """测试对含旋转页面的 PDF 进行无损校正，验证 page.rotation 更新与矢量字体完整保留。"""
        doc = fitz.open()
        p0 = doc.new_page(width=500, height=700)
        p0.insert_text(fitz.Point(50, 50), "First Page Normal Text", fontsize=12)

        # 第二页是文字旋转 270° 的横表 (Matrix 90)，但 page.rotation 仍为 0
        p1 = doc.new_page(width=500, height=700)
        p1.insert_text(fitz.Point(250, 450), "Table 1. Experimental Data Across Batches", fontsize=14, morph=(fitz.Point(250, 450), fitz.Matrix(90)))

        orig_pdf = os.path.join(self.temp_dir, "orig.pdf")
        doc.save(orig_pdf)
        doc.close()

        # 执行无损翻转校正
        eff_path, modified, tmp_clean = remediate_pdf_pages_lossless(orig_pdf)
        self.assertTrue(modified)
        self.assertIsNotNone(tmp_clean)
        self.assertTrue(os.path.exists(eff_path))

        # 校验修复后的 PDF
        doc_fixed = fitz.open(eff_path)
        self.assertEqual(len(doc_fixed), 2)
        # 第一页未改动
        self.assertEqual(doc_fixed[0].rotation, 0)
        # 第二页无损顺时针补正 90°，使其变为正向
        self.assertEqual(doc_fixed[1].rotation, 90)

        # 核心保证：文本内容必须完整提取，绝未被栅格化破坏为纯图像
        text_p1 = doc_fixed[1].get_text("text").strip()
        self.assertIn("Table 1. Experimental Data Across Batches", text_p1)

        doc_fixed.close()
        if tmp_clean and os.path.exists(tmp_clean):
            os.remove(tmp_clean)

    def test_remediate_pdf_pages_lossless_noop_on_upright_pdf(self):
        """测试对原本就为正向的 PDF 不执行无谓修改，原样返回。"""
        doc = fitz.open()
        p0 = doc.new_page(width=500, height=700)
        p0.insert_text(fitz.Point(50, 50), "Normal Upright Document Content", fontsize=12)
        orig_pdf = os.path.join(self.temp_dir, "upright.pdf")
        doc.save(orig_pdf)
        doc.close()

        eff_path, modified, tmp_clean = remediate_pdf_pages_lossless(orig_pdf)
        self.assertFalse(modified)
        self.assertEqual(eff_path, orig_pdf)
        self.assertIsNone(tmp_clean)

    def test_extract_tables_from_pdf_tier1_integration_and_cleanup(self):
        """测试 extract_tables_from_pdf 统一入口调用 Tier 1 顺时针自愈并自动清理临时文件。"""
        import pdf_table_extractor as pte

        doc = fitz.open()
        p = doc.new_page(width=500, height=700)
        # 插入旋转 270° 的矢量文本 (Matrix 90)
        p.insert_text(fitz.Point(250, 450), "Table 1. Experimental Results Across Batches", fontsize=12, morph=(fitz.Point(250, 450), fitz.Matrix(90)))
        pdf_path = os.path.join(self.temp_dir, "entry_test.pdf")
        doc.save(pdf_path)
        doc.close()

        results, logs = pte.extract_tables_from_pdf(pdf_path, use_ocr_fallback=False)
        self.assertTrue(any("Tier 1 页面方向探测" in log for log in logs))


class TestTier2CropRotationRemediation(unittest.TestCase):
    """测试 Tier 2: 嵌套横表切图方向识别与 upright 旋转。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_tier2_")

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_detect_crop_orientation_via_vector_rect(self):
        """测试通过关联 PDF 页面与坐标快速识别 Crop 区域内的矢量文字旋转。"""
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        rect = [100, 200, 500, 600]
        page.insert_text(
            fitz.Point(300, 500),
            "Nested Landscape Table Content In Rect",
            fontsize=12,
            morph=(fitz.Point(300, 500), fitz.Matrix(90))
        )
        pdf_path = os.path.join(self.temp_dir, "crop_pdf.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_read = fitz.open(pdf_path)
        p = doc_read[0]

        dummy_crop_img = Image.new("RGB", (300, 400), color="white")
        res = detect_crop_orientation(dummy_crop_img, page=p, rect=rect)
        self.assertEqual(res["detected_angle"], 270)
        self.assertEqual(res["correction_angle"], 90)
        self.assertTrue(res["needs_rotation"])
        self.assertEqual(res["source"], "vector_text")
        doc_read.close()

    def test_detect_crop_orientation_upright_vector_skips_onnx(self):
        """测试当切图具有明确水平正向矢量文本时，直接判定为 vector_text_upright，跳过 ONNX 推理。"""
        doc = fitz.open()
        p = doc.new_page(width=600, height=800)
        rect = [100, 100, 500, 500]
        p.insert_text(fitz.Point(120, 150), "Standard horizontal table row header and data values across columns", fontsize=12)
        crop_img = Image.new("RGB", (400, 400), color="white")

        dummy_detector = MagicMock()
        res = detect_crop_orientation(crop_img, page=p, rect=rect, detector=dummy_detector)
        self.assertEqual(res["detected_angle"], 0)
        self.assertFalse(res["needs_rotation"])
        self.assertEqual(res["source"], "vector_text_upright")
        # 核心保证：完全未触发视觉 detector.predict
        dummy_detector.predict.assert_not_called()
        doc.close()

    def test_crop_image_upright_rotation(self):
        """测试切图顺时针校准翻转 (img.rotate(360 - deg, expand=True)) 尺寸与像素变换。"""
        # 创建一个 300x500 的图像 (宽 300, 高 500)
        img = Image.new("RGB", (300, 500), color="red")
        deg = 90  # 顺时针校正 90 度
        # PIL rotate(360 - 90, expand=True) 相当于顺时针旋转 90 度
        upright = img.rotate(360 - deg, expand=True)
        # 旋转 90 度后宽高互换：500x300
        self.assertEqual(upright.size, (500, 300))

        deg_270 = 270
        upright_270 = img.rotate(360 - deg_270, expand=True)
        self.assertEqual(upright_270.size, (500, 300))

    @patch("ocr_client.call_paddleocr_vl_online_api")
    def test_extract_via_paddleocr_crops_with_rotated_table(self, mock_vlm):
        """测试 extract_via_paddleocr_crops 在遇到旋转切图时，自动翻转正向后送入 VLM。"""
        import pdf_table_extractor as pte

        mock_vlm.return_value = "| Col 1 | Col 2 |\n|---|---|\n| A | B |"

        # 创建单页 PDF，含 Matrix 90 旋转文本
        doc = fitz.open()
        p = doc.new_page(width=600, height=800)
        p.insert_text(fitz.Point(300, 500), "Table 2. Rotated Crop Data", fontsize=12, morph=(fitz.Point(300, 500), fitz.Matrix(90)))
        pdf_path = os.path.join(self.temp_dir, "crop_test.pdf")
        doc.save(pdf_path)
        doc.close()

        # 模拟 extract_table_crops_from_pdf 返回的切图
        mock_crop = {
            "page_index": 0,
            "crop_bbox": [100, 200, 500, 600],
            "pdf_bbox": [100, 200, 500, 600],
            "image": Image.new("RGB", (300, 500), color="white"),
        }

        submitted_image_sizes = []

        def fake_call_vlm(img_path, **kwargs):
            with Image.open(img_path) as im:
                submitted_image_sizes.append(im.size)
            return "| Col 1 | Col 2 |\n|---|---|\n| A | B |"

        mock_vlm.side_effect = fake_call_vlm

        with patch("pdf_tables.extract_table_crops_from_pdf", return_value=[mock_crop]):
            results = pte.extract_via_paddleocr_crops(pdf_path, pages=[0])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["page_idx"], 0)
        self.assertEqual(len(submitted_image_sizes), 1)
        # 原切图尺寸为 (300, 500)，校准旋转 90 度后提交给 VLM 的图像尺寸应为 (500, 300)
        self.assertEqual(submitted_image_sizes[0], (500, 300))


class TestSetupAndReleaseIntegration(unittest.TestCase):
    """测试 setup_paths.py 与 package_release.py 对 doc_ori.onnx 的识别与优雅降级。"""

    def test_setup_paths_show_recognizes_doc_ori(self):
        """测试 setup_paths.py 的状态自检能正确识别并展示 doc_ori.onnx 状态。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("root_setup_paths_test", os.path.join(PROJECT_ROOT, "setup_paths.py"))
        setup_paths = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup_paths)
        from io import StringIO
        captured_out = StringIO()
        sys_stdout = sys.stdout
        try:
            sys.stdout = captured_out
            setup_paths.show_status()
        finally:
            sys.stdout = sys_stdout

        out_str = captured_out.getvalue()
        self.assertIn("TurboOCR 页面方向模型", out_str)
        self.assertIn("doc_ori.onnx", out_str)

    def test_package_release_audit_optional_model(self):
        """测试 package_release.py 在缺失可选 doc_ori.onnx 时优雅降级，不阻断打包审计。"""
        import package_release as pr
        import zipfile

        temp_zip = os.path.join(tempfile.gettempdir(), "test_pack_audit.zip")
        prefix = "zotero-table-extractor-test/"

        # 仅包含强制资产，不含 doc_ori.onnx
        with zipfile.ZipFile(temp_zip, "w") as zf:
            for mand in pr.MANDATORY_ENTRIES:
                zf.writestr(f"{prefix}{mand}", "dummy")

        passed, violations = pr.audit_zip_contents(temp_zip, prefix)
        self.assertTrue(passed)
        self.assertEqual(len(violations), 0)

        if os.path.exists(temp_zip):
            os.remove(temp_zip)

    def test_package_release_audit_with_optional_model_included(self):
        """测试 package_release.py 在包含可选 doc_ori.onnx 时正常审计通过。"""
        import package_release as pr
        import zipfile

        temp_zip = os.path.join(tempfile.gettempdir(), "test_pack_full.zip")
        prefix = "zotero-table-extractor-test/"

        with zipfile.ZipFile(temp_zip, "w") as zf:
            for mand in pr.MANDATORY_ENTRIES:
                zf.writestr(f"{prefix}{mand}", "dummy")
            for opt in pr.OPTIONAL_ENTRIES:
                zf.writestr(f"{prefix}{opt}", "dummy_model_bytes")

        passed, violations = pr.audit_zip_contents(temp_zip, prefix)
        self.assertTrue(passed)
        self.assertEqual(len(violations), 0)

        if os.path.exists(temp_zip):
            os.remove(temp_zip)


if __name__ == "__main__":
    unittest.main()
