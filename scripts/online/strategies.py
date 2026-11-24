"""online.strategies — 各渠道在线表格抓取策略与 HTML/Markdown 解析（原 online_utils.py 拆分）。"""

import os
import re
import json
import time
import threading
import hashlib
import urllib.parse
import requests
import io
from typing import Any, Dict
import pandas as pd

try:
    from .. import common
except ImportError:
    import common

if hasattr(common, "ensure_macos_corefoundation"):
    common.ensure_macos_corefoundation()

try:
    from curl_cffi import requests as requests_impersonate
except ImportError:
    requests_impersonate = None

try:
    from ..playwright_utils import (
        find_playwright_chromium,
        resolve_sync_playwright,
        extract_browser_pid,
        close_browser_safely,
        get_thread_browser_pool,
    )
except ImportError:
    try:
        from playwright_utils import (
            find_playwright_chromium,
            resolve_sync_playwright,
            extract_browser_pid,
            close_browser_safely,
            get_thread_browser_pool,
        )
    except ImportError:
        find_playwright_chromium = lambda: None
        resolve_sync_playwright = None
        extract_browser_pid = lambda **kwargs: None
        close_browser_safely = lambda **kwargs: None
        get_thread_browser_pool = None


# Aliases to shared helpers (kept as bare names so call sites are unchanged)
get_cnki_cookies_path = common.get_cnki_cookies_path
get_crawl4ai_cookies_path = common.get_crawl4ai_cookies_path
load_config = common.load_config
load_cookies_from_file = common.load_cookies_from_file
get_requests_proxies = getattr(common, "get_requests_proxies", lambda config=None: None)
build_elsevier_headers = getattr(
    common,
    "build_elsevier_headers",
    lambda config=None, accept="application/json": (
        {"X-ELS-APIKey": (config or {}).get("ELSEVIER_API_KEY", ""), **({"Accept": accept} if accept else {})}
        if (config or {}).get("ELSEVIER_API_KEY")
        else {}
    ),
)
get_polite_email = getattr(common, "get_polite_email", lambda config=None, service="unpaywall": "supp_downloader@academic-research.org")
is_metadata_table = getattr(common, "is_metadata_table", lambda _df: False)
is_anti_bot_blocked = getattr(common, "is_anti_bot_blocked", lambda _t="", _h="": False)


_SUP_UNICODE = {'0':'⁰','1':'¹','2':'²','3':'³','4':'⁴','5':'⁵','6':'⁶','7':'⁷','8':'⁸','9':'⁹','+':'⁺','-':'⁻','−':'⁻','=':'⁼','(':'⁽',')':'⁾','n':'ⁿ','i':'ⁱ'}
_SUB_UNICODE = {'0':'₀','1':'₁','2':'₂','3':'₃','4':'₄','5':'₅','6':'₆','7':'₇','8':'₈','9':'₉','+':'₊','-':'₋','−':'₋','=':'₌','(':'₍',')':'₎','a':'ₐ','e':'ₑ','o':'ₒ','x':'ₓ','h':'ₕ','k':'ₖ','l':'ₗ','m':'ₘ','n':'ₙ','p':'ₚ','s':'ₛ','t':'ₜ'}

def _xml_cell_to_text(elem) -> str:
    """递归提取 XML 单元格文本，保留 <sup> 与 <sub> 为 Unicode 上下标或 LaTeX 语义表示，并正确处理换行与块级分隔。"""
    if elem is None:
        return ""
    parts = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        tag = child.tag.split('}')[-1].lower()
        if tag in ('break', 'br'):
            parts.append('\n')
            if child.tail:
                parts.append(child.tail)
            continue
        child_text = _xml_cell_to_text(child)
        if tag == 'sup':
            if child_text and all(c in _SUP_UNICODE for c in child_text):
                parts.append(''.join(_SUP_UNICODE[c] for c in child_text))
            else:
                parts.append(f'^{{{child_text}}}')
        elif tag == 'sub':
            if child_text and all(c in _SUB_UNICODE for c in child_text):
                parts.append(''.join(_SUB_UNICODE[c] for c in child_text))
            else:
                parts.append(f'_{{{child_text}}}')
        elif tag in ('p', 'div'):
            if parts and not parts[-1].endswith((' ', '\n')):
                parts.append(' ')
            parts.append(child_text)
        else:
            parts.append(child_text)
        if child.tail:
            parts.append(child.tail)
    return ''.join(parts).strip()


def _bs4_cell_to_text(cell) -> str:
    """递归提取 BeautifulSoup HTML 单元格文本，保留 <sup> 与 <sub> 为 Unicode 上下标或 LaTeX 语义表示，并正确处理换行与块级分隔。"""
    if cell is None:
        return ""
    parts = []
    for child in getattr(cell, 'contents', []):
        if isinstance(child, str):
            parts.append(str(child))
        elif hasattr(child, 'name') and child.name:
            tag = child.name.lower()
            if tag == 'br':
                parts.append('\n')
                continue
            inner = _bs4_cell_to_text(child)
            if tag == 'sup':
                if inner and all(c in _SUP_UNICODE for c in inner):
                    parts.append(''.join(_SUP_UNICODE[c] for c in inner))
                else:
                    parts.append(f'^{{{inner}}}')
            elif tag == 'sub':
                if inner and all(c in _SUB_UNICODE for c in inner):
                    parts.append(''.join(_SUB_UNICODE[c] for c in inner))
                else:
                    parts.append(f'_{{{inner}}}')
            elif tag in ('p', 'div'):
                if parts and not parts[-1].endswith((' ', '\n')):
                    parts.append(' ')
                parts.append(inner)
            else:
                parts.append(inner)
    return ''.join(parts).strip()


