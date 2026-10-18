#!/usr/bin/env python3
"""
test_audit_oct18_continued_fixes.py — 针对最新 5 处严重缺陷与端到端 Excel 导出全链路核验的综合测试套件：
1. [P1] 保留的冲突版本，在导出到 Excel 时不再被粗暴去重删除，分配独立 Sheet/文件，0 丢失样本
2. [P1] 污染表修复严禁丢失有效数据记录（核对 clean_n_rows 与样本保留，拒绝只返回一半行的截断 OCR）
3. [P1] 分文件/分工作表导出会正确识别远隔页面的同号独立表 (page distance >= 2)，绝不互相覆盖
4. [P2] MinerU 结构化产物包多表示逐表补漏 (跨 candidate JSON 融合非重复表，杜绝单一 JSON 部分成功后漏表)
5. [P2] 损坏或空缓存安全失效并自愈回退至 MinerU 主抽取接口，杜绝漏表
6. 端到端全链路测试：从解析、合并到实际 Excel Workbook 导出，深入核验 Sheet、行数、列名与真实数据元胞值
"""

import io
import os
import sys
import json
import zipfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
from openpyxl import load_workbook

# 确保 scripts 目录在 sys.path 中
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import extract_zotero_table as cli
import excel_export as ee
import ocr_client as oc
import pdf_table_extractor as pe
import table_postprocess as tp
import table_validator as tv
import common


