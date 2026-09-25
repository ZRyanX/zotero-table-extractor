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

try:
    from .models import ExtractedTable, TableCell
except ImportError:
    try:
        from models import ExtractedTable, TableCell
    except ImportError:
        ExtractedTable = None
        TableCell = None


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


def load_config():
    """Loads config.json from the skill root, applying OS-specific profile overrides.

    敏感配置支持环境变量覆盖：
    - FIRECRAWL_API_KEY ← $FIRECRAWL_API_KEY
    - ELSEVIER_API_KEY ← $ELSEVIER_API_KEY
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

    # 跨平台路径回退（macOS, Linux, Windows 统一生效）
    if not data.get("PAPER_TABLES_DIR"):
        data["PAPER_TABLES_DIR"] = get_paper_tables_dir()
    if not data.get("ZOTERO_DB_PATH"):
        data["ZOTERO_DB_PATH"] = get_zotero_db_path()

    # 默认多云与引擎配置
    if "MINERU_API_KEY" not in data:
        data["MINERU_API_KEY"] = ""
    if "MINERU_API_BASE" not in data:
        data["MINERU_API_BASE"] = "https://mineru.net"
    if "OCR_ENGINE" not in data:
        data["OCR_ENGINE"] = "auto"

    # 环境变量覆盖敏感配置
    env_overrides = {
        'FIRECRAWL_API_KEY': 'FIRECRAWL_API_KEY',
        'ELSEVIER_API_KEY': 'ELSEVIER_API_KEY',
        'PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN': 'PADDLEOCR_ACCESS_TOKEN',
        'MINERU_API_KEY': 'MINERU_API_KEY',
        'MINERU_API_BASE': 'MINERU_API_BASE',
        'OCR_ENGINE': 'OCR_ENGINE',
    }
    for config_key, env_key in env_overrides.items():
        env_val = os.getenv(env_key)
        if env_val:
            data[config_key] = env_val

    return data


# ---------------------------------------------------------------------------
# 浏览器提取全局锁（并行模式下的序列化保护）
# ---------------------------------------------------------------------------

_BROWSER_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def browser_extraction_lock(name="browser_extraction"):
    """
    浏览器类提取（Playwright 调试 Chrome / 共享 user-data-dir）的全局互斥锁。

    并行提取时（HTML ∥ Paddle 竞速、批量多论文并发），基于共享浏览器状态的
    渠道（调试端口 9222、共享 profile 目录）不能同时运行，否则会出现端口冲突、
    profile 文件锁竞争甚至互相 killall 浏览器的问题。

    本锁提供两层保护：
      1. 进程内 threading.Lock —— 保护同进程内多线程并发；
      2. 跨进程文件锁（fcntl / msvcrt）—— 保护 batch_run.py 多子进程并发。

    非浏览器类渠道（HTTP 直连 / curl_cffi / Firecrawl / Scrapling / Crawl4AI
    独立浏览器实例）无需此锁，可自由并行。
    """
    lock_path = os.path.join(tempfile.gettempdir(), f"zotero_table_extractor_{name}.lock")
    _BROWSER_THREAD_LOCK.acquire()
    f = None
    try:
        try:
            f = open(lock_path, "a+")
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        yield
    finally:
        try:
            if f:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                f.close()
        except Exception:
            pass
        _BROWSER_THREAD_LOCK.release()


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


def load_cookies_from_file(file_path):
    """
    Parses cookies from a Netscape cookie file or JSON cookie file.
    Returns a list of dicts suitable for Playwright's context.add_cookies().
    """
    cookies = []
    if not file_path or not os.path.exists(file_path):
        return cookies
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()

        # Try JSON first
        try:
            data = json.loads(content)
            if isinstance(data, list):
                for c in data:
                    cookie = {
                        "name": c.get("name"),
                        "value": c.get("value"),
                        "domain": c.get("domain"),
                        "path": c.get("path", "/"),
                        "secure": c.get("secure", False),
                        "httpOnly": c.get("httpOnly", False)
                    }
                    if "expirationDate" in c:
                        cookie["expires"] = int(c["expirationDate"])
                    elif "expires" in c:
                        cookie["expires"] = int(c["expires"])
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


def clean_text(val):
    if val is None:
        return ""
    val = str(val).strip()
    val = val.replace('\x00', '')
    val = val.replace('\r', '').replace('\n', ' ')
    val = " ".join(val.split())
    return val

def clean_latex_and_ocr(val):
    """Clean raw LaTeX math markup, OCR typos, and garbled symbols."""
    if not isinstance(val, str) or not val:
        return val

    # 0. Remove HTML tags if present
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

    # Superscripts / subscripts cleanup: ^{206} -> 206, _{...} -> ...
    val = re.sub(r'\^\{+([0-9a-zA-Z\+\-\*\s]+)\}+', r'^\1', val)
    val = re.sub(r'_\{+([0-9a-zA-Z\+\-\*\s]+)\}+', r'_\1', val)
    val = re.sub(r'(?<=\d)\s*times\s*(?=\d)', ' × ', val)

    # Strip LaTeX $ delimiters and braces
    val = val.replace('$', '').replace('\\', '').replace('{', '').replace('}', '')
    val = re.sub(r'([A-Za-z])_([0-9]+)', r'\1\2', val)
    val = re.sub(r'\s*±\s*', ' ± ', val)
    val = re.sub(r'\s*\^\s*([0-9+-]+)\s*', r'^\1', val)

    # 4. Fix common OCR typos in chemical formulas & mineral terms
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

    # Check if it already starts with "表几" or "Table几" or "Supplementary material几" or "Additional Table几" or "Additional file几"
    pattern = r'^([表T][a-z]*\.?\s*(?:S\s*)?(?:\d+|[ivxlcdm]+|ni|rn|[a-z])|Supplementary\s+material\s+(?:\d+|[ivxlcdm]+|ni|rn|[a-z])|Additional\s+Table\s+(?:\d+|[ivxlcdm]+|ni|rn|[a-z])|Additional\s+file\s+(?:\d+|[ivxlcdm]+|ni|rn|[a-z]))'
    if not re.match(pattern, cleaned, re.IGNORECASE):
        return f"Table {index} {cleaned}"

    return cleaned


HYPHEN_CHARS = r'[\-\u2010\u2011\u2012\u2013\u2014\u2015—–._~]'
CHINESE_DIGITS = r'[一二三四五六七八九十百千万零壹贰叁肆伍陆柒捌玖拾]'
ROMAN_DIGITS = r'[\u2160-\u217fIVXLCDMivxlcdm]'
FULLWIDTH_DIGITS = r'[\uff10-\uff19]'
TABLE_NUM_PART = r'(?:[0-9\uff10-\uff19]+|[A-Za-z]|' + CHINESE_DIGITS + r'+|' + ROMAN_DIGITS + r'+)'
TABLE_LABEL_PATTERN = r'(?:附表|附录表|补充表|表|Supplementary\s+Table|Supplemental\s+Table|Extended\s+Data\s+Table|Appendix\s+Table|TABLE|Table|Tab\.)\s*(?:' + TABLE_NUM_PART + r'(?:\s*' + HYPHEN_CHARS + r'\s*' + TABLE_NUM_PART + r')*(?:\([a-zA-Z0-9]\)|（[a-zA-Z0-9]）|[a-zA-Z])?)'
TABLE_LABEL_RE = re.compile(TABLE_LABEL_PATTERN, re.IGNORECASE)


def format_table_label(raw_label: str) -> str:
    """
    规范化表号名称，严密保留并标准化文中的所有真实命名形式。
    支持罗马数字 (Table I/IV)、S-附表 (Table S1)、中文汉字 (表一/附表一)、章节多级表号以及严格剥离标题首词。
    """
    if not raw_label:
        return ""
    trans_fw = str.maketrans('０１２３４５６７８９', '0123456789')
    s = raw_label.strip().replace('\xa0', ' ').replace('\u3000', ' ').translate(trans_fw)
    
    # 截断紧随在表号后面的标题部分（如 "Table 1. Description", "Table 1: Geological", "Table 1 - Summary"）
    s = re.sub(r'^((?:Table|TABLE|Tab\.|附表|表|Supplementary\s+Table)\s*(?:S\s*)?(?:\d+|[IVXLCDMivxlcdm]+|[一二三四五六七八九十]+)(?:[\.\-–—]\d+)?)(?:[\.\:\：\—\–\s]+[A-Za-z\u4e00-\u9fa5].*)?$', r'\1', s, flags=re.IGNORECASE).strip()

    # 1. 中文表号规范化
    m_cn = re.match(r'^(附表|附录表|补充表|表)\s*([A-Za-z0-9\u4e00-\u9fa5\u2160-\u217fIVXLCDMivxlcdm]+(?:\s*' + HYPHEN_CHARS + r'\s*[A-Za-z0-9\u4e00-\u9fa5\u2160-\u217fIVXLCDMivxlcdm]+)*(?:\([a-zA-Z0-9]\)|（[a-zA-Z0-9]）|[a-zA-Z])?)$', s, re.IGNORECASE)
    if m_cn:
        prefix, num = m_cn.groups()
        num_clean = re.sub(r'[\s\u2010\u2011\u2012\u2013\u2014\u2015—–_]', '-', num)
        num_clean = re.sub(r'-{2,}', '-', num_clean).strip('-')
        return f"{prefix}{num_clean}"

    # 1b. 纯数字章节表号（如 4.9, 4.10, 4.11）
    m_num = re.match(r'^(\d+\.\d+(?:\.\d+)?)$', s)
    if m_num:
        return f"表{m_num.group(1)}"
        
    # 2. 英文表号规范化
    m_en = re.match(r'^(Supplementary\s+Table|Supplemental\s+Table|Extended\s+Data\s+Table|Appendix\s+Table|TABLE|Table|Tab\.)\s*([A-Za-z0-9IVXLCDMivxlcdm]+(?:[-—–._][A-Za-z0-9IVXLCDMivxlcdm]+)*(?:\([a-zA-Z0-9]\)|[a-zA-Z])?)$', s, re.IGNORECASE)
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
    """
    if df is None or df.empty or df.shape[0] < 2:
        return True

    # 1. 检查是否混入了正文段落或论文元数据（基金项目、作者简介、章节标题等）
    article_metadata_kws = ['基金项目', '作者简介', '中图分类号', '文献标识码', '收稿日期', '通信作者', '引用格式', 'received:', 'accepted:', 'revised:']
    prose_pollution_count = 0
    
    # 检查表头是否属于地质学合法的文本/描述型表格
    header_str = ' '.join(str(c) for c in df.columns).lower()
    is_valid_summary_table = any(kw in header_str for kw in ['时间', '单位', '内容', '特征', '描述', '层位', '构造', '类型', '阶段', '矿物', '样品', '岩性', '年龄', '方法', '来源', '编号', '地点', '产状'])
    
    for r in range(df.shape[0]):
        row_cells = [str(df.iat[r, c]).strip() for c in range(df.shape[1]) if str(df.iat[r, c]).strip() not in ['', 'nan', 'none']]
        row_str = ' '.join(row_cells)
        # 匹配论文元数据行
        if any(kw in row_str.lower() for kw in article_metadata_kws):
            prose_pollution_count += 1
            continue
        # 匹配章节标题（如 1.2 地质背景）
        if re.match(r'^\d+\.\d+(?:\.\d+)?\s*[\u4e00-\u9fa5]{2,}', row_str):
            prose_pollution_count += 1
            continue
        # 匹配大面积正文陈述句（整行只有 1 个单元格且包含句号长句）
        if len(row_cells) == 1 and len(row_str) >= 30 and '。' in row_str and not is_valid_summary_table:
            prose_pollution_count += 1
            continue

    # 只要包含 2 行以上论文正文/元数据污染行，判定为污染表
    if prose_pollution_count >= 2:
        return True

    # 2. 检查是否有竖排文字拆行导致的碎片化（例如某列连续多行只有单个汉字：木、利、锑、矿、床）
    for c in range(min(3, df.shape[1])):
        col_vals = [str(df.iat[r, c]).strip() for r in range(df.shape[0]) if str(df.iat[r, c]).strip() not in ['', 'nan', 'none']]
        single_chars = [v for v in col_vals if len(v) == 1 and re.match(r'[\u4e00-\u9fa5]', v)]
        if len(single_chars) >= 4:
            return True

    # 3. 检查空单元格占比（稀疏度过高通常为错位散文或误切）
    # 豁免：地质化学元素分析表/同位素比值表/相关性矩阵天然存在下三角未测空白
    geo_elem_pattern = r'\b(?:SiO2|TiO2|Al2O3|Fe2O3|FeO|MnO|MgO|CaO|Na2O|K2O|P2O5|LOI|Total|Fe|Cu|Pb|Zn|Au|Ag|Sb|As|Hg|W|Mo|Bi|Co|Ni|Cr|V|Ba|Sr|Rb|Cs|Zr|Hf|Nb|Ta|Th|U|La|Ce|Pr|Nd|Sm|Eu|Gd|Tb|Dy|Ho|Er|Tm|Yb|Lu|Y|REE|δ34S|δ18O|δ13C|δD|206Pb/204Pb|207Pb/204Pb|208Pb/204Pb|wt%|ppm|ppb|Ma|Ga|ka|测点|样品|矿物)\b'
    col_str_joined = " ".join(str(c) for c in df.columns)
    is_geochem_table = bool(re.search(geo_elem_pattern, col_str_joined, re.IGNORECASE))

    total_cells = df.shape[0] * df.shape[1]
    non_empty = sum(1 for v in df.values.flatten() if v is not None and str(v).strip() and str(v).strip().lower() not in ['', 'nan', 'none'])
    if df.shape[0] >= 3 and df.shape[1] >= 3 and (non_empty / total_cells) < 0.25 and not is_geochem_table:
        return True

    # 4. 检查是否存在 100% 空白列（原生提取常见列切分损坏）
    for c in range(df.shape[1]):
        col_cells = [str(df.iat[r, c]).strip() for r in range(df.shape[0]) if str(df.iat[r, c]).strip() not in ['', 'nan', 'none']]
        if not col_cells and df.shape[1] > 2:
            return True

    # 5. 检查是否为散点图/直方图坐标轴与图表刻度残片（如含 Watson et al 2006, 700, 900, kbar, T , °C）
    all_cells_flat = [str(df.iat[r, c]).strip() for r in range(df.shape[0]) for c in range(df.shape[1]) if str(df.iat[r, c]).strip() not in ['', 'nan', 'none']]
    all_str_concat = " ".join(all_cells_flat).lower()
    if any(kw in all_str_concat for kw in ['et al 200', 'et al 199', 'et al., 20', 'kbar', 't , °c', 't (°c)', 'wt% tio2', 'wt% sio2']):
        # 若没有明确的数据列字段名且行数较少，判定为图表坐标轴残片
        has_named_header = any(str(c).lower().strip() in ['sample', 'mineral', 'au', 'sb', 'fe', 'as', '样品', '矿物', '含量'] for c in df.columns)
        if not has_named_header and df.shape[0] <= 20:
            return True

    return False


