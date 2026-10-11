"""Import genuine official monthly rows; no reconstruction from adjacent months.

Historical HTML retrieval validates the target period and known table schema.
A 200 response alone is not accepted as a snapshot.
"""
import json
import math
import re
from html.parser import HTMLParser
from datetime import datetime, timezone
from urllib.request import urlopen
try:
    from .v53_monthly_revenue_snapshot import build_snapshot
except ImportError:
    from v53_monthly_revenue_snapshot import build_snapshot


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).strip().replace(',', ''))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def roc_date(value):
    value = str(value).strip().replace('/', '').replace('-', '')
    try:
        return datetime(int(value[:-4])+1911, int(value[-4:-2]), int(value[-2:])).date().isoformat()
    except (ValueError, TypeError):
        return None


def import_official_rows(market, period, rows, *, fetched_at, source, store=None):
    """Rows must be genuine target-month t187ap05 data, never derived values."""
    records, dates = [], set()
    if not isinstance(rows, list) or not rows:
        raise ValueError('empty_rows')
    for raw in rows:
        value = str(raw.get('資料年月', '')).strip()
        if not value.isdigit() or len(value) not in (5, 6):
            raise ValueError('invalid_row_period')
        actual = f'{int(value[:-2])+1911:04d}-{int(value[-2:]):02d}'
        if actual != period:
            raise ValueError('target_period_mismatch')
        required = {'公司代號', '營業收入-當月營收', '營業收入-去年同月增減(%)'}
        if not required <= raw.keys():
            raise ValueError('unsupported_official_schema')
        date = roc_date(raw.get('出表日期'))
        if date:
            dates.add(date)
        pct = number(raw.get('營業收入-去年同月增減(%)'))
        records.append(dict(market=market, period=period, code=str(raw['公司代號']).strip(),
                            company_name=raw.get('公司名稱'), source_date=date, unit='TWD_thousand',
                            monthly_revenue=number(raw.get('營業收入-當月營收')),
                            previous_month_revenue=number(raw.get('營業收入-上月營收')),
                            previous_year_same_month_revenue=number(raw.get('營業收入-去年當月營收')),
                            monthly_revenue_yoy_pct=pct, monthly_revenue_yoy_ratio=pct/100 if pct is not None else None,
                            raw_row=raw))
    snapshot = build_snapshot(market, period, records, source=source,
                              source_date=next(iter(dates)) if len(dates)==1 else None,
                              fetched_at=fetched_at, raw_rows=rows, raw_metadata={'source_dates': sorted(dates)})
    if store is not None:
        store.save(snapshot)
    return snapshot


def import_layer_snapshot(market, period, layer_snapshot, store=None):
    """Adapter for existing layer snapshot; preserves original fetched_at."""
    return import_official_rows(market, period, layer_snapshot['raw_rows'],
                                fetched_at=layer_snapshot['fetched_at'],
                                source=layer_snapshot.get('source', layer_snapshot.get('source_id', 'official_layer_snapshot')), store=store)


class _TableRows(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows=[]; self.row=[]; self.cell=None
    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.row=[]
        if tag in ('td','th'): self.cell=[]
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(''.join(self.cell).strip()); self.cell=None
        if tag == 'tr' and self.row: self.rows.append(self.row); self.row=[]


def parse_historical_html(body, period):
    """Strict known MOPS table layout; no positional guessing on changed schema."""
    try: text=body.decode('utf-8')
    except UnicodeDecodeError: text=body.decode('cp950')
    year,month=map(int,period.split('-'))
    if not re.search(rf'{year-1911}年\s*{month}月', text):
        raise ValueError('historical_page_period_unverified')
    parser=_TableRows(); parser.feed(text)
    headings=' '.join(' '.join(r) for r in parser.rows)
    if not all(x in headings for x in ('公司代號','當月營收','去年同月','去年累計')):
        raise ValueError('historical_html_schema_unverified')
    expected=['公司代號','公司名稱','當月營收','上月營收','去年當月營收','上月比較增減(%)','去年同月增減(%)','當月累計營收','去年累計營收','前期比較增減(%)']
    result=[]
    verified_header=False
    for r in parser.rows:
        if '公司代號' in r:
            if r != expected: raise ValueError('historical_column_order_changed')
            verified_header=True
            continue
        if not r or not re.fullmatch(r'[0-9]{4,6}',r[0]): continue
        if not verified_header or len(r)!=11: raise ValueError('historical_row_schema_changed')
        result.append(dict(zip(['公司代號','公司名稱','營業收入-當月營收','營業收入-上月營收',
                                '營業收入-去年當月營收','營業收入-上月比較增減(%)',
                                '營業收入-去年同月增減(%)','累計營業收入-當月累計營收',
                                '累計營業收入-去年累計營收','累計營業收入-前期比較增減(%)','備註'],r),
                           資料年月=f'{year-1911}{month:02d}'))
    if not result: raise ValueError('no_verified_historical_rows')
    return result


def fetch_historical_month(market, period, *, opener=urlopen, timeout=25, store=None):
    """Fetch both domestic and foreign-company MOPS pages, once each, no retry.

    Both pages must validate before a snapshot is saved. Missing source_date is
    left null: retrieval time is not an official publication date.
    """
    if market not in ('TWSE', 'TPEX'):
        raise ValueError('invalid_market')
    year, month = map(int, period.split('-'))
    if not 1 <= month <= 12 or year < 1912:
        raise ValueError('invalid_period')
    board = 'sii' if market == 'TWSE' else 'otc'
    urls = [f'https://mopsov.twse.com.tw/nas/t21/{board}/t21sc03_{year-1911}_{month}_{part}.html' for part in (0, 1)]
    requests, rows = [], []
    for url in urls:
        try:
            with opener(url, timeout=timeout) as response:
                status = response.status
                body = response.read()
            page_rows = parse_historical_html(body, period)
            rows.extend(page_rows)
            requests.append(dict(source=url, http_status=status, row_count=len(page_rows)))
        except Exception as exc:
            requests.append(dict(source=url, http_status=getattr(exc, 'code', None), reason=type(exc).__name__))
            return dict(status='historical_snapshot_unavailable', snapshot=None, requests=requests,
                        reason=type(exc).__name__, detail='Both official historical pages must validate; no reconstruction performed')
    try:
        snapshot = import_official_rows(market, period, rows, fetched_at=datetime.now(timezone.utc).isoformat(),
                                       source={'institution': market, 'endpoints': urls, 'dataset': 'MOPS_t21sc03_history'}, store=store)
        return dict(status='downloaded', snapshot=snapshot, requests=requests)
    except Exception as exc:
        return dict(status='historical_snapshot_unavailable', snapshot=None, requests=requests, reason=type(exc).__name__)
