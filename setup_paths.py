#!/usr/bin/env python3
"""
setup_paths.py — Zotero Table Extractor 跨平台路径自定义与交互式初始化向导。

功能：
1. 自动探测当前操作系统（macOS / Windows / Linux）并寻找 Zotero 默认安装/数据目录；
2. 引导用户交互式确认或自定义各项关键路径（Zotero 数据库、附件存储、Excel 导出目录、模型目录、浏览器 Profile 等）；
3. 兼顾跨平台路径差异（自动处理 ~ 展开、斜杠/反斜杠转换与环境变量解析）；
4. 提供路径存在性实时校验、自动创建缺失目录功能；
5. 可选引导配置在线 API 密钥（百度 AIStudio、Elsevier、Firecrawl、LLM 等）；
6. 将配置安全写入 config.json，绝不污染代码库，受 .gitignore 严密保护。

用法：
  python setup_paths.py            # 交互式引导配置
  python setup_paths.py --auto     # 非交互式一键自动探测并保存推荐路径
  python setup_paths.py --show     # 查看当前各项路径配置与有效性状态
  python setup_paths.py --reset    # 重置 config.json 为默认空白模板
"""

import os
import sys
import json
import shutil
import platform
import argparse
from typing import Dict, Any, Optional, Tuple

# 终端 ANSI 彩色显示（在支持的终端上高亮，Windows 自动适配）
if sys.platform.startswith('win'):
    # 启用 Windows 虚拟终端转义序列
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

def c_print(msg: str, color: str = ""):
    """带颜色的安全打印。"""
    if color:
        print(f"{color}{msg}{COLOR_RESET}")
    else:
        print(msg)

# ---------------------------------------------------------------------------
# 1. 基础环境与路径获取
# ---------------------------------------------------------------------------
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(ROOT_DIR, "config.example.json")

SYSTEM = platform.system()
IS_MACOS = (SYSTEM == "Darwin")
IS_WINDOWS = (SYSTEM == "Windows")
IS_LINUX = (SYSTEM == "Linux")

def get_home_dir() -> str:
    """获取当前用户家目录。"""
    if IS_WINDOWS:
        prof = os.environ.get("USERPROFILE")
        if prof and os.path.isdir(prof):
            return prof
    return os.path.expanduser("~")

def clean_input_path(raw_path: str) -> str:
    """清洗用户输入的路径：去除外层引号、展开 ~ 与环境变量、规范化分隔符。"""
    if not raw_path:
        return ""
    p = raw_path.strip().strip('"').strip("'")
    if p.startswith("~"):
        p = os.path.expanduser(p)
    p = os.path.expandvars(p)
    return os.path.normpath(p)

def format_exist_status(path: str, is_dir: bool = True) -> str:
    """返回路径存在状态的彩色标签。"""
    if not path:
        return f"{COLOR_GRAY}[未设置]{COLOR_RESET}"
    real_path = os.path.expanduser(path)
    if os.path.exists(real_path):
        if is_dir and os.path.isdir(real_path):
            return f"{COLOR_GREEN}[✓ 存在目录]{COLOR_RESET}"
        elif not is_dir and os.path.isfile(real_path):
            size_kb = os.path.getsize(real_path) / 1024
            return f"{COLOR_GREEN}[✓ 存在文件, {size_kb:.1f}KB]{COLOR_RESET}"
        else:
            return f"{COLOR_YELLOW}[! 类型不匹配]{COLOR_RESET}"
    else:
        return f"{COLOR_RED}[✗ 路径不存在]{COLOR_RESET}"

# ---------------------------------------------------------------------------
# 2. 自动智能探测
# ---------------------------------------------------------------------------
def detect_zotero_defaults() -> Dict[str, str]:
    """智能推测 Zotero 数据主目录及数据库与存储附件位置。"""
    home = get_home_dir()
    detected = {
        "zotero_dir": "",
        "zotero_db": "",
        "zotero_storage": "",
    }

    candidates = []
    if IS_MACOS:
        candidates = [
            os.path.join(home, "Zotero"),
            os.path.join(home, "Library", "Application Support", "Zotero"),
        ]
    elif IS_WINDOWS:
        candidates = [
            os.path.join(home, "Zotero"),
            os.path.join(os.environ.get("APPDATA", home), "Zotero"),
            r"D:\Zotero",
            r"E:\Zotero",
        ]
    else:
        candidates = [
            os.path.join(home, "Zotero"),
            os.path.join(home, ".zotero"),
        ]

    for cand in candidates:
        if cand and os.path.isdir(cand):
            db_cand = os.path.join(cand, "zotero.sqlite")
            if os.path.isfile(db_cand):
                detected["zotero_dir"] = cand
                detected["zotero_db"] = db_cand
                detected["zotero_storage"] = os.path.join(cand, "storage")
                return detected

    # 兜底默认值
    default_dir = os.path.join(home, "Zotero")
    detected["zotero_dir"] = default_dir
    detected["zotero_db"] = os.path.join(default_dir, "zotero.sqlite")
    detected["zotero_storage"] = os.path.join(default_dir, "storage")
    return detected

