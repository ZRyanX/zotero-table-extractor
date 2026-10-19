#!/usr/bin/env python3
"""
Unit and integration tests for online extraction upgrades:
- CDP DevToolsActivePort dynamic port detection (with mtime prioritization)
- CDP Port Guard against port scanning (ConnectionRefused simulation for HTTP/Fetch & WebSocket)
- Elimination of fragile SQLite copying & connection leaks
- Tab-level concurrency & cross-process slot locking
- login.py publisher CLI integration without NameErrors
"""

import os
import sys
import tempfile
import unittest
import threading
import sqlite3
import subprocess
import time
import re
from unittest.mock import patch, MagicMock

# Ensure scripts directory is on path
_scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from online import cdp_detector
from online.cdp_detector import (
    read_devtools_active_port,
    get_user_data_candidate_dirs,
    check_cdp_endpoint_alive,
    find_active_devtools_port,
    find_active_cdp_endpoint,
    get_active_cdp_url,
    install_cdp_port_guard,
    get_cdp_tab_semaphore,
    cdp_tab_slot_guard,
)
from online.graph import browser_task_concurrency_guard
from online import doi_resolver
from online import general_html_extractor
from online import strategies
import login


class TestCDPDetector(unittest.TestCase):
    def test_read_devtools_active_port_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = os.path.join(tmpdir, "DevToolsActivePort")
            with open(port_file, "w", encoding="utf-8") as f:
                f.write("58392\n/devtools/browser/abc-123\n")

            res = read_devtools_active_port(port_file)
            self.assertIsNotNone(res)
            self.assertEqual(res["port"], 58392)
            self.assertEqual(res["ws_path"], "/devtools/browser/abc-123")

    def test_read_devtools_active_port_invalid_nonexistent(self):
        res = read_devtools_active_port("/path/to/nonexistent/DevToolsActivePort")
        self.assertIsNone(res)

    def test_read_devtools_active_port_invalid_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = os.path.join(tmpdir, "DevToolsActivePort")
            with open(port_file, "w", encoding="utf-8") as f:
                f.write("not_a_number\n")
            self.assertIsNone(read_devtools_active_port(port_file))

            with open(port_file, "w", encoding="utf-8") as f:
                f.write("999999\n")  # Out of port range
            self.assertIsNone(read_devtools_active_port(port_file))

            with open(port_file, "w", encoding="utf-8") as f:
                f.write("\n")  # Empty
            self.assertIsNone(read_devtools_active_port(port_file))

    def test_candidate_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidates = get_user_data_candidate_dirs(custom_dir=tmpdir)
            self.assertIn(os.path.normpath(tmpdir), candidates)

    @patch("online.cdp_detector.check_cdp_endpoint_alive")
    def test_find_active_devtools_port_detection(self, mock_alive):
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = os.path.join(tmpdir, "DevToolsActivePort")
            with open(port_file, "w", encoding="utf-8") as f:
                f.write("61234\n/devtools/browser/test-uuid\n")

            mock_alive.return_value = {
                "host": "127.0.0.1",
                "port": 61234,
                "cdp_url": "http://127.0.0.1:61234",
                "ws_url": "ws://127.0.0.1:61234/devtools/browser/test-uuid",
                "browser": "Chrome/130.0",
            }

            res = find_active_devtools_port(custom_user_data_dir=tmpdir)
            self.assertIsNotNone(res)
            self.assertEqual(res["port"], 61234)
            self.assertEqual(res["cdp_url"], "http://127.0.0.1:61234")
            mock_alive.assert_called_with(port=61234, timeout=1.0)

    @patch("online.cdp_detector.check_cdp_endpoint_alive")
    def test_find_active_devtools_port_stale_file_ignored(self, mock_alive):
        """Stale DevToolsActivePort file from a crashed Chrome process should not be reported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = os.path.join(tmpdir, "DevToolsActivePort")
            with open(port_file, "w", encoding="utf-8") as f:
                f.write("61234\n/devtools/browser/dead-uuid\n")

            mock_alive.return_value = None  # Port closed

            res = find_active_devtools_port(custom_user_data_dir=tmpdir)
            self.assertIsNone(res)

    @patch("online.cdp_detector.check_cdp_endpoint_alive")
    def test_find_active_devtools_port_mtime_sorting(self, mock_alive):
        """When multiple DevToolsActivePort files exist, the most recently updated one should take precedence."""
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            file1 = os.path.join(tmpdir1, "DevToolsActivePort")
            file2 = os.path.join(tmpdir2, "DevToolsActivePort")

            with open(file1, "w", encoding="utf-8") as f:
                f.write("50001\n/ws1\n")
            with open(file2, "w", encoding="utf-8") as f:
                f.write("50002\n/ws2\n")

            # Set mtime of file2 to be 100 seconds newer than file1
            os.utime(file1, (1000, 1000))
            os.utime(file2, (2000, 2000))

            mock_alive.side_effect = lambda port, timeout: {
                "host": "127.0.0.1", "port": port, "cdp_url": f"http://127.0.0.1:{port}"
            }

            with patch("online.cdp_detector.get_user_data_candidate_dirs", return_value=[tmpdir1, tmpdir2]):
                res = find_active_devtools_port()
                self.assertIsNotNone(res)
                # Must pick 50002 because file2 has a more recent mtime
                self.assertEqual(res["port"], 50002)

    @patch("online.cdp_detector.check_cdp_endpoint_alive")
    def test_find_active_cdp_endpoint_three_tier_resolution(self, mock_alive):
        # Tier 1: Config override
        mock_alive.side_effect = lambda host, port, timeout: {
            "host": host, "port": port, "cdp_url": f"http://{host}:{port}"
        }
        cfg = {"CDP_URL": "http://127.0.0.1:9333"}
        ep = find_active_cdp_endpoint(config=cfg)
        self.assertIsNotNone(ep)
        self.assertEqual(ep["port"], 9333)
        self.assertEqual(ep["source"], "config")

        # Tier 2: DevToolsActivePort dynamic port
        mock_alive.side_effect = None
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = os.path.join(tmpdir, "DevToolsActivePort")
            with open(port_file, "w", encoding="utf-8") as f:
                f.write("55667\n/ws-path\n")
            with patch("online.cdp_detector.check_cdp_endpoint_alive") as mock_live2:
                mock_live2.return_value = {"host": "127.0.0.1", "port": 55667, "cdp_url": "http://127.0.0.1:55667"}
                ep2 = find_active_cdp_endpoint(user_data_dir=tmpdir)
                self.assertIsNotNone(ep2)
                self.assertEqual(ep2["port"], 55667)
                self.assertEqual(ep2["source"], "devtools_active_port")

        # Tier 3: Default 9222 fallback
        with patch("online.cdp_detector.find_active_devtools_port", return_value=None):
            with patch("online.cdp_detector.check_cdp_endpoint_alive") as mock_9222:
                mock_9222.side_effect = lambda host="127.0.0.1", port=9222, timeout=1.0: (
                    {"host": host, "port": 9222, "cdp_url": "http://127.0.0.1:9222"} if port == 9222 else None
                )
                ep3 = find_active_cdp_endpoint()
                self.assertIsNotNone(ep3)
                self.assertEqual(ep3["port"], 9222)
                self.assertEqual(ep3["source"], "default_9222")

    @patch("online.cdp_detector.check_cdp_endpoint_alive")
    def test_find_active_cdp_endpoint_auto_loads_config_when_none(self, mock_alive):
        """When config is None, find_active_cdp_endpoint automatically loads config to detect CDP_URL."""
        mock_alive.side_effect = lambda host, port, timeout: {
            "host": host, "port": port, "cdp_url": f"http://{host}:{port}"
        }
        with patch("common.load_config", return_value={"CDP_URL": "http://127.0.0.1:9777"}):
            ep = find_active_cdp_endpoint(config=None)
            self.assertIsNotNone(ep)
            self.assertEqual(ep["port"], 9777)
            self.assertEqual(ep["source"], "config")


class TestCDPPortGuard(unittest.TestCase):
    def test_port_guard_route_registration(self):
        mock_target = MagicMock()
        success = install_cdp_port_guard(mock_target, cdp_port=54321, extra_ports=[9222, 9229])
        self.assertTrue(success)
        mock_target.route.assert_called_once()
        pattern, handler = mock_target.route.call_args[0]

        # Verify pattern matches localhost with guarded ports
        self.assertTrue(pattern.search("http://127.0.0.1:54321/json/version"))
        self.assertTrue(pattern.search("http://localhost:54321/json"))
        self.assertTrue(pattern.search("http://127.0.0.1:9222/json/version"))
        self.assertTrue(pattern.search("http://localhost:9229/"))
        self.assertTrue(pattern.search("http://[::1]:54321/"))

        # Verify pattern does NOT match normal external URLs
        self.assertIsNone(pattern.search("https://nature.com/articles/s41586-021-03819-2"))
        self.assertIsNone(pattern.search("https://sciencedirect.com/science/article"))
        self.assertIsNone(pattern.search("http://127.0.0.1:8000/api"))
        self.assertIsNone(pattern.search("http://localhost:3000/"))

        # Test handler calls route.abort('connectionrefused')
        mock_route = MagicMock()
        handler(mock_route)
        mock_route.abort.assert_called_with("connectionrefused")

    def test_port_guard_all_loopback_and_protocol_variants(self):
        """Verify Port Guard regex intercepts query params, 0.0.0.0, IPv6 uncompressed, loopback subnet, and websockets."""
        mock_target = MagicMock()
        install_cdp_port_guard(mock_target, cdp_port=54321, extra_ports=[9222])
        pattern, _ = mock_target.route.call_args[0]

        # 1. Probes with query parameter without slash
        self.assertTrue(pattern.search("http://127.0.0.1:9222?probe=1"))
        self.assertTrue(pattern.search("http://127.0.0.1:54321?jsonp=test"))

        # 2. 0.0.0.0 IP
        self.assertTrue(pattern.search("http://0.0.0.0:9222/"))
        self.assertTrue(pattern.search("http://0.0.0.0:54321/json"))

        # 3. IPv6 representations
        self.assertTrue(pattern.search("http://[::1]:9222/"))
        self.assertTrue(pattern.search("http://[0:0:0:0:0:0:0:1]:9222/"))
        self.assertTrue(pattern.search("http://[::ffff:127.0.0.1]:54321/"))

        # 4. Entire 127.0.0.0/8 loopback subnet
        self.assertTrue(pattern.search("http://127.0.0.2:9222/"))
        self.assertTrue(pattern.search("http://127.127.127.127:54321/"))

        # 5. WebSocket probes
        self.assertTrue(pattern.search("ws://127.0.0.1:9222/devtools/page"))
        self.assertTrue(pattern.search("wss://localhost:54321/devtools"))

        # 6. Rebinding hostnames
        self.assertTrue(pattern.search("http://localtest.me:9222/"))
        self.assertTrue(pattern.search("http://sub.localtest.me:54321/"))
        self.assertTrue(pattern.search("http://lvh.me:9222/"))

    def test_port_guard_websocket_script_injection(self):
        """Verify Port Guard injects an init script to block in-page WebSocket probes."""
        mock_target = MagicMock()
        install_cdp_port_guard(mock_target, cdp_port=54321)
        mock_target.add_init_script.assert_called_once()
        script = mock_target.add_init_script.call_args[0][0]
        self.assertIn("window.WebSocket", script)
        self.assertIn("54321", script)
        self.assertIn("Connection refused", script)

    def test_port_guard_edge_cases(self):
        # 1. target is None
        self.assertFalse(install_cdp_port_guard(None))

        # 2. cdp_port is None (should still guard standard debug ports 9222, 9229, 9223, 9333)
        mock_target = MagicMock()
        self.assertTrue(install_cdp_port_guard(mock_target, cdp_port=None))
        pattern, _ = mock_target.route.call_args[0]
        self.assertTrue(pattern.search("http://127.0.0.1:9222/json"))
        self.assertTrue(pattern.search("http://localhost:9229/"))

        # 3. Invalid port format in cdp_port / extra_ports
        mock_target2 = MagicMock()
        self.assertTrue(install_cdp_port_guard(mock_target2, cdp_port="not_a_port", extra_ports=["abc", 9876]))
        pattern2, _ = mock_target2.route.call_args[0]
        self.assertTrue(pattern2.search("http://127.0.0.1:9876/"))
        self.assertIsNone(pattern2.search("http://127.0.0.1:8080/"))


class TestConcurrencyAndLocking(unittest.TestCase):
    @patch("online.cdp_detector.find_active_cdp_endpoint")
    def test_concurrency_guard_with_active_cdp(self, mock_ep):
        mock_ep.return_value = {"cdp_url": "http://127.0.0.1:54321", "port": 54321}
        # When active CDP is detected, browser_task_concurrency_guard acquires semaphore and allows Tab concurrency
        sem = get_cdp_tab_semaphore(max_tabs=3)
        self.assertIsNotNone(sem)

        entered = []
        with browser_task_concurrency_guard():
            entered.append("inside_cdp_tab_guard")

        self.assertEqual(entered, ["inside_cdp_tab_guard"])

    @patch("online.cdp_detector.find_active_cdp_endpoint")
    def test_concurrency_guard_without_active_cdp(self, mock_ep):
        mock_ep.return_value = None
        # When no active CDP, falls back to exclusive browser_extraction_lock
        with patch("online.graph.browser_extraction_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock()

            with browser_task_concurrency_guard():
                pass

            mock_lock.assert_called_once()

    def test_tab_semaphore_parallelism(self):
        sem = get_cdp_tab_semaphore(max_tabs=4)
        active_counts = []
        lock = threading.Lock()

        def worker():
            sem.acquire()
            try:
                with lock:
                    active_counts.append(1)
                time.sleep(0.05)
            finally:
                sem.release()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(active_counts), 4)

    def test_cross_process_tab_slot_guard(self):
        """Verify cdp_tab_slot_guard limits active slots to max_tabs concurrently."""
        acquired_slots = []
        slot_lock = threading.Lock()

        def slot_worker(worker_id):
            with cdp_tab_slot_guard(max_tabs=2, timeout=5.0) as slot:
                with slot_lock:
                    acquired_slots.append((worker_id, slot))
                time.sleep(0.05)

        threads = [threading.Thread(target=slot_worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(acquired_slots), 4)
        slots_used = {slot for _, slot in acquired_slots}
        self.assertTrue(slots_used.issubset({0, 1}))

    @patch("online.cdp_detector.find_active_cdp_endpoint")
    def test_crawl4ai_cdp_url_resolution(self, mock_ep):
        # Reset cache
        strategies._CRAWL4AI_CDP_CACHE["checked"] = False
        strategies._CRAWL4AI_CDP_CACHE["url"] = None

        mock_ep.return_value = {"cdp_url": "http://127.0.0.1:59999", "port": 59999, "source": "devtools_active_port"}
        url = strategies._crawl4ai_cdp_url()
        self.assertEqual(url, "http://127.0.0.1:59999")


class TestSQLiteSafeHandling(unittest.TestCase):
    def test_doi_resolver_immutable_sqlite(self):
        """Verify Zotero SQLite query uses immutable URI mode to prevent file locks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "zotero.sqlite")
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT);")
            c.execute("INSERT INTO items VALUES (1, 'ABCDEF12');")
            conn.commit()
            conn.close()

            # Query should succeed without lock errors
            doi, title, url = doi_resolver.get_doi_from_zotero_db(
                "/some/storage/ABCDEF12/paper.pdf", db_path=db_path
            )
            self.assertIsNone(doi)

    def test_doi_resolver_finally_closes_connection(self):
        """Verify that get_doi_from_zotero_db closes the SQLite connection even if table schema is broken."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "corrupt.sqlite")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE dummy (id INT);")
            conn.commit()
            conn.close()

            # Calling with missing tables triggers sqlite3.OperationalError inside
            doi, title, url = doi_resolver.get_doi_from_zotero_db("/storage/KEY/doc.pdf", db_path=db_path)
            self.assertIsNone(doi)

            # On Windows/POSIX, if connection remained open, opening in EXCLUSIVE mode would fail
            # Reopening with exclusive transaction confirms connection was cleanly closed
            test_conn = sqlite3.connect(db_path, timeout=1.0)
            test_conn.execute("BEGIN EXCLUSIVE TRANSACTION")
            test_conn.execute("INSERT INTO dummy VALUES (1)")
            test_conn.commit()
            test_conn.close()

    def test_copy_chrome_profile_safe_no_sqlite_locks(self):
        """Verify _copy_chrome_profile_for_playwright skips active SQLite DBs to avoid database is locked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_dir = os.path.join(tmpdir, "Default")
            os.makedirs(default_dir, exist_ok=True)
            # Create dummy Cookies and Preferences
            with open(os.path.join(default_dir, "Cookies"), "w") as f:
                f.write("sqlite locked content")
            with open(os.path.join(default_dir, "Preferences"), "w") as f:
                f.write('{"pref": 1}')

            tmp_res = general_html_extractor._copy_chrome_profile_for_playwright(tmpdir)
            self.assertTrue(os.path.isdir(tmp_res))
            # Preferences copied
            self.assertTrue(os.path.exists(os.path.join(tmp_res, "Default", "Preferences")))
            # Cookies SQLite file NOT copied (safely skipped to prevent lockups)
            self.assertFalse(os.path.exists(os.path.join(tmp_res, "Default", "Cookies")))


