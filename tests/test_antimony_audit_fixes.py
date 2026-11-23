#!/usr/bin/env python3
"""
Regression test suite for all 12 root causes identified in the Zotero "锑矿"
290-paper live online-extraction audit (P0/P1/P2 fixes).
"""

import contextlib
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock, patch

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import common
import excel_export
import extract_zotero_table as ezt
import table_validator
from online import doi_resolver, general_html_extractor, graph, negative_cache, strategies


class TestRC1Pre2020GateAndCnkiThesis(unittest.TestCase):
    """RC-1 & RC-5: Zotero Chinese filename false-positives & CNKI thesis fast-skip."""

    def test_english_zotero_filenames_with_chinese_connectors_not_misclassified(self):
        english_cases = [
            "Pavlova和Borisenko - 2008 - The age of ag–sb deposits of central asia.pdf",
            "Akçay 等 - 2003 - Geochemistry of a high-T hydrothermal dolostone.pdf",
            "An和Zhu - 2010 - Native antimony in the Baogutu gold deposit.pdf",
            "Dill和Horn - 1996 - The origin of a hypogene sarabauite-calcite mineralization.pdf",
            "Smith 等_2015.pdf",
        ]
        for fname in english_cases:
            self.assertFalse(
                ezt.is_pre_2020_chinese_paper(fname, plan=None),
                f"English PDF filename misclassified as pre-2020 Chinese: {fname}",
            )

    def test_genuine_pre2020_chinese_papers_still_detected(self):
        cn_cases = [
            "胡阿香和彭建堂 - 2018 - 湖南渣滓溪锑钨矿床流体包裹体特征.pdf",
            "张三等 - 2015 - 黔东锑矿地质特征.pdf",
            "2018_LA-ICP-MS_U-Pb_定年及成矿意义.pdf",
            "张三等_2015.pdf",
        ]
        for fname in cn_cases:
            self.assertTrue(
                ezt.is_pre_2020_chinese_paper(fname, plan=None),
                f"Pre-2020 Chinese PDF was NOT detected: {fname}",
            )

    def test_post2020_chinese_papers_not_skipped(self):
        self.assertFalse(
            ezt.is_pre_2020_chinese_paper(
                "胡阿香和彭建堂 - 2020 - 湖南渣滓溪锑钨矿床流体包裹体特征.pdf",
                plan=None,
            )
        )
        self.assertFalse(
            ezt.is_pre_2020_chinese_paper(
                "test.pdf",
                plan={"title": "湖南渣滓溪锑钨矿床", "year": "2023"},
            )
        )

    def test_cnki_thesis_detection(self):
        self.assertTrue(doi_resolver.is_cnki_thesis_metadata(doi="10.27493/d.cnki.gzdzy.2019.001622"))
        self.assertTrue(doi_resolver.is_cnki_thesis_metadata(doi="10.27200/d.cnki.gkmlu.2022.002505"))
        self.assertTrue(
            doi_resolver.is_cnki_thesis_metadata(
                url="https://kns.cnki.net/kcms2/article/abstract?dbcode=CMFD&filename=1022001234.nh"
            )
        )
        self.assertTrue(
            doi_resolver.is_cnki_thesis_metadata(
                item_type="thesis", title="滇东南木利锑矿床成矿与岩浆作用的关系"
            )
        )
        # Non-thesis journal articles and English theses must NOT be skipped as CNKI thesis
        self.assertFalse(doi_resolver.is_cnki_thesis_metadata(doi="10.16539/j.ddgzyckx.2020.03.008"))
        self.assertFalse(
            doi_resolver.is_cnki_thesis_metadata(
                item_type="thesis",
                title="Geology and geochemistry of antimony deposits",
                url="https://www.proquest.com/docview/12345",
            )
        )

    def test_race_extract_fast_skips_cnki_thesis(self):
        state = {
            "url": "https://link.cnki.net/doi/10.27493/d.cnki.gzdzy.2020.000425",
            "doi": "10.27493/d.cnki.gzdzy.2020.000425",
            "title": "甘肃崖湾锑矿床地质地球化学特征及成因探讨",
            "is_cnki": True,
        }
        dfs, history = graph._race_extract(state)
        self.assertEqual(dfs, [])
        self.assertEqual(history, ["skip:cnki-thesis"])

    def test_get_zotero_item_metadata_reads_type_and_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "zotero.sqlite")
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
                CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER, key TEXT);
                CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, path TEXT);
                CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
                CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
                CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
                INSERT INTO itemTypes VALUES (1, 'thesis'), (2, 'attachment');
                INSERT INTO items VALUES (10, 1, 'PARENT01'), (11, 2, 'ATTKEY01');
                INSERT INTO itemAttachments VALUES (11, 10, 'storage:demo.pdf');
                INSERT INTO fields VALUES (1, 'DOI'), (2, 'title'), (3, 'url'), (4, 'date');
                INSERT INTO itemDataValues VALUES
                    (1, '10.27200/d.cnki.gkmlu.2022.002505'),
                    (2, '滇东南木利锑矿床成矿与岩浆作用的关系'),
                    (3, 'https://kns.cnki.net/kcms2/article/abstract?dbcode=CMFD'),
                    (4, '2022-05-01');
                INSERT INTO itemData VALUES (10, 1, 1), (10, 2, 2), (10, 3, 3), (10, 4, 4);
                """
            )
            conn.commit()
            conn.close()

            pdf_fake = os.path.join(tmp, "storage", "ATTKEY01", "demo.pdf")
            meta = doi_resolver.get_zotero_item_metadata(pdf_fake, db_path=db_path)
            self.assertEqual(meta["item_type"], "thesis")
            self.assertEqual(meta["year"], "2022")
            self.assertEqual(meta["doi"], "10.27200/d.cnki.gkmlu.2022.002505")
            self.assertIn("木利锑矿", meta["title"])


class TestRC2LockAndEphemeralCDP(unittest.TestCase):
    """RC-2: Real cross-process lock exclusion & ephemeral owned debug Chrome isolation."""

    def test_browser_extraction_lock_cross_process_mutual_exclusion(self):
        lock_name = f"test_mutex_{os.getpid()}_{int(time.time() * 1000)}"
        child_code = "\n".join([
            "import sys",
            f"sys.path.insert(0, {SCRIPTS_DIR!r})",
            "import common",
            "try:",
            f"    with common.browser_extraction_lock({lock_name!r}, timeout=0.4):",
            "        print('ACQUIRED')",
            "except TimeoutError:",
            "    print('TIMEOUT')",
        ])
        with common.browser_extraction_lock(lock_name, timeout=5.0):
            res = subprocess.run(
                [sys.executable, "-c", child_code],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertIn("TIMEOUT", res.stdout, f"Child bypassed lock! stdout={res.stdout!r}, stderr={res.stderr!r}")

        # After parent releases lock, child must acquire immediately
        res2 = subprocess.run(
            [sys.executable, "-c", child_code],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertIn("ACQUIRED", res2.stdout, f"Child failed to acquire released lock: {res2.stdout!r} {res2.stderr!r}")

    def test_owned_debug_chrome_uses_ephemeral_port_and_temp_profile(self):
        captured_cmd = []
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None

        def _fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return fake_proc

        orig_procs = list(general_html_extractor._OWNED_DEBUG_CHROME_PROCS)
        general_html_extractor._OWNED_DEBUG_CHROME_PROCS.clear()
        try:
            with patch("online.cdp_detector.find_active_cdp_endpoint", return_value=None), \
                 patch("online.cdp_detector.check_cdp_endpoint_alive", side_effect=lambda port, timeout=0.5: {"port": port, "cdp_url": f"http://127.0.0.1:{port}"}), \
                 patch("os.path.exists", return_value=True), \
                 patch("subprocess.Popen", side_effect=_fake_popen):
                ok = general_html_extractor._ensure_chrome_debug_running()
                self.assertTrue(ok)
                port_args = [a for a in captured_cmd if a.startswith("--remote-debugging-port=")]
                dir_args = [a for a in captured_cmd if a.startswith("--user-data-dir=")]
                self.assertEqual(len(port_args), 1)
                self.assertNotEqual(port_args[0], "--remote-debugging-port=9222")
                self.assertEqual(len(dir_args), 1)
                self.assertIn("zte_owned_cdp_", dir_args[0])
                ep = getattr(general_html_extractor._THREAD_OWNED_CDP, "endpoint", None)
                self.assertIsNotNone(ep)
                self.assertEqual(ep["source"], "owned_ephemeral")
        finally:
            general_html_extractor._cleanup_owned_debug_chrome(0)
            general_html_extractor._OWNED_DEBUG_CHROME_PROCS[:] = orig_procs


class TestRC3SupplementaryExtraction(unittest.TestCase):
    """RC-3: MDPI /s1 discovery, Tier-1 built-in supp, & fallback when external downloader times out."""

    def test_discover_mdpi_s1_supplementary_link(self):
        html = """
        <html><body>
          <section class="supplementary">
            <p>Supplementary Materials: <a href="/article/10.3390/geosciences15120462/s1">Table S1–S4</a></p>
          </section>
        </body></html>
        """
        links = strategies._discover_supplementary_links("https://www.mdpi.com/2076-3263/15/12/462", html)
        self.assertTrue(any("/s1" in item["url"] and item["marker"] == "strong" for item in links), links)

    def test_extract_general_supplementary_prefers_builtin_when_html_given(self):
        fake_df = pd.DataFrame({"Sample": ["S1"], "Sb_ppm": ["12.3"]})
        with patch.object(strategies, "extract_builtin_supplementary_tables", return_value=[fake_df]) as mock_builtin, \
             patch.object(subprocess, "Popen") as mock_popen:
            dfs = strategies.extract_general_supplementary_tables(
                "https://link.springer.com/article/10.1007/s00126-025-01387-3",
                html_content="<html><a href='126_2025_1387_MOESM2_ESM.xlsx'>ESM 2</a></html>",
            )
            self.assertEqual(len(dfs), 1)
            mock_builtin.assert_called_once()
            mock_popen.assert_not_called()

    def test_extract_general_supplementary_falls_back_to_builtin_after_timeout(self):
        fake_proc = MagicMock()
        fake_proc.pid = 77777
        fake_proc.poll.return_value = None
        fake_df = pd.DataFrame({"Sample": ["A1"], "Au": ["1.2"]})
        mono_vals = iter([10.0, 99.0, 99.0])

        with patch.object(os.path, "exists", return_value=True), \
             patch.object(subprocess, "Popen", return_value=fake_proc), \
             patch.object(subprocess, "run"), \
             patch.object(time, "monotonic", side_effect=lambda: next(mono_vals, 99.0)), \
             patch.object(strategies, "extract_builtin_supplementary_tables", return_value=[fake_df]) as mock_builtin:
            dfs = strategies.extract_general_supplementary_tables(
                "https://link.springer.com/article/10.1007/s00126-019-00937-w",
                max_timeout=5,
            )
            self.assertEqual(len(dfs), 1)
            mock_builtin.assert_called_once()

    def test_direct_http_appends_builtin_supplementary_when_main_html_has_no_tables(self):
        springer_html = """
        <html><head><meta name="citation_doi" content="10.1007/s00126-025-01387-3"></head>
        <body><article><a href="https://static-content.springer.com/esm/art%3A10.1007/MediaObjects/126_2025_1387_MOESM2_ESM.xlsx">Supplementary Table S1</a></article></body></html>
        """
        fake_resp = SimpleNamespace(status_code=200, text=springer_html, url="https://link.springer.com/article/10.1007/s00126-025-01387-3")
        fake_supp_df = pd.DataFrame({"Sample": ["ZK1"], "Sb": ["5.4"]})
        fake_supp_df.attrs["label"] = "Supplementary Table S1"

        meta = {}
        with patch.object(strategies, "requests_impersonate", None), \
             patch.object(strategies.requests, "get", return_value=fake_resp), \
             patch.object(strategies, "extract_builtin_supplementary_tables", return_value=[fake_supp_df]) as mock_supp:
            dfs = strategies.extract_tables_via_direct_http(
                "https://link.springer.com/article/10.1007/s00126-025-01387-3",
                meta_out=meta,
            )
            self.assertEqual(len(dfs), 1)
            self.assertEqual(meta.get("status_code"), 200)
            mock_supp.assert_called_once()


class TestRC4WaterfallOptimizations(unittest.TestCase):
    """RC-4: Skip redundant ImpersonatedHTTP, skip duplicate Tier-4 Unpaywall, enforce time budget."""

    def test_direct_http_200_via_impersonate_skips_duplicate_impersonated_http(self):
        def _fake_direct(url, table_idx="all", cancel_event=None, doi=None, meta_out=None):
            if isinstance(meta_out, dict):
                meta_out["used_impersonate"] = True
                meta_out["status_code"] = 200
                meta_out["was_blocked"] = False
            return []

        with patch.object(graph, "extract_tables_via_direct_http", side_effect=_fake_direct), \
             patch.object(graph, "_fetch_tables_via_impersonated_http") as mock_imp, \
             patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
             patch.object(graph, "extract_tables_via_ncbi_pmc", return_value=[]), \
             patch.object(graph, "extract_tables_via_crawl4ai", return_value=[]), \
             patch.object(graph, "extract_tables_via_scrapling", return_value=[]), \
             patch.object(graph, "extract_tables_via_firecrawl", return_value=[]), \
             patch.object(graph, "_run_node_locked", return_value=({"dfs": []}, None)), \
             patch.object(graph.AcademicSearchGraphNode, "execute", return_value=({"dfs": []}, None)), \
             patch.object(graph.BrowserActBypassNode, "execute", return_value=({"dfs": []}, None)):
            graph._race_extract({"url": "https://example.org/paper", "doi": "10.9999/test", "is_cnki": False})
            mock_imp.assert_not_called()

    def test_academic_search_skips_unpaywall_when_tier1_already_tried(self):
        with patch.object(graph, "extract_tables_via_unpaywall") as mock_upw, \
             patch.object(graph.requests, "get", side_effect=RuntimeError("offline")):
            node = graph.AcademicSearchGraphNode()
            node.execute({"doi": "10.1000/abc", "_unpaywall_tried": True})
            mock_upw.assert_not_called()


class TestRC6ElsevierXmlEdgeCases(unittest.TestCase):
    """RC-6: Preserve 1-row headerless Elsevier tables & warn on <ce:link> image tables."""

    def test_elsevier_xocs_preserves_single_row_headerless_table(self):
        xml = """
        <full-text-retrieval-response xmlns:ce="http://www.elsevier.com/xml/common/dtd">
          <ce:table id="tbl1">
            <ce:label>Table 1</ce:label>
            <ce:caption><ce:simple-para>Summary of fluid inclusion parameters</ce:simple-para></ce:caption>
            <tgroup cols="3">
              <colspec colname="col1"/><colspec colname="col2"/><colspec colname="col3"/>
              <tbody>
                <row><entry>Stage I</entry><entry>245 °C</entry><entry>6.2 wt%</entry></row>
              </tbody>
            </tgroup>
          </ce:table>
        </full-text-retrieval-response>
        """
        dfs = strategies._parse_elsevier_xocs_tables(xml)
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].shape, (1, 3))
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")
        self.assertEqual(dfs[0].iat[0, 0], "Stage I")

    def test_elsevier_xocs_logs_warning_for_image_table_link(self):
        xml = """
        <full-text-retrieval-response xmlns:ce="http://www.elsevier.com/xml/common/dtd">
          <ce:table id="tbl7">
            <ce:label>Table 7</ce:label>
            <ce:caption><ce:simple-para>Mineral paragenesis chart</ce:simple-para></ce:caption>
            <ce:link locator="fx1"/>
          </ce:table>
        </full-text-retrieval-response>
        """
        with patch("builtins.print") as mock_print:
            dfs = strategies._parse_elsevier_xocs_tables(xml)
            self.assertEqual(dfs, [])
            printed = "\n".join(" ".join(str(a) for a in call.args) for call in mock_print.call_args_list)
            self.assertIn("Table 7", printed)
            self.assertIn("<ce:link>", printed)


class TestRC7CompletenessFalseMisses(unittest.TestCase):
    """RC-7: T ABLE N collapse, Hindawi tbl-0001 id label, supp sheet naming, Table 2-3 citation."""

    def test_format_table_label_collapses_spaced_small_caps(self):
        self.assertEqual(common.format_table_label("T ABLE 1"), "Table 1")
        self.assertEqual(common.format_table_label("T A B L E 2. Isotope data"), "Table 2")
        self.assertEqual(strategies._extract_normalized_table_label("T ABLE 3. Trace elements"), "Table 3")
        # Ensure normal words like "Stable" are untouched
        self.assertEqual(common.format_table_label("Stable isotopes"), "Stable isotopes")

    def test_hindawi_wrapper_dom_id_populates_table_label(self):
        html = """
        <html><body>
          <div class="article-table-content" id="tbl-0001">
            <div class="article-table-content-wrapper">
              <table class="table article-section__table">
                <thead><tr><th>Sample</th><th>Sb (ppm)</th></tr></thead>
                <tbody><tr><td>K-1</td><td>120</td></tr><tr><td>K-2</td><td>95</td></tr></tbody>
              </table>
            </div>
          </div>
          <div class="article-table-content" id="tbl-0002">
            <div class="article-table-content-wrapper">
              <table class="table article-section__table">
                <thead><tr><th>Sample</th><th>d34S</th></tr></thead>
                <tbody><tr><td>K-1</td><td>-2.3</td></tr><tr><td>K-2</td><td>-1.8</td></tr></tbody>
              </table>
            </div>
          </div>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(html, "all")
        self.assertEqual(len(dfs), 2)
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")
        self.assertEqual(dfs[1].attrs.get("label"), "Table 2")

    def test_supplementary_excel_sheet_named_table_1_does_not_collide_with_main_table_1(self):
        self.assertEqual(strategies._format_supp_excel_label("mmc2", "Table 1", True), "Supplementary Table 1")
        self.assertEqual(strategies._format_supp_excel_label("mmc2", "Table S4", True), "Supplementary Table S4")
        self.assertEqual(strategies._format_supp_excel_label("mmc1", "Longkou", True), "Supplementary Table mmc1_Longkou")
        self.assertEqual(
            strategies._format_supp_excel_label("126_2019_937_MOESM1_ESM", "EPMA", True),
            "Supplementary Table MOESM1_ESM_EPMA",
        )

        main_df = pd.DataFrame({"Sample": ["M1", "M2"], "Au": ["1.1", "2.2"]})
        main_df.attrs["label"] = "Table 1"
        supp_df = pd.DataFrame({"Sample": ["S1", "S2"], "Sb": ["5.1", "6.2"]})
        supp_df.attrs["label"] = "Supplementary Table mmc2_Table 1"
        supp_df.attrs["table_title"] = "Supplementary Table from sheet 'Table 1' of file 'mmc2.xlsx'"

        with tempfile.TemporaryDirectory() as tmp:
            ok = excel_export.save_tables_to_excel([main_df, supp_df], tmp, single_file=False)
            self.assertTrue(ok)
            files = sorted(f for f in os.listdir(tmp) if f.endswith(".xlsx"))
            self.assertIn("Table 1.xlsx", files)
            self.assertIn("Supplementary Table 1.xlsx", files)
            self.assertNotIn("Table 1_v2.xlsx", files)

    def test_table_validator_rejects_wrapped_parenthetical_range_citation(self):
        import fitz

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "range_cite.pdf")
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), "Table 1 Tonnages and grades of deposits")
            page.insert_text((72, 120), "Table 2 Microthermometric data of fluid inclusions")
            page.insert_text((72, 200), "Table 2–3; Fig. 5), precluding a source from the mafic rocks.")
            doc.save(pdf_path)
            doc.close()

            decls = table_validator.scan_pdf_table_declarations(pdf_path)
            found_labels = [d["label"] for caps in decls.values() for d in caps]
            self.assertIn("Table 1", found_labels)
            self.assertIn("Table 2", found_labels)
            self.assertNotIn("Table 2-3", found_labels)

    def test_e2e_online_completeness_maps_supp_and_mmc_and_counts_expected_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            dummy_pdf = os.path.join(tmp, "elsevier_2025.pdf")
            with open(dummy_pdf, "wb") as fh:
                fh.write(b"%PDF-1.4\n%%EOF\n")
            args = SimpleNamespace(
                output=tmp,
                online_only=True,
                pdf_only=False,
                single_file=False,
                headers=None,
                skip_supplementary=False,
                db_path=None,
                table_idx="all",
                firecrawl_key=None,
                cnki_strategy="auto",
                headed=False,
                user_data_dir=None,
                sequential=False,
                enable_llm=False,
            )
            df1 = pd.DataFrame({"Sample": ["A1", "A2"], "Sb": ["1.2", "3.4"]})
            df1.attrs["label"] = "Table 1"
            df2 = pd.DataFrame({"Sample": ["B1", "B2"], "Au": ["0.5", "0.9"]})
            df2.attrs["label"] = "Supplementary Table mmc2"
            df3 = pd.DataFrame({"Sample": ["C1", "C2"], "As": ["9.1", "8.2"]})
            df3.attrs["label"] = "Table S1"

            expected = {"Table 1", "Table S2", "Supplementary Table S1"}
            with patch.object(ezt, "scan_pdf_table_expected_labels", return_value=(expected, 3)), \
                 patch.object(ezt.online, "extract_tables_online", return_value=([df1, df2, df3], "Demo")), \
                 patch("builtins.print") as mock_print:
                ok = ezt.process_single_pdf(dummy_pdf, args)
                self.assertTrue(ok)
                printed = "\n".join(" ".join(str(a) for a in c.args) for c in mock_print.call_args_list)
                self.assertIn("覆盖全部 3 个预期表号", printed)
                self.assertNotIn("缺失预期表号", printed)


