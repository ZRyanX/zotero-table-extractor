"""
tests/test_omission_prevention_remediation.py
============================================
针对审查建议与丢表/漏表防范机制的系统性回归测试用例。
涵盖：
1. pages_with_tables 未定义变量修复与安全页码映射
2. 候选页面多信号并集体系与来源追踪
3. 表级精细对齐与 OCR 替换策略 (杜绝误删同页有效原生兄弟表)
4. 二维 IoU 空间判定与表号防冲突聚类 (杜绝同页不同表格误合并)
5. YOLO 切图覆盖不足时触发 PP-Structure 补充定位
6. 无 Caption 候选页提取未果时由验证器安全接管
7. 在线提取基于表号集合的完整性核验与本地 PDF 补漏
"""

import os
import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import pdf_table_extractor as pte
from table_validator import (
    validate_native_extraction_pipeline,
    scan_pdf_table_declarations,
)
import extract_zotero_table
from extract_zotero_table import (
    scan_pdf_table_expected_labels,
    _count_pdf_table_captions,
    process_single_pdf,
)
import pdf_tables


class TestPagesWithTablesAndCandidateUnion(unittest.TestCase):
    """1 & 2: 验证 pages_with_tables 修复与多信号候选页并集"""

    @patch("pdf_table_extractor.has_text_layer", return_value=True)
    @patch("pdf_table_extractor.classify_and_extract_via_inspector")
    @patch("table_validator.scan_pdf_table_declarations")
    @patch("fitz.open")
    def test_pages_with_tables_name_error_prevented(self, mock_fitz, mock_decls, mock_clf, mock_has_text):
        """当 inspector 返回未标注 page_idx 的表格且只有 pages_with_tables 时，确保无 NameError 且映射正确"""
        mock_decls.return_value = {}  # 无文本 caption
        
        # 模拟 pdf-inspector 返回 1-based page numbers [2] (即 page index 1)
        mock_df = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        mock_clf.return_value = {
            'pdf_type': 'text_based',
            'confidence': 1.0,
            'pages_with_tables': [2],  # 1-based
            'pages_needing_ocr': [],
            'markdown': '',
            'error': None,
            'structure_tables': [],
            'tables': [{'df': mock_df, 'page_idx': None, 'table_idx': 0, 'bbox': None}],
        }

        # 模拟 fitz 文档
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 5
        mock_doc.__iter__.return_value = []
        mock_fitz.return_value = mock_doc

        # 调用 extract_tables_from_pdf，应平稳运行，绝不抛出 NameError: name 'pages_with_tables' is not defined
        try:
            results, logs = pte.extract_tables_from_pdf("dummy.pdf", use_ocr_fallback=False)
        except NameError as e:
            self.fail(f"extract_tables_from_pdf raised NameError: {e}")

        # 验证该表格被正确映射为 0-based index 1 (来自 [2] - 1)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]["page_idx"], 1)

    @patch("pdf_table_extractor.has_text_layer", return_value=True)
    @patch("pdf_table_extractor.classify_and_extract_via_inspector")
    @patch("table_validator.scan_pdf_table_declarations")
    @patch("fitz.open")
    def test_candidate_pages_multi_signal_union(self, mock_fitz, mock_decls, mock_clf, mock_has_text):
        """验证 candidate_pages 将 caption、inspector、structure tree 取并集，不因单方信号排斥他方"""
        # Caption 仅在 Page 0 (0-based)
        mock_decls.return_value = {0: [{'label': '表1', 'title': '表1', 'is_continuation': False}]}
        
        # Inspector 在 Page 3 (1-based -> 0-based 为 2) 检测到表格
        df_inst = pd.DataFrame({"Col1": [1, 2], "Col2": [3, 4]})
        # Structure tree 在 Page 4 (0-based) 检测到表格
        df_struct = pd.DataFrame({"X": [10, 20], "Y": [30, 40]})
        df_struct.attrs['page_idx'] = 4

        mock_clf.return_value = {
            'pdf_type': 'text_based',
            'confidence': 1.0,
            'pages_with_tables': [3],  # Page 2 (0-based)
            'pages_needing_ocr': [],
            'markdown': '',
            'error': None,
            'structure_tables': [{'df': df_struct, 'page_idx': 4, 'table_idx': 0, 'bbox': None}],
            'tables': [{'df': df_inst, 'page_idx': 2, 'table_idx': 0, 'bbox': None}],
        }

        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 10
        mock_doc.__iter__.return_value = []
        mock_fitz.return_value = mock_doc

        results, logs = pte.extract_tables_from_pdf("dummy.pdf", use_ocr_fallback=False)
        
        # 查看日志中的候选页面列表，必须包含 1, 3, 5 (1-based) 即 0, 2, 4 (0-based)
        candidate_log = [l for l in logs if "候选表格页面" in l]
        self.assertTrue(len(candidate_log) > 0)
        self.assertIn("1", candidate_log[0])
        self.assertIn("3", candidate_log[0])
        self.assertIn("5", candidate_log[0])


