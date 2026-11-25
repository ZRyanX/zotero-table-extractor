#!/usr/bin/env python3
"""
common.py — 共享工具函数（原 _common.py；is_metadata_table 已并入原 general_html_extractor 的扩展启发式）

本模块收敛了原本散落在多个脚本中的重复实现：
- 配置加载：load_config / get_config_path
- Cookie 工具：get_cnki_cookies_path / get_crawl4ai_cookies_path / load_cookies_from_file
- 文本与表格清洗（HTML 提取场景）：clean_text / escape_formula / clean_table_filename
- 表格过滤：is_supplementary / is_metadata_table

注意：extract_zotero_table.py 与 online/ 包中的 clean_text / escape_formula
是语义不同的特化版本（PDF 文本保留内部换行、数值型公式识别等），仍各自保留，
不在本模块中统一。
"""

import os
import re
import json
import tempfile
import threading
import contextlib
from typing import Optional, Tuple

try:
    from .models import ExtractedTable, TableCell
except ImportError:
    try:
        from models import ExtractedTable, TableCell
    except ImportError:
        ExtractedTable = None
        TableCell = None

try:
    from .teds import teds_similarity, dataframe_to_table_tree, parse_html_to_tree, TableNode
except ImportError:
    try:
        from teds import teds_similarity, dataframe_to_table_tree, parse_html_to_tree, TableNode
    except ImportError:
        teds_similarity = None
        dataframe_to_table_tree = None
        parse_html_to_tree = None
        TableNode = None



try:
    from .system_detector import (
        IS_MACOS, IS_WINDOWS, IS_LINUX, SYSTEM,
        get_paper_tables_dir, get_zotero_db_path, get_zotero_storage_dir,
        get_aihub_root, get_audit_db_path, get_targeted_audit_db_path,
        get_journal_downloader_script, get_agent_reasoning_dir,
        adapt_path, setup_environment
    )