def _rows_to_dataframe(rows_data, has_explicit_header=None):
    """
    将 [[cell,...],...] 二维列表转为 DataFrame。
    优先保留源文件的表头标记 (has_explicit_header=True/False)；
    若未显式指定 (None)，基于真实表头和连续序号判据智能识别。
    """
    if not rows_data:
        return pd.DataFrame()
    # 对齐各行列数
    max_cols = max(len(r) for r in rows_data)
    for r in rows_data:
        if len(r) < max_cols:
            r.extend([""] * (max_cols - len(r)))

    if has_explicit_header is True:
        if len(rows_data) >= 2:
            return pd.DataFrame(rows_data[1:], columns=rows_data[0])
        elif len(rows_data) == 1:
            return pd.DataFrame([], columns=rows_data[0])
    if has_explicit_header is False:
        return pd.DataFrame(rows_data)

    first_row = [str(c).strip() if c is not None else "" for c in rows_data[0]]

    # 严格检验首行是否为连续列序号行 [0,1,2,...] 或 [1,2,3,...]
    # 绝不能仅凭“全为数字”误删合法的纯数值数据行
    is_true_index_row = False
    if len(rows_data) >= 3:
        clean_first = [c for c in first_row if c]
        int_candidates = []
        for c in clean_first:
            m = re.match(r'^(?:[\(\[]\s*)?(\d+)(?:\s*[\)\]])?$', c)
            if m:
                int_candidates.append(int(m.group(1)))
            else:
                break
        if len(int_candidates) >= 2 and len(int_candidates) == len(clean_first):
            start_val = int_candidates[0]
            if start_val in (0, 1) and int_candidates == list(range(start_val, start_val + len(int_candidates))):
                second_row = [str(c).strip() if c is not None else "" for c in rows_data[1]]
                non_numeric_2 = sum(1 for c in second_row if c and not c.replace('.', '').replace('-', '').replace(',', '').replace('%', '').replace('+', '').isdigit())
                if len(second_row) > 0 and non_numeric_2 >= max(1, len(second_row) // 3):
                    is_true_index_row = True

    if is_true_index_row:
        second_row = [str(c).strip() if c is not None else "" for c in rows_data[1]]
        return pd.DataFrame(rows_data[2:], columns=second_row)

    # 判断首行是否像表头（含非数字文本）
    non_numeric = sum(1 for c in first_row if c and not c.replace('.', '').replace('-', '').replace(',', '').replace('%', '').replace('+', '').isdigit())
    if len(first_row) > 0 and non_numeric >= max(1, len(first_row) // 3) and len(rows_data) >= 2:
        df = pd.DataFrame(rows_data[1:], columns=first_row)
    else:
        df = pd.DataFrame(rows_data)

    # 单列挤压检测
    if df.shape[1] == 1 and df.shape[0] >= 5:
        first_val = str(df.iloc[0, 0]) if pd.notna(df.iloc[0, 0]) else ""
        if '\t' in first_val or re.search(r'  {2,}', first_val):
            try:
                col_name = df.columns[0]
                expanded = df.iloc[:, 0].astype(str).str.split(r'\t|  +', expand=True)
                if expanded.shape[1] >= 2:
                    expanded.columns = [f"{col_name}_{i}" for i in range(expanded.shape[1])]
                    expanded.attrs = df.attrs.copy() if hasattr(df, 'attrs') else {}
                    df = expanded
            except Exception:
                pass

    return df


def get_domain_profile_dir(url):
    """
    Returns a persistent domain-isolated user profile directory inside the skill's root folder.
    E.g. d:/AIHub/skills/zotero-table-extractor/profiles/cnki
    """
    skill_dir = common.get_skill_root()
    profiles_dir = os.path.join(skill_dir, "profiles")
    os.makedirs(profiles_dir, exist_ok=True)
    
    domain = "general"
    if url:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()
        if "cnki.net" in netloc or "cnki.com.cn" in netloc:
            domain = "cnki"
        elif any(k in netloc for k in ["springer.com", "springerlink", "nature.com", "biomedcentral.com", "springeropen.com"]):
            domain = "springer"
        elif "sciencedirect.com" in netloc or "elsevier.com" in netloc:
            domain = "elsevier"
        elif "wiley.com" in netloc or "agu.org" in netloc:
            domain = "wiley"
        elif "tandfonline.com" in netloc or "taylorandfrancis.com" in netloc:
            domain = "taylor_and_francis"
        elif "mdpi.com" in netloc:
            domain = "mdpi"
        elif "frontiersin.org" in netloc:
            domain = "frontiers"
        elif any(k in netloc for k in ["geoscienceworld.org", "silverchair", "oup.com", "oxfordacademic"]):
            domain = "geoscienceworld"
            
    domain_path = os.path.join(profiles_dir, domain)
    os.makedirs(domain_path, exist_ok=True)
    return domain_path


def interactive_cnki_cookie_capture():
    """
    Opens a headed Playwright browser to www.cnki.net, lets the user log in,
    captures the cookies upon closing, and saves them to the home directory.
    """
    try:
        if resolve_sync_playwright:
            _cnki_sync_pw, _cnki_eng = resolve_sync_playwright(prefer_patchright=True)
        else:
            from playwright.sync_api import sync_playwright as _cnki_sync_pw
            _cnki_eng = "playwright"
        import json
        
        cookies_path = get_cnki_cookies_path()
        print("\n==================================================")
        print("  正在启动知网 Cookie 自动配置向导...")
        print("  1. 程序将为您打开一个可见的浏览器窗口。")
        print("  2. 请在网页中登录您的知网账号，或完成您的机构/校园网授权。")
        print("  3. 确认登录/授权成功后，直接【关闭浏览器窗口】即可。")
        print("  程序会自动获取并保存您的登录 Cookies 到本地用户文件夹下。")
        print("==================================================\n")
        
        with _cnki_sync_pw() as p:
            # Launch headed browser
            exec_path = find_playwright_chromium()
            if exec_path:
                print(f"[Playwright] 使用自动识别的 Chromium 路径: {exec_path}")
            browser = p.chromium.launch(headless=False, executable_path=exec_path)
            _cnki_pid = extract_browser_pid(browser=browser, playwright_obj=p)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            # Go to CNKI
            try:
                page.goto("https://www.cnki.net/", wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"警告：打开网页遇到问题（可能是网络延迟）：{e}")
                
            # Wait for user to close browser, and periodically capture cookies while browser is open
            closed = [False]
            def on_close():
                closed[0] = True
            page.on("close", on_close)
            
            import time
            last_valid_cookies = []
            
            # Keep waiting while page is not closed
            while not closed[0]:
                try:
                    # Capture cookies periodically while the browser context is still active
                    current_cookies = context.cookies()
                    if current_cookies:
                        last_valid_cookies = current_cookies
                    time.sleep(0.5)
                except Exception:
                    # If page/context gets closed, break the loop and use last captured cookies
                    break
                    
            # Extract target cookies
            cnki_cookies = [c for c in last_valid_cookies if "cnki.net" in c.get("domain", "") or "cnki.com.cn" in c.get("domain", "")]
            target_cookies = cnki_cookies if cnki_cookies else last_valid_cookies
            
            if not target_cookies:
                print("错误：未捕获到任何 Cookies，请确保已打开网页并进行登录。")
                return False
                
            # Save to user home directory
            try:
                with open(cookies_path, "w", encoding="utf-8") as f:
                    json.dump(target_cookies, f, indent=2, ensure_ascii=False)
                print(f"成功！已捕获 {len(target_cookies)} 个 Cookies 并保存到：{cookies_path}")
                return True
            except Exception as e:
                print(f"错误：保存 Cookie 文件失败：{e}")
                return False
    except Exception as e:
        print(f"自动配置 Cookie 失败：{e}")
        print("请检查是否正确安装了 Playwright (运行: playwright install chromium)")
        return False


def clean_text(val):
    """Clean invalid unicode characters, null bytes, and strip whitespace."""
    if val is None:
        return ""
    if not isinstance(val, str):
        val = str(val)
    val = val.replace('\x00', '')
    val = val.encode('utf-8', 'ignore').decode('utf-8')
    return val.strip()


_DOM_WRAPPER_SELECTORS = (
    "div.tables", ".c-article-table-container", ".html-table_show",
    "div[id*='-tbl-'].article-table-content", ".article-table-content",
    "div.ArticleTable", "figure.table", "div.article-fulltext-table-wrap",
    "div.boxout.boxout-bdr-grey",
    ".table-wrap", ".NLM_table-wrap", "figure.c-article-table__wrapper",
    "div.c-article-table", ".html-table-container", "section.article-section__table",
    "div[data-widget-def='tableWidget']", "div.entry-table", "div.table-item",
    ".table-wrap-content", ".table-box",
    "div.figure.table_new", ".article_table_fullText", ".tableTextBox",
)
_DOM_CAPTION_SELECTORS = (
    "span.captions", "div.captions", "header.u-mb-16", ".c-article-table__figcaption",
    ".html-caption", "header.article-table-caption", "header.article-fulltext-label-header",
    ".table-wrap-title", ".figureTitle", "figcaption", "caption", ".table-caption",
    ".c-article-table__caption", ".caption", ".table-title", ".table-header", ".html-table-title",
)
_DOM_LABEL_BADGE_SELECTORS = (
    ".table-label", ".table-wrap-label", "span.label", ".label",
    ".c-article-table__label", ".article-table-label",
)
_DOM_FOOTNOTE_SELECTORS = (
    "div.c-article-table-footer", ".html-table_foot", "div.article-section__table-footnotes",
    ".ArticleTable__foot", "div.notes", "dl.footnotes", ".legend", ".tbl-foot",
    ".table-wrap-foot", ".table-footnotes", ".c-article-table__notes",
    ".table-foot", ".table-footnote", ".table-note", ".html-table-footnote", "tfoot",
)


def _extract_normalized_table_label(text: str) -> str:
    """Normalize a table label prefix from caption/header text using common.TABLE_LABEL_RE when available."""
    if not text:
        return ""
    norm = re.sub(r'\s+', ' ', text.replace('\xa0', ' ').replace('\u3000', ' ')).strip()
    lbl_re = getattr(common, "TABLE_LABEL_RE", None)
    fmt_fn = getattr(common, "format_table_label", None)

    def _fmt(raw_val: str) -> str:
        raw_val = raw_val.strip()
        if callable(fmt_fn):
            formatted = fmt_fn(raw_val)
            if formatted:
                return formatted
        return raw_val

    if lbl_re is not None:
        m = re.match(r'^(?:' + lbl_re.pattern + r')', norm, re.IGNORECASE)
        if m:
            return _fmt(m.group(0))
        # Handle "Additional file 1: Table S1", "ESM 1. Table S1", or short prefix noise before real Table label
        m_sub = re.match(
            r'^(?:Additional\s+file\s+\d+|ESM\s*\d+|Electronic\s+Supplementary\s+Material(?:\s*\d+)?)\s*[:.\-\u2014]?\s*(.+)$',
            norm,
            re.IGNORECASE,
        )
        if m_sub:
            m_inner = re.match(r'^(?:' + lbl_re.pattern + r')', m_sub.group(1).strip(), re.IGNORECASE)
            if m_inner:
                return _fmt(m_inner.group(0))
    m = re.match(
        r'^((?:Supplementary\s+Table|Supplemental\s+Table|Extended\s+Data\s+Table|Appendix\s+Table|T\s*A\s*B\s*L\s*E\s*A\s*U\s*X?|T\s*A\s*B\s*L\s*E|T\s*A\s*B\s*\.|附表|附录表|补充表|表))\s*([A-Za-z]?\d+(?:[.\-]\d+)*|[ivxlcdm]+|[A-Za-z])',
        norm,
        re.IGNORECASE,
    )
    if m:
        raw_prefix = re.sub(r'\s+', '', m.group(1))
        num_part = m.group(2)
        if any(k in raw_prefix for k in ("附表", "附录表", "补充表", "表")):
            return f"{raw_prefix}{num_part}"
        if raw_prefix.upper() in ("TABLE", "TAB.", "TABLEAU", "TABLEAUX"):
            return _fmt(f"Table {num_part}")
        return _fmt(f"{m.group(1).strip()} {num_part}")
    return ""


def _infer_label_from_dom_id(el) -> str:
    """Infer normalized table label from DOM id/data-table attributes (e.g. 'tbl-0001' -> 'Table 1')."""
    if el is None:
        return ""
    for attr in ("id", "data-table", "aria-label"):
        val = str(el.get(attr) or "").strip()
        if not val:
            continue
        m_id = re.match(r'^(?:tbl|table|tab)[-_]?([sa])?0*(\d+(?:[-.]\d+)?)$', val, re.IGNORECASE)
        if m_id:
            kind = (m_id.group(1) or "").upper()
            num = m_id.group(2)
            if kind == "S":
                return f"Table S{num}"
            if kind == "A":
                return f"Table A{num}"
            return f"Table {num}"
        m_short = re.match(r'^t0*(\d+)$', val, re.IGNORECASE)
        if m_short:
            return f"Table {m_short.group(1)}"
        lbl = _extract_normalized_table_label(val)
        if lbl:
            return lbl
    return ""


def _extract_caption_and_label_from_wrapper(wrapper):
    """Extract caption text and normalized Table label from a BeautifulSoup table wrapper."""
    caption_text = ""
    ft_hdr = wrapper.select_one(".ArticleTable__header")
    ft_desc = wrapper.select_one(".ArticleTable__description")
    if ft_hdr or ft_desc:
        caption_text = ". ".join(
            s for s in (
                ft_hdr.get_text(" ", strip=True) if ft_hdr else "",
                ft_desc.get_text(" ", strip=True) if ft_desc else "",
            ) if s
        )
    if not caption_text:
        aps_hdr = wrapper.select_one("header.article-fulltext-label-header")
        aps_p = wrapper.select_one("p.article-fulltext-paragraph")
        if aps_hdr and aps_p:
            caption_text = clean_text(f"{aps_hdr.get_text(' ', strip=True)} {aps_p.get_text(' ', strip=True)}")
    if not caption_text:
        for sel in _DOM_CAPTION_SELECTORS:
            el = wrapper.select_one(sel)
            if el:
                txt = clean_text(el.get_text(" ", strip=True))
                if txt:
                    caption_text = txt
                    break

    # Silverchair/GSW 等将表号放在 .label / .table-label、描述放在 .caption 中时合并提取
    label_text = _extract_normalized_table_label(caption_text)
    if not label_text:
        for lsel in _DOM_LABEL_BADGE_SELECTORS:
            lel = wrapper.select_one(lsel)
            if lel:
                ltxt = clean_text(lel.get_text(" ", strip=True))
                cand_lbl = _extract_normalized_table_label(ltxt)
                if cand_lbl:
                    label_text = cand_lbl
                    if caption_text and cand_lbl.lower() not in caption_text.lower():
                        caption_text = f"{ltxt.rstrip('. ')}: {caption_text}"
                    elif not caption_text:
                        caption_text = ltxt
                    break

    # Springer /tables/N 子页 (<main><header class="u-mb-16"><h1>Table 1...</h1></header><div class="c-article-table-container">)
    # 与中文 Magtech 平台 (<div class="figure"><div class="figureTitle">表 1...</div><div class="article_table_fullText">...)
    if not caption_text or not label_text:
        sib_candidates = []
        prev = wrapper.find_previous_sibling()
        for _ in range(3):
            if prev is None:
                break
            if getattr(prev, "name", None):
                sib_candidates.append(prev)
            prev = prev.find_previous_sibling()
        parent = getattr(wrapper, "parent", None)
        if parent is not None and getattr(parent, "name", None) in ("main", "div", "section", "article"):
            for sel in ("header.u-mb-16", "h1.c-article-satellite-title", ".figureTitle"):
                try:
                    el = parent.select_one(f":scope > {sel}")
                except Exception:
                    el = None
                if el and el not in sib_candidates:
                    sib_candidates.append(el)
        for cand_el in sib_candidates:
            cand_txt = clean_text(cand_el.get_text(" ", strip=True))
            if not cand_txt:
                continue
            cand_lbl = _extract_normalized_table_label(cand_txt)
            if cand_lbl:
                label_text = label_text or cand_lbl
                if not caption_text:
                    caption_text = cand_txt
                elif cand_lbl.lower() not in caption_text.lower():
                    caption_text = f"{cand_txt} {caption_text}"
                break

    if caption_text:
        caption_text = re.sub(r"\s+", " ", caption_text.replace("\xa0", " ")).strip()
    if not label_text and caption_text:
        label_text = _extract_normalized_table_label(caption_text)
    if not label_text:
        label_text = _infer_label_from_dom_id(wrapper)
    return caption_text, label_text


def _is_dom_noise_table(table_el, check_nested: bool = True) -> bool:
    """Skip publisher boilerplate, disclaimer, supporting-info, form, citation, and duplicate modal tables."""
    if table_el.find_parent("section", class_="html-fn_group") or table_el.find_parent(class_="html-fn_group"):
        return True
    for noise_cls in ("messageBoard", "messageBoardWrap", "copyCitationInfo", "citationCn", "citationEn", "quot"):
        if table_el.find_parent(class_=noise_cls):
            return True
    if check_nested:
        # 嵌套排版外框表（含内层 <table>）：只解析内层实际数据表
        if table_el.find("table") is not None:
            return True
        if len(table_el.find_all(["input", "textarea", "select"])) >= 2:
            return True
    else:
        if table_el.find("table") is None and len(table_el.find_all(["input", "textarea", "select"])) >= 2:
            return True
    cls = table_el.get("class") or []
    if "support-info__table" in cls or table_el.find_parent(class_="support-info__table-wrapper") or table_el.find_parent("section", class_="article-section__supporting"):
        return True
    modal = table_el.find_parent(class_="table-modal") or table_el.find_parent(class_="reveal-modal")
    if modal:
        wrap = modal.find_parent(class_="table-wrap") or modal.find_parent(class_="figure-table-wrapper")
        if wrap and wrap.select_one(".table-overflow table"):
            return True
    if "body" in cls:
        trs = table_el.find_all("tr")
        first_cols = len(trs[0].find_all(["th", "td"])) if trs else 0
        if len(trs) <= 2 and first_cols <= 1:
            return True
    # 过滤单行两列公式排版表（如 China Geology / MathJax: [LaTeX/MathML 公式, "(1)"]）
    trs_eq = table_el.find_all("tr")
    if len(trs_eq) == 1:
        eq_cells = trs_eq[0].find_all(["td", "th"])
        if len(eq_cells) == 2:
            c0 = (eq_cells[0].get_text(" ", strip=True) or "")
            c1 = (eq_cells[1].get_text(" ", strip=True) or "").strip()
            if re.match(r"^[\(\[（]\s*\d+[a-zA-Z]?\s*[\)\]）]$", c1) and (
                table_el.find("math") is not None
                or any(tok in c0 for tok in ("\\math", "\\frac", "\\Delta", "\\delta", "\\approx", "\\times", "="))
            ):
                return True
    return False


def _parse_dom_wrappers_bs4(html_content, read_html_fn):
    """Use BeautifulSoup to extract tables with publisher-aware captions, footnotes, and noise filtering."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    parser = "lxml"
    try:
        soup = BeautifulSoup(html_content, parser)
    except Exception:
        soup = BeautifulSoup(html_content, "html.parser")

    wrappers = []
    seen_wrappers = set()
    for sel in _DOM_WRAPPER_SELECTORS:
        for el in soup.select(sel):
            if id(el) not in seen_wrappers:
                seen_wrappers.add(id(el))
                wrappers.append(el)
    if not wrappers:
        return []

    seen_tables = set()
    seen_signatures = set()
    valid_dfs = []
    for wrapper in wrappers:
        table_els = [wrapper] if wrapper.name == "table" else wrapper.find_all("table")
        if not table_els:
            continue
        caption_text, label_text = _extract_caption_and_label_from_wrapper(wrapper)
        footnote_text = ""
        for fsel in _DOM_FOOTNOTE_SELECTORS:
            fel = wrapper.select_one(fsel)
            if fel:
                footnote_text = clean_text(fel.get_text(" ", strip=True))
                if footnote_text:
                    break
        for tbl in table_els:
            if id(tbl) in seen_tables or _is_dom_noise_table(tbl):
                continue
            seen_tables.add(id(tbl))
            tbl_copy = BeautifulSoup(str(tbl), "html.parser")
            for tf in tbl_copy.find_all("tfoot"):
                if not footnote_text:
                    footnote_text = clean_text(tf.get_text(" ", strip=True))
                tf.decompose()
            try:
                sub_dfs = read_html_fn(str(tbl_copy))
            except (ValueError, IndexError):
                continue
            tbl_label_text = label_text or _infer_label_from_dom_id(tbl)
            for df in sub_dfs:
                if df.empty or len(df.columns) < 1 or len(df) < 1:
                    continue
                df.columns = [clean_text(c) for c in df.columns]
                for col_i in range(len(df.columns)):
                    df.isetitem(col_i, df.iloc[:, col_i].astype(str).apply(clean_text))
                if tbl_label_text:
                    df.attrs['label'] = tbl_label_text
                if caption_text:
                    df.attrs['table_title'] = caption_text
                if footnote_text:
                    df.attrs['footnotes'] = [footnote_text]
                if is_metadata_table(df):
                    continue
                first_row_sig = tuple(str(v) for v in df.iloc[0].tolist()) if len(df) else ()
                sig = (tbl_label_text.lower() if tbl_label_text else "", df.shape, tuple(df.columns), first_row_sig)
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)
                valid_dfs.append(df)
    return valid_dfs


def parse_tables_from_html(html_content, table_idx="all"):
    """
    Parses tables from raw HTML using publisher-aware BeautifulSoup wrappers when present,
    falling back to pandas.read_html and line-based caption matching.
    Applies is_metadata_table() filtering per table.
    """
    try:
        # Wrap literal HTML in StringIO to avoid deprecation warnings
        try:
            from ..table_postprocess import read_html_preserving_text
        except ImportError:
            from table_postprocess import read_html_preserving_text

        valid_dfs = _parse_dom_wrappers_bs4(html_content, read_html_preserving_text)

        if not valid_dfs:
            filtered_html = html_content
            bs4_table_meta = []
            try:
                from bs4 import BeautifulSoup
                _soup = BeautifulSoup(html_content, "html.parser")
                for _tbl in list(_soup.find_all("table")):
                    if getattr(_tbl, "parent", None) is None:
                        continue
                    if _is_dom_noise_table(_tbl, check_nested=False):
                        _tbl.decompose()
                    elif _tbl.find("table") is not None:
                        _tbl.unwrap()
                for _tbl in _soup.find_all("table"):
                    _cap = _tbl.find("caption")
                    _cap_txt = clean_text(_cap.get_text(" ", strip=True)) if _cap else ""
                    _lbl_txt = _extract_normalized_table_label(_cap_txt) if _cap_txt else ""
                    if not _lbl_txt:
                        _lbl_txt = _infer_label_from_dom_id(_tbl)
                    if not _lbl_txt and getattr(_tbl, "parent", None) is not None:
                        _lbl_txt = _infer_label_from_dom_id(_tbl.parent)
                    bs4_table_meta.append((_lbl_txt, _cap_txt))
                filtered_html = str(_soup)
            except Exception:
                filtered_html = html_content

            dfs = read_html_preserving_text(filtered_html)
            if not dfs:
                return []

            # Split filtered_html by <table to find preceding text blocks
            parts = re.split(r'<table\b', filtered_html, flags=re.IGNORECASE)

            valid_dfs = []
            for idx, df in enumerate(dfs):
                if df.empty or len(df.columns) < 1 or len(df) < 1:
                    continue
                # Basic cleanup of columns (index by iloc so duplicate column names never fail)
                df.columns = [clean_text(c) for c in df.columns]
                for col_i in range(len(df.columns)):
                    df.isetitem(col_i, df.iloc[:, col_i].astype(str).apply(clean_text))

                # Extract caption from BS4 <caption> / DOM id first, then preceding text block
                caption_text = ""
                label_text = ""
                if idx < len(bs4_table_meta):
                    label_text, caption_text = bs4_table_meta[idx]
                if not label_text and idx < len(parts):
                    preceding_text = parts[idx]
                    lines = preceding_text.split('\n')
                    for line in reversed(lines[-15:]):
                        line_clean = clean_text(line).strip()
                        # Strip HTML tags
                        line_no_html = re.sub(r'<[^>]+>', '', line_clean).strip()
                        # Strip leading markdown/formatting chars
                        line_stripped = re.sub(r'^[#*_\-\s\(\)\:\.\,\»]+', '', line_no_html).strip()
                        lbl_cand = _extract_normalized_table_label(line_stripped)
                        if lbl_cand:
                            caption_text = caption_text or line_stripped
                            label_text = lbl_cand
                            break

                if label_text:
                    df.attrs['label'] = label_text
                if caption_text:
                    df.attrs['table_title'] = caption_text
                if common.is_unlabeled_single_col_layout_shell(df, has_explicit_label=bool(label_text or caption_text)):
                    continue
                if is_metadata_table(df):
                    continue

                valid_dfs.append(df)

        if not valid_dfs:
            return []

        return _select_tables(valid_dfs, table_idx) or []
    except Exception as e:
        print(f"Warning: Failed to parse HTML tables: {e}")
        return []


def parse_tables_from_markdown(md_content, table_idx="all"):
    """
    Parses Markdown tables from markdown content and extracts captions directly preceding them.
    """
    tables_found = []
    lines = md_content.split('\n')
    
    current_table = []
    preceding_lines = []
    
    def _is_markdown_sep_row(row_str, row_pos):
        stripped = row_str.strip()
        if not (stripped.startswith('|') and stripped.endswith('|')):
            return False
        cells = [p.strip() for p in stripped[1:-1].split('|')]
        if not cells:
            return False
        if row_pos == 1:
            return all(bool(re.match(r'^:?-{1,}:?$', c)) for c in cells)
        return all(bool(re.match(r'^:?-{2,}:?$', c)) for c in cells)

    def process_table_and_add(t_lines, prec_lines):
        try:
            parsed_rows = []
            for r_idx, r in enumerate(t_lines):
                if _is_markdown_sep_row(r, r_idx):
                    continue
                stripped = r.strip()
                parts = [clean_text(p) for p in stripped[1:-1].split('|')]
                parsed_rows.append(parts)

            if len(parsed_rows) < 2:
                return

            max_cols = max(len(r) for r in parsed_rows)
            if max_cols < 1:
                return
            norm_rows = [r + [''] * (max_cols - len(r)) for r in parsed_rows]
            header_row = [clean_text(c) for c in norm_rows[0]]
            data_rows = norm_rows[1:]
            df = pd.DataFrame(data_rows, columns=header_row, dtype=str)
            if not df.empty and len(df.columns) >= 1:
                for col_i in range(len(df.columns)):
                    df.iloc[:, col_i] = df.iloc[:, col_i].astype(str).apply(clean_text)
                    
                caption_text = ""
                label_text = ""
                for prec_line in reversed(prec_lines[-15:]):
                    prec_clean = prec_line.strip()
                    # Strip leading markdown formatting characters
                    prec_clean_stripped = re.sub(r'^[#*_\-\s\(\)\:\.\,\»]+', '', prec_clean).strip()
                    lbl_cand = _extract_normalized_table_label(prec_clean_stripped)
                    if lbl_cand:
                        caption_text = prec_clean_stripped
                        label_text = lbl_cand
                        break

                non_empty_cols = sum(
                    1
                    for col_i in range(len(df.columns))
                    if str(df.columns[col_i]).strip() or df.iloc[:, col_i].astype(str).str.strip().ne("").any()
                )
                if non_empty_cols < 2 and not label_text and not caption_text:
                    return
                if label_text:
                    df.attrs['label'] = label_text
                if caption_text:
                    df.attrs['table_title'] = caption_text
                if is_metadata_table(df):
                    return
                tables_found.append(df)
        except Exception:
            pass

    for line in lines:
        line_strip = line.strip()
        if line_strip.startswith('|') and line_strip.endswith('|'):
            current_table.append(line_strip)
        else:
            if current_table:
                if len(current_table) >= 2:
                    process_table_and_add(current_table, preceding_lines)
                current_table = []
            preceding_lines.append(line)
            
    if current_table and len(current_table) >= 2:
        process_table_and_add(current_table, preceding_lines)
        
    if not tables_found:
        return []
        
    if table_idx == "all":
        return tables_found
    elif table_idx == "first":
        return [tables_found[0]]
    else:
        try:
            idx = int(table_idx)
            return [tables_found[idx]] if idx < len(tables_found) else tables_found
        except ValueError:
            return tables_found


def extract_tables_via_paper_fetch(doi_or_url, table_idx="all", cancel_event=None):
    """
    Invokes paper-fetch-skill API.
    """
    if cancel_event is not None and cancel_event.is_set():
        return []
    try:
        # Load Elsevier & polite-pool credentials dynamically and inject into environment for paper-fetch-skill
        config = load_config()
        for _env_k in ("ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN", "UNPAYWALL_EMAIL", "CROSSREF_EMAIL", "OPENALEX_API_KEY"):
            _v = config.get(_env_k)
            if _v:
                os.environ[_env_k] = str(_v)
        if config.get("IS_CAMPUS_NETWORK"):
            os.environ["IS_CAMPUS_NETWORK"] = "true"
            
        import sys
        # Add paper-fetch-skill src to path
        pf_src = os.path.abspath(os.path.join(common.get_skill_root(), '..', 'paper-fetch-skill', 'src'))
        if pf_src not in sys.path:
            sys.path.append(pf_src)
            
        from paper_fetch.service import fetch_paper
        from paper_fetch.models import RenderOptions
        
        print(f"Querying paper-fetch-skill API for {doi_or_url}...")
        envelope = fetch_paper(
            query=doi_or_url,
            modes={"article", "markdown"},
            render=RenderOptions(include_refs="none")
        )
        if envelope:
            # Check if there is fulltext markdown
            if envelope.markdown:
                print("paper-fetch-skill: Fulltext markdown retrieved.")
                dfs = parse_tables_from_markdown(envelope.markdown, table_idx)
                if dfs:
                    return dfs
            
            # Check sections
            if envelope.article and envelope.article.sections:
                print("paper-fetch-skill: Sections retrieved.")
                all_text = "\n".join(sec.text for sec in envelope.article.sections)
                dfs = parse_tables_from_markdown(all_text, table_idx)
                if dfs:
                    return dfs
    except Exception as e:
        print(f"paper-fetch-skill API lookup skipped: {e}")
    return []


def _elsevier_api_get(url, headers, params=None, timeout=20, proxies=None, cancel_event=None, max_retries=2):
    """Elsevier API GET helper with 429 Retry-After/exponential backoff and optional proxy passthrough."""
    req_kwargs = {"headers": headers, "timeout": timeout}
    if params is not None:
        req_kwargs["params"] = params
    if proxies:
        req_kwargs["proxies"] = proxies

    resp = None
    for attempt in range(max_retries + 1):
        if cancel_event is not None and cancel_event.is_set():
            return None
        resp = requests.get(url, **req_kwargs)
        if resp.status_code != 429 or attempt >= max_retries:
            return resp
        hdrs = getattr(resp, "headers", None) or {}
        retry_after = hdrs.get("Retry-After") or hdrs.get("retry-after")
        try:
            wait_s = min(max(0.5, float(retry_after)), 30.0) if retry_after else min(2.0 * (2 ** attempt), 30.0)
        except (TypeError, ValueError):
            wait_s = min(2.0 * (2 ** attempt), 30.0)
        print(f"[Elsevier API] 触发 429 限流，{wait_s:.1f}s 后重试 ({attempt + 1}/{max_retries})...")
        if cancel_event is not None:
            if cancel_event.wait(wait_s):
                return None
        else:
            time.sleep(wait_s)
    return resp


def _extract_elsevier_status_text(resp):
    """Extract <statusCode>/<statusText> (XML/JSON) or X-ELS-Status header from an Elsevier API response."""
    if resp is None:
        return "", ""
    hdrs = getattr(resp, "headers", None) or {}
    els_hdr = str(hdrs.get("X-ELS-Status") or hdrs.get("x-els-status") or "").strip()
    body = str(getattr(resp, "text", "") or "")
    status_code = ""
    status_text = ""

    m_code = re.search(r"<statusCode[^>]*>(.*?)</statusCode>", body, re.IGNORECASE | re.DOTALL)
    m_text = re.search(r"<statusText[^>]*>(.*?)</statusText>", body, re.IGNORECASE | re.DOTALL)
    if m_code:
        status_code = re.sub(r"\s+", " ", m_code.group(1)).strip()
    if m_text:
        status_text = re.sub(r"\s+", " ", m_text.group(1)).strip()

    if not status_code and not status_text:
        try:
            jdata = resp.json()
            if isinstance(jdata, dict):
                st = (jdata.get("service-error") or {}).get("status") or {}
                status_code = str(st.get("statusCode") or "").strip()
                status_text = str(st.get("statusText") or "").strip()
        except Exception:
            pass

    if not status_text and els_hdr:
        status_text = els_hdr
    return status_code, status_text


def _diagnose_elsevier_403(resp, doi, headers, proxies=None, cancel_event=None):
    """On Elsevier HTTP 403, extract or probe view=META <statusText> for actionable entitlement/auth diagnostics."""
    code, text = _extract_elsevier_status_text(resp)
    if not text and doi and (cancel_event is None or not cancel_event.is_set()):
        try:
            meta_url = f"https://api.elsevier.com/content/article/doi/{doi}"
            meta_resp = _elsevier_api_get(
                meta_url,
                headers=headers,
                params={"view": "META"},
                timeout=10,
                proxies=proxies,
                cancel_event=cancel_event,
                max_retries=0,
            )
            m_code, m_text = _extract_elsevier_status_text(meta_resp)
            code = code or m_code
            text = text or m_text
        except Exception:
            pass

    diag = " / ".join(p for p in (code, text) if p)
    hint = ""
    low_diag = diag.lower()
    if any(k in low_diag for k in ("entitlement", "authorization", "insttoken", "subscriber", "not entitled")):
        hint = "（提示：需配置机构 X-ELS-InstToken 或在校园网 IP 下访问全文权限）"
    elif "authentication" in low_diag or "api key" in low_diag:
        hint = "（提示：请检查 ELSEVIER_API_KEY 是否有效）"
    if diag:
        print(f"[Elsevier API] HTTP 403 诊断: {diag}{hint}")
    else:
        print("[Elsevier API] HTTP 403 诊断: 无详细 statusText（可能缺少机构订阅权限或 X-ELS-InstToken）")
    return diag


def _detect_elsevier_soft_degradation(resp) -> Dict[str, Any]:
    """
    检测 Elsevier HTTP 200 响应中的配额软降级 / META 视图截断特征：
    - `quota_exhausted`: X-RateLimit-Remaining == 0 或 X-ELS-Status 明确指示配额/限流耗尽；
    - `meta_only`: 响应包含 <coredata> 但完全缺少 <originalText>/<ce:table>/<xocs:rawtext>/<body>。
    """
    info = {
        "quota_exhausted": False,
        "meta_only": False,
        "els_status": "",
        "rate_remaining": "",
    }
    if resp is None:
        return info
    hdrs = getattr(resp, "headers", None) or {}
    lower_hdrs = {str(k).lower(): v for k, v in hdrs.items()} if hasattr(hdrs, "items") else {}
    raw_els_status = lower_hdrs.get("x-els-status")
    raw_rate_rem = lower_hdrs.get("x-ratelimit-remaining")
    els_status = "" if raw_els_status is None else str(raw_els_status).strip()
    rate_rem = "" if raw_rate_rem is None else str(raw_rate_rem).strip()
    info["els_status"] = els_status
    info["rate_remaining"] = rate_rem

    low_status = els_status.lower()
    if rate_rem == "0" or any(k in low_status for k in ("quota", "rate limit", "ratelimit", "exceeded", "too many")):
        info["quota_exhausted"] = True

    body = str(getattr(resp, "text", "") or "")
    if body:
        low_body = body.lower()
        has_coredata = ("<coredata" in low_body) or ("<full-text-retrieval-response" in low_body)
        has_fulltext = any(
            marker in low_body
            for marker in ("<originaltext", "<ce:table", "<xocs:rawtext", "<xocs:doc", "<body")
        )
        if has_coredata and not has_fulltext:
            info["meta_only"] = True
    return info


def extract_tables_via_elsevier_api(doi, table_idx="all", cancel_event=None):
    """
    Extracts tables from Elsevier articles using the Elsevier Full-Text Retrieval API.
    Always uses the DOI endpoint (/content/article/doi/{DOI}) which has proper auth.
    Returns XOCS XML containing <ce:table> elements — no browser needed.
    Requires ELSEVIER_API_KEY in config.json, companion config, or environment.
    Automatically attaches X-ELS-InstToken when ELSEVIER_INSTTOKEN is configured.
    Supports 429 Retry-After backoff, NETWORK_PROXY passthrough, HTTP 200 META soft-degradation
    diagnostics, and 403 view=META diagnostics.
    """
    if cancel_event is not None and cancel_event.is_set():
        return []
    try:
        config = load_config()
        headers = build_elsevier_headers(config, accept="text/xml")
        if not headers:
            return []
        els_key = headers["X-ELS-APIKey"]
        insttoken = headers.get("X-ELS-InstToken", "")
        proxies = get_requests_proxies(config)

        try:
            from .negative_cache import is_negatively_cached, record_negative_cache
        except Exception:
            is_negatively_cached = None
            record_negative_cache = None

        api_url = f"https://api.elsevier.com/content/article/doi/{doi}"
        if is_negatively_cached:
            hit = is_negatively_cached("elsevier_api", url=api_url, doi=doi, config=config)
            if hit:
                neg_reason = hit.get("reason") if isinstance(hit, dict) else str(hit)
                print(f"[Elsevier API] 命中短期配额熔断缓存 ({neg_reason})，跳过无效请求: {doi}")
                return []

        print(f"[Elsevier API] \u6b63\u5728\u83b7\u53d6 XOCS XML: {doi}")
        resp = _elsevier_api_get(
            api_url,
            headers=headers,
            timeout=20,
            proxies=proxies,
            cancel_event=cancel_event,
        )
        if resp is None:
            return []
        if resp.status_code != 200:
            print(f"[Elsevier API] \u8bf7\u6c42\u5931\u8d25: HTTP {resp.status_code}")
            if resp.status_code == 403:
                _diagnose_elsevier_403(resp, doi, headers, proxies=proxies, cancel_event=cancel_event)
            elif resp.status_code == 429 and record_negative_cache:
                record_negative_cache("elsevier_api", url=api_url, doi=doi, status_code=429, config=config)
            return []

        soft_deg = _detect_elsevier_soft_degradation(resp)
        if soft_deg["quota_exhausted"]:
            print(
                f"[Elsevier API Warning] 检测到 API 配额耗尽/限流软降级 "
                f"(X-RateLimit-Remaining={soft_deg['rate_remaining'] or 'unknown'}, "
                f"X-ELS-Status={soft_deg['els_status'] or 'none'})，跳过冗余 Object API 请求并启用短期熔断。"
            )
            if record_negative_cache:
                record_negative_cache("elsevier_api", url=api_url, doi=doi, status_code=429, config=config)
            return []
        if soft_deg["meta_only"]:
            print(
                f"[Elsevier API Warning] HTTP 200 响应仅包含元数据 (<coredata>) 且缺少正文 (<originalText>)，"
                f"疑似 API Key 触发配额软降级或缺少该文献全文机构权限 "
                f"(X-ELS-Status={soft_deg['els_status'] or 'none'}, X-RateLimit-Remaining={soft_deg['rate_remaining'] or 'unknown'})。"
            )

        tables = _parse_elsevier_xocs_tables(resp.text, table_idx)
        tables.extend(
            extract_elsevier_supplementary_tables(
                doi, els_key, insttoken=insttoken, proxies=proxies, cancel_event=cancel_event
            )
        )
        return tables
    except Exception as e:
        print(f"[Elsevier API] \u63d0\u53d6\u5931\u8d25: {e}")
        return []


def extract_elsevier_supplementary_tables(doi, api_key, insttoken="", proxies=None, cancel_event=None):
    """
    Finds and extracts tables from all non-image supplementary attachments
    associated with an Elsevier article using the Elsevier Object API.
    """
    import io
    import zipfile
    import xml.etree.ElementTree as ET
    import pandas as pd
    try:
        import fitz
    except ImportError:
        fitz = None
        
    supplementary_dfs = []
    if not api_key or (cancel_event is not None and cancel_event.is_set()):
        return supplementary_dfs
    cfg = None
    if not insttoken or proxies is None:
        try:
            cfg = load_config() or {}
        except Exception:
            cfg = {}
    if not insttoken:
        insttoken = str((cfg or {}).get("ELSEVIER_INSTTOKEN") or os.environ.get("ELSEVIER_INSTTOKEN") or "").strip()
    if proxies is None:
        proxies = get_requests_proxies(cfg)

    try:
        # Step 1: Fetch attachment metadata using DOI
        api_url = f"https://api.elsevier.com/content/object/doi/{doi}"
        headers = {
            "X-ELS-APIKey": api_key,
            "Accept": "application/json"
        }
        if insttoken:
            headers["X-ELS-InstToken"] = insttoken
        params = {
            "view": "META"
        }
        print(f"[Elsevier API] Fetching supplementary metadata for DOI: {doi}")
        resp = _elsevier_api_get(
            api_url,
            headers=headers,
            params=params,
            timeout=20,
            proxies=proxies,
            cancel_event=cancel_event,
        )
        if resp is None or resp.status_code != 200:
            if resp is not None:
                print(f"[Elsevier API] Supplementary metadata request failed: HTTP {resp.status_code}")
            return supplementary_dfs
            
        data = resp.json()
        attachments = data.get("attachment-metadata-response", {}).get("attachment", [])
        if not attachments:
            print(f"[Elsevier API] No attachments found for DOI: {doi}")
            return supplementary_dfs

        print(f"[Elsevier API] Found {len(attachments)} total attachments. Filtering for non-image files...")
        
        for att in attachments:
            if cancel_event is not None and cancel_event.is_set():
                break
            mimetype = (att.get("mimetype") or "").lower()
            filename = (att.get("filename") or "").lower()
            ref = att.get("ref") or ""
            eid = att.get("eid")
            
            # Filter non-image attachments and non-supplementary manuscript files (e.g. am.pdf)
            if not mimetype or mimetype.startswith("image/"):
                continue
            if not eid:
                continue
            ref_clean = str(ref).lower().strip()
            fname_clean = str(filename).lower().strip()
            if ref_clean in ("am", "main", "document") or fname_clean in ("am.pdf", "main.pdf") or fname_clean.startswith("am."):
                print(f"[Elsevier API] Skipping non-supplementary article manuscript file: {filename} (Ref={ref})")
                continue

            print(f"[Elsevier API] Attempting to download supplementary: {filename} (Ref={ref}, MIME={mimetype})")
            
            # Step 2: Download the attachment bytes
            dl_url = f"https://api.elsevier.com/content/object/eid/{eid}"
            dl_headers = {"X-ELS-APIKey": api_key}
            if insttoken:
                dl_headers["X-ELS-InstToken"] = insttoken
            dl_resp = _elsevier_api_get(
                dl_url,
                headers=dl_headers,
                timeout=30,
                proxies=proxies,
                cancel_event=cancel_event,
            )
            if dl_resp is None or dl_resp.status_code != 200:
                if dl_resp is not None:
                    print(f"[Elsevier API] Failed to download {filename}: HTTP {dl_resp.status_code}")
                continue
                
            content_bytes = dl_resp.content
            filename_no_ext = os.path.splitext(filename)[0]
            
            # Step 3: Parse tables based on file type
            # Case A: Excel (.xlsx, .xls)
            if mimetype in ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel") or filename.endswith((".xlsx", ".xls")):
                try:
                    excel_data = pd.read_excel(io.BytesIO(content_bytes), sheet_name=None)
                    for s_name, df in excel_data.items():
                        if not df.empty and df.shape[0] >= 1 and df.shape[1] >= 2:
                            tbl_label = _format_supp_excel_label(filename_no_ext, s_name, len(excel_data) > 1)
                            df.attrs['label'] = tbl_label
                            df.attrs['table_title'] = f"Supplementary Table ({tbl_label}) from sheet '{s_name}' of file '{filename}'"
                            supplementary_dfs.append(df)
                except Exception as ex:
                    print(f"  -> Error parsing Excel {filename}: {ex}")
                    
            # Case B: Word (.docx)
            elif mimetype in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword") or filename.endswith(".docx"):
                try:
                    doc_stream = io.BytesIO(content_bytes)
                    with zipfile.ZipFile(doc_stream) as zf:
                        xml_content = zf.read('word/document.xml')
                        tree = ET.fromstring(xml_content)
                        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                        tables = tree.findall('.//w:tbl', ns)
                        for t_idx, table in enumerate(tables):
                            rows_data = []
                            for row in table.findall('.//w:tr', ns):
                                row_data = []
                                for cell in row.findall('.//w:tc', ns):
                                    cell_text = "".join(text_elem.text for text_elem in cell.findall('.//w:t', ns) if text_elem.text)
                                    row_data.append(cell_text.strip())
                                if row_data:
                                    rows_data.append(row_data)
                            if rows_data:
                                df = _rows_to_dataframe(rows_data)
                                tbl_label = f"Supplementary Table {filename_no_ext}" if len(tables) == 1 else f"Supplementary Table {filename_no_ext}_T{t_idx+1}"
                                df.attrs['label'] = tbl_label
                                df.attrs['table_title'] = f"Supplementary Table {t_idx+1} from file '{filename}'"
                                supplementary_dfs.append(df)
                except Exception as ex:
                    print(f"  -> Error parsing Word {filename}: {ex}")
                    
            # Case C: PDF (.pdf)
            elif mimetype == "application/pdf" or filename.endswith(".pdf"):
                if fitz is None:
                    print("  -> fitz (PyMuPDF) is not installed. Skipping PDF attachment.")
                    continue
                try:
                    pdf_doc = fitz.open(stream=content_bytes, filetype="pdf")
                    print(f"  -> Successfully opened PDF {filename} ({len(pdf_doc)} pages).")
                    for p_idx in range(len(pdf_doc)):
                        page = pdf_doc[p_idx]
                        try:
                            finder = page.find_tables()
                            tbl_list = list(finder.tables) if finder and finder.tables else []
                        except Exception:
                            tbl_list = []
                        if not tbl_list:
                            try:
                                finder = page.find_tables(strategy="text")
                                tbl_list = list(finder.tables) if finder and finder.tables else []
                            except Exception:
                                tbl_list = []
                        for t_idx, table in enumerate(tbl_list):
                            rows = table.extract()
                            if rows and len(rows) >= 2 and len(rows[0]) >= 1:
                                df = _rows_to_dataframe(rows)
                                tbl_label = f"Supplementary Table {filename_no_ext}_P{p_idx+1}" if len(tbl_list) == 1 else f"Supplementary Table {filename_no_ext}_P{p_idx+1}_T{t_idx+1}"
                                df.attrs['label'] = tbl_label
                                df.attrs['table_title'] = f"Supplementary Table from page {p_idx+1} of file '{filename}'"
                                supplementary_dfs.append(df)
                except Exception as ex:
                    print(f"  -> Error parsing PDF {filename}: {ex}")
                    
    except Exception as e:
        print(f"[Elsevier API] Error processing supplementary: {e}")
        
    return supplementary_dfs


def _clean_supp_filename_stem(filename_no_ext: str) -> str:
    """Strip Springer/Nature numeric journal-year-id prefixes (e.g. '126_2019_937_MOESM1_ESM' -> 'MOESM1_ESM')
    so TABLE_LABEL_RE does not truncate multi-sheet supplementary filenames to '126-2019-937'."""
    stem = str(filename_no_ext or "supplementary").strip()
    m_sp = re.match(r'^\d+[_-]\d{4}[_-]\d+[_-]((?:MOESM|ESM)\d+.*)$', stem, re.IGNORECASE)
    if m_sp:
        return m_sp.group(1)
    return stem


def _ensure_supp_label_prefix(stem: str) -> str:
    """Prefix a supplementary label with 'Supplementary Table ' only if it does not already carry one."""
    s = str(stem or "supplementary").strip()
    if re.match(
        r'^(?:supplementary\s+table|supplemental\s+table|extended\s+data\s+table|appendix\s+table|table\s+s\d|附表|附录表|补充表)',
        s,
        re.IGNORECASE,
    ):
        return s
    return f"Supplementary Table {s}"


def _format_supp_excel_label(filename_no_ext: str, sheet_name: str, multi_sheet: bool) -> str:
    """Format a supplementary Excel sheet label while preserving supplementary semantics."""
    s_clean = str(sheet_name or "").strip()
    m_st = re.match(
        r'^(?:supplementary\s+table|supplemental\s+table|table|tab\.?|附表|补充表|表)?\s*(s?\d+(?:[-.]\d+)?[a-z]?)$',
        s_clean,
        re.IGNORECASE,
    )
    if m_st and not re.match(r'^sheet\s*\d+$', s_clean, re.IGNORECASE):
        num = m_st.group(1)
        if num.lower().startswith("s"):
            return f"Supplementary Table S{num[1:]}"
        return f"Supplementary Table {num}"
    stem = _clean_supp_filename_stem(filename_no_ext)
    if multi_sheet:
        return _ensure_supp_label_prefix(f"{stem}_{s_clean}")
    return _ensure_supp_label_prefix(stem)


_SUPP_STRONG_URL_RE = re.compile(
    r"(?:/mediaobjects/|/action/downloadsupplement|/suppl(?:ement(?:ary)?)?/|/suppdata/|/suppinfo/"
    r"|/esm\d*|moesm\d*|mmc\d+|table[_-]?s\d+|supplementary[_-]?table|additional[_-]?file"
    r"|extdata|supporting[_-]?info|source[_-]?data|appendix[_-]?table"
    r"|mdpi\.com/.+/s\d+(?:[/?#]|$))",
    re.IGNORECASE,
)
_SUPP_WEAK_TEXT_RE = re.compile(
    r"(?:supplementary|supplemental|supporting\s+info(?:rmation)?|additional\s+file"
    r"|extended\s+data|electronic\s+supplementary|source\s+data|appendix\s+table"
    r"|table\s*s\d+|dataset|esm\s*\d+|附表|补充材料|补充表|附件)",
    re.IGNORECASE,
)
_SUPP_ALLOWED_EXTS = (".xlsx", ".xls", ".csv", ".tsv", ".docx", ".zip", ".pdf")
_SUPP_TABULAR_EXTS = (".xlsx", ".xls", ".csv", ".tsv", ".docx", ".zip")
_SUPP_IGNORED_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".tif", ".tiff", ".bmp", ".webp",
    ".mp4", ".mov", ".avi", ".webm", ".mp3", ".wav",
    ".bib", ".ris", ".enw", ".css", ".js", ".ico", ".woff", ".woff2",
)
_SUPP_MAIN_PDF_RE = re.compile(
    r"(?:/doi/e?pdf/|/content/pdf/|/pdfdirect/|/(?:am|main|article|fulltext)\.pdf(?:\?|$)|download=fulltext)",
    re.IGNORECASE,
)
_SUPP_REJECT_CTYPES = (
    "text/html",
    "application/xhtml",
    "image/",
    "video/",
    "audio/",
    "text/css",
    "javascript",
)


def _parse_supplementary_bytes(content_bytes, filename, content_type="", _depth=0, deadline=None, cancel_event=None):
    """Parse supplementary attachment bytes (.xlsx/.xls/.csv/.tsv/.docx/.pdf/.zip) into DataFrames."""
    import zipfile
    import xml.etree.ElementTree as ET

    try:
        import fitz
    except ImportError:
        fitz = None

    if not content_bytes:
        return []
    fname = (filename or "supplementary").strip()
    low_name = fname.lower()
    ctype = (content_type or "").lower()
    filename_no_ext = _clean_supp_filename_stem(os.path.splitext(os.path.basename(fname))[0] or "supplementary")
    dfs = []

    # Magic byte / content-type format detection
    is_zip_magic = content_bytes[:4] == b"PK\x03\x04"
    is_pdf_magic = content_bytes[:5] == b"%PDF-"
    is_ole_magic = content_bytes[:4] == b"\xd0\xcf\x11\xe0"

    # 1. Excel (.xlsx / .xls)
    if (
        low_name.endswith((".xlsx", ".xls"))
        or "spreadsheetml" in ctype
        or "ms-excel" in ctype
        or (is_ole_magic and not low_name.endswith(".doc"))
    ):
        try:
            excel_data = pd.read_excel(io.BytesIO(content_bytes), sheet_name=None)
            _cover_sheet_re = re.compile(
                r"^(?:title|cover|readme|read\s*me|info|notes?|metadata|contents?|instructions?|about|summary\s*info|说明|封面|目录|备注)$",
                re.IGNORECASE,
            )
            for sheet_name, df in excel_data.items():
                if not df.empty and df.shape[0] >= 1 and df.shape[1] >= 1:
                    # 多工作表补充 Excel 中跳过单列纯文字封面/说明页（如 Springer MOESM*-ESM-Title）
                    if len(excel_data) > 1 and _cover_sheet_re.match(str(sheet_name or "").strip()):
                        non_empty_cols = sum(
                            1 for col_i in range(df.shape[1])
                            if any(
                                v is not None and str(v).strip() not in ("", "nan", "none")
                                for v in df.iloc[:, col_i].tolist()
                            )
                        )
                        if non_empty_cols <= 1:
                            continue
                    tbl_label = _format_supp_excel_label(filename_no_ext, sheet_name, len(excel_data) > 1)
                    df.attrs["label"] = tbl_label
                    df.attrs["table_title"] = f"Supplementary Table ({tbl_label}) from sheet '{sheet_name}' in file '{fname}'"
                    df.attrs["extractor"] = "builtin_supplementary"
                    dfs.append(df)
            return dfs
        except Exception as ex:
            print(f"  -> [Built-in Supp] 解析 Excel 失败 {fname}: {ex}")
            return []

    # 2. CSV / TSV
    if low_name.endswith((".csv", ".tsv")) or "text/csv" in ctype or "tab-separated-values" in ctype:
        try:
            sep = "\t" if (low_name.endswith(".tsv") or "tab-separated" in ctype) else ","
            text_val = content_bytes.decode("utf-8-sig", errors="replace")
            if text_val.lstrip().lower().startswith(("<!doctype", "<html")):
                return []
            df = pd.read_csv(io.StringIO(text_val), sep=sep, dtype=str, keep_default_na=False)
            if not df.empty and df.shape[0] >= 1 and df.shape[1] >= 2:
                df.attrs["label"] = _ensure_supp_label_prefix(filename_no_ext)
                df.attrs["table_title"] = f"Supplementary Table from file '{fname}'"
                df.attrs["extractor"] = "builtin_supplementary"
                dfs.append(df)
            return dfs
        except Exception as ex:
            print(f"  -> [Built-in Supp] 解析 CSV/TSV 失败 {fname}: {ex}")
            return []

    # 3. Word (.docx)
    if low_name.endswith(".docx") or "wordprocessingml" in ctype:
        try:
            with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
                xml_content = zf.read("word/document.xml")
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            root = ET.fromstring(xml_content)
            tables = root.findall(".//w:tbl", ns)
            for t_idx, tbl in enumerate(tables):
                rows_data = []
                for row in tbl.findall(".//w:tr", ns):
                    row_data = []
                    for cell in row.findall(".//w:tc", ns):
                        cell_text = "".join(t.text for t in cell.findall(".//w:t", ns) if t.text)
                        row_data.append(cell_text.strip())
                    if row_data:
                        rows_data.append(row_data)
                if rows_data:
                    df = _rows_to_dataframe(rows_data)
                    if not df.empty:
                        tbl_label = (
                            _ensure_supp_label_prefix(filename_no_ext)
                            if len(tables) == 1
                            else _ensure_supp_label_prefix(f"{filename_no_ext}_T{t_idx + 1}")
                        )
                        df.attrs["label"] = tbl_label
                        df.attrs["table_title"] = f"Supplementary Table {t_idx + 1} from file '{fname}'"
                        df.attrs["extractor"] = "builtin_supplementary"
                        dfs.append(df)
            return dfs
        except Exception as ex:
            print(f"  -> [Built-in Supp] 解析 Word 失败 {fname}: {ex}")
            return []

    # 4. ZIP archive (bounded single-level expansion with zip-bomb guard)
    if (low_name.endswith(".zip") or "application/zip" in ctype or is_zip_magic) and _depth == 0:
        try:
            with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
                names = zf.namelist()
                # Distinguish OOXML (.xlsx / .docx) packaged without file extension
                if "xl/workbook.xml" in names:
                    return _parse_supplementary_bytes(content_bytes, f"{filename_no_ext}.xlsx", _depth=1)
                if "word/document.xml" in names:
                    return _parse_supplementary_bytes(content_bytes, f"{filename_no_ext}.docx", _depth=1)
                total_uncompressed = 0
                parsed_count = 0
                for info in zf.infolist():
                    if info.is_dir() or info.filename.startswith("__MACOSX/"):
                        continue
                    inner_low = info.filename.lower()
                    if not inner_low.endswith((".xlsx", ".xls", ".csv", ".tsv", ".docx")):
                        continue
                    if info.file_size > 20 * 1024 * 1024 or total_uncompressed + info.file_size > 50 * 1024 * 1024:
                        continue
                    total_uncompressed += info.file_size
                    inner_bytes = zf.read(info.filename)
                    dfs.extend(_parse_supplementary_bytes(inner_bytes, os.path.basename(info.filename), _depth=1))
                    parsed_count += 1
                    if parsed_count >= 10:
                        break
            return dfs
        except Exception as ex:
            print(f"  -> [Built-in Supp] 解析 ZIP 失败 {fname}: {ex}")
            return []

    # 5. PDF
    if low_name.endswith(".pdf") or "application/pdf" in ctype or is_pdf_magic:
        if fitz is None:
            return []
        try:
            with fitz.open(stream=content_bytes, filetype="pdf") as pdf_doc:
                for p_idx in range(min(len(pdf_doc), 30)):
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    page = pdf_doc[p_idx]
                    try:
                        finder = page.find_tables()
                        tbl_list = list(finder.tables) if finder and finder.tables else []
                    except Exception:
                        tbl_list = []
                    if not tbl_list:
                        try:
                            finder = page.find_tables(strategy="text")
                            tbl_list = list(finder.tables) if finder and finder.tables else []
                        except Exception:
                            tbl_list = []
                    for t_idx, table in enumerate(tbl_list):
                        rows = table.extract()
                        if rows and len(rows) >= 2 and len(rows[0]) >= 2:
                            df = _rows_to_dataframe(rows)
                            if not df.empty:
                                tbl_label = (
                                    _ensure_supp_label_prefix(f"{filename_no_ext}_P{p_idx + 1}")
                                    if len(tbl_list) == 1
                                    else _ensure_supp_label_prefix(f"{filename_no_ext}_P{p_idx + 1}_T{t_idx + 1}")
                                )
                                df.attrs["label"] = tbl_label
                                df.attrs["table_title"] = f"Supplementary Table from page {p_idx + 1} of file '{fname}'"
                                df.attrs["extractor"] = "builtin_supplementary"
                                dfs.append(df)
            return dfs
        except Exception as ex:
            print(f"  -> [Built-in Supp] 解析 PDF 失败 {fname}: {ex}")
            return []

    return dfs


def _discover_supplementary_links(base_url, html_content, max_links=6):
    """
    从文章 HTML 中零依赖发现补充材料表格下载链接（强/弱特征分级 + 过滤正文 PDF 与图像/引文链接）。
    返回按优先级排序的 [{"url": str, "score": int, "filename": str, "marker": "strong"|"weak"}, ...]。
    """
    if not html_content:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    try:
        soup = BeautifulSoup(html_content, "html.parser")
    except Exception:
        return []

    base_parsed = urllib.parse.urlparse(base_url or "")
    base_sig = (
        (base_parsed.netloc or "").lower(),
        (base_parsed.path or "").rstrip("/"),
        base_parsed.query or "",
    )

    scored = {}
    for a in soup.find_all("a", href=True):
        raw_href = (a.get("href") or "").strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_url = urllib.parse.urljoin(base_url or "", raw_href)
        parsed = urllib.parse.urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue
        # 过滤指向当前文章页面自身的页内锚点（例如 Springer https://link.springer.com/article/...#MOESM1）
        cand_sig = (
            (parsed.netloc or "").lower(),
            (parsed.path or "").rstrip("/"),
            parsed.query or "",
        )
        if base_sig[0] and cand_sig == base_sig:
            continue
        if parsed.fragment:
            abs_url = urllib.parse.urlunparse(parsed._replace(fragment=""))
            parsed = urllib.parse.urlparse(abs_url)

        path_low = (parsed.path or "").lower()
        full_low = abs_url.lower()
        if any(path_low.endswith(ext) for ext in _SUPP_IGNORED_EXTS):
            continue
        if _SUPP_MAIN_PDF_RE.search(full_low):
            continue

        anchor_text = " ".join((a.get_text(" ", strip=True) or "").split())
        title_attr = str(a.get("title") or a.get("aria-label") or a.get("data-track-label") or "")
        parent_text = ""
        if a.parent is not None:
            parent_text = " ".join((a.parent.get_text(" ", strip=True) or "")[:240].split())
        context_text = f"{anchor_text} {title_attr} {parent_text}"

        has_tabular_ext = any(path_low.endswith(ext) for ext in _SUPP_TABULAR_EXTS)
        has_pdf_ext = path_low.endswith(".pdf")
        has_strong_url = bool(_SUPP_STRONG_URL_RE.search(full_low))
        has_weak_text = bool(_SUPP_WEAK_TEXT_RE.search(context_text))

        score = 0
        marker = ""
        if has_strong_url and has_tabular_ext:
            score = 100
            marker = "strong"
        elif has_strong_url:
            score = 80 if not has_pdf_ext else 70
            marker = "strong"
        elif has_tabular_ext and has_weak_text:
            score = 75
            marker = "strong"
        elif has_tabular_ext:
            # 裸 .xlsx/.xls/.csv/.tsv 链接（非引文）亦具较高表格概率
            if any(path_low.endswith(e) for e in (".xlsx", ".xls", ".csv", ".tsv")):
                score = 65
                marker = "strong"
            else:
                continue
        elif has_pdf_ext and has_weak_text:
            score = 45
            marker = "weak"
        else:
            continue

        # 轻微加权优先电子表格（Excel/CSV）高于 PDF
        if any(path_low.endswith(e) for e in (".xlsx", ".xls", ".csv", ".tsv")):
            score += 15
        elif path_low.endswith(".docx"):
            score += 8

        fname = os.path.basename(urllib.parse.unquote(parsed.path)) or "supplementary"
        prev = scored.get(abs_url)
        if prev is None or score > prev["score"]:
            scored[abs_url] = {
                "url": abs_url,
                "score": score,
                "filename": fname,
                "marker": marker,
            }

    ranked = sorted(scored.values(), key=lambda x: (-x["score"], x["url"]))
    return ranked[:max_links]


def _build_requests_cookie_jar(cookies_list, url=None):
    """Convert Playwright/companion cookie dicts into a requests CookieJar."""
    jar = requests.cookies.RequestsCookieJar()
    for c in cookies_list or []:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            continue
        dom = c.get("domain")
        if not dom and url:
            dom = urllib.parse.urlparse(url).hostname
        try:
            jar.set(
                str(c["name"]),
                str(c["value"]),
                domain=dom,
                path=c.get("path") or "/",
            )
        except Exception:
            pass
    return jar


def extract_builtin_supplementary_tables(
    url,
    html_content=None,
    cookies=None,
    cancel_event=None,
    max_files=6,
    max_timeout=25,
    prefetched_candidates=None,
):
    """
    零外部依赖的内置补充材料表格发现与解析回退通道（当外部 journal-supp-downloader 不可用时启用）。
    特点：
    - 复用浏览器/伴生工具 Cookie 与可选 NETWORK_PROXY；
    - 强/弱特征链接分级筛选，严格过滤正文 PDF 与图像/引文链接；
    - 下载前校验 Content-Type 与 HTML 软拦截响应，25MB 体积上限与 cancel_event 协作取消。
    """
    if not url or (cancel_event is not None and cancel_event.is_set()):
        return []

    deadline = time.monotonic() + max(5.0, float(max_timeout or 25))
    config = load_config() if callable(load_config) else {}
    proxies = get_requests_proxies(config)
    fetch_base_url = url
    try:
        from .proxy_rewriter import maybe_rewrite_institutional_url
    except Exception:
        maybe_rewrite_institutional_url = None
    if maybe_rewrite_institutional_url and (config.get("EZPROXY_ENABLED") or config.get("WEBVPN_ENABLED")):
        fetch_base_url = maybe_rewrite_institutional_url(url, config=config)
    cookie_list = cookies if cookies is not None else _collect_domain_cookies_for_scrapling(fetch_base_url)
    cookie_jar = _build_requests_cookie_jar(cookie_list, url=fetch_base_url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": fetch_base_url,
    }

    def _get(target_u, timeout_s, stream=False):
        per_req_to = max(2.0, min(float(timeout_s), 10.0))
        req_kw = {
            "headers": headers,
            "cookies": cookie_jar,
            "timeout": (min(per_req_to, 5.0), per_req_to),
            "allow_redirects": True,
        }
        if stream:
            req_kw["stream"] = True
        if proxies:
            req_kw["proxies"] = proxies
        resp = requests.get(target_u, **req_kw)
        if getattr(resp, "status_code", 200) in (403, 429) and requests_impersonate is not None:
            try:
                if hasattr(resp, "close"):
                    resp.close()
            except Exception:
                pass
            try:
                imp_kw = {
                    "impersonate": "chrome110",
                    "timeout": per_req_to,
                    "allow_redirects": True,
                }
                if proxies:
                    imp_kw["proxies"] = proxies
                imp_resp = requests_impersonate.get(target_u, **imp_kw)
                if getattr(imp_resp, "status_code", 0) == 200:
                    return imp_resp
            except Exception:
                pass
        return resp

    def _read_bounded_bytes(resp):
        """Read response body while strictly enforcing wall-clock `deadline`, `cancel_event`, and `max_bytes`."""
        try:
            iter_fn = getattr(resp, "iter_content", None)
            if callable(iter_fn) and not getattr(resp, "_content_consumed", False):
                try:
                    buf = bytearray()
                    for chunk in iter_fn(chunk_size=65536):
                        if cancel_event is not None and cancel_event.is_set():
                            return b""
                        if time.monotonic() >= deadline:
                            return b""
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            return b""
                    return bytes(buf)
                except (TypeError, RuntimeError):
                    pass
            raw = getattr(resp, "content", None) or b""
            return raw if len(raw) <= max_bytes else b""
        finally:
            try:
                if hasattr(resp, "close"):
                    resp.close()
            except Exception:
                pass

    if prefetched_candidates is not None:
        candidates = list(prefetched_candidates)[:max(1, int(max_files or 6))]
    else:
        page_html = html_content
        if not page_html:
            rem = deadline - time.monotonic()
            if rem <= 1.0:
                return []
            try:
                resp = _get(fetch_base_url, rem)
                if resp.status_code == 200 and resp.text and not is_anti_bot_blocked("", resp.text[:4000]):
                    page_html = resp.text
            except Exception as e:
                print(f"[Built-in Supp] 获取文章主页 HTML 失败: {e}")
                return []

        if not page_html:
            return []

        candidates = _discover_supplementary_links(fetch_base_url, page_html, max_links=max_files)
    if not candidates:
        return []

    print(f"[Built-in Supp] 发现 {len(candidates)} 个候选补充材料链接，开始校验下载并解析...")
    all_dfs = []
    blocked_strong_items = []
    max_bytes = 25 * 1024 * 1024

    def _is_akamai_browser_supp_candidate(item_dict):
        u_low = str(item_dict.get("url") or "").lower()
        return item_dict.get("marker") == "strong" and "mdpi.com/" in u_low and bool(re.search(r"/s\d+(?:[/?#]|$)", u_low))

    for item in candidates:
        if cancel_event is not None and cancel_event.is_set():
            break
        rem = deadline - time.monotonic()
        if rem <= 1.0:
            break
        dl_url = item["url"]
        try:
            r = _get(dl_url, rem, stream=True)
            if r.status_code != 200:
                if r.status_code in (403, 429) and _is_akamai_browser_supp_candidate(item):
                    blocked_strong_items.append(item)
                try:
                    if hasattr(r, "close"):
                        r.close()
                except Exception:
                    pass
                continue
            r_hdrs = getattr(r, "headers", None) or {}
            ctype = str(r_hdrs.get("Content-Type") or "").lower()
            if any(bad in ctype for bad in _SUPP_REJECT_CTYPES):
                if "text/html" in ctype and _is_akamai_browser_supp_candidate(item):
                    blocked_strong_items.append(item)
                try:
                    if hasattr(r, "close"):
                        r.close()
                except Exception:
                    pass
                continue
            content_bytes = _read_bounded_bytes(r)
            if not content_bytes or len(content_bytes) > max_bytes:
                continue
            head_low = content_bytes[:512].lstrip().lower()
            if head_low.startswith((b"<!doctype html", b"<html")):
                if _is_akamai_browser_supp_candidate(item):
                    blocked_strong_items.append(item)
                continue

            fname = item["filename"]
            cd = str(r_hdrs.get("Content-Disposition") or "")
            m_cd = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', cd, re.IGNORECASE)
            if m_cd:
                fname = urllib.parse.unquote(m_cd.group(1).strip())

            parsed_dfs = _parse_supplementary_bytes(
                content_bytes,
                fname,
                content_type=ctype,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            if parsed_dfs:
                all_dfs.extend(parsed_dfs)
        except Exception as ex:
            print(f"[Built-in Supp] 下载/解析 {dl_url} 跳过: {ex}")

    if not all_dfs and blocked_strong_items and not (cancel_event is not None and cancel_event.is_set()):
        browser_dfs = _download_blocked_supp_via_browser(
            fetch_base_url,
            blocked_strong_items,
            max_timeout=18.0,
            cancel_event=cancel_event,
        )
        if browser_dfs:
            all_dfs.extend(browser_dfs)

    if all_dfs:
        print(f"[Built-in Supp] 内置补充材料通道成功提取 {len(all_dfs)} 张补充表格。")
    return all_dfs


def _download_blocked_supp_via_browser(base_url, blocked_items, max_timeout=22.0, cancel_event=None):
    """
    当强特征补充材料链接（如 MDPI /s1 ZIP）在纯 HTTP 下触发 Akamai 403 / bm-verify HTML 壳时，
    使用无头有界 StealthySession 通过验证并复用该浏览器会话直接下载解析附件。
    """
    if not base_url or not blocked_items:
        return []
    if cancel_event is not None and cancel_event.is_set():
        return []
    if os.environ.get("ZTE_DISABLE_BROWSER_SUPP_FALLBACK") == "1":
        return []

    collected = []
    eff_max_t = max(18.0, float(max_timeout or 22.0))
    cap_ms = max(15000, min(int(eff_max_t * 1000), 25000))
    dl_deadline = time.monotonic() + eff_max_t + 8.0

    def _fetch_supp_action(page):
        if cancel_event is not None and cancel_event.is_set():
            return
        try:
            _scrapling_page_action(page)
        except Exception:
            pass
        req_api = getattr(getattr(page, "context", None), "request", None) or getattr(page, "request", None)
        if req_api is None:
            return
        max_bytes = 25 * 1024 * 1024
        for item in blocked_items[:2]:
            if cancel_event is not None and cancel_event.is_set():
                break
            rem_s = dl_deadline - time.monotonic()
            if rem_s <= 1.5:
                break
            dl_url = item["url"]
            try:
                resp = req_api.get(dl_url, timeout=min(18000, max(10000, int(rem_s * 1000))))
                if getattr(resp, "status", 0) != 200:
                    continue
                hdrs = {str(k).lower(): str(v) for k, v in (getattr(resp, "headers", None) or {}).items()}
                ctype = hdrs.get("content-type", "").lower()
                if any(bad in ctype for bad in _SUPP_REJECT_CTYPES):
                    continue
                body = resp.body()
                if not body or len(body) > max_bytes:
                    continue
                head_low = body[:512].lstrip().lower()
                if head_low.startswith((b"<!doctype html", b"<html")):
                    continue
                fname = item.get("filename") or "supplementary"
                cd = hdrs.get("content-disposition", "")
                m_cd = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', cd, re.IGNORECASE)
                if m_cd:
                    fname = urllib.parse.unquote(m_cd.group(1).strip())
                parsed_dfs = _parse_supplementary_bytes(
                    body,
                    fname,
                    content_type=ctype,
                    deadline=dl_deadline,
                    cancel_event=cancel_event,
                )
                if parsed_dfs:
                    collected.extend(parsed_dfs)
            except Exception as ex:
                print(f"[Browser Supp Fallback] 下载/解析 {dl_url} 跳过: {ex}")

    print(f"[Built-in Supp] 检测到 {len(blocked_items)} 个强特征补充链接遭反爬拦截，启动无头浏览器会话回退下载...")
    try:
        _scrapling_fetch_html(
            base_url,
            {
                "headless": True,
                "solve_cloudflare": False,
                "network_idle": False,
                "wait": 1500,
                "timeout": cap_ms,
                "page_action": _fetch_supp_action,
            },
        )
    except Exception as ex:
        print(f"[Browser Supp Fallback] 会话回退跳过: {ex}")
    if collected:
        print(f"[Browser Supp Fallback] 无头浏览器会话成功补提 {len(collected)} 张补充表格。")
    return collected


def _download_supp_via_browser_context(
    request_api,
    base_url,
    html_text,
    cancel_event=None,
    max_files=4,
    max_timeout=22.0,
    strong_only=True,
):
    """
    Download and parse supplementary attachments using an active authenticated browser
    context (Playwright/Scrapling `page.context.request.get`), inheriting solved
    Akamai `bm-verify` / Cloudflare cookies (critical for MDPI `/s1` & gated supp links).
    Zero network requests when no matching supplementary links exist in `html_text`.
    """
    if request_api is None or not base_url or not html_text:
        return []
    if cancel_event is not None and cancel_event.is_set():
        return []
    try:
        candidates = _discover_supplementary_links(base_url, html_text, max_links=max(max_files, 6))
        if strong_only:
            candidates = [c for c in candidates if c.get("marker") == "strong"]
        candidates = candidates[:max(1, int(max_files or 4))]
    except Exception:
        return []
    if not candidates:
        return []

    deadline = time.monotonic() + max(15.0, float(max_timeout or 22.0))
    all_dfs = []
    max_bytes = 25 * 1024 * 1024
    print(f"[Browser Supp] 发现 {len(candidates)} 个强特征补充材料链接，复用当前浏览器会话下载解析...")
    for item in candidates:
        if cancel_event is not None and cancel_event.is_set():
            break
        rem = deadline - time.monotonic()
        if rem <= 1.0:
            break
        dl_url = item["url"]
        try:
            resp = request_api.get(
                dl_url,
                headers={"Referer": base_url},
                timeout=int(min(rem, 18.0) * 1000),
            )
            status = getattr(resp, "status", 0)
            if status != 200:
                continue
            hdrs = getattr(resp, "headers", None) or {}
            ctype = str(hdrs.get("content-type") or hdrs.get("Content-Type") or "").lower()
            if any(bad in ctype for bad in _SUPP_REJECT_CTYPES):
                continue
            body = resp.body() if callable(getattr(resp, "body", None)) else b""
            if not body or len(body) > max_bytes:
                continue
            head_low = body[:512].lstrip().lower()
            if head_low.startswith((b"<!doctype html", b"<html")):
                continue
            fname = item["filename"]
            cd = str(hdrs.get("content-disposition") or hdrs.get("Content-Disposition") or "")
            m_cd = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', cd, re.IGNORECASE)
            if m_cd:
                fname = urllib.parse.unquote(m_cd.group(1).strip())
            parsed = _parse_supplementary_bytes(body, fname, content_type=ctype)
            if parsed:
                all_dfs.extend(parsed)
        except Exception as ex:
            print(f"[Browser Supp] 下载/解析 {dl_url} 跳过: {ex}")
    if all_dfs:
        print(f"[Browser Supp] 复用浏览器会话成功提取 {len(all_dfs)} 张补充表格。")
    return all_dfs


def _append_builtin_supp_if_any(
    dfs,
    base_url,
    html_text,
    table_idx="all",
    cancel_event=None,
    max_timeout=12,
    main_present_cap=6.0,
):
    """
    Fast in-memory supplementary link check on already-fetched article HTML.
    Zero network requests if no matching supplementary links exist; when strong
    supplementary links (e.g. Springer MOESM .xlsx/.docx, MDPI /s1) are discovered,
    downloads and appends them directly during Tier-1/Tier-2 extraction.
    When main `dfs` are already present, caps supplementary download using
    `main_present_cap` (default 6.0s for slow browser/cloud channels; Tier-1 fast
    HTTP callers can pass a larger cap like 12-15s) so slow/large attachments never
    blow the online budget and discard main tables.
    """
    if str(table_idx).lower() != "all" or not base_url or not html_text:
        return dfs
    if cancel_event is not None and cancel_event.is_set():
        return dfs
    try:
        candidates = _discover_supplementary_links(base_url, html_text, max_links=6)
        if dfs:
            candidates = [c for c in candidates if c.get("marker") == "strong"]
        if not candidates:
            return dfs
        base_timeout = float(max_timeout or 12)
        if dfs and main_present_cap is not None:
            eff_timeout = min(base_timeout, float(main_present_cap))
        else:
            eff_timeout = base_timeout
        supp_dfs = extract_builtin_supplementary_tables(
            base_url,
            html_content=html_text,
            cancel_event=cancel_event,
            max_files=len(candidates),
            max_timeout=eff_timeout,
            prefetched_candidates=candidates,
        )
        if supp_dfs:
            result = list(dfs) if dfs else []
            result.extend(supp_dfs)
            return result
    except Exception as ex:
        print(f"[Built-in Supp] 自动追加补充材料跳过: {ex}")
    return dfs


def extract_general_supplementary_tables(url, cancel_event=None, max_timeout=30, html_content=None):
    """
    Calls the journal-supp-downloader script to download supplementary files,
    then parses tables from them in-memory or from temp files.
    When already-fetched html_content is provided, tries the zero-dependency
    built-in supplementary link discovery FIRST (sub-second, no subprocess overhead),
    and also falls back to built-in discovery if the external script is unavailable,
    times out, or downloads zero files.
    """
    import subprocess
    import tempfile
    import shutil
    import os
    import io
    import sys
    import zipfile
    import xml.etree.ElementTree as ET
    import pandas as pd
    try:
        import fitz
    except ImportError:
        fitz = None

    if cancel_event is not None and cancel_event.is_set():
        return []

    supplementary_dfs = []
    builtin_tried_with_html = False

    # Fast path: when article HTML is already fetched in-memory, run zero-subprocess
    # built-in supplementary link discovery first (handles Springer MOESM, MDPI /s1, etc. in ~1-2s).
    if html_content:
        builtin_tried_with_html = True
        builtin_dfs = extract_builtin_supplementary_tables(
            url,
            html_content=html_content,
            cancel_event=cancel_event,
            max_timeout=min(15, max(5, int(max_timeout or 30))),
        )
        if builtin_dfs:
            return builtin_dfs
        if cancel_event is not None and cancel_event.is_set():
            return []
    
    # Locate the journal_downloader.py script (configured path first, then sibling skill dir)
    downloader_script = ""
    try:
        cfg_dl = (load_config() or {}).get("SUPP_DOWNLOADER_SCRIPT")
        if cfg_dl and os.path.exists(cfg_dl):
            downloader_script = cfg_dl
    except Exception:
        pass
    if not downloader_script:
        skill_dir = common.get_skill_root()
        skills_root = os.path.dirname(skill_dir)
        downloader_script = os.path.join(skills_root, "journal-supp-downloader", "scripts", "journal_downloader.py")
    
    if not os.path.exists(downloader_script):
        if builtin_tried_with_html:
            return []
        print(f"[在线提取] 找不到 journal-supp-downloader 技能脚本 ({downloader_script})，启用内置零依赖补充材料发现回退...")
        return extract_builtin_supplementary_tables(
            url,
            html_content=html_content,
            cancel_event=cancel_event,
            max_timeout=max_timeout,
        )
        
    temp_dir = tempfile.mkdtemp(prefix="supp_dl_")
    timed_out = False
    was_cancelled = False
    try:
        print(f"[在线提取] 调用 journal-supp-downloader 脚本获取补充文件: {url}")
        cmd = [sys.executable, downloader_script, url, "-o", temp_dir]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + max(5.0, float(max_timeout or 30))
        while proc.poll() is None:
            if (cancel_event is not None and cancel_event.is_set()) or time.monotonic() >= deadline:
                was_cancelled = bool(cancel_event is not None and cancel_event.is_set())
                timed_out = not was_cancelled
                reason = "取消信号" if was_cancelled else f"{max_timeout}s 超时"
                print(f"[在线提取] journal-supp-downloader 收到{reason}，终止子进程。")
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True,
                            timeout=5,
                        )
                    proc.kill()
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
                break
            time.sleep(0.25)

        if not timed_out and not was_cancelled:
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass

            # Search the temp_dir recursively for downloaded files
            downloaded_files = []
            for root_dir, dirs, files in os.walk(temp_dir):
                for file in files:
                    downloaded_files.append(os.path.join(root_dir, file))

            if not downloaded_files:
                print("[在线提取] 未能通过 journal-supp-downloader 下载到任何补充文件。")
            else:
                print(f"[在线提取] 成功下载 {len(downloaded_files)} 个补充文件。开始解析表格...")
                for filepath in downloaded_files:
                    filename = os.path.basename(filepath)
                    filename_no_ext = os.path.splitext(filename)[0]
                    try:
                        with open(filepath, "rb") as f:
                            content_bytes = f.read()
                    except Exception as e:
                        print(f"  -> 无法读取文件 {filename}: {e}")
                        continue

                    if filename.lower().endswith((".xlsx", ".xls")):
                        try:
                            excel_data = pd.read_excel(io.BytesIO(content_bytes), sheet_name=None)
                            print(f"  -> 成功解析 Excel 补充文件 {filename}。")
                            for sheet_name, df in excel_data.items():
                                if not df.empty:
                                    tbl_lbl = _format_supp_excel_label(filename_no_ext, sheet_name, len(excel_data) > 1)
                                    df.attrs['label'] = tbl_lbl
                                    df.attrs['table_title'] = f"Supplementary Table ({tbl_lbl}) from sheet '{sheet_name}' of file '{filename}'"
                                    supplementary_dfs.append(df)
                        except Exception as ex:
                            print(f"  -> 解析 Excel 失败 {filename}: {ex}")
                    elif filename.lower().endswith(".docx"):
                        try:
                            parsed_doc = _parse_supplementary_bytes(content_bytes, filename)
                            if parsed_doc:
                                supplementary_dfs.extend(parsed_doc)
                        except Exception as ex:
                            print(f"  -> 解析 Word 失败 {filename}: {ex}")
                    elif filename.lower().endswith((".csv", ".tsv", ".zip", ".pdf")):
                        try:
                            parsed_other = _parse_supplementary_bytes(content_bytes, filename)
                            if parsed_other:
                                supplementary_dfs.extend(parsed_other)
                        except Exception as ex:
                            print(f"  -> 解析补充文件失败 {filename}: {ex}")
    except Exception as e:
        print(f"[在线提取] 调用 journal-supp-downloader 失败: {e}")
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    if not supplementary_dfs and not was_cancelled and not builtin_tried_with_html:
        if timed_out:
            print("[在线提取] journal-supp-downloader 超时，自动回退内置零依赖补充材料发现通道...")
        else:
            print("[在线提取] journal-supp-downloader 未获取到有效补充表，自动回退内置零依赖补充材料发现通道...")
        return extract_builtin_supplementary_tables(
            url,
            html_content=html_content,
            cancel_event=cancel_event,
            max_timeout=min(12, max(5, int(max_timeout or 12))),
        )

    return supplementary_dfs


def _parse_elsevier_xocs_tables(xml_content, table_idx="all"):
    """
    Parses Elsevier XOCS XML (<ce:table> / CALS model) into DataFrames.
    Supports multi-tgroup tables (wide tables split horizontally).
    """
    import xml.etree.ElementTree as ET

    def iter_text(elem):
        parts = []
        if elem.text:
            parts.append(elem.text.strip())
        for child in elem:
            parts.extend(iter_text(child))
            if child.tail:
                parts.append(child.tail.strip())
        return parts

    def get_cell_text(cell_elem):
        return ' '.join(t for t in iter_text(cell_elem) if t)

    def parse_single_tgroup(tgroup, parent_elem):
        num_cols = int(tgroup.attrib.get('cols', 0))
        colspecs = tgroup.findall('.//colspec')
        col_names = []
        for idx, cs in enumerate(colspecs):
            col_names.append(cs.attrib.get('colname', f'col{idx+1}'))
        
        all_rows = tgroup.findall('.//row')
        if not all_rows:
            all_rows = parent_elem.findall('.//row')
            
        if not all_rows:
            return None
            
        if num_cols <= 0:
            for row in all_rows:
                row_cols = 0
                for entry in row.findall('.//entry'):
                    namest = entry.attrib.get('namest')
                    nameend = entry.attrib.get('nameend')
                    if namest and nameend:
                        m1 = re.search(r'\d+', namest)
                        m2 = re.search(r'\d+', nameend)
                        if m1 and m2:
                            colspan = int(m2.group()) - int(m1.group()) + 1
                        else:
                            colspan = 1
                    else:
                        colspan = int(entry.attrib.get('colspan', 1))
                    row_cols += colspan
                num_cols = max(num_cols, row_cols)
            if num_cols <= 0:
                num_cols = max(len(row.findall('.//entry')) for row in all_rows)
        
        if not col_names:
            col_names = [f'col{i+1}' for i in range(num_cols)]
            
        num_rows = len(all_rows)
        grid = [[None for _ in range(num_cols)] for _ in range(num_rows)]
        
        for r_idx, row in enumerate(all_rows):
            entries = row.findall('.//entry')
            if not entries:
                entries = row.findall('.//td') + row.findall('.//th')
            c_idx = 0
            for entry in entries:
                while c_idx < num_cols and grid[r_idx][c_idx] is not None:
                    c_idx += 1
                if c_idx >= num_cols:
                    break
                    
                text = get_cell_text(entry)
                
                namest = entry.attrib.get('namest')
                nameend = entry.attrib.get('nameend')
                if namest and nameend:
                    try:
                        start_idx = col_names.index(namest)
                        end_idx = col_names.index(nameend)
                        colspan = end_idx - start_idx + 1
                    except ValueError:
                        colspan = 1
                else:
                    colspan = int(entry.attrib.get('colspan', 1))
                    
                morerows = entry.attrib.get('morerows')
                if morerows:
                    rowspan = int(morerows) + 1
                else:
                    rowspan = int(entry.attrib.get('rowspan', 1))
                    
                for dr in range(rowspan):
                    for dc in range(colspan):
                        if r_idx + dr < num_rows and c_idx + dc < num_cols:
                            grid[r_idx + dr][c_idx + dc] = text
                c_idx += colspan

        thead = tgroup.find('.//thead')
        if thead is None:
            thead = parent_elem.find('.//thead')
        raw_thead_rows = len(thead.findall('.//row')) if thead is not None else 0

        # Preserve single-row headerless tables (e.g., older Elsevier XOCS XML with thead=0, tbody=1 row)
        if raw_thead_rows == 0 and num_rows == 1:
            row0 = [c if c is not None else "" for c in grid[0]]
            if not any(str(c).strip() for c in row0):
                return None
            unique_headers = [
                (col_names[c] if c < len(col_names) and col_names[c] else f"Col_{c + 1}")
                for c in range(num_cols)
            ]
            return pd.DataFrame([row0], columns=unique_headers)

        thead_rows_count = raw_thead_rows if raw_thead_rows > 0 else 1
        if num_rows <= thead_rows_count:
            return None
            
        unique_headers = []
        seen = {}
        for col in range(num_cols):
            col_headers = []
            for r in range(thead_rows_count):
                val = grid[r][col]
                if val:
                    val = val.strip()
                    val = re.sub(r'\s+', ' ', val)
                    if val and val not in col_headers:
                        col_headers.append(val)
            header_name = ' - '.join(col_headers) if col_headers else "Unnamed"
            if header_name in seen:
                seen[header_name] += 1
                unique_headers.append(f"{header_name}_{seen[header_name]}")
            else:
                seen[header_name] = 0
                unique_headers.append(header_name)
                
        data_rows = grid[thead_rows_count:]
        for r in data_rows:
            for c in range(num_cols):
                if r[c] is None:
                    r[c] = ""
                    
        df = pd.DataFrame(data_rows, columns=unique_headers)
        return df

    try:
        xml_clean = re.sub(r'(</?)[a-zA-Z0-9_]+:', r'\1', xml_content)
        xml_clean = re.sub(r'\s+[a-zA-Z0-9_]+:([a-zA-Z0-9__-]+=(?:["\']))', r' \1', xml_clean)
        xml_clean = re.sub(r'\s+xmlns(?::[a-zA-Z0-9_]+)?="[^"]+"', '', xml_clean)
        root = ET.fromstring(xml_clean)

        tables_found = []
        table_containers = [elem for elem in root.iter() if (elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag) == 'table']
        
        if not table_containers:
            table_containers = [elem for elem in root.iter() if (elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag) == 'tgroup']

        for elem in table_containers:
            label_text = ""
            caption_text = ""
            
            label_elem = elem.find('.//label')
            if label_elem is not None:
                label_text = get_cell_text(label_elem).strip()
                
            caption_elem = elem.find('.//caption')
            if caption_elem is not None:
                caption_text = get_cell_text(caption_elem).strip()

            tgroups = elem.findall('.//tgroup')
            if not tgroups:
                tgroups = [elem]

            part_dfs = []
            for tg in tgroups:
                df_part = parse_single_tgroup(tg, elem)
                if df_part is not None and not df_part.empty:
                    part_dfs.append(df_part)

            if not part_dfs:
                has_img_ref = (
                    elem.find('.//link') is not None
                    or elem.find('.//graphic') is not None
                    or elem.find('.//object') is not None
                )
                if has_img_ref:
                    print(
                        f"[Elsevier API Warning] {label_text or 'Table'} 在 XOCS XML 中以图片引用 (<ce:link>) 存储，"
                        f"无结构化文本表格，需由本地 PDF/OCR 补漏。"
                    )
                continue

            if len(part_dfs) == 1:
                df = part_dfs[0]
            else:
                base_df = part_dfs[0]
                extra_dfs = []
                for other_df in part_dfs[1:]:
                    if other_df.columns[0] == base_df.columns[0]:
                        extra_dfs.append(other_df.iloc[:, 1:])
                    else:
                        extra_dfs.append(other_df)
                df = pd.concat([base_df] + extra_dfs, axis=1)

            if not df.empty:
                if label_text:
                    df.attrs['label'] = label_text
                if caption_text:
                    df.attrs['title'] = caption_text
                    
                if label_text and caption_text:
                    df.attrs['table_title'] = f"{label_text}. {caption_text}"
                elif label_text:
                    df.attrs['table_title'] = label_text
                elif caption_text:
                    df.attrs['table_title'] = caption_text
                tables_found.append(df)

        if not tables_found:
            print("[Elsevier API] XML 中未找到可解析的表格结构。")
            return []

        print(f"[Elsevier API] 从 XOCS XML 中提取到 {len(tables_found)} 张表格。")
        if table_idx == "all":
            return tables_found
        elif table_idx == "first":
            return [tables_found[0]]
        else:
            try:
                idx = int(table_idx)
                return [tables_found[idx]] if idx < len(tables_found) else tables_found
            except ValueError:
                return tables_found
        return tables_found
    except Exception as e:
        print(f"[Elsevier API] XOCS XML 解析失败: {e}")
        return []


def _parse_jats_xml_tables(xml_content, table_idx="all", source="ncbi_pmc_jats", log_prefix="[NCBI PMC JATS]"):
    """
    解析 NISO JATS XML（NCBI PMC / Europe PMC / Springer Nature TDM / Copernicus 等）中的 <table-wrap> 表格元素为 DataFrames。
    支持 XHTML table 规范 (thead, tbody, tr, th, td) 与 CALS 规范 (oasis:table, tgroup)。
    完美保留 rowspan, colspan 拓扑结构、caption 标题以及 table-wrap-foot 尾注。
    保真度 100%，无 OCR 误差。
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_content)
    except Exception as e:
        print(f"{log_prefix} XML 解析失败: {e}")
        return []

    tables_found = []
    table_wraps = root.findall('.//{*}table-wrap')
    if not table_wraps:
        table_wraps = [el for el in root.iter() if el.tag.split('}')[-1] == 'table-wrap']

    for tw_idx, tw in enumerate(table_wraps):
        label_elem = tw.find('.//{*}label')
        label_text = "".join(label_elem.itertext()).strip() if label_elem is not None else ""

        cap_elem = tw.find('.//{*}caption')
        caption_text = "".join(cap_elem.itertext()).strip() if cap_elem is not None else ""

        footnotes = []
        foot_elem = tw.find('.//{*}table-wrap-foot')
        if foot_elem is not None:
            fn_nodes = foot_elem.findall('.//{*}fn') or foot_elem.findall('.//{*}p')
            for fn in fn_nodes:
                fn_text = _xml_cell_to_text(fn)
                if fn_text:
                    footnotes.append(fn_text)
            if not footnotes:
                combined_foot = _xml_cell_to_text(foot_elem)
                if combined_foot:
                    footnotes.append(combined_foot)

        tbl_node = tw.find('.//{*}table')
        if tbl_node is not None:
            thead = tbl_node.find('.//{*}thead')
            first_tr = tbl_node.find('.//{*}tr')
            first_row_has_th = bool(first_tr is not None and any(c.tag.split('}')[-1] == 'th' for c in first_tr))
            has_explicit_header = True if (thead is not None or first_row_has_th) else None

            rows = tbl_node.findall('.//{*}tr')
            if rows:
                grid = []
                for r_idx, tr in enumerate(rows):
                    if len(grid) <= r_idx:
                        grid.append([])
                    cells = [c for c in tr if c.tag.split('}')[-1] in ('th', 'td')]
                    c_idx = 0
                    for cell in cells:
                        while c_idx < len(grid[r_idx]) and grid[r_idx][c_idx] is not None:
                            c_idx += 1
                        try:
                            colspan = int(cell.attrib.get('colspan', 1))
                        except Exception:
                            colspan = 1
                        try:
                            rowspan = int(cell.attrib.get('rowspan', 1))
                        except Exception:
                            rowspan = 1

                        text = _xml_cell_to_text(cell)

                        while len(grid) < r_idx + rowspan:
                            grid.append([])

                        for dr in range(rowspan):
                            curr_r = r_idx + dr
                            while len(grid[curr_r]) < c_idx + colspan:
                                grid[curr_r].append(None)
                            for dc in range(colspan):
                                grid[curr_r][c_idx + dc] = text
                        c_idx += colspan

                if grid:
                    max_cols = max(len(r) for r in grid)
                    clean_grid = []
                    for r in grid:
                        row_vals = [c if c is not None else "" for c in r]
                        if len(row_vals) < max_cols:
                            row_vals.extend([""] * (max_cols - len(row_vals)))
                        clean_grid.append(row_vals)

                    df = _rows_to_dataframe(clean_grid, has_explicit_header=has_explicit_header)
                    if not df.empty and df.shape[0] >= 1 and df.shape[1] >= 1:
                        if label_text:
                            df.attrs['label'] = label_text
                        if caption_text:
                            df.attrs['table_title'] = f"{label_text} {caption_text}".strip()
                        elif label_text:
                            df.attrs['table_title'] = label_text
                        df.attrs['extractor'] = source
                        df.attrs['source'] = source
                        if footnotes:
                            df.attrs['footnotes'] = footnotes
                        tables_found.append(df)
                        continue

        tgroups = tw.findall('.//{*}tgroup')
        if tgroups:
            cals_dfs = _parse_elsevier_xocs_tables(ET.tostring(tw, encoding='unicode'))
            for c_df in cals_dfs:
                if label_text:
                    c_df.attrs['label'] = label_text
                if caption_text:
                    c_df.attrs['table_title'] = f"{label_text} {caption_text}".strip()
                c_df.attrs['extractor'] = source
                c_df.attrs['source'] = source
                if footnotes:
                    c_df.attrs['footnotes'] = footnotes
                tables_found.append(c_df)

    if not tables_found:
        print(f"{log_prefix} XML 中未找到可解析的表格。")
        return []

    print(f"{log_prefix} 从 JATS XML 中提取到 {len(tables_found)} 张高保真表格。")
    if table_idx == "all":
        return tables_found
    elif table_idx == "first":
        return [tables_found[0]]
    else:
        try:
            idx = int(table_idx)
            return [tables_found[idx]] if idx < len(tables_found) else tables_found
        except ValueError:
            return tables_found


_SPRINGER_NATURE_DOI_PREFIXES = (
    "10.1007/",
    "10.1186/",
    "10.1038/",
    "10.1023/",
    "10.1057/",
    "10.1140/",
    "10.1365/",
    "10.1065/",
    "10.1557/",
    "10.1134/",
    "10.1245/",
    "10.2165/",
    "10.3758/",
)

_SPRINGER_NATURE_DOMAINS = (
    "springer.com",
    "springernature.com",
    "nature.com",
    "biomedcentral.com",
    "springeropen.com",
    "palgrave.com",
)


def is_springer_nature_target(doi=None, url=None) -> bool:
    """Return True if DOI prefix or publisher URL belongs to Springer Nature / BMC / Palgrave / Nature."""
    if doi:
        clean = str(doi).strip().lower()
        if clean.startswith("doi:"):
            clean = clean[4:].strip()
        clean = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", clean)
        if any(clean.startswith(p) for p in _SPRINGER_NATURE_DOI_PREFIXES):
            return True
    if url:
        low_u = str(url).strip().lower()
        if any(dom in low_u for dom in _SPRINGER_NATURE_DOMAINS):
            return True
    return False


def extract_tables_via_springer_tdm(doi, table_idx="all", api_key=None, cancel_event=None):
    """
    通过 Springer Nature TDM JATS XML API 直接提取高保真表格。
    Endpoint: https://api.springernature.com/xmldata/jats?q=doi:"{doi}"&p=1&s=1&api_key={key}
    支持 Springer / BMC / Nature / Kluwer / Palgrave / EPJ 等前缀（10.1007, 10.1186, 10.1038, 10.1023, 10.1057, 10.1140 等）。
    """
    if not doi or (cancel_event is not None and cancel_event.is_set()):
        return []

    cfg = load_config() if callable(load_config) else {}
    if not cfg.get("SPRINGER_TDM_ENABLED", True):
        return []

    key = str(api_key or cfg.get("SPRINGER_API_KEY") or os.environ.get("SPRINGER_API_KEY") or "").strip()
    if not key:
        return []

    clean_doi = str(doi).strip()
    if clean_doi.lower().startswith("doi:"):
        clean_doi = clean_doi[4:].strip()
    clean_doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", clean_doi, flags=re.IGNORECASE).strip()
    if not clean_doi:
        return []

    proxies = get_requests_proxies(cfg)
    query_val = urllib.parse.quote(f'doi:"{clean_doi}"', safe=':"/')
    api_url = (
        f"https://api.springernature.com/xmldata/jats"
        f"?q={query_val}&p=1&s=1&api_key={urllib.parse.quote(key, safe='')}"
    )
    headers = {
        "Accept": "application/xml, text/xml",
        "User-Agent": "zotero-table-extractor/1.6.0",
    }
    req_kwargs = {"headers": headers, "timeout": 20}
    if proxies:
        req_kwargs["proxies"] = proxies

    print(f"[Springer TDM JATS] 正在获取 JATS XML: {clean_doi}")
    try:
        for attempt in range(2):
            if cancel_event is not None and cancel_event.is_set():
                return []
            resp = requests.get(api_url, **req_kwargs)
            if resp.status_code == 429 and attempt == 0:
                hdrs = getattr(resp, "headers", None) or {}
                retry_after = hdrs.get("Retry-After") or hdrs.get("retry-after")
                try:
                    wait_s = min(max(1.0, float(retry_after)), 20.0) if retry_after else 3.0
                except (TypeError, ValueError):
                    wait_s = 3.0
                print(f"[Springer TDM JATS] 触发 429 限流，{wait_s:.1f}s 后重试一次...")
                if cancel_event is not None:
                    if cancel_event.wait(wait_s):
                        return []
                else:
                    time.sleep(wait_s)
                continue
            if resp.status_code == 200 and resp.text:
                low_xml = resp.text.lower()
                if "table-wrap" in low_xml or "<table" in low_xml or ":table" in low_xml:
                    return _parse_jats_xml_tables(
                        resp.text,
                        table_idx=table_idx,
                        source="springer_tdm_jats",
                        log_prefix="[Springer TDM JATS]",
                    )
            print(f"[Springer TDM JATS] 获取失败或无正文表格: HTTP {resp.status_code}")
            break
    except Exception as e:
        safe_err = str(e).replace(key, "***").replace(urllib.parse.quote(key, safe=""), "***")
        print(f"[Springer TDM JATS] 获取 JATS XML 异常: {safe_err}")
    return []


def extract_tables_via_europepmc_xml(pmcid, table_idx="all", cancel_event=None):
    """
    通过 Europe PMC REST API 直接获取全文 JATS XML (/{pmcid}/fullTextXML) 提取高保真表格。
    """
    if not pmcid or (cancel_event is not None and cancel_event.is_set()):
        return []
    from .doi_resolver import extract_pmcid

    clean_pmc = (extract_pmcid(pmcid) or str(pmcid).strip().upper())
    if not clean_pmc.startswith("PMC"):
        return []

    cfg = load_config() if callable(load_config) else {}
    proxies = get_requests_proxies(cfg)
    xml_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{clean_pmc}/fullTextXML"
    headers = {
        "Accept": "application/xml, text/xml",
        "User-Agent": "zotero-table-extractor/1.6.0",
    }
    req_kwargs = {"headers": headers, "timeout": 15}
    if proxies:
        req_kwargs["proxies"] = proxies

    print(f"[Europe PMC JATS] 正在直取全文 JATS XML: {clean_pmc}")
    try:
        r = requests.get(xml_url, **req_kwargs)
        if r.status_code == 200 and r.text:
            low_xml = r.text.lower()
            if "table-wrap" in low_xml or "<table" in low_xml or ":table" in low_xml:
                return _parse_jats_xml_tables(
                    r.text,
                    table_idx=table_idx,
                    source="europepmc_jats",
                    log_prefix="[Europe PMC JATS]",
                )
        print(f"[Europe PMC JATS] 获取失败或无正文表格: HTTP {r.status_code}")
    except Exception as e:
        print(f"[Europe PMC JATS] 获取全文 JATS XML 异常: {e}")
    return []


def extract_tables_via_ncbi_pmc(pmcid_or_doi, table_idx="all"):
    """
    通过 NCBI PMC Open Access API 获取正文 JATS XML 并提取高保真表格。
    支持输入 PMCID (如 PMC1234567) 或 DOI (如 10.1371/journal.pone.0251234)。
    免费开放获取，无需任何付费 API Key。
    """
    if not pmcid_or_doi:
        return []

    cfg = load_config() if callable(load_config) else {}
    if not cfg.get("NCBI_PMC_ENABLED", True):
        return []

    from .doi_resolver import resolve_pmcid_from_doi, extract_pmcid

    pmcid = extract_pmcid(pmcid_or_doi)
    if not pmcid and ("10." in pmcid_or_doi or "/" in pmcid_or_doi):
        pmcid = resolve_pmcid_from_doi(pmcid_or_doi)

    if not pmcid:
        return []

    clean_pmc = pmcid.upper()
    print(f"[NCBI PMC] 正在获取 JATS XML: {clean_pmc}")
    efetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={clean_pmc}&retmode=xml"
    headers = {
        "User-Agent": "zotero-table-extractor/1.6.0 (mailto:extractor@zotero.org)"
    }
    try:
        r = requests.get(efetch_url, headers=headers, timeout=15)
        if r.status_code == 200 and ("<table-wrap" in r.text or "<table" in r.text):
            return _parse_jats_xml_tables(r.text, table_idx=table_idx)
        else:
            print(f"[NCBI PMC] 获取失败或无正文表格: HTTP {r.status_code}")
    except Exception as e:
        print(f"[NCBI PMC] 获取 JATS XML 异常: {e}")
    return []


def extract_tables_via_arxiv_html(arxiv_id_or_url, table_idx="all"):
    """
    通过 arXiv 官方 HTML (arXiv LaTeXML) 或 ar5iv 获取论文 HTML 并提取高保真表格。
    arXiv 官方自 2024 年起提供高保真 HTML 渲染 (https://arxiv.org/html/<id>)，
    并可通过 ar5iv (https://ar5iv.labs.arxiv.org/html/<id>) 兜底。
    保真度 100%，无 OCR 误差。
    """
    if not arxiv_id_or_url:
        return []

    cfg = load_config() if callable(load_config) else {}
    if not cfg.get("ARXIV_HTML_ENABLED", True):
        return []

    from .doi_resolver import extract_arxiv_id
    arxiv_id = extract_arxiv_id(arxiv_id_or_url)
    if not arxiv_id:
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    }

    urls_to_try = [
        f"https://arxiv.org/html/{arxiv_id}",
        f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}"
    ]

    html_content = ""
    source_name = ""
    for url in urls_to_try:
        try:
            print(f"[arXiv HTML] 正在请求论文原生 HTML: {url}")
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200 and ("ltx_table" in r.text or "<table" in r.text):
                html_content = r.text
                source_name = "arxiv_html" if "arxiv.org" in url else "ar5iv_html"
                break
        except Exception as e:
            print(f"[arXiv HTML] 请求 {url} 失败: {e}")

    if not html_content:
        print(f"[arXiv HTML] 无法获取论文 {arxiv_id} 的 HTML 版本。")
        return []

    tables_found = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")

        figures = soup.find_all("figure", class_="ltx_table")
        if not figures:
            figures = soup.find_all("figure")
            figures = [f for f in figures if f.find("table")]

        if figures:
            for fig in figures:
                cap_el = fig.find("figcaption")
                caption_text = cap_el.get_text(" ", strip=True) if cap_el else ""
                tag_el = fig.find("span", class_="ltx_tag_table")
                label_text = tag_el.get_text(strip=True).rstrip(":") if tag_el else ""

                if not label_text and caption_text:
                    m_lbl = re.search(r'^(?:Table|表)\s*\d+(?:\.\d+)?', caption_text, re.IGNORECASE)
                    if m_lbl:
                        label_text = m_lbl.group(0)

                footnotes = []
                notes_div = fig.find("div", class_=re.compile(r'ltx_(?:table_)?notes?'))
                if notes_div:
                    fn_str = _bs4_cell_to_text(notes_div)
                    if fn_str:
                        footnotes.append(fn_str)

                tbl = fig.find("table")
                if not tbl:
                    continue

                has_thead = tbl.find("thead") is not None
                first_tr = tbl.find("tr")
                first_row_has_th = bool(first_tr is not None and first_tr.find_all("th"))
                has_explicit_header = True if (has_thead or first_row_has_th) else None

                rows = tbl.find_all("tr")
                if not rows:
                    continue

                grid = []
                for r_idx, tr in enumerate(rows):
                    if len(grid) <= r_idx:
                        grid.append([])
                    cells = tr.find_all(["th", "td"])
                    c_idx = 0
                    for cell in cells:
                        while c_idx < len(grid[r_idx]) and grid[r_idx][c_idx] is not None:
                            c_idx += 1
                        try:
                            colspan = int(cell.get("colspan", 1))
                        except Exception:
                            colspan = 1
                        try:
                            rowspan = int(cell.get("rowspan", 1))
                        except Exception:
                            rowspan = 1

                        text = _bs4_cell_to_text(cell)

                        while len(grid) < r_idx + rowspan:
                            grid.append([])

                        for dr in range(rowspan):
                            curr_r = r_idx + dr
                            while len(grid[curr_r]) < c_idx + colspan:
                                grid[curr_r].append(None)
                            for dc in range(colspan):
                                grid[curr_r][c_idx + dc] = text
                        c_idx += colspan

                if grid:
                    max_cols = max(len(r) for r in grid)
                    clean_grid = []
                    for r in grid:
                        row_vals = [c if c is not None else "" for c in r]
                        if len(row_vals) < max_cols:
                            row_vals.extend([""] * (max_cols - len(row_vals)))
                        clean_grid.append(row_vals)

                    df = _rows_to_dataframe(clean_grid, has_explicit_header=has_explicit_header)
                    if not df.empty and df.shape[0] >= 1 and df.shape[1] >= 1:
                        if label_text:
                            df.attrs['label'] = label_text
                        if caption_text:
                            df.attrs['table_title'] = caption_text
                        elif label_text:
                            df.attrs['table_title'] = label_text
                        df.attrs['extractor'] = source_name
                        df.attrs['source'] = source_name
                        if footnotes:
                            df.attrs['footnotes'] = footnotes
                        tables_found.append(df)
        else:
            tables_found = parse_tables_from_html(html_content, table_idx=table_idx)
            for df in tables_found:
                df.attrs['extractor'] = source_name
                df.attrs['source'] = source_name

    except Exception as e:
        print(f"[arXiv HTML] 解析 HTML 异常: {e}")
        return []

    print(f"[arXiv HTML] 成功提取到 {len(tables_found)} 张高保真表格。")
    if table_idx == "all":
        return tables_found
    elif table_idx == "first":
        return [tables_found[0]]
    else:
        try:
            idx = int(table_idx)
            return [tables_found[idx]] if idx < len(tables_found) else tables_found
        except ValueError:
            return tables_found