class TestAuditOct18ContinuedFixes(unittest.TestCase):
    """验证最新 5 处严重缺陷修复与端到端导出质量。"""

    def _create_archive(self, files_dict: dict) -> bytes:
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, 'w', zipfile.ZIP_DEFLATED) as zf:
            for name, data in files_dict.items():
                if isinstance(data, (dict, list)):
                    zf.writestr(name, json.dumps(data, ensure_ascii=False))
                elif isinstance(data, str):
                    zf.writestr(name, data)
                elif isinstance(data, bytes):
                    zf.writestr(name, data)
        return bio.getvalue()

    def test_issue1_conflict_versions_preserved_in_excel_export(self):
        """[P1] 保留的冲突版本在 Excel 导出时不再被删除，且分配独立 Sheet 保留全量样本"""
        online = pd.DataFrame({'Sample': ['X', 'Y'], 'Ca': ['99', '100']})
        online.attrs.update({'label': 'Table 1', 'table_title': 'Table 1', 'page_idx': 0, 'source': 'online'})
        pdf = pd.DataFrame({'Sample': ['A', 'B'], 'Si': ['10', '20']})
        pdf.attrs.update({'label': 'Table 1', 'table_title': 'Table 1', 'page_idx': 0, 'source': 'pdf', 'extractor': 'pdf_table_extractor'})

        # 1. 验证上游合并正确保留双方
        merged_upstream = cli._merge_online_and_pdf_tables([online], [pdf])
        self.assertEqual(len(merged_upstream), 2, "上游合并应保留两份冲突版本")

        # 2. 验证 merge_continuation_tables 不再删除该冲突版本
        merged_post = tp.merge_continuation_tables(merged_upstream)
        self.assertEqual(len(merged_post), 2, "merge_continuation_tables 绝不可按同页同表号去重丢弃冲突数据")

        # 3. 验证实际保存至 Excel 工作簿
        with tempfile.TemporaryDirectory() as tmpdir:
            out_xlsx = os.path.join(tmpdir, "conflict_out.xlsx")
            ee.save_tables_to_excel(merged_upstream, out_xlsx, single_file=True)

            wb = load_workbook(out_xlsx, data_only=True)
            self.assertEqual(len(wb.sheetnames), 2, f"最终 Excel 必须包含 2 个工作表，实际为: {wb.sheetnames}")
            self.assertIn("Table 1", wb.sheetnames)
            # 第二个工作表应包含版本或来源后缀
            second_sheet = [s for s in wb.sheetnames if s != "Table 1"][0]
            self.assertTrue("(PDF)" in second_sheet or "(v2)" in second_sheet)

            # 核验数据保真度：X, Y 和 A, B 样本均毫发无损
            ws1 = wb["Table 1"]
            ws1_vals = [[str(v) for v in row if v is not None] for row in ws1.values]
            self.assertIn(["X", "99"], ws1_vals)
            self.assertIn(["Y", "100"], ws1_vals)

            ws2 = wb[second_sheet]
            ws2_vals = [[str(v) for v in row if v is not None] for row in ws2.values]
            self.assertIn(["A", "10"], ws2_vals)
            self.assertIn(["B", "20"], ws2_vals)
            wb.close()

    def test_issue2_contaminated_table_cannot_lose_valid_rows(self):
        """[P1] 污染表修复严禁丢掉一半有效数据：10 行有效数据 + 2 行投稿信息，OCR 只返回 5 行时必须拒绝替换"""
        clean = pd.DataFrame({
            'Sample': [f'S{i}' for i in range(10)],
            'Si': [str(13 + i) for i in range(10)],
            'Ca': [str(37 + i) for i in range(10)]
        })
        # 混入投稿信息垃圾行
        contaminated = pd.concat([
            clean,
            pd.DataFrame({'Sample': ['received: 2020', 'accepted: 2021'], 'Si': ['', ''], 'Ca': ['', '']})
        ], ignore_index=True)
        # OCR 只提取出前 5 行有效数据（丢弃了后 5 个样本）
        ocr_half = clean.iloc[:5].copy()

        contaminated.attrs.update({'label': 'Table 1', 'page_idx': 0})
        ocr_half.attrs.update({'label': 'Table 1', 'page_idx': 0})

        # 验证 _should_replace_with_ocr 拒绝这种丢失一半样本的 OCR
        should_replace = pe._should_replace_with_ocr({'df': contaminated, 'page_idx': 0}, {'df': ocr_half, 'page_idx': 0})
        self.assertFalse(should_replace, "截断丢失样本的 OCR 绝不能通过替换门禁")

        logs = []
        result = pe.reconcile_native_and_ocr_tables(
            [{'df': contaminated, 'page_idx': 0}],
            [{'df': ocr_half, 'page_idx': 0}],
            [0],
            logs
        )
        self.assertEqual(len(result), 1)
        res_df = result[0]['df']
        # 确认保留的是原生全量表（样本 S0~S9 全部保留）
        self.assertGreaterEqual(len(res_df), 10)
        for i in range(10):
            self.assertIn(f'S{i}', res_df['Sample'].values)

    def test_issue2_contaminated_table_accepted_when_ocr_preserves_all_rows(self):
        """[P1] 污染表修复在 OCR 完整保留全部有效数据行（10 行）时应被采纳并剔除污染"""
        clean = pd.DataFrame({
            'Sample': [f'S{i}' for i in range(10)],
            'Si': [str(13 + i) for i in range(10)],
            'Ca': [str(37 + i) for i in range(10)]
        })
        contaminated = pd.concat([
            clean,
            pd.DataFrame({'Sample': ['received: 2020', 'accepted: 2021'], 'Si': ['', ''], 'Ca': ['', '']})
        ], ignore_index=True)
        ocr_full = clean.copy()

        contaminated.attrs.update({'label': 'Table 1', 'page_idx': 0})
        ocr_full.attrs.update({'label': 'Table 1', 'page_idx': 0})

        should_replace = pe._should_replace_with_ocr({'df': contaminated, 'page_idx': 0}, {'df': ocr_full, 'page_idx': 0})
        self.assertTrue(should_replace, "OCR 完整保留 10 行并剥离污染行时应当被采纳替换")

    def test_issue3_distant_same_numbered_tables_export_without_overwrite(self):
        """[P1] 分文件导出远隔页面的同号独立表 (page distance >= 2) 必须导出为独立文件，杜绝覆盖"""
        t_p1 = pd.DataFrame({'Sample': ['A', 'B'], 'Value': ['13', '25']})
        t_p1.attrs.update({'label': 'Table 1', 'table_title': 'Table 1 Initial Data', 'page_idx': 0})

        t_p11 = pd.DataFrame({'Sample': ['C', 'D'], 'Value': ['31', '46']})
        t_p11.attrs.update({'label': 'Table 1', 'table_title': 'Table 1 End Data', 'page_idx': 10})

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = os.path.join(tmpdir, "distant_export")
            ee.save_tables_to_excel([t_p1, t_p11], out_dir, single_file=False)

            xlsx_files = sorted(Path(out_dir).glob("*.xlsx"))
            self.assertEqual(len(xlsx_files), 2, f"必须导出 2 个独立的 Excel 文件，实际为: {[f.name for f in xlsx_files]}")

            file_names = [f.name for f in xlsx_files]
            self.assertIn("Table 1.xlsx", file_names)
            self.assertTrue(any("Table 1_p11.xlsx" in fn or "Table 1_P11.xlsx" in fn for fn in file_names))

            # 验证各文件数据内容无覆盖
            wb1 = load_workbook(os.path.join(out_dir, "Table 1.xlsx"), data_only=True)
            v1 = [[str(x) for x in r if x is not None] for r in wb1.active.values]
            wb1.close()
            self.assertIn(["A", "13"], v1)
            self.assertIn(["B", "25"], v1)

            p11_file = [f for f in xlsx_files if f.name != "Table 1.xlsx"][0]
            wb2 = load_workbook(p11_file, data_only=True)
            v2 = [[str(x) for x in r if x is not None] for r in wb2.active.values]
            wb2.close()
            self.assertIn(["C", "31"], v2)
            self.assertIn(["D", "46"], v2)

            # 验证 table_captions.txt 中两表均有记录
            captions_file = os.path.join(out_dir, "table_captions.txt")
            self.assertTrue(os.path.exists(captions_file))
            with open(captions_file, "r", encoding="utf-8") as f_cap:
                cap_text = f_cap.read()
            self.assertIn("Table 1", cap_text)
            self.assertIn("p11", cap_text.lower())

    def test_issue4_mineru_archive_multi_json_representation_merge(self):
        """[P2] MinerU 结果包部分解析成功后，跨 candidate JSON 融合补齐单一文件遗漏的表格"""
        h1 = '<table><tr><th>Sample</th><th>Value</th></tr><tr><td>Alpha</td><td>13</td></tr><tr><td>Beta</td><td>25</td></tr></table>'
        h2 = '<table><tr><th>Item</th><th>Score</th></tr><tr><td>Gamma</td><td>31</td></tr><tr><td>Delta</td><td>46</td></tr></table>'

        # content_list.json 中 Table 2 损坏/空体
        v1 = [
            {'type': 'table', 'table_body': h1, 'table_caption': ['Table 1'], 'page_idx': 0},
            {'type': 'table', 'table_body': '', 'table_caption': ['Table 2'], 'page_idx': 1}
        ]
        # content_list_v2.json 中 Table 1 与 Table 2 均完整
        v2 = [
            [{'type': 'table', 'content': {'html': h1, 'table_caption': [{'type': 'text', 'content': 'Table 1'}]}}],
            [{'type': 'table', 'content': {'html': h2, 'table_caption': [{'type': 'text', 'content': 'Table 2'}]}}]
        ]

        archive_bytes = self._create_archive({
            'content_list.json': v1,
            'content_list_v2.json': v2
        })

        dfs = oc.extract_mineru_structured_archive(archive_bytes)
        self.assertEqual(len(dfs), 2, "应融合 content_list_v2 中的 Table 2，总计返回 2 张表")
        labels = [d.attrs.get('label') for d in dfs]
        self.assertEqual(labels, ['Table 1', 'Table 2'])
        self.assertEqual(len(dfs[0]), 2)
        self.assertEqual(len(dfs[1]), 2)

    def test_issue5_corrupt_or_empty_cache_does_not_block_primary_api(self):
        """[P2] 损坏或空缓存会安全失效，不阻断 MinerU 主抽取请求"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "test_doc.pdf")
            # 创建 dummy pdf
            try:
                import pymupdf as fitz
                doc = fitz.open()
                pg = doc.new_page()
                pg.insert_text((50, 50), "Table 1 Data")
                doc.save(pdf_path)
                doc.close()
            except Exception:
                with open(pdf_path, "wb") as f:
                    f.write(b"%PDF-1.4 dummy")

            cache_dir = os.path.join(tmpdir, "mineru_cache")
            os.makedirs(cache_dir, exist_ok=True)
            cache_file = os.path.join(cache_dir, "test_doc.zip")

            # 1. 测试损坏缓存
            with open(cache_file, "wb") as f:
                f.write(b"completely broken zip content")

            dummy_df = pd.DataFrame({'Sample': ['A'], 'Val': ['10']})
            dummy_df.attrs.update({'label': 'Table 1', 'page_idx': 0})

            with patch.object(oc, 'is_mineru_available', return_value=True), \
                 patch.object(oc, 'call_mineru_structured_api', return_value=[dummy_df]) as mock_api, \
                 patch.object(pe, 'classify_and_extract_via_inspector', return_value={'pdf_type': 'text_based', 'confidence': 1.0, 'pages_with_tables': [1], 'tables': []}), \
                 patch.object(tv, 'verify_table_numeric_integrity', return_value=(True, {'is_valid': True, 'match_ratio': 1.0, 'font_corrupted': False})):
                res, logs = pe._do_extract_tables_from_pdf(pdf_path)
                self.assertEqual(mock_api.call_count, 1, "损坏缓存必须自动失效并成功调用 MinerU 官方主抽取 API")
                self.assertFalse(os.path.exists(cache_file), "损坏的缓存文件必须被自动删除")

            # 2. 测试空缓存包 (包含有效 zip 但无表格)
            empty_zip_bytes = self._create_archive({'content_list.json': []})
            with open(cache_file, "wb") as f:
                f.write(empty_zip_bytes)

            with patch.object(oc, 'is_mineru_available', return_value=True), \
                 patch.object(oc, 'call_mineru_structured_api', return_value=[dummy_df]) as mock_api, \
                 patch.object(pe, 'classify_and_extract_via_inspector', return_value={'pdf_type': 'text_based', 'confidence': 1.0, 'pages_with_tables': [1], 'tables': []}), \
                 patch.object(tv, 'verify_table_numeric_integrity', return_value=(True, {'is_valid': True, 'match_ratio': 1.0, 'font_corrupted': False})):
                res, logs = pe._do_extract_tables_from_pdf(pdf_path)
                self.assertEqual(mock_api.call_count, 1, "空缓存必须自动失效并成功调用 MinerU 官方主抽取 API")
                self.assertFalse(os.path.exists(cache_file), "空的缓存文件必须被自动删除")

    def test_issue6_end_to_end_extraction_merging_and_excel_verification(self):
        """[P1] 端到端全链路深度核验：从解析、合并到实际 Excel Workbook 导出，核验 0 丢失样本与数值精准"""
        # 构造包含复杂场景的表格集合：
        # 1. 在线提取表 1 (Sample X, Y)
        t_online = pd.DataFrame({'Sample ID': ['SMP-01', 'SMP-02'], 'Ca (ppm)': ['12.5', '14.8']})
        t_online.attrs.update({'label': 'Table 1', 'table_title': 'Table 1 Main Elements', 'page_idx': 0, 'source': 'online'})

        # 2. 本地 PDF 冲突表 1 (Sample A, B, 无法对齐，必须作为独立版本保留)
        t_pdf_conflict = pd.DataFrame({'Sample ID': ['SMP-03', 'SMP-04'], 'Si (%)': ['45.2', '48.1']})
        t_pdf_conflict.attrs.update({'label': 'Table 1', 'table_title': 'Table 1 Main Elements', 'page_idx': 0, 'source': 'pdf', 'extractor': 'mineru_api'})

        # 3. 第 5 页受污染的原生表 (10 行样本 + 2 行期刊元数据，OCR 试图截断为 5 行被保护)
        clean_rows = [{'Sample': f'NORM-{i:02d}', 'Value': str(100 + i)} for i in range(10)]
        t_nat_polluted = pd.concat([
            pd.DataFrame(clean_rows),
            pd.DataFrame([{'Sample': 'Manuscript received: 2021', 'Value': ''}, {'Sample': 'Accepted for publication', 'Value': ''}])
        ], ignore_index=True)
        t_nat_polluted.attrs.update({'label': 'Table 2', 'table_title': 'Table 2 Complete Normal Standards', 'page_idx': 4})

        ocr_truncated = pd.DataFrame(clean_rows[:5])
        ocr_truncated.attrs.update({'label': 'Table 2', 'table_title': 'Table 2 Complete Normal Standards', 'page_idx': 4})

        # 4. 第 15 页同名远隔独立表 (Table 1, distant independent table)
        t_distant = pd.DataFrame({'Sample ID': ['DIST-A', 'DIST-B'], 'Loss on Ignition': ['1.2%', '1.5%']})
        t_distant.attrs.update({'label': 'Table 1', 'table_title': 'Table 1 Loss on Ignition (Distant)', 'page_idx': 14})

        # 阶段 1: 在线与本地表合并
        merged_p1 = cli._merge_online_and_pdf_tables([t_online], [t_pdf_conflict])
        self.assertEqual(len(merged_p1), 2, "冲突表版本必须被保留")

        # 阶段 2: 污染表对齐保护
        reconciled_p2 = pe.reconcile_native_and_ocr_tables(
            [{'df': t_nat_polluted, 'page_idx': 4}],
            [{'df': ocr_truncated, 'page_idx': 4}],
            [4]
        )
        self.assertEqual(len(reconciled_p2), 1)
        # 确认 10 行有效记录都在
        self.assertGreaterEqual(len(reconciled_p2[0]['df']), 10)

        # 阶段 3: 汇集全文献全部表格
        all_tables = merged_p1 + [reconciled_p2[0]['df'], t_distant]
        self.assertEqual(len(all_tables), 4, "全链路输入应为 4 张独立逻辑表格")

        # 阶段 4: 保存至 Excel 单工作簿
        with tempfile.TemporaryDirectory() as tmpdir:
            out_workbook = os.path.join(tmpdir, "comprehensive_out.xlsx")
            success = ee.save_tables_to_excel(all_tables, out_workbook, single_file=True)
            self.assertTrue(success)

            # 阶段 5: 逐单元格核验 Excel 实体文件
            wb = load_workbook(out_workbook, data_only=True)
            sheet_names = wb.sheetnames
            # 应存在 4 个独立工作表：Table 1, Table 1 (PDF)/Table 1 (v2), Table 2, Table 1_p15
            self.assertEqual(len(sheet_names), 4, f"工作表数量应严格为 4，实际为: {sheet_names}")
            self.assertIn("Table 1", sheet_names)
            self.assertIn("Table 2", sheet_names)
            self.assertTrue(any("Table 1_p15" in s or "Table 1_P15" in s for s in sheet_names), f"远隔表应带页码后缀，实际 sheets: {sheet_names}")

            # 核验各工作表数据无缝无损：
            # 1. Table 1: SMP-01, SMP-02
            ws_t1 = wb["Table 1"]
            t1_vals = [[str(c) for c in row if c is not None] for row in ws_t1.values]
            self.assertIn(["SMP-01", "12.5"], t1_vals)
            self.assertIn(["SMP-02", "14.8"], t1_vals)

            # 2. Table 1 (PDF/v2): SMP-03, SMP-04
            pdf_sheet_name = [s for s in sheet_names if s.startswith("Table 1") and s not in ("Table 1", "Table 1_p15", "Table 1_P15")][0]
            ws_pdf = wb[pdf_sheet_name]
            pdf_vals = [[str(c) for c in row if c is not None] for row in ws_pdf.values]
            self.assertIn(["SMP-03", "45.2"], pdf_vals)
            self.assertIn(["SMP-04", "48.1"], pdf_vals)

            # 3. Table 2: NORM-00 ~ NORM-09 (10 个完整样本 0 遗漏)
            ws_t2 = wb["Table 2"]
            t2_vals = [[str(c) for c in row if c is not None] for row in ws_t2.values]
            for i in range(10):
                self.assertTrue(any(f"NORM-{i:02d}" in row for row in t2_vals), f"NORM-{i:02d} 必须存在于 Table 2")

            # 4. Table 1_p15: DIST-A, DIST-B
            distant_sheet_name = [s for s in sheet_names if "p15" in s.lower()][0]
            ws_dist = wb[distant_sheet_name]
            dist_vals = [[str(c) for c in row if c is not None] for row in ws_dist.values]
            self.assertIn(["DIST-A", "1.2%"], dist_vals)
            self.assertIn(["DIST-B", "1.5%"], dist_vals)

            wb.close()

    def test_edge_online_table_without_page_idx_preserved(self):
        """[Edge Case] 在线表格 page_idx 为 None 时，与本地 PDF 表格绝不可在 merge_continuation_tables 中被错误拼合为续表"""
        online = pd.DataFrame({'Sample': ['X', 'Y'], 'Ca': ['99', '100']})
        online.attrs.update({'label': 'Table 1', 'source': 'online'})  # page_idx is None
        pdf = pd.DataFrame({'Sample': ['A', 'B'], 'Si': ['10', '20']})
        pdf.attrs.update({'label': 'Table 1', 'source': 'pdf', 'page_idx': 0})

        merged_post = tp.merge_continuation_tables([online, pdf])
        self.assertEqual(len(merged_post), 2, "缺失 page_idx 的在线表与本地 PDF 表不可合并为单张续表")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_xlsx = os.path.join(tmpdir, "edge_online_none_page.xlsx")
            ee.save_tables_to_excel([online, pdf], out_xlsx, single_file=True)
            wb = load_workbook(out_xlsx, data_only=True)
            self.assertEqual(len(wb.sheetnames), 2)
            self.assertIn("Table 1", wb.sheetnames)
            second = [s for s in wb.sheetnames if s != "Table 1"][0]
            self.assertTrue("(PDF)" in second or "(v2)" in second)
            wb.close()

    def test_edge_distant_pdf_tables_single_file_named_with_page(self):
        """[Edge Case] 两个均来自本地 PDF 的 Table 1 (分别在第 1 页和第 11 页)，在单文件导出时工作表应命名为 Table 1 和 Table 1_p11，绝非 Table 1 (PDF)"""
        t1 = pd.DataFrame({'Sample': ['A', 'B'], 'Val': ['1', '2']})
        t1.attrs.update({'label': 'Table 1', 'page_idx': 0, 'source': 'pdf', 'extractor': 'pdf_table_extractor'})

        t2 = pd.DataFrame({'Sample': ['C', 'D'], 'Val': ['3', '4']})
        t2.attrs.update({'label': 'Table 1', 'page_idx': 10, 'source': 'pdf', 'extractor': 'pdf_table_extractor'})

        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, 'distant_pdf.xlsx')
            ee.save_tables_to_excel([t1, t2], out, single_file=True)
            wb = load_workbook(out)
            self.assertEqual(wb.sheetnames, ['Table 1', 'Table 1_p11'])
            wb.close()

    def test_edge_distant_table_on_disk_overlap_not_overwritten(self):
        """[Edge Case] 目录导出模式下，当既有 Table 1.xlsx 已在磁盘（现有页码未保存在内存），后方页面 (第4页, curr_p=3) 且列名重合的 Table 1 绝不可覆盖 Table 1.xlsx"""
        t_p1 = pd.DataFrame({'Sample': ['A', 'B'], 'Value': ['13', '25']})
        t_p1.attrs.update({'label': 'Table 1', 'table_title': 'Table 1 Initial Data', 'page_idx': 0})

        t_p4 = pd.DataFrame({'Sample': ['A', 'C'], 'Value': ['13', '46']})
        t_p4.attrs.update({'label': 'Table 1', 'table_title': 'Table 1 Later Data', 'page_idx': 3})

        with tempfile.TemporaryDirectory() as tmpdir:
            ee.save_tables_to_excel([t_p1], tmpdir, single_file=False)
            ee.save_tables_to_excel([t_p4], tmpdir, single_file=False)
            files = sorted(f.name for f in Path(tmpdir).glob('*.xlsx'))
            self.assertEqual(len(files), 2, f"必须保留两个文件，实际为: {files}")
            self.assertIn("Table 1.xlsx", files)
            self.assertIn("Table 1_p4.xlsx", files)

    def test_edge_low_quality_contaminated_rejects_truncated_ocr(self):
        """[Edge Case] 当剔除污染后的表格因长文本列被判定为低质量时，OCR 截断丢失一半行记录依然必须被严格拒绝"""
        clean = pd.DataFrame({
            'Sample': [f'S{i}' for i in range(10)],
            'Desc': ['A very long descriptive note detailing the procedure and experimental setup ' * 3 for _ in range(10)],
            'Val': [str(10 + i) for i in range(10)]
        })
        contaminated = pd.concat([
            clean,
            pd.DataFrame({'Sample': ['received: 2020'], 'Desc': [''], 'Val': ['']})
        ], ignore_index=True)
        ocr_half = clean.iloc[:5].copy()

        contaminated.attrs.update({'label': 'Table 1', 'page_idx': 0})
        ocr_half.attrs.update({'label': 'Table 1', 'page_idx': 0})

        should_replace = pe._should_replace_with_ocr({'df': contaminated, 'page_idx': 0}, {'df': ocr_half, 'page_idx': 0})
        self.assertFalse(should_replace, "即便原生表含有长文本列，截断有效记录的 OCR 依然必须被拒绝")


if __name__ == '__main__':
    unittest.main()
