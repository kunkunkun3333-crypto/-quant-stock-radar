"""Observation only. No requests, retries, cache writes or credential logging."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

_CURRENT = ContextVar('v53_fundamental_diagnostic', default=None)


def _emit(record):
    try:
        print('V53_FUND_DIAG ' + json.dumps(record, ensure_ascii=False,
              allow_nan=False, separators=(',', ':')), file=sys.stderr, flush=True)
    except Exception:
        pass


def begin(ticker):
    try:
        record = {'event': 'summary', 'ticker': ticker, 'call_id': uuid4().hex,
                  'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                  'cache_status': None, 'loader_called': False,
                  'stale_fallback_used': False, 'loader_exception_type': None,
                  'final_exception_type': None, 'http_request_count': 0}
        state = {'record': record, 'start': time.perf_counter()}
        state['token'] = _CURRENT.set(state)
        return state
    except Exception:
        return None


def loader_event(state, exc=None):
    try:
        state['record']['loader_called'] = True
        if exc is not None:
            state['record']['loader_exception_type'] = type(exc).__name__
    except Exception:
        pass


def finish(state, cached=None, exc=None):
    try:
        r = state['record']
        status = getattr(cached, 'status', None)
        r['cache_status'] = status if status in ('fresh', 'stale', 'downloaded') else None
        r['stale_fallback_used'] = status == 'stale'
        r['final_exception_type'] = type(exc).__name__ if exc is not None else None
        r['outcome'] = 'failure' if exc is not None else 'success'
        r['elapsed_ms'] = round((time.perf_counter() - state['start']) * 1000, 3)
        _emit(r)
    except Exception:
        pass
    finally:
        try:
            _CURRENT.reset(state['token'])
        except Exception:
            pass


def _endpoint(url):
    # Output only static templates; never output arbitrary path/query/userinfo.
    p = urlsplit(url)
    host = p.hostname or ''
    allowed = {'query1.finance.yahoo.com', 'query2.finance.yahoo.com',
               'fc.yahoo.com', 'consent.yahoo.com', 'guce.yahoo.com'}
    if host not in allowed:
        return 'other', 'other', 'other'
    for marker, label, template in (
        ('/v10/finance/quoteSummary/', 'quoteSummary', '/v10/finance/quoteSummary/{ticker}'),
        ('/v7/finance/quote', 'quote', '/v7/finance/quote'),
        ('/ws/fundamentals-timeseries/', 'fundamentals-timeseries', '/ws/fundamentals-timeseries/v1/finance/timeseries/{ticker}'),
        ('/v1/test/getcrumb', 'crumb', '/v1/test/getcrumb'),
    ):
        if p.path.startswith(marker):
            return label, host, template
    if host == 'fc.yahoo.com':
        return 'cookie', host, '/'
    if host in ('consent.yahoo.com', 'guce.yahoo.com'):
        return 'consent', host, '/{redacted}'
    return 'other', host, '/{redacted}'


def request_begin(url):
    try:
        state = _CURRENT.get()
        if state is None:
            return None
        r = state['record']
        r['http_request_count'] += 1
        kind, host, path = _endpoint(url)
        return {'event': 'http', 'ticker': r['ticker'], 'call_id': r['call_id'],
                'request_seq': r['http_request_count'], 'endpoint_type': kind,
                'host': host, 'path_template': path, '_start': time.perf_counter()}
    except Exception:
        return None


def _category(response, status):
    if status == 429:
        return 'RateLimited'
    if status != 401:
        return None
    # Classify in memory only. Never emit descriptions or response bodies.
    try:
        if len(response.content) <= 16384:
            body = response.json()
            for key in ('finance', 'quoteSummary', 'timeseries', 'quoteResponse'):
                error = (body.get(key) or {}).get('error') or {}
                if error.get('description') == 'Invalid Crumb':
                    return 'InvalidCrumb'
    except Exception:
        pass
    return 'Unauthorized'


def request_end(event, response=None, exc=None):
    try:
        if event is None:
            return
        status = getattr(response, 'status_code', None)
        status = status if isinstance(status, int) and 100 <= status <= 599 else None
        event['request_elapsed_ms'] = round((time.perf_counter() - event.pop('_start')) * 1000, 3)
        event['http_status'] = status
        event['exception_type'] = type(exc).__name__ if exc is not None else None
        event['yahoo_error_category'] = _category(response, status)
        _emit(event)
    except Exception:
        pass
