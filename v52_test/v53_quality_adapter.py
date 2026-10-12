"""Quality-only official inputs. No Yahoo, scanning or backtest imports.

V53_UNIVERSE_CSV points to the previously accepted fixed universe CSV.
Default search: beside this module, then repository root. Context failures
are persisted as null per run; resuming never silently downloads a replacement.
"""
import copy
import csv
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from statistics import median


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def build_context(root, universe_path=None, now=None):
    from v53_official_fundamentals import refresh_official_fundamentals
    from v53_monthly_revenue_snapshot import SnapshotStore
    from v53_monthly_revenue_history import import_layer_snapshot, fetch_historical_month
    from v53_monthly_revenue_common import select_common_complete_month, universe_codes
    now = now or datetime.now(ZoneInfo('Asia/Taipei'))
    supplied = universe_path or os.environ.get('V53_UNIVERSE_CSV')
    candidates = [Path(supplied)] if supplied else [Path(__file__).with_name('tw_universe_v52.csv'), Path(__file__).parent.parent / 'tw_universe_v52.csv']
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise ValueError('fixed_universe_snapshot_missing')
    data = path.read_bytes()
    rows = list(csv.DictReader(data.decode('utf-8-sig').splitlines()))
    pools = universe_codes(rows)
    if len(rows) != 1988 or len(pools['TWSE']) != 1095 or len(pools['TPEX']) != 893:
        raise ValueError('fixed_universe_denominator_mismatch')
    # Pin the accepted CSV, never substitute a freshly fetched or scanned subset.
    expected = 'a45287b792d58b7106d0f81a0339230bf1844d8a6e0e39d506e5c84c1a1b5352'
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('fixed_universe_sha256_mismatch')
    root = Path(root)
    official = refresh_official_fundamentals(cache_dir=root / 'official')
    archive = SnapshotStore(root / 'monthly')
    issues = []
    for source_id, source in official['sources'].items():
        if source.get('dataset') != 'monthly_revenue' or source.get('status') == 'unavailable':
            continue
        periods = {r['period']['year_month'] for r in source.get('records', {}).values()}
        for period in periods:
            try:
                part = dict(source, raw_rows=[r for r in source['raw_rows'] if str(r.get('資料年月')) == f'{int(period[:4])-1911}{period[-2:]}'])
                import_layer_snapshot(source['market'], period, part, archive)
            except Exception as exc:
                issues.append(type(exc).__name__)
    stored = archive.load_all()
    selection = select_common_complete_month(stored, rows, now=now)
    # At most two allowed past months, once per missing market/month; no retry.
    if selection['status'] != 'available':
        known = {(s['market'], s['period']) for s in stored}
        for lag in (1, 2):
            ordinal = now.year * 12 + now.month - 1 - lag
            year, month = divmod(ordinal, 12)
            period = f'{year:04d}-{month+1:02d}'
            for market in ('TWSE', 'TPEX'):
                if (market, period) not in known:
                    result = fetch_historical_month(market, period, store=archive)
                    issues.append({'market': market, 'period': period, 'status': result['status']})
            selection = select_common_complete_month(archive.load_all(), rows, now=now)
            if selection['status'] == 'available':
                break
    context = dict(schema_version=1, as_of=now.isoformat(), official=official, selection=selection,
                   selected_period=selection.get('selected_period'), universe_sha256=expected,
                   coverage=selection['audits'], issues=issues,
                   monthly_snapshot_versions={m:s['snapshot_version'] for m,s in selection['selected_snapshots'].items()},
                   pe_snapshot_versions={k:digest(v) for k,v in official['sources'].items() if v.get('dataset')=='pe'})
    context['context_version'] = digest(context)
    return context


def context_for_run(run_id, root, *, create=False, builder=None):
    """Atomic sidecar independent of scan checkpoints. Never retry a failed build."""
    root = Path(root)
    key = hashlib.sha256(str(run_id).encode()).hexdigest()
    path = root / 'contexts' / (key + '.json')
    claim = path.with_suffix('.claim')
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding='utf-8'))
            if payload.get('sha256') != digest(payload.get('context')):
                return None
            return payload['context']
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return None
        try:
            context = builder() if builder is not None else build_context(root)
        except Exception:
            context = None
        payload = dict(context=context, sha256=digest(context))
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, allow_nan=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp): os.unlink(temp)
        return context
    except Exception:
        return None