class TestGranularOcrReplacement(unittest.TestCase):
    """3: 验证表级细粒度对齐与 OCR 替换策略 (杜绝误删同页有效原生兄弟表)"""

    def test_reconcile_native_and_ocr_tables_preserves_sibling_tables(self):
        """当同页有表1和表2，OCR仅对表2识别出切图时，确保表2被精准替换，而表1安全保留"""
        df1 = pd.DataFrame({"ColA": [1, 2], "ColB": [3, 4]})
        df1.attrs['label'] = "Table 1"
        df1.attrs['page_idx'] = 0

        df2_bad = pd.DataFrame({"ColC": ["a", "b"], "ColD": ["c", "d"]})
        df2_bad.attrs['label'] = "Table 2"
        df2_bad.attrs['page_idx'] = 0

        native_results = [
            {'df': df1, 'page_idx': 0, 'table_idx': 0, 'label': 'Table 1'},
            {'df': df2_bad, 'page_idx': 0, 'table_idx': 1, 'label': 'Table 2'},
        ]

        # OCR 仅成功获得了 Table 2 的切图
        df2_ocr = pd.DataFrame({"ColC": ["a", "b"], "ColD": ["c", "d"], "ColE": ["e", "f"]})
        df2_ocr.attrs['label'] = "Table 2"
        df2_ocr.attrs['page_idx'] = 0
        df2_ocr.attrs['extractor'] = 'paddleocr_vl_crop'
        ocr_results = [
            {'df': df2_ocr, 'page_idx': 0, 'table_idx': 0, 'label': 'Table 2'}
        ]

        logs = []
        reconciled = pte.reconcile_native_and_ocr_tables(native_results, ocr_results, [0], logs)

        # 结果中必须保留 2 个表格：Table 1 (原生) + Table 2 (OCR)
        self.assertEqual(len(reconciled), 2)
        labels = [r['df'].attrs.get('label') for r in reconciled]
        self.assertIn("Table 1", labels)
        self.assertIn("Table 2", labels)

        # 确认 Table 2 来自 OCR (含有 ColE 列)，Table 1 来自原生
        t2_res = next(r for r in reconciled if r['df'].attrs.get('label') == "Table 2")
        self.assertEqual(t2_res['df'].attrs.get('extractor'), 'paddleocr_vl_crop')
        self.assertIn("ColE", t2_res['df'].columns)

        t1_res = next(r for r in reconciled if r['df'].attrs.get('label') == "Table 1")
        self.assertIn("ColA", t1_res['df'].columns)

    def test_reconcile_native_and_ocr_tables_spatial_bbox_matching(self):
        """当没有表号标签时，基于 2D bbox 空间 IoU/IoM 正确替换对应区域的表格，保留另一区域表格"""
        df_top = pd.DataFrame({"Top1": [1, 2], "Top2": [3, 4]})
        df_bottom = pd.DataFrame({"Bot1": [5, 6], "Bot2": [7, 8]})

        # 原生提取到顶部表格与底部表格
        top_bbox = [50, 100, 500, 250]
        bottom_bbox = [50, 500, 500, 700]
        native_results = [
            {'df': df_top, 'page_idx': 1, 'bbox': top_bbox, 'pdf_bbox': top_bbox},
            {'df': df_bottom, 'page_idx': 1, 'bbox': bottom_bbox, 'pdf_bbox': bottom_bbox},
        ]

        # OCR 切图仅重做了底部表格
        df_bottom_ocr = pd.DataFrame({"Bot1": [5, 6], "Bot2": [7, 8], "Bot3": [9, 10]})
        df_bottom_ocr.attrs['extractor'] = 'paddleocr_vl_crop'
        ocr_bbox = [48, 498, 502, 702]
        ocr_results = [
            {'df': df_bottom_ocr, 'page_idx': 1, 'bbox': ocr_bbox, 'pdf_bbox': ocr_bbox}
        ]

        logs = []
        reconciled = pte.reconcile_native_and_ocr_tables(native_results, ocr_results, [1], logs)

        self.assertEqual(len(reconciled), 2)
        # 顶部表格完整保留，底部表格被替换
        top_kept = [r for r in reconciled if "Top1" in r['df'].columns]
        bot_replaced = [r for r in reconciled if "Bot3" in r['df'].columns]
        self.assertEqual(len(top_kept), 1)
        self.assertEqual(len(bot_replaced), 1)


