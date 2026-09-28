#!/usr/bin/env python3
"""
tests/test_doclayout_yolo.py — 针对升级后的 DocLayout-YOLO 1280 检测器与旧版回退兼容的深度单元测试套件。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from PIL import Image
import numpy as np

# 将 scripts 目录与根目录加入 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import doclayout_yolo_detector as dyd
from doclayout_yolo_detector import DocLayoutYoloDetector, ensure_model_file, download_model, CLASS_LABELS


class TestDocLayoutYoloRealModel(unittest.TestCase):
    """针对项目 models/ 目录下的真实 1280 ONNX 模型的端到端验证。"""

    def setUp(self):
        self.model_path = os.path.join(PROJECT_ROOT, "models", dyd.MODEL_FILENAME)

    def test_real_model_loading_and_input_shape(self):
        """验证真实 1280 模型可正常被 ONNX Runtime 加载并识别 1280x1280 输入。"""
        if not os.path.exists(self.model_path):
            self.skipTest("模型文件尚不存在")
        detector = DocLayoutYoloDetector(self.model_path)
        self.assertEqual(detector.input_size, (1280, 1280))
        self.assertIsNotNone(detector.session)

    def test_real_model_blank_image_detection(self):
        """验证真实 1280 模型对空白页的检测鲁棒性（不误检）。"""
        if not os.path.exists(self.model_path):
            self.skipTest("模型文件尚不存在")
        detector = DocLayoutYoloDetector(self.model_path)
        # 测试 RGB
        img_rgb = Image.new("RGB", (600, 800), color="white")
        dets = detector.detect(img_rgb)
        self.assertIsInstance(dets, list)
        self.assertEqual(len(dets), 0)

        # 测试 RGBA
        img_rgba = Image.new("RGBA", (600, 800), color=(255, 255, 255, 255))
        dets_rgba = detector.detect(img_rgba)
        self.assertEqual(len(dets_rgba), 0)

        # 测试 灰度图 (L)
        img_l = Image.new("L", (600, 800), color=255)
        dets_l = detector.detect(img_l)
        self.assertEqual(len(dets_l), 0)

        # 验证 extract_extended_table_regions 无表格时返回空列表
        regions = detector.extract_extended_table_regions(img_rgb)
        self.assertEqual(regions, [])


class TestDocLayoutYoloParsing1280(unittest.TestCase):
    """针对新版 1280 模型 [1, 300, 6] 输出格式与内置 NMS 的逻辑测试。"""

    def setUp(self):
        self.detector = DocLayoutYoloDetector.__new__(DocLayoutYoloDetector)
        self.detector.input_size = (1280, 1280)
        self.mock_session = MagicMock()
        inp_mock = MagicMock()
        inp_mock.shape = [1, 3, 1280, 1280]
        inp_mock.name = "images"
        self.mock_session.get_inputs.return_value = [inp_mock]
        self.detector.session = self.mock_session

    def test_parse_1280_output_coordinates_and_classes(self):
        """验证 [1, 300, 6] 格式下的坐标反算、类别映射与置信度过滤。"""
        # 图片尺寸 1000 x 1000 -> scale = 1.28, pad_w = 0, pad_h = 0
        # 目标检测框 1: table (cid=5), pad 空间 [128, 128, 640, 640] -> 原图 [100.0, 100.0, 500.0, 500.0]
        # 目标检测框 2: table_caption (cid=6), pad 空间 [128, 64, 640, 115.2] -> 原图 [100.0, 50.0, 500.0, 90.0]
        # 目标检测框 3: 低置信度项 (score=0.05) -> 应被过滤
        out = np.zeros((1, 300, 6), dtype=np.float32)
        out[0, 0] = [128.0, 128.0, 640.0, 640.0, 0.95, 5.0]
        out[0, 1] = [128.0, 64.0, 640.0, 115.2, 0.88, 6.0]
        out[0, 2] = [200.0, 200.0, 300.0, 300.0, 0.05, 5.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1000, 1000))
        results = self.detector.detect(img)

        self.assertEqual(len(results), 2)
        tbl = next(r for r in results if r["type"] == "table")
        cap = next(r for r in results if r["type"] == "table_caption")

        self.assertAlmostEqual(tbl["score"], 0.95, places=3)
        self.assertEqual(tbl["bbox"], [100.0, 100.0, 500.0, 500.0])

        self.assertAlmostEqual(cap["score"], 0.88, places=3)
        self.assertEqual(cap["bbox"], [100.0, 50.0, 500.0, 90.0])

    def test_1280_nms_deduplication(self):
        """验证同类别高度重叠框被 NMS 抑制，保留最高分项。"""
        out = np.zeros((1, 300, 6), dtype=np.float32)
        # 表格高分检测
        out[0, 0] = [100, 100, 500, 500, 0.92, 5.0]
        # 表格轻微抖动重叠检测 (IoU > 0.8)
        out[0, 1] = [102, 101, 498, 499, 0.75, 5.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1280, 1280))
        results = self.detector.detect(img)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["score"], 0.92)

    def test_1280_cross_class_deduplication(self):
        """验证同一物理区域被不同类别多重输出时，保留更高置信度类别并抑制低置信度项。"""
        out = np.zeros((1, 300, 6), dtype=np.float32)
        # abandon 类别 (cid=2, 0.44)
        out[0, 0] = [200, 200, 400, 400, 0.44, 2.0]
        # 同一区域 plain_text 类别 (cid=1, 0.25)
        out[0, 1] = [200, 200, 400, 400, 0.25, 1.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1280, 1280))
        results = self.detector.detect(img)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "abandon")
        self.assertEqual(results[0]["score"], 0.44)

    def test_1280_nan_inf_guard(self):
        """验证模型输出中若存在 NaN 或 Inf 异常浮点值时安全跳过而不崩溃。"""
        out = np.zeros((1, 300, 6), dtype=np.float32)
        out[0, 0] = [np.nan, 100, 500, 500, 0.95, 5.0]
        out[0, 1] = [100, 100, 500, 500, np.inf, 5.0]
        out[0, 2] = [100, 100, 500, 500, 0.90, 5.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1280, 1280))
        results = self.detector.detect(img)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["score"], 0.90)


class TestDocLayoutYoloParsingLegacyFallback(unittest.TestCase):
    """针对旧版 640 模型 [1, 14, 8400] 及转置 [1, 8400, 14] 格式的回退解析兼容测试。"""

    def setUp(self):
        self.detector = DocLayoutYoloDetector.__new__(DocLayoutYoloDetector)
        self.detector.input_size = (640, 640)
        self.mock_session = MagicMock()
        inp_mock = MagicMock()
        inp_mock.shape = [1, 3, 640, 640]
        inp_mock.name = "images"
        self.mock_session.get_inputs.return_value = [inp_mock]
        self.detector.session = self.mock_session

    def test_parse_legacy_640_channels_first(self):
        """验证 [1, 14, 8400] 格式的 cx, cy, w, h 坐标解码与多类别解析。"""
        # 图片尺寸 500 x 500 -> scale = 1.28, pad_w = 0, pad_h = 0
        # cx=320, cy=320, w=256, h=256 -> pad 范围 [192, 192, 448, 448] -> 原图 [150.0, 150.0, 350.0, 350.0]
        out = np.zeros((1, 14, 8400), dtype=np.float32)
        out[0, 0, 0] = 320.0
        out[0, 1, 0] = 320.0
        out[0, 2, 0] = 256.0
        out[0, 3, 0] = 256.0
        out[0, 4 + 5, 0] = 0.93  # cid 5 = table

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (500, 500))
        results = self.detector.detect(img)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "table")
        self.assertEqual(results[0]["score"], 0.93)
        self.assertEqual(results[0]["bbox"], [150.0, 150.0, 350.0, 350.0])

    def test_parse_legacy_640_transposed(self):
        """验证 [1, 8400, 14] 格式的回退解析兼容性。"""
        out = np.zeros((1, 8400, 14), dtype=np.float32)
        out[0, 0, 0] = 320.0
        out[0, 0, 1] = 320.0
        out[0, 0, 2] = 256.0
        out[0, 0, 3] = 256.0
        out[0, 0, 4 + 7, ] = 0.85  # cid 7 = table_footnote

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (500, 500))
        results = self.detector.detect(img)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "table_footnote")
        self.assertEqual(results[0]["score"], 0.85)


class TestExtractExtendedTableRegions(unittest.TestCase):
    """测试 extract_extended_table_regions 表格与标题/脚注智能融合提取。"""

    def setUp(self):
        self.detector = DocLayoutYoloDetector.__new__(DocLayoutYoloDetector)
        self.detector.input_size = (1280, 1280)
        self.mock_session = MagicMock()
        inp_mock = MagicMock()
        inp_mock.shape = [1, 3, 1280, 1280]
        inp_mock.name = "images"
        self.mock_session.get_inputs.return_value = [inp_mock]
        self.detector.session = self.mock_session

    def test_merge_caption_above_and_footnote_below(self):
        """验证表格与上方表题 (caption) 及下方脚注 (footnote) 自动完整合并。"""
        out = np.zeros((1, 300, 6), dtype=np.float32)
        # 表格: [100, 200, 500, 600]
        out[0, 0] = [100, 200, 500, 600, 0.95, 5.0]
        # 表题 (上方 30px): [100, 160, 500, 195]
        out[0, 1] = [100, 160, 500, 195, 0.90, 6.0]
        # 脚注 (下方 20px): [100, 605, 500, 640]
        out[0, 2] = [100, 605, 500, 640, 0.85, 7.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1280, 1280))
        regions = self.detector.extract_extended_table_regions(img, padding=10)

        self.assertEqual(len(regions), 1)
        r = regions[0]
        self.assertEqual(r["table_index"], 0)
        self.assertTrue(r["has_caption"])
        self.assertTrue(r["has_footnote"])
        self.assertEqual(r["table_bbox"], [100.0, 200.0, 500.0, 600.0])
        # 扩充范围: min_y = 160 - 10 = 150, max_y = 640 + 10 = 650, min_x = 100 - 10 = 90, max_x = 500 + 10 = 510
        self.assertEqual(r["crop_bbox"], [90, 150, 510, 650])
        self.assertEqual(r["image"].size, (420, 500))

    def test_caption_below_table(self):
        """验证位于表格下方的表题也可正常合并。"""
        out = np.zeros((1, 300, 6), dtype=np.float32)
        # 表格: [100, 200, 500, 400]
        out[0, 0] = [100, 200, 500, 400, 0.92, 5.0]
        # 表题位于下方: [100, 405, 500, 430]
        out[0, 1] = [100, 405, 500, 430, 0.88, 6.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1280, 1280))
        regions = self.detector.extract_extended_table_regions(img, padding=5)

        self.assertEqual(len(regions), 1)
        self.assertTrue(regions[0]["has_caption"])
        self.assertFalse(regions[0]["has_footnote"])
        self.assertEqual(regions[0]["crop_bbox"], [95, 195, 505, 435])

    def test_multiple_tables_sorted_by_y(self):
        """验证多表格页面按纵坐标从上到下排序，索引正确递增。"""
        out = np.zeros((1, 300, 6), dtype=np.float32)
        # 表格 2 (位于下方 y=700)
        out[0, 0] = [100, 700, 500, 900, 0.91, 5.0]
        # 表格 1 (位于上方 y=100)
        out[0, 1] = [100, 100, 500, 300, 0.95, 5.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1280, 1280))
        regions = self.detector.extract_extended_table_regions(img)

        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["table_index"], 0)
        self.assertEqual(regions[0]["table_bbox"][1], 100.0)
        self.assertEqual(regions[1]["table_index"], 1)
        self.assertEqual(regions[1]["table_bbox"][1], 700.0)

    def test_side_by_side_tables_no_cross_column_stealing(self):
        """验证双栏排版下左右两张并列表格不会跨栏误抢标题，裁剪框各自保持独立。"""
        out = np.zeros((1, 300, 6), dtype=np.float32)
        # 表格 1 (左栏): [50, 100, 320, 400]
        out[0, 0] = [50, 100, 320, 400, 0.95, 5.0]
        # 表格 2 (右栏): [340, 100, 610, 400]
        out[0, 1] = [340, 100, 610, 400, 0.95, 5.0]
        # 标题 2 (右栏上方): [340, 70, 610, 90]
        out[0, 2] = [340, 70, 610, 90, 0.90, 6.0]
        # 标题 1 (左栏上方): [50, 70, 320, 90]
        out[0, 3] = [50, 70, 320, 90, 0.90, 6.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1280, 1280))
        regions = self.detector.extract_extended_table_regions(img, padding=10)

        self.assertEqual(len(regions), 2)
        r0 = next(r for r in regions if r["table_bbox"][0] == 50.0)
        r1 = next(r for r in regions if r["table_bbox"][0] == 340.0)

        self.assertTrue(r0["has_caption"])
        self.assertTrue(r1["has_caption"])
        # 左栏表格裁切框右边界严禁侵入右栏 (<= 330)
        self.assertLessEqual(r0["crop_bbox"][2], 330)
        # 右栏表格裁切框左边界严禁侵入左栏 (>= 330)
        self.assertGreaterEqual(r1["crop_bbox"][0], 330)

    def test_stacked_tables_no_vertical_caption_stealing(self):
        """验证上下堆叠表格中，上方高表格不会贪心误抢下方表格的顶部标题。"""
        out = np.zeros((1, 300, 6), dtype=np.float32)
        # 表格 1 (上方大表格): [100, 100, 500, 700]
        out[0, 0] = [100, 100, 500, 700, 0.95, 5.0]
        # 表格 2 (下方表格): [100, 800, 500, 1200]
        out[0, 1] = [100, 800, 500, 1200, 0.95, 5.0]
        # 标题 2 (紧邻表格 2 顶部 5px): [100, 770, 500, 795]
        out[0, 2] = [100, 770, 500, 795, 0.90, 6.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1280, 1280))
        regions = self.detector.extract_extended_table_regions(img, padding=10)

        self.assertEqual(len(regions), 2)
        r0 = next(r for r in regions if r["table_bbox"][1] == 100.0)
        r1 = next(r for r in regions if r["table_bbox"][1] == 800.0)

        self.assertFalse(r0["has_caption"])
        self.assertTrue(r1["has_caption"])
        # 表格 1 的裁切框绝不应吞并 770 处的标题
        self.assertLessEqual(r0["crop_bbox"][3], 750)
        # 表格 2 的裁切框完整包含其标题 (min_y <= 770)
        self.assertLessEqual(r1["crop_bbox"][1], 770)

    def test_adjacent_tables_padding_boundary_clipping(self):
        """验证间距极小的邻近表格自动进行 padding 边界裁剪，不吞入邻近表格内容。"""
        out = np.zeros((1, 300, 6), dtype=np.float32)
        # 表格 1: y=[100, 500]
        out[0, 0] = [100, 100, 500, 500, 0.95, 5.0]
        # 表格 2: 紧邻下方仅相距 10px y=[510, 900]
        out[0, 1] = [100, 510, 500, 900, 0.95, 5.0]

        self.mock_session.run.return_value = [out]
        img = Image.new("RGB", (1280, 1280))
        # padding=20，如果不限制，表格 1 裁切框 y2 将达到 520 (侵入表格 2 达 10px)
        regions = self.detector.extract_extended_table_regions(img, padding=20)

        self.assertEqual(len(regions), 2)
        # 表格 1 底部裁切框被安全限制在两表正中 505
        self.assertLessEqual(regions[0]["crop_bbox"][3], 505)
        # 表格 2 顶部裁切框被安全限制在两表正中 505
        self.assertGreaterEqual(regions[1]["crop_bbox"][1], 505)


class TestEnsureModelFileAndDownload(unittest.TestCase):
    """测试模型自动检索、全局缓存同步与下载回退机制。"""

    def test_ensure_model_file_from_target_dir(self):
        """目标目录存在有效 1280 模型时直接返回。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            model_file = os.path.join(tmp_dir, dyd.MODEL_FILENAME)
            with open(model_file, "wb") as f:
                f.write(b"0" * (11 * 1024 * 1024))

            p = ensure_model_file(tmp_dir)
            self.assertEqual(p, model_file)

    def test_ensure_model_file_direct_filepath(self):
        """直接指定模型文件绝对路径时支持直通。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            model_file = os.path.join(tmp_dir, "my_custom_model.onnx")
            with open(model_file, "wb") as f:
                f.write(b"0" * (11 * 1024 * 1024))

            p = ensure_model_file(model_file)
            self.assertEqual(p, model_file)

    def test_fallback_to_legacy_model_on_download_error(self):
        """当 1280 模型不存在且下载失败时，若存在旧版 640 模型则平滑回退。"""
        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as empty_global:
            legacy_file = os.path.join(tmp_dir, dyd.LEGACY_MODEL_FILENAME)
            with open(legacy_file, "wb") as f:
                f.write(b"0" * (11 * 1024 * 1024))

            with patch.object(dyd, "GLOBAL_MODEL_DIR", empty_global):
                with patch.object(dyd, "download_model", side_effect=RuntimeError("Connection refused")):
                    p = ensure_model_file(tmp_dir)
                    self.assertEqual(p, legacy_file)

    def test_raise_error_when_no_model_and_download_fails(self):
        """当没有任何模型且下载失败时抛出 FileNotFoundError。"""
        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as empty_global:
            with patch.object(dyd, "GLOBAL_MODEL_DIR", empty_global):
                with patch.object(dyd, "download_model", side_effect=RuntimeError("Connection refused")):
                    with self.assertRaises(FileNotFoundError):
                        ensure_model_file(tmp_dir)

    def test_download_model_atomic_write(self):
        """验证 download_model 使用原子写入保证文件完整性。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = os.path.join(tmp_dir, dyd.MODEL_FILENAME)
            dummy_content = b"ONNX_DATA_" + b"x" * (11 * 1024 * 1024)

            mock_cm = MagicMock()
            mock_resp = MagicMock()
            mock_resp.read.side_effect = [dummy_content, b""]
            mock_cm.__enter__.return_value = mock_resp
            mock_cm.__exit__.return_value = False

            with patch("urllib.request.urlopen", return_value=mock_cm):
                result_path = download_model(target_path)
                self.assertEqual(result_path, target_path)
                self.assertTrue(os.path.exists(target_path))
                self.assertFalse(os.path.exists(target_path + ".download.tmp"))

    def test_global_cache_fallback_when_copy_fails(self):
        """验证当目标目录只读导致从全局缓存复制失败时，直接返回全局缓存模型路径而不崩溃。"""
        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as global_dir:
            global_model = os.path.join(global_dir, dyd.MODEL_FILENAME)
            with open(global_model, "wb") as f:
                f.write(b"0" * (11 * 1024 * 1024))

            with patch.object(dyd, "GLOBAL_MODEL_DIR", global_dir):
                with patch("shutil.copy2", side_effect=PermissionError("Permission denied")):
                    p = ensure_model_file(tmp_dir)
                    self.assertEqual(p, global_model)


if __name__ == "__main__":
    unittest.main()