class TestRC8NegativeCacheAndAntiBot(unittest.TestCase):
    """RC-8: Full 200 article HTML not flagged as anti-bot; shorter TTL for 429 & transient bm-verify."""

    def test_full_200_article_with_gdpr_cookie_text_not_blocked(self):
        full_article = (
            '<html><head><title>Geosciences | Free Full-Text | Sb Deposit</title>'
            '<meta name="citation_doi" content="10.3390/geosciences15120462"/>'
            '<meta name="citation_title" content="Sb Deposit"/>'
            '</head><body><div>Your privacy, your choice</div>'
            '<div>If there was a problem loading data...</div></body></html>'
        )
        self.assertFalse(common.is_anti_bot_blocked("", full_article))
        self.assertEqual(negative_cache.should_negative_cache(status_code=200, html_snippet=full_article), (False, ""))

    def test_transient_bm_verify_and_429_have_short_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "nc.sqlite")
            ok = negative_cache.record_negative_cache(
                "direct_http",
                url="https://www.mdpi.com/2076-3263/15/12/462",
                status_code=200,
                html_snippet='<meta http-equiv="refresh" content="5; URL=\'/2076-3263/15/12/462?bm-verify=AAQ\'" />',
                db_path=db_path,
                now=1000.0,
            )
            self.assertTrue(ok)
            hit = negative_cache.is_negatively_cached(
                "direct_http", url="https://www.mdpi.com/2076-3263/15/12/999", db_path=db_path, now=1050.0
            )
            self.assertIsNotNone(hit)
            self.assertLessEqual(hit["expires_at"] - hit["created_at"], 120.0)


