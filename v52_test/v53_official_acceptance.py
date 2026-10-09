"""Opt-in diagnostics only. No scan, Yahoo, Quality, or universe downloader imports.

Upload the previously accepted universe CSV; never fetch/replace the denominator.
Optional evidence JSON: a list of {market, code, listing_date, reference_url,
field, category, confirmed, explanation}. Categories are documented in CATEGORIES.
Evidence is user-supplied; URLs are recorded, not fetched or independently verified.
"""
import csv
import hashlib
import io
import json
import math
import platform
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SAMPLES = [('TWSE', '2330'), ('TWSE', '2317'), ('TWSE', '2308'),
           ('TPEX', '6488'), ('TPEX', '3293'), ('TWSE', '2881'),
           ('TWSE', '2850'), ('TPEX', '6015')]
FIELDS = {'pe': ('pe', 'official_pe'), 'eps': ('eps_summary', 'official_basic_eps_ytd'),
          'monthly_revenue': ('monthly_revenue', 'revenue_current_month'),
          'monthly_revenue_yoy': ('monthly_revenue', 'monthly_revenue_yoy_pct')}
CATEGORIES = ['新上市／歷史不足', 'P/E 不適用待確認', '金融／保險／證券格式差異',
              '官方未提供／原因未確認', '股票池與官方代號不一致', '處理問題']
OFFICIAL_HOSTS = ('twse.com.tw', 'tpex.org.tw')


def _layer():
    # Lazy import keeps upload validation independent of the data layer.
    try:
        from . import v53_official_fundamentals as layer
    except ImportError:
        import v53_official_fundamentals as layer
    return layer


def _market(value):
    return {'TWSE': 'TWSE', 'TPEX': 'TPEX', '上市': 'TWSE', '上櫃': 'TPEX',
            'TW': 'TWSE', 'TWO': 'TPEX'}.get(str(value or '').strip().upper())


def parse_universe(data):
    rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
    if not rows:
        raise ValueError('股票池 CSV 為空')
    output, errors, seen, duplicates = [], [], set(), []
    for number, raw in enumerate(rows, 2):
        ticker = str(raw.get('Ticker') or raw.get('ticker') or '').strip().upper()
        code = str(raw.get('code') or raw.get('公司代號') or '').strip()
        suffix_market = 'TPEX' if ticker.endswith('.TWO') else 'TWSE' if ticker.endswith('.TW') else None
        ticker_code = ticker.rsplit('.', 1)[0] if suffix_market else ticker
        label = raw.get('market') or raw.get('上市櫃') or ''
        market = _market(label) or suffix_market
        if (label and _market(label) is None) or (suffix_market and market != suffix_market):
            errors.append({'line': number, 'reason': 'market_mismatch', 'raw': raw});continue
        if code and ticker_code and code != ticker_code:
            errors.append({'line': number, 'reason': 'code_mismatch', 'raw': raw});continue
        code = code or ticker_code
        if not market or not code or not code.isalnum():
            errors.append({'line': number, 'reason': 'unknown_market_or_code', 'raw': raw});continue
        key = (market, code)
        if key in seen:
            duplicates.append({'line': number, 'market': market, 'code': code});continue
        seen.add(key)
        output.append({'market': market, 'code': code,
                       'company_name': raw.get('company_name') or raw.get('公司名稱') or raw.get('Company Name') or '',
                       'universe_date': raw.get('清單日期') or raw.get('source_date') or '', 'raw': raw})
    return {'sha256': hashlib.sha256(data).hexdigest(), 'raw_count': len(rows),
            'unique_count': len(output), 'rows': output, 'errors': errors, 'duplicates': duplicates,
            'valid_denominator': len(output) == 1988 and not errors and not duplicates}