def _extract_table_subpage_urls(base_url, html_text, limit=12):
    """Extract unique Springer/Nature-style /tables/<N> subpage URLs from article HTML."""
    urls = []
    seen = set()
    for m in re.finditer(r'href=["\']([^"\']*?/tables/\d+[^"\']*)["\']', html_text or "", re.IGNORECASE):
        abs_u = urllib.parse.urljoin(base_url, m.group(1).split("#")[0])
        if abs_u != base_url and abs_u not in seen:
            seen.add(abs_u)
            urls.append(abs_u)
            if len(urls) >= limit:
                break
    return urls


def _follow_table_subpages(base_url, html_text, fetch_html_fn, table_idx="all", cancel_event=None):
    """Follow /tables/N subpage links or Copernicus JATS XML links using fetch_html_fn."""
    if cancel_event is not None and cancel_event.is_set():
        return []
    # Copernicus & open-access JATS XML link (e.g. se-9-373-2018.xml)
    if "copernicus.org" in (base_url or "").lower():
        xml_m = re.search(r'(?:href|content)=["\']([^"\']+?\.xml)["\']', html_text or "", re.IGNORECASE)
        if xml_m:
            xml_url = urllib.parse.urljoin(base_url, xml_m.group(1))
            xml_text = fetch_html_fn(xml_url)
            if xml_text and "<table-wrap" in xml_text:
                xml_dfs = _parse_jats_xml_tables(
                    xml_text,
                    table_idx=table_idx,
                    source="copernicus_jats",
                    log_prefix="[Copernicus JATS]",
                )
                if xml_dfs:
                    return xml_dfs

    sub_urls = _extract_table_subpage_urls(base_url, html_text)
    if not sub_urls:
        return []
    all_dfs = []
    for sub_u in sub_urls:
        if cancel_event is not None and cancel_event.is_set():
            break
        sub_html = ""
        for _attempt in range(2):
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                cand_html = fetch_html_fn(sub_u)
            except Exception:
                cand_html = ""
            if cand_html and not is_anti_bot_blocked("", cand_html):
                sub_html = cand_html
                break
            if _attempt == 0:
                if cancel_event is not None:
                    if cancel_event.wait(0.5):
                        break
                else:
                    time.sleep(0.5)
        if not sub_html:
            print(f"[Subpage Warning] 表格子页抓取失败（重试后仍为空或被拦截）: {sub_u}")
            continue
        sub_dfs = parse_tables_from_html(sub_html, "all")
        if sub_dfs:
            all_dfs.extend(sub_dfs)
    if not all_dfs:
        return []
    return _select_tables(all_dfs, table_idx) or []


