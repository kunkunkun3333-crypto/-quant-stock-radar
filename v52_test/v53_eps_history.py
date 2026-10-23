"""Independent MOPS historical cumulative basic EPS archive (stdlib only).

No imports from the scanner, Quality, Yahoo or Backtest. No network on import/read.
Scope: complete CURRENT MOPS summary response for a requested market/quarter;
NOT a point-in-time universe, original filing archive or survivor-bias-free data.

fetch_historical_eps_batch('TWSE', 2025, 2) downloads but does not save.
save_historical_eps_snapshot(batch, archive_dir) revalidates then atomically commits.
get_historical_eps(archive_dir, 'TWSE', '2330', 2025, 2) only reads validated disk data.

Versions hash the request + sorted raw CSV content hashes (not volatile manifest
filenames/fetch time). Identical CSV bytes reuse the existing immutable version.
A CSV change, including its official output date, produces a new version.
"""
from __future__ import annotations

import csv
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

MANIFEST_URL = 'https://mopsov.twse.com.tw/mops/web/ajax_t163sb04'
CSV_URL = 'https://mopsov.twse.com.tw/server-java/t105sb02'
SOURCE = 'MOPS/t163sb04'
SCOPE = 'complete_current_mops_summary_for_requested_period'
MAX_BYTES = 32 * 1024 * 1024
FILE_RE = re.compile(r't163sb04_[0-9_]+\.csv\Z')
VERSION_RE = re.compile(r'[0-9a-f]{64}\Z')
EPS_HEADER = '基本每股盈餘（元）'


class EPSHistoryError(ValueError):
    """Fail closed: a rejected batch must never be published."""
    def __init__(self, code, message='', retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(f'{code}: {message}')


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    complete: bool = True


def _fail(code, message='', retryable=False):
    raise EPSHistoryError(code, message, retryable)


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _request(market, fiscal_year, quarter):
    if market not in ('TWSE', 'TPEX'):
        _fail('market', 'Use TWSE or TPEX')
    if type(fiscal_year) is not int or not 2013 <= fiscal_year <= 9999:
        _fail('year', 'Gregorian fiscal_year >= 2013 required')
    if type(quarter) is not int or quarter not in (1, 2, 3, 4):
        _fail('quarter')
    return dict(market=market, fiscal_year=fiscal_year,
                fiscal_year_roc=fiscal_year-1911, quarter=quarter)


def _utc(value=None):
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if d.tzinfo is None:
            raise ValueError('Timezone required')
        return d.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError):
        _fail('fetched_at')


def _date(value):
    s = value.strip()
    parts = re.split(r'[/.-]', s)
    try:
        if len(parts) == 3:
            y, m, d = map(int, parts)
            if len(parts[0]) <= 3:
                y += 1911
        elif re.fullmatch(r'\d{7}', s):
            y, m, d = int(s[:3])+1911, int(s[3:5]), int(s[5:])
        elif re.fullmatch(r'\d{8}', s):
            y, m, d = int(s[:4]), int(s[4:6]), int(s[6:])
        else:
            raise ValueError()
        return date(y, m, d).isoformat()
    except ValueError:
        _fail('source_date', s)


def _eps(s):
    s = s.strip().replace(',', '')
    if s in ('', '--', 'N/A'):
        return None  # Official missing marker, never replaced with zero.
    try:
        d = Decimal(s)
        if not d.is_finite() or not math.isfinite(float(d)):
            raise InvalidOperation()
        return d
    except (InvalidOperation, ValueError, OverflowError):
        _fail('eps_parse', s)


def _header(s):
    return re.sub(r'\s+', '', s).replace('(', '（').replace(')', '）')