def parse_evidence(data):
    if not data:
        return []
    items = json.loads(data.decode('utf-8-sig'))
    if not isinstance(items, list):
        raise ValueError('證據 JSON 必須為陣列')
    for item in items:
        if not isinstance(item, dict) or _market(item.get('market')) is None or not isinstance(item.get('code'), str):
            raise ValueError('證據需有 market 及字串 code')
        host = (urlparse(str(item.get('reference_url', ''))).hostname or '').lower()
        if not any(host == h or host.endswith('.' + h) for h in OFFICIAL_HOSTS):
            raise ValueError('證據 reference_url 必須為 TWSE／TPEx 官方網址')
        if item.get('category') and item['category'] not in CATEGORIES:
            raise ValueError('未知缺失分類')
    return items


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def source_summary(result):
    output = []
    for sid, source in result.get('sources', {}).items():
        records = list(source.get('records', {}).values())
        output.append({k: source.get(k) for k in ('source_id', 'status', 'http_status', 'http_request_count',
                                                  'elapsed_ms', 'row_count', 'normalized_count', 'fetched_at', 'error')} |
                      {'source_id': sid, 'source_dates': sorted({r['source_date'] for r in records}),
                       'periods': [json.loads(p) for p in sorted({json.dumps(r['period'], sort_keys=True) for r in records})],
                       'warnings': source.get('warnings', [])})
    return output


def _raw_value_is_malformed(raw, key):
    aliases = {'official_pe': ('PEratio', 'PriceEarningRatio'),
               'official_basic_eps_ytd': ('基本每股盈餘(元)', '基本每股盈餘'),
               'revenue_current_month': ('營業收入-當月營收',),
               'monthly_revenue_yoy_pct': ('營業收入-去年同月增減(%)',)}
    value = next((raw[k] for k in aliases[key] if k in raw), None)
    return value is not None and str(value).strip() not in ('', '--', '-', 'N/A')


def _field_audit(source, code, dataset, value_key, evidence):
    record = source.get('records', {}).get(code)
    raw_match = next((r for r in source.get('raw_rows', []) if isinstance(r, dict) and
                      str(r.get('Code', r.get('SecuritiesCompanyCode', r.get('公司代號', '')))).strip() == code), None)
    value = record.get('values', {}).get(value_key) if record else None
    status = source.get('status', 'unavailable')
    usable = status in ('downloaded', 'fresh_cache', 'stale_fallback') and _finite(value)
    if value_key == 'official_pe':
        usable = usable and value > 0
    result = {'official_row_present': raw_match is not None if status != 'unavailable' else None,
              'normalized_row_present': record is not None, 'value': value, 'has_value': usable,
              'source_status': status, 'source_date': record.get('source_date') if record else None,
              'period': record.get('period') if record else None,
              'fetched_at': source.get('fetched_at'), 'unit': record.get('unit', {}).get(value_key) if record else None,
              'category': None, 'reason': None, 'reason_confirmed': False, 'evidence': [],
              'warnings': source.get('warnings', [])}
    if usable:
        return result
    if status == 'unavailable':
        result.update(category='處理問題', reason={'error': source.get('error'), 'warnings': source.get('warnings', [])}, reason_confirmed=True)
    elif raw_match is not None and record is None:
        result.update(category='處理問題', reason='schema/date/duplicate：原始列存在但未標準化', reason_confirmed=True)
    elif record and value is None and _raw_value_is_malformed(raw_match, value_key):
        result.update(category='處理問題', reason='parse：原始值非一般空值但無法解析', reason_confirmed=True)
    elif value_key == 'official_pe' and raw_match is not None:
        result.update(category='P/E 不適用待確認', reason='P/E 缺值或非正；未以累計 EPS 推定近四季分母')
    else:
        result.update(category='官方未提供／原因未確認', reason='官方快照無匹配列或欄位缺值')
    for item in evidence:
        if item.get('confirmed') is True and item.get('category') and item.get('explanation'):
            # Do not hide actual transport/parser failures with business explanations.
            if result['category'] != '處理問題':
                result.update(category=item['category'], reason=item['explanation'], reason_confirmed=True)
            result['evidence'].append(item)
    return result