class TestTableSimilarityAndClustering(unittest.TestCase):
    """4: 验证表格相似度与空间防错合聚类"""

    def test_table_similarity_no_false_positive_on_unrelated_shape(self):
        """外形相同 (如均是 4x3) 但内容毫无关系的表格，相似度必须为 0.0，不得误判为相似"""
        df1 = pd.DataFrame({
            "Name": ["Alice", "Bob", "Charlie"],
            "City": ["Beijing", "Shanghai", "Shenzhen"],
            "Role": ["Dev", "Test", "PM"]
        })
        df2 = pd.DataFrame({
            "Year": ["2020", "2021", "2022"],
            "Revenue": ["100M", "150M", "200M"],
            "Growth": ["10%", "15%", "20%"]
        })
        sim = pte._table_similarity(df1, df2)
        self.assertEqual(sim, 0.0)

    def test_align_page_tables_no_false_merge_on_disjoint_bboxes(self):
        """同一页上一上一下两个不同表格，即便外形或得分相近，因 2D bbox 无重叠，严禁聚入同簇"""
        df_top = pd.DataFrame({"C1": ["A", "B"], "C2": ["C", "D"]})
        df_bot = pd.DataFrame({"C1": ["E", "F"], "C2": ["G", "H"]})

        page_tables = {
            'find_tables': [
                {'df': df_top, 'bbox': [50, 100, 500, 200], 'page_idx': 0},
                {'df': df_bot, 'bbox': [50, 500, 500, 600], 'page_idx': 0},
            ]
        }
        clusters = pte._align_page_tables(page_tables, priority=['find_tables'])
        # 必须分为两个独立的簇，绝不能合并为一个簇
        self.assertEqual(len(clusters), 2)

    def test_align_page_tables_label_collision_prevention(self):
        """显式表号冲突 (Table 1 vs Table 2) 的表格，绝对严禁聚入同簇"""
        df1 = pd.DataFrame({"C1": ["1", "2"], "C2": ["3", "4"]})
        df1.attrs['label'] = "Table 1"
        df2 = pd.DataFrame({"C1": ["1", "2"], "C2": ["3", "4"]})
        df2.attrs['label'] = "Table 2"

        page_tables = {
            'find_tables': [{'df': df1, 'label': 'Table 1', 'page_idx': 0}],
            'camelot': [{'df': df2, 'label': 'Table 2', 'page_idx': 0}],
        }
        clusters = pte._align_page_tables(page_tables, priority=['find_tables', 'camelot'])
        self.assertEqual(len(clusters), 2)