class _ManifestParser(HTMLParser):
    """Parse only static tables; never execute scripts or guess download URLs."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.files, self.tables, self.headings = [], [], []
        self.last_file = None
        self.depth = 0
        self.active = None
        self.row = None
        self.cell = None
        self.h2 = None
        self.text = []
        self.echo = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'input':
            if a.get('name') == 'filename':
                fn = a.get('value', '')
                if not FILE_RE.fullmatch(fn):
                    _fail('filename', 'Unsafe/unrecognized filename')
                self.last_file = fn
                if fn not in self.files:
                    self.files.append(fn)
            if a.get('name') in ('year', 'season', 'TYPEK'):
                self.echo.setdefault(a['name'], set()).add(a.get('value', ''))
        if tag == 'h2':
            self.h2 = []
        if tag == 'table':
            self.depth += 1
            if 'hasBorder' in a.get('class', '').split():
                if self.active is not None or not self.last_file:
                    _fail('manifest_schema', 'Unbound/nested data table')
                self.active = dict(filename=self.last_file, rows=[], depth=self.depth)
        if self.active is not None:
            if tag == 'tr':
                if self.row is not None:
                    _fail('partial_body', 'Unclosed row', True)
                self.row = []
            if tag in ('td', 'th') and self.row is not None:
                if self.cell is not None:
                    _fail('partial_body', 'Unclosed cell', True)
                self.cell = []

    def handle_data(self, data):
        self.text.append(data)
        if self.cell is not None:
            self.cell.append(data)
        if self.h2 is not None:
            self.h2.append(data)

    def handle_endtag(self, tag):
        if tag == 'h2' and self.h2 is not None:
            self.headings.append(''.join(self.h2).strip())
            self.h2 = None
        if self.active is not None:
            if tag in ('td', 'th') and self.cell is not None:
                self.row.append(''.join(self.cell).strip())
                self.cell = None
            if tag == 'tr' and self.row is not None:
                if self.cell is not None:
                    _fail('partial_body', 'Unclosed cell', True)
                self.active['rows'].append(self.row)
                self.row = None
            if tag == 'table' and self.depth == self.active['depth']:
                if self.row is not None:
                    _fail('partial_body', 'Unclosed row', True)
                self.tables.append(self.active)
                self.active = None
        if tag == 'table':
            self.depth -= 1
            if self.depth < 0:
                _fail('manifest_schema', 'Unbalanced tables')


def _body(response):
    if not isinstance(response, HTTPResponse) or not isinstance(response.body, bytes):
        _fail('transport_contract', 'Return HTTPResponse(status, bytes, complete)')
    if response.status != 200:
        _fail('http_status', str(response.status), response.status == 503)
    b = response.body
    if not response.complete or not b:
        _fail('partial_body', 'Incomplete transport', True)
    if len(b) > MAX_BYTES:
        _fail('size_limit')
    if b'upstream connect error' in b.lower() or b'upstream request timeout' in b.lower():
        _fail('upstream_error', retryable=True)
    return b


def _manifest(response, req):
    b = _body(response)
    try:
        s = b.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        _fail('manifest_encoding')
    if not re.search(r'</html>\s*$', s, re.I) or not re.search(r'</body>', s, re.I):
        _fail('partial_body', 'Missing HTML ending', True)
    p = _ManifestParser()
    p.feed(s)
    p.close()
    if p.depth or p.active is not None or p.cell is not None:
        _fail('partial_body', 'Unclosed table', True)
    market = '上市' if req['market'] == 'TWSE' else '上櫃'
    season = '一二三四'[req['quarter']-1]
    if not any(f'{market}公司第{season}季資料' in _header(h) for h in p.headings):
        _fail('manifest_period', 'Market/quarter heading differs or is missing')
    if '截至該季止之累計金額' not in ''.join(p.text):
        _fail('basis', 'Cumulative YTD evidence missing')
    expected_echo = {'year': str(req['fiscal_year_roc']),
                     'season': f"{req['quarter']:02d}",
                     'TYPEK': 'sii' if req['market'] == 'TWSE' else 'otc'}
    for k, vals in p.echo.items():
        if vals != {expected_echo[k]}:
            _fail('manifest_period', f'Conflicting {k}')
    # Current MOPS HTML does NOT echo a fiscal year. Year is proven by ALL
    # CSV period fields plus their exact per-table correspondence to this request.
    tables = {}
    codes = set()
    for table in p.tables:
        fn, rows = table['filename'], table['rows']
        if fn in tables or len(rows) < 2:
            _fail('manifest_schema', 'Duplicate/empty data table')
        h = [_header(x) for x in rows[0]]
        if len(h) != len(set(h)) or any(k not in h for k in ('公司代號', '公司名稱', EPS_HEADER)):
            _fail('manifest_schema', 'Missing/duplicate headers')
        entries = {}
        for row in rows[1:]:
            if len(row) != len(h):
                _fail('row_width', fn)
            r = dict(zip(h, row))
            c = r['公司代號'].strip()
            if not re.fullmatch(r'\d{4,6}', c):
                _fail('code', c)
            if c in codes:
                _fail('duplicate_code', c)
            codes.add(c)
            entries[c] = dict(name=r['公司名稱'].strip(), eps=_eps(r[EPS_HEADER]))
        tables[fn] = dict(header=h, entries=entries)
    if not tables or set(tables) != set(p.files):
        _fail('manifest_schema', 'Each filename must have exactly one data table')
    return dict(files=p.files, tables=tables, year_echo=bool(p.echo.get('year')))


def _csv(response, fn, table, req):
    b = _body(response)
    if re.search(br'<(?:!doctype|html|body)\b', b[:1024], re.I):
        _fail('html_error', fn)
    if not b.endswith(b'\n'):
        _fail('partial_body', 'CSV missing final newline', True)
    try:
        s = b.decode('cp950', errors='strict')
    except UnicodeDecodeError:
        _fail('csv_encoding', fn)
    try:
        rows = list(csv.reader(io.StringIO(s, newline=''), strict=True))
    except csv.Error:
        _fail('partial_body', 'Malformed CSV quoting', True)
    if len(rows) < 2:
        _fail('csv_schema', 'Missing rows')
    h = [_header(x) for x in rows[0]]
    expected_h = ['出表日期', '年度', '季別'] + table['header']
    if h != expected_h:
        _fail('csv_schema', 'CSV/HTML header signatures differ')
    result = {}
    for row in rows[1:]:
        if len(row) != len(h):
            _fail('row_width', fn)
        r = dict(zip(h, row))
        if r['年度'].strip() != str(req['fiscal_year_roc']) or r['季別'].strip() not in (str(req['quarter']), f"{req['quarter']:02d}"):
            _fail('period', fn)
        c = r['公司代號'].strip()
        if not re.fullmatch(r'\d{4,6}', c):
            _fail('code', c)
        if c in result:
            _fail('duplicate_code', c)
        e = _eps(r[EPS_HEADER])
        result[c] = dict(code=c, company_name=r['公司名稱'].strip(),
                         basic_eps=None if e is None else float(e),
                         source_date=_date(r['出表日期']), raw_filename=fn,
                         content_sha256=_sha(b))
        if c in table['entries']:
            hr = table['entries'][c]
            if hr['eps'] != e:
                _fail('eps_mismatch', c)
            if hr['name'] != r['公司名稱'].strip():
                _fail('company_mismatch', c)
    want = set(table['entries'])
    got = set(result)
    if got != want:
        _fail('code_set_mismatch', fn, got < want)  # Missing rows: bounded retry.
    return result, dict(filename=fn, content_sha256=_sha(b), byte_count=len(b),
                        row_count=len(result), field_count=len(h),
                        header_signature=_sha(_json(h)))


def validate_historical_eps_batch(batch):
    """Revalidate raw evidence, ignoring caller-supplied records/status/version.

