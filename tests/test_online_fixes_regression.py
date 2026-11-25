import json
import os
import subprocess
import sys
import threading
import time
import types
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import scripts.common as common
import scripts.online.cnki_html_extractor as cnki_extractor
import scripts.online.doi_resolver as doi_resolver
import scripts.online.general_html_extractor as ghe
general_html_extractor = ghe
import scripts.online.graph as graph
import scripts.online.strategies as strategies


class _DummyResponse:
    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data if json_data is not None else {}
        self.headers = headers if headers is not None else {}

    def json(self):
        return self._json


class TestFirecrawlCircuitBreaker(unittest.TestCase):
    def setUp(self):
        strategies._FIRECRAWL_DISABLED = False
        strategies._FIRECRAWL_MEMO.clear()
        strategies._OA_CHECK_CACHE.clear()

    def tearDown(self):
        strategies._FIRECRAWL_DISABLED = False
        strategies._FIRECRAWL_MEMO.clear()
        strategies._OA_CHECK_CACHE.clear()

    def test_403_trips_global_circuit_breaker_across_modules(self):
        forbidden = _DummyResponse(403, '{"success":false,"error":"Unauthorized: This account has been banned"}')
        with patch.object(strategies.requests, "post", return_value=forbidden) as mock_strat_post, \
             patch.object(doi_resolver.requests, "get") as mock_doi_get:
            # 1st call trips circuit breaker on 403
            dfs1 = strategies.extract_tables_via_firecrawl("https://example.com/a", api_key="fc-test", is_oa=True)
            self.assertEqual(dfs1, [])
            self.assertTrue(strategies.is_firecrawl_disabled())
            self.assertEqual(mock_strat_post.call_count, 1)

            # 2nd call to strategies skips HTTP entirely
            dfs2 = strategies.extract_tables_via_firecrawl("https://example.com/b", api_key="fc-test", is_oa=True)
            self.assertEqual(dfs2, [])
            self.assertEqual(mock_strat_post.call_count, 1)

            # doi_resolver shares the same circuit breaker and skips HTTP
            res_doi = doi_resolver.get_doi_by_title_firecrawl("Antimony deposit geology", api_key="fc-test")
            self.assertIsNone(res_doi)
            mock_doi_get.assert_not_called()

            # general_html_extractor custom grid fallback also skips HTTP
            mock_page = MagicMock()
            mock_page.content.return_value = "<html><body><div>grid</div></body></html>"
            dfs_grid = ghe._extract_custom_grid_tables(mock_page, "https://arxiv.org/html/2401.00001", api_key="fc-test")
            self.assertEqual(dfs_grid, [])
            self.assertEqual(mock_strat_post.call_count, 1)

    def test_401_and_402_trip_from_doi_resolver(self):
        for code in (401, 402):
            strategies._FIRECRAWL_DISABLED = False
            strategies._FIRECRAWL_MEMO.clear()
            err_resp = _DummyResponse(code, "error")
            with patch.object(doi_resolver.requests, "get", return_value=err_resp) as mock_get:
                self.assertIsNone(doi_resolver.get_doi_by_title_firecrawl("Some Paper Title", api_key="fc-test"))
                self.assertTrue(strategies.is_firecrawl_disabled())
                self.assertEqual(mock_get.call_count, 1)

    def test_429_wait_respects_cancel_event(self):
        rate_limited = _DummyResponse(429, "rate limited")
        cancel_ev = threading.Event()
        cancel_ev.set()
        with patch.object(strategies.requests, "post", return_value=rate_limited) as mock_post:
            dfs = strategies.extract_tables_via_firecrawl(
                "https://example.com/rl", api_key="fc-test", cancel_event=cancel_ev, is_oa=True
            )
            self.assertEqual(dfs, [])
            self.assertEqual(mock_post.call_count, 0)

        cancel_ev2 = threading.Event()
        def _post_and_cancel(*args, **kwargs):
            cancel_ev2.set()
            return rate_limited

        with patch.object(strategies.requests, "post", side_effect=_post_and_cancel) as mock_post2:
            dfs2 = strategies.extract_tables_via_firecrawl(
                "https://example.com/rl2", api_key="fc-test", cancel_event=cancel_ev2, is_oa=True
            )
            self.assertEqual(dfs2, [])
            self.assertEqual(mock_post2.call_count, 1)


class TestDirectHTTPAndSubpages(unittest.TestCase):
    def test_direct_http_fetches_each_subpage_only_once(self):
        main_html = """
        <html><body>
          <a href="/article/10.1007/s123/tables/1">Table 1</a>
          <a href="/article/10.1007/s123/tables/2">Table 2</a>
        </body></html>
        """
        sub1_html = """
        <main>
          <header class="u-mb-16"><h1 class="c-article-satellite-title">Table 1 Sample</h1></header>
          <div class="c-article-table-container">
            <table><thead><tr><th>ID</th><th>Sb</th></tr></thead><tbody><tr><td>A</td><td>12.5</td></tr></tbody></table>
          </div>
        </main>
        """
        sub2_html = """
        <main>
          <header class="u-mb-16"><h1 class="c-article-satellite-title">Table 2 Results</h1></header>
          <div class="c-article-table-container">
            <table><thead><tr><th>Site</th><th>Au</th></tr></thead><tbody><tr><td>B</td><td>1.2</td></tr></tbody></table>
          </div>
        </main>
        """
        visited = []

        def fake_get(url, **kwargs):
            visited.append(url)
            if url.endswith("/tables/1"):
                return _DummyResponse(200, sub1_html)
            if url.endswith("/tables/2"):
                return _DummyResponse(200, sub2_html)
            return _DummyResponse(200, main_html)

        with patch.object(strategies, "requests_impersonate", None), \
             patch.object(strategies.requests, "get", side_effect=fake_get):
            dfs = strategies.extract_tables_via_direct_http("https://link.springer.com/article/10.1007/s123")

        self.assertEqual(len(dfs), 2)
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")
        self.assertEqual(dfs[1].attrs.get("label"), "Table 2")
        self.assertEqual(
            visited,
            [
                "https://link.springer.com/article/10.1007/s123",
                "https://link.springer.com/article/10.1007/s123/tables/1",
                "https://link.springer.com/article/10.1007/s123/tables/2",
            ],
        )

    def test_direct_http_obeys_cancel_event(self):
        ev = threading.Event()
        ev.set()
        with patch.object(strategies, "requests_impersonate", None), \
             patch.object(strategies.requests, "get") as mock_get:
            dfs = strategies.extract_tables_via_direct_http("https://example.com/article", cancel_event=ev)
            self.assertEqual(dfs, [])
            mock_get.assert_not_called()


class TestSupplementarySubprocessCancellation(unittest.TestCase):
    def test_supplementary_popen_cancels_and_terminates_quickly(self):
        # Pre-set cancel_event returns before spawning subprocess
        pre_ev = threading.Event()
        pre_ev.set()
        with patch.object(subprocess, "Popen") as mock_pre_popen:
            self.assertEqual(
                strategies.extract_general_supplementary_tables("https://example.com/a", cancel_event=pre_ev),
                [],
            )
            mock_pre_popen.assert_not_called()

        # Cancel arriving while child process is running terminates child immediately
        fake_proc = MagicMock()
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None
        cancel_ev = threading.Event()

        def _spawn_and_cancel(*args, **kwargs):
            cancel_ev.set()
            return fake_proc

        with patch.object(os.path, "exists", return_value=True), \
             patch.object(subprocess, "Popen", side_effect=_spawn_and_cancel), \
             patch.object(subprocess, "run") as mock_taskkill:
            dfs = strategies.extract_general_supplementary_tables(
                "https://example.com/article", cancel_event=cancel_ev, max_timeout=10
            )
            self.assertEqual(dfs, [])
            self.assertTrue(fake_proc.kill.called or mock_taskkill.called)

    def test_supplementary_timeout_kills_stalled_subprocess(self):
        fake_proc = MagicMock()
        fake_proc.pid = 88888
        fake_proc.poll.return_value = None

        mono_vals = iter([100.0, 200.0, 200.0])
        with patch.object(os.path, "exists", return_value=True), \
             patch.object(subprocess, "Popen", return_value=fake_proc), \
             patch.object(subprocess, "run") as mock_taskkill, \
             patch.object(time, "monotonic", side_effect=lambda: next(mono_vals, 200.0)):
            dfs = strategies.extract_general_supplementary_tables(
                "https://example.com/article", cancel_event=None, max_timeout=5
            )
            self.assertEqual(dfs, [])
            self.assertTrue(fake_proc.kill.called or mock_taskkill.called)


class TestChndoiRoutingAndGatewayTitles(unittest.TestCase):
    def test_resolve_metadata_routes_chndoi_to_cnki(self):
        chndoi_url = "https://www.chndoi.org/Resolution/Handler?doi=10.19658/j.issn.1007-2802.2023.42.025"
        cnki_url = "https://link.cnki.net/doi/10.19658/j.issn.1007-2802.2023.42.025"
        with patch.object(graph, "resolve_chinese_doi", return_value=cnki_url):
            node = graph.ResolveMetadataNode(fallback_node="NextNode")
            state, nxt = node.execute({"pdf_path": chndoi_url})
            self.assertEqual(nxt, "Crawl4AICNKINode")
            self.assertEqual(state["url"], cnki_url)
            self.assertTrue(state["is_cnki"])
            self.assertEqual(state["doi"], "10.19658/j.issn.1007-2802.2023.42.025")

    def test_extract_tables_online_pre_resolved_chndoi_sets_is_cnki(self):
        chndoi_url = "https://www.chndoi.org/Resolution/Handler?doi=10.19658/j.issn.1007-2802.2023.42.025"
        cnki_url = "https://link.cnki.net/doi/10.19658/j.issn.1007-2802.2023.42.025"
        with patch.object(graph, "resolve_chinese_doi", return_value=cnki_url), \
             patch.object(graph, "_race_extract") as mock_race:
            mock_race.side_effect = lambda st, ce=None: ([pd.DataFrame({"A": [1]})], ["cnki"])
            dfs, _title = graph.extract_tables_online(
                pdf_path="",
                pre_resolved={"url": chndoi_url, "doi": "10.19658/j.issn.1007-2802.2023.42.025", "is_cnki": False},
                race=True,
            )
            self.assertEqual(len(dfs), 1)
            passed_state = mock_race.call_args[0][0]
            self.assertEqual(passed_state["url"], cnki_url)
            self.assertTrue(passed_state["is_cnki"])

    def test_playwright_general_node_passes_cancel_skip_stealth_and_45s_supp_timeout(self):
        ev = threading.Event()
        fake_df = pd.DataFrame({"A": [1], "B": [2]})
        with patch.object(graph, "load_config", return_value={}), \
             patch.object(graph, "get_domain_profile_dir", return_value="profile"), \
             patch.object(graph, "browser_task_concurrency_guard") as mock_guard, \
             patch.object(ghe, "extract_general_html_tables", return_value=([fake_df], "Paper Title")) as mock_pw, \
             patch.object(graph, "extract_general_supplementary_tables", return_value=[]) as mock_supp:
            mock_guard.return_value.__enter__ = lambda s: None
            mock_guard.return_value.__exit__ = lambda s, *a: False
            node = graph.PlaywrightGeneralNode()
            state, nxt = node.execute({
                "url": "https://link.springer.com/article/10.1007/test",
                "_skip_stealth_fallback": True,
                "cancel_event": ev,
            })
            self.assertIsNone(nxt)
            self.assertEqual(len(state["dfs"]), 1)
            self.assertTrue(mock_pw.call_args.kwargs.get("skip_stealth_fallback"))
            self.assertIs(mock_pw.call_args.kwargs.get("cancel_event"), ev)
            self.assertEqual(mock_supp.call_args.kwargs.get("max_timeout"), 30)
            self.assertIs(mock_supp.call_args.kwargs.get("cancel_event"), ev)

    def test_academic_search_node_ignores_bogus_cnki_gateway_title(self):
        node = graph.AcademicSearchGraphNode(fallback_node=None)
        with patch.object(graph.requests, "get") as mock_get:
            state, _ = node.execute({"doi": None, "title": "CNKI原文下载服务"})
            mock_get.assert_not_called()
            self.assertFalse(state.get("dfs"))