class TestYoloAndPPStructureCooperation(unittest.TestCase):
    """5: 验证 YOLO 覆盖不足时触发 PP-Structure 补扫"""

    @patch("os.path.exists", return_value=True)
    @patch("pdf_tables.inspect_crop_region")
    @patch("pdf_tables.extract_pp_structure_table_crops")
    @patch("pdf_tables.DocLayoutYoloDetector")
    @patch("fitz.open")
    def test_yolo_partial_coverage_triggers_pp_structure_supplement(
        self, mock_fitz, mock_detector_cls, mock_pp_crops, mock_inspect, mock_exists
    ):
        """当 YOLO 仅在部分页面检出切图时，确保调用 PP-Structure 对遗漏页面进行补扫并合并"""
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 3
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Page content"
        mock_pix = MagicMock()
        mock_pix.width = 100
        mock_pix.height = 100
        mock_pix.samples = b'\x00' * (100 * 100 * 3)
        mock_page.get_pixmap.return_value = mock_pix
        mock_doc.__getitem__.return_value = mock_page
        mock_fitz.return_value = mock_doc

        mock_inspect.return_value = {
            "needs_ocr": False, "ocr_reason": "", "text": "", "pdf_bbox": [10, 10, 100, 100]
        }

        # 模拟 YOLO 仅在 Page 0 检出切图
        mock_det = MagicMock()
        def mock_extract(img, conf_threshold):
            # 仅第一次调用（Page 0）返回切图，Page 1 返回空
            if not hasattr(mock_extract, "called"):
                mock_extract.called = True
                return [{"crop_bbox": [10, 10, 100, 100], "score": 0.9}]
            return []
        mock_det.extract_extended_table_regions.side_effect = mock_extract
        mock_detector_cls.return_value = mock_det

        # 模拟 PP-Structure 对 Page 1 补扫出切图
        mock_pp_crops.return_value = [
            {"page_index": 1, "crop_bbox": [20, 20, 200, 200], "score": 0.85}
        ]

        config = {"USE_PP_STRUCTURE": True}
        crops = pdf_tables.extract_table_crops_from_pdf(
            "dummy.pdf", pages=[0, 1], config=config
        )

        # 验证 PP-Structure 针对 Page 1 进行了补扫
        mock_pp_crops.assert_called_once()
        call_kwargs = mock_pp_crops.call_args[1]
        self.assertEqual(call_kwargs.get("pages"), [1])

        # 最终合并切图应包含 Page 0 和 Page 1
        pages_in_crops = set(c["page_index"] for c in crops)
        self.assertEqual(pages_in_crops, {0, 1})


class TestTableValidatorCompleteness(unittest.TestCase):
    """6: 验证验证器在无 Caption 或表号未标注时的全覆盖检查"""

    @patch("table_validator.scan_pdf_table_declarations", return_value={})
    @patch("fitz.open")
    def test_validator_flags_candidate_page_without_caption_when_no_tables_extracted(
        self, mock_fitz, mock_scan
    ):
        """当候选页来自视觉检测（无 Caption 声明），若原生提取未获得表格，必须标记由 OCR 接管"""
        # target_pages 包含 Page 2 (0-based)
        target_pages = [2]
        native_results = []  # 未提取到任何表格
        vote_summary = {}

        valid_fused, pages_req_ocr, val_logs = validate_native_extraction_pipeline(
            native_results, "dummy.pdf", target_pages, vote_summary
        )

        self.assertIn(2, pages_req_ocr)
        self.assertTrue(any("Page 3 为候选表格页但未获得任何有效原生表格" in l for l in val_logs))

    @patch("table_validator.scan_pdf_table_declarations")
    @patch("fitz.open")
    def test_validator_auto_binds_unlabeled_table_on_declared_page(
        self, mock_fitz, mock_scan
    ):
        """当 declared_page 提取到了有效表格但未分配标签时，自动绑定并避免假阴性标记缺失"""
        mock_scan.return_value = {
            1: [{'label': 'Table 2', 'title': 'Table 2 Characteristics', 'is_continuation': False}]
        }
        df_valid = pd.DataFrame({"Sample": ["S1", "S2", "S3", "S4"], "Value": [10.5, 20.3, 30.1, 40.2]})
        df_valid.attrs['page_idx'] = 1  # 无 label
        native_results = [{'df': df_valid, 'page_idx': 1}]

        valid_fused, pages_req_ocr, val_logs = validate_native_extraction_pipeline(
            native_results, "dummy.pdf", [1], {}
        )

        # Table 2 被自动关联绑定，无需误判为缺失表号重走 OCR
        self.assertNotIn(1, pages_req_ocr)
        self.assertEqual(df_valid.attrs.get('label'), "Table 2")


