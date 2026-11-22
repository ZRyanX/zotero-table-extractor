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
import contextlib
import urllib.parse
import concurrent.futures
import requests

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
    from ..playwright_utils import find_playwright_chromium
except ImportError:
    try:
        from playwright_utils import find_playwright_chromium
    except ImportError:
        find_playwright_chromium = lambda: None


# Aliases to shared helpers (kept as bare names so call sites are unchanged)
get_cnki_cookies_path = common.get_cnki_cookies_path
load_config = common.load_config
get_polite_email = getattr(common, "get_polite_email", lambda config=None, service="crossref": "supp_downloader@academic-research.org")
get_requests_proxies = getattr(common, "get_requests_proxies", lambda config=None: None)
browser_extraction_lock = common.browser_extraction_lock


@contextlib.contextmanager
def browser_task_concurrency_guard(user_data_dir=None, config=None):
    """
    智能浏览器任务并发调度（吸收 web-access 单物理实例 + Tab 隔离机制）：
    - 若探测到活动 CDP 端点（DevToolsActivePort 动态端口或 9222），说明复用已存在的物理浏览器实例：
      跳过全局粗粒度跨进程锁，采用 Tab 级轻量并发信号量，允许多任务并行在各自后台 Tab 中抽取；
    - 若未探测到 CDP 端点，需要自启独立持久化浏览器实例并占用磁盘 user_data_dir：
      获取全局 browser_extraction_lock，防止 SingletonLock 冲突与数据损坏。
    """
    has_cdp = False
    try:
        from .cdp_detector import find_active_cdp_endpoint
        ep = find_active_cdp_endpoint(timeout=0.6, user_data_dir=user_data_dir, config=config)
        has_cdp = ep is not None
    except Exception:
        has_cdp = False

    if has_cdp:
        from .cdp_detector import cdp_tab_slot_guard
        with cdp_tab_slot_guard():
            yield True
        return

    with browser_extraction_lock():
        yield False



from .doi_resolver import (
    _resolve_pii_to_doi,
    get_cnki_url_from_pdf_content,
    get_doi_by_title,
    get_doi_by_title_firecrawl,
    get_doi_from_pdf_content,
    get_doi_from_zotero_db,
    is_chinese_doi,
    normalize_title,
    resolve_chinese_doi,
    resolve_doi_canonical_url,
    extract_arxiv_id,
    extract_pmcid,
    resolve_pmcid_from_doi,
)

from .strategies import (
    _extract_doi_from_url,
    extract_cnki_tables_via_playwright,
    extract_cnki_tables_via_scrapling,
    extract_general_supplementary_tables,
    extract_tables_via_crawl4ai,
    extract_tables_via_direct_http,
    extract_tables_via_elsevier_api,
    extract_tables_via_europepmc_xml,
    extract_tables_via_firecrawl,
    extract_tables_via_paper_fetch,
    extract_tables_via_scrapling,
    extract_tables_via_springer_tdm,
    extract_tables_via_unpaywall,
    extract_tables_via_arxiv_html,
    extract_tables_via_ncbi_pmc,
    get_domain_profile_dir,
    is_springer_nature_target,
    parse_tables_from_html,
)

try:
    from .negative_cache import is_negatively_cached, record_negative_cache, clear_negative_cache
except ImportError:
    is_negatively_cached = None
    record_negative_cache = None
    clear_negative_cache = None

try:
    from .proxy_rewriter import (
        is_institutional_proxy_url,
        maybe_rewrite_institutional_url,
        validate_institutional_session,
    )
except ImportError:
    is_institutional_proxy_url = None
    maybe_rewrite_institutional_url = None
    validate_institutional_session = None


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


def _normalize_publisher_url(url: str) -> str:
    """Normalize bare DOI host typos and upgrade Atypon Literatum /doi/abs/ URLs to /doi/full/."""
    if not url:
        return url
    m_bare = re.match(r"^https?://(10\.\d{4,9})/([^\s]+)$", url.strip(), re.IGNORECASE)
    if m_bare:
        url = f"https://doi.org/{m_bare.group(1)}/{m_bare.group(2)}"
    if "/doi/abs/" in url and any(dom in url.lower() for dom in (
        "onlinelibrary.wiley.com", "tandfonline.com", "sagepub.com",
        "geoscienceworld.org", "lyellcollection.org", "agu.onlinelibrary.wiley.com",
        "pubs.acs.org",
    )):
        return url.replace("/doi/abs/", "/doi/full/", 1)
    return url


def _resolve_chinese_doi_and_classify(url: str, is_cnki: bool = False):
    """Resolve chndoi.org/chinadoi.cn to domestic CNKI link and update is_cnki classification."""
    if not url:
        return url, is_cnki
    low = url.lower()
    if "chndoi.org" in low or "chinadoi.cn" in low:
        resolved = resolve_chinese_doi(url)
        if resolved and resolved != url:
            url = resolved
            low = url.lower()
    if "cnki.net" in low or "cnki.com.cn" in low:
        is_cnki = True
    return url, is_cnki


class ResolveMetadataNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("ResolveMetadataNode", fallback_node)

    def execute(self, state):
        pdf_path = state.get("pdf_path")
        db_path = state.get("db_path")
        api_key = state.get("api_key")
        
        # 0. Check if pdf_path is already a URL
        if isinstance(pdf_path, str) and (pdf_path.startswith("http://") or pdf_path.startswith("https://")):
            url = _normalize_publisher_url(pdf_path)
            url, is_cnki = _resolve_chinese_doi_and_classify(url, False)
            doi = None
            low_url = url.lower()
            if "doi.org/" in low_url:
                _doi_match = re.search(r'doi\.org/(10\.\d{4,9}/[-._;()/:A-Z0-9]+)', urllib.parse.unquote(url), re.IGNORECASE)
                if _doi_match:
                    doi = _doi_match.group(1).rstrip(".,;:/")
            elif "chndoi.org" in pdf_path.lower() or "chinadoi.cn" in pdf_path.lower():
                _doi_match = re.search(
                    r'[?&]doi=(10\.\d{4,9}/[^\s&#]+)',
                    urllib.parse.unquote(pdf_path),
                    re.IGNORECASE,
                )
                if _doi_match:
                    doi = _doi_match.group(1).rstrip(".,;:/")
            if not doi:
                extracted_doi = _extract_doi_from_url(url) or _extract_doi_from_url(pdf_path)
                if extracted_doi:
                    doi = extracted_doi
            if doi and not is_cnki and is_chinese_doi(doi):
                parsed_host = (urllib.parse.urlparse(url).hostname or "").lower()
                if parsed_host in ("doi.org", "dx.doi.org", "hdl.handle.net"):
                    chndoi_url = f"https://www.chndoi.org/Resolution/Handler?doi={doi}"
                    resolved = resolve_chinese_doi(chndoi_url)
                    if resolved and resolved != chndoi_url:
                        url = resolved
                        low_res = resolved.lower()
                        is_cnki = ("cnki.net" in low_res or "cnki.com.cn" in low_res)
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
            
            url = state.get("url")
            is_cnki = False
            
            # Prioritize direct Zotero URL if available (saves redirect timeouts)
            if url and isinstance(url, str) and url.startswith("http"):
                url, is_cnki = _resolve_chinese_doi_and_classify(url, is_cnki)
            elif zotero_url and zotero_url.startswith("http"):
                url, is_cnki = _resolve_chinese_doi_and_classify(zotero_url, is_cnki)
            elif cnki_url:
                is_cnki = True
                url = cnki_url
            elif doi:
                if is_chinese_doi(doi):
                    chndoi_url = f"https://www.chndoi.org/Resolution/Handler?doi={doi}"
                    resolved = resolve_chinese_doi(chndoi_url)
                    if resolved and resolved != chndoi_url:
                        url = resolved
                        low_res = resolved.lower()
                        is_cnki = ("cnki.net" in low_res or "cnki.com.cn" in low_res)
                    else:
                        canonical = resolve_doi_canonical_url(doi)
                        if canonical:
                            url, is_cnki = _resolve_chinese_doi_and_classify(canonical, False)
                        else:
                            url = f"https://doi.org/{doi}"
                            is_cnki = ("cnki" in doi.lower())
                else:
                    # Handle API 一次拿 canonical URL：免 302 重定向链、免落地页整页下载
                    canonical = resolve_doi_canonical_url(doi)
                    if canonical:
                        if "chndoi.org" in canonical or "chinadoi.cn" in canonical:
                            resolved = resolve_chinese_doi(canonical)
                            if "cnki.net" in resolved or "cnki.com.cn" in resolved:
                                is_cnki = True
                                url = resolved
                            elif resolved and resolved != canonical:
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

            state["url"] = _normalize_publisher_url(url)
            if not state.get("doi"):
                extracted_doi = _extract_doi_from_url(state["url"])
                if extracted_doi:
                    state["doi"] = extracted_doi
            state["is_cnki"] = is_cnki

        # 检查 arXiv 与 PMC
        arxiv_cand = extract_arxiv_id(state.get("url") or state.get("doi"))
        if arxiv_cand:
            state["arxiv_id"] = arxiv_cand
            state["is_arxiv"] = True

        pmc_cand = extract_pmcid(state.get("url") or state.get("doi"))
        if pmc_cand:
            state["pmcid"] = pmc_cand
            state["is_pmc"] = True
        
        print(f"[Graph] Resolved metadata: URL={state.get('url')}, DOI={state.get('doi')}, Title={state.get('title')}, is_cnki={state.get('is_cnki')}, arxiv={state.get('arxiv_id')}, pmc={state.get('pmcid')}")
        
        cfg = load_config() if callable(load_config) else {}
        if state.get("is_cnki"):
            return state, "Crawl4AICNKINode"
        elif state.get("is_arxiv") and cfg.get("ARXIV_HTML_ENABLED", True):
            return state, "ArxivHTMLNode"
        elif state.get("is_pmc") and cfg.get("NCBI_PMC_ENABLED", True):
            return state, "NCBIPMCNode"
        else:
            return state, "DirectHTTPNode"



class DirectHTTPNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("DirectHTTPNode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        doi = state.get("doi")
        table_idx = state.get("table_idx", "all")

        dfs = extract_tables_via_direct_http(
            url,
            table_idx,
            cancel_event=state.get("cancel_event"),
            doi=doi,
        )
        if dfs:
            state["dfs"] = dfs
            return state, None

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
        if is_springer_nature_target(doi=doi, url=url):
            return state, "SpringerTDMNode"
        return state, self.fallback_node


def _fetch_tables_via_impersonated_http(
    url,
    table_idx="all",
    doi=None,
    cancel_event=None,
    config=None,
    log_prefix="[ImpersonatedHTTP]",
):
    """Fetch HTML via curl_cffi TLS impersonation with negative-cache, proxy/institutional rewrite, and anti-bot checks."""
    if not url or requests_impersonate is None:
        return []
    if cancel_event is not None and cancel_event.is_set():
        return []

    cfg = config if isinstance(config, dict) else (load_config() if callable(load_config) else {})
    if callable(is_negatively_cached):
        hit = is_negatively_cached("direct_http", url=url, doi=doi, config=cfg)
        if hit:
            print(
                f"{log_prefix} 命中跨进程负缓存 ({hit.get('target_key')}, {hit.get('reason')})，跳过 TLS 模拟请求。"
            )
            return []

    proxies = get_requests_proxies(cfg) if callable(get_requests_proxies) else None
    fetch_url = url
    inst_proxy_active = False
    if callable(maybe_rewrite_institutional_url) and (cfg.get("EZPROXY_ENABLED") or cfg.get("WEBVPN_ENABLED")):
        fetch_url = maybe_rewrite_institutional_url(url, config=cfg)
        inst_proxy_active = (fetch_url != url) or (
            callable(is_institutional_proxy_url) and is_institutional_proxy_url(fetch_url, config=cfg)
        )

    print(f"{log_prefix} Fetching page using TLS impersonation (Chrome): {fetch_url}")
    try:
        kw = {"impersonate": "chrome", "timeout": 10, "allow_redirects": True}
        if proxies:
            kw["proxies"] = proxies
        r = requests_impersonate.get(fetch_url, **kw)
        if cancel_event is not None and cancel_event.is_set():
            return []

        status_code = getattr(r, "status_code", None)
        html = getattr(r, "text", "") or ""
        final_url = str(getattr(r, "url", "") or fetch_url)

        if inst_proxy_active and callable(validate_institutional_session):
            sess = validate_institutional_session(
                html_text=html,
                final_url=final_url,
                status_code=status_code,
            )
            if not sess.get("valid") and sess.get("needs_relogin"):
                print(
                    f"{log_prefix} 机构代理会话已失效 ({sess.get('reason')})，请先刷新 EZProxy/WebVPN 登录态后重试（不写入反爬负缓存）。"
                )
                return []

        if status_code == 200:
            if html:
                if hasattr(common, "is_anti_bot_blocked") and common.is_anti_bot_blocked("", html):
                    print(f"{log_prefix} 请求被反爬拦截 (Cloudflare / Access Denied / Radware)。")
                    if callable(record_negative_cache):
                        record_negative_cache(
                            "direct_http",
                            url=url,
                            doi=doi,
                            status_code=200,
                            html_snippet=html,
                            config=cfg,
                        )
                    return []
                dfs = parse_tables_from_html(html, table_idx)
                if dfs:
                    if callable(clear_negative_cache):
                        clear_negative_cache("direct_http", url=url, doi=doi, config=cfg)
                    print(f"{log_prefix} Successfully extracted {len(dfs)} tables via TLS impersonation.")
                    return dfs
        elif status_code is not None:
            print(f"{log_prefix} Request returned status code: {status_code}")
            if callable(record_negative_cache) and status_code in (403, 429, 503):
                record_negative_cache(
                    "direct_http",
                    url=url,
                    doi=doi,
                    status_code=status_code,
                    html_snippet=html,
                    config=cfg,
                )
    except Exception as e:
        print(f"{log_prefix} Warning: TLS impersonation request failed: {e}")

    return []


class ImpersonatedHTTPNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("ImpersonatedHTTPNode", fallback_node)

    def execute(self, state):
        url = state.get("url")
        doi = state.get("doi")
        table_idx = state.get("table_idx", "all")

        if requests_impersonate is None:
            print("[ImpersonatedHTTPNode] curl_cffi is not installed. Bypassing node...")
            return state, self.fallback_node

        dfs = _fetch_tables_via_impersonated_http(
            url,
            table_idx=table_idx,
            doi=doi,
            cancel_event=state.get("cancel_event"),
            log_prefix="[ImpersonatedHTTPNode]",
        )
        if dfs:
            state["dfs"] = dfs
            return state, None

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
            dfs = extract_tables_via_elsevier_api(_els_doi, table_idx, cancel_event=state.get("cancel_event"))
            if dfs:
                state["dfs"] = dfs
                return state, None
                
        return state, self.fallback_node


class SpringerTDMNode(ExtractionNode):
    def __init__(self, fallback_node=None):
        super().__init__("SpringerTDMNode", fallback_node)

    def execute(self, state):
        doi = state.get("doi")
        table_idx = state.get("table_idx", "all")
        if doi:
            dfs = extract_tables_via_springer_tdm(doi, table_idx, cancel_event=state.get("cancel_event"))
            if dfs:
                state["dfs"] = dfs
                return state, None
        return state, self.fallback_node


class ArxivHTMLNode(ExtractionNode):
    def __init__(self, fallback_node="DirectHTTPNode"):
        super().__init__("ArxivHTMLNode", fallback_node)

    def execute(self, state):
        cfg = load_config() if callable(load_config) else {}
        if not cfg.get("ARXIV_HTML_ENABLED", True):
            return state, self.fallback_node

        url = state.get("url")
        doi = state.get("doi")
        table_idx = state.get("table_idx", "all")
        arxiv_id = state.get("arxiv_id") or extract_arxiv_id(url or doi)
        if not arxiv_id:
            return state, self.fallback_node

        print(f"[ArxivHTMLNode] 尝试直接从 arXiv HTML/ar5iv 提取高保真表格: {arxiv_id}")
        dfs = extract_tables_via_arxiv_html(arxiv_id, table_idx)
        if dfs:
            state["dfs"] = dfs
            return state, None
        return state, self.fallback_node


class NCBIPMCNode(ExtractionNode):
    def __init__(self, fallback_node="DirectHTTPNode"):
        super().__init__("NCBIPMCNode", fallback_node)

    def execute(self, state):
        cfg = load_config() if callable(load_config) else {}
        if not cfg.get("NCBI_PMC_ENABLED", True):
            return state, self.fallback_node

        doi = state.get("doi")
        url = state.get("url")
        pmcid = state.get("pmcid") or extract_pmcid(url or doi)
        table_idx = state.get("table_idx", "all")
        target = pmcid or doi
        if not target:
            return state, self.fallback_node

        print(f"[NCBIPMCNode] 尝试直接从 NCBI PMC 获取 JATS XML 提取高保真表格: {target}")
        dfs = extract_tables_via_ncbi_pmc(target, table_idx)
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
            
        # 共享浏览器状态调度：若已有 CDP 实例则免全局锁走 Tab 隔离并发，否则全局加锁保护 profile
        with browser_task_concurrency_guard(user_data_dir=user_data_dir, config=config):
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
        cancel_event = state.get("cancel_event")
        if cancel_event is not None and cancel_event.is_set():
            return state, None
            
        # 共享浏览器状态调度：若已有 CDP 实例则免全局锁走 Tab 隔离并发，否则全局加锁保护 profile
        _pw_meta = {}
        with browser_task_concurrency_guard(user_data_dir=user_data_dir, config=config):
            try:
                res = general_html_extractor.extract_general_html_tables(
                    url,
                    table_idx,
                    headed,
                    user_data_dir,
                    api_key,
                    cancel_event=cancel_event,
                    skip_stealth_fallback=bool(state.get("_skip_stealth_fallback", False)),
                    meta_out=_pw_meta,
                )
            except TypeError:
                res = general_html_extractor.extract_general_html_tables(
                    url,
                    table_idx,
                    headed,
                    user_data_dir,
                    api_key,
                    cancel_event=cancel_event,
                    skip_stealth_fallback=bool(state.get("_skip_stealth_fallback", False)),
                )
        _reroute_cnki = _pw_meta.get("reroute_cnki_url")
        if _reroute_cnki:
            print(f"[在线提取] 检测到 chndoi/跳转落地知网 ({_reroute_cnki})，动态重路由至 CNKI 专用抽取器...")
            cnki_state = dict(state)
            cnki_state["url"] = _reroute_cnki
            cnki_state["is_cnki"] = True
            state["url"] = _reroute_cnki
            state["is_cnki"] = True
            return PlaywrightCNKINode(fallback_node=self.fallback_node).execute(cnki_state)

        if isinstance(res, tuple):
            dfs, resolved_title = res
            _error_titles = {
                "access denied", "are you a robot", "sciencedirect", "just a moment",
                "performing security verification", "error", "403", "page not found",
                "cloudflare", "your privacy, your choice", "there was a problem",
                "请稍候", "radware", "安全验证", "attention required",
            }
            if resolved_title and dfs:
                title_lower = resolved_title.lower().strip()
                if not any(err in title_lower for err in _error_titles):
                    state["title"] = resolved_title
        else:
            dfs = res

        if cancel_event is not None and cancel_event.is_set():
            if dfs:
                state["dfs"] = dfs
            return state, None
            
        def _run_supp(timeout_s):
            _pw_html = _pw_meta.get("html_content")
            if _pw_html:
                try:
                    return extract_general_supplementary_tables(
                        url, cancel_event=cancel_event, max_timeout=timeout_s, html_content=_pw_html
                    )
                except TypeError:
                    pass
            return extract_general_supplementary_tables(url, cancel_event=cancel_event, max_timeout=timeout_s)

        if dfs:
            # Also attempt to download and parse general supplementary tables via journal-supp-downloader
            supp_dfs = _run_supp(30)
            if supp_dfs:
                dfs.extend(supp_dfs)
                print(f"[在线提取] 成功获取并追加了 {len(supp_dfs)} 个补充表格。")
            state["dfs"] = dfs
            return state, None
            
        if _pw_meta.get("was_dead_http") and not dfs:
            print("[在线提取] 页面返回 HTTP 404/410 死链，跳过补充材料子进程下载。")
            return state, self.fallback_node

        # If no main tables found, still try bounded supplementary extraction
        _supp_timeout = 20 if _pw_meta.get("was_blocked") else 30
        supp_dfs = _run_supp(_supp_timeout)
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
        doi = state.get("doi")
        table_idx = state.get("table_idx", "all")
        api_key = state.get("api_key")

        dfs = extract_tables_via_firecrawl(
            url, api_key, table_idx, cancel_event=state.get("cancel_event"), doi=doi
        )
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
        cancel_event = state.get("cancel_event")

        if cancel_event is not None and cancel_event.is_set():
            return state, None
        
        # 1. Unpaywall fallback
        if doi:
            print("[AcademicSearchGraph] Querying Unpaywall for open-access URLs...")
            dfs = extract_tables_via_unpaywall(doi, table_idx, cancel_event=cancel_event)
            if dfs:
                state["dfs"] = dfs
                return state, None
            if cancel_event is not None and cancel_event.is_set():
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
                            print(f"[AcademicSearchGraph] Found Europe PMC PMCID: {pmcid}，优先直取 JATS XML...")
                            dfs = extract_tables_via_ncbi_pmc(pmcid, table_idx)
                            if dfs:
                                state["dfs"] = dfs
                                return state, None
                            if cancel_event is not None and cancel_event.is_set():
                                return state, None

                            dfs = extract_tables_via_europepmc_xml(pmcid, table_idx, cancel_event=cancel_event)
                            if dfs:
                                state["dfs"] = dfs
                                return state, None
                            if cancel_event is not None and cancel_event.is_set():
                                return state, None

                            pmc_landing = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
                            print(f"[AcademicSearchGraph] JATS XML 未命中表格，回退 PMC HTML 落地页: {pmc_landing}")
                            
                            dfs = extract_tables_via_direct_http(pmc_landing, table_idx, cancel_event=cancel_event)
                            if dfs:
                                state["dfs"] = dfs
                                return state, None
                            if cancel_event is not None and cancel_event.is_set():
                                return state, None
                                
                            dfs = extract_tables_via_scrapling(pmc_landing, table_idx, cancel_event=cancel_event)
                            if dfs:
                                state["dfs"] = dfs
                                return state, None
                            if cancel_event is not None and cancel_event.is_set():
                                return state, None
                                
                            dfs = extract_tables_via_firecrawl(pmc_landing, api_key, table_idx, cancel_event=cancel_event, is_oa=True, doi=doi)
                            if dfs:
                                state["dfs"] = dfs
                                return state, None
            except Exception as e:
                print(f"[AcademicSearchGraph Warning] Europe PMC search failed: {e}")

        if cancel_event is not None and cancel_event.is_set():
            return state, None
                
        # 3. Title-based Crossref preprint search
        norm_title = normalize_title(title) if title else ""
        _bogus_title_kws = ("cnki原文下载服务", "中国知网", "access denied", "just a moment", "attention required", "page not found")
        if norm_title and not any(k in norm_title.lower() for k in _bogus_title_kws):
            print(f"[AcademicSearchGraph] Searching alternative open-access repositories by title: {norm_title}")
            try:
                polite_email = get_polite_email(load_config(), service="crossref")
                crossref_url = (
                    f"https://api.crossref.org/works?query.title={urllib.parse.quote(norm_title)}"
                    f"&rows=3&mailto={urllib.parse.quote(polite_email)}"
                )
                r = requests.get(
                    crossref_url,
                    headers={"User-Agent": f"ZoteroTableExtractor/1.0 (mailto:{polite_email})"},
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("message", {}).get("items", [])
                    for item in items:
                        if cancel_event is not None and cancel_event.is_set():
                            return state, None
                        links = item.get("link", [])
                        for link in links:
                            if cancel_event is not None and cancel_event.is_set():
                                return state, None
                            cand_url = link.get("URL")
                            if cand_url and any(domain in cand_url for domain in ["biorxiv.org", "medrxiv.org", "arxiv.org", "zenodo.org", "researchsquare.com"]):
                                print(f"[AcademicSearchGraph] Found alternative preprint link: {cand_url}")
                                dfs = extract_tables_via_direct_http(cand_url, table_idx, cancel_event=cancel_event)
                                if dfs:
                                    state["dfs"] = dfs
                                    return state, None
                                dfs = extract_tables_via_firecrawl(cand_url, api_key, table_idx, cancel_event=cancel_event, is_oa=True, doi=doi)
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

def _race_candidates(candidates, cancel_event=None, authoritative_names=None, grace_timeout=6.0):
    """
    并发执行一组候选抓取渠道，优先权威 XML/LaTeXML 全文数据源并取表格更完整的结果。

    candidates: [(name, callable -> list[DataFrame]), ...]
    authoritative_names: 若非权威渠道 (如 DirectHTTP 网页片段) 先返回，而权威全文通道
        (ElsevierXML / NCBIPMC / ArxivHTML) 仍在执行，等待短窗口 grace_timeout，防止
        网页摘要/单表片段提前中断完整 XML/补充材料抽取。
    """
    import queue as _q
    import time as _t
    if not candidates:
        return [], None
    if cancel_event is not None and cancel_event.is_set():
        return [], None

    auth_set = set(authoritative_names or ())
    result_queue = _q.Queue()
    pending = {name for name, _ in candidates}

    def _run_candidate(c_name, c_fn):
        try:
            c_dfs = c_fn() or []
            result_queue.put((c_name, c_dfs, None))
        except Exception as exc:
            result_queue.put((c_name, [], exc))

    for name, fn in candidates:
        t = threading.Thread(target=_run_candidate, args=(name, fn), daemon=True, name=f"race-{name}")
        t.start()

    best_fast = None  # (dfs, name)
    grace_deadline = None

    while pending:
        if cancel_event is not None and cancel_event.is_set():
            print("[Race] 收到取消信号，放弃剩余竞速渠道。")
            break
        wait_step = 0.5
        if grace_deadline is not None:
            rem = grace_deadline - _t.monotonic()
            if rem <= 0:
                break
            wait_step = min(0.5, rem)

        done_items = []
        try:
            done_items.append(result_queue.get(timeout=wait_step))
            while True:
                done_items.append(result_queue.get_nowait())
        except _q.Empty:
            pass

        for name, dfs, err in done_items:
            pending.discard(name)
            if err is not None:
                print(f"[Race] 渠道 {name} 异常: {err}")
                dfs = []
            if not dfs:
                continue
            if name in auth_set or not any(p in auth_set for p in pending):
                if best_fast is not None and len(best_fast[0]) > len(dfs) and name not in auth_set:
                    print(f"[Race] 渠道 {best_fast[1]} 保留更多表格 ({len(best_fast[0])} >= {len(dfs)})。")
                    return best_fast
                print(f"[Race] 渠道 {name} 竞速获胜，提取到 {len(dfs)} 个表格。")
                return dfs, name
            # 非权威渠道先到达且权威渠道仍在请求，记录兜底并短暂等待权威渠道完成
            if best_fast is None or len(dfs) > len(best_fast[0]):
                best_fast = (dfs, name)
            if grace_deadline is None:
                grace_deadline = _t.monotonic() + float(grace_timeout)
        if grace_deadline is not None:
            if _t.monotonic() >= grace_deadline or not any(p in auth_set for p in pending):
                break

    if best_fast is not None:
        print(f"[Race] 渠道 {best_fast[1]} 竞速获胜，提取到 {len(best_fast[0])} 个表格。")
        return best_fast
    return [], None


def _hedged_race_candidates(primary_candidates, hedged_candidates, hedge_delay_s=4.0, cancel_event=None):
    """
    Hedged Cascade 竞速调度器（用于 Tier-2）：
    1. 先启动低成本本地渠道（primary_candidates，如 Crawl4AI / Scrapling）；
    2. 若在 hedge_delay_s 内任意本地渠道成功提取表格，直接返回，零云端额度开销；
    3. 若所有本地渠道在 hedge_delay_s 之前即快速失败（或被负缓存全部跳过），立即触发 hedged_candidates（Firecrawl），绝不空等；
    4. 若达到 hedge_delay_s 本地渠道仍未成功，则并发触发 hedged_candidates，谁先出表谁获胜。
    """
    import queue as _q
    import time as _t

    prim = list(primary_candidates or ())
    hedged = list(hedged_candidates or ())
    if not prim and not hedged:
        return [], None
    if cancel_event is not None and cancel_event.is_set():
        return [], None
    if not prim:
        return _race_candidates(hedged, cancel_event=cancel_event)
    if not hedged or float(hedge_delay_s or 0.0) <= 0.0:
        return _race_candidates(prim + hedged, cancel_event=cancel_event)

    result_queue = _q.Queue()
    pending = set()

    def _spawn(items):
        for c_name, c_fn in items:
            pending.add(c_name)

            def _run(n=c_name, f=c_fn):
                try:
                    c_dfs = f() or []
                    result_queue.put((n, c_dfs, None))
                except Exception as exc:
                    result_queue.put((n, [], exc))

            t = threading.Thread(target=_run, daemon=True, name=f"hedged-{c_name}")
            t.start()

    _spawn(prim)
    hedged_launched = False
    hedge_deadline = _t.monotonic() + float(hedge_delay_s)

    while pending or not hedged_launched:
        if cancel_event is not None and cancel_event.is_set():
            print("[HedgedRace] 收到取消信号，放弃剩余竞速渠道。")
            break

        if not hedged_launched:
            now = _t.monotonic()
            if not pending or now >= hedge_deadline:
                trigger_reason = "本地渠道已全部快速失败" if not pending else f"等待 {hedge_delay_s:.1f}s 触发对冲"
                print(f"[HedgedRace] {trigger_reason}，启动云端对冲渠道: {[n for n, _ in hedged]}")
                _spawn(hedged)
                hedged_launched = True
                if not pending:
                    break
                wait_step = 0.5
            else:
                wait_step = min(0.25, max(0.01, hedge_deadline - now))
        else:
            wait_step = 0.5

        done_items = []
        try:
            done_items.append(result_queue.get(timeout=wait_step))
            while True:
                done_items.append(result_queue.get_nowait())
        except _q.Empty:
            pass

        for name, dfs, err in done_items:
            pending.discard(name)
            if err is not None:
                print(f"[HedgedRace] 渠道 {name} 异常: {err}")
                dfs = []
            if dfs:
                print(f"[HedgedRace] 渠道 {name} 竞速获胜，提取到 {len(dfs)} 个表格。")
                return dfs, name

    return [], None


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
            return extract_tables_via_elsevier_api(_els_doi, table_idx, cancel_event=cancel_event)
        return []

    def _impersonated(config=None):
        return _fetch_tables_via_impersonated_http(
            url,
            table_idx=table_idx,
            doi=doi,
            cancel_event=cancel_event,
            config=config,
            log_prefix="[Race][ImpersonatedHTTP]",
        )

    def _academic_search():
        if cancel_event is not None and cancel_event.is_set():
            return []
        s, _n = AcademicSearchGraphNode().execute(dict(state))
        return s.get("dfs") or []

    # ---- 学术搜索/开放获取尾流（两个分支共用） ----
    def _tail_race(skip_firecrawl=False):
        candidates = []
        if doi or title:
            candidates.append(("AcademicSearch", _academic_search))
        if doi:
            candidates.append(("PaperFetch", lambda: extract_tables_via_paper_fetch(doi, table_idx, cancel_event=cancel_event)))
        if url and not skip_firecrawl:
            candidates.append(("Firecrawl", lambda: extract_tables_via_firecrawl(url, api_key, table_idx, cancel_event=cancel_event, doi=doi)))
        return _race_candidates(candidates, cancel_event)

    if state.get("is_cnki"):
        # ===== 知网分支 =====
        cnki_strategy = state.get("cnki_strategy", "auto")
        cookies_path = get_cnki_cookies_path()

        tier1 = []
        _is_cnki_abstract_shell = bool(url and "link.cnki.net/doi/" in url.lower())
        if not (cnki_strategy == "auto" and _is_cnki_abstract_shell):
            if cnki_strategy in ("auto", "scrapling"):
                tier1.append(("ScraplingCNKI",
                              lambda: extract_cnki_tables_via_scrapling(url, table_idx, cookies_path, cancel_event=cancel_event)))
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
    cfg = load_config() if callable(load_config) else {}
    arxiv_enabled = cfg.get("ARXIV_HTML_ENABLED", True)
    pmc_enabled = cfg.get("NCBI_PMC_ENABLED", True)

    arxiv_cand = state.get("arxiv_id") or extract_arxiv_id(url or doi)
    if arxiv_cand and arxiv_enabled:
        tier1.append(("ArxivHTML", lambda: extract_tables_via_arxiv_html(arxiv_cand, table_idx)))
    pmc_cand = state.get("pmcid") or extract_pmcid(url or doi)
    if pmc_enabled and (pmc_cand or doi):
        tier1.append(("NCBIPMC", lambda: extract_tables_via_ncbi_pmc(pmc_cand or doi, table_idx)))
    if url:
        def _direct_then_impersonated():
            res = extract_tables_via_direct_http(url, table_idx, cancel_event=cancel_event, doi=doi)
            if res:
                return res
            if cancelled():
                return []
            if callable(is_negatively_cached):
                hit = is_negatively_cached("direct_http", url=url, doi=doi, config=cfg)
                if hit:
                    print(
                        f"[Race] DirectHTTP 已命中负缓存 ({hit.get('target_key')}, {hit.get('reason')})，跳过 ImpersonatedHTTP 重试。"
                    )
                    return []
            return _impersonated(config=cfg)

        tier1.append(("DirectHTTP", _direct_then_impersonated))
    if _is_elsevier():
        tier1.append(("ElsevierXML", _elsevier_xml))
    if doi and is_springer_nature_target(doi=doi, url=url):
        tier1.append(("SpringerTDM", lambda: extract_tables_via_springer_tdm(doi, table_idx, cancel_event=cancel_event)))
    if doi:
        tier1.append(("Unpaywall", lambda: extract_tables_via_unpaywall(doi, table_idx, cancel_event=cancel_event)))
    dfs, winner = _race_candidates(
        tier1,
        cancel_event,
        authoritative_names={"ArxivHTML", "NCBIPMC", "ElsevierXML", "SpringerTDM"},
    )

    history.append(f"general-tier1:{winner or 'none'}")
    # 若仅靠非权威 DirectHTTP 在动态出版社页面拿到单个极小表 (可能漏掉折叠/补充表)，继续让浏览器渠道尝试补全
    _tier1_incomplete_hint = (
        dfs
        and table_idx == "all"
        and winner not in ("ArxivHTML", "NCBIPMC", "ElsevierXML", "SpringerTDM", "Unpaywall")
        and len(dfs) == 1
        and len(dfs[0]) <= 2
        and bool(url and any(dom in url.lower() for dom in (
            "sciencedirect.com", "springer.com", "nature.com", "wiley.com",
            "tandfonline.com", "oup.com", "geoscienceworld.org", "lyellcollection.org",
            "acs.org", "rsc.org", "degruyter.com", "mdpi.com", "iopscience.iop.org",
            "frontiersin.org", "science.org", "copernicus.org",
        )))
    )
    if dfs and not _tier1_incomplete_hint:
        return dfs, history
    best_partial = dfs if _tier1_incomplete_hint else []
    if cancelled():
        return best_partial, history

    # Tier-2: Hedged Cascade（先启动低成本本地渠道 Crawl4AI/Scrapling；延迟或快速全失败后再对冲触发云端 Firecrawl）
    tier2_primary = []
    tier2_hedged = []
    if url:
        def _not_neg_cached(ch_name):
            if not callable(is_negatively_cached):
                return True
            hit = is_negatively_cached(ch_name, url=url, doi=doi, config=cfg)
            if hit:
                print(f"[Race] Tier-2 跳过负缓存渠道 {ch_name} ({hit.get('target_key')}, {hit.get('reason')})")
                return False
            return True

        if _not_neg_cached("crawl4ai"):
            tier2_primary.append(("Crawl4AI", lambda: extract_tables_via_crawl4ai(url, table_idx, cancel_event=cancel_event)))
        if _not_neg_cached("scrapling"):
            tier2_primary.append(("Scrapling", lambda: extract_tables_via_scrapling(url, table_idx, cancel_event=cancel_event)))
        if _not_neg_cached("firecrawl"):
            tier2_hedged.append(("Firecrawl", lambda: extract_tables_via_firecrawl(url, api_key, table_idx, cancel_event=cancel_event, doi=doi)))

    try:
        hedge_delay_s = max(0.0, float(cfg.get("TIER2_HEDGE_DELAY_S", 4.0)))
    except (TypeError, ValueError):
        hedge_delay_s = 4.0

    dfs, winner = _hedged_race_candidates(
        tier2_primary,
        tier2_hedged,
        hedge_delay_s=hedge_delay_s,
        cancel_event=cancel_event,
    )
    history.append(f"general-tier2:{winner or 'none'}")
    if dfs and len(dfs) > len(best_partial):
        return dfs, history
    if cancelled():
        return best_partial, history

    # Tier-3: 共享浏览器 Playwright（锁内串行，含补充材料下载）
    if url:
        print("[Race] General Tier-3: PlaywrightGeneral（浏览器锁内）")
        tier3_state = dict(state)
        tier3_state["_skip_stealth_fallback"] = True
        s, _n = _run_node_locked(PlaywrightGeneralNode(), tier3_state)
        history.append("general-tier3:playwright")
        if s.get("dfs") and len(s["dfs"]) > len(best_partial):
            return s["dfs"], history
        if s.get("title") and not state.get("title"):
            state["title"] = s["title"]
        if cancelled():
            return best_partial, history

    if best_partial:
        return best_partial, history

    # Tier-4: 学术搜索尾流 + BrowserAct（Firecrawl 已在 Tier-2 竞速，此处跳过重复抓取）
    dfs, winner = _tail_race(skip_firecrawl=True)
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

    if state.get("url"):
        orig_url = state["url"]
        resolved_u, resolved_cnki = _resolve_chinese_doi_and_classify(orig_url, bool(state.get("is_cnki")))
        state["url"] = _normalize_publisher_url(resolved_u)
        state["is_cnki"] = resolved_cnki
        if not state.get("doi"):
            extracted_doi = _extract_doi_from_url(state["url"]) or _extract_doi_from_url(orig_url)
            if extracted_doi:
                state["doi"] = extracted_doi
        if state.get("doi") and not state.get("is_cnki") and is_chinese_doi(state["doi"]):
            parsed_host = (urllib.parse.urlparse(state["url"]).hostname or "").lower()
            if parsed_host in ("doi.org", "dx.doi.org", "hdl.handle.net"):
                chndoi_url = f"https://www.chndoi.org/Resolution/Handler?doi={state['doi']}"
                resolved = resolve_chinese_doi(chndoi_url)
                if resolved and resolved != chndoi_url:
                    state["url"] = _normalize_publisher_url(resolved)
                    low_res = resolved.lower()
                    state["is_cnki"] = ("cnki.net" in low_res or "cnki.com.cn" in low_res)

    if race:
        # 并行竞速管线（url 缺失时仍进解析节点做 URL 解析；节点对已有 doi 幂等）
        if not state.get("url"):
            node_state, _n = ResolveMetadataNode().execute(state)
            state.update({k: node_state.get(k) for k in ("url", "doi", "title", "is_cnki")})
        else:
            state["url"] = _normalize_publisher_url(state["url"])
        print(f"[Race] 在线分层竞速开始: URL={state.get('url')}, DOI={state.get('doi')}, is_cnki={state.get('is_cnki')}")
        dfs, history = _race_extract(state, cancel_event)
        print(f"[Race] 竞速管线结束. History: {history}")
        return dfs, state.get("title")

    # ===== 原有串行 DAG 编排（race=False 时完整保留旧行为） =====
    # Initialize the graph
    graph = ExtractionGraph()

    # Add nodes to graph
    graph.add_node("ResolveMetadataNode", ResolveMetadataNode())
    graph.add_node("ArxivHTMLNode", ArxivHTMLNode(fallback_node="DirectHTTPNode"))
    graph.add_node("NCBIPMCNode", NCBIPMCNode(fallback_node="DirectHTTPNode"))
    graph.add_node("DirectHTTPNode", DirectHTTPNode(fallback_node="ImpersonatedHTTPNode"))
    graph.add_node("ImpersonatedHTTPNode", ImpersonatedHTTPNode(fallback_node="Crawl4AIGeneralNode"))
    graph.add_node("ElsevierXMLNode", ElsevierXMLNode(fallback_node="Crawl4AIGeneralNode"))
    graph.add_node("SpringerTDMNode", SpringerTDMNode(fallback_node="ImpersonatedHTTPNode"))


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
        cfg = load_config() if callable(load_config) else {}
        if state.get("is_cnki"):
            graph.set_entry_point("Crawl4AICNKINode")
        elif state.get("is_arxiv") and cfg.get("ARXIV_HTML_ENABLED", True):
            graph.set_entry_point("ArxivHTMLNode")
        elif state.get("is_pmc") and cfg.get("NCBI_PMC_ENABLED", True):
            graph.set_entry_point("NCBIPMCNode")
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
