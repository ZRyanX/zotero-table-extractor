#!/usr/bin/env python3
"""
tests/test_audit_oct18_fixes.py — 回归测试套件，全面覆盖 2026-10-18 审计发现的 4 项边界问题：
1. [P1] extract_zotero_table.py: 补列行身份严格匹配与键归一化（禁止按行位置盲目补列，对齐浮点与整型键，杜绝数值颠倒分配）。
2. [P1] pdf_table_extractor.py: OCR 替换门禁（严禁少行少列 OCR 替换非退化原生表，保护已有 10x4 不被 7x4 或 10x3 降级）。
3. [P1] extract_zotero_table.py: 在线独有列保护（PDF 列数达 2 倍时不跳过补列，严防在线独有列如 Ca 丢失）。
4. [P2] table_postprocess.py: 描述性续表标题保护（如 Chemical composition (continued)、续表：化学成分 不误当作冲突表号）。
"""

import os
import sys
import unittest
from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import extract_zotero_table as cli
from scripts import pdf_table_extractor as pe
from scripts import table_postprocess as tp


class TestAuditOct18Fixes(unittest.TestCase):
    def setUp(self):
        os.environ['MINERU_TABLE_MERGE_ENABLE'] = 'true'

    # ──────────────────────────────────────────────────────────────────────────
    # 1. [P1] 补列行身份匹配与键归一化
    # ──────────────────────────────────────────────────────────────────────────
    def test_complementary_merge_sample_alignment_with_float_and_int(self):
        """测试在线表与 PDF 表行顺序不同、且键存在浮点数与整型差异 (25.0 vs 25) 时的精确对齐，杜绝错配"""
        pdf_df = pd.DataFrame({
            'Si': ['13', '25', '31'],
            'Sample': ['A', 'B', 'C']
        })
        pdf_df.attrs.update(label='Table 1', page_idx=0)

        # 在线表逆序排放，Si 为 25.0 与 13.0
        online_df = pd.DataFrame({
            'Si': ['25.0', '13.0'],
            'Sample': ['B', 'A'],
            'Ca': ['8', '7']
        })
        online_df.attrs.update(label='Table 1', page_idx=0)

        merged_list = cli._merge_online_and_pdf_tables([online_df], [pdf_df])
        self.assertEqual(len(merged_list), 1)
        res = merged_list[0]

        # 验证行顺序保持 PDF 主干 (A, B, C)
        self.assertEqual(list(res['Sample']), ['A', 'B', 'C'])
        # 验证 Ca 字段精准对应 Sample A 为 7，Sample B 为 8，Sample C 为空
        self.assertEqual(res.loc[res['Sample'] == 'A', 'Ca'].values[0], '7')
        self.assertEqual(res.loc[res['Sample'] == 'B', 'Ca'].values[0], '8')
        self.assertEqual(res.loc[res['Sample'] == 'C', 'Ca'].values[0], '')

    def test_complementary_merge_cannot_verify_row_identities_returns_none(self):
        """测试共同字段匹配失败时返回 None，绝不按物理位置盲目补列错配数据"""
        prim = pd.DataFrame({
            'Sample': ['A', 'B'],
            'Val': ['1', '2']
        })
        sec = pd.DataFrame({
            'Sample': ['X', 'Y'],  # 键值完全无交集
            'Extra': ['100', '200']
        })
        merged = cli._merge_complementary_tables(prim, sec)
        self.assertIsNone(merged, "无法置信确认行身份时，必须返回 None 而非盲目位置追加")

    def test_normalize_merge_key_preserves_leading_zero_codes(self):
        """测试标准化合并键保护前导零整数编码 (01, 007) 不被错误塌陷为整数 1, 7"""
        self.assertEqual(cli._normalize_merge_key("01"), "01")
        self.assertEqual(cli._normalize_merge_key("007"), "007")
        self.assertEqual(cli._normalize_merge_key("1"), "1")
        self.assertEqual(cli._normalize_merge_key("25.0"), "25")
        self.assertEqual(cli._normalize_merge_key("13.00"), "13")
        self.assertEqual(cli._normalize_merge_key("0"), "0")
        self.assertEqual(cli._normalize_merge_key("0.0"), "0")

    def test_complementary_merge_non_identifier_column_value_coincidence_returns_none(self):
        """测试不同样本名在非标识测量列 (Si=13) 存在巧合重合时，坚决不误跨样本错配数据"""
        prim = pd.DataFrame({
            'Sample': ['A', 'B'],
            'Si': ['13', '13']
        })
        sec = pd.DataFrame({
            'Sample': ['X', 'Y'],
            'Si': ['13', '99'],
            'Extra': ['WRONG_DATA', 'OTHER']
        })
        merged = cli._merge_complementary_tables(prim, sec)
        self.assertIsNone(merged, "样本标识列不匹配时，绝不能仅凭测量列 (Si) 巧合相同而把 WRONG_DATA 分配给 Sample A/B")

    def test_complementary_merge_preserves_both_when_unmatched(self):
        """在 _merge_online_and_pdf_tables 中，若行身份无法置信匹配，独立保留冲突双方数据"""
        online_df = pd.DataFrame({
            'Sample': ['X', 'Y'],
            'Ca': ['99', '100']
        })
        online_df.attrs.update(label='Table 1', page_idx=0)

        pdf_df = pd.DataFrame({
            'Sample': ['A', 'B'],
            'Si': ['10', '20']
        })
        pdf_df.attrs.update(label='Table 1', page_idx=0)

        res = cli._merge_online_and_pdf_tables([online_df], [pdf_df])
        # 双方均保留，不发生错误覆盖或错误赋值
        self.assertEqual(len(res), 2)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. [P1] OCR 替换门禁
    # ──────────────────────────────────────────────────────────────────────────
    def test_ocr_cannot_replace_native_when_fewer_rows(self):
        """测试 10x4 的原生表绝不能被 7x4 的 OCR 表替换"""
        nat_df = pd.DataFrame({
            'Sample': [f'S{i}' for i in range(10)],
            'Si': [str(10 + i) for i in range(10)],
            'Ca': [str(30 + i) for i in range(10)],
            'Mg': [str(50 + i) for i in range(10)]
        })
        nat_df.attrs.update(label='Table 1', page_idx=0)

        ocr_df = nat_df.iloc[:7].copy()
        ocr_df.attrs.update(label='Table 1', page_idx=0)

        self.assertFalse(pe._should_replace_with_ocr({'df': nat_df}, {'df': ocr_df}))

        logs = []
        reconciled = pe.reconcile_native_and_ocr_tables(
            [{'df': nat_df, 'page_idx': 0}],
            [{'df': ocr_df, 'page_idx': 0}],
            [0],
            logs
        )
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]['df'].shape, (10, 4))
        self.assertTrue(any('保留原生版本' in l for l in logs))

    def test_ocr_cannot_replace_native_when_fewer_columns(self):
        """测试 10x4 的原生表绝不能被 10x3 的少列 OCR 表替换"""
        nat_df = pd.DataFrame({
            'Sample': [f'S{i}' for i in range(10)],
            'Si': [str(10 + i) for i in range(10)],
            'Ca': [str(30 + i) for i in range(10)],
            'Mg': [str(50 + i) for i in range(10)]
        })
        nat_df.attrs.update(label='Table 1', page_idx=0)

        ocr_df = nat_df.drop(columns=['Mg']).copy()
        ocr_df.attrs.update(label='Table 1', page_idx=0)

        self.assertFalse(pe._should_replace_with_ocr({'df': nat_df}, {'df': ocr_df}))

        logs = []
        reconciled = pe.reconcile_native_and_ocr_tables(
            [{'df': nat_df, 'page_idx': 0}],
            [{'df': ocr_df, 'page_idx': 0}],
            [0],
            logs
        )
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]['df'].shape, (10, 4))
        self.assertIn('Mg', reconciled[0]['df'].columns)

    def test_ocr_can_replace_degenerate_native(self):
        """原生表若退化为空表或单单元格残表，允许合格 OCR 替换"""
        degen_df = pd.DataFrame([['SingleCell']])
        degen_df.attrs.update(label='Table 1', page_idx=0)

        ocr_df = pd.DataFrame({
            'Col1': ['A', 'B'],
            'Col2': ['1', '2']
        })
        ocr_df.attrs.update(label='Table 1', page_idx=0)

        self.assertTrue(pe._should_replace_with_ocr({'df': degen_df}, {'df': ocr_df}))

    def test_ocr_replaces_identical_valid_table(self):
        """行列完整且数据完整的等大合格 OCR 表允许替换"""
        nat_df = pd.DataFrame({
            'Col1': ['A', 'B'],
            'Col2': ['1', '2']
        })
        nat_df.attrs.update(label='Table 1', page_idx=0)

        ocr_df = nat_df.copy()
        ocr_df.attrs.update(label='Table 1', page_idx=0)

        self.assertTrue(pe._should_replace_with_ocr({'df': nat_df}, {'df': ocr_df}))

    # ──────────────────────────────────────────────────────────────────────────
    # 3. [P1] PDF 列数达 2 倍时不丢失在线独有列
    # ──────────────────────────────────────────────────────────────────────────
    def test_wider_pdf_preserves_unique_online_columns(self):
        """测试 PDF 列数达在线表 2 倍时，在线独有字段 (Ca) 绝不被丢弃"""
        online_df = pd.DataFrame({
            'Si': ['25.0', '13.0'],
            'Sample': ['B', 'A'],
            'Ca': ['8', '7']
        })
        online_df.attrs.update(label='Table 1', page_idx=0)

        # PDF 包含 6 列 (在线的 2 倍)，但缺少 Ca 列
        pdf_df = pd.DataFrame({
            'Sample': ['A', 'B', 'C'],
            'Si': ['13', '25', '31'],
            'Mg': ['55', '56', '57'],
            'Fe': ['70', '71', '72'],
            'Al': ['80', '81', '82'],
            'K': ['90', '91', '92']
        })
        pdf_df.attrs.update(label='Table 1', page_idx=0)

        merged_list = cli._merge_online_and_pdf_tables([online_df], [pdf_df])
        self.assertEqual(len(merged_list), 1)
        res = merged_list[0]

        # 必须保留 PDF 的全量列并补充在线独有的 Ca 列，共 7 列
        self.assertIn('Ca', res.columns)
        self.assertEqual(len(res.columns), 7)
        self.assertEqual(len(res), 3)

        # 且 Ca 的数值正确对应 Sample A 为 7，Sample B 为 8
        self.assertEqual(res.loc[res['Sample'] == 'A', 'Ca'].values[0], '7')
        self.assertEqual(res.loc[res['Sample'] == 'B', 'Ca'].values[0], '8')

    # ──────────────────────────────────────────────────────────────────────────
    # 4. [P2] 描述性续表标题保护
    # ──────────────────────────────────────────────────────────────────────────
    def test_continuation_with_descriptive_titles(self):
        """测试包含描述性文字的续表标题（如 Chemical composition (continued)、续表：化学成分）被正常合并为续表"""
        titles_to_test = [
            '(continued)',
            'Chemical composition (continued)',
            '续表',
            '续表：化学成分',
            'Table 1 (continued)'
        ]

        for title in titles_to_test:
            with self.subTest(title=title):
                df_a = pd.DataFrame({
                    'Sample': ['A', 'B'],
                    'Si': ['13', '25'],
                    'Ca': ['7', '8']
                })
                df_a.attrs.update(
                    label='Table 1',
                    table_title='Table 1 Chemical composition',
                    page_idx=0
                )

                df_b = pd.DataFrame({
                    'Sample': ['C', 'D'],
                    'Si': ['31', '42'],
                    'Ca': ['9', '10']
                })
                df_b.attrs.update(
                    table_title=title,
                    page_idx=1,
                    is_continuation=True
                )

                res = tp.merge_continuation_tables([df_a, df_b])
                self.assertEqual(
                    len(res), 1,
                    f"续表标题 [{title}] 应与前表成功合并为 1 张表，实际产生了 {len(res)} 张表"
                )
                self.assertEqual(len(res[0]), 4)
                self.assertEqual(res[0].attrs.get('covered_pages'), [0, 1])

    def test_explicit_conflicting_table_numbers_not_merged(self):
        """明确的不同表号（如 Table 1 与 Table 2）即使同页相邻，坚决独立，杜绝误合并"""
        df_1 = pd.DataFrame({'Sample': ['A', 'B'], 'Si': ['13', '25']})
        df_1.attrs.update(label='Table 1', table_title='Table 1 Composition', page_idx=0)

        df_2 = pd.DataFrame({'Sample': ['C', 'D'], 'Si': ['31', '42']})
        df_2.attrs.update(label='Table 2', table_title='Table 2 Parameters', page_idx=1)

        res = tp.merge_continuation_tables([df_1, df_2])
        self.assertEqual(len(res), 2, "明确不同的表号必须独立保留")

    def test_conflicting_table_number_in_continuation_title_not_merged(self):
        """前表为 Table 1，后表虽带有续表字样但明确声明为 Table 2 (continued)，绝不与 Table 1 合并"""
        df_1 = pd.DataFrame({'Sample': ['A', 'B'], 'Si': ['13', '25']})
        df_1.attrs.update(label='Table 1', table_title='Table 1 Composition', page_idx=0)

        df_2 = pd.DataFrame({'Sample': ['C', 'D'], 'Si': ['31', '42']})
        df_2.attrs.update(label='Table 2', table_title='Table 2 (continued)', page_idx=1, is_continuation=True)

        res = tp.merge_continuation_tables([df_1, df_2])
        self.assertEqual(len(res), 2, "表号显式冲突的续表绝不能合并入前一表号")

    def test_continuation_merges_when_df1_only_has_table_title_without_label_attr(self):
        """测试主表 df1 仅设置了 table_title（如 'Table 1 Chemical composition'）而缺失 attrs['label'] 时，仍可与续表合并"""
        df1 = pd.DataFrame({'Sample': ['A', 'B'], 'Si': ['13', '25']})
        df1.attrs.update(table_title='Table 1 Chemical composition', page_idx=0)

        df2 = pd.DataFrame({'Sample': ['C', 'D'], 'Si': ['31', '42']})
        df2.attrs.update(table_title='Chemical composition (continued)', page_idx=1, is_continuation=True)

        res = tp.merge_continuation_tables([df1, df2])
        self.assertEqual(len(res), 1, "即便主表未显式挂载 label 属性，也应从 table_title 提取表号并成功合并续表")
        self.assertEqual(len(res[0]), 4)
        self.assertEqual(res[0].attrs.get('covered_pages'), [0, 1])
        self.assertEqual(res[0].attrs.get('label'), 'Table 1')

    # ──────────────────────────────────────────────────────────────────────────
    # 5. [P1 & P2] 2026-10-18 边界问题回归测试
    # ──────────────────────────────────────────────────────────────────────────
    def test_complementary_merge_preserves_online_unique_rows(self):
        """[Issue 1] 补列时保留双方全量行：在线独有行或未匹配行决不被丢弃"""
        # Case 1: 在线表行数更多 (A, B, C, D vs A, B)
        online_df = pd.DataFrame({'Sample': ['A', 'B', 'C', 'D'], 'Ca': ['7', '8', '9', '10']})
        online_df.attrs.update(label='Table 1', page_idx=0)
        pdf_df = pd.DataFrame({'Sample': ['A', 'B'], 'Si': ['13', '25']})
        pdf_df.attrs.update(label='Table 1', page_idx=0)

        merged_list = cli._merge_online_and_pdf_tables([online_df], [pdf_df])
        self.assertEqual(len(merged_list), 1)
        res = merged_list[0]
        self.assertEqual(len(res), 4, "在线表独有的样本 C、D 必须全量保留")
        self.assertEqual(list(res['Sample']), ['A', 'B', 'C', 'D'])
        self.assertEqual(res.loc[res['Sample'] == 'C', 'Ca'].values[0], '9')
        self.assertEqual(res.loc[res['Sample'] == 'D', 'Ca'].values[0], '10')

        # Case 2: 部分交集且 PDF 行数更多 (在线 A, B, X vs PDF A, B, C, D)
        online_df2 = pd.DataFrame({'Sample': ['A', 'B', 'X'], 'Ca': ['7', '8', '99']})
        online_df2.attrs.update(label='Table 1', page_idx=0)
        pdf_df2 = pd.DataFrame({'Sample': ['A', 'B', 'C', 'D'], 'Si': ['13', '25', '31', '40']})
        pdf_df2.attrs.update(label='Table 1', page_idx=0)

        merged_list2 = cli._merge_online_and_pdf_tables([online_df2], [pdf_df2])
        self.assertEqual(len(merged_list2), 1)
        res2 = merged_list2[0]
        self.assertEqual(len(res2), 5, "双方未匹配行 (PDF 的 C/D 与在线的 X) 必须全部保留")
        samples = list(res2['Sample'])
        self.assertIn('X', samples)
        self.assertEqual(res2.loc[res2['Sample'] == 'X', 'Ca'].values[0], '99')

    def test_composite_key_duplicate_with_conflicting_data_refuses_merge(self):
        """[Issue 2] 复合键去重必须进行冲突检测：重复键若存在冲突数值，严禁覆盖数值，应独立保留版本"""
        online_df = pd.DataFrame({
            'Sample': ['A', 'A'],
            'Si': ['13', '13'],
            'Ca': ['7', '8']  # 冲突值
        })
        online_df.attrs.update(label='Table 1', page_idx=0)
        pdf_df = pd.DataFrame({
            'Sample': ['A', 'A'],
            'Si': ['13', '13']
        })
        pdf_df.attrs.update(label='Table 1', page_idx=0)

        # 直接合并互补表应返回 None
        merged = cli._merge_complementary_tables(pdf_df, online_df)
        self.assertIsNone(merged, "复合键重复且数据存在冲突时，必须拒绝合并")

        # 入口层应独立保留双方版本
        combined = cli._merge_online_and_pdf_tables([online_df], [pdf_df])
        self.assertEqual(len(combined), 2, "发生数据冲突时应独立保留双方表格")

    def test_normalize_merge_key_lossless_precision(self):
        """[Issue 3] 匹配键归一化无损精度：大整数与高精度浮点不碰撞、不塌陷"""
        k1 = cli._normalize_merge_key('1.234567')
        k2 = cli._normalize_merge_key('1.234568')
        self.assertNotEqual(k1, k2, "高精度浮点数不得因为 :g 格式化而发生碰撞")
        self.assertEqual(k1, '1.234567')
        self.assertEqual(k2, '1.234568')

        k_id1 = cli._normalize_merge_key('9007199254740992')
        k_id2 = cli._normalize_merge_key('9007199254740993')
        self.assertNotEqual(k_id1, k_id2, "大整数样本编号不得因 float() 精度塌陷而发生碰撞")
        self.assertEqual(k_id1, '9007199254740992')
        self.assertEqual(k_id2, '9007199254740993')

        # 验证不同样本 ID 绝不被误合并
        online_df = pd.DataFrame({'Sample': ['9007199254740993'], 'Ca': ['99']})
        online_df.attrs.update(label='Table 1', page_idx=0)
        pdf_df = pd.DataFrame({'Sample': ['9007199254740992'], 'Si': ['13']})
        pdf_df.attrs.update(label='Table 1', page_idx=0)

        res = cli._merge_online_and_pdf_tables([online_df], [pdf_df])
        self.assertEqual(len(res), 2, "大整数编号不同的样本绝不能被误合并")

    def test_ocr_replaces_contaminated_native_table(self):
        """[Issue 4] 原生表存在投稿信息/元数据污染或重复表头时，允许合格 OCR 替换修复"""
        # Case 1: 投稿信息污染 (received/accepted)
        native_polluted = pd.DataFrame({
            'Sample': ['A', 'received: 2020', 'accepted: 2021', 'B'],
            'Si': ['13', '', '', '25'],
            'Ca': ['7', '', '', '8']
        })
        ocr_clean = pd.DataFrame({
            'Sample': ['A', 'B'],
            'Si': ['13', '25'],
            'Ca': ['7', '8']
        })
        self.assertTrue(pe._should_replace_with_ocr({'df': native_polluted}, {'df': ocr_clean}))

        logs = []
        r = pe.reconcile_native_and_ocr_tables(
            [{'df': native_polluted, 'page_idx': 0, 'label': 'Table 1'}],
            [{'df': ocr_clean, 'page_idx': 0, 'label': 'Table 1'}],
            [0],
            logs
        )
        self.assertEqual(len(r), 1)
        self.assertEqual(len(r[0]['df']), 2, "污染行被剥离，保留清洁的 2 行数据")
        self.assertTrue(any('精准替换' in l for l in logs))

        # Case 2: 表格内部重复打印表头
        native_header = pd.DataFrame({
            'Sample': ['A', 'Sample', 'B'],
            'Si': ['13', 'Si', '25'],
            'Ca': ['7', 'Ca', '8']
        })
        self.assertTrue(pe._should_replace_with_ocr({'df': native_header}, {'df': ocr_clean}))

    def test_complementary_merge_with_string_index(self):
        """测试包含自定义字符串索引的 DataFrame 互补合并时正常对齐，绝不抛出 ValueError"""
        prim = pd.DataFrame({'Sample': ['A', 'B'], 'Si': ['13', '25']}, index=['r1', 'r2'])
        sec = pd.DataFrame({'Sample': ['A', 'B'], 'Ca': ['7', '8']}, index=['s1', 's2'])
        res = cli._merge_complementary_tables(prim, sec)
        self.assertIsNotNone(res, "字符串索引应安全重置并成功合并")
        self.assertEqual(len(res), 2)
        self.assertEqual(res.loc[res['Sample'] == 'A', 'Ca'].values[0], '7')

    def test_complementary_merge_preserves_unmatched_rows_with_empty_keys(self):
        """测试次表即使包含空键行（如汇总行或未标注样本行），互补合并时也全量保留，杜绝丢行"""
        prim = pd.DataFrame({'Sample': ['A', 'B'], 'Si': ['13', '25']})
        sec = pd.DataFrame({'Sample': ['A', 'B', ''], 'Ca': ['7', '8', '100']})
        res = cli._merge_complementary_tables(prim, sec)
        self.assertIsNotNone(res)
        self.assertEqual(len(res), 3, "空键行必须在未匹配行中全量保留")
        self.assertEqual(res.loc[res['Sample'] == '', 'Ca'].values[0], '100')

    def test_merge_online_and_pdf_same_columns_online_unique_sample(self):
        """测试列结构完全相同时，在线表独有的样本行 (X) 绝不因 PDF 行数更多而被直接覆盖冲刷丢弃"""
        online_df = pd.DataFrame({'Sample': ['A', 'B', 'X'], 'Si': ['13', '25', '99']})
        online_df.attrs.update(label='Table 1', page_idx=0)
        pdf_df = pd.DataFrame({'Sample': ['A', 'B', 'C', 'D'], 'Si': ['13', '25', '31', '40']})
        pdf_df.attrs.update(label='Table 1', page_idx=0)

        res = cli._merge_online_and_pdf_tables([online_df], [pdf_df])
        self.assertEqual(len(res), 1)
        merged = res[0]
        self.assertEqual(len(merged), 5, "应保留双方并集全量行 (A, B, C, D, X)")
        self.assertIn('X', list(merged['Sample']))
        self.assertEqual(merged.loc[merged['Sample'] == 'X', 'Si'].values[0], '99')

    def test_normalize_merge_key_signed_codes_and_numbers(self):
        """测试键归一化对带正负号样号与编码的保护及科学计数法对齐"""
        self.assertEqual(cli._normalize_merge_key("+007"), "007")
        self.assertEqual(cli._normalize_merge_key("-007"), "-007")
        self.assertEqual(cli._normalize_merge_key("+123"), "123")
        self.assertEqual(cli._normalize_merge_key("-123"), "-123")
        self.assertEqual(cli._normalize_merge_key("1.5e-3"), "0.0015")
        self.assertEqual(cli._normalize_merge_key("0.0015"), "0.0015")

    def test_ocr_clean_n_handles_non_default_index(self):
        """测试切片或非默认索引的原生表在污染识别与 OCR 替换门禁中不抛 KeyError"""
        df_polluted = pd.DataFrame({
            'Sample': ['A', 'received: 2020', 'B'],
            'Si': ['13', '', '25'],
            'Ca': ['7', '', '8']
        }, index=[10, 20, 30])
        ocr_clean = pd.DataFrame({
            'Sample': ['A', 'B'],
            'Si': ['13', '25'],
            'Ca': ['7', '8']
        })
        self.assertTrue(pe._should_replace_with_ocr({'df': df_polluted}, {'df': ocr_clean}))

    def test_ocr_cannot_replace_contaminated_native_when_ocr_drops_columns(self):
        """测试原生表受行污染时，少列的 OCR 版本（即使行数清洁）也绝不能替换原生表造成字段遗失"""
        df_polluted = pd.DataFrame({
            'Sample': ['A', 'received: 2020', 'B'],
            'Si': ['13', '', '25'],
            'Ca': ['7', '', '8'],
            'Mg': ['5', '', '6']
        })
        # OCR 剥离了污染行但遗漏了 Mg 列
        ocr_missing_col = pd.DataFrame({
            'Sample': ['A', 'B'],
            'Si': ['13', '25'],
            'Ca': ['7', '8']
        })
        self.assertFalse(pe._should_replace_with_ocr({'df': df_polluted}, {'df': ocr_missing_col}))


if __name__ == '__main__':
    unittest.main()