def extract_tables_via_direct_http(url, table_idx="all", cancel_event=None, doi=None, meta_out=None):

    """
    Attempts to fetch HTML directly via requests from the local network/IP.
    If the user is within an institutional IP network, this direct access
    will easily fetch the page with full text and tables without using browser automation
    or third-party cloud scraper proxies (like Firecrawl) which route through non-institutional IPs.
    We prioritize curl_cffi for TLS impersonation to bypass Cloudflare bot detection.
    """
    if cancel_event is not None and cancel_event.is_set():
        return []
    try:
        from .negative_cache import is_negatively_cached, record_negative_cache, clear_negative_cache
    except Exception:
        is_negatively_cached = None
        record_negative_cache = None
        clear_negative_cache = None
    try:
        from .proxy_rewriter import (
            is_institutional_proxy_url,
            maybe_rewrite_institutional_url,
            validate_institutional_session,
        )
    except Exception:
        is_institutional_proxy_url = None
        maybe_rewrite_institutional_url = None
        validate_institutional_session = None

    config = load_config() if callable(load_config) else {}
    if is_negatively_cached:
        hit = is_negatively_cached("direct_http", url=url, doi=doi, config=config)
        if hit:
            print(f"[DirectHTTP] 命中跨进程负缓存 ({hit.get('target_key')}, {hit.get('reason')})，跳过请求。")
            return []

    try:
        proxies = get_requests_proxies(config)
        fetch_url = url
        inst_proxy_active = False
        if maybe_rewrite_institutional_url and (config.get("EZPROXY_ENABLED") or config.get("WEBVPN_ENABLED")):
            fetch_url = maybe_rewrite_institutional_url(url, config=config)
            inst_proxy_active = (fetch_url != url) or (
                callable(is_institutional_proxy_url) and is_institutional_proxy_url(fetch_url, config=config)
            )

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "max-age=0",
            "Upgrade-Insecure-Requests": "1"
        }

        used_impersonate = requests_impersonate is not None
        if isinstance(meta_out, dict):
            meta_out["used_impersonate"] = used_impersonate

        def _fetch_one(target_url):
            if cancel_event is not None and cancel_event.is_set():
                return None, "", target_url
            if requests_impersonate is not None:
                kw = {"impersonate": "chrome110", "timeout": 12, "allow_redirects": True}
                if proxies:
                    kw["proxies"] = proxies
                resp = requests_impersonate.get(target_url, **kw)
                return resp.status_code, resp.text, str(getattr(resp, "url", "") or target_url)
            kw = {"headers": headers, "timeout": 12, "allow_redirects": True}
            if proxies:
                kw["proxies"] = proxies
            resp = requests.get(target_url, **kw)
            return resp.status_code, resp.text, str(getattr(resp, "url", "") or target_url)

        if used_impersonate:
            print(f"尝试本地直接 HTTP 请求网页 (机构 IP 直连 + TLS Chrome 模拟): {fetch_url} ...")
        else:
            print(f"尝试本地直接 HTTP 请求网页 (机构 IP 直连 + 标头伪装): {fetch_url} ...")
        status_code, text, final_url = _fetch_one(fetch_url)
        if cancel_event is not None and cancel_event.is_set():
            return []

        if isinstance(meta_out, dict):
            meta_out["status_code"] = status_code
            meta_out["final_url"] = final_url
            meta_out["was_dead_http"] = status_code in (404, 410)

        if inst_proxy_active and validate_institutional_session:
            sess = validate_institutional_session(
                html_text=text or "",
                final_url=final_url,
                status_code=status_code,
            )
            if not sess.get("valid") and sess.get("needs_relogin"):
                print(
                    f"[DirectHTTP] 机构代理会话已失效 ({sess.get('reason')})，请先刷新 EZProxy/WebVPN 登录态后重试（不写入反爬负缓存）。"
                )
                return []

        # Follow Akamai bm-verify meta-refresh once before declaring blocked (e.g., MDPI)
        if status_code == 200 and text and "bm-verify=" in text[:4000].lower():
            m_bm = re.search(
                r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\'][^"\']*url=([^"\'>\s]+)',
                text[:4000],
                re.IGNORECASE,
            )
            if m_bm:
                bm_url = urllib.parse.urljoin(final_url or fetch_url, m_bm.group(1).strip("'\""))
                try:
                    sc2, txt2, fu2 = _fetch_one(bm_url)
                    if sc2 == 200 and txt2 and not is_anti_bot_blocked("", txt2):
                        status_code, text, final_url = sc2, txt2, fu2
                        if isinstance(meta_out, dict):
                            meta_out["final_url"] = final_url
                except Exception:
                    pass

        if status_code == 200 and text:
            if is_anti_bot_blocked("", text):
                if isinstance(meta_out, dict):
                    meta_out["was_blocked"] = True
                print("本地直接 HTTP 请求被反爬拦截 (Cloudflare / Access Denied / Radware)。")
                if record_negative_cache:
                    record_negative_cache("direct_http", url=url, doi=doi, status_code=200, html_snippet=text, config=config)
                return []

            base_u = final_url or fetch_url
            dfs = parse_tables_from_html(text, table_idx)
            if not dfs:
                def _fetch_ok_html(su):
                    try:
                        sc, body, _fu = _fetch_one(su)
                        return body if sc == 200 else ""
                    except Exception:
                        return ""
                dfs = _follow_table_subpages(base_u, text, _fetch_ok_html, table_idx, cancel_event=cancel_event)

            # Fast in-memory check for strong supplementary links (Springer MOESM, MDPI /s1, etc.)
            # Tier-1 DirectHTTP finishes quickly with ample budget left, so allow up to 12s wall-clock for multi-ESM papers.
            dfs = _append_builtin_supp_if_any(
                dfs,
                base_u,
                text,
                table_idx=table_idx,
                cancel_event=cancel_event,
                max_timeout=12,
                main_present_cap=12.0,
            )
            if dfs:
                if clear_negative_cache:
                    clear_negative_cache("direct_http", url=url, doi=doi, config=config)
                print(f"本地直接 HTTP 提取成功，解析到 {len(dfs)} 个表格。")
                return dfs
            else:
                print("本地直接 HTTP 提取未解析到任何表格。")
        else:
            print(f"本地直接 HTTP 请求返回状态码: {status_code}")
            if record_negative_cache and status_code in (403, 429, 503):
                record_negative_cache("direct_http", url=url, doi=doi, status_code=status_code, html_snippet=text or "", config=config)
    except Exception as e:
        print(f"本地直接 HTTP 请求发生异常: {e}")
    return []