class TestOnlineLabelSetCompleteness(unittest.TestCase):
    """7: 验证在线提取表号集合核验与增量补漏"""

    @patch("fitz.open")
    def test_scan_pdf_table_expected_labels_full_doc(self, mock_fitz):
        """确保扫描全篇 PDF，不人为受限在 150 页之内"""
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 200
        # 在第 180 页设置表标题
        def mock_get_text(p_idx):
            p = MagicMock()
            if p_idx == 180:
                p.get_text.return_value = "Table 5. Results of experiment\nData follows..."
            else:
                p.get_text.return_value = "Normal text..."
            return p
        mock_doc.__getitem__.side_effect = mock_get_text
        mock_fitz.return_value = mock_doc

        labels, count = scan_pdf_table_expected_labels("dummy.pdf")
        self.assertIn("Table 5", labels)
        self.assertEqual(count, 1)


    @patch("online.extract_tables_online")
    @patch("extract_zotero_table.save_tables_to_excel", return_value=True)
    @patch("extract_zotero_table.scan_pdf_table_expected_labels")
    @patch("pdf_table_extractor.quick_classify_pdf")
    def test_online_missing_label_triggers_pdf_supplement(
        self, mock_clf, mock_scan, mock_save, mock_online
    ):
        """当在线表数虽然达标但缺失具体表号时 (如提取了两个 Table 1 漏了 Table 2)，确保触发本地 PDF 补充"""
        mock_clf.return_value = {'confidence': 1.0, 'pdf_type': 'text_based'}
        # PDF 预期 Table 1 和 Table 2
        mock_scan.return_value = ({"Table 1", "Table 2"}, 2)

        df1_a = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        df1_a.attrs['label'] = "Table 1"
        df1_b = pd.DataFrame({"A": [5, 6], "B": [7, 8]})
        df1_b.attrs['label'] = "Table 1"
        # online 获得了 2 个表格，但全是 Table 1，缺失 Table 2
        online_dfs = [df1_a, df1_b]
        mock_online.return_value = (online_dfs, "Mock Title")

        # 本地 PDF 补充提取到了 Table 2
        df2 = pd.DataFrame({"C": [9, 10], "D": [11, 12]})
        df2.attrs['label'] = "Table 2"
        df2.attrs['extractor'] = 'find_tables'

        class DummyArgs:
            output = 'dummy_out'
            single_file = False
            online_only = False
            pdf_only = False
            headers = None
            skip_supplementary = False
            structured_file = None
            table_idx = 'all'
            db_path = None
            firecrawl_key = None
            cnki_strategy = 'auto'
            headed = False
            user_data_dir = None
            sequential = True
            enable_llm = False

        args = DummyArgs()
        real_pdf = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scratch", "test_bbox.pdf"))

        with patch.object(extract_zotero_table, 'extract_tables_from_pdf', return_value=([{'df': df2, 'page_idx': 1}], ["Extracted Table 2"])) as mock_pdf_extract:
            success = process_single_pdf(real_pdf, args)
            self.assertTrue(success)
            # 必须触发了本地 PDF 补充
            mock_pdf_extract.assert_called_once()


