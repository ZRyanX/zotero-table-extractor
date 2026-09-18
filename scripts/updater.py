#!/usr/bin/env python3
"""
scripts/updater.py — Zotero Table Extractor 技能在线更新与个人配置绝对防护引擎。

核心设计哲学：
1. 【零丢失原则】：用户的 API 密钥、自定义路径（Zotero 数据目录、输出目录、浏览器 Profile 等）
   在任何更新（Git pull、Rebase、Release 解压、强制拉取）操作下绝对不被覆盖或清空。
2. 【智能双向合并】：当上游更新引入新配置项或新字段时，以 config.example.json 为基准，
   自动将新配置项安全补充进 config.json，同时 100% 继承并保留用户所有既有配置。
3. 【全自动快照与回滚】：每次更新或配置迁移前自动创建时间戳备份，记录变更元数据（Manifest），
   并提供一键式 --rollback 回滚能力（支持代码版本与配置回滚）。
4. 【双模在线更新】：
   - Git 模式：针对本地 Git 仓库克隆，支持自动探测分支/追踪上游、安全工作区暂存（Stash）、
     快进合并（Fast-Forward Merge）或安全同步；
   - Release 归档模式：针对免 Git 的 ZIP/解压包部署环境，支持通过 GitHub Releases/Archive
     自动下载最新安全包，实施严格白名单覆盖更新，防范 Zip Slip 攻击，严密隔离敏感与本地文件。
5. 【跨平台原生支持】：macOS (Apple Silicon / Intel)、Windows 10/11、Linux 统一切换适配。
"""

import os
import sys
import json
import shutil
import fnmatch
import platform
import subprocess
import hashlib
import tempfile
import zipfile
import urllib.request
import urllib.error
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 终端 ANSI 彩色显示（兼容 Windows 虚拟终端转义）
# ---------------------------------------------------------------------------
if sys.platform.startswith("win"):
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

COLOR_RESET = "\033[0m"
COLOR_BOLD = "\033[1m"
COLOR_GREEN = "\033[32m"
COLOR_BLUE = "\033[34m"
COLOR_CYAN = "\033[36m"
COLOR_YELLOW = "\033[33m"
COLOR_RED = "\033[31m"
COLOR_GRAY = "\033[90m"
COLOR_MAGENTA = "\033[35m"

def c_print(msg: str, color: str = ""):
    """带颜色的安全打印。"""
    if color:
        print(f"{color}{msg}{COLOR_RESET}")
    else:
        print(msg)


# ---------------------------------------------------------------------------
# 基础常量与目录定位
# ---------------------------------------------------------------------------
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.dirname(CURRENT_DIR) if os.path.basename(CURRENT_DIR) == "scripts" else CURRENT_DIR)

DEFAULT_REPO_URL = "https://github.com/ZRyanX/zotero-table-extractor.git"
GITHUB_API_LATEST_RELEASE = "https://api.github.com/repos/ZRyanX/zotero-table-extractor/releases/latest"
GITHUB_ARCHIVE_MAIN_ZIP = "https://github.com/ZRyanX/zotero-table-extractor/archive/refs/heads/main.zip"
DEFAULT_BRANCH = "main"

# 绝对受保护的文件与目录集合（Release 模式严禁覆盖，跨平台大小写不敏感匹配）
PROTECTED_PATTERNS = [
    "config.json",
    "config.backup.json",
    ".backups",
    "backups",
    ".updater_tmp",
    "profiles",
    ".zotero_playwright_profile",
    ".zotero_chrome_debug_profile",
    "scratch",
    ".git",
    ".env",
    "*.local.json",
    "*.backup.json",
    "*.bak",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "__pycache__",
]

PROTECTED_DIR_NAMES = {
    ".backups",
    "backups",
    ".updater_tmp",
    "profiles",
    ".zotero_playwright_profile",
    ".zotero_chrome_debug_profile",
    "scratch",
    ".git",
    "__pycache__",
}


def is_protected_path(rel_path: str) -> bool:
    """
    判断相对路径是否命中受保护列表。
    跨平台特性：
    1. 统一归一化路径分隔符；
    2. 全流程大小写不敏感匹配（杜绝 macOS/Windows 大小写混用如 Config.json 导致的覆写风险）；
    3. 支持各级父目录命中与文件名通配符模式命中。
    """
    if not rel_path or rel_path in (".", ""):
        return False

    norm = os.path.normpath(rel_path)
    parts = norm.lower().replace("\\", "/").split("/")

    # 1. 检查各级目录是否属于受保护目录
    for p in parts[:-1]:
        if p in PROTECTED_DIR_NAMES:
            return True

    # 检查最后一级是否是受保护目录名
    if parts[-1] in PROTECTED_DIR_NAMES:
        return True

    # 2. 检查文件名本身
    fname = parts[-1]
    if fname in ("config.json", "config.backup.json", ".env"):
        return True
    if fname.startswith(".env."):
        return True

    file_glob_patterns = [
        "*.local.json",
        "*.backup.json",
        "*.bak",
        "*.db",
        "*.sqlite*",
        "*.log",
        "*.tmp",
        "*.tmp_*",
    ]
    for pattern in file_glob_patterns:
        if fnmatch.fnmatch(fname, pattern):
            return True

    return False


