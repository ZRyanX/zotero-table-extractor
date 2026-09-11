"""online.graph — 提取图（DAG）编排与各渠道节点实现（原 online_utils.py 拆分）。

并行化说明：
  extract_tables_online(..., race=True) 默认走"分层竞速"管线：
    - Tier-1 纯 HTTP 渠道（DirectHTTP / Impersonated / ElsevierXML / Unpaywall /
      PaperFetch / AcademicSearch）并发竞速，首个成功者获胜；
    - Tier-2 独立浏览器/API 渠道（Crawl4AI / Firecrawl / Scrapling）并发竞速；
    - Tier-3 共享浏览器渠道（Playwright / BrowserAct）在 browser_extraction_lock
      全局锁下串行兜底。
  知网分支同理：ScraplingCNKI ∥ Crawl4AICNKI 竞速，PlaywrightCNKI 锁内兜底。
  race=False 时回退到原有的串行 DAG 编排，行为与旧版完全一致。
"""

import os
import re
import threading
import urllib.parse
import concurrent.futures
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
get_cnki_cookies_path = common.get_cnki_cookies_path
load_config = common.load_config
browser_extraction_lock = common.browser_extraction_lock

from .doi_resolver import (
    _resolve_pii_to_doi,
    get_cnki_url_from_pdf_content,
    get_doi_by_title,
    get_doi_by_title_firecrawl,
    get_doi_from_pdf_content,
    get_doi_from_zotero_db,
    resolve_chinese_doi,
    resolve_doi_canonical_url,
)

from .strategies import (
    extract_cnki_tables_via_playwright,
    extract_cnki_tables_via_scrapling,
    extract_general_supplementary_tables,
    extract_tables_via_crawl4ai,
    extract_tables_via_direct_http,
    extract_tables_via_elsevier_api,
    extract_tables_via_firecrawl,
    extract_tables_via_paper_fetch,
    extract_tables_via_scrapling,
    extract_tables_via_unpaywall,
    get_domain_profile_dir,
    parse_tables_from_html,
)

class ExtractionNode:
    def __init__(self, name, fallback_node=None):
        self.name = name
        self.fallback_node = fallback_node

    def execute(self, state):
        """
        Executes this node's scraping logic.
        Returns:
            (state, next_node_name)
        """
        raise NotImplementedError


class ExtractionGraph:
    def __init__(self):
        self.nodes = {}
        self.entry_point = None

    def add_node(self, name, node):
        self.nodes[name] = node

    def set_entry_point(self, name):
        self.entry_point = name

    def run(self, initial_state):
        state = initial_state.copy()
        state["errors"] = {}
        state["history"] = []
        
        current_node_name = self.entry_point
        while current_node_name and not state.get("dfs"):
            node = self.nodes.get(current_node_name)
            if not node:
                print(f"[Graph Error] Node {current_node_name} not found in graph.")
                break
                
            print(f"[Graph] Executing Node: {current_node_name}")
            state["history"].append(current_node_name)
            
            next_node_name = None
            try:
                state, next_node_name = node.execute(state)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"[Graph Warning] Node {current_node_name} failed: {e}")
                state["errors"][current_node_name] = str(e)
                next_node_name = node.fallback_node
                
            current_node_name = next_node_name
            
        return state


class ResolveMetadataNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("ResolveMetadataNode", fallback_node)

    def execute(self, state):
        pdf_path = state.get("pdf_path")
        db_path = state.get("db_path")
        api_key = state.get("api_key")
        
        # 0. Check if pdf_path is already a URL
        if isinstance(pdf_path, str) and (pdf_path.startswith("http://") or pdf_path.startswith("https://")):
            url = pdf_path
            is_cnki = "cnki.net" in url or "cnki.com.cn" in url
            doi = None
            if "doi.org/" in url:
                _doi_match = re.search(r'doi\.org/(10\.\d{4,9}/[-._;()/:A-Z0-9]+)', url, re.IGNORECASE)
                if _doi_match:
                    doi = _doi_match.group(1)
            state["url"] = url
            state["doi"] = doi
            state["is_cnki"] = is_cnki
            state["title"] = None
        else:
            # 幂等：state 已预注入 doi/title（如主入口查库命名结果）时，
            # 跳过重复的 DB/PDF 内容/标题搜索，只做后续 URL 解析。
            doi = state.get("doi")
            title = state.get("title")
            cnki_url = None
            zotero_url = None
            if not doi:
                cnki_url = get_cnki_url_from_pdf_content(pdf_path)
                res_db = get_doi_from_zotero_db(pdf_path, db_path)
                doi, title, zotero_url = res_db if res_db else (None, None, None)

                if not doi:
                    doi = get_doi_from_pdf_content(pdf_path)
                if not doi and title:
                    doi = get_doi_by_title(title)
                    if not doi:
                        doi = get_doi_by_title_firecrawl(title, api_key)

            state["doi"] = doi
            state["title"] = title
            
            url = None
            is_cnki = False
            
            # Prioritize direct Zotero URL if available (saves redirect timeouts)
            if zotero_url and zotero_url.startswith("http"):
                url = zotero_url
                if "cnki.net" in url or "cnki.com.cn" in url:
                    is_cnki = True
            elif cnki_url:
                is_cnki = True
                url = cnki_url
            elif doi:
                if "cnki" in doi.lower() or any(x in doi for x in ["10.16539", "10.18654", "10.3799", "10.13722", "10.16111"]):
                    is_cnki = True
                    chndoi_url = f"https://www.chndoi.org/Resolution/Handler?doi={doi}"
                    resolved = resolve_chinese_doi(chndoi_url)
                    if resolved != chndoi_url:
                        url = resolved
                    else:
                        url = f"https://doi.org/{doi}"
                else:
                    # Handle API 一次拿 canonical URL：免 302 重定向链、免落地页整页下载
                    canonical = resolve_doi_canonical_url(doi)
                    if canonical:
                        if "chndoi.org" in canonical or "chinadoi.cn" in canonical:
                            resolved = resolve_chinese_doi(canonical)
                            if "cnki.net" in resolved or "cnki.com.cn" in resolved:
                                is_cnki = True
                                url = resolved
                            else:
                                url = canonical
                        elif "cnki.net" in canonical or "cnki.com.cn" in canonical:
                            is_cnki = True
                            url = canonical
                        else:
                            url = canonical
                    else:
                        url = f"https://doi.org/{doi}"

                # Check for redirects to CNKI（仅当 URL 仍是 doi.org 代理形式时的兜底）
                if not is_cnki and url.startswith("https://doi.org/"):
                    try:
                        r = requests.get(url, allow_redirects=True, timeout=5)
                        final_url = r.url
                        if "chndoi.org" in final_url or "chinadoi.cn" in final_url:
                            resolved = resolve_chinese_doi(final_url)
                            if "cnki.net" in resolved or "cnki.com.cn" in resolved:
                                is_cnki = True
                                url = resolved
                        elif "cnki.net" in final_url or "cnki.com.cn" in final_url:
                            is_cnki = True
                            url = final_url
                    except Exception:
                        pass
                        
            if not url:
                print(f"[Graph] Could not resolve DOI or URL for PDF: {pdf_path}")
                return state, self.fallback_node
                
            state["url"] = url
            state["is_cnki"] = is_cnki
        
        print(f"[Graph] Resolved metadata: URL={state.get('url')}, DOI={state.get('doi')}, Title={state.get('title')}, is_cnki={state.get('is_cnki')}")
        
        if state["is_cnki"]:
            return state, "Crawl4AICNKINode"
        else:
            return state, "DirectHTTPNode"


class DirectHTTPNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("DirectHTTPNode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        
        dfs = extract_tables_via_direct_http(url, table_idx)
        if dfs:
            state["dfs"] = dfs
            return state, None
            
        doi = state.get("doi")
        _is_elsevier = (
            url and ("sciencedirect.com" in url or "elsevier.com" in url)
        ) or (
            doi and any(doi.lower().startswith(p) for p in [
                "10.1016/", "10.1006/", "10.1053/", "10.1067/", "10.1078/",
                "10.1383/", "10.1517/",
            ])
        )
        if _is_elsevier:
            return state, "ElsevierXMLNode"
        else:
            return state, self.fallback_node


class ImpersonatedHTTPNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("ImpersonatedHTTPNode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        
        if requests_impersonate is None:
            print("[ImpersonatedHTTPNode] curl_cffi is not installed. Bypassing node...")
            return state, self.fallback_node
            
        print(f"[ImpersonatedHTTPNode] Fetching page using TLS impersonation (Chrome): {url}")
        try:
            r = requests_impersonate.get(url, impersonate="chrome", timeout=15)
            if r.status_code == 200:
                html = r.text
                if html:
                    dfs = parse_tables_from_html(html, table_idx)
                    if dfs:
                        print(f"[ImpersonatedHTTPNode] Successfully extracted {len(dfs)} tables via TLS impersonation.")
                        state["dfs"] = dfs
                        return state, None
            else:
                print(f"[ImpersonatedHTTPNode] Request returned status code: {r.status_code}")
        except Exception as e:
            print(f"[ImpersonatedHTTPNode Warning] TLS impersonation request failed: {e}")
            
        return state, self.fallback_node


class ElsevierXMLNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("ElsevierXMLNode", fallback_node)

    def execute(self, state):
        doi = state.get("doi")
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        
        _els_doi = doi
        if not _els_doi and url:
            _pii_match = re.search(r'/pii/([A-Z0-9]+)', url, re.IGNORECASE)
            if _pii_match:
                _pii = _pii_match.group(1).upper()
                _els_doi = _resolve_pii_to_doi(_pii)
                
        if _els_doi:
            dfs = extract_tables_via_elsevier_api(_els_doi, table_idx)
            if dfs:
                state["dfs"] = dfs
                return state, None
                
        return state, self.fallback_node


class PlaywrightCNKINode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("PlaywrightCNKINode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        headed = state.get("headed", False)
        user_data_dir = state.get("user_data_dir")
        cookies_path = get_cnki_cookies_path()
        
        config = load_config()
        if not headed and config.get("PLAYWRIGHT_HEADED", False):
            headed = True
        if not user_data_dir:
            if config.get("PLAYWRIGHT_USER_DATA_DIR"):
                user_data_dir = config["PLAYWRIGHT_USER_DATA_DIR"]
            else:
                user_data_dir = get_domain_profile_dir(url)
            
        # 共享浏览器状态（调试端口 / user-data-dir）在并行模式下必须串行
        with browser_extraction_lock():
            res = extract_cnki_tables_via_playwright(url, table_idx, headed, user_data_dir, cookies_path)
        if isinstance(res, tuple):
            dfs, resolved_title = res
            if resolved_title:
                state["title"] = resolved_title
        else:
            dfs = res
            
        if dfs:
            state["dfs"] = dfs
            return state, None
            
        return state, self.fallback_node


class ScraplingCNKINode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("ScraplingCNKINode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        cookies_path = get_cnki_cookies_path()
        
        dfs = extract_cnki_tables_via_scrapling(url, table_idx, cookies_path)
        if dfs:
            state["dfs"] = dfs
            return state, None
            
        return state, self.fallback_node


class PlaywrightGeneralNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("PlaywrightGeneralNode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        headed = state.get("headed", False)
        user_data_dir = state.get("user_data_dir")
        api_key = state.get("api_key")
        
        config = load_config()
        if not headed and config.get("PLAYWRIGHT_HEADED", False):
            headed = True
        if not user_data_dir:
            if config.get("PLAYWRIGHT_USER_DATA_DIR"):
                user_data_dir = config["PLAYWRIGHT_USER_DATA_DIR"]
            else:
                user_data_dir = get_domain_profile_dir(url)
            
        from . import general_html_extractor
            
        # 共享浏览器状态（调试端口 9222 / user-data-dir）在并行模式下必须串行
        with browser_extraction_lock():
            res = general_html_extractor.extract_general_html_tables(url, table_idx, headed, user_data_dir, api_key)
        if isinstance(res, tuple):
            dfs, resolved_title = res
            _error_titles = {
                "access denied", "are you a robot", "sciencedirect", "just a moment",
                "performing security verification", "error", "403", "page not found",
                "cloudflare", "your privacy, your choice", "there was a problem",
            }
            if resolved_title and dfs:
                title_lower = resolved_title.lower().strip()
                if not any(err in title_lower for err in _error_titles):
                    state["title"] = resolved_title
        else:
            dfs = res
            
        if dfs:
            # Also attempt to download and parse general supplementary tables via journal-supp-downloader
            supp_dfs = extract_general_supplementary_tables(url)
            if supp_dfs:
                dfs.extend(supp_dfs)
                print(f"[在线提取] 成功获取并追加了 {len(supp_dfs)} 个补充表格。")
            state["dfs"] = dfs
            return state, None
            
        # If no main tables found, still try to extract supplementary tables alone
        supp_dfs = extract_general_supplementary_tables(url)
        if supp_dfs:
            state["dfs"] = supp_dfs
            print(f"[在线提取] 成功提取并载入 {len(supp_dfs)} 个补充表格。")
            return state, None
            
        return state, self.fallback_node


class ScraplingGeneralNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("ScraplingGeneralNode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        
        dfs = extract_tables_via_scrapling(url, table_idx)
        if dfs:
            state["dfs"] = dfs
            return state, None
            
        return state, self.fallback_node


class PaperFetchNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("PaperFetchNode", fallback_node)

    def execute(self, state):
        doi = state.get("doi")
        table_idx = state.get("table_idx", "all")
        
        if doi:
            dfs = extract_tables_via_paper_fetch(doi, table_idx)
            if dfs:
                state["dfs"] = dfs
                return state, None
                
        return state, self.fallback_node


class FirecrawlNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("FirecrawlNode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        api_key = state.get("api_key")

        dfs = extract_tables_via_firecrawl(url, api_key, table_idx, cancel_event=state.get("cancel_event"))
        if dfs:
            state["dfs"] = dfs
            return state, None

        return state, self.fallback_node


class AcademicSearchGraphNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("AcademicSearchGraphNode", fallback_node)

    def execute(self, state):
        doi = state.get("doi")
        title = state.get("title")
        table_idx = state.get("table_idx", "all")
        api_key = state.get("api_key")
        
        # 1. Unpaywall fallback
        if doi:
            print("[AcademicSearchGraph] Querying Unpaywall for open-access URLs...")
            dfs = extract_tables_via_unpaywall(doi, table_idx, cancel_event=state.get("cancel_event"))
            if dfs:
                state["dfs"] = dfs
                return state, None
                
            # 2. Europe PMC search
            print(f"[AcademicSearchGraph] Querying Europe PMC for DOI: {doi}")
            try:
                epmc_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{doi}&format=json"
                r = requests.get(epmc_url, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    results = data.get("resultList", {}).get("result", [])
                    if results:
                        pmcid = results[0].get("pmcid")
                        if pmcid:
                            pmc_landing = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
                            print(f"[AcademicSearchGraph] Found PMC version: {pmc_landing}")
                            
                            dfs = extract_tables_via_direct_http(pmc_landing, table_idx)
                            if dfs:
                                state["dfs"] = dfs
                                return state, None
                                
                            dfs = extract_tables_via_scrapling(pmc_landing, table_idx)
                            if dfs:
                                state["dfs"] = dfs
                                return state, None
                                
                            dfs = extract_tables_via_firecrawl(pmc_landing, api_key, table_idx, cancel_event=state.get("cancel_event"))
                            if dfs:
                                state["dfs"] = dfs
                                return state, None
            except Exception as e:
                print(f"[AcademicSearchGraph Warning] Europe PMC search failed: {e}")
                
        # 3. Title-based Crossref preprint search
        if title:
            print(f"[AcademicSearchGraph] Searching alternative open-access repositories by title: {title}")
            try:
                crossref_url = f"https://api.crossref.org/works?query.title={urllib.parse.quote(title)}&rows=3"
                r = requests.get(crossref_url, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("message", {}).get("items", [])
                    for item in items:
                        links = item.get("link", [])
                        for link in links:
                            cand_url = link.get("URL")
                            if cand_url and any(domain in cand_url for domain in ["biorxiv.org", "medrxiv.org", "arxiv.org", "zenodo.org", "researchsquare.com"]):
                                print(f"[AcademicSearchGraph] Found alternative preprint link: {cand_url}")
                                dfs = extract_tables_via_direct_http(cand_url, table_idx)
                                if dfs:
                                    state["dfs"] = dfs
                                    return state, None
                                dfs = extract_tables_via_firecrawl(cand_url, api_key, table_idx, cancel_event=state.get("cancel_event"))
                                if dfs:
                                    state["dfs"] = dfs
                                    return state, None
            except Exception as e:
                print(f"[AcademicSearchGraph Warning] Title-based preprint search failed: {e}")
                
        return state, self.fallback_node


class BrowserActBypassNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("BrowserActBypassNode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        
        import subprocess
        try:
            res = subprocess.run(["browser-act", "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode != 0:
                print("[BrowserActBypassNode] browser-act CLI is not working correctly. Bypassing...")
                return state, self.fallback_node
        except Exception:
            print("[BrowserActBypassNode] browser-act CLI is not found on PATH. Bypassing...")
            return state, self.fallback_node
            
        print(f"[BrowserActBypassNode] Attempting stealth extraction with BrowserAct: {url}")
        try:
            res = subprocess.run(["browser-act", "stealth-extract", url, "--timeout", "35"], capture_output=True, text=True, timeout=45)
            if res.returncode == 0 and res.stdout.strip():
                html = res.stdout
                if html:
                    dfs = parse_tables_from_html(html, table_idx)
                    if dfs:
                        print(f"[BrowserActBypassNode] Successfully extracted {len(dfs)} tables using BrowserAct.")
                        state["dfs"] = dfs
                        return state, None
            else:
                print(f"[BrowserActBypassNode] stealth-extract failed. Output: {res.stderr or res.stdout}")
        except Exception as e:
            print(f"[BrowserActBypassNode Warning] BrowserAct extraction failed: {e}")
            
        return state, self.fallback_node


class Crawl4AIGeneralNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("Crawl4AIGeneralNode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        
        print(f"[Crawl4AIGeneralNode] Attempting Crawl4AI extraction: {url}")
        dfs = extract_tables_via_crawl4ai(url, table_idx, cancel_event=state.get("cancel_event"))
        if dfs:
            state["dfs"] = dfs
            return state, None
            
        print("[Crawl4AIGeneralNode] Crawl4AI extraction yielded no tables. Falling back...")
        return state, self.fallback_node


class Crawl4AICNKINode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("Crawl4AICNKINode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        table_idx = state.get("table_idx", "all")
        
        print(f"[Crawl4AICNKINode] Attempting Crawl4AI CNKI extraction: {url}")
        dfs = extract_tables_via_crawl4ai(url, table_idx, cancel_event=state.get("cancel_event"))
        if dfs:
            state["dfs"] = dfs
            return state, None
            
        print("[Crawl4AICNKINode] Crawl4AI extraction yielded no tables. Falling back...")
        return state, self.fallback_node


# ---------------------------------------------------------------------------
# 并行竞速管线（race pipeline）
# ---------------------------------------------------------------------------

def _race_candidates(candidates, cancel_event=None):
    """
    并发执行一组候选抓取渠道，首个返回非空表格列表的渠道获胜。

    candidates: [(name, callable -> list[DataFrame]), ...]
    返回 (dfs, winner_name)。失败者线程不阻塞返回（shutdown(wait=False)，
    各渠道自身均有网络超时兜底）。
    """
    if not candidates:
        return [], None
    if cancel_event is not None and cancel_event.is_set():
        return [], None

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates))
    try:
        fut_map = {ex.submit(fn): name for name, fn in candidates}
        pending = set(fut_map)
        while pending:
            if cancel_event is not None and cancel_event.is_set():
                print("[Race] 收到取消信号，放弃剩余竞速渠道。")
                break
            done, pending = concurrent.futures.wait(
                pending, timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for fut in done:
                name = fut_map[fut]
                try:
                    dfs = fut.result() or []
                except Exception as e:
                    print(f"[Race] 渠道 {name} 异常: {e}")
                    dfs = []
                if dfs:
                    print(f"[Race] 渠道 {name} 竞速获胜，提取到 {len(dfs)} 个表格。")
                    return dfs, name
        return [], None
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)


def _run_node_locked(node, state):
    """执行共享浏览器类节点（Playwright 系列）。

    节点 execute() 内部已持有 browser_extraction_lock（threading.Lock 不可重入，
    此处不可重复加锁），此包装仅用于语义标记与后续统一切换点。
    """
    return node.execute(state)


def _race_extract(state, cancel_event=None):
    """
    分层竞速提取管线。state 需已包含 url/doi/title/is_cnki。
    返回 (dfs, history)。
    """
    url = state.get("url")
    doi = state.get("doi")
    title = state.get("title")
    table_idx = state.get("table_idx", "all")
    api_key = state.get("api_key")
    history = []

    def cancelled():
        return cancel_event is not None and cancel_event.is_set()

    def _is_elsevier():
        return (
            url and ("sciencedirect.com" in url or "elsevier.com" in url)
        ) or (
            doi and any(doi.lower().startswith(p) for p in [
                "10.1016/", "10.1006/", "10.1053/", "10.1067/", "10.1078/",
                "10.1383/", "10.1517/",
            ])
        )

    def _elsevier_xml():
        _els_doi = doi
        if not _els_doi and url:
            _pii_match = re.search(r'/pii/([A-Z0-9]+)', url, re.IGNORECASE)
            if _pii_match:
                _els_doi = _resolve_pii_to_doi(_pii_match.group(1).upper())
        if _els_doi:
            return extract_tables_via_elsevier_api(_els_doi, table_idx)
        return []

    def _impersonated():
        if requests_impersonate is None:
            return []
        if cancel_event is not None and cancel_event.is_set():
            return []
        try:
            r = requests_impersonate.get(url, impersonate="chrome", timeout=15)
            if cancel_event is not None and cancel_event.is_set():
                return []
            if r.status_code == 200 and r.text:
                return parse_tables_from_html(r.text, table_idx) or []
        except Exception as e:
            print(f"[Race] ImpersonatedHTTP 失败: {e}")
        return []

    def _academic_search():
        if cancel_event is not None and cancel_event.is_set():
            return []
        s, _n = AcademicSearchGraphNode().execute(dict(state))
        return s.get("dfs") or []

    # ---- 学术搜索/开放获取尾流（两个分支共用） ----
    def _tail_race():
        candidates = []
        if doi or title:
            candidates.append(("AcademicSearch", _academic_search))
        if doi:
            candidates.append(("PaperFetch", lambda: extract_tables_via_paper_fetch(doi, table_idx, cancel_event=cancel_event)))
        if url:
            candidates.append(("Firecrawl", lambda: extract_tables_via_firecrawl(url, api_key, table_idx, cancel_event=cancel_event)))
        return _race_candidates(candidates, cancel_event)

    if state.get("is_cnki"):
        # ===== 知网分支 =====
        cnki_strategy = state.get("cnki_strategy", "auto")
        cookies_path = get_cnki_cookies_path()

        tier1 = []
        if cnki_strategy in ("auto", "scrapling"):
            tier1.append(("ScraplingCNKI",
                          lambda: extract_cnki_tables_via_scrapling(url, table_idx, cookies_path)))
        if cnki_strategy in ("auto",):
            tier1.append(("Crawl4AICNKI",
                          lambda: extract_tables_via_crawl4ai(url, table_idx, cancel_event=cancel_event)))
        dfs, winner = _race_candidates(tier1, cancel_event)
        history.append(f"cnki-tier1:{winner or 'none'}")
        if dfs:
            return dfs, history
        if cancelled():
            return [], history

        # Tier-2: Playwright CNKI（共享浏览器，锁内串行）
        if cnki_strategy in ("auto", "playwright"):
            print("[Race] CNKI Tier-2: PlaywrightCNKI（浏览器锁内）")
            s, _n = _run_node_locked(PlaywrightCNKINode(), state)
            history.append("cnki-tier2:playwright")
            if s.get("dfs"):
                return s["dfs"], history
            if s.get("title") and not state.get("title"):
                state["title"] = s["title"]
            if cancelled():
                return [], history

        # Tier-3: 学术搜索尾流 + BrowserAct
        dfs, winner = _tail_race()
        history.append(f"cnki-tail:{winner or 'none'}")
        if dfs:
            return dfs, history
        if cancelled():
            return [], history

        s, _n = BrowserActBypassNode().execute(state)
        history.append("cnki-tail:browseract")
        if s.get("dfs"):
            return s["dfs"], history
        return [], history

    # ===== 通用出版社分支 =====
    if not url and not doi:
        return [], history

    # Tier-1: 纯 HTTP / API 渠道并发竞速
    tier1 = []
    if url:
        tier1.append(("DirectHTTP", lambda: extract_tables_via_direct_http(url, table_idx, cancel_event=cancel_event)))
        tier1.append(("ImpersonatedHTTP", _impersonated))
    if _is_elsevier():
        tier1.append(("ElsevierXML", _elsevier_xml))
    if doi:
        tier1.append(("Unpaywall", lambda: extract_tables_via_unpaywall(doi, table_idx, cancel_event=cancel_event)))
    dfs, winner = _race_candidates(tier1, cancel_event)
    history.append(f"general-tier1:{winner or 'none'}")
    if dfs:
        return dfs, history
    if cancelled():
        return [], history

    # Tier-2: 独立浏览器 / 云抓取渠道并发竞速
    tier2 = []
    if url:
        tier2.append(("Crawl4AI", lambda: extract_tables_via_crawl4ai(url, table_idx, cancel_event=cancel_event)))
        tier2.append(("Firecrawl", lambda: extract_tables_via_firecrawl(url, api_key, table_idx, cancel_event=cancel_event)))
        tier2.append(("Scrapling", lambda: extract_tables_via_scrapling(url, table_idx)))
    dfs, winner = _race_candidates(tier2, cancel_event)
    history.append(f"general-tier2:{winner or 'none'}")
    if dfs:
        return dfs, history
    if cancelled():
        return [], history

    # Tier-3: 共享浏览器 Playwright（锁内串行，含补充材料下载）
    if url:
        print("[Race] General Tier-3: PlaywrightGeneral（浏览器锁内）")
        s, _n = _run_node_locked(PlaywrightGeneralNode(), state)
        history.append("general-tier3:playwright")
        if s.get("dfs"):
            return s["dfs"], history
        if s.get("title") and not state.get("title"):
            state["title"] = s["title"]
        if cancelled():
            return [], history

    # Tier-4: 学术搜索尾流 + BrowserAct
    dfs, winner = _tail_race()
    history.append(f"general-tail:{winner or 'none'}")
    if dfs:
        return dfs, history
    if cancelled():
        return [], history

    s, _n = BrowserActBypassNode().execute(state)
    history.append("general-tail:browseract")
    if s.get("dfs"):
        return s["dfs"], history
    return [], history


def extract_tables_online(pdf_path, table_idx="all", db_path=None, api_key=None, cnki_strategy="auto",
                          headed=False, user_data_dir=None, pre_resolved=None, race=True, cancel_event=None):
    """
    Attempts to extract tables online by resolving the DOI/URL and querying multiple sources.
    Returns a list of DataFrames if successful, else an empty list.

    pre_resolved: 可选的预分析结果（planner.analyze_pdf 的 plan），
                  包含 url/doi/title/is_cnki 时跳过元数据解析节点，直接竞速。
    race:         True（默认）走分层并行竞速管线；False 回退原有串行 DAG。
    cancel_event: 可选 threading.Event，置位后尽快放弃剩余渠道（外层竞速失败方）。
    """
    state = {
        "pdf_path": pdf_path,
        "table_idx": table_idx,
        "db_path": db_path,
        "api_key": api_key,
        "cnki_strategy": cnki_strategy,
        "headed": headed,
        "user_data_dir": user_data_dir,
        "cancel_event": cancel_event,
        "dfs": []
    }

    # 0. 预分析结果直接注入，跳过元数据解析（"先分析、后提取"）
    if pre_resolved:
        state["url"] = pre_resolved.get("url")
        state["doi"] = pre_resolved.get("doi")
        state["title"] = pre_resolved.get("title")
        state["is_cnki"] = pre_resolved.get("is_cnki", False)

    if race:
        # 并行竞速管线（url 缺失时仍进解析节点做 URL 解析；节点对已有 doi 幂等）
        if not state.get("url"):
            node_state, _n = ResolveMetadataNode().execute(state)
            state.update({k: node_state.get(k) for k in ("url", "doi", "title", "is_cnki")})
        print(f"[Race] 在线分层竞速开始: URL={state.get('url')}, DOI={state.get('doi')}, is_cnki={state.get('is_cnki')}")
        dfs, history = _race_extract(state, cancel_event)
        print(f"[Race] 竞速管线结束. History: {history}")
        return dfs, state.get("title")

    # ===== 原有串行 DAG 编排（race=False 时完整保留旧行为） =====
    # Initialize the graph
    graph = ExtractionGraph()

    # Add nodes to graph
    graph.add_node("ResolveMetadataNode", ResolveMetadataNode())
    graph.add_node("DirectHTTPNode", DirectHTTPNode(fallback_node="ImpersonatedHTTPNode"))
    graph.add_node("ImpersonatedHTTPNode", ImpersonatedHTTPNode(fallback_node="Crawl4AIGeneralNode"))
    graph.add_node("ElsevierXMLNode", ElsevierXMLNode(fallback_node="Crawl4AIGeneralNode"))

    graph.add_node("Crawl4AICNKINode", Crawl4AICNKINode(fallback_node="PlaywrightCNKINode"))
    graph.add_node("PlaywrightCNKINode", PlaywrightCNKINode(fallback_node="ScraplingCNKINode"))
    graph.add_node("ScraplingCNKINode", ScraplingCNKINode(fallback_node="AcademicSearchGraphNode"))

    graph.add_node("Crawl4AIGeneralNode", Crawl4AIGeneralNode(fallback_node="PlaywrightGeneralNode"))
    graph.add_node("PlaywrightGeneralNode", PlaywrightGeneralNode(fallback_node="ScraplingGeneralNode"))
    graph.add_node("ScraplingGeneralNode", ScraplingGeneralNode(fallback_node="AcademicSearchGraphNode"))
    graph.add_node("AcademicSearchGraphNode", AcademicSearchGraphNode(fallback_node="BrowserActBypassNode"))
    graph.add_node("BrowserActBypassNode", BrowserActBypassNode(fallback_node="PaperFetchNode"))
    graph.add_node("PaperFetchNode", PaperFetchNode(fallback_node="FirecrawlNode"))
    graph.add_node("FirecrawlNode", FirecrawlNode())

    # Set entry point & initial state
    if pre_resolved and state.get("url"):
        # 跳过元数据解析节点，从已解析状态直接进入对应分支
        # （url 缺失时仍从 ResolveMetadataNode 进入做 URL 解析，节点对已有 doi 幂等）
        if state.get("is_cnki"):
            graph.set_entry_point("Crawl4AICNKINode")
        else:
            graph.set_entry_point("DirectHTTPNode")
    else:
        graph.set_entry_point("ResolveMetadataNode")

    # Run graph
    print("[Graph] Starting extraction graph pipeline...")
    final_state = graph.run(state)
    print(f"[Graph] Pipeline finished. History: {final_state.get('history')}")
    if final_state.get("errors"):
        print(f"[Graph] Node errors: {final_state.get('errors')}")

    return final_state.get("dfs"), final_state.get("title")
