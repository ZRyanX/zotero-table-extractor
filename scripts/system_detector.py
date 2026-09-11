#!/usr/bin/env python3
"""
system_detector.py — 跨平台系统环境检测与路径自适应适配模块。

功能：
1. 检测当前运行操作系统 (macOS / Windows / Linux) 与硬件环境；
2. 保护 macOS 既有路径与环境配置完全不变；
3. 为 Windows 系统提供与 macOS 语义等价的映射路径、环境编码与执行环境；
4. 提供自适应路径转换器 (adapt_path) 与统一环境初始化函数 (setup_environment)；
5. 提供可执行 CLI 诊断工具与代码自动调整能力 (--apply)。
"""

import os
import sys
import glob
import json
import platform
import tempfile
from typing import Dict, Any, Optional, List, Tuple


# ---------------------------------------------------------------------------
# 1. 核心系统环境标识与判定
# ---------------------------------------------------------------------------
SYSTEM = platform.system()               # 'Darwin', 'Windows', 'Linux'
OS_TYPE = SYSTEM.lower()                 # 'darwin', 'windows', 'linux'
IS_MACOS = (SYSTEM == "Darwin")
IS_WINDOWS = (SYSTEM == "Windows")
IS_LINUX = (SYSTEM == "Linux")
MACHINE = platform.machine()             # 'arm64', 'x86_64', 'AMD64'
PYTHON_VERSION = sys.version.split()[0]
PYTHON_EXECUTABLE = sys.executable

_MACOS_USER_HOME = os.path.expanduser("~")
_MACOS_AIHUB_ROOT = "/Volumes/ExFat/AIHub"
_MACOS_PAPER_TABLES = "/Volumes/ExFat/AIHub/paper_tables"
_MACOS_ZOTERO_DIR = os.path.join(_MACOS_USER_HOME, "Zotero")
_MACOS_ZOTERO_DB = os.path.join(_MACOS_ZOTERO_DIR, "zotero.sqlite")
_MACOS_ZOTERO_STORAGE = os.path.join(_MACOS_ZOTERO_DIR, "storage")
_MACOS_AGENT_REASONING = "/Volumes/ExFat/AIHub/paper_tables/.agent_reasoning"
_MACOS_SUPP_DOWNLOADER = "/Volumes/ExFat/AIHub/skills/journal-supp-downloader/scripts/journal_downloader.py"
_MACOS_AUDIT_DB = "/Volumes/ExFat/AIHub/paper_tables/audit_inspection_8h.db"
_MACOS_TARGETED_AUDIT_DB = "/Volumes/ExFat/AIHub/paper_tables/targeted_audit_inspection_8h.db"
_MACOS_CHROME_PROFILE = os.path.join(_MACOS_USER_HOME, ".zotero_chrome_debug_profile")
_MACOS_PLAYWRIGHT_PROFILE = os.path.join(_MACOS_USER_HOME, ".zotero_playwright_profile")


# ---------------------------------------------------------------------------
# 2. Windows 环境自适应路径探测
# ---------------------------------------------------------------------------
def get_user_home() -> str:
    """获取当前用户主目录（Windows: USERPROFILE, macOS/Linux: HOME）。"""
    if IS_WINDOWS:
        prof = os.environ.get("USERPROFILE")
        if prof:
            return prof
        home = os.path.expanduser("~")
        if not home.startswith("/"):
            return home
        # 兼容模拟或缺省状态：构建标准的 Windows 用户主目录
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "User"
        return f"C:\\Users\\{user}"
    return os.path.expanduser("~")