except ImportError:
    try:
        from system_detector import (
            IS_MACOS, IS_WINDOWS, IS_LINUX, SYSTEM,
            get_paper_tables_dir, get_zotero_db_path, get_zotero_storage_dir,
            get_aihub_root, get_audit_db_path, get_targeted_audit_db_path,
            get_journal_downloader_script, get_agent_reasoning_dir,
            adapt_path, setup_environment
        )
    except ImportError:
        IS_MACOS = True
        IS_WINDOWS = False
        IS_LINUX = False
        SYSTEM = "Darwin"
        adapt_path = lambda p: p
        setup_environment = lambda: None


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def get_skill_root():
    """Returns the skill root folder (parent of the scripts directory)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_config_path():
    """Returns the path to config.json in the skill root folder."""
    return os.path.join(get_skill_root(), 'config.json')


_COMPANION_KEY_MAP = {
    "ELSEVIER_API_KEY": "ELSEVIER_API_KEY",
    "elsevier_api_key": "ELSEVIER_API_KEY",
    "ELSEVIER_INSTTOKEN": "ELSEVIER_INSTTOKEN",
    "elsevier_insttoken": "ELSEVIER_INSTTOKEN",
    "SPRINGER_API_KEY": "SPRINGER_API_KEY",
    "springer_api_key": "SPRINGER_API_KEY",
    "IS_CAMPUS_NETWORK": "IS_CAMPUS_NETWORK",
    "is_campus_network": "IS_CAMPUS_NETWORK",
    "NETWORK_PROXY": "NETWORK_PROXY",
    "network_proxy": "NETWORK_PROXY",
    "PROXY_URL": "NETWORK_PROXY",
    "proxy_url": "NETWORK_PROXY",
    "EZPROXY_ENABLED": "EZPROXY_ENABLED",
    "ezproxy_enabled": "EZPROXY_ENABLED",
    "EZPROXY_LOGIN_URL": "EZPROXY_LOGIN_URL",
    "ezproxy_login_url": "EZPROXY_LOGIN_URL",
    "EZPROXY_URL": "EZPROXY_LOGIN_URL",
    "ezproxy_url": "EZPROXY_LOGIN_URL",
    "WEBVPN_ENABLED": "WEBVPN_ENABLED",
    "webvpn_enabled": "WEBVPN_ENABLED",
    "WEBVPN_BASE_URL": "WEBVPN_BASE_URL",
    "webvpn_base_url": "WEBVPN_BASE_URL",
    "WEBVPN_KEY": "WEBVPN_KEY",
    "webvpn_key": "WEBVPN_KEY",
    "WEBVPN_IV": "WEBVPN_IV",
    "webvpn_iv": "WEBVPN_IV",
    "CARSI_ENABLED": "CARSI_ENABLED",
    "carsi_enabled": "CARSI_ENABLED",
    "CARSI_IDP": "CARSI_IDP",
    "carsi_idp": "CARSI_IDP",
    "CARSI_IDP_NAME": "CARSI_IDP",
    "carsi_idp_name": "CARSI_IDP",
    "UNPAYWALL_EMAIL": "UNPAYWALL_EMAIL",
    "unpaywall_email": "UNPAYWALL_EMAIL",
    "CROSSREF_EMAIL": "CROSSREF_EMAIL",
    "crossref_email": "CROSSREF_EMAIL",
    "email": "UNPAYWALL_EMAIL",
    "OPENALEX_API_KEY": "OPENALEX_API_KEY",
    "openalex_api_key": "OPENALEX_API_KEY",
    "CORE_API_KEY": "CORE_API_KEY",
    "core_api_key": "CORE_API_KEY",
    "ZOTERO_API_KEY": "ZOTERO_API_KEY",
    "zotero_api_key": "ZOTERO_API_KEY",
    "ZOTERO_USER_ID": "ZOTERO_USER_ID",
    "zotero_library_id": "ZOTERO_USER_ID",
    "ZOTERO_LIBRARY_TYPE": "ZOTERO_LIBRARY_TYPE",
    "zotero_library_type": "ZOTERO_LIBRARY_TYPE",
}

_COMPANION_BOOL_KEYS = frozenset({
    "IS_CAMPUS_NETWORK",
    "EZPROXY_ENABLED",
    "WEBVPN_ENABLED",
    "CARSI_ENABLED",
})


def _parse_bool_flag(val):
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "y")


def _parse_dotenv_file(env_path):
    """Parse a .env file into a dict without mutating os.environ."""
    parsed = {}
    if not env_path or not os.path.isfile(env_path):
        return parsed
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                elif " #" in val:
                    val = val.split(" #", 1)[0].rstrip()
                if key:
                    parsed[key] = val
    except Exception:
        pass
    return parsed


def _discover_companion_downloader_roots():
    """Return candidate journal-supp-downloader root directories that exist locally."""
    roots = []
    seen = set()

    def _add(path):
        if path:
            norm = os.path.abspath(path)
            if os.path.isdir(norm) and norm not in seen:
                seen.add(norm)
                roots.append(norm)

    try:
        if callable(globals().get("get_journal_downloader_script")):
            script_path = get_journal_downloader_script()
            if script_path and os.path.isfile(script_path):
                _add(os.path.dirname(os.path.dirname(script_path)))
    except Exception:
        pass

    skill_parent = os.path.dirname(get_skill_root())
    _add(os.path.join(skill_parent, "journal-supp-downloader"))
    home = os.path.expanduser("~")
    _add(os.path.join(home, ".gemini", "antigravity-cli", "skills", "journal-supp-downloader"))
    _add(os.path.join(home, ".gemini", "config", "skills", "journal-supp-downloader"))
    return roots


def _load_companion_academic_credentials():
    """
    从 companion 生态（~/.scansci-pdf、~/.journal_supp_downloader_profile、
    journal-supp-downloader/.env & config*.json）读取未在主配置中设定的凭据与网络标记。
    返回映射后的标准大写键字典；任何读取异常均静默忽略，且永不打印密钥值。
    """
    if os.getenv("ZTE_DISABLE_COMPANION_CREDS") == "1":
        return {}

    merged = {}
    home = os.path.expanduser("~")
    json_candidates = [
        os.path.join(home, ".scansci-pdf", "config.json"),
        os.path.join(home, ".journal_supp_downloader_profile", "config.json"),
    ]
    roots = _discover_companion_downloader_roots()
    for root in roots:
        json_candidates.append(os.path.join(root, "config.json"))
        json_candidates.append(os.path.join(root, "config.local.json"))

    def _absorb(raw_key, raw_val):
        if raw_val in (None, ""):
            return
        mapped = _COMPANION_KEY_MAP.get(raw_key)
        if not mapped:
            return
        if mapped in _COMPANION_BOOL_KEYS:
            merged[mapped] = _parse_bool_flag(raw_val)
        else:
            val_str = str(raw_val).strip()
            if val_str:
                merged[mapped] = val_str
                if raw_key == "email" and not merged.get("CROSSREF_EMAIL"):
                    merged["CROSSREF_EMAIL"] = val_str

    for jpath in json_candidates:
        if not os.path.isfile(jpath):
            continue
        try:
            with open(jpath, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                for k, v in payload.items():
                    _absorb(k, v)
        except Exception:
            pass

    for root in roots:
        for env_name in (".env", ".env.local"):
            env_map = _parse_dotenv_file(os.path.join(root, env_name))
            for k, v in env_map.items():
                _absorb(k, v)

    return merged


def load_config():
    """Loads config.json from the skill root, applying companion & env overrides.

    敏感配置支持环境变量覆盖，并在主配置留空时回退读取同生态工具
    （journal-supp-downloader .env / ~/.scansci-pdf/config.json）的凭据：
    - FIRECRAWL_API_KEY ← $FIRECRAWL_API_KEY
    - ELSEVIER_API_KEY ← $ELSEVIER_API_KEY
    - ELSEVIER_INSTTOKEN ← $ELSEVIER_INSTTOKEN
    - PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN ← $PADDLEOCR_ACCESS_TOKEN
    """
    config_path = get_config_path()
    data = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                import platform
                os_type = platform.system().lower()
                if os_type == "windows":
                    if data.get("PLAYWRIGHT_USER_DATA_DIR_WINDOWS"):
                        data["PLAYWRIGHT_USER_DATA_DIR"] = data["PLAYWRIGHT_USER_DATA_DIR_WINDOWS"]
                elif os_type == "darwin" and data.get("PLAYWRIGHT_USER_DATA_DIR_MAC"):
                    data["PLAYWRIGHT_USER_DATA_DIR"] = data["PLAYWRIGHT_USER_DATA_DIR_MAC"]
        except Exception as e:
            print(f"Warning: Failed to load config from {config_path}: {e}")

    # 跨平台路径适配与回退（macOS, Linux, Windows 统一生效）
    for _path_key in (
        "PAPER_TABLES_DIR",
        "ZOTERO_DIR",
        "ZOTERO_DB_PATH",
        "ZOTERO_STORAGE_DIR",
        "PLAYWRIGHT_USER_DATA_DIR",
        "SUPP_DOWNLOADER_SCRIPT",
    ):
        if data.get(_path_key) and isinstance(data[_path_key], str):
            data[_path_key] = adapt_path(data[_path_key])

    if not data.get("PAPER_TABLES_DIR"):
        data["PAPER_TABLES_DIR"] = get_paper_tables_dir()
    if not data.get("ZOTERO_DB_PATH"):
        data["ZOTERO_DB_PATH"] = get_zotero_db_path()

    # 同生态凭据与网络标志低优先级回退（仅填补主配置未设置或为空的项）
    companion_creds = _load_companion_academic_credentials()
    for c_key, c_val in companion_creds.items():
        if data.get(c_key) in (None, "") and c_val not in (None, ""):
            data[c_key] = c_val

    # 默认多云与引擎配置
    if "MINERU_API_KEY" not in data:
        data["MINERU_API_KEY"] = ""
    if "MINERU_API_BASE" not in data:
        data["MINERU_API_BASE"] = "https://mineru.net"
    if "MINERU_TOKEN" not in data:
        data["MINERU_TOKEN"] = ""
    if "MINERU_USE_CLI" not in data:
        data["MINERU_USE_CLI"] = "auto"
    if "MINERU_CLI_PATH" not in data:
        data["MINERU_CLI_PATH"] = ""
    if "MINERU_MODEL" not in data:
        data["MINERU_MODEL"] = "vlm"
    if "OCR_ENGINE" not in data:
        data["OCR_ENGINE"] = "auto"
    if "DOCLING_ENABLED" not in data:
        data["DOCLING_ENABLED"] = True
    if "DOCLING_OFFLINE_FALLBACK" not in data:
        data["DOCLING_OFFLINE_FALLBACK"] = True
    if "GMFT_ENABLED" not in data:
        data["GMFT_ENABLED"] = True
    if "IMG2TABLE_ENABLED" not in data:
        data["IMG2TABLE_ENABLED"] = True
    if "ELSEVIER_INSTTOKEN" not in data:
        data["ELSEVIER_INSTTOKEN"] = ""
    if "SPRINGER_API_KEY" not in data:
        data["SPRINGER_API_KEY"] = ""
    if "SPRINGER_TDM_ENABLED" not in data:
        data["SPRINGER_TDM_ENABLED"] = True
    if "IS_CAMPUS_NETWORK" not in data:
        data["IS_CAMPUS_NETWORK"] = False
    if "NETWORK_PROXY" not in data:
        data["NETWORK_PROXY"] = ""
    if "EZPROXY_ENABLED" not in data:
        data["EZPROXY_ENABLED"] = False
    if "EZPROXY_LOGIN_URL" not in data:
        data["EZPROXY_LOGIN_URL"] = ""
    if "WEBVPN_ENABLED" not in data:
        data["WEBVPN_ENABLED"] = False
    if "WEBVPN_BASE_URL" not in data:
        data["WEBVPN_BASE_URL"] = ""
    if "WEBVPN_KEY" not in data:
        data["WEBVPN_KEY"] = ""
    if "WEBVPN_IV" not in data:
        data["WEBVPN_IV"] = ""
    if "UNPAYWALL_EMAIL" not in data:
        data["UNPAYWALL_EMAIL"] = ""
    if "CROSSREF_EMAIL" not in data:
        data["CROSSREF_EMAIL"] = ""
    if "OPENALEX_API_KEY" not in data:
        data["OPENALEX_API_KEY"] = ""
    if "CORE_API_KEY" not in data:
        data["CORE_API_KEY"] = ""
    if "ZOTERO_SYNC_ENABLED" not in data:
        data["ZOTERO_SYNC_ENABLED"] = False
    if "ZOTERO_BETTER_NOTES_ENABLED" not in data:
        data["ZOTERO_BETTER_NOTES_ENABLED"] = True
    if "ZOTERO_AUTO_ATTACH_XLSX" not in data:
        data["ZOTERO_AUTO_ATTACH_XLSX"] = True
    if "ZOTERO_API_KEY" not in data:
        data["ZOTERO_API_KEY"] = ""
    if "ZOTERO_USER_ID" not in data:
        data["ZOTERO_USER_ID"] = ""
    if "ZOTERO_LIBRARY_TYPE" not in data:
        data["ZOTERO_LIBRARY_TYPE"] = "user"
    if "CARSI_ENABLED" not in data:
        data["CARSI_ENABLED"] = False
    if "CARSI_IDP" not in data:
        data["CARSI_IDP"] = ""
    if "TIER2_HEDGE_DELAY_S" not in data:
        data["TIER2_HEDGE_DELAY_S"] = 4.0
    if "NEGATIVE_CACHE_ENABLED" not in data:
        data["NEGATIVE_CACHE_ENABLED"] = True
    if "NEGATIVE_CACHE_TTL_S" not in data:
        data["NEGATIVE_CACHE_TTL_S"] = 1800
    if "MINERU_POPO_ENABLED" not in data:
        data["MINERU_POPO_ENABLED"] = False
    if "MINERU_POPO_AUTO_HARDWARE" not in data:
        data["MINERU_POPO_AUTO_HARDWARE"] = True
    if "MINERU_POPO_BACKEND" not in data:
        data["MINERU_POPO_BACKEND"] = "auto"
    if "MINERU_POPO_MODEL_PATH" not in data:
        data["MINERU_POPO_MODEL_PATH"] = "DreamEternal/MinerU-Popo"
    if "MINERU_POPO_VLLM_URL" not in data:
        data["MINERU_POPO_VLLM_URL"] = ""
    if "MINERU_POPO_VLLM_API_KEY" not in data:
        data["MINERU_POPO_VLLM_API_KEY"] = ""
    if "MINERU_POPO_MODEL_NAME" not in data:
        data["MINERU_POPO_MODEL_NAME"] = "Popo"
    if "MINERU_POPO_REPO_PATH" not in data:
        data["MINERU_POPO_REPO_PATH"] = ""
    if "MINERU_POPO_TIMEOUT" not in data:
        data["MINERU_POPO_TIMEOUT"] = 60
    if "MINERU_POPO_ALLOW_DOWNLOAD" not in data:
        data["MINERU_POPO_ALLOW_DOWNLOAD"] = False

    # 环境变量覆盖敏感配置
    env_overrides = {
        'FIRECRAWL_API_KEY': 'FIRECRAWL_API_KEY',
        'ELSEVIER_API_KEY': 'ELSEVIER_API_KEY',
        'ELSEVIER_INSTTOKEN': 'ELSEVIER_INSTTOKEN',
        'SPRINGER_API_KEY': 'SPRINGER_API_KEY',
        'SPRINGER_TDM_ENABLED': 'SPRINGER_TDM_ENABLED',
        'IS_CAMPUS_NETWORK': 'IS_CAMPUS_NETWORK',
        'NETWORK_PROXY': 'NETWORK_PROXY',
        'EZPROXY_ENABLED': 'EZPROXY_ENABLED',
        'EZPROXY_LOGIN_URL': 'EZPROXY_LOGIN_URL',
        'WEBVPN_ENABLED': 'WEBVPN_ENABLED',
        'WEBVPN_BASE_URL': 'WEBVPN_BASE_URL',
        'WEBVPN_KEY': 'WEBVPN_KEY',
        'WEBVPN_IV': 'WEBVPN_IV',
        'UNPAYWALL_EMAIL': 'UNPAYWALL_EMAIL',
        'CROSSREF_EMAIL': 'CROSSREF_EMAIL',
        'OPENALEX_API_KEY': 'OPENALEX_API_KEY',
        'CORE_API_KEY': 'CORE_API_KEY',
        'PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN': 'PADDLEOCR_ACCESS_TOKEN',
        'MINERU_API_KEY': 'MINERU_API_KEY',
        'MINERU_API_BASE': 'MINERU_API_BASE',
        'MINERU_TOKEN': 'MINERU_TOKEN',
        'MINERU_USE_CLI': 'MINERU_USE_CLI',
        'MINERU_CLI_PATH': 'MINERU_CLI_PATH',
        'MINERU_MODEL': 'MINERU_MODEL',
        'MINERU_POPO_ENABLED': 'MINERU_POPO_ENABLED',
        'MINERU_POPO_AUTO_HARDWARE': 'MINERU_POPO_AUTO_HARDWARE',
        'MINERU_POPO_BACKEND': 'MINERU_POPO_BACKEND',
        'MINERU_POPO_MODEL_PATH': 'MINERU_POPO_MODEL_PATH',
        'MINERU_POPO_VLLM_URL': 'MINERU_POPO_VLLM_URL',
        'MINERU_POPO_VLLM_API_KEY': 'MINERU_POPO_VLLM_API_KEY',
        'MINERU_POPO_MODEL_NAME': 'MINERU_POPO_MODEL_NAME',
        'MINERU_POPO_REPO_PATH': 'MINERU_POPO_REPO_PATH',
        'MINERU_POPO_ALLOW_DOWNLOAD': 'MINERU_POPO_ALLOW_DOWNLOAD',
        'OCR_ENGINE': 'OCR_ENGINE',
        'DOCLING_ENABLED': 'DOCLING_ENABLED',
        'DOCLING_OFFLINE_FALLBACK': 'DOCLING_OFFLINE_FALLBACK',
        'GMFT_ENABLED': 'GMFT_ENABLED',
        'IMG2TABLE_ENABLED': 'IMG2TABLE_ENABLED',
        'ZOTERO_SYNC_ENABLED': 'ZOTERO_SYNC_ENABLED',
        'ZOTERO_BETTER_NOTES_ENABLED': 'ZOTERO_BETTER_NOTES_ENABLED',
        'ZOTERO_AUTO_ATTACH_XLSX': 'ZOTERO_AUTO_ATTACH_XLSX',
        'ZOTERO_API_KEY': 'ZOTERO_API_KEY',
        'ZOTERO_USER_ID': 'ZOTERO_USER_ID',
        'ZOTERO_LIBRARY_TYPE': 'ZOTERO_LIBRARY_TYPE',
        'CARSI_ENABLED': 'CARSI_ENABLED',
        'CARSI_URL': 'CARSI_URL',
        'CARSI_IDP': 'CARSI_IDP',
        'NEGATIVE_CACHE_ENABLED': 'NEGATIVE_CACHE_ENABLED',
    }
    for config_key, env_key in env_overrides.items():
        env_val = os.getenv(env_key)
        if env_val:
            if env_val.lower() in ('true', '1'):
                data[config_key] = True
            elif env_val.lower() in ('false', '0'):
                data[config_key] = False
            else:
                data[config_key] = env_val

    if not data.get("CARSI_IDP") and os.getenv("CARSI_IDP_NAME"):
        data["CARSI_IDP"] = os.getenv("CARSI_IDP_NAME").strip()

    # 兼容 opendatalab/MinerU-Popo 官方 POPO_* 环境变量别名
    for _popo_cfg_k, _popo_env_k in (
        ("MINERU_POPO_BACKEND", "POPO_INFERENCE_BACKEND"),
        ("MINERU_POPO_MODEL_PATH", "POPO_MODEL_PATH"),
        ("MINERU_POPO_VLLM_URL", "POPO_VLLM_URL"),
        ("MINERU_POPO_VLLM_API_KEY", "POPO_VLLM_API_KEY"),
        ("MINERU_POPO_MODEL_NAME", "POPO_MODEL_NAME"),
    ):
        _pv = os.getenv(_popo_env_k)
        if _pv and _pv.strip():
            data[_popo_cfg_k] = _pv.strip()

    for _num_key, _cast in (("TIER2_HEDGE_DELAY_S", float), ("NEGATIVE_CACHE_TTL_S", int), ("MINERU_POPO_TIMEOUT", int)):
        _env_num = os.getenv(_num_key)
        if _env_num is not None and _env_num.strip() != "":
            try:
                data[_num_key] = _cast(_env_num.strip())
            except (TypeError, ValueError):
                pass

    # 自动同步 MINERU_TOKEN 与 MINERU_API_KEY
    if data.get("MINERU_TOKEN") and not data.get("MINERU_API_KEY"):
        data["MINERU_API_KEY"] = data["MINERU_TOKEN"]
    elif data.get("MINERU_API_KEY") and not data.get("MINERU_TOKEN"):
        data["MINERU_TOKEN"] = data["MINERU_API_KEY"]

    return data


def is_carsi_configured(config=None) -> bool:
    """Return True only when CARSI institutional SSO is explicitly configured."""
    cfg = config if isinstance(config, dict) else load_config()
    enabled = cfg.get("CARSI_ENABLED")
    if isinstance(enabled, bool) and enabled:
        return True
    if isinstance(enabled, str) and enabled.strip().lower() in ("true", "1", "yes"):
        return True
    for key in ("CARSI_URL", "CARSI_IDP", "CARSI_IDP_NAME", "CARSI_SCHOOL"):
        val = cfg.get(key) or os.getenv(key, "")
        if isinstance(val, str) and val.strip():
            return True
    env_flag = (os.getenv("CARSI_ENABLED") or "").strip().lower()
    return env_flag in ("true", "1", "yes")


def get_requests_proxies(config=None):
    """返回 requests 兼容的 proxies 字典（仅当显式配置了 NETWORK_PROXY 时），否则返回 None。"""
    cfg = config if isinstance(config, dict) else load_config()
    proxy = str(cfg.get("NETWORK_PROXY") or os.getenv("NETWORK_PROXY") or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    return {"http": proxy, "https": proxy}


def build_elsevier_headers(config=None, accept="application/json"):
    """构造 Elsevier API 请求头，自动附带 X-ELS-APIKey 与可选 X-ELS-InstToken。"""
    cfg = config if isinstance(config, dict) else load_config()
    api_key = str(cfg.get("ELSEVIER_API_KEY") or os.getenv("ELSEVIER_API_KEY") or "").strip()
    if not api_key:
        return {}
    headers = {"X-ELS-APIKey": api_key}
    if accept:
        headers["Accept"] = accept
    insttoken = str(cfg.get("ELSEVIER_INSTTOKEN") or os.getenv("ELSEVIER_INSTTOKEN") or "").strip()
    if insttoken:
        headers["X-ELS-InstToken"] = insttoken
    return headers


def get_polite_email(config=None, service="unpaywall"):
    """返回 Unpaywall/Crossref/OpenAlex 礼貌池（Polite Pool）邮箱标识。"""
    cfg = config if isinstance(config, dict) else load_config()
    svc = (service or "").strip().lower()
    if svc == "crossref":
        email = cfg.get("CROSSREF_EMAIL") or cfg.get("UNPAYWALL_EMAIL")
    else:
        email = cfg.get("UNPAYWALL_EMAIL") or cfg.get("CROSSREF_EMAIL")
    email = str(email or os.getenv("UNPAYWALL_EMAIL") or os.getenv("CROSSREF_EMAIL") or "").strip()
    return email or "supp_downloader@academic-research.org"


# ---------------------------------------------------------------------------
# 浏览器提取全局锁（并行模式下的序列化保护）
# ---------------------------------------------------------------------------

_BROWSER_THREAD_LOCK = threading.Lock()
_BROWSER_THREAD_LOCKS_GUARD = threading.Lock()
_BROWSER_THREAD_LOCKS = {"browser_extraction": _BROWSER_THREAD_LOCK}


def _get_browser_thread_lock(name: str) -> threading.Lock:
    with _BROWSER_THREAD_LOCKS_GUARD:
        lk = _BROWSER_THREAD_LOCKS.get(name)
        if lk is None:
            lk = threading.Lock()
            _BROWSER_THREAD_LOCKS[name] = lk
        return lk


@contextlib.contextmanager
def browser_extraction_lock(name="browser_extraction", timeout: float = 120.0, cancel_event=None):
    """
    浏览器类提取（Playwright 调试 Chrome / 共享 user-data-dir）的全局互斥锁。

    并行提取时（HTML ∥ Paddle 竞速、批量多论文并发），基于共享浏览器状态的
    渠道（共享 profile 目录）不能同时运行，否则会出现 profile 文件锁竞争
    与崩溃。

    本锁提供两层保护：
      1. 进程内 threading.Lock —— 保护同进程内多线程并发；
      2. 跨进程文件锁（fcntl / Windows msvcrt LK_NBLCK 有界重试）—— 保护
         多子进程并发，修复 Windows 下 msvcrt.LK_LOCK 仅等 10s 抛出 Errno 36
         后静默放行的并发崩溃问题；支持 cancel_event 提前退出等待。

    非浏览器类渠道（HTTP 直连 / curl_cffi / Firecrawl / Scrapling / Crawl4AI
    独立浏览器实例）无需此锁，可自由并行。
    """
    import time as _time

    lock_path = os.path.join(tempfile.gettempdir(), f"zotero_table_extractor_{name}.lock")
    thread_lock = _get_browser_thread_lock(name)
    wait_s = max(0.5, float(120.0 if timeout is None else timeout))
    deadline = _time.monotonic() + wait_s

    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError(f"Cancelled waiting for in-process browser lock '{name}'")
        rem = deadline - _time.monotonic()
        if rem <= 0:
            raise TimeoutError(f"Timed out waiting for in-process browser lock '{name}' after {wait_s:.1f}s")
        if thread_lock.acquire(timeout=min(0.1, rem)):
            break

    f = None
    file_locked = False
    try:
        try:
            f = open(lock_path, "a+")
        except OSError:
            f = None

        if f is not None:
            if os.name == "nt":
                import msvcrt
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise TimeoutError(f"Cancelled waiting for cross-process browser lock '{name}'")
                    try:
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        file_locked = True
                        break
                    except OSError:
                        if _time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Timed out waiting for cross-process browser lock '{name}' after {wait_s:.1f}s"
                            )
                        _time.sleep(0.05)
            else:
                import fcntl
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise TimeoutError(f"Cancelled waiting for cross-process browser lock '{name}'")
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        file_locked = True
                        break
                    except BlockingIOError:
                        if _time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Timed out waiting for cross-process browser lock '{name}' after {wait_s:.1f}s"
                            )
                        _time.sleep(0.05)
                    except OSError:
                        # Filesystems without flock support: fall back gracefully
                        break
        yield
    finally:
        if f is not None:
            try:
                if file_locked:
                    if os.name == "nt":
                        import msvcrt
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass
        thread_lock.release()


# ---------------------------------------------------------------------------
# Cookie 工具
# ---------------------------------------------------------------------------

def get_cnki_cookies_path():
    """Resolves and returns the path to cnki_cookies.txt in the user's home directory."""
    home = os.path.expanduser('~')
    # Check both normal and hidden variants
    paths = [
        os.path.join(home, 'cnki_cookies.txt'),
        os.path.join(home, '.cnki_cookies.txt')
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[0]  # Default fallback


def get_crawl4ai_cookies_path():
    home = os.path.expanduser("~")
    return os.path.join(home, ".zotero_crawl4ai_cookies.json")


def get_companion_cookie_paths():
    """Return existing publisher/institutional cookie files shared with companion tools."""
    import glob

    home = os.path.expanduser("~")
    scansci_cache = os.path.join(home, ".scansci-pdf", "cache")
    candidates = [
        get_crawl4ai_cookies_path(),
        os.path.join(home, ".journal_supp_downloader_profile", "cookies.json"),
        os.path.join(scansci_cache, "publisher_cookies.json"),
        os.path.join(scansci_cache, "instsci-cookies.json"),
        os.path.join(scansci_cache, "instsci_cookies.json"),
        os.path.join(scansci_cache, "vpnsci-cookies.json"),
        os.path.join(scansci_cache, "vpnsci_cookies.json"),
        os.path.join(scansci_cache, "ezproxy_cookies.json"),
        os.path.join(home, ".scansci-pdf", "instsci-cookies.json"),
    ]
    carsi_dir = os.path.join(scansci_cache, "carsi_cookies")
    if os.path.isdir(carsi_dir):
        candidates.extend(sorted(glob.glob(os.path.join(carsi_dir, "*.json"))))
    for root in _discover_companion_downloader_roots():
        candidates.append(os.path.join(root, "cookies.json"))
        candidates.append(os.path.join(root, "instsci-cookies.json"))
    seen = set()
    existing = []
    for p in candidates:
        norm = os.path.abspath(p)
        if norm not in seen and os.path.isfile(norm):
            seen.add(norm)
            existing.append(norm)
    return existing


def _normalize_same_site(raw_val):
    """Normalize browser/extension sameSite values to Playwright-accepted ('Strict'|'Lax'|'None')."""
    if not raw_val or not isinstance(raw_val, str):
        return None
    low = raw_val.strip().lower()
    if low == "strict":
        return "Strict"
    if low == "lax":
        return "Lax"
    if low in ("none", "no_restriction"):
        return "None"
    # 'unspecified' or unknown values must be omitted for Playwright add_cookies()
    return None


def _extract_cookie_dicts_from_json(data):
    """Flatten JSON cookie payloads: list, {'cookies': [...]}, or domain/publisher-keyed dicts."""
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            return [c for c in data["cookies"] if isinstance(c, dict)]
        collected = []
        for val in data.values():
            if isinstance(val, list):
                collected.extend(c for c in val if isinstance(c, dict) and c.get("name"))
            elif isinstance(val, dict) and isinstance(val.get("cookies"), list):
                collected.extend(c for c in val["cookies"] if isinstance(c, dict) and c.get("name"))
        if collected:
            return collected
    return []


def load_cookies_from_file(file_path):
    """
    Parses cookies from a Netscape cookie file or JSON cookie file.
    Supports list, {'cookies': [...]}, and publisher/domain-keyed JSON stores.
    Returns a list of dicts suitable for Playwright's context.add_cookies().
    """
    import math

    cookies = []
    if not file_path or not os.path.exists(file_path):
        return cookies
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()

        # Try JSON first
        try:
            data = json.loads(content)
            raw_list = _extract_cookie_dicts_from_json(data)
            if isinstance(data, (list, dict)):
                for c in raw_list:
                    name = c.get("name")
                    if not name:
                        continue
                    domain = c.get("domain")
                    url_val = c.get("url")
                    if not domain and not url_val:
                        continue
                    val = c.get("value")
                    cookie = {
                        "name": str(name),
                        "value": "" if val is None else str(val),
                        "secure": bool(c.get("secure", False)),
                        "httpOnly": bool(c.get("httpOnly", False)),
                    }
                    if domain:
                        cookie["domain"] = str(domain)
                        cookie["path"] = str(c.get("path") or "/")
                    else:
                        cookie["url"] = str(url_val)

                    raw_exp = c.get("expirationDate")
                    if raw_exp is None:
                        raw_exp = c.get("expires")
                    if raw_exp is not None and raw_exp != "":
                        try:
                            exp_f = float(raw_exp)
                            if math.isfinite(exp_f):
                                cookie["expires"] = int(exp_f)
                        except (TypeError, ValueError):
                            pass

                    norm_ss = _normalize_same_site(c.get("sameSite"))
                    if norm_ss:
                        cookie["sameSite"] = norm_ss
                    cookies.append(cookie)
                return cookies
        except Exception:
            pass

        # Fallback to Netscape cookie format
        for line in content.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) >= 7:
                domain, flag, path, secure_str, expiration, name, value = parts[:7]
                cookie = {
                    "domain": domain,
                    "path": path,
                    "secure": secure_str.upper() == "TRUE",
                    "expires": int(expiration) if expiration.isdigit() else -1,
                    "name": name,
                    "value": value
                }
                cookies.append(cookie)
    except Exception as e:
        print(f"Warning: Failed to parse cookies from {file_path}: {e}")
    return cookies


