"""Independent regressions for omission, lexical fidelity and failure reporting."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pandas as pd
import fitz
import requests
from openpyxl import load_workbook
import common
import ocr_client as oc
import table_postprocess as tp
import table_validator as tv
import pdf_table_extractor as pe
import excel_export as ee
import extract_zotero_table as cli


def html(rows, headers=None):
    head = '' if headers is None else '<tr>' + ''.join(f'<th>{h}</th>' for h in headers) + '</tr>'
    return '<table>' + head + ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in row) + '</tr>' for row in rows) + '</table>'


def table(data, page=0):
    df = pd.DataFrame(data)
    df.attrs.update(label='Table 1', page_idx=page, extractor='mineru_api', is_continuation=page > 0)
    return df


class TestDataIntegrity(unittest.TestCase):
    def test_mixed_formats_preserve_document_order(self):
        md = 'Table 1\n|Sample|Value|\n|---|---|\n|A|13|\n|B|25|\n\n'
        h = 'Table 2\n' + html([['C', 37], ['D', 49]], ['Sample', 'Value'])
        tail = '\n\nTable 3\n|Sample|Value|\n|---|---|\n|E|61|\n'
        dfs = tp.parse_structured_vlm_content(md + h + tail)
        self.assertEqual([d.attrs.get('label') for d in dfs], ['Table 1', 'Table 2', 'Table 3'])
        self.assertEqual([d.iloc[0, 0] for d in dfs], ['A', 'C', 'E'])

    def test_lexical_values_survive_parsing_and_excel(self):
        rows = [['001', 'NA', '1234567890123456789'], ['002', 'None', ''], ['003', 'N.D.', '1234567890123456791']]
        markdown = '|Sample|Category|Identifier|\n|---|---|---|\n' + '\n'.join('|' + '|'.join(r) + '|' for r in rows)
        for content in [html(rows, ['Sample', 'Category', 'Identifier']), markdown]:
            with self.subTest(content=content[:20]), tempfile.TemporaryDirectory() as td:
                dfs = tp.parse_structured_vlm_content(content)
                self.assertEqual(dfs[0].values.tolist(), rows)
                path = Path(td) / 'tables.xlsx'
                self.assertTrue(ee.save_tables_to_excel(dfs, str(path), single_file=True))
                wb = load_workbook(path)
                try:
                    values = list(wb.active.values)
                    self.assertEqual(values[1], tuple(rows[0]))
                    self.assertEqual(values[3], tuple(rows[2]))
                finally:
                    wb.close()

    def test_detection_limits_are_data(self):
        for value in ['<0.01', '≤0.01', 'N.D.', 'ND', 'BDL', 'NA', '—']:
            with self.subTest(value=value):
                df = oc._parse_mineru_table_html(html([['A01', value], ['A02', '0.02']]))[0]
                df.attrs['extractor'] = 'mineru_api'
                cleaned = tp.postprocess_dataframe(df)
                self.assertEqual(len(cleaned), 2)
                self.assertEqual(cleaned.iloc[0, 0], 'A01')

    def test_explicit_numeric_headers_survive_cleanup(self):
        for headers in [['1', '2'], ['0', '1']]:
            df = oc._parse_mineru_table_html(html([['A', 'B']], headers))[0]
            df.attrs['extractor'] = 'mineru_api'
            self.assertEqual(list(tp.postprocess_dataframe(df).columns), headers)

    def test_explicit_numeric_headers_survive_continuation(self):
        dfs = []
        for page, row in enumerate([['A', '13'], ['B', '25']]):
            df = oc._parse_mineru_table_html(html([row], ['1', '2']))[0]
            df.attrs.update(label='Table 1', page_idx=page, extractor='mineru_api', is_continuation=page > 0)
            dfs.append(df)
        merged = tp.merge_continuation_tables(dfs)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].values.tolist(), [['A', '13'], ['B', '25']])

    def test_vertical_continuation_preserves_sample_rows(self):
        a = table({'Sample': ['Sample A', 'Sample B'], 'Value': ['13', '25']})
        b = table({'Sample': ['Sample C', 'Sample D'], 'Value': ['37', '49']}, 1)
        with patch.dict(os.environ, {'MINERU_TABLE_MERGE_ENABLE': 'true'}):
            dfs = tp.merge_continuation_tables([a, b])
        self.assertEqual(sum(len(d) for d in dfs), 4)
        self.assertEqual(dfs[0]['Sample'].tolist(), ['Sample A', 'Sample B', 'Sample C', 'Sample D'])

    def test_ambiguous_horizontal_parts_stay_separate(self):
        a = table({'Sample': ['A', 'B', 'A'], 'Si': ['13', '25', '37']})
        b = table({'Sample': ['A', 'A', 'B'], 'Ca': ['61', '73', '85']}, 1)
        with patch.dict(os.environ, {'MINERU_TABLE_MERGE_ENABLE': 'true'}):
            dfs = tp.merge_continuation_tables([a, b])
        self.assertEqual(len(dfs), 2)
        self.assertEqual(dfs[1].loc[dfs[1]['Sample'] == 'B', 'Ca'].tolist(), ['85'])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'parts.xlsx'
            ee.save_tables_to_excel(dfs, str(path))
            wb = load_workbook(path)
            try:
                self.assertEqual(len(wb.worksheets), 2)
            finally:
                wb.close()
            directory = str(Path(td) / 'individual')
            self.assertTrue(ee.save_tables_to_excel(dfs, directory, single_file=False))
            self.assertEqual(len(list(Path(directory).glob('*.xlsx'))), 2)

    def test_unique_horizontal_keys_still_merge_by_identity(self):
        a = table({'Sample': ['A', 'B'], 'Si': ['13', '25']})
        b = table({'Sample': ['B', 'A'], 'Ca': ['85', '61']}, 1)
        merged = tp.combine_df_group([a, None, pd.DataFrame(), b])
        self.assertEqual(merged.loc[merged.Sample == 'B', 'Ca'].tolist(), ['85'])

    def test_failed_directory_update_is_not_success_and_preserves_file(self):
        df = table({'Sample': ['Alpha', 'Beta'], 'Value': ['13', '25']})
        with tempfile.TemporaryDirectory() as td:
            ee.save_tables_to_excel([df], td, single_file=False)
            path = next(Path(td).glob('*.xlsx'))
            before = path.read_bytes()
            def partial_write(path, **kwargs):
                Path(path).write_bytes(b'incomplete workbook')
                raise OSError('simulated write failure')
            with patch.object(pd.DataFrame, 'to_excel', side_effect=partial_write):
                try:
                    result = ee.save_tables_to_excel([df.copy()], td, single_file=False)
                except OSError:
                    result = False
            self.assertFalse(result)
            self.assertEqual(path.read_bytes(), before)


class TestIntegrationContracts(unittest.TestCase):
    def test_modern_middle_coordinates_use_schema_and_layout(self):
        data = {'schema': 'docvortex.middle', 'schema_version': '2.0',
                'extensions': {'docvortex_layout': {'pages': [{'page_idx': 2, 'width_pt': 600, 'height_pt': 800}]}},
                'pages': [{'page_idx': 2, 'blocks': [{'type': 'table', 'bbox': [0.1, 0.5, 0.9, 0.625],
                           'content': [{'html': html([['A', '13']], ['Sample', 'Value'])}]}]}]}
        for name in ['paper.json', 'middle_json.json']:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as zf:
                zf.writestr(name, json.dumps(data))
            df = oc.extract_mineru_structured_archive(buf.getvalue())[0]
            self.assertEqual(df.attrs['bbox'], [100, 500, 900, 625])
            self.assertEqual(df.attrs['bbox_format'], 'norm_1000')
            self.assertEqual(df.attrs['page_size'], [600, 800])
            self.assertEqual(df.attrs['page_idx'], 2)

    def test_content_list_filename_does_not_change_units(self):
        data = [{'type': 'table', 'table_body': html([['A', '13']], ['Sample', 'Value']), 'bbox': [100, 500, 900, 625], 'page_idx': 0}]
        for name in ['paper.json', 'Middle_Jurassic.json', 'Middle_Jurassic_content_list.json', 'middle.json']:
            with self.subTest(name=name):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, 'w') as zf:
                    zf.writestr(name, json.dumps(data))
                df = oc.extract_mineru_structured_archive(buf.getvalue())[0]
                self.assertEqual(df.attrs['bbox_format'], 'norm_1000')

    def test_tls_hostname_survives_proxy_pool(self):
        adapter = oc.HostHeaderSSLAdapter('assets.example.invalid')
        req = requests.Request('GET', 'https://203.0.113.8/file.zip').prepare()
        try:
            for proxies in [{}, {'https': 'http://proxy.invalid:8080'}]:
                if hasattr(adapter, 'get_connection_with_tls_context'):
                    pool = adapter.get_connection_with_tls_context(req, verify=True, proxies=proxies)
                else:
                    pool = adapter.get_connection(req.url, proxies=proxies)
                self.assertEqual(pool.assert_hostname, 'assets.example.invalid')
                self.assertEqual(pool.conn_kw.get('server_hostname'), 'assets.example.invalid')
        finally:
            adapter.close()

    def test_cli_failure_returns_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / 'input.pdf'
            pdf.write_bytes(b'%PDF-test')
            code = "import sys; from unittest.mock import patch; import extract_zotero_table as c\nwith patch.object(c, 'process_single_pdf', return_value=False):\n sys.exit(c.main())\n"
            env = dict(os.environ, PYTHONPATH=str(Path(oc.__file__).parent) + os.pathsep + os.environ.get('PYTHONPATH', ''))
            result = subprocess.run([sys.executable, '-c', code, '--pdf', str(pdf), '--output', str(Path(td) / 'out'), '--pdf-only'], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_cli_partial_batch_failure_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            for name in ['a.pdf', 'b.pdf']:
                (Path(td) / name).write_bytes(b'%PDF-test')
            args = ['extract_zotero_table.py', '--pdf', td, '--output', str(Path(td) / 'out'), '--pdf-only']
            with patch.object(sys, 'argv', args), patch.object(cli, 'process_single_pdf', side_effect=lambda p, *a, **kw: str(p).endswith('a.pdf')):
                self.assertEqual(cli.main(), 1)

    def test_repeated_labels_need_separate_page_coverage(self):
        decls = {0: [{'label': 'Table 1', 'title': 'Initial data'}], 4: [{'label': 'Table 1', 'title': 'Appendix data'}]}
        with patch.object(tv, 'scan_pdf_table_declarations', return_value=decls):
            report = tv.cross_check_missed_tables([table({'A': [13]})], 'nonexistent.pdf')
        self.assertFalse(report['is_complete'])
        self.assertEqual([d['page_idx'] for d in report['missed_tables']], [4])

    def test_continuation_covered_pages_remain_covered(self):
        df = table({'A': [13]})
        df.attrs['covered_pages'] = [0, 1]
        decls = {0: [{'label': 'Table 1'}], 1: [{'label': 'Table 1', 'is_continuation': True}]}
        with patch.object(tv, 'scan_pdf_table_declarations', return_value=decls):
            self.assertTrue(tv.cross_check_missed_tables([df], 'nonexistent.pdf')['is_complete'])

    def test_duplicate_recovery_does_not_starve_missing_table(self):
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as stack:
            pdf = Path(td) / 'tables.pdf'
            doc = fitz.open(); page = doc.new_page(width=600, height=800)
            page.insert_text((50, 80), 'Table 1. Alpha data'); page.insert_text((50, 400), 'Table 2. Beta data')
            names = [['Alpha', 'Beta', 'Gamma', 'Delta'], ['Epsilon', 'Zeta', 'Eta', 'Theta']]
            values = [[13, 25, 37, 49], [61, 73, 85, 97]]
            tables = []
            for idx, y in enumerate([110, 430]):
                for row, (name, value) in enumerate(zip(names[idx], values[idx])):
                    page.insert_text((50, y + 20 * row), name); page.insert_text((200, y + 20 * row), str(value))
                d = table({'Sample': names[idx], 'Value': list(map(str, values[idx]))})
                d.attrs.update(label=f'Table {idx+1}', bbox=[40, y-20, 300, y+85], bbox_format='points')
                tables.append(d)
            doc.save(pdf); doc.close()
            a, b = tables
            stack.enter_context(patch.object(common, 'load_config', return_value={'MINERU_TOKEN': 'offline-test'}))
            stack.enter_context(patch.object(pe, 'classify_and_extract_via_inspector', return_value={'pdf_type': 'text_based', 'confidence': 1., 'pages_with_tables': [1], 'tables': [], 'structure_tables': []}))
            for name in ['extract_via_structure_tree', 'extract_via_find_tables', 'extract_via_pdfplumber', 'extract_via_camelot', 'extract_via_img2table', 'extract_via_gmft', 'extract_via_pymupdf4llm', 'extract_via_ocr', 'extract_via_paddleocr_fullpage']:
                stack.enter_context(patch.object(pe, name, return_value=[]))
            stack.enter_context(patch.object(oc, 'is_mineru_available', return_value=True))
            stack.enter_context(patch.object(oc, 'call_mineru_structured_api', return_value=[a.copy()]))
            stack.enter_context(patch.object(pe, 'is_rapid_table_available', return_value=True))
            stack.enter_context(patch.object(pe, 'extract_via_rapid_table', return_value=[{'df': a.copy(), 'page_idx': 0}]))
            crops = stack.enter_context(patch.object(pe, 'extract_via_paddleocr_crops', return_value=[]))
            stack.enter_context(patch.object(oc, 'extract_via_docling', return_value=[]))
            align = stack.enter_context(patch.object(pe, 'extract_via_text_alignment', return_value=[{'df': b.copy(), 'page_idx': 0}]))
            results, _ = pe._do_extract_tables_from_pdf(str(pdf), use_ocr_fallback=True)
            self.assertGreater(crops.call_count, 0)
            self.assertGreater(align.call_count, 0)
            self.assertIn('Table 2', [r['df'].attrs.get('label') for r in results])


if __name__ == '__main__':
    unittest.main()
