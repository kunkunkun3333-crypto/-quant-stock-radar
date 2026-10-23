"""Offline deterministic fixtures; no production integrations or network calls.
Run: python -m unittest discover -s v52_test/tests -p test_v53_eps_history.py -v
The six EPS values are acceptance fixtures, not a new live download.
"""
import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v53_eps_history as h


def fixture(market='TWSE', eps='29.31'):
    samples = [('2330', eps), ('2303', '1.34'), ('2454', '35.92'),
               ('3711', '3.48'), ('2801', '0'), ('2881', '-1')]
    if market == 'TPEX':
        samples = [('6488', '6.56'), ('3293', '18.00')]
    title = '上市' if market == 'TWSE' else '上櫃'
    html = ['<html><body><h2>'+title+'公司第二季資料</h2>截至該季止之累計金額']
    files = {}
    header = ['公司代號', '公司名稱', h.EPS_HEADER]
    for i, (code, value) in enumerate(samples):
        fn = f't163sb04_20261022_{i}.csv'
        inp = f'<input name="filename" value="{fn}">'
        html += [inp, '<table class="hasBorder"><tr>'+''.join('<th>'+c+'</th>' for c in header)+'</tr>',
                 '<tr>'+''.join('<td>'+c+'</td>' for c in [code, '測試公司', value])+'</tr></table>', inp]
        s = io.StringIO(newline='')
        w = csv.writer(s)
        w.writerow(['出表日期', '年度', '季別']+header)
        w.writerow(['115/10/22', '114', '2', code, '測試公司', value])
        files[fn] = h.HTTPResponse(200, s.getvalue().encode('cp950'))
    html.append('</body></html>')
    return dict(market=market, fiscal_year=2025, quarter=2,
                fetched_at='2026-10-22T12:00:00+00:00',
                manifest=h.HTTPResponse(200, ''.join(html).encode()), csv_files=files)


def replace_csv(b, transform):
    fn = next(iter(b['csv_files']))
    r = b['csv_files'][fn]
    b['csv_files'][fn] = h.HTTPResponse(200, transform(r.body))
    return b