class TestSpatialIoUAndContinuation(unittest.TestCase):
    """8: 验证 2D Bbox IoU 计算与续表推断"""

    def test_compute_bbox_iou_disjoint_and_overlap(self):
        """验证 2D IoU 与 IoM 正确计算"""
        # 不相交
        b1 = [0, 0, 100, 100]
        b2 = [200, 200, 300, 300]
        iou, iom = pte._compute_bbox_iou(b1, b2)
        self.assertEqual(iou, 0.0)
        self.assertEqual(iom, 0.0)

        # 完全包含 (小框在大框内)
        b_outer = [0, 0, 100, 100]
        b_inner = [10, 10, 90, 90]
        iou, iom = pte._compute_bbox_iou(b_outer, b_inner)
        self.assertEqual(iom, 1.0)
        self.assertTrue(iou > 0.5)

    @patch("pdf_table_extractor.has_text_layer", return_value=True)
    @patch("pdf_table_extractor.classify_and_extract_via_inspector")
    @patch("table_validator.scan_pdf_table_declarations")
    @patch("fitz.open")
    def test_continuation_stops_at_new_table_declaration(
        self, mock_fitz, mock_decls, mock_clf, mock_has_text
    ):
        """当后继页包含新的独立表格声明时，前一表格的跨页续表追溯必须在此终止"""
        mock_decls.return_value = {
            0: [{'label': '表1', 'title': '表1', 'is_continuation': False}],
            1: [{'label': '表2', 'title': '表2 新表格', 'is_continuation': False}],
        }
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

        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 3
        p0 = MagicMock()
        p0.get_text.return_value = "Normal text..."
        p0.get_images.return_value = []
        p1 = MagicMock()
        p1.get_text.return_value = "表2 新表格\n10 20 30"
        p1.get_images.return_value = []
        p2 = MagicMock()
        p2.get_text.return_value = "Normal text page 3..."
        p2.get_images.return_value = []

        mock_doc.__getitem__.side_effect = lambda idx: [p0, p1, p2][idx]
        mock_fitz.return_value = mock_doc

        results, logs = pte.extract_tables_from_pdf("dummy.pdf", use_ocr_fallback=False)
        cand_log = [l for l in logs if "候选表格页面" in l]
        self.assertTrue(len(cand_log) > 0)
        # Page 1 (index 0) 和 Page 2 (index 1) 来自各自的声明，Page 3 (index 2) 绝不可被误判为续表
        self.assertIn("1", cand_log[0])
        self.assertIn("2", cand_log[0])
        self.assertNotIn("'continuation'", str(cand_log[0]))