def get_skill_root() -> str:
    """获取当前技能根目录 (zotero-table-extractor)。"""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def find_windows_aihub_root() -> str:
    """
    在 Windows 下定位 AIHub 根目录。
    优先级：
    1. 环境变量 AIHUB_ROOT
    2. config.json 中的 AIHUB_ROOT / PAPER_TABLES_DIR 父级
    3. 基于当前技能路径向上推导 (skills/zotero-table-extractor -> 上上级目录)
    4. 常见 Windows 盘符探测 (D:\\AIHub, E:\\AIHub, F:\\AIHub, C:\\AIHub)
    5. 当前用户家目录 (%USERPROFILE%\\AIHub)
    """
    if os.environ.get("AIHUB_ROOT"):
        cand = os.path.abspath(os.environ["AIHUB_ROOT"])
        if os.path.isdir(cand):
            return cand

    # 从技能所在目录向上推导 (e.g. D:\AIHub\skills\zotero-table-extractor -> D:\AIHub)
    skill_root = get_skill_root()
    skills_dir = os.path.dirname(skill_root)
    if os.path.basename(skills_dir).lower() == "skills":
        probable_aihub = os.path.dirname(skills_dir)
        # Windows 路径不应以 /Volumes/ 开头
        if not probable_aihub.startswith("/Volumes/") and (
            os.path.basename(probable_aihub).lower() == "aihub" or os.path.isdir(os.path.join(probable_aihub, "paper_tables"))
        ):
            return probable_aihub

    # 盘符候选探测
    drive_candidates = [
        r"D:\AIHub",
        r"E:\AIHub",
        r"F:\AIHub",
        r"C:\AIHub",
        os.path.join(get_user_home(), "AIHub"),
    ]
    for cand in drive_candidates:
        if os.path.isdir(cand):
            return cand

    # 兜底返回 D:\AIHub 或 用户目录\AIHub
    if os.path.exists("D:\\"):
        return r"D:\AIHub"
    return os.path.join(get_user_home(), "AIHub")