# ---------------------------------------------------------------------------
# Firecrawl（云端 API）共享状态：进程内 memo、磁盘缓存、401/402/403 熔断
# ---------------------------------------------------------------------------

_FIRECRAWL_MEMO = {}
_FIRECRAWL_MEMO_LOCK = threading.Lock()
_FIRECRAWL_DISABLED = False  # 401/402/403（认证失败/封禁/额度耗尽）时置 True，本次进程内禁用该渠道
_OA_CHECK_CACHE = {}
_OA_CHECK_LOCK = threading.Lock()
_KNOWN_OA_DOMAINS = (
    "arxiv.org",
    "ncbi.nlm.nih.gov/pmc",
    "pmc.ncbi.nlm.nih.gov",
    "europepmc.org",
    "biorxiv.org",
    "medrxiv.org",
    "zenodo.org",
    "researchsquare.com",
    "mdpi.com",
    "frontiersin.org",
    "copernicus.org",
    "plos.org",
    "hindawi.com",
    "peerj.com",
    "elifesciences.org",
    "f1000research.com",
    "scielo.",
)


def _looks_like_pdf_url(url: str) -> bool:
    """Return True if the URL path clearly points to a direct PDF binary/endpoint."""
    if not url:
        return False
    low = str(url).strip().lower()
    path = urllib.parse.urlsplit(low).path or low
    return bool(re.search(r"(?:\.pdf|/e?pdf)(?:$)", path))