Input keys: market, fiscal_year (AD), quarter, manifest (HTTPResponse),
csv_files {dynamic filename: HTTPResponse}, optional fetched_at and batch_id.
Raises EPSHistoryError on any failed gate; never returns a partial success.
"""
    req = _request(batch['market'], batch['fiscal_year'], batch['quarter'])
    manifest = _manifest(batch['manifest'], req)
    files = batch['csv_files']
    if set(files) != set(manifest['files']):
        _fail('file_set', 'Missing/extra CSV; whole batch rejected')
    fetched = _utc(batch.get('fetched_at'))
    records, file_metadata = {}, []
    for fn in manifest['files']:
        rows, meta = _csv(files[fn], fn, manifest['tables'][fn], req)
        if records.keys() & rows.keys():
            _fail('duplicate_code', 'Cross-table duplicate')
        records.update(rows)
        file_metadata.append(meta)
    version = _sha(_json({'schema_version': 1, 'request': req,
                         'csv_content_hashes': sorted(x['content_sha256'] for x in file_metadata)}))
    for r in records.values():
        r.update(req, basis='cumulative_ytd', eps_type='basic', source=SOURCE,
                 fetched_at=fetched, snapshot_version=version,
                 statement_scope='unknown', revision_id='unknown',
                 comparative_restatement_status='unknown', identity_status='unverified')
    batch_id = batch.get('batch_id') or uuid.uuid4().hex
    if not re.fullmatch(r'[0-9a-f]{32}', batch_id):
        _fail('batch_id')
    metadata = dict(req, batch_id=batch_id, fetched_at=fetched,
                    manifest_sha256=_sha(batch['manifest'].body),
                    manifest_filename_count=len(manifest['files']),
                    csv_file_count=len(files), total_rows=len(records),
                    unique_codes=len(records), validation_status='validated',
                    snapshot_version=version, schema_version=1, source=SOURCE,
                    scope=SCOPE, identity_status='unverified',
                    point_in_time_universe=False, published_at=None,
                    period_verification='request_binding+manifest_market_quarter+all_csv_year_quarter',
                    manifest_year_echoed=manifest['year_echo'], files=file_metadata)
    return dict(metadata=metadata, records=records, manifest=batch['manifest'],
                csv_files=files, request_log=list(batch.get('request_log', [])))


def _http_post(url, form, timeout):
    """Default transport. Socket timeout, finite body size, elapsed read deadline.