# ---------------------------------------------------------------------------
# 文本与表格清洗（HTML / 结构化提取场景）
# 注意：本文件中的 clean_text / clean_latex_and_ocr 专用于通用提取与 HTML 场景；
# 复杂的 PDF 地学矿物科学表格后处理逻辑集中在 table_postprocess.py。
# ---------------------------------------------------------------------------

def is_markdown_separator(line: str) -> bool:
    """
    判断一行文本是否为 Markdown 表格的表头分割线（如 |---|---| 或 |:---:|---:|）。

    严谨判定护栏：
    1. 首尾必须包裹竖线 | ；
    2. 至少分割出 2 个或以上单元格 (len(cells) >= 2)；
    3. 每个单元格去除首尾空格后必须非空；
    4. 单元格字符集必须严格为连字符 '-' 与冒号 ':' 的子集，且每个单元格内至少包含一个连字符 '-'。
    杜绝将全缺失值或包含连字符的普通数据行误判为分隔线。
    """
    if not isinstance(line, str):
        return False
    stripped = line.strip()
    if not (stripped.startswith('|') and stripped.endswith('|')):
        return False
    inner = stripped[1:-1]
    cells = [c.strip() for c in inner.split('|')]
    if len(cells) < 2:
        return False
    return all(len(c) > 0 and set(c).issubset({'-', ':'}) and '-' in c for c in cells)