def _extract_doi_from_url(url: str) -> str:
    """Best-effort DOI extraction from publisher or resolver URLs."""
    if not url:
        return ""
    decoded = urllib.parse.unquote(str(url))
    m = re.search(r"(?<![0-9A-Za-z])(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", decoded)
    if not m:
        return ""
    doi = m.group(1).rstrip(".,;:/")
    doi = re.sub(r"/suppl_file/.*$", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"/xml(?:/[A-Za-z0-9_-]+)?$", "", doi, flags=re.IGNORECASE)
    _suffixes = (
        "/fulltext.html",
        "/full",
        "/abstract",
        "/pdf",
        "/epdf",
        "/meta",
        "/html",
        "/references",
        "/metrics",
        ".pdf",
        ".html",
        ".htm",
    )
    while True:
        prev = doi
        for suffix in _suffixes:
            if doi.lower().endswith(suffix):
                doi = doi[: -len(suffix)]
        doi = re.sub(r"/(?:tables?|figures?)/\d+$", "", doi, flags=re.IGNORECASE)
        doi = doi.rstrip(".,;:/")
        if doi == prev:
            break
    return doi


def is_open_access_paper(url=None, doi=None, cancel_event=None) -> bool:
    """Determine whether a paper/URL is verified Open Access before invoking Firecrawl.

    Fail-closed by design: only verified OA domains or Unpaywall/OpenAlex ``is_oa=True``
    records pass so paid Firecrawl credits and wait time are never spent on paywalled
    abstract pages.
    """
    if cancel_event is not None and cancel_event.is_set():
        return False

    low_url = str(url or "").lower()
    if low_url and any(dom in low_url for dom in _KNOWN_OA_DOMAINS):
        return True

    clean_doi = (doi or _extract_doi_from_url(url) or "").strip().lower()
    if not clean_doi:
        return False

    with _OA_CHECK_LOCK:
        if clean_doi in _OA_CHECK_CACHE:
            return bool(_OA_CHECK_CACHE[clean_doi])

    is_oa = False
    try:
        config = load_config()
        polite_email = get_polite_email(config, service="unpaywall")
        oa_api = f"https://api.unpaywall.org/v2/{urllib.parse.quote(clean_doi, safe='')}?email={urllib.parse.quote(polite_email)}"
        resp = requests.get(
            oa_api,
            headers={"User-Agent": f"ZoteroTableExtractor/1.0 (mailto:{polite_email})"},
            timeout=6,
        )
        if resp.status_code == 200:
            data = resp.json() or {}
            is_oa = bool(data.get("is_oa"))
        else:
            oa_oa = requests.get(
                f"https://api.openalex.org/works/https://doi.org/{urllib.parse.quote(clean_doi, safe='')}?select=open_access",
                headers={"User-Agent": f"ZoteroTableExtractor/1.0 (mailto:{polite_email})"},
                timeout=6,
            )
            if oa_oa.status_code == 200:
                is_oa = bool(((oa_oa.json() or {}).get("open_access") or {}).get("is_oa"))
    except Exception:
        is_oa = False

    with _OA_CHECK_LOCK:
        _OA_CHECK_CACHE[clean_doi] = is_oa
    return is_oa


def is_firecrawl_disabled() -> bool:
    """Thread-safe check for whether Firecrawl API has been circuit-broken."""
    with _FIRECRAWL_MEMO_LOCK:
        return bool(_FIRECRAWL_DISABLED)


def disable_firecrawl(reason: str = "") -> None:
    """Trip the process-wide Firecrawl circuit-breaker (shared across all extractors)."""
    global _FIRECRAWL_DISABLED
    with _FIRECRAWL_MEMO_LOCK:
        already = _FIRECRAWL_DISABLED
        _FIRECRAWL_DISABLED = True
    if not already and reason:
        print(f"[Firecrawl] {reason}，本次运行内禁用 Firecrawl 渠道。")


def _select_tables(dfs, table_idx):
    """按 table_idx 选择表格；索引越界返回 None（让级联解析尝试下一格式）。"""
    if not dfs:
        return None
    if table_idx == "all":
        return dfs
    if table_idx == "first":
        return [dfs[0]]
    try:
        idx = int(table_idx)
    except (ValueError, TypeError):
        return dfs
    return [dfs[idx]] if 0 <= idx < len(dfs) else None


def _firecrawl_cache_file(url):
    config = load_config()
    cache_dir = os.path.expanduser(config.get("FIRECRAWL_CACHE_DIR") or "~/.zotero_firecrawl_cache")
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        return None
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, f"{digest}.json")


def _firecrawl_read_cache(url):
    """读取 Firecrawl 响应的本地磁盘缓存（默认 24h TTL），未命中/过期返回 None。"""
    path = _firecrawl_cache_file(url)
    if not path or not os.path.exists(path):
        return None
    try:
        config = load_config()
        ttl = int(config.get("FIRECRAWL_CACHE_TTL_S") or 86400)
        if ttl > 0 and time.time() - os.path.getmtime(path) > ttl:
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and (data.get("markdown") or data.get("html") or data.get("json")):
            print(f"[Firecrawl] 命中本地缓存: {url}")
            return data
    except Exception:
        return None
    return None


def _firecrawl_write_cache(url, data):
    path = _firecrawl_cache_file(url)
    if not path or not isinstance(data, dict):
        return
    try:
        slim = {k: data.get(k) for k in ("json", "html", "markdown") if data.get(k)}
        if slim:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(slim, f, ensure_ascii=False)
    except Exception:
        pass


def _is_firecrawl_noise_table(df) -> bool:
    """Filter bibliographic, author-roster, attachment, or LLM prose-summary tables."""
    if is_metadata_table(df):
        return True
    cols_low = {str(c).strip().lower() for c in df.columns}
    if cols_low in ({"field", "details"}, {"finding", "description"}, {"zoom option"}):
        return True
    return False


def _firecrawl_tables_to_dfs(tables):
    """Firecrawl JSON 结构化抽取结果 -> DataFrame 列表（过滤元信息与伪表）。"""
    dfs = []
    for idx, t in enumerate(tables or []):
        if not isinstance(t, dict):
            continue
        headers_list = t.get("headers")
        rows = t.get("rows")
        if not headers_list or not isinstance(rows, list):
            continue
        cleaned_rows = []
        for r in rows:
            if isinstance(r, list):
                cleaned_rows.append([clean_text(str(cell)) for cell in r])
        if not cleaned_rows:
            continue
        cleaned_headers = [clean_text(str(h)) for h in headers_list]
        try:
            df = pd.DataFrame(cleaned_rows, columns=cleaned_headers)
        except ValueError:
            # 列数不匹配（LLM 抽取错位），放弃该表，级联将尝试 HTML/Markdown 格式
            continue
        # 先检测元信息/假表，防止伪造的 Table 1 标签绕过 is_metadata_table() 检查
        if _is_firecrawl_noise_table(df):
            continue
        df.attrs['label'] = clean_text(t.get("table_label") or f"Table {idx+1}")
        df.attrs['table_title'] = clean_text(t.get("table_title") or t.get("table_label") or "")
        dfs.append(df)
    return dfs


def _firecrawl_parse_response(data, table_idx):
    """
    从 Firecrawl 单次抓取的响应数据（含 html/markdown/json 多格式）中级联解析表格。
    始终优先使用原始 HTML -> Markdown 规则解析（保留完整多表与合并表头，杜绝 LLM
    对摘要页造假表或截断多表），仅在前两项无结果时兜底已过滤的 JSON 结构化表。
    """
    if not isinstance(data, dict):
        return []

    html = data.get("html") or ""
    md = data.get("markdown") or ""

    json_tables = []
    j_val = data.get("json")
    if isinstance(j_val, dict):
        json_tables = j_val.get("tables") or []
    if not json_tables:
        e_val = data.get("extract")
        if isinstance(e_val, dict):
            json_tables = e_val.get("tables") or []
    if not json_tables and isinstance(data.get("tables"), list):
        json_tables = data["tables"]

    html_l = html.lower()
    order = ("html", "markdown", "json")

    last_dfs = []
    for kind in order:
        dfs = []
        if kind == "json":
            # 若原网页连 <table> 标签都不存在，LLM JSON 极易在摘要页凭空总结假表，直接跳过
            if json_tables and "<table" in html_l:
                dfs = _firecrawl_tables_to_dfs(json_tables)
        elif kind == "html":
            if "<table" in html_l:
                dfs = [d for d in parse_tables_from_html(html, "all") if not _is_firecrawl_noise_table(d)]
        else:
            if md:
                if "<table" in md.lower():
                    dfs = [d for d in parse_tables_from_html(md, "all") if not _is_firecrawl_noise_table(d)]
                if not dfs:
                    dfs = [d for d in parse_tables_from_markdown(md, "all") if not _is_firecrawl_noise_table(d)]
        if dfs:
            last_dfs = dfs
        selected = _select_tables(dfs, table_idx)
        if selected:
            print(f"[Firecrawl] 单次抓取 {kind} 格式解析成功，提取到 {len(selected)} 个表格。")
            return selected
    if last_dfs:
        print(f"[Firecrawl] 请求索引 {table_idx} 越界，兜底返回全部 {len(last_dfs)} 个表格。")
    return last_dfs


def extract_tables_via_firecrawl(url, api_key=None, table_idx="all", cancel_event=None, is_oa=None, doi=None):
    """
    Firecrawl Scrape API v2 —— OA 门控 + 轻量多格式（html + markdown）抓取。

    优化要点：
    - 严格先验判断 OA（开放获取才进入），避免在闭源订阅摘要页浪费等待时间与额度；
    - 默认仅请求 html + markdown（约 1 credit/次），避免 LLM JSON 抽取在无表页面造假表及额外扣费；
    - 本地磁盘缓存（默认 24h TTL）+ 进程内 memo；
    - 401/402/403 全局熔断 + 429 退避重试 + cancel_event 响应。
    """
    if is_firecrawl_disabled():
        print("[Firecrawl] 已熔断（认证/额度受限），跳过。")
        return []
    if cancel_event is not None and cancel_event.is_set():
        return []
    if _looks_like_pdf_url(url):
        print(f"[Firecrawl] 目标链接指向 PDF 文件，跳过网页抓取: {url}")
        return []
    if is_oa is None:
        is_oa = is_open_access_paper(url=url, doi=doi, cancel_event=cancel_event)
    if not is_oa:
        print(f"[Firecrawl] 非开放获取文献（OA 检查未通过），跳过云端抓取，节省时间与额度: {url}")
        return []

    memo_key = (url, str(table_idx))
    with _FIRECRAWL_MEMO_LOCK:
        if memo_key in _FIRECRAWL_MEMO:
            return _FIRECRAWL_MEMO[memo_key]

    def _finish(dfs):
        with _FIRECRAWL_MEMO_LOCK:
            _FIRECRAWL_MEMO[memo_key] = dfs
        return dfs

    config = load_config()
    key = api_key or config.get("FIRECRAWL_API_KEY") or os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        return _finish([])

    def _resolve_fc_base_url(data_dict, fallback_url):
        """Prefer Firecrawl's final redirected publisher URL over dx.doi.org/doi.org for relative supp links."""
        meta = (data_dict.get("metadata") or {}) if isinstance(data_dict, dict) else {}
        if isinstance(meta, dict):
            for k in ("url", "ogUrl", "sourceURL"):
                cand = str(meta.get(k) or "").strip()
                if cand.startswith(("http://", "https://")):
                    host = (urllib.parse.urlparse(cand).hostname or "").lower()
                    if host not in ("doi.org", "dx.doi.org", "hdl.handle.net"):
                        return cand
        return fallback_url

    # 1) 本地磁盘缓存（24h TTL）
    cached = _firecrawl_read_cache(url)
    if cached is not None:
        fc_dfs = _firecrawl_parse_response(cached, table_idx)
        fc_html = (cached.get("html") or "") if isinstance(cached, dict) else ""
        if fc_html:
            fc_base = _resolve_fc_base_url(cached, url)
            fc_dfs = _append_builtin_supp_if_any(fc_dfs, fc_base, fc_html, table_idx=table_idx, cancel_event=cancel_event, max_timeout=12)
        return _finish(fc_dfs)

    formats = ["html", "markdown"]
    if bool(config.get("FIRECRAWL_ENABLE_JSON_EXTRACT")):
        table_schema = {
            "type": "object",
            "properties": {
                "tables": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "table_label": {"type": "string", "description": "e.g., Table 1, Table 2, 表 1"},
                            "table_title": {"type": "string", "description": "Full caption/title of the table"},
                            "headers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Column names of the table"
                            },
                            "rows": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                },
                                "description": "Rows of data. Each row must be an array of strings representing the cells corresponding to headers."
                            }
                        },
                        "required": ["table_label", "headers", "rows"]
                    }
                }
            },
            "required": ["tables"]
        }
        formats = [{"type": "json", "schema": table_schema}, "html", "markdown"]

    payload = {
        "url": url,
        "formats": formats,
        "onlyMainContent": True,
    }
    try:
        max_age = int(config.get("FIRECRAWL_MAX_AGE_MS") or 0)
    except (TypeError, ValueError):
        max_age = 0
    if max_age > 0:
        payload["maxAge"] = max_age
    try:
        page_timeout = int(config.get("FIRECRAWL_PAGE_TIMEOUT_MS") or 0)
    except (TypeError, ValueError):
        page_timeout = 0
    if page_timeout > 0:
        payload["timeout"] = page_timeout

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }

    # 2) 单次云端抓取（429 退避重试一次；402 熔断）
    for attempt in range(2):
        if cancel_event is not None and cancel_event.is_set():
            return []
        try:
            print(f"[Firecrawl] Scrape API v2 单次多格式抓取: {url}" + (" (429 重试)" if attempt else ""))
            response = requests.post(
                "https://api.firecrawl.dev/v2/scrape",
                json=payload, headers=headers, timeout=(10, 90),
            )
        except Exception as e:
            print(f"[Firecrawl] 请求异常: {e}")
            break

        if response.status_code == 200:
            try:
                payload_data = response.json().get("data", {}) or {}
            except Exception:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            if payload_data:
                _firecrawl_write_cache(url, payload_data)
            fc_dfs = _firecrawl_parse_response(payload_data, table_idx)
            fc_html = (payload_data.get("html") or "") if isinstance(payload_data, dict) else ""
            if fc_html:
                fc_base = _resolve_fc_base_url(payload_data, url)
                fc_dfs = _append_builtin_supp_if_any(fc_dfs, fc_base, fc_html, table_idx=table_idx, cancel_event=cancel_event, max_timeout=12)
            return _finish(fc_dfs)

        if response.status_code in (401, 402, 403):
            err_msg = ""
            try:
                err_msg = str((response.json() or {}).get("error") or "")[:120]
            except Exception:
                err_msg = (response.text or "")[:120]
            reason_tag = "云端额度耗尽（402）" if response.status_code == 402 else f"认证失败或账号受限（{response.status_code}{': ' + err_msg if err_msg else ''}）"
            disable_firecrawl(reason_tag)
            break

        if response.status_code == 429 and attempt == 0:
            retry_after = response.headers.get("Retry-After")
            try:
                wait_s = min(float(retry_after), 30.0) if retry_after else 5.0
            except (TypeError, ValueError):
                wait_s = 5.0
            print(f"[Firecrawl] 触发限流（429），{wait_s:.0f}s 后重试一次...")
            if cancel_event is not None:
                if cancel_event.wait(wait_s):
                    return []
            else:
                time.sleep(wait_s)
            continue

        print(f"[Firecrawl] 请求失败，状态码: {response.status_code}")
        break

    return _finish([])


def _normalize_cookies_for_scrapling(cookies, default_url=None):
    """规范化 Cookie 列表使其满足 Playwright / Scrapling 的结构要求。"""
    if not cookies:
        return []
    normalized = []
    for item in cookies:
        if not isinstance(item, dict) or not item.get("name") or item.get("value") is None:
            continue
        c = dict(item)
        c["name"] = str(c["name"])
        c["value"] = str(c["value"])
        if not c.get("domain") and not c.get("url"):
            if default_url:
                host = urllib.parse.urlsplit(default_url).hostname
                if host:
                    c["domain"] = host
                    c["path"] = c.get("path") or "/"
                else:
                    c["url"] = default_url
            else:
                continue
        if c.get("url"):
            c.pop("domain", None)
            c.pop("path", None)
        elif not c.get("path"):
            c["path"] = "/"
        ss = c.get("sameSite")
        if isinstance(ss, str):
            ss_low = ss.strip().lower()
            if ss_low == "strict":
                c["sameSite"] = "Strict"
            elif ss_low == "lax":
                c["sameSite"] = "Lax"
            elif ss_low in ("none", "no_restriction"):
                c["sameSite"] = "None"
            else:
                c.pop("sameSite", None)
        elif "sameSite" in c:
            c.pop("sameSite", None)
        normalized.append(c)
    return normalized


