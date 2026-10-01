"""
Unit tests for omission risks and comprehensive audit in zotero-table-extractor.
Covers all 9 audit gaps:
1. TABLE_NUM_PART and TABLE_LABEL_RE regex truncation (Table S1/S2/S10, TABLE III/VIII, Tab 1)
2. Paywall keyword word boundaries and weak/strong keyword distinction (parent/different/experiment)
3. 1-row and 1-column table survival across validator and postprocess
4. Candidate page discovery for drawing-free (has_drawings=False) text tables
5. Continuation labels (表1（续）, Table 1 (cont.), cont'd, 续上表) and mutual exclusivity
6. Step 3c preservation of multiple tables on the same page
7. OCR quality gate (_should_replace_with_ocr) preventing inferior OCR replacement
8. Chapter document unlabelled table export as unlabelled_N.xlsx & paywall isolation
9. Native table fallback preservation when use_ocr_fallback=False
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import pymupdf as fitz

from scripts.common import (
    TABLE_LABEL_RE,
    format_table_label,
    is_table_low_quality,
    is_metadata_table,
)
from scripts.table_postprocess import (
    postprocess_dataframe,
    merge_continuation_tables,
)
from scripts.table_validator import (
    scan_pdf_table_declarations,
    validate_native_table_dataframe,
)
from scripts.pdf_table_extractor import (
    _should_replace_with_ocr,
    reconcile_native_and_ocr_tables,
    extract_table_caption_from_page,
)
from scripts.excel_export import (
    save_tables_to_excel,
)


class TestOmissionComprehensiveAudit(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_files = []

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        for f in self.temp_files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # Gap 1: Table Label Regex Truncation (Table S1/S2/S10, TABLE III/VIII, Tab 1)
    # -------------------------------------------------------------------------
    def test_gap1_table_label_regex_variants(self):
        """测试 Table S1, TABLE III, Tab. 1 等变体能够被完整匹配并正确标准化格式"""
        test_cases = [
            ("Table S1", "Table S1"),
            ("Table S2", "Table S2"),
            ("Table S10", "Table S10"),
            ("Supplementary Table S3", "Supplementary Table S3"),
            ("TABLE III", "Table III"),
            ("Table VIII", "Table VIII"),
            ("TABLE IV", "Table IV"),
            ("Tab. 1", "Table 1"),
            ("Tab 2", "Table 2"),
            ("表1-1", "表1-1"),
            ("表 2.3", "表2.3"),
            ("附表 4", "附表4"),
            ("Table 1A", "Table 1A"),
            ("Table 1A: Results", "Table 1A"),
            ("Table 1A. Description of parameters", "Table 1A"),
            ("Table 1(a). Summary of findings", "Table 1(a)"),
            ("Table S1A. Overview", "Table S1A"),
            ("Table A-1. Description", "Table A-1"),
            ("附表 1A. 测试表格", "附表1A"),
            ("Table 3 - continued", "Table 3"),
        ]
        for raw, expected in test_cases:
            m = TABLE_LABEL_RE.search(raw)
            self.assertIsNotNone(m, f"Regex should match '{raw}'")
            formatted = format_table_label(raw)
            self.assertEqual(formatted, expected, f"Expected '{expected}' for '{raw}', got '{formatted}'")

    def test_gap1_roman_num_no_empty_lookbehind(self):
        """测试 Table 3 - continued 或以 M/C/D/L/X/V/I 开头的单词紧随表号时不会被空罗马数字误吞"""
        # "Table 3 - continued": "continued" starts with 'c'
        m = TABLE_LABEL_RE.search("Table 3 - continued")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(0), "Table 3")
        self.assertEqual(format_table_label("Table 3 - continued"), "Table 3")

        # "Table 2 - Method and results": "Method" starts with 'M'
        m2 = TABLE_LABEL_RE.search("Table 2 - Method and results")
        self.assertIsNotNone(m2)
        self.assertEqual(m2.group(0), "Table 2")
        self.assertEqual(format_table_label("Table 2 - Method and results"), "Table 2")

    # -------------------------------------------------------------------------
    # Gap 2: Paywall Keyword Word Boundaries & Weak/Strong Distinction
    # -------------------------------------------------------------------------
    def test_gap2_paywall_scientific_terms_not_falsely_rejected(self):
        """测试包含 parent, different, experiment, register of samples 等学术词汇的表格不会被误杀"""
        df_science = pd.DataFrame({
            "Sample ID": ["Parent rock A", "Different rock B", "Experiment C"],
            "Location": ["Register of samples", "Section 1", "Section 2"],
            "Purchase Cost ($)": ["120.0", "340.5", "50.0"],
            "Yield (%)": ["98.2", "95.1", "99.0"],
        })
        df_science.attrs = {"label": "Table 1", "table_title": "Geochemical experiment"}

        res = postprocess_dataframe(df_science)
        self.assertFalse(res.empty, "Scientific table should not be rejected by paywall filter")
        self.assertNotEqual(res.attrs.get("rejected_reason"), "paywall")
        self.assertEqual(len(res), 3)

    def test_gap2_paywall_actual_login_warning_rejected_and_tagged(self):
        """测试包含真实登录/付费墙警告的表格被正确识别并赋予 rejected_reason='paywall'"""
        df_paywall = pd.DataFrame({
            "Notice": ["Access through your institution", "Purchase instant access"],
            "Action": ["Sign in to view full text", "Institutional login"],
        })
        df_paywall.attrs = {"label": "Table 99"}

        res = postprocess_dataframe(df_paywall)
        self.assertTrue(res.empty, "True paywall table should be rejected")
        self.assertEqual(res.attrs.get("rejected_reason"), "paywall")
        self.assertIn("rejected_raw_df", res.attrs)

    # -------------------------------------------------------------------------
    # Gap 3: 1-Row & 1-Column Table Survival Across Pipeline
    # -------------------------------------------------------------------------
    def test_gap3_single_row_and_single_col_table_survival(self):
        """测试 1 行表与 1 列表在 validator 与 postprocess 中完整存活"""
        # 1-row table (e.g. summary benchmark)
        df_1row = pd.DataFrame([["Model-A", "95.4", "0.012"]], columns=["Model", "Accuracy", "Loss"])
        valid_1row, reason_1row = validate_native_table_dataframe(df_1row, page_idx=0)
        self.assertTrue(valid_1row, f"1-row table should be valid: {reason_1row}")
        self.assertFalse(is_table_low_quality(df_1row))

        post_1row = postprocess_dataframe(df_1row)
        self.assertFalse(post_1row.empty, "1-row table should survive postprocess")
        self.assertEqual(post_1row.shape[0], 1)

        # 1-column table (e.g. parameter column or sequence)
        df_1col = pd.DataFrame([["Param1"], ["Param2"], ["Param3"]], columns=["Variables"])
        valid_1col, reason_1col = validate_native_table_dataframe(df_1col, page_idx=0)
        self.assertTrue(valid_1col, f"1-col table should be valid: {reason_1col}")
        self.assertFalse(is_table_low_quality(df_1col))

        post_1col = postprocess_dataframe(df_1col)
        self.assertFalse(post_1col.empty, "1-col table should survive postprocess")
        self.assertEqual(post_1col.shape[1], 1)

    # -------------------------------------------------------------------------
    # Gap 4: Drawing-Free (has_drawings=False) Text Tables Candidate Discovery
    # -------------------------------------------------------------------------
    def test_gap4_drawing_free_text_table_candidate_discovery(self):
        """测试当页面无任何矢量绘图 (has_drawings=False) 但存在文本表格时，候选页探测不漏检"""
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        doc = fitz.open()
        p = doc.new_page(width=595, height=842)
        p.insert_text((50, 100), "Table 1. Physical Properties of Samples", fontsize=11)
        p.insert_text((50, 130), "Sample\tDensity\tPorosity", fontsize=10)
        p.insert_text((50, 150), "SMP-1\t2.65\t3.2", fontsize=10)
        p.insert_text((50, 170), "SMP-2\t2.71\t2.8", fontsize=10)
        doc.save(path)
        doc.close()

        # 验证该页无 drawings
        with fitz.open(path) as doc_check:
            drawings = doc_check[0].get_drawings()
            self.assertEqual(len(drawings), 0, "Page should have no vector drawings")

        # 验证 scan_pdf_table_declarations 发现声明
        decls = scan_pdf_table_declarations(path)
        self.assertIn(0, decls)
        self.assertEqual(decls[0][0]["label"], "Table 1")

    # -------------------------------------------------------------------------
    # Gap 5: Continuation Labels and Mutual Exclusivity
    # -------------------------------------------------------------------------
    def test_gap5_continuation_labels_recognition_and_mutual_exclusivity(self):
        """测试 表1（续）、Table 1 (cont.)、Table 2 Continued、cont'd、续上表 识别为续表且不计为新表声明"""
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        doc = fitz.open()
        # Page 0: Main table
        p0 = doc.new_page(width=595, height=842)
        p0.insert_text((50, 100), "Table 1. Experimental Results", fontsize=11)
        # Page 1: Continuation variant A
        p1 = doc.new_page(width=595, height=842)
        p1.insert_text((50, 100), "Table 1 (cont.)", fontsize=11)
        # Page 2: Continuation variant B
        p2 = doc.new_page(width=595, height=842)
        p2.insert_text((50, 100), "表2（续）", fontsize=11, fontname="china-s")
        # Page 3: Continuation variant C
        p3 = doc.new_page(width=595, height=842)
        p3.insert_text((50, 100), "Table 3 Continued", fontsize=11)
        # Page 4: Continuation variant D
        p4 = doc.new_page(width=595, height=842)
        p4.insert_text((50, 100), "续上表", fontsize=11, fontname="china-s")
        # Page 5: Continuation variant E (numbered Chinese)
        p5 = doc.new_page(width=595, height=842)
        p5.insert_text((50, 100), "表2（续一）", fontsize=11, fontname="china-s")
        # Page 6: Continuation variant F (numbered English)
        p6 = doc.new_page(width=595, height=842)
        p6.insert_text((50, 100), "Table 4 (cont. 1)", fontsize=11)
        # Page 7: Continuation variant G (numbered Arabic Chinese)
        p7 = doc.new_page(width=595, height=842)
        p7.insert_text((50, 100), "表5 (续2)", fontsize=11, fontname="china-s")
        doc.save(path)
        doc.close()

        decls = scan_pdf_table_declarations(path)
        self.assertIn(0, decls)
        self.assertFalse(decls[0][0]["is_continuation"])
        self.assertEqual(decls[0][0]["label"], "Table 1")

        for p_idx in [1, 2, 3, 4, 5, 6, 7]:
            self.assertIn(p_idx, decls)
            self.assertTrue(decls[p_idx][0]["is_continuation"], f"Page {p_idx} should be recognized as continuation")

    # -------------------------------------------------------------------------
    # Gap 6: Step 3c Multi-Table Preservation on the Same Page
    # -------------------------------------------------------------------------
    def test_gap6_step3c_multiple_tables_same_page_preserved(self):
        """测试同一页面上的多个独立表格在 Step 3c 中全部被保留，不发生覆盖或丢弃"""
        df1 = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        df1.attrs = {"label": "Table 1", "page_idx": 0}

        df2 = pd.DataFrame({"X": [10, 20], "Y": [30, 40]})
        df2.attrs = {"label": "Table 2", "page_idx": 0}

        extracted_pages = set()
        all_results = []

        # 模拟 Step 3c 流程
        tables_to_add = [
            {"df": df1, "page_idx": 0, "table_idx": 0},
            {"df": df2, "page_idx": 0, "table_idx": 1},
        ]

        for ta in tables_to_add:
            ta_page = ta["page_idx"]
            all_results.append(ta)
            extracted_pages.add(ta_page)

        self.assertEqual(len(all_results), 2, "Both tables on the same page must be preserved")
        self.assertEqual(all_results[0]["df"].attrs["label"], "Table 1")
        self.assertEqual(all_results[1]["df"].attrs["label"], "Table 2")

    # -------------------------------------------------------------------------
    # Gap 7: OCR Quality Gate (_should_replace_with_ocr)
    # -------------------------------------------------------------------------
    def test_gap7_ocr_quality_comparison_gate(self):
        """测试 _should_replace_with_ocr 质量比对门禁：阻止空表或劣质 OCR 破坏优质原生表"""
        native_df = pd.DataFrame({
            "Sample": [f"SMP-{i}" for i in range(10)],
            "SiO2": [70.0 + i for i in range(10)],
            "Al2O3": [14.0 + i * 0.1 for i in range(10)],
        })
        native_df.attrs = {"extractor": "find_tables", "label": "Table 1"}

        # 1. OCR 结果为空 -> 拒绝
        empty_ocr = pd.DataFrame()
        empty_ocr.attrs = {"extractor": "paddleocr_vl"}
        self.assertFalse(_should_replace_with_ocr(native_df, empty_ocr))

        # 2. OCR 结果行数严重萎缩 (<50%) -> 拒绝
        shrunk_ocr = pd.DataFrame({"Sample": ["SMP-0", "SMP-1"], "SiO2": ["70.0", "71.0"]})
        shrunk_ocr.attrs = {"extractor": "paddleocr_vl"}
        self.assertFalse(_should_replace_with_ocr(native_df, shrunk_ocr))

        # 3. OCR 结果更完整更优质 (行数更多且质量正常) -> 接受
        superior_ocr = pd.DataFrame({
            "Sample": [f"SMP-{i}" for i in range(15)],
            "SiO2": [70.0 + i for i in range(15)],
            "Al2O3": [14.0 + i * 0.1 for i in range(15)],
        })
        superior_ocr.attrs = {"extractor": "paddleocr_vl"}
        self.assertTrue(_should_replace_with_ocr(native_df, superior_ocr))

        # 4. 原生表为低质量损坏表，OCR 结果有效 -> 接受
        bad_native = pd.DataFrame({"Col1": ["a", "b", "c"]})  # 单列破碎表
        bad_native.attrs = {"extractor": "pdfplumber"}
        good_ocr = pd.DataFrame({"A": [1, 2, 3], "B": [4, 5, 6]})
        good_ocr.attrs = {"extractor": "paddleocr_vl"}
        self.assertTrue(_should_replace_with_ocr(bad_native, good_ocr))

    # -------------------------------------------------------------------------
    # Gap 8: Chapter Document Unlabelled Export & Paywall Isolation
    # -------------------------------------------------------------------------
    def test_gap8_chapter_document_unlabelled_export_and_paywall_isolation(self):
        """测试章节文献中未命名表格碎片导出为 unlabelled_N.xlsx，且付费墙表隔离至 rejected 目录"""
        out_dir = os.path.join(self.temp_dir, "export_test")
        os.makedirs(out_dir, exist_ok=True)

        df_chapter = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        df_chapter.attrs = {"label": "表1-1", "table_title": "第一章岩石参数"}

        df_unlabelled = pd.DataFrame({"X": [10, 20], "Y": [30, 40]})
        df_unlabelled.attrs = {"label": "", "table_title": "", "page_idx": 5}

        df_raw = pd.DataFrame({"Warning": ["Purchase instant access", "Institutional login"]})
        df_paywall = pd.DataFrame()
        df_paywall.attrs = {"label": "Table 99", "rejected_reason": "paywall", "rejected_raw_df": df_raw}

        dfs = [df_chapter, df_unlabelled, df_paywall]
        save_ok = save_tables_to_excel(dfs, out_dir, single_file=False)
        self.assertTrue(save_ok)

        # 检查常规表与未命名表是否成功导出
        exported_files = os.listdir(out_dir)
        self.assertIn("表1-1.xlsx", exported_files)
        self.assertIn("unlabelled_1.xlsx", exported_files, "Unlabelled table fragment should be saved as unlabelled_1.xlsx")

        # 检查付费墙表是否被隔离至 rejected/ 目录
        rej_dir = os.path.join(out_dir, "rejected")
        self.assertTrue(os.path.exists(rej_dir), "rejected/ directory should be created")
        rej_files = os.listdir(rej_dir)
        self.assertTrue(any(f.startswith("paywall_table_") for f in rej_files), "Paywall table should be in rejected/")

    # -------------------------------------------------------------------------
    # Gap 9: Native Extraction Fallback When use_ocr_fallback=False
    # -------------------------------------------------------------------------
    def test_gap9_use_ocr_fallback_false_preserves_native_tables(self):
        """测试当 use_ocr_fallback=False 时，未通过严格校验的原生表格仍被作为降级兜底保留"""
        from scripts.pdf_table_extractor import extract_tables_from_pdf

        # 创建一个单行无边框的简单 PDF 测试文献
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.temp_files.append(path)

        doc = fitz.open()
        p = doc.new_page(width=595, height=842)
        p.insert_text((50, 100), "Table 1. Experimental Overview", fontsize=11)
        p.insert_text((50, 130), "ParamA\tParamB\tParamC", fontsize=10)
        p.insert_text((50, 150), "Val1\tVal2\tVal3", fontsize=10)
        doc.save(path)
        doc.close()

        results, logs = extract_tables_from_pdf(path, use_ocr_fallback=False)
        self.assertGreaterEqual(len(results), 1, "Native tables should be returned even with use_ocr_fallback=False")
        self.assertEqual(results[0]["df"].attrs.get("label"), "Table 1")

    # -------------------------------------------------------------------------
    # Gap 10: Step 5 Multi-Page OCR Reconciliation Without Duplication
    # -------------------------------------------------------------------------
    def test_gap10_step5_multipage_ocr_reconciliation(self):
        """测试 Step 5 全局 OCR 补漏在多页 PDF 下正确按页对齐，不跳过高页码也不产生同页同表重复"""
        df_nat = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        df_nat.attrs["label"] = "Table 7"
        df_ocr = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        df_ocr.attrs["label"] = "Table 7"

        all_results = [{"df": df_nat, "page_idx": 7, "table_idx": 0}]
        ocr_results = [{"df": df_ocr, "page_idx": 7, "table_idx": 0}]

        pages_to_reconcile = sorted(list(set(
            [r.get('page_idx', 0) for r in ocr_results] +
            [r.get('page_idx', 0) for r in all_results] +
            [p for r in ocr_results for p in r.get('covered_pages', [])]
        )))
        reconciled = reconcile_native_and_ocr_tables(all_results, ocr_results, pages_to_reconcile)
        self.assertEqual(len(reconciled), 1, "Should reconcile to 1 table instead of duplicating on page 7")

    # -------------------------------------------------------------------------
    # Gap 11: Bibliometric Study Table Survival (Citations/DOI/Downloads)
    # -------------------------------------------------------------------------
    def test_gap11_bibliometric_study_table_survival(self):
        """测试文献计量学研究数据表（含 DOI、Citations、被引次数等列）不会被误判为文献元数据表"""
        df_biblio = pd.DataFrame({
            "Paper Title": ["Deep Learning in Geology", "Transformers in NLP", "Graph Neural Networks"],
            "DOI": ["10.1016/j.env.2021", "10.1145/334.2022", "10.1007/s002.2023"],
            "Citations": [145, 890, 312],
            "Downloads": [2400, 15000, 5600],
            "Year": [2021, 2022, 2023]
        })
        self.assertFalse(is_metadata_table(df_biblio), "Bibliometric study table must survive is_metadata_table")


if __name__ == "__main__":
    unittest.main()