class TestGeneralHtmlFastFailAndCrawl4AI(unittest.TestCase):
    def test_extract_general_html_skips_stealth_on_cancel_or_skip_flag(self):
        ev = threading.Event()
        ev.set()
        with patch.object(ghe, "_stealth_fallback_extract") as mock_stealth:
            dfs, title = ghe.extract_general_html_tables("https://example.com/x", cancel_event=ev)
            self.assertEqual(dfs, [])
            self.assertIsNone(title)
            mock_stealth.assert_not_called()

    def test_crawl4ai_follows_springer_subpages_in_session(self):
        main_html = '<a href="/article/10.1007/s999/tables/1">T1</a><a href="/article/10.1007/s999/tables/2">T2</a>'
        sub1_html = '<main><header class="u-mb-16"><h1>Table 1 Main</h1></header><div class="c-article-table-container"><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></div></main>'
        sub2_html = '<main><header class="u-mb-16"><h1>Table 2 Extra</h1></header><div class="c-article-table-container"><table><tr><th>C</th><th>D</th></tr><tr><td>3</td><td>4</td></tr></table></div></main>'

        class FakeResult:
            def __init__(self, html):
                self.success = True
                self.html = html
                self.markdown = ""
                self.tables = []
                self.error_message = ""

        class FakeCrawler:
            def __init__(self, config=None):
                self.urls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def arun(self, url, config=None):
                self.urls.append(url)
                if url.endswith("/tables/1"):
                    return FakeResult(sub1_html)
                if url.endswith("/tables/2"):
                    return FakeResult(sub2_html)
                return FakeResult(main_html)

        fake_crawl4ai = types.ModuleType("crawl4ai")
        fake_crawl4ai.AsyncWebCrawler = FakeCrawler
        fake_crawl4ai.BrowserConfig = lambda **kw: kw
        fake_crawl4ai.CrawlerRunConfig = lambda **kw: kw
        fake_crawl4ai.CacheMode = types.SimpleNamespace(BYPASS="bypass")

        fake_table_mod = types.ModuleType("crawl4ai.table_extraction")
        fake_table_mod.DefaultTableExtraction = lambda: object()

        def _sync_drive(coro):
            # Drive async def coroutines that complete without OS I/O so tests also run cleanly
            # when Windows ProactorEventLoop socketpair() is blocked by offline_guard.
            try:
                while True:
                    coro.send(None)
            except StopIteration as stop:
                return stop.value

        class _DoneTask:
            def __init__(self, coro):
                self._val = _sync_drive(coro)

            def result(self):
                return self._val

            def cancel(self):
                pass

        async def _instant_wait(tasks, **kwargs):
            return set(tasks), set()

        import asyncio as _asyncio
        with patch.dict(sys.modules, {"crawl4ai": fake_crawl4ai, "crawl4ai.table_extraction": fake_table_mod}), \
             patch.object(_asyncio, "run", side_effect=_sync_drive), \
             patch.object(_asyncio, "ensure_future", side_effect=_DoneTask), \
             patch.object(_asyncio, "wait", side_effect=_instant_wait), \
             patch.object(strategies, "_crawl4ai_cdp_url", return_value=None), \
             patch.object(strategies, "_crawl4ai_domain_blocked", return_value=False), \
             patch.object(strategies, "_crawl4ai_semaphore", return_value=threading.Semaphore(1)), \
             patch.object(strategies, "load_config", return_value={}), \
             patch.object(strategies, "get_crawl4ai_cookies_path", return_value="__nonexistent__.json"):
            dfs = strategies.extract_tables_via_crawl4ai("https://link.springer.com/article/10.1007/s999")

        self.assertEqual(len(dfs), 2)
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")
        self.assertEqual(dfs[1].attrs.get("label"), "Table 2")


class TestDOMLabelsAndNoiseFiltering(unittest.TestCase):
    def test_springer_silverchair_magtech_and_supplementary_labels(self):
        html = """
        <html><body>
          <main>
            <header class="u-mb-16"><h1 class="c-article-satellite-title">Table 1 Trace elements</h1></header>
            <div class="c-article-table-container">
              <table><thead><tr><th>El</th><th>Val</th></tr></thead><tbody><tr><td>Sb</td><td>12</td></tr></tbody></table>
            </div>
          </main>
          <section class="figure-table-wrapper">
            <div class="table-wrap">
              <span class="label">Table S1</span>
              <div class="caption">Supplementary geochemical data</div>
              <div class="table-overflow">
                <table><thead><tr><th>Sample</th><th>Au</th></tr></thead><tbody><tr><td>S1</td><td>0.5</td></tr></tbody></table>
              </div>
              <div class="table-modal">
                <table><thead><tr><th>Sample</th><th>Au</th></tr></thead><tbody><tr><td>S1</td><td>0.5</td></tr></tbody></table>
              </div>
            </div>
          </section>
          <div class="figure table_new">
            <div class="figureTitle">表\xa0\r\n\t 2 矿石矿物化学成分</div>
            <div class="article_table_fullText">
              <table><thead><tr><th>矿物</th><th>含量</th></tr></thead><tbody><tr><td>辉锑矿</td><td>71%</td></tr></tbody></table>
            </div>
          </div>
          <div class="copyCitationInfo">
            <table><tr><td>DOI: 10.1007/test</td><td>复制</td></tr></table>
          </div>
          <div class="messageBoard">
            <table><tr><td><input name="n"/></td><td><textarea name="m"></textarea></td></tr></table>
          </div>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(html)
        self.assertEqual(len(dfs), 3)
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")
        self.assertEqual(dfs[1].attrs.get("label"), "Table S1")
        self.assertIn("Table S1", dfs[1].attrs.get("table_title", ""))
        self.assertEqual(dfs[2].attrs.get("label"), "表2")
        self.assertEqual(dfs[2].attrs.get("table_title"), "表 2 矿石矿物化学成分")

    def test_appendix_table_label_and_single_column_layout_filtering(self):
        self.assertEqual(
            strategies._extract_normalized_table_label("Appendix Table A1 Geochemistry"),
            "Appendix Table A1",
        )
        self.assertEqual(
            strategies._extract_normalized_table_label("Table S2 Trace Elements"),
            "Table S2",
        )

        view_abstract_html = """
        <html><body>
          <table>
            <tr><td>张三, 李四. 湖南某锑矿成矿特征[J]. 地质论丛, 2024.</td></tr>
            <tr><td>点此下载全文</td></tr>
            <tr><td>引用本文: 张三等, 2024</td></tr>
            <tr><td>摘要：本文研究了……</td></tr>
            <tr><td>关键词：锑矿</td></tr>
          </table>
          <table>
            <tr><td>作者</td><td>单位</td></tr>
            <tr><td>张三</td><td>中国地质大学</td></tr>
            <tr><td>李四</td><td>中国科学院</td></tr>
          </table>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(view_abstract_html)
        self.assertEqual(dfs, [])

    def test_nested_outer_layout_table_preserves_inner_data_table(self):
        html = """
        <html><body>
          <table>
            <tr><td>
              <p>Table 3 Ore assay</p>
              <table>
                <thead><tr><th>Sample</th><th>Sb (%)</th></tr></thead>
                <tbody><tr><td>M1</td><td>4.8</td></tr></tbody>
              </table>
            </td></tr>
          </table>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(html)
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].shape, (1, 2))

    def test_markdown_layout_shell_filtered_and_labels_normalized(self):
        md_layout = """
| | |
| --- | --- |
| 何永淼等. 湖南西部钨锑金矿床[J]. 地质找矿论丛 | |
| **标题** | |
| 投稿时间：2012-11-06 | |

表 1 测试数据表

| 样品 | Sb (%) |
| --- | --- |
| A1 | 4.8 |

Table S2 Supplementary values