# ---------------------------------------------------------------------------
# 1. 配置快照、持久化与回滚管理器 (ConfigBackupManager)
# ---------------------------------------------------------------------------
class ConfigBackupManager:
    """负责 config.json 的原子写入、自动多版本快照生成、Manifest 索引维护及精准回滚。"""

    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = os.path.abspath(root_dir)
        self.config_path = os.path.join(self.root_dir, "config.json")
        self.example_path = os.path.join(self.root_dir, "config.example.json")
        self.quick_backup_path = os.path.join(self.root_dir, "config.backup.json")
        self.backup_dir = os.path.join(self.root_dir, ".backups")
        self.manifest_path = os.path.join(self.backup_dir, "manifest.json")
        os.makedirs(self.backup_dir, exist_ok=True)

    def _load_manifest(self) -> Dict[str, Any]:
        if os.path.isfile(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"version": "1.0", "backups": []}

    def _save_manifest(self, data: Dict[str, Any]):
        manifest_tmp = self.manifest_path + f".tmp_{os.getpid()}"
        try:
            with open(manifest_tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(manifest_tmp, self.manifest_path)
        except Exception as e:
            c_print(f"警告：写入备份索引 manifest.json 失败: {e}", COLOR_YELLOW)
        finally:
            if os.path.exists(manifest_tmp):
                try:
                    os.remove(manifest_tmp)
                except Exception:
                    pass

    def create_backup(self, reason: str = "manual", extra_meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """在修改前创建 config.json 的全量时间戳备份，并更新快捷备份 config.backup.json。"""
        if not os.path.isfile(self.config_path):
            return None

        try:
            with open(self.config_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
                try:
                    config_data = json.loads(content)
                except Exception:
                    config_data = {}
        except Exception as e:
            c_print(f"警告：现有 config.json 存在读取解析异常 ({e})，将以原始内容创建紧急快照", COLOR_YELLOW)
            content = ""
            config_data = {}

        # 包含微秒以防在 FAT/ExFAT 等粗粒度文件系统上同秒创建时哈希文件名冲突
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_id = f"backup_{timestamp_str}_{reason}"
        backup_filename = f"config_{timestamp_str}_{reason}.json"
        backup_file_path = os.path.join(self.backup_dir, backup_filename)

        try:
            # 写入时间戳快照文件
            with open(backup_file_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            # 同时更新单文件快速回滚快照 config.backup.json
            quick_tmp = self.quick_backup_path + f".tmp_{os.getpid()}"
            with open(quick_tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(quick_tmp, self.quick_backup_path)

            # 计算校验哈希
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            entry = {
                "id": backup_id,
                "timestamp": datetime.now().isoformat(),
                "reason": reason,
                "filename": backup_filename,
                "file_path": os.path.abspath(backup_file_path),
                "keys_count": len(config_data) if isinstance(config_data, dict) else 0,
                "sha256": content_hash,
                "extra": extra_meta or {}
            }

            manifest = self._load_manifest()
            manifest.setdefault("backups", []).append(entry)
            # 最多保留 30 个历史快照，自动清理多余超期文件
            if len(manifest["backups"]) > 30:
                to_remove = manifest["backups"][:-30]
                manifest["backups"] = manifest["backups"][-30:]
                for item in to_remove:
                    try:
                        old_p = item.get("file_path")
                        if old_p and os.path.isfile(old_p):
                            os.remove(old_p)
                    except Exception:
                        pass
            self._save_manifest(manifest)

            return entry
        except Exception as e:
            c_print(f"创建配置备份失败: {e}", COLOR_RED)
            return None

    def list_backups(self) -> List[Dict[str, Any]]:
        """获取所有已记录且实体存在的历史配置备份列表（按时间倒序排列）。"""
        manifest = self._load_manifest()
        manifest_backups = manifest.get("backups", [])
        backups = []

        seen_paths = set()
        for b in manifest_backups:
            fpath = b.get("file_path")
            if fpath and os.path.isfile(fpath):
                backups.append(b)
                seen_paths.add(os.path.abspath(fpath))

        # 若 manifest 为空或遗漏了 .backups 目录下的文件，进行动态自愈发现
        if os.path.isdir(self.backup_dir):
            for fname in os.listdir(self.backup_dir):
                if fname.startswith("config_") and fname.endswith(".json"):
                    fpath = os.path.abspath(os.path.join(self.backup_dir, fname))
                    if fpath not in seen_paths and os.path.isfile(fpath):
                        backups.append({
                            "id": fname[:-5],
                            "timestamp": datetime.fromtimestamp(os.path.getmtime(fpath)).isoformat(),
                            "reason": "discovered",
                            "filename": fname,
                            "file_path": fpath,
                            "keys_count": 0,
                            "sha256": "",
                            "extra": {}
                        })
                        seen_paths.add(fpath)

        backups.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return backups

    def restore_backup(self, backup_id: Optional[str] = "latest") -> Dict[str, Any]:
        """将 config.json 精准回滚至指定或最近一次历史快照。"""
        backups = self.list_backups()
        if not backups:
            # 尝试回退至根目录的 config.backup.json
            if os.path.isfile(self.quick_backup_path):
                target_path = self.quick_backup_path
                target_meta = {"id": "config.backup.json", "file_path": target_path}
            else:
                raise FileNotFoundError("未在 .backups 目录或项目根目录下找到任何可用的配置备份文件。")
        else:
            if not backup_id or backup_id == "latest":
                # 默认回滚至最近一次非 pre_rollback 检查点的快照，防止连续 rollback 自我反转
                candidates = [b for b in backups if not b.get("reason", "").startswith("pre_rollback")]
                target_meta = candidates[0] if candidates else backups[0]
            else:
                matched = [b for b in backups if b.get("id") == backup_id or b.get("filename") == backup_id]
                if not matched:
                    # 支持前缀或部分 ID 匹配
                    matched = [b for b in backups if backup_id in b.get("id", "") or backup_id in b.get("filename", "")]
                if not matched:
                    raise ValueError(f"未找到匹配 ID 为 '{backup_id}' 的备份快照。可通过 --list-backups 查看可用快照。")
                target_meta = matched[0]
            target_path = target_meta.get("file_path")

        if not target_path or not os.path.isfile(target_path):
            raise FileNotFoundError(f"备份文件实体不存在：{target_path}")

        # 校验目标备份是否为合法 JSON
        with open(target_path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
            restored_data = json.loads(raw)

        # 在恢复前，为当前可能损坏/变更的状态也留存一份快照，防止误操作无法挽回
        pre_rollback_entry = None
        if os.path.isfile(self.config_path):
            pre_rollback_entry = self.create_backup(reason="pre_rollback")

        # 原子恢复写入 config.json
        tmp_target = self.config_path + f".tmp_restore_{os.getpid()}"
        try:
            with open(tmp_target, "w", encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_target, self.config_path)
        finally:
            if os.path.exists(tmp_target):
                try:
                    os.remove(tmp_target)
                except Exception:
                    pass

        return {
            "success": True,
            "restored_from": target_path,
            "backup_id": target_meta.get("id"),
            "pre_rollback_backup_id": pre_rollback_entry.get("id") if pre_rollback_entry else None,
            "keys_count": len(restored_data) if isinstance(restored_data, dict) else 0
        }


# ---------------------------------------------------------------------------
# 2. 智能配置合并与迁移引擎 (ConfigMerger)
# ---------------------------------------------------------------------------
class ConfigMerger:
    """
    负责在更新后将上游最新的 config.example.json 智能融入用户的 config.json。
    严格保障：
    1. 用户既有配置（API Keys、路径、开关等）值绝不被覆盖或置空；
    2. 上游新增的配置项自动填入；
    3. 识别出占位符并智能规避伪凭据写入；
    4. 用户自定义附加字段绝对保留；
    5. 原子写回并执行后置一致性校验。
    """

    SENSITIVE_KEY_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    PATH_KEY_SUBSTRINGS = ("DIR", "PATH", "URL", "SCRIPT")

    @classmethod
    def is_sensitive(cls, key: str) -> bool:
        k = key.upper()
        return any(s in k for s in cls.SENSITIVE_KEY_SUBSTRINGS)

    @classmethod
    def is_path(cls, key: str) -> bool:
        k = key.upper()
        return any(p in k for p in cls.PATH_KEY_SUBSTRINGS)

    @classmethod
    def mask_value(cls, key: str, val: Any) -> str:
        """安全脱敏显示敏感内容。"""
        if val is None or val == "":
            return f"{COLOR_GRAY}[未设置]{COLOR_RESET}"
        s_val = str(val)
        if cls.is_sensitive(key):
            if cls.is_template_placeholder(s_val):
                return f"{COLOR_YELLOW}[模板占位符: {s_val}]{COLOR_RESET}"
            if len(s_val) > 10:
                masked = f"{s_val[:4]}...{s_val[-4:]}"
            elif len(s_val) > 4:
                masked = f"{s_val[:2]}***{s_val[-2:]}"
            else:
                masked = "***"
            return f"{COLOR_GREEN}{masked}{COLOR_RESET}"
        return f"{COLOR_CYAN}{s_val}{COLOR_RESET}"

    @classmethod
    def is_template_placeholder(cls, val: Any) -> bool:
        if isinstance(val, str):
            v_low = val.strip().lower()
            if not v_low:
                return False
            if v_low.startswith(("your-", "your_", "example-", "example_", "insert_", "enter_")):
                return True
            if (v_low.startswith("<") and v_low.endswith(">")) or (v_low.startswith("[") and v_low.endswith("]")):
                return True
            if any(kw in v_low for kw in ("placeholder", "replace_me", "change_me", "your_api_key", "your-api-key", "todo")):
                return True
        return False

    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = os.path.abspath(root_dir)
        self.config_path = os.path.join(self.root_dir, "config.json")
        self.example_path = os.path.join(self.root_dir, "config.example.json")
        self.backup_mgr = ConfigBackupManager(self.root_dir)

    def load_configs(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """读取用户配置与示例配置，具备容错与自动恢复机制。"""
        user_cfg = {}
        example_cfg = {}

        if os.path.isfile(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8-sig") as f:
                    content = f.read().strip()
                    if content:
                        user_cfg = json.loads(content)
            except Exception as e:
                c_print(f"警告：读取现有 config.json 失败 ({e})，正在尝试从快速备份或历史快照自愈恢复...", COLOR_YELLOW)
                quick_bak = os.path.join(self.root_dir, "config.backup.json")
                if os.path.isfile(quick_bak):
                    try:
                        with open(quick_bak, "r", encoding="utf-8-sig") as f:
                            user_cfg = json.load(f)
                        c_print(f"[✓] 已从 config.backup.json 成功自愈恢复配置内容", COLOR_GREEN)
                    except Exception:
                        pass

        if os.path.isfile(self.example_path):
            try:
                with open(self.example_path, "r", encoding="utf-8-sig") as f:
                    example_cfg = json.load(f)
            except Exception as e:
                c_print(f"警告：读取 config.example.json 模板失败: {e}", COLOR_YELLOW)

        return user_cfg, example_cfg

    def analyze_diff(self, user_cfg: Dict[str, Any], example_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """深入分析当前用户配置与上游模板的差异。"""
        existing_keys = set(user_cfg.keys())
        example_keys = set(example_cfg.keys())

        missing_in_user = [k for k in example_cfg.keys() if k not in existing_keys]
        extra_in_user = [k for k in user_cfg.keys() if k not in example_keys]
        common_keys = [k for k in example_cfg.keys() if k in existing_keys]

        configured_sensitive = []
        for k in user_cfg:
            if self.is_sensitive(k) and user_cfg[k] and not self.is_template_placeholder(user_cfg[k]):
                configured_sensitive.append(k)

        configured_paths = []
        for k in user_cfg:
            if self.is_path(k) and user_cfg[k]:
                configured_paths.append(k)

        return {
            "total_user_keys": len(user_cfg),
            "total_example_keys": len(example_cfg),
            "missing_in_user": missing_in_user,
            "extra_in_user": extra_in_user,
            "common_keys": common_keys,
            "configured_sensitive": configured_sensitive,
            "configured_paths": configured_paths,
            "needs_migration": len(missing_in_user) > 0 or not os.path.isfile(self.config_path)
        }

    def merge_configs(self, user_cfg: Dict[str, Any], example_cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        核心智能合并逻辑：
        1. 严格保留 user_cfg 中的每一个键与每一个已有值；
        2. 将 example_cfg 中存在但 user_cfg 中缺失的字段补齐；
           如果模板中是占位符 (如 your-firecrawl-api-key)，则置为标准空字符串 ""，防止发往 API 引发未认证误伤；
           如果模板中是默认配置参数 (如数值、布尔值、内部模型名称)，则安全采纳默认值；
        3. 保留用户自定义但不在模板中的所有扩展键；
        4. 维持优化的排版顺序（以模板为骨架，末尾附加用户私有扩展键）。
        """
        merged = {}
        added_keys = []
        preserved_keys = []
        custom_keys = []

        # 1. 按照 example_cfg 的组织顺序遍历排布
        for k, default_val in example_cfg.items():
            if k in user_cfg:
                # 严格使用用户原有配置，绝不改动
                merged[k] = user_cfg[k]
                preserved_keys.append(k)
            else:
                # 用户缺失的新配置项，注入安全默认值
                if self.is_template_placeholder(default_val):
                    merged[k] = ""
                else:
                    merged[k] = default_val
                added_keys.append(k)

        # 2. 追加用户独有的自定义配置项
        for k, user_val in user_cfg.items():
            if k not in merged:
                merged[k] = user_val
                custom_keys.append(k)

        # 3. 后置一致性双重校验：确保原有 user_cfg 的每一对键值在 merged 中严格相等
        for k, orig_v in user_cfg.items():
            if merged.get(k) != orig_v:
                raise RuntimeError(f"配置合并完整性校验失败：键 '{k}' 原有值遭意外修改！已紧急熔断！")

        report = {
            "preserved_count": len(preserved_keys),
            "preserved_keys": preserved_keys,
            "added_count": len(added_keys),
            "added_keys": added_keys,
            "custom_count": len(custom_keys),
            "custom_keys": custom_keys,
            "total_keys": len(merged)
        }
        return merged, report

    def safe_merge_and_save(self, create_backup: bool = True) -> Dict[str, Any]:
        """执行端到端安全合并、校验、备份与原子持久化。"""
        user_cfg, example_cfg = self.load_configs()

        if not example_cfg:
            return {"success": False, "error": "未找到 config.example.json 模板文件"}

        backup_info = None
        if create_backup and os.path.isfile(self.config_path):
            backup_info = self.backup_mgr.create_backup(reason="pre_config_merge")

        merged_cfg, report = self.merge_configs(user_cfg, example_cfg)

        # 原子写入 config.json
        tmp_file = self.config_path + f".tmp_{os.getpid()}"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(merged_cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.config_path)
        except Exception as e:
            raise IOError(f"原子写入 config.json 失败: {e}")
        finally:
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except Exception:
                    pass

        report["success"] = True
        report["backup"] = backup_info
        return report


# ---------------------------------------------------------------------------
# 3. Git 在线更新策略执行器 (GitUpdateStrategy)
# ---------------------------------------------------------------------------
class GitUpdateStrategy:
    """针对标准 Git 克隆部署环境的更新控制器。"""

    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = os.path.abspath(root_dir)

    def _run_git(self, args: List[str], timeout: int = 30) -> Tuple[int, str, str]:
        try:
            p = subprocess.run(
                ["git"] + args,
                cwd=self.root_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout
            )
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except Exception as e:
            return -1, "", str(e)

    def is_git_repo(self) -> bool:
        return os.path.isdir(os.path.join(self.root_dir, ".git"))

    def check_updates(self, branch: Optional[str] = None) -> Dict[str, Any]:
        """连接远端仓库并检测是否有最新提交。"""
        if not self.is_git_repo():
            return {"is_git": False, "error": "当前目录非 Git 仓库"}

        # 获取当前所在分支
        code, current_branch, _ = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        if code != 0:
            return {"is_git": True, "error": "无法解析当前 Git 分支状态"}

        # 获取本地当前 commit
        _, local_commit, _ = self._run_git(["rev-parse", "--short", "HEAD"])
        if not local_commit:
            local_commit = "unknown"

        # 处理游离 HEAD 状态
        is_detached = (current_branch == "HEAD")

        # 探测目标 remote 与 branch
        target_remote = "origin"
        target_branch = branch
        if not target_branch:
            # 尝试获取当前分支的上游追踪分支 (例如 origin/main)
            code_u, upstream_ref, _ = self._run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
            if code_u == 0 and "/" in upstream_ref:
                parts = upstream_ref.split("/", 1)
                target_remote = parts[0]
                target_branch = parts[1]
            elif not is_detached:
                target_branch = current_branch
            else:
                target_branch = DEFAULT_BRANCH

        # 获取远端状态
        fetch_code, _, fetch_err = self._run_git(["fetch", target_remote, target_branch, "--tags", "--prune"], timeout=20)
        if fetch_code != 0:
            return {
                "is_git": True,
                "network_error": True,
                "current_branch": current_branch,
                "target_branch": target_branch,
                "target_remote": target_remote,
                "local_commit": local_commit,
                "is_detached": is_detached,
                "error": f"无法连接 Git 远端 ({target_remote}/{target_branch}) 进行 fetch: {fetch_err}"
            }

        # 检查落后与超前计数
        target_ref = f"{target_remote}/{target_branch}"
        code, counts, _ = self._run_git(["rev-list", "--left-right", "--count", f"HEAD...{target_ref}"])
        ahead = 0
        behind = 0
        if code == 0 and counts:
            parts = counts.split()
            if len(parts) >= 2:
                try:
                    ahead = int(parts[0])
                    behind = int(parts[1])
                except ValueError:
                    pass

        # 获取远端最新 commit
        _, remote_commit, _ = self._run_git(["rev-parse", "--short", target_ref])

        # 获取最新变更的日志摘要
        incoming_commits = []
        if behind > 0:
            _, log_out, _ = self._run_git(["log", f"HEAD..{target_ref}", "--oneline", "-n", "10"])
            if log_out:
                incoming_commits = log_out.splitlines()

        # 检查本地是否有跟踪文件的未提交改动
        _, status_out, _ = self._run_git(["status", "--porcelain"])
        modified_tracked = []
        if status_out:
            for line in status_out.splitlines():
                st = line[:2]
                fname = line[3:].strip()
                if not st.startswith("??"):
                    modified_tracked.append(fname)

        return {
            "is_git": True,
            "has_update": behind > 0,
            "current_branch": current_branch,
            "target_branch": target_branch,
            "target_remote": target_remote,
            "local_commit": local_commit,
            "remote_commit": remote_commit or "unknown",
            "ahead_count": ahead,
            "behind_count": behind,
            "is_detached": is_detached,
            "incoming_commits": incoming_commits,
            "has_local_tracked_changes": len(modified_tracked) > 0,
            "modified_tracked_files": modified_tracked
        }

    def execute_update(self, branch: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        """
        执行 Git 更新管线：
        1. 健全性检查（处理非 Git 环境、错误与游离 HEAD）；
        2. 若本地跟踪文件存在改动：自动执行安全的临时 git stash 暂存；
        3. 执行 git pull --ff-only origin <branch> 快进同步；
        4. 恢复本地暂存；
        5. 报告更新执行状态与 commit 迁移。
        """
        status_info = self.check_updates(branch)
        if not status_info.get("is_git"):
            return {"success": False, "error": status_info.get("error", "当前目录非 Git 仓库")}

        if status_info.get("network_error"):
            return {"success": False, "error": status_info.get("error", "Git 远端连接失败")}

        if "error" in status_info and not status_info.get("has_update"):
            return {"success": False, "error": status_info["error"]}

        local_commit = status_info.get("local_commit", "unknown")
        target_remote = status_info.get("target_remote", "origin")
        target_branch = status_info.get("target_branch", branch or DEFAULT_BRANCH)
        target_ref = f"{target_remote}/{target_branch}"

        if not status_info.get("has_update") and not force:
            return {
                "success": True,
                "already_latest": True,
                "local_commit": local_commit,
                "message": "当前已是最新版本，无需拉取代码。"
            }

        if status_info.get("is_detached") and not force:
            return {
                "success": False,
                "error": "当前工作区处于 detached HEAD (游离提交) 状态。为防代码意外分叉丢失，请先执行 git checkout <branch> 切换回目标分支，或使用 --force 强制覆盖。"
            }

        stashed = False
        stash_msg = f"zotero_extractor_updater_autostash_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        if status_info.get("has_local_tracked_changes"):
            if force:
                c_print("[!] --force 启用：正在丢弃本地已跟踪文件的未提交修改...", COLOR_YELLOW)
                self._run_git(["reset", "--hard", "HEAD"])
            else:
                c_print(f"[*] 检测到本地存在 {len(status_info['modified_tracked_files'])} 个已跟踪文件的改动，正在执行安全自动暂存...", COLOR_CYAN)
                c, _, s_err = self._run_git(["stash", "push", "-m", stash_msg])
                if c == 0:
                    stashed = True
                else:
                    return {"success": False, "error": f"创建本地暂存失败: {s_err}，已中止更新以防代码冲突"}

        # 尝试安全快进拉取
        pull_code, pull_out, pull_err = self._run_git(["pull", "--ff-only", target_remote, target_branch])

        if pull_code != 0:
            if force:
                c_print("[!] 快进拉取遇到分叉，正在以 --force 模式同步至最新远端分支...", COLOR_YELLOW)
                self._run_git(["reset", "--hard", target_ref])
            else:
                if stashed:
                    self._run_git(["stash", "pop"])
                return {
                    "success": False,
                    "error": f"Git 快进拉取 (ff-only) 失败: {pull_err}。\n"
                             "可能本地提交与远端存在分叉，您可以尝试使用 --force 重新更新，或手动执行 git pull/git merge。"
                }

        # 恢复先前被暂存的修改
        stash_conflict = False
        if stashed:
            c_print("[*] 正在恢复更新前的本地工作区暂存...", COLOR_CYAN)
            pop_code, _, pop_err = self._run_git(["stash", "pop"])
            if pop_code != 0:
                stash_conflict = True
                c_print(f"[!] 警告：恢复本地暂存时产生轻微冲突或需手动解决：{pop_err}。修改仍保存在 git stash 栈中，未有代码丢失。", COLOR_YELLOW)

        # 获取更新后的 commit
        _, updated_commit, _ = self._run_git(["rev-parse", "--short", "HEAD"])

        return {
            "success": True,
            "old_commit": local_commit,
            "new_commit": updated_commit or "unknown",
            "stashed_and_restored": stashed,
            "stash_conflict": stash_conflict,
            "incoming_commits": status_info.get("incoming_commits", [])
        }


# ---------------------------------------------------------------------------
# 4. Release / Archive 归档下载更新策略执行器 (ArchiveUpdateStrategy)
# ---------------------------------------------------------------------------
class ArchiveUpdateStrategy:
    """针对无 Git 环境（或下载 ZIP 部署）的归档包在线拉取更新执行器。"""

    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = os.path.abspath(root_dir)
        self.tmp_dir = os.path.join(self.root_dir, ".updater_tmp")

    def check_release(self) -> Dict[str, Any]:
        """查询 GitHub API 获取最新的 Release 信息。"""
        headers = {"User-Agent": "ZoteroTableExtractor-Updater"}
        try:
            req = urllib.request.Request(GITHUB_API_LATEST_RELEASE, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "available": True,
                    "tag_name": data.get("tag_name"),
                    "name": data.get("name"),
                    "zipball_url": data.get("zipball_url") or GITHUB_ARCHIVE_MAIN_ZIP,
                    "published_at": data.get("published_at"),
                    "body": data.get("body", "")
                }
        except Exception as e:
            return {
                "available": True,
                "tag_name": "main-latest",
                "name": "GitHub main branch archive",
                "zipball_url": GITHUB_ARCHIVE_MAIN_ZIP,
                "fallback": True,
                "note": f"Release API 探测备选 ({e})，可直接从主分支归档更新"
            }

    @staticmethod
    def _safe_extract(zf: zipfile.ZipFile, dest_dir: str):
        """严密防范 Zip Slip 跨目录穿越攻击的安全解压。"""
        dest_abs = os.path.abspath(dest_dir)
        for member in zf.infolist():
            target_path = os.path.abspath(os.path.join(dest_dir, member.filename))
            if not (target_path == dest_abs or target_path.startswith(dest_abs + os.sep)):
                raise RuntimeError(f"Zip 包含非法路径穿越项: {member.filename}")
            zf.extract(member, dest_dir)

    def execute_update(self, download_url: Optional[str] = None) -> Dict[str, Any]:
        """
        下载并解压最新归档包，执行安全白名单替换：
        - 坚决屏蔽 config.json, config.backup.json, .backups, profiles, .git, scratch 等；
        - 防范 Zip Slip 攻击；
        - 正确识别并剥离 GitHub 归档顶层文件夹（自动过滤 __MACOSX / .DS_Store）；
        - 更新核心代码与配置模板。
        """
        target_url = download_url or GITHUB_ARCHIVE_MAIN_ZIP
        os.makedirs(self.tmp_dir, exist_ok=True)
        zip_path = os.path.join(self.tmp_dir, "latest_update.zip")
        extract_dir = os.path.join(self.tmp_dir, "extracted")

        try:
            c_print(f"[*] 正在从 GitHub 下载最新版本包: {target_url}...", COLOR_CYAN)
            headers = {"User-Agent": "ZoteroTableExtractor-Updater"}
            req = urllib.request.Request(target_url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, "wb") as out_f:
                shutil.copyfileobj(resp, out_f)

            c_print("[*] 下载完成，正在解压与准备文件替换校验...", COLOR_CYAN)
            if os.path.isdir(extract_dir):
                shutil.rmtree(extract_dir)
            os.makedirs(extract_dir, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zf:
                self._safe_extract(zf, extract_dir)

            # GitHub 归档通常包裹在一级子文件夹中（如 zotero-table-extractor-main/）
            # 过滤掉 macOS 元数据目录与隐藏文件
            meaningful_children = [
                c for c in os.listdir(extract_dir)
                if c not in ("__MACOSX", ".DS_Store") and not c.startswith("._")
            ]
            if len(meaningful_children) == 1 and os.path.isdir(os.path.join(extract_dir, meaningful_children[0])):
                source_root = os.path.join(extract_dir, meaningful_children[0])
            else:
                source_root = extract_dir

            # 遍历并安全同步文件
            updated_files = []
            for root, dirs, files in os.walk(source_root):
                rel_dir = os.path.relpath(root, source_root)

                # 检查目录是否命中保护目录（跨平台大小写不敏感）
                if is_protected_path(rel_dir):
                    dirs[:] = []
                    continue

                dest_dir = os.path.normpath(os.path.join(self.root_dir, rel_dir))
                os.makedirs(dest_dir, exist_ok=True)

                for f in files:
                    # 过滤系统元数据垃圾
                    if f in (".DS_Store", "Thumbs.db") or f.startswith("._"):
                        continue

                    rel_file = os.path.normpath(os.path.join(rel_dir, f))
                    if is_protected_path(rel_file):
                        continue

                    src_f = os.path.join(root, f)
                    dest_f = os.path.join(dest_dir, f)

                    # 若目标已存在且同为大型模型权重文件且已有内容，避免无谓重写
                    if f.endswith((".onnx", ".bin", ".pt", ".safetensors")) and os.path.isfile(dest_f) and os.path.getsize(dest_f) > 1000000:
                        continue

                    # 原子覆写
                    dest_tmp = dest_f + f".tmp_{os.getpid()}"
                    try:
                        shutil.copy2(src_f, dest_tmp)
                        os.replace(dest_tmp, dest_f)
                        updated_files.append(rel_file)
                    finally:
                        if os.path.exists(dest_tmp):
                            try:
                                os.remove(dest_tmp)
                            except Exception:
                                pass

            return {
                "success": True,
                "updated_files_count": len(updated_files),
                "sample_files": updated_files[:10]
            }

        except Exception as e:
            return {"success": False, "error": f"归档更新失败: {e}"}
        finally:
            try:
                if os.path.isdir(self.tmp_dir):
                    shutil.rmtree(self.tmp_dir)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 5. 依赖包版本智能核对器 (DependencyChecker)
# ---------------------------------------------------------------------------
class DependencyChecker:
    """检测 requirements.txt 变更并提供一键式升级。"""

    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = os.path.abspath(root_dir)
        self.req_path = os.path.join(self.root_dir, "requirements.txt")

    def install_requirements(self, uv_index_url: Optional[str] = None) -> bool:
        if not os.path.isfile(self.req_path):
            return True

        cmd = [sys.executable, "-m", "pip", "install", "-r", self.req_path]
        if uv_index_url:
            cmd += ["-i", uv_index_url]

        c_print(f"[*] 正在自动安装/更新 Python 运行依赖: {' '.join(cmd)}", COLOR_CYAN)
        try:
            p = subprocess.run(cmd, cwd=self.root_dir, text=True)
            return p.returncode == 0
        except Exception as e:
            c_print(f"[!] 依赖自动安装出错: {e}", COLOR_YELLOW)
            return False


# ---------------------------------------------------------------------------
# 6. 统一技能更新与配置保护门面控制器 (SkillUpdater)
# ---------------------------------------------------------------------------
class SkillUpdater:
    """技能总控器：串联检测、备份、更新、配置合并与回滚。"""

    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = os.path.abspath(root_dir)
        self.backup_mgr = ConfigBackupManager(self.root_dir)
        self.merger = ConfigMerger(self.root_dir)
        self.git_strategy = GitUpdateStrategy(self.root_dir)
        self.archive_strategy = ArchiveUpdateStrategy(self.root_dir)
        self.dep_checker = DependencyChecker(self.root_dir)

    def check(self, method: str = "auto") -> Dict[str, Any]:
        """全面检查更新状态、缺失配置项与环境状态。"""
        user_cfg, example_cfg = self.merger.load_configs()
        cfg_diff = self.merger.analyze_diff(user_cfg, example_cfg)

        is_git = self.git_strategy.is_git_repo()
        selected_method = method
        if method == "auto":
            selected_method = "git" if is_git else "release"

        res = {
            "mode": selected_method,
            "is_git": is_git,
            "config_diff": cfg_diff,
            "has_update": False
        }

        if selected_method == "git" and is_git:
            git_status = self.git_strategy.check_updates()
            res["git_status"] = git_status
            res["has_update"] = git_status.get("has_update", False)
        else:
            rel_status = self.archive_strategy.check_release()
            res["release_status"] = rel_status
            res["has_update"] = rel_status.get("available", False)

        return res

    def perform_update(
        self,
        method: str = "auto",
        force: bool = False,
        dry_run: bool = False,
        install_deps: bool = False
    ) -> Dict[str, Any]:
        """
        执行标准更新流程：
        【第 1 步】预检现有配置有效性，生成第一道强力防护快照；
        【第 2 步】执行 Git 快进拉取或 Release 归档覆盖；
        【第 3 步】提取最新模板，执行智能配置合并（严格继承所有 API 路径与密钥）；
        【第 4 步】双重断言一致性自检；
        【第 5 步】(可选) 更新 Python 依赖。
        """
        c_print("\n" + "═" * 72, COLOR_BLUE)
        c_print("   🛡️  Zotero Table Extractor 在线安全更新引擎启动", COLOR_BOLD + COLOR_GREEN)
        c_print("═" * 72, COLOR_BLUE)

        if dry_run:
            c_print("[!] 当前处于 --dry-run 演练模式，将仅进行流程模拟，不写入任何磁盘改动。", COLOR_YELLOW)

        # 1. 预备份
        pre_backup = None
        if not dry_run:
            c_print("\n[阶段 1/4] 正在对当前本地配置建立完整快照与指纹防护...", COLOR_CYAN)
            pre_backup = self.backup_mgr.create_backup(reason="pre_update")
            if pre_backup:
                c_print(f"  [✓] 已生成时间戳快照: {pre_backup['filename']}", COLOR_GREEN)
                c_print(f"  [✓] 包含当前已有配置项: {pre_backup['keys_count']} 项", COLOR_GREEN)
            else:
                c_print("  [*] 当前未检测到已有 config.json，更新后将自动根据模板初始化", COLOR_GRAY)

        # 2. 拉取代码更新
        c_print("\n[阶段 2/4] 正在检测并拉取远端核心代码更新...", COLOR_CYAN)
        is_git = self.git_strategy.is_git_repo()
        actual_method = method
        if method == "auto":
            actual_method = "git" if is_git else "release"
        elif method == "git" and not is_git:
            c_print("[!] 指定了 git 模式但当前目录非 Git 仓库，自动降级为 release 归档模式", COLOR_YELLOW)
            actual_method = "release"

        update_res = {}
        if dry_run:
            update_res = {"success": True, "dry_run": True, "message": "演练通过"}
        elif actual_method == "git":
            update_res = self.git_strategy.execute_update(force=force)
            if not update_res.get("success"):
                c_print(f"\n[✗] 代码更新中断: {update_res.get('error')}", COLOR_RED)
                return update_res
            if update_res.get("already_latest"):
                c_print(f"  [✓] 代码已为最新 commit ({update_res.get('local_commit')})", COLOR_GREEN)
            else:
                c_print(f"  [✓] 代码成功同步: {update_res.get('old_commit')} ➔ {update_res.get('new_commit')}", COLOR_BOLD + COLOR_GREEN)
        else:
            update_res = self.archive_strategy.execute_update()
            if not update_res.get("success"):
                c_print(f"\n[✗] 归档下载覆盖失败: {update_res.get('error')}", COLOR_RED)
                return update_res
            c_print(f"  [✓] 归档安全同步完成，共更新 {update_res.get('updated_files_count')} 个文件", COLOR_GREEN)

        # 3. 智能配置合并
        c_print("\n[阶段 3/4] 正在执行智能配置合并（严格保障 API 路径与个人密钥 0 覆盖）...", COLOR_CYAN)
        merge_report = {}
        if dry_run:
            user_cfg, ex_cfg = self.merger.load_configs()
            _, merge_report = self.merger.merge_configs(user_cfg, ex_cfg)
            c_print("  [演练结果] 模拟合并完成，预期保留项目完整。", COLOR_GREEN)
        else:
            merge_report = self.merger.safe_merge_and_save(create_backup=True)
            c_print(f"  [✓] 100% 成功保留并迁移已有配置: {merge_report['preserved_count']} 项", COLOR_BOLD + COLOR_GREEN)
            if merge_report["added_count"] > 0:
                c_print(f"  [✓] 智能补全新增配置项 ({merge_report['added_count']} 项): {', '.join(merge_report['added_keys'])}", COLOR_GREEN)
            if merge_report["custom_count"] > 0:
                c_print(f"  [✓] 完整保留用户扩展项 ({merge_report['custom_count']} 项): {', '.join(merge_report['custom_keys'])}", COLOR_GREEN)

        # 4. 依赖项检查与安装
        c_print("\n[阶段 4/4] 正在验证环境依赖与安装包...", COLOR_CYAN)
        if install_deps and not dry_run:
            user_cfg, _ = self.merger.load_configs()
            uv_url = user_cfg.get("UV_INDEX_URL")
            self.dep_checker.install_requirements(uv_index_url=uv_url)
        else:
            c_print("  [*] 跳过依赖自动安装（可使用 --install-deps 进行依赖自动同步）", COLOR_GRAY)

        # 5. 打印敏感凭证与关键路径保留证明（全量动态搜寻并列出所有敏感与路径键）
        c_print("\n" + "─" * 72, COLOR_BLUE)
        c_print("【用户关键隐私信息防护核验单】", COLOR_BOLD + COLOR_CYAN)
        c_print("─" * 72, COLOR_BLUE)
        final_cfg, _ = self.merger.load_configs()
        priority_keys = [
            "FIRECRAWL_API_KEY",
            "ELSEVIER_API_KEY",
            "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN",
            "LLM_API_KEY",
            "PAPER_TABLES_DIR",
            "ZOTERO_DIR",
            "ZOTERO_DB_PATH",
            "ZOTERO_STORAGE_DIR",
            "PLAYWRIGHT_USER_DATA_DIR",
            "PLAYWRIGHT_USER_DATA_DIR_MAC",
            "PLAYWRIGHT_USER_DATA_DIR_WINDOWS",
        ]
        tracked_keys = []
        for k in priority_keys:
            if k in final_cfg and k not in tracked_keys:
                tracked_keys.append(k)
        for k in sorted(final_cfg.keys()):
            if (ConfigMerger.is_sensitive(k) or ConfigMerger.is_path(k)) and k not in tracked_keys:
                tracked_keys.append(k)

        for k in tracked_keys:
            val_display = ConfigMerger.mask_value(k, final_cfg[k])
            c_print(f"  • {k:<36}: {val_display}")

        c_print("═" * 72, COLOR_BLUE)
        c_print("🎉 恭喜！技能在线更新已顺利完成，所有既有私有配置与密钥毫发无损！", COLOR_BOLD + COLOR_GREEN)
        c_print("═" * 72, COLOR_BLUE)

        return {
            "success": True,
            "pre_backup": pre_backup,
            "update_res": update_res,
            "merge_report": merge_report
        }

    def rollback(self, backup_id: Optional[str] = "latest") -> Dict[str, Any]:
        """执行配置快照回滚。"""
        c_print(f"[*] 正在尝试回滚至配置快照: {backup_id or 'latest'}...", COLOR_CYAN)
        res = self.backup_mgr.restore_backup(backup_id=backup_id)
        c_print(f"[✓] 已成功回滚 config.json！源快照: {res['restored_from']}", COLOR_GREEN)
        c_print(f"[✓] 恢复后的配置项总数: {res['keys_count']} 项", COLOR_GREEN)
        if res.get("pre_rollback_backup_id"):
            c_print(f"[i] 回滚前状态已留存备份 ({res['pre_rollback_backup_id']})。若需撤销本次回滚，可随时执行: python update.py --rollback {res['pre_rollback_backup_id']}", COLOR_GRAY)
        return res


# ---------------------------------------------------------------------------
# 7. CLI 命令入口与交互处理
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Zotero Table Extractor 技能在线更新与个人配置绝对防护引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
常用示例:
  python update.py                          # 一键在线安全更新并智能合并配置
  python update.py --check                  # 仅检查远端更新与缺失配置项
  python update.py --dry-run                # 演练模式模拟更新
  python update.py --rollback               # 回滚至最近一次备份的配置
  python update.py --rollback <backup_id>   # 回滚至指定 ID 的历史快照
  python update.py --list-backups           # 查看所有历史快照
  python update.py --merge-only             # 仅安全同步 example 模板新字段至 config.json
  python update.py --install-deps           # 更新代码并自动升级依赖
        """
    )
    parser.add_argument("--check", action="store_true", help="仅检测更新状态与缺失字段，不执行任何写入")
    parser.add_argument("--update", action="store_true", help="执行在线更新与配置合并（默认行为）")
    parser.add_argument("--dry-run", action="store_true", help="模拟演练更新全流程，不修改文件")
    parser.add_argument("--force", action="store_true", help="强制更新（跳过分叉警告/覆盖已跟踪代码文件）")
    parser.add_argument("--rollback", nargs="?", const="latest", help="回滚 config.json 至最近或指定 ID 的快照")
    parser.add_argument("--list-backups", action="store_true", help="列出所有可用的历史配置快照")
    parser.add_argument("--merge-only", action="store_true", help="仅将 config.example.json 中的新项安全并入 config.json")
    parser.add_argument("--install-deps", action="store_true", help="更新后自动运行 pip install -r requirements.txt")
    parser.add_argument("--method", choices=["auto", "git", "release"], default="auto", help="指定更新方式 (默认 auto)")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出结果，便于 Agent 与脚本集成")

    args = parser.parse_args()
    updater = SkillUpdater(ROOT_DIR)

    # 1. 查看快照
    if args.list_backups:
        backups = updater.backup_mgr.list_backups()
        if args.json:
            print(json.dumps({"backups": backups}, indent=2, ensure_ascii=False))
            return
        c_print("\n【历史配置快照清单】", COLOR_BOLD + COLOR_CYAN)
        if not backups:
            c_print("  当前暂无历史快照记录。", COLOR_GRAY)
            return
        for idx, b in enumerate(backups, 1):
            c_print(f"  [{idx}] ID: {b.get('id')} | 时间: {b.get('timestamp')} | 原因: {b.get('reason')} | 键数: {b.get('keys_count')}")
            c_print(f"      路径: {b.get('file_path')}", COLOR_GRAY)
        return

    # 2. 回滚配置
    if args.rollback is not None:
        try:
            res = updater.rollback(args.rollback)
            if args.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
        except Exception as e:
            c_print(f"[✗] 回滚失败: {e}", COLOR_RED)
            sys.exit(1)
        return

    # 3. 仅合并配置项
    if args.merge_only:
        res = updater.merger.safe_merge_and_save(create_backup=True)
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            c_print(f"[✓] 配置模板合并完毕！保留项: {res['preserved_count']}, 新增项: {res['added_count']}", COLOR_GREEN)
            if res["added_keys"]:
                c_print(f"    新增项列表: {', '.join(res['added_keys'])}", COLOR_CYAN)
        return

    # 4. 检测更新
    if args.check:
        check_res = updater.check(method=args.method)
        if args.json:
            print(json.dumps(check_res, indent=2, ensure_ascii=False))
            return

        c_print("=" * 72, COLOR_BLUE)
        c_print("  🔍 Zotero Table Extractor 在线更新与环境检测报告", COLOR_BOLD + COLOR_CYAN)
        c_print("=" * 72, COLOR_BLUE)

        diff = check_res["config_diff"]
        c_print(f"• 本地配置文件 (config.json)      : {diff['total_user_keys']} 项已配置")
        c_print(f"• 官方最新模板 (config.example.json): {diff['total_example_keys']} 项标准参数")

        if diff["missing_in_user"]:
            c_print(f"  [!] 本地缺失的新参数 ({len(diff['missing_in_user'])} 个): {', '.join(diff['missing_in_user'])}", COLOR_YELLOW)
            c_print("      建议运行 updater 进行一键安全平滑迁移（原有私有配置 100% 保留）", COLOR_CYAN)
        else:
            c_print("  [✓] 本地配置字段完全齐备，无需迁移补齐", COLOR_GREEN)

        if check_res.get("is_git"):
            git_st = check_res.get("git_status", {})
            c_print(f"• Git 当前分支: {git_st.get('current_branch')} | 本地 commit: {git_st.get('local_commit')}")
            if git_st.get("network_error"):
                c_print(f"  [!] 网络检测异常: {git_st.get('error')}", COLOR_YELLOW)
            elif git_st.get("has_update"):
                c_print(f"  [★] 发现远端最新代码！落后远端 {git_st.get('behind_count')} 个提交 (远端: {git_st.get('remote_commit')})", COLOR_BOLD + COLOR_GREEN)
                for c in git_st.get("incoming_commits", [])[:5]:
                    c_print(f"      - {c}", COLOR_CYAN)
            else:
                c_print("  [✓] 本地代码已是远端最新版本", COLOR_GREEN)
        else:
            rel_st = check_res.get("release_status", {})
            c_print(f"• 归档版本: {rel_st.get('tag_name')} ({rel_st.get('name')})")
            if rel_st.get("available"):
                c_print("  [✓] 可通过 GitHub Release / Archive 归档进行安全更新", COLOR_GREEN)

        c_print("=" * 72, COLOR_BLUE)
        return

    # 5. 默认执行更新流程
    res = updater.perform_update(
        method=args.method,
        force=args.force,
        dry_run=args.dry_run,
        install_deps=args.install_deps
    )

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))

    if not res.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