class TestRC9RC10RC12MiscFixes(unittest.TestCase):
    """RC-9 (--online-only skips PDF preprocess), RC-10 (subpage retry), RC-12 (winner metadata filter)."""

    def test_online_only_skips_preprocess_local_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            dummy_pdf = os.path.join(tmp, "paper_2024.pdf")
            with open(dummy_pdf, "wb") as fh:
                fh.write(b"%PDF-1.4\n%%EOF\n")
            args = SimpleNamespace(
                output=tmp,
                online_only=True,
                pdf_only=False,
                single_file=False,
                headers=None,
                skip_supplementary=False,
                db_path=None,
                table_idx="all",
                firecrawl_key=None,
                cnki_strategy="auto",
                headed=False,
                user_data_dir=None,
                sequential=False,
                enable_llm=False,
            )
            with patch.object(ezt, "_preprocess_local_pdf") as mock_pre, \
                 patch.object(ezt.online, "extract_tables_online", return_value=([], None)):
                ezt.process_single_pdf(dummy_pdf, args)
                mock_pre.assert_not_called()

    def test_subpage_follow_retries_transient_failure(self):
        html = '<a href="/article/10.1007/s11631-023-00653-3/tables/1">Table 1</a>'
        sub_html = """
        <header class="u-mb-16"><h1>Table 1 Element data</h1></header>
        <div class="c-article-table-container">
          <table><thead><tr><th>Sample</th><th>Sb</th></tr></thead><tbody><tr><td>S1</td><td>12.5</td></tr></tbody></table>
        </div>
        """
        calls = {"n": 0}

        def _flaky_fetch(u):
            calls["n"] += 1
            return "" if calls["n"] == 1 else sub_html

        dfs = strategies._follow_table_subpages(
            "https://link.springer.com/article/10.1007/s11631-023-00653-3",
            html,
            _flaky_fetch,
            table_idx="all",
        )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(dfs), 1)

    def test_race_candidates_rejects_metadata_only_junk_winner(self):
        meta_df = pd.DataFrame(
            {
                "Author": ["Pacák et al.", "Smith et al."],
                "Affiliation": ["Charles University", "Czech Geological Survey"],
                "E-mail": ["a@example.edu", "b@example.edu"],
            }
        )
        good_df = pd.DataFrame(
            {
                "Sample": ["Q-1", "Q-2", "Q-3"],
                "Sb_ppm": ["15.2", "22.4", "18.9"],
                "As_ppm": ["45.1", "39.0", "51.2"],
            }
        )

        def _fast_junk():
            return [meta_df]

        def _slow_good():
            time.sleep(0.15)
            return [good_df]

        dfs, winner = graph._race_candidates([("Junk", _fast_junk), ("Good", _slow_good)])
        self.assertEqual(winner, "Good")
        self.assertEqual(len(dfs), 1)
        self.assertIn("Sb_ppm", dfs[0].columns)