def coverage_audit(universe, result, evidence=None, *, today=None):
    evidence = evidence or []
    today = today or date.today()
    try:
        cutoff = today.replace(year=today.year - 1)
    except ValueError:
        cutoff = today.replace(year=today.year - 1, day=28)
    rows = []
    for stock in universe['rows']:
        market, code = stock['market'], stock['code']
        prefix = 'twse' if market == 'TWSE' else 'tpex'
        row = {k: stock[k] for k in ('market', 'code', 'company_name')}
        row['fields'] = {}
        for name, (dataset, key) in FIELDS.items():
            sid = prefix + '_' + ('eps' if dataset == 'eps_summary' else dataset)
            relevant = [e for e in evidence if _market(e.get('market')) == market and e.get('code') == code and e.get('field') == name]
            row['fields'][name] = _field_audit(result.get('sources', {}).get(sid, {}), code, dataset, key, relevant)
        row['available_three'] = sum(row['fields'][key]['has_value'] for key in ('pe', 'eps', 'monthly_revenue'))
        rows.append(row)
    summary = {}
    for market in ('TWSE', 'TPEX'):
        group = [r for r in rows if r['market'] == market]
        counts = {'unique_count': len(group)}
        for name in FIELDS:
            yes = sum(r['fields'][name]['has_value'] for r in group)
            counts[name] = {'with_value': yes, 'without_value': len(group) - yes,
                            'coverage_pct': yes / len(group) * 100 if group else None}
        counts.update(all_three=sum(r['available_three'] == 3 for r in group),
                      partial=sum(r['available_three'] in (1, 2) for r in group),
                      none=sum(r['available_three'] == 0 for r in group))
        counts['missing_categories'] = {name: {category: sum(r['fields'][name]['category'] == category for r in group)
                                              for category in CATEGORIES} for name in FIELDS}
        summary[market] = counts
    by_key = {(r['market'], r['code']): r for r in rows}
    recent = []
    for item in evidence:
        try:
            listed = date.fromisoformat(item.get('listing_date', ''))
        except (ValueError, TypeError):
            continue
        key = (_market(item.get('market')), item.get('code'))
        if item.get('confirmed') is True and cutoff <= listed <= today and key in by_key and key not in [(r['market'], r['code']) for r in recent]:
            recent.append(dict(by_key[key], listing_evidence=item))
    selected_recent = []
    for market in ('TWSE', 'TPEX'):
        candidate = next((r for r in recent if r['market'] == market), None)
        if candidate:
            selected_recent.append(candidate)
    for candidate in recent:
        if len(selected_recent) >= 3:
            break
        if candidate not in selected_recent:
            selected_recent.append(candidate)
    return {'summary': summary, 'rows': rows,
            'samples': [{'market': m, 'code': c, 'result': by_key.get((m, c))} for m, c in SAMPLES],
            'recent_listing_samples': selected_recent,
            'recent_listing_check': 'PASS' if len(recent) >= 3 and len({r['market'] for r in recent}) == 2 else 'INCOMPLETE',
            'evidence_note': '官方網址及原因由使用者提供；本工具未連線獨立核實證據內容。'}


def run_acceptance(universe, evidence=None):
    if not universe['valid_denominator']:
        raise ValueError('固定股票池驗證失敗：需唯一 1988 檔、無重複、無市場／代號錯誤')
    layer = _layer()
    run_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='v53_acceptance_') as directory:
        started = time.monotonic()
        first = layer.refresh_official_fundamentals(cache_dir=directory)
        first_ms = (time.monotonic() - started) * 1000
        started = time.monotonic()
        second = layer.refresh_official_fundamentals(cache_dir=directory)
        second_ms = (time.monotonic() - started) * 1000
    sources = first.get('sources', {})
    cold_ok = len(sources) == 6 and first.get('http_request_count') == 6 and all(
        s.get('status') == 'downloaded' and s.get('http_status') == 200 for s in sources.values())
    second_sources = second.get('sources', {})
    fresh_ok = len(second_sources) == 6 and second.get('http_request_count') == 0 and all(
        s.get('status') == 'fresh_cache' for s in second_sources.values())
    audit = coverage_audit(universe, first, evidence, today=datetime.now(ZoneInfo('Asia/Taipei')).date())
    hashes = {}
    for filename in ('v53_official_fundamentals.py', 'v53_fundamental_normalize.py', 'v53_fundamental_snapshot.py'):
        p = Path(layer.__file__).with_name(filename)
        hashes[filename] = hashlib.sha256(p.read_bytes()).hexdigest()
    report = {'run_id': run_id, 'executed_at': datetime.now(timezone.utc).isoformat(),
              'python': platform.python_version(), 'module_sha256': hashes,
              'universe': {k: v for k, v in universe.items() if k != 'rows'},
              'first': {'http_total': first.get('http_request_count'), 'wall_elapsed_ms': first_ms, 'sources': source_summary(first)},
              'second': {'http_total': second.get('http_request_count'), 'wall_elapsed_ms': second_ms,
                         'all_fresh_cache': fresh_ok, 'sources': source_summary(second)},
              'cloud_connectivity': 'PASS' if cold_ok and fresh_ok else 'FAIL',
              'coverage': audit, 'quality_integration': 'NOT_EVALUATED_NOT_CONNECTED'}
    json.dumps(report, allow_nan=False)
    return report


