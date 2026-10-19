"""online.doi_resolver — DOI 解析与知网 URL 定位（原 online_utils.py 拆分）。"""

import os
import re
import sqlite3
import urllib.parse
import urllib.request
import uuid
import requests

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
load_config = common.load_config

def get_doi_from_zotero_db(pdf_path, db_path=None):
    """
    Attempts to query Zotero SQLite database to retrieve the DOI, Title, and URL of a PDF.
    """
    if db_path is None:
        try:
            from system_detector import get_zotero_db_path
            db_path = get_zotero_db_path()
        except ImportError:
            try:
                from ..system_detector import get_zotero_db_path
                db_path = get_zotero_db_path()
            except ImportError:
                cfg = load_config() if callable(load_config) else {}
                db_path = cfg.get("ZOTERO_DB_PATH") or os.path.join(os.path.expanduser('~'), 'Zotero', 'zotero.sqlite')

    if not os.path.exists(db_path):
        print(f"Zotero DB not found at: {db_path}")
        return None, None, None
        
    temp_db_path = None
    conn = None
    try:
        # 优先使用 SQLite URI 只读不可变模式（mode=ro&immutable=1）：零磁盘拷贝、零文件锁冲突
        abs_db = os.path.abspath(db_path)
        uri = f"file:{urllib.request.pathname2url(abs_db)}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
    except Exception:
        # 回退至临时文件拷贝只读连接
        import shutil
        import tempfile
        temp_db_path = os.path.join(tempfile.gettempdir(), f'zotero_extractor_{os.getpid()}_{uuid.uuid4().hex[:6]}.sqlite')
        try:
            shutil.copy2(db_path, temp_db_path)
            conn = sqlite3.connect(temp_db_path)
        except Exception as e:
            print(f"Warning: Failed to copy Zotero DB: {e}")
            temp_db_path = db_path
            conn = sqlite3.connect(db_path)

    doi = None
    title = None
    url = None

    try:
        cursor = conn.cursor()
        parent_id = None
        
        # 1. Try to extract key from directory (e.g. storage/KEY/filename.pdf)
        norm_path = os.path.abspath(pdf_path).replace('\\', '/')
        parts = norm_path.split('/')
        if len(parts) >= 2:
            key_cand = parts[-2]
            if len(key_cand) == 8 and key_cand.isalnum():
                cursor.execute("""
                    SELECT ia.parentItemID 
                    FROM itemAttachments ia
                    JOIN items i ON ia.itemID = i.itemID
                    WHERE i.key = ?
                """, (key_cand.upper(),))
                row = cursor.fetchone()
                if row:
                    parent_id = row[0]
                    
        # 2. Try to search by filename if key matching didn't yield a parent_id
        if not parent_id:
            filename = os.path.basename(pdf_path)
            cursor.execute("""
                SELECT parentItemID 
                FROM itemAttachments 
                WHERE path LIKE ?
            """, (f'%{filename}',))
            row = cursor.fetchone()
            if row:
                parent_id = row[0]
                
        # 3. Retrieve DOI, title, and url from parent item
        if parent_id:
            cursor.execute("""
                SELECT f.fieldName, idv.value
                FROM itemData id
                JOIN fields f ON id.fieldID = f.fieldID
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                WHERE id.itemID = ? AND f.fieldName IN ('DOI', 'title', 'url')
            """, (parent_id,))
            rows = cursor.fetchall()
            for fieldName, value in rows:
                if fieldName == 'DOI':
                    doi = value.strip()
                elif fieldName == 'title':
                    # Strip HTML tags that Zotero stores in some titles
                    # e.g. "H<sub>2</sub>O-NaCl" → "H2O-NaCl"
                    import re as _re
                    title = _re.sub(r'<[^>]+>', '', value).strip()
                elif fieldName == 'url':
                    url = value.strip()
            if doi or title or url:
                print(f"Zotero DB match found: DOI={doi}, Title={title}, URL={url}")
    except Exception as e:
        print(f"Warning: Zotero DB lookup failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        if temp_db_path and temp_db_path != db_path and os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass

    if doi or title or url:
        return doi, title, url
    return None, None, None


def get_doi_from_pdf_content(pdf_path):
    """
    Fallback method using PyMuPDF (fitz) to extract DOI from PDF metadata/text.
    """
    try:
        import fitz
        with fitz.open(pdf_path) as doc:
            # 1. Check metadata
            if doc.metadata:
                for key in ['doi', 'DOI']:
                    val = doc.metadata.get(key)
                    if val:
                        val = val.strip()
                        if val.lower().startswith("doi:"):
                            val = val[4:].strip()
                        if re.match(r'^10\.\d{4,9}/', val):
                            print(f"DOI found in PDF metadata: {val}")
                            return val
                            
                # Check subject/description metadata for DOI
                subject = doc.metadata.get('subject') or ''
                doi_match = re.search(r'\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b', subject, re.IGNORECASE)
                if doi_match:
                    doi = doi_match.group(0)
                    print(f"DOI found in PDF subject metadata: {doi}")
                    return doi
                    
            # 2. Scan first few pages
            doi_pattern = re.compile(r'\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b', re.IGNORECASE)
            for i in range(min(5, len(doc))):
                text = doc[i].get_text()
                matches = doi_pattern.findall(text)
                if matches:
                    doi = matches[0].strip()
                    if doi.endswith('.') or doi.endswith(',') or doi.endswith(')'):
                        doi = doi[:-1]
                    print(f"DOI found in PDF text page {i+1}: {doi}")
                    return doi
    except Exception as e:
        print(f"Warning: Failed to extract DOI from PDF content: {e}")
    return None


def get_doi_by_title(title):
    """
    Queries Crossref API to find DOI by title and validates title similarity.
    """
    if not title or len(title) < 10:
        return None
    url = f"https://api.crossref.org/works?query.title={urllib.parse.quote(title)}&rows=1"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            items = data.get("message", {}).get("items", [])
            if items:
                best_match = items[0]
                ref_title = best_match.get("title", [""])[0]
                if ref_title:
                    # Compute Jaccard similarity (character-level for Chinese, word-level for English)
                    is_chinese = bool(re.search(r'[\u4e00-\u9fff]', title + ref_title))
                    t1_clean = re.sub(r'[^\w\s]', '', title.lower())
                    t2_clean = re.sub(r'[^\w\s]', '', ref_title.lower())
                    
                    if is_chinese:
                        s1 = set(t1_clean.replace(' ', ''))
                        s2 = set(t2_clean.replace(' ', ''))
                    else:
                        s1 = set(t1_clean.split())
                        s2 = set(t2_clean.split())
                        
                    similarity = 0.0
                    if s1 and s2:
                        similarity = len(s1.intersection(s2)) / len(s1.union(s2))
                        
                    if similarity >= 0.6:
                        doi = best_match.get("DOI")
                        print(f"DOI resolved from Crossref via title: {doi} (Similarity: {similarity:.2f})")
                        return doi
                    else:
                        print(f"Warning: Best Crossref match rejected due to low similarity ({similarity:.2f}): '{ref_title}'")
    except Exception as e:
        print(f"Warning: Crossref lookup failed: {e}")
    return None


def get_doi_by_title_firecrawl(title, api_key=None):
    """
    Queries Firecrawl Research Index API to find DOI by title and validates title similarity.
    """
    if not title or len(title) < 10:
        return None
    config = load_config()
    key = api_key or config.get("FIRECRAWL_API_KEY") or os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        return None
        
    print(f"Querying Firecrawl Research Index to resolve DOI for title: {title}...")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    params = {
        "query": title,
        "k": 5
    }
    try:
        response = requests.get("https://api.firecrawl.dev/v2/search/research/papers", params=params, headers=headers, timeout=15)
        if response.status_code == 200:
            data = response.json()
            papers = data.get("results", [])
            if not papers and isinstance(data, dict):
                papers = data.get("data", [])
                
            for paper in papers:
                ref_title = paper.get("title", "")
                if not ref_title:
                    continue
                
                # Jaccard title similarity check (matching Crossref method)
                is_chinese = bool(re.search(r'[\u4e00-\u9fff]', title + ref_title))
                t1_clean = re.sub(r'[^\w\s]', '', title.lower())
                t2_clean = re.sub(r'[^\w\s]', '', ref_title.lower())
                
                if is_chinese:
                    s1 = set(t1_clean.replace(' ', ''))
                    s2 = set(t2_clean.replace(' ', ''))
                else:
                    s1 = set(t1_clean.split())
                    s2 = set(t2_clean.split())
                    
                similarity = 0.0
                if s1 and s2:
                    similarity = len(s1.intersection(s2)) / len(s1.union(s2))
                    
                if similarity >= 0.6:
                    # 1. Try ids.doi list first
                    doi_list = paper.get("ids", {}).get("doi", [])
                    if isinstance(doi_list, list) and doi_list:
                        doi_val = doi_list[0]
                        if doi_val:
                            if doi_val.lower().startswith("doi:"):
                                doi_val = doi_val[4:].strip()
                            if re.match(r'^10\.\d{4,9}/', doi_val):
                                print(f"DOI resolved from Firecrawl Research Index ids.doi: {doi_val} (Similarity: {similarity:.2f})")
                                return doi_val
                                
                    # 2. Try primaryId and paperId as fallbacks
                    cand_ids = [paper.get("primaryId", ""), paper.get("paperId", "")]
                    for cid in cand_ids:
                        if not cid:
                            continue
                        if cid.lower().startswith("doi:"):
                            cid = cid[4:].strip()
                        if re.match(r'^10\.\d{4,9}/', cid):
                            print(f"DOI resolved from Firecrawl Research Index cand_ids: {cid} (Similarity: {similarity:.2f})")
                            return cid
            print("Firecrawl Research Index completed, but no high-similarity paper with a valid DOI was found.")
    except Exception as e:
        print(f"Warning: Firecrawl Research Index lookup failed: {e}")
    return None


def _resolve_pii_to_doi(pii):
    """
    Resolves a ScienceDirect PII to a DOI via the Elsevier metadata API.
    Falls back to CrossRef title search if metadata API is unavailable.
    Returns DOI string or None.
    """
    try:
        config = load_config()
        els_key = config.get("ELSEVIER_API_KEY") or os.environ.get("ELSEVIER_API_KEY", "")
        if els_key:
            # Use Elsevier abstract/metadata endpoint with field=doi
            url = f"https://api.elsevier.com/content/abstract/pii/{pii}?field=doi&httpAccept=application/json"
            r = requests.get(url, headers={"X-ELS-APIKey": els_key}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                doi_val = (data.get("abstracts-retrieval-response", {})
                              .get("coredata", {})
                              .get("prism:doi"))
                if doi_val:
                    print(f"[Elsevier API] PII {pii} \u89e3\u6790\u4e3a DOI: {doi_val}")
                    return doi_val
    except Exception as e:
        print(f"[Elsevier API] PII\u2192DOI \u89e3\u6790\u5931\u8d25: {e}")
    return None


def resolve_doi_canonical_url(doi, timeout=8):
    """
    通过 doi.org Handle API 一次请求获取 DOI 的 canonical（出版社落地）URL，
    避免跟踪 302 重定向链、避免下载整个落地页 HTML。

    主：GET https://doi.org/api/handles/{doi}（Handle REST API，JSON 中 type=URL 的值）。
    备：HEAD https://doi.org/{doi}（302 Location 头，零响应体）。
    失败返回 None（调用方回退到 doi.org 代理 URL + 旧的重定向探测）。
    """
    if not doi:
        return None
    # 1) Handle API
    try:
        r = requests.get(f"https://doi.org/api/handles/{doi}", timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            if data.get("responseCode") == 1:
                url_values = [
                    v for v in data.get("values", [])
                    if isinstance(v, dict) and v.get("type") == "URL"
                ]
                url_values.sort(key=lambda v: v.get("index", 999))
                for v in url_values:
                    val = v.get("data", {}).get("value")
                    if isinstance(val, str) and val.startswith("http"):
                        print(f"[DOI] Handle API 解析 canonical URL: {val}")
                        return val
    except Exception as e:
        print(f"[DOI] Handle API 解析失败（尝试 HEAD 兜底）: {e}")

    # 2) HEAD 302 Location 兜底
    try:
        r = requests.head(f"https://doi.org/{doi}", allow_redirects=False, timeout=timeout)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            if loc and loc.startswith("http"):
                print(f"[DOI] HEAD Location 解析 canonical URL: {loc}")
                return loc
    except Exception as e:
        print(f"[DOI] HEAD 解析失败: {e}")

    return None


def resolve_chinese_doi(url):
    """
    If the URL is a China DOI resolution page, parse it to extract the domestic (境内) CNKI link.
    """
    if "chndoi.org" in url or "chinadoi.cn" in url:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
            }
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                html = r.text
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, 'html.parser')
                
                # 1. 优先查找明确标注 (境内) 的列表项与链接
                for li in soup.find_all(['li', 'tr', 'td', 'div']):
                    li_text = li.get_text()
                    if '境内' in li_text:
                        a = li.find('a')
                        if a and a.get('href'):
                            cand = a.get('href').strip()
                            print(f"[在线提取] 成功从 China DOI 页面解析出境内知网链接: {cand}")
                            return cand
                            
                # 2. 匹配非 oversea 的 link.cnki.net
                for a in soup.find_all('a'):
                    href = a.get('href', '').strip()
                    if 'link.cnki.net' in href and 'oversea' not in href:
                        print(f"[在线提取] 成功从 China DOI 页面解析出境内知网链接: {href}")
                        return href
        except Exception as e:
            print(f"[在线提取] 解析 China DOI 页面失败: {e}")
    return url


def get_cnki_url_from_pdf_content(pdf_path):
    """
    Scans the PDF metadata and first few pages for CNKI URLs (e.g. link.cnki.net or kns.cnki.net).
    """
    try:
        import fitz
        with fitz.open(pdf_path) as doc:
            cnki_pattern = re.compile(r'https?://(?:link|kns)\.cnki\.net/[^\s\]\)\u4e00-\u9fa5]+', re.IGNORECASE)
            for i in range(min(5, len(doc))):
                text = doc[i].get_text()
                matches = cnki_pattern.findall(text)
                if matches:
                    url = matches[0].strip()
                    if url.endswith('.') or url.endswith(',') or url.endswith(')') or url.endswith(']'):
                        url = url[:-1]
                    print(f"[在线提取] 从 PDF 第 {i+1} 页文本中识别出知网链接: {url}")
                    return url
    except Exception as e:
        print(f"Warning: Failed to extract CNKI URL from PDF content: {e}")
    return None


def extract_arxiv_id(url_or_doi_or_str):
    """
    从 DOI、URL 或文本中解析出标准的 arXiv ID (例如 '2301.12345' 或 'math/0211159')。
    支持:
    - 10.48550/arXiv.2301.12345
    - https://arxiv.org/abs/2301.12345
    - https://arxiv.org/html/2301.12345v1
    - https://ar5iv.labs.arxiv.org/html/2301.12345
    - arXiv:2301.12345
    - 经典格式: math/0211159, cs/0112017, quant-ph/0501001
    """
    if not url_or_doi_or_str:
        return None
    s = str(url_or_doi_or_str).strip()

    # 1. DOI 格式: 10.48550/arXiv.YYMM.NNNNN
    m_doi = re.search(r'10\.48550/arXiv\.([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)', s, re.IGNORECASE)
    if m_doi:
        return m_doi.group(1)

    # 2. arXiv URL 格式 (abs, html, pdf)
    m_url = re.search(r'(?:arxiv\.org|ar5iv(?:\.labs)?\.arxiv\.org)/(?:abs|html|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?|[a-z\-]+/[0-9]{7}(?:v[0-9]+)?)', s, re.IGNORECASE)
    if m_url:
        val = m_url.group(1)
        if val.endswith('.pdf'):
            val = val[:-4]
        return val

    # 3. 显式 arXiv: 前缀
    m_prefix = re.search(r'\barXiv:\s*([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?|[a-z\-]+/[0-9]{7}(?:v[0-9]+)?)\b', s, re.IGNORECASE)
    if m_prefix:
        return m_prefix.group(1)

    # 4. 纯现代 arXiv ID (YYMM.NNNNN) 或 经典格式 (math/0211159)
    m_pure = re.match(r'^([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?|[a-z\-]+/[0-9]{7}(?:v[0-9]+)?)$', s, re.IGNORECASE)
    if m_pure:
        return m_pure.group(1)


    return None


def extract_pmcid(url_or_doi_or_str):
    """
    从字符串或 URL 中提取 PMCID (如 PMC1234567)。
    """
    if not url_or_doi_or_str:
        return None
    s = str(url_or_doi_or_str).strip()
    m = re.search(r'\b(PMC\d{5,9})\b', s, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def resolve_pmcid_from_doi(doi, timeout=8):
    """
    通过 NCBI PMC ID Converter API 将 DOI 解析为 PMCID。
    API: https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={doi}&format=json
    成功返回 'PMCxxxxxxx'，若不在 PMC 开放获取库中返回 None。
    """
    if not doi:
        return None
    clean_doi = doi.strip()
    if clean_doi.lower().startswith("doi:"):
        clean_doi = clean_doi[4:].strip()

    pmc_self = extract_pmcid(clean_doi)
    if pmc_self:
        return pmc_self

    url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={urllib.parse.quote(clean_doi)}&format=json&tool=zotero-table-extractor&email=extractor@zotero.org"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            records = data.get("records", [])
            for rec in records:
                pmcid = rec.get("pmcid")
                if pmcid and str(pmcid).upper().startswith("PMC"):
                    res = str(pmcid).strip().upper()
                    print(f"[NCBI PMC] DOI {clean_doi} 解析为 PMCID: {res}")
                    return res
    except Exception as e:
        print(f"[NCBI PMC] ID 转换接口调用失败: {e}")
    return None