class TestReviewRound2Fixes(unittest.TestCase):
    """Regression tests for bugs identified and fixed during critical review."""

    def test_partial_meta_with_url_without_doi_still_resolves_doi(self):
        with tempfile.TemporaryDirectory() as td:
            fake_pdf = os.path.join(td, "sample.pdf")
            with open(fake_pdf, "wb") as f:
                f.write(b"%PDF-1.4\n")

            resolve_called = {"n": 0}
            captured_state = {}

            def _fake_resolve(self_node, st):
                resolve_called["n"] += 1
                st["doi"] = "10.1016/j.oregeorev.2020.103000"
                st["url"] = st.get("url") or "https://www.sciencedirect.com/science/article/pii/S016913682030001X"
                st["is_cnki"] = False
                return st, None

            def _fake_race(st, cancel_event=None):
                captured_state.update(st)
                return [], ["mock"]

            with mock.patch.object(graph.ResolveMetadataNode, "execute", _fake_resolve), \
                 mock.patch.object(graph, "_race_extract", _fake_race):
                graph.extract_tables_online(
                    pdf_path=fake_pdf,
                    pre_resolved={
                        "url": "https://www.sciencedirect.com/science/article/pii/S016913682030001X",
                        "doi": None,
                        "title": "Sample Paper",
                        "item_type": "journalarticle",
                        "_partial_meta": True,
                    },
                    race=True,
                )
            self.assertEqual(resolve_called["n"], 1)
            self.assertEqual(captured_state.get("doi"), "10.1016/j.oregeorev.2020.103000")

            # Also ensure non-http URL in pre_resolved is rejected and triggers ResolveMetadataNode
            resolve_called["n"] = 0
            with mock.patch.object(graph.ResolveMetadataNode, "execute", _fake_resolve), \
                 mock.patch.object(graph, "_race_extract", _fake_race):
                graph.extract_tables_online(
                    pdf_path=fake_pdf,
                    pre_resolved={"url": "not-a-valid-url", "doi": None},
                    race=True,
                )
            self.assertEqual(resolve_called["n"], 1)

    def test_browser_extraction_lock_honors_cancel_event_and_playwright_node_handles_timeout(self):
        lock_name = f"cancel_test_{os.getpid()}_{int(time.time() * 1000)}"
        held = threading.Event()
        release = threading.Event()

        def _holder():
            with common.browser_extraction_lock(name=lock_name, timeout=10.0):
                held.set()
                release.wait(5.0)

        t = threading.Thread(target=_holder, daemon=True)
        t.start()
        self.assertTrue(held.wait(3.0))
        try:
            cancel_ev = threading.Event()
            threading.Timer(0.15, cancel_ev.set).start()
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                with common.browser_extraction_lock(name=lock_name, timeout=10.0, cancel_event=cancel_ev):
                    pass
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            release.set()
            t.join(timeout=3.0)

        # PlaywrightGeneralNode must catch TimeoutError gracefully instead of crashing
        @contextlib.contextmanager
        def _timeout_guard(*args, **kwargs):
            raise TimeoutError("simulated lock timeout")
            yield False

        with mock.patch.object(graph, "browser_task_concurrency_guard", _timeout_guard), \
             mock.patch.object(graph, "load_config", return_value={}):
            st, nxt = graph.PlaywrightGeneralNode(fallback_node="FallbackNode").execute(
                {"url": "https://example.org/paper", "table_idx": "all"}
            )
            self.assertEqual(nxt, "FallbackNode")
            self.assertEqual(st.get("dfs", []), [])

    def test_race_extract_budget_timer_fires_even_with_caller_cancel_event(self):
        caller_cancel = threading.Event()
        saw_cancelled = {"flag": False}

        def _slow_direct(url, table_idx="all", cancel_event=None, doi=None, meta_out=None):
            for _ in range(30):
                if cancel_event is not None and cancel_event.is_set():
                    saw_cancelled["flag"] = True
                    return []
                time.sleep(0.05)
            return []

        with mock.patch.dict(os.environ, {"ONLINE_EXTRACTION_BUDGET_S": "0.25"}), \
             mock.patch.object(graph, "load_config", return_value={"ARXIV_HTML_ENABLED": False, "NCBI_PMC_ENABLED": False}), \
             mock.patch.object(graph, "extract_tables_via_direct_http", side_effect=_slow_direct), \
             mock.patch.object(graph, "extract_tables_via_unpaywall", return_value=[]):
            t0 = time.monotonic()
            dfs, _hist = graph._race_extract(
                {"url": "https://example.org/slow", "doi": None, "table_idx": "all"},
                cancel_event=caller_cancel,
            )
            elapsed = time.monotonic() - t0

        self.assertEqual(dfs, [])
        self.assertTrue(saw_cancelled["flag"], "Budget timer should fire even when caller passes cancel_event")
        self.assertFalse(caller_cancel.is_set(), "Caller's cancel_event must NOT be mutated by budget timer")
        self.assertLess(elapsed, 2.0)

    def test_elsevier_xml_not_preempted_by_ncbipmc_mid_supp_download(self):
        main_dfs = [pd.DataFrame({"Sample": ["A", "B"], "Sb": ["1", "2"]}) for _ in range(4)]
        supp_df = pd.DataFrame({"Sample": ["S1", "S2"], "Au": ["3", "4"]})
        supp_df.attrs["label"] = "Supplementary Table mmc1"

        def _fast_pmc(*args, **kwargs):
            return list(main_dfs)

        def _slightly_slower_elsevier(*args, **kwargs):
            time.sleep(0.25)
            return list(main_dfs) + [supp_df]

        with mock.patch.object(graph, "load_config", return_value={"ARXIV_HTML_ENABLED": False, "NCBI_PMC_ENABLED": True}), \
             mock.patch.object(graph, "extract_tables_via_ncbi_pmc", side_effect=_fast_pmc), \
             mock.patch.object(graph, "extract_tables_via_elsevier_api", side_effect=_slightly_slower_elsevier), \
             mock.patch.object(graph, "extract_tables_via_direct_http", return_value=[]), \
             mock.patch.object(graph, "extract_tables_via_unpaywall", return_value=[]), \
             mock.patch.object(graph, "is_negatively_cached", return_value=None):
            dfs, history = graph._race_extract(
                {
                    "url": "https://www.sciencedirect.com/science/article/pii/S2589004220307677",
                    "doi": "10.1016/j.isci.2020.101575",
                    "api_key": "fake-key",
                    "table_idx": "all",
                }
            )
        self.assertEqual(len(dfs), 5)
        self.assertIn("general-tier1:ElsevierXML", history)

    def test_browser_context_supp_download_parses_mdpi_s1_zip_without_double_prefix(self):
        import zipfile
        # Build an in-memory .xlsx inside a .zip mimicking MDPI geosciences-15-00462-s001.zip
        xlsx_buf = io.BytesIO()
        pd.DataFrame({"Sample": ["GTS-1", "GTS-2"], "Sb_wt": ["71.2", "70.8"]}).to_excel(
            xlsx_buf, index=False, sheet_name="Sheet1"
        )
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("Supplementary Table 1_Stibnite major element geochemistry.xlsx", xlsx_buf.getvalue())
        zip_bytes = zip_buf.getvalue()

        html = '<html><body><a href="/article/10.3390/geosciences15120462/s1">Supplementary Materials</a></body></html>'

        class _FakeResp:
            status = 200
            headers = {
                "content-type": "application/octet-stream",
                "content-disposition": "attachment; filename*=UTF-8''geosciences-15-00462-s001.zip",
            }
            def body(self):
                return zip_bytes

        class _FakeReqApi:
            def __init__(self):
                self.urls = []
            def get(self, u, headers=None, timeout=None):
                self.urls.append(u)
                return _FakeResp()

        req_api = _FakeReqApi()
        dfs = strategies._download_supp_via_browser_context(
            req_api,
            "https://www.mdpi.com/2076-3263/15/12/462",
            html,
            max_files=4,
            max_timeout=10.0,
        )
        self.assertEqual(len(req_api.urls), 1)
        self.assertTrue(req_api.urls[0].endswith("/s1"))
        self.assertEqual(len(dfs), 1)
        lbl = dfs[0].attrs.get("label", "")
        self.assertTrue(lbl.startswith("Supplementary Table 1"))
        self.assertNotIn("Supplementary Table Supplementary Table", lbl)

    def test_append_builtin_supp_passes_prefetched_strong_candidates(self):
        captured = {}
        fake_cands = [
            {"url": "https://example.org/weak.xlsx", "score": 90, "filename": "weak.xlsx", "marker": "weak"},
            {"url": "https://example.org/MOESM1_ESM.xlsx", "score": 80, "filename": "MOESM1_ESM.xlsx", "marker": "strong"},
        ]
        main_df = pd.DataFrame({"Sample": ["A"], "Val": ["1"]})
        supp_df = pd.DataFrame({"Sample": ["S1"], "Val": ["2"]})

        def _fake_extract(url, **kwargs):
            captured["prefetched"] = kwargs.get("prefetched_candidates")
            return [supp_df]

        with mock.patch.object(strategies, "_discover_supplementary_links", return_value=fake_cands), \
             mock.patch.object(strategies, "extract_builtin_supplementary_tables", side_effect=_fake_extract):
            out = strategies._append_builtin_supp_if_any([main_df], "https://example.org/art", "<html></html>")

        self.assertEqual(len(out), 2)
        self.assertEqual(len(captured["prefetched"]), 1)
        self.assertEqual(captured["prefetched"][0]["url"], "https://example.org/MOESM1_ESM.xlsx")