| Sample | Au |
| --- | --- |
| B1 | 0.2 |
"""
        dfs = strategies.parse_tables_from_markdown(md_layout)
        self.assertEqual(len(dfs), 2)
        self.assertEqual(dfs[0].attrs.get("label"), "表1")
        self.assertEqual(dfs[1].attrs.get("label"), "Table S2")

    def test_cnki_extract_tables_from_page_sets_label(self):
        def _cell(txt):
            c = MagicMock()
            c.inner_text.return_value = txt
            c.get_attribute.return_value = None
            return c

        r0 = MagicMock()
        r0.query_selector_all.return_value = [_cell("样品"), _cell("δ34S")]
        r1 = MagicMock()
        r1.query_selector_all.return_value = [_cell("XT-1"), _cell("3.2")]
        tbl = MagicMock()
        tbl.evaluate.return_value = "表1 小屯乡矿床辉锑矿S同位素组成"
        tbl.query_selector_all.side_effect = lambda sel: [r0, r1] if sel == "tr" else []
        page = MagicMock()
        page.query_selector_all.return_value = [tbl]

        dfs = cnki_extractor.extract_tables_from_page(page)
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].attrs.get("label"), "表1")
        self.assertIn("表1", dfs[0].attrs.get("table_title", ""))

    def test_single_col_unlabeled_js_shell_filtered_even_with_default_fallback(self):
        page = MagicMock()
        page.evaluate.return_value = [
            {
                "caption": "",
                "footnote": "",
                "rows": [["点此下载全文"], ["摘要：本文研究了某锑矿床特征"], ["关键词：锑矿"]],
            },
            {
                "caption": "",
                "footnote": "",
                "rows": [["Items"], ["Alpha"], ["Beta"]],
            },
            {
                "caption": "Table 1. Valid 1-col",
                "footnote": "",
                "rows": [["Sample"], ["S1"], ["S2"]],
            },
            {
                "caption": "",
                "footnote": "",
                "rows": [["Sample", "Sb (%)"], ["S1", "4.5"], ["S2", "3.2"]],
            },
        ]
        dfs = ghe.extract_tables_from_element(page, table_idx="all", default_title="")
        self.assertEqual(len(dfs), 3)
        self.assertEqual(list(dfs[0].columns), ["Items"])
        self.assertEqual(dfs[1].attrs.get("label"), "Table 1")
        self.assertEqual(dfs[2].shape, (2, 2))

    def test_clean_table_filename_and_extract_table_label_extended_prefixes(self):
        cases = [
            ("Appendix Table A1 Geochemistry", "Appendix Table A1 Geochemistry", "Appendix Table A1"),
            ("Supplementary Table S1 Trace elements", "Supplementary Table S1 Trace elements", "Supplementary Table S1"),
            ("Supplemental Table 2 Isotopes", "Supplemental Table 2 Isotopes", "Supplemental Table 2"),
            ("Extended Data Table 3 Summary", "Extended Data Table 3 Summary", "Extended Data Table 3"),
            ("Table A1 Spatial variations", "Table A1 Spatial variations", "Table A1"),
            ("附表S1 矿石成分", "附表S1 矿石成分", "附表S1"),
            ("表 1 矿石成分", "表 1 矿石成分", "表1"),
        ]
        for raw, expected_fname, expected_lbl in cases:
            cleaned = common.clean_table_filename(raw, 1)
            self.assertEqual(cleaned, expected_fname, f"clean_table_filename failed for {raw!r}")
            lbl = ghe.extract_table_label_from_title(cleaned)
            self.assertEqual(lbl, expected_lbl, f"extract_table_label_from_title failed for {cleaned!r}")

    def test_resolve_chinese_doi_avoids_outer_div_oversea_trap(self):
        html = """
        <html><body>
          <div class="wrapper">
            <ul>
              <li><a href="https://oversea.cnki.net/KCMS/detail/detail.aspx?dbcode=CJFD">CNKI (境外)</a></li>
              <li><a href="https://link.cnki.net/doi/10.16539/j.ddgzyckx.2022.01.001">CNKI (境内)</a></li>
            </ul>
          </div>
        </body></html>
        """
        resp = MagicMock()
        resp.status_code = 200
        resp.text = html
        with patch.object(doi_resolver.requests, "get", return_value=resp):
            resolved = doi_resolver.resolve_chinese_doi("https://www.chndoi.org/Resolution/Handler?doi=10.16539/test")
        self.assertEqual(resolved, "https://link.cnki.net/doi/10.16539/j.ddgzyckx.2022.01.001")

    def test_playwright_general_node_skips_supp_subprocess_on_dead_http(self):
        node = graph.PlaywrightGeneralNode()

        def _fake_extract(*args, **kwargs):
            meta_out = kwargs.get("meta_out")
            if isinstance(meta_out, dict):
                meta_out["was_dead_http"] = True
            return [], "404 Not Found"

        with patch.object(ghe, "extract_general_html_tables", side_effect=_fake_extract), \
             patch.object(graph, "extract_general_supplementary_tables") as mock_supp:
            state = {"url": "https://example.org/404", "table_idx": "all", "headed": False}
            out_state, next_node = node.execute(state)
        mock_supp.assert_not_called()
        self.assertFalse(out_state.get("dfs"))

    def test_extract_general_html_tables_dead_http_skips_probes_and_cancel_cleans_tmp_profile(self):
        created_profiles = []
        fake_page = MagicMock()
        fake_resp = MagicMock()
        fake_resp.status = 404
        fake_page.goto.return_value = fake_resp
        fake_page.url = "https://example.org/missing"
        fake_page.title.return_value = "404 Not Found"
        fake_page.evaluate.return_value = "Page not found"

        fake_context = MagicMock()
        fake_context.pages = [fake_page]
        fake_browser = MagicMock()
        fake_p = MagicMock()
        fake_pw_cm = MagicMock()
        fake_pw_cm.__enter__.return_value = fake_p
        fake_pw_cm.__exit__.return_value = False

        # Case 1: 404 sets meta_out["was_dead_http"]=True and skips frames/custom grid/links
        meta = {}
        fake_page.locator.return_value.count.return_value = 0
        with patch.object(ghe, "sync_playwright", return_value=fake_pw_cm), \
             patch.object(ghe, "find_playwright_chromium", return_value=None), \
             patch.object(ghe, "_try_real_chrome_user_data", return_value=None), \
             patch.object(ghe, "_launch_context_or_browser", return_value=(fake_browser, fake_context)), \
             patch.object(ghe, "_extract_custom_grid_tables") as mock_custom:
            dfs, title = ghe.extract_general_html_tables(
                "https://example.org/missing", _allow_headed_fallback=False, meta_out=meta
            )
        self.assertEqual(dfs, [])
        self.assertTrue(meta.get("was_dead_http"))
        mock_custom.assert_not_called()
        fake_page.query_selector_all.assert_not_called()
        fake_browser.close.assert_called_once()

        # Case 2: cancel_event set during page.wait_for_timeout still runs CDP tab cleanup (page.close, NOT browser.close)
        cancel_ev = threading.Event()
        cdp_page = MagicMock()
        cdp_page.url = "https://example.org/ok"
        cdp_page.goto.return_value = MagicMock(status=200)
        cdp_page.wait_for_timeout.side_effect = lambda _ms: cancel_ev.set()
        cdp_ctx = MagicMock()
        cdp_ctx.pages = []
        cdp_ctx.new_page.return_value = cdp_page
        cdp_browser = MagicMock()
        cdp_browser.contexts = [cdp_ctx]
        fake_p.chromium.connect_over_cdp.return_value = cdp_browser
        with patch.object(ghe, "sync_playwright", return_value=fake_pw_cm), \
             patch.object(ghe, "find_playwright_chromium", return_value=None), \
             patch.object(ghe, "_try_real_chrome_user_data", return_value=None), \
             patch("scripts.online.cdp_detector.find_active_cdp_endpoint", return_value={"cdp_url": "http://127.0.0.1:9222", "port": 9222, "source": "test"}), \
             patch("scripts.online.cdp_detector.find_matching_cdp_page", return_value=None):
            dfs2, _ = ghe.extract_general_html_tables(
                "https://example.org/ok", cancel_event=cancel_ev, _allow_headed_fallback=False
            )
        self.assertEqual(dfs2, [])
        cdp_page.close.assert_called_once()
        cdp_browser.close.assert_not_called()

    def test_crawl4ai_subpages_run_when_c4_tables_has_noise_and_c4_filters_metadata(self):
        main_html = """
        <html><body>
          <table class="layout-nav"><tr><td>Nav</td></tr></table>
          <a href="/article/10.1007/s999/tables/1">Table 1</a>
        </body></html>
        """
        t1_html = """
        <html><body>
          <header class="u-mb-16"><h1 class="c-article-satellite-title">Table 1 Trace elements</h1></header>
          <table><tr><th>Sample</th><th>Sb (ppm)</th></tr><tr><td>A1</td><td>150</td></tr></table>
        </body></html>
        """
        noise_c4 = [{"headers": ["Author", "Email", "Affiliation"], "rows": [["Alice", "a@b.c", "Univ"]]}]

        class _FakeCrawler:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                return False
            async def arun(self, url, config=None):
                if url.endswith("/tables/1"):
                    return types.SimpleNamespace(success=True, html=t1_html, markdown="", tables=[], error_message="")
                return types.SimpleNamespace(success=True, html=main_html, markdown="", tables=noise_c4, error_message="")

        fake_crawl4ai = types.ModuleType("crawl4ai")
        fake_crawl4ai.AsyncWebCrawler = _FakeCrawler
        fake_crawl4ai.BrowserConfig = lambda **kw: kw
        fake_crawl4ai.CrawlerRunConfig = lambda **kw: kw
        fake_crawl4ai.CacheMode = types.SimpleNamespace(BYPASS="BYPASS")
        fake_table_mod = types.ModuleType("crawl4ai.table_extraction")
        fake_table_mod.DefaultTableExtraction = lambda: object()

        def _sync_drive(coro):
            try:
                while True:
                    coro.send(None)
            except StopIteration as stop:
                return stop.value

        class _DoneTask:
            def __init__(self, coro):
                self._val = _sync_drive(coro)
            def result(self):
                return self._val
            def cancel(self):
                pass

        async def _instant_wait(tasks, **kwargs):
            return set(tasks), set()

        import asyncio as _asyncio
        with patch.dict(sys.modules, {"crawl4ai": fake_crawl4ai, "crawl4ai.table_extraction": fake_table_mod}), \
             patch.object(_asyncio, "run", side_effect=_sync_drive), \
             patch.object(_asyncio, "ensure_future", side_effect=_DoneTask), \
             patch.object(_asyncio, "wait", side_effect=_instant_wait), \
             patch.object(strategies, "_crawl4ai_cdp_url", return_value=None), \
             patch.object(strategies, "_crawl4ai_domain_blocked", return_value=False), \
             patch.object(strategies, "_crawl4ai_semaphore", return_value=threading.Semaphore(1)), \
             patch.object(strategies, "load_config", return_value={}), \
             patch.object(strategies, "get_crawl4ai_cookies_path", return_value="__nonexistent__.json"):
            dfs = strategies.extract_tables_via_crawl4ai("https://link.springer.com/article/10.1007/s999")
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")

        # Also test parsed_c4 filtering when there are no HTML tables and no subpages:
        # metadata table is dropped, valid data table gets normalized label.
        class _FakeCrawlerC4Only(_FakeCrawler):
            async def arun(self, url, config=None):
                return types.SimpleNamespace(
                    success=True,
                    html="<html><body><p>text</p></body></html>",
                    markdown="",
                    tables=[
                        {"headers": ["作者", "单位", "E-mail"], "rows": [["张三", "某大学", "z@u.cn"]]},
                        {"caption": "表 2 稀土元素分析", "headers": ["样品", "La"], "rows": [["S1", "12.4"]]},
                    ],
                    error_message="",
                )

        fake_crawl4ai.AsyncWebCrawler = _FakeCrawlerC4Only
        with patch.dict(sys.modules, {"crawl4ai": fake_crawl4ai, "crawl4ai.table_extraction": fake_table_mod}), \
             patch.object(_asyncio, "run", side_effect=_sync_drive), \
             patch.object(_asyncio, "ensure_future", side_effect=_DoneTask), \
             patch.object(_asyncio, "wait", side_effect=_instant_wait), \
             patch.object(strategies, "_crawl4ai_cdp_url", return_value=None), \
             patch.object(strategies, "_crawl4ai_domain_blocked", return_value=False), \
             patch.object(strategies, "_crawl4ai_semaphore", return_value=threading.Semaphore(1)), \
             patch.object(strategies, "load_config", return_value={}), \
             patch.object(strategies, "get_crawl4ai_cookies_path", return_value="__nonexistent__.json"):
            dfs_c4 = strategies.extract_tables_via_crawl4ai("https://example.org/c4only")
        self.assertEqual(len(dfs_c4), 1)
        self.assertEqual(dfs_c4[0].attrs.get("label"), "表2")

    def test_academic_search_and_scrapling_cnki_respect_cancel_event(self):
        ev = threading.Event()
        ev.set()
        node = graph.AcademicSearchGraphNode()
        with patch.object(graph, "extract_tables_via_unpaywall") as mock_unpaywall:
            out_state, next_node = node.execute({"doi": "10.1000/test", "cancel_event": ev})
        mock_unpaywall.assert_not_called()
        self.assertIsNone(next_node)

        with patch.object(doi_resolver, "resolve_chinese_doi") as mock_resolve:
            cnki_dfs = strategies.extract_cnki_tables_via_scrapling(
                "https://www.chndoi.org/Resolution/Handler?doi=10.1/x", cancel_event=ev
            )
        mock_resolve.assert_not_called()
        self.assertEqual(cnki_dfs, [])


class TestRound2FixesRegression(unittest.TestCase):
    def test_general_html_extractor_handles_target_closed_on_title_and_links(self):
        class TargetClosedError(Exception):
            pass

        class _ClosedDuringTitlePage:
            def __init__(self, fail_on_call=1):
                self.url = "https://link.springer.com/article/10.1007/test"
                self.frames = []
                self._title_calls = 0
                self._fail_on_call = fail_on_call
                self.queried_links = False

            def goto(self, *a, **kw):
                return SimpleNamespace(status=200)

            def wait_for_timeout(self, *a, **kw):
                pass

            def wait_for_load_state(self, *a, **kw):
                pass

            def wait_for_selector(self, *a, **kw):
                pass

            def locator(self, *a, **kw):
                return SimpleNamespace(count=lambda: 0)

            def add_init_script(self, *a, **kw):
                pass

            def evaluate(self, script, *a, **kw):
                if "innerText" in str(script):
                    return "Checking your browser" if self._fail_on_call == 2 else ""
                return False

            def title(self):
                self._title_calls += 1
                if self._title_calls >= self._fail_on_call:
                    raise TargetClosedError("Page.title: Target page, context or browser has been closed")
                return "Just a moment..."

            def is_closed(self):
                return self._title_calls >= self._fail_on_call

            def query_selector_all(self, sel):
                if sel == "a":
                    self.queried_links = True
                return []

            def close(self):
                pass

        for fail_on_call, headed in ((1, False), (2, True)):
            fake_page = _ClosedDuringTitlePage(fail_on_call=fail_on_call)
            fake_ctx = SimpleNamespace(
                pages=[fake_page],
                new_page=lambda: fake_page,
                route=lambda *a, **kw: None,
                add_init_script=lambda *a, **kw: None,
                add_cookies=lambda *a, **kw: None,
                close=lambda: None,
            )
            fake_browser = SimpleNamespace(contexts=[fake_ctx], new_context=lambda *a, **kw: fake_ctx, close=lambda: None)
            fake_pw_cm = MagicMock()
            fake_pw_cm.__enter__.return_value = SimpleNamespace(
                chromium=SimpleNamespace(
                    launch_persistent_context=lambda *a, **kw: fake_ctx,
                    launch=lambda *a, **kw: fake_browser,
                )
            )
            with patch.object(general_html_extractor, "sync_playwright", return_value=fake_pw_cm), \
                 patch.object(general_html_extractor, "_launch_context_or_browser", return_value=(fake_browser, fake_ctx)), \
                 patch.object(general_html_extractor, "load_config", return_value={}), \
                 patch.object(general_html_extractor, "_try_real_chrome_user_data", return_value=""), \
                 patch.object(general_html_extractor, "_dismiss_cookie_consent"), \
                 patch.object(general_html_extractor, "_expand_silverchair_gsw_tables"), \
                 patch.object(general_html_extractor, "_purge_boilerplate_dom"), \
                 patch("scripts.online.cdp_detector.find_active_cdp_endpoint", return_value=None):
                dfs, title = general_html_extractor.extract_general_html_tables(
                    "https://link.springer.com/article/10.1007/test",
                    headed=headed,
                    _allow_headed_fallback=False,
                    skip_stealth_fallback=True,
                )
            self.assertEqual(dfs, [])
            self.assertFalse(fake_page.queried_links)

    def test_crawl4ai_cdp_config_isolated_context_and_fallback_on_cdp_error(self):
        import asyncio as _asyncio

        fake_crawl4ai = types.ModuleType("crawl4ai")
        fake_crawl4ai.BrowserConfig = lambda **kw: kw
        fake_crawl4ai.CrawlerRunConfig = lambda **kw: kw
        fake_crawl4ai.CacheMode = SimpleNamespace(BYPASS="bypass")

        seen_configs = []

        class _FakeFallbackCrawler:
            def __init__(self, config=None):
                self.config = config
                seen_configs.append(config)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def arun(self, url, config=None):
                if self.config.get("cdp_url"):
                    raise RuntimeError("Failed on navigating ACS-GOTO: Page.goto: net::ERR_ABORTED; maybe frame was detached?")
                return SimpleNamespace(
                    success=True,
                    html="<html><body><p>Table 1. Data</p><table><tr><th>Sample</th><th>Sb</th></tr><tr><td>A1</td><td>12.5</td></tr></table></body></html>",
                    markdown="",
                    tables=[],
                )

        fake_crawl4ai.AsyncWebCrawler = _FakeFallbackCrawler
        fake_table_mod = types.ModuleType("crawl4ai.table_extraction")
        fake_table_mod.DefaultTableExtraction = lambda: object()

        class _DoneTask:
            def __init__(self, coro):
                self._res = _sync_drive(coro)

            def result(self):
                return self._res

            def cancel(self):
                pass

        def _sync_drive(coro):
            try:
                coro.send(None)
            except StopIteration as stop:
                return stop.value

        def _instant_wait(tasks, timeout=None):
            return set(tasks), set()

        strategies._CRAWL4AI_CDP_CACHE["checked"] = True
        strategies._CRAWL4AI_CDP_CACHE["url"] = "http://127.0.0.1:9222"
        with patch.dict(sys.modules, {"crawl4ai": fake_crawl4ai, "crawl4ai.table_extraction": fake_table_mod}), \
             patch.object(_asyncio, "run", side_effect=_sync_drive), \
             patch.object(_asyncio, "ensure_future", side_effect=_DoneTask), \
             patch.object(_asyncio, "wait", side_effect=_instant_wait), \
             patch.object(strategies, "_crawl4ai_cdp_url", return_value="http://127.0.0.1:9222"), \
             patch.object(strategies, "_crawl4ai_domain_blocked", return_value=False), \
             patch.object(strategies, "_crawl4ai_record") as mock_record, \
             patch.object(strategies, "_crawl4ai_semaphore", return_value=threading.Semaphore(1)), \
             patch.object(strategies, "load_config", return_value={}), \
             patch.object(strategies, "get_crawl4ai_cookies_path", return_value="__nonexistent__.json"):
            dfs = strategies.extract_tables_via_crawl4ai("https://example.org/cdp-fallback")

        self.assertEqual(len(seen_configs), 2)
        self.assertEqual(seen_configs[0].get("cdp_url"), "http://127.0.0.1:9222")
        self.assertTrue(seen_configs[0].get("create_isolated_context"))
        self.assertTrue(seen_configs[0].get("cdp_cleanup_on_close"))
        self.assertEqual(seen_configs[0].get("cdp_close_delay"), 0.0)
        self.assertIsNone(seen_configs[1].get("cdp_url"))
        self.assertTrue(seen_configs[1].get("headless"))
        self.assertTrue(seen_configs[1].get("enable_stealth"))
        self.assertFalse(strategies._CRAWL4AI_CDP_CACHE["checked"])
        self.assertIsNone(strategies._CRAWL4AI_CDP_CACHE["url"])
        self.assertEqual(len(dfs), 1)
        mock_record.assert_called_with("https://example.org/cdp-fallback", hard_failure=False)

    def test_crawl4ai_cdp_url_invalidates_dead_cached_endpoint(self):
        strategies._CRAWL4AI_CDP_CACHE["checked"] = True
        strategies._CRAWL4AI_CDP_CACHE["url"] = "http://127.0.0.1:59999"
        with patch.object(strategies, "load_config", return_value={}), \
             patch("scripts.online.cdp_detector.check_cdp_endpoint_alive", return_value=None), \
             patch("scripts.online.cdp_detector.find_active_cdp_endpoint", return_value=None):
            resolved = strategies._crawl4ai_cdp_url()
        self.assertIsNone(resolved)
        self.assertIsNone(strategies._CRAWL4AI_CDP_CACHE["url"])

    def test_chndoi_non_cnki_keeps_is_cnki_false_and_cnki_avoids_networkidle(self):
        chndoi_url = "https://www.chndoi.org/Resolution/Handler?doi=10.3969/test"
        wanfang_url = "https://d.wanfangdata.com.cn/periodical/12345"
        with patch.object(graph, "resolve_chinese_doi", return_value=wanfang_url):
            out_url, is_cnki = graph._resolve_chinese_doi_and_classify(chndoi_url, False)
        self.assertEqual(out_url, wanfang_url)
        self.assertFalse(is_cnki)

        goto_waits = []
        load_state_waits = []

        class _FakeHtmlBtn:
            def count(self):
                return 1

            def is_visible(self, timeout=None):
                return True

            def click(self):
                pass

        class _FakeReaderPage:
            def wait_for_load_state(self, state, timeout=None):
                load_state_waits.append(state)

            def wait_for_selector(self, *a, **kw):
                pass

            def wait_for_timeout(self, *a, **kw):
                pass

            def query_selector(self, *a, **kw):
                return None

            def title(self):
                raise RuntimeError("Target page, context or browser has been closed")

            def close(self):
                pass

        reader_page = _FakeReaderPage()

        class _FakeMainPage:
            url = "https://link.cnki.net/doi/10.19388/j.zgdzdc.2022.02.03"

            def goto(self, u, wait_until=None, timeout=None):
                goto_waits.append(wait_until)

            def wait_for_selector(self, *a, **kw):
                pass

            def locator(self, sel):
                if "HTML阅读" in sel:
                    return SimpleNamespace(first=_FakeHtmlBtn())
                return SimpleNamespace(count=lambda: 0)

            def close(self):
                pass

            def add_init_script(self, *a, **kw):
                pass

        class _CtxMgr:
            def __init__(self, val):
                self.value = val

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        fake_main = _FakeMainPage()
        fake_ctx = SimpleNamespace(
            pages=[fake_main],
            new_page=lambda: fake_main,
            route=lambda *a, **kw: None,
            add_init_script=lambda *a, **kw: None,
            add_cookies=lambda *a, **kw: None,
            expect_page=lambda timeout=None: _CtxMgr(reader_page),
            close=lambda: None,
        )
        fake_browser = SimpleNamespace(contexts=[fake_ctx], new_context=lambda *a, **kw: fake_ctx, close=lambda: None)
        fake_pw_cm = MagicMock()
        fake_pw_cm.__enter__.return_value = SimpleNamespace(
            chromium=SimpleNamespace(
                launch_persistent_context=lambda *a, **kw: fake_ctx,
                launch=lambda *a, **kw: fake_browser,
            )
        )
        with patch.object(cnki_extractor, "sync_playwright", return_value=fake_pw_cm), \
             patch("scripts.online.cdp_detector.find_active_cdp_endpoint", return_value=None):
            dfs, title = cnki_extractor.extract_cnki_html_tables("https://link.cnki.net/doi/10.19388/j.zgdzdc.2022.02.03")
        self.assertEqual(dfs, [])
        self.assertIsNone(title)
        self.assertEqual(goto_waits, ["domcontentloaded"])
        self.assertEqual(load_state_waits, ["domcontentloaded"])

    def test_parse_tables_survives_index_error_and_filters_author_affiliation_grid(self):
        from scripts.table_postprocess import read_html_preserving_text

        html = """
        <html><body>
          <table id="broken"><tr><th>A</th></tr></table>
          <p>Table 1. Major elements</p>
          <table id="good"><tr><th>Sample</th><th>Sb</th></tr><tr><td>S1</td><td>4.2</td></tr></table>
        </body></html>
        """
        orig_read_html = pd.read_html
        call_count = {"n": 0}

        def _flaky_read_html(buf, *args, **kwargs):
            call_count["n"] += 1
            txt = buf.getvalue() if hasattr(buf, "getvalue") else str(buf)
            if call_count["n"] == 1 or 'id="broken"' in txt:
                raise IndexError("list index out of range")
            return orig_read_html(buf, *args, **kwargs)

        with patch("pandas.read_html", side_effect=_flaky_read_html):
            dfs = read_html_preserving_text(html)
        self.assertEqual(len(dfs), 1)
        self.assertEqual(list(dfs[0].columns), ["Sample", "Sb"])

        author_df = pd.DataFrame(
            [
                ["张嘉玮", "2,3,4 *"],
                ["胡瑞忠", "1"],
                ["罗开", "5"],
                ["祝亚男", "4"],
                ["尹润生", "1"],
            ],
            columns=["邓昌州", "1"],
        )
        self.assertTrue(common.is_metadata_table(author_df))

        real_two_col_df = pd.DataFrame(
            [
                ["Sample A", "12.4"],
                ["Sample B", "18.9"],
                ["Sample C", "7.1"],
            ],
            columns=["Sample", "Sb (ppm)"],
        )
        self.assertFalse(common.is_metadata_table(real_two_col_df))

    def test_gsw_owned_debug_chrome_cleaned_on_exception_and_no_headed_fallback(self):
        from scripts.online import general_html_extractor

        class _FakeProc:
            def __init__(self):
                self.pid = 424242
                self.terminated = False

            def poll(self):
                return 0 if self.terminated else None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.terminated = True

        fake_proc = _FakeProc()
        orig_procs = list(general_html_extractor._OWNED_DEBUG_CHROME_PROCS)
        general_html_extractor._OWNED_DEBUG_CHROME_PROCS.clear()

        def _fake_ensure():
            general_html_extractor._OWNED_DEBUG_CHROME_PROCS.append(fake_proc)
            return True

        fake_pw_cm = MagicMock()
        fake_pw_cm.__enter__.side_effect = RuntimeError("simulated playwright crash")

        try:
            with patch.object(general_html_extractor, "_ensure_chrome_debug_running", side_effect=_fake_ensure), \
                 patch.object(general_html_extractor, "sync_playwright", return_value=fake_pw_cm), \
                 patch.object(general_html_extractor, "load_config", return_value={}), \
                 patch.object(general_html_extractor, "_try_real_chrome_user_data", return_value=""):
                with self.assertRaises(RuntimeError):
                    general_html_extractor.extract_general_html_tables(
                        "https://pubs.geoscienceworld.org/segweb/economicgeology/article/1/2",
                        _allow_headed_fallback=False,
                    )
            self.assertTrue(fake_proc.terminated)
            self.assertEqual(general_html_extractor._OWNED_DEBUG_CHROME_PROCS, [])
        finally:
            general_html_extractor._OWNED_DEBUG_CHROME_PROCS[:] = orig_procs

    def test_resolve_chinese_doi_redirect_and_relative_links(self):
        from scripts.online import doi_resolver

        redirect_resp = SimpleNamespace(
            status_code=200,
            url="https://www.wanfangdata.com.cn/details/detail.do?_type=perio&id=zgdz202001",
            text="<html></html>",
        )
        with patch("scripts.online.doi_resolver.requests.get", return_value=redirect_resp):
            resolved = doi_resolver.resolve_chinese_doi("https://www.chndoi.org/Resolution/Handler?doi=10.1234/wf")
        self.assertEqual(resolved, redirect_resp.url)

        rel_html = """
        <html><body>
          <ul>
            <li><a href="/jump?target=wanfang">(境内) 万方数据</a></li>
          </ul>
        </body></html>
        """
        rel_resp = SimpleNamespace(
            status_code=200,
            url="https://www.chndoi.org/Resolution/Handler?doi=10.1234/rel",
            text=rel_html,
        )
        with patch("scripts.online.doi_resolver.requests.get", return_value=rel_resp):
            resolved_rel = doi_resolver.resolve_chinese_doi("https://www.chndoi.org/Resolution/Handler?doi=10.1234/rel")
        self.assertEqual(resolved_rel, "https://www.chndoi.org/jump?target=wanfang")

    def test_crawl4ai_passes_table_extraction_kwarg_not_table_extraction_strategy(self):
        from scripts.online import strategies

        captured_run_kwargs = {}

        class _FakeBrowserConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class _FakeCrawlerRunConfig:
            def __init__(self, table_extraction=None, cache_mode=None, page_timeout=None, wait_for=None):
                captured_run_kwargs["table_extraction"] = table_extraction

        class _FakeAsyncWebCrawler:
            def __init__(self, config=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def arun(self, url, config=None):
                return SimpleNamespace(
                    success=True,
                    html="<html><title>Paper</title><body><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>",
                    tables=[],
                    markdown="",
                    error_message="",
                )

        class _FakeDefaultTableExtraction:
            pass

        fake_crawl4ai = types.ModuleType("crawl4ai")
        fake_crawl4ai.AsyncWebCrawler = _FakeAsyncWebCrawler
        fake_crawl4ai.BrowserConfig = _FakeBrowserConfig
        fake_crawl4ai.CrawlerRunConfig = _FakeCrawlerRunConfig
        fake_crawl4ai.CacheMode = SimpleNamespace(BYPASS="bypass")
        fake_table_mod = types.ModuleType("crawl4ai.table_extraction")
        fake_table_mod.DefaultTableExtraction = _FakeDefaultTableExtraction

        import asyncio as _asyncio

        class _DoneTask:
            def __init__(self, coro):
                self._coro = coro
                self._done = False
                self._res = None
                self._exc = None

            def run_sync(self):
                if not self._done:
                    try:
                        while True:
                            self._coro.send(None)
                    except StopIteration as stop:
                        self._res = stop.value
                    except BaseException as exc:
                        self._exc = exc
                    finally:
                        self._done = True

            def cancel(self):
                return False

            def result(self):
                self.run_sync()
                if self._exc is not None:
                    raise self._exc
                return self._res

        def _sync_drive(coro):
            try:
                while True:
                    coro.send(None)
            except StopIteration as stop:
                return stop.value

        def _instant_wait(tasks, timeout=None):
            return set(tasks), set()

        with patch.dict(sys.modules, {"crawl4ai": fake_crawl4ai, "crawl4ai.table_extraction": fake_table_mod}), \
             patch.object(_asyncio, "run", side_effect=_sync_drive), \
             patch.object(_asyncio, "ensure_future", side_effect=_DoneTask), \
             patch.object(_asyncio, "wait", side_effect=_instant_wait), \
             patch.object(strategies, "_crawl4ai_cdp_url", return_value=None), \
             patch.object(strategies, "_crawl4ai_domain_blocked", return_value=False), \
             patch.object(strategies, "_crawl4ai_record"), \
             patch.object(strategies, "_crawl4ai_semaphore", return_value=threading.Semaphore(1)), \
             patch.object(strategies, "load_config", return_value={}), \
             patch.object(strategies, "get_crawl4ai_cookies_path", return_value="__nonexistent__.json"):
            dfs = strategies.extract_tables_via_crawl4ai("https://example.org/paper")
        self.assertEqual(len(dfs), 1)
        self.assertIsInstance(captured_run_kwargs.get("table_extraction"), _FakeDefaultTableExtraction)

    def test_playwright_general_node_allows_105s_supp_timeout_when_no_main_tables(self):
        with patch.object(graph, "load_config", return_value={}), \
             patch.object(graph, "get_domain_profile_dir", return_value="profile"), \
             patch.object(graph, "browser_task_concurrency_guard") as mock_guard, \
             patch.object(ghe, "extract_general_html_tables", return_value=([], "Paper Title")), \
             patch.object(graph, "extract_general_supplementary_tables", return_value=[pd.DataFrame({"S": [1]})]) as mock_supp:
            mock_guard.return_value.__enter__ = lambda s: None
            mock_guard.return_value.__exit__ = lambda s, *a: False
            node = graph.PlaywrightGeneralNode()
            state, nxt = node.execute({"url": "https://onlinelibrary.wiley.com/doi/full/10.1111/ter.12201"})
        self.assertIsNone(nxt)
        self.assertEqual(len(state["dfs"]), 1)
        self.assertEqual(mock_supp.call_args.kwargs.get("max_timeout"), 30)

    def test_firecrawl_skips_non_oa_and_pdf_urls_before_api_call(self):
        strategies._FIRECRAWL_DISABLED = False
        strategies._FIRECRAWL_MEMO.clear()
        strategies._OA_CHECK_CACHE.clear()

        # Closed-access DOI -> Unpaywall is_oa=False -> never calls Firecrawl /v2/scrape
        closed_unpaywall = _DummyResponse(200, json_data={"is_oa": False})
        with patch.object(strategies.requests, "get", return_value=closed_unpaywall) as mock_get, \
             patch.object(strategies.requests, "post") as mock_post:
            dfs = strategies.extract_tables_via_firecrawl(
                "https://link.springer.com/article/10.1007/s001260050242",
                api_key="fc-test",
                doi="10.1007/s001260050242",
            )
            self.assertEqual(dfs, [])
            self.assertEqual(mock_get.call_count, 1)
            mock_post.assert_not_called()

            # Cached non-OA DOI skips even Unpaywall on repeat lookup
            dfs2 = strategies.extract_tables_via_firecrawl(
                "https://link.springer.com/article/10.1007/s001260050242",
                api_key="fc-test",
            )
            self.assertEqual(dfs2, [])
            self.assertEqual(mock_get.call_count, 1)
            mock_post.assert_not_called()

            # Custom-grid Firecrawl fallback also respects OA gate
            mock_page = MagicMock()
            self.assertEqual(
                ghe._extract_custom_grid_tables(
                    mock_page,
                    "https://link.springer.com/article/10.1007/s001260050242",
                    api_key="fc-test",
                ),
                [],
            )
            mock_post.assert_not_called()

        # Direct .pdf links never call Firecrawl even if domain is OA
        with patch.object(strategies.requests, "post") as mock_post_pdf:
            self.assertEqual(
                strategies.extract_tables_via_firecrawl(
                    "https://www.mdpi.com/2075-163X/14/2/141/pdf",
                    api_key="fc-test",
                ),
                [],
            )
            mock_post_pdf.assert_not_called()

    def test_firecrawl_prefers_html_over_json_and_filters_metadata_pseudo_tables(self):
        strategies._FIRECRAWL_DISABLED = False
        strategies._FIRECRAWL_MEMO.clear()

        # 1) HTML has 3 real tables, JSON truncated to 1 -> must return all 3 HTML tables
        html_3 = (
            "<html><body>"
            "<table><tr><th>S</th><th>Sb</th></tr><tr><td>1</td><td>12.3</td></tr></table>"
            "<table><tr><th>S</th><th>Au</th></tr><tr><td>2</td><td>0.4</td></tr></table>"
            "<table><tr><th>S</th><th>As</th></tr><tr><td>3</td><td>5.1</td></tr></table>"
            "</body></html>"
        )
        payload = {
            "html": html_3,
            "json": {"tables": [{"table_label": "Table 1", "headers": ["S", "Sb"], "rows": [["1", "12.3"]]}]},
        }
        dfs = strategies._firecrawl_parse_response(payload, "all")
        self.assertEqual(len(dfs), 3)

        # 2) Paywalled 0-<table> page with LLM fake tables (citation, multi-row author+email, attachment, zoom) -> []
        fake_payload = {
            "html": "<html><body><h1>Abstract Only</h1></body></html>",
            "json": {
                "tables": [
                    {"table_label": "Table 1", "headers": ["Title", "Authors", "Journal", "Volume", "Issue", "Date", "Pages"], "rows": [["T", "A", "Lithos", "32", "3", "1994", "1-10"]]},
                    {"table_label": "Table 2", "headers": ["Author", "Affiliation", "Email"], "rows": [["Alice", "Univ 610082", "a@b.c"]] * 5},
                    {"table_label": "Table 3", "headers": ["Filename", "Description"], "rows": [["sup.docx", "23 KB"]]},
                    {"table_label": "Table 4", "headers": ["Zoom Option"], "rows": [["Actual Size"]]},
                    {"table_label": "Table 5", "headers": ["Field", "Details"], "rows": [["Topic", "Antimony"]]},
                ]
            },
        }
        self.assertEqual(strategies._firecrawl_parse_response(fake_payload, "all"), [])

        # 3) Default Firecrawl scrape payload uses lightweight ["html", "markdown"] without json schema
        captured = {}
        ok_resp = _DummyResponse(200, json_data={"success": True, "data": {"html": html_3}})
        def _capture_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return ok_resp

        with patch.object(strategies, "_firecrawl_read_cache", return_value=None), \
             patch.object(strategies, "_firecrawl_write_cache"), \
             patch.object(strategies, "load_config", return_value={"FIRECRAWL_API_KEY": "fc-test"}), \
             patch.object(strategies.requests, "post", side_effect=_capture_post):
            dfs_live = strategies.extract_tables_via_firecrawl(
                "https://www.mdpi.com/2075-163X/14/2/141",
                api_key="fc-test",
            )
        self.assertEqual(len(dfs_live), 3)
        self.assertEqual(captured["json"]["formats"], ["html", "markdown"])

    def test_unpaywall_tries_direct_http_before_firecrawl_and_skips_pdf(self):
        strategies._FIRECRAWL_DISABLED = False
        strategies._FIRECRAWL_MEMO.clear()

        unpaywall_json = {
            "is_oa": True,
            "best_oa_location": {"url_for_landing_page": "https://example.org/oa/article/view/1/2"},
        }
        up_resp = _DummyResponse(200, json_data=unpaywall_json)
        pdf_resp = _DummyResponse(200, "%PDF-1.7 binary")
        pdf_resp.headers = {"Content-Type": "application/pdf"}
        pdf_resp.content = b"%PDF-1.7 binary"

        with patch.object(strategies.requests, "get", side_effect=[up_resp, pdf_resp]), \
             patch.object(strategies, "extract_tables_via_firecrawl") as mock_fc:
            self.assertEqual(strategies.extract_tables_via_unpaywall("10.1000/pdf_oa"), [])
            mock_fc.assert_not_called()

        blocked_resp = _DummyResponse(403, "Just a moment...")
        blocked_resp.headers = {"Content-Type": "text/html"}
        with patch.object(strategies.requests, "get", side_effect=[up_resp, blocked_resp]), \
             patch.object(strategies, "load_config", return_value={"FIRECRAWL_API_KEY": "fc-test"}), \
             patch.object(strategies, "extract_tables_via_firecrawl", return_value=[pd.DataFrame({"A": [1]})]) as mock_fc2:
            dfs = strategies.extract_tables_via_unpaywall("10.1000/html_oa")
            self.assertEqual(len(dfs), 1)
            self.assertTrue(mock_fc2.call_args.kwargs.get("is_oa"))

    def test_supplementary_timeout_budget_and_playwright_node_cap(self):
        import inspect
        from scripts.online import graph, strategies

        sig = inspect.signature(strategies.extract_general_supplementary_tables)
        self.assertLessEqual(sig.parameters["max_timeout"].default, 30)

        captured_timeouts = []
        def _fake_supp(url, cancel_event=None, max_timeout=30):
            captured_timeouts.append(max_timeout)
            return []

        node = graph.PlaywrightGeneralNode(fallback_node="ScraplingGeneralNode")
        with patch("scripts.online.general_html_extractor.extract_general_html_tables", return_value=([], "Blocked")), \
             patch.object(graph, "load_config", return_value={}), \
             patch.object(graph, "get_domain_profile_dir", return_value=""), \
             patch.object(graph, "extract_general_supplementary_tables", side_effect=_fake_supp):
            state = {"url": "https://pubs.geoscienceworld.org/article/1", "table_idx": "all"}
            _out_state, next_node = node.execute(state)
        self.assertEqual(next_node, "ScraplingGeneralNode")
        self.assertEqual(len(captured_timeouts), 1)
        self.assertLessEqual(captured_timeouts[0], 30)

    def test_springer_gsw_label_preservation_and_modal_dedup(self):
        from scripts.online import general_html_extractor, strategies

        self.assertEqual(general_html_extractor.extract_table_label_from_title("ESM 1: Table S1 Trace elements"), "Table S1")
        self.assertEqual(general_html_extractor.extract_table_label_from_title("Additional file 2: Appendix Table A1"), "Appendix Table A1")
        self.assertEqual(strategies._extract_normalized_table_label("ESM 1. Table S1 Results"), "Table S1")

        # Silverchair/GSW duplicate .table-overflow and detached .table-modal must deduplicate identical Table 2
        gsw_html = """
        <html><body>
          <div class="table-wrap">
            <div class="table-wrap-title"><span class="label">Table 2</span><div class="caption">Sulfur isotopes</div></div>
            <div class="table-overflow"><table><tr><th>Sample</th><th>d34S</th></tr><tr><td>Sb-1</td><td>2.4</td></tr></table></div>
          </div>
          <div class="table-modal">
            <div class="table-wrap-title"><span class="label">Table 2</span><div class="caption">Sulfur isotopes</div></div>
            <table><tr><th>Sample</th><th>d34S</th></tr><tr><td>Sb-1</td><td>2.4</td></tr></table>
          </div>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(gsw_html)
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].attrs.get("label"), "Table 2")

    def test_springer_idp_redirect_timeout_recovery(self):
        from scripts.online import cdp_detector

        class _FakeIdpPage:
            def __init__(self):
                self.url = "https://idp.springer.com/authorize?response_type=cookie&client_id=springerlink&redirect_uri=https%3A%2F%2Flink.springer.com%2Farticle%2F10.1007%2Fs00126-024-01333-9"
                self.visited = []

            def wait_for_timeout(self, ms):
                pass

            def goto(self, url, wait_until="domcontentloaded", timeout=20000):
                self.visited.append(url)
                self.url = url

            def content(self):
                return "<html><body><main><table><tr><th>A</th></tr><tr><td>1</td></tr></table></main></body></html>"

        fake_page = _FakeIdpPage()
        ok = cdp_detector.wait_through_transient_challenge(fake_page, max_wait_s=2.0)
        self.assertTrue(ok)
        self.assertEqual(fake_page.visited, ["https://link.springer.com/article/10.1007/s00126-024-01333-9"])

    def test_gsw_cookie_injection_discovers_profile_and_companion_stores(self):
        import json
        import tempfile
        from scripts.online import general_html_extractor

        class _FakeContext:
            def __init__(self):
                self.added = []

            def cookies(self):
                return [{"name": "existing_ck", "domain": ".geoscienceworld.org", "path": "/"}]

            def add_cookies(self, cookies):
                self.added.extend(cookies)

        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_file = os.path.join(tmpdir, "cookies.json")
            with open(cookie_file, "w", encoding="utf-8") as f:
                json.dump([
                    {"name": "existing_ck", "value": "old", "domain": ".geoscienceworld.org", "path": "/"},
                    {"name": "gsw_sess", "value": "abc123", "domain": ".geoscienceworld.org", "path": "/"},
                ], f)
            ctx = _FakeContext()
            with patch("scripts.common.get_companion_cookie_paths", return_value=[]):
                general_html_extractor._inject_cookies_from_profile(ctx, tmpdir, url="https://pubs.geoscienceworld.org/article/1")
        names = [c["name"] for c in ctx.added]
        self.assertIn("gsw_sess", names)
        self.assertNotIn("existing_ck", names)

    def test_chndoi_meta_refresh_and_dynamic_cnki_reroute(self):
        from scripts.online import doi_resolver, graph

        meta_html = '<html><head><meta http-equiv="refresh" content="0;url=https://link.cnki.net/doi/10.19388/j.zgdzdc.2022.02.03"></head></html>'
        meta_resp = _DummyResponse(200, text=meta_html)
        meta_resp.url = "https://www.chndoi.org/Resolution/Handler?doi=10.19388/j.zgdzdc.2022.02.03"
        with patch.object(doi_resolver.requests, "get", return_value=meta_resp):
            resolved = doi_resolver.resolve_chinese_doi("https://www.chndoi.org/Resolution/Handler?doi=10.19388/j.zgdzdc.2022.02.03")
        self.assertEqual(resolved, "https://link.cnki.net/doi/10.19388/j.zgdzdc.2022.02.03")

        def _fake_general_extract(url, table_idx="all", headed=False, user_data_dir=None, api_key=None, cancel_event=None, skip_stealth_fallback=False, meta_out=None):
            if isinstance(meta_out, dict):
                meta_out["reroute_cnki_url"] = "https://link.cnki.net/doi/10.19388/j.zgdzdc.2022.02.03"
            return [], ""

        cnki_df = pd.DataFrame({"Col": [1]})
        node = graph.PlaywrightGeneralNode(fallback_node="ScraplingGeneralNode")
        with patch("scripts.online.general_html_extractor.extract_general_html_tables", side_effect=_fake_general_extract), \
             patch.object(graph, "load_config", return_value={}), \
             patch.object(graph, "get_domain_profile_dir", return_value=""), \
             patch.object(graph, "extract_general_supplementary_tables") as mock_supp, \
             patch.object(graph.PlaywrightCNKINode, "execute", side_effect=lambda st: ({**st, "dfs": [cnki_df]}, None)) as mock_cnki:
            st = {"url": "https://www.chndoi.org/Resolution/Handler?doi=10.19388/j.zgdzdc.2022.02.03", "table_idx": "all"}
            out_st, next_node = node.execute(st)
        mock_supp.assert_not_called()
        mock_cnki.assert_called_once()
        self.assertTrue(out_st.get("is_cnki"))
        self.assertEqual(out_st.get("url"), "https://link.cnki.net/doi/10.19388/j.zgdzdc.2022.02.03")
        self.assertEqual(len(out_st.get("dfs") or []), 1)
        self.assertIsNone(next_node)

    def test_expand_multiline_subrows_no_int_len_crash(self):
        from scripts.table_postprocess import expand_multiline_subrows

        df = pd.DataFrame({
            "Sample": ["S1", "S2", None],
            "Value": ["10\n20", "30\n40", "50"],
        })
        res = expand_multiline_subrows(df)
        self.assertEqual(len(res), 5)
        self.assertEqual(list(res["Value"]), ["10", "20", "30", "40", "50"])

    def test_parse_tables_from_html_no_future_warning_on_numeric_cols(self):
        import warnings
        from scripts.online.strategies import parse_tables_from_html

        html = """
        <table>
          <thead><tr><th>h k l</th><th>h k l</th><th>val</th></tr></thead>
          <tbody>
            <tr><td>0</td><td>3</td><td>1.5</td></tr>
            <tr><td>1</td><td>2</td><td>2.5</td></tr>
          </tbody>
        </table>
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", FutureWarning)
            dfs = parse_tables_from_html(html, "all")
        future_warnings = [w for w in caught if issubclass(w.category, FutureWarning)]
        self.assertEqual(future_warnings, [])
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].shape, (2, 3))

    def test_format_table_label_period_space_not_subtable(self):
        from scripts.common import format_table_label, TABLE_LABEL_RE

        self.assertEqual(TABLE_LABEL_RE.search("Table 2. A summary of data").group(0), "Table 2")
        self.assertEqual(format_table_label("Table 2. A summary of data"), "Table 2")
        self.assertEqual(format_table_label("Table 2. Comparison of samples"), "Table 2")
        self.assertEqual(format_table_label("Table 2.1"), "Table 2.1")
        self.assertEqual(format_table_label("Table 2-1"), "Table 2-1")
        self.assertEqual(format_table_label("Table 2.a"), "Table 2.a")

    def test_empty_cookies_json_no_error_log(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from scripts.online import general_html_extractor

        class _DummyCtx:
            def cookies(self):
                return []

            def add_cookies(self, cookies):
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_file = os.path.join(tmpdir, "cookies.json")
            with open(cookie_file, "w", encoding="utf-8") as f:
                f.write("")
            buf = io.StringIO()
            with patch("scripts.common.get_companion_cookie_paths", return_value=[]):
                with redirect_stdout(buf):
                    general_html_extractor._inject_cookies_from_profile(_DummyCtx(), tmpdir, url="https://example.com")
        self.assertNotIn("载入 cookies.json 失败", buf.getvalue())

    def test_race_candidates_uses_daemon_threads_and_returns_fast(self):
        import threading
        import time
        from scripts.online.graph import _race_candidates

        observed_daemon = []
        started = threading.Event()

        def slow_fn():
            observed_daemon.append(threading.current_thread().daemon)
            started.set()
            time.sleep(5.0)
            return []

        def fast_fn():
            started.wait(timeout=1.0)
            return [pd.DataFrame({"A": [1, 2], "B": [3, 4]})]

        t0 = time.monotonic()
        dfs, winner = _race_candidates([("Slow", slow_fn), ("Fast", fast_fn)])
        elapsed = time.monotonic() - t0
        self.assertEqual(winner, "Fast")
        self.assertEqual(len(dfs), 1)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(observed_daemon, [True])

    def test_heuristic_grid_rejects_related_articles_prose_list(self):
        from scripts.online.general_html_extractor import _extract_local_heuristic_grids

        class _FakePage:
            def evaluate(self, script):
                return [{
                    "title": "Heuristic Grid 1",
                    "rows": [
                        [
                            "Geology of Gold Deposits at the Marigold Mine, Battle Mountain District, Nevada",
                            "Diversity in Carlin-Style Gold Deposits",
                        ],
                        [
                            "Comparison of the Allchar Au-As-Sb-Tl Deposit with Carlin-Type Gold Deposits",
                            "Diversity in Carlin-Style Gold Deposits",
                        ],
                        [
                            "Ore Genesis Constraints on the Agdarreh and Zarshouran Gold Deposits",
                            "Diversity in Carlin-Style Gold Deposits",
                        ],
                    ],
                }]

        dfs = _extract_local_heuristic_grids(_FakePage(), "all")
        self.assertEqual(dfs, [])

    def test_scan_pdf_dedups_bilingual_labels(self):
        from scripts.extract_zotero_table import scan_pdf_table_expected_labels

        fake_decls = {
            1: [{"label": "表1", "is_continuation": False}, {"label": "Table 1", "is_continuation": False}],
            2: [{"label": "表2", "is_continuation": False}, {"label": "Table 2", "is_continuation": False}],
        }
        with patch("scripts.table_validator.scan_pdf_table_declarations", return_value=fake_decls):
            labels, count = scan_pdf_table_expected_labels("dummy.pdf")
        self.assertEqual(count, 2)
        self.assertEqual(labels, {"表1", "表2"})

    def test_carsi_not_configured_puts_carsi_last_and_skips_idp_wait(self):
        from scripts import common
        from scripts.online import cdp_detector, general_html_extractor

        self.assertFalse(common.is_carsi_configured({"CARSI_ENABLED": False}))
        self.assertTrue(common.is_carsi_configured({"CARSI_ENABLED": True}))
        self.assertTrue(common.is_carsi_configured({"CARSI_URL": "https://carsi.example.edu.cn"}))

        # When CARSI is not configured:
        # 1) idp.springer.com / carsi.edu.cn should not stall in looks_like_transient_challenge_shell
        with patch.object(cdp_detector, "_carsi_configured", return_value=False):
            self.assertFalse(
                cdp_detector.looks_like_transient_challenge_shell(
                    "<html><body>Sign in via CARSI</body></html>",
                    "https://idp.springer.com/authorize?redirect_uri=https%3A%2F%2Flink.springer.com%2Farticle%2F10.1007%2Ftest",
                )
            )

        # 2) headless general extractor should not invoke _try_real_chrome_user_data (CARSI session) up front
        called_chrome_profile = {"count": 0}

        def _fake_real_chrome():
            called_chrome_profile["count"] += 1
            return "C:/fake/chrome"

        fake_pw_cm = MagicMock()
        fake_pw_cm.__enter__.side_effect = RuntimeError("stop before playwright")
        with patch.object(general_html_extractor, "_try_real_chrome_user_data", side_effect=_fake_real_chrome), \
             patch.object(general_html_extractor, "sync_playwright", return_value=fake_pw_cm), \
             patch.object(general_html_extractor, "load_config", return_value={"IS_CAMPUS_NETWORK": True, "CARSI_ENABLED": False}):
            with self.assertRaises(RuntimeError):
                general_html_extractor.extract_general_html_tables(
                    "https://www.sciencedirect.com/science/article/pii/S0375674221001126",
                    headed=False,
                    _allow_headed_fallback=False,
                )
        self.assertEqual(called_chrome_profile["count"], 0)

    def test_cdp_matched_tab_sets_context_without_unbound_local_error(self):
        from scripts.online import general_html_extractor, cdp_detector

        fake_ctx = MagicMock()
        fake_page = MagicMock()
        fake_page.is_closed.return_value = False
        fake_page.url = "https://onlinelibrary.wiley.com/doi/10.1111/1755-6724.12480"
        fake_page.context = fake_ctx
        fake_page.title.return_value = "Wiley Article"
        fake_page.content.return_value = (
            "<html><body><table>"
            "<caption>Table 1</caption>"
            "<thead><tr><th>Element</th><th>Value</th></tr></thead>"
            "<tbody><tr><td>Sb</td><td>12.3</td></tr><tr><td>As</td><td>4.5</td></tr></tbody>"
            "</table></body></html>"
        )
        fake_page.evaluate.return_value = ""

        fake_browser = MagicMock()
        fake_browser.contexts = [fake_ctx]
        fake_pw = MagicMock()
        fake_pw.chromium.connect_over_cdp.return_value = fake_browser
        fake_pw_cm = MagicMock()
        fake_pw_cm.__enter__.return_value = fake_pw

        with patch.object(cdp_detector, "find_active_cdp_endpoint", return_value={"cdp_url": "http://127.0.0.1:9222", "port": 9222, "source": "config"}), \
             patch.object(cdp_detector, "find_matching_cdp_page", return_value=fake_page), \
             patch.object(cdp_detector, "install_cdp_port_guard"), \
             patch.object(cdp_detector, "wait_through_transient_challenge", return_value=True), \
             patch.object(general_html_extractor, "sync_playwright", return_value=fake_pw_cm), \
             patch.object(general_html_extractor, "load_config", return_value={}), \
             patch.object(general_html_extractor, "_inject_cookies_from_profile") as inj_mock:
            dfs, title = general_html_extractor.extract_general_html_tables(
                "https://onlinelibrary.wiley.com/doi/10.1111/1755-6724.12480",
                _allow_headed_fallback=False,
            )
        inj_mock.assert_called_once()
        self.assertEqual(inj_mock.call_args[0][0], fake_ctx)
        self.assertIsInstance(dfs, list)

    def test_setup_env_check_and_dry_run(self):
        import setup_env

        report = setup_env.check_readiness()
        self.assertIn("os", report)
        self.assertIn("modules", report)
        self.assertIn("onnxruntime", report["modules"])
        self.assertIn("patchright", report["modules"])
        self.assertIn("patchright_available", report)
        self.assertIn(report.get("stealth_browser_engine"), ("patchright", "playwright"))
        self.assertTrue(any(p.startswith("patchright") for p in setup_env.ONLINE_EXT_PACKAGES))
        res = setup_env.install_python_deps(with_all=True, dry_run=True)
        self.assertEqual(res["failed"], [])
        self.assertGreaterEqual(len(res["installed"]), 10)
        self.assertTrue(any(p.startswith("patchright") for p in res["installed"]))
        self.assertTrue(setup_env.install_patchright_chromium(dry_run=True))
        with patch.object(setup_env.importlib.util, "find_spec", return_value=None):
            self.assertFalse(setup_env.install_patchright_chromium(dry_run=False))

    def test_mineru_usable_unverified_table_skips_rapidtable_and_step2(self):
        import tempfile
        from scripts import pdf_table_extractor as pte
        import fitz
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "scanned_mineru.pdf")
            doc = fitz.open()
            p0 = doc.new_page(width=595, height=842)
            p0.insert_text((50, 60), "Introduction\nThis study reports trace element compositions of stibnite.")
            page = doc.new_page(width=595, height=842)
            # 10x10 PNG image so page 1 looks like a scanned/image page with no text layer
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False)
            pix.clear_with(255)
            page.insert_image(fitz.Rect(50, 50, 500, 400), pixmap=pix)
            doc.save(pdf_path)
            doc.close()

            df_m = pd.DataFrame(
                [["S1", "12.3", "45.6"], ["S2", "23.4", "56.7"]],
                columns=["Sample", "Sb_ppm", "As_ppm"],
            )
            df_m.attrs = {
                "page_idx": 1,
                "label": "Table 1",
                "table_title": "Table 1. Trace elements",
                "extractor": "mineru_api",
            }

            with patch.object(pte.ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(pte.ocr_client, "call_mineru_structured_api", return_value=[df_m]), \
                 patch.object(pte, "extract_via_rapid_table") as mock_rapid, \
                 patch.object(pte, "extract_via_paddleocr_crops") as mock_crops, \
                 patch.object(pte, "extract_via_find_tables") as mock_ft:
                results, _logs = pte.extract_tables_from_pdf(pdf_path, candidate_pages=[1])

            self.assertEqual(len(results), 1)
            mock_rapid.assert_not_called()
            mock_crops.assert_not_called()
            mock_ft.assert_not_called()

    def test_mineru_figure_only_scanned_pages_skip_rapidtable_ocr(self):
        import tempfile
        from scripts import pdf_table_extractor as pte
        import fitz
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "mineru_with_figure_page.pdf")
            doc = fitz.open()
            p0 = doc.new_page(width=595, height=842)
            p0.insert_text((50, 60), "Table 1. Major elements\nSample SiO2 Al2O3\nA1 65.2 14.1\nA2 66.8 13.9")
            p1 = doc.new_page(width=595, height=842)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False)
            pix.clear_with(200)
            p1.insert_image(fitz.Rect(50, 80, 500, 600), pixmap=pix)
            p1.insert_text((50, 630), "Fig. 1. Geological sketch map of the ore deposit.")
            doc.save(pdf_path)
            doc.close()

            df_m = pd.DataFrame(
                [["A1", "65.2", "14.1"], ["A2", "66.8", "13.9"]],
                columns=["Sample", "SiO2", "Al2O3"],
            )
            df_m.attrs = {
                "page_idx": 0,
                "label": "Table 1",
                "table_title": "Table 1. Major elements",
                "extractor": "mineru_api",
            }

            with patch.object(pte.ocr_client, "is_mineru_available", return_value=True), \
                 patch.object(pte.ocr_client, "call_mineru_structured_api", return_value=[df_m]), \
                 patch.object(pte, "extract_via_rapid_table") as mock_rapid, \
                 patch.object(pte, "extract_via_paddleocr_crops") as mock_crops, \
                 patch.object(pte, "extract_via_ocr") as mock_ocr:
                results, logs = pte.extract_tables_from_pdf(pdf_path)

            self.assertEqual(len(results), 1)
            mock_rapid.assert_not_called()
            mock_crops.assert_not_called()
            mock_ocr.assert_not_called()
            self.assertTrue(any("跳过冗余 OCR 复扫" in l for l in logs))

    def test_aistudio_semaphore_offloads_overflow_to_mineru_in_parallel(self):
        import tempfile
        from scripts import ocr_client

        with tempfile.TemporaryDirectory() as tmpdir:
            dummy_pdf = os.path.join(tmpdir, "sample.pdf")
            with open(dummy_pdf, "wb") as f:
                f.write(b"%PDF-1.4\n%dummy\n")

            cfg = {
                "PADDLEOCR_ACCESS_TOKEN": "fake_aistudio_token",
                "MINERU_API_KEY": "fake_mineru_token",
                "AISTUDIO_MAX_CONCURRENCY": 1,
                "OCR_ENGINE": "auto",
            }
            sem, cap = ocr_client._get_aistudio_semaphore(cfg)
            self.assertEqual(cap, 1)
            self.assertTrue(sem.acquire(blocking=False))
            try:
                with patch.object(ocr_client, "is_mineru_available", return_value=True), \
                     patch.object(ocr_client, "call_mineru_api", return_value="| A | B |\n|---|---|\n| 1 | 2 |") as mock_mineru, \
                     patch.object(ocr_client, "_call_paddleocr_job_unlocked") as mock_http:
                    res = ocr_client.call_paddleocr_job(dummy_pdf, model="PaddleOCR-VL-1.6", config=cfg)
                self.assertTrue(res["success"])
                self.assertTrue(res.get("failover"))
                self.assertEqual(res.get("model"), "mineru")
                mock_mineru.assert_called_once()
                mock_http.assert_not_called()
            finally:
                sem.release()

    def test_adaptive_ocr_timeout_relaxes_for_dissertations(self):
        import tempfile
        import fitz
        from scripts import ocr_client

        with tempfile.TemporaryDirectory() as tmpdir:
            short_pdf = os.path.join(tmpdir, "short.pdf")
            long_pdf = os.path.join(tmpdir, "thesis.pdf")
            d1 = fitz.open()
            for _ in range(10):
                d1.new_page()
            d1.save(short_pdf)
            d1.close()

            d2 = fitz.open()
            for _ in range(80):
                d2.new_page()
            d2.save(long_pdf)
            d2.close()

            self.assertEqual(ocr_client.compute_adaptive_ocr_timeout(short_pdf, base_timeout=600), 600)
            self.assertEqual(ocr_client.compute_adaptive_ocr_timeout(long_pdf, base_timeout=600, page_ranges=[0, 1]), 600)
            self.assertEqual(ocr_client.compute_adaptive_ocr_timeout(long_pdf, base_timeout=600), 1350)
            self.assertEqual(ocr_client.compute_adaptive_ocr_timeout(long_pdf, base_timeout=180), 180)


if __name__ == "__main__":
    unittest.main()
