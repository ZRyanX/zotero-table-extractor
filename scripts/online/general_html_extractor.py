import os
import re
import urllib.parse
import urllib.request
import pandas as pd
from playwright.sync_api import sync_playwright

try:
    from ..playwright_utils import find_playwright_chromium
except ImportError:
    try:
        from playwright_utils import find_playwright_chromium
    except ImportError:
        find_playwright_chromium = lambda: None

# Shared helpers (text cleaning, table filtering, config)
try:
    from ..common import clean_text, escape_formula, is_supplementary, clean_table_filename, load_config, is_metadata_table, df_map
except ImportError:
    from common import clean_text, escape_formula, is_supplementary, clean_table_filename, load_config, is_metadata_table, df_map


def get_table_number(title):
    if not title:
        return None
    if re.search(r'\b(?:Table\s+of\s+contents|Contents|List\s+of\s+tables)\b', title, re.IGNORECASE):
        return None
    match = re.search(r'\b(?:Tableau?x?|Tables?|Tab\b|\b表)\.?\s*(?:S\s*)?(\d+(?:[\.\-]\d+)?|\b[ivxlcdm]+\b|[A-Za-z]\b)', title, re.IGNORECASE)
    if match:
        val = match.group(1)
        if val.isdigit():
            return int(val)
        return val
    return None

def extract_table_label_from_title(title):
    if not title:
        return None
    if re.search(r'\b(?:Table\s+of\s+contents|Contents|List\s+of\s+tables)\b', title, re.IGNORECASE):
        return None
    token = r'(?:\d+(?:[\.\-]\d+)?|\b[ivxlcdm]+\b|[A-Za-z]\b)'
    sep_token = rf'(?:\s*[\/\-_&,]\s*(?:S\s*)?{token})*'
    prefix = r'(?:Tableau?x?|Tables?|Tab\b|表|Supplementary\s+material|Additional\s+Table|Additional\s+file)'
    match = re.search(rf'^(?:{prefix}\.?\s*(?:S\s*)?{token}{sep_token})', title, re.IGNORECASE)
    if match:
        label = match.group(0).strip()
        label = re.sub(r'[\/\-_&,]+$', '', label).strip()
        label = re.sub(r'\s+', ' ', label)
        label = label.replace(' / ', '_').replace('/', '_').replace(' - ', '_').replace('-', '_')
        return label
    return None

def is_duplicate_table(df, existing_dfs):
    title1 = getattr(df, 'attrs', {}).get('table_title', '') or ''
    label1 = getattr(df, 'attrs', {}).get('label', '') or ''
    is_supp1 = is_supplementary(label1 or title1)
    num1 = get_table_number(label1 or title1)
    
    for existing in existing_dfs:
        title2 = getattr(existing, 'attrs', {}).get('table_title', '') or ''
        label2 = getattr(existing, 'attrs', {}).get('label', '') or ''
        is_supp2 = is_supplementary(label2 or title2)
        
        if is_supp1 != is_supp2:
            continue
            
        num2 = get_table_number(label2 or title2)
        
        if num1 is not None and num2 is not None:
            if num1 == num2:
                # Check column overlap to distinguish between duplicate panels and split parts of the same table
                cols1 = [str(c).lower().strip() for c in df.columns]
                cols2 = [str(c).lower().strip() for c in existing.columns]
                
                compare_cols1 = [c for c in cols1 if 'unnamed' not in c]
                compare_cols2 = [c for c in cols2 if 'unnamed' not in c]
                
                intersection = set(compare_cols1).intersection(set(compare_cols2))
                if compare_cols1 and compare_cols2:
                    overlap_ratio = len(intersection) / min(len(compare_cols1), len(compare_cols2))
                    if overlap_ratio > 0.5:
                        return True
                    else:
                        continue
                else:
                    if df.shape == existing.shape:
                        return True
                    continue
                
        # Fallback to existing checks
        if df.shape == existing.shape:
            cols1 = [str(c).lower().strip() for c in df.columns]
            cols2 = [str(c).lower().strip() for c in existing.columns]
            if cols1 == cols2:
                if df.empty or existing.empty:
                    return True
                try:
                    if df.iloc[0].equals(existing.iloc[0]):
                        return True
                except Exception:
                    pass
    return False


