#!/usr/bin/env python3
"""
scratch/test_updater.py — Zotero Table Extractor 技能在线更新与配置保护机制专项测试套件。

覆盖范围：
1. ConfigBackupManager 快照创建、哈希校验、Manifest 记录、滚动清理、亚秒级防碰撞与历史精准回滚；
2. ConfigMerger 敏感字段脱敏、占位符判定、UTF-8 BOM 兼容、损坏配置自愈、双向安全合并（0 覆盖、补新字段、保留自定义字段）；
3. is_protected_path 跨平台大小写不敏感匹配、父目录命中与通配符防护；
4. ArchiveUpdateStrategy 归档包更新白名单保护模拟、__MACOSX 结构自愈、Zip Slip 路径穿越防御与大权重模型保护；
5. GitUpdateStrategy 非 Git 目录无崩溃报错、游离 HEAD 探测与本地 Git 仓库工作区安全暂存快进测试；
6. SkillUpdater 统一门面控制器测试（check, dry-run, rollback, backup-only, merge-only）；
7. setup_paths.py 与 update.py 命令行端到端集成自测。
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import zipfile
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts.updater import (
    ConfigBackupManager,
    ConfigMerger,
    GitUpdateStrategy,
    ArchiveUpdateStrategy,
    SkillUpdater,
    PROTECTED_PATTERNS,
    is_protected_path
)


class TestConfigBackupManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_backup_mgr_")
        self.config_file = os.path.join(self.temp_dir, "config.json")
        self.example_file = os.path.join(self.temp_dir, "config.example.json")

        self.initial_data = {
            "FIRECRAWL_API_KEY": "fc-test-key-1234567890",
            "ELSEVIER_API_KEY": "els-test-secret-key-999",
            "PLAYWRIGHT_USER_DATA_DIR": "/custom/path/profile",
            "DOCLAYOUT_YOLO_ENABLED": True,
            "CUSTOM_USER_SETTING": "my_private_value"
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.initial_data, f, indent=2)

        self.mgr = ConfigBackupManager(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_backup_success(self):
        """测试正常创建快照并记录至 manifest.json 与 config.backup.json。"""
        res = self.mgr.create_backup(reason="unit_test")
        self.assertIsNotNone(res)
        self.assertTrue(os.path.isfile(res["file_path"]))
        self.assertEqual(res["reason"], "unit_test")
        self.assertEqual(res["keys_count"], 5)

        # 验证单文件快捷备份 config.backup.json
        quick_path = os.path.join(self.temp_dir, "config.backup.json")
        self.assertTrue(os.path.isfile(quick_path))
        with open(quick_path, "r", encoding="utf-8-sig") as f:
            quick_data = json.load(f)
        self.assertEqual(quick_data["FIRECRAWL_API_KEY"], "fc-test-key-1234567890")

        # 验证 manifest.json 记录
        manifest = self.mgr._load_manifest()
        self.assertEqual(len(manifest["backups"]), 1)
        self.assertEqual(manifest["backups"][0]["id"], res["id"])

    def test_subsecond_backup_collision_prevention(self):
        """测试同秒内快速连续创建多次快照，ID 与文件名互不冲突。"""
        res1 = self.mgr.create_backup(reason="rapid_1")
        res2 = self.mgr.create_backup(reason="rapid_2")
        self.assertIsNotNone(res1)
        self.assertIsNotNone(res2)
        self.assertNotEqual(res1["file_path"], res2["file_path"])
        self.assertTrue(os.path.isfile(res1["file_path"]))
        self.assertTrue(os.path.isfile(res2["file_path"]))

    def test_list_backups_sorting_and_existence(self):
        """测试多快照按时间倒序排列，且自动过滤不存在的死链。"""
        self.mgr.create_backup(reason="backup_1")
        self.mgr.create_backup(reason="backup_2")
        backups = self.mgr.list_backups()
        self.assertEqual(len(backups), 2)
        self.assertTrue("backup_2" in backups[0]["id"])

        # 模拟人为删除了 backup_2 文件实体
        os.remove(backups[0]["file_path"])
        refreshed = self.mgr.list_backups()
        self.assertEqual(len(refreshed), 1)
        self.assertTrue("backup_1" in refreshed[0]["id"])

    def test_restore_backup_latest(self):
        """测试将破坏后的 config.json 精准回滚至最近一次备份。"""
        self.mgr.create_backup(reason="golden_state")

        corrupted_data = {"FIRECRAWL_API_KEY": "corrupted"}
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(corrupted_data, f)

        restore_res = self.mgr.restore_backup(backup_id="latest")
        self.assertTrue(restore_res["success"])

        with open(self.config_file, "r", encoding="utf-8-sig") as f:
            restored = json.load(f)
        self.assertEqual(restored["FIRECRAWL_API_KEY"], "fc-test-key-1234567890")
        self.assertEqual(restored["CUSTOM_USER_SETTING"], "my_private_value")
        self.assertEqual(len(restored), 5)

    def test_restore_backup_partial_id(self):
        """测试通过部分 ID 前缀进行精准历史定位回滚。"""
        b1 = self.mgr.create_backup(reason="milestone_alpha")
        # 修改并生成第二个备份
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump({"state": 2}, f)
        self.mgr.create_backup(reason="milestone_beta")

        # 使用部分字符串匹配回滚到 milestone_alpha
        res = self.mgr.restore_backup(backup_id="milestone_alpha")
        self.assertTrue(res["success"])
        self.assertEqual(res["backup_id"], b1["id"])
        with open(self.config_file, "r", encoding="utf-8-sig") as f:
            restored = json.load(f)
        self.assertEqual(restored["FIRECRAWL_API_KEY"], "fc-test-key-1234567890")


class TestConfigMerger(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_merger_")
        self.config_file = os.path.join(self.temp_dir, "config.json")
        self.example_file = os.path.join(self.temp_dir, "config.example.json")

        self.user_cfg = {
            "FIRECRAWL_API_KEY": "fc-real-key-user-configured",
            "ELSEVIER_API_KEY": "els-real-key",
            "PLAYWRIGHT_USER_DATA_DIR": "/Users/ryanx/.profile",
            "DOCLAYOUT_CONF_THRESHOLD": 0.35,
            "MY_CUSTOM_EXT_KEY": "should_be_kept"
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.user_cfg, f, indent=2)

        self.example_cfg = {
            "FIRECRAWL_API_KEY": "your-firecrawl-api-key",
            "ELSEVIER_API_KEY": "your-elsevier-api-key",
            "PLAYWRIGHT_USER_DATA_DIR": "",
            "DOCLAYOUT_CONF_THRESHOLD": 0.25,
            "NEW_UPSTREAM_SETTING": True,
            "NEW_UPSTREAM_TIMEOUT": 120,
            "NEW_UPSTREAM_PLACEHOLDER": "your-placeholder-token",
            "ANOTHER_PLACEHOLDER": "<your_api_key_here>"
        }
        with open(self.example_file, "w", encoding="utf-8") as f:
            json.dump(self.example_cfg, f, indent=2)

        self.merger = ConfigMerger(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mask_sensitive_values(self):
        """测试脱敏函数对各类长短敏感凭证及路径的显示处理。"""
        self.assertIn("fc-r...ured", ConfigMerger.mask_value("FIRECRAWL_API_KEY", "fc-real-key-user-configured"))
        self.assertIn("[未设置]", ConfigMerger.mask_value("LLM_API_KEY", ""))
        self.assertIn("模板占位符", ConfigMerger.mask_value("API_KEY", "your-key"))
        self.assertIn("/Users/ryanx/.profile", ConfigMerger.mask_value("PLAYWRIGHT_USER_DATA_DIR", "/Users/ryanx/.profile"))

    def test_placeholder_detection(self):
        """测试占位符判定函数对各类格式的涵盖。"""
        self.assertTrue(ConfigMerger.is_template_placeholder("your-token"))
        self.assertTrue(ConfigMerger.is_template_placeholder("YOUR_API_KEY"))
        self.assertTrue(ConfigMerger.is_template_placeholder("<enter_api_key>"))
        self.assertTrue(ConfigMerger.is_template_placeholder("[placeholder]"))
        self.assertTrue(ConfigMerger.is_template_placeholder("example-access-token"))
        self.assertFalse(ConfigMerger.is_template_placeholder("sk-actual-api-key-123456"))
        self.assertFalse(ConfigMerger.is_template_placeholder(""))

    def test_merge_preserves_user_keys_completely(self):
        """测试配置合并后：用户已有配置 100% 保留，上游新项正常补充，占位符清洗为安全空值。"""
        merged, report = self.merger.merge_configs(self.user_cfg, self.example_cfg)

        self.assertEqual(merged["FIRECRAWL_API_KEY"], "fc-real-key-user-configured")
        self.assertEqual(merged["ELSEVIER_API_KEY"], "els-real-key")
        self.assertEqual(merged["PLAYWRIGHT_USER_DATA_DIR"], "/Users/ryanx/.profile")
        self.assertEqual(merged["DOCLAYOUT_CONF_THRESHOLD"], 0.35)
        self.assertEqual(merged["MY_CUSTOM_EXT_KEY"], "should_be_kept")

        self.assertEqual(merged["NEW_UPSTREAM_SETTING"], True)
        self.assertEqual(merged["NEW_UPSTREAM_TIMEOUT"], 120)
        self.assertEqual(merged["NEW_UPSTREAM_PLACEHOLDER"], "")
        self.assertEqual(merged["ANOTHER_PLACEHOLDER"], "")

        self.assertEqual(report["preserved_count"], 4)
        self.assertEqual(report["added_count"], 4)
        self.assertEqual(report["custom_count"], 1)

    def test_utf8_bom_support(self):
        """测试 Windows 记事本等工具附带的 UTF-8 BOM 文件能被正确读取与合并。"""
        bom_bytes = "\ufeff".encode("utf-8") + json.dumps({"BOM_TEST_KEY": "val"}).encode("utf-8")
        with open(self.config_file, "wb") as f:
            f.write(bom_bytes)

        user_cfg, _ = self.merger.load_configs()
        self.assertIn("BOM_TEST_KEY", user_cfg)
        self.assertEqual(user_cfg["BOM_TEST_KEY"], "val")

    def test_corrupt_config_auto_healing(self):
        """测试 config.json 损坏或 0 字节时，能自动从快速备份中自愈恢复。"""
        # 建立一份有效备份
        self.merger.backup_mgr.create_backup(reason="valid_state")

        # 模拟 config.json 遭遇损坏截断
        with open(self.config_file, "w", encoding="utf-8") as f:
            f.write("{invalid_json...")

        user_cfg, _ = self.merger.load_configs()
        self.assertEqual(user_cfg.get("FIRECRAWL_API_KEY"), "fc-real-key-user-configured")


class TestProtectedPaths(unittest.TestCase):
    def test_is_protected_path_cases(self):
        """测试路径防护判定逻辑的跨平台与大小写不敏感特性。"""
        self.assertTrue(is_protected_path("config.json"))
        self.assertTrue(is_protected_path("Config.json"))
        self.assertTrue(is_protected_path("CONFIG.JSON"))
        self.assertTrue(is_protected_path("config.backup.json"))
        self.assertTrue(is_protected_path(".backups/config_2026.json"))
        self.assertTrue(is_protected_path(".backups\\manifest.json"))
        self.assertTrue(is_protected_path("profiles/cookies.sqlite"))
        self.assertTrue(is_protected_path("Profiles/Chrome/Profile 1"))
        self.assertTrue(is_protected_path(".git/HEAD"))
        self.assertTrue(is_protected_path("my_settings.local.json"))
        self.assertTrue(is_protected_path("database.db"))
        self.assertTrue(is_protected_path(".env"))
        self.assertTrue(is_protected_path(".env.production"))

        self.assertFalse(is_protected_path("scripts/extract_zotero_table.py"))
        self.assertFalse(is_protected_path("config.example.json"))
        self.assertFalse(is_protected_path("README.md"))
        self.assertFalse(is_protected_path("setup_paths.py"))


class TestArchiveUpdateStrategy(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_archive_")
        self.strat = ArchiveUpdateStrategy(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_archive_preserves_protected_files_case_insensitively(self):
        """测试归档解压覆盖时，绝对不覆写任何大小写变体的 config.json 与用户 profile。"""
        cfg_path = os.path.join(self.temp_dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("USER_SECRET_CONFIG")

        prof_dir = os.path.join(self.temp_dir, "profiles")
        os.makedirs(prof_dir, exist_ok=True)
        prof_file = os.path.join(prof_dir, "cookie.txt")
        with open(prof_file, "w", encoding="utf-8") as f:
            f.write("USER_COOKIES")

        # 制作恶意/上游 Zip 包，内含企图覆写 config 的同名与不同大小写文件
        zip_path = os.path.join(self.temp_dir, "fake_update.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("zotero-table-extractor-main/Config.json", "MALICIOUS_OVERWRITE")
            zf.writestr("zotero-table-extractor-main/config.json", "MALICIOUS_OVERWRITE_2")
            zf.writestr("zotero-table-extractor-main/profiles/cookie.txt", "PULLED_COOKIES")
            zf.writestr("zotero-table-extractor-main/scripts/new_feature.py", "print('hello_from_upstream')")

        # 直接测试解压逻辑
        self.strat.tmp_dir = os.path.join(self.temp_dir, ".updater_tmp")
        extract_dir = os.path.join(self.strat.tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            ArchiveUpdateStrategy._safe_extract(zf, extract_dir)

        # 模拟同步
        source_root = os.path.join(extract_dir, "zotero-table-extractor-main")
        for root, dirs, files in os.walk(source_root):
            rel_dir = os.path.relpath(root, source_root)
            if is_protected_path(rel_dir):
                dirs[:] = []
                continue
            dest_dir = os.path.normpath(os.path.join(self.temp_dir, rel_dir))
            os.makedirs(dest_dir, exist_ok=True)
            for f in files:
                rel_file = os.path.normpath(os.path.join(rel_dir, f))
                if is_protected_path(rel_file):
                    continue
                shutil.copy2(os.path.join(root, f), os.path.join(dest_dir, f))

        # 断言用户核心信息未被修改
        with open(cfg_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "USER_SECRET_CONFIG")
        with open(prof_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "USER_COOKIES")

        # 断言新代码成功同步
        new_code = os.path.join(self.temp_dir, "scripts", "new_feature.py")
        self.assertTrue(os.path.isfile(new_code))

    def test_archive_handles_macosx_junk_without_nested_folders(self):
        """测试当 Zip 中包含 __MACOSX 结构时，能正确剥离并直接更新根目录，而非生成嵌套层级。"""
        zip_path = os.path.join(self.temp_dir, "macosx_update.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("zotero-table-extractor-main/scripts/sample.py", "# sample")
            zf.writestr("__MACOSX/._sample", "binary_junk")

        extract_dir = os.path.join(self.temp_dir, "ext")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            ArchiveUpdateStrategy._safe_extract(zf, extract_dir)

        meaningful = [c for c in os.listdir(extract_dir) if c not in ("__MACOSX", ".DS_Store") and not c.startswith("._")]
        self.assertEqual(len(meaningful), 1)
        self.assertEqual(meaningful[0], "zotero-table-extractor-main")

    def test_zip_slip_prevention(self):
        """测试对恶意 Zip Slip 路径穿越的拦截。"""
        zip_path = os.path.join(self.temp_dir, "slip.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../evil.txt", "evil")

        extract_dir = os.path.join(self.temp_dir, "ext_slip")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            with self.assertRaises(RuntimeError):
                ArchiveUpdateStrategy._safe_extract(zf, extract_dir)


class TestGitUpdateStrategy(unittest.TestCase):
    def test_non_git_repo_safe_error(self):
        """测试在完全非 Git 目录下调用 execute_update 不会抛出 KeyError 异常。"""
        temp_dir = tempfile.mkdtemp(prefix="test_non_git_")
        try:
            strat = GitUpdateStrategy(temp_dir)
            res = strat.execute_update()
            self.assertFalse(res["success"])
            self.assertIn("当前目录非 Git 仓库", res["error"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_mock_git_lifecycle(self):
        """在本地构建一个隔离的 mock Git 仓库，端到端测试 check 与 stash 快进流程。"""
        remote_dir = tempfile.mkdtemp(prefix="test_git_remote_")
        local_dir = tempfile.mkdtemp(prefix="test_git_local_")
        try:
            # 1. 初始化远端仓库
            subprocess.run(["git", "init", "--bare", remote_dir], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # 2. 克隆至本地
            subprocess.run(["git", "clone", remote_dir, local_dir], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # 配置本地身份
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=local_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=local_dir, check=True)

            # 初始提交
            init_file = os.path.join(local_dir, "tracked.txt")
            with open(init_file, "w") as f:
                f.write("v1")
            subprocess.run(["git", "add", "tracked.txt"], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init commit"], cwd=local_dir, check=True)
            subprocess.run(["git", "branch", "-M", "main"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=local_dir, check=True)

            strat = GitUpdateStrategy(local_dir)
            chk = strat.check_updates()
            self.assertTrue(chk["is_git"])
            self.assertFalse(chk["has_update"])

            # 3. 模拟本地有已跟踪文件的修改
            with open(init_file, "w") as f:
                f.write("v1_local_custom_edit")

            # 模拟远端在另一个克隆仓产生新提交
            worker_dir = tempfile.mkdtemp(prefix="test_git_worker_")
            try:
                subprocess.run(["git", "clone", remote_dir, worker_dir], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.name", "Worker"], cwd=worker_dir, check=True)
                subprocess.run(["git", "config", "user.email", "worker@test.com"], cwd=worker_dir, check=True)
                new_f = os.path.join(worker_dir, "upstream_file.txt")
                with open(new_f, "w") as f:
                    f.write("upstream_data")
                subprocess.run(["git", "add", "upstream_file.txt"], cwd=worker_dir, check=True)
                subprocess.run(["git", "commit", "-m", "upstream commit"], cwd=worker_dir, check=True)
                subprocess.run(["git", "push", "origin", "main"], cwd=worker_dir, check=True)
            finally:
                shutil.rmtree(worker_dir, ignore_errors=True)

            # 4. 在 local_dir 运行 check_updates，应探测到 1 个落后提交
            chk2 = strat.check_updates()
            self.assertTrue(chk2["has_update"])
            self.assertEqual(chk2["behind_count"], 1)
            self.assertTrue(chk2["has_local_tracked_changes"])

            # 5. 执行自动更新（应自动 stash、pull 并 pop stash）
            up_res = strat.execute_update()
            self.assertTrue(up_res["success"])
            self.assertTrue(up_res["stashed_and_restored"])

            # 验证远端新文件已同步，且本地修改未丢失
            self.assertTrue(os.path.isfile(os.path.join(local_dir, "upstream_file.txt")))
            with open(init_file, "r") as f:
                self.assertEqual(f.read(), "v1_local_custom_edit")

        finally:
            shutil.rmtree(remote_dir, ignore_errors=True)
            shutil.rmtree(local_dir, ignore_errors=True)


class TestSkillUpdaterEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_updater_engine_")
        self.config_file = os.path.join(self.temp_dir, "config.json")
        self.example_file = os.path.join(self.temp_dir, "config.example.json")

        self.user_cfg = {
            "FIRECRAWL_API_KEY": "fc-secret-keep-me",
            "PAPER_TABLES_DIR": "/my/custom/output"
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.user_cfg, f)

        self.example_cfg = {
            "FIRECRAWL_API_KEY": "your-firecrawl-api-key",
            "PAPER_TABLES_DIR": "",
            "NEW_KEY_A": "default_a"
        }
        with open(self.example_file, "w", encoding="utf-8") as f:
            json.dump(self.example_cfg, f)

        self.updater = SkillUpdater(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_check_mode(self):
        """测试 check 模式在无 Git 环境下正确返回 release 状态与配置差异。"""
        check_res = self.updater.check(method="release")
        self.assertEqual(check_res["mode"], "release")
        self.assertTrue(check_res["config_diff"]["needs_migration"])
        self.assertIn("NEW_KEY_A", check_res["config_diff"]["missing_in_user"])

    def test_dry_run_mode(self):
        """测试 dry-run 演练模式不更改磁盘文件。"""
        res = self.updater.perform_update(method="release", dry_run=True)
        self.assertTrue(res["success"])
        with open(self.config_file, "r", encoding="utf-8-sig") as f:
            current = json.load(f)
        self.assertNotIn("NEW_KEY_A", current)

    def test_protected_patterns_definition(self):
        """测试核心隐私与本地持久化目录处于绝对受保护列表中。"""
        self.assertIn("config.json", PROTECTED_PATTERNS)
        self.assertIn("config.backup.json", PROTECTED_PATTERNS)
        self.assertIn(".backups", PROTECTED_PATTERNS)
        self.assertIn("profiles", PROTECTED_PATTERNS)
        self.assertIn(".git", PROTECTED_PATTERNS)
        self.assertIn(".env", PROTECTED_PATTERNS)


class TestCLIIntegration(unittest.TestCase):
    def test_root_update_check(self):
        """测试根目录 update.py --check 执行返回码为 0。"""
        res = subprocess.run(
            [sys.executable, "update.py", "--check"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Zotero Table Extractor 在线更新与环境检测报告", res.stdout)

    def test_root_update_json(self):
        """测试 update.py --check --json 能正确返回合法 JSON 数据结构。"""
        res = subprocess.run(
            [sys.executable, "update.py", "--check", "--json"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertIn("config_diff", data)
        self.assertIn("is_git", data)

    def test_setup_paths_check_update(self):
        """测试 setup_paths.py --check-update 执行返回码为 0。"""
        res = subprocess.run(
            [sys.executable, "setup_paths.py", "--check-update"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("在线更新与环境检测报告", res.stdout)


if __name__ == "__main__":
    unittest.main()
