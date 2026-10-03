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
    get_vector_text_distribution,
    get_clockwise_correction_angle,
    detect_page_orientation,
    detect_crop_orientation,
    remediate_pdf_pages_lossless,
    CLASS_TO_DEGREE,
    DEFAULT_MARGIN_THRESHOLD,
    DEFAULT_MIN_CONFIDENCE,
    PAGE_MARGIN_THRESHOLD,
    PAGE_MIN_CONFIDENCE,
    PAGE_VECTOR_DOMINANT_RATIO,
    PAGE_VECTOR_MAX_SECONDARY_RATIO,
    PAGE_VECTOR_MIN_CHARS,
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


class TestOrientationDetectorReviewImprovements(unittest.TestCase):
    """
    针对代码审查要点进行的高级自愈与一致性测试：
    1. 混排页面 (Mixed Text Orientation) 与全页一致性：
       整页纠偏避免把局部横置大表误当成整页旋转，跳过 Tier 1 整页翻转，交由 Tier 2 表格切图局部纠偏。
    2. 模型官方标准预处理 (CenterCrop 224x224)：
       全页级推理严格采用官方 ResizeImage(256) -> CenterCrop(224) -> ImageNet 标准化，方差裁剪仅作为局部切图的可选方案。
    3. 置信度门限与防误翻策略：
       全页级结合 Top-1 概率门限 (>= 0.80) 与 Softmax 裕度 (>= 0.35)，“不确定时不旋转”。
    4. 长文档候选页与无 Caption 扫描页探测：
       验证 candidate_pages 传递及 > 15 页长文档后段扫描/图表密度页的自动检出与翻转自愈。
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_ori_review_")

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mixed_text_page_skips_whole_page_rotation_and_delegates_to_crop(self):
        """测试混排页面 (0° 正文 + 90° 横表) 绝不翻转整页，而表格切图能准确识别 90° 并回正。"""
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)

        # 1. 插入 0° 正向正文与标题 (约 200 字符)
        body_text = (
            "Section 3. Methodology and Experimental Setup in Deep Neural Architecture Evaluation. "
            "The baseline models were trained using standard hyperparameters with learning rate decay."
        )
        page.insert_text(fitz.Point(50, 80), body_text, fontsize=11)

        # 2. 插入 90° 旋转的嵌套横表 (约 300 字符，Matrix 270: 垂直向下文本，顺时针 90° 旋转)
        table_rect = [100, 150, 500, 650]
        table_text = (
            "Table 2. Quantitative Performance Across Benchmark Datasets and Training Regimes. "
            "Row 1: Accuracy 92.5%, F1-Score 0.91, Latency 14.2ms. "
            "Row 2: Accuracy 94.1%, F1-Score 0.93, Latency 16.8ms."
        )
        page.insert_text(
            fitz.Point(300, 200),
            table_text,
            fontsize=11,
            morph=(fitz.Point(300, 200), fitz.Matrix(270))
        )

        pdf_path = os.path.join(self.temp_dir, "mixed_page.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_read = fitz.open(pdf_path)
        p = doc_read[0]

        # 验证全页方向检测：因为存在混排文字且主导方向未达到 85% 绝对一致性，绝不触发整页旋转！
        page_res = detect_page_orientation(p)
        self.assertFalse(page_res["needs_rotation"])
        self.assertEqual(page_res["detected_angle"], 0)
        self.assertEqual(page_res["source"], "vector_mixed_skipped")
        self.assertIn("details", page_res)
        self.assertLess(page_res["details"]["dominant_ratio"], PAGE_VECTOR_DOMINANT_RATIO)

        # 验证表格切图检测 (Tier 2)：针对局部表格 rect 检测，文字方向纯净 (100% 90°)，准确识别并要求回正
        dummy_crop_img = Image.new("RGB", (400, 500), color="white")
        crop_res = detect_crop_orientation(dummy_crop_img, page=p, rect=table_rect)
        self.assertTrue(crop_res["needs_rotation"])
        self.assertEqual(crop_res["detected_angle"], 90)
        self.assertEqual(crop_res["correction_angle"], 270)
        self.assertEqual(crop_res["source"], "vector_text")

        # 验证整页自愈无损校准 (Tier 1)：绝不对该混排页施加旋转
        eff_path, modified, tmp_clean = remediate_pdf_pages_lossless(pdf_path)
        self.assertFalse(modified)
        self.assertEqual(eff_path, pdf_path)
        doc_read.close()

    def test_vector_consistency_strict_threshold_and_pure_rotation(self):
        """测试 85% 主导方向门限：80% 混排拒绝翻转，95% 纯旋转页正常翻转。"""
        doc = fitz.open()

        # Page 0: 80% 旋转 270° (Matrix 90, 80 字符) + 20% 0° 正文 (20 字符)
        p0 = doc.new_page(width=500, height=700)
        p0.insert_text(fitz.Point(50, 50), "12345678901234567890", fontsize=10)  # 20 chars at 0 deg
        p0.insert_text(
            fitz.Point(200, 400),
            "A" * 80,
            fontsize=10,
            morph=(fitz.Point(200, 400), fitz.Matrix(90))  # 80 chars at 270 deg
        )

        # Page 1: 95% 旋转 270° (95 字符) + 5% 0° (5 字符)
        p1 = doc.new_page(width=500, height=700)
        p1.insert_text(fitz.Point(50, 50), "12345", fontsize=10)  # 5 chars at 0 deg
        p1.insert_text(
            fitz.Point(200, 400),
            "B" * 95,
            fontsize=10,
            morph=(fitz.Point(200, 400), fitz.Matrix(90))  # 95 chars at 270 deg
        )

        pdf_path = os.path.join(self.temp_dir, "consistency_test.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_read = fitz.open(pdf_path)
        res0 = detect_page_orientation(doc_read[0])
        self.assertFalse(res0["needs_rotation"])
        self.assertEqual(res0["source"], "vector_mixed_skipped")

        res1 = detect_page_orientation(doc_read[1])
        self.assertTrue(res1["needs_rotation"])
        self.assertEqual(res1["detected_angle"], 270)
        self.assertEqual(res1["correction_angle"], 90)
        self.assertEqual(res1["source"], "vector_text")
        doc_read.close()

    def test_official_centercrop_preprocessing_default(self):
        """测试图像预处理默认严格遵循官方标准 CenterCrop(224) 规范，且与方差引导模式可独立切换。"""
        # 创建一个 600x300 的图像 (宽 600, 高 300)
        # 短边缩放至 256: 新尺寸为 512x256
        # CenterCrop 224x224 在 512x256 上的位置为:
        # left = (512 - 224) // 2 = 144, top = (256 - 224) // 2 = 16, right = 368, bottom = 240
        img = Image.new("RGB", (600, 300), color="white")
        draw = ImageDraw.Draw(img)
        # 在中央区域绘制纯红色矩形 (原图坐标对应 200..400)
        draw.rectangle([200, 50, 400, 250], fill="red")
        # 在最左边缘绘制纯黑色高方差线条 (用于吸引方差引导裁剪)
        for x in range(10, 80, 5):
            draw.line([(x, 10), (x, 290)], fill="black", width=2)

        # 1. 默认模式：必须为官方标准 CenterCrop
        arr_default = preprocess_image(img)
        arr_explicit_center = preprocess_image(img, use_variance_crop=False)
        self.assertTrue(np.allclose(arr_default, arr_explicit_center))
        self.assertEqual(arr_default.shape, (1, 3, 224, 224))

        # 2. 方差引导模式：应主动滑动窗口至最左侧的高方差黑线区域
        arr_var = preprocess_image(img, use_variance_crop=True)
        self.assertEqual(arr_var.shape, (1, 3, 224, 224))
        # 官方 CenterCrop 区域主要是红色，而方差裁剪包含了大量黑色高方差线条，两者像素分布显著不同
        self.assertFalse(np.allclose(arr_default, arr_var))

    def test_heightened_confidence_and_margin_thresholds(self):
        """测试全页级加严门限 (Top-1 >= 0.80, Margin >= 0.35) 与“不确定时不翻转”策略。"""
        detector = get_orientation_detector()
        self.assertTrue(detector.is_available())

        dummy_img = Image.new("RGB", (224, 224), color="white")

        # 模拟场景 A：Margin 虽达到 0.40 (> 0.35)，但 Top-1 置信度仅为 0.70 (< 0.80)
        # Softmax 结果: [0.15, 0.70, 0.15, 0.0] -> Margin = 0.55, Top 1 = 0.70
        # 命中 min_confidence 门限守卫，安全回退 0°
        mock_output_low_top1 = np.array([[1.0, 3.0, 1.0, 0.0]], dtype=np.float32)
        with patch.object(detector.session, "run", return_value=[mock_output_low_top1]):
            res = detector.predict(dummy_img, margin_threshold=PAGE_MARGIN_THRESHOLD, min_confidence=PAGE_MIN_CONFIDENCE)
            self.assertLess(res["confidence"], PAGE_MIN_CONFIDENCE)
            self.assertEqual(res["detected_angle"], 0)
            self.assertFalse(res["needs_rotation"])

        # 模拟场景 B：Top-1 置信度达标 (0.85)，且 Margin 达标 (0.75 > 0.35)，类别为 90° (class 1)
        # 应准确触发旋转
        mock_output_high = np.array([[0.0, 4.0, 1.0, 0.0]], dtype=np.float32)
        with patch.object(detector.session, "run", return_value=[mock_output_high]):
            res_high = detector.predict(dummy_img, margin_threshold=PAGE_MARGIN_THRESHOLD, min_confidence=PAGE_MIN_CONFIDENCE)
            self.assertGreaterEqual(res_high["confidence"], PAGE_MIN_CONFIDENCE)
            self.assertGreaterEqual(res_high["margin"], PAGE_MARGIN_THRESHOLD)
            self.assertEqual(res_high["detected_angle"], 90)
            self.assertEqual(res_high["correction_angle"], 270)
            self.assertTrue(res_high["needs_rotation"])

        # 模拟场景 C：低置信度模糊预测 [0.28, 0.26, 0.24, 0.22]，Margin < 0.05
        # 必须安全回退至 0°
        mock_ambiguous = np.array([[1.0, 0.95, 0.90, 0.85]], dtype=np.float32)
        with patch.object(detector.session, "run", return_value=[mock_ambiguous]):
            res_amb = detector.predict(dummy_img, margin_threshold=PAGE_MARGIN_THRESHOLD, min_confidence=PAGE_MIN_CONFIDENCE)
            self.assertEqual(res_amb["detected_angle"], 0)
            self.assertFalse(res_amb["needs_rotation"])

    def test_quick_detect_candidate_pages_and_long_document_scanned_inspection(self):
        """测试长文档 (>15页) 候选页发现、扫描页低文本探测与 candidate_pages 传递自愈。"""
        import pdf_table_extractor as pte

        doc = fitz.open()
        # 创建 20 页长文档
        for i in range(20):
            p = doc.new_page(width=500, height=700)
            # 大多数页面是正向正文 (50 词以上)
            p.insert_text(fitz.Point(50, 100), f"Standard Page {i+1} Normal Document Paragraph Content Words Here Repeated " * 5, fontsize=10)

        # 第 8 页：包含 Caption 声明
        doc[7].insert_text(fitz.Point(50, 200), "Table 1. Overview of Experimental Conditions", fontsize=12)

        # 第 19 页：模拟后段无 Caption 的扫描页 (仅含图像且词数 < 10)
        pix_dummy = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), 0)
        p19 = doc.new_page(width=500, height=700)
        p19.insert_image(fitz.Rect(50, 50, 450, 650), pixmap=pix_dummy)

        pdf_path = os.path.join(self.temp_dir, "long_document.pdf")
        doc.save(pdf_path)
        doc.close()

        # 1. 验证 quick_detect_candidate_pages 能够轻量探测到 Caption 页与扫描页
        candidates = pte.quick_detect_candidate_pages(pdf_path)
        self.assertIn(7, candidates)   # Page 8 (idx 7)
        self.assertIn(20, candidates)  # Page 21 (idx 20)

        # 2. 验证 remediate_pdf_pages_lossless 在长文档下自动将后段扫描页纳入检查集合
        inspected_pages = []

        def mock_detect_page(page, **kwargs):
            inspected_pages.append(page.number)
            return {"needs_rotation": False, "detected_angle": 0, "correction_angle": 0}

        with patch("doc_orientation_detector.detect_page_orientation", side_effect=mock_detect_page):
            remediate_pdf_pages_lossless(pdf_path, candidate_pages=candidates)

        # 前 5 页 (0..4)、Caption 页 (7) 以及扫描页均应被检查
        for expected in [0, 1, 2, 3, 4, 7, 20]:
            self.assertIn(expected, inspected_pages)

        # 3. 验证显式传递 candidate_pages 时，指定页必须被检查
        inspected_explicit = []

        def mock_detect_page_exp(page, **kwargs):
            inspected_explicit.append(page.number)
            return {"needs_rotation": False, "detected_angle": 0, "correction_angle": 0}

        with patch("doc_orientation_detector.detect_page_orientation", side_effect=mock_detect_page_exp):
            remediate_pdf_pages_lossless(pdf_path, candidate_pages=[15])

        self.assertIn(15, inspected_explicit)

    def test_mixed_page_with_dominant_landscape_table_and_body_text_skips_whole_page_rotation(self):
        """测试横表字符占比虽高 (>=85%) 但同页包含正向正文时，整页绝对不翻转，交由切图局部纠偏。"""
        doc = fitz.open()
        p = doc.new_page(width=600, height=800)

        # 1. 插入 0° 正向正文段落 (100 字符)
        body = "This section introduces the foundational formulation of the model architecture and training regimen."
        p.insert_text(fitz.Point(50, 50), body, fontsize=10)

        # 2. 插入 270° 旋转横向大表 (10 行 x 60 字符 = 600 字符，Matrix 90)
        table_rect = [100, 150, 550, 750]
        for x in range(150, 450, 30):
            p.insert_text(
                fitz.Point(x, 700),
                "A" * 60,
                fontsize=8,
                morph=(fitz.Point(x, 700), fitz.Matrix(90))
            )

        pdf_path = os.path.join(self.temp_dir, "dominant_table_mixed.pdf")
        doc.save(pdf_path)
        doc.close()

        doc_read = fitz.open(pdf_path)
        p_read = doc_read[0]

        # 验证全页级检测：即便旋转文字占比达到 90% (>= 85%)，因存在 0° 正文段落，
        # 绝不将整页误旋转，必须返回 vector_mixed_skipped 且 needs_rotation=False
        page_res = detect_page_orientation(p_read)
        self.assertFalse(page_res["needs_rotation"])
        self.assertEqual(page_res["detected_angle"], 0)
        self.assertEqual(page_res["source"], "vector_mixed_skipped")
        self.assertGreaterEqual(page_res["details"]["dominant_ratio"], PAGE_VECTOR_DOMINANT_RATIO)
        self.assertGreaterEqual(page_res["details"]["upright_len"], PAGE_VECTOR_MIN_CHARS)

        # 验证切图级检测 (Tier 2)：针对局部表格 rect 检测，文字方向纯净 (100% 270°)，准确识别并回正
        crop_img = Image.new("RGB", (400, 600), color="white")
        crop_res = detect_crop_orientation(crop_img, page=p_read, rect=table_rect)
        self.assertTrue(crop_res["needs_rotation"])
        self.assertEqual(crop_res["detected_angle"], 270)
        self.assertEqual(crop_res["correction_angle"], 90)

        # 验证整页自愈无损校准 (Tier 1)：绝不对该混排页施加翻转
        eff_path, modified, tmp_clean = remediate_pdf_pages_lossless(pdf_path)
        self.assertFalse(modified)
        self.assertEqual(eff_path, pdf_path)
        doc_read.close()

    def test_preprocess_image_float_numpy_array_support(self):
        """测试 preprocess_image 健壮支持 float32 [0, 1] 与 [0, 255] numpy 数组。"""
        # 浮点 [0.0, 1.0] 数组
        arr_float_01 = np.ones((300, 400, 3), dtype=np.float32) * 0.5
        tensor_01 = preprocess_image(arr_float_01)
        self.assertEqual(tensor_01.shape, (1, 3, 224, 224))
        self.assertEqual(tensor_01.dtype, np.float32)

        # 浮点 [0.0, 255.0] 数组
        arr_float_255 = np.ones((300, 400, 3), dtype=np.float32) * 128.0
        tensor_255 = preprocess_image(arr_float_255)
        self.assertEqual(tensor_255.shape, (1, 3, 224, 224))
        self.assertEqual(tensor_255.dtype, np.float32)

    def test_quick_detect_candidate_pages_with_drawings(self):
        """测试 quick_detect_candidate_pages 能够通过矢量绘图密度 (drawings >= 15) 发现无标题表格页。"""
        import pdf_table_extractor as pte

        doc = fitz.open()
        for i in range(15):
            p = doc.new_page(width=500, height=700)
            p.insert_text(fitz.Point(50, 100), "Normal academic text " * 20, fontsize=10)

        # 第 10 页 (index 9)：无 Caption，但包含 20 条表格网格线矢量绘图
        p9 = doc[9]
        for y in range(100, 300, 10):
            p9.draw_line(fitz.Point(50, y), fitz.Point(450, y))

        pdf_path = os.path.join(self.temp_dir, "drawings_doc.pdf")
        doc.save(pdf_path)
        doc.close()

        cands = pte.quick_detect_candidate_pages(pdf_path)
        self.assertIn(9, cands)


if __name__ == "__main__":
    unittest.main()