def ensure_macos_corefoundation():
    """Preload macOS CoreFoundation with RTLD_GLOBAL before importing curl_cffi/scrapling."""
    import sys
    if sys.platform != "darwin":
        return True
    try:
        import ctypes
        cf_path = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        if os.path.exists(cf_path):
            ctypes.CDLL(cf_path, mode=ctypes.RTLD_GLOBAL)
        return True
    except Exception:
        return False


def copy_sqlite_snapshot(src_db: str, dst_db: str) -> str:
    """Create a WAL-consistent, non-blocking snapshot copy of a SQLite database (e.g. live zotero.sqlite)."""
    import shutil
    import sqlite3
    import time

    dst_dir = os.path.dirname(os.path.abspath(dst_db))
    if dst_dir:
        os.makedirs(dst_dir, exist_ok=True)

    # Do NOT call sqlite3.Connection.backup() on a potentially locked live DB:
    # CPython's backup() loops indefinitely on SQLITE_BUSY/SQLITE_LOCKED.
    # Instead copy main DB + -wal sidecar cleanly and validate/checkpoint on the private copy.
    for attempt in range(2):
        cleanup_sqlite_snapshot(dst_db)
        shutil.copy2(src_db, dst_db)
        wal_src = src_db + "-wal"
        has_wal = os.path.exists(wal_src)
        if has_wal:
            try:
                shutil.copy2(wal_src, dst_db + "-wal")
            except Exception:
                has_wal = False

        conn = None
        try:
            conn = sqlite3.connect(dst_db)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            if has_wal:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return dst_db
        except Exception:
            if attempt == 0:
                time.sleep(0.05)
                continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # Final best-effort copy including both -wal and -shm sidecars
    cleanup_sqlite_snapshot(dst_db)
    shutil.copy2(src_db, dst_db)
    for suffix in ("-wal", "-shm"):
        sidecar = src_db + suffix
        if os.path.exists(sidecar):
            try:
                shutil.copy2(sidecar, dst_db + suffix)
            except Exception:
                pass
    return dst_db


def cleanup_sqlite_snapshot(dst_db: str):
    """Remove temporary SQLite snapshot and any -wal/-shm sidecar files."""
    if not dst_db:
        return
    for suffix in ("", "-wal", "-shm"):
        target = dst_db + suffix
        if os.path.exists(target):
            try:
                os.remove(target)
            except Exception:
                pass


def clean_text(val):
    if val is None:
        return ""
    val = str(val).strip()
    val = val.replace('\x00', '')
    val = val.replace('\r', '').replace('\n', ' ')
    val = " ".join(val.split())
    return val

def clean_latex_and_ocr(val, enable_chemical_typo_fix: bool = False, preserve_scripts: bool = False):
    """Clean raw LaTeX math markup, OCR typos, and garbled symbols.
    Aggressive chemical substitutions (H20 -> H2O, CQ -> CO2, Tin -> Tm) are decoupled
    and disabled by default to prevent destroying legitimate sample codes or mineral abbreviations.
    When preserve_scripts=True, script markers (^{...}, _{...}, X_n) are preserved for Excel CellRichText rendering.
    """
    if not isinstance(val, str) or not val:
        return val

    # 0. Convert HTML sub/sup markup before stripping remaining tags
    val = re.sub(r'<sub(?:\s+[^>]*)?>(.*?)</sub>', r'_{\1}', val, flags=re.IGNORECASE | re.DOTALL)
    val = re.sub(r'<sup(?:\s+[^>]*)?>(.*?)</sup>', r'^{\1}', val, flags=re.IGNORECASE | re.DOTALL)
    val = re.sub(r'<[^>]+>', ' ', val)
    val = re.sub(r'\\n', ' ', val)

    # 1. Clean LaTeX cases environments
    val = re.sub(r'\\begin\{[^}]*\}|\\end\{[^}]*\}', '', val)
    val = re.sub(r'\\\\', ' ', val)
    val = re.sub(r'&amp;|&', ' ', val)

    # 2. Remove \text{...}, \mathrm{...}, \mathbf{...}
    val = re.sub(r'\\(text|mathrm|mathbf|mathit|boldsymbol)\{([^}]*)\}', r'\2', val)
    val = re.sub(r'\\math[a-z]+\{([^}]*)\}', r'\1', val)

    # 3. Standardize math symbols & superscripts
    val = val.replace(r'\times', '×').replace(r'\pm', '±').replace(r'\cdot', '·').replace(r'^\circ', '°').replace(r'\circ', '°').replace(r'^{\circ}', '°')
    val = val.replace(r'\approx', '≈').replace(r'\leq', '≤').replace(r'\geq', '≥').replace(r'\neq', '≠')
    val = val.replace(r'\Delta', 'Δ').replace(r'\delta', 'δ').replace(r'\mu', 'μ').replace(r'\omega', 'ω').replace(r'\phi', 'φ').replace(r'\beta', 'β').replace(r'\gamma', 'γ')

    # Superscripts / subscripts cleanup: ^{206} -> 206 or preserved as ^{206}
    val = re.sub(r'\^\{+([0-9a-zA-Z\+\-\*\s]+)\}+', r'^{\1}' if preserve_scripts else r'^\1', val)
    val = re.sub(r'_\{+([0-9a-zA-Z\+\-\*\s]+)\}+', r'_{\1}' if preserve_scripts else r'_\1', val)
    val = re.sub(r'(?<=\d)\s*times\s*(?=\d)', ' × ', val)

    if preserve_scripts:
        val = val.replace('^{', '\uE000').replace('_{', '\uE001')
        val = re.sub('(\uE000|\uE001)([^\uE002{}]*)}', '\\1\\2\uE002', val)

    # Strip LaTeX $ delimiters and braces
    val = val.replace('$', '').replace('\\', '').replace('{', '').replace('}', '')
    if preserve_scripts:
        val = val.replace('\uE000', '^{').replace('\uE001', '_{').replace('\uE002', '}')
    else:
        def _strip_chem_sub(m):
            prefix = val[:m.start()]
            if re.search(r'(?:^|[^A-Za-z])[a-z]{2,}$', prefix + m.group(1)):
                return m.group(0)
            return f"{m.group(1)}{m.group(2)}"
        val = re.sub(r'([A-Za-z])_([0-9]+)', _strip_chem_sub, val)
    val = re.sub(r'\s*±\s*', ' ± ', val)
    val = re.sub(r'\s*\^\s*([0-9+-]+)\s*', r'^\1', val)

    # 4. Fix common OCR typos in chemical formulas & mineral terms (Only when explicitly enabled)
    if enable_chemical_typo_fix:
        val = re.sub(r'\bNaC1\b', 'NaCl', val)
        val = re.sub(r'\bCaC12\b', 'CaCl2', val)
        val = re.sub(r'\bH20\b', 'H2O', val)
        val = re.sub(r'\bCQ\b', 'CO2', val)
        val = re.sub(r'\bCtL\b', 'CH4', val)
        val = re.sub(r'\bTin-\b', 'Tm -', val)
        val = re.sub(r'\bTin\b', 'Tm', val)
        val = re.sub(r'\bcastiterite\b', 'cassiterite', val, flags=re.I)
        val = re.sub(r'\bberthlerite\b', 'berthierite', val, flags=re.I)
        val = re.sub(r'\bFable\b', 'Table', val, flags=re.I)

    # Fix common OCR garbled text in mineral paragenesis
    val = val.replace('(_+qz)', '(± qz)').replace('(_+ qz)', '(± qz)')
    val = re.sub(r'\b200o\b', '200°', val)
    val = val.replace("'-", "-")

    # Normalize space line-by-line while preserving \n for subrow expansion
    lines = [re.sub(r'[ \t]+', ' ', l).strip() for l in re.split(r'\n|\\n', val)]
    return '\n'.join([l for l in lines if l])


def escape_formula(val):
    if isinstance(val, str) and str(val).startswith(('=', '+', '-', '@')):
        return "'" + str(val)
    return val


def df_map(df, func):
    """
    Pandas 2.x / 3.x 兼容的 DataFrame 逐元素映射函数。
    在 Pandas 2.1+ / 3.0+ 优先使用 df.map()，旧版本兼容回退到 df.applymap()。
    """
    if df is None:
        return None
    if hasattr(df, 'map'):
        return df.map(func)
    return df.applymap(func)


def clean_table_filename(raw_title, index):
    if not raw_title:
        return f"Table {index}"

    cleaned = raw_title.strip().replace('\r', '').replace('\n', ' ')
    cleaned = " ".join(cleaned.split())

    # Remove illegal characters for windows filename
    cleaned = re.sub(r'[\\/*?:"<>|]', '_', cleaned)
    cleaned = cleaned[:100].strip()

    # Check if it already starts with "表几" / "附表几" / "Table几" / "Appendix Table A1" / "Supplementary Table S1" etc.
    pattern = (
        r'^(?:'
        r'附表|附录表|补充表|表|'
        r'Supplementary\s+(?:Table|material)|Supplemental\s+Table|'
        r'Extended\s+Data\s+Table|Appendix\s+Table|'
        r'Additional\s+(?:Table|file)|'
        r'Tableau?x?|Tables?|Tab\b'
        r')\.?\s*'
        r'(?:[A-Za-z]?\d+(?:[\.\-]\d+)*|[ivxlcdm]+|ni|rn|[一二三四五六七八九十百千万零壹贰叁肆伍陆柒捌玖拾]+|[A-Za-z]\b)'
    )
    if not re.match(pattern, cleaned, re.IGNORECASE):
        return f"Table {index} {cleaned}"

    return cleaned


HYPHEN_CHARS = r'[\-\u2010\u2011\u2012\u2013\u2014\u2015—–._~]'
TABLE_SUB_SEP = r'(?:\s*[\-\u2010\u2011\u2012\u2013\u2014\u2015—–_~]\s*|\.(?!\s))'
CHINESE_DIGITS = r'[一二三四五六七八九十百千万零〇两壹贰叁肆伍陆柒捌玖拾佰仟]'
ROMAN_DIGITS = r'[\u2160-\u217fIVXLCDMivxlcdm]'
FULLWIDTH_DIGITS = r'[\uff10-\uff19]'
ROMAN_NUM_PATTERN = r'(?:[\u2160-\u217f]+|\b(?=[MDCLXVI])M*(?:C[MD]|D?C{0,3})(?:X[CL]|L?X{0,3})(?:I[XV]|V?I{0,3})(?<=[MDCLXVI])\b)'
TABLE_NUM_PART = r'(?:[A-Za-z]?[0-9\uff10-\uff19]+|' + ROMAN_NUM_PATTERN + r'|' + CHINESE_DIGITS + r'+|\b[A-Za-z]\b)'
TABLE_LABEL_PATTERN = r'(?:附表|附录表|补充表|表|Supplementary\s+Table|Supplemental\s+Table|Extended\s+Data\s+Table|Appendix\s+Table|TABLE|Table|Tab(?:\.|\b))\s*(?:' + TABLE_NUM_PART + r'(?:' + TABLE_SUB_SEP + TABLE_NUM_PART + r')*(?:\([a-zA-Z0-9]\)|（[a-zA-Z0-9]）|[a-zA-Z]\b)?)'
TABLE_LABEL_RE = re.compile(TABLE_LABEL_PATTERN, re.IGNORECASE)