def extract_tables_from_element(element, table_idx="all", default_title=None):
    """
    全出版社通用 HTML 表格提取器（Universal Multi-Publisher HTML Extractor）。
    覆盖 Elsevier (ScienceDirect), Springer Nature, Atypon (Wiley/ACS/T&F), Silverchair (OUP/GSA),
    MDPI, Frontiers, PLOS, IEEE 及 CNKI 等主流平台的原生 DOM 结构。
    """
    try:
        extracted_tables = element.evaluate("""
        (rootEl) => {
            // 1. 全球主流出版社全量表格容器选择器矩阵（Springer, MDPI, Wiley/AGU, T&F, GSW等）
            const WRAPPER_SELECTORS = [
                // JATS 通用与 Elsevier
                ".table-wrap", "div.Table", "section.table", "div.table-container",
                // Springer Nature
                "figure.c-article-table__wrapper", "div.c-article-table", "div.c-article-table__table", "div[id^='Tab']",
                // MDPI
                ".html-table-container", ".html-table-wrapper", "div.html-table", "table.html-table",
                // Wiley, AGU & Atypon Literatum
                ".NLM_table-wrap", ".article-table-content", "section.article-section__table", "div[id^='t00']", "div.table__wrapper",
                // Taylor & Francis
                "div[data-widget-def='tableWidget']", "div.entry-table", "div.table-item",
                // Silverchair (GSW, OUP) & CNKI
                ".table-modal", ".table-wrap-content", ".table-box", "div[role='grid']"
            ];

            let candidateWrappers = [];
            WRAPPER_SELECTORS.forEach(sel => {
                rootEl.querySelectorAll(sel).forEach(el => {
                    if (!candidateWrappers.includes(el)) candidateWrappers.push(el);
                });
            });

            // 若未匹配到外层容器，回退至所有 <table> 标签
            if (candidateWrappers.length === 0) {
                candidateWrappers = Array.from(rootEl.querySelectorAll("table"));
            }

            const results = [];
            const processedTables = new Set();

            candidateWrappers.forEach((wrapper, wIdx) => {
                const tableEl = wrapper.tagName === 'TABLE' ? wrapper : wrapper.querySelector('table');
                if (!tableEl || processedTables.has(tableEl)) return;
                processedTables.add(tableEl);

                // 2. 多维度高精表题定位（适配各出版商）
                let captionText = '';
                const capEl = wrapper.querySelector('figcaption, caption, .table-caption, .c-article-table__caption, .caption, .table-wrap-title, .table-title, .table-header, .table-intro, .html-table-title, .entry-table-title, header.article-table-header');
                if (capEl) {
                    captionText = (capEl.innerText || capEl.textContent || '').trim();
                }
                if (!captionText) {
                    // 向上回溯兄弟节点
                    let prev = wrapper.previousElementSibling;
                    for (let i = 0; i < 3 && prev; i++) {
                        const txt = (prev.innerText || prev.textContent || '').trim();
                        if (txt && (/^(?:表|Table|Tab\\.|TABLE)\\s*\\d+/i.test(txt) || txt.toLowerCase().includes('table'))) {
                            captionText = txt;
                            break;
                        }
                        prev = prev.previousElementSibling;
                    }
                }

                // 3. 脚注独立分离（适配各出版商）
                let footnoteText = '';
                const footEl = wrapper.querySelector('.table-wrap-foot, .table-footnotes, .c-article-table__notes, .c-article-table__notes-list, .table-foot, .table-footnote, .table-note, .table__foot, .html-table-footnote, tfoot');
                if (footEl) {
                    footnoteText = (footEl.innerText || footEl.textContent || '').trim();
                }

                // 4. 二维网格跨行跨列 (rowspan / colspan) 完整展开
                const rows = Array.from(tableEl.querySelectorAll('tr'));
                const grid = [];
                const occupied = {};
                const mark = (r, c) => { if (!occupied[r]) occupied[r] = {}; occupied[r][c] = true; };
                const nextFreeCol = (r, startC) => { let c = startC; while (occupied[r] && occupied[r][c]) c++; return c; };

                rows.forEach((tr, r) => {
                    // 排除被包含在 tfoot 里的脚注行
                    if (footEl && footEl.contains(tr)) return;
                    if (!grid[r]) grid[r] = {};
                    let col = 0;
                    const cells = Array.from(tr.querySelectorAll('td, th'));
                    cells.forEach(cell => {
                        col = nextFreeCol(r, col);
                        const rowspan = parseInt(cell.getAttribute('rowspan') || '1', 10);
                        const colspan = parseInt(cell.getAttribute('colspan') || '1', 10);
                        const text = (cell.innerText || cell.textContent || '').trim().replace(/\\r/g, '').replace(/\\n/g, ' ').replace(/\\s+/g, ' ');
                        for (let dr = 0; dr < rowspan; dr++) {
                            for (let dc = 0; dc < colspan; dc++) {
                                if (!grid[r + dr]) grid[r + dr] = {};
                                grid[r + dr][col + dc] = text;
                                mark(r + dr, col + dc);
                            }
                        }
                        col += colspan;
                    });
                });

                const nRows = grid.length;
                let nCols = 0;
                for (let r = 0; r < nRows; r++) {
                    if (grid[r]) {
                        const maxC = Math.max(...Object.keys(grid[r]).map(Number));
                        if (maxC + 1 > nCols) nCols = maxC + 1;
                    }
                }
                const rowList = [];
                for (let r = 0; r < nRows; r++) {
                    const row = [];
                    for (let c = 0; c < nCols; c++) {
                        row.push((grid[r] && grid[r][c] !== undefined) ? grid[r][c] : '');
                    }
                    if (row.some(v => v.trim())) rowList.push(row);
                }

                if (rowList.length >= 1) {
                    results.push({
                        caption: captionText,
                        footnote: footnoteText,
                        rows: rowList
                    });
                }
            });

            return results;
        }
        """)
    except Exception as e:
        print(f"[通用 HTML 提取] JS 全局多出版商表格扫描异常: {e}")
        extracted_tables = []

    extracted_dfs = []
    for t_idx, t_data in enumerate(extracted_tables or []):
        rows_data = t_data.get('rows', [])
        if not rows_data or len(rows_data) < 2:
            continue

        raw_title = t_data.get('caption', '') or default_title or f"Table {t_idx + 1}"
        footnote = t_data.get('footnote', '')

        max_cols = max(len(r) for r in rows_data)
        for i in range(len(rows_data)):
            if len(rows_data[i]) < max_cols:
                rows_data[i] = rows_data[i] + [''] * (max_cols - len(rows_data[i]))

        header_row = rows_data[0]
        data_rows = rows_data[1:]

        seen = {}
        unique_header = []
        for col in header_row:
            col = col.strip() if col else ''
            if not col:
                col = 'Unnamed'
            if col in seen:
                seen[col] += 1
                unique_header.append(f"{col}_{seen[col]}")
            else:
                seen[col] = 0
                unique_header.append(col)

        df = pd.DataFrame(data_rows, columns=unique_header)
        if is_metadata_table(df):
            continue

        df.columns = [escape_formula(clean_text(c)) for c in df.columns]
        df = df_map(df, lambda v: escape_formula(clean_text(v)))

        cleaned_title = clean_table_filename(raw_title, t_idx + 1)
        df.attrs['table_title'] = cleaned_title
        if footnote:
            df.attrs['footnotes'] = footnote
        label = extract_table_label_from_title(cleaned_title)
        if label:
            df.attrs['label'] = label

        extracted_dfs.append(df)

    return extracted_dfs


def _dismiss_cookie_consent(page, timeout_ms: int = 4000):
    """
    Attempts to auto-dismiss GDPR/cookie consent banners (OneTrust, Springer, Nature, etc.).
    Clicks common "Accept All" / "I Agree" buttons so the actual page content is visible.
    Safe to call even if no banner is present.
    """
    CONSENT_SELECTORS = [
        # OneTrust (used by Springer Nature)
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
        "[id*='accept-all']",
        "[id*='acceptAll']",
        # Generic patterns
        "button[data-testid='accept-all']",
        "button[aria-label*='accept all' i]",
        "button[aria-label*='agree' i]",
        ".cc-btn.cc-allow",
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        # Springer-specific
        ".c-button--primary[data-test='accept-cookie']",
        "[data-cc='accept-all']",
    ]
    for sel in CONSENT_SELECTORS:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(800)
                print(f"  [同意按鈕] 已自动点击同意按鈕: {sel}")
                return True
        except Exception:
            pass
    return False


def _expand_silverchair_gsw_tables(page):
    """
    针对 GeoScienceWorld (GSW) / Silverchair 平台期刊（GSA Bulletin, Geology, Economic Geology,
    American Mineralogist, Lithosphere 等）的定制优化：
    1. 强制展开折叠的大型表格模态框 (.table-modal, .table-wrap-content)；
    2. 解决 Silverchair 懒加载与多层嵌套 JATS table-wrap 渲染问题；
    3. 模拟触发展开全部数据按钮，确保完整行数呈现。
    """
    try:
        page.evaluate("""
            () => {
                // 1. 展开所有 Silverchair 折叠的表格和模态框
                const gswTableContainers = document.querySelectorAll(
                    '.table-wrap, .table-modal, .table-wrap-content, .table-wrap-body, ' +
                    'section[class*="table-wrap"], div[data-widget-def="tableWidget"]'
                );
                gswTableContainers.forEach(el => {
                    el.style.display = 'block';
                    el.style.visibility = 'visible';
                    el.style.maxHeight = 'none';
                    el.style.overflow = 'visible';
                });

                // 2. 模拟点击 Silverchair 'View Large' / 'Open in Viewer' 展开按钮
                const expandBtns = document.querySelectorAll(
                    'button[class*="table-expand"], a[class*="view-large"], .table-modal-trigger, .table-view-all'
                );
                expandBtns.forEach(b => {
                    try { b.click(); } catch(e) {}
                });
            }
        """)
        print("[GeoScienceWorld 适配] 已成功激活 Silverchair/GSW 深度展开与无损渲染")
    except Exception as e:
        pass


