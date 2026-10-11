"""Pure coverage and common-month selection; never fills another period."""
import hashlib
import math
from datetime import datetime
from zoneinfo import ZoneInfo
try:
    from .v53_monthly_revenue_snapshot import canonical, verify
except ImportError:
    from v53_monthly_revenue_snapshot import canonical, verify


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def universe_codes(universe):
    result = {'TWSE': set(), 'TPEX': set()}
    for row in universe:
        ticker = str(row.get('Ticker', '')).strip()
        suffix = '.TWO' if ticker.endswith('.TWO') else '.TW' if ticker.endswith('.TW') else None
        if suffix is None or not ticker[:-len(suffix)]:
            raise ValueError('invalid_universe_ticker')
        result['TPEX' if suffix == '.TWO' else 'TWSE'].add(ticker[:-len(suffix)])
    if not all(result.values()):
        raise ValueError('empty_market_denominator')
    return result


def audit_month(snapshot, universe, revenue_coverage_threshold=0.98, yoy_coverage_threshold=0.98):
    verify(snapshot)
    pools = universe_codes(universe)
    codes = pools[snapshot['market']]
    records, conflicts = {}, set()
    for row in snapshot['records']:
        code = row['code']
        if code in records and canonical(records[code]) != canonical(row):
            conflicts.add(code)
        records[code] = row
    for code in conflicts:
        records.pop(code, None)
    revenue = sum(finite(records.get(c, {}).get('monthly_revenue')) for c in codes)
    yoy = sum(finite(records.get(c, {}).get('monthly_revenue_yoy_pct')) for c in codes)
    denominator = len(codes)
    return dict(market=snapshot['market'], period=snapshot['period'], denominator=denominator,
                revenue_valid_count=revenue, yoy_valid_count=yoy,
                revenue_coverage=revenue / denominator, yoy_coverage=yoy / denominator,
                revenue_coverage_pass=not conflicts and revenue / denominator >= revenue_coverage_threshold,
                yoy_coverage_pass=not conflicts and yoy / denominator >= yoy_coverage_threshold,
                duplicate_conflicts=sorted(conflicts), matched_row_count=len(codes & records.keys()),
                snapshot_version=snapshot['snapshot_version'],
                universe_sha256=hashlib.sha256(canonical({k: sorted(v) for k, v in pools.items()})).hexdigest())


def select_common_complete_month(snapshots, universe, revenue_coverage_threshold=0.98,
                                 yoy_coverage_threshold=0.98, max_period_lag_months=2, now=None):
    if not 0 <= revenue_coverage_threshold <= 1 or not 0 <= yoy_coverage_threshold <= 1:
        raise ValueError('invalid_threshold')
    if not isinstance(max_period_lag_months, int) or max_period_lag_months < 0:
        raise ValueError('invalid_max_period_lag')
    now = now or datetime.now(ZoneInfo('Asia/Taipei'))
    if isinstance(now, datetime) and now.tzinfo:
        now = now.astimezone(ZoneInfo('Asia/Taipei'))
    latest, invalid = {}, []
    for snapshot in snapshots:
        try:
            verify(snapshot)
        except (ValueError, KeyError, TypeError) as exc:
            invalid.append(str(exc))
            continue
        key = (snapshot['market'], snapshot['period'])
        stamp = datetime.fromisoformat(snapshot['fetched_at'])
        if key not in latest or stamp > datetime.fromisoformat(latest[key]['fetched_at']):
            latest[key] = snapshot
        elif stamp == datetime.fromisoformat(latest[key]['fetched_at']) and snapshot['content_sha256'] != latest[key]['content_sha256']:
            raise ValueError('ambiguous_same_time_versions')
    audits = {key: audit_month(s, universe, revenue_coverage_threshold, yoy_coverage_threshold) for key, s in latest.items()}
    common = sorted({p for m, p in audits if all((x, p) in audits and audits[x, p]['revenue_coverage_pass'] for x in ('TWSE', 'TPEX'))}, reverse=True)
    base = dict(status='unavailable', selected_period=None, monthly_revenue_yoy_eligible=False,
                audits=list(audits.values()), invalid_snapshots=invalid, selected_snapshots={})
    for period in common:
        year, month = map(int, period.split('-'))
        lag = (now.year-year)*12 + now.month-month
        if lag < 1:
            continue  # Current/future month is not a completed reporting period.
        if lag > max_period_lag_months:
            return dict(base, reason='common_month_too_old', candidate_period=period, period_lag_months=lag)
        return dict(base, status='available', reason='common_complete_month', selected_period=period,
                    period_lag_months=lag,
                    monthly_revenue_yoy_eligible=all(audits[m, period]['yoy_coverage_pass'] for m in ('TWSE', 'TPEX')),
                    selected_snapshots={m: latest[m, period] for m in ('TWSE', 'TPEX')})
    return dict(base, reason='no_common_complete_month')


def get_monthly_record(selection, ticker):
    """Return gated scoring fields and explicitly named raw audit values.

    Both legacy monthly_revenue_yoy_* and eligible_monthly_revenue_yoy_*
    are null when ineligible. raw_monthly_revenue_yoy_* are audit-only.
    Snapshot records are never mutated.
    """
    missing = dict(status='unavailable', display='N/A', monthly_revenue=None,
                   monthly_revenue_yoy_pct=None, monthly_revenue_yoy_ratio=None,
                   raw_monthly_revenue_yoy_pct=None, raw_monthly_revenue_yoy_ratio=None,
                   eligible_monthly_revenue_yoy_pct=None, eligible_monthly_revenue_yoy_ratio=None,
                   monthly_revenue_yoy_eligible=False, monthly_revenue_yoy_display='N/A',
                   period=selection.get('selected_period'))
    if selection['status'] != 'available':
        return dict(missing, reason=selection['reason'])
    market = 'TPEX' if ticker.endswith('.TWO') else 'TWSE' if ticker.endswith('.TW') else None
    if not market:
        return dict(missing, reason='invalid_ticker')
    code = ticker.rsplit('.', 1)[0]
    row = next((r for r in selection['selected_snapshots'][market]['records'] if r['code'] == code), None)
    if row is None:
        return dict(missing, reason='missing_in_common_month')
    raw_pct = row.get('monthly_revenue_yoy_pct')
    raw_ratio = row.get('monthly_revenue_yoy_ratio')
    eligible = selection['monthly_revenue_yoy_eligible'] and finite(raw_pct)
    score_pct = raw_pct if eligible else None
    score_ratio = raw_ratio if eligible and finite(raw_ratio) else None
    return dict(row, status='available' if finite(row.get('monthly_revenue')) else 'unavailable',
                display=None if finite(row.get('monthly_revenue')) else 'N/A',
                monthly_revenue_yoy_eligible=eligible,
                raw_monthly_revenue_yoy_pct=raw_pct, raw_monthly_revenue_yoy_ratio=raw_ratio,
                monthly_revenue_yoy_pct=score_pct, monthly_revenue_yoy_ratio=score_ratio,
                eligible_monthly_revenue_yoy_pct=score_pct, eligible_monthly_revenue_yoy_ratio=score_ratio,
                monthly_revenue_yoy_display=None if eligible else 'N/A')