_CN_DIGIT_MAP = {
    '零': 0, '〇': 0,
    '一': 1, '壹': 1,
    '二': 2, '贰': 2, '两': 2,
    '三': 3, '叁': 3,
    '四': 4, '肆': 4,
    '五': 5, '伍': 5,
    '六': 6, '陆': 6,
    '七': 7, '柒': 7,
    '八': 8, '捌': 8,
    '九': 9, '玖': 9,
}
_CN_UNIT_MAP = {'十': 10, '拾': 10, '百': 100, '佰': 100, '千': 1000, '仟': 1000}


def chinese_numeral_to_arabic(cn_str: str) -> Optional[int]:
    """将中文数字（如 '一', '三', '十', '十二', '二十三', '壹', '拾贰'）转换为正整数。"""
    if not cn_str:
        return None
    s = cn_str.strip()
    if not s or not all(ch in _CN_DIGIT_MAP or ch in _CN_UNIT_MAP for ch in s):
        return None
    total = 0
    current = 0
    has_unit = False
    for ch in s:
        if ch in _CN_DIGIT_MAP:
            current = _CN_DIGIT_MAP[ch]
        elif ch in _CN_UNIT_MAP:
            unit = _CN_UNIT_MAP[ch]
            has_unit = True
            if current == 0:
                current = 1
            total += current * unit
            current = 0
    if not has_unit and len(s) > 1:
        # 逐位中文数字（如 '一二' -> 12）
        val = 0
        for ch in s:
            val = val * 10 + _CN_DIGIT_MAP[ch]
        return val if val > 0 else None
    total += current
    return total if total > 0 else None


def _normalize_cn_numeral_segments(num_str: str) -> str:
    """将表号中纯中文数字段（如 '三', '二-三', '十二'）规范化为阿拉伯数字（'3', '2-3', '12'）。"""
    if not num_str:
        return num_str

    def _repl(match):
        token = match.group(0)
        val = chinese_numeral_to_arabic(token)
        return str(val) if val is not None else token

    return re.sub(r'[零〇一二两三四五六七八九十百千壹贰叁肆伍陆柒捌玖拾佰仟]+', _repl, num_str)


_ROMAN_NUMERAL_MAP = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5,
    'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10,
    'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 14, 'xv': 15,
    'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20,
    'xxi': 21, 'xxii': 22, 'xxiii': 23, 'xxiv': 24, 'xxv': 25,
    'xxvi': 26, 'xxvii': 27, 'xxviii': 28, 'xxix': 29, 'xxx': 30,
}


def _normalize_roman_key(num_str: str) -> str:
    """将纯罗马数字表号 (如 'ii', 'iv', 'xii') 映射为阿拉伯数字字符串，用于跨格式表号匹配。"""
    s = (num_str or "").strip().lower()
    if s in _ROMAN_NUMERAL_MAP:
        return str(_ROMAN_NUMERAL_MAP[s])
    return s


def canonicalize_table_label_key(raw_label: str) -> Optional[Tuple[str, str]]:
    """
    将中英文、中文数字及罗马数字表号映射为统一规范键 `(category, num_key)`，用于双语同页去重与跨语言表号核验。
    示例：
      - '表3' / 'Table 3' / '表三' / 'Table III' -> ('main', '3')
      - '表3.13' / 'Table 3.13' -> ('main', '3.13')
      - '表3-13' / 'Table 3-13' -> ('main', '3-13')
      - '附表1' / 'Table S1' / 'Supplementary Table 1' -> ('supp', '1')
      - '附录表1' / 'Table A1' / 'Appendix Table 1' -> ('app', '1')
    """
    fmt = format_table_label(raw_label)
    if not fmt:
        return None
    s = fmt.strip()

    m_supp = re.match(
        r'^(?:(?:Supplementary|Supplemental)\s+Table\s+S?|Table\s+S|附表\s*S?|补充表\s*S?)(.+)$',
        s,
        re.IGNORECASE,
    )
    if m_supp:
        num = _normalize_roman_key(re.sub(r'\s+', '', m_supp.group(1)).lower())
        return ('supp', num)

    m_app = re.match(
        r'^(?:Appendix\s+Table\s+A?|Table\s+A|附录表\s*A?)(.+)$',
        s,
        re.IGNORECASE,
    )
    if m_app:
        num = _normalize_roman_key(re.sub(r'\s+', '', m_app.group(1)).lower())
        return ('app', num)

    m_ext = re.match(
        r'^(?:Extended\s+Data\s+Table\s+E?|Table\s+E)(.+)$',
        s,
        re.IGNORECASE,
    )
    if m_ext:
        num = _normalize_roman_key(re.sub(r'\s+', '', m_ext.group(1)).lower())
        return ('ext', num)

    m_main = re.match(r'^(?:Table\s+|表\s*)(.+)$', s, re.IGNORECASE)
    if m_main:
        num = _normalize_roman_key(re.sub(r'\s+', '', m_main.group(1)).lower())
        return ('main', num)

    return None


def format_table_label(raw_label: str) -> str:
    """
    规范化表号名称，严密保留并标准化文中的所有真实命名形式。
    支持罗马数字 (Table I/IV)、S-附表 (Table S1)、中文汉字 (表一/表三 -> 表1/表3)、章节多级表号以及严格剥离标题首词。
    """
    if not raw_label:
        return ""
    trans_fw = str.maketrans('０１２３４５６７８９', '0123456789')
    s = raw_label.strip().replace('\xa0', ' ').replace('\u3000', ' ').translate(trans_fw)
    # 折叠小型大写字母导致的拆字空格（如 GeoScienceWorld/Silverchair 的 "T ABLE 1" / "T A B L E 2"）
    s = re.sub(r'(?<![A-Za-z])T\s*A\s*B\s*L\s*E(?![A-Za-z])', 'Table', s, flags=re.IGNORECASE)
    s = re.sub(r'(?<![A-Za-z])T\s*A\s*B\s*\.(?![A-Za-z])', 'Tab.', s, flags=re.IGNORECASE)
    
    # 截断紧随在表号后面的标题部分（如 "Table 1. Description", "Table 1: Geological", "Table 1 - Summary", "Tab 1: Description", "表三、标题"）
    prefix_pat = r'^((?:Supplementary\s+Table|Supplemental\s+Table|Extended\s+Data\s+Table|Appendix\s+Table|TABLE|Table|Tab(?:\.|\b)|附表|附录表|补充表|表)\s*(?:' + TABLE_NUM_PART + r'(?:' + TABLE_SUB_SEP + TABLE_NUM_PART + r')*(?:\([a-zA-Z0-9]\)|（[a-zA-Z0-9]）|[a-zA-Z]\b)?))(?:[\.\:\：\—\–\、\s]+.*)?$'
    s = re.sub(prefix_pat, r'\1', s, flags=re.IGNORECASE).strip()

    # 1. 中文表号规范化（含中文大写/小写数字转阿拉伯数字：表一 -> 表1, 表三 -> 表3, 表一〇 -> 表10, 附表二 -> 附表2）
    m_cn = re.match(r'^(附表|附录表|补充表|表)\s*([A-Za-z0-9\u3007\u4e00-\u9fa5\u2160-\u217fIVXLCDMivxlcdm]+(?:\s*' + HYPHEN_CHARS + r'\s*[A-Za-z0-9\u3007\u4e00-\u9fa5\u2160-\u217fIVXLCDMivxlcdm]+)*(?:\([a-zA-Z0-9]\)|（[a-zA-Z0-9]）|[a-zA-Z])?)$', s, re.IGNORECASE)
    if m_cn:
        prefix, num = m_cn.groups()
        num_clean = re.sub(r'[\s\u2010\u2011\u2012\u2013\u2014\u2015—–_]', '-', num)
        num_clean = re.sub(r'-{2,}', '-', num_clean).strip('-')
        num_clean = _normalize_cn_numeral_segments(num_clean)
        return f"{prefix}{num_clean}"

    # 1b. 纯数字章节表号（如 4.9, 4.10, 4.11）
    m_num = re.match(r'^(\d+\.\d+(?:\.\d+)?)$', s)
    if m_num:
        return f"表{m_num.group(1)}"
        
    # 2. 英文表号规范化
    m_en = re.match(r'^(Supplementary\s+Table|Supplemental\s+Table|Extended\s+Data\s+Table|Appendix\s+Table|TABLE|Table|Tab(?:\.|\b))\s*([A-Za-z0-9IVXLCDMivxlcdm]+(?:[-—–._][A-Za-z0-9IVXLCDMivxlcdm]+)*(?:\([a-zA-Z0-9]\)|[a-zA-Z])?)$', s, re.IGNORECASE)
    if m_en:
        prefix, num = m_en.groups()
        num_clean = re.sub(r'[—–_]', '-', num)
        if prefix.upper() in ('TABLE', 'TABLE ', 'TAB.', 'TAB'):
            prefix_clean = 'Table'
        elif 'extended' in prefix.lower():
            prefix_clean = 'Extended Data Table'
        elif 'supplemental' in prefix.lower():
            prefix_clean = 'Supplemental Table'
        elif 'supplementary' in prefix.lower():
            prefix_clean = 'Supplementary Table'
        elif 'appendix' in prefix.lower():
            prefix_clean = 'Appendix Table'
        else:
            prefix_clean = re.sub(r'\s+', ' ', prefix).capitalize()
        return f"{prefix_clean} {num_clean}"
        
    return s


def make_unique_columns(cols: list) -> list:
    """
    确保 DataFrame 列名列表中的每个列名严格唯一，避免同名列导致的索引混乱或报错。
    若存在重复列名，按出现的先后顺序添加 _2, _3 等后缀。
    """
    seen = {}
    unique_cols = []
    for i, c in enumerate(cols):
        name = str(c).strip() if (c is not None and str(c).strip()) else f"Col_{i+1}"
        if name not in seen:
            seen[name] = 1
            unique_cols.append(name)
        else:
            seen[name] += 1
            unique_cols.append(f"{name}_{seen[name]}")
    return unique_cols


# ---------------------------------------------------------------------------
# 表格过滤
# ---------------------------------------------------------------------------

def is_table_squeezed(df) -> bool:
    """
    检测 DataFrame 是否存在列挤压问题（即因提取器列边界合并导致多个数值/元素挤入单个单元格）。
    """
    if df is None or df.empty or df.shape[1] == 0:
        return False
    for col_idx in range(df.shape[1]):
        col_vals = df.iloc[:, col_idx].dropna().astype(str).tolist()
        if not col_vals:
            continue
        squeezed_count = sum(1 for v in col_vals if len(re.findall(r'(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9_])', str(v))) >= 2 and len(str(v).split()) >= 2)
        if len(col_vals) >= 3 and (squeezed_count / len(col_vals)) >= 0.3:
            return True
    return False