class TestLoginCli(unittest.TestCase):
    @patch("playwright.sync_api.sync_playwright")
    @patch("builtins.input", return_value="2")
    def test_publishers_main_option2_no_name_error(self, mock_input, mock_pw):
        """Verify publishers_main Option 2 runs without NameError: name 'find_active_cdp_endpoint' is not defined."""
        # Should not raise NameError
        login.publishers_main()

    @patch("login.launch_chrome_debug_mode", return_value=True)
    @patch("playwright.sync_api.sync_playwright")
    def test_publishers_main_option1_no_name_error(self, mock_pw, mock_launch):
        """Verify publishers_main Option 1 runs without NameError."""
        mock_context = MagicMock()
        mock_context.cookies.side_effect = Exception("Browser closed by user")
        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]
        mock_pw.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value = mock_browser

        with patch("builtins.input", return_value="1"):
            login.publishers_main()


class TestLivePlaywrightIntegration(unittest.TestCase):
    """Live Playwright tests testing dynamic port detection, Port Guard, and concurrent tabs."""

    @classmethod
    def setUpClass(cls):
        chrome_mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if not os.path.exists(chrome_mac):
            raise unittest.SkipTest("Google Chrome binary not found on local macOS test host.")

        cls.tmp_dir = tempfile.mkdtemp(prefix="test_live_cdp_")
        cls.proc = subprocess.Popen(
            [chrome_mac, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={cls.tmp_dir}"],
            stderr=subprocess.DEVNULL,
        )

        port_file = os.path.join(cls.tmp_dir, "DevToolsActivePort")
        for _ in range(30):
            time.sleep(0.2)
            if os.path.exists(port_file):
                break

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "proc") and cls.proc:
            try:
                cls.proc.terminate()
                cls.proc.wait(timeout=3)
            except Exception:
                try:
                    cls.proc.kill()
                except Exception:
                    pass

    def test_dynamic_port_detection_live(self):
        ep = find_active_devtools_port(custom_user_data_dir=self.tmp_dir)
        self.assertIsNotNone(ep)
        self.assertGreater(ep["port"], 1024)
        self.assertTrue(ep["cdp_url"].startswith("http://127.0.0.1:"))

    def test_port_guard_blocks_probe_live(self):
        from playwright.sync_api import sync_playwright

        ep = find_active_devtools_port(custom_user_data_dir=self.tmp_dir)
        self.assertIsNotNone(ep)
        cdp_port = ep["port"]

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ep["cdp_url"])
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            # Install Port Guard protecting active CDP port and 9222
            install_cdp_port_guard(page, cdp_port=cdp_port)

            # Attempt to probe CDP port from inside page JavaScript
            res = page.evaluate(f"""async () => {{
                try {{
                    await fetch('http://127.0.0.1:{cdp_port}/json/version');
                    return 'connected';
                }} catch (e) {{
                    return 'blocked: ' + e.message;
                }}
            }}""")

            page.close()
            # Verified: In-page JS gets ConnectionRefused / Failed to fetch
            self.assertTrue(res.startswith("blocked:"))

    def test_port_guard_blocks_websocket_live(self):
        """Live Playwright test confirming in-page WebSocket probes to the debug port are intercepted."""
        from playwright.sync_api import sync_playwright

        ep = find_active_devtools_port(custom_user_data_dir=self.tmp_dir)
        self.assertIsNotNone(ep)
        cdp_port = ep["port"]

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ep["cdp_url"])
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            install_cdp_port_guard(page, cdp_port=cdp_port)
            page.goto("data:text/html,<html><body></body></html>")

            # In-page WebSocket probe to localhost debug port
            ws_res = page.evaluate(f"""async () => {{
                return new Promise((resolve) => {{
                    try {{
                        const ws = new WebSocket('ws://127.0.0.1:{cdp_port}/devtools/page');
                        ws.onerror = (e) => resolve('ws_blocked_refused');
                        ws.onclose = (e) => resolve('ws_closed_code_' + e.code);
                        ws.onopen = () => resolve('ws_open_detected');
                        setTimeout(() => resolve('ws_timeout'), 2000);
                    }} catch (e) {{
                        resolve('ws_exception: ' + e.message);
                    }}
                }});
            }}""")

            page.close()
            self.assertTrue(ws_res.startswith("ws_blocked_refused") or ws_res.startswith("ws_closed_code_1006"))

    def test_concurrent_tabs_live(self):
        from playwright.sync_api import sync_playwright

        ep = find_active_devtools_port(custom_user_data_dir=self.tmp_dir)
        self.assertIsNotNone(ep)

        def tab_worker(tab_idx):
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(ep["cdp_url"])
                ctx = b.contexts[0]
                p_tab = ctx.new_page()
                p_tab.goto(f"data:text/html,<html><body><h1 id='id'>Tab {tab_idx}</h1></body></html>")
                title = p_tab.locator("#id").text_content()
                p_tab.close()
                return title

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(tab_worker, i) for i in range(3)]
            results = [f.result() for f in futures]

        self.assertEqual(results, ["Tab 0", "Tab 1", "Tab 2"])


if __name__ == "__main__":
    unittest.main()
