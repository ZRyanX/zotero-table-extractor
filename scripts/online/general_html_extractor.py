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
    from ..common import clean_text, escape_formula, is_supplementary, clean_table_filename, load_config, is_metadata_table, is_anti_bot_blocked, df_map
except ImportError:
    from common import clean_text, escape_formula, is_supplementary, clean_table_filename, load_config, is_metadata_table, is_anti_bot_blocked, df_map


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
            const root = (rootEl && typeof rootEl.querySelectorAll === 'function') ? rootEl : document;
            if (!root || typeof root.querySelectorAll !== 'function') return [];
            // 1. 全球主流出版社全量表格容器选择器矩阵（Elsevier, Springer, Nature, MDPI, Wiley/AGU, T&F, GSW, Frontiers, AAAS, APS等）
            const WRAPPER_SELECTORS = [
                // JATS 通用与 Elsevier (ScienceDirect)
                "div.tables", ".table-wrap", "div.Table", "section.table", "div.table-container",
                // Springer Nature (/tables/N 子页与内联)
                "div.c-article-table-container", "figure.c-article-table__wrapper", "div.c-article-table", "div.c-article-table__table", "div[id^='Tab']",
                // MDPI (真实数据表位于隐藏的 .html-table_show 容器，.html-table-wrap 仅含缩略图)
                ".html-table_show", ".html-table-container", ".html-table-wrapper", "div.html-table", "table.html-table",
                // Wiley, AGU & Atypon Literatum
                "div[id*='-tbl-'].article-table-content", ".NLM_table-wrap", ".article-table-content", "section.article-section__table", "div[id^='t00']", "div.table__wrapper",
                // Taylor & Francis
                "div[data-widget-def='tableWidget']", "div.entry-table", "div.table-item",
                // Frontiers, AAAS (Science), APS
                "div.ArticleTable", "figure.table", "div.article-fulltext-table-wrap",
                // IOPscience
                "div.boxout.boxout-bdr-grey",
                // Silverchair (GSW, OUP) & CNKI
                ".table-wrap-content", ".table-box", "div[role='grid']", ".table-modal"
            ];

            let candidateWrappers = [];
            WRAPPER_SELECTORS.forEach(sel => {
                root.querySelectorAll(sel).forEach(el => {
                    if (!candidateWrappers.includes(el)) candidateWrappers.push(el);
                });
            });

            // 若未匹配到外层容器，回退至所有 <table> 标签
            if (candidateWrappers.length === 0) {
                candidateWrappers = Array.from(root.querySelectorAll("table"));
            }

            const results = [];
            const processedTables = new Set();

            const isNoiseTable = (tableEl, wrapper) => {
                // MDPI 免责声明/出版商声明 1x2 表格
                if (tableEl.closest('section.html-fn_group, .html-fn_group')) return true;
                // Wiley/Atypon 支撑材料文件列表表 (Filename | Description)
                if (tableEl.matches('.support-info__table') || tableEl.closest('.support-info__table-wrapper, section.article-section__supporting')) return true;
                // Silverchair/GSW 同一表格会在 .table-overflow 与 .table-modal 中重复渲染两次
                const modalParent = tableEl.closest('.table-modal, .reveal-modal');
                if (modalParent) {
                    const wrapRoot = modalParent.closest('.table-wrap, section.figure-table-wrapper');
                    if (wrapRoot && wrapRoot.querySelector('.table-overflow table')) return true;
                }
                // Annual Reviews 等正文 1x1 文本框
                if (tableEl.classList.contains('body')) {
                    const trs = tableEl.querySelectorAll('tr');
                    const firstCols = trs.length ? trs[0].querySelectorAll('th, td').length : 0;
                    if (trs.length <= 2 && firstCols <= 1) return true;
                }
                return false;
            };

            candidateWrappers.forEach((wrapper, wIdx) => {
                const tableEls = wrapper.tagName === 'TABLE' ? [wrapper] : Array.from(wrapper.querySelectorAll('table'));
                if (!tableEls.length) return;

                // 2. 多维度高精表题定位（适配各出版商）
                let captionText = '';
                const ftHdr = wrapper.querySelector('.ArticleTable__header');
                const ftDesc = wrapper.querySelector('.ArticleTable__description');
                if (ftHdr || ftDesc) {
                    captionText = [(ftHdr ? ftHdr.innerText : ''), (ftDesc ? ftDesc.innerText : '')]
                        .map(s => (s || '').trim()).filter(Boolean).join('. ');
                }
                if (!captionText) {
                    const apsHdr = wrapper.querySelector('header.article-fulltext-label-header');
                    const apsP = wrapper.querySelector('p.article-fulltext-paragraph');
                    if (apsHdr && apsP) {
                        captionText = [(apsHdr.innerText || ''), (apsP.innerText || '')]
                            .map(s => (s || '').trim()).filter(Boolean).join(' ');
                    }
                }
                const capEl = !captionText && wrapper.querySelector('span.captions, div.captions, header.u-mb-16, .c-article-table__figcaption, .html-caption, header.article-table-caption, header.article-fulltext-label-header, figcaption, caption, .table-caption, .c-article-table__caption, .caption, .table-wrap-title, .table-title, .table-header, .table-intro, .html-table-title, .entry-table-title, header.article-table-header');
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
                const footEl = wrapper.querySelector('div.c-article-table-footer, .html-table_foot, div.article-section__table-footnotes, .ArticleTable__foot, div.notes, dl.footnotes, .legend, .tbl-foot, .table-wrap-foot, .table-footnotes, .c-article-table__notes, .c-article-table__notes-list, .table-foot, .table-footnote, .table-note, .table__foot, .html-table-footnote, tfoot');
                if (footEl) {
                    footnoteText = (footEl.innerText || footEl.textContent || '').trim();
                }

                tableEls.forEach((tableEl) => {
                    if (!tableEl || processedTables.has(tableEl) || isNoiseTable(tableEl, wrapper)) return;
                    processedTables.add(tableEl);

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
    Attempts to auto-dismiss GDPR/cookie consent banners (OneTrust, Springer, Nature, Wiley, T&F, etc.).
    Clicks common "Accept All" / "I Agree" buttons so the actual page content is visible.
    Safe to call even if no banner is present.
    """
    CONSENT_SELECTORS = [
        # OneTrust (Springer, Nature, ScienceDirect, Wiley, Taylor & Francis, GSW)
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
        "[id*='accept-all']",
        "[id*='acceptAll']",
        # Springer / Nature / Cookiebot / Generic patterns
        ".c-button--primary[data-test='accept-cookie']",
        "button[data-test='accept-cookie']",
        "button[data-cc-action='accept']",
        "[data-cc='accept-all']",
        "button[data-testid='accept-all']",
        "button[aria-label*='accept all' i]",
        "button[aria-label*='agree' i]",
        ".cc-btn.cc-allow",
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button:has-text('Accept all cookies')",
        "button:has-text('Accept All Cookies')",
        "button:has-text('Accept All')",
        "button:has-text('Accept cookies')",
        "button:has-text('I Accept')",
    ]
    for sel in CONSENT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=400):
                btn.click()
                page.wait_for_timeout(600)
                print(f"  [同意按钮] 已自动点击同意按钮: {sel}")
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
    3. 模拟点击 .link-reveal.link-table 等懒加载触发器与展开全部数据按钮。
    """
    try:
        clicked_lazy = page.evaluate("""
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

                // 2. 模拟点击 Silverchair 'View Large' / 'Open in Viewer' / '.link-reveal.link-table' 展开按钮
                let clicks = 0;
                const expandBtns = document.querySelectorAll(
                    'button[class*="table-expand"], a[class*="view-large"], .table-modal-trigger, .table-view-all, .link-reveal.link-table'
                );
                expandBtns.forEach(b => {
                    try { b.click(); clicks++; } catch(e) {}
                });
                return clicks;
            }
        """)
        if clicked_lazy:
            page.wait_for_timeout(1500)
        print("[GeoScienceWorld 适配] 已成功激活 Silverchair/GSW 深度展开与无损渲染")
    except Exception:
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
        rows_data = page.evaluate("""
        (frag) => {
            if (!frag) return null;
            // Find anchor element safely without CSS selector interpolation
            const anchor = document.getElementById(frag) ||
                           Array.from(document.querySelectorAll('[data-table]')).find(e => e.getAttribute('data-table') === frag) ||
                           Array.from(document.querySelectorAll('[id]')).find(e => e.getAttribute('id') === frag);
            if (!anchor) return null;

            // Search for a table: first inside the anchor, then walking up siblings
            let tableEl = anchor.querySelector('table');
            if (!tableEl) {
                // Walk up to parent, then look at siblings and their descendants
                let parent = anchor.parentElement;
                for (let depth = 0; depth < 5 && parent && !tableEl; depth++) {
                    tableEl = parent.querySelector('table');
                    parent = parent.parentElement;
                }
            }
            if (!tableEl) {
                // Try next sibling elements
                let sibling = anchor.nextElementSibling;
                for (let i = 0; i < 5 && sibling && !tableEl; i++) {
                    if (sibling.tagName === 'TABLE') tableEl = sibling;
                    else tableEl = sibling.querySelector('table');
                    sibling = sibling.nextElementSibling;
                }
            }
            if (!tableEl) return null;

            // Extract caption/title
            let caption = '';
            const cap = tableEl.querySelector('caption');
            if (cap) caption = cap.innerText.trim();
            if (!caption) {
                // Try previous sibling heading
                let prev = tableEl.previousElementSibling || anchor;
                for (let i = 0; i < 4 && prev; i++) {
                    const txt = (prev.innerText || prev.textContent || '').trim();
                    if (txt && (txt.toLowerCase().includes('table') || txt.includes('表'))) {
                        caption = txt;
                        break;
                    }
                    prev = prev.previousElementSibling;
                }
            }

            // Extract rows with rowspan/colspan expansion
            const grid = [];
            const occupied = {};
            const mark = (r, c) => { if (!occupied[r]) occupied[r] = {}; occupied[r][c] = true; };
            const nextFreeCol = (r, startC) => { let c = startC; while (occupied[r] && occupied[r][c]) c++; return c; };
            tableEl.querySelectorAll('tr').forEach((tr, r) => {
                if (!grid[r]) grid[r] = {};
                let col = 0;
                tr.querySelectorAll('td, th').forEach(cell => {
                    col = nextFreeCol(r, col);
                    const rs = parseInt(cell.getAttribute('rowspan') || '1', 10);
                    const cs = parseInt(cell.getAttribute('colspan') || '1', 10);
                    const text = (cell.innerText || cell.textContent || '').trim().replace(/\\s+/g, ' ');
                    for (let dr = 0; dr < rs; dr++) for (let dc = 0; dc < cs; dc++) {
                        if (!grid[r+dr]) grid[r+dr] = {};
                        grid[r+dr][col+dc] = text; mark(r+dr, col+dc);
                    }
                    col += cs;
                });
            });
            const nRows = grid.length;
            let nCols = 0;
            for (let r = 0; r < nRows; r++) if (grid[r]) { const mx = Math.max(...Object.keys(grid[r]).map(Number)); if (mx+1 > nCols) nCols = mx+1; }
            const rows = [];
            for (let r = 0; r < nRows; r++) {
                const row = [];
                for (let c = 0; c < nCols; c++) row.push((grid[r] && grid[r][c] !== undefined) ? grid[r][c] : '');
                if (row.some(v => v.trim())) rows.push(row);
            }

            return { caption, rows };
        }
        """, fragment)

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
    【已弃用并防御性安全加固】
    原本用于向临时目录拷贝用户配置，但 Chrome 运行时对 Cookies 等 SQLite 数据库施加排他锁，
    极易导致 'database is locked' 崩溃。
    现在优先推荐使用 CDP DevToolsActivePort 内存直连；若调用此函数，则自动跳过
    已被锁定的 SQLite 文件，保障进程健壮性。
    """
    import shutil
    import tempfile

    try:
        tmp = tempfile.mkdtemp(prefix="pw_chrome_profile_")
        default_src = os.path.join(src_user_data, "Default")
        default_dst = os.path.join(tmp, "Default")
        os.makedirs(default_dst, exist_ok=True)

        essential_files = [
            "Preferences", "Secure Preferences",
            "Local State",
        ]
        local_state_src = os.path.join(src_user_data, "Local State")
        if os.path.exists(local_state_src):
            try:
                shutil.copy2(local_state_src, os.path.join(tmp, "Local State"))
            except Exception:
                pass

        for fname in essential_files:
            src_f = os.path.join(default_src, fname)
            if os.path.exists(src_f):
                try:
                    shutil.copy2(src_f, os.path.join(default_dst, fname))
                except Exception:
                    pass

        # 安全跳过活跃的 SQLite 数据库文件（如 Cookies, Web Data），防止 database is locked
        print(f"[通用自动化] 防御性安全隔离配置目录已就绪: {tmp}")
        return tmp
    except Exception as e:
        print(f"[通用自动化] 隔离配置创建告警: {e}")
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
    cookie_files = []
    if user_data_dir:
        cookies_json = os.path.join(user_data_dir, "cookies.json")
        if os.path.exists(cookies_json):
            cookie_files.append(cookies_json)
    try:
        from .. import common as _common_mod
    except ImportError:
        try:
            import common as _common_mod
        except ImportError:
            _common_mod = None
    if _common_mod and callable(getattr(_common_mod, "get_companion_cookie_paths", None)):
        for cp in _common_mod.get_companion_cookie_paths():
            if cp not in cookie_files:
                cookie_files.append(cp)

    for cookies_json in cookie_files:
        try:
            import json
            with open(cookies_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
            cookies = data.get("cookies", []) if isinstance(data, dict) else data
            if isinstance(cookies, list) and cookies:
                for c in cookies:
                    try:
                        context.add_cookies([c])
                    except Exception:
                        pass
                print(f"[通用自动化] 从 {os.path.basename(cookies_json)} 成功注入并载入了 {len(cookies)} 个会话 Cookies。")
        except Exception as e:
            print(f"[通用自动化] 载入 {os.path.basename(cookies_json)} 失败: {e}")

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
    import subprocess
    import platform
    import tempfile
    import time
    import os

    try:
        from .cdp_detector import find_active_cdp_endpoint
    except ImportError:
        try:
            from cdp_detector import find_active_cdp_endpoint
        except ImportError:
            find_active_cdp_endpoint = None

    if find_active_cdp_endpoint:
        existing = find_active_cdp_endpoint(timeout=0.8)
        if existing:
            print(f"[通用自动化] 检测到已有活动 CDP 调试端点: {existing['cdp_url']} (来源: {existing.get('source')})，直接复用。")
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
    os.makedirs(profile_dir, exist_ok=True)

    print(f"[通用自动化] 自动启动调试版 Chrome: {chrome_path}")
    print(f"[通用自动化] 调试会话独立隔离用户目录: {profile_dir} (零 SQLite 拷贝)")

    cmd = [
        chrome_path,
        "--remote-debugging-port=9222",
        f"--user-data-dir={profile_dir}"
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(0.5)
            if find_active_cdp_endpoint:
                endpoint = find_active_cdp_endpoint(timeout=0.5, user_data_dir=profile_dir)
                if endpoint:
                    print(f"[通用自动化] 调试版 Chrome 启动成功，CDP 端点已就绪: {endpoint['cdp_url']}！")
                    return True
            else:
                try:
                    import socket
                    s = socket.socket()
                    s.settimeout(0.5)
                    s.connect(('127.0.0.1', 9222))
                    s.close()
                    print("[通用自动化] 调试版 Chrome 启动并成功启用 9222 端口！")
                    return True
                except Exception:
                    pass
    except Exception as e:
        print(f"[通用自动化] 自动启动 Chrome 失败: {e}")

    return False


_GATED_PUBLISHER_DOMAINS = (
    "sciencedirect.com", "onlinelibrary.wiley.com", "tandfonline.com",
    "mdpi.com", "geoscienceworld.org", "iopscience.iop.org", "science.org",
    "springer.com", "nature.com", "frontiersin.org", "journals.aps.org", "link.aps.org",
)


def _launch_context_or_browser(p, user_data_dir, headless, exec_path):
    """Launch persistent or ephemeral Playwright context, preferring real Chrome/Edge when headed."""
    launch_variants = []
    if headless is False:
        launch_variants.append({"channel": "chrome"})
        launch_variants.append({"channel": "msedge"})
    if exec_path:
        launch_variants.append({"executable_path": exec_path})
    else:
        launch_variants.append({})

    if user_data_dir and os.path.isdir(user_data_dir):
        for extra in launch_variants:
            try:
                kwargs = {
                    "headless": headless,
                    "args": ["--profile-directory=Default"] + _STEALTH_ARGS,
                    "ignore_default_args": ["--enable-automation"],
                    "viewport": {"width": 1280, "height": 900},
                }
                kwargs.update(extra)
                # Preserve native Chrome/Edge UA when launching everyday browser channel
                if "channel" not in extra:
                    kwargs["user_agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                context = p.chromium.launch_persistent_context(user_data_dir, **kwargs)
                return None, context
            except Exception as e:
                if "channel" in extra:
                    continue
                print(f"[通用自动化] Chrome 用户数据目录已被排他锁定 ({type(e).__name__})。")
                print("[通用自动化] 为保护本地数据库完整性并规避 SQLite 锁死，平滑降级为独立反检测隔离实例。")
                user_data_dir = None

    for extra in launch_variants:
        try:
            launch_kwargs = {
                "headless": headless,
                "args": _STEALTH_ARGS,
                "ignore_default_args": ["--enable-automation"],
            }
            launch_kwargs.update(extra)
            browser = p.chromium.launch(**launch_kwargs)
            ctx_kwargs = {"viewport": {"width": 1280, "height": 900}, "locale": "en-US"}
            if "channel" not in extra:
                ctx_kwargs["user_agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            context = browser.new_context(**ctx_kwargs)
            return browser, context
        except Exception:
            if "channel" in extra:
                continue
            raise
    return None, None


def _stealth_fallback_extract(url, table_idx="all"):
    """Run headed StealthyFetcher (with Turnstile solver) or BrowserSkill outside sync_playwright() loop."""
    try:
        from .strategies import extract_tables_via_scrapling, parse_tables_from_html
    except ImportError:
        from strategies import extract_tables_via_scrapling, parse_tables_from_html

    try:
        print(f"[通用自动化] 启动 StealthyFetcher 有头反拦截兜底: {url}")
        dfs = extract_tables_via_scrapling(url, table_idx=table_idx, force_headed=True) or []
        if dfs:
            return dfs
    except Exception as e:
        print(f"[通用自动化] StealthyFetcher 兜底失败: {e}")

    try:
        try:
            from .cdp_detector import try_fetch_html_via_browserskill
        except ImportError:
            from cdp_detector import try_fetch_html_via_browserskill
        bsk_html, _st, _fu = try_fetch_html_via_browserskill(url, timeout=30.0)
        if bsk_html:
            return parse_tables_from_html(bsk_html, table_idx) or []
    except Exception:
        pass
    return []


def _download_tandfonline_csvs(page, context, url_no_fragment):
    """Extract Taylor & Francis tables via DOM downloadTable links (T0001/t0001) and .tableView captions."""
    import io
    import tempfile

    print("[通用自动化] 检测到 Taylor & Francis 出版商。启用 Direct CSV 下载管道...")
    # 1. Discover exact downloadTable links and captions from DOM (handles both T0001 and t0001)
    dom_links = []
    try:
        dom_links = page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href*="downloadTable"]')).map((a, idx) => {
                const tv = a.closest('.tableView, div[class*="table"]');
                let cap = '';
                if (tv) {
                    const c = tv.querySelector('.tableCaption, p, .caption');
                    cap = c ? (c.innerText || c.textContent || '').trim() : (tv.innerText || '').split('Download CSV')[0].trim();
                }
                return { href: a.getAttribute('href') || '', caption: cap, idx: idx + 1 };
            })
        """) or []
    except Exception:
        dom_links = []

    candidates = []
    seen_urls = set()
    for item in dom_links:
        href = item.get("href") or ""
        if href:
            abs_dl = urllib.parse.urljoin(page.url or url_no_fragment, href)
            if abs_dl not in seen_urls:
                seen_urls.add(abs_dl)
                candidates.append((item["idx"], abs_dl, item.get("caption") or f"Table {item['idx']}"))

    if not candidates:
        doi_match = re.search(r'/doi/(?:full|abs|pdf)/(10\.\d{4,9}/[a-zA-Z0-9./\-_()]+)', url_no_fragment, re.IGNORECASE)
        if doi_match:
            tf_doi = doi_match.group(1)
            print(f"[通用自动化] 解析出 Taylor & Francis DOI: {tf_doi}")
            for idx in range(1, 21):
                for prefix_ch in ("T", "t"):
                    table_id = f"{prefix_ch}{idx:04d}"
                    dl_u = f"https://www.tandfonline.com/action/downloadTable?id={table_id}&doi={tf_doi}&downloadType=CSV"
                    candidates.append((idx, dl_u, f"Table {idx}"))

    dfs = []
    consecutive_fails = 0
    tried_idx_success = set()
    for idx, download_url, cap_title in candidates:
        if dom_links == [] and idx in tried_idx_success:
            continue
        csv_text = ""
        try:
            resp = context.request.get(download_url, timeout=15000)
            if resp.status == 200:
                body = resp.text()
                if body and not is_anti_bot_blocked("", body) and not body.lstrip().startswith("<"):
                    csv_text = body
        except Exception:
            pass

        if not csv_text:
            dl_page = context.new_page()
            temp_csv_path = None
            def handle_dl(download_obj):
                nonlocal temp_csv_path
                temp_csv_path = os.path.join(tempfile.gettempdir(), f"tf_table_{idx}_{os.getpid()}.csv")
                try:
                    download_obj.save_as(temp_csv_path)
                except Exception:
                    pass
            dl_page.on("download", handle_dl)
            try:
                dl_page.goto(download_url, wait_until="domcontentloaded", timeout=20000)
                dl_page.wait_for_timeout(2500)
            except Exception:
                pass
            finally:
                dl_page.close()
            if temp_csv_path and os.path.exists(temp_csv_path):
                try:
                    with open(temp_csv_path, "r", encoding="utf-8", errors="ignore") as f:
                        csv_text = f.read()
                finally:
                    try:
                        os.remove(temp_csv_path)
                    except Exception:
                        pass

        if csv_text and not csv_text.lstrip().startswith(("<!DOCTYPE", "<html", "<HTML")) and not is_anti_bot_blocked("", csv_text):
            try:
                df = pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)
                if not df.empty and len(df.columns) >= 1:
                    df.columns = [clean_text(c) for c in df.columns]
                    for col_i in range(len(df.columns)):
                        df.iloc[:, col_i] = df.iloc[:, col_i].astype(str).apply(clean_text)
                    df.attrs['table_title'] = clean_text(cap_title) or f"Table {idx}"
                    lbl = extract_table_label_from_title(df.attrs['table_title'])
                    if lbl:
                        df.attrs['label'] = lbl
                    dfs.append(df)
                    tried_idx_success.add(idx)
                    consecutive_fails = 0
                    print(f"     ✓ 成功下载并解析 {df.attrs['table_title']} (Shape: {df.shape[0]}x{df.shape[1]})")
                    continue
            except Exception as e:
                print(f"     · 解析 CSV 失败: {e}")
        consecutive_fails += 1
        if not dom_links and consecutive_fails >= 4:
            print(f"[通用自动化] 连续 {consecutive_fails} 次下载失败或结束，停止 T&F 直连下载。")
            break
    return dfs


def _extract_copernicus_xml_tables(page, table_idx="all"):
    """Fetch JATS XML linked from Copernicus article pages and parse tables."""
    try:
        from .strategies import _parse_jats_xml_tables
    except ImportError:
        from strategies import _parse_jats_xml_tables
    try:
        xml_href = page.evaluate("""
            () => {
                const a = document.querySelector('a[href$=".xml"]');
                if (a) return a.getAttribute('href') || '';
                const m = document.querySelector('meta[name="citation_xml_url"]');
                return m ? (m.getAttribute('content') || '') : '';
            }
        """)
        if xml_href:
            xml_url = urllib.parse.urljoin(page.url, xml_href)
            resp = page.request.fetch(xml_url, timeout=15000)
            if resp.status == 200:
                xml_text = resp.text()
                if "<table-wrap" in xml_text:
                    return _parse_jats_xml_tables(xml_text, table_idx=table_idx) or []
    except Exception as e:
        print(f"[通用自动化] Copernicus JATS XML 提取警告: {e}")
    return []


def extract_general_html_tables(url, table_idx="all", headed=False, user_data_dir=None, api_key=None, _allow_headed_fallback=True):
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
    was_blocked = False
    _effective_profile = user_data_dir

    # Prefer configured OS-specific persistent profile (with cookies.json), then real Chrome user data
    if not user_data_dir:
        try:
            _cfg_prof = load_config().get("PLAYWRIGHT_USER_DATA_DIR")
            if _cfg_prof and os.path.isdir(_cfg_prof):
                user_data_dir = _cfg_prof
                _effective_profile = _cfg_prof
        except Exception:
            pass
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

        # Strategy 0: Dynamic CDP Auto-discovery (DevToolsActivePort + 9222 fallback)
        connected_via_cdp = False
        skip_navigation = False
        cdp_port = None
        cdp_endpoint = None
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
            try:
                from cdp_detector import (
                    find_active_cdp_endpoint,
                    find_matching_cdp_page,
                    get_publisher_table_wait_selector,
                    install_cdp_port_guard,
                    install_slow_tracker_route_blocker,
                    wait_through_transient_challenge,
                )
            except ImportError:
                find_active_cdp_endpoint = None
                find_matching_cdp_page = None
                get_publisher_table_wait_selector = lambda _u: "table"
                install_cdp_port_guard = None
                install_slow_tracker_route_blocker = None
                wait_through_transient_challenge = None

        if find_active_cdp_endpoint:
            cfg = None
            try:
                from . import load_config
                cfg = load_config()
            except Exception:
                try:
                    import common
                    cfg = common.load_config()
                except Exception:
                    cfg = None
            cdp_endpoint = find_active_cdp_endpoint(timeout=1.5, user_data_dir=user_data_dir, config=cfg)
        else:
            try:
                with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1.2) as response:
                    if response.status == 200:
                        cdp_endpoint = {"cdp_url": "http://127.0.0.1:9222", "port": 9222, "source": "default_9222"}
            except Exception:
                cdp_endpoint = None

        if cdp_endpoint:
            try:
                cdp_url = cdp_endpoint["cdp_url"]
                cdp_port = cdp_endpoint.get("port")
                src = cdp_endpoint.get("source", "active")
                print(f"[通用自动化] 检测到活动 CDP 调试端点 ({cdp_url}，来源: {src})。正在进行内存直连...")
                browser = p.chromium.connect_over_cdp(cdp_url)

                # Try to find an existing tab that matches the target URL, DOI, PII, or meta citation_doi
                target_page = find_matching_cdp_page(browser, url) if find_matching_cdp_page else None
                if not target_page:
                    for context in browser.contexts:
                        for p_instance in context.pages:
                            p_url = p_instance.url
                            if p_url and (p_url.rstrip('/') == url.rstrip('/') or (len(url) > 20 and url in p_url)):
                                target_page = p_instance
                                break
                        if target_page:
                            break
                if target_page:
                    print(f"[通用自动化] 发现已打开的目标页面标签页: {target_page.url}，将直接复用！")

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
                print("[通用自动化] CDP 内存直连成功！零磁盘损耗继承当前 Chrome 登录态（校园网/CARSI 等）。")
                if install_cdp_port_guard:
                    if context:
                        install_cdp_port_guard(context, cdp_port=cdp_port)
                    install_cdp_port_guard(page, cdp_port=cdp_port)
                    print(f"[通用自动化] CDP Port Guard 已激活，主动拦截网页对本地调试端口 ({cdp_port}) 的扫描。")
            except Exception as e:
                print(f"[通用自动化] CDP 内存直连失败: {e}")

        if user_data_dir and not os.path.isdir(user_data_dir):
            print(f"[通用自动化] 警告: 指定的用户数据目录不存在 ({user_data_dir})，降级为新建环境。")
            user_data_dir = None

        if not connected_via_cdp:
            browser, context = _launch_context_or_browser(p, user_data_dir, headless, exec_path)
            if browser is not None:
                user_data_dir = None
            page = context.pages[0] if context.pages else context.new_page()
            page.add_init_script(_STEALTH_INIT_SCRIPT)
            if user_data_dir:
                _inject_cookies_from_profile(context, user_data_dir)
            if install_cdp_port_guard:
                install_cdp_port_guard(context)
                install_cdp_port_guard(page)


        # 拦截 mailto:, tel: 等外部系统协议，防止 macOS 弹出系统 Mail.app 邮件应用
        try:
            page.route(
                lambda u: any(u.lower().startswith(p) for p in ["mailto:", "tel:", "sms:", "itms:", "macappstore:"]),
                lambda route: route.abort()
            )
        except Exception:
            pass
        if install_slow_tracker_route_blocker:
            install_slow_tracker_route_blocker(page, url_no_fragment)

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

                # ── Akamai Bot Manager meta-refresh interstitial (e.g. MDPI ?bm-verify=...) ──
                try:
                    has_bm = page.evaluate("""() => {
                        const m = document.querySelector('meta[http-equiv="refresh" i]');
                        return Boolean(m && (m.getAttribute('content') || '').includes('bm-verify'));
                    }""")
                    if has_bm:
                        print("[通用自动化] 检测到 Akamai bm-verify 跳转挑战，等待自动重定向完成...")
                        try:
                            page.wait_for_url(lambda u: "bm-verify=" in u, timeout=9000)
                            page.wait_for_load_state("domcontentloaded", timeout=15000)
                            page.wait_for_timeout(1500)
                        except Exception:
                            page.wait_for_timeout(6500)
                except Exception:
                    pass

                # ── Elsevier linkinghub meta-refresh / crasolve / Turnstile / F5 /tspd/ / AIP loading 有界等待 ──
                if wait_through_transient_challenge:
                    wait_through_transient_challenge(page, max_wait_s=8.0)

                # ── CNKI Abstract -> HTML阅读 link follow-up (after chndoi.org resolution) ──
                if "cnki.net" in (page.url or "").lower():
                    try:
                        html_btn = page.locator("a:has-text('HTML阅读')").first
                        if html_btn.count() > 0 and html_btn.is_visible(timeout=2000):
                            print("[通用自动化] 在知网详情页发现 'HTML阅读' 链接，正在打开正文...")
                            try:
                                with context.expect_page(timeout=8000) as new_pg_info:
                                    html_btn.click()
                                reader_pg = new_pg_info.value
                                reader_pg.wait_for_load_state("domcontentloaded", timeout=30000)
                                reader_pg.wait_for_timeout(2500)
                                page = reader_pg
                            except Exception:
                                html_href = html_btn.get_attribute("href")
                                if html_href:
                                    page.goto(urllib.parse.urljoin(page.url, html_href), wait_until="domcontentloaded", timeout=30000)
                                    page.wait_for_timeout(2500)
                    except Exception:
                        pass

                # Try to wait for publisher-specific table containers/tables
                try:
                    page.wait_for_selector(get_publisher_table_wait_selector(page.url or url_no_fragment), timeout=8000)
                except Exception:
                    pass
                # If fragment target, scroll to anchor element to trigger lazy-loading
                if fragment:
                    try:
                        page.evaluate("""(frag) => {
                            if (!frag) return;
                            const el = document.getElementById(frag) ||
                                       Array.from(document.querySelectorAll('[data-table]')).find(e => e.getAttribute('data-table') === frag);
                            if (el) el.scrollIntoView({behavior: 'instant', block: 'center'});
                        }""", fragment)
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
        try:
            _body_sample = page.evaluate("document.body ? document.body.innerText.slice(0, 4000) : ''")
        except Exception:
            _body_sample = ""
        was_blocked = is_anti_bot_blocked(page_title, _body_sample)
        if was_blocked and headed:
            print(f"[通用自动化] 有头模式检测到反爬验证页面 ({page_title})，等待 3.5 秒观察自动验证/Cookies 生效...")
            page.wait_for_timeout(3500)
            _dismiss_cookie_consent(page)
            page_title = page.title()
            try:
                _body_sample = page.evaluate("document.body ? document.body.innerText.slice(0, 4000) : ''")
            except Exception:
                _body_sample = ""
            was_blocked = is_anti_bot_blocked(page_title, _body_sample)

        # ── Special Taylor & Francis Direct CSV Download Pipeline ──
        if "tandfonline.com" in (page.url or url_no_fragment) and not was_blocked:
            all_dfs.extend(_download_tandfonline_csvs(page, context, url_no_fragment))
        if not all_dfs and "copernicus.org" in (page.url or url_no_fragment):
            all_dfs.extend(_extract_copernicus_xml_tables(page, table_idx=table_idx))
        # ── NEW: prioritize anchor-targeted table for Springer/Nature inline tables (including iframes) ──
        if fragment and target_table_num is not None:
            anchor_dfs = _extract_anchor_table(page, fragment, target_table_num)
            if not anchor_dfs:
                for frame in page.frames:
                    try:
                        anchor_dfs = _extract_anchor_table(frame, fragment, target_table_num)
                        if anchor_dfs:
                            break
                    except Exception:
                        pass
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

    # Headless -> Headed everyday Chrome -> StealthyFetcher Turnstile escalation (outside sync_playwright)
    _is_gated_domain = any(dom in url.lower() for dom in _GATED_PUBLISHER_DOMAINS)
    if not all_dfs and _allow_headed_fallback and (was_blocked or (headless and _is_gated_domain)):
        if headless:
            print(f"[通用自动化] 无头模式{'被反爬拦截' if was_blocked else '未发现表格'}，自动切换日常有头 Chrome 浏览器重试 (自动接受 Cookies)...")
            headed_dfs, headed_title = extract_general_html_tables(
                url,
                table_idx=table_idx,
                headed=True,
                user_data_dir=_effective_profile or user_data_dir,
                api_key=api_key,
                _allow_headed_fallback=False,
            )
            if headed_dfs:
                return headed_dfs, headed_title or page_title
            if headed_title and not is_anti_bot_blocked(headed_title):
                page_title = headed_title

        stealth_dfs = _stealth_fallback_extract(url, table_idx=table_idx)
        if stealth_dfs:
            all_dfs = stealth_dfs
            if is_anti_bot_blocked(page_title):
                page_title = ""

    # NOTE: We do NOT filter final DataFrames by fragment/anchor number.
    # All tables from all subpages are returned regardless of which #TabN was in the URL.
    # The caller (extract_zotero_table.py) can further filter using --table-idx if needed.

    print(f"[通用自动化] 成功提取到 {len(all_dfs)} 张表格数据。")
    return all_dfs, page_title
