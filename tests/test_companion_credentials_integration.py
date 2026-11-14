import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import common
from online import doi_resolver, strategies


class TestCompanionCredentialsIntegration(unittest.TestCase):
    def test_parse_dotenv_file_handles_quotes_exports_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "# comment\n"
                    "ELSEVIER_API_KEY='dummy_key_123'\n"
                    'export ELSEVIER_INSTTOKEN="dummy_tok_456"\n'
                    "IS_CAMPUS_NETWORK=true # inline comment\n"
                    "UNPAYWALL_EMAIL=lab@example.edu\n"
                )
            parsed = common._parse_dotenv_file(env_path)
            self.assertEqual(parsed["ELSEVIER_API_KEY"], "dummy_key_123")
            self.assertEqual(parsed["ELSEVIER_INSTTOKEN"], "dummy_tok_456")
            self.assertEqual(parsed["IS_CAMPUS_NETWORK"], "true")
            self.assertEqual(parsed["UNPAYWALL_EMAIL"], "lab@example.edu")

    def test_load_config_companion_fallback_and_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = os.path.join(tmp, "home")
            scansci_dir = os.path.join(fake_home, ".scansci-pdf")
            os.makedirs(scansci_dir, exist_ok=True)
            with open(os.path.join(scansci_dir, "config.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "elsevier_api_key": "scansci_els_key",
                        "elsevier_insttoken": "scansci_inst_tok",
                        "email": "scansci@example.edu",
                        "openalex_api_key": "oa_key_1",
                    },
                    fh,
                )

            jsd_root = os.path.join(tmp, "journal-supp-downloader")
            os.makedirs(jsd_root, exist_ok=True)
            with open(os.path.join(jsd_root, ".env"), "w", encoding="utf-8") as fh:
                fh.write(
                    "ELSEVIER_API_KEY=dotenv_els_key\n"
                    "IS_CAMPUS_NETWORK=true\n"
                    "CROSSREF_EMAIL=crossref@example.edu\n"
                )

            cfg_file = os.path.join(tmp, "config.json")
            with open(cfg_file, "w", encoding="utf-8") as fh:
                json.dump({"ELSEVIER_API_KEY": "primary_cfg_key", "ELSEVIER_INSTTOKEN": ""}, fh)

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in {
                    "ELSEVIER_API_KEY",
                    "ELSEVIER_INSTTOKEN",
                    "IS_CAMPUS_NETWORK",
                    "UNPAYWALL_EMAIL",
                    "CROSSREF_EMAIL",
                    "OPENALEX_API_KEY",
                    "CORE_API_KEY",
                    "ZTE_DISABLE_COMPANION_CREDS",
                }
            }
            with patch.dict(os.environ, clean_env, clear=True), \
                 patch.object(common, "get_config_path", return_value=cfg_file), \
                 patch.object(common, "_discover_companion_downloader_roots", return_value=[jsd_root]), \
                 patch("os.path.expanduser", side_effect=lambda p: p.replace("~", fake_home)):
                cfg = common.load_config()
                # Primary config wins over companion sources
                self.assertEqual(cfg["ELSEVIER_API_KEY"], "primary_cfg_key")
                # Empty key in primary config falls back to companion ~/.scansci-pdf & .env
                self.assertEqual(cfg["ELSEVIER_INSTTOKEN"], "scansci_inst_tok")
                self.assertTrue(cfg["IS_CAMPUS_NETWORK"])
                self.assertEqual(cfg["UNPAYWALL_EMAIL"], "scansci@example.edu")
                self.assertEqual(cfg["CROSSREF_EMAIL"], "crossref@example.edu")
                self.assertEqual(cfg["OPENALEX_API_KEY"], "oa_key_1")

                # Explicit env overrides both primary config and companion config
                with patch.dict(os.environ, {"ELSEVIER_INSTTOKEN": "env_tok_override"}):
                    cfg2 = common.load_config()
                    self.assertEqual(cfg2["ELSEVIER_INSTTOKEN"], "env_tok_override")

    def test_elsevier_api_and_supp_and_pii_forward_insttoken(self):
        fake_cfg = {
            "ELSEVIER_API_KEY": "dummy_els_key",
            "ELSEVIER_INSTTOKEN": "dummy_inst_token",
            "UNPAYWALL_EMAIL": "team@example.edu",
            "CROSSREF_EMAIL": "cr@example.edu",
        }
        seen_calls = []

        def fake_get(url, headers=None, params=None, timeout=None, **kwargs):
            seen_calls.append((url, dict(headers or {}), params))
            if "/content/article/doi/" in url:
                xml = """<full-text-retrieval-response xmlns:ce="http://www.elsevier.com/xml/common/dtd">
                <originalText><xocs:doc xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">
                <ce:table id="t1"><ce:label>Table 1</ce:label>
                <ce:caption><ce:simple-para>Results</ce:simple-para></ce:caption>
                <tgroup cols="2"><thead><row><entry>Sample</entry><entry>Value</entry></row></thead>
                <tbody><row><entry>S1</entry><entry>12.5</entry></row></tbody></tgroup>
                </ce:table></xocs:doc></originalText></full-text-retrieval-response>"""
                return SimpleNamespace(status_code=200, text=xml, content=xml.encode("utf-8"))
            if "/content/object/doi/" in url:
                return SimpleNamespace(
                    status_code=200,
                    json=lambda: {"attachment-metadata-response": {"attachment": []}},
                )
            if "/content/abstract/pii/" in url:
                return SimpleNamespace(
                    status_code=200,
                    json=lambda: {"abstracts-retrieval-response": {"coredata": {"prism:doi": "10.1016/j.test.2026.1"}}},
                )
            return SimpleNamespace(status_code=404, text="")

        with patch.object(strategies, "load_config", return_value=fake_cfg), \
             patch.object(doi_resolver, "load_config", return_value=fake_cfg), \
             patch("requests.get", side_effect=fake_get):
            dfs = strategies.extract_tables_via_elsevier_api("10.1016/j.test.2026.1")
            self.assertEqual(len(dfs), 1)
            doi_val = doi_resolver._resolve_pii_to_doi("S0169136826000010")
            self.assertEqual(doi_val, "10.1016/j.test.2026.1")

        self.assertGreaterEqual(len(seen_calls), 3)
        for _url, hdrs, _params in seen_calls:
            self.assertEqual(hdrs.get("X-ELS-APIKey"), "dummy_els_key")
            self.assertEqual(hdrs.get("X-ELS-InstToken"), "dummy_inst_token")

    def test_unpaywall_and_crossref_use_polite_email(self):
        fake_cfg = {
            "UNPAYWALL_EMAIL": "unpaywall@example.edu",
            "CROSSREF_EMAIL": "crossref@example.edu",
        }
        seen_urls = []
        seen_headers = []

        def fake_get(url, headers=None, timeout=None, **kwargs):
            seen_urls.append(url)
            seen_headers.append(dict(headers or {}))
            if "api.unpaywall.org" in url:
                return SimpleNamespace(status_code=200, json=lambda: {"oa_locations": []})
            if "api.crossref.org" in url:
                return SimpleNamespace(
                    status_code=200,
                    json=lambda: {
                        "message": {
                            "items": [
                                {"title": ["Mineralization and geochemical evolution of the deposit"], "DOI": "10.1007/test-doi"}
                            ]
                        }
                    },
                )
            return SimpleNamespace(status_code=404, text="")

        with patch.object(strategies, "load_config", return_value=fake_cfg), \
             patch.object(doi_resolver, "load_config", return_value=fake_cfg), \
             patch("requests.get", side_effect=fake_get):
            strategies.extract_tables_via_unpaywall("10.1007/test-doi")
            resolved = doi_resolver.get_doi_by_title("Mineralization and geochemical evolution of the deposit")
            self.assertEqual(resolved, "10.1007/test-doi")

        self.assertTrue(any("email=unpaywall%40example.edu" in u for u in seen_urls))
        self.assertTrue(any("mailto=crossref%40example.edu" in u for u in seen_urls))
        self.assertTrue(any("mailto:unpaywall@example.edu" in h.get("User-Agent", "") for h in seen_headers))
        self.assertTrue(any("mailto:crossref@example.edu" in h.get("User-Agent", "") for h in seen_headers))

    def test_load_cookies_from_file_supports_wrapped_cookies_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = os.path.join(tmp, "vpnsci-cookies.json")
            with open(cp, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "cookies": [
                            {"name": "SID", "value": "dummy_sid", "domain": ".sciencedirect.com", "path": "/"}
                        ]
                    },
                    fh,
                )
            loaded = common.load_cookies_from_file(cp)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["name"], "SID")
            self.assertEqual(loaded[0]["domain"], ".sciencedirect.com")


if __name__ == "__main__":
    unittest.main()