def detect_output_default() -> str:
    """推荐默认表格成果输出目录。"""
    home = get_home_dir()
    # 如果已存在历史目录则优先
    legacy_macos = "/Volumes/ExFat/AIHub/paper_tables"
    if IS_MACOS and os.path.isdir(legacy_macos):
        return legacy_macos
    if IS_WINDOWS:
        if os.path.isdir(r"D:\AIHub\paper_tables"):
            return r"D:\AIHub\paper_tables"
        if os.path.isdir(r"D:\paper_tables"):
            return r"D:\paper_tables"
    return os.path.join(home, "paper_tables")

# ---------------------------------------------------------------------------
# 3. 配置文件读写
# ---------------------------------------------------------------------------
def load_existing_config() -> Dict[str, Any]:
    """读取现有配置，若不存在则读取示例配置。"""
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            c_print(f"读取 config.json 出错: {e}，将使用模板", COLOR_YELLOW)

    if os.path.isfile(EXAMPLE_CONFIG_PATH):
        try:
            with open(EXAMPLE_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(config_data: Dict[str, Any]):
    """保存配置至 config.json。"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)
    c_print(f"\n[✓] 配置已成功写入：{CONFIG_PATH}", COLOR_GREEN)

# ---------------------------------------------------------------------------
# 4. 交互引导逻辑
# ---------------------------------------------------------------------------
def ask_path_step(
    step_num: int,
    title: str,
    description: str,
    default_val: str,
    is_dir: bool = True,
    allow_empty: bool = False
) -> str:
    """单步交互式路径询问。"""
    c_print(f"\n[{step_num}] {title}", COLOR_BOLD + COLOR_CYAN)
    c_print(f"    说明: {description}", COLOR_GRAY)
    status_str = format_exist_status(default_val, is_dir=is_dir)
    c_print(f"    推荐/当前值: {default_val}  {status_str}")

    prompt = f"    请输入自定义路径 (直接按 [Enter] 确认默认): "
    user_val = input(prompt).strip()
    if not user_val:
        return default_val

    cleaned = clean_input_path(user_val)
    if not cleaned and allow_empty:
        return ""

    # 存在性检查与创建提示
    if is_dir and not os.path.isdir(cleaned):
        c_print(f"    [!] 目录当前不存在：{cleaned}", COLOR_YELLOW)
        ans = input("    是否需要在保存时自动创建该目录？ [Y/n]: ").strip().lower()
        if ans not in ("n", "no"):
            try:
                os.makedirs(cleaned, exist_ok=True)
                c_print(f"    [✓] 已成功创建目录：{cleaned}", COLOR_GREEN)
            except Exception as e:
                c_print(f"    [✗] 自动创建目录失败: {e}", COLOR_RED)
    elif not is_dir and not os.path.isfile(cleaned):
        c_print(f"    [!] 文件当前不存在，请确认路径是否正确：{cleaned}", COLOR_YELLOW)

    return cleaned

def run_interactive_wizard():
    """运行完整的多步骤交互式配置向导。"""
    c_print("=" * 72, COLOR_BLUE)
    c_print("   📚 Zotero Table Extractor — 跨平台路径与运行环境初始化向导", COLOR_BOLD + COLOR_GREEN)
    c_print("   支持系统：macOS (Apple Silicon / Intel) / Windows 10/11 / Linux", COLOR_CYAN)
    c_print("=" * 72, COLOR_BLUE)

    home = get_home_dir()
    c_print(f"当前系统: {SYSTEM} ({platform.machine()})")
    c_print(f"当前用户目录: {home}")
    c_print(f"项目根目录: {ROOT_DIR}")

    cfg = load_existing_config()
    zotero_info = detect_zotero_defaults()
    default_output = detect_output_default()

    # 1. Zotero 主目录
    curr_zotero_dir = cfg.get("ZOTERO_DIR") or zotero_info["zotero_dir"]
    new_zotero_dir = ask_path_step(
        1, "Zotero 数据主目录 (ZOTERO_DIR)",
        "本地 Zotero 的主要工作目录，通常位于用户家目录下的 Zotero 文件夹",
        curr_zotero_dir, is_dir=True
    )

    # 2. Zotero 数据库文件
    derived_db = os.path.join(new_zotero_dir, "zotero.sqlite") if new_zotero_dir else zotero_info["zotero_db"]
    curr_zotero_db = cfg.get("ZOTERO_DB_PATH") or derived_db
    new_zotero_db = ask_path_step(
        2, "Zotero SQLite 核心数据库文件 (ZOTERO_DB_PATH)",
        "保存所有论文条目元数据的本地数据库（zotero.sqlite）",
        curr_zotero_db, is_dir=False
    )

    # 3. Zotero 附件存储目录
    derived_storage = os.path.join(new_zotero_dir, "storage") if new_zotero_dir else zotero_info["zotero_storage"]
    curr_zotero_storage = cfg.get("ZOTERO_STORAGE_DIR") or derived_storage
    new_zotero_storage = ask_path_step(
        3, "Zotero PDF 附件库目录 (ZOTERO_STORAGE_DIR)",
        "Zotero 存放所有已下载 PDF 附件的真实物理存储目录（storage）",
        curr_zotero_storage, is_dir=True
    )

    # 4. 表格提取主输出目录
    curr_output_dir = cfg.get("PAPER_TABLES_DIR") or default_output
    new_output_dir = ask_path_step(
        4, "表格提取成果与 Excel 导出总目录 (PAPER_TABLES_DIR)",
        "所有提取出的规范 Excel (.xlsx)、全库日志和审计数据库的主输出存储位置",
        curr_output_dir, is_dir=True
    )

    # 5. Playwright 浏览器用户配置目录
    default_playwright_dir = os.path.join(home, ".zotero_playwright_profile")
    curr_playwright_dir = cfg.get("PLAYWRIGHT_USER_DATA_DIR") or default_playwright_dir
    new_playwright_dir = ask_path_step(
        5, "浏览器会话与 Cookie 持久化目录 (PLAYWRIGHT_USER_DATA_DIR)",
        "保存知网 (CNKI)、Springer、Wiley 等期刊网站的登录会话及 Cookies",
        curr_playwright_dir, is_dir=True
    )

    # 6. 本地离线 ONNX 版面模型目录
    default_model_dir = os.path.join(ROOT_DIR, "models")
    curr_model_dir = cfg.get("DOCLAYOUT_MODEL_DIR") or default_model_dir
    new_model_dir = ask_path_step(
        6, "本地离线视觉版面检测模型目录 (DOCLAYOUT_MODEL_DIR)",
        "存放 DocLayout-YOLO ONNX 权重的目录（默认使用项目自带的 ./models）",
        curr_model_dir, is_dir=True
    )

    # 7. 网页爬取缓存目录
    default_cache_dir = os.path.join(home, ".zotero_firecrawl_cache")
    curr_cache_dir = cfg.get("FIRECRAWL_CACHE_DIR") or default_cache_dir
    new_cache_dir = ask_path_step(
        7, "网页提取离线缓存目录 (FIRECRAWL_CACHE_DIR)",
        "缓存出版商 HTML 与 Firecrawl 解析结果，避免频繁重复抓取",
        curr_cache_dir, is_dir=True
    )

    # 8. (可选) 外部附表下载器脚本路径
    curr_supp = cfg.get("SUPP_DOWNLOADER_SCRIPT") or ""
    new_supp = ask_path_step(
        8, "[可选] 外部附表下载技能主脚本路径 (SUPP_DOWNLOADER_SCRIPT)",
        "若本地配有 journal-supp-downloader 技能可填入，若无请直接按 Enter 留空跳过",
        curr_supp, is_dir=False, allow_empty=True
    )

    # 更新配置字典
    cfg["ZOTERO_DIR"] = new_zotero_dir
    cfg["ZOTERO_DB_PATH"] = new_zotero_db
    cfg["ZOTERO_STORAGE_DIR"] = new_zotero_storage
    cfg["PAPER_TABLES_DIR"] = new_output_dir
    cfg["PLAYWRIGHT_USER_DATA_DIR"] = new_playwright_dir
    cfg["DOCLAYOUT_MODEL_DIR"] = new_model_dir
    cfg["FIRECRAWL_CACHE_DIR"] = new_cache_dir
    cfg["SUPP_DOWNLOADER_SCRIPT"] = new_supp

    # 9. (可选) 是否配置在线 API Keys
    c_print("\n[9] 在线 API 密钥配置向导", COLOR_BOLD + COLOR_CYAN)
    ask_api = input("    是否需要顺便配置在线 API 密钥（飞桨 AIStudio、Elsevier 等）？ [y/N]: ").strip().lower()
    if ask_api in ("y", "yes"):
        # 百度飞桨 AIStudio Token
        old_token = cfg.get("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN", "")
        mask_token = (old_token[:6] + "..." + old_token[-4:]) if len(old_token) > 10 else (old_token or "[未设置]")
        c_print(f"    百度 AIStudio Access Token (用于 PP-StructureV3 / PaddleOCR-VL): 当前 {mask_token}")
        t_in = input("    请输入新 Token (直接按 [Enter] 保持原样): ").strip()
        if t_in:
            cfg["PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN"] = t_in

        # Elsevier API Key
        old_els = cfg.get("ELSEVIER_API_KEY", "")
        mask_els = (old_els[:4] + "..." + old_els[-4:]) if len(old_els) > 8 else (old_els or "[未设置]")
        c_print(f"    Elsevier API Key (用于 ScienceDirect 原生高保真 XML): 当前 {mask_els}")
        e_in = input("    请输入新 Key (直接按 [Enter] 保持原样): ").strip()
        if e_in:
            cfg["ELSEVIER_API_KEY"] = e_in

        # Firecrawl API Key
        old_fc = cfg.get("FIRECRAWL_API_KEY", "")
        mask_fc = (old_fc[:4] + "..." + old_fc[-4:]) if len(old_fc) > 8 else (old_fc or "[未设置]")
        c_print(f"    Firecrawl API Key (用于通用在线网页清洗): 当前 {mask_fc}")
        f_in = input("    请输入新 Key (直接按 [Enter] 保持原样): ").strip()
        if f_in:
            cfg["FIRECRAWL_API_KEY"] = f_in

    # 打印最终变更汇总
    c_print("\n" + "─" * 72, COLOR_BLUE)
    c_print("【配置变更汇总预览】", COLOR_BOLD + COLOR_GREEN)
    c_print("─" * 72, COLOR_BLUE)
    summary_items = [
        ("Zotero 数据主目录", cfg["ZOTERO_DIR"], True),
        ("Zotero SQLite 数据库", cfg["ZOTERO_DB_PATH"], False),
        ("Zotero PDF 附件目录", cfg["ZOTERO_STORAGE_DIR"], True),
        ("表格输出主目录", cfg["PAPER_TABLES_DIR"], True),
        ("浏览器 Profile 目录", cfg["PLAYWRIGHT_USER_DATA_DIR"], True),
        ("本地视觉模型目录", cfg["DOCLAYOUT_MODEL_DIR"], True),
        ("网页提取缓存目录", cfg["FIRECRAWL_CACHE_DIR"], True),
        ("外部附表下载器脚本", cfg.get("SUPP_DOWNLOADER_SCRIPT", ""), False),
    ]

    for label, path_val, is_dir_item in summary_items:
        stat = format_exist_status(path_val, is_dir=is_dir_item)
        c_print(f"• {label:<22}: {path_val}  {stat}")

    c_print("─" * 72, COLOR_BLUE)
    confirm = input("确认将上述配置保存至 config.json 吗？ [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        c_print("\n[!] 用户取消，未做任何修改。", COLOR_YELLOW)
        return

    # 保存配置
    save_config(cfg)

    # 运行自测
    c_print("\n正在调用 system_detector 进行环境自检...", COLOR_CYAN)
    try:
        sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
        import system_detector
        c_print(f"[✓] 解析出的 Zotero 数据库: {system_detector.get_zotero_db_path()}", COLOR_GREEN)
        c_print(f"[✓] 解析出的表格输出目录: {system_detector.get_paper_tables_dir()}", COLOR_GREEN)
        c_print(f"[✓] 解析出的 PDF 附件目录: {system_detector.get_zotero_storage_dir()}", COLOR_GREEN)
        c_print(f"\n🎉 恭喜！环境路径已全部配置完毕。您可以立即开始提取表格！", COLOR_BOLD + COLOR_GREEN)
        c_print(f"示例命令：python scripts/extract_zotero_table.py --pdf <论文.pdf> --output {cfg['PAPER_TABLES_DIR']}", COLOR_CYAN)
    except Exception as e:
        c_print(f"[!] 自检触发提示: {e}", COLOR_YELLOW)


def run_auto_defaults():
    """非交互式一键应用系统探测推荐值。"""
    c_print("正在执行一键自动探测与路径初始化...", COLOR_CYAN)
    cfg = load_existing_config()
    zotero_info = detect_zotero_defaults()
    default_output = detect_output_default()
    home = get_home_dir()

    cfg["ZOTERO_DIR"] = cfg.get("ZOTERO_DIR") or zotero_info["zotero_dir"]
    cfg["ZOTERO_DB_PATH"] = cfg.get("ZOTERO_DB_PATH") or zotero_info["zotero_db"]
    cfg["ZOTERO_STORAGE_DIR"] = cfg.get("ZOTERO_STORAGE_DIR") or zotero_info["zotero_storage"]
    cfg["PAPER_TABLES_DIR"] = cfg.get("PAPER_TABLES_DIR") or default_output
    cfg["PLAYWRIGHT_USER_DATA_DIR"] = cfg.get("PLAYWRIGHT_USER_DATA_DIR") or os.path.join(home, ".zotero_playwright_profile")
    cfg["DOCLAYOUT_MODEL_DIR"] = cfg.get("DOCLAYOUT_MODEL_DIR") or os.path.join(ROOT_DIR, "models")
    cfg["FIRECRAWL_CACHE_DIR"] = cfg.get("FIRECRAWL_CACHE_DIR") or os.path.join(home, ".zotero_firecrawl_cache")

    os.makedirs(cfg["PAPER_TABLES_DIR"], exist_ok=True)
    save_config(cfg)
    c_print("[✓] 自动初始化完成！", COLOR_GREEN)


def show_status():
    """展示当前配置及路径有效性。"""
    cfg = load_existing_config()
    c_print("=" * 72, COLOR_BLUE)
    c_print(f"  当前系统环境: {SYSTEM} | 配置文件: {CONFIG_PATH}", COLOR_BOLD + COLOR_CYAN)
    c_print("=" * 72, COLOR_BLUE)

    items = [
        ("ZOTERO_DIR", cfg.get("ZOTERO_DIR", ""), True),
        ("ZOTERO_DB_PATH", cfg.get("ZOTERO_DB_PATH", ""), False),
        ("ZOTERO_STORAGE_DIR", cfg.get("ZOTERO_STORAGE_DIR", ""), True),
        ("PAPER_TABLES_DIR", cfg.get("PAPER_TABLES_DIR", ""), True),
        ("PLAYWRIGHT_USER_DATA_DIR", cfg.get("PLAYWRIGHT_USER_DATA_DIR", ""), True),
        ("DOCLAYOUT_MODEL_DIR", cfg.get("DOCLAYOUT_MODEL_DIR", ""), True),
        ("FIRECRAWL_CACHE_DIR", cfg.get("FIRECRAWL_CACHE_DIR", ""), True),
        ("SUPP_DOWNLOADER_SCRIPT", cfg.get("SUPP_DOWNLOADER_SCRIPT", ""), False),
    ]

    for key, val, is_dir_item in items:
        status_str = format_exist_status(val, is_dir=is_dir_item)
        print(f"  • {key:<26}: {val or '[未配置]'}  {status_str}")

    c_print("─" * 72, COLOR_GRAY)
    token = cfg.get("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN", "")
    t_status = f"{COLOR_GREEN}[已配置]{COLOR_RESET}" if token else f"{COLOR_YELLOW}[未配置]{COLOR_RESET}"
    print(f"  • PADDLEOCR TOKEN           : {t_status}")
    els = cfg.get("ELSEVIER_API_KEY", "")
    e_status = f"{COLOR_GREEN}[已配置]{COLOR_RESET}" if els else f"{COLOR_YELLOW}[未配置]{COLOR_RESET}"
    print(f"  • ELSEVIER API KEY          : {e_status}")
    c_print("=" * 72, COLOR_BLUE)


def reset_to_template():
    """重置为默认模板。"""
    if os.path.isfile(EXAMPLE_CONFIG_PATH):
        shutil.copy(EXAMPLE_CONFIG_PATH, CONFIG_PATH)
        c_print(f"[✓] 已重置 config.json 为官方出厂模板：{CONFIG_PATH}", COLOR_GREEN)
    else:
        c_print(f"[✗] 未找到模板文件：{EXAMPLE_CONFIG_PATH}", COLOR_RED)


def main():
    parser = argparse.ArgumentParser(description="Zotero Table Extractor 跨平台路径与环境初始化配置向导")
    parser.add_argument("--auto", action="store_true", help="非交互式一键自动探测并保存推荐路径")
    parser.add_argument("--show", "--status", action="store_true", help="查看当前各项路径的配置与有效性状态")
    parser.add_argument("--reset", action="store_true", help="重置 config.json 为默认空白模板")
    args = parser.parse_args()

    if args.reset:
        reset_to_template()
    elif args.show:
        show_status()
    elif args.auto:
        run_auto_defaults()
    else:
        run_interactive_wizard()

if __name__ == "__main__":
    main()