def is_supplementary(title_or_label):
    if not title_or_label:
        return False
    title_lower = str(title_or_label).lower()
    if any(kw in title_lower for kw in ["supplement", "suppl", "supp_", "esm", "appendix", "附表", "si_", "additional"]):
        return True
    if re.search(r'\bS\d+', str(title_or_label)) or re.search(r'Table\s+S\d+', str(title_or_label), re.IGNORECASE) or re.search(r'Tab\.\s+S\d+', str(title_or_label), re.IGNORECASE):
        return True
    return False


def is_metadata_table(df):
    """
    Checks if a DataFrame represents literature metadata, citation info, or reference lists.
    Includes size/prose heuristics for general publisher pages (merged from general_html_extractor).

    修复：
    1. metadata_keywords 新增 CNKI 引文块关键词（题名/出版机构/DOI码/注册时间/同方知网/link.cnki.net），
       旧版缺失导致 28 个 CNKI 文献元数据表未被过滤。
    2. 新增数值占比检查：对多行多列表，若数值单元格占比 < 10% 且无字段名行，
       判定为纯文字定性描述表（影响 102 个表）。
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
        # 摘要页元数据框关键词（作者/单位/摘要/关键词/版权/目录）
        "摘要", "关键词", "版权", "目录", "outline", "table of contents",
        "点击次数", "下载次数", "引用次数", "被引",
        "摘要点击", "下载", "全文链接",
        # 摘要页作者/单位布局表列名（中文）
        # 注意：这些词仅在列名检查中生效，不用于内容检查（避免"测试单位"误匹配）
        "作者", "单位",
    ]
    
    # 仅用于特定结构检查的关键词（避免误杀含"测试单位"或"参考文献"数据列的正常数据表）
    col_only_keywords = {"作者", "单位", "参考文献", "references", "reference"}

    # 1. Check if the headers or columns contain metadata keywords
    col_set = set(str(c).lower().strip() for c in df.columns)
    if "作者" in col_set and "单位" in col_set:
        return True
    if "作者简介" in col_set or "通讯作者" in col_set:
        return True

    header_col_strs = [str(c).strip() for c in df.columns]
    has_prose_headers = any(
        (len(sub) > 18 and re.search(r'[\u4e00-\u9fa5]{6,}', sub)) or
        re.search(r'(?:进行了|分析在|根据|由于|表明|可以|可知|显示|其中|本次研究|实验室完|测试过程|目前尚不)', c)
        for c in header_col_strs
        for sub in (c.split('_') if '_' in c else [c])
    )
    is_valid_summary_table = (
        not has_prose_headers and 
        any(any(kw == sub.lower() or (kw in sub.lower() and len(sub) <= 12) for kw in ['矿床', '年龄', '阶段', '样品', '矿物', '方法', '层位', '同位素', '特征', '年代', '定年', 'deposit', 'age', 'sample', 'mineral', 'method', 'isotope', 'stage', 'dating']) for c in header_col_strs for sub in (c.split('_') if '_' in c else [c]))
    )

    for col in df.columns:
        col_str = str(col).lower().strip()
        for kw in metadata_keywords:
            if kw in col_only_keywords:
                if kw in ("作者", "单位") and col_str == kw:
                    return True
                if kw in ("参考文献", "references", "reference") and (col_str == kw or col_str.startswith(kw)) and not is_valid_summary_table:
                    return True
                continue
            if kw.isascii():
                if re.search(r'\b' + re.escape(kw) + r'\b', col_str):
                    return True
            else:
                if kw in col_str:
                    return True

    # 3c. 过滤插图说明 / 图注（Fig. / Figure / 图 / 附图）
    # 扫描表头及前 4 行的所有单元格内容
    top_cells_text = []
    for col in df.columns:
        top_cells_text.append(str(col).strip())
    for r in range(min(4, len(df))):
        for c in range(min(6, len(df.columns))):
            val = str(df.iloc[r, c]).strip()
            if val and val != 'nan':
                top_cells_text.append(val)
                
    for txt in top_cells_text:
        if re.search(r'^(?:Fig(?:ure|\.)?|图\s*\d+|附图\s*\d+|Plate\s*\d+|Photo\s*\d+)\s*[:.\s\d]', txt, re.IGNORECASE):
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
    all_table_text = " ".join(str(v) for v in df.values.flatten() if v is not None and str(v) != 'nan').translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    year_citations = len(re.findall(r'\b(?:19|20)\d{2}\b', all_table_text))
    et_al_hits = len(re.findall(r'\b(?:et\s+al\.|pp\.|vol\.|journal|thesis|dissertation)\b', all_table_text, re.IGNORECASE))
    if (year_citations >= 5 or et_al_hits >= 3 or '参考文献' in all_table_text) and len(df) >= 4 and not is_valid_summary_table:
        numeric_count = sum(1 for v in df.values.flatten() if v is not None and re.match(r'^-?\d+(?:\.\d+)?$', str(v).strip()))
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
            if re.search(r'\d+(?:[.\-～~]\d+)?\s*(?:Ma|Ga|ka|ppm|ppb|‰|％|%|°C|bar|kbar|Mpa|Gpa)?', val_str):
                if re.search(r'\b\d+(?:\.\d+)?\b', val_str):
                    numeric_cells += 1
        if total_cells > 0:
            numeric_ratio = numeric_cells / total_cells
            prose_ratio = long_prose_cells / total_cells
            if prose_ratio >= 0.40:
                return True
            if prose_ratio > 0.25 and numeric_ratio < 0.15 and not is_valid_summary_table:
                return True
            named_cols = [c for c in df.columns if str(c).strip() and not str(c).strip().isdigit() and not str(c).startswith('Col') and not str(c).startswith('Unnamed') and not str(c).startswith('_')]
            if len(named_cols) == 0 and prose_ratio > 0.20 and numeric_ratio < 0.15:
                return True

    # 2. Check first few rows (排除 col_only_keywords 中的词，避免"测试单位"误匹配)
    text_content = ""
    for idx, row in df.head(8).iterrows():
        text_content += " ".join(str(val) for val in row).lower() + " "

    for kw in metadata_keywords:
        if kw in col_only_keywords:
            continue  # 跳过"作者"/"单位"——不在内容中检查
        if kw.isascii():
            if re.search(r'\b' + re.escape(kw) + r'\b', text_content):
                return True
        else:
            if kw in text_content:
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
