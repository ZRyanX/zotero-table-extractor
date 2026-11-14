import os
import sys
import unittest
from unittest.mock import patch

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import common
import system_detector
from online import strategies, graph


class TestPublisherDomAuditFixes(unittest.TestCase):
    def test_sciencedirect_wrapper_caption_footnote(self):
        html = """
        <html><body>
          <div id="t0005" class="tables frame-topbot">
            <span class="captions text-s">Table 1. Trace elements in K-feldspar from drill hole ZK802.</span>
            <div class="groups">
              <table>
                <thead><tr><th>Sample</th><th>Cu (ppm)</th><th>Au (ppb)</th></tr></thead>
                <tbody>
                  <tr><td>ZK-1</td><td>125.4</td><td>18.2</td></tr>
                  <tr><td>ZK-2</td><td>98.1</td><td>11.5</td></tr>
                </tbody>
              </table>
            </div>
            <dl class="footnotes"><dd>BDL = below detection limit.</dd></dl>
          </div>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(html)
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].shape, (2, 3))
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")
        self.assertIn("Trace elements in K-feldspar", dfs[0].attrs.get("table_title", ""))
        self.assertIn("BDL = below detection limit", " ".join(dfs[0].attrs.get("footnotes", [])))

    def test_springer_nature_subpage_container_and_link_follow(self):
        article_html = """
        <html><body>
          <div id="table-1" class="c-article-table">
            <a class="c-article__pill-button" href="/article/10.1007/s11053-025-10568-w/tables/1">Full size table</a>
          </div>
        </body></html>
        """
        subpage_html = """
        <html><body>
          <div class="c-article-table-container">
            <header class="u-mb-16"><h2>Table 1 Summary of geochemical training splits</h2></header>
            <div class="c-article-table-border c-table-scroll-wrapper">
              <div class="c-table-scroll-wrapper__content">
                <table>
                  <thead><tr><th>Element</th><th>Train</th><th>Test</th></tr></thead>
                  <tbody><tr><td>Sb</td><td>120</td><td>30</td></tr></tbody>
                </table>
              </div>
            </div>
            <div class="c-article-table-footer"><p>All concentrations in ppm.</p></div>
          </div>
        </body></html>
        """
        dfs = strategies._follow_table_subpages(
            "https://link.springer.com/article/10.1007/s11053-025-10568-w",
            article_html,
            lambda _u: subpage_html,
        )
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")
        self.assertIn("Summary of geochemical training splits", dfs[0].attrs.get("table_title", ""))
        self.assertIn("All concentrations in ppm", " ".join(dfs[0].attrs.get("footnotes", [])))

    def test_mdpi_hidden_table_show_and_disclaimer_filtered(self):
        html = """
        <html><body>
          <div id="minerals-12-00123-t001" class="html-table-wrap">
            <img src="thumb.png"/>
          </div>
          <div id="table_body_display_minerals-12-00123-t001" class="html-table_show mfp-hide">
            <div class="html-caption">Table 1. Microprobe compositions of rutile grains.</div>
            <table>
              <thead><tr><th>Grain</th><th>TiO2 (wt%)</th><th>W (ppm)</th></tr></thead>
              <tbody><tr><td>R-1</td><td>98.4</td><td>450</td></tr></tbody>
            </table>
            <div class="html-table_foot">Note: Fe as total FeO.</div>
          </div>
          <section class="html-fn_group">
            <table>
              <tbody><tr><td>Disclaimer/Publisher's Note</td><td>MDPI stays neutral with regard to jurisdictional claims.</td></tr></tbody>
            </table>
          </section>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(html)
        self.assertEqual(len(dfs), 1)
        self.assertIn("Microprobe compositions", dfs[0].attrs.get("table_title", ""))
        self.assertEqual(dfs[0].attrs.get("label"), "Table 1")
        self.assertIn("Fe as total FeO", " ".join(dfs[0].attrs.get("footnotes", [])))

    def test_wiley_multi_table_split_and_support_info_skip(self):
        html = """
        <html><body>
          <div id="rge12279-tbl-0003" class="article-table-content">
            <header class="article-table-caption">Table 3. Pyrite LA-ICP-MS trace elements</header>
            <div class="article-table-content-wrapper">
              <table class="article-section__table">
                <thead><tr><th>Sample</th><th>As</th><th>Au</th></tr></thead>
                <tbody><tr><td>Py1</td><td>210</td><td>1.2</td></tr></tbody>
              </table>
              <table class="article-section__table">
                <thead><tr><th>Sample</th><th>Sb</th><th>Ag</th></tr></thead>
                <tbody><tr><td>Py2</td><td>85</td><td>0.8</td></tr></tbody>
              </table>
            </div>
            <div class="article-section__table-footnotes">b.d.l. = below detection limit.</div>
          </div>
          <section class="article-section__supporting">
            <div class="support-info__table-wrapper">
              <table class="support-info__table">
                <thead><tr><th>Filename</th><th>Description</th></tr></thead>
                <tbody><tr><td>TableS1.xlsx</td><td>Supplementary data</td></tr></tbody>
              </table>
            </div>
          </section>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(html)
        self.assertEqual(len(dfs), 2)
        self.assertEqual(list(dfs[0].columns), ["Sample", "As", "Au"])
        self.assertEqual(list(dfs[1].columns), ["Sample", "Sb", "Ag"])
        self.assertIn("Pyrite LA-ICP-MS", dfs[0].attrs.get("table_title", ""))
        self.assertIn("b.d.l.", " ".join(dfs[1].attrs.get("footnotes", [])))

    def test_gsw_silverchair_dedups_table_modal(self):
        html = """
        <html><body>
          <section class="figure-table-wrapper">
            <div class="table-wrap">
              <div class="table-wrap-title">Table 1. Deposit grades</div>
              <div class="table-overflow">
                <table>
                  <thead><tr><th>Deposit</th><th>Grade (g/t)</th></tr></thead>
                  <tbody><tr><td>Zaozigou</td><td>3.45</td></tr></tbody>
                </table>
              </div>
              <div class="table-modal reveal-modal">
                <table>
                  <thead><tr><th>Deposit</th><th>Grade (g/t)</th></tr></thead>
                  <tbody><tr><td>Zaozigou</td><td>3.45</td></tr></tbody>
                </table>
              </div>
              <div class="table-wrap-foot">Data compiled from 2024 survey.</div>
            </div>
          </section>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(html)
        self.assertEqual(len(dfs), 1)
        self.assertIn("Deposit grades", dfs[0].attrs.get("table_title", ""))

    def test_frontiers_aps_iop_and_annual_reviews_callout(self):
        html = """
        <html><body>
          <div id="T1" class="ArticleTable">
            <div class="ArticleTable__header">TABLE 1</div>
            <div class="ArticleTable__content">
              <table><thead><tr><th>Unit</th><th>Lithology</th></tr></thead><tbody><tr><td>Q</td><td>Sand</td></tr></tbody></table>
            </div>
            <div class="ArticleTable__description">Stratigraphic interpretation flags.</div>
            <div class="ArticleTable__foot">Landsat-8 OLI bands used.</div>
          </div>
          <div id="t1" class="article-fulltext-table-wrap">
            <header class="article-fulltext-label-header">TABLE I.</header>
            <p class="article-fulltext-paragraph">Sample pressure and laser heating range.</p>
            <table class="article-fulltext-table">
              <thead><tr><th>Spot</th><th>P (GPa)</th></tr></thead>
              <tbody><tr><td>S1</td><td>9.18</td></tr></tbody>
            </table>
          </div>
          <div id="mlstad3c0ft1" class="boxout boxout-bdr-grey">
            <p class="table-caption">Table 1. Classifier hyper-parameters.</p>
            <table><thead><tr><th>Model</th><th>F1</th></tr></thead><tbody><tr><td>RF</td><td>0.91</td></tr></tbody></table>
          </div>
          <table id="b1" class="body"><tbody><tr><td>Callout summary box text</td></tr></tbody></table>
        </body></html>
        """
        dfs = strategies.parse_tables_from_html(html)
        self.assertEqual(len(dfs), 3)
        self.assertEqual(dfs[0].attrs.get("table_title"), "TABLE 1. Stratigraphic interpretation flags.")
        self.assertIsInstance(dfs[0].attrs.get("footnotes"), list)
        self.assertIn("Landsat-8 OLI", " ".join(dfs[0].attrs.get("footnotes", [])))
        self.assertIn("TABLE I. Sample pressure", dfs[1].attrs.get("table_title", ""))
        self.assertEqual(dfs[2].attrs.get("label"), "Table 1")

    def test_copernicus_jats_xml_follow(self):
        landing_html = '<html><body><a href="/articles/9/373/2018/se-9-373-2018.xml">XML</a></body></html>'
        jats_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <article><body>
          <table-wrap id="T1">
            <label>Table 1</label>
            <caption><p>Statistical correlation coefficients.</p></caption>
            <table>
              <thead><tr><th>Band</th><th>R2</th></tr></thead>
              <tbody><tr><td>B4/B2</td><td>0.84</td></tr></tbody>
            </table>
          </table-wrap>
        </body></article>
        """
        dfs = strategies._follow_table_subpages(
            "https://se.copernicus.org/articles/9/373/2018/",
            landing_html,
            lambda _u: jats_xml,
        )
        self.assertEqual(len(dfs), 1)
        self.assertEqual(dfs[0].shape, (1, 2))

    def test_anti_bot_detection_markers(self):
        self.assertTrue(common.is_anti_bot_blocked("请稍候…", ""))
        self.assertTrue(common.is_anti_bot_blocked("Radware Bot Manager Captcha", ""))
        self.assertTrue(common.is_anti_bot_blocked("ScienceDirect", ""))
        self.assertTrue(common.is_anti_bot_blocked("安全验证", ""))
        self.assertTrue(common.is_anti_bot_blocked("", '<meta http-equiv="refresh" content="5; URL=\'/2075-163X/16/4/363?bm-verify=AAQ\'" />'))
        self.assertFalse(common.is_anti_bot_blocked("Ore Geology Reviews - ScienceDirect", "<html><body>Article content</body></html>"))

    def test_excel_export_string_footnote_not_split_by_char(self):
        import tempfile, openpyxl, pandas as pd
        from scripts import excel_export
        df = pd.DataFrame([{"A": 1, "B": 2}])
        with tempfile.TemporaryDirectory() as tmpdir:
            xlsx_path = os.path.join(tmpdir, "out.xlsx")
            with pd.ExcelWriter(xlsx_path) as writer:
                df.to_excel(writer, sheet_name="Table 1", index=False)
            excel_export.autofit_excel_columns(xlsx_path, sheet_footnotes={"Table 1": "p < 0.05."})
            wb = openpyxl.load_workbook(xlsx_path, data_only=True)
            ws = wb["Table 1"]
            vals = [ws.cell(r, 1).value for r in range(1, ws.max_row + 1)]
            self.assertEqual(vals[-1], "p < 0.05.")
            self.assertEqual(len(vals), 3)
            wb.close()

    def test_scrapling_retries_headed_when_headless_blocked(self):
        calls = []
        blocked_html = "<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>"
        good_html = """
        <html><body>
          <div class="html-table_show">
            <div class="html-caption">Table 1. Data</div>
            <table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>
          </div>
        </body></html>
        """

        def fake_fetch(url, extra_kwargs=None):
            calls.append(extra_kwargs)
            return blocked_html if (extra_kwargs or {}).get("headless", True) else good_html

        with patch.object(strategies, "_scrapling_fetch_html", side_effect=fake_fetch):
            dfs = strategies.extract_tables_via_scrapling("https://www.mdpi.com/2075-163X/12/1/10")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["headless"])
        self.assertFalse(calls[1]["headless"])
        self.assertEqual(len(dfs), 1)

    def test_cross_platform_foreign_path_guard(self):
        if system_detector.IS_WINDOWS:
            adapted = system_detector.adapt_path("/Volumes/ExFat/AIHub/paper_tables")
            self.assertFalse(adapted.startswith("/Volumes/"))
            self.assertTrue(system_detector._is_foreign_nonexistent_path("/Users/ryanx/Zotero/zotero.sqlite"))

    def test_atypon_abs_to_full_normalization_in_resolve(self):
        node = graph.ResolveMetadataNode("ElsevierXMLNode")
        with patch.object(graph, "load_config", return_value={}):
            state = {"url": "https://www.tandfonline.com/doi/abs/10.1080/08120099.2016.1225601", "doi": "10.1080/08120099.2016.1225601"}
            out_state, _ = node.execute(state)
            self.assertEqual(out_state["url"], "https://www.tandfonline.com/doi/full/10.1080/08120099.2016.1225601")


if __name__ == "__main__":
    unittest.main()
