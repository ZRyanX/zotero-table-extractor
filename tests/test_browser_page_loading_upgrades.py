import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from online import cdp_detector, graph, strategies


class TestBrowserPageLoadingUpgrades(unittest.TestCase):
    def test_find_matching_cdp_page_matches_doi_pii_and_meta(self):
        page_other = SimpleNamespace(url="https://www.google.com/", evaluate=lambda _js: "")
        page_pii = SimpleNamespace(
            url="https://www.sciencedirect.com/science/article/pii/S0169136824001471",
            evaluate=lambda _js: "",
        )
        page_meta_doi = SimpleNamespace(
            url="https://www.cell.com/current-biology/fulltext/unknown",
            evaluate=lambda _js: "10.1016/j.cub.2025.01.001",
        )
        browser_pii = SimpleNamespace(contexts=[SimpleNamespace(pages=[page_other, page_pii])])
        matched_pii = cdp_detector.find_matching_cdp_page(
            browser_pii, "https://www.sciencedirect.com/science/article/pii/S0169136824001471?via=ihub"
        )
        self.assertIs(matched_pii, page_pii)

        browser_meta = SimpleNamespace(contexts=[SimpleNamespace(pages=[page_other, page_meta_doi])])
        matched_meta = cdp_detector.find_matching_cdp_page(
            browser_meta, "https://doi.org/10.1016/j.cub.2025.01.001"
        )
        self.assertIs(matched_meta, page_meta_doi)

    def test_transient_challenge_detection_and_wait(self):
        self.assertTrue(
            cdp_detector.looks_like_transient_challenge_shell(
                "<html><title>Just a moment...</title><body>cf-turnstile</body></html>",
                "https://onlinelibrary.wiley.com/doi/full/10.1111/test",
            )
        )
        self.assertTrue(
            cdp_detector.looks_like_transient_challenge_shell(
                "<html><body><div>Loading...</div></body></html>",
                "https://pubs.aip.org/aip/jap/article/1/2/3",
            )
        )
        self.assertTrue(
            cdp_detector.looks_like_transient_challenge_shell(
                "<html><body>ok</body></html>",
                "https://www.sciencedirect.com/science/article/pii/S123?crasolve=1",
            )
        )
        self.assertFalse(
            cdp_detector.looks_like_transient_challenge_shell(
                "<html><body><h1>Full Article</h1><p>" + ("x" * 1600) + "</p></body></html>",
                "https://www.sciencedirect.com/science/article/pii/S123",
            )
        )

        class FakePage:
            def __init__(self):
                self.url = "https://linkinghub.elsevier.com/retrieve/pii/S0169136824001471"
                self.polls = 0
                self.gotos = []

            def wait_for_timeout(self, _ms):
                self.polls += 1
                if self.polls >= 2:
                    self.url = "https://www.sciencedirect.com/science/article/pii/S0169136824001471"

            def content(self):
                if self.polls < 3:
                    return "<html><body>Checking your browser...</body></html>"
                return "<html><body><article>" + ("data " * 400) + "</article></body></html>"

            def evaluate(self, _js):
                return None

            def goto(self, u, **_kw):
                self.gotos.append(u)

        fp = FakePage()
        settled = cdp_detector.wait_through_transient_challenge(fp, max_wait_s=2.0)
        self.assertTrue(settled)
        self.assertGreaterEqual(fp.polls, 3)

    def test_merge_blocked_domains_exempts_target_and_handles_proxy(self):
        blocked = cdp_detector.merge_blocked_domains("https://pubs.geoscienceworld.org/gsa/geology/article/1")
        self.assertIn("cookie-cdn.cookiepro.com", blocked)
        self.assertIn("geolocation.onetrust.com", blocked)
        self.assertIn("ajax.googleapis.com", blocked)

        blocked_proxy = cdp_detector.merge_blocked_domains(
            "https://onlinelibrary.wiley.com/doi/full/10.1002/test", proxy="http://127.0.0.1:7890"
        )
        self.assertNotIn("ajax.googleapis.com", blocked_proxy)
        self.assertIn("cookie-cdn.cookiepro.com", blocked_proxy)

        blocked_self = cdp_detector.merge_blocked_domains("https://scholar.google.com/citations")
        self.assertNotIn("scholar.google.com", blocked_self)

    def test_publisher_wait_selectors_and_url_normalization(self):
        self.assertIn(".tables", cdp_detector.get_publisher_table_wait_selector("https://www.sciencedirect.com/science/article/pii/S1"))
        self.assertIn(".article-table-content", cdp_detector.get_publisher_table_wait_selector("https://onlinelibrary.wiley.com/doi/full/10.1002/1"))
        self.assertIn(".table-wrap", cdp_detector.get_publisher_table_wait_selector("https://pubs.geoscienceworld.org/gsa/1"))
        self.assertIn(".NLM_table-wrap", cdp_detector.get_publisher_table_wait_selector("https://pubs.acs.org/doi/10.1021/test"))
        self.assertEqual(
            graph._normalize_publisher_url("http://10.5066/P94MI7E5"),
            "https://doi.org/10.5066/P94MI7E5",
        )
        self.assertEqual(
            graph._normalize_publisher_url("https://pubs.acs.org/doi/abs/10.1021/test"),
            "https://pubs.acs.org/doi/full/10.1021/test",
        )

    def test_scrapling_headed_uses_real_chrome_and_bsk_fallback(self):
        calls = []
        blocked_html = "<html><title>Just a moment...</title><body>cf-turnstile</body></html>"
        good_html = "<html><body><table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table></body></html>"

        def fake_fetch(_url, extra_kwargs=None):
            calls.append(dict(extra_kwargs or {}))
            return blocked_html

        with patch.object(strategies, "_scrapling_fetch_html", side_effect=fake_fetch), \
             patch.object(cdp_detector, "has_system_chrome", return_value=True), \
             patch.object(cdp_detector, "try_fetch_html_via_browserskill", return_value=(good_html, None, "https://www.wiley.com/test")) as bsk_mock:
            dfs = strategies.extract_tables_via_scrapling("https://onlinelibrary.wiley.com/doi/full/10.1002/test")

        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["headless"])
        self.assertFalse(calls[1]["headless"])
        self.assertTrue(calls[1]["real_chrome"])
        bsk_mock.assert_called_once()
        self.assertEqual(len(dfs), 1)


if __name__ == "__main__":
    unittest.main()
