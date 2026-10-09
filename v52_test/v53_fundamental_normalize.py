"""Official fundamentals normalization only; no model or Yahoo dependencies."""
from datetime import date
import math
import re

SOURCES = {
    'twse_pe': ('TWSE', 'pe', 'https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL'),
    'tpex_pe': ('TPEX', 'pe', 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis'),
    'twse_eps': ('TWSE', 'eps_summary', 'https://openapi.twse.com.tw/v1/opendata/t187ap14_L'),
    'tpex_eps': ('TPEX', 'eps_summary', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap14_O'),
    'twse_monthly_revenue': ('TWSE', 'monthly_revenue', 'https://openapi.twse.com.tw/v1/opendata/t187ap05_L'),
    'tpex_monthly_revenue': ('TPEX', 'monthly_revenue', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O'),
}


def parse_number(value):
    """Preserve finite zero/negative numbers; never coerce missing values to zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(str(value).strip().replace(',', ''))
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_roc_date(value):
    text = str(value).strip()
    match = re.fullmatch(r'(\d{2,3})(\d{2})(\d{2})', text)
    if not match:
        match = re.fullmatch(r'(\d{1,3})[/-](\d{1,2})[/-](\d{1,2})', text)
    try:
        return date(int(match[1]) + 1911, int(match[2]), int(match[3])).isoformat() if match else None
    except ValueError:
        return None


def parse_roc_period(year, quarter=None, month=None):
    """A compact ROC YYYYMM-like value (e.g. 11508) is accepted for month periods."""
    try:
        text = str(year).strip()
        if quarter is None and month is None:
            match = re.fullmatch(r'(\d{2,3})(\d{2})', text)
            if match:
                text, month = match.groups()
        if not re.fullmatch(r'\d{1,3}', text):
            return None
        roc = int(text)
        if roc < 1 or (quarter is not None and month is not None):
            return None
        result = {'fiscal_year_roc': roc, 'fiscal_year': roc + 1911}
        if quarter is not None:
            q = str(quarter).strip()
            if q not in ('1', '2', '3', '4'):
                return None
            result.update(quarter=int(q), basis='cumulative_ytd')
        elif month is not None:
            m = str(month).strip()
            if not m.isdigit() or not 1 <= int(m) <= 12:
                return None
            result.update(month=int(m), year_month=f'{roc + 1911:04d}-{int(m):02d}')
        return result
    except (ValueError, TypeError):
        return None


def json_safe(value):
    """Retain raw text; represent raw numeric nonfinite values as null for valid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def normalize_source(source_id, raw_rows):
    market, dataset, endpoint = SOURCES[source_id]
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError('empty_or_invalid_dataset')
    otc = market == 'TPEX'
    if dataset == 'pe':
        code_key, name_key = ('SecuritiesCompanyCode', 'CompanyName') if otc else ('Code', 'Name')
        date_key = 'Date'
        fields = {'official_pe': 'PriceEarningRatio' if otc else 'PEratio'}
    elif dataset == 'eps_summary':
        code_key, name_key = ('SecuritiesCompanyCode', 'CompanyName') if otc else ('公司代號', '公司名稱')
        date_key = 'Date' if otc else '出表日期'
        fields = {'official_basic_eps_ytd': '基本每股盈餘' if otc else '基本每股盈餘(元)'}
    else:
        code_key, name_key, date_key = '公司代號', '公司名稱', '出表日期'
        fields = {
            'revenue_current_month': '營業收入-當月營收',
            'revenue_previous_month': '營業收入-上月營收',
            'revenue_same_month_last_year': '營業收入-去年當月營收',
            'monthly_revenue_yoy_pct': '營業收入-去年同月增減(%)',
            'revenue_cumulative': '累計營業收入-當月累計營收',
            'revenue_cumulative_last_year': '累計營業收入-去年累計營收',
            'cumulative_revenue_yoy_pct': '累計營業收入-前期比較增減(%)',
        }
    required = {code_key, date_key, *fields.values()}
    if dataset == 'eps_summary':
        required.update({'Year' if otc else '年度', '季別'})
    elif dataset == 'monthly_revenue':
        required.add('資料年月')
    if not any(isinstance(r, dict) and required <= r.keys() for r in raw_rows):
        raise ValueError('required_schema_missing')
    records, rejected, warnings, duplicates = {}, [], [], set()
    for index, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            rejected.append({'row_index': index, 'reason': 'not_object'})
            continue
        code = row.get(code_key)
        code = str(code).strip() if code is not None else ''
        source_date = parse_roc_date(row.get(date_key))
        if dataset == 'eps_summary':
            period = parse_roc_period(row.get('Year' if otc else '年度'), quarter=row.get('季別'))
            if period and 'quarter' not in period:
                period = None
        elif dataset == 'monthly_revenue':
            period = parse_roc_period(row.get('資料年月'))
            if period and 'month' not in period:
                period = None
        else:
            period = {'trading_date': source_date} if source_date else None
        if not re.fullmatch(r'[0-9A-Za-z]+', code) or not source_date or not period:
            rejected.append({'row_index': index, 'code': code, 'reason': 'invalid_identity_date_or_period'})
            continue
        if code in records or code in duplicates:
            records.pop(code, None)
            duplicates.add(code)
            rejected.append({'row_index': index, 'code': code, 'reason': 'duplicate_code_quarantined'})
            continue
        values = {key: parse_number(row.get(raw)) for key, raw in fields.items()}
        missing = {key: 'missing_or_unparseable_nonfinite' for key, value in values.items() if value is None}
        if dataset == 'monthly_revenue':
            pct = values['monthly_revenue_yoy_pct']
            values['monthly_revenue_yoy_ratio'] = pct / 100 if pct is not None else None
        unit = ({'official_pe': 'multiple'} if dataset == 'pe' else
                {'official_basic_eps_ytd': 'TWD/share'} if dataset == 'eps_summary' else
                {key: 'ratio' if key.endswith('_ratio') else 'percent' if key.endswith('_pct') else 'TWD_thousand' for key in values})
        records[code] = {
            'market': market, 'code': code, 'company_name': row.get(name_key),
            'source': {'institution': market, 'source_id': source_id, 'endpoint': endpoint},
            'source_date': source_date,
            'source_date_kind': 'trading_date' if dataset == 'pe' else 'report_generated_date',
            'period': period, 'unit': unit, 'fetched_at': None, 'published_at': None,
            'status': 'partial' if missing else 'valid', 'values': values,
            'raw_row': json_safe(row),
            'raw_metadata': {'source_date': row.get(date_key), 'note': row.get('備註'), 'industry': row.get('產業別')},
            'missing_reasons': missing, 'warnings': [],
        }
    if not records:
        raise ValueError('no_usable_records')
    if rejected:
        warnings.append({'type': 'quarantined_rows', 'count': len(rejected)})
    return {'records': records, 'row_count': len(raw_rows), 'normalized_count': len(records),
            'raw_rows': json_safe(raw_rows), 'rejected_rows': rejected, 'warnings': warnings}
