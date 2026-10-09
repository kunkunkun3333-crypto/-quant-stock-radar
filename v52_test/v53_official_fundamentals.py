"""Official Fundamentals Layer v1: six batch sources; no automatic retry or integration.

refresh_official_fundamentals(cache_dir=None) uses memory only. An explicit
cache_dir enables isolated JSON snapshots. get_company_fundamentals never
performs I/O. Fetch TTL is independent of the underlying reporting period.
"""
from copy import deepcopy
import json
import time
from threading import RLock
from urllib.request import build_opener, HTTPRedirectHandler, Request
from urllib.error import HTTPError

try:
    from .v53_fundamental_normalize import SOURCES, normalize_source
    from .v53_fundamental_snapshot import (load_snapshot, save_snapshot, classify_snapshot,
                                          as_datetime, utc_now, SCHEMA_VERSION)
except ImportError:
    from v53_fundamental_normalize import SOURCES, normalize_source
    from v53_fundamental_snapshot import (load_snapshot, save_snapshot, classify_snapshot,
                                         as_datetime, utc_now, SCHEMA_VERSION)

# Independent official-data TTLs; no existing cache configuration is imported.
TTL_SECONDS = {source_id: 86400 for source_id in SOURCES}
# Maximum total snapshot age since fetched_at, independent of fresh TTL.
FALLBACK_MAX_AGE_SECONDS = {
    source_id: {'pe': 7, 'monthly_revenue': 45, 'eps_summary': 180}[dataset] * 86400
    for source_id, (_, dataset, _) in SOURCES.items()
}
_REFRESH_LOCK = RLock()


class _NoRedirect(HTTPRedirectHandler):
    # Redirects are surfaced as failures, keeping the six-request upper bound.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_source(source_id, *, timeout_s=25):
    market, dataset, url = SOURCES[source_id]
    start = time.monotonic()
    http_status = None
    try:
        request = Request(url, headers={'Accept': 'application/json'})
        with build_opener(_NoRedirect()).open(request, timeout=timeout_s) as response:
            http_status = response.status
            rows = json.loads(response.read().decode('utf-8-sig'))
        if not isinstance(rows, list) or not rows:
            raise ValueError('empty_or_invalid_dataset')
        return {'ok': True, 'raw_rows': rows, 'http_status': http_status,
                'http_request_count': 1, 'elapsed_ms': (time.monotonic() - start) * 1000,
                'fetched_at': utc_now().isoformat(), 'error': None}
    except Exception as error:
        if isinstance(error, HTTPError):
            http_status = error.code
        return {'ok': False, 'raw_rows': None, 'http_status': http_status,
                'http_request_count': 1, 'elapsed_ms': (time.monotonic() - start) * 1000,
                'error': {'type': type(error).__name__, 'reason': 'official_source_fetch_failed'}}


def refresh_official_fundamentals(*, cache_dir=None, force_refresh=False, now=None):
    """Return each source independently. All lookups are local after this call.

    now is an optional timezone-aware clock for deterministic acceptance tests.
    Cache failures never discard successfully downloaded data.
    """
    with _REFRESH_LOCK:
        return _refresh(cache_dir=cache_dir, force_refresh=force_refresh, now=now)


def _refresh(*, cache_dir, force_refresh, now):
    result = {'schema_version': SCHEMA_VERSION, 'sources': {}, 'http_request_count': 0}
    for source_id, (market, dataset, endpoint) in SOURCES.items():
        attempt = as_datetime(now or utc_now()).isoformat()
        warnings = []
        try:
            old = load_snapshot(source_id, cache_dir=cache_dir)
        except Exception as error:
            old = None
            warnings.append({'type': 'snapshot_read_failed', 'exception_type': type(error).__name__})
        if not force_refresh and classify_snapshot(old, now=attempt, max_age_seconds=TTL_SECONDS[source_id]) == 'fresh_cache':
            output = deepcopy(old)
            output.update(status='fresh_cache', last_attempt_at=attempt, error=None,
                          http_request_count=0, http_status=None, elapsed_ms=0)
            result['sources'][source_id] = output
            continue
        fetch = {'http_request_count': 0, 'http_status': None, 'elapsed_ms': 0}
        try:
            fetch = fetch_source(source_id)
            if not fetch['ok']:
                raise _FetchFailure(fetch['error'])
            normalized = normalize_source(source_id, fetch['raw_rows'])
            fetched_at = attempt if now is not None else fetch['fetched_at']
            for row in normalized['records'].values():
                row['fetched_at'] = fetched_at
            output = dict(normalized, schema_version=SCHEMA_VERSION, source_id=source_id,
                          source={'institution': market, 'source_id': source_id, 'endpoint': endpoint},
                          market=market, dataset=dataset, fetched_at=fetched_at,
                          last_attempt_at=attempt, status='downloaded', error=None)
            output['warnings'].extend(warnings)
            try:
                saved = save_snapshot(source_id, output, cache_dir=cache_dir)
                if saved['error']:
                    output['warnings'].append({'type': 'snapshot_write_failed', 'reason': saved['error']})
            except Exception as error:
                output['warnings'].append({'type': 'snapshot_write_failed', 'reason': type(error).__name__})
        except Exception as error:
            reason = error.error if isinstance(error, _FetchFailure) else {'type': type(error).__name__, 'reason': str(error)}
            fallback_allowed = False
            if old:
                try:
                    age = (as_datetime(now or utc_now()) - as_datetime(old['fetched_at'])).total_seconds()
                    fallback_allowed = 0 <= age <= FALLBACK_MAX_AGE_SECONDS[source_id]
                except (KeyError, TypeError, ValueError):
                    pass
                if not fallback_allowed:
                    warnings.append({'type': 'snapshot_fallback_age_exceeded_or_invalid'})
            output = deepcopy(old) if fallback_allowed else {
                'schema_version': SCHEMA_VERSION, 'source_id': source_id,
                'source': {'institution': market, 'source_id': source_id, 'endpoint': endpoint},
                'market': market, 'dataset': dataset, 'records': {}, 'raw_rows': [],
                'row_count': 0, 'normalized_count': 0,
                'fetched_at': old.get('fetched_at') if old else None, 'warnings': []}
            output.update(status='stale_fallback' if fallback_allowed else 'unavailable',
                          last_attempt_at=attempt, error=reason)
            output['warnings'].extend(warnings)
        output.update(http_request_count=fetch.get('http_request_count', 0),
                      http_status=fetch.get('http_status'), elapsed_ms=fetch.get('elapsed_ms', 0))
        result['http_request_count'] += output['http_request_count']
        result['sources'][source_id] = output
    return result


class _FetchFailure(Exception):
    def __init__(self, error):
        super().__init__('official_source_fetch_failed')
        self.error = error


def get_company_fundamentals(refresh_result, *, market, code):
    market = str(market).upper()
    if market not in ('TWSE', 'TPEX'):
        raise ValueError('market_must_be_TWSE_or_TPEX')
    code = str(code).strip()
    output = {'market': market, 'code': code, 'datasets': {}}
    for source_id, (source_market, dataset, _) in SOURCES.items():
        if source_market != market:
            continue
        source = refresh_result.get('sources', {}).get(source_id, {})
        record = source.get('records', {}).get(code)
        output['datasets'][dataset] = {
            'source_id': source_id, 'source_status': source.get('status', 'unavailable'),
            'status': record['status'] if record else 'unavailable',
            'record': deepcopy(record), 'error': deepcopy(source.get('error')),
            'reason': None if record else 'company_not_present_in_available_snapshot',
        }
    return output
