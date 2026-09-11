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

load_config = common.load_config
get_cnki_cookies_path = common.get_cnki_cookies_path
get_crawl4ai_cookies_path = common.get_crawl4ai_cookies_path
load_cookies_from_file = common.load_cookies_from_file

from .doi_resolver import (
    get_doi_from_zotero_db,
    get_doi_from_pdf_content,
    get_doi_by_title,
    get_doi_by_title_firecrawl,
    _resolve_pii_to_doi,
    resolve_chinese_doi,
    resolve_doi_canonical_url,
    get_cnki_url_from_pdf_content,
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
)
from .graph import (
    ExtractionNode,
    ExtractionGraph,
    ResolveMetadataNode,
    DirectHTTPNode,
    ImpersonatedHTTPNode,
    ElsevierXMLNode,
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
    extract_tables_online,
)

__all__ = ['AcademicSearchGraphNode', 'BrowserActBypassNode', 'Crawl4AICNKINode', 'Crawl4AIGeneralNode', 'DirectHTTPNode', 'ElsevierXMLNode', 'ExtractionGraph', 'ExtractionNode', 'FirecrawlNode', 'ImpersonatedHTTPNode', 'PaperFetchNode', 'PlaywrightCNKINode', 'PlaywrightGeneralNode', 'ResolveMetadataNode', 'ScraplingCNKINode', 'ScraplingGeneralNode', '_parse_elsevier_xocs_tables', '_resolve_pii_to_doi', 'clean_text', 'extract_cnki_tables_via_playwright', 'extract_cnki_tables_via_scrapling', 'extract_elsevier_supplementary_tables', 'extract_general_supplementary_tables', 'extract_tables_online', 'extract_tables_via_crawl4ai', 'extract_tables_via_direct_http', 'extract_tables_via_elsevier_api', 'extract_tables_via_firecrawl', 'extract_tables_via_paper_fetch', 'extract_tables_via_playwright', 'extract_tables_via_scrapling', 'extract_tables_via_unpaywall', 'find_playwright_chromium', 'get_cnki_cookies_path', 'get_cnki_url_from_pdf_content', 'get_crawl4ai_cookies_path', 'get_doi_by_title', 'get_doi_by_title_firecrawl', 'get_doi_from_pdf_content', 'get_doi_from_zotero_db', 'get_domain_profile_dir', 'has_image_based_tables', 'interactive_cnki_cookie_capture', 'load_config', 'load_cookies_from_file', 'parse_tables_from_html', 'parse_tables_from_markdown', 'requests_impersonate', 'resolve_chinese_doi', 'resolve_doi_canonical_url']