# ---------------------------------------------------------------------------
# 3. 跨平台语义路径获取函数 (支持 config.json / 环境变量自定义覆盖)
# ---------------------------------------------------------------------------
def _read_config_dict() -> Dict[str, Any]:
    """读取 config.json 中的自定义配置。"""
    try:
        cfg_path = os.path.join(get_skill_root(), "config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _get_path_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """获取自定义路径设置，优先环境变量，其次 config.json，最后默认值。"""
    env_val = os.environ.get(key)
    if env_val:
        return os.path.normpath(os.path.expanduser(env_val))
    cfg = _read_config_dict()
    val = cfg.get(key)
    if val and isinstance(val, str) and val.strip():
        return os.path.normpath(os.path.expanduser(val.strip()))
    return default


def get_aihub_root() -> str:
    """获取 AIHub 根目录。"""
    custom = _get_path_setting("AIHUB_ROOT")
    if custom:
        return custom
    if IS_MACOS and os.path.isdir(_MACOS_AIHUB_ROOT):
        return _MACOS_AIHUB_ROOT
    return find_windows_aihub_root()


def get_paper_tables_dir() -> str:
    """获取 paper_tables 根目录。优先级：config.json/环境变量 > macOS历史路径 > 用户主目录。"""
    custom = _get_path_setting("PAPER_TABLES_DIR")
    if custom:
        return custom
    if IS_MACOS and os.path.isdir(_MACOS_PAPER_TABLES):
        return _MACOS_PAPER_TABLES
    # Windows/通用优先使用 AIHub/paper_tables，若无则使用 用户目录/paper_tables
    aihub_tables = os.path.join(get_aihub_root(), "paper_tables")
    if os.path.isdir(aihub_tables):
        return aihub_tables
    return os.path.join(get_user_home(), "paper_tables")


def get_zotero_dir() -> str:
    """获取 Zotero 数据主目录。优先级：config.json > 默认 ~/Zotero"""
    custom = _get_path_setting("ZOTERO_DIR")
    if custom:
        return custom
    if IS_MACOS and os.path.isdir(_MACOS_ZOTERO_DIR):
        return _MACOS_ZOTERO_DIR
    return os.path.join(get_user_home(), "Zotero")


def get_zotero_db_path() -> str:
    """获取 zotero.sqlite 路径。优先级：config.json > <zotero_dir>/zotero.sqlite"""
    custom = _get_path_setting("ZOTERO_DB_PATH")
    if custom:
        return custom
    cand = os.path.join(get_zotero_dir(), "zotero.sqlite")
    if os.path.isfile(cand):
        return cand
    if IS_MACOS and os.path.isfile(_MACOS_ZOTERO_DB):
        return _MACOS_ZOTERO_DB
    return cand


def get_zotero_storage_dir() -> str:
    """获取 Zotero storage 目录。优先级：config.json > <zotero_dir>/storage"""
    custom = _get_path_setting("ZOTERO_STORAGE_DIR")
    if custom:
        return custom
    cand = os.path.join(get_zotero_dir(), "storage")
    if os.path.isdir(cand):
        return cand
    if IS_MACOS and os.path.isdir(_MACOS_ZOTERO_STORAGE):
        return _MACOS_ZOTERO_STORAGE
    return cand


def get_agent_reasoning_dir() -> str:
    """获取 .agent_reasoning 目录。"""
    return os.path.join(get_paper_tables_dir(), ".agent_reasoning")


def get_journal_downloader_script() -> str:
    """获取 journal-supp-downloader 技能主脚本路径。"""
    custom = _get_path_setting("SUPP_DOWNLOADER_SCRIPT")
    if custom and os.path.isfile(custom):
        return custom

    # 1. 相对路径推导（任何系统最可靠方式）
    rel_cand = os.path.abspath(
        os.path.join(get_skill_root(), "..", "journal-supp-downloader", "scripts", "journal_downloader.py")
    )
    if os.path.isfile(rel_cand):
        return rel_cand

    if IS_MACOS and os.path.isfile(_MACOS_SUPP_DOWNLOADER):
        return _MACOS_SUPP_DOWNLOADER

    # Windows 候选路径
    candidates = [
        os.path.join(get_aihub_root(), "skills", "journal-supp-downloader", "scripts", "journal_downloader.py"),
        os.path.join(get_user_home(), ".gemini", "antigravity-cli", "skills", "journal-supp-downloader", "scripts", "journal_downloader.py"),
        os.path.join(get_user_home(), ".gemini", "config", "skills", "journal-supp-downloader", "scripts", "journal_downloader.py"),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return candidates[0]


def get_audit_db_path(filename: str = "audit_inspection_8h.db") -> str:
    """获取审计数据库路径。"""
    return os.path.join(get_paper_tables_dir(), filename)


def get_targeted_audit_db_path() -> str:
    """获取定向审计数据库路径。"""
    return os.path.join(get_paper_tables_dir(), "targeted_audit_inspection_8h.db")


def get_chrome_debug_profile_dir() -> str:
    """获取 Chrome 调试 Profile 目录。"""
    custom = _get_path_setting("CHROME_DEBUG_PROFILE_DIR")
    if custom:
        return custom
    if IS_MACOS and os.path.isdir(_MACOS_CHROME_PROFILE):
        return _MACOS_CHROME_PROFILE
    return os.path.join(get_user_home(), ".zotero_chrome_debug_profile")


def get_playwright_profile_dir() -> str:
    """获取 Playwright 用户数据目录。"""
    custom = _get_path_setting("PLAYWRIGHT_USER_DATA_DIR")
    if custom:
        return custom
    if IS_MACOS and os.path.isdir(_MACOS_PLAYWRIGHT_PROFILE):
        return _MACOS_PLAYWRIGHT_PROFILE
    return os.path.join(get_user_home(), ".zotero_playwright_profile")


# ---------------------------------------------------------------------------
# 4. 通用路径智能映射器 (Universal Path Adapter)
# ---------------------------------------------------------------------------
def adapt_path(path: str) -> str:
    """
    通用路径自适应映射：
    - 在 macOS 下：100% 保持入参的原样字符串返回，绝不作变动；
    - 在 Windows 下：自动将 macOS 格式的硬编码路径映射至对应的 Windows 本地路径。
    """
    if not path or not isinstance(path, str):
        return path

    if IS_MACOS:
        return path

    # Windows 映射逻辑
    norm = path.replace("\\", "/")

    # 1. /Volumes/ExFat/AIHub/paper_tables/...
    if norm.startswith("/Volumes/ExFat/AIHub/paper_tables"):
        sub = norm[len("/Volumes/ExFat/AIHub/paper_tables"):].lstrip("/")
        base = get_paper_tables_dir()
        return os.path.normpath(os.path.join(base, sub)) if sub else base

    # 2. /Volumes/ExFat/AIHub/...
    if norm.startswith("/Volumes/ExFat/AIHub"):
        sub = norm[len("/Volumes/ExFat/AIHub"):].lstrip("/")
        base = get_aihub_root()
        return os.path.normpath(os.path.join(base, sub)) if sub else base

    # 3. Zotero path adapt
    if "/Zotero" in norm and norm.startswith("/Users/"):
        parts = norm.split("/Zotero", 1)
        sub = parts[1].lstrip("/") if len(parts) > 1 else ""
        base = get_zotero_dir()
        return os.path.normpath(os.path.join(base, sub)) if sub else base

    # 4. User home path adapt
    if norm.startswith("/Users/"):
        parts = norm.split("/", 3)
        sub = parts[3] if len(parts) > 3 else ""
        base = get_user_home()
        return os.path.normpath(os.path.join(base, sub))

    # 5. 普通 POSIX 路径转 Windows 路径
    if norm.startswith("/") and len(norm) > 2 and norm[2] == "/":
        drive = norm[1].upper()
        sub = norm[2:].replace("/", "\\")
        return f"{drive}:{sub}"

    return os.path.normpath(path)


# ---------------------------------------------------------------------------
# 5. 跨平台运行环境自动化配置 (Environment Setup)
# ---------------------------------------------------------------------------
def setup_environment():
    """
    配置并修复操作系统环境：
    - Windows: 强制配置控制台 UTF-8、消除 gbk 乱码、设置环境变量。
    - macOS: 保持原生设置。
    """
    if IS_WINDOWS:
        # 1. 修复控制台与标准输出编码
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

        # 2. 注入全局 UTF-8 环境变量
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        os.environ.setdefault("PYTHONLEGACYWINDOWSSTDIO", "0")

        # 3. 确保必要的 Windows 工作目录存在
        try:
            os.makedirs(get_paper_tables_dir(), exist_ok=True)
            os.makedirs(get_agent_reasoning_dir(), exist_ok=True)
        except Exception:
            pass


# 模块导入时自动执行基础环境适配
setup_environment()


# ---------------------------------------------------------------------------
# 6. 代码文件自动扫描与自适应重构工具 (Auto Code Adjuster)
# ---------------------------------------------------------------------------
def get_scripts_dir() -> str:
    """获取 scripts 目录绝对路径。"""
    return os.path.abspath(os.path.dirname(__file__))


def scan_and_adjust_file(file_path: str, apply_changes: bool = False) -> List[str]:
    """
    扫描单个 Python 脚本文件，检测其中的 macOS 硬编码路径，
    并在 apply_changes=True 时自动重组为动态系统自适应写法。
    """
    changes = []
    if not os.path.isfile(file_path):
        return changes

    filename = os.path.basename(file_path)
    if filename in ("system_detector.py", "env_detector.py"):
        return changes

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return [f"读取失败 {file_path}: {e}"]

    original = content
    modified = content

    # 1. 检查是否含有 macOS 独有硬编码
    has_macos_hardcode = (
        "/Volumes/ExFat/AIHub" in modified or
        "/Zotero/storage" in modified or
        "/Zotero" in modified
    )

    if not has_macos_hardcode:
        return changes

    # 针对不同文件进行精确定向适配：
    if filename == "batch_run.py":
        target = '_LEGACY_PAPER_TABLES = "/Volumes/ExFat/AIHub/paper_tables"  # macOS 历史默认'
        replacement = '_LEGACY_PAPER_TABLES = get_paper_tables_dir()  # macOS 保持历史默认，Windows 自动适配'
        if target in modified:
            modified = modified.replace(target, replacement)
            changes.append("batch_run.py: 将 _LEGACY_PAPER_TABLES 重定向为 get_paper_tables_dir()")

    elif filename == "agent_bridge.py":
        target = 'legacy = "/Volumes/ExFat/AIHub/paper_tables/.agent_reasoning"'
        replacement = 'legacy = adapt_path("/Volumes/ExFat/AIHub/paper_tables/.agent_reasoning")'
        if target in modified:
            modified = modified.replace(target, replacement)
            changes.append("agent_bridge.py: 将 legacy 路径使用 adapt_path 封装自适应")

    elif filename == "extract_zotero_table.py":
        target = '"/Volumes/ExFat/AIHub/skills/journal-supp-downloader/scripts/journal_downloader.py",'
        replacement = 'get_journal_downloader_script(),'
        if target in modified:
            modified = modified.replace(target, replacement)
            changes.append("extract_zotero_table.py: 将附表下载脚本重定向为 get_journal_downloader_script()")

    elif filename == "verify_tables.py":
        target = 'default="/Volumes/ExFat/AIHub/paper_tables"'
        replacement = 'default=get_paper_tables_dir()'
        if target in modified:
            modified = modified.replace(target, replacement)
            changes.append("verify_tables.py: 将 --root 默认值重定向为 get_paper_tables_dir()")

    elif filename in ("exhaustive_audit_runner.py", "targeted_audit_runner.py"):
        if 'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/audit_inspection_8h.db"' in modified:
            modified = modified.replace(
                'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/audit_inspection_8h.db"',
                'DB_PATH = get_audit_db_path("audit_inspection_8h.db")'
            )
            changes.append(f"{filename}: 将 DB_PATH 重定向为 get_audit_db_path()")
        if 'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/targeted_audit_inspection_8h.db"' in modified:
            modified = modified.replace(
                'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/targeted_audit_inspection_8h.db"',
                'DB_PATH = get_targeted_audit_db_path()'
            )
            changes.append(f"{filename}: 将 DB_PATH 重定向为 get_targeted_audit_db_path()")
        if 'OUTPUT_BASE_DIR = "/Volumes/ExFat/AIHub/paper_tables"' in modified:
            modified = modified.replace(
                'OUTPUT_BASE_DIR = "/Volumes/ExFat/AIHub/paper_tables"',
                'OUTPUT_BASE_DIR = get_paper_tables_dir()'
            )
            changes.append(f"{filename}: 将 OUTPUT_BASE_DIR 重定向为 get_paper_tables_dir()")
        if 'STORAGE_DIR =' in modified and '/Zotero/storage"' in modified:
            modified = re.sub(
                r'STORAGE_DIR = ".*?/Zotero/storage"',
                'STORAGE_DIR = get_zotero_storage_dir()',
                modified
            )
            changes.append(f"{filename}: 将 STORAGE_DIR 重定向为 get_zotero_storage_dir()")
        if 'broad_db = "/Volumes/ExFat/AIHub/paper_tables/audit_inspection_8h.db"' in modified:
            modified = modified.replace(
                'broad_db = "/Volumes/ExFat/AIHub/paper_tables/audit_inspection_8h.db"',
                'broad_db = get_audit_db_path("audit_inspection_8h.db")'
            )
            changes.append(f"{filename}: 将 broad_db 重定向为 get_audit_db_path()")

    elif filename == "deep_8h_iterative_healer.py":
        if 'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/targeted_audit_inspection_8h.db"' in modified:
            modified = modified.replace(
                'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/targeted_audit_inspection_8h.db"',
                'DB_PATH = get_targeted_audit_db_path()'
            )
            changes.append("deep_8h_iterative_healer.py: 将 DB_PATH 重定向为 get_targeted_audit_db_path()")
        if 'OUTPUT_BASE_DIR = "/Volumes/ExFat/AIHub/paper_tables"' in modified:
            modified = modified.replace(
                'OUTPUT_BASE_DIR = "/Volumes/ExFat/AIHub/paper_tables"',
                'OUTPUT_BASE_DIR = get_paper_tables_dir()'
            )
            changes.append("deep_8h_iterative_healer.py: 将 OUTPUT_BASE_DIR 重定向为 get_paper_tables_dir()")

    elif filename in ("audit_reporter.py", "targeted_audit_reporter.py"):
        if 'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/audit_inspection_8h.db"' in modified:
            modified = modified.replace(
                'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/audit_inspection_8h.db"',
                'DB_PATH = get_audit_db_path("audit_inspection_8h.db")'
            )
            changes.append(f"{filename}: 将 DB_PATH 重定向为 get_audit_db_path()")
        if 'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/targeted_audit_inspection_8h.db"' in modified:
            modified = modified.replace(
                'DB_PATH = "/Volumes/ExFat/AIHub/paper_tables/targeted_audit_inspection_8h.db"',
                'DB_PATH = get_targeted_audit_db_path()'
            )
            changes.append(f"{filename}: 将 DB_PATH 重定向为 get_targeted_audit_db_path()")

    # 若发生改动且尚未包含导入，在顶部注入 import 语句
    if changes and "system_detector" not in modified:
        import_stmt = (
            "try:\n"
            "    from .system_detector import (\n"
            "        IS_MACOS, IS_WINDOWS, get_paper_tables_dir, get_zotero_db_path,\n"
            "        get_zotero_storage_dir, get_journal_downloader_script,\n"
            "        get_agent_reasoning_dir, get_audit_db_path, get_targeted_audit_db_path, adapt_path\n"
            "    )\n"
            "except ImportError:\n"
            "    from system_detector import (\n"
            "        IS_MACOS, IS_WINDOWS, get_paper_tables_dir, get_zotero_db_path,\n"
            "        get_zotero_storage_dir, get_journal_downloader_script,\n"
            "        get_agent_reasoning_dir, get_audit_db_path, get_targeted_audit_db_path, adapt_path\n"
            "    )\n"
        )
        lines = modified.split("\n")
        insert_idx = 0
        in_docstring = False
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                if not in_docstring:
                    in_docstring = True
                    if len(stripped) > 3 and (stripped.endswith('"""') or stripped.endswith("'''")):
                        insert_idx = idx + 1
                        break
                else:
                    insert_idx = idx + 1
                    break
            elif not in_docstring and (line.startswith("import ") or line.startswith("from ")):
                insert_idx = idx
                break

        lines.insert(insert_idx, import_stmt)
        modified = "\n".join(lines)

    if apply_changes and modified != original:
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(modified)
        except Exception as e:
            changes.append(f"写入失败 {file_path}: {e}")

    return changes


def adjust_all_scripts(apply_changes: bool = False) -> Dict[str, List[str]]:
    """扫描并自适应调整 scripts 目录下所有 Python 文件的系统路径配置。"""
    scripts_dir = get_scripts_dir()
    results = {}
    py_files = sorted(glob.glob(os.path.join(scripts_dir, "**", "*.py"), recursive=True))
    for f in py_files:
        if os.path.basename(f).startswith("._"):
            continue
        c = scan_and_adjust_file(f, apply_changes=apply_changes)
        if c:
            results[os.path.basename(f)] = c
    return results


# ---------------------------------------------------------------------------
# 7. CLI 诊断与执行入口
# ---------------------------------------------------------------------------
def print_system_diagnostics():
    """打印详细的跨平台系统检测与路径诊断报告。"""
    print("=" * 70)
    print("       Zotero Table Extractor - 跨平台系统环境检测报告")
    print("=" * 70)
    print(f"操作系统名称 (OS):       {SYSTEM} ({OS_TYPE})")
    print(f"硬件架构 (Architecture): {MACHINE}")
    print(f"Python 解释器版本:       {PYTHON_VERSION}")
    print(f"Python 解释器路径:       {PYTHON_EXECUTABLE}")
    print(f"当前用户 (User):         {os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown'}")
    print(f"当前用户主目录:          {get_user_home()}")
    print("-" * 70)
    print("【当前运行环境解析路径】")
    print(f"  • AIHub 根目录:        {get_aihub_root()}")
    print(f"  • paper_tables 目录:   {get_paper_tables_dir()}")
    print(f"  • Zotero 主目录:       {get_zotero_dir()}")
    print(f"  • Zotero SQLite 路径:  {get_zotero_db_path()}")
    print(f"  • Zotero storage 目录: {get_zotero_storage_dir()}")
    print(f"  • 附表下载脚本路径:    {get_journal_downloader_script()}")
    print(f"  • .agent_reasoning:    {get_agent_reasoning_dir()}")
    print(f"  • 审计数据库 (8h):     {get_audit_db_path()}")
    print(f"  • 定向审计数据库:      {get_targeted_audit_db_path()}")
    print(f"  • Playwright Profile:  {get_playwright_profile_dir()}")
    print(f"  • Chrome Debug Profile:{get_chrome_debug_profile_dir()}")
    print("-" * 70)
    # 模拟展示 Windows 环境下对应的映射标准
    win_aihub = find_windows_aihub_root()
    print("【对应 Windows 系统映射标准 (语义对齐)】")
    print(f"  • Windows AIHub 根目录: {win_aihub}")
    print(f"  • Windows paper_tables: {os.path.join(win_aihub, 'paper_tables')}")
    print(f"  • Windows Zotero DB:    {os.path.join(get_user_home(), 'Zotero', 'zotero.sqlite')}")
    print(f"  • Windows Zotero storage: {os.path.join(get_user_home(), 'Zotero', 'storage')}")
    print(f"  • Windows Playwright:   {os.path.join(get_user_home(), '.gemini', 'antigravity', 'zotero_playwright_profile')}")
    print("=" * 70)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="跨平台系统检测与环境/路径自适应调整工具")
    parser.add_argument("--check", action="store_true", help="检测并打印当前系统环境与路径映射")
    parser.add_argument("--apply", action="store_true", help="自动调整 scripts 目录下其他 py 文件中的硬编码路径")
    args = parser.parse_args()

    print_system_diagnostics()

    if args.apply:
        print("\n正在自适应调整其他 Python 脚本...")
        adjustments = adjust_all_scripts(apply_changes=True)
        if adjustments:
            for fn, chs in adjustments.items():
                print(f"  [✓] {fn}:")
                for c in chs:
                    print(f"      • {c}")
            print(f"\n成功完成对 {len(adjustments)} 个脚本的自适应调整！")
        else:
            print("  所有脚本均已处于系统自适应状态，无需修改。")
    else:
        # 默认只检查
        adjustments = adjust_all_scripts(apply_changes=False)
        if adjustments:
            print("\n检测到以下脚本可进行自适应调整 (运行 `python scripts/system_detector.py --apply` 应用变更):")
            for fn, chs in adjustments.items():
                print(f"  • {fn}: {len(chs)} 处待调整")
        else:
            print("\n所有脚本均已处于系统自适应状态。")


if __name__ == "__main__":
    main()