class TestRun2ConcurrencyAndCNKILazyFixes(unittest.TestCase):
    """Regression tests for bugs surfaced by the 290-paper 6-worker Run 2 stress test."""

    def test_append_builtin_supp_caps_timeout_when_main_dfs_exist(self):
        captured = {}
        fake_cands = [
            {"url": "https://example.org/MOESM1_ESM.xlsx", "score": 115, "filename": "MOESM1_ESM.xlsx", "marker": "strong"},
        ]
        main_df = pd.DataFrame({"Sample": ["A"], "Val": ["1"]})

        def _fake_extract(url, **kwargs):
            captured["max_timeout"] = kwargs.get("max_timeout")
            return []

        with mock.patch.object(strategies, "_discover_supplementary_links", return_value=fake_cands), \
             mock.patch.object(strategies, "extract_builtin_supplementary_tables", side_effect=_fake_extract):
            out = strategies._append_builtin_supp_if_any([main_df], "https://example.org/art", "<html></html>", max_timeout=20)
        self.assertEqual(len(out), 1)
        self.assertLessEqual(captured["max_timeout"], 6.0)

        # Tier-1 fast HTTP callers can explicitly opt into a larger main_present_cap (e.g. 12s for multi-ESM Springer)
        with mock.patch.object(strategies, "_discover_supplementary_links", return_value=fake_cands), \
             mock.patch.object(strategies, "extract_builtin_supplementary_tables", side_effect=_fake_extract):
            out2 = strategies._append_builtin_supp_if_any(
                [main_df], "https://example.org/art", "<html></html>", max_timeout=12, main_present_cap=12.0
            )
        self.assertEqual(len(out2), 1)
        self.assertEqual(captured["max_timeout"], 12.0)

    def test_builtin_supp_streaming_download_aborts_on_cancel_event(self):
        cancel = threading.Event()
        closed = []

        class _SlowStreamResp:
            status_code = 200
            headers = {"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
            def iter_content(self, chunk_size=65536):
                yield b"chunk1"
                cancel.set()
                yield b"chunk2"
            def close(self):
                closed.append(True)

        fake_cands = [
            {"url": "https://example.org/MOESM1_ESM.xlsx", "score": 115, "filename": "MOESM1_ESM.xlsx", "marker": "strong"},
        ]
        with mock.patch.object(strategies.requests, "get", return_value=_SlowStreamResp()), \
             mock.patch.object(strategies, "_collect_domain_cookies_for_scrapling", return_value=[]):
            res = strategies.extract_builtin_supplementary_tables(
                "https://example.org/art",
                prefetched_candidates=fake_cands,
                cancel_event=cancel,
                max_timeout=12,
            )
        self.assertEqual(res, [])
        self.assertTrue(closed, "Slow streamed supplementary response must be closed on abort")

    def test_scrapling_does_not_duplicate_builtin_supp_after_browser_supp_attempt(self):
        html_page = "<html><body><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
        called_builtin = []

        def _fake_fetch(url, extra_kwargs=None):
            action = (extra_kwargs or {}).get("page_action")
            if callable(action):
                class _FakePage:
                    url = "https://example.org/art"
                    class context:
                        request = object()
                    def content(self):
                        return html_page
                action(_FakePage())
            return html_page

        with mock.patch.object(strategies, "_scrapling_fetch_html", side_effect=_fake_fetch), \
             mock.patch.object(strategies, "_scrapling_page_action", return_value=None), \
             mock.patch.object(strategies, "_download_supp_via_browser_context", return_value=[]), \
             mock.patch.object(strategies, "_append_builtin_supp_if_any", side_effect=lambda dfs, *a, **kw: (called_builtin.append(True), dfs)[1]):
            dfs = strategies.extract_tables_via_scrapling("https://example.org/art", table_idx="all")

        self.assertEqual(len(dfs), 1)
        self.assertEqual(called_builtin, [], "Should not re-run _append_builtin_supp_if_any after browser supp attempted")

    def test_crawl4ai_standalone_does_not_acquire_global_browser_lock(self):
        import inspect
        src = inspect.getsource(strategies.extract_tables_via_crawl4ai)
        self.assertNotIn("browser_extraction_lock(", src)

    def test_cnki_wait_for_lazy_tables_polls_until_table_count_stabilizes(self):
        from online import cnki_html_extractor as cnki_ext

        counts = iter([1, 1, 3, 3, 3, 3, 3])
        scrolled = []

        class _FakePage:
            def wait_for_load_state(self, state, timeout=None):
                pass
            def evaluate(self, js):
                scrolled.append(True)
            def query_selector_all(self, sel):
                try:
                    n = next(counts)
                except StopIteration:
                    n = 3
                return [object()] * n
            def wait_for_timeout(self, ms):
                time.sleep(0.05)

        p = _FakePage()
        cnki_ext._wait_for_cnki_lazy_tables(p, max_wait_s=2.0)
        self.assertTrue(scrolled)
        self.assertEqual(len(p.query_selector_all("table")), 3)


class TestRun4FollowupFixes(unittest.TestCase):
    """Regression tests for bugs surfaced by Run 4 full-corpus verification."""

    def test_discover_supplementary_links_skips_same_page_fragment_anchors(self):
        base = "https://link.springer.com/article/10.1007/s00126-019-00937-w"
        html = (
            '<html><body>'
            '<a href="https://link.springer.com/article/10.1007/s00126-019-00937-w#MOESM1">ESM 1</a>'
            '<a href="/article/10.1007/s00126-019-00937-w#MOESM2">ESM 2</a>'
            '<a href="https://static-content.springer.com/esm/art%3A10.1007%2Fs00126-019-00937-w/MediaObjects/126_2019_937_MOESM1_ESM.xlsx#top">Download ESM1</a>'
            '</body></html>'
        )
        cands = strategies._discover_supplementary_links(base, html, max_links=6)
        self.assertEqual(len(cands), 1)
        self.assertTrue(cands[0]["url"].endswith("126_2019_937_MOESM1_ESM.xlsx"))
        self.assertNotIn("#", cands[0]["url"])

    def test_firecrawl_uses_metadata_url_for_relative_supplementary_links(self):
        captured = {}
        fake_cached = {
            "html": '<html><body><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>'
                    '<a href="/article/10.3390/min9020087/s1">Supplementary Materials</a></body></html>',
            "metadata": {"url": "https://www.mdpi.com/2075-163X/9/2/87", "sourceURL": "http://dx.doi.org/10.3390/min9020087"},
        }

        def _fake_append(dfs, base_u, html_text, **kwargs):
            captured["base_u"] = base_u
            return dfs

        with mock.patch.object(strategies, "is_firecrawl_disabled", return_value=False), \
             mock.patch.object(strategies, "load_config", return_value={"FIRECRAWL_API_KEY": "fc-test"}), \
             mock.patch.object(strategies, "_firecrawl_read_cache", return_value=fake_cached), \
             mock.patch.object(strategies, "_append_builtin_supp_if_any", side_effect=_fake_append):
            strategies._FIRECRAWL_MEMO.clear()
            dfs = strategies.extract_tables_via_firecrawl(
                "http://dx.doi.org/10.3390/min9020087", api_key="fc-test", table_idx="all", is_oa=True
            )
        self.assertEqual(len(dfs), 1)
        self.assertEqual(captured.get("base_u"), "https://www.mdpi.com/2075-163X/9/2/87")

    def test_unpaywall_follows_springer_table_subpages_before_supp(self):
        article_html = (
            '<html><body>'
            '<a href="/article/10.1007/s00126-023-01222-7/tables/1">Table 1</a>'
            '</body></html>'
        )
        subpage_html = (
            '<html><body><p>Table 1. Isotope values</p>'
            '<table><tr><th>Sample</th><th>d123Sb</th></tr><tr><td>S1</td><td>0.12</td></tr></table>'
            '</body></html>'
        )

        class _Resp:
            def __init__(self, status_code, text="", json_data=None, url="", content=b"", headers=None):
                self.status_code = status_code
                self.text = text
                self._json = json_data
                self.url = url
                self.content = content or text.encode("utf-8")
                self.headers = headers or {"Content-Type": "text/html"}
            def json(self):
                return self._json

        def _fake_get(u, **kwargs):
            if "api.unpaywall.org" in u:
                return _Resp(200, json_data={"best_oa_location": {"url_for_landing_page": "https://doi.org/10.1007/s00126-023-01222-7"}})
            if "/tables/1" in u:
                return _Resp(200, text=subpage_html, url=u)
            return _Resp(200, text=article_html, url="https://link.springer.com/article/10.1007/s00126-023-01222-7")

        with mock.patch.object(strategies.requests, "get", side_effect=_fake_get), \
             mock.patch.object(strategies, "_append_builtin_supp_if_any", side_effect=lambda dfs, *a, **kw: dfs):
            dfs = strategies.extract_tables_via_unpaywall("10.1007/s00126-023-01222-7", table_idx="all")
        self.assertEqual(len(dfs), 1)
        self.assertEqual(list(dfs[0].columns), ["Sample", "d123Sb"])

    def test_elsevier_tier1_grace_allows_20s_window(self):
        import inspect
        src = inspect.getsource(graph._race_extract_body)
        self.assertIn("_tier1_grace = 20.0 if _is_els_target else 6.0", src)

    def test_normalize_publisher_url_upgrades_bare_wiley_doi_to_full(self):
        self.assertEqual(
            graph._normalize_publisher_url("https://onlinelibrary.wiley.com/doi/10.1111/1755-6724.13549"),
            "https://onlinelibrary.wiley.com/doi/full/10.1111/1755-6724.13549",
        )
        # Already /full/ or /pdf/ or non-Atypon ACS bare /doi/ stays intact
        self.assertEqual(
            graph._normalize_publisher_url("https://onlinelibrary.wiley.com/doi/full/10.1111/1755-6724.13549"),
            "https://onlinelibrary.wiley.com/doi/full/10.1111/1755-6724.13549",
        )
        self.assertEqual(
            graph._normalize_publisher_url("https://pubs.acs.org/doi/10.1021/acs.est.4c01234"),
            "https://pubs.acs.org/doi/10.1021/acs.est.4c01234",
        )

    def test_parse_tables_from_html_filters_mathjax_equation_layout_tables(self):
        html = (
            '<html><body>'
            '<table><tr><td>\\mathit{\\delta}^{123}\\mathrm{Sb} = (\\delta + 1000) \\times (1 - f)</td><td>(1)</td></tr></table>'
            '<table><tr><td>fn = 1 - (Dsample / Dtotal)</td><td>(2)</td></tr></table>'
            '</body></html>'
        )
        dfs = strategies.parse_tables_from_html(html, "all")
        self.assertEqual(dfs, [])

    def test_builtin_supp_escalates_akamai_blocked_mdpi_s1_to_browser_fallback(self):
        fake_supp_df = pd.DataFrame({"Sample": ["S1"], "Sb": ["12.3"]})
        fake_supp_df.attrs["label"] = "Supplementary Table S1"
        called = {}

        class _BlockedResp:
            status_code = 403
            headers = {"Content-Type": "text/html"}
            content = b"<HTML><TITLE>Access Denied</TITLE></HTML>"
            def close(self):
                pass

        def _fake_browser_fb(base_u, items, **kwargs):
            called["base_u"] = base_u
            called["urls"] = [it["url"] for it in items]
            return [fake_supp_df]

        cands = [
            {"url": "https://www.mdpi.com/2075-163X/9/2/87/s1", "score": 80, "filename": "s1", "marker": "strong"},
        ]
        with mock.patch.object(strategies.requests, "get", return_value=_BlockedResp()), \
             mock.patch.object(strategies, "requests_impersonate", None), \
             mock.patch.object(strategies, "_collect_domain_cookies_for_scrapling", return_value=[]), \
             mock.patch.object(strategies, "_download_blocked_supp_via_browser", side_effect=_fake_browser_fb):
            out = strategies.extract_builtin_supplementary_tables(
                "https://www.mdpi.com/2075-163X/9/2/87",
                prefetched_candidates=cands,
                max_timeout=12,
            )
        self.assertEqual(len(out), 1)
        self.assertEqual(called["urls"], ["https://www.mdpi.com/2075-163X/9/2/87/s1"])


if __name__ == "__main__":
    unittest.main()
