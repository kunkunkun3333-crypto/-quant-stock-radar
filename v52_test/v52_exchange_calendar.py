"""只辨識有雙交易所月報證據的孤立休市空列；不產生行情或改動回測。"""
import json
import math
import threading
import time
from datetime import date
from urllib.request import Request, urlopen
import numpy as np
import pandas as pd

_LOCK = threading.RLock()
_MONTHS = {}  # 僅官方月報查詢的程序內記憶，不讀寫行情 cache/checkpoint。


def _url(market, year, month):
    if market == 'TWSE':
        return f'https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={year:04d}{month:02d}01&response=json'
    return f'https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingIndex?date={year:04d}/{month:02d}/01&response=json'


def _parse(payload, market, year, month):
    if str(payload.get('stat', '')).lower() != 'ok':
        raise ValueError('官方月報未成功')
    if market == 'TWSE':
        table = payload
        if '發行量加權股價指數' not in table.get('title', ''):
            raise ValueError('非加權指數月報')
        count = table.get('total')
        value_col = 4
    else:
        tables = [t for t in payload.get('tables', [])
                  if '櫃買指數' in t.get('fields', [])]
        if len(tables) != 1:
            raise ValueError('無法識別櫃買指數月報')
        table = tables[0]
        count = table.get('totalCount')
        value_col = table['fields'].index('櫃買指數')
    rows = table.get('data', [])
    if not 5 <= len(rows) <= 31 or int(count) != len(rows):
        raise ValueError('月報筆數不完整')
    if not table.get('fields') or table['fields'][0] != '日期':
        raise ValueError('日期欄位不符')
    days = []
    for row in rows:
        y, m, d = map(int, row[0].split('/'))
        day = date(y + 1911 if y < 1911 else y, m, d)
        if (day.year, day.month) != (year, month):
            raise ValueError('月報日期錯置')
        value = float(str(row[value_col]).replace(',', ''))
        if not math.isfinite(value) or value <= 0:
            raise ValueError('官方指數資料不完整')
        days.append(day)
    if days != sorted(set(days)):
        raise ValueError('官方日期重複或未排序')
    return frozenset(days)


def _month(year, month):
    key = (year, month)
    with _LOCK:
        cached = _MONTHS.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        try:
            records = []
            for market in ('TWSE', 'TPEX'):
                url = _url(market, year, month)
                with urlopen(Request(url, headers={'User-Agent': 'QuantStockRadar/5.2'}), timeout=8) as response:
                    payload = json.loads(response.read(1_000_001))
                records.append((_parse(payload, market, year, month), url))
            result = tuple(records)
        except Exception:
            # 查詢失敗絕不等同休市；短暫記憶失敗，避免逐檔重複打官方 API。
            result = None
        _MONTHS[key] = (time.monotonic() + (86400 if result else 60), result)
        return result


def closure_evidence(day, previous, following):
    """True 僅來自完整月報的區間內缺席，並要求兩市場前後交易日吻合。"""
    if not (previous < day < following):
        return None
    if any((d.year, d.month) != (day.year, day.month) for d in (previous, following)):
        return None  # 跨月端點無法在單月核實，不猜測。
    records = _month(day.year, day.month)
    if not records:
        return None
    for days, _ in records:
        if day in days or previous not in days or following not in days:
            return None
        if any(previous < d < following for d in days):
            return None  # Yahoo 鄰列間還漏了真實交易日。
    return {'date': day.isoformat(), 'previous_session': previous.isoformat(),
            'next_session': following.isoformat(),
            'sources': [url for _, url in records],
            'verified_at': pd.Timestamp.now(tz='UTC').isoformat()}


def remove_confirmed_closure_blanks(h, ticker):
    if not (ticker == '^TWII' or ticker.endswith(('.TW', '.TWO'))):
        return h
    cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    if not all(c in h.columns for c in cols):
        return h  # 缺少欄位不等於原始 OHLCV 全空。
    empty = h[cols].isna().all(axis=1).to_numpy()
    values = h[cols[:4]].apply(pd.to_numeric, errors='coerce').to_numpy()
    bad = (~np.isfinite(values) | (values <= 0)).any(axis=1)
    # 不因一列休市空列而掩蓋多筆近期缺損。
    if int(bad[-60:].sum()) > 1:
        return h
    removed = []
    for i in np.flatnonzero(empty):
        if i == 0 or i == len(h) - 1 or bad[i-1] or bad[i+1]:
            continue
        evidence = closure_evidence(h.index[i].date(), h.index[i-1].date(), h.index[i+1].date())
        if evidence:
            removed.append(evidence)
    if not removed:
        return h
    dates = [r['date'] for r in removed]
    out = h.loc[~h.index.strftime('%Y-%m-%d').isin(dates)].copy()
    prior = out.attrs.get('confirmed_closure_bars', [])
    out.attrs['confirmed_closure_bars'] = prior + [r for r in removed if r not in prior]
    warning = 'Data Quality Warning：移除已確認休市日空白行情列：' + ', '.join(dates) + '；未補值'
    out.attrs['closure_bar_warning'] = warning
    # 既有 full/incremental 清洗會重新建立 data_quality_warning，沿用其提示合併通道。
    out.attrs['latest_bar_warning'] = '；'.join(dict.fromkeys(
        x for x in (out.attrs.get('latest_bar_warning', ''), warning) if x))
    return out