def _extract_anchor_table(page, fragment: str, target_table_num: int):
    """
    Directly extracts the table element nearest to the anchor #fragment on the page.
    This handles Springer/Nature inline tables that are referenced by URL anchors like
    #Tab1, #Tab2, etc. — the table is IN the page HTML, not a download link.

    Strategy:
    1. Find element with id=fragment (e.g. <div id="Tab1">)
    2. Walk the DOM from that element to find the nearest <table>
    3. Extract it as a DataFrame
    """
    try:
        rows_data = page.evaluate(f"""
        () => {{
            // Find anchor element
            const anchor = document.getElementById('{fragment}') ||
                           document.querySelector('[data-table="{fragment}"]') ||
                           document.querySelector('[id="{fragment}"]');
            if (!anchor) return null;

            // Search for a table: first inside the anchor, then walking up siblings
            let tableEl = anchor.querySelector('table');
            if (!tableEl) {{
                // Walk up to parent, then look at siblings and their descendants
                let parent = anchor.parentElement;
                for (let depth = 0; depth < 5 && parent && !tableEl; depth++) {{
                    tableEl = parent.querySelector('table');
                    parent = parent.parentElement;
                }}
            }}
            if (!tableEl) {{
                // Try next sibling elements
                let sibling = anchor.nextElementSibling;
                for (let i = 0; i < 5 && sibling && !tableEl; i++) {{
                    if (sibling.tagName === 'TABLE') tableEl = sibling;
                    else tableEl = sibling.querySelector('table');
                    sibling = sibling.nextElementSibling;
                }}
            }}
            if (!tableEl) return null;

            // Extract caption/title
            let caption = '';
            const cap = tableEl.querySelector('caption');
            if (cap) caption = cap.innerText.trim();
            if (!caption) {{
                // Try previous sibling heading
                let prev = tableEl.previousElementSibling || anchor;
                for (let i = 0; i < 4 && prev; i++) {{
                    const txt = (prev.innerText || prev.textContent || '').trim();
                    if (txt && (txt.toLowerCase().includes('table') || txt.includes('表'))) {{
                        caption = txt;
                        break;
                    }}
                    prev = prev.previousElementSibling;
                }}
            }}

            // Extract rows with rowspan/colspan expansion
            const grid = [];
            const occupied = {{}};
            const mark = (r, c) => {{ if (!occupied[r]) occupied[r] = {{}}; occupied[r][c] = true; }};
            const nextFreeCol = (r, startC) => {{ let c = startC; while (occupied[r] && occupied[r][c]) c++; return c; }};
            tableEl.querySelectorAll('tr').forEach((tr, r) => {{
                if (!grid[r]) grid[r] = {{}};
                let col = 0;
                tr.querySelectorAll('td, th').forEach(cell => {{
                    col = nextFreeCol(r, col);
                    const rs = parseInt(cell.getAttribute('rowspan') || '1', 10);
                    const cs = parseInt(cell.getAttribute('colspan') || '1', 10);
                    const text = (cell.innerText || cell.textContent || '').trim().replace(/\\s+/g, ' ');
                    for (let dr = 0; dr < rs; dr++) for (let dc = 0; dc < cs; dc++) {{
                        if (!grid[r+dr]) grid[r+dr] = {{}};
                        grid[r+dr][col+dc] = text; mark(r+dr, col+dc);
                    }}
                    col += cs;
                }});
            }});
            const nRows = grid.length;
            let nCols = 0;
            for (let r = 0; r < nRows; r++) if (grid[r]) {{ const mx = Math.max(...Object.keys(grid[r]).map(Number)); if (mx+1 > nCols) nCols = mx+1; }}
            const rows = [];
            for (let r = 0; r < nRows; r++) {{
                const row = [];
                for (let c = 0; c < nCols; c++) row.push((grid[r] && grid[r][c] !== undefined) ? grid[r][c] : '');
                if (row.some(v => v.trim())) rows.push(row);
            }}

            return {{ caption, rows }};
        }}
        """)

        if not rows_data or not rows_data.get('rows'):
            return []

        rows = rows_data['rows']
        caption = rows_data.get('caption', '') or f"Table {target_table_num}"

        # Normalize row widths
        max_cols = max(len(r) for r in rows)
        for r in rows:
            while len(r) < max_cols:
                r.append("")

        header_row = rows[0]
        data_rows = rows[1:]

        # Deduplicate column names
        seen = {}
        unique_header = []
        for col in header_row:
            col = col or "Unnamed"
            if col in seen:
                seen[col] += 1
                unique_header.append(f"{col}_{seen[col]}")
            else:
                seen[col] = 0
                unique_header.append(col)

        df = pd.DataFrame(data_rows, columns=unique_header)
        if is_metadata_table(df):
            return []

        df.columns = [escape_formula(c) for c in df.columns]
        df = df_map(df, escape_formula)

        cleaned_title = clean_table_filename(caption, target_table_num)
        df.attrs['table_title'] = cleaned_title
        return [df]

    except Exception as e:
        print(f"[通用自动化] _extract_anchor_table 警告: {e}")
        return []

def _try_real_chrome_user_data() -> str:
    """
    Tries to find the real Chrome/Edge user data directory on Windows/macOS/Linux.
    Returns the path if found, else empty string.
    """
    import platform
    system = platform.system()
    candidates = []
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(local, "Google", "Chrome", "User Data"),
            os.path.join(local, "Microsoft", "Edge", "User Data"),
            os.path.join(local, "Chromium", "User Data"),
        ]
    elif system == "Darwin":
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
            os.path.join(home, "Library", "Application Support", "Microsoft Edge"),
        ]
    else:  # Linux
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".config", "google-chrome"),
            os.path.join(home, ".config", "chromium"),
        ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return ""


def _copy_chrome_profile_for_playwright(src_user_data: str) -> str:
    """
    Copies the essential parts of a Chrome user profile (cookies, preferences,
    local storage) to a temporary directory so Playwright can use it without
    conflicting with a running Chrome instance (which locks the profile).

    Returns the path to the temp profile dir, or empty string on failure.
    """
    import shutil
    import tempfile

    try:
        tmp = tempfile.mkdtemp(prefix="pw_chrome_profile_")
        default_src = os.path.join(src_user_data, "Default")
        default_dst = os.path.join(tmp, "Default")
        os.makedirs(default_dst, exist_ok=True)

        # Copy only the files needed for session/cookies (not the whole profile)
        essential_files = [
            "Cookies", "Cookies-journal",
            "Preferences", "Secure Preferences",
            "Local State",
            "Web Data", "Web Data-journal",
        ]
        # Copy Local State to tmp root (Chrome looks for it there)
        local_state_src = os.path.join(src_user_data, "Local State")
        if os.path.exists(local_state_src):
            shutil.copy2(local_state_src, os.path.join(tmp, "Local State"))

        for fname in essential_files:
            src_f = os.path.join(default_src, fname)
            if os.path.exists(src_f):
                try:
                    shutil.copy2(src_f, os.path.join(default_dst, fname))
                except Exception:
                    pass  # File may be locked; skip it

        # Copy network/session storage folder if present
        for folder in ["Network", "Session Storage", "IndexedDB"]:
            src_folder = os.path.join(default_src, folder)
            if os.path.isdir(src_folder):
                try:
                    shutil.copytree(src_folder, os.path.join(default_dst, folder))
                except Exception:
                    pass

        print(f"[通用自动化] 已复制 Chrome 配置到临时目录: {tmp}")
        return tmp
    except Exception as e:
        print(f"[通用自动化] 复制 Chrome 配置失败: {e}")
        return ""


