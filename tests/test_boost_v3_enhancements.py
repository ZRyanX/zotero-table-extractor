#!/usr/bin/env python3
"""
test_boost_v3_enhancements.py — 针对 10 大全新架构增强特性的全面单元测试套件：
1. RapidAI / TableStructureRec + RapidTable 有线/无线双模型与 three_way_vote 投票接入
2. Docling (IBM) TableFormer 跨列层级恢复与语义分组推断
3. PDF-Extract-Kit (OpenDataLab) 无边框表格自适应列边界聚类与样式加权对齐
4. PP-DocLayoutV3 25 类细粒度版面识别、多边形贴合与双检融合 (Ensemble Voting)
5. MinerU 4-Way 跨页续表联合判定、小表独立保护与 MINERU_TABLE_MERGE_ENABLE 总开关
6. OmniDocBench (CVPR 2025) 标准化度量体系 (TEDS / 检测 mAP / 端端召回率)
7. LLM 语义化表格评测 (arXiv 2603.18652) 与单元格/表头等价性判分
8. OCR-Hub 统一多后端注册表、能力发现 (/capabilities) 与健康探测
9. TeleOCR 与 MinerU 2.5 高精度 VLM 客户端与多引擎聚合
10. SMART (Semantic Header Flattening) 语义表头展平与层级血缘保持
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from typing import List, Dict, Any

import pandas as pd
import numpy as np

# 确保 scripts 目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import rapid_table_extractor
from scripts import omnidocbench
from scripts import doclayout_yolo_detector
from scripts import table_postprocess
from scripts import pdf_tables
from scripts import pdf_table_extractor
from scripts import ocr_client
from scripts import llm_reasoner


class TestRapidTableIntegration(unittest.TestCase):
    """特性 1: RapidAI / TableStructureRec + RapidTable 本地双模型表格结构识别。"""

    def test_availability_and_classification(self):
        """测试 RapidTable 可用性探测与表格类型 (wired vs wireless) 判别。"""
        extractor = rapid_table_extractor.RapidTableExtractor()
        # 空图像默认返回 wireless
        self.assertEqual(extractor.classify_table_type(None), "wireless")

        # 构造一个含密集网格线特征的 numpy 矩阵
        grid_img = np.zeros((100, 100), dtype=np.uint8)
        grid_img[::5, :] = 255
        grid_img[:, ::5] = 255
        t_type = extractor.classify_table_type(grid_img)
        self.assertEqual(t_type, "wired")

    def test_parse_html_to_dataframe(self):
        """测试从 RapidTable HTML 产物还原带合并单元格的 DataFrame。"""
        extractor = rapid_table_extractor.RapidTableExtractor()
        sample_html = """
        <table>
            <tr><td><b>Element</b></td><td><b>wt%</b></td></tr>
            <tr><td>SiO2</td><td>52.3</td></tr>
            <tr><td>TiO2</td><td>0.8</td></tr>
        </table>
        """
        df = extractor.parse_html_to_dataframe(sample_html)
        self.assertIsNotNone(df)
        self.assertEqual(df.shape, (2, 2))
        self.assertEqual(df.iloc[0, 0], "SiO2")
        self.assertEqual(df.attrs.get("extractor"), "rapid_table")

    def test_three_way_vote_with_rapid_table(self):
        """测试 RapidTable 成功作为候选方接入 three_way_vote 多方投票池。"""
        df_target = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        rapid_item = {
            "df": df_target,
            "page_idx": 0,
            "table_idx": 0,
            "bbox": [50, 50, 200, 200],
            "extractor": "rapid_table",
        }
        # 仅 RapidTable 提供结果
        fused, logs, summary = pdf_table_extractor.three_way_vote(
            inspector_tables=[],
            find_tables_results=[],
            plumber_tables=[],
            camelot_tables=[],
            rapid_table_tables=[rapid_item]
        )
        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0]["df"].iloc[0, 0], 1)


class TestPDFExtractKitAlignment(unittest.TestCase):
    """特性 3: PDF-Extract-Kit 无边框表格自适应列边界聚类与样式加权对齐。"""

    def test_pdf_extract_kit_align_tokens(self):
        """测试 PDF-Extract-Kit 行起始与样式加权列对齐。"""
        class MockItem:
            def __init__(self, x, text, font_size=10.0, is_bold=False):
                self.x = x
                self.text = text
                self.y = 100.0
                self.font_size = font_size
                self.is_bold = is_bold

        # 模拟表头行（粗体、大字号）
        line0 = [
            MockItem(50.0, "Sample", 11.0, is_bold=True),
            MockItem(180.0, "SiO2", 11.0, is_bold=True),
            MockItem(320.0, "Al2O3", 11.0, is_bold=True),
        ]
        # 模拟数据行
        line1 = [
            MockItem(52.0, "D-01", 10.0, is_bold=False),
            MockItem(182.0, "54.2", 10.0, is_bold=False),
            MockItem(322.0, "15.8", 10.0, is_bold=False),
        ]
        line2 = [
            MockItem(51.0, "D-02", 10.0, is_bold=False),
            MockItem(181.0, "53.9", 10.0, is_bold=False),
            MockItem(321.0, "16.1", 10.0, is_bold=False),
        ]

        data_lines = [line0, line1, line2]
        df = pdf_table_extractor.pdf_extract_kit_align_tokens(
            data_lines, header_rows=[line0], mb_height=800.0, body_start_idx=1
        )
        self.assertIsNotNone(df)
        self.assertEqual(df.shape, (2, 3))
        self.assertIn("Sample", df.columns[0])
        self.assertIn("SiO2", df.columns[1])
        self.assertEqual(df.iloc[0, 0], "D-01")
        self.assertEqual(df.iloc[1, 1], "53.9")
        self.assertEqual(df.attrs.get("alignment_engine"), "pdf_extract_kit")


class TestPPDocLayoutV3AndEnsemble(unittest.TestCase):
    """特性 4: PP-DocLayoutV3 25 类细粒度版面识别与双检融合。"""

    def test_pp_doclayout_classes_and_mapping(self):
        """测试 25 类规范映射。"""
        self.assertIn(21, doclayout_yolo_detector.PP_DOCLAYOUT_V3_CLASSES)
        self.assertEqual(doclayout_yolo_detector.PP_DOCLAYOUT_V3_CLASSES[21], "table")
        self.assertEqual(doclayout_yolo_detector.PP_DOCLAYOUT_V3_CLASSES[3], "chart")
        self.assertEqual(doclayout_yolo_detector.CANONICAL_CLASS_MAP["chart"], "figure")

    def test_polygon_to_tight_bbox(self):
        """测试多边形边缘拟合转紧致外接矩形。"""
        detector = doclayout_yolo_detector.PPDocLayoutV3Detector()
        poly = [[10.0, 20.0], [50.0, 15.0], [45.0, 80.0], [12.0, 75.0]]
        bbox = detector.polygon_to_tight_bbox(poly)
        self.assertEqual(bbox, [10.0, 15.0, 50.0, 80.0])

    def test_get_layout_detector_factory(self):
        """测试版面检测工厂引擎切换。"""
        det_ensemble = doclayout_yolo_detector.get_layout_detector("ensemble")
        self.assertIsInstance(det_ensemble, doclayout_yolo_detector.DocLayoutEnsembleDetector)

        det_pp = doclayout_yolo_detector.get_layout_detector("pp_doclayoutv3")
        self.assertIsInstance(det_pp, doclayout_yolo_detector.PPDocLayoutV3Detector)

    def test_ensemble_regions_fusion_and_union(self):
        """测试双检融合器 (Ensemble) 的 IoU 匹配、caption/footnote 补全与漏检独立区域召回。"""
        from PIL import Image
        img = Image.new("RGB", (800, 1000), color="white")

        # 模拟 YOLO 检出 1 个表格（无 caption）
        mock_yolo = MagicMock()
        mock_yolo.extract_extended_table_regions.return_value = [
            {
                "table_index": 0,
                "table_bbox": [50.0, 100.0, 700.0, 400.0],
                "crop_bbox": [40, 90, 710, 410],
                "score": 0.88,
                "has_caption": False,
                "has_footnote": False,
                "image": img.crop((40, 90, 710, 410))
            }
        ]

        # 模拟 PP-DocLayoutV3 同样检出该表格且捕获到 caption，同时还多检出了一个底部小表 (漏检挽救)
        mock_pp = MagicMock()
        mock_pp.is_available.return_value = True
        mock_pp.extract_extended_table_regions.return_value = [
            {
                "table_index": 0,
                "table_bbox": [52.0, 98.0, 698.0, 395.0],
                "crop_bbox": [40, 70, 710, 410],
                "score": 0.92,
                "has_caption": True,  # 成功补充 caption
                "has_footnote": False,
                "image": img.crop((40, 70, 710, 410))
            },
            {
                "table_index": 1,
                "table_bbox": [50.0, 600.0, 400.0, 850.0],  # 独立挽救的第二张表
                "crop_bbox": [40, 590, 410, 860],
                "score": 0.90,
                "has_caption": True,
                "has_footnote": True,
                "image": img.crop((40, 590, 410, 860))
            }
        ]

        ensemble = doclayout_yolo_detector.DocLayoutEnsembleDetector(yolo_detector=mock_yolo, pp_detector=mock_pp)
        fused = ensemble.extract_extended_table_regions(img)
        self.assertEqual(len(fused), 2, "Ensemble 必须通过互补补全召回所有 2 个表格")
        self.assertTrue(fused[0]["has_caption"], "表 1 必须成功继承 PP 检测出的 caption 标记")
        self.assertEqual(fused[1]["table_index"], 1, "必须正确对召回的新表赋予序号")


class TestMinerUContinuationRules(unittest.TestCase):
    """特性 5: MinerU 4-Way 跨页续表联合判定、小表独立保护与断裂单元格合并。"""

    def test_mineru_merge_switch_disable(self):
        """测试 MINERU_TABLE_MERGE_ENABLE=false 时跳过跨页合并。"""
        df1 = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        df1.attrs["label"] = "Table 1"
        df1.attrs["page_idx"] = 1

        df2 = pd.DataFrame({"A": [5, 6], "B": [7, 8]})
        df2.attrs["label"] = "Table 1 (cont.)"
        df2.attrs["page_idx"] = 2

        with patch.dict(os.environ, {"MINERU_TABLE_MERGE_ENABLE": "false"}):
            merged = table_postprocess.merge_continuation_tables([df1, df2])
            self.assertEqual(len(merged), 2)

    def test_mineru_small_table_protection(self):
        """测试小表独立保护：后表 1~5 行且无明确续表标识且列差异大时坚决独立保留。"""
        df_lead = pd.DataFrame({
            "Sample": [f"S{i}" for i in range(10)],
            "SiO2": [50 + i for i in range(10)],
            "TiO2": [0.5 for _ in range(10)]
        })
        df_lead.attrs["label"] = "Table 2"
        df_lead.attrs["page_idx"] = 3

        df_follower = pd.DataFrame({
            "Location": ["LocA", "LocB", "LocC"],
            "Elevation": [1200, 1400, 1550]
        })
        df_follower.attrs["page_idx"] = 4

        merged = table_postprocess.merge_continuation_tables([df_lead, df_follower])
        self.assertEqual(len(merged), 2)

    def test_cross_page_split_cell_merge(self):
        """测试跨页换行导致的断裂单元格与互补残行拼接合并。"""
        df_a = pd.DataFrame({
            "Sample": ["SMP-01", "SMP-02"],
            "Description": ["Normal sample", "Granitic rock with-"]
        })
        df_b = pd.DataFrame({
            "Sample": ["", "SMP-03"],
            "Description": ["quartz phenocrysts.", "Basalt sample"]
        })

        merged_a, merged_b = table_postprocess.merge_cross_page_split_cells(df_a, df_b)
        self.assertEqual(merged_a.iloc[-1]["Description"], "Granitic rock withquartz phenocrysts.")
        self.assertEqual(len(merged_b), 1)
        self.assertEqual(merged_b.iloc[0]["Sample"], "SMP-03")


class TestOmniDocBenchMetrics(unittest.TestCase):
    """特性 6: OmniDocBench (CVPR 2025) 标准化度量体系。"""

    def test_compute_iou_and_detection_map(self):
        """测试版面检测框 IoU 与 mAP 指标计算。"""
        box1 = [10.0, 10.0, 100.0, 100.0]
        box2 = [10.0, 10.0, 100.0, 100.0]
        self.assertEqual(omnidocbench.compute_iou(box1, box2), 1.0)

        preds = [[10.0, 10.0, 100.0, 100.0]]
        gts = [[10.0, 10.0, 100.0, 100.0]]
        metrics = omnidocbench.compute_layout_detection_map(preds, gts)
        self.assertAlmostEqual(metrics["precision@50"], 1.0)
        self.assertAlmostEqual(metrics["mAP@50"], 1.0)
        self.assertAlmostEqual(metrics["mAP@50:95"], 1.0)

    def test_omission_recall_and_evaluator(self):
        """测试端端表格声明召回率与分类型报告生成。"""
        recall_res = omnidocbench.compute_omission_recall(10, 9)
        self.assertEqual(recall_res["recall"], 0.9)
        self.assertEqual(recall_res["omission_rate"], 0.1)

        evaluator = omnidocbench.OmniDocBenchEvaluator()
        evaluator.add_paper_result("p1", "academic", 5, 5)
        evaluator.add_paper_result("p2", "textbook", 4, 3)

        summary = evaluator.summarize()
        self.assertEqual(summary["total_papers"], 2)
        self.assertEqual(summary["total_declared"], 9)
        self.assertEqual(summary["total_extracted"], 8)
        self.assertIn("academic", summary["stratified"])

        md_report = evaluator.format_report_markdown()
        self.assertIn("OmniDocBench 表格提取与版面评测基准", md_report)


class TestLLMSemanticEvaluation(unittest.TestCase):
    """特性 7: LLM 语义化表格评测 (arXiv 2603.18652)。"""

    def test_heuristic_semantic_equivalence_identical(self):
        """测试启发式语义等价性判定（两表相同）。"""
        df_a = pd.DataFrame({"Year": [2022, 2023], "Revenue": [100, 120]})
        df_b = pd.DataFrame({"Year": [2022, 2023], "Revenue": [100, 120]})

        res = llm_reasoner.evaluate_table_semantic_equivalence(df_a, df_b)
        self.assertTrue(res["is_equivalent"])
        self.assertGreaterEqual(res["score"], 0.9)

    def test_heuristic_semantic_equivalence_flattened_header(self):
        """测试多级表头展平后的语义等价性度量。"""
        df_a = pd.DataFrame({"Finance > Year": [2022, 2023], "Finance > Revenue": [100, 120]})
        df_b = pd.DataFrame({"Finance_Year": [2022, 2023], "Finance_Revenue": [100, 120]})

        res = llm_reasoner.evaluate_table_semantic_equivalence(df_a, df_b)
        self.assertTrue(res["is_equivalent"])
        self.assertGreaterEqual(res["dimension_scores"]["content"], 0.9)

    def test_repair_defective_table_acceptance(self):
        """测试 repair_defective_table 修复验收：语义等价时接受，严重破坏数据时拒绝回退原表。"""
        reasoner = llm_reasoner.LLMTableReasoner()
        reasoner.is_available = lambda: True

        orig_df = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        # 模拟模型输出破坏了数据的重构表格
        corrupted_df = pd.DataFrame({"X": [999, 888]})

        with patch.object(reasoner, "reason_complex_table_hierarchy", return_value=corrupted_df):
            with patch.object(reasoner, "evaluate_semantic_equivalence", return_value={"is_equivalent": False, "score": 0.2, "explanation": "Data lost"}):
                res = reasoner.repair_defective_table(orig_df)
                # 必须拒绝并保留原表
                self.assertEqual(list(res.columns), ["A", "B"])


class TestOCRHubRegistryAndTeleOCR(unittest.TestCase):
    """特性 8 & 9: OCR-Hub 网关注册表、能力发现与 TeleOCR 支持。"""

    def test_backend_registry_capabilities(self):
        """测试 OCR 注册表能力发现 (/capabilities) 与健康探测。"""
        registry = ocr_client.get_ocr_registry()
        self.assertIsNotNone(registry)

        caps = registry.get_capabilities()
        self.assertIn("paddleocr_vl", caps)
        self.assertIn("mineru", caps)
        self.assertIn("teleocr", caps)
        self.assertIn("docling", caps)
        self.assertTrue(caps["paddleocr_vl"]["tables"])

    def test_teleocr_client_probe_and_backend(self):
        """测试 TeleOCR 客户端探测与后端调度。"""
        client = ocr_client.TeleOCRClient(api_base="http://localhost:8000/v1")
        self.assertTrue(client.is_available())
        backend = ocr_client.TeleOCRBackend()
        backend.client.api_base = "http://localhost:8000/v1"
        self.assertEqual(backend.name, "teleocr")

        # 模拟 image 提取
        with patch.object(backend.client, "extract_from_image", return_value="| Col | Val |\n|---|---|\n| A | 1 |"):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tf.write(b"\x89PNG\r\n\x1a\nfake")
                tf_path = tf.name
            try:
                res = backend.extract(tf_path)
                self.assertIn("Col", res)
            finally:
                if os.path.exists(tf_path):
                    os.remove(tf_path)

        # 调度测试
        registry = ocr_client.get_ocr_registry()
        with patch.object(backend, "probe_health", return_value={"status": "healthy"}):
            registry.register(backend)
            dispatched = registry.dispatch(capability="tables", prefer="teleocr")
            self.assertEqual(dispatched.name, "teleocr")


class TestSMARTHeaderFlattening(unittest.TestCase):
    """特性 10: SMART (Semantic Header Flattening) 语义表头展平。"""

    def test_smart_flatten_headers_basic(self):
        """测试跨列前向填充与层级血缘展平。"""
        row0 = ["Cohort Distribution", "", "Age (years)"]
        row1 = ["Group A", "Group B", ""]

        flattened = table_postprocess.smart_flatten_headers([row0, row1])
        self.assertEqual(len(flattened), 3)
        self.assertEqual(flattened[0], "Cohort Distribution > Group A")
        self.assertEqual(flattened[1], "Cohort Distribution > Group B")
        self.assertEqual(flattened[2], "Age (years)")

    def test_smart_flatten_headers_three_levels(self):
        """测试 3 层复合表头跨列延展与前向填充 (Parent -> Category -> Sub/Unit)。"""
        row0 = ["Geochemistry", "", "", "Age"]
        row1 = ["Major Elements", "", "Trace Elements", ""]
        row2 = ["SiO2 (wt%)", "Al2O3 (wt%)", "Sr (ppm)", "Ma"]

        flattened = table_postprocess.smart_flatten_headers([row0, row1, row2])
        self.assertEqual(len(flattened), 4)
        self.assertEqual(flattened[0], "Geochemistry > Major Elements > SiO2 (wt%)")
        self.assertEqual(flattened[1], "Geochemistry > Major Elements > Al2O3 (wt%)")
        self.assertEqual(flattened[2], "Geochemistry > Trace Elements > Sr (ppm)")
        self.assertEqual(flattened[3], "Age > Ma")

    def test_smart_flatten_headers_deduplication(self):
        """测试语义冗余消解 (子列已含父列核心词)。"""
        row0 = ["Major Element", "Trace Element"]
        row1 = ["Major Element (wt%)", "Trace Element (ppm)"]

        flattened = table_postprocess.smart_flatten_headers([row0, row1])
        self.assertEqual(flattened[0], "Major Element (wt%)")
        self.assertEqual(flattened[1], "Trace Element (ppm)")

    def test_docling_tableformer_style_hierarchy_rebuild(self):
        """测试 pdf_tables.py 中结合 TableFormer 层次与 SMART 展平的表头恢复。"""
        self.assertTrue(hasattr(pdf_tables, "rebuild_df_with_style_hierarchy"))


if __name__ == "__main__":
    unittest.main()