def is_table_low_quality(df) -> bool:
    """
    检测 DataFrame 是否为低质量/损坏提取（如混入大量正文长句段落、章节标题、或有效数据极度稀疏）。
    支持 1 行表与单列表，避免对汇总行或键值表的形状误杀。
    """
    if df is None or df.empty or df.shape[0] < 1 or df.shape[1] < 1:
        return True

    # 1. 检查是否混入了正文段落或论文元数据（基金项目、作者简介、章节标题等）
    article_metadata_kws = ['基金项目', '作者简介', '中图分类号', '文献标识码', '收稿日期', '通信作者', '引用格式', 'received:', 'accepted:', 'revised:']
    prose_pollution_count = 0
    
    # 检查表头是否属于合法的文本/描述型表格（通用科学表头保护）
    header_str = ' '.join(str(c) for c in df.columns).lower()
    is_valid_summary_table = any(kw in header_str for kw in [
        '时间', '单位', '内容', '特征', '描述', '层位', '构造', '类型', '阶段', '矿物', '样品', '岩性', '年龄', '方法', '来源', '编号', '地点', '产状',
        'variable', 'parameter', 'description', 'definition', 'property', 'value', 'type', 'category', 'method', 'condition', 'name', 'unit'
    ])
    
    for r in range(df.shape[0]):
        row_cells = [str(df.iat[r, c]).strip() for c in range(df.shape[1]) if str(df.iat[r, c]).strip() not in ['', 'nan', 'none']]
        row_str = ' '.join(row_cells)
        # 匹配论文元数据行
        if any(kw in row_str.lower() for kw in article_metadata_kws):
            prose_pollution_count += 1
            continue
        # 匹配章节标题（如 1.2 地质背景）：仅在单格或极窄行且首格自身即为章节编号+标题时触发，杜绝首列浮点数 + 次列中文矿物名被误杀
        if len(row_cells) <= 2 and row_cells and re.match(r'^\d+\.\d+(?:\.\d+)?\s+[\u4e00-\u9fa5]{2,}', row_cells[0]):
            prose_pollution_count += 1
            continue
        # 匹配大面积正文陈述句（整行只有 1 个单元格且包含句号长句）
        if len(row_cells) == 1 and len(row_str) >= 30 and '。' in row_str and not is_valid_summary_table:
            prose_pollution_count += 1
            continue

    # 只要包含 2 行以上论文正文/元数据污染行，且占比超过 30%，判定为污染表
    if prose_pollution_count >= 2 and (df.shape[0] <= 4 or prose_pollution_count / max(1, df.shape[0]) >= 0.3):
        return True

    # 2. 检查是否有竖排文字拆行导致的碎片化（例如某列连续多行只有单个汉字且无其他有效列支撑）
    common_single_cn_tokens = {
        '金', '银', '铜', '铁', '铅', '锌', '钨', '锡', '钼', '锑', '汞', '砷', '硫', '碳', '氧', '硅', '铝', '钙', '镁', '钾', '钠',
        '是', '否', '有', '无', '高', '中', '低', '强', '弱', '大', '小', '优', '良', '差', '男', '女',
        '甲', '乙', '丙', '丁', '戊', '己', '庚', '辛', '一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '东', '西', '南', '北',
    }
    total_cells = df.shape[0] * df.shape[1]
    non_empty = sum(1 for v in df.values.flatten() if v is not None and str(v).strip() and str(v).strip().lower() not in ['', 'nan', 'none'])
    for c in range(min(3, df.shape[1])):
        col_vals = [str(df.iat[r, c]).strip() for r in range(df.shape[0]) if str(df.iat[r, c]).strip() not in ['', 'nan', 'none']]
        single_chars = [v for v in col_vals if len(v) == 1 and re.match(r'[\u4e00-\u9fa5]', v) and v not in common_single_cn_tokens]
        if len(single_chars) >= 6 and (len(single_chars) / max(1, len(col_vals)) >= 0.85) and (df.shape[1] <= 2 or (non_empty / max(1, total_cells)) < 0.35):
            return True

    # 3. 检查空单元格占比（稀疏度过高通常为错位散文或误切）
    # 豁免：化学元素分析表/同位素比值表/相关性与三角稀疏矩阵天然存在结构性空白
    geo_elem_pattern = r'\b(?:SiO2|TiO2|Al2O3|Fe2O3|FeO|MnO|MgO|CaO|Na2O|K2O|P2O5|LOI|Total|Fe|Cu|Pb|Zn|Au|Ag|Sb|As|Hg|W|Mo|Bi|Co|Ni|Cr|V|Ba|Sr|Rb|Cs|Zr|Hf|Nb|Ta|Th|U|La|Ce|Pr|Nd|Sm|Eu|Gd|Tb|Dy|Ho|Er|Tm|Yb|Lu|Y|REE|δ34S|δ18O|δ13C|δD|206Pb/204Pb|207Pb/204Pb|208Pb/204Pb|wt%|ppm|ppb|Ma|Ga|ka|测点|样品|矿物)\b'
    try:
        cfg = load_config()
        extra_domain_kws = cfg.get("DOMAIN_KEYWORDS", [])
        if extra_domain_kws:
            geo_elem_pattern = geo_elem_pattern[:-2] + '|' + '|'.join(re.escape(k) for k in extra_domain_kws) + r')\b'
    except Exception:
        pass
    col_str_joined = " ".join(str(c) for c in df.columns)
    is_geochem_table = bool(re.search(geo_elem_pattern, col_str_joined, re.IGNORECASE))

    if df.shape[0] >= 3 and df.shape[1] >= 3 and (non_empty / total_cells) < 0.25:
        # 通用科学稀疏矩阵保护：若有明确表头且含数值或单位，或符合领域特征，予以保留（稀疏豁免不局限于单一学科）
        has_numeric = any(re.search(r'\d', str(v)) for v in df.values.flatten() if v is not None)
        has_named_header = any(bool(str(c).strip() and not str(c).startswith('Unnamed:')) for c in df.columns)
        if not (is_geochem_table or (has_numeric and has_named_header and (non_empty / total_cells) >= 0.08)):
            return True

    # 4. 检查是否存在 100% 空白列（原生提取常见列切分损坏）
    if df.shape[1] > 3:
        all_empty_cols = 0
        for c in range(df.shape[1]):
            col_cells = [str(df.iat[r, c]).strip() for r in range(df.shape[0]) if str(df.iat[r, c]).strip() not in ['', 'nan', 'none']]
            if not col_cells:
                all_empty_cols += 1
        if all_empty_cols >= 2:
            return True

    # 5. 检查是否为散点图/直方图坐标轴与图表刻度残片（仅针对明确的坐标轴对比如 'δ34s (‰) vs'，严禁将 'et al., 20' 或 'wt% sio2' 等正常地球化学特征词判为残片）
    all_cells_flat = [str(df.iat[r, c]).strip() for r in range(df.shape[0]) for c in range(df.shape[1]) if str(df.iat[r, c]).strip() not in ['', 'nan', 'none']]
    all_str_concat = " ".join(all_cells_flat).lower()
    if any(kw in all_str_concat for kw in ['δ34s (‰) vs', 'watson et al 2006', 'tomkins et al']):
        has_named_header = any(
            bool(str(c).strip() and not str(c).startswith(('Unnamed', 'Col')) and not str(c).strip().isdigit())
            for c in df.columns
        )
        if not has_named_header and df.shape[0] <= 10 and (non_empty / max(1, total_cells)) < 0.45:
            return True

    # 6. 单列坍缩多列表检测：保留合法单列参数/序列清单（如 ["Param1", "Param2", "Param3"]），
    #    仅拦截明显由多列科学表格坍缩为 1 列的畸形提取（每行塞入多个空白分隔列/数值字段或整行表题+多列数据）
    if df.shape[1] == 1 and df.shape[0] >= 3:
        col0_vals = [
            str(df.iat[r, 0]).strip()
            for r in range(df.shape[0])
            if str(df.iat[r, 0]).strip() not in ('', 'nan', 'none')
        ]
        if len(col0_vals) >= 3:
            if is_table_squeezed(df):
                return True
            collapsed_rows = sum(
                1 for v in col0_vals
                if (len(v.split()) >= 5 and len(v) >= 28)
                or (len(re.findall(r'(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9_])', v)) >= 2 and len(v.split()) >= 3)
            )
            if collapsed_rows >= 3 and (collapsed_rows / len(col0_vals)) >= 0.45:
                return True
            hdr0_str = str(df.columns[0]).strip()
            if (
                re.match(r'^(?:Table|Tab\.?|表)\s*\d+', hdr0_str, re.IGNORECASE)
                and len(hdr0_str) >= 25
                and sum(1 for v in col0_vals if len(v) >= 25 or len(v.split()) >= 4) >= max(2, int(len(col0_vals) * 0.4))
            ):
                return True

    # 7. 图注 / 图例伪表过滤（如表头或首行直接为 "Fig. 3 A" / "Histograms of ..." / "Photomicrographs of ..." 且几乎无数据列）
    if df.shape[0] <= 8:
        hdr_first = " ".join(
            [str(c).strip() for c in df.columns[:3]]
            + ([str(df.iat[0, c]).strip() for c in range(min(3, df.shape[1]))] if df.shape[0] >= 1 else [])
        )
        if re.search(
            r'(?:^|\b)(?:Fig\.?\s*\d+\s*[A-Za-z]?|Figure\s*\d+|Histograms?\s+of|Photomicrographs?\s+of|Back-?scattered\s+electron\s+image)',
            hdr_first,
            re.IGNORECASE,
        ):
            num_cells = sum(1 for v in all_cells_flat if re.fullmatch(r'[-+]?\d+(?:\.\d+)?%?', v))
            if (num_cells / max(1, len(all_cells_flat))) < 0.15:
                return True

    return False


def is_supplementary(title_or_label):
    if not title_or_label:
        return False
    s = str(title_or_label).strip()
    if re.search(
        r'(?:supplement|suppl\.?|supporting\s+info(?:rmation)?|appendix|extended\s+data\s+table|\badditional\s+(?:file|table|data|material)|\bsupp_|\b(?:table|tab\.?|表)\s*S\d+|附表|附录表|补充表)',
        s,
        re.IGNORECASE,
    ):
        return True
    # Strict case-sensitive identifiers for SI / ESM / standalone S1 labels (avoids Si_ppm or "Samples S1-S5")
    if re.search(r'(?:^|[\s/\\])(?:SI|ESM)[_\-\s](?:Table|Tab|\d|[A-Za-z])', s):
        return True
    if re.match(r'^\s*S\d+(?:[.\-]\d+)*\s*$', s):
        return True
    return False


