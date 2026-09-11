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
import pandas as pd

try:
    from curl_cffi import requests as requests_impersonate
except ImportError:
    requests_impersonate = None

try:
    from ..playwright_utils import find_playwright_chromium
except ImportError:
    try:
        from playwright_utils import find_playwright_chromium
    except ImportError:
        find_playwright_chromium = lambda: None

try:
    from .. import common
except ImportError:
    import common


# Aliases to shared helpers (kept as bare names so call sites are unchanged)
get_cnki_cookies_path = common.get_cnki_cookies_path
get_crawl4ai_cookies_path = common.get_crawl4ai_cookies_path
load_config = common.load_config
load_cookies_from_file = common.load_cookies_from_file


def _rows_to_dataframe(rows_data):
    """
    将 [[cell,...],...] 二维列表转为 DataFrame，自动将首行提升为表头。
    修复：旧版直接 pd.DataFrame(rows_data) 不传 columns，导致 [0,1,2,...] 整数列名
    被 to_excel(header=True) 写入第一行，真实表头被下推到第二行（影响 121 个表）。

    判据：若首行含至少 1 个非数字单元格（字段名），则将其作为 columns，
    其余行作为 data；否则保持默认整数列名（由 postprocess 后续提升）。

    新增修复：
    1. 检测首行为纯序号行 [0,1,2,...] 或 [1,2,3,...] 时，跳过首行，从第二行取表头。
    2. 单列挤压检测：若仅 1 列且单元格含 Tab/多空格分隔符，尝试重新分列。
    """
    if not rows_data:
        return pd.DataFrame()
    # 对齐各行列数
    max_cols = max(len(r) for r in rows_data)
    for r in rows_data:
        if len(r) < max_cols:
            r.extend([""] * (max_cols - len(r)))

    # 检测首行是否为纯序号行 [0,1,2,...] 或 ['',1,2,3,...]
    first_row = [str(c).strip() if c is not None else "" for c in rows_data[0]]
    numeric_first = sum(1 for c in first_row if c and c.replace('.', '').replace('-', '').isdigit())
    is_index_row = (numeric_first >= max(2, len(first_row) * 0.7) and
                    all(c == '' or c.replace('.', '').replace('-', '').isdigit() for c in first_row))

    if is_index_row and len(rows_data) >= 2:
        # 跳过序号行，从第二行取表头
        second_row = [str(c).strip() if c is not None else "" for c in rows_data[1]]
        non_numeric_2 = sum(1 for c in second_row if c and not c.replace('.', '').replace('-', '').replace(',', '').replace('%', '').replace('+', '').isdigit())
        if len(second_row) > 0 and non_numeric_2 >= max(1, len(second_row) // 3) and len(rows_data) >= 3:
            return pd.DataFrame(rows_data[2:], columns=second_row)
        return pd.DataFrame(rows_data[1:])

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
        from playwright.sync_api import sync_playwright
        import json
        
        cookies_path = get_cnki_cookies_path()
        print("\n==================================================")
        print("  正在启动知网 Cookie 自动配置向导...")
        print("  1. 程序将为您打开一个可见的浏览器窗口。")
        print("  2. 请在网页中登录您的知网账号，或完成您的机构/校园网授权。")
        print("  3. 确认登录/授权成功后，直接【关闭浏览器窗口】即可。")
        print("  程序会自动获取并保存您的登录 Cookies 到本地用户文件夹下。")
        print("==================================================\n")
        
        with sync_playwright() as p:
            # Launch headed browser
            exec_path = find_playwright_chromium()
            if exec_path:
                print(f"[Playwright] 使用自动识别的 Chromium 路径: {exec_path}")
            browser = p.chromium.launch(headless=False, executable_path=exec_path)
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


def parse_tables_from_html(html_content, table_idx="all"):
    """
    Parses tables from raw HTML using pandas.read_html and extracts captions directly preceding them.

    修复：不再因检测到摘要页就整体跳过——有些期刊摘要页确实嵌入了数据表。
    改为逐表过滤：依赖 is_metadata_table() 过滤文献信息框/摘要版权框/目录大纲/参考文献列表等非数据表，
    仅当所有表都被过滤掉时才返回空。
    """
    try:
        # Wrap literal HTML in StringIO to avoid deprecation warnings
        dfs = pd.read_html(io.StringIO(html_content))
        if not dfs:
            return []
            
        # Split html_content by <table to find preceding text blocks
        parts = html_content.split('<table')
        
        valid_dfs = []
        for idx, df in enumerate(dfs):
            if df.empty or len(df.columns) < 2 or len(df) < 1:
                continue
            # Basic cleanup of columns
            df.columns = [clean_text(c) for c in df.columns]
            for col in df.columns:
                df[col] = df[col].astype(str).apply(clean_text)
                
            # Extract caption from preceding text block
            caption_text = ""
            label_text = ""
            if idx < len(parts):
                preceding_text = parts[idx]
                lines = preceding_text.split('\n')
                for line in reversed(lines[-15:]):
                    line_clean = clean_text(line).strip()
                    # Strip HTML tags
                    line_no_html = re.sub(r'<[^>]+>', '', line_clean).strip()
                    # Strip leading markdown/formatting chars
                    line_stripped = re.sub(r'^[#*_\-\s\(\)\:\.\,\»]+', '', line_no_html).strip()
                    # Robust pattern matching for spaced spelling like T A B L E
                    pattern = r'^(T\s*A\s*B\s*L\s*E\s*A\s*U\s*X?|T\s*A\s*B\s*L\s*E|T\s*A\s*B\s*\.|表)(?:\s*S\s*)?\s*(\d+|[ivxlcdm]+|ni|rn|[a-z])'
                    match = re.match(pattern, line_stripped, re.IGNORECASE)
                    if match:
                        caption_text = line_stripped
                        tbl_word = match.group(1).upper()
                        tbl_num = match.group(2)
                        if "表" in tbl_word:
                            label_text = f"表 {tbl_num}"
                        else:
                            label_text = f"Table {tbl_num}"
                        break
                        
            if label_text:
                df.attrs['label'] = label_text
            if caption_text:
                df.attrs['table_title'] = caption_text
                
            valid_dfs.append(df)
            
        if not valid_dfs:
            return []
            
        if table_idx == "all":
            return valid_dfs
        elif table_idx == "first":
            return [valid_dfs[0]]
        else:
            try:
                idx = int(table_idx)
                if idx < len(valid_dfs):
                    return [valid_dfs[idx]]
                else:
                    return valid_dfs
            except ValueError:
                return valid_dfs
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
    
    def process_table_and_add(t_lines, prec_lines):
        try:
            processed_lines = []
            for r in t_lines:
                if '---' in r or '-|-' in r:
                    continue
                parts = [p.strip() for p in r.split('|')[1:-1]]
                processed_lines.append('\t'.join(parts))
                
            csv_data = '\n'.join(processed_lines)
            df = pd.read_csv(io.StringIO(csv_data), sep='\t')
            if not df.empty and len(df.columns) >= 2:
                df.columns = [clean_text(c) for c in df.columns]
                for col in df.columns:
                    df[col] = df[col].astype(str).apply(clean_text)
                    
                caption_text = ""
                label_text = ""
                for prec_line in reversed(prec_lines[-15:]):
                    prec_clean = prec_line.strip()
                    # Strip leading markdown formatting characters
                    prec_clean_stripped = re.sub(r'^[#*_\-\s\(\)\:\.\,\»]+', '', prec_clean).strip()
                    # Robust pattern matching for spaced spelling like T A B L E
                    pattern = r'^(T\s*A\s*B\s*L\s*E\s*A\s*U\s*X?|T\s*A\s*B\s*L\s*E|T\s*A\s*B\s*\.|表)(?:\s*S\s*)?\s*(\d+|[ivxlcdm]+|ni|rn|[a-z])'
                    match = re.match(pattern, prec_clean_stripped, re.IGNORECASE)
                    if match:
                        caption_text = prec_clean_stripped
                        tbl_word = match.group(1).upper()
                        tbl_num = match.group(2)
                        if "表" in tbl_word:
                            label_text = f"表 {tbl_num}"
                        else:
                            label_text = f"Table {tbl_num}"
                        break
                        
                if label_text:
                    df.attrs['label'] = label_text
                if caption_text:
                    df.attrs['table_title'] = caption_text
                tables_found.append(df)
        except Exception:
            pass

    for line in lines:
        line_strip = line.strip()
        if line_strip.startswith('|') and line_strip.endswith('|'):
            current_table.append(line_strip)
        else:
            if current_table:
                if len(current_table) >= 3:
                    process_table_and_add(current_table, preceding_lines)
                current_table = []
            preceding_lines.append(line)
            
    if current_table and len(current_table) >= 3:
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
        # Load Elsevier Key dynamically and inject into environment for paper-fetch-skill
        config = load_config()
        els_key = config.get("ELSEVIER_API_KEY")
        if els_key:
            os.environ["ELSEVIER_API_KEY"] = els_key
            
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


def extract_tables_via_elsevier_api(doi, table_idx="all"):
    """
    Extracts tables from Elsevier articles using the Elsevier Full-Text Retrieval API.
    Always uses the DOI endpoint (/content/article/doi/{DOI}) which has proper auth.
    Returns XOCS XML containing <ce:table> elements — no browser needed.
    Requires ELSEVIER_API_KEY in config.json or environment.
    """
    try:
        config = load_config()
        els_key = config.get("ELSEVIER_API_KEY") or os.environ.get("ELSEVIER_API_KEY", "")
        if not els_key:
            return []

        api_url = f"https://api.elsevier.com/content/article/doi/{doi}"
        headers = {"X-ELS-APIKey": els_key, "Accept": "text/xml"}
        print(f"[Elsevier API] \u6b63\u5728\u83b7\u53d6 XOCS XML: {doi}")
        resp = requests.get(api_url, headers=headers, timeout=20)
        if resp.status_code != 200:
            print(f"[Elsevier API] \u8bf7\u6c42\u5931\u8d25: HTTP {resp.status_code}")
            return []
        
        tables = _parse_elsevier_xocs_tables(resp.text, table_idx)
        tables.extend(extract_elsevier_supplementary_tables(doi, els_key))
        return tables
    except Exception as e:
        print(f"[Elsevier API] \u63d0\u53d6\u5931\u8d25: {e}")
        return []


def extract_elsevier_supplementary_tables(doi, api_key):
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
    if not api_key:
        return supplementary_dfs

    try:
        # Step 1: Fetch attachment metadata using DOI
        api_url = f"https://api.elsevier.com/content/object/doi/{doi}"
        headers = {
            "X-ELS-APIKey": api_key,
            "Accept": "application/json"
        }
        params = {
            "view": "META"
        }
        print(f"[Elsevier API] Fetching supplementary metadata for DOI: {doi}")
        resp = requests.get(api_url, headers=headers, params=params, timeout=20)
        if resp.status_code != 200:
            print(f"[Elsevier API] Supplementary metadata request failed: HTTP {resp.status_code}")
            return supplementary_dfs
            
        data = resp.json()
        attachments = data.get("attachment-metadata-response", {}).get("attachment", [])
        if not attachments:
            print(f"[Elsevier API] No attachments found for DOI: {doi}")
            return supplementary_dfs

        print(f"[Elsevier API] Found {len(attachments)} total attachments. Filtering for non-image files...")
        
        for att in attachments:
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
            dl_resp = requests.get(dl_url, headers={"X-ELS-APIKey": api_key}, timeout=30)
            if dl_resp.status_code != 200:
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
                            tbl_label = f"Supplementary Table {filename_no_ext}_{s_name}" if len(excel_data) > 1 else f"Supplementary Table {filename_no_ext}"
                            df.attrs['label'] = tbl_label
                            df.attrs['table_title'] = f"Supplementary Table from sheet '{s_name}' of file '{filename}'"
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
                        tbl_list = page.find_tables().tables
                        for t_idx, table in enumerate(tbl_list):
                            rows = table.extract()
                            if rows and len(rows) >= 3 and len(rows[0]) >= 2:
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


def extract_general_supplementary_tables(url):
    """
    Calls the journal-supp-downloader script to download supplementary files,
    then parses tables from them in-memory or from temp files.
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

    supplementary_dfs = []
    
    # Locate the journal_downloader.py script in the workspace
    skill_dir = common.get_skill_root()
    skills_root = os.path.dirname(skill_dir)
    downloader_script = os.path.join(skills_root, "journal-supp-downloader", "scripts", "journal_downloader.py")
    
    if not os.path.exists(downloader_script):
        print(f"[在线提取] 找不到 journal-supp-downloader 技能脚本: {downloader_script}")
        return supplementary_dfs
        
    temp_dir = tempfile.mkdtemp(prefix="supp_dl_")
    try:
        print(f"[在线提取] 调用 journal-supp-downloader 脚本获取补充文件: {url}")
        # Run the downloader script
        cmd = [sys.executable, downloader_script, url, "-o", temp_dir]
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        # Search the temp_dir recursively for downloaded files
        downloaded_files = []
        for root_dir, dirs, files in os.walk(temp_dir):
            for file in files:
                downloaded_files.append(os.path.join(root_dir, file))
                
        if not downloaded_files:
            print("[在线提取] 未能通过 journal-supp-downloader 下载到任何补充文件。")
            return supplementary_dfs
            
        print(f"[在线提取] 成功下载 {len(downloaded_files)} 个补充文件。开始解析表格...")
        for filepath in downloaded_files:
            filename = os.path.basename(filepath)
            filename_no_ext = os.path.splitext(filename)[0]
            
            # Read file bytes
            try:
                with open(filepath, "rb") as f:
                    content_bytes = f.read()
            except Exception as e:
                print(f"  -> 无法读取文件 {filename}: {e}")
                continue
                
            # Parse based on file type
            # Excel
            if filename.lower().endswith((".xlsx", ".xls")):
                try:
                    excel_data = pd.read_excel(io.BytesIO(content_bytes), sheet_name=None)
                    print(f"  -> 成功解析 Excel 补充文件 {filename}。")
                    for sheet_name, df in excel_data.items():
                        if not df.empty:
                            df.attrs['label'] = f"Supplementary Table {filename_no_ext}_{sheet_name}"
                            df.attrs['table_title'] = f"Supplementary Table from sheet '{sheet_name}' in file '{filename}'"
                            supplementary_dfs.append(df)
                except Exception as ex:
                    print(f"  -> 解析 Excel 失败 {filename}: {ex}")
                    
            # Word docx
            elif filename.lower().endswith(".docx"):
                try:
                    with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
                        xml_content = z.read("word/document.xml")
                    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                    root = ET.fromstring(xml_content)
                    tables = root.findall('.//w:tbl', ns)
                    print(f"  -> 成功解析 Word 补充文件 {filename}。发现 {len(tables)} 张表格。")
                    for t_idx, tbl in enumerate(tables):
                        rows_data = []
                        for row in tbl.findall('.//w:tr', ns):
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
                    print(f"  -> 解析 Word 失败 {filename}: {ex}")
                    
            # PDF
            elif filename.lower().endswith(".pdf"):
                if fitz is None:
                    continue
                try:
                    pdf_doc = fitz.open(stream=content_bytes, filetype="pdf")
                    print(f"  -> 成功解析 PDF 补充文件 {filename}。")
                    for p_idx in range(len(pdf_doc)):
                        page = pdf_doc[p_idx]
                        try:
                            tbl_finder = page.find_tables(strategy="text")
                            tbl_list = tbl_finder.tables if tbl_finder else []
                        except Exception:
                            tbl_list = []
                        for t_idx, table in enumerate(tbl_list):
                            rows = table.extract()
                            if rows and len(rows) >= 2:
                                df = _rows_to_dataframe(rows)
                                tbl_label = f"Supplementary Table {filename_no_ext}_P{p_idx+1}" if len(tbl_list) == 1 else f"Supplementary Table {filename_no_ext}_P{p_idx+1}_T{t_idx+1}"
                                df.attrs['label'] = tbl_label
                                df.attrs['table_title'] = f"Supplementary Table from page {p_idx+1} of file '{filename}'"
                                supplementary_dfs.append(df)
                except Exception as ex:
                    print(f"  -> 解析 PDF 失败 {filename}: {ex}")
    except Exception as e:
        print(f"[在线提取] 调用 journal-supp-downloader 遇到异常: {e}")
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass
            
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
        thead_rows_count = len(thead.findall('.//row')) if thead is not None else 0
        if thead_rows_count == 0:
            thead_rows_count = 1
            
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


def extract_tables_via_direct_http(url, table_idx="all", cancel_event=None):
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
        text = None
        status_code = None
        
        # Try curl_cffi first to mimic real Chrome browser TLS signatures
        try:
            from curl_cffi import requests as crequests
            print(f"尝试本地直接 HTTP 请求网页 (机构 IP 直连 + TLS Chrome 模拟): {url} ...")
            resp = crequests.get(url, impersonate="chrome110", timeout=12, allow_redirects=True)
            if cancel_event is not None and cancel_event.is_set():
                return []
            text = resp.text
            status_code = resp.status_code
        except ImportError:
            # Fallback to standard requests with realistic browser headers
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "max-age=0",
                "Upgrade-Insecure-Requests": "1"
            }
            print(f"尝试本地直接 HTTP 请求网页 (机构 IP 直连 + 标头伪装): {url} ...")
            resp = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
            text = resp.text
            status_code = resp.status_code
            
        if status_code == 200 and text:
            # Basic check for Cloudflare or access errors
            if "cf-browser-verification" in text or "cf_styles" in text or "Access Denied" in text or "Access denied" in text or "Forbidden" in text:
                print("本地直接 HTTP 请求被 Cloudflare 或 Access Denied 阻止。")
                return []
                
            dfs = parse_tables_from_html(text, table_idx)
            if dfs:
                print(f"本地直接 HTTP 提取成功，解析到 {len(dfs)} 个表格。")
                return dfs
            else:
                print("本地直接 HTTP 提取未解析到任何表格。")
        else:
            print(f"本地直接 HTTP 请求返回状态码: {status_code}")
    except Exception as e:
        print(f"本地直接 HTTP 请求发生异常: {e}")
    return []


# ---------------------------------------------------------------------------
# Firecrawl（云端 API）共享状态：进程内 memo、磁盘缓存、402 熔断
# ---------------------------------------------------------------------------

_FIRECRAWL_MEMO = {}
_FIRECRAWL_MEMO_LOCK = threading.Lock()
_FIRECRAWL_DISABLED = False  # 402（额度耗尽）时置 True，本次进程内禁用该渠道


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


def _firecrawl_tables_to_dfs(tables):
    """Firecrawl JSON 结构化抽取结果 -> DataFrame 列表。"""
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
        df.attrs['label'] = clean_text(t.get("table_label") or f"Table {idx+1}")
        df.attrs['table_title'] = clean_text(t.get("table_title") or t.get("table_label") or "")
        dfs.append(df)
    return dfs


def _firecrawl_parse_response(data, table_idx):
    """
    从 Firecrawl 单次抓取的响应数据（含 json/html/markdown 多格式）中级联解析表格。
    复杂表（HTML 含 colspan/rowspan 合并单元格）优先走原始 HTML 解析，
    避免 LLM 扁平化导致多级表头错位。
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
    has_complex = "<table" in html_l and ("colspan" in html_l or "rowspan" in html_l)
    order = ("html", "json", "markdown") if has_complex else ("json", "html", "markdown")

    last_dfs = []
    for kind in order:
        dfs = []
        if kind == "json":
            if json_tables:
                dfs = _firecrawl_tables_to_dfs(json_tables)
        elif kind == "html":
            if "<table" in html_l:
                dfs = parse_tables_from_html(html, "all")
        else:
            if md:
                if "<table" in md.lower():
                    dfs = parse_tables_from_html(md, "all")
                if not dfs:
                    dfs = parse_tables_from_markdown(md, "all")
        if dfs:
            last_dfs = dfs
        selected = _select_tables(dfs, table_idx)
        if selected:
            print(f"[Firecrawl] 单次抓取 {kind} 格式解析成功，提取到 {len(selected)} 个表格。")
            return selected
    if last_dfs:
        print(f"[Firecrawl] 请求索引 {table_idx} 越界，兜底返回全部 {len(last_dfs)} 个表格。")
    return last_dfs


def extract_tables_via_firecrawl(url, api_key=None, table_idx="all", cancel_event=None):
    """
    Firecrawl Scrape API v2 —— 单次请求多格式（json + html + markdown）抓取。

    相对旧版三次串行 scrape 的优化：
    - 单次 scrape 同时返回结构化 JSON / 原始 HTML / Markdown，本地级联解析，
      费用与延迟均降至旧版的约 1/3；
    - maxAge 云端缓存（FIRECRAWL_MAX_AGE_MS）+ 本地磁盘缓存（默认 24h TTL），
      重复 URL 近零成本；
    - per-URL 进程内 memo：竞速管线内 Unpaywall / Tier-2 / tail race 不重复扣费；
    - 402 熔断：额度耗尽后本次运行内禁用 Firecrawl 渠道；
    - 429 按 Retry-After 退避重试一次；
    - cancel_event：外层竞速已分出胜负时不再发起付费请求。
    """
    global _FIRECRAWL_DISABLED
    if _FIRECRAWL_DISABLED:
        print("[Firecrawl] 已熔断（额度耗尽），跳过。")
        return []
    if cancel_event is not None and cancel_event.is_set():
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

    # 1) 本地磁盘缓存（24h TTL）
    cached = _firecrawl_read_cache(url)
    if cached is not None:
        return _finish(_firecrawl_parse_response(cached, table_idx))

    # Table JSON Schema for Structured Extraction
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

    # 单次请求同时获取三种格式，本地级联解析（不再为同一 URL 重复付费渲染）
    payload = {
        "url": url,
        "formats": [
            {"type": "json", "schema": table_schema},
            "html",
            "markdown"
        ],
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
            return _finish(_firecrawl_parse_response(payload_data, table_idx))

        if response.status_code == 402:
            print("[Firecrawl] 云端额度耗尽（402），本次运行内禁用 Firecrawl 渠道。")
            _FIRECRAWL_DISABLED = True
            break

        if response.status_code == 429 and attempt == 0:
            retry_after = response.headers.get("Retry-After")
            try:
                wait_s = min(float(retry_after), 30.0) if retry_after else 5.0
            except (TypeError, ValueError):
                wait_s = 5.0
            print(f"[Firecrawl] 触发限流（429），{wait_s:.0f}s 后重试一次...")
            time.sleep(wait_s)
            continue

        print(f"[Firecrawl] 请求失败，状态码: {response.status_code}")
        break

    return _finish([])


def extract_tables_via_scrapling(url, table_idx="all"):
    """
    Invokes Scrapling StealthyFetcher.
    """
    try:
        from scrapling import StealthyFetcher
        print(f"Querying Scrapling StealthyFetcher for {url}...")
        fetcher = StealthyFetcher()
        res = fetcher.fetch(url)
        html = res.html_content
        if html:
            dfs = parse_tables_from_html(html, table_idx)
            if dfs:
                return dfs
    except Exception as e:
        print(f"Scrapling query skipped: {e}")
    return []


def extract_tables_via_playwright(url, table_idx="all"):
    """
    Invokes Playwright directly.
    """
    try:
        from playwright.sync_api import sync_playwright
        print(f"Querying Playwright browser for {url}...")
        with sync_playwright() as p:
            exec_path = find_playwright_chromium()
            if exec_path:
                print(f"[Playwright] 使用自动识别的 Chromium 路径: {exec_path}")
            browser = p.chromium.launch(headless=True, executable_path=exec_path)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page.goto(url, wait_until="networkidle", timeout=30000)
            try:
                page.wait_for_selector("table", timeout=5000)
            except Exception:
                pass
            html = page.content()
            browser.close()
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


def extract_cnki_tables_via_scrapling(url, table_idx="all", cookies_path=None):
    """
    Fetches CNKI page using Scrapling StealthyFetcher with optional cookies.
    """
    try:
        from scrapling import StealthyFetcher
        print(f"CNKI Scrapling: Loading {url}...")
        fetcher = StealthyFetcher()
        
        extra_kwargs = {}
        if cookies_path and os.path.exists(cookies_path):
            cookies = load_cookies_from_file(cookies_path)
            if cookies:
                try:
                    extra_kwargs["cookies"] = cookies
                    print(f"CNKI Scrapling: Loaded {len(cookies)} cookies from {cookies_path}")
                except Exception as e:
                    print(f"CNKI Scrapling: Failed to apply cookies: {e}")
                    
        res = fetcher.fetch(url, **extra_kwargs)
        html = res.html_content
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
    确定 Crawl4AI 是否通过 CDP 接管技能已有的调试 Chrome（9222 端口）。
    config CRAWL4AI_CDP_URL 显式指定优先；为空时自动探测 127.0.0.1:9222
    （每进程仅探测一次）。返回 None 表示自启独立 stealth 浏览器。
    """
    config = load_config()
    explicit = (config.get("CRAWL4AI_CDP_URL") or "").strip()
    if explicit:
        return explicit
    if _CRAWL4AI_CDP_CACHE["checked"]:
        return _CRAWL4AI_CDP_CACHE["url"]
    probe = None
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=0.8) as resp:
            if resp.status == 200:
                probe = "http://127.0.0.1:9222"
    except Exception:
        probe = None
    _CRAWL4AI_CDP_CACHE["checked"] = True
    _CRAWL4AI_CDP_CACHE["url"] = probe
    if probe:
        print("[Crawl4AI] 检测到 9222 调试 Chrome，将经 CDP 复用该浏览器（免启动、继承登录态）。")
    return probe


def _filter_kwargs(cls, kwargs):
    """按目标类的签名/字段过滤 kwargs，兼容不同 crawl4ai 版本的参数差异。"""
    accepted = set()
    try:
        import inspect
        accepted |= set(inspect.signature(cls.__init__).parameters) - {"self"}
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
    - 优先经 CDP 接管技能已有的 9222 调试 Chrome（CRAWL4AI_CDP_URL，默认自动探测）：
      零启动开销、直接继承日常登录态；探测不到则回退自启 stealth 浏览器；
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

    config = load_config()
    try:
        page_timeout_ms = int(config.get("CRAWL4AI_PAGE_TIMEOUT_MS") or 45000)
    except (TypeError, ValueError):
        page_timeout_ms = 45000
    cdp_url = _crawl4ai_cdp_url()

    sem = _crawl4ai_semaphore()
    # CDP 模式与 Playwright 桥接共用同一个 9222 调试 Chrome：纳入共享浏览器锁
    # （线程锁 + 跨进程文件锁），避免与 Playwright 渠道并发驱动同一浏览器。
    lock_ctx = common.browser_extraction_lock("browser_extraction") if cdp_url else None
    if lock_ctx is not None:
        lock_ctx.__enter__()
    sem.acquire()
    try:
        if cancel_event is not None and cancel_event.is_set():
            return []

        # CDP 模式复用真实浏览器（自带登录态），无需注入 Cookie；
        # 自启浏览器模式按目标域名过滤注入已保存的 Cookie。
        cookies_list = None
        if not cdp_url:
            cookies_path = get_crawl4ai_cookies_path()
            if os.path.exists(cookies_path):
                try:
                    with open(cookies_path, "r", encoding="utf-8") as f:
                        raw_cookies = json.load(f)
                    if isinstance(raw_cookies, list):
                        valid_keys = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
                        cookies_list = []
                        for c in raw_cookies:
                            if isinstance(c, dict):
                                cleaned_c = {k: c[k] for k in valid_keys if k in c}
                                cookies_list.append(cleaned_c)
                        cookies_list = _cookies_for_domain(cookies_list, url)
                    print(f"[Crawl4AI] Loaded {len(cookies_list or [])} domain-matched cookies from: {cookies_path}")
                except Exception as e:
                    print(f"[Crawl4AI Warning] Failed to load cookies: {e}")

        async def _crawl():
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

            browser_kwargs = {
                "headless": cdp_url is None,
                "enable_stealth": cdp_url is None,
            }
            if cdp_url:
                # 接管技能已有的调试 Chrome（跨进程复用，替代不可行的进程池）
                browser_kwargs.update({
                    "use_managed_browser": True,
                    "browser_mode": "cdp",  # 新版参数名；旧版本由签名过滤丢弃
                    "cdp_url": cdp_url,
                })
            elif cookies_list:
                browser_kwargs["cookies"] = cookies_list
            browser_config = BrowserConfig(**_filter_kwargs(BrowserConfig, browser_kwargs))

            run_kwargs = {
                "cache_mode": CacheMode.BYPASS,
                "page_timeout": page_timeout_ms,
                # 表格出现立即返回；无表页面加载完成 3s 后也不再空等
                "wait_for": "js:() => document.querySelectorAll('table').length > 0 || (document.readyState === 'complete' && performance.now() > 3000)",
            }
            run_config = CrawlerRunConfig(**_filter_kwargs(CrawlerRunConfig, run_kwargs))

            async with AsyncWebCrawler(config=browser_config) as crawler:
                task = asyncio.ensure_future(crawler.arun(url=url, config=run_config))
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        task.cancel()
                        print("[Crawl4AI] 收到取消信号，已中止抓取。")
                        return None, None, "cancel"
                    done, _pending = await asyncio.wait({task}, timeout=0.5)
                    if done:
                        break
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    return None, None, "cancel"
                if result.success:
                    md_obj = result.markdown
                    if md_obj is not None and not isinstance(md_obj, str):
                        # crawl4ai >= 0.5 返回 MarkdownGenerationResult 对象
                        md_text = getattr(md_obj, "raw_markdown", None) or getattr(md_obj, "fit_markdown", None) or str(md_obj)
                    else:
                        md_text = md_obj
                    return result.html, md_text, "ok"
                print(f"[Crawl4AI] Crawl failed: {result.error_message}")
                return None, None, "fail"

        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is not None:
                import nest_asyncio
                nest_asyncio.apply()
                html, markdown, status = running_loop.run_until_complete(_crawl())
            else:
                html, markdown, status = asyncio.run(_crawl())
        except Exception as e:
            print(f"[Crawl4AI Error] Exception during crawl: {e}")
            _crawl4ai_record(url, hard_failure=True)
            return []

        if status == "fail":
            _crawl4ai_record(url, hard_failure=True)
            return []

        if html:
            # 抓取成功（无论是否解析到表格）都清零该域失败计数
            _crawl4ai_record(url, hard_failure=False)
            dfs = parse_tables_from_html(html, table_idx)
            if dfs:
                print(f"[Crawl4AI] Successfully extracted {len(dfs)} tables from HTML.")
                return dfs

            if markdown:
                dfs = parse_tables_from_markdown(markdown, table_idx)
                if dfs:
                    print(f"[Crawl4AI] Successfully extracted {len(dfs)} tables from Markdown.")
                    return dfs
        return []
    finally:
        sem.release()
        if lock_ctx is not None:
            lock_ctx.__exit__(None, None, None)


def extract_tables_via_unpaywall(doi, table_idx="all", cancel_event=None):
    """
    Queries the Unpaywall API for a free full-text version of a DOI,
    then tries to extract HTML tables from that open-access URL.
    No API key required.  Covers PMC, Europe PMC, bioRxiv, repositories, etc.
    """
    if cancel_event is not None and cancel_event.is_set():
        return []
    try:
        oa_api = f"https://api.unpaywall.org/v2/{doi}?email=zotero-table-extractor@local"
        resp = requests.get(oa_api, timeout=10)
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

        # Try Firecrawl on OA URL
        config = load_config()
        key = config.get("FIRECRAWL_API_KEY") or os.environ.get("FIRECRAWL_API_KEY")
        if key:
            dfs = extract_tables_via_firecrawl(best_url, key, table_idx, cancel_event=cancel_event)
            if dfs:
                print(f"[Unpaywall] \u901a\u8fc7 Firecrawl \u4ece\u5f00\u653e\u7248\u672c\u63d0\u53d6\u5230 {len(dfs)} \u5f20\u8868\u683c\u3002")
                return dfs

        # Direct HTTP parse
        try:
            r = requests.get(best_url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ZoteroTableExtractor/1.0)"
            })
            if r.status_code == 200:
                dfs = parse_tables_from_html(r.text, table_idx)
                if dfs:
                    print(f"[Unpaywall] \u901a\u8fc7\u76f4\u63a5 HTTP \u4ece\u5f00\u653e\u7248\u672c\u63d0\u53d6\u5230 {len(dfs)} \u5f20\u8868\u683c\u3002")
                    return dfs
        except Exception as e:
            print(f"[Unpaywall] \u76f4\u63a5 HTTP \u83b7\u53d6\u5931\u8d25: {e}")

    except Exception as e:
        print(f"[Unpaywall] \u67e5\u8be2\u5931\u8d25: {e}")

    return []