def _scrapling_bounded_session_fetch(url, filtered_kwargs):
    """
    借鉴 journal-supp-downloader 的 stealthy_fetch_bounded 机制：
    通过 StealthySession 执行抓取并注入 page_setup 超时夹紧（规避 solve_cloudflare=True
    将超时强制拉回 60s），拦截第三方卡死域，并在导航竞态异常时自动重试一次。
    """
    import time as _time
    from scrapling.engines._browsers._types import StealthSession
    from scrapling.engines.toolbelt.custom import Response as _Resp  # noqa: F401
    from scrapling.fetchers import StealthySession

    try:
        from .cdp_detector import merge_blocked_domains
    except ImportError:
        try:
            from cdp_detector import merge_blocked_domains
        except ImportError:
            merge_blocked_domains = None

    ann = getattr(StealthSession, "__annotations__", {}) or {}
    req_ms = max(1000, int(filtered_kwargs.get("timeout") or 35000))
    deadline_t = _time.monotonic() + (req_ms / 1000.0)

    session_kwargs = {k: v for k, v in filtered_kwargs.items() if not ann or k in ann}
    session_kwargs.setdefault("retries", 1)
    session_kwargs.setdefault("block_ads", True)
    if merge_blocked_domains and "blocked_domains" in ann:
        proxy_val = str(session_kwargs.get("proxy") or "")
        session_kwargs["blocked_domains"] = merge_blocked_domains(
            url, session_kwargs.get("blocked_domains"), proxy=proxy_val
        )

    orig_setup = session_kwargs.pop("page_setup", None)
    orig_action = session_kwargs.pop("page_action", None)
    wait_ms = session_kwargs.pop("wait", None)
    session_kwargs.pop("timeout", None)

    def _wrapped_setup(page):
        def _clamp_post_load(_p=None):
            rem_ms = max(1000, int((deadline_t - _time.monotonic()) * 1000))
            capped_ms = min(req_ms, rem_ms)
            try:
                page.set_default_timeout(capped_ms)
            except Exception:
                pass
            try:
                page.set_default_navigation_timeout(capped_ms)
            except Exception:
                pass

        _clamp_post_load()
        orig_wfls = getattr(page, "wait_for_load_state", None)
        if callable(orig_wfls):
            def _bounded_wait_for_load_state(state="load", *w_args, **w_kwargs):
                if state in ("networkidle", "load", "domcontentloaded"):
                    rem_ms = int((deadline_t - _time.monotonic()) * 1000)
                    if rem_ms <= 0:
                        return None
                    max_wait = 4000 if state == "networkidle" else 8000
                    cap_ms = min(max_wait, max(500, rem_ms))
                    if "timeout" in w_kwargs and w_kwargs["timeout"] is not None:
                        w_kwargs["timeout"] = min(int(w_kwargs["timeout"]), cap_ms)
                    elif w_args:
                        w_args = (min(int(w_args[0]), cap_ms),) + w_args[1:]
                    else:
                        w_kwargs["timeout"] = cap_ms
                    try:
                        return orig_wfls(state, *w_args, **w_kwargs)
                    except Exception:
                        if state in ("load", "domcontentloaded"):
                            return None
                        raise
                return orig_wfls(state, *w_args, **w_kwargs)

            try:
                page.wait_for_load_state = _bounded_wait_for_load_state
            except Exception:
                pass

        try:
            page.on("domcontentloaded", _clamp_post_load)
            page.on("load", _clamp_post_load)
        except Exception:
            pass
        if orig_setup is not None:
            orig_setup(page)

    def _wrapped_action(page):
        if _time.monotonic() >= deadline_t:
            return
        rem_ms = max(1000, int((deadline_t - _time.monotonic()) * 1000))
        capped_ms = min(req_ms, rem_ms)
        try:
            page.set_default_timeout(capped_ms)
        except Exception:
            pass
        try:
            page.set_default_navigation_timeout(capped_ms)
        except Exception:
            pass
        if orig_action is not None:
            return orig_action(page)

    fetch_kwargs = {"timeout": req_ms}
    if "page_setup" in ann:
        fetch_kwargs["page_setup"] = _wrapped_setup
    if orig_action is not None:
        fetch_kwargs["page_action"] = _wrapped_action
    if wait_ms is not None:
        fetch_kwargs["wait"] = min(int(wait_ms), max(0, req_ms // 3))

    with StealthySession(**session_kwargs) as engine:
        try:
            return engine.fetch(url, **fetch_kwargs)
        except Exception as exc:
            err_l = str(exc).lower()
            is_nav_race = (
                "navigating and changing the content" in err_l
                or "execution context was destroyed" in err_l
            )
            rem_s = deadline_t - _time.monotonic()
            if not is_nav_race or rem_s <= 4.0:
                raise
            retry_kwargs = dict(fetch_kwargs)
            retry_kwargs["timeout"] = max(1000, min(req_ms, int(rem_s * 1000)))
            return engine.fetch(url, **retry_kwargs)


def _scrapling_fetch_html(url, extra_kwargs=None):
    """Fetch rendered HTML using Scrapling StealthyFetcher / bounded StealthySession."""
    import inspect
    if hasattr(common, "ensure_macos_corefoundation"):
        common.ensure_macos_corefoundation()
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        from scrapling import StealthyFetcher
    fetch_kwargs = {
        "headless": True,
        "network_idle": False,
        "solve_cloudflare": True,
        "hide_canvas": True,
        "block_webrtc": True,
        "wait": 1200,
        "timeout": 45000,
    }
    if extra_kwargs:
        fetch_kwargs.update(extra_kwargs)

    # Filter kwargs by concrete signature or StealthSession schema when inspectable
    allowed_keys = None
    try:
        sig = inspect.signature(StealthyFetcher.fetch)
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if not has_varkw:
            allowed_keys = set(sig.parameters.keys())
        else:
            try:
                from scrapling.engines._browsers._types import StealthSession
                ann = getattr(StealthSession, "__annotations__", None)
                if isinstance(ann, dict) and ann:
                    allowed_keys = set(ann.keys())
            except Exception:
                allowed_keys = None
    except Exception:
        allowed_keys = None

    filtered_kwargs = (
        {k: v for k, v in fetch_kwargs.items() if k in allowed_keys or (extra_kwargs and k in extra_kwargs)}
        if allowed_keys is not None else dict(fetch_kwargs)
    )

    # 真实 Scrapling 环境下优先走有界 StealthySession（支持 blocked_domains 与超时夹紧）
    is_real_stealth = (
        getattr(StealthyFetcher, "__module__", "") == "scrapling.fetchers.stealth_chrome"
        and getattr(getattr(StealthyFetcher, "fetch", None), "__module__", "") == "scrapling.engines.toolbelt.custom"
        or "scrapling" in getattr(StealthyFetcher, "__module__", "")
    )
    if is_real_stealth and os.environ.get("ZTE_DISABLE_SCRAPLING_BOUNDED") != "1":
        try:
            res = _scrapling_bounded_session_fetch(url, filtered_kwargs)
        except Exception as bounded_exc:
            # 仅在接口/签名不兼容时回退到 StealthyFetcher.fetch；
            # 真实浏览器超时或网络错误不再重复拉起第二次无界浏览器，避免拖垮 65s 预算。
            if isinstance(bounded_exc, (ImportError, AttributeError, TypeError)):
                res = None
            else:
                print(f"[Scrapling] 有界会话抓取未完成，跳过重复无界重试: {bounded_exc}")
                return ""
        if res is not None:
            html = getattr(res, "html_content", None) or getattr(res, "text", None)
            if html:
                return html
            return ""

    def _call_with_graceful_strip(fn, target_url, kwargs_dict):
        cur = dict(kwargs_dict)
        strip_order = ["real_chrome", "blocked_domains", "cookies", "hide_canvas", "block_webrtc", "solve_cloudflare", "wait", "timeout", "network_idle", "headless"]
        while True:
            try:
                return fn(target_url, **cur)
            except TypeError:
                removed = False
                for opt_k in strip_order:
                    if opt_k in cur:
                        cur.pop(opt_k)
                        removed = True
                        break
                if not removed:
                    raise

    try:
        res = _call_with_graceful_strip(StealthyFetcher.fetch, url, filtered_kwargs)
    except TypeError:
        try:
            res = StealthyFetcher.fetch(url, **(extra_kwargs or {}))
        except TypeError:
            fetcher = StealthyFetcher()
            res = _call_with_graceful_strip(fetcher.fetch, url, filtered_kwargs)
    if res is None:
        return ""
    html = getattr(res, "html_content", None) or getattr(res, "text", None)
    if not html:
        body = getattr(res, "body", None)
        if isinstance(body, bytes):
            html = body.decode("utf-8", errors="replace")
        elif isinstance(body, str):
            html = body
    return html or ""


def _scrapling_page_action(page):
    """Wait out Akamai/Elsevier/Turnstile shells, accept cookie banners, and expand lazy tables/accordions."""
    try:
        from .cdp_detector import wait_through_transient_challenge
    except ImportError:
        try:
            from cdp_detector import wait_through_transient_challenge
        except ImportError:
            wait_through_transient_challenge = None

    try:
        has_bm = page.evaluate("""() => {
            const m = document.querySelector('meta[http-equiv="refresh" i]');
            return Boolean(m && (m.getAttribute('content') || '').includes('bm-verify'));
        }""")
        if has_bm:
            try:
                page.wait_for_url(lambda u: "bm-verify=" in u, timeout=9000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
            except Exception:
                page.wait_for_timeout(6500)
    except Exception:
        pass

    if wait_through_transient_challenge:
        wait_through_transient_challenge(page, max_wait_s=6.0)

    for sel in (
        "#onetrust-accept-btn-handler",
        "button[data-cc-action='accept']",
        "button[data-test='accept-cookie']",
        ".c-button--primary[data-test='accept-cookie']",
        "button:has-text('Accept all cookies')",
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=400):
                btn.click()
                page.wait_for_timeout(400)
                break
        except Exception:
            pass
    try:
        clicked = page.evaluate("""
            () => {
                let n = 0;
                document.querySelectorAll("details:not([open])").forEach(d => {
                    d.open = true;
                    n++;
                });
                document.querySelectorAll('.link-reveal.link-table, .tableDownloadOption a, a.showTable').forEach(el => {
                    const txt = (el.innerText || '').toLowerCase();
                    if (el.matches('.link-reveal.link-table, a.showTable') || txt.includes('display table')) {
                        try { el.click(); n++; } catch(e) {}
                    }
                });
                const accSelectors = [
                    "button[aria-expanded='false']",
                    "a[aria-expanded='false']",
                    "[role='button'][aria-expanded='false']",
                    ".accordion-button.collapsed",
                    ".accordion-button[aria-expanded='false']"
                ].join(", ");
                const kw = ["table", "supp", "appendi", "extended", "additional", "data", "material", "esm", "附", "补充", "表"];
                document.querySelectorAll(accSelectors).forEach(el => {
                    const txt = (el.innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("title") || "").toLowerCase();
                    if (/delete|remove|edit|upload|publish|purchase|删除|移除|编辑|上传|发布|购买/i.test(txt)) return;
                    if (kw.some(k => txt.includes(k))) {
                        try { el.click(); n++; } catch(e) {}
                    }
                });
                return n;
            }
        """)
        if clicked:
            page.wait_for_timeout(1200)
    except Exception:
        pass


def _extract_tf_csvs_from_scrapling_html(base_url, html_text, headless=False):
    """When T&F HTML has downloadTable links, fetch them in a single authenticated StealthyFetcher session."""
    dl_matches = re.findall(r'href=["\']([^"\']*?/action/downloadTable\?[^"\']+)["\']', html_text or "", re.IGNORECASE)
    if not dl_matches:
        return []
    seen = []
    for raw in dl_matches:
        u = urllib.parse.urljoin(base_url, raw.replace("&amp;", "&"))
        if u not in seen:
            seen.append(u)
    collected = []
    def _fetch_all(page):
        _scrapling_page_action(page)
        for idx, dl_u in enumerate(seen, 1):
            try:
                resp = page.request.get(dl_u, timeout=15000)
                if resp.status == 200:
                    txt = resp.text()
                    if txt and not txt.lstrip().startswith(("<!DOCTYPE", "<html")) and not is_anti_bot_blocked("", txt):
                        collected.append((idx, txt))
            except Exception:
                pass
    _scrapling_fetch_html(base_url, {"headless": headless, "solve_cloudflare": True, "network_idle": False, "wait": 2000, "page_action": _fetch_all})
    dfs = []
    for idx, csv_txt in collected:
        try:
            df = pd.read_csv(io.StringIO(csv_txt), dtype=str, keep_default_na=False)
            if not df.empty:
                df.columns = [clean_text(c) for c in df.columns]
                for ci in range(len(df.columns)):
                    df.iloc[:, ci] = df.iloc[:, ci].astype(str).apply(clean_text)
                df.attrs["table_title"] = f"Table {idx}"
                dfs.append(df)
        except Exception:
            pass
    return dfs


def _collect_domain_cookies_for_scrapling(url):
    """收集并归一化与目标域匹配的共享 Cookie，供 Scrapling 无头/有头浏览器注入。"""
    paths = []
    primary = get_crawl4ai_cookies_path()
    if primary and os.path.exists(primary):
        paths.append(primary)
    get_companion_cp = getattr(common, "get_companion_cookie_paths", None)
    if callable(get_companion_cp):
        for cp in get_companion_cp():
            if cp not in paths:
                paths.append(cp)
    raw = []
    for cp in paths:
        try:
            raw.extend(load_cookies_from_file(cp) or [])
        except Exception:
            pass
    if not raw:
        return []
    matched = _cookies_for_domain(raw, url)
    return _normalize_cookies_for_scrapling(matched, default_url=url)


def extract_tables_via_scrapling(url, table_idx="all", force_headed=False, cancel_event=None):
    """
    Invokes Scrapling StealthyFetcher, auto-retrying in headed mode (real Chrome when installed)
    when headless is blocked by Cloudflare Turnstile, Radware Bot Manager, or Access Denied,
    and optionally escalating to Tencent BrowserSkill if installed and ready.
    """
    if cancel_event is not None and cancel_event.is_set():
        return []
    try:
        from .negative_cache import is_negatively_cached, record_negative_cache, clear_negative_cache
    except Exception:
        is_negatively_cached = None
        record_negative_cache = None
        clear_negative_cache = None

    _scr_cfg = load_config() if callable(load_config) else {}
    if is_negatively_cached and not force_headed:
        hit = is_negatively_cached("scrapling", url=url, config=_scr_cfg)
        if hit:
            print(f"[Scrapling] 命中跨进程负缓存 ({hit.get('target_key')}, {hit.get('reason')})，跳过抓取。")
            return []

    try:
        from .cdp_detector import has_system_chrome, try_fetch_html_via_browserskill
    except ImportError:
        try:
            from cdp_detector import has_system_chrome, try_fetch_html_via_browserskill
        except ImportError:
            has_system_chrome = lambda: False
            try_fetch_html_via_browserskill = None

    try:
        print(f"Querying Scrapling StealthyFetcher for {url} (force_headed={force_headed})...")
        used_headless = not force_headed
        domain_cookies = _collect_domain_cookies_for_scrapling(url)
        browser_supp_dfs = []
        browser_supp_attempted = [False]

        def _scrapling_action_with_supp(page):
            _scrapling_page_action(page)
            if str(table_idx).lower() == "all" and not (cancel_event is not None and cancel_event.is_set()):
                try:
                    cur_u = getattr(page, "url", None) or url
                    cur_html = page.content() or ""
                    if cur_html and not is_anti_bot_blocked("", cur_html):
                        req_api = getattr(getattr(page, "context", None), "request", None) or getattr(page, "request", None)
                        if req_api is not None:
                            browser_supp_attempted[0] = True
                            got_supp = _download_supp_via_browser_context(
                                req_api,
                                cur_u,
                                cur_html,
                                cancel_event=cancel_event,
                                max_files=4,
                                max_timeout=10.0,
                                strong_only=True,
                            )
                            if got_supp:
                                browser_supp_dfs.extend(got_supp)
                except Exception:
                    pass
            return page

        first_kwargs = {
            "headless": used_headless,
            "solve_cloudflare": True,
            "network_idle": False,
            "timeout": 35000,
            "wait": 2000 if used_headless else 2500,
            "page_action": _scrapling_action_with_supp,
        }
        if not used_headless and has_system_chrome():
            first_kwargs["real_chrome"] = True
        if domain_cookies:
            first_kwargs["cookies"] = domain_cookies

        html = _scrapling_fetch_html(url, first_kwargs)
        if cancel_event is not None and cancel_event.is_set():
            return list(browser_supp_dfs) if browser_supp_dfs else []
        if not force_headed and (not html or is_anti_bot_blocked("", html)):
            print("Scrapling 无头模式受阻，自动切换有头浏览器模式重试...")
            used_headless = False
            browser_supp_dfs.clear()
            browser_supp_attempted[0] = False
            headed_kwargs = {
                "headless": False,
                "solve_cloudflare": True,
                "network_idle": False,
                "timeout": 28000,
                "wait": 2500,
                "page_action": _scrapling_action_with_supp,
            }
            if has_system_chrome():
                headed_kwargs["real_chrome"] = True
            if domain_cookies:
                headed_kwargs["cookies"] = domain_cookies
            html = _scrapling_fetch_html(url, headed_kwargs)
            if cancel_event is not None and cancel_event.is_set():
                return list(browser_supp_dfs) if browser_supp_dfs else []

        if (not html or is_anti_bot_blocked("", html)) and try_fetch_html_via_browserskill:
            bsk_html, _bsk_status, _bsk_final = try_fetch_html_via_browserskill(url, timeout=30.0)
            if bsk_html and not is_anti_bot_blocked("", bsk_html):
                html = bsk_html
            if cancel_event is not None and cancel_event.is_set():
                return []

        if html and is_anti_bot_blocked("", html):
            if record_negative_cache:
                record_negative_cache("scrapling", url=url, status_code=403, html_snippet=html, config=_scr_cfg)
            return []

        if html and not is_anti_bot_blocked("", html):
            if clear_negative_cache:
                clear_negative_cache("scrapling", url=url, config=_scr_cfg)
            dfs = parse_tables_from_html(html, table_idx)
            if not dfs and "tandfonline.com" in url.lower():
                dfs = _extract_tf_csvs_from_scrapling_html(url, html, headless=used_headless)
                if dfs:
                    dfs = _select_tables(dfs, table_idx) or []
            if not dfs:
                dfs = _follow_table_subpages(
                    url,
                    html,
                    lambda su: ("" if (cancel_event is not None and cancel_event.is_set()) else _scrapling_fetch_html(su, {"headless": used_headless, "solve_cloudflare": not used_headless, "network_idle": False})),
                    table_idx,
                    cancel_event=cancel_event,
                )
            if browser_supp_dfs:
                dfs = list(dfs) if dfs else []
                dfs.extend(browser_supp_dfs)
            elif not browser_supp_attempted[0]:
                dfs = _append_builtin_supp_if_any(
                    dfs, url, html, table_idx=table_idx, cancel_event=cancel_event, max_timeout=10
                )
            if dfs:
                return dfs
    except Exception as e:
        print(f"Scrapling query skipped: {e}")
    return []


def extract_tables_via_playwright(url, table_idx="all"):
    """
    Invokes Playwright/Patchright directly, reusing an active CDP browser tab when available,
    preferring Patchright stealth engine for self-launched browsers, blocking hanging third-party trackers,
    and waiting out transient anti-bot shells.
    """
    try:
        try:
            from .cdp_detector import (
                find_active_cdp_endpoint,
                find_matching_cdp_page,
                get_publisher_table_wait_selector,
                install_cdp_port_guard,
                install_slow_tracker_route_blocker,
                wait_through_transient_challenge,
            )
        except ImportError:
            from cdp_detector import (
                find_active_cdp_endpoint,
                find_matching_cdp_page,
                get_publisher_table_wait_selector,
                install_cdp_port_guard,
                install_slow_tracker_route_blocker,
                wait_through_transient_challenge,
            )

        cdp_ep = find_active_cdp_endpoint(timeout=1.0)
        if resolve_sync_playwright:
            _pw_factory, _pw_engine = resolve_sync_playwright(prefer_patchright=not bool(cdp_ep))
        else:
            from playwright.sync_api import sync_playwright as _pw_factory
            _pw_engine = "playwright"

        print(f"Querying Playwright browser ({_pw_engine}) for {url}...")
        use_pool = (
            not cdp_ep
            and get_thread_browser_pool is not None
            and (os.environ.get("ZTE_DISABLE_THREAD_BROWSER_POOL") or "").strip().lower() not in ("1", "true", "yes")
        )
        if use_pool:
            exec_path = find_playwright_chromium()
            pool = get_thread_browser_pool()
            with pool.acquire_context(
                _pw_factory,
                engine_name=_pw_engine,
                headless=True,
                exec_path=exec_path,
                context_kwargs={
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                },
            ) as (_browser, context):
                page = context.new_page()
                install_cdp_port_guard(page)
                install_slow_tracker_route_blocker(page, url)
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                wait_through_transient_challenge(page, max_wait_s=8.0)
                try:
                    page.wait_for_selector(get_publisher_table_wait_selector(url), timeout=6000)
                except Exception:
                    pass
                html = page.content()
                if html:
                    return parse_tables_from_html(html, table_idx)
                return []

        with _pw_factory() as p:
            browser = None
            page = None
            connected_cdp = False
            reused_tab = False
            _self_pid = None

            if cdp_ep:
                try:
                    browser = p.chromium.connect_over_cdp(cdp_ep["cdp_url"], timeout=8000)
                    connected_cdp = True
                    matched = find_matching_cdp_page(browser, url)
                    if matched is not None:
                        page = matched
                        reused_tab = True
                    elif browser.contexts:
                        page = browser.contexts[0].new_page()
                    if page is not None:
                        install_cdp_port_guard(page, cdp_port=cdp_ep.get("port"))
                except Exception:
                    connected_cdp = False
                    page = None

            if page is None:
                exec_path = find_playwright_chromium()
                if exec_path:
                    print(f"[Playwright] 使用自动识别的 Chromium 路径: {exec_path}")
                browser = p.chromium.launch(headless=True, executable_path=exec_path)
                _self_pid = extract_browser_pid(browser=browser, playwright_obj=p)
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                )
                install_cdp_port_guard(page)

            install_slow_tracker_route_blocker(page, url)
            if not reused_tab:
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                wait_through_transient_challenge(page, max_wait_s=8.0)

            try:
                page.wait_for_selector(get_publisher_table_wait_selector(url), timeout=6000)
            except Exception:
                pass
            html = page.content()

            if connected_cdp:
                if not reused_tab and page:
                    try:
                        page.close()
                    except Exception:
                        pass
            elif browser:
                close_browser_safely(browser=browser, proc_pid=_self_pid)

            if html:
                return parse_tables_from_html(html, table_idx)
    except Exception as e:
        print(f"Playwright query skipped: {e}")
    return []


def has_image_based_tables(html):
    """
    Checks if the CNKI HTML page uses image-based tables instead of standard HTML tables.
    """
    import re
    # Look for image tags representing tables, e.g. <img id="ImgTab..." or <img src="...Table..." or class="table-img"
    img_table_patterns = [
        r'<img[^>]+(id|class|name|src)\s*=\s*["\'][^"\']*(table|tab|img-table|table-img)[^"\']*["\']',
        r'<div[^>]+class\s*=\s*["\'][^"\']*(table-img|img-table|image-table)[^"\']*["\'][^>]*>\s*<img',
        r'class\s*=\s*["\']table_image["\']'
    ]
    
    # Also check if there are standard HTML tables
    table_count = len(re.findall(r'<table\b', html, re.IGNORECASE))
    
    # If there are table image patterns and few/no standard tables, it's likely image-based
    for pattern in img_table_patterns:
        if re.search(pattern, html, re.IGNORECASE):
            if table_count <= 2: # CNKI pages often have 1-2 navigation tables
                return True
    return False


def extract_cnki_tables_via_scrapling(url, table_idx="all", cookies_path=None, cancel_event=None):
    """
    Fetches CNKI page using Scrapling StealthyFetcher with optional cookies.
    """
    try:
        if cancel_event is not None and cancel_event.is_set():
            return []
        if "chndoi.org" in (url or "").lower() or "chinadoi.cn" in (url or "").lower():
            try:
                from .doi_resolver import resolve_chinese_doi
            except ImportError:
                from doi_resolver import resolve_chinese_doi
            url = resolve_chinese_doi(url)
        if cancel_event is not None and cancel_event.is_set():
            return []
        print(f"CNKI Scrapling: Loading {url}...")
        extra_kwargs = {
            "solve_cloudflare": False,
            "timeout": 12000,
        }
        if cookies_path and os.path.exists(cookies_path):
            cookies = load_cookies_from_file(cookies_path)
            if cookies:
                try:
                    extra_kwargs["cookies"] = cookies
                    print(f"CNKI Scrapling: Loaded {len(cookies)} cookies from {cookies_path}")
                except Exception as e:
                    print(f"CNKI Scrapling: Failed to apply cookies: {e}")

        html = _scrapling_fetch_html(url, extra_kwargs=extra_kwargs)
        if html:
            if has_image_based_tables(html):
                print("检测到知网网页表格为图片格式，无法直接抓取文本。自动跳转至本地 PDF 识别模式。")
                return []
            dfs = parse_tables_from_html(html, table_idx)
            if dfs:
                return dfs
    except Exception as e:
        print(f"CNKI Scrapling query skipped: {e}")
    return []


def extract_cnki_tables_via_playwright(url, table_idx="all", headed=False, user_data_dir=None, cookies_path=None):
    """
    Renders CNKI page using Playwright Chromium to handle dynamic frames.
    For CNKI URLs, redirects to the automated html reader extractor.
    For non-CNKI URLs, falls back to the general table crawler.
    """
    if "cnki.net" in url or "cnki.com.cn" in url:
        try:
            from . import cnki_html_extractor

            print("[在线提取] 检测到知网链接，使用 cnki_html_extractor 模块进行自动化读取及滑块绕过...")
            return cnki_html_extractor.extract_cnki_html_tables(url, cookies_path, table_idx, headed, user_data_dir)
        except Exception as e:
            print(f"[警告] 知网自动化提取模块报错: {e}，将尝试使用常规 Playwright 模式作为备用。")

    # Call general table crawler for all non-CNKI journals!
    try:
        from . import general_html_extractor
        
        print("[在线提取] 使用 general_html_extractor 模块进行通用网页表格抓取及子页面跟进...")
        return general_html_extractor.extract_general_html_tables(url, table_idx, headed, user_data_dir)
    except Exception as e:
        print(f"[警告] 通用网页表格抓取模块报错: {e}。")
        return []


# ---------------------------------------------------------------------------
# Crawl4AI（本地/桥接浏览器）共享状态：并发信号量、按域名熔断、CDP 探测
# ---------------------------------------------------------------------------

_CRAWL4AI_SEM = None
_CRAWL4AI_SEM_SIZE = None
_CRAWL4AI_DOMAIN_FAILS = {}
_CRAWL4AI_DOMAIN_LOCK = threading.Lock()
_CRAWL4AI_CDP_CACHE = {"checked": False, "url": None}


def _crawl4ai_semaphore():
    """按 config 的 CRAWL4AI_MAX_CONCURRENT 懒建并发信号量（默认 2）。"""
    global _CRAWL4AI_SEM, _CRAWL4AI_SEM_SIZE
    config = load_config()
    try:
        size = max(1, int(config.get("CRAWL4AI_MAX_CONCURRENT") or 2))
    except (TypeError, ValueError):
        size = 2
    if _CRAWL4AI_SEM is None or _CRAWL4AI_SEM_SIZE != size:
        _CRAWL4AI_SEM = threading.Semaphore(size)
        _CRAWL4AI_SEM_SIZE = size
    return _CRAWL4AI_SEM


def _crawl4ai_domain_key(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""


def _crawl4ai_domain_blocked(url):
    """同一域名连续硬失败 >=3 次（如 IP 被封）则熔断该域的 Crawl4AI 渠道。"""
    key = _crawl4ai_domain_key(url)
    with _CRAWL4AI_DOMAIN_LOCK:
        return _CRAWL4AI_DOMAIN_FAILS.get(key, 0) >= 3


def _crawl4ai_record(url, hard_failure):
    """记录抓取结果：硬失败（抓取异常/页面失败）累计熔断；成功则清零。"""
    key = _crawl4ai_domain_key(url)
    with _CRAWL4AI_DOMAIN_LOCK:
        if hard_failure:
            _CRAWL4AI_DOMAIN_FAILS[key] = _CRAWL4AI_DOMAIN_FAILS.get(key, 0) + 1
            if _CRAWL4AI_DOMAIN_FAILS[key] == 3:
                print(f"[Crawl4AI] 域名 {key} 连续失败 3 次，本次运行内熔断该域的 Crawl4AI 渠道。")
        else:
            _CRAWL4AI_DOMAIN_FAILS.pop(key, None)


def _crawl4ai_cdp_url():
    """
    确定 Crawl4AI 是否通过 CDP 接管技能已有的调试 Chrome。
    config CRAWL4AI_CDP_URL / CDP_URL 显式指定优先；为空时自动动态探测 DevToolsActivePort / 9222
    （缓存已检测到的存活端点，若端点失活则自动失效重探）。返回 None 表示自启独立 stealth 浏览器。
    """
    config = load_config()
    explicit = (config.get("CRAWL4AI_CDP_URL") or config.get("CDP_URL") or "").strip()
    if explicit:
        return explicit
    if _CRAWL4AI_CDP_CACHE["checked"]:
        cached_url = _CRAWL4AI_CDP_CACHE["url"]
        if not cached_url:
            return None
        # Verify cached CDP endpoint is still alive before reusing it
        alive = False
        try:
            import urllib.parse
            parsed = urllib.parse.urlparse(cached_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 9222
            from .cdp_detector import check_cdp_endpoint_alive
            alive = bool(check_cdp_endpoint_alive(host=host, port=port, timeout=0.5))
        except Exception:
            try:
                import urllib.request
                with urllib.request.urlopen(cached_url.rstrip("/") + "/json/version", timeout=0.5) as resp:
                    alive = (resp.status == 200)
            except Exception:
                alive = False
        if alive:
            return cached_url
        _CRAWL4AI_CDP_CACHE["checked"] = False
        _CRAWL4AI_CDP_CACHE["url"] = None
    probe = None
    try:
        from .cdp_detector import find_active_cdp_endpoint
        ep = find_active_cdp_endpoint(timeout=0.8, config=config)
        if ep:
            probe = ep["cdp_url"]
            print(f"[Crawl4AI] 检测到活动 CDP 调试 Chrome ({probe}，来源: {ep.get('source')})，将经 CDP 复用该浏览器（免启动、继承登录态）。")
    except Exception:
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=0.8) as resp:
                if resp.status == 200:
                    probe = "http://127.0.0.1:9222"
                    print("[Crawl4AI] 检测到 9222 调试 Chrome，将经 CDP 复用该浏览器（免启动、继承登录态）。")
        except Exception:
            probe = None
    _CRAWL4AI_CDP_CACHE["checked"] = True
    _CRAWL4AI_CDP_CACHE["url"] = probe
    return probe


def _filter_kwargs(cls, kwargs):
    """按目标类的签名/字段过滤 kwargs，兼容不同 crawl4ai 版本的参数差异。"""
    accepted = set()
    try:
        import inspect
        target = cls.__init__ if isinstance(cls, type) else cls
        sig = inspect.signature(target)
        for param in sig.parameters.values():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                return kwargs
            if param.name != "self":
                accepted.add(param.name)
    except Exception:
        pass
    fields = getattr(cls, "model_fields", None) or getattr(cls, "__fields__", None) or {}
    try:
        accepted |= set(fields)
    except TypeError:
        pass
    if not accepted:
        return kwargs
    return {k: v for k, v in kwargs.items() if k in accepted}


def _cookies_for_domain(cookies_list, url):
    """按目标域名过滤 Cookie，避免全量注入造成请求头臃肿与风控。"""
    host = _crawl4ai_domain_key(url)
    if not host:
        return cookies_list
    kept = []
    for c in cookies_list:
        dom = (c.get("domain") or "").lstrip(".").lower()
        if not dom or host == dom or host.endswith("." + dom) or dom.endswith("." + host):
            kept.append(c)
    return kept


def extract_tables_via_crawl4ai(url, table_idx="all", cancel_event=None):
    """
    Crawl4AI 抓取目标 URL 并解析表格。

    相对旧版的优化：
    - 优先经 CDP 接管技能已有的 9222 / 动态端口调试 Chrome（支持动态 DevToolsActivePort 发现）：
      零启动开销、直接继承日常登录态；探测不到则回退自启 stealth 浏览器；
    - 标签页级隔离并发：CDP 模式下免除全局排他进程锁，直接实现多 Tab 隔离并行；
    - page_timeout（CRAWL4AI_PAGE_TIMEOUT_MS）+ wait_for（表格出现或加载 3s 即返回），
      竞速输家不再变僵尸浏览器；
    - Cookie 按目标域名过滤注入（CDP 模式下无需注入）；
    - 进程级并发信号量（CRAWL4AI_MAX_CONCURRENT，默认 2）与按域名连续失败熔断；
    - cancel_event 协作式取消：外层竞速分出胜负后立即中止抓取；
    - result.markdown 对象/字符串版本兼容；事件循环处理改用 asyncio.run 优先。
    """
    import asyncio

    if cancel_event is not None and cancel_event.is_set():
        return []
    if _crawl4ai_domain_blocked(url):
        print(f"[Crawl4AI] 域名 {_crawl4ai_domain_key(url)} 连续失败过多，已熔断跳过。")
        return []
    try:
        from .negative_cache import is_negatively_cached, record_negative_cache, clear_negative_cache
    except Exception:
        is_negatively_cached = None
        record_negative_cache = None
        clear_negative_cache = None
    config = load_config()
    if is_negatively_cached:
        hit = is_negatively_cached("crawl4ai", url=url, config=config)
        if hit:
            print(f"[Crawl4AI] 命中跨进程负缓存 ({hit.get('target_key')}, {hit.get('reason')})，跳过抓取。")
            return []
    try:
        page_timeout_ms = int(config.get("CRAWL4AI_PAGE_TIMEOUT_MS") or 45000)
    except (TypeError, ValueError):
        page_timeout_ms = 45000
    cdp_url = _crawl4ai_cdp_url()

    sem = _crawl4ai_semaphore()
    # Crawl4AI 在非 CDP 模式下使用独立隔离的临时浏览器实例（不读写 PLAYWRIGHT_USER_DATA_DIR），
    # 无需抢占全局跨进程 browser_extraction_lock，避免与 Tier-3 Playwright/CNKI 互相阻塞。
    lock_ctx = None

    sem.acquire()

    try:
        if cancel_event is not None and cancel_event.is_set():
            return []

        # CDP 模式复用真实浏览器（自带登录态），无需注入 Cookie；
        # 自启浏览器模式按目标域名过滤注入已保存的 Cookie（含 journal-supp-downloader 共享 cookie）。
        def _load_domain_cookies():
            cookie_paths = []
            primary_cp = get_crawl4ai_cookies_path()
            if os.path.exists(primary_cp):
                cookie_paths.append(primary_cp)
            get_companion_cp = getattr(common, "get_companion_cookie_paths", None)
            if callable(get_companion_cp):
                for cp in get_companion_cp():
                    if cp not in cookie_paths:
                        cookie_paths.append(cp)

            valid_keys = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
            merged_cookies = []
            for cp in cookie_paths:
                try:
                    raw_cookies = load_cookies_from_file(cp)
                    if not raw_cookies:
                        with open(cp, "r", encoding="utf-8") as f:
                            loaded = json.load(f)
                        if isinstance(loaded, dict) and isinstance(loaded.get("cookies"), list):
                            raw_cookies = loaded["cookies"]
                    if isinstance(raw_cookies, list):
                        for c in raw_cookies:
                            if isinstance(c, dict) and c.get("name"):
                                merged_cookies.append({k: c[k] for k in valid_keys if k in c})
                except Exception as e:
                    print(f"[Crawl4AI Warning] Failed to load cookies from {cp}: {e}")
            if merged_cookies:
                filtered = _cookies_for_domain(merged_cookies, url)
                print(f"[Crawl4AI] Loaded {len(filtered or [])} domain-matched cookies from {len(cookie_paths)} store(s)")
                return filtered
            return None

        async def _crawl(active_cdp_url):
            os.environ.setdefault("CRAWL4AI_NO_CLOUD_NOTICE", "1")
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

            browser_kwargs = {
                "headless": active_cdp_url is None,
                "enable_stealth": active_cdp_url is None,
            }
            if active_cdp_url:
                # 接管技能已有的调试 Chrome，并强制隔离上下文 + 退出清理，避免多并发共享 context.pages[0] 冲突
                browser_kwargs.update({
                    "use_managed_browser": True,
                    "browser_mode": "cdp",  # 新版参数名；旧版本由签名过滤丢弃
                    "cdp_url": active_cdp_url,
                    "create_isolated_context": True,
                    "cdp_cleanup_on_close": True,
                    "cdp_close_delay": 0.0,
                })
            else:
                cookies_list = _load_domain_cookies()
                if cookies_list:
                    browser_kwargs["cookies"] = cookies_list
            browser_config = BrowserConfig(**_filter_kwargs(BrowserConfig, browser_kwargs))

            run_kwargs = {
                "cache_mode": CacheMode.BYPASS,
                "page_timeout": page_timeout_ms,
                # 表格出现立即返回；无表页面加载完成 3s 后也不再空等
                "wait_for": "js:() => document.querySelectorAll('table').length > 0 || (document.readyState === 'complete' && performance.now() > 3000)",
            }
            try:
                from crawl4ai.table_extraction import DefaultTableExtraction
                run_kwargs["table_extraction"] = DefaultTableExtraction()
            except Exception:
                try:
                    import crawl4ai as _c4a
                    _dte = getattr(_c4a, "DefaultTableExtraction", None)
                    if _dte is not None:
                        run_kwargs["table_extraction"] = _dte()
                except Exception:
                    pass
            run_config = CrawlerRunConfig(**_filter_kwargs(CrawlerRunConfig, run_kwargs))

            async with AsyncWebCrawler(config=browser_config) as crawler:
                async def _arun_cancellable(target_u):
                    task = asyncio.ensure_future(crawler.arun(url=target_u, config=run_config))
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            task.cancel()
                            return None, True
                        done, _pending = await asyncio.wait({task}, timeout=0.5)
                        if done:
                            break
                    try:
                        return task.result(), False
                    except asyncio.CancelledError:
                        return None, True

                result, _cancelled = await _arun_cancellable(url)
                if _cancelled or result is None:
                    print("[Crawl4AI] 收到取消信号，已中止抓取。")
                    return None, None, None, "cancel", {}
                if result.success:
                    md_obj = result.markdown
                    if md_obj is not None and not isinstance(md_obj, str):
                        # crawl4ai >= 0.5 返回 MarkdownGenerationResult 对象
                        md_text = getattr(md_obj, "raw_markdown", None) or getattr(md_obj, "fit_markdown", None) or str(md_obj)
                    else:
                        md_text = md_obj
                    c4_tables = getattr(result, "tables", None)
                    sub_htmls = {}
                    # 若正文 HTML 未提取出有效表格（哪怕 c4_tables 含布局噪声表）且含 /tables/N 等子页链接，复用当前 crawler 抓取子页
                    if result.html:
                        sub_urls = _extract_table_subpage_urls(url, result.html)
                        if sub_urls and not parse_tables_from_html(result.html, table_idx):
                            for sub_u in sub_urls:
                                if cancel_event is not None and cancel_event.is_set():
                                    break
                                try:
                                    sub_res, sub_cancelled = await _arun_cancellable(sub_u)
                                    if sub_cancelled:
                                        break
                                    if getattr(sub_res, "success", False) and getattr(sub_res, "html", None):
                                        sub_htmls[sub_u] = sub_res.html
                                except Exception:
                                    pass
                    return result.html, md_text, c4_tables, "ok", sub_htmls
                print(f"[Crawl4AI] Crawl failed: {result.error_message}")
                return None, None, None, "fail", {}

        def _run_crawl_attempt(active_cdp_url):
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is not None:
                import nest_asyncio
                nest_asyncio.apply()
                return running_loop.run_until_complete(_crawl(active_cdp_url))
            return asyncio.run(_crawl(active_cdp_url))

        html = markdown = c4_tables = None
        sub_htmls = {}
        status = "fail"
        try:
            html, markdown, c4_tables, status, sub_htmls = _run_crawl_attempt(cdp_url)
        except Exception as e:
            print(f"[Crawl4AI Error] Exception during crawl: {e}")
            status = "fail"

        # If CDP mode failed/aborted/collided and run wasn't cancelled, invalidate stale CDP cache
        # and fall back once to an isolated stealth Crawl4AI browser.
        if cdp_url and status == "fail" and not (cancel_event is not None and cancel_event.is_set()):
            print(f"[Crawl4AI] CDP 模式 ({cdp_url}) 失败或冲突，清除缓存并回退至独立隔离浏览器重试...")
            _CRAWL4AI_CDP_CACHE["checked"] = False
            _CRAWL4AI_CDP_CACHE["url"] = None
            try:
                html, markdown, c4_tables, status, sub_htmls = _run_crawl_attempt(None)
            except Exception as e:
                print(f"[Crawl4AI Error] Isolated fallback exception: {e}")
                status = "fail"

        if status == "cancel":
            return []
        if status == "fail":
            _crawl4ai_record(url, hard_failure=True)
            return []

        if html or c4_tables:
            if html and is_anti_bot_blocked("", html):
                if record_negative_cache:
                    record_negative_cache("crawl4ai", url=url, status_code=403, html_snippet=html, config=config)
                return []
            # 抓取成功（无论是否解析到表格）都清零该域失败计数
            _crawl4ai_record(url, hard_failure=False)
            if clear_negative_cache:
                clear_negative_cache("crawl4ai", url=url, config=config)
            if html:
                dfs = parse_tables_from_html(html, table_idx)
                if not dfs and sub_htmls:
                    dfs = _follow_table_subpages(
                        url,
                        html,
                        lambda su: sub_htmls.get(su, ""),
                        table_idx,
                        cancel_event=cancel_event,
                    )
                dfs = _append_builtin_supp_if_any(
                    dfs, url, html, table_idx=table_idx, cancel_event=cancel_event, max_timeout=12
                )
                if dfs:
                    print(f"[Crawl4AI] Successfully extracted {len(dfs)} tables from HTML.")
                    return dfs

            if c4_tables:
                parsed_c4 = []
                for idx_t, t_item in enumerate(c4_tables):
                    if not isinstance(t_item, dict):
                        continue
                    headers = t_item.get("headers") or []
                    rows = t_item.get("rows") or []
                    if not rows:
                        continue
                    try:
                        if headers:
                            df_t = pd.DataFrame(rows, columns=[clean_text(h) for h in headers], dtype=str)
                        else:
                            df_t = _rows_to_dataframe(rows)
                        if df_t is None or df_t.empty:
                            continue
                        cap_text = clean_text(t_item.get("caption") or "")
                        lbl_text = _extract_normalized_table_label(cap_text) if cap_text else ""
                        if common.is_unlabeled_single_col_layout_shell(df_t, has_explicit_label=bool(lbl_text or cap_text)):
                            continue
                        if is_metadata_table(df_t):
                            continue
                        if cap_text:
                            df_t.attrs["table_title"] = cap_text
                        if lbl_text:
                            df_t.attrs["label"] = lbl_text
                        df_t.attrs["extractor"] = "crawl4ai_table"
                        parsed_c4.append(df_t)
                    except Exception:
                        pass
                if parsed_c4:
                    print(f"[Crawl4AI] Successfully extracted {len(parsed_c4)} tables via DefaultTableExtraction.")
                    return parsed_c4

            if markdown:
                dfs = parse_tables_from_markdown(markdown, table_idx)
                if dfs:
                    print(f"[Crawl4AI] Successfully extracted {len(dfs)} tables from Markdown.")
                    return dfs
        return []
    finally:
        try:
            sem.release()
        except Exception:
            pass
        if lock_ctx is not None:
            try:
                lock_ctx.__exit__(None, None, None)
            except Exception:
                pass


def extract_tables_via_unpaywall(doi, table_idx="all", cancel_event=None):
    """
    Queries the Unpaywall API for a free full-text version of a DOI,
    then tries to extract HTML tables from that open-access URL.
    No API key required.  Covers PMC, Europe PMC, bioRxiv, repositories, etc.
    """
    if cancel_event is not None and cancel_event.is_set():
        return []
    try:
        config = load_config()
        polite_email = get_polite_email(config, service="unpaywall")
        oa_api = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}?email={urllib.parse.quote(polite_email)}"
        resp = requests.get(
            oa_api,
            headers={"User-Agent": f"ZoteroTableExtractor/1.0 (mailto:{polite_email})"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()

        # Prefer PMC/EuropePMC HTML pages (best table structure)
        best_url = None
        for loc in data.get("oa_locations", []):
            loc_url = loc.get("url_for_landing_page") or loc.get("url") or ""
            if "ncbi.nlm.nih.gov/pmc" in loc_url or "europepmc.org" in loc_url:
                best_url = loc_url
                break

        # Fallback to best_oa_location landing page
        if not best_url:
            best_loc = data.get("best_oa_location") or {}
            best_url = best_loc.get("url_for_landing_page") or best_loc.get("url")

        if not best_url:
            return []

        print(f"[Unpaywall] \u627e\u5230\u5f00\u653e\u83b7\u53d6\u7248\u672c: {best_url}")

        # Skip PDF binaries/viewers in HTML/Firecrawl extractors
        if _looks_like_pdf_url(best_url):
            return []

        # 1) Direct HTTP parse FIRST (0 credits, sub-second on permissive OA hosts)
        is_pdf_response = False
        try:
            r = requests.get(best_url, timeout=12, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ZoteroTableExtractor/1.0)"
            })
            ctype = (r.headers.get("Content-Type") or "").lower()
            raw_head = (getattr(r, "content", None) or b"")[:5]
            if "application/pdf" in ctype or raw_head == b"%PDF-":
                is_pdf_response = True
            elif r.status_code == 200 and not is_anti_bot_blocked("", r.text[:4000]):
                final_u = str(getattr(r, "url", "") or best_url)
                dfs = [d for d in parse_tables_from_html(r.text, table_idx) if not _is_firecrawl_noise_table(d)]
                if not dfs:
                    def _fetch_sub_html(su):
                        try:
                            sr = requests.get(su, timeout=10, headers={
                                "User-Agent": "Mozilla/5.0 (compatible; ZoteroTableExtractor/1.0)"
                            })
                            return sr.text if sr.status_code == 200 else ""
                        except Exception:
                            return ""
                    sub_dfs = _follow_table_subpages(final_u, r.text, _fetch_sub_html, table_idx, cancel_event=cancel_event)
                    dfs = [d for d in sub_dfs if not _is_firecrawl_noise_table(d)]
                dfs = _append_builtin_supp_if_any(
                    dfs,
                    final_u,
                    r.text,
                    table_idx=table_idx,
                    cancel_event=cancel_event,
                    max_timeout=12,
                    main_present_cap=12.0,
                )
                if dfs:
                    print(f"[Unpaywall] \u901a\u8fc7\u76f4\u63a5 HTTP \u4ece\u5f00\u653e\u7248\u672c\u63d0\u53d6\u5230 {len(dfs)} \u5f20\u8868\u683c\u3002")
                    return dfs
        except Exception as e:
            print(f"[Unpaywall] \u76f4\u63a5 HTTP \u83b7\u53d6\u5931\u8d25: {e}")

        if is_pdf_response:
            return []

        # 2) Fallback to Firecrawl ONLY for verified non-PDF OA landing URLs
        config = load_config()
        key = config.get("FIRECRAWL_API_KEY") or os.environ.get("FIRECRAWL_API_KEY")
        if key:
            dfs = extract_tables_via_firecrawl(
                best_url,
                key,
                table_idx,
                cancel_event=cancel_event,
                is_oa=True,
                doi=doi,
            )
            if dfs:
                print(f"[Unpaywall] \u901a\u8fc7 Firecrawl \u4ece\u5f00\u653e\u7248\u672c\u63d0\u53d6\u5230 {len(dfs)} \u5f20\u8868\u683c\u3002")
                return dfs

    except Exception as e:
        print(f"[Unpaywall] \u67e5\u8be2\u5931\u8d25: {e}")

    return []
