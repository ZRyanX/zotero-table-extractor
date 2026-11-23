"""online — 在线表格提取包（由 online_utils.py 拆分而来）。

子模块：
  doi_resolver  DOI 解析与知网 URL 定位
  strategies    各渠道抓取策略 + HTML/Markdown 表格解析
  graph         提取图（DAG）编排与各渠道节点实现

所有历史上的公共名称均在此重新导出，外部调用方式不变。
"""

import io
import os
import re
import sqlite3
import urllib.parse
import requests
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
    from ..playwright_utils import find_playwright_chromium
except ImportError:
    try:
        from playwright_utils import find_playwright_chromium
    except ImportError:
        find_playwright_chromium = lambda: None

load_config = common.load_config
get_cnki_cookies_path = common.get_cnki_cookies_path
get_crawl4ai_cookies_path = common.get_crawl4ai_cookies_path
load_cookies_from_file = common.load_cookies_from_file

from .doi_resolver import (
    get_doi_from_zotero_db,
    get_zotero_item_metadata,
    get_doi_from_pdf_content,
    get_doi_by_title,
    get_doi_by_title_firecrawl,
    _resolve_pii_to_doi,
    is_chinese_doi,
    is_cnki_thesis_metadata,
    normalize_title,
    compute_title_similarity,
    resolve_chinese_doi,
    resolve_doi_canonical_url,
    get_cnki_url_from_pdf_content,
    extract_arxiv_id,
    extract_pmcid,
    resolve_pmcid_from_doi,
)
from .strategies import (
    clean_text,
    get_domain_profile_dir,
    interactive_cnki_cookie_capture,
    parse_tables_from_html,
    parse_tables_from_markdown,
    extract_tables_via_paper_fetch,
    extract_tables_via_elsevier_api,
    extract_elsevier_supplementary_tables,
    extract_general_supplementary_tables,
    extract_builtin_supplementary_tables,
    _discover_supplementary_links,
    _parse_elsevier_xocs_tables,
    extract_tables_via_direct_http,
    extract_tables_via_firecrawl,
    extract_tables_via_scrapling,
    extract_tables_via_playwright,
    has_image_based_tables,
    extract_cnki_tables_via_scrapling,
    extract_cnki_tables_via_playwright,
    extract_tables_via_crawl4ai,
    extract_tables_via_unpaywall,
    extract_tables_via_arxiv_html,
    extract_tables_via_ncbi_pmc,
    extract_tables_via_springer_tdm,
    extract_tables_via_europepmc_xml,
    is_springer_nature_target,
    _parse_jats_xml_tables,
)
from .negative_cache import (
    extract_target_key,
    should_negative_cache,
    is_negatively_cached,
    record_negative_cache,
    clear_negative_cache,
)
from .proxy_rewriter import (
    rewrite_ezproxy_url,
    rewrite_webvpn_url,
    decode_webvpn_url,
    is_institutional_proxy_url,
    maybe_rewrite_institutional_url,
    validate_institutional_session,
)
from .cdp_detector import (
    find_active_devtools_port,
    find_active_cdp_endpoint,
    get_active_cdp_url,
    install_cdp_port_guard,
    get_cdp_tab_semaphore,
    cdp_tab_slot_guard,
)
from .graph import (
    ExtractionNode,
    ExtractionGraph,
    ResolveMetadataNode,
    DirectHTTPNode,
    ImpersonatedHTTPNode,
    ElsevierXMLNode,
    SpringerTDMNode,
    ArxivHTMLNode,
    NCBIPMCNode,
    PlaywrightCNKINode,
    ScraplingCNKINode,
    PlaywrightGeneralNode,
    ScraplingGeneralNode,
    PaperFetchNode,
    FirecrawlNode,
    AcademicSearchGraphNode,
    BrowserActBypassNode,
    Crawl4AIGeneralNode,
    Crawl4AICNKINode,
    _hedged_race_candidates,
    extract_tables_online,
)

__all__ = [
    'AcademicSearchGraphNode', 'ArxivHTMLNode', 'BrowserActBypassNode', 'Crawl4AICNKINode',
    'Crawl4AIGeneralNode', 'DirectHTTPNode', 'ElsevierXMLNode', 'ExtractionGraph',
    'ExtractionNode', 'FirecrawlNode', 'ImpersonatedHTTPNode', 'NCBIPMCNode',
    'PaperFetchNode', 'PlaywrightCNKINode', 'PlaywrightGeneralNode', 'ResolveMetadataNode',
    'ScraplingCNKINode', 'ScraplingGeneralNode', 'SpringerTDMNode',
    '_discover_supplementary_links', '_hedged_race_candidates', '_parse_elsevier_xocs_tables',
    '_parse_jats_xml_tables', '_resolve_pii_to_doi', 'clean_text', 'clear_negative_cache',
    'compute_title_similarity', 'decode_webvpn_url',
    'extract_arxiv_id', 'extract_builtin_supplementary_tables',
    'extract_cnki_tables_via_playwright', 'extract_cnki_tables_via_scrapling',
    'extract_elsevier_supplementary_tables', 'extract_general_supplementary_tables',
    'extract_pmcid', 'extract_tables_online', 'extract_tables_via_arxiv_html',
    'extract_tables_via_crawl4ai', 'extract_tables_via_direct_http', 'extract_tables_via_elsevier_api',
    'extract_tables_via_europepmc_xml', 'extract_tables_via_firecrawl', 'extract_tables_via_ncbi_pmc',
    'extract_tables_via_paper_fetch', 'extract_tables_via_playwright', 'extract_tables_via_scrapling',
    'extract_tables_via_springer_tdm', 'extract_tables_via_unpaywall', 'extract_target_key',
    'find_playwright_chromium', 'get_cnki_cookies_path', 'get_cnki_url_from_pdf_content',
    'get_crawl4ai_cookies_path', 'get_doi_by_title', 'get_doi_by_title_firecrawl',
    'get_doi_from_pdf_content', 'get_doi_from_zotero_db', 'get_zotero_item_metadata', 'get_domain_profile_dir',
    'has_image_based_tables', 'interactive_cnki_cookie_capture', 'is_chinese_doi', 'is_cnki_thesis_metadata',
    'is_institutional_proxy_url', 'is_negatively_cached', 'is_springer_nature_target', 'load_config',
    'load_cookies_from_file', 'maybe_rewrite_institutional_url', 'normalize_title',
    'parse_tables_from_html', 'parse_tables_from_markdown', 'record_negative_cache',
    'requests_impersonate', 'resolve_chinese_doi', 'resolve_doi_canonical_url',
    'rewrite_ezproxy_url', 'rewrite_webvpn_url', 'should_negative_cache',
    'validate_institutional_session',
    'find_active_devtools_port', 'find_active_cdp_endpoint', 'get_active_cdp_url',
    'install_cdp_port_guard', 'get_cdp_tab_semaphore', 'cdp_tab_slot_guard',
    'resolve_pmcid_from_doi'
]