_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
]
_STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "window.chrome = {runtime: {}};"  # Fake chrome object
)

def _inject_cookies_from_profile(context, user_data_dir):
    if not user_data_dir:
        return
    cookies_json = os.path.join(user_data_dir, "cookies.json")
    if os.path.exists(cookies_json):
        try:
            import json
            with open(cookies_json, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            if cookies:
                # Add cookies one by one to avoid crashing the context on invalid ones
                for c in cookies:
                    try:
                        context.add_cookies([c])
                    except Exception:
                        pass
                print(f"[通用自动化] 从 cookies.json 成功注入并载入了 {len(cookies)} 个会话 Cookies。")
        except Exception as e:
            print(f"[通用自动化] 载入 cookies.json 失败: {e}")

def _purge_boilerplate_dom(page):
    """
    Purges boilerplate nodes (nav, footer, ads, sidebar, etc.) from the page to clean
    the DOM and prevent extracting noise tables.
    """
    try:
        page.evaluate("""
            () => {
                const boilerplateSelectors = [
                    "nav", "footer", "header", "aside", 
                    ".ad", ".ads", ".sidebar", ".widget", 
                    ".cookie-consent", "#onetrust-banner-sdk",
                    "[class*='footer' i]", "[class*='sidebar' i]", "[class*='navigation' i]",
                    "[id*='footer' i]", "[id*='sidebar' i]", "[id*='navigation' i]"
                ];
                boilerplateSelectors.forEach(selector => {
                    try {
                        const elements = document.querySelectorAll(selector);
                        elements.forEach(el => {
                            if (!el.querySelector("table") && !el.querySelector("[role='grid']")) {
                                el.remove();
                            }
                        });
                    } catch (e) {}
                });
            }
        """)
        print("[通用自动化] 已成功过滤和净化网页 DOM 中的冗余/广告/导航模块。")
    except Exception as e:
        print(f"[通用自动化] DOM 净化降噪失败: {e}")

def _extract_local_heuristic_grids(page, table_idx="all"):
    """
    Local heuristic grid extraction. Finds repeating div/flex/grid containers that mimic tables.
    Returns a list of DataFrames.
    """
    try:
        grids_data = page.evaluate("""
            () => {
                const results = [];
                const candidates = Array.from(document.querySelectorAll("div, [role='grid']"));
                
                let tableCount = 0;
                candidates.forEach((container) => {
                    if (container.offsetWidth === 0 || container.offsetHeight === 0) return;
                    
                    const role = container.getAttribute("role") || "";
                    const display = window.getComputedStyle(container).display;
                    const isGridLike = role === "grid" || display === "grid" || container.classList.contains("grid-container") || container.classList.contains("ag-theme-balham");
                    
                    let rows = [];
                    if (role === "grid") {
                        rows = Array.from(container.querySelectorAll("[role='row']"));
                    }
                    
                    if (rows.length === 0) {
                        const children = Array.from(container.children);
                        if (children.length > 2) {
                            const firstChildClass = children[0].className;
                            if (firstChildClass && children.every(c => c.className === firstChildClass)) {
                                rows = children;
                            }
                        }
                    }
                    
                    if (rows.length < 2) return;
                    
                    const matrix = [];
                    rows.forEach(r => {
                        let cells = [];
                        if (r.getAttribute("role") === "row") {
                            cells = Array.from(r.querySelectorAll("[role='gridcell'], [role='columnheader']"));
                        }
                        if (cells.length === 0) {
                            cells = Array.from(r.children).filter(c => {
                                const style = window.getComputedStyle(c);
                                return style.display !== "none";
                            });
                        }
                        const cellTexts = cells.map(c => (c.innerText || c.textContent || "").trim().replace(/\\s+/g, " "));
                        if (cellTexts.some(txt => txt !== "")) {
                            matrix.push(cellTexts);
                        }
                    });
                    
                    if (matrix.length >= 2) {
                        const widths = matrix.map(r => r.length);
                        const firstWidth = widths[0];
                        const isConsistent = widths.every(w => Math.abs(w - firstWidth) <= 1 && w > 1);
                        
                        if (isConsistent) {
                            let label = "";
                            let prev = container.previousElementSibling;
                            for (let i = 0; i < 3 && prev; i++) {
                                let txt = (prev.innerText || prev.textContent || "").trim();
                                if (txt && (txt.toLowerCase().includes("table") || txt.includes("表") || txt.toLowerCase().includes("supplementary material") || txt.toLowerCase().includes("additional table") || txt.toLowerCase().includes("additional file"))) {
                                    label = txt;
                                    break;
                                }
                                prev = prev.previousElementSibling;
                            }
                            
                            tableCount++;
                            results.push({
                                title: label || `Heuristic Table ${tableCount}`,
                                rows: matrix
                            });
                        }
                    }
                });
                return results;
            }
        """)
        
        dfs = []
        if grids_data:
            print(f"[通用自动化] 本地启发式算法成功检测到 {len(grids_data)} 个可能的数据网格（Grid）。")
            for t_idx, g in enumerate(grids_data):
                rows = g["rows"]
                title = g["title"]
                
                max_cols = max(len(r) for r in rows)
                for r in rows:
                    while len(r) < max_cols:
                        r.append("")
                        
                header_row = rows[0]
                data_rows = rows[1:]
                
                seen = {}
                unique_header = []
                for col in header_row:
                    col = col or "Unnamed"
                    if col in seen:
                        seen[col] += 1
                        unique_header.append(f"{col}_{seen[col]}")
                    else:
                        seen[col] = 0
                        unique_header.append(col)
                        
                df = pd.DataFrame(data_rows, columns=unique_header)
                if is_metadata_table(df):
                    continue
                    
                df.columns = [escape_formula(c) for c in df.columns]
                df = df_map(df, escape_formula)
                    
                cleaned_title = clean_table_filename(title, t_idx + 1)
                df.attrs['table_title'] = cleaned_title
                dfs.append(df)
                print(f"     ✓ 本地启发式网格提取成功: {cleaned_title} (Shape: {df.shape[0]}x{df.shape[1]})")
                
            return dfs
    except Exception as e:
        print(f"[通用自动化] 本地启发式网格提取失败: {e}")
    return []

def _extract_custom_grid_tables(page, url, api_key=None):
    """
    Queries Firecrawl structured JSON extraction to discover CSS selectors for custom grids,
    then uses Playwright to extract rows/columns.
    """
    import requests
    
    # 1. Resolve key
    key = api_key
    if not key:
        config = load_config()
        key = config.get("FIRECRAWL_API_KEY")
        
    if not key:
        return []
        
    print(f"[通用自动化] 未能发现标准 HTML <table> 标签。尝试调用 Firecrawl V2 API 寻找自适应 CSS 选择器...")
    
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    
    # Define selector schema
    selector_schema = {
        "type": "object",
        "properties": {
            "custom_table_selectors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "table_label": {"type": "string", "description": "e.g., Table 1, Table 2"},
                        "container_selector": {"type": "string", "description": "CSS selector for the grid container element, e.g., 'div.grid-container', '.my-table'"},
                        "row_selector": {"type": "string", "description": "CSS selector for rows inside the container, e.g., 'div.grid-row', 'div.slick-row', 'tr'"},
                        "cell_selector": {"type": "string", "description": "CSS selector for cells inside each row, e.g., 'div.grid-cell', 'div.slick-cell', 'td'"}
                    },
                    "required": ["container_selector", "row_selector", "cell_selector"]
                }
            }
        },
        "required": ["custom_table_selectors"]
    }
    
    payload = {
        "url": url,
        "formats": [
            {
                "type": "json",
                "schema": selector_schema
            }
        ]
    }
    
    try:
        response = requests.post("https://api.firecrawl.dev/v2/scrape", json=payload, headers=headers, timeout=25)
        if response.status_code == 200:
            data = response.json()
            json_data = data.get("data", {}).get("json", {})
            selectors = json_data.get("custom_table_selectors", [])
            if not selectors:
                print("[通用自动化] Firecrawl 未返回任何自定义 CSS 选择器。")
                return []
                
            print(f"[通用自动化] 成功获取 {len(selectors)} 个自定义 CSS 选择器。开始动态提取网格数据...")
            
            custom_dfs = []
            for s_idx, sel in enumerate(selectors):
                c_sel = sel.get("container_selector")
                r_sel = sel.get("row_selector")
                ce_sel = sel.get("cell_selector")
                label = sel.get("table_label") or f"Table {s_idx + 1}"
                
                if not c_sel or not r_sel or not ce_sel:
                    continue
                    
                print(f"  -> 应用选择器: container='{c_sel}', row='{r_sel}', cell='{ce_sel}'")
                
                containers = page.query_selector_all(c_sel)
                for c_idx, container in enumerate(containers):
                    rows_data = []
                    rows = container.query_selector_all(r_sel)
                    for r in rows:
                        cells = r.query_selector_all(ce_sel)
                        cells_text = [clean_text(cell.inner_text()) for cell in cells]
                        if any(cells_text):
                            rows_data.append(cells_text)
                            
                    if rows_data:
                        max_cols = max(len(row) for row in rows_data)
                        for i in range(len(rows_data)):
                            if len(rows_data[i]) < max_cols:
                                rows_data[i] = rows_data[i] + [""] * (max_cols - len(rows_data[i]))
                                
                        header_row = rows_data[0]
                        data_rows = rows_data[1:]
                        
                        seen = {}
                        unique_header = []
                        for col in header_row:
                            col = col or "Unnamed"
                            if col in seen:
                                seen[col] += 1
                                unique_header.append(f"{col}_{seen[col]}")
                            else:
                                seen[col] = 0
                                unique_header.append(col)
                                
                        df = pd.DataFrame(data_rows, columns=unique_header)
                        df.columns = [escape_formula(c) for c in df.columns]
                        df = df_map(df, escape_formula)
                            
                        cleaned_title = clean_table_filename(label, s_idx + 1)
                        df.attrs['table_title'] = cleaned_title
                        custom_dfs.append(df)
                        print(f"     ✓ 成功从自定义网格 {c_sel} 提取表格 (Shape: {df.shape[0]}x{df.shape[1]})")
                        
            return custom_dfs
    except Exception as e:
        print(f"[通用自动化] 自适应 CSS 选择器提取失败: {e}")
        
    return []