def is_metadata_table(df):
    """
    Checks if a DataFrame represents literature metadata, citation info, or reference lists.
    Includes size/prose heuristics for general publisher pages (merged from general_html_extractor).
    避免误杀含“单位”(计量单位)列的数据表或文献计量学研究表。
    """
    metadata_keywords = [
        "收稿日期", "基金项目", "作者简介", "通信作者", "通讯作者",
        "中图分类号", "文献标志码", "文献标识码", "文章编号", "参考文献",
        "文献信息", "作者信息", "journal info", "copyright", "conflict of interest",
        "author", "authors", "email", "e-mail", "affiliation", "affiliations",
        "correspondence", "orcid", "contributor", "contributors", "doi",
        "published date", "publication date", "citation info", "document details",
        # CNKI 引文块关键词
        "题名", "出版机构", "出版年", "DOI码", "注册时间", "同方知网",
        "link.cnki.net", "kns.cnki.net", "中国知网", "手机知网",
        "以下是您获得的URL地址",
        # 摘要页元数据框关键词
        "摘要", "关键词", "版权", "目录", "outline", "table of contents",
        "点击次数", "下载次数", "引用次数", "被引次数",
        "摘要点击", "全文链接",
    ]
    
    # 仅用于特定结构检查的关键词（避免误杀含"单位"(计量单位)或"参考文献"数据列的正常数据表）
    col_only_keywords = {"作者", "参考文献", "references", "reference"}

    # 1. 严格多特征共现判定：单独一列"单位"绝对不构成元数据表（单位是常见物理量/计量单位列）
    col_set = set(str(c).lower().strip() for c in df.columns)
    row0_set = set()
    if len(df) >= 1 and all(re.match(r'^(?:\d+|Unnamed:\s*\d+)$', str(c).strip()) for c in df.columns):
        row0_set = set(str(v).lower().strip() for v in df.iloc[0].tolist())
    combined_hdr_set = col_set | row0_set
    if "作者" in combined_hdr_set and ("单位" in combined_hdr_set or "机构" in combined_hdr_set or "affiliation" in combined_hdr_set or "e-mail" in combined_hdr_set or "email" in combined_hdr_set):
        return True
    if "作者简介" in combined_hdr_set or "通讯作者" in combined_hdr_set or "通信作者" in combined_hdr_set:
        return True
    # 英文作者-机构-邮箱名单表 / CRediT 贡献角色表（机构邮编等含数字也不应逃逸）
    if (
        ({"author", "authors", "name"} & combined_hdr_set)
        and ({"affiliation", "affiliations", "lead role", "supporting role"} & combined_hdr_set)
        and ({"email", "e-mail", "correspondence", "lead role", "supporting role"} & combined_hdr_set)
    ):
        return True
    # 摘要页引文卡片 / PDF 查看器元信息 UI / 附件列表框
    bib_card_hits = len(
        {"title", "authors", "journal", "volume", "issue", "date", "pages", "published date", "doi", "publisher"}
        & combined_hdr_set
    )
    if bib_card_hits >= 4 and df.shape[0] <= 3:
        return True
    if "zoom option" in combined_hdr_set and df.shape[1] == 1:
        return True
    if ({"filename", "file name"} & combined_hdr_set) and (
        {"description", "file size", "pdf version", "page count", "fast web view"} & combined_hdr_set
    ):
        return True
    # 期刊详情页复制引文 1x2 小框（含 "复制"/"copy" + "doi:" 或 "引用"）
    if df.shape[0] <= 2 and df.shape[1] <= 2 and not any(
        TABLE_LABEL_RE.search(str(getattr(df, 'attrs', {}).get(k, '') or '')) for k in ('label', 'table_title')
    ):
        flat_lower = " ".join(str(v).lower() for v in df.values.flatten() if v is not None)
        if ("doi:" in flat_lower or "引用" in flat_lower) and ("复制" in flat_lower or "copy" in flat_lower or len(df) == 1):
            return True

    # 参考文献列表伪表拦截（如单/双列内连续出现 "[1]包民伟... [J]" 或 "Author et al., 2015, 123-145"）
    if df.shape[1] <= 2 and df.shape[0] >= 2:
        row_texts = [
            " ".join(
                str(df.iat[r, c]).strip()
                for c in range(df.shape[1])
                if str(df.iat[r, c]).strip() not in ("", "nan", "none")
            )
            for r in range(df.shape[0])
        ]
        non_empty_rows = [rt for rt in row_texts if rt]
        if len(non_empty_rows) >= 2:
            bib_re = re.compile(
                r'(?:^\s*\[\d+\]\s*\S|\[[JMCDRP](?:/OL)?\]|et\s+al\.,?\s*(?:19|20)\d{2}.*\d+[-–]\d+)',
                re.IGNORECASE,
            )
            bib_hits = sum(1 for rt in non_empty_rows if bib_re.search(rt))
            if bib_hits >= 2 and (bib_hits / len(non_empty_rows)) >= 0.40:
                return True

    # 作者-单位角标映射表（如 ['邓昌州', '1'] / ['张嘉玮', '2,3,4 *']）
    if df.shape[1] == 2 and not any(
        TABLE_LABEL_RE.search(str(getattr(df, 'attrs', {}).get(k, '') or '')) for k in ('label', 'table_title')
    ):
        hdr0 = str(df.columns[0]).strip()
        hdr1 = str(df.columns[1]).strip()
        affil_idx_re = re.compile(r'^\d+(?:[,，、\s\-–]+\d+)*(?:\s*[*†‡§#])?$')
        col1_vals = [str(v).strip() for v in df.iloc[:, 1].tolist() if v is not None and str(v).strip()]
        col0_vals = [str(v).strip() for v in df.iloc[:, 0].tolist() if v is not None and str(v).strip()]
        if col1_vals and col0_vals and affil_idx_re.match(hdr1):
            all_col1_affil = all(affil_idx_re.match(v) for v in col1_vals)
            all_col0_names = not re.search(r'\d', hdr0) and len(hdr0) <= 30 and all(
                not re.search(r'\d', v) and len(v) <= 30 for v in col0_vals
            )
            if all_col1_affil and all_col0_names:
                return True

    header_col_strs = [str(c).strip() for c in df.columns]
    has_prose_headers = any(
        (len(sub) > 18 and re.search(r'[\u4e00-\u9fa5]{6,}', sub)) or
        re.search(r'(?:进行了|分析在|根据|由于|表明|可以|可知|显示|其中|本次研究|实验室完|测试过程|目前尚不)', c)
        for c in header_col_strs
        for sub in (c.split('_') if '_' in c else [c])
    )
    valid_summary_kws = [
        '矿床', '年龄', '阶段', '样品', '矿物', '方法', '层位', '同位素', '特征', '年代', '定年',
        'deposit', 'age', 'sample', 'mineral', 'method', 'isotope', 'stage', 'dating',
        'summary', 'comparison', 'parameter', 'variable', 'definition', 'property', 'value', 'type', 'category', 'description', 'unit',
        '指标', '参数', '变量', '对比', '属性', '类型', '结果', '实验', '模型', '定义', '说明', '描述', '单位'
    ]
    try:
        cfg = load_config()
        extra_domain_kws = cfg.get("DOMAIN_KEYWORDS", [])
        if extra_domain_kws:
            valid_summary_kws.extend([str(k).lower() for k in extra_domain_kws])
    except Exception:
        pass

    has_explicit_table_label = bool(
        hasattr(df, 'attrs') and (
            TABLE_LABEL_RE.search(str(df.attrs.get('label', '') or '')) or
            TABLE_LABEL_RE.search(str(df.attrs.get('table_title', '') or ''))
        )
    )
    is_valid_summary_table = (
        has_explicit_table_label or (
            not has_prose_headers and 
            any(any(kw == sub.lower() or (kw in sub.lower() and len(sub) <= 12) for kw in valid_summary_kws) for c in header_col_strs for sub in (c.split('_') if '_' in c else [c]))
        )
    )

    # 统计元数据列命中数（需多特征共现或强特征命中，防止单列 DOI/被引 误杀文献计量表）
    strong_metadata_hits = 0
    weak_metadata_hits = 0
    strong_metadata_kws = {"收稿日期", "基金项目", "作者简介", "通信作者", "通讯作者", "中图分类号", "文献标志码", "文献标识码", "文章编号", "同方知网", "link.cnki.net", "kns.cnki.net", "以下是您获得的URL地址"}
    
    for col in df.columns:
        col_str = str(col).lower().strip()
        for kw in metadata_keywords:
            if kw in col_only_keywords:
                if kw in ("参考文献", "references", "reference") and (col_str == kw or col_str.startswith(kw)) and not is_valid_summary_table and df.shape[1] <= 2:
                    return True
                continue
            if kw.isascii():
                hit = bool(re.search(r'\b' + re.escape(kw) + r'\b', col_str))
            else:
                hit = kw in col_str
            if hit:
                if kw in strong_metadata_kws:
                    strong_metadata_hits += 1
                else:
                    weak_metadata_hits += 1

    has_numbers = any(re.search(r'\d', str(v)) for v in df.values.flatten() if v is not None)
    if strong_metadata_hits >= 1 or (weak_metadata_hits >= 2 and (df.shape[0] <= 3 or not has_numbers)):
        return True

    # 3c. 过滤插图说明 / 图注（Fig. / Figure / 图 / 附图）：
    # 仅当表格窄小（列数 <= 2 且行数 <= 3）且无正规表号、表头或首格本身为长句图注时才判定为图注，杜绝数据格引用 "Fig. 1" / "图1" 时一票否决整表
    if not has_explicit_table_label and df.shape[1] <= 2 and df.shape[0] <= 3:
        top_cells_text = [str(col).strip() for col in df.columns]
        for r in range(min(2, len(df))):
            for c in range(min(2, len(df.columns))):
                val = str(df.iloc[r, c]).strip()
                if val and val != 'nan':
                    top_cells_text.append(val)
        for txt in top_cells_text:
            if len(txt) >= 15 and re.search(r'^(?:Fig(?:ure|\.)?|图\s*\d+|附图\s*\d+|Plate\s*\d+|Photo\s*\d+)\s*[:.\s\d]', txt, re.IGNORECASE):
                return True

    # 检查表头是否全部为页码与期刊运行眉题（如 216 F. An, Y. Zhu / Ore Geology Reviews）
    header_str = " ".join(str(c) for c in df.columns).lower()
    if any(kw in header_str for kw in ['ore geology reviews', 'geochimica', 'sciencedirect', 'journal of', 'mineralium deposita', 'economic geology', 'gsa bulletin']):
        if re.search(r'\b(?:19|20)\d{2}\b', header_str) and ('/' in header_str or 'vol.' in header_str or 'pp.' in header_str):
            # 如果下方前2行也没有正规科学列名，则判定为页眉/非数据表
            first_row_str = " ".join(str(x) for x in df.iloc[0]).lower() if len(df) > 0 else ""
            if any(kw in first_row_str for kw in ['fig.', 'figure', '图', 'table 1.', 'table 2.', 'table 3.']):
                return True

    # 3d. 过滤无编号文献列表 / 参考文献段落（Harvard 格式等）
    # 识别包含 ±、区间 –/-、不等号 <、百分号/千分号的科学数值，避免把带文献列的同位素/矿床汇总表误判为 References
    all_table_text = " ".join(str(v) for v in df.values.flatten() if v is not None and str(v) != 'nan').translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    year_citations = len(re.findall(r'\b(?:19|20)\d{2}\b', all_table_text))
    et_al_hits = len(re.findall(r'\b(?:et\s+al\.|pp\.|vol\.|journal|thesis|dissertation)\b', all_table_text, re.IGNORECASE))
    if (year_citations >= 5 or et_al_hits >= 3 or '参考文献' in all_table_text) and len(df) >= 4 and not is_valid_summary_table and df.shape[1] <= 3:
        sci_num_re = re.compile(r'^(?:[<>≤≥±~]?\s*[-+]?\d+(?:\.\d+)?(?:\s*(?:[±/–\-~至]\s*[-+]?\d+(?:\.\d+)?)?)?\s*(?:[%‰]|ppm|ppb|wt%|Ma|Ga|ka|°C)?)$', re.IGNORECASE)
        numeric_count = sum(1 for v in df.values.flatten() if v is not None and sci_num_re.match(str(v).strip()))
        num_ratio = numeric_count / max(1, df.size)
        if num_ratio < 0.15:
            return True

    # 3e. 数值占比与多列散文检查：纯文字定性描述段落误判为表格（全面支持中文双栏正文与英文长句）
    if has_prose_headers and not is_valid_summary_table:
        return True

    if df.shape[0] >= 3 and df.shape[1] >= 2:
        total_cells = 0
        numeric_cells = 0
        long_prose_cells = 0
        for val in df.values.flatten():
            if val is None:
                continue
            val_str = str(val).strip()
            if not val_str or val_str == 'nan':
                continue
            total_cells += 1
            # 判定长句/正文散文单元格（排除顿号、地质类型名词短语，精准锁定连贯叙述句）
            is_cn_prose = (
                any(p in val_str for p in ['。', '；', '！', '？']) or
                (len(val_str) > 25 and '，' in val_str) or
                bool(re.search(r'(?:进行了|分析在|根据|由于|表明|可以|可知|显示|其中|本次研究|实验室完|测试过程|目前尚不)', val_str)) or
                (len(val_str) > 30 and re.search(r'[\u4e00-\u9fa5]{15,}', val_str))
            )
            is_en_prose = (len(val_str) > 30 and ' ' in val_str and ('.' in val_str or ';' in val_str))
            if is_cn_prose or is_en_prose:
                long_prose_cells += 1
            if re.search(r'[-+]?\d+(?:[.\-–～~±]\d+)?\s*(?:Ma|Ga|ka|ppm|ppb|‰|％|%|°C|bar|kbar|Mpa|Gpa)?', val_str):
                if re.search(r'\d+(?:\.\d+)?', val_str):
                    numeric_cells += 1
        if total_cells > 0:
            numeric_ratio = numeric_cells / total_cells
            prose_ratio = long_prose_cells / total_cells
            if not is_valid_summary_table:
                if prose_ratio >= 0.40 and (numeric_ratio < 0.15 or df.shape[1] <= 2):
                    return True
                if prose_ratio > 0.25 and numeric_ratio < 0.15:
                    return True
            elif prose_ratio >= 0.80 and numeric_ratio == 0 and df.shape[1] <= 2 and not has_explicit_table_label:
                return True
            named_cols = [c for c in df.columns if str(c).strip() and not str(c).strip().isdigit() and not str(c).startswith('Col') and not str(c).startswith('Unnamed') and not str(c).startswith('_')]
            if len(named_cols) == 0 and prose_ratio > 0.20 and numeric_ratio < 0.15:
                return True

    # 2. Check first few rows (多特征共现与强特征命中，防止单行粘连页脚误杀多行真实数据表)
    text_content = ""
    strong_meta_row_count = 0
    head_rows = df.head(8)
    for idx, row in head_rows.iterrows():
        row_lower = " ".join(str(val) for val in row).lower()
        text_content += row_lower + " "
        if any((re.search(r'\b' + re.escape(kw) + r'\b', row_lower) if kw.isascii() else (kw in row_lower)) for kw in strong_metadata_kws):
            strong_meta_row_count += 1

    if strong_meta_row_count >= 1 and (df.shape[0] <= 3 or strong_meta_row_count / max(1, len(head_rows)) >= 0.5):
        return True

    content_weak_hits = 0
    for kw in metadata_keywords:
        if kw in col_only_keywords or kw in strong_metadata_kws:
            continue
        hit = bool(re.search(r'\b' + re.escape(kw) + r'\b', text_content)) if kw.isascii() else (kw in text_content)
        if hit:
            content_weak_hits += 1

    if content_weak_hits >= 3 and not is_valid_summary_table and (df.shape[0] <= 3 or not has_numbers):
        return True

    # 3. Check if it's a reference list (e.g. bibliography where first col has bracketed numbers like [1], [2])
    if len(df.columns) > 0:
        try:
            first_col = df.iloc[:, 0].dropna()
            if len(first_col) > 2:
                citation_rows = 0
                for val in first_col:
                    val_str = str(val).strip()
                    if val_str.startswith('[') and ']' in val_str:
                        # Simple check if there's a number inside brackets
                        inside = val_str[1:val_str.index(']')]
                        if inside.isdigit():
                            citation_rows += 1
                if (citation_rows / len(first_col)) > 0.5:
                    return True
        except Exception:
            pass

    # 3b. CNKI 引文块特征：单列表 + 行首含 "题名：" / "作者：" / "DOI码：" 等
    if df.shape[1] == 1 and df.shape[0] >= 5:
        first_col_vals = [str(v).strip() for v in df.iloc[:, 0].dropna()]
        cnki_label_count = sum(1 for v in first_col_vals if re.match(
            r'^(?:题名|作者|来源|出版机构|出版年|DOI码|注册时间|以下是您获得的URL地址)[：:]', v))
        if cnki_label_count >= 3:
            return True

    # 3b2. 附表目录 Sheet：内容为 "Table S1: ..." / "Supplementary Table N: ..." 描述行
    if df.shape[1] <= 3 and df.shape[0] >= 3:
        all_text = " ".join(str(v).strip() for v in df.values.flatten() if v is not None and str(v).strip()).lower()
        toc_patterns = [
            r'table\s*s\d+\s*[:：-]',
            r'supplementary\s+table\s*\d+\s*[:：-]',
            r'附表\s*\d+\s*[:：-]',
            r'description\s+of\s+(?:samples|table)',
        ]
        toc_hits = sum(1 for p in toc_patterns if re.search(p, all_text))
        if toc_hits >= 2:
            return True

    # 3c. 数值占比检查：纯文字定性描述表（数值单元格占比 < 10%）
    #     修复：地质学中有很多合法的文字为主数据表（特征比较表、成矿阶段表），
    #     需要区分"结构化文字表"（有明确列字段名）和"散文段落"（无列结构）。
    if df.shape[0] >= 3 and df.shape[1] >= 2:
        total_cells = 0
        numeric_cells = 0
        for val in df.values.flatten():
            if val is None:
                continue
            val_str = str(val).strip()
            if not val_str:
                continue
            total_cells += 1
            # 改进数字识别：识别带单位/范围符号的数字（如 "482 Ma", "482～646", "119Ma"）
            if re.search(r'\d+(?:[.\-～~]\d+)?\s*(?:Ma|Ga|ka|ppm|ppb|‰|％|%|°C|bar|kbar|Mpa|Gpa)?', val_str):
                # 至少含一个数字 token
                if re.search(r'\b\d+(?:\.\d+)?\b', val_str):
                    numeric_cells += 1
        if total_cells > 0:
            numeric_ratio = numeric_cells / total_cells
            avg_cell_len = sum(len(str(v).strip()) for v in df.values.flatten() if v is not None and str(v).strip()) / max(1, total_cells)
            # 检查是否有明确列名或表标签（科学数据表/定性对比表不过滤）
            has_structured_header = False
            named_cols = [c for c in df.columns if str(c).strip() and not str(c).strip().isdigit() and not str(c).startswith('Col') and not str(c).startswith('Unnamed')]
            if len(named_cols) >= 2 or df.attrs.get('label'):
                has_structured_header = True
            elif df.shape[1] >= 3:
                first_row = [str(c).strip() if c is not None else '' for c in df.iloc[0]]
                non_numeric_headers = sum(1 for c in first_row if c and not c.replace('.', '').replace('-', '').replace(',', '').isdigit())
                if non_numeric_headers >= 2:
                    has_structured_header = True
            # 纯文字段落：无列名、数值占比 < 5% 且单元格平均长度 > 35
            if numeric_ratio < 0.05 and avg_cell_len > 35 and not has_structured_header:
                return True

    # 4. Reject extremely small tables that are mostly prose blocks
    #    (extended heuristics for general publisher pages)
    #    修复：旧版 shape[0]<=2 or shape[1]<=1 一律拒绝，误杀合法小表
    #    （1 行汇总表、单列样品清单、2 行 3 列小表）。
    #    新版：仅当「行少 AND 列少 AND 单元格为长文本（散文）」时拒绝。
    if df.shape[0] <= 1 and df.shape[1] <= 1:
        return True
    if df.shape[0] <= 3 and df.shape[1] <= 2:
        has_numeric = False
        max_cell_len = 0
        non_empty_cells = []
        for val in df.values.flatten():
            if val is not None:
                val_str = str(val).strip()
                if val_str:
                    non_empty_cells.append(val_str)
                    if re.search(r'\d', val_str) and not re.search(r'[a-zA-Z]{15,}', val_str):
                        has_numeric = True
                    # 中文每字符信息量高，15 汉字 ≈ 30 英文字符
                    chinese_count = len(re.findall(r'[\u4e00-\u9fa5]', val_str))
                    effective_len = chinese_count * 2 + (len(val_str) - chinese_count)
                    max_cell_len = max(max_cell_len, effective_len)
        avg_len = sum(len(c) for c in non_empty_cells) / len(non_empty_cells) if non_empty_cells else 0
        # 含数字且单元格短 -> 合法数据表，不拒绝
        if has_numeric and max_cell_len <= 30:
            return False
        # 无数字且有长单元格 -> 散文块，拒绝
        if not has_numeric and max_cell_len > 30:
            return True
        # 无数字且全部短文本 -> 仍可能是分类列表，保留

    return False