def prepare_quality_inputs(rows, context, cfg):
    """Return separate per-ticker rows and metadata; original rows stay untouched."""
    from v53_monthly_revenue_common import get_monthly_record
    result = {}
    selection = context.get('selection', {})
    as_of = datetime.fromisoformat(context['as_of'])
    period = selection.get('selected_period')
    monthly_ok = False
    try:
        year, month = map(int, period.split('-'))
        lag = (as_of.year-year)*12 + as_of.month-month
        audits = {a['market']:a for a in selection['audits'] if a['period']==period}
        monthly_ok = (selection['status']=='available' and 1 <= lag <= 2 and
                      selection.get('monthly_revenue_yoy_eligible') is True and
                      all(audits[m]['revenue_coverage'] >= .98 and audits[m]['yoy_coverage'] >= .98
                          and audits[m]['revenue_coverage_pass'] and audits[m]['yoy_coverage_pass'] for m in ('TWSE','TPEX')))
    except (KeyError, TypeError, ValueError):
        pass
    for row in rows:
        ticker = row['Ticker']
        market = 'TPEX' if ticker.endswith('.TWO') else 'TWSE' if ticker.endswith('.TW') else None
        if market is None: continue
        source = context.get('official', {}).get('sources', {}).get('tpex_pe' if market=='TPEX' else 'twse_pe', {})
        record = source.get('records', {}).get(ticker.rsplit('.',1)[0])
        pe = None; reason = 'official_source_unavailable'
        if source.get('status') in ('downloaded','fresh_cache','stale_fallback'):
            reason = 'official_row_missing'
            if record:
                reason = 'official_not_provided_or_not_applicable'
                value = record.get('values',{}).get('official_pe')
                try:
                    d = date.fromisoformat(record['source_date'])
                    age = (as_of.date()-d).days
                    fetched = datetime.fromisoformat(source['fetched_at'])
                    if 0 <= age <= 7 and 0 <= (as_of-fetched).total_seconds() <= 7*86400:
                        if finite(value) and value > 0:
                            pe=value; reason='official_valid'
                    else: reason='official_date_stale_or_invalid'
                except (KeyError, ValueError, TypeError): reason='official_date_invalid'
        monthly = get_monthly_record(selection, ticker) if monthly_ok else {}
        ratio = monthly.get('eligible_monthly_revenue_yoy_ratio')
        if monthly.get('monthly_revenue_yoy_eligible') is not True or not finite(ratio): ratio=None
        q = dict(row)
        q.update({'P/E':pe,'Peer PE Median':None,'Monthly Revenue YoY':ratio})
        metadata = dict(quality_pe=pe, quality_pe_source=copy.deepcopy(source.get('source')),
                        quality_pe_trading_date=record.get('source_date') if record else None,
                        quality_pe_status=reason, quality_peer_sample_count=0,
                        quality_peer_pe_median=None, quality_monthly_revenue_yoy=ratio,
                        quality_monthly_period=period, quality_monthly_status='eligible' if ratio is not None else 'N/A',
                        official_quality_context_version=context.get('context_version'))
        result[ticker] = (q, metadata)
    groups = {}
    for ticker,(q,meta) in result.items():
        industry=q.get('Industry')
        if industry is None or industry in ('','其他分類','示範','N/A') or (isinstance(industry,float) and math.isnan(industry)):continue
        groups.setdefault((q.get('Market'),industry),[]).append(ticker)
    for tickers in groups.values():
        dates={result[t][1]['quality_pe_trading_date'] for t in tickers if result[t][1]['quality_pe'] is not None}
        values=[result[t][1]['quality_pe'] for t in tickers if result[t][1]['quality_pe'] is not None]
        reference=median(values) if len(dates)==1 and len(values)>=cfg.min_peers else None
        for t in tickers:
            result[t][0]['Peer PE Median']=reference
            result[t][1].update(quality_peer_sample_count=len(values),quality_peer_pe_median=reference)
    return result