def csv_bytes(report):
    out = io.StringIO()
    names = ['market', 'code', 'company_name', 'field', 'official_row_present', 'normalized_row_present',
             'value', 'has_value', 'source_status', 'source_date', 'period', 'fetched_at', 'unit',
             'category', 'reason', 'reason_confirmed', 'evidence', 'warnings']
    writer = csv.DictWriter(out, fieldnames=names);writer.writeheader()
    for row in report['coverage']['rows']:
        for field, detail in row['fields'].items():
            record = {k: row[k] for k in ('market', 'code', 'company_name')}
            record.update(field=field, **detail)
            for key, value in record.items():
                if isinstance(value, (dict, list)):
                    record[key] = json.dumps(value, ensure_ascii=False, allow_nan=False)
                elif isinstance(value, str) and value.startswith(('=', '+', '-', '@')):
                    record[key] = "'" + value
            writer.writerow(record)
    return out.getvalue().encode('utf-8-sig')


def main():
    import streamlit as st
    st.title('V5.3 官方基本面 Cloud & Coverage 驗收')
    st.caption('獨立診斷模式；不會進入掃描。關閉本頁或移除網址的 v53_official_audit 參數即可返回。')
    if st.query_params.get('scan_task') or st.session_state.get('v52_auto_token'):
        st.error('本工作階段有掃描任務識別碼。請先完成／暫停掃描，再以無 scan_task 的新工作階段開啟診斷。')
        return
    st.warning('請先確認其他分頁也沒有正在執行 Auto Scan。本頁不會停止背景掃描。')
    uploaded = st.file_uploader('上傳已驗收的 tw_universe_v52.csv（固定 1,988 檔）', type=['csv'])
    evidence_file = st.file_uploader('選填：官方掛牌日期／缺失原因證據 JSON', type=['json'])
    if uploaded is None:
        st.info('不自動下載股票池。沒有原驗收清單時，完整 coverage 無法通過。');return
    try:
        universe = parse_universe(uploaded.getvalue())
        evidence = parse_evidence(evidence_file.getvalue() if evidence_file else None)
    except Exception as error:
        st.error(f'輸入錯誤：{error}');return
    st.json({k: v for k, v in universe.items() if k != 'rows'})
    confirmed = st.checkbox('我確認這是已驗收的 1,988 檔原股票池快照，且目前沒有正在執行的掃描', value=False)
    if st.button('執行一次 Cloud 連線與 Coverage 驗收', disabled=not (confirmed and universe['valid_denominator'])):
        try:
            with st.spinner('執行六來源冷啟動、第二輪快取及固定股票池比對…'):
                report = run_acceptance(universe, evidence)
            st.session_state['v53_acceptance_report'] = report
            log = {k: v for k, v in report.items() if k != 'coverage'}
            log['coverage'] = {k: v for k, v in report['coverage'].items() if k != 'rows'}
            print('V53_OFFICIAL_AUDIT ' + json.dumps(log, ensure_ascii=False, allow_nan=False), flush=True)
        except Exception as error:
            st.error(f'驗收未完成：{type(error).__name__}: {error}');return
    report = st.session_state.get('v53_acceptance_report')
    if report:
        if report['universe']['sha256'] != universe['sha256']:
            st.warning('顯示的是上一輪報告，股票池雜湊與目前上傳檔不同，請重新驗收。')
        st.write('連線驗收：', report['cloud_connectivity'])
        st.json({k: report[k] for k in ('run_id', 'executed_at', 'first', 'second')})
        st.json(report['coverage']['summary'])
        st.write('近期掛牌抽查：', report['coverage']['recent_listing_check'])
        st.json(report['coverage']['samples'])
        st.json(report['coverage']['recent_listing_samples'])
        st.download_button('下載完整 JSON 報告', json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2),
                           f"v53_official_audit_{report['run_id']}.json", 'application/json')
        st.download_button('下載逐欄位 Coverage CSV', csv_bytes(report),
                           f"v53_official_coverage_{report['run_id']}.csv", 'text/csv')


if __name__ == '__main__':
    main()
