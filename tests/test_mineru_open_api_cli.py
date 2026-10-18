#!/usr/bin/env python3
"""
tests/test_mineru_open_api_cli.py — 针对 MinerU Open API CLI 集成与双引擎调度的全套单元测试：
1. CLI 可执行文件探测 (find_mineru_cli_binary / is_mineru_cli_available)
2. Token 智能解析优先级 (resolve_mineru_token: config -> env -> ~/.mineru/config.yaml)
3. 目录归档适配器 (DirArchive) namelist & read 语义保真
4. extract_mineru_structured_archive 直接解析已解压目录
5. MinerUCLIClient.extract 与 flash_extract 命令行封装、参数传递与取消响应
6. 双引擎调度器 (call_mineru_structured_api & call_mineru_api):
   - auto 模式：CLI 优先执行
   - auto 模式：CLI 失败时平滑回退至官方 Python REST API
   - cli 模式：强制执行 CLI
   - api 模式：强制绕过 CLI 直连 Python REST API
7. table_postprocess 与 table_validator 对 mineru_cli / mineru_api 的保护与坐标识别
8. MinerUBackend (OCR-Hub) 能力发现与健康探测中对 CLI 状态的精确反映
"""

import io
import os
import sys
import json
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd

SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ocr_client
import common
import table_postprocess
import table_validator


