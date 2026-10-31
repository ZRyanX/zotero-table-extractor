#!/usr/bin/env python3
"""
tests/test_audit_oct19_fixes.py — 针对 10月19日 确认的 5 个关键缺陷的专项回归测试：
1. [P1] 新表头推断会吞掉真实数据，单行表整表消失、科学计数法与年份表头误判
2. [P1] 官方 CLI 的输出文件名 (如 coordinate.json / 论文名.json) 坐标单位判断 (norm_1000 vs points)
3. [P1] 弹性下载器移除跨域通用固定 IP 兜底，并强制保留原域名 TLS 身份校验 (禁止 verify=False)
4. [P2] 取消 CLI (cancel_event) 后直接退出，绝不重新提交 REST 任务
5. [P2] 浏览器并发保护槽位严格排队，杜绝绕过降级，并彻底消除 generator didn't stop after throw()
"""

import io
import os
import sys
import json
import zipfile
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd

SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ocr_client as oc
import table_postprocess as tp
import excel_export as ee
from online.graph import browser_task_concurrency_guard


class TestAuditOct19Fixes(unittest.TestCase):

    # =========================================================================
    # 1. 表头推断与单行表保护
    # =========================================================================
    def test_headerless_two_rows_not_promoted(self):
        """无表头表格中的 A01 / 13 不应被提升为列名，必须完整保留为数据行。"""
        html = "<table><tr><td>A01</td><td>13</td></tr><tr><td>A02</td><td>25</td></tr></table>"
        dfs = oc._parse_mineru_table_html(html)
        self.assertEqual(len(dfs), 1)
        df = dfs[0]
        self.assertEqual(df.columns.tolist(), ["Col_1", "Col_2"])
        self.assertEqual(df.values.tolist(), [["A01", "13"], ["A02", "25"]])

    def test_single_row_headerless_not_swallowed(self):
        """单行表且为数据行时，禁止提升为表头，且导出 Excel 时绝不整表消失。"""
        html = "<table><tr><td>A01</td><td>13</td></tr></table>"
        dfs = oc._parse_mineru_table_html(html)
        self.assertEqual(len(dfs), 1, "单行表解析后数量必须为 1")
        df = dfs[0]
        self.assertEqual(df.columns.tolist(), ["Col_1", "Col_2"])
        self.assertEqual(df.values.tolist(), [["A01", "13"]])

        # 验证经过 postprocess 与 excel_export 全链路后不消失
        df.attrs["label"] = "Table 1"
        df.attrs["extractor"] = "mineru_api"
        with tempfile.TemporaryDirectory() as td:
            xlsx_path = os.path.join(td, "single.xlsx")
            saved = ee.save_tables_to_excel(dfs, xlsx_path)
            self.assertTrue(saved, "单行表必须成功保存至 Excel")
            read_df = pd.read_excel(xlsx_path)
            self.assertEqual(len(read_df), 1, "导出的 Excel 表格必须包含 1 行真实数据")

    def test_scientific_notation_data_not_promoted(self):
        """科学计数法 (如 1e-3) 是数值数据，绝不应被判定为表头文本。"""
        html = "<table><tr><td>1e-3</td><td>13</td></tr><tr><td>2e-3</td><td>25</td></tr></table>"
        dfs = oc._parse_mineru_table_html(html)
        self.assertEqual(len(dfs), 1)
        df = dfs[0]
        self.assertEqual(df.columns.tolist(), ["Col_1", "Col_2"])
        self.assertEqual(df.values.tolist(), [["1e-3", "13"], ["2e-3", "25"]])

    def test_real_year_headers_correctly_promoted(self):
        """真实年份表头 (如 2020 / 2021) 虽为数字，但应正确推断并提升为列名。"""
        html = "<table><tr><td>2020</td><td>2021</td></tr><tr><td>10</td><td>20</td></tr></table>"
        dfs = oc._parse_mineru_table_html(html)
        self.assertEqual(len(dfs), 1)
        df = dfs[0]
        self.assertEqual(df.columns.tolist(), ["2020", "2021"])
        self.assertEqual(df.values.tolist(), [["10", "20"]])

    def test_country_and_year_headers_promoted(self):
        """复合年份表头 (如 Country / 2020 / 2021) 正确提升为列名。"""
        html = "<table><tr><td>Country</td><td>2020</td><td>2021</td></tr><tr><td>China</td><td>10</td><td>20</td></tr></table>"
        dfs = oc._parse_mineru_table_html(html)
        self.assertEqual(len(dfs), 1)
        df = dfs[0]
        self.assertEqual(df.columns.tolist(), ["Country", "2020", "2021"])
        self.assertEqual(df.values.tolist(), [["China", "10", "20"]])

    def test_vlm_clean_dataframe_preserves_headerless(self):
        """_clean_vlm_dataframe 在处理默认列名时，严禁吞噬 A01 / 13 首行。"""
        df = pd.DataFrame([["A01", "13"], ["A02", "25"]])
        df.attrs["extractor"] = "mineru_api"
        cleaned = tp._clean_vlm_dataframe(df, df.attrs)
        self.assertNotIn("A01", cleaned.columns.tolist())
        self.assertNotIn("13", cleaned.columns.tolist())
        self.assertEqual(len(cleaned), 2, "两行数据必须全数保留")

    def test_numeric_headers_with_th_preserved(self):
        """带有 <th>2020</th><th>2021</th> 的真实年份表头必须完整保留，严禁被误判为默认列名并覆盖为 Col_1/Col_2。"""
        html = "<table><tr><th>2020</th><th>2021</th></tr><tr><td>A01</td><td>13</td></tr></table>"
        dfs = oc._parse_mineru_table_html(html)
        self.assertEqual(len(dfs), 1)
        df = dfs[0]
        self.assertEqual(df.columns.tolist(), ["2020", "2021"])
        self.assertEqual(df.values.tolist(), [["A01", "13"]])

    def test_vlm_clean_dataframe_preserves_numeric_headers(self):
        """_clean_vlm_dataframe 严禁将真实年份表头 (如 2020 / 2021) 误判为系统默认列名并重置。"""
        df = pd.DataFrame([["A01", "13"]], columns=["2020", "2021"])
        df.attrs["extractor"] = "mineru_api"
        cleaned = tp._clean_vlm_dataframe(df, df.attrs)
        self.assertEqual(cleaned.columns.tolist(), ["2020", "2021"])
        self.assertEqual(len(cleaned), 1)

    # =========================================================================
    # 2. 官方 CLI 产物文件名与千分制坐标格式识别
    # =========================================================================
    def test_cli_output_filename_recognized_as_norm_1000(self):
        """官方 CLI 输出文件名如 coordinate.json 或 paper.json 应被正确识别为 norm_1000 坐标单位。"""
        table_data = [{
            "type": "table",
            "table_body": "<table><tr><th>Sample</th><th>Value</th></tr><tr><td>A01</td><td>13</td></tr></table>",
            "bbox": [100, 500, 900, 625],
            "page_idx": 0
        }]
        with tempfile.TemporaryDirectory() as td:
            json_file = os.path.join(td, "coordinate.json")
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(table_data, f)

            # 1. 从 DirArchive (CLI 输出目录适配器) 读取
            dfs_dir = oc.extract_mineru_structured_archive(td)
            self.assertEqual(len(dfs_dir), 1)
            self.assertEqual(dfs_dir[0].attrs.get("bbox_format"), "norm_1000")
            self.assertEqual(dfs_dir[0].attrs.get("coord_format"), "norm_1000")
            self.assertEqual(dfs_dir[0].attrs.get("mineru_source"), "cli")

            # 2. 从 Zip 包中直接以 paper.json 命名读取
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w") as zf:
                zf.writestr("paper.json", json.dumps(table_data))
            dfs_zip = oc.extract_mineru_structured_archive(bio.getvalue())
            self.assertEqual(len(dfs_zip), 1)
            self.assertEqual(dfs_zip[0].attrs.get("bbox_format"), "norm_1000")
            self.assertEqual(dfs_zip[0].attrs.get("coord_format"), "norm_1000")

    def test_middle_json_preserved_as_points(self):
        """middle.json (无论在 Zip 包中还是在已解压目录中) 依然保持为 PDF 点坐标 points。"""
        mid_data = {
            "pdf_info": [{
                "page_idx": 0,
                "page_size": [600, 800],
                "preproc_blocks": [{
                    "type": "table",
                    "bbox": [60, 400, 540, 500],
                    "table_body": "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
                }]
            }]
        }
        # 1. Zip 包形式
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w") as zf:
            zf.writestr("paper_middle.json", json.dumps(mid_data))
        dfs = oc.extract_mineru_structured_archive(bio.getvalue())
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].attrs.get("bbox_format"), "points")
        self.assertEqual(dfs[0].attrs.get("coord_format"), "points")

        # 2. 目录 (DirArchive) 形式
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "paper_middle.json"), "w", encoding="utf-8") as f:
                json.dump(mid_data, f)
            dfs_dir = oc.extract_mineru_structured_archive(td)
            self.assertEqual(len(dfs_dir), 1)
            self.assertEqual(dfs_dir[0].attrs.get("bbox_format"), "points")
            self.assertEqual(dfs_dir[0].attrs.get("coord_format"), "points")

    # =========================================================================
    # 3. 弹性下载器移除跨域通用固定 IP 兜底，并强制保留原域名 TLS 身份校验
    # =========================================================================
    @patch("requests.get")
    def test_resilient_downloader_removes_untrusted_ip_fallback(self, mock_get):
        """下载失败时，严禁将包含凭据/签名的完整请求转发至跨域硬编码固定 IP 节点。"""
        mock_get.side_effect = Exception("direct connect fail")
        content = oc.download_resilient_asset("https://assets.example.invalid/paper.zip?signature=AUDIT_DUMMY", timeout=1)
        self.assertIsNone(content)
        # 验证调用历史中绝不包含 171.107.77.45 等硬编码跨域 IP
        for call_item in mock_get.call_args_list:
            args_str = str(call_item)
            self.assertNotIn("171.107.77.45", args_str, "严禁将未知域名请求转发至硬编码 IP 171.107.77.45")
            self.assertNotIn("verify=False", args_str, "严禁在下载请求中关闭证书验证")

    # =========================================================================
    # 4. 取消 CLI 后直接退出，绝不重新提交 REST 任务
    # =========================================================================
    @patch.object(oc.MinerUCLIClient, "extract", return_value={"success": False, "error": "cancelled", "dfs": []})
    @patch.object(oc.MinerUClient, "submit_task")
    def test_call_mineru_api_cancelled_does_not_submit_rest(self, mock_submit, mock_cli):
        """在取消状态下，call_mineru_api 必须直接退出，严禁向 REST API 重新发起任务提交。"""
        cancel_event = MagicMock()
        cancel_event.is_set.return_value = True

        res = oc.call_mineru_api("dummy.pdf", config={"MINERU_USE_CLI": "auto"}, cancel_event=cancel_event)
        self.assertEqual(res, "")
        mock_cli.assert_called_once()
        mock_submit.assert_not_called()

    @patch.object(oc.MinerUCLIClient, "extract", return_value={"success": False, "error": "cancelled", "dfs": []})
    @patch.object(oc.MinerUClient, "submit_task")
    def test_call_mineru_structured_api_cancelled_does_not_submit_rest(self, mock_submit, mock_cli):
        """在取消状态下，call_mineru_structured_api 必须直接退出，严禁向 REST API 重新发起任务提交。"""
        cancel_event = MagicMock()
        cancel_event.is_set.return_value = True

        res = oc.call_mineru_structured_api("dummy.pdf", config={"MINERU_USE_CLI": "auto"}, cancel_event=cancel_event)
        self.assertEqual(res, [])
        mock_cli.assert_called_once()
        mock_submit.assert_not_called()

    # =========================================================================
    # 5. 浏览器并发保护槽位严格排队，杜绝绕过降级，并彻底消除 generator didn't stop after throw()
    # =========================================================================
    @patch("online.cdp_detector.find_active_cdp_endpoint", return_value={"cdp_url": "http://127.0.0.1:9222"})
    def test_browser_task_guard_propagates_original_exception(self, mock_ep):
        """任务内部抛出的异常必须原样抛出至外层调用方，绝不能被捕获并转化为 generator didn't stop after throw()。"""
        with self.assertRaises(ValueError) as ctx:
            with browser_task_concurrency_guard():
                raise ValueError("original extraction error")
        self.assertEqual(str(ctx.exception), "original extraction error")

    @patch("online.cdp_detector.find_active_cdp_endpoint", return_value={"cdp_url": "http://127.0.0.1:9222"})
    @patch("online.cdp_detector.cdp_tab_slot_guard", side_effect=TimeoutError("Could not acquire cross-process CDP tab slot lock."))
    def test_browser_task_guard_strict_concurrency_blocks_entry(self, mock_slot, mock_ep):
        """当 6 个跨进程槽位占满超时时，严格抛出 TimeoutError，严禁降级绕过进入受保护区域。"""
        entered = False
        with self.assertRaises(TimeoutError):
            with browser_task_concurrency_guard():
                entered = True
        self.assertFalse(entered, "跨进程槽位占满时绝对不允许进入受保护区域")


if __name__ == "__main__":
    unittest.main()