No retries/cookies/session mutation here. Dependency injection is supported by
fetch_historical_eps_batch(transport=callable(url, form, timeout)).
"""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode('ascii'),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'},
                                 method='POST')
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            pieces, size = [], 0
            while True:
                part = response.read(65536)
                if not part:
                    break
                pieces.append(part)
                size += len(part)
                if size > MAX_BYTES:
                    _fail('size_limit')
                if time.monotonic()-started > timeout:
                    raise TimeoutError('Response read deadline')
            return HTTPResponse(response.status, b''.join(pieces))
    except urllib.error.HTTPError as exc:
        return HTTPResponse(exc.code, b'')


def fetch_historical_eps_batch(market, fiscal_year, quarter, *, transport=None,
                               manifest_timeout=240, csv_timeout=60,
                               max_attempts=3, sleep=time.sleep):
    """Fetch one immutable-manifest batch; no disk writes or implicit archive.

At most 3 attempts per request; backoff 2, 8 seconds. A filename expiry rejects
this batch. Caller may explicitly start an entirely new batch (no mixed files).
"""
    req = _request(market, fiscal_year, quarter)
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        _fail('retry_policy')
    for timeout in (manifest_timeout, csv_timeout):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            _fail('timeout_policy')
    transport = transport or _http_post
    log = []

    def run(url, form, timeout, check):
        for attempt in range(1, max_attempts+1):
            start = time.monotonic()
            response = None
            try:
                response = transport(url, form, timeout)
                check(response)
                log.append(dict(endpoint='manifest' if url == MANIFEST_URL else 'csv',
                                filename=form.get('filename'), attempt=attempt,
                                http_status=response.status, error=None,
                                elapsed_ms=round((time.monotonic()-start)*1000, 2)))
                return response
            except (TimeoutError, socket.timeout, http.client.IncompleteRead) as exc:
                err = EPSHistoryError('timeout_or_partial', type(exc).__name__, True)
            except urllib.error.URLError as exc:
                err = EPSHistoryError('network_error', type(exc.reason).__name__,
                                      isinstance(exc.reason, (TimeoutError, socket.timeout)))
            except EPSHistoryError as exc:
                err = exc
            log.append(dict(endpoint='manifest' if url == MANIFEST_URL else 'csv',
                            filename=form.get('filename'), attempt=attempt,
                            http_status=None if response is None else response.status,
                            error=err.code, elapsed_ms=round((time.monotonic()-start)*1000, 2)))
            if not err.retryable or attempt == max_attempts:
                raise err
            sleep((2, 8)[attempt-1])
        raise AssertionError('unreachable')

    form = dict(encodeURIComponent='1', step='1', firstin='1', off='1', isQuery='Y',
                TYPEK='sii' if market == 'TWSE' else 'otc',
                year=str(req['fiscal_year_roc']), season=f'{quarter:02d}')
    response = run(MANIFEST_URL, form, manifest_timeout, lambda r: _manifest(r, req))
    manifest = _manifest(response, req)
    csv_files = {}
    for fn in manifest['files']:
        csv_files[fn] = run(CSV_URL, dict(firstin='true', step='10', filename=fn, saveCSV='Y'),
                            csv_timeout, lambda r, f=fn: _csv(r, f, manifest['tables'][f], req))
    return validate_historical_eps_batch(dict(req, manifest=response, csv_files=csv_files,
                                              fetched_at=_utc(), request_log=log))


def _period_dir(archive_dir, market, fiscal_year, quarter):
    _request(market, fiscal_year, quarter)
    return Path(archive_dir) / market / str(fiscal_year) / f'Q{quarter}' / 'versions'


def _write(path, data):
    with open(path, 'xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def save_historical_eps_snapshot(batch, archive_dir):
    """Revalidate, stage all raw evidence, then atomically publish a directory.