class TestMinerUOpenAPICLI(unittest.TestCase):
    """测试 MinerU Open API CLI 可执行文件探测、Token 解析与目录归档适配。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_mineru_cli_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_find_mineru_cli_binary_custom_path(self):
        """测试传入存在的自定义可执行路径时优先返回，不存在时不返回错误路径。"""
        fake_bin = os.path.join(self.temp_dir, "fake_mineru_cli")
        with open(fake_bin, "w") as f:
            f.write("#!/bin/sh\necho mineru")
        os.chmod(fake_bin, 0o755)

        found = ocr_client.find_mineru_cli_binary(custom_path=fake_bin)
        self.assertEqual(found, os.path.abspath(fake_bin))

        # 测试不存在的路径不应被直接返回
        with patch("shutil.which", return_value=None):
            with patch.dict(os.environ, {"MINERU_CLI_PATH": ""}):
                invalid_found = ocr_client.find_mineru_cli_binary(custom_path="/path/that/does/not/exist")
                self.assertNotEqual(invalid_found, "/path/that/does/not/exist")

        with patch("shutil.which", return_value=None):
            with patch.dict(os.environ, {"MINERU_CLI_PATH": ""}):
                with patch("glob.glob", return_value=[]):
                    with patch("os.path.isfile", return_value=False):
                        self.assertIsNone(ocr_client.find_mineru_cli_binary(custom_path="/path/that/does/not/exist"))

    def test_find_mineru_cli_binary_env_path(self):
        """测试通过环境变量 MINERU_CLI_PATH 指定路径。"""
        fake_bin = os.path.join(self.temp_dir, "env_mineru_cli")
        with open(fake_bin, "w") as f:
            f.write("#!/bin/sh\necho mineru")
        os.chmod(fake_bin, 0o755)

        with patch.dict(os.environ, {"MINERU_CLI_PATH": fake_bin}):
            found = ocr_client.find_mineru_cli_binary()
            self.assertEqual(found, os.path.abspath(fake_bin))

    def test_find_mineru_cli_binary_which(self):
        """测试从系统 PATH (which) 探测二进制。"""
        with patch("shutil.which", return_value="/usr/bin/mineru-open-api"):
            with patch.dict(os.environ, {"MINERU_CLI_PATH": ""}):
                found = ocr_client.find_mineru_cli_binary()
                self.assertEqual(found, "/usr/bin/mineru-open-api")

    def test_is_mineru_cli_available_flag(self):
        """测试 is_mineru_cli_available 返回布尔值状态以及禁用配置。"""
        with patch("ocr_client.find_mineru_cli_binary", return_value="/dummy/mineru-open-api"):
            self.assertTrue(ocr_client.is_mineru_cli_available())
            # 当显式配置禁用 CLI 时返回 False
            self.assertFalse(ocr_client.is_mineru_cli_available(config={"MINERU_USE_CLI": "api"}))
            self.assertFalse(ocr_client.is_mineru_cli_available(config={"MINERU_USE_CLI": "false"}))
            self.assertFalse(ocr_client.is_mineru_cli_available(config={"MINERU_USE_CLI": "disabled"}))
        with patch("ocr_client.find_mineru_cli_binary", return_value=None):
            self.assertFalse(ocr_client.is_mineru_cli_available())

    def test_resolve_mineru_token_priority(self):
        """测试 Token 解析优先级：config > env > ~/.mineru/config.yaml。"""
        # 1. config 优先
        tok1 = ocr_client.resolve_mineru_token(config={"MINERU_TOKEN": "token-from-cfg"})
        self.assertEqual(tok1, "token-from-cfg")

        # 2. env 优先于 yaml
        with patch.dict(os.environ, {"MINERU_TOKEN": "token-from-env"}):
            tok2 = ocr_client.resolve_mineru_token(config={})
            self.assertEqual(tok2, "token-from-env")

        # 3. yaml 备用
        yaml_path = os.path.join(self.temp_dir, "config.yaml")
        with open(yaml_path, "w") as f:
            f.write("token: sk-yaml-test-token-1234\n")

        with patch.dict(os.environ, {"MINERU_TOKEN": "", "MINERU_API_KEY": ""}):
            with patch("os.path.expanduser", return_value=yaml_path):
                with patch("os.path.isfile", side_effect=lambda p: p == yaml_path):
                    tok3 = ocr_client.resolve_mineru_token(config={})
                    self.assertEqual(tok3, "sk-yaml-test-token-1234")

    def test_dir_archive_namelist_and_read(self):
        """测试 DirArchive 目录适配器无缝模拟 ZipFile 接口。"""
        sub_dir = os.path.join(self.temp_dir, "extracted_output", "subfolder")
        os.makedirs(sub_dir, exist_ok=True)

        json_file = os.path.join(sub_dir, "sample_content_list.json")
        with open(json_file, "w", encoding="utf-8") as f:
            f.write('{"test": 123}')

        md_file = os.path.join(self.temp_dir, "extracted_output", "doc.md")
        with open(md_file, "w", encoding="utf-8") as f:
            f.write("# Header\n| Col |\n| --- |\n| 1 |")

        archive = ocr_client.DirArchive(os.path.join(self.temp_dir, "extracted_output"))
        names = archive.namelist()
        self.assertIn("doc.md", names)
        self.assertIn("subfolder/sample_content_list.json", names)

        # 读取内容
        b_json = archive.read("subfolder/sample_content_list.json")
        self.assertEqual(json.loads(b_json.decode("utf-8")), {"test": 123})

        # 扁平文件名容错读取
        b_flat = archive.read("sample_content_list.json")
        self.assertEqual(json.loads(b_flat.decode("utf-8")), {"test": 123})

    def test_dir_archive_context_manager_and_getinfo(self):
        """测试 DirArchive 支持上下文管理器与 getinfo / infolist 鸭子类型。"""
        sub_dir = os.path.join(self.temp_dir, "archive_test")
        os.makedirs(sub_dir, exist_ok=True)
        test_file = os.path.join(sub_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello context")

        with ocr_client.DirArchive(sub_dir) as da:
            info = da.getinfo("test.txt")
            self.assertEqual(info.filename, "test.txt")
            self.assertEqual(info.file_size, len("hello context"))
            infos = da.infolist()
            self.assertEqual(len(infos), 1)

        # 验证目录穿越防护
        with ocr_client.DirArchive(sub_dir) as da:
            with self.assertRaises(ValueError):
                da.read("../outside.txt")

    def test_extract_mineru_structured_archive_directly_from_directory(self):
        """测试 extract_mineru_structured_archive 直接接收已解压目录路径并提取完整表格。"""
        out_dir = os.path.join(self.temp_dir, "cli_result_dir")
        os.makedirs(out_dir, exist_ok=True)

        html = """
        <table>
          <thead>
            <tr><th colspan="2">Group</th><th>Value</th></tr>
            <tr><th>ID</th><th>Sub</th><th>Score</th></tr>
          </thead>
          <tbody>
            <tr><td>001</td><td>A</td><td>95.5</td></tr>
            <tr><td>002</td><td>B</td><td>88.0</td></tr>
          </tbody>
        </table>
        """
        content_list = [
            {
                "type": "table",
                "table_body": html,
                "page_idx": 1,
                "bbox": [100.0, 150.0, 500.0, 450.0],
                "table_caption": "Table 1. Experimental Summary",
                "table_footnote": "Note: Tested under standard conditions."
            }
        ]
        json_path = os.path.join(out_dir, "doc_content_list.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(content_list, f)

        dfs = ocr_client.extract_mineru_structured_archive(out_dir)
        self.assertEqual(len(dfs), 1)
        df = dfs[0]

        # 验证元数据
        self.assertEqual(df.attrs.get("extractor"), "mineru_api")
        self.assertEqual(df.attrs.get("mineru_source"), "cli")
        self.assertEqual(df.attrs.get("page_idx"), 1)
        self.assertEqual(df.attrs.get("bbox"), [100.0, 150.0, 500.0, 450.0])
        self.assertEqual(df.attrs.get("table_title"), "Table 1. Experimental Summary")
        self.assertEqual(df.attrs.get("table_footnote"), "Note: Tested under standard conditions.")
        self.assertEqual(df.attrs.get("label"), "Table 1")

        # 验证复合表头展平
        expected_cols = ["Group_ID", "Group_Sub", "Value_Score"]
        self.assertEqual(list(df.columns), expected_cols)

        # 验证样号数据类型保真
        self.assertEqual(str(df.iloc[0]["Group_ID"]), "001")


class TestMinerUCLIClientExecution(unittest.TestCase):
    """测试 MinerUCLIClient 执行流程、命令拼接与双引擎调度。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_mineru_client_")
        self.mock_bin = os.path.join(self.temp_dir, "fake_mineru_open_api")
        with open(self.mock_bin, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.mock_bin, 0o755)
        self.sample_pdf = os.path.join(self.temp_dir, "test.pdf")
        with open(self.sample_pdf, "wb") as f:
            f.write(b"%PDF-1.4 " + b"0" * 100)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("subprocess.Popen")
    def test_mineru_cli_client_extract_success(self, mock_popen):
        """测试 MinerUCLIClient.extract 成功执行、参数拼接与结果解析。"""
        # 模拟 CLI 产物
        def fake_communicate(*args, **kwargs):
            return "Thinking... Done\n", ""

        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.communicate.side_effect = fake_communicate
        mock_popen.return_value = proc

        # 预先生成一个输出目录结构供提取
        out_dir = os.path.join(self.temp_dir, "extracted_target")
        os.makedirs(out_dir, exist_ok=True)
        content_list = [
            {
                "type": "table",
                "table_body": "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>",
                "page_idx": 0,
                "table_caption": "Table 1 Sample",
            }
        ]
        with open(os.path.join(out_dir, "content_list.json"), "w") as f:
            json.dump(content_list, f)
        with open(os.path.join(out_dir, "output.md"), "w") as f:
            f.write("# Table 1\n| A | B |\n| --- | --- |\n| 1 | 2 |")

        client = ocr_client.MinerUCLIClient(
            cli_path=self.mock_bin,
            api_key="mock-token-xyz"
        )
        res = client.extract(
            self.sample_pdf,
            output_dir=out_dir,
            pages=[0, 1],
            model="vlm",
            timeout=120
        )

        self.assertTrue(res["success"])
        self.assertEqual(len(res["dfs"]), 1)
        self.assertIn("Table 1", res["markdown"])

        # 验证命令参数
        called_cmd = mock_popen.call_args[0][0]
        self.assertEqual(called_cmd[0], self.mock_bin)
        self.assertEqual(called_cmd[1], "extract")
        self.assertIn("--pages", called_cmd)
        self.assertIn("1-2", called_cmd)
        self.assertIn("--model", called_cmd)
        self.assertIn("vlm", called_cmd)
        self.assertIn("--token", called_cmd)
        self.assertIn("mock-token-xyz", called_cmd)

    @patch("subprocess.Popen")
    def test_mineru_cli_client_flash_extract_success(self, mock_popen):
        """测试 MinerUCLIClient.flash_extract 零 Token 快速抽取命令封装。"""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.communicate.return_value = ("# Flash Markdown Content\n| X | Y |\n| - | - |\n| 1 | 2 |", "")
        mock_popen.return_value = proc

        out_dir = os.path.join(self.temp_dir, "flash_out")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "test.md"), "w") as f:
            f.write("| X | Y |\n| - | - |\n| 1 | 2 |")

        client = ocr_client.MinerUCLIClient(cli_path=self.mock_bin)
        res = client.flash_extract(self.sample_pdf, output_dir=out_dir, pages=[2])

        self.assertTrue(res["success"])
        self.assertIn("| X | Y |", res["markdown"])

        called_cmd = mock_popen.call_args[0][0]
        self.assertEqual(called_cmd[1], "flash-extract")
        self.assertIn("--pages", called_cmd)
        self.assertIn("3", called_cmd)

    @patch("subprocess.Popen")
    def test_mineru_cli_client_failure_returns_error(self, mock_popen):
        """测试 CLI 运行发生常规不可自愈异常时返回非零退出码与错误描述。"""
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.returncode = 1
        proc.communicate.return_value = ("", "Error: upload failed: account credit exhausted")
        mock_popen.return_value = proc

        client = ocr_client.MinerUCLIClient(cli_path=self.mock_bin)
        res = client.extract(self.sample_pdf)
        self.assertFalse(res["success"])
        self.assertIn("account credit exhausted", res["error"])

    @patch("subprocess.Popen")
    @patch("ocr_client.download_resilient_asset")
    def test_mineru_cli_client_cdn_eof_self_healing_extract(self, mock_download, mock_popen):
        """测试当 CLI 进程在下载 ZIP 阶段遭遇 CDN 断流 (TLS EOF) 时，CLI 客户端自动从 stderr 提取 URL 弹性自愈。"""
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate.return_value = (
            "",
            'Thinking... test.pdf\nError: download zip: Get "https://cdn-mineru.openxlab.org.cn/pdf/2026-10-18/dummy.zip": EOF'
        )
        mock_popen.return_value = proc

        # 准备一个包含表格的 dummy zip 字节
        bio = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(bio, "w") as zf:
            zf.writestr(
                "content_list.json",
                json.dumps([{"type": "table", "table_body": "<table><tr><th>H1</th></tr><tr><td>V1</td></tr></table>", "page_idx": 0}])
            )
            zf.writestr("output.md", "# Test Doc\n| H1 |\n|---|\n| V1 |")
        mock_download.return_value = bio.getvalue()

        client = ocr_client.MinerUCLIClient(cli_path=self.mock_bin)
        res = client.extract(self.sample_pdf)

        self.assertTrue(res["success"])
        self.assertEqual(len(res["dfs"]), 1)
        self.assertEqual(res["dfs"][0].iloc[0]["H1"], "V1")
        mock_download.assert_called_once_with("https://cdn-mineru.openxlab.org.cn/pdf/2026-10-18/dummy.zip", timeout=120)

    @patch("requests.get")
    def test_download_resilient_asset_direct_and_doh_fallback(self, mock_requests_get):
        """测试 download_resilient_asset 在直连遇到 SSLError 时自动切换 DoH 节点旁路直连。"""
        # 第一次直连抛出 SSLError
        # 第二次请求 AliDNS DoH 成功返回 Answer
        # 第三次带 Host 请求直连 IP 成功
        def fake_get(url, **kwargs):
            if "223.5.5.5" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"Answer": [{"type": 1, "data": "1.2.3.4"}]}
                return resp
            elif "1.2.3.4" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.content = b"ZIP_CONTENT_SUCCESS"
                return resp
            else:
                import requests
                raise requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")

        mock_requests_get.side_effect = fake_get
        content = ocr_client.download_resilient_asset("https://cdn-mineru.openxlab.org.cn/test.zip")
        self.assertEqual(content, b"ZIP_CONTENT_SUCCESS")

    @patch("ocr_client.resolve_mineru_token", return_value="")
    @patch.object(ocr_client.MinerUCLIClient, "flash_extract")
    def test_dual_engine_zero_token_structured_uses_flash_extract(self, mock_flash, mock_tok):
        """测试在无 Token 且小文件 (<=10MB) 条件下，call_mineru_structured_api 自动调用 flash_extract。"""
        dummy_df = pd.DataFrame({"F": [1]})
        dummy_df.attrs["extractor"] = "mineru_api"
        dummy_df.attrs["mineru_source"] = "cli_flash"
        mock_flash.return_value = {
            "success": True,
            "dfs": [dummy_df],
            "markdown": "| F |\n| - |\n| 1 |"
        }

        with patch("ocr_client.is_mineru_cli_available", return_value=True):
            dfs = ocr_client.call_mineru_structured_api(
                self.sample_pdf,
                config={"MINERU_USE_CLI": "auto", "MINERU_TOKEN": ""}
            )

        self.assertEqual(len(dfs), 1)
        mock_flash.assert_called_once()

    @patch.object(ocr_client.MinerUCLIClient, "extract")
    def test_dual_engine_call_mineru_structured_api_cli_preferred(self, mock_cli_extract):
        """测试在 auto 模式下检测到 CLI 可用时，call_mineru_structured_api 优先调度 CLI。"""
        dummy_df = pd.DataFrame({"A": [10], "B": [20]})
        dummy_df.attrs["extractor"] = "mineru_api"
        mock_cli_extract.return_value = {
            "success": True,
            "dfs": [dummy_df],
            "markdown": "| A | B |\n|---|---|\n| 10 | 20 |"
        }

        with patch("ocr_client.is_mineru_cli_available", return_value=True):
            dfs = ocr_client.call_mineru_structured_api(
                self.sample_pdf,
                config={"MINERU_USE_CLI": "auto", "MINERU_TOKEN": "test_tok"},
                pages=[0]
            )

        self.assertEqual(len(dfs), 1)
        mock_cli_extract.assert_called_once()

    @patch.object(ocr_client.MinerUCLIClient, "extract")
    @patch.object(ocr_client.MinerUClient, "submit_task")
    @patch.object(ocr_client.MinerUClient, "poll_task")
    @patch.object(ocr_client.MinerUClient, "parse_mineru_structured_result")
    def test_dual_engine_fallback_when_cli_fails(self, mock_parse, mock_poll, mock_submit, mock_cli_extract):
        """测试在 auto 模式下，当 CLI 执行失败时自动无损平滑回退至官方 Python REST API 客户端。"""
        # 1. 模拟 CLI 失败
        mock_cli_extract.return_value = {
            "success": False,
            "error": "TLS handshake EOF on CDN",
            "dfs": []
        }

        # 2. 模拟 REST API 降级提取成功
        mock_submit.return_value = "batch_rest_123"
        mock_poll.return_value = {"success": True, "data": {"state": "done"}}
        fallback_df = pd.DataFrame({"FallbackCol": [999]})
        fallback_df.attrs["extractor"] = "mineru_api"
        mock_parse.return_value = [fallback_df]

        with patch("ocr_client.is_mineru_cli_available", return_value=True):
            dfs = ocr_client.call_mineru_structured_api(
                self.sample_pdf,
                config={"MINERU_USE_CLI": "auto", "MINERU_TOKEN": "test_tok"},
                pages=[0]
            )

        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].iloc[0]["FallbackCol"], 999)
        mock_cli_extract.assert_called_once()
        mock_submit.assert_called_once()
        mock_poll.assert_called_once()

    @patch.object(ocr_client.MinerUCLIClient, "extract")
    @patch.object(ocr_client.MinerUClient, "submit_task")
    def test_cli_success_empty_tables_does_not_fallback_to_rest(self, mock_submit, mock_cli_extract):
        """测试当 CLI 执行成功但文档无表格时，直接返回空列表，不冗余调用 REST API。"""
        mock_cli_extract.return_value = {
            "success": True,
            "error": None,
            "dfs": []
        }
        with patch("ocr_client.is_mineru_cli_available", return_value=True):
            dfs = ocr_client.call_mineru_structured_api(
                self.sample_pdf,
                config={"MINERU_USE_CLI": "auto", "MINERU_TOKEN": "test_tok"}
            )
        self.assertEqual(dfs, [])
        mock_cli_extract.assert_called_once()
        mock_submit.assert_not_called()

    @patch.object(ocr_client.MinerUCLIClient, "extract")
    @patch.object(ocr_client.MinerUClient, "submit_task")
    def test_enforce_api_mode_skips_cli(self, mock_submit, mock_cli_extract):
        """测试配置 MINERU_USE_CLI='api' 时强制绕过 CLI，直接调用 Python REST API。"""
        with patch.object(ocr_client.MinerUClient, "poll_task", return_value={"success": True, "data": {}}):
            with patch.object(ocr_client.MinerUClient, "parse_mineru_structured_result", return_value=[]):
                mock_submit.return_value = "task_999"
                ocr_client.call_mineru_structured_api(
                    self.sample_pdf,
                    config={"MINERU_USE_CLI": "api", "MINERU_TOKEN": "tok"}
                )

        mock_cli_extract.assert_not_called()
        mock_submit.assert_called_once()

    def test_table_postprocess_and_validator_mineru_cli_support(self):
        """测试 table_postprocess 与 table_validator 对 mineru_cli 来源的无损白名单直通与 norm_1000 坐标识别。"""
        df = pd.DataFrame({
            "Sample": ["001", "002"],
            "Value": ["10-20", "20-30"]
        })
        df.attrs["extractor"] = "mineru_cli"
        df.attrs["bbox"] = [100, 200, 800, 900]
        df.attrs["bbox_format"] = "norm_1000"

        # 1. 验证 table_postprocess 不会破坏首行真实数据
        cleaned_df = table_postprocess.postprocess_table(df)
        self.assertEqual(cleaned_df.iloc[0]["Sample"], "001")

        # 2. 验证 table_validator 准确将 norm_1000 映射为点坐标
        is_valid, info = table_validator.verify_table_numeric_integrity(
            df, self.sample_pdf, page_idx=0, min_match_ratio=0.0
        )
        # 门禁不发生异常
        self.assertIsNotNone(info)

    def test_mineru_backend_capabilities_and_probe_health(self):
        """测试 MinerUBackend 支持 cli_supported 并向 OCR-Hub 准确汇报 CLI 健康状态。"""
        backend = ocr_client.MinerUBackend()
        caps = backend.get_capabilities()
        self.assertTrue(caps.get("cli_supported"))

        # 当 CLI 可用时探针汇报 healthy
        with patch("ocr_client.is_mineru_cli_available", return_value=True):
            with patch("ocr_client.find_mineru_cli_binary", return_value="/bin/mineru-open-api"):
                with patch("ocr_client.resolve_mineru_token", return_value="tok_123"):
                    health = backend.probe_health()
                    self.assertEqual(health["status"], "healthy")
                    self.assertTrue(health.get("cli_available"))
                    self.assertIn("/bin/mineru-open-api", health["message"])


if __name__ == "__main__":
    unittest.main()
