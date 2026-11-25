import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
for p in (REPO_ROOT, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import scripts.common as common
import scripts.playwright_utils as playwright_utils
import scripts.online.doi_resolver as doi_resolver
import scripts.online.general_html_extractor as ghe
import scripts.online.cnki_html_extractor as cnki_ext
import scripts.online.graph as graph
import scripts.online.negative_cache as negative_cache
import scripts.online.proxy_rewriter as proxy_rewriter
import scripts.online.strategies as strategies


SAMPLE_JATS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <records>
    <article>
      <body>
        <table-wrap id="Tab1">
          <label>Table 1</label>
          <caption><p>Geochemical composition of samples</p></caption>
          <table>
            <thead>
              <tr><th>Sample</th><th>SiO<sub>2</sub> (wt%)</th><th>Au (ppb)</th></tr>
            </thead>
            <tbody>
              <tr><td>S-01</td><td>68.4</td><td>14.2</td></tr>
              <tr><td>S-02</td><td>70.1</td><td>9.8</td></tr>
            </tbody>
          </table>
          <table-wrap-foot><fn><p>BDL = below detection limit</p></fn></table-wrap-foot>
        </table-wrap>
      </body>
    </article>
  </records>
</response>
"""


class TestP0CompanionCookiesAndConfig(unittest.TestCase):
    def test_companion_cookie_paths_discover_scansci_publisher_carsi_and_instsci(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = os.path.join(tmp, "home")
            cache_dir = os.path.join(fake_home, ".scansci-pdf", "cache")
            carsi_dir = os.path.join(cache_dir, "carsi_cookies")
            os.makedirs(carsi_dir, exist_ok=True)

            pub_path = os.path.join(cache_dir, "publisher_cookies.json")
            inst_path = os.path.join(cache_dir, "instsci-cookies.json")
            carsi_a = os.path.join(carsi_dir, "sciencedirect.json")
            carsi_b = os.path.join(carsi_dir, "springer.json")
            for p in (pub_path, inst_path, carsi_a, carsi_b):
                with open(p, "w", encoding="utf-8") as fh:
                    json.dump([], fh)

            with patch("os.path.expanduser", side_effect=lambda x: x.replace("~", fake_home)), \
                 patch.object(common, "_discover_companion_downloader_roots", return_value=[]):
                paths = common.get_companion_cookie_paths()

            for expected in (pub_path, inst_path, carsi_a, carsi_b):
                self.assertIn(os.path.abspath(expected), paths)

    def test_load_cookies_from_file_handles_domain_keyed_publisher_cookies_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "publisher_cookies.json")
            payload = {
                "springer": [
                    {"name": "idp_session", "value": "abc", "domain": ".springer.com", "path": "/"}
                ],
                "elsevier": {
                    "cookies": [
                        {"name": "sd_session", "value": "xyz", "domain": ".sciencedirect.com", "path": "/", "expirationDate": 1893456000.5}
                    ]
                },
            }
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            loaded = common.load_cookies_from_file(p)
            names = {c["name"] for c in loaded}
            self.assertEqual(names, {"idp_session", "sd_session"})
            sd = [c for c in loaded if c["name"] == "sd_session"][0]
            self.assertEqual(sd["expires"], 1893456000)

    def test_companion_key_map_absorbs_springer_proxy_ezproxy_carsi_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = os.path.join(tmp, "home")
            scansci_dir = os.path.join(fake_home, ".scansci-pdf")
            os.makedirs(scansci_dir, exist_ok=True)
            with open(os.path.join(scansci_dir, "config.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "springer_api_key": "sp_key_from_scansci",
                        "network_proxy": "http://127.0.0.1:7890",
                        "ezproxy_enabled": True,
                        "ezproxy_login_url": "https://ezproxy.example.edu/login?url={url}",
                        "carsi_enabled": True,
                        "carsi_idp_name": "Peking University",
                    },
                    fh,
                )
            cfg_file = os.path.join(tmp, "config.json")
            with open(cfg_file, "w", encoding="utf-8") as fh:
                json.dump({}, fh)

            clean_env = {
                k: v for k, v in os.environ.items()
                if k not in {
                    "SPRINGER_API_KEY", "NETWORK_PROXY", "EZPROXY_ENABLED",
                    "EZPROXY_LOGIN_URL", "CARSI_ENABLED", "CARSI_IDP", "CARSI_IDP_NAME",
                    "ZTE_DISABLE_COMPANION_CREDS",
                }
            }
            with patch.dict(os.environ, clean_env, clear=True), \
                 patch.object(common, "get_config_path", return_value=cfg_file), \
                 patch.object(common, "_discover_companion_downloader_roots", return_value=[]), \
                 patch("os.path.expanduser", side_effect=lambda x: x.replace("~", fake_home)):
                cfg = common.load_config()
                self.assertEqual(cfg["SPRINGER_API_KEY"], "sp_key_from_scansci")
                self.assertEqual(cfg["NETWORK_PROXY"], "http://127.0.0.1:7890")
                self.assertTrue(cfg["EZPROXY_ENABLED"])
                self.assertEqual(cfg["EZPROXY_LOGIN_URL"], "https://ezproxy.example.edu/login?url={url}")
                self.assertTrue(cfg["CARSI_ENABLED"])
                self.assertEqual(cfg["CARSI_IDP"], "Peking University")
                self.assertTrue(common.is_carsi_configured(cfg))
                self.assertEqual(
                    common.get_requests_proxies(cfg),
                    {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
                )


class TestP0SpringerTDMAndEuropePMCXML(unittest.TestCase):
    def test_is_springer_nature_target_prefixes_and_domains(self):
        for doi in (
            "10.1007/s00126-024-01333-9",
            "10.1186/s13059-024-0001-x",
            "10.1038/s41586-024-0001-y",
            "10.1023/A:10123456789",
            "10.1057/s41599-024-0001-z",
            "10.1140/epjc/s10052-024-0001",
        ):
            self.assertTrue(strategies.is_springer_nature_target(doi=doi), f"Expected Springer match for {doi}")
        self.assertFalse(strategies.is_springer_nature_target(doi="10.1016/j.oregeorev.2024.100001"))
        self.assertTrue(strategies.is_springer_nature_target(url="https://www.nature.com/articles/s41586-024-0001-y"))

    def test_springer_tdm_jats_extracts_tables_with_parameterized_source(self):
        seen_urls = []

        def fake_get(url, headers=None, timeout=None, **kwargs):
            seen_urls.append(url)
            return SimpleNamespace(status_code=200, text=SAMPLE_JATS_XML, headers={})

        with patch.object(strategies, "load_config", return_value={"SPRINGER_API_KEY": "sp-secret-123", "SPRINGER_TDM_ENABLED": True}), \
             patch.object(strategies.requests, "get", side_effect=fake_get):
            dfs = strategies.extract_tables_via_springer_tdm("10.1186/s13059-024-0001-x")

        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].attrs.get("extractor"), "springer_tdm_jats")
        self.assertEqual(dfs[0].attrs.get("source"), "springer_tdm_jats")
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")
        self.assertEqual(len(seen_urls), 1)
        self.assertIn("api.springernature.com/xmldata/jats", seen_urls[0])
        self.assertIn("10.1186/s13059-024-0001-x", seen_urls[0])
        self.assertIn("api_key=sp-secret-123", seen_urls[0])

    def test_springer_tdm_skips_when_key_missing(self):
        with patch.object(strategies, "load_config", return_value={"SPRINGER_API_KEY": ""}), \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(strategies.requests, "get") as mock_get:
            os.environ.pop("SPRINGER_API_KEY", None)
            dfs = strategies.extract_tables_via_springer_tdm("10.1038/s41586-024-0001-y")
            self.assertEqual(dfs, [])
            mock_get.assert_not_called()

    def test_springer_tdm_is_authoritative_in_tier1_race(self):
        small_df = pd.DataFrame({"A": ["1"]})
        full_df1 = pd.DataFrame({"Sample": ["S1", "S2"], "Au": ["10", "20"]})
        full_df2 = pd.DataFrame({"Sample": ["S3", "S4"], "Cu": ["30", "40"]})

        def slow_springer(doi, table_idx="all", cancel_event=None):
            time.sleep(0.15)
            return [full_df1, full_df2]

        with patch.object(graph, "load_config", return_value={"NCBI_PMC_ENABLED": False, "ARXIV_HTML_ENABLED": False}), \
             patch.object(graph, "extract_tables_via_direct_http", return_value=[small_df]), \
             patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
             patch.object(graph, "extract_tables_via_springer_tdm", side_effect=slow_springer):
            dfs, history = graph._race_extract({
                "url": "https://link.springer.com/article/10.1038/s41586-024-0001-y",
                "doi": "10.1038/s41586-024-0001-y",
                "is_cnki": False,
                "table_idx": "all",
            })

        self.assertEqual(len(dfs), 2)
        self.assertEqual(history[0], "general-tier1:SpringerTDM")

    def test_europepmc_tries_direct_jats_xml_before_html_scraping(self):
        epmc_search_resp = SimpleNamespace(
            status_code=200,
            json=lambda: {"resultList": {"result": [{"pmcid": "PMC9876543"}]}},
        )
        epmc_xml_resp = SimpleNamespace(status_code=200, text=SAMPLE_JATS_XML, headers={})

        def fake_requests_get(url, **kwargs):
            if "europepmc/webservices/rest/search" in url:
                return epmc_search_resp
            if "europepmc/webservices/rest/PMC9876543/fullTextXML" in url:
                return epmc_xml_resp
            return SimpleNamespace(status_code=404, text="")

        node = graph.AcademicSearchGraphNode(fallback_node=None)
        with patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
             patch.object(graph, "extract_tables_via_ncbi_pmc", return_value=[]), \
             patch("requests.get", side_effect=fake_requests_get), \
             patch.object(graph, "extract_tables_via_direct_http") as mock_html_direct, \
             patch.object(graph, "extract_tables_via_scrapling") as mock_scrapling:
            out_state, nxt = node.execute({"doi": "10.1371/journal.pone.0123456", "table_idx": "all"})

        self.assertIsNone(nxt)
        self.assertEqual(len(out_state["dfs"]), 1)
        self.assertEqual(out_state["dfs"][0].attrs.get("source"), "europepmc_jats")
        mock_html_direct.assert_not_called()
        mock_scrapling.assert_not_called()

    def test_springer_tdm_429_retry_and_cancel_event(self):
        calls = {"n": 0}

        def fake_get(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return SimpleNamespace(status_code=429, text="", headers={"Retry-After": "1"})
            return SimpleNamespace(status_code=200, text=SAMPLE_JATS_XML, headers={})

        with patch.object(strategies, "load_config", return_value={"SPRINGER_API_KEY": "sp-key"}), \
             patch.object(strategies.requests, "get", side_effect=fake_get), \
             patch.object(strategies.time, "sleep") as mock_sleep:
            dfs = strategies.extract_tables_via_springer_tdm("10.1007/s00126-024-01333-9")
        self.assertEqual(len(dfs), 1)
        self.assertEqual(calls["n"], 2)
        mock_sleep.assert_called_once_with(1.0)

        ev = threading.Event()
        ev.set()
        with patch.object(strategies.requests, "get") as mock_get:
            self.assertEqual(strategies.extract_tables_via_springer_tdm("10.1007/s00126-024-01333-9", cancel_event=ev), [])
            mock_get.assert_not_called()


class TestP1BrowserStealthAndLifecycle(unittest.TestCase):
    def test_resolve_sync_playwright_prefers_patchright_for_self_launch_and_respects_monkeypatch(self):
        default_pw = object()
        custom_pw = object()
        # Monkeypatched module sync_playwright is always preserved
        fn, eng = playwright_utils.resolve_sync_playwright(
            module_sync_pw=custom_pw,
            default_playwright_sync=default_pw,
            prefer_patchright=True,
        )
        self.assertIs(fn, custom_pw)
        self.assertEqual(eng, "custom")

        # CDP connection (prefer_patchright=False) uses standard playwright
        fn2, eng2 = playwright_utils.resolve_sync_playwright(
            module_sync_pw=default_pw,
            default_playwright_sync=default_pw,
            prefer_patchright=False,
        )
        self.assertIs(fn2, default_pw)
        self.assertEqual(eng2, "playwright")

        # Self-launched (prefer_patchright=True) uses patchright when installed
        with patch.dict(os.environ, {"ZTE_DISABLE_PATCHRIGHT": "0"}):
            fn3, eng3 = playwright_utils.resolve_sync_playwright(
                module_sync_pw=default_pw,
                default_playwright_sync=default_pw,
                prefer_patchright=True,
            )
            self.assertEqual(eng3, "patchright")
            self.assertIsNot(fn3, default_pw)

        # Graceful fallback when patchright disabled
        with patch.dict(os.environ, {"ZTE_DISABLE_PATCHRIGHT": "1"}):
            fn4, eng4 = playwright_utils.resolve_sync_playwright(
                module_sync_pw=default_pw,
                default_playwright_sync=default_pw,
                prefer_patchright=True,
            )
            self.assertIs(fn4, default_pw)
            self.assertEqual(eng4, "playwright")

    def test_kill_process_tree_and_close_browser_safely_windows_taskkill(self):
        # Must never kill own PID or invalid PID
        self.assertFalse(playwright_utils.kill_process_tree(os.getpid()))
        self.assertFalse(playwright_utils.kill_process_tree(0))
        self.assertFalse(playwright_utils.kill_process_tree(-5))

        broken_browser = MagicMock()
        broken_browser.close.side_effect = RuntimeError("hang on close")
        with patch.object(os, "name", "nt"), \
             patch.object(playwright_utils.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as mock_run:
            playwright_utils.close_browser_safely(browser=broken_browser, proc_pid=54321)
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd, ["taskkill", "/F", "/T", "/PID", "54321"])

    def test_thread_affine_browser_pool_reuses_browser_per_thread_and_cleans_up(self):
        pool = playwright_utils.ThreadAffineBrowserPool()
        launch_calls = []

        class FakeContext:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeBrowser:
            def __init__(self):
                self.connected = True
                self.closed = False
                self.contexts_created = []

            def is_connected(self):
                return self.connected

            def new_context(self, **kwargs):
                ctx = FakeContext()
                self.contexts_created.append(ctx)
                return ctx

            def close(self):
                self.closed = True
                self.connected = False

        class FakePWCM:
            def __init__(self):
                self.exited = False
                self.browser = FakeBrowser()
                self.chromium = SimpleNamespace(launch=self._launch)

            def _launch(self, **kwargs):
                launch_calls.append(kwargs)
                return self.browser

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.exited = True

        factory = MagicMock(side_effect=lambda: FakePWCM())
        with pool.acquire_context(factory, engine_name="patchright", headless=True) as (b1, c1):
            self.assertFalse(c1.closed)
        self.assertTrue(c1.closed)

        # Second call in same thread with same key reuses the existing browser
        with pool.acquire_context(factory, engine_name="patchright", headless=True) as (b2, c2):
            self.assertIs(b1, b2)
            self.assertIsNot(c1, c2)
        self.assertTrue(c2.closed)
        self.assertEqual(len(launch_calls), 1)

        pool.close_all()
        self.assertTrue(b1.closed)

    def test_native_user_agent_preserved_for_patchright_and_channels(self):
        self.assertTrue(playwright_utils.should_use_native_user_agent("patchright"))
        self.assertTrue(playwright_utils.should_use_native_user_agent("playwright", {"channel": "chrome"}))
        self.assertFalse(playwright_utils.should_use_native_user_agent("playwright"))
        self.assertFalse(playwright_utils.should_use_native_user_agent("custom"))

        pr_kwargs = playwright_utils.apply_browser_user_agent({"viewport": {"width": 1280, "height": 800}}, engine_name="patchright")
        self.assertNotIn("user_agent", pr_kwargs)

        ch_kwargs = playwright_utils.apply_browser_user_agent({}, engine_name="playwright", launch_kwargs={"channel": "msedge"})
        self.assertNotIn("user_agent", ch_kwargs)

        pw_kwargs = playwright_utils.apply_browser_user_agent({}, engine_name="playwright")
        self.assertEqual(pw_kwargs.get("user_agent"), playwright_utils.DEFAULT_BROWSER_USER_AGENT)

        explicit_kwargs = playwright_utils.apply_browser_user_agent({"user_agent": "CustomUA/1.0"}, engine_name="patchright")
        self.assertEqual(explicit_kwargs.get("user_agent"), "CustomUA/1.0")

    def test_general_launch_context_or_browser_omits_ua_for_patchright(self):
        import tempfile
        from scripts.online import general_html_extractor as ghe

        seen_persistent = []
        seen_context = []

        class FakeBrowser:
            def new_context(self, **kwargs):
                seen_context.append(dict(kwargs))
                return SimpleNamespace(pages=[])

        class FakeChromium:
            def launch_persistent_context(self, user_data_dir, **kwargs):
                seen_persistent.append(dict(kwargs))
                return SimpleNamespace(pages=[])

            def launch(self, **kwargs):
                return FakeBrowser()

        fake_p = SimpleNamespace(chromium=FakeChromium())

        # Ephemeral Patchright context keeps native UA
        ghe._launch_context_or_browser(fake_p, None, True, None, engine_name="patchright")
        self.assertNotIn("user_agent", seen_context[-1])

        # Ephemeral plain Playwright context receives modern fallback UA
        ghe._launch_context_or_browser(fake_p, None, True, None, engine_name="playwright")
        self.assertEqual(seen_context[-1].get("user_agent"), playwright_utils.DEFAULT_BROWSER_USER_AGENT)

        # Persistent Patchright context keeps native UA
        with tempfile.TemporaryDirectory() as tmpdir:
            ghe._launch_context_or_browser(fake_p, tmpdir, True, None, engine_name="patchright")
            self.assertNotIn("user_agent", seen_persistent[-1])
            ghe._launch_context_or_browser(fake_p, tmpdir, True, None, engine_name="playwright")
            self.assertEqual(seen_persistent[-1].get("user_agent"), playwright_utils.DEFAULT_BROWSER_USER_AGENT)


class TestP1HedgedCascadeAndNegativeCache(unittest.TestCase):
    def test_negative_cache_strict_classification_rejects_404_and_paywall(self):
        # Never cache 404, 410, 401, 402, or paywall/entitlement pages
        self.assertFalse(negative_cache.should_negative_cache(status_code=404)[0])
        self.assertFalse(negative_cache.should_negative_cache(status_code=410)[0])
        self.assertFalse(negative_cache.should_negative_cache(status_code=401)[0])
        self.assertFalse(negative_cache.should_negative_cache(status_code=402)[0])
        self.assertFalse(
            negative_cache.should_negative_cache(
                status_code=403,
                html_snippet="<html><body>Please purchase PDF or subscribe to view this article</body></html>",
            )[0]
        )
        self.assertFalse(
            negative_cache.should_negative_cache(
                status_code=403,
                error_msg="ENTITLEMENT_ERROR: Requestor is not entitled to this content",
            )[0]
        )

        # Strictly cache 429, anti-bot 403, Cloudflare/Turnstile/Radware/Akamai
        self.assertEqual(negative_cache.should_negative_cache(status_code=429), (True, "http_429"))
        self.assertEqual(negative_cache.should_negative_cache(status_code=403, html_snippet="Access Denied")[0], True)
        self.assertEqual(
            negative_cache.should_negative_cache(status_code=200, html_snippet="<title>Just a moment...</title><div class='cf-turnstile'></div>")[0],
            True,
        )

    def test_negative_cache_sqlite_wal_ttl_and_key_normalization(self):
        self.assertEqual(
            negative_cache.extract_target_key(url="https://www.sciencedirect.com/science/article/pii/S123"),
            "domain:sciencedirect.com",
        )
        # Redirector doi.org must fall back to DOI publisher prefix, never domain:doi.org
        self.assertEqual(
            negative_cache.extract_target_key(url="https://doi.org/10.1016/j.test.2026.01"),
            "prefix:10.1016",
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "neg_cache.sqlite")
            # 404 must NOT be written
            self.assertFalse(
                negative_cache.record_negative_cache(
                    "direct_http",
                    url="https://onlinelibrary.wiley.com/doi/10.1002/x",
                    status_code=404,
                    db_path=db_path,
                )
            )
            # 429 MUST be written and readable across fresh connections
            self.assertTrue(
                negative_cache.record_negative_cache(
                    "scrapling",
                    url="https://onlinelibrary.wiley.com/doi/10.1002/x",
                    status_code=429,
                    ttl_s=60,
                    db_path=db_path,
                    now=1000.0,
                )
            )
            # Verify WAL journal mode is active
            conn = sqlite3.connect(db_path)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "wal")
            finally:
                conn.close()

            hit = negative_cache.is_negatively_cached(
                "scrapling",
                url="https://onlinelibrary.wiley.com/doi/10.1002/other",
                db_path=db_path,
                now=1030.0,
            )
            self.assertIsNotNone(hit)
            self.assertEqual(hit["reason"], "http_429")
            # Different channel is NOT blocked
            self.assertIsNone(
                negative_cache.is_negatively_cached(
                    "crawl4ai",
                    url="https://onlinelibrary.wiley.com/doi/10.1002/other",
                    db_path=db_path,
                    now=1030.0,
                )
            )
            # Expired entry is automatically evicted
            self.assertIsNone(
                negative_cache.is_negatively_cached(
                    "scrapling",
                    url="https://onlinelibrary.wiley.com/doi/10.1002/other",
                    db_path=db_path,
                    now=1061.0,
                )
            )

    def test_hedged_cascade_skips_firecrawl_when_local_succeeds_fast(self):
        df = pd.DataFrame({"Col": ["Val"]})
        hedged_called = threading.Event()

        def local_ok():
            time.sleep(0.05)
            return [df]

        def cloud_firecrawl():
            hedged_called.set()
            return [df]

        dfs, winner = graph._hedged_race_candidates(
            [("Crawl4AI", local_ok)],
            [("Firecrawl", cloud_firecrawl)],
            hedge_delay_s=2.0,
        )
        self.assertEqual(len(dfs), 1)
        self.assertEqual(winner, "Crawl4AI")
        self.assertFalse(hedged_called.is_set())

    def test_hedged_cascade_triggers_immediately_on_fast_local_failure(self):
        df = pd.DataFrame({"Col": ["CloudVal"]})
        t0 = time.monotonic()

        def local_fail():
            return []

        def cloud_ok():
            return [df]

        dfs, winner = graph._hedged_race_candidates(
            [("Crawl4AI", local_fail), ("Scrapling", local_fail)],
            [("Firecrawl", cloud_ok)],
            hedge_delay_s=5.0,
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(len(dfs), 1)
        self.assertEqual(winner, "Firecrawl")
        self.assertLess(elapsed, 1.5, "Fast local failure should immediately trigger hedged candidate without waiting 5s")

    def test_hedged_cascade_triggers_after_delay_when_local_stalls(self):
        df = pd.DataFrame({"Col": ["CloudHedge"]})

        def slow_local():
            time.sleep(2.0)
            return []

        def fast_cloud():
            return [df]

        t0 = time.monotonic()
        dfs, winner = graph._hedged_race_candidates(
            [("Crawl4AI", slow_local)],
            [("Firecrawl", fast_cloud)],
            hedge_delay_s=0.2,
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(len(dfs), 1)
        self.assertEqual(winner, "Firecrawl")
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.2)

    def test_negative_cache_cross_process_sqlite_wal_and_tier2_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "cross_proc_neg.sqlite")
            child_code = (
                "import sys; sys.path.insert(0, %r); "
                "from scripts.online import negative_cache as nc; "
                "ok = nc.record_negative_cache('crawl4ai', url='https://www.tandfonline.com/doi/full/10.1080/1', status_code=429, ttl_s=120, db_path=%r); "
                "assert ok"
            ) % (REPO_ROOT, db_path)
            proc = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

            # Parent process reads entry written by child without locking error
            hit = negative_cache.is_negatively_cached(
                "crawl4ai",
                url="https://www.tandfonline.com/doi/full/10.1080/2",
                db_path=db_path,
            )
            self.assertIsNotNone(hit)
            self.assertEqual(hit["target_key"], "domain:tandfonline.com")

            # Tier-2 skips negatively cached crawl4ai and runs scrapling/firecrawl
            fake_df = pd.DataFrame({"X": ["1"], "Y": ["2"]})
            with patch.object(graph, "load_config", return_value={
                "NCBI_PMC_ENABLED": False,
                "ARXIV_HTML_ENABLED": False,
                "NEGATIVE_CACHE_ENABLED": True,
                "NEGATIVE_CACHE_DB_PATH": db_path,
                "TIER2_HEDGE_DELAY_S": 0.1,
            }), \
                 patch.object(graph, "extract_tables_via_direct_http", return_value=[]), \
                 patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
                 patch.object(graph, "extract_tables_via_crawl4ai") as mock_c4a, \
                 patch.object(graph, "extract_tables_via_scrapling", return_value=[]) as mock_scr, \
                 patch.object(graph, "extract_tables_via_firecrawl", return_value=[fake_df]) as mock_fc:
                dfs, history = graph._race_extract({
                    "url": "https://www.tandfonline.com/doi/full/10.1080/999",
                    "doi": "10.1080/999",
                    "is_cnki": False,
                    "table_idx": "all",
                })
            mock_c4a.assert_not_called()
            mock_scr.assert_called_once()
            mock_fc.assert_called_once()
            self.assertEqual(len(dfs), 1)
            self.assertIn("general-tier2:Firecrawl", history)


class TestP1InstitutionalProxyAndSessionValidation(unittest.TestCase):
    def test_ezproxy_rewriting_templates_and_idempotency(self):
        target = "https://link.springer.com/article/10.1007/s00126-024-01333-9?foo=1#Tab1"

        # {url} template
        u1 = proxy_rewriter.rewrite_ezproxy_url(target, "https://ezproxy.univ.edu/login?url={url}")
        self.assertEqual(u1, f"https://ezproxy.univ.edu/login?url={target}")
        # Idempotent
        self.assertEqual(proxy_rewriter.rewrite_ezproxy_url(u1, "https://ezproxy.univ.edu/login?url={url}"), u1)

        # {qurl} / qurl={url} template
        u2 = proxy_rewriter.rewrite_ezproxy_url(target, "https://ezproxy.univ.edu/login?qurl={url}")
        self.assertIn("qurl=https%3A%2F%2Flink.springer.com", u2)

        # {host_dashed} template
        u3 = proxy_rewriter.rewrite_ezproxy_url(target, "https://{host_dashed}.ezproxy.univ.edu{path}")
        self.assertEqual(u3, "https://link-springer-com.ezproxy.univ.edu/article/10.1007/s00126-024-01333-9?foo=1#Tab1")
        # Idempotent
        self.assertEqual(proxy_rewriter.rewrite_ezproxy_url(u3, "https://{host_dashed}.ezproxy.univ.edu{path}"), u3)

        # API/localhost URLs are never rewritten
        self.assertEqual(
            proxy_rewriter.rewrite_ezproxy_url("https://api.elsevier.com/content/article/doi/10.1016/x", "https://ezproxy.univ.edu/login?url={url}"),
            "https://api.elsevier.com/content/article/doi/10.1016/x",
        )

    def test_webvpn_rewriting_lazy_import_graceful_fallback_and_crypto_roundtrip(self):
        target = "https://link.springer.com/article/10.1007/test?a=1"
        # Simulate pycryptodome missing -> graceful None return without crashing
        with patch.dict(sys.modules, {"Crypto": None, "Crypto.Cipher": None}):
            res = proxy_rewriter.rewrite_webvpn_url(target, "https://webvpn.univ.edu.cn")
            self.assertIsNone(res)

        # Simulate AES-128-CFB cipher to verify exact WrdVPN URL format & roundtrip decode
        class FakeAES:
            MODE_CFB = 3

            @classmethod
            def new(cls, key, mode, iv=None, segment_size=128):
                assert len(key) == 16 and len(iv) == 16 and segment_size == 128
                return cls(key, iv)

            def __init__(self, key, iv):
                self._mask = bytes(a ^ b for a, b in zip(key, iv))

            def encrypt(self, data):
                return bytes(b ^ self._mask[i % 16] for i, b in enumerate(data))

            def decrypt(self, data):
                return self.encrypt(data)

        fake_cipher_mod = SimpleNamespace(AES=FakeAES)
        fake_crypto_pkg = SimpleNamespace(Cipher=fake_cipher_mod)
        with patch.dict(sys.modules, {"Crypto": fake_crypto_pkg, "Crypto.Cipher": fake_cipher_mod}):
            rewritten = proxy_rewriter.rewrite_webvpn_url(target, "https://webvpn.univ.edu.cn")
            self.assertTrue(rewritten.startswith("https://webvpn.univ.edu.cn/https/"))
            self.assertTrue(rewritten.endswith("/article/10.1007/test?a=1"))
            decoded = proxy_rewriter.decode_webvpn_url(rewritten)
            self.assertEqual(decoded, target)

    def test_conservative_session_validation(self):
        # Normal article page with table is valid
        ok = proxy_rewriter.validate_institutional_session(
            html_text="<html><body><table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table></body></html>",
            final_url="https://link.springer.com/article/10.1007/test",
            status_code=200,
        )
        self.assertTrue(ok["valid"])
        self.assertFalse(ok["needs_relogin"])

        # Login redirect is flagged
        redir = proxy_rewriter.validate_institutional_session(
            html_text="<html><body>Sign in</body></html>",
            final_url="https://idp.univ.edu/cas/login?service=https://link.springer.com",
            status_code=200,
        )
        self.assertFalse(redir["valid"])
        self.assertTrue(redir["needs_relogin"])

        # Password form without data table is flagged
        pw_form = proxy_rewriter.validate_institutional_session(
            html_text='<html><body><form><input type="password" name="pwd"></form></body></html>',
            final_url="https://ezproxy.univ.edu/auth",
            status_code=200,
        )
        self.assertFalse(pw_form["valid"])
        self.assertTrue(pw_form["needs_relogin"])

        # Expired cookies are flagged
        expired = proxy_rewriter.validate_institutional_session(
            cookies=[{"name": "ezproxy", "value": "x", "expires": 1000}],
            now=2000.0,
        )
        self.assertFalse(expired["valid"])
        self.assertEqual(expired["reason"], "all_cookies_expired")


class TestP2ElsevierResilienceAndDiagnostics(unittest.TestCase):
    def test_elsevier_429_backoff_and_proxy_passthrough(self):
        xml_ok = """<full-text-retrieval-response xmlns:ce="http://www.elsevier.com/xml/common/dtd">
        <originalText><xocs:doc xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">
        <ce:table id="t1"><ce:label>Table 1</ce:label><tgroup cols="2">
        <thead><row><entry>A</entry><entry>B</entry></row></thead>
        <tbody><row><entry>1</entry><entry>2</entry></row></tbody></tgroup></ce:table>
        </xocs:doc></originalText></full-text-retrieval-response>"""

        seen_proxies = []
        calls = {"n": 0}

        def fake_get(url, headers=None, params=None, timeout=None, proxies=None, **kwargs):
            seen_proxies.append(proxies)
            if "/content/article/doi/" in url:
                calls["n"] += 1
                if calls["n"] == 1:
                    return SimpleNamespace(status_code=429, text="Rate limit", headers={"Retry-After": "1"})
                return SimpleNamespace(status_code=200, text=xml_ok, headers={})
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"attachment-metadata-response": {"attachment": []}},
                headers={},
            )

        fake_cfg = {
            "ELSEVIER_API_KEY": "els-key",
            "NETWORK_PROXY": "http://127.0.0.1:8888",
        }
        with patch.object(strategies, "load_config", return_value=fake_cfg), \
             patch.object(strategies.requests, "get", side_effect=fake_get), \
             patch.object(strategies.time, "sleep") as mock_sleep:
            dfs = strategies.extract_tables_via_elsevier_api("10.1016/j.test.2026.01")

        self.assertEqual(len(dfs), 1)
        self.assertEqual(calls["n"], 2)
        mock_sleep.assert_called_once_with(1.0)
        self.assertTrue(all(p == {"http": "http://127.0.0.1:8888", "https": "http://127.0.0.1:8888"} for p in seen_proxies))

    def test_elsevier_403_triggers_view_meta_statustext_diagnostic(self):
        seen_params = []

        def fake_get(url, headers=None, params=None, timeout=None, **kwargs):
            seen_params.append(params)
            if params and params.get("view") == "META":
                return SimpleNamespace(
                    status_code=403,
                    text="<service-error><status><statusCode>ENTITLEMENT_ERROR</statusCode><statusText>Requestor is not entitled to full text</statusText></status></service-error>",
                    headers={},
                )
            return SimpleNamespace(status_code=403, text="", headers={})

        with patch.object(strategies, "load_config", return_value={"ELSEVIER_API_KEY": "els-key"}), \
             patch.object(strategies.requests, "get", side_effect=fake_get):
            dfs = strategies.extract_tables_via_elsevier_api("10.1016/j.test.2026.02")

        self.assertEqual(dfs, [])
        self.assertIn({"view": "META"}, seen_params)


class TestP2ChineseDOIRoutingAndTitleNormalization(unittest.TestCase):
    def test_title_normalization_nfkc_fullwidth_zerowidth_and_similarity(self):
        raw = "　Ｈ２Ｏ\u200b－ＮａＣｌ体系（流体包裹体）<sup>1</sup>　"
        norm = doi_resolver.normalize_title(raw)
        self.assertEqual(norm, "H2O-NaCl体系(流体包裹体)1")

        sim = doi_resolver.compute_title_similarity(
            "Ａｎｔｉｍｏｎｙ\u200b—Ｇｏｌｄ\u200c Deposits in South China",
            "Antimony-Gold Deposits in South China",
        )
        self.assertEqual(sim, 1.0)

    def test_chinese_doi_detection_and_journal_landing_resolution(self):
        self.assertTrue(doi_resolver.is_chinese_doi("10.19658/j.issn.1007-2802.2023.42.025"))
        self.assertTrue(doi_resolver.is_chinese_doi("10.3969/j.issn.1000-0569.2024.01.001"))
        self.assertTrue(doi_resolver.is_chinese_doi("10.12029/gc20240101"))
        self.assertFalse(doi_resolver.is_chinese_doi("10.1016/j.oregeorev.2024.100001"))

        # chndoi page without CNKI link resolves to official Chinese journal Magtech landing page
        html = """
        <html><body>
          <a href="http://www.ysxb.ac.cn/article/doi/10.18654/1000-0569/2024.01.01">期刊官网全文</a>
        </body></html>
        """
        resp = SimpleNamespace(status_code=200, text=html, url="https://www.chndoi.org/Resolution/Handler?doi=10.18654/1000-0569/2024.01.01")
        with patch.object(doi_resolver.requests, "get", return_value=resp):
            resolved = doi_resolver.resolve_chinese_doi("https://www.chndoi.org/Resolution/Handler?doi=10.18654/1000-0569/2024.01.01")
        self.assertEqual(resolved, "http://www.ysxb.ac.cn/article/doi/10.18654/1000-0569/2024.01.01")

        # ResolveMetadataNode routes non-CNKI official journal HTML to DirectHTTPNode (is_cnki=False)
        node = graph.ResolveMetadataNode(fallback_node=None)
        with patch.object(graph, "resolve_chinese_doi", return_value=resolved), \
             patch.object(graph, "load_config", return_value={}):
            st, nxt = node.execute({"pdf_path": "paper.pdf", "doi": "10.18654/1000-0569/2024.01.01"})
        self.assertEqual(st["url"], resolved)
        self.assertFalse(st["is_cnki"])
        self.assertEqual(nxt, "DirectHTTPNode")


class TestP2BuiltinSupplementaryFallback(unittest.TestCase):
    def test_discover_supplementary_links_strong_weak_and_exclusions(self):
        html = """
        <html><body>
          <a href="/content/pdf/10.1007/s00126-024-01.pdf">Download article PDF</a>
          <a href="/images/fig1.png">Figure 1</a>
          <a href="/citation.ris">Download citation</a>
          <a href="/MediaObjects/126_2024_MOESM1_ESM.xlsx">Electronic Supplementary Material Table S1</a>
          <p>Appendix data: <a href="/files/appendix_data.pdf">Supplementary Table S2 PDF</a></p>
          <a href="/static/table_s3.csv">Source Data CSV</a>
        </body></html>
        """
        links = strategies._discover_supplementary_links("https://link.springer.com/article/10.1007/s00126-024-01", html)
        urls = [item["url"] for item in links]
        self.assertEqual(urls[0], "https://link.springer.com/MediaObjects/126_2024_MOESM1_ESM.xlsx")
        self.assertIn("https://link.springer.com/static/table_s3.csv", urls)
        self.assertIn("https://link.springer.com/files/appendix_data.pdf", urls)
        # Main PDF, PNG, and RIS must be excluded
        self.assertFalse(any("s00126-024-01.pdf" in u or u.endswith(".png") or u.endswith(".ris") for u in urls))

    def test_builtin_supplementary_fallback_downloads_parses_and_rejects_html_soft_block(self):
        # Build a real in-memory ZIP containing a CSV supplementary table
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("Table_S1.csv", "Sample,FeO,MgO\nS1,12.3,8.4\nS2,11.1,9.0\n")
        zip_bytes = zip_buf.getvalue()

        article_html = """
        <html><body>
          <a href="/suppl/Table_S1_pack.zip">Supplementary Data Archive</a>
          <a href="/suppl/Table_S2.csv">Supplementary Table S2</a>
        </body></html>
        """
        seen_cookies = []

        def fake_get(url, headers=None, cookies=None, timeout=None, allow_redirects=True, **kwargs):
            seen_cookies.append(dict(cookies or {}))
            if url.endswith("Table_S1_pack.zip"):
                return SimpleNamespace(
                    status_code=200,
                    headers={"Content-Type": "application/zip"},
                    content=zip_bytes,
                )
            if url.endswith("Table_S2.csv"):
                # Soft-blocked HTML login page disguised as 200 -> MUST be rejected
                return SimpleNamespace(
                    status_code=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    content=b"<!DOCTYPE html><html><body>Please sign in</body></html>",
                )
            return SimpleNamespace(status_code=200, text=article_html, headers={"Content-Type": "text/html"})

        fake_cookies = [{"name": "SID", "value": "inst_cookie_val", "domain": "example.org", "path": "/"}]
        with patch.object(strategies, "load_config", return_value={}), \
             patch.object(strategies, "_collect_domain_cookies_for_scrapling", return_value=fake_cookies), \
             patch.object(os.path, "exists", return_value=False), \
             patch.object(strategies.requests, "get", side_effect=fake_get):
            dfs = strategies.extract_general_supplementary_tables("https://example.org/article/123")

        self.assertEqual(len(dfs), 1)
        self.assertEqual(list(dfs[0].columns), ["Sample", "FeO", "MgO"])
        self.assertEqual(dfs[0].attrs.get("extractor"), "builtin_supplementary")
        self.assertTrue(any(c.get("SID") == "inst_cookie_val" for c in seen_cookies))

    def test_parse_supplementary_bytes_xlsx_docx_and_extensionless_ooxml(self):
        # 1. Real .xlsx binary in memory
        xlsx_buf = io.BytesIO()
        pd.DataFrame({"Sample": ["A1", "A2"], "Au_ppb": [12.5, 19.3]}).to_excel(xlsx_buf, index=False)
        xlsx_bytes = xlsx_buf.getvalue()
        dfs_xlsx = strategies._parse_supplementary_bytes(xlsx_bytes, "Table_S1.xlsx")
        self.assertEqual(len(dfs_xlsx), 1)
        self.assertEqual(list(dfs_xlsx[0].columns), ["Sample", "Au_ppb"])
        self.assertEqual(dfs_xlsx[0].attrs.get("extractor"), "builtin_supplementary")

        # 2. Extensionless OOXML .xlsx detected via ZIP magic + xl/workbook.xml
        dfs_extless_xlsx = strategies._parse_supplementary_bytes(xlsx_bytes, "download_blob")
        self.assertEqual(len(dfs_extless_xlsx), 1)
        self.assertEqual(list(dfs_extless_xlsx[0].columns), ["Sample", "Au_ppb"])

        # 3. Real .docx binary in memory (minimal valid word/document.xml)
        docx_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:tbl>
              <w:tr><w:tc><w:p><w:r><w:t>Mineral</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>FeO</w:t></w:r></w:p></w:tc></w:tr>
              <w:tr><w:tc><w:p><w:r><w:t>Pyrite</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>46.5</w:t></w:r></w:p></w:tc></w:tr>
            </w:tbl>
          </w:body>
        </w:document>"""
        docx_buf = io.BytesIO()
        with zipfile.ZipFile(docx_buf, "w") as zf:
            zf.writestr("word/document.xml", docx_xml)
        docx_bytes = docx_buf.getvalue()
        dfs_docx = strategies._parse_supplementary_bytes(docx_bytes, "Table_S2.docx")
        self.assertEqual(len(dfs_docx), 1)
        self.assertEqual(list(dfs_docx[0].columns), ["Mineral", "FeO"])
        self.assertEqual(dfs_docx[0].iloc[0]["Mineral"], "Pyrite")

        # 4. Extensionless OOXML .docx detected via ZIP magic + word/document.xml
        dfs_extless_docx = strategies._parse_supplementary_bytes(docx_bytes, "attachment_raw")
        self.assertEqual(len(dfs_extless_docx), 1)
        self.assertEqual(list(dfs_extless_docx[0].columns), ["Mineral", "FeO"])


class TestReviewHardeningRegressions(unittest.TestCase):
    def test_load_cookies_from_file_normalizes_samesite_url_only_and_bad_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "mixed_cookies.json")
            payload = [
                {
                    "name": "chrome_ext_cookie",
                    "value": "v1",
                    "domain": ".springer.com",
                    "path": "/",
                    "sameSite": "no_restriction",
                    "expirationDate": "session",  # invalid float -> must NOT drop file
                },
                {
                    "name": "url_only_cookie",
                    "value": "v2",
                    "url": "https://link.springer.com/article/10.1007/x",
                    "sameSite": "lax",
                },
                {
                    "name": "unspecified_ss",
                    "value": "v3",
                    "domain": ".nature.com",
                    "sameSite": "unspecified",
                },
                {
                    "name": "orphan_no_domain_or_url",
                    "value": "v4",
                },
            ]
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)

            loaded = common.load_cookies_from_file(p)
            by_name = {c["name"]: c for c in loaded}
            self.assertEqual(set(by_name.keys()), {"chrome_ext_cookie", "url_only_cookie", "unspecified_ss"})
            self.assertEqual(by_name["chrome_ext_cookie"]["sameSite"], "None")
            self.assertNotIn("expires", by_name["chrome_ext_cookie"])
            self.assertEqual(by_name["url_only_cookie"]["sameSite"], "Lax")
            self.assertIn("url", by_name["url_only_cookie"])
            self.assertNotIn("domain", by_name["url_only_cookie"])
            self.assertNotIn("path", by_name["url_only_cookie"])
            self.assertNotIn("sameSite", by_name["unspecified_ss"])

    def test_negative_cache_ignores_200_cdn_scripts_and_institutional_login_walls_but_caches_429(self):
        # Normal 200 article loading MathJax from cdnjs.cloudflare.com MUST NOT be cached
        normal_cdn_html = (
            '<html><head><script src="https://cdnjs.cloudflare.com/ajax/libs/mathjax/3.2.0/es5/tex-mml-chtml.js"></script>'
            '<script src="https://static.akamaihd.net/app.js"></script></head>'
            '<body><article><p>A study on CAPTCHA usability</p></article></body></html>'
        )
        self.assertFalse(negative_cache.should_negative_cache(status_code=200, html_snippet=normal_cdn_html)[0])

        # HTTP 429 MUST always be cached even if footer mentions "subscribe to"
        self.assertEqual(
            negative_cache.should_negative_cache(
                status_code=429,
                html_snippet="<html><body>Too many requests. Subscribe to our newsletter.</body></html>",
            ),
            (True, "http_429"),
        )

        # HTTP 403 EZProxy/CAS login wall MUST NOT poison anti-bot negative cache
        self.assertFalse(
            negative_cache.should_negative_cache(
                status_code=403,
                html_snippet="<html><body>Please log in to EZProxy / 统一身份认证</body></html>",
            )[0]
        )

    def test_session_validation_avoids_false_positives_on_large_articles_and_query_params(self):
        # Large publisher HTML (>30KB) with hidden header login modal and table past char 20,000
        large_article = (
            '<html><head><meta name="citation_doi" content="10.1007/s00126-024-01333-9"></head>'
            '<body><header><form class="login-modal"><input type="password" name="pw"></form></header>'
            + ("<p>Geochemical prose padding text.</p>" * 900)
            + "<article><table><thead><tr><th>Sample</th><th>Au</th></tr></thead><tbody><tr><td>S1</td><td>12</td></tr></tbody></table></article>"
            + "</body></html>"
        )
        res = proxy_rewriter.validate_institutional_session(
            html_text=large_article,
            final_url="https://link.springer.com/article/10.1007/s00126-024-01333-9?return=/login",
            status_code=200,
        )
        self.assertTrue(res["valid"])
        self.assertFalse(res["needs_relogin"])

        # Schemeless WebVPN base_url gets https:// normalized automatically
        class FakeAES:
            MODE_CFB = 3
            @classmethod
            def new(cls, key, mode, iv=None, segment_size=128):
                return cls()
            def encrypt(self, data):
                return data
        fake_cipher = SimpleNamespace(AES=FakeAES)
        with patch.dict(sys.modules, {"Crypto": SimpleNamespace(Cipher=fake_cipher), "Crypto.Cipher": fake_cipher}):
            out = proxy_rewriter.rewrite_webvpn_url("https://link.springer.com/article/1", "webvpn.univ.edu.cn")
            self.assertTrue(out.startswith("https://webvpn.univ.edu.cn/https/"))

    def test_direct_http_expired_ezproxy_session_does_not_poison_negative_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "neg_ez.sqlite")
            cfg = {
                "EZPROXY_ENABLED": True,
                "EZPROXY_LOGIN_URL": "https://ezproxy.univ.edu/login?url={url}",
                "NEGATIVE_CACHE_ENABLED": True,
                "NEGATIVE_CACHE_DB_PATH": db_path,
            }
            login_resp = SimpleNamespace(
                status_code=200,
                text='<html><body><h1>EZProxy Login</h1><form><input type="password"></form></body></html>',
                url="https://ezproxy.univ.edu/login?url=https://link.springer.com/article/10.1007/test",
            )
            with patch.object(strategies, "load_config", return_value=cfg), \
                 patch.object(strategies, "requests_impersonate", None), \
                 patch.object(strategies.requests, "get", return_value=login_resp):
                dfs = strategies.extract_tables_via_direct_http("https://link.springer.com/article/10.1007/test")
            self.assertEqual(dfs, [])
            self.assertIsNone(
                negative_cache.is_negatively_cached(
                    "direct_http",
                    url="https://link.springer.com/article/10.1007/test",
                    db_path=db_path,
                )
            )

    def test_springer_tdm_redacts_api_key_handles_doi_url_and_namespaced_jats(self):
        self.assertTrue(strategies.is_springer_nature_target(doi="https://doi.org/10.1007/s00126-024-01333-9"))

        # API key must be redacted from exception output
        secret = "TOP_SECRET_SPRINGER_KEY_999"
        buf = io.StringIO()
        with patch.object(strategies, "load_config", return_value={"SPRINGER_API_KEY": secret, "SPRINGER_TDM_ENABLED": True}), \
             patch.object(strategies.requests, "get", side_effect=RuntimeError(f"boom https://api.springernature.com/xmldata/jats?api_key={secret}")), \
             patch("sys.stdout", buf):
            dfs = strategies.extract_tables_via_springer_tdm("10.1007/s00126-024-01333-9")
        self.assertEqual(dfs, [])
        self.assertNotIn(secret, buf.getvalue())
        self.assertIn("***", buf.getvalue())

        # Namespaced <jats:table-wrap> must be parsed
        ns_jats = """<?xml version="1.0" encoding="UTF-8"?>
        <response xmlns:jats="http://jats.nlm.nih.gov">
          <jats:table-wrap>
            <jats:label>Table 1</jats:label>
            <jats:table>
              <jats:thead><jats:tr><jats:th>ID</jats:th><jats:th>Val</jats:th></jats:tr></jats:thead>
              <jats:tbody><jats:tr><jats:td>1</jats:td><jats:td>2.5</jats:td></jats:tr></jats:tbody>
            </jats:table>
          </jats:table-wrap>
        </response>"""
        with patch.object(strategies, "load_config", return_value={"SPRINGER_API_KEY": "k", "SPRINGER_TDM_ENABLED": True}), \
             patch.object(strategies.requests, "get", return_value=SimpleNamespace(status_code=200, text=ns_jats, headers={})):
            dfs_ns = strategies.extract_tables_via_springer_tdm("10.1007/s00126-024-01333-9")
        self.assertEqual(len(dfs_ns), 1)
        self.assertEqual(dfs_ns[0].attrs.get("source"), "springer_tdm_jats")

    def test_chndoi_ignores_istic_and_root_homepages_and_picks_real_article_url(self):
        html = """
        <html><body>
          <a href="http://www.istic.ac.cn/">ISTIC</a>
          <a href="http://www.ysxb.ac.cn/">Journal Home</a>
          <a href="http://www.ysxb.ac.cn/index.html">Index</a>
          <a href="http://www.ysxb.ac.cn/article/doi/10.18654/1000-0569/2024.01.01">Full Article</a>
        </body></html>
        """
        resp = SimpleNamespace(status_code=200, text=html, url="https://www.chndoi.org/Resolution/Handler?doi=10.18654/1000-0569/2024.01.01")
        with patch.object(doi_resolver.requests, "get", return_value=resp):
            resolved = doi_resolver.resolve_chinese_doi("https://www.chndoi.org/Resolution/Handler?doi=10.18654/1000-0569/2024.01.01")
        self.assertEqual(resolved, "http://www.ysxb.ac.cn/article/doi/10.18654/1000-0569/2024.01.01")

    def test_thread_browser_pool_distinguishes_launch_kwargs(self):
        pool = playwright_utils.ThreadAffineBrowserPool()
        k1 = pool._entry_key("patchright", True, None, launch_kwargs={"args": ["--a"]})
        k2 = pool._entry_key("patchright", True, None, launch_kwargs={"args": ["--b"]})
        k3 = pool._entry_key("patchright", True, None, launch_kwargs={"args": ["--a"]})
        self.assertEqual(k1, k3)
        self.assertNotEqual(k1, k2)

    def test_direct_then_impersonated_skips_impersonated_when_direct_http_negatively_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "neg_direct_imp.sqlite")
            cfg = {
                "NCBI_PMC_ENABLED": False,
                "ARXIV_HTML_ENABLED": False,
                "NEGATIVE_CACHE_ENABLED": True,
                "NEGATIVE_CACHE_DB_PATH": db_path,
                "TIER2_HEDGE_DELAY_S": 0.05,
            }
            url = "https://onlinelibrary.wiley.com/doi/full/10.1002/2024gc000001"
            fake_impersonate = MagicMock()
            fake_df = pd.DataFrame({"Sample": ["S1"], "Val": ["1.2"]})

            # 1. DirectHTTP hits 429 -> writes "direct_http" negative cache -> _impersonated MUST NOT run
            resp_429 = SimpleNamespace(status_code=429, text="Too Many Requests", url=url)
            with patch.object(strategies, "load_config", return_value=cfg), \
                 patch.object(graph, "load_config", return_value=cfg), \
                 patch.object(strategies, "requests_impersonate", None), \
                 patch.object(strategies.requests, "get", return_value=resp_429) as mock_direct_get, \
                 patch.object(graph, "requests_impersonate", fake_impersonate), \
                 patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
                 patch.object(graph, "extract_tables_via_crawl4ai", return_value=[fake_df]), \
                 patch.object(graph, "extract_tables_via_scrapling", return_value=[]):
                dfs, history = graph._race_extract({
                    "url": url,
                    "doi": "10.1002/2024gc000001",
                    "is_cnki": False,
                    "table_idx": "all",
                })

            mock_direct_get.assert_called_once()
            fake_impersonate.get.assert_not_called()
            self.assertEqual(len(dfs), 1)
            self.assertIn("general-tier2:Crawl4AI", history)
            hit = negative_cache.is_negatively_cached("direct_http", url=url, db_path=db_path)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["reason"], "http_429")

            # 2. Second request on same host hits negative cache in both DirectHTTP and _impersonated (zero HTTP hits)
            mock_direct_get.reset_mock()
            fake_impersonate.reset_mock()
            with patch.object(strategies, "load_config", return_value=cfg), \
                 patch.object(graph, "load_config", return_value=cfg), \
                 patch.object(strategies, "requests_impersonate", None), \
                 patch.object(strategies.requests, "get", return_value=resp_429) as mock_direct_get2, \
                 patch.object(graph, "requests_impersonate", fake_impersonate), \
                 patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
                 patch.object(graph, "extract_tables_via_crawl4ai", return_value=[fake_df]), \
                 patch.object(graph, "extract_tables_via_scrapling", return_value=[]):
                dfs2, _ = graph._race_extract({
                    "url": "https://onlinelibrary.wiley.com/doi/full/10.1002/2024gc000002",
                    "doi": "10.1002/2024gc000002",
                    "is_cnki": False,
                    "table_idx": "all",
                })
            mock_direct_get2.assert_not_called()
            fake_impersonate.get.assert_not_called()
            self.assertEqual(len(dfs2), 1)

    def test_impersonated_http_node_applies_ezproxy_proxies_session_validation_antibot_and_neg_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "neg_imp_node.sqlite")
            target_url = "https://link.springer.com/article/10.1007/s00126-024-01333-9"
            ez_cfg = {
                "EZPROXY_ENABLED": True,
                "EZPROXY_LOGIN_URL": "https://ezproxy.univ.edu/login?url={url}",
                "NETWORK_PROXY": "http://127.0.0.1:7890",
                "NEGATIVE_CACHE_ENABLED": True,
                "NEGATIVE_CACHE_DB_PATH": db_path,
            }
            valid_html = (
                "<html><body><table><thead><tr><th>Sample</th><th>Au</th></tr></thead>"
                "<tbody><tr><td>S1</td><td>12.4</td></tr><tr><td>S2</td><td>8.9</td></tr></tbody></table></body></html>"
            )

            # 1. Applies EZProxy rewrite + NETWORK_PROXY and extracts tables
            seen_calls = []

            def fake_get_ok(url, **kwargs):
                seen_calls.append((url, kwargs))
                return SimpleNamespace(
                    status_code=200,
                    text=valid_html,
                    url="https://link-springer-com.ezproxy.univ.edu/article/10.1007/s00126-024-01333-9",
                )

            fake_imp = SimpleNamespace(get=fake_get_ok)
            node = graph.ImpersonatedHTTPNode(fallback_node="Crawl4AIGeneralNode")
            with patch.object(graph, "load_config", return_value=ez_cfg), \
                 patch.object(graph, "requests_impersonate", fake_imp):
                out_state, nxt = node.execute({"url": target_url, "doi": "10.1007/s00126-024-01333-9", "table_idx": "all"})

            self.assertIsNone(nxt)
            self.assertEqual(len(out_state["dfs"]), 1)
            self.assertEqual(seen_calls[0][0], f"https://ezproxy.univ.edu/login?url={target_url}")
            self.assertEqual(
                seen_calls[0][1].get("proxies"),
                {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
            )

            # 2. Expired EZProxy login wall does NOT poison negative cache
            login_resp = SimpleNamespace(
                status_code=200,
                text='<html><body><h1>EZProxy Login</h1><form><input type="password" name="pw"></form></body></html>',
                url=f"https://ezproxy.univ.edu/login?url={target_url}",
            )
            with patch.object(graph, "load_config", return_value=ez_cfg), \
                 patch.object(graph, "requests_impersonate", SimpleNamespace(get=lambda u, **kw: login_resp)):
                out_state2, nxt2 = node.execute({"url": target_url, "table_idx": "all"})
            self.assertEqual(nxt2, "Crawl4AIGeneralNode")
            self.assertIsNone(out_state2.get("dfs"))
            self.assertIsNone(negative_cache.is_negatively_cached("direct_http", url=target_url, db_path=db_path))

            # 3. HTTP 403 paywall does NOT poison negative cache
            plain_cfg = {"NEGATIVE_CACHE_ENABLED": True, "NEGATIVE_CACHE_DB_PATH": db_path}
            paywall_resp = SimpleNamespace(
                status_code=403,
                text="<html><body>Please purchase PDF or subscribe to view this article</body></html>",
                url=target_url,
            )
            with patch.object(graph, "load_config", return_value=plain_cfg), \
                 patch.object(graph, "requests_impersonate", SimpleNamespace(get=lambda u, **kw: paywall_resp)):
                _, nxt_pw = node.execute({"url": target_url, "table_idx": "all"})
            self.assertEqual(nxt_pw, "Crawl4AIGeneralNode")
            self.assertIsNone(negative_cache.is_negatively_cached("direct_http", url=target_url, db_path=db_path))

            # 4. HTTP 200 Cloudflare anti-bot challenge is blocked AND recorded in "direct_http" negative cache
            antibot_html = (
                "<html><head><title>Just a moment...</title></head>"
                "<body><div class='cf-turnstile'>Checking your browser</div>"
                "<table><tr><th>Fake</th><th>Col</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
            )
            antibot_resp = SimpleNamespace(status_code=200, text=antibot_html, url=target_url)
            with patch.object(graph, "load_config", return_value=plain_cfg), \
                 patch.object(graph, "requests_impersonate", SimpleNamespace(get=lambda u, **kw: antibot_resp)):
                out_state3, nxt3 = node.execute({"url": target_url, "table_idx": "all"})
            self.assertEqual(nxt3, "Crawl4AIGeneralNode")
            self.assertIsNone(out_state3.get("dfs"))
            hit = negative_cache.is_negatively_cached("direct_http", url=target_url, db_path=db_path)
            self.assertIsNotNone(hit)

            # 5. Subsequent ImpersonatedHTTPNode call skips immediately due to negative cache
            mock_imp_skip = MagicMock()
            with patch.object(graph, "load_config", return_value=plain_cfg), \
                 patch.object(graph, "requests_impersonate", mock_imp_skip):
                _, nxt4 = node.execute({"url": target_url, "table_idx": "all"})
            self.assertEqual(nxt4, "Crawl4AIGeneralNode")
            mock_imp_skip.get.assert_not_called()

    def test_race_impersonated_checks_antibot_rewrites_proxy_and_records_neg_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "neg_race_imp.sqlite")
            target_url = "https://pubs.acs.org/doi/10.1021/acs.est.4c01234"
            cfg = {
                "NCBI_PMC_ENABLED": False,
                "ARXIV_HTML_ENABLED": False,
                "EZPROXY_ENABLED": True,
                "EZPROXY_LOGIN_URL": "https://ezproxy.univ.edu/login?url={url}",
                "NETWORK_PROXY": "http://127.0.0.1:9999",
                "NEGATIVE_CACHE_ENABLED": True,
                "NEGATIVE_CACHE_DB_PATH": db_path,
                "TIER2_HEDGE_DELAY_S": 0.05,
            }
            seen = []
            antibot_html = (
                "<html><head><title>Just a moment...</title></head>"
                "<body><div class='cf-turnstile'>Verify you are human</div>"
                "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
            )

            def fake_imp_get(u, **kw):
                seen.append((u, kw))
                return SimpleNamespace(
                    status_code=200,
                    text=antibot_html,
                    url="https://pubs-acs-org.ezproxy.univ.edu/doi/10.1021/acs.est.4c01234",
                )

            fallback_df = pd.DataFrame({"Sample": ["X1"], "Val": ["42"]})
            with patch.object(graph, "load_config", return_value=cfg), \
                 patch.object(graph, "extract_tables_via_direct_http", return_value=[]), \
                 patch.object(graph, "requests_impersonate", SimpleNamespace(get=fake_imp_get)), \
                 patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
                 patch.object(graph, "extract_tables_via_crawl4ai", return_value=[fallback_df]), \
                 patch.object(graph, "extract_tables_via_scrapling", return_value=[]):
                dfs, history = graph._race_extract({
                    "url": target_url,
                    "doi": "10.1021/acs.est.4c01234",
                    "is_cnki": False,
                    "table_idx": "all",
                })

            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][0], f"https://ezproxy.univ.edu/login?url={target_url}")
            self.assertEqual(seen[0][1].get("proxies"), {"http": "http://127.0.0.1:9999", "https": "http://127.0.0.1:9999"})
            # Must NOT accept the fake table inside the 200 Cloudflare challenge page
            self.assertIn("general-tier1:none", history)
            self.assertIn("general-tier2:Crawl4AI", history)
            self.assertEqual(len(dfs), 1)
            # And MUST record "direct_http" negative cache keyed on original publisher host (not ezproxy host)
            hit = negative_cache.is_negatively_cached("direct_http", url=target_url, db_path=db_path)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["target_key"], "domain:pubs.acs.org")

    def test_resolve_metadata_extracts_doi_from_direct_publisher_urls_and_triggers_tier1_channels(self):
        node = graph.ResolveMetadataNode(fallback_node=None)
        with patch.object(graph, "load_config", return_value={}):
            # 1. Direct Springer URL populates state["doi"]
            st1, nxt1 = node.execute({"pdf_path": "https://link.springer.com/article/10.1007/s00126-024-01333-9"})
            self.assertEqual(st1["doi"], "10.1007/s00126-024-01333-9")
            self.assertFalse(st1["is_cnki"])
            self.assertEqual(nxt1, "DirectHTTPNode")

            # 2. Direct Springer table subpage URL with query + fragment strips /tables/1
            st2, _ = node.execute({"pdf_path": "https://link.springer.com/article/10.1007/s00126-024-01333-9/tables/1?foo=bar#Tab1"})
            self.assertEqual(st2["doi"], "10.1007/s00126-024-01333-9")

            # 3. Direct Wiley /doi/abs/ URL normalizes to /doi/full/ AND extracts DOI
            st3, _ = node.execute({"pdf_path": "https://onlinelibrary.wiley.com/doi/abs/10.1002/2016GC006684"})
            self.assertEqual(st3["url"], "https://onlinelibrary.wiley.com/doi/full/10.1002/2016GC006684")
            self.assertEqual(st3["doi"], "10.1002/2016GC006684")

            # 4. Non-DOI ScienceDirect PII and arXiv numeric URL (2010.12345/v1) must NOT false-positive
            st4, _ = node.execute({"pdf_path": "https://www.sciencedirect.com/science/article/pii/S0169136824001234"})
            self.assertIsNone(st4["doi"])
            st5, _ = node.execute({"pdf_path": "https://arxiv.org/abs/2010.12345/v1"})
            self.assertIsNone(st5["doi"])

            # 5. Direct Chinese journal URL extracts DOI without hijacking URL via chndoi
            cn_journal_url = "http://www.ysxb.ac.cn/article/doi/10.18654/1000-0569/2024.01.01"
            with patch.object(graph, "resolve_chinese_doi") as mock_cn_res:
                st6, nxt6 = node.execute({"pdf_path": cn_journal_url})
            mock_cn_res.assert_not_called()
            self.assertEqual(st6["doi"], "10.18654/1000-0569/2024.01.01")
            self.assertEqual(st6["url"], cn_journal_url)
            self.assertFalse(st6["is_cnki"])
            self.assertEqual(nxt6, "DirectHTTPNode")

            # 6. URL-encoded doi.org and hdl.handle.net Chinese DOI redirectors resolve via chndoi
            cnki_resolved = "https://link.cnki.net/doi/10.19658/j.issn.1007-2802.2023.42.025"
            with patch.object(graph, "resolve_chinese_doi", return_value=cnki_resolved) as mock_cn_res2:
                st7, nxt7 = node.execute({"pdf_path": "https://doi.org/10.19658%2Fj.issn.1007-2802.2023.42.025"})
            mock_cn_res2.assert_called_once()
            self.assertEqual(st7["doi"], "10.19658/j.issn.1007-2802.2023.42.025")
            self.assertEqual(st7["url"], cnki_resolved)
            self.assertTrue(st7["is_cnki"])
            self.assertEqual(nxt7, "Crawl4AICNKINode")

            with patch.object(graph, "resolve_chinese_doi", return_value=cnki_resolved) as mock_cn_res3:
                st8, nxt8 = node.execute({"pdf_path": "https://hdl.handle.net/10.19658/j.issn.1007-2802.2023.42.025"})
            mock_cn_res3.assert_called_once()
            self.assertEqual(st8["doi"], "10.19658/j.issn.1007-2802.2023.42.025")
            self.assertTrue(st8["is_cnki"])
            self.assertEqual(nxt8, "Crawl4AICNKINode")

        # 7. End-to-end extract_tables_online with direct Springer URL triggers SpringerTDM in Tier-1
        springer_df = pd.DataFrame({"Sample": ["S1", "S2"], "Au": ["11.2", "9.5"]})
        with patch.object(graph, "load_config", return_value={"NCBI_PMC_ENABLED": False, "ARXIV_HTML_ENABLED": False}), \
             patch.object(graph, "extract_tables_via_direct_http", return_value=[]), \
             patch.object(graph, "requests_impersonate", None), \
             patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
             patch.object(graph, "extract_tables_via_springer_tdm", return_value=[springer_df]) as mock_tdm:
            dfs, _title = graph.extract_tables_online(
                "https://link.springer.com/article/10.1007/s00126-024-01333-9",
                race=True,
            )
        mock_tdm.assert_called_once()
        self.assertEqual(mock_tdm.call_args[0][0], "10.1007/s00126-024-01333-9")
        self.assertEqual(len(dfs), 1)

    def test_redirector_url_with_doi_shares_direct_http_negative_cache_in_race_and_serial_dag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "neg_redir.sqlite")
            cfg = {
                "NCBI_PMC_ENABLED": False,
                "ARXIV_HTML_ENABLED": False,
                "NEGATIVE_CACHE_ENABLED": True,
                "NEGATIVE_CACHE_DB_PATH": db_path,
                "TIER2_HEDGE_DELAY_S": 0.05,
            }
            url = "https://linkinghub.elsevier.com/retrieve/pii/S0169136824001234"
            doi = "10.1016/j.oregeorev.2024.105999"
            fake_impersonate = MagicMock()
            fake_df = pd.DataFrame({"Sample": ["S1"], "Val": ["3.4"]})
            resp_429 = SimpleNamespace(
                status_code=429,
                text="Too Many Requests",
                url="https://www.sciencedirect.com/science/article/pii/S0169136824001234",
            )

            # 1. Race mode: DirectHTTP on redirector URL + DOI records prefix:10.1016 and skips _impersonated
            with patch.object(strategies, "load_config", return_value=cfg), \
                 patch.object(graph, "load_config", return_value=cfg), \
                 patch.object(strategies, "requests_impersonate", None), \
                 patch.object(strategies.requests, "get", return_value=resp_429) as mock_direct_get, \
                 patch.object(graph, "requests_impersonate", fake_impersonate), \
                 patch.object(graph, "extract_tables_via_elsevier_api", return_value=[]), \
                 patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
                 patch.object(graph, "extract_tables_via_crawl4ai", return_value=[fake_df]), \
                 patch.object(graph, "extract_tables_via_scrapling", return_value=[]):
                dfs, _history = graph._race_extract({
                    "url": url,
                    "doi": doi,
                    "is_cnki": False,
                    "table_idx": "all",
                })

            mock_direct_get.assert_called_once()
            fake_impersonate.get.assert_not_called()
            self.assertEqual(len(dfs), 1)
            hit = negative_cache.is_negatively_cached("direct_http", url=url, doi=doi, db_path=db_path)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["target_key"], "prefix:10.1016")

            # 2. Serial DAG (race=False): DirectHTTPNode -> ImpersonatedHTTPNode skips ImpersonatedHTTPNode on 429
            db_path_serial = os.path.join(tmp, "neg_serial.sqlite")
            cfg_serial = dict(cfg, NEGATIVE_CACHE_DB_PATH=db_path_serial)
            wiley_url = "https://onlinelibrary.wiley.com/doi/full/10.1002/2024gc000999"
            fake_impersonate_serial = MagicMock()
            with patch.object(strategies, "load_config", return_value=cfg_serial), \
                 patch.object(graph, "load_config", return_value=cfg_serial), \
                 patch.object(strategies, "requests_impersonate", None), \
                 patch.object(strategies.requests, "get", return_value=SimpleNamespace(status_code=429, text="Rate limited", url=wiley_url)) as mock_serial_get, \
                 patch.object(graph, "requests_impersonate", fake_impersonate_serial), \
                 patch.object(graph, "extract_tables_via_crawl4ai", return_value=[fake_df]):
                dfs_serial, _ = graph.extract_tables_online(wiley_url, race=False)

            mock_serial_get.assert_called_once()
            fake_impersonate_serial.get.assert_not_called()
            self.assertEqual(len(dfs_serial), 1)

    def test_antibot_turnstile_and_deep_offset_200_html_blocked_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "neg_antibot_align.sqlite")
            cfg = {"NEGATIVE_CACHE_ENABLED": True, "NEGATIVE_CACHE_DB_PATH": db_path}

            # 1. Turnstile HTTP 200 HTML without "Just a moment" is blocked AND negative-cached
            turnstile_url = "https://link.springer.com/article/10.1007/s00126-024-09999-1"
            turnstile_html = (
                "<html><body><div class='cf-turnstile'>Verify you are human</div>"
                "<table><tr><th>Fake</th><th>Col</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
            )
            with patch.object(graph, "load_config", return_value=cfg), \
                 patch.object(graph, "requests_impersonate", SimpleNamespace(
                     get=lambda u, **kw: SimpleNamespace(status_code=200, text=turnstile_html, url=turnstile_url)
                 )):
                dfs = graph._fetch_tables_via_impersonated_http(turnstile_url, config=cfg)
            self.assertEqual(dfs, [])
            hit1 = negative_cache.is_negatively_cached("direct_http", url=turnstile_url, db_path=db_path)
            self.assertIsNotNone(hit1)

            # 2. Deep-offset (>6000 bytes, <12000 bytes) anti-bot challenge is blocked AND negative-cached
            deep_url = "https://pubs.acs.org/doi/10.1021/acs.est.4c99999"
            deep_html = (
                "<html><head><style>" + ("a" * 7000) + "</style></head>"
                "<body><div class='cf-browser-verification'>Just a moment...</div>"
                "<table><tr><th>Fake</th><th>Col</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
            )
            with patch.object(graph, "load_config", return_value=cfg), \
                 patch.object(graph, "requests_impersonate", SimpleNamespace(
                     get=lambda u, **kw: SimpleNamespace(status_code=200, text=deep_html, url=deep_url)
                 )):
                dfs2 = graph._fetch_tables_via_impersonated_http(deep_url, config=cfg)
            self.assertEqual(dfs2, [])
            hit2 = negative_cache.is_negatively_cached("direct_http", url=deep_url, db_path=db_path)
            self.assertIsNotNone(hit2)

    def test_pre_rewritten_institutional_proxy_url_still_validates_session_without_poisoning_neg_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "neg_pre_ez.sqlite")
            cfg = {
                "EZPROXY_ENABLED": True,
                "EZPROXY_LOGIN_URL": "https://ezproxy.univ.edu/login?url={url}",
                "NEGATIVE_CACHE_ENABLED": True,
                "NEGATIVE_CACHE_DB_PATH": db_path,
            }
            ez_url = "https://link-springer-com.ezproxy.univ.edu/article/10.1007/s00126-024-01333-9"
            login_403 = SimpleNamespace(
                status_code=403,
                text='<html><body><form><input type="password" name="pw"></form></body></html>',
                url="https://sso.univ.edu/auth",
            )
            self.assertTrue(proxy_rewriter.is_institutional_proxy_url(ez_url, config=cfg))

            with patch.object(graph, "load_config", return_value=cfg), \
                 patch.object(graph, "requests_impersonate", SimpleNamespace(get=lambda u, **kw: login_403)):
                dfs = graph._fetch_tables_via_impersonated_http(ez_url, config=cfg)
            self.assertEqual(dfs, [])
            self.assertIsNone(negative_cache.is_negatively_cached("direct_http", url=ez_url, db_path=db_path))

            with patch.object(strategies, "load_config", return_value=cfg), \
                 patch.object(strategies, "requests_impersonate", None), \
                 patch.object(strategies.requests, "get", return_value=login_403):
                dfs2 = strategies.extract_tables_via_direct_http(ez_url)
            self.assertEqual(dfs2, [])
            self.assertIsNone(negative_cache.is_negatively_cached("direct_http", url=ez_url, db_path=db_path))

    def test_doi_extraction_edge_cases_chndoi_fragment_pre_resolved_and_publisher_subpaths(self):
        # 1. Publisher subpaths & stacked suffixes
        cases = {
            "https://link.springer.com/article/10.1007/s00126-024-01333-9/figures/2": "10.1007/s00126-024-01333-9",
            "https://pubs.acs.org/doi/suppl/10.1021/acs.est.4c01234/suppl_file/es4c01234_si_001.pdf": "10.1021/acs.est.4c01234",
            "https://www.frontiersin.org/articles/10.3389/feart.2024.1234567/xml/nlm": "10.3389/feart.2024.1234567",
            "https://link.springer.com/article/10.1007/s00126-024-01333-9/full/pdf": "10.1007/s00126-024-01333-9",
            "https://link.springer.com/article/10.1007/s00126-024-01333-9/tables/1.html": "10.1007/s00126-024-01333-9",
            "https://doi.org/10.1016/S0016-7037(01)00745-1": "10.1016/S0016-7037(01)00745-1",
            "http://www.ysxb.ac.cn/article/doi/10.18654/1000-0569/2024.01.01": "10.18654/1000-0569/2024.01.01",
            "https://arxiv.org/abs/2010.12345/v1": "",
        }
        for url, expected in cases.items():
            self.assertEqual(strategies._extract_doi_from_url(url), expected, msg=url)

        # 2. chndoi.org with #fragment and URL-encoded %2F
        node = graph.ResolveMetadataNode(fallback_node=None)
        with patch.object(graph, "load_config", return_value={}), \
             patch.object(graph, "resolve_chinese_doi", return_value="https://kns.cnki.net/kcms2/article/abstract?v=abc"):
            st_frag, _ = node.execute({
                "pdf_path": "https://www.chndoi.org/Resolution/Handler?doi=10.18654/1000-0569/2024.01.01#sec1"
            })
            self.assertEqual(st_frag["doi"], "10.18654/1000-0569/2024.01.01")
            st_enc, _ = node.execute({
                "pdf_path": "https://www.chndoi.org/Resolution/Handler?doi=10.19658%2Fj.issn.1007-2802.2023.42.025#top"
            })
            self.assertEqual(st_enc["doi"], "10.19658/j.issn.1007-2802.2023.42.025")

        # 3. extract_tables_online pre_resolved chndoi URL preserves DOI even when resolved CNKI URL has no DOI
        captured_state = {}

        def fake_race(st, cancel_event=None):
            captured_state.update(st)
            return [], []

        with patch.object(graph, "resolve_chinese_doi", return_value="https://kns.cnki.net/kcms2/article/abstract?v=abc"), \
             patch.object(graph, "_race_extract", side_effect=fake_race):
            graph.extract_tables_online(
                "dummy.pdf",
                pre_resolved={
                    "url": "https://www.chndoi.org/Resolution/Handler?doi=10.19658/j.issn.1007-2802.2023.42.025",
                    "doi": None,
                    "is_cnki": False,
                },
                race=True,
            )
        self.assertEqual(captured_state.get("doi"), "10.19658/j.issn.1007-2802.2023.42.025")
        self.assertTrue(captured_state.get("is_cnki"))


if __name__ == "__main__":
    unittest.main()