class TestAdvancedRemediationRegression(unittest.TestCase):
    """9: 深度回归测试 - 修复同页多表覆盖、跨页 OCR 重复、NoneType 异常与全篇切图范围"""

    def test_validator_auto_binds_two_unlabeled_tables_without_overwriting(self):
        """同页上有两个未标注表号的表格，且声明了 Table 1 与 Table 2 时，确保两者分别绑定，绝不互相抢夺覆盖"""
        decls = {
            0: [
                {'label': 'Table 1', 'title': 'Table 1 Demographics', 'is_continuation': False},
                {'label': 'Table 2', 'title': 'Table 2 Clinical Outcomes', 'is_continuation': False},
            ]
        }
        with patch("table_validator.scan_pdf_table_declarations", return_value=decls):
            df_t1 = pd.DataFrame({"Age": [25, 30], "Sex": ["M", "F"]})
            df_t1.attrs['table_title'] = "Table 1 Demographics"
            df_t1.attrs['page_idx'] = 0

            df_t2 = pd.DataFrame({"Outcome": ["Good", "Fair"], "Score": [90, 75]})
            df_t2.attrs['table_title'] = "Table 2 Clinical Outcomes"
            df_t2.attrs['page_idx'] = 0

            native_results = [
                {'df': df_t1, 'page_idx': 0},
                {'df': df_t2, 'page_idx': 0},
            ]

            valid_fused, pages_req_ocr, val_logs = validate_native_extraction_pipeline(
                native_results, "dummy.pdf", [0], {}
            )

            # 两个表格应分别成功绑定 Table 1 和 Table 2，无需报错重走 OCR
            self.assertEqual(len(pages_req_ocr), 0)
            labels = [r['df'].attrs.get('label') for r in valid_fused]
            self.assertIn("Table 1", labels)
            self.assertIn("Table 2", labels)
            self.assertEqual(len(labels), 2)

    def test_reconcile_native_and_ocr_tables_multi_page_no_duplicate(self):
        """当 OCR 结果包含跨页多分页大表 (covered_pages=[0, 1]) 时，确保在合并结果中仅存在 1 份，严禁重复追加"""
        df_multi = pd.DataFrame({"Col": [1, 2, 3, 4]})
        df_multi.attrs['label'] = "Table 1"
        df_multi.attrs['covered_pages'] = [0, 1]

        ocr_results = [
            {'df': df_multi, 'page_idx': 0, 'covered_pages': [0, 1], 'label': 'Table 1'}
        ]

        df_nat0 = pd.DataFrame({"Col": [1, 2]})
        df_nat0.attrs['label'] = "Table 1"
        df_nat1 = pd.DataFrame({"Col": [3, 4]})
        df_nat1.attrs['label'] = "Table 1"
        native_results = [
            {'df': df_nat0, 'page_idx': 0, 'label': 'Table 1'},
            {'df': df_nat1, 'page_idx': 1, 'label': 'Table 1'},
        ]

        logs = []
        reconciled = pte.reconcile_native_and_ocr_tables(native_results, ocr_results, [0, 1], logs)

        # 跨页 OCR 结果合并后必须仅有 1 个表格
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]['df'].attrs.get('covered_pages'), [0, 1])

    def test_reconcile_native_and_ocr_tables_handles_none_df_safely(self):
        """当 native_results 或 ocr_results 包含 df 为 None 的异常字典时，绝不抛出 AttributeError"""
        native_results = [
            {'df': None, 'page_idx': 0, 'label': None}
        ]
        ocr_results = [
            {'df': None, 'page_idx': 0, 'label': None}
        ]
        logs = []
        try:
            reconciled = pte.reconcile_native_and_ocr_tables(native_results, ocr_results, [0], logs)
        except AttributeError as e:
            self.fail(f"reconcile_native_and_ocr_tables raised AttributeError: {e}")

    @patch("os.path.exists", return_value=True)
    @patch("pdf_tables.inspect_crop_region")
    @patch("pdf_tables.extract_pp_structure_table_crops")
    @patch("pdf_tables.DocLayoutYoloDetector")
    @patch("fitz.open")
    def test_extract_table_crops_pages_none_does_not_mark_all_pages_missing(
        self, mock_fitz, mock_detector_cls, mock_pp_crops, mock_inspect, mock_exists
    ):
        """当 pages=None (整篇扫描) 且全篇仅 Page 0 有 1 个表格且 YOLO 已检出时，绝不把其他无表普通页面误判为 missing_pages"""
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 5  # 5 页 PDF
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Regular paragraph text..."
        mock_pix = MagicMock()
        mock_pix.width = 100
        mock_pix.height = 100
        mock_pix.samples = b'\x00' * (100 * 100 * 3)
        mock_page.get_pixmap.return_value = mock_pix
        mock_doc.__getitem__.return_value = mock_page
        mock_fitz.return_value = mock_doc

        mock_inspect.return_value = {
            "needs_ocr": False, "ocr_reason": "", "text": "", "pdf_bbox": [10, 10, 100, 100]
        }

        # YOLO 仅在 Page 0 提取到了切图
        mock_det = MagicMock()
        def mock_extract(img, conf_threshold):
            if not hasattr(mock_extract, "called"):
                mock_extract.called = True
                return [{"crop_bbox": [10, 10, 100, 100], "score": 0.9}]
            return []
        mock_det.extract_extended_table_regions.side_effect = mock_extract
        mock_detector_cls.return_value = mock_det

        # 全文仅 Page 0 声明了 1 个表格
        with patch("table_validator.scan_pdf_table_declarations", return_value={0: [{'label': '表1', 'is_continuation': False}]}):
            crops = pdf_tables.extract_table_crops_from_pdf("dummy.pdf", pages=None, config={"USE_PP_STRUCTURE": True})
            
            # 因为 Page 0 已有切图，且其他页面没有表格声明，PP-Structure 绝不应被调用补扫无表页面
            mock_pp_crops.assert_not_called()
            self.assertEqual(len(crops), 1)

    def test_align_page_tables_hard_disqualification_spatial_disjoint(self):
        """当一个簇已有带坐标表格 A 时，候选表格 B 若与 A 空间完全不重叠 (IoM=0)，严禁加入该簇"""
        df_a = pd.DataFrame({"Col": [1, 2]})
        df_b = pd.DataFrame({"Col": [1, 2]})  # 相似内容

        page_tables = {
            'pdf_inspector': [{'df': df_a, 'bbox': [50, 100, 500, 200], 'page_idx': 0}],
            'camelot': [{'df': df_b, 'bbox': [50, 600, 500, 700], 'page_idx': 0}],
        }
        clusters = pte._align_page_tables(page_tables, priority=['pdf_inspector', 'camelot'])
        # 必须分为两个独立的簇
        self.assertEqual(len(clusters), 2)


if __name__ == "__main__":
    unittest.main()