Returns the validated stored snapshot. Does not overwrite any old version.
Failed writes leave no visible version; no shared mutable latest pointer.
"""
    if 'metadata' in batch:
        raw = dict(batch['metadata'], manifest=batch['manifest'], csv_files=batch['csv_files'],
                   request_log=batch.get('request_log', []))
    else:
        raw = batch
    result = validate_historical_eps_batch(raw)
    meta = result['metadata']
    versions = _period_dir(archive_dir, meta['market'], meta['fiscal_year'], meta['quarter'])
    versions.mkdir(parents=True, exist_ok=True)
    dest = versions / meta['snapshot_version']
    if dest.exists():
        return load_historical_eps_snapshot(archive_dir, meta['market'], meta['fiscal_year'],
                                            meta['quarter'], meta['snapshot_version'])
    stage = Path(tempfile.mkdtemp(prefix='.staging-', dir=versions))
    try:
        (stage/'raw'/'csv').mkdir(parents=True)
        _write(stage/'raw'/'manifest.html', result['manifest'].body)
        for fn, response in result['csv_files'].items():
            _write(stage/'raw'/'csv'/fn, response.body)
        payload = dict(metadata=meta, records=result['records'], request_log=result['request_log'])
        envelope = dict(payload=payload, payload_sha256=_sha(_json(payload)))
        _write(stage/'snapshot.json', _json(envelope))
        try:
            os.rename(stage, dest)
        except OSError:
            if not dest.is_dir():
                raise
            # Concurrent identical content: load/revalidate winner, never overwrite.
        return load_historical_eps_snapshot(archive_dir, meta['market'], meta['fiscal_year'],
                                            meta['quarter'], meta['snapshot_version'])
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _load_version(path, req):
    try:
        envelope = json.loads((path/'snapshot.json').read_text('utf-8'))
        payload = envelope['payload']
        if _sha(_json(payload)) != envelope['payload_sha256']:
            _fail('snapshot_hash')
        m = payload['metadata']
        if m['validation_status'] != 'validated' or any(m[k] != v for k, v in req.items()):
            _fail('snapshot_status_or_period')
        if m['snapshot_version'] != path.name or not VERSION_RE.fullmatch(path.name):
            _fail('snapshot_version')
        names = [f['filename'] for f in m['files']]
        if len(names) != len(set(names)) or any(not FILE_RE.fullmatch(n) for n in names):
            _fail('snapshot_files')
        raw_manifest = (path/'raw'/'manifest.html').read_bytes()
        files = {n: HTTPResponse(200, (path/'raw'/'csv'/n).read_bytes()) for n in names}
        verified = validate_historical_eps_batch(dict(m, manifest=HTTPResponse(200, raw_manifest), csv_files=files))
        if verified['metadata'] != m or verified['records'] != payload['records']:
            _fail('snapshot_integrity')
        return payload
    except EPSHistoryError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise EPSHistoryError('snapshot_corrupt', type(exc).__name__) from exc


def load_historical_eps_snapshot(archive_dir, market, fiscal_year, quarter, snapshot_version=None):
    """Disk only. None if absent. Reject corruption instead of silent fallback.

Without version, latest means latest stored fetched_at (NOT official revision).
Every read verifies raw hashes and re-runs the full batch validation.
"""
    req = _request(market, fiscal_year, quarter)
    versions = _period_dir(archive_dir, market, fiscal_year, quarter)
    if snapshot_version is not None:
        if not VERSION_RE.fullmatch(snapshot_version):
            _fail('snapshot_version')
        path = versions/snapshot_version
        return _load_version(path, req) if path.exists() else None
    if not versions.exists():
        return None
    snapshots = [_load_version(p, req) for p in versions.iterdir()
                 if p.is_dir() and VERSION_RE.fullmatch(p.name)]
    if not snapshots:
        return None
    return max(snapshots, key=lambda s: (s['metadata']['fetched_at'], s['metadata']['batch_id']))


def get_historical_eps(archive_dir, market, code, fiscal_year, quarter, snapshot_version=None):
    """Return the archived record or None. No networking or EPS Growth logic."""
    if not isinstance(code, str) or not re.fullmatch(r'\d{4,6}', code):
        _fail('code', 'Use a string security code without .TW/.TWO')
    snapshot = load_historical_eps_snapshot(archive_dir, market, fiscal_year, quarter, snapshot_version)
    return None if snapshot is None else snapshot['records'].get(code)