def _ensure_chrome_debug_running():
    import socket
    import subprocess
    import platform
    import tempfile
    import time
    import os

    def is_listening():
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(('127.0.0.1', 9222))
            s.close()
            return True
        except Exception:
            return False

    if is_listening():
        return True

    os_type = platform.system().lower()
    chrome_path = None
    if os_type == "windows":
        candidate_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        try:
            import winreg
            for hkey in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                try:
                    with winreg.OpenKey(hkey, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                        val, _ = winreg.QueryValueEx(key, "")
                        if val and os.path.exists(val):
                            candidate_paths.insert(0, val)
                except Exception:
                    pass
        except Exception:
            pass

        for p in candidate_paths:
            if os.path.exists(p):
                chrome_path = p
                break
    elif os_type == "darwin":
        candidate_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        ]
        for p in candidate_paths:
            if os.path.exists(p):
                chrome_path = p
                break

    if not chrome_path:
        print("[通用自动化] 未能自动识别到 Google Chrome 浏览器的可执行文件路径。")
        return False
    profile_dir = os.path.join(tempfile.gettempdir(), "chrome_debug_profile")
    
    # Clone default cookies/session if available to preserve user login
    real_profile = _try_real_chrome_user_data()
    if real_profile and os.path.exists(real_profile):
        import shutil
        try:
            os.makedirs(profile_dir, exist_ok=True)
            src_state = os.path.join(real_profile, "Local State")
            dst_state = os.path.join(profile_dir, "Local State")
            if os.path.exists(src_state):
                shutil.copy2(src_state, dst_state)
            dst_net = os.path.join(profile_dir, "Default", "Network")
            os.makedirs(dst_net, exist_ok=True)
            src_cookies = os.path.join(real_profile, "Default", "Network", "Cookies")
            dst_cookies = os.path.join(dst_net, "Cookies")
            if os.path.exists(src_cookies):
                shutil.copy2(src_cookies, dst_cookies)
            src_ls = os.path.join(real_profile, "Default", "Local Storage")
            dst_ls = os.path.join(profile_dir, "Default", "Local Storage")
            if os.path.isdir(src_ls):
                shutil.copytree(src_ls, dst_ls, dirs_exist_ok=True)
            src_ss = os.path.join(real_profile, "Default", "Session Storage")
            dst_ss = os.path.join(profile_dir, "Default", "Session Storage")
            if os.path.isdir(src_ss):
                shutil.copytree(src_ss, dst_ss, dirs_exist_ok=True)
            print("[通用自动化] 已成功克隆默认 Chrome 会话与 Cookie 数据库至调试沙箱。")
        except Exception as e:
            print(f"[通用自动化] 克隆 Chrome 会话失败: {e}")

    print(f"[通用自动化] 自动启动调试版 Chrome: {chrome_path}")
    print(f"[通用自动化] 调试会话用户数据目录: {profile_dir}")
    
    cmd = [
        chrome_path,
        "--remote-debugging-port=9222",
        f"--user-data-dir={profile_dir}"
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(0.5)
            if is_listening():
                print("[通用自动化] 调试版 Chrome 启动并成功启用 9222 端口！")
                return True
    except Exception as e:
        print(f"[通用自动化] 自动启动 Chrome 失败: {e}")

    return False

def extract_general_html_tables(url, table_idx="all", headed=False, user_data_dir=None, api_key=None):
    """
    General purpose table crawler. Follows subpages and checks tabs/iframes.
    Supports real Chrome user session (user_data_dir) to access paywalled content.
    Handles inline anchor-targeted tables (#Tab1, #Tab2 etc.) on Springer/Nature pages.

    修复：不再因摘要页 URL 就整体跳过——逐表交由 is_metadata_table() 过滤
    文献信息框/摘要版权框/目录大纲/参考文献列表，仅保留真正的数据表。
    """
    # Parse URL fragment to detect specific target table (e.g. #Tab2 or #table-3)
    parsed_url = urllib.parse.urlparse(url)
    fragment = parsed_url.fragment  # e.g., 'Tab2' or 'table-2'
    # URL without fragment for navigation (goto)
    url_no_fragment = urllib.parse.urlunparse(parsed_url._replace(fragment=''))

    target_table_num = None
    if fragment:
        # Match common anchor patterns from multiple publishers:
        # Springer:  #Tab1, #Tab2
        # Elsevier:  #tbl1, #tbl2
        # Wiley:     #t001, #t002
        # MDPI:      #table_body_display_...-t004  (extract trailing number)
        # Generic:   #table-1, #table_1, #Table1
        match = re.search(r'(?:[tT](?:ab(?:le)?)?[-_]?)(\d+)$', fragment)
        if not match:
            match = re.search(r'[-_t](\d+)$', fragment)  # fallback: trailing number
        if match:
            target_table_num = int(match.group(1))
            print(f"[通用自动化] 检测到 URL 锚点 #{fragment}，定位目标表格为 Table {target_table_num}。")


    headless = not headed

    # If no user_data_dir explicitly given, try auto-detect real Chrome to use existing login session
    if not user_data_dir:
        auto_detected = _try_real_chrome_user_data()
        if auto_detected:
            user_data_dir = auto_detected
            print(f"[通用自动化] 自动检测到 Chrome 用户数据目录: {user_data_dir}")
            print(f"[通用自动化] 将复用已有的浏览器登录 Session（如 Springer 机构认证）。")

    print(f"[通用自动化] 启动通用 HTML 阅读提取器 (headless={headless}, user_data_dir={user_data_dir or '(新建)'})...")

    all_dfs = []
    page_title = None

    is_gsw = "geoscienceworld.org" in url or "lyellcollection.org" in url or "10.1144" in url
    if is_gsw:
        print("[通用自动化] 检测到 GeoscienceWorld/Lyell 目标平台，自动激活 CDP 桥接调试浏览器...")
        _ensure_chrome_debug_running()

    exec_path = find_playwright_chromium()
    if exec_path:
        print(f"[Playwright] 使用自动识别的 Chromium 路径: {exec_path}")
    with sync_playwright() as p:
        _tmp_profile = None  # track temp profile for cleanup
        browser = None       # track browser reference for non-persistent cleanup

        # Strategy 0: Try connecting over CDP first (allows zero-detection and bypasses Cloudflare)
        connected_via_cdp = False
        skip_navigation = False
        try:
            # Check if port 9222 is open and listening
            with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1.5) as response:
                if response.status == 200:
                    print("[通用自动化] 检测到有运行在 9222 端口的 Chrome 进程。正在进行 CDP 桥接...")
                    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                    
                    # Try to find an existing tab that matches the target URL
                    target_page = None
                    for context in browser.contexts:
                        for p_instance in context.pages:
                            p_url = p_instance.url
                            if p_url and (p_url.rstrip('/') == url.rstrip('/') or (len(url) > 20 and url in p_url)):
                                target_page = p_instance
                                print(f"[通用自动化] 发现已打开的目标页面标签页: {p_url}，将直接复用！")
                                break
                        if target_page:
                            break
                    
                    if target_page:
                        page = target_page
                        try:
                            page.bring_to_front()
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        skip_navigation = True
                    else:
                        context = browser.contexts[0] if browser.contexts else browser.new_context()
                        page = context.new_page()
                        skip_navigation = False
                    connected_via_cdp = True
                    print("[通用自动化] CDP 桥接成功！将使用您当前活动的物理 Chrome 窗口。")
        except Exception as e:
            print(f"[通用自动化] CDP 桥接连接失败: {e}")

        if user_data_dir and not os.path.isdir(user_data_dir):
            print(f"[通用自动化] 警告: 指定的用户数据目录不存在 ({user_data_dir})，降级为新建环境。")
            user_data_dir = None

        if not connected_via_cdp and user_data_dir and os.path.isdir(user_data_dir):
            # Strategy 1: Try persistent_context (fastest — reuses full session)
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=headless,
                    executable_path=exec_path,
                    args=["--profile-directory=Default"] + _STEALTH_ARGS,
                    ignore_default_args=["--enable-automation"],
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900}
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.add_init_script(_STEALTH_INIT_SCRIPT)
                _inject_cookies_from_profile(context, user_data_dir)
            except Exception as e:
                print(f"[通用自动化] 无法使用已有 Chrome 用户数据 ({type(e).__name__})，尝试复制配置文件...")
                # Strategy 2: Copy profile to temp dir (handles locked profile)
                _tmp_profile = _copy_chrome_profile_for_playwright(user_data_dir)
                if _tmp_profile:
                    try:
                        context = p.chromium.launch_persistent_context(
                            _tmp_profile,
                            headless=headless,
                            executable_path=exec_path,
                            args=["--profile-directory=Default"] + _STEALTH_ARGS,
                            ignore_default_args=["--enable-automation"],
                            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                            viewport={"width": 1280, "height": 900}
                        )
                        page = context.pages[0] if context.pages else context.new_page()
                        page.add_init_script(_STEALTH_INIT_SCRIPT)
                        _inject_cookies_from_profile(context, _tmp_profile)
                        print(f"[通用自动化] 已使用临时配置文件目录启动。")
                    except Exception as e2:
                        print(f"[通用自动化] 临时配置文件启动也失败 ({e2})，降级为新建 Chromium 上下文。")
                        _tmp_profile = None
                        user_data_dir = None
                else:
                    user_data_dir = None

        if not connected_via_cdp and not user_data_dir:
            # Strategy 3: Fresh browser with stealth mode (handles open-access sites)
            effective_headless = headless
            print(f"[通用自动化] 使用新建 Chromium 上下文（无头模式={effective_headless}，反检测模式）...")
            browser = p.chromium.launch(
                headless=effective_headless,
                executable_path=exec_path,
                args=_STEALTH_ARGS,
                ignore_default_args=["--enable-automation"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = context.new_page()
            page.add_init_script(_STEALTH_INIT_SCRIPT)

        # 拦截 mailto:, tel: 等外部系统协议，防止 macOS 弹出系统 Mail.app 邮件应用
        try:
            page.route(
                lambda u: any(u.lower().startswith(p) for p in ["mailto:", "tel:", "sms:", "itms:", "macappstore:"]),
                lambda route: route.abort()
            )
        except Exception:
            pass

        if not skip_navigation:
            print(f"[通用自动化] 正在载入主页面: {url_no_fragment}")
            try:
                page.goto(url_no_fragment, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)

                # ── Crossref Chooser Resolver ──
                current_url = page.url
                if "chooser.crossref.org" in current_url or "crossref.org" in current_url:
                    print("[通用自动化] 检测到 Crossref 选项页面。正在解析重定向链接...")
                    ext_links = page.query_selector_all("a[href]")
                    target_urls = []
                    for el in ext_links:
                        href = el.get_attribute("href") or ""
                        if href.startswith("http") and "crossref.org" not in href and "doi.org" not in href:
                            target_urls.append(href)
                    
                    target_url = None
                    if target_urls:
                        # Prioritize geoscienceworld.org as it has weaker anti-scraping blocks
                        for url_option in target_urls:
                            if "geoscienceworld.org" in url_option:
                                target_url = url_option
                                break
                        if not target_url:
                            target_url = target_urls[0]
                            
                    if target_url:
                        print(f"[通用自动化] 自动选择重定向目标: {target_url}")
                        url_no_fragment = target_url
                        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(4000)

                # ── China DOI (chndoi.org / chinadoi.cn) Multi-resolution Chooser ──
                current_url = page.url
                if "chndoi.org" in current_url or "chinadoi.cn" in current_url or page.locator("text=多重解析地址").count() > 0:
                    print("[通用自动化] 检测到 China DOI 多重解析页面，自动提取【境内】知网地址...")
                    domestic_link = None
                    try:
                        # 查找带有 (境内) 的链接
                        links = page.query_selector_all("li, tr, td")
                        for l_el in links:
                            txt = l_el.inner_text() or ""
                            if "境内" in txt:
                                a_el = l_el.query_selector("a")
                                if a_el:
                                    domestic_link = a_el.get_attribute("href")
                                    break
                        if not domestic_link:
                            # 兜底查找非 oversea 的 link.cnki.net
                            a_els = page.query_selector_all("a[href*='link.cnki.net']")
                            for a_el in a_els:
                                h = a_el.get_attribute("href") or ""
                                if "oversea" not in h:
                                    domestic_link = h
                                    break
                    except Exception:
                        pass
                    if domestic_link:
                        print(f"[通用自动化] 自动跳转至境内地址: {domestic_link}")
                        url_no_fragment = domestic_link
                        page.goto(domestic_link, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(4000)

                # Try to wait for table elements
                try:
                    page.wait_for_selector("table", timeout=8000)
                except Exception:
                    pass
                # If fragment target, scroll to anchor element to trigger lazy-loading
                if fragment:
                    try:
                        page.evaluate(f"""() => {{
                            const el = document.getElementById('{fragment}') ||
                                       document.querySelector('[data-table=\"{fragment}\"]') ||
                                       document.querySelector('#{fragment}');
                            if (el) el.scrollIntoView({{behavior: 'instant', block: 'center'}});
                        }}""")
                        page.wait_for_timeout(2000)  # wait for lazy-loaded content after scroll
                    except Exception:
                        pass
            except Exception as e:
                print(f"[通用自动化] 页面加载警告: {e}")

        # Auto-dismiss GDPR cookie consent and expand GeoScienceWorld/Silverchair tables
        _dismiss_cookie_consent(page)
        _expand_silverchair_gsw_tables(page)

        # Scroll through the full page to trigger lazy-loaded table links

        try:
            page.evaluate("""
            async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                const scrollStep = Math.floor(window.innerHeight * 0.8);
                let lastH = 0;
                let steps = 0;
                while (steps < 40) {
                    window.scrollBy(0, scrollStep);
                    await delay(250);  // reduced from 400ms
                    const newH = document.body.scrollHeight;
                    if (window.scrollY + window.innerHeight >= newH && newH === lastH) break;
                    lastH = newH;
                    steps++;
                }
                window.scrollTo(0, 0);
            }
            """)
            page.wait_for_timeout(1500)
        except Exception:
            pass
            
        # ── NEW: Purge boilerplate components (headers, footers, sidebars, ads) ──
        if not is_gsw:
            _purge_boilerplate_dom(page)
        else:
            print("[通用自动化] GeoscienceWorld/Lyell 目标平台：跳过 DOM 净化以保留分片侧栏表格容器。")
            
        page_title = page.title()
        
        # ── Special Taylor & Francis Direct CSV Download Pipeline ──
        if "tandfonline.com" in url_no_fragment:
            print("[通用自动化] 检测到 Taylor & Francis 出版商。启用 Direct CSV 下载管道...")
            tf_doi = None
            doi_match = re.search(r'/doi/(?:full|abs|pdf)/(10\.\d{4,9}/[a-zA-Z0-9./\-_()]+)', url_no_fragment, re.IGNORECASE)
            if doi_match:
                tf_doi = doi_match.group(1)
            if tf_doi:
                print(f"[通用自动化] 解析出 Taylor & Francis DOI: {tf_doi}")
                consecutive_fails = 0
                import tempfile
                for idx in range(1, 21):
                    table_id = f"T{idx:04d}"
                    download_url = f"https://www.tandfonline.com/action/downloadTable?id={table_id}&doi={tf_doi}&downloadType=CSV"
                    print(f"  -> 尝试直连下载 Table {idx} ({table_id})...")
                    dl_page = context.new_page()
                    download_triggered = False
                    temp_csv_path = None
                    def handle_dl(download_obj):
                        nonlocal download_triggered, temp_csv_path
                        download_triggered = True
                        temp_csv_path = os.path.join(tempfile.gettempdir(), f"tf_table_{table_id}_{os.getpid()}.csv")
                        try:
                            download_obj.save_as(temp_csv_path)
                        except Exception:
                            pass
                    dl_page.on("download", handle_dl)
                    try:
                        dl_page.goto(download_url, wait_until="domcontentloaded", timeout=20000)
                        dl_page.wait_for_timeout(3000)
                    except Exception:
                        pass
                    finally:
                        dl_page.close()
                    if download_triggered and temp_csv_path and os.path.exists(temp_csv_path):
                        try:
                            with open(temp_csv_path, "r", encoding="utf-8", errors="ignore") as f:
                                prefix = f.read(150).strip()
                            if prefix.startswith("<!DOCTYPE") or "html" in prefix.lower() or "just a moment" in prefix.lower():
                                print(f"     · Table {idx} ({table_id}) 未订阅或被 Cloudflare 拦截。")
                                consecutive_fails += 1
                            else:
                                df = pd.read_csv(temp_csv_path)
                                if not df.empty and len(df.columns) >= 2:
                                    df.columns = [clean_text(c) for c in df.columns]
                                    for col in df.columns:
                                        df[col] = df[col].astype(str).apply(clean_text)
                                    df.attrs['table_title'] = f"Table {idx}"
                                    all_dfs.append(df)
                                    print(f"     ✓ 成功下载并解析 Table {idx} (Shape: {df.shape[0]}x{df.shape[1]})")
                                    consecutive_fails = 0
                                else:
                                    consecutive_fails += 1
                        except Exception as e:
                            print(f"     · 解析 CSV 失败: {e}")
                            consecutive_fails += 1
                        finally:
                            try:
                                os.remove(temp_csv_path)
                            except Exception:
                                pass
                    else:
                        consecutive_fails += 1
                    if consecutive_fails >= 2:
                        print(f"[通用自动化] 连续 {consecutive_fails} 次下载失败或结束，停止 T&F 直连下载。")
                        break
        # ── NEW: prioritize anchor-targeted table for Springer/Nature inline tables ──
        if fragment and target_table_num is not None:
            anchor_dfs = _extract_anchor_table(page, fragment, target_table_num)
            if anchor_dfs:
                print(f"[通用自动化] 通过锚点 #{fragment} 直接提取到 {len(anchor_dfs)} 个内联表格。")
                all_dfs.extend(anchor_dfs)

        if not all_dfs:
            frames = page.frames
            for f_idx, frame in enumerate(frames):
                try:
                    frame_dfs = extract_tables_from_element(frame, table_idx)
                    for df in frame_dfs:
                        if not is_duplicate_table(df, all_dfs):
                            all_dfs.append(df)
                except Exception:
                    pass
                    
            # ── NEW: Local Heuristic Grid Extractor & Adaptive Selector Fallback ──
            if not all_dfs:
                try:
                    # Stage 1: Local Heuristic Grid Extraction (Offline-First)
                    heuristic_dfs = _extract_local_heuristic_grids(page, table_idx)
                    for df in heuristic_dfs:
                        if not is_duplicate_table(df, all_dfs):
                            all_dfs.append(df)
                    
                    # Stage 2: Firecrawl Adaptive Selector Generation (Cloud Fallback)
                    if not all_dfs:
                        custom_dfs = _extract_custom_grid_tables(page, url, api_key)
                        for df in custom_dfs:
                            if not is_duplicate_table(df, all_dfs):
                                all_dfs.append(df)
                except Exception as e:
                    print(f"[通用自动化] 动态网格自适应提取或启发式提取抛出异常: {e}")
                
        # 2. Discover potential table subpage links (Springer / Silverchair style)
        links = page.query_selector_all("a")
        subpage_urls = set()
        
        for link in links:
            try:
                href = link.get_attribute("href") or ""
                text = link.inner_text().strip().lower()
                
                # Build absolute URL using current page URL to support redirects
                abs_url = urllib.parse.urljoin(page.url, href)
                
                # Check patterns
                # a) Springer table subpages (e.g. /article/xxx/tables/1)
                # b) Silverchair article-lookup table subpages (e.g. /article-lookup/table/xxx)
                # c) Link text has 'table' or '表' and href goes to a valid link
                is_table_link = False
                # Springer / Nature
                if "/tables/" in href:
                    is_table_link = True
                # GeoScienceWorld / Oxford (Silverchair CMS)
                elif "/article-lookup/table/" in href:
                    is_table_link = True
                # Wiley Online Library
                elif re.search(r'/doi/[^/]+/table/', href):
                    is_table_link = True
                # Taylor & Francis
                elif "/tables/" in href or "tableid=" in href.lower():
                    is_table_link = True
                # Generic: link text mentions 'table' and points to a numbered resource
                elif ("table" in text or "表" in text) and re.search(r'/\d+$', href):
                    is_table_link = True

                    
                if is_table_link and abs_url != url:
                    subpage_urls.add(abs_url)
            except Exception:
                pass
                
        # NOTE: We do NOT filter subpage URLs by fragment/anchor.
        # A URL like #Tab1 is only a hint for where the user originally landed;
        # we always scrape ALL discovered table subpages (/tables/1, /tables/2, ...)
        # so that the full article table set is captured.
        if target_table_num is not None and subpage_urls:
            print(f"[通用自动化] 检测到锚点 #Tab{target_table_num}，但将抓取文章全部 {len(subpage_urls)} 个表格子页面。")

        # ── Proactive Springer /tables/N probe ──
        # If we already found at least one /tables/N link, speculatively probe
        # /tables/2, /tables/3, ... until we hit a non-200 response.
        # This handles Springer pages where table links beyond the first are lazy-loaded
        # and may not appear even after full-page scroll.
        springer_base = None
        for su in subpage_urls:
            m = re.search(r'(https?://[^/]+(?:/[^/]+)*/tables)/\d+$', su)
            if m:
                springer_base = m.group(1)
                break
        if springer_base:
            existing_nums = set()
            for su in subpage_urls:
                m = re.search(r'/tables/(\d+)$', su)
                if m:
                    existing_nums.add(int(m.group(1)))
            # Probe up to table 20 (break on 2 consecutive misses)
            consecutive_misses = 0
            for n in range(1, 21):
                if n in existing_nums:
                    consecutive_misses = 0
                    continue
                probe_url = f"{springer_base}/{n}"
                try:
                    # Use Playwright's built-in request API instead of requests.head().
                    # This reuses the browser's Chrome Session cookies (institutional auth),
                    # is faster (no extra auth redirects), and has a shorter timeout.
                    resp = page.request.fetch(probe_url, method="HEAD", timeout=4000)
                    if resp.status == 200:
                        subpage_urls.add(probe_url)
                        print(f"[通用自动化] 探测到额外表格子页: {probe_url}")
                        consecutive_misses = 0
                    else:
                        consecutive_misses += 1
                        if consecutive_misses >= 2:
                            break
                except Exception:
                    consecutive_misses += 1
                    if consecutive_misses >= 2:
                        break

        if subpage_urls:
            # Sort by table number for deterministic ordering
            def _table_num(u):
                m = re.search(r'/(\d+)$', u)
                return int(m.group(1)) if m else 999
            sorted_subpage_urls = sorted(subpage_urls, key=_table_num)

            print(f"[通用自动化] 发现 {len(sorted_subpage_urls)} 个表格子页面链接，开始深入抓取...")
            for idx, sub_url in enumerate(sorted_subpage_urls):
                print(f"  -> 抓取子页面 [{idx+1}/{len(sorted_subpage_urls)}]: {sub_url}")
                try:
                    # ── KEY FIX: navigate the SAME page (not a new tab) ──
                    # The main page already accepted cookie consent via the Chrome session.
                    # Opening a new tab triggers Springer's full-page consent redirect.
                    # Reusing the same page object avoids this entirely.
                    page.goto(sub_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1200)  # reduced from 2000ms
                    # Still try to dismiss any consent banner, just in case
                    _dismiss_cookie_consent(page)
                    _expand_silverchair_gsw_tables(page)

                    # Scrape subpage heading for title
                    sub_title = ""
                    try:
                        h1 = page.query_selector(".c-article-table-heading, .c-article-table-title, .table-title, .table-caption, caption")
                        if h1 and h1.inner_text().strip():
                            sub_title = h1.inner_text().strip()
                        else:
                            headings = page.query_selector_all("h1, h2, h3")
                            for h in headings:
                                h_text = h.inner_text().strip()
                                if h_text and any(kw in h_text.lower() for kw in ["table", "表", "tab."]):
                                    sub_title = h_text
                                    break
                    except Exception:
                        pass

                    sub_dfs = extract_tables_from_element(page, table_idx, default_title=sub_title)
                    added = 0
                    for df in sub_dfs:
                        if not is_duplicate_table(df, all_dfs):
                            all_dfs.append(df)
                            added += 1
                    if added:
                        print(f"     ✓ 提取到 {added} 张表格")
                    else:
                        print(f"     · 未发现 HTML 表格（可能为图片/PDF 格式）")
                except Exception as e:
                    print(f"  Warning: 抓取子页面 {sub_url} 失败: {e}")


                    
        # Close context and clean up
        try:
            if connected_via_cdp:
                # 保护用户物理浏览器：CDP 模式下绝不调用 browser.close()，仅在新建标签页时关闭该标签页
                if not skip_navigation and page:
                    try:
                        page.close()
                    except Exception:
                        pass
            elif browser:
                browser.close()
            elif user_data_dir or _tmp_profile:
                context.close()
        except Exception:
            pass
        # Clean up temp profile dir
        if _tmp_profile and os.path.isdir(_tmp_profile):
            try:
                import shutil
                shutil.rmtree(_tmp_profile, ignore_errors=True)
            except Exception:
                pass

            
    # Clean paper title
    if page_title:
        for suffix in [" - Springer", " - Nature", " | SpringerLink", " - ScienceDirect", " - Wiley Online Library"]:
            if suffix in page_title:
                page_title = page_title.replace(suffix, "").strip()
                
    # NOTE: We do NOT filter final DataFrames by fragment/anchor number.
    # All tables from all subpages are returned regardless of which #TabN was in the URL.
    # The caller (extract_zotero_table.py) can further filter using --table-idx if needed.

    print(f"[通用自动化] 成功提取到 {len(all_dfs)} 张表格数据。")
    return all_dfs, page_title