class ArchiveTests(unittest.TestCase):
    def bad(self, b, code=None):
        with self.assertRaises(h.EPSHistoryError) as cm:
            h.validate_historical_eps_batch(b)
        if code:
            self.assertEqual(cm.exception.code, code)

    def fetch(self, b, faults=()):
        calls, waits = [], []
        faults = list(faults)
        def transport(url, form, timeout):
            calls.append((url, form))
            if faults:
                f = faults.pop(0)
                if isinstance(f, Exception):
                    raise f
                return f
            return b['manifest'] if url == h.MANIFEST_URL else b['csv_files'][form['filename']]
        result = h.fetch_historical_eps_batch(b['market'], 2025, 2, transport=transport, sleep=waits.append)
        return result, calls, waits

    def test_twse_six_files(self):
        r, calls, _ = self.fetch(fixture())
        self.assertEqual(r['metadata']['csv_file_count'], 6)
        self.assertEqual(len(calls), 7)
        self.assertEqual(calls[0][1]['year'], '114')
        self.assertEqual(calls[0][1]['season'], '02')
    def test_tpex_two_files(self):
        r, calls, _ = self.fetch(fixture('TPEX'))
        self.assertEqual(r['metadata']['csv_file_count'], 2)
        self.assertEqual(len(calls), 3)
    def test_duplicate_filename_dedup(self):
        b = fixture()
        self.assertEqual(b['manifest'].body.count(b'name="filename"'), 12)
        self.assertEqual(h.validate_historical_eps_batch(b)['metadata']['manifest_filename_count'], 6)
    def test_503_retry(self):
        _, calls, waits = self.fetch(fixture(), [h.HTTPResponse(503, b'')])
        self.assertEqual(waits, [2]); self.assertEqual(len(calls), 8)
    def test_timeout_retry(self):
        _, _, waits = self.fetch(fixture(), [TimeoutError(), TimeoutError()])
        self.assertEqual(waits, [2, 8])
    def test_retry_limit(self):
        calls = []
        def fail(*args):
            calls.append(1); raise TimeoutError()
        with self.assertRaises(h.EPSHistoryError):
            h.fetch_historical_eps_batch('TWSE', 2025, 2, transport=fail, sleep=lambda _: None)
        self.assertEqual(len(calls), 3)
    def test_upstream_retry(self):
        _, _, waits = self.fetch(fixture(), [h.HTTPResponse(200, b'upstream connect error')])
        self.assertEqual(waits, [2])
    def test_partial_retry(self):
        _, _, waits = self.fetch(fixture(), [h.HTTPResponse(200, b'<html>', False)])
        self.assertEqual(waits, [2])
    def test_html_error(self):
        self.bad(replace_csv(fixture(), lambda _: b'<html>Error</html>\n'), 'html_error')
    def test_midrow_truncation(self):
        self.bad(replace_csv(fixture(), lambda b: b[:-7]), 'partial_body')
    def test_complete_newline_truncation(self):
        b = fixture()
        # Remove a whole last row from the manifest's expected two-row table.
        fn = next(iter(b['csv_files']))
        s = b['csv_files'][fn].body.decode('cp950')
        extra = s.splitlines(True)[1].replace('2330', '2331')
        full = s+extra
        body = b['manifest'].body.decode().replace('</tr></table>', '</tr><tr><td>2331</td><td>測試公司</td><td>29.31</td></tr></table>', 1)
        b['manifest'] = h.HTTPResponse(200, body.encode())
        b['csv_files'][fn] = h.HTTPResponse(200, full.encode('cp950'))
        h.validate_historical_eps_batch(b)
        b['csv_files'][fn] = h.HTTPResponse(200, s.encode('cp950'))
        self.bad(b, 'code_set_mismatch')
    def test_cp950(self):
        self.bad(replace_csv(fixture(), lambda b: b'\x81\n'), 'csv_encoding')
    def test_row_width(self):
        self.bad(replace_csv(fixture(), lambda b: b.replace(b'29.31', b'29.31,extra')), 'row_width')
    def test_period(self):
        self.bad(replace_csv(fixture(), lambda b: b.replace(b',114,2,', b',113,2,')), 'period')
    def test_eps_mismatch(self):
        self.bad(replace_csv(fixture(), lambda b: b.replace(b'29.31', b'29.32')), 'eps_mismatch')
    def test_duplicate_code(self):
        self.bad(replace_csv(fixture(), lambda b: b+b.splitlines(True)[1]), 'duplicate_code')
    def test_codeset(self):
        self.bad(replace_csv(fixture(), lambda b: b.replace(b'2330', b'2331')), 'code_set_mismatch')
    def test_manifest_market(self):
        b = fixture(); b['market'] = 'TPEX'; self.bad(b, 'manifest_period')
    def test_manifest_truncation(self):
        b = fixture(); b['manifest'] = h.HTTPResponse(200, b['manifest'].body[:-7]); self.bad(b, 'partial_body')
    def test_missing_file(self):
        b = fixture(); b['csv_files'].pop(next(iter(b['csv_files']))); self.bad(b, 'file_set')
    def test_nan_infinity(self):
        for val in ('NaN', 'Infinity', '-Infinity', 'abc'):
            self.bad(fixture(eps=val))
    def test_zero_negative_missing(self):
        for val, expected in [('0', 0), ('-2.3', -2.3), ('--', None)]:
            r = h.validate_historical_eps_batch(fixture(eps=val))
            self.assertEqual(r['records']['2330']['basic_eps'], expected)
    def test_hash_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            h.save_historical_eps_snapshot(fixture(), d)
            p = next(Path(d).rglob('*.csv')); p.write_bytes(p.read_bytes()+b'\n')
            with self.assertRaises(h.EPSHistoryError):
                h.load_historical_eps_snapshot(d, 'TWSE', 2025, 2)
    def test_same_content_no_new_version(self):
        with tempfile.TemporaryDirectory() as d:
            a = h.save_historical_eps_snapshot(fixture(), d)
            b = fixture(); b['fetched_at'] = '2026-10-23T12:00:00+00:00'
            c = h.save_historical_eps_snapshot(b, d)
            self.assertEqual(a, c)
            self.assertEqual(len(list(Path(d).rglob('snapshot.json'))), 1)
    def test_changed_content_new_version(self):
        with tempfile.TemporaryDirectory() as d:
            a = h.save_historical_eps_snapshot(fixture(), d)
            b = h.save_historical_eps_snapshot(fixture(eps='30.00'), d)
            self.assertNotEqual(a['metadata']['snapshot_version'], b['metadata']['snapshot_version'])
            self.assertEqual(len(list(Path(d).rglob('snapshot.json'))), 2)
    def test_partial_batch_not_published(self):
        with tempfile.TemporaryDirectory() as d:
            b = fixture(); b['csv_files'].pop(next(iter(b['csv_files'])))
            with self.assertRaises(h.EPSHistoryError): h.save_historical_eps_snapshot(b, d)
            self.assertEqual(list(Path(d).rglob('snapshot.json')), [])
    def test_read_no_network_and_six_values(self):
        with tempfile.TemporaryDirectory() as d:
            for m in ('TWSE', 'TPEX'): h.save_historical_eps_snapshot(fixture(m), d)
            with patch.object(h, '_http_post', side_effect=AssertionError('NETWORK')):
                for market, code, expected in [('TWSE','2330',29.31),('TWSE','2303',1.34),('TWSE','2454',35.92),('TWSE','3711',3.48),('TPEX','6488',6.56),('TPEX','3293',18.00)]:
                    r = h.get_historical_eps(d, market, code, 2025, 2)
                    self.assertEqual(r['basic_eps'], expected)
                    self.assertEqual(r['basis'], 'cumulative_ytd')
    def test_interrupted_write(self):
        with tempfile.TemporaryDirectory() as d:
            a = h.save_historical_eps_snapshot(fixture(), d)
            with patch.object(h, '_write', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): h.save_historical_eps_snapshot(fixture(eps='30'), d)
            self.assertEqual(h.load_historical_eps_snapshot(d, 'TWSE', 2025, 2), a)
            self.assertEqual(len(list(Path(d).rglob('snapshot.json'))), 1)
    def test_unknown_metadata(self):
        r = h.validate_historical_eps_batch(fixture())['records']['2330']
        for k in ('statement_scope','revision_id','comparative_restatement_status'): self.assertEqual(r[k], 'unknown')
        self.assertEqual(r['identity_status'], 'unverified')
        self.assertEqual(r['source_date'], '2026-10-22')
    def test_absent_read(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(h.get_historical_eps(d, 'TWSE', '2330', 2025, 2))
    def test_schema_does_not_retry(self):
        b = fixture(); calls = []
        def bad(*args): calls.append(1); return h.HTTPResponse(200, b'<html><body>Error</body></html>')
        with self.assertRaises(h.EPSHistoryError): h.fetch_historical_eps_batch('TWSE',2025,2,transport=bad,sleep=lambda _:None)
        self.assertEqual(len(calls),1)

if __name__ == '__main__': unittest.main()