_SINGLE_COL_CHROME_RE = re.compile(
    r'(?:摘要|关键词|基金项目|引用本文|收稿日期|修回日期|点击次数|下载次数|下载全文|'
    r'上一篇|下一篇|版权所有|ISSN|CNKI|DOI\s*[:：]|Abstract|Keywords|Download|Citation|'
    r'Copyright|References|E-?mail|通讯作者|作者简介)',
    re.IGNORECASE,
)


def is_unlabeled_single_col_layout_shell(df, has_explicit_label: bool = False) -> bool:
    """
    区分「单列外框/导航/摘要排版壳」（如 view_abstract.aspx 无表头且含论文导航/长散文的 1 列表）
    与「合法单列数据表」（如 <table><tr><th>Items</th></tr><tr><td>Alpha</td></tr></table>）。
    """
    if df is None or df.empty:
        return True
    if df.shape[1] >= 2:
        return False
    if has_explicit_label or df.attrs.get('label') or df.attrs.get('table_title'):
        return False
    if df.shape[1] == 0:
        return True

    col0 = df.columns[0]
    col0_s = str(col0).strip() if col0 is not None else ""
    if not col0_s or col0_s.isdigit() or col0_s.lower().startswith("unnamed:") or col0_s.lower().startswith("col"):
        return True

    texts = [col0_s]
    for val in df.iloc[:, 0].tolist():
        if val is not None:
            s = str(val).strip()
            if s and s.lower() != "nan":
                texts.append(s)
    if len(texts) <= 1:
        return True
    for t in texts:
        if len(t) > 80 or _SINGLE_COL_CHROME_RE.search(t):
            return True
    return False


_ANTI_BOT_MARKERS = (
    "just a moment",
    "请稍候",
    "performing security verification",
    "attention required",
    "cf-browser-verification",
    "cf-challenge",
    "cf-turnstile",
    "challenges.cloudflare.com",
    "__cf_chl_",
    "challenge-platform",
    "cf_styles",
    "checking your browser",
    "access denied",
    "are you a robot",
    "verify you are human",
    "there was a problem",
    "enable javascript and cookies to continue",
    "radware bot manager",
    "bot manager",
    "validate.perfdrive.com",
    "bm-verify",
    "datadome",
    "perimeterx",
    "incapsula",
    "imperva",
    "/tspd/",
    "apm_do_not_touch",
    "安全验证",
    "your privacy, your choice",
)


def is_anti_bot_blocked(title: str = "", html_or_text: str = "") -> bool:
    """Return True if a fetched page title or HTML/text indicates an anti-bot or challenge block."""
    t_low = (title or "").strip().lower()
    if t_low in ("sciencedirect", "cloudflare", "error", "403", "page not found", "security verification"):
        return True
    if t_low and any(marker in t_low for marker in _ANTI_BOT_MARKERS):
        return True

    raw = html_or_text or ""
    if not raw:
        return False

    raw_head = raw[:12000].lower()
    # Genuine full-text academic HTML pages (with citation_doi/citation_title metadata or large article HTML)
    # may embed GDPR consent text ("your privacy, your choice") or JS strings ("there was a problem").
    # Do not false-positive them as challenge shells when their <title> is not an anti-bot page.
    has_article_meta = (
        'name="citation_doi"' in raw_head
        or "name='citation_doi'" in raw_head
        or 'name="citation_title"' in raw_head
        or "name='citation_title'" in raw_head
        or 'name="dc.identifier"' in raw_head
        or (len(raw) > 30000 and ("<article" in raw_head or "<table" in raw[:40000].lower()))
    )
    if has_article_meta:
        m_title = re.search(r"<title[^>]*>(.*?)</title>", raw_head, re.IGNORECASE | re.DOTALL)
        html_title = re.sub(r"\s+", " ", m_title.group(1)).strip() if m_title else ""
        if html_title and (
            html_title in ("sciencedirect", "cloudflare", "error", "403", "page not found", "security verification")
            or any(m in html_title for m in _ANTI_BOT_MARKERS)
        ):
            return True
        return False

    return any(marker in raw_head for marker in _ANTI_BOT_MARKERS)
